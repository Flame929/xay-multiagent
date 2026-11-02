"""适配器共同部分：事件、能力声明、抽象接口、脱敏、错误分类。

一个适配器 = 一个员工背后的一段原生对话（Codex thread / dsh session / agy conversation）。
工位通过 ``open / send / steer / interrupt / compact / close`` 控制它，通过 ``events()`` 读它发出的
统一事件（``AdapterEvent``）。所有文字在进入事件之前都经过 ``redact``。

事件种类（kind -> data）：
    session            {native_id, ...}              原生对话号（codex 还带 provider/model/effort）
    turn_started       {native_turn}
    thinking           {text}                        思考文字片段（有的家看不到）
    text               {text}                        回复文字片段（把同一轮的片段拼起来就是回复）
    tool               {kind: read|write|cmd|search|web|mcp|other, text}
    tool_output        {text, ok?}
    usage              {used, window, pct, estimated, source}   记忆条（pct 是百分数 0~100）
    plan               {steps: [{text, status}]}     status: pending | in_progress | completed
    compaction_started {…}
    compacted          {before?, after?, …}          before/after 是压缩前/后的 pct
    turn_ended         {status: completed|failed|interrupted, final_text,
                        error_kind?: capacity|quota|rate_limit|context_overflow|auth|network|config|other, error?, usage?}
    usage 事件另可带 turn_total：这一轮到现在累计用了多少 token（in+out+cached，和 turn_ended.usage.total 同口径）
    status             {state}                       idle | working | compacting | offline
    rate_limits        {line, windows:[{key: 5h|week|<n>m, mins, used_pct, resets_at?}], reached?}
                                                     额度（只有 Codex：app-server 的 account/rateLimits/updated，已合并稀疏更新；
                                                     中转站两个窗口都是 null → windows 为空）
    error              {kind, text}                  kind 另有 resume_failed（agy 冷续接没接上、dsh 会话号不认）
                                                     和 config（dsh 补丁没生效，拒绝开工）
"""

from __future__ import annotations

import abc
import os
import queue
import re
import signal
import subprocess
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Dict, Iterator, List, Optional

EVENT_KINDS = (
    "session", "turn_started", "thinking", "text", "tool", "tool_output", "usage", "plan",
    "compaction_started", "compacted", "turn_ended", "status", "error",
    "rate_limits",
)
TOOL_KINDS = ("read", "write", "cmd", "search", "web", "mcp", "other")
ERROR_KINDS = ("capacity", "quota", "rate_limit", "context_overflow", "auth", "network", "resume_failed", "config", "other")
STATES = ("idle", "working", "compacting", "offline")

MAX_TOOL_TEXT = 2000
MAX_OUTPUT_LINES = 40
MAX_OUTPUT_CHARS = 4000

_SENTINEL = object()


class AdapterError(RuntimeError):
    """适配器无法完成请求（进程没起来、协议报错、已关闭……）。"""


class AdapterBusy(AdapterError):
    """这个员工正在做上一轮，不能再开新的一轮。"""


class AdapterConfigError(AdapterError):
    """CLI 的设置不对、硬性约束可能失效（比如 dsh 补丁里的条目没找到、模型不是规定的那个）：拒绝开工。"""


# ---------------------------------------------------------------------------
# 脱敏（移植自 scripts/xay_normalize.redact，多补了几种常见 token 形态）
# ---------------------------------------------------------------------------

_SECRET_PATTERNS = (
    (re.compile(r"(?i)\b(API[_-]?KEY|TOKEN|SECRET|PASSWORD)\s*=\s*\S+"), r"\1=<redacted>"),
    (re.compile(r"(?i)\bAuthorization:[ \t]*[^\r\n]+"), "Authorization: <redacted>"),   # 整行，含 Bearer xxx
    (re.compile(r"(?i)\bBearer\s+\S+"), "Bearer <redacted>"),
    (re.compile(r'(?i)"(api_key|token|secret|password)"\s*:\s*"[^"]+"'), r'"\1":"<redacted>"'),
    (re.compile(r"\bsk-[A-Za-z0-9_\-]{8,}"), "<redacted>"),          # 旧版只认字母数字，sk-proj-… 会漏
    (re.compile(r"\bgh[pousr]_[A-Za-z0-9]{20,}"), "<redacted>"),      # GitHub token
    (re.compile(r"\bAKIA[0-9A-Z]{16}\b"), "<redacted>"),              # AWS access key id
    (re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----.*?-----END [A-Z ]*PRIVATE KEY-----", re.S),
     "<redacted-private-key>"),
)
# 这些键的值是编号 / 枚举，不会是密钥，不脱敏（避免误伤 id）
_NO_REDACT_KEYS = frozenset({"native_id", "native_turn", "kind", "status", "state", "error_kind",
                             "source", "provider", "model", "effort", "method"})
_SECRET_KEYS = frozenset({"api_key", "apikey", "token", "secret", "password"})


def redact(text: Any) -> str:
    value = "" if text is None else (text if isinstance(text, str) else str(text))
    if not value:
        return value
    for pattern, replacement in _SECRET_PATTERNS:
        value = pattern.sub(replacement, value)
    return value


def redact_data(value: Any, depth: int = 0) -> Any:
    """递归脱敏事件 data 里的字符串（键是 api_key/token/… 的值整个替换）。"""
    if depth > 8:
        return value
    if isinstance(value, str):
        return redact(value)
    if isinstance(value, (list, tuple)):
        return [redact_data(v, depth + 1) for v in value]
    if isinstance(value, dict):
        out = {}
        for key, item in value.items():
            lowered = str(key).lower()
            if lowered in _SECRET_KEYS:
                out[key] = "<redacted>"
            elif lowered in _NO_REDACT_KEYS and isinstance(item, str):
                out[key] = item
            else:
                out[key] = redact_data(item, depth + 1)
        return out
    return value


def clip(text: Any, limit: int = MAX_TOOL_TEXT) -> str:
    value = "" if text is None else str(text)
    return value if len(value) <= limit else value[:limit] + "…"


def clip_tail(text: Any, max_lines: int = MAX_OUTPUT_LINES, max_chars: int = MAX_OUTPUT_CHARS) -> str:
    """命令输出只留最后若干行、若干字符。"""
    value = "" if text is None else str(text)
    value = value.replace("\r\n", "\n").replace("\r", "\n")
    rows = value.split("\n")
    while rows and not rows[-1]:
        rows.pop()
    value = "\n".join(rows[-max_lines:])
    return value if len(value) <= max_chars else "…" + value[-max_chars:]


def strip_shell(command: Any) -> str:
    """去掉 `/bin/zsh -lc '…'` 包装（Codex 的 commandExecution 带着它）。"""
    if isinstance(command, (list, tuple)):
        parts = [str(p) for p in command]
        if len(parts) >= 3 and parts[1] in ("-lc", "-c"):
            return strip_shell(parts[2])
        command = " ".join(parts)
    command = "" if command is None else str(command)
    for prefix in ("/bin/zsh -lc ", "/bin/bash -lc ", "bash -lc ", "zsh -lc "):
        if command.startswith(prefix):
            command = command[len(prefix):]
            break
    else:
        return command
    if len(command) >= 2 and command[0] == command[-1] == "'":
        command = command[1:-1].replace("'\\''", "'")
    elif len(command) >= 2 and command[0] == '"' and command[-1] in "\"'":
        command = command[1:-1].replace('\\"', '"').replace("\\\\", "\\")
    return command


# ---------------------------------------------------------------------------
# 进程
# ---------------------------------------------------------------------------

def signal_group(proc: "subprocess.Popen", sig: int) -> None:
    """给整个进程组发信号（子进程是 start_new_session 起的，自己就是组长）；失败就只发给它本身。"""
    try:
        os.killpg(proc.pid, sig)
    except (ProcessLookupError, PermissionError, OSError):
        try:
            proc.send_signal(sig)
        except (ProcessLookupError, OSError):
            pass


def close_pipe(stream) -> None:
    """关一个管道（已经关了 / 进程没有这个管道都不算错）。"""
    try:
        if stream is not None:
            stream.close()
    except (OSError, ValueError):
        pass


def stop_process(proc: Optional["subprocess.Popen"], grace: float = 3.0,
                 first: int = signal.SIGTERM) -> None:
    """先礼后兵：first 信号 -> 等 grace 秒 -> SIGKILL（都发给整个进程组）。"""
    if proc is None or proc.poll() is not None:
        return
    signal_group(proc, first)
    try:
        proc.wait(timeout=grace)
        return
    except subprocess.TimeoutExpired:
        pass
    signal_group(proc, signal.SIGKILL)
    try:
        proc.wait(timeout=3)
    except subprocess.TimeoutExpired:
        pass


# ---------------------------------------------------------------------------
# 错误分类
# ---------------------------------------------------------------------------

_CONTEXT_RE = re.compile(
    r"context[_ ](length|window)|maximum context|context_length_exceeded|exceeds? the (context|token)"
    r"|prompt is too long|too many tokens|contextWindowExceeded|上下文(超|太长|过长)", re.I)
# 注意顺序：先判额度再判限流、容量。Gemini 的 "429 RESOURCE_EXHAUSTED … exhausted your capacity … quota" 是额度，
# "429 Too Many Requests" / "rate limit" 是限流（等一会儿就好），Codex 的 "Selected model is at capacity" 是容量（临时满载）。
_QUOTA_RE = re.compile(
    r"usage\s*limit|usageLimitExceeded|DAILY_LIMIT_EXCEEDED|quota|RESOURCE_EXHAUSTED"
    r"|exhausted your|insufficient[ _](credits|balance|funds)"
    r"|credit balance|billing|payment required|\b402\b|(usage|token) limit (reached|exceeded)"
    r"|limit_exceeded|额度|余额不足", re.I)
# 账号本身不能用（地区不支持、没有资格）：换号能解决，和额度一样由工位补救（agy 换号）。2026-10-31 真机：
# "Eligibility check failed: Your current account is not eligible for Antigravity, because it is not currently available in …"
_INELIGIBLE_RE = re.compile(
    r"not eligible|eligibility check failed|not (currently )?available in your (country|region|location)"
    r"|unsupported (country|region|location)|账号(不可用|没有资格)", re.I)
_RATE_RE = re.compile(r"rate.?limit|too many requests|\b429\b|RATE_LIMITED|request limit (reached|exceeded)|限流|请求太频繁", re.I)
_CAPACITY_RE = re.compile(
    r"at capacity|overloaded|server.?overload|serverOverloaded|\bcapacity\b|\b503\b"
    r"|service unavailable|temporarily unavailable", re.I)
_AUTH_RE = re.compile(
    r"\b401\b|\b403\b|unauthori[sz]ed|forbidden|invalid[ _](api[ _-]?key|token|credentials?)|authenticat"
    r"|not (logged|signed) in|log ?in required|please (log|sign) in|token (has )?expired|认证失败|未登录|请先登录", re.I)
_NETWORK_RE = re.compile(
    r"ECONNRESET|ECONNREFUSED|ETIMEDOUT|ENOTFOUND|EAI_AGAIN|ENETUNREACH|getaddrinfo|socket hang up|fetch failed"
    r"|connection (reset|refused|closed|error|aborted)|connection was reset|network (error|is unreachable)"
    r"|timed? ?out|broken pipe|\bEOF\b|网络", re.I)

_CODE_KIND = {
    "contextWindowExceeded": "context_overflow",
    "usageLimitExceeded": "quota",
    "rateLimitExceeded": "rate_limit",
    "serverOverloaded": "capacity",
    "unauthorized": "auth",
}

# 额度类：工位会补救（Codex 换线路、agy 换号），反应表按"额度原因失败"处理
QUOTA_LIKE = ("capacity", "quota", "rate_limit", "ineligible")


def classify_error(text: Any = "", code: Any = None) -> str:
    """把各家的错误文字 / 结构化错误码归成 context_overflow | ineligible | quota | rate_limit | capacity | auth | network | other。"""
    if isinstance(code, str) and code in _CODE_KIND:
        return _CODE_KIND[code]
    blob = "" if text is None else str(text)
    if isinstance(code, str):
        blob = code + " " + blob
    if _CONTEXT_RE.search(blob):
        return "context_overflow"
    if _INELIGIBLE_RE.search(blob):
        return "ineligible"
    if _QUOTA_RE.search(blob):
        return "quota"
    if _RATE_RE.search(blob):
        return "rate_limit"
    if _CAPACITY_RE.search(blob):
        return "capacity"
    if _AUTH_RE.search(blob):
        return "auth"
    if _NETWORK_RE.search(blob):
        return "network"
    return "other"


# 各家工具名 -> 统一的 tool kind
def tool_kind(name: Any) -> str:
    text = str(name or "").lower()
    if text in ("bash", "shell", "cmd", "command", "run_command", "commandexecution", "exec_command"):
        return "cmd"
    if text in ("read", "view", "read_file", "view_file", "cat"):
        return "read"
    if text in ("write", "edit", "patch", "multi_edit", "write_to_file", "replace_file_content",
                "multi_replace_file_content", "sed_file", "notebook_edit", "filechange", "apply_patch"):
        return "write"
    if text in ("grep", "rg", "search", "glob", "find", "grep_search", "find_by_name", "list_dir", "ls"):
        return "search"
    if text in ("webfetch", "web_fetch", "websearch", "web_search", "search_web", "read_url_content") \
            or text.startswith("browser_"):
        return "web"
    if text in ("call_mcp_tool", "mcptoolcall") or text.startswith("mcp"):
        return "mcp"
    return "other"


# ---------------------------------------------------------------------------
# 数据结构
# ---------------------------------------------------------------------------

@dataclass
class Capabilities:
    """这家适配器能做什么（页面和工位按"能力"判断，不按名字判断）。"""

    steer: str = "none"              # live：当场插话；none：不支持
    interrupt: str = "kill"          # soft：礼貌打断；kill：只能杀进程
    resume: bool = True              # 能用原生对话号续接
    native_compact: bool = False     # 自带压缩可调
    usage: str = "step"              # live / step / turn：记忆条更新频率
    thinking: bool = False           # 看得到思考文字
    plan: bool = False               # 有自带计划步骤
    persistent_process: bool = False # 一个进程跨多轮
    cli_version: str = ""

    def as_dict(self) -> dict:
        return {
            "steer": self.steer, "interrupt": self.interrupt, "resume": self.resume,
            "native_compact": self.native_compact, "usage": self.usage, "thinking": self.thinking,
            "plan": self.plan, "persistent_process": self.persistent_process,
            "cli_version": self.cli_version,
        }


@dataclass
class AdapterEvent:
    kind: str
    data: Dict[str, Any] = field(default_factory=dict)
    ts: float = field(default_factory=time.time)

    def as_dict(self) -> dict:
        return {"kind": self.kind, "data": dict(self.data), "ts": self.ts}


def compose_prompt(text: str, extra_context: Any = None) -> str:
    """没有专门通道的 AI（dsh / agy）：把额外背景放在任务文字前面，用 <context> 包起来。"""
    extra = context_entries(extra_context)
    if not extra:
        return str(text)
    body = "\n\n".join(value for _, value in extra)
    return "<context>\n%s\n</context>\n\n%s" % (body, text)


def context_entries(extra_context: Any) -> List[tuple]:
    """extra_context 可以是字符串，或 {来源名: 文字}；统一成 [(来源名, 文字)]，丢掉空的。"""
    if not extra_context:
        return []
    if isinstance(extra_context, str):
        return [("xay", extra_context)] if extra_context.strip() else []
    if isinstance(extra_context, dict):
        return [(str(k), str(v)) for k, v in extra_context.items() if str(v).strip()]
    raise TypeError("extra_context 要么是字符串，要么是 {来源: 文字}")


# ---------------------------------------------------------------------------
# 抽象接口
# ---------------------------------------------------------------------------

class Adapter(abc.ABC):
    """一个员工的原生对话。线程安全：send/steer/interrupt/compact/close 可以从别的线程调用，
    ``events()`` 由工位的读取线程消费。"""

    agent = "?"

    def __init__(self) -> None:
        self.capabilities = Capabilities()
        self.native_id: Optional[str] = None
        self.cwd: Optional[str] = None
        self.card: Dict[str, Any] = {}
        self.last_usage: Optional[Dict[str, Any]] = None
        self._q: "queue.Queue[Any]" = queue.Queue()
        self._lock = threading.RLock()
        self._opened = False
        self._closed = False
        self._state: Optional[str] = None

    # ---- 子类实现 ----------------------------------------------------

    @abc.abstractmethod
    def open(self, cwd: str, native_id: Optional[str] = None, card: Optional[dict] = None) -> None:
        """开始（native_id=None）或续接（给了 native_id）这段原生对话，工作目录固定为 cwd。"""

    @abc.abstractmethod
    def send(self, text: str, extra_context: Any = None) -> None:
        """开始一轮。立即返回；进展从 events() 来。正忙时抛 AdapterBusy。"""

    def steer(self, text: str) -> bool:
        """当场插话。不支持（或此刻没有可插话的一轮）返回 False。"""
        return False

    @abc.abstractmethod
    def interrupt(self) -> str:
        """打断当前这一轮。返回 "soft"（礼貌打断，进程还在）或 "kill"（杀掉了进程）。"""

    def compact(self) -> bool:
        """原生压缩。不支持返回 False（工位改走交接压缩）。"""
        return False

    @abc.abstractmethod
    def close(self) -> None:
        """关掉进程并结束 events()。可以重复调用。"""

    @abc.abstractmethod
    def alive(self) -> bool:
        """适配器还能用吗（打开了、没关、需要的进程还活着）。"""

    # ---- 事件 --------------------------------------------------------

    def emit(self, event_kind: str, /, **data: Any) -> AdapterEvent:
        """脱敏后放进队列。事件种类是位置参数，这样 data 里可以有名叫 kind 的键（tool / error 都有）。"""
        if event_kind not in EVENT_KINDS:
            raise ValueError("不认识的事件种类：%r" % event_kind)
        event = AdapterEvent(kind=event_kind, data=redact_data(data), ts=time.time())
        self._q.put(event)
        return event

    def set_state(self, state: str) -> None:
        """只在状态真的变了才发 status 事件。"""
        if state not in STATES:
            raise ValueError("不认识的状态：%r" % state)
        with self._lock:
            if state == self._state:
                return
            self._state = state
        self.emit("status", state=state)

    @property
    def state(self) -> Optional[str]:
        return self._state

    def emit_usage(self, used: int, window: int, estimated: bool, source: str,
                   turn_total: Optional[int] = None,
                   turn_out: Optional[int] = None,
                   turn_fresh: Optional[int] = None) -> AdapterEvent:
        pct = round(100.0 * used / window, 1) if window else 0.0
        info = {"used": int(used), "window": int(window), "pct": pct,
                "estimated": bool(estimated), "source": source}
        self.last_usage = dict(info)
        if turn_total is not None:
            info["turn_total"] = int(turn_total)     # 这一轮到现在累计用的 token（预算中途能管住）
        if turn_out is not None:
            info["turn_out"] = int(turn_out)         # 这一轮到现在写出的 token
        if turn_fresh is not None:
            info["turn_fresh"] = int(turn_fresh)     # 这一轮到现在新读的 token（输入减去缓存命中）
        return self.emit("usage", **info)

    def child_pids(self) -> List[int]:
        """这个适配器起的命令行进程（都是 start_new_session 起的，自己就是进程组组长；总闸要连组一起杀）。"""
        proc = getattr(self, "_proc", None)
        try:
            return [proc.pid] if proc is not None and proc.poll() is None else []
        except Exception:  # noqa: BLE001
            return []

    def events(self, timeout: Optional[float] = None) -> Iterator[AdapterEvent]:
        """阻塞迭代事件，直到 close() 之后队列读空。给了 timeout：超过这么久没新事件就结束迭代。"""
        while True:
            try:
                item = self._q.get(timeout=timeout)
            except queue.Empty:
                return
            if item is _SENTINEL:
                self._q.put(_SENTINEL)  # 让之后再调用 events() 也立刻结束
                return
            yield item

    def drain(self) -> List[AdapterEvent]:
        """不等待，取出现在队列里所有事件。"""
        out: List[AdapterEvent] = []
        while True:
            try:
                item = self._q.get_nowait()
            except queue.Empty:
                return out
            if item is _SENTINEL:
                self._q.put(_SENTINEL)
                return out
            out.append(item)

    def collect(self, until: Optional[str] = None, timeout: float = 10.0, count: int = 1,
                predicate=None) -> List[AdapterEvent]:
        """（测试 / 简单脚本用）读事件，直到第 count 个 kind==until（且满足 predicate）的事件为止。
        超时或适配器关闭就返回已读到的。"""
        deadline = time.monotonic() + timeout
        seen: List[AdapterEvent] = []
        hits = 0
        while True:
            left = deadline - time.monotonic()
            if left <= 0:
                return seen
            try:
                item = self._q.get(timeout=left)
            except queue.Empty:
                return seen
            if item is _SENTINEL:
                self._q.put(_SENTINEL)
                return seen
            seen.append(item)
            if until is not None and item.kind == until and (predicate is None or predicate(item)):
                hits += 1
                if hits >= count:
                    return seen

    def _finish_events(self) -> None:
        self._q.put(_SENTINEL)

    def _require_open(self) -> None:
        if not self._opened or self._closed:
            raise AdapterError("%s 适配器没有打开（或已经关闭）" % self.agent)
