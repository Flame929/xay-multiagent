#!/usr/bin/env python3
"""把四个执行通道的原始事件归一为 arch-v4 §3.4 的事件信封。

支持：
  * codex app-server（JSON-RPC 通知 / 服务端请求 / 响应，research/codex.md §2）
  * codex exec（`codex exec --json` 的 JSONL）
  * dsh headless（`dsh ... --headless` 的 JSONL）
  * agy stream-json（`agy -p ... --output-format stream-json`）
  * xay 自身的控制/决定/信号/笔记行（xay-agent 侧车，构造样本时使用）

约定：
  * 信封字段与 arch-v4 §3.4 完全一致；`session` 是负责人会话 `ls_…`，
    通道自己的会话 id 放 `data.native_session_id`（对应 Execution.native_session_id）。
  * 全局 `seq` 由看板服务在 tail 时分配；本模块默认写 `seq: null`，
    需要本地序号时传 `assign_seq=True`。
  * 所有 `*delta*` 通知只做必要聚合（推理摘要、最终回复、命令输出尾巴），
    绝不为单条 delta 生成事件；`NormWriter` 另有兜底过滤。
  * 命令输出只保留尾 5 行 + 哈希；文本按 §9.3 脱敏。
  * `to_action()` 复用 `scripts/xay_watch.py` 的 `humanize()`，
    输出 §3.8 规定的 `action.append` data（humanize 字段 + 定位字段），
    因此与看板 v2 的 Action 完全兼容。

只使用 Python 标准库。
"""

from __future__ import annotations

import hashlib
import importlib.util
import json
import os
import re
import sys
import time
from datetime import datetime, timezone

# ---------------------------------------------------------------------------
# 契约常量
# ---------------------------------------------------------------------------

ENVELOPE_FIELDS = (
    "seq", "ts", "ws", "session", "phase", "task", "key",
    "execution", "turn", "type", "kind", "path", "text", "data", "by",
)

# arch-v4 §3.4 的全部 type
EVENT_TYPES = (
    "session.started", "turn.started", "turn.completed", "reasoning", "message",
    "tool.call", "tool.result", "file.changed", "usage", "approval.requested",
    "question", "denied", "warning", "error", "control.requested",
    "control.acked", "control.failed", "signal", "decision.created",
    "decision.resolved", "note.updated",
)
_EVENT_TYPE_SET = frozenset(EVENT_TYPES)

# 工具种类（§3.4）
TOOL_KINDS = ("read", "write", "cmd", "search", "web", "mcp", "other")

# 各来源的别名
SOURCE_ALIASES = {
    "codex-app-server": "codex-app-server",
    "codex_app_server": "codex-app-server",
    "codexappserver": "codex-app-server",
    "app-server": "codex-app-server",
    "appserver": "codex-app-server",
    "codex-appserver": "codex-app-server",
    "codex-exec": "codex-exec",
    "codex_exec": "codex-exec",
    "codexexec": "codex-exec",
    "codex": "codex-auto",
    "codex-jsonl": "codex-exec",
    "dsh-headless": "dsh-headless",
    "dsh_headless": "dsh-headless",
    "dshheadless": "dsh-headless",
    "dsh": "dsh-headless",
    "dsh-jsonl": "dsh-headless",
    "agy-stream": "agy-stream",
    "agy_stream": "agy-stream",
    "agystream": "agy-stream",
    "agy": "agy-stream",
    "agy-jsonl": "agy-stream",
    "xay-control": "xay-control",
    "xay_control": "xay-control",
    "control": "xay-control",
    "agent": "xay-control",
    "sidecar": "xay-control",
    "auto": "auto",
    "": "auto",
}

_CODEX_EXEC_TYPES = frozenset({
    "thread.started", "turn.started", "turn.completed", "turn.failed",
    "item.started", "item.completed",
})
_DSH_TYPES = frozenset({
    "session", "status", "tool_call", "tool_result", "thinking", "text", "final",
})
_CONTROL_TYPES = frozenset({
    "control.requested", "control.acked", "control.failed", "signal",
    "decision.created", "decision.resolved", "note.updated", "warning", "error",
})

MAX_REASONING = 200
MAX_TAIL_LINES = 5
MAX_LINE_CHARS = 500
MAX_CALL_TEXT = 2000
MAX_RAW_TEXT = 4000
MAX_TAIL_CHARS = 2000
MAX_TURN_STATUS = 32

# §9.3 脱敏模式
_SECRET_PATTERNS = (
    (re.compile(r"(?i)\b(API[_-]?KEY|TOKEN|SECRET|PASSWORD)\s*=\s*\S+"), r"\1=<redacted>"),
    (re.compile(r"(?i)\bBearer\s+\S+"), "Bearer <redacted>"),
    (re.compile(r"(?i)\bAuthorization:\s*\S+"), "Authorization: <redacted>"),
    (re.compile(r'(?i)"(api_key|token|secret|password)"\s*:\s*"[^"]+"'),
     r'"\1":"<redacted>"'),
    (re.compile(r"\bsk-[A-Za-z0-9]{8,}"), "<redacted>"),
    (re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----.*?-----END [A-Z ]*PRIVATE KEY-----",
                re.S), "<redacted-private-key>"),
)


# ---------------------------------------------------------------------------
# 小工具
# ---------------------------------------------------------------------------

def _first(mapping, *names, default=None):
    if not isinstance(mapping, dict):
        return default
    for name in names:
        value = mapping.get(name)
        if value is not None:
            return value
    return default


def _as_int(value, default=0):
    try:
        if isinstance(value, bool):
            return int(value)
        return int(value)
    except (TypeError, ValueError):
        return default


def _text(value):
    if value is None:
        return ""
    if isinstance(value, (dict, list)):
        try:
            return json.dumps(value, ensure_ascii=False, sort_keys=True)
        except (TypeError, ValueError):
            return str(value)
    return str(value)


def _base(value):
    return os.path.basename(str(value or ""))


def _content_text(value, depth=0):
    """尽量从 item 的 text/content 结构里取出纯文本。"""
    if depth > 6:
        return _text(value)
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    if isinstance(value, dict):
        for key in ("text", "content", "summary", "message"):
            nested = value.get(key)
            if nested is not None:
                return _content_text(nested, depth + 1)
        return _text(value)
    if isinstance(value, (list, tuple)):
        parts = [_content_text(item, depth + 1) for item in value]
        return "".join(part for part in parts if part)
    return _text(value)


def _diff_counts(diff):
    """只按 hunk 内的 +/- 行计数；没有 hunk 时返回 (None, None)。"""
    add = delete = 0
    seen = False
    for line in _text(diff).splitlines():
        if line.startswith("@@"):
            seen = True
            continue
        if line.startswith("+++ ") or line.startswith("--- "):
            continue
        if line.startswith("+"):
            add += 1
        elif line.startswith("-"):
            delete += 1
    return (add, delete) if seen else (None, None)


def redact(text):
    """按 §9.3 对单个字符串脱敏。"""
    value = _text(text)
    if not value:
        return value
    for pattern, replacement in _SECRET_PATTERNS:
        value = pattern.sub(replacement, value)
    return value


def _redact_value(value, depth=0):
    """递归脱敏 data 里的字符串；只处理 JSON 可序列化结构。"""
    if depth > 8:
        return value
    if isinstance(value, str):
        return redact(value)
    if isinstance(value, list):
        return [_redact_value(item, depth + 1) for item in value]
    if isinstance(value, tuple):
        return [_redact_value(item, depth + 1) for item in value]
    if isinstance(value, dict):
        cleaned = {}
        for key, item in value.items():
            if str(key).lower() in ("api_key", "apikey", "token", "secret", "password"):
                cleaned[key] = "<redacted>"
            else:
                cleaned[key] = _redact_value(item, depth + 1)
        return cleaned
    return value


def _clip(value, limit):
    value = _text(value)
    return value if len(value) <= limit else value[:limit] + "…"


def output_tail(value, lines=MAX_TAIL_LINES):
    """把命令输出裁成尾 N 行（每行再限长），返回字符串列表。

    先整体脱敏再切行：私钥块等跨行模式必须按整段匹配。
    """
    raw = redact(_text(value)).replace("\r\n", "\n").replace("\r", "\n")
    rows = raw.split("\n")
    while rows and not rows[-1]:
        rows.pop()
    return [_clip(row, MAX_LINE_CHARS) for row in rows[-lines:]] if rows else []


def output_hash(value):
    return hashlib.sha256(_text(value).encode("utf-8", "replace")).hexdigest()[:16]


def _strip_shell(command):
    """去掉 `/bin/zsh -lc '…'` 包装，与看板 v2 EventParser 的行为一致。"""
    command = _text(command)
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


def _command_text(command):
    """app-server 的 command 可能是字符串或 argv 数组。"""
    if isinstance(command, (list, tuple)):
        parts = [_text(part) for part in command]
        if len(parts) >= 3 and parts[1] in ("-lc", "-c"):
            return _strip_shell(parts[2])
        return _strip_shell(" ".join(parts))
    return _strip_shell(command)


def _parse_ts(value):
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        stamp = float(value)
        if stamp > 1e11:  # 毫秒
            stamp /= 1000.0
        return stamp
    text = str(value).strip()
    if not text:
        return None
    try:
        stamp = float(text)
    except ValueError:
        pass
    else:
        if stamp > 1e11:
            stamp /= 1000.0
        return stamp
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.timestamp()


def event_ts(raw, clock=None):
    """优先使用原始事件时间，否则用注入时钟（默认 time.time()）。"""
    if isinstance(raw, dict):
        stamp = _parse_ts(_first(raw, "ts", "timestamp", "time", "created", "created_at"))
        if stamp is not None:
            return stamp
    clock = clock or time.time
    try:
        return float(clock())
    except (TypeError, ValueError):
        return time.time()


def normalize_source(source):
    key = str(source or "auto").strip().lower()
    return SOURCE_ALIASES.get(key, key)


def detect_source(event):
    """根据单条事件猜来源；猜不出返回 None。"""
    if not isinstance(event, dict):
        return None
    if "method" in event:
        return "codex-app-server"
    if "id" in event and ("result" in event or "error" in event) and "type" not in event:
        return "codex-app-server"
    etype = event.get("type")
    if "event" in event and str(event.get("event")) in (
            "init", "step_update", "result", "error"):
        return "agy-stream"
    if etype in _CONTROL_TYPES:
        return "xay-control"
    if etype in _DSH_TYPES:
        return "dsh-headless"
    if etype in _CODEX_EXEC_TYPES:
        return "codex-exec"
    return None


def _usage_payload(raw, granularity):
    """把各通道的 usage 归一为 {in,out,cached,total,granularity}。"""
    if not isinstance(raw, dict):
        return None
    source = raw
    for name in ("usage", "tokenUsage", "token_usage", "last", "total"):
        nested = raw.get(name)
        if isinstance(nested, dict):
            source = nested
            break
    in_tokens = _first(source, "in", "input", "input_tokens", "inputTokens")
    out_tokens = _first(source, "out", "output", "output_tokens", "outputTokens")
    cached = _first(source, "cached", "cached_input_tokens", "cache_read_tokens",
                    "cachedInputTokens", "cacheReadTokens", "cache_read_input_tokens")
    total = _first(source, "total", "total_tokens", "totalTokens")
    if in_tokens is None and out_tokens is None and cached is None and total is None:
        return None
    payload = {
        "in": _as_int(in_tokens),
        "out": _as_int(out_tokens),
        "cached": _as_int(cached),
        "granularity": granularity,
    }
    if total is not None:
        payload["total"] = _as_int(total)
    return payload


def _turn_status(value, default="completed"):
    text = str(value or default).strip().lower()
    mapping = {
        "completed": "completed", "complete": "completed", "success": "completed",
        "succeeded": "completed", "done": "completed", "ok": "completed",
        "interrupted": "interrupted", "interrupt": "interrupted",
        "canceled": "interrupted", "cancelled": "interrupted",
        "failed": "failed", "failure": "failed", "error": "failed",
    }
    return mapping.get(text, default)[:MAX_TURN_STATUS]


def _tool_kind(value):
    text = str(value or "").strip().lower()
    aliases = {
        "edit": "write", "write": "write", "patch": "write", "file_change": "write",
        "read": "read", "view": "read",
        "cmd": "cmd", "command": "cmd", "bash": "cmd", "shell": "cmd",
        "search": "search", "web": "web", "mcp": "mcp",
    }
    return aliases.get(text, "other")


def _diff_files(diff):
    """从统一 diff 中提取 (path, add, del)，用于 turn/diff/updated。"""
    rows = []
    current = None
    for line in _text(diff).splitlines():
        if line.startswith("+++ "):
            path = line[4:].strip()
            if path.startswith("b/"):
                path = path[2:]
            if current is not None:
                rows.append(current)
            current = {"path": path, "add": 0, "del": 0}
        elif line.startswith("--- "):
            continue
        elif current is not None and line.startswith("+") and not line.startswith("+++"):
            current["add"] += 1
        elif current is not None and line.startswith("-") and not line.startswith("---"):
            current["del"] += 1
    if current is not None:
        rows.append(current)
    return [row for row in rows if row["path"] and row["path"] != "/dev/null"]


# ---------------------------------------------------------------------------
# humanize 兼容层
# ---------------------------------------------------------------------------

_HUMANIZE = None
_HUMANIZE_TRIED = False


def _load_humanize():
    """按路径加载 scripts/xay_watch.py 里的 humanize；失败时返回 None。"""
    global _HUMANIZE, _HUMANIZE_TRIED
    if _HUMANIZE_TRIED:
        return _HUMANIZE
    _HUMANIZE_TRIED = True
    path = os.path.join(os.path.dirname(os.path.realpath(__file__)), "xay_watch.py")
    if not os.path.isfile(path):
        return None
    try:
        spec = importlib.util.spec_from_file_location("xay_watch_normalize_compat", path)
        if spec is None or spec.loader is None:
            return None
        module = importlib.util.module_from_spec(spec)
        sys.modules[spec.name] = module
        spec.loader.exec_module(module)
        if hasattr(module, "humanize"):
            _HUMANIZE = module.humanize
    except Exception:  # 加载看板失败不能拖垮归一
        _HUMANIZE = None
    return _HUMANIZE


def _fallback_humanize(action):
    action = action or {}
    kind = action.get("kind", "")
    text = str(action.get("text", "") or "")
    if kind in ("cmd", "read", "search", "edit", "tool", "think", "say", "plan"):
        return {"kind": kind, "text": text[:40], "raw": text,
                "state": action.get("state", "active")}
    return {"kind": kind, "text": text[:40], "raw": text,
            "state": action.get("state", "active")}


def humanize(action):
    """调用看板 v2 的 humanize（加载失败时退化为同形输出）。"""
    fn = _load_humanize()
    if fn is not None:
        return fn(action)
    return _fallback_humanize(action)


# normalized tool kind -> humanize 的 action kind
_CALL_ACTION_KIND = {
    "read": "read", "write": "edit", "cmd": "cmd",
    "search": "search", "web": "search", "mcp": "tool", "other": "tool",
}


def _action_id(event):
    data = event.get("data") or {}
    ident = _first(data, "id", "call_id", "request_id") or event.get("id")
    if ident:
        return str(ident)
    etype = str(event.get("type") or "")
    if etype.startswith("control."):
        # 控制行没有业务 id，用 seq/ts 保证看板 reducer 有稳定键。
        return "control:%s" % (event.get("seq") if event.get("seq") is not None
                               else event.get("ts"))
    return ""


def to_action(event):
    """把一条归一事件投影成 §3.8 的 action.append data。

    返回 `{kind,text,raw,state,id,execution,turn,by}`，其中前四个字段
    就是 scripts/xay_watch.py `humanize()` 的输出（§3.8）；非动作类事件返回 None。
    看板追加 action 时再补 `ts`（ui-v4 §6.2），不写进本函数的契约字段。
    """
    if not isinstance(event, dict):
        return None
    etype = event.get("type")
    data = event.get("data") or {}
    action_kind = ""
    raw = ""
    state = "done"

    if etype == "tool.call":
        tool_kind = _tool_kind(event.get("kind") or data.get("kind"))
        raw = _text(event.get("text"))
        if data.get("tool") in ("todo_list", "plan") or data.get("display_kind") == "plan":
            action_kind = "plan"
        else:
            action_kind = _CALL_ACTION_KIND.get(tool_kind, "tool")
        state = str(data.get("status") or "active")
    elif etype == "tool.result":
        tool_kind = _tool_kind(data.get("kind"))
        action_kind = _CALL_ACTION_KIND.get(tool_kind, "tool")
        raw = _text(_first(data, "display", "command", "text", default=event.get("text")))
        state = "done" if data.get("ok", True) else "failed"
    elif etype == "reasoning":
        action_kind, raw = "think", _text(event.get("text"))
        state = str(data.get("status") or "done")
    elif etype == "message":
        action_kind, raw = "say", _text(event.get("text"))
        state = str(data.get("status") or "done")
    elif etype == "control.requested":
        act = str(data.get("action") or "")
        action_kind = "steer" if act == "steer" else "control"
        raw = _text(event.get("text")) or act
        state = "active"
    elif etype == "control.acked":
        action_kind = "control"
        raw = "%s 已生效（%s）" % (
            _text(data.get("action")), _text(data.get("effective") or data.get("accepted_as")))
        state = "done"
    elif etype == "control.failed":
        action_kind = "error"
        raw = _text(data.get("error") or event.get("text")) or "控制失败"
        state = "error"
    else:
        return None

    action = humanize({"kind": action_kind, "text": raw, "state": state})
    action.update({
        "id": _action_id(event),
        "execution": event.get("execution"),
        "turn": event.get("turn"),
        "by": event.get("by"),
    })
    return action


# 语义别名：把一条归一事件转成 Action
humanize_event = to_action


def merge_actions(actions):
    """按 id 就地合并连续动作，模拟看板的 reducer（保持首次出现顺序）。"""
    merged = []
    index = {}
    for action in actions:
        if not action:
            continue
        key = action.get("id") or ""
        if key and key in index:
            target = merged[index[key]]
            for field in ("kind", "text", "raw", "state"):
                if action.get(field) not in (None, ""):
                    target[field] = action[field]
            continue
        if key:
            index[key] = len(merged)
        merged.append(dict(action))
    return merged


# ---------------------------------------------------------------------------
# 归一化器
# ---------------------------------------------------------------------------

class Normalizer:
    """有状态的单通道归一化器。

    典型用法::

        norm = Normalizer("dsh", ws="ws_a1b2", session="ls_…", phase="ph_…",
                          task="task_…", key="wire", execution="xs_…",
                          turn="turn_…")
        for line in open(run_dir / "events.raw"):
            for event in norm.feed_line(line):
                write(event)
        for event in norm.close():
            write(event)
    """

    def __init__(self, source="auto", context=None, clock=None,
                 assign_seq=False, redact_secrets=True, **kwargs):
        # 允许用 agent=/driver=/channel= 传通道名（与 source= 等价）。
        if normalize_source(source) == "auto":
            for alias in ("driver", "agent", "channel"):
                if kwargs.get(alias):
                    source = kwargs.pop(alias)
                    break
        ctx = dict(context or {})
        ctx.update(kwargs)
        self.source = normalize_source(source)
        self.clock = clock
        self.assign_seq = bool(assign_seq)
        self.redact_secrets = bool(redact_secrets)
        self.ctx = {field: ctx.get(field) for field in
                    ("ws", "session", "phase", "task", "key", "execution", "turn")}
        self.context = self.ctx
        self.native_session_id = None
        self.native_turn_id = None
        self._event_ts = None
        self._seq = 0
        self._turn_seq = 0
        self._closed = False
        self._session_started = set()
        self._turn_started = set()
        self._tool_calls = {}
        self._request_meta = {}
        self._errors_seen = set()
        self._warnings_seen = set()
        self._stream = None            # {"kind":"message"|"reasoning","id":…,"parts":[]}
        self._pending_message = None   # {"text","id","native_id"}
        self._agy_response = None      # {"idx","parts"}
        self._agy_turn_active = False  # 每轮结束后复位，resume 才能产生新的 turn.started
        self._reasoning_parts = {}     # app-server reasoning delta
        self._output_parts = {}        # app-server command output delta（只留尾巴）
        self._message_seq = 0

    # -- 公共入口 ---------------------------------------------------------

    def feed_line(self, line):
        if line is None:
            return []
        if isinstance(line, bytes):
            line = line.decode("utf-8", "replace")
        text = str(line).strip()
        if not text:
            return []
        try:
            event = json.loads(text)
        except (TypeError, ValueError):
            return []
        return self.feed(event)

    def feed_lines(self, lines):
        events = []
        for line in lines or []:
            events.extend(self.feed_line(line))
        return events

    def feed(self, raw):
        if self._closed:
            return []
        if isinstance(raw, (str, bytes)):
            return self.feed_line(raw)
        if isinstance(raw, (list, tuple)):
            events = []
            for item in raw:
                events.extend(self.feed(item))
            return events
        if not isinstance(raw, dict):
            return []
        self._event_ts = event_ts(raw, self.clock)
        source = self._source_for(raw)
        if source == "codex-app-server":
            return self._feed_appserver(raw)
        if source == "codex-exec":
            return self._feed_codex_exec(raw)
        if source == "dsh-headless":
            return self._feed_dsh(raw)
        if source == "agy-stream":
            return self._feed_agy(raw)
        if source == "xay-control":
            return self._feed_control(raw)
        return []

    def close(self):
        """冲刷聚合缓冲；幂等。"""
        if self._closed:
            return []
        self._closed = True
        events = []
        events.extend(self._flush_stream(final=True))
        if self._pending_message:
            events.extend(self._flush_message(final=True))
        if self._agy_response:
            events.extend(self._flush_agy_response(final=True))
        return events

    # -- 内部：信封与来源 -------------------------------------------------

    def _source_for(self, event):
        if self.source in ("auto", "codex-auto"):
            detected = detect_source(event)
            if detected:
                return detected
            return "codex-exec" if self.source == "codex-auto" else "auto"
        return self.source

    def _emit(self, etype, kind=None, path=None, text=None, data=None, by=None, ts=None):
        if etype not in _EVENT_TYPE_SET:
            raise ValueError("unknown normalized event type: %r" % (etype,))
        self._seq += 1
        if self.assign_seq:
            seq = self._seq
        else:
            seq = None
        payload = _redact_value(data) if self.redact_secrets else data
        value = str(text) if text is not None else None
        if value is not None and self.redact_secrets:
            value = redact(value)
        event = {
            "seq": seq,
            "ts": float(
                ts if ts is not None
                else self._event_ts if self._event_ts is not None
                else (self.clock() if self.clock else time.time())
            ),
            "ws": self.ctx.get("ws"),
            "session": self.ctx.get("session"),
            "phase": self.ctx.get("phase"),
            "task": self.ctx.get("task"),
            "key": self.ctx.get("key"),
            "execution": self.ctx.get("execution"),
            "turn": self.ctx.get("turn"),
            "type": etype,
            "kind": kind,
            "path": path,
            "text": value,
            "data": payload if payload is not None else {},
            "by": by,
        }
        return event

    def _emit_error(self, message, data=None):
        message = _text(message)
        if not message or message in self._errors_seen:
            return []
        self._errors_seen.add(message)
        payload = dict(data or {})
        payload.setdefault("message", message)
        return [self._emit("error", text=message, data=payload)]

    def _emit_warning(self, message, data=None):
        message = _text(message)
        if not message or message in self._warnings_seen:
            return []
        self._warnings_seen.add(message)
        payload = dict(data or {})
        payload.setdefault("message", message)
        return [self._emit("warning", text=message, data=payload)]

    # -- 内部：消息 / 推理 / 流 ------------------------------------------

    def _queue_message(self, text, ident=None, native_id=None, source=None):
        """暂存一条助手消息；新消息到来时把上一条按非最终消息冲出。"""
        text = _text(text)
        if not text:
            return []
        events = []
        if self._pending_message:
            events.extend(self._flush_message(final=False))
        self._message_seq += 1
        self._pending_message = {
            "text": text,
            "id": ident or "message:%d" % self._message_seq,
            "native_id": native_id,
            "source": source,
        }
        return events

    def _flush_message(self, final=False):
        pending = self._pending_message
        if not pending:
            return []
        self._pending_message = None
        data = {
            "id": pending["id"],
            "final": bool(final),
            "status": "done",
            "source": pending.get("source") or self.source,
            "chars": len(pending["text"]),
        }
        if pending.get("native_id"):
            data["native_item_id"] = pending["native_id"]
        return [self._emit("message", text=pending["text"], data=data)]

    def _emit_reasoning(self, text, ident=None, status="done", source=None):
        text = _text(text)
        if not text:
            return []
        truncated = len(text) > MAX_REASONING
        data = {
            "id": ident or "reasoning:%d" % (self._seq + 1),
            "status": status,
            "source": source or self.source,
            "chars": len(text),
        }
        if truncated:
            data["truncated"] = True
        return [self._emit("reasoning", text=_clip(text, MAX_REASONING), data=data)]

    def _feed_stream(self, kind, text):
        """dsh 的流式 text/thinking：只有聚合结果会落盘。"""
        text = _text(text)
        if not text:
            return []
        events = []
        if self._stream and self._stream["kind"] != kind:
            events.extend(self._flush_stream(final=False))
        if not self._stream:
            self._stream = {"kind": kind, "id": None, "parts": []}
        self._stream["parts"].append(text)
        return events

    def _flush_stream(self, final=False, text_override=None):
        stream = self._stream
        if not stream:
            return []
        self._stream = None
        text = text_override if text_override is not None else "".join(stream["parts"])
        if not _text(text):
            return []
        if stream["kind"] == "reasoning":
            return self._emit_reasoning(text, ident=stream.get("id"), status="done")
        self._message_seq += 1
        ident = stream.get("id") or "message:%d" % self._message_seq
        data = {
            "id": ident,
            "final": bool(final),
            "status": "done",
            "source": self.source,
            "chars": len(_text(text)),
        }
        return [self._emit("message", text=_text(text), data=data)]

    def _emit_usage(self, raw, granularity):
        payload = _usage_payload(raw, granularity)
        if not payload:
            return []
        return [self._emit("usage", data=payload)]

    # -- 内部：codex app-server ------------------------------------------

    def _feed_appserver(self, event):
        method = event.get("method")
        params = event.get("params")
        if not isinstance(params, dict):
            params = {}
        if method is None:
            return self._appserver_response(event)
        if method == "thread/started":
            thread = params.get("thread") if isinstance(params.get("thread"), dict) else {}
            native = _first(params, "threadId", "thread_id") or _first(thread, "id")
            if native:
                self.native_session_id = _text(native)
            if not native or native in self._session_started:
                return []
            self._session_started.add(native)
            return [self._emit("session.started", data={
                "native_session_id": _text(native),
                "driver": "codex-app-server",
                "cwd": _first(params, "cwd") or _first(thread, "cwd"),
                "model": _first(params, "model") or _first(thread, "model"),
                "sandbox": _first(params, "sandbox") or _first(thread, "sandbox"),
            })]
        if method == "turn/started":
            events = []
            events.extend(self._flush_message(final=False))
            native_turn = _first(params, "turnId", "turn_id") or _first(
                params.get("turn") or {}, "id")
            if native_turn:
                self.native_turn_id = _text(native_turn)
            self._turn_seq += 1
            data = {"native_turn_id": _text(native_turn) or None,
                    "turn_seq": self._turn_seq}
            if not native_turn or native_turn not in self._turn_started:
                if native_turn:
                    self._turn_started.add(native_turn)
                events.append(self._emit("turn.started", data=data))
            return events
        if method == "item/started":
            return self._appserver_item(params, started=True)
        if method == "item/completed":
            return self._appserver_item(params, started=False)
        if method == "item/agentMessage/delta":
            item_id = _text(_first(params, "itemId", "item_id"))
            delta = _text(_first(params, "delta", "text", default=""))
            if item_id and delta:
                self._output_parts.setdefault("msg:" + item_id, []).append(delta)
            return []
        if method == "item/commandExecution/outputDelta":
            item_id = _text(_first(params, "itemId", "item_id"))
            delta = _text(_first(params, "delta", "text", default=""))
            if item_id and delta:
                buf = self._output_parts.setdefault("out:" + item_id, [])
                buf.append(delta)
                joined = "".join(buf)
                if len(joined) > MAX_TAIL_CHARS * 2:
                    self._output_parts["out:" + item_id] = [joined[-MAX_TAIL_CHARS:]]
            return []
        if method in ("item/reasoning/delta", "item/reasoning/textDelta", "item/reasoning/summaryDelta"):
            item_id = _text(_first(params, "itemId", "item_id"))
            delta = _text(_first(params, "delta", "text", default=""))
            if item_id and delta:
                self._reasoning_parts.setdefault(item_id, []).append(delta)
            return []
        if method.endswith("/delta") or method.endswith("Delta"):
            return []  # 其余 delta 一律不落盘
        if method == "thread/tokenUsage/updated":
            raw = _first(params, "tokenUsage", "usage", "token_usage")
            # 每次模型请求推一条：total 是整个线程的累计，last 是这一次请求；按 last 逐条累加才是本轮用量
            if isinstance(raw, dict) and isinstance(raw.get("last"), dict):
                return self._emit_usage(raw["last"], "step")
            return self._emit_usage(raw or params, "live")
        if method == "turn/completed":
            return self._appserver_turn_completed(params)
        if method == "turn/failed":
            failed = dict(params)
            failed["status"] = "failed"
            failed.setdefault("error", params.get("error"))
            return self._appserver_turn_completed(failed)
        if method == "turn/diff/updated":
            diff = _text(_first(params, "diff", "unifiedDiff", "unified_diff"))
            return self._file_changed_from_diff(diff, source="native")
        if method == "thread/status/changed":
            return []
        if method == "warning":
            message = _text(_first(params, "message", "text")) or _text(params)
            return self._emit_warning(message, {"source": "codex-app-server"})
        if method == "error":
            message = _text(_first(params, "message", "text")) or _text(
                params.get("error") or params)
            # Codex 自己会重连的临时断流（willRetry=true）只记警告，不能判任务失败
            if params.get("willRetry"):
                return self._emit_warning(message, {"source": "codex-app-server", "willRetry": True})
            return self._emit_error(message, {"source": "codex-app-server"})
        if method in ("item/commandExecution/requestApproval",
                      "item/fileChange/requestApproval",
                      "item/permissions/requestApproval"):
            return self._appserver_approval(event, params)
        if method == "item/tool/requestUserInput":
            return self._appserver_question(event, params)
        if method in ("mcpServer/elicitation/request", "item/tool/call"):
            return []  # 阶段 1 不生成交互事件
        # 未知通知：忽略，保持前向兼容
        return []

    def _appserver_response(self, event):
        request_id = event.get("id")
        key = str(request_id)
        meta = self._request_meta.pop(key, None)
        if not meta:
            return []
        if "error" in event:
            error = event.get("error")
            message = _text(_first(error or {}, "message", default=error))
            return self._emit_error(message, {"request_id": request_id,
                                              "kind": meta.get("kind")})
        result = event.get("result")
        if meta.get("kind") == "approval":
            decision = result
            if isinstance(result, dict):
                decision = _first(result, "decision", "response", "approved", default=result)
            if isinstance(decision, dict):
                decision = _first(decision, "decision", "type", "response", default=decision)
            text = str(decision).strip().lower()
            denied = text in ("denied", "deny", "declined", "decline", "rejected",
                              "reject", "no", "false")
            if isinstance(decision, bool):
                denied = not decision
            if denied:
                return [self._emit(
                    "denied",
                    text=meta.get("what") or "命令被拒绝",
                    data={"request_id": request_id, "what": meta.get("what"),
                          "kind": meta.get("approval_kind")},
                )]
        return []

    def _appserver_approval(self, event, params):
        request_id = event.get("id")
        what = _text(_first(params, "command", "reason", "path", "description")) or "请求批准"
        meta = {"kind": "approval", "what": what,
                "approval_kind": event.get("method")}
        self._request_meta[str(request_id)] = meta
        return [self._emit(
            "approval.requested",
            text=what,
            data={"request_id": request_id, "what": what,
                  "kind": event.get("method"), "cwd": _first(params, "cwd"),
                  "native_item_id": _first(params, "itemId", "item_id")},
        )]

    def _appserver_question(self, event, params):
        request_id = event.get("id")
        questions = params.get("questions")
        if not isinstance(questions, list):
            questions = [questions] if questions else []
        texts = []
        for item in questions:
            if isinstance(item, dict):
                texts.append(_text(_first(item, "question", "text", "prompt")))
            elif item:
                texts.append(_text(item))
        text = "；".join([value for value in texts if value]) or "需要你回答"
        self._request_meta[str(request_id)] = {"kind": "question"}
        return [self._emit(
            "question",
            text=text,
            data={"request_id": request_id, "text": text,
                  "questions": questions,
                  "native_item_id": _first(params, "itemId", "item_id")},
        )]

    def _appserver_item(self, params, started):
        item = params.get("item")
        if not isinstance(item, dict):
            item = {}
        item_id = _text(_first(item, "id") or _first(params, "itemId", "item_id"))
        itype = _text(_first(item, "type", "kind"))
        status = _text(_first(item, "status", default="inProgress"))
        events = []
        if itype == "userMessage":
            return []
        if itype == "reasoning":
            if started:
                return []
            text = _content_text(_first(item, "text", "content", "summary"))
            if not text and item_id:
                text = "".join(self._reasoning_parts.pop(item_id, []))
            return self._emit_reasoning(text, ident=item_id, status="done",
                                        source="codex-app-server")
        if itype == "agentMessage":
            if started:
                return []
            text = _content_text(_first(item, "text", "content"))
            if not text and item_id:
                text = "".join(self._output_parts.pop("msg:" + item_id, []))
            elif item_id:
                self._output_parts.pop("msg:" + item_id, None)
            return self._queue_message(text, ident=item_id, native_id=item_id,
                                       source="codex-app-server")
        if itype in ("commandExecution", "command_execution"):
            command = _first(item, "command", "cmd")
            display = _clip(redact(_command_text(command)), MAX_CALL_TEXT)
            if started:
                self._tool_calls[item_id] = {"kind": "cmd", "display": display}
                return [self._emit(
                    "tool.call", kind="cmd", text=display,
                    data={"id": item_id, "tool": "commandExecution",
                          "raw": _clip(_text(command), MAX_RAW_TEXT),
                          "status": "active", "cwd": _first(item, "cwd"),
                          "source": "codex-app-server"},
                )]
            if item_id and item_id not in self._tool_calls:
                self._tool_calls[item_id] = {"kind": "cmd", "display": display}
                events.append(self._emit(
                    "tool.call", kind="cmd", text=display,
                    data={"id": item_id, "tool": "commandExecution",
                          "raw": _clip(_text(command), MAX_RAW_TEXT),
                          "status": "active", "cwd": _first(item, "cwd"),
                          "source": "codex-app-server"},
                ))
            output = _first(item, "aggregatedOutput", "aggregated_output", "output")
            if output is None and item_id:
                output = "".join(self._output_parts.pop("out:" + item_id, []))
            exit_code = _first(item, "exitCode", "exit_code")
            ok = str(status).lower() not in ("failed", "cancelled", "canceled", "error")
            if exit_code is not None and _as_int(exit_code, 0) != 0:
                ok = False
            tail = output_tail(output)
            data = {"id": item_id, "ok": ok,
                    "exit": None if exit_code is None else _as_int(exit_code),
                    "tail": tail, "hash": output_hash(output),
                    "kind": "cmd", "display": display,
                    "command": display,
                    "source": "codex-app-server"}
            events.append(self._emit("tool.result", text="\n".join(tail), data=data))
            return events
        if itype in ("fileChange", "file_change"):
            changes = _first(item, "changes", default=[])
            if not isinstance(changes, list):
                changes = [changes] if changes else []
            paths = []
            for change in changes:
                if isinstance(change, dict):
                    paths.append(_text(_first(change, "path", "file")))
                else:
                    paths.append(_text(change))
            display = ", ".join(_base(path) for path in paths if path)
            if started:
                self._tool_calls[item_id] = {"kind": "write", "display": display}
                return [self._emit(
                    "tool.call", kind="write", text=display,
                    data={"id": item_id, "tool": "fileChange", "status": "active",
                          "paths": paths, "source": "codex-app-server"},
                )]
            if display:
                self._tool_calls[item_id] = {"kind": "write", "display": display}
                events.append(self._emit(
                    "tool.call", kind="write", text=display,
                    data={"id": item_id, "tool": "fileChange", "status": "done",
                          "paths": paths, "source": "codex-app-server"},
                ))
            for change, path in zip(changes, paths):
                if not path:
                    continue
                add = _first(change, "add", "added") if isinstance(change, dict) else None
                delete = _first(change, "del", "delete", "deleted") if isinstance(change, dict) else None
                if (add is None or delete is None) and isinstance(change, dict):
                    diff_add, diff_del = _diff_counts(change.get("diff"))
                    add = diff_add if add is None else add
                    delete = diff_del if delete is None else delete
                events.append(self._emit(
                    "file.changed", path=path,
                    data={"add": _as_int(add), "del": _as_int(delete),
                          "kind": _first(change, "kind", default="update") if isinstance(change, dict) else "update",
                          "source": "native"},
                ))
            return events
        if itype in ("mcpToolCall", "mcp_tool_call"):
            server = _text(_first(item, "server", "serverName", "server_name"))
            tool = _text(_first(item, "tool", "toolName", "tool_name")) or server
            display = tool or "mcp"
            if started:
                self._tool_calls[item_id] = {"kind": "mcp", "display": display}
                return [self._emit(
                    "tool.call", kind="mcp", text=display,
                    data={"id": item_id, "tool": "mcpToolCall", "server": server,
                          "status": "active", "source": "codex-app-server"},
                )]
            if item_id and item_id not in self._tool_calls:
                self._tool_calls[item_id] = {"kind": "mcp", "display": display}
                events.append(self._emit(
                    "tool.call", kind="mcp", text=display,
                    data={"id": item_id, "tool": "mcpToolCall", "server": server,
                          "status": "active", "source": "codex-app-server"},
                ))
            ok = str(status).lower() not in ("failed", "cancelled", "canceled", "error")
            result = _first(item, "result", "output", "error")
            if result is not None:
                tail = output_tail(_text(result))
                events.append(self._emit(
                    "tool.result", text="\n".join(tail),
                    data={"id": item_id, "ok": ok, "exit": None, "tail": tail,
                          "hash": output_hash(_text(result)), "kind": "mcp",
                          "display": display, "command": display,
                          "server": server, "source": "codex-app-server"},
                ))
            return events
        if itype in ("error", "warning"):
            message = _text(_first(item, "message", "text"))
            if itype == "error":
                return self._emit_error(message, {"source": "codex-app-server"})
            return self._emit_warning(message, {"source": "codex-app-server"})
        return []

    def _appserver_turn_completed(self, params):
        events = []
        events.extend(self._flush_message(final=True))
        raw_turn = _first(params, "turnId", "turn_id") or _first(
            params.get("turn") or {}, "id")
        turn_obj = params.get("turn") if isinstance(params.get("turn"), dict) else {}
        status = _turn_status(_first(params, "status") or _first(turn_obj, "status")
                              or "completed")
        usage = _first(params, "usage", "tokenUsage")
        if usage:
            events.extend(self._emit_usage(usage, "turn"))
        error = params.get("error")
        if status == "failed" and error:
            message = _text(_first(error, "message") if isinstance(error, dict) else error)
            events.extend(self._emit_error(message, {"source": "codex-app-server"}))
        data = {"status": status, "native_turn_id": _text(raw_turn) or None,
                "source": "codex-app-server"}
        events.append(self._emit("turn.completed", data=data))
        return events

    # -- 内部：codex exec ------------------------------------------------

    def _feed_codex_exec(self, event):
        etype = event.get("type")
        if etype == "thread.started":
            native = _text(event.get("thread_id"))
            self.native_session_id = native or self.native_session_id
            if not native or native in self._session_started:
                return []
            self._session_started.add(native)
            return [self._emit("session.started", data={
                "native_session_id": native, "driver": "codex-exec",
                "cwd": event.get("cwd"),
            })]
        if etype == "turn.started":
            events = []
            events.extend(self._flush_message(final=False))
            self._turn_seq += 1
            events.append(self._emit("turn.started", data={
                "turn_seq": self._turn_seq, "driver": "codex-exec"}))
            return events
        if etype == "turn.failed":
            events = []
            events.extend(self._flush_message(final=False))
            message = _text(_first(event.get("error") or {}, "message",
                                   default=event.get("error")))
            events.extend(self._emit_error(message, {"source": "codex-exec"}))
            events.append(self._emit("turn.completed", data={
                "status": "failed", "source": "codex-exec"}))
            return events
        if etype == "turn.completed":
            events = []
            events.extend(self._flush_message(final=True))
            events.extend(self._emit_usage(event.get("usage"), "turn"))
            events.append(self._emit("turn.completed", data={
                "status": "completed", "source": "codex-exec"}))
            return events
        if etype not in ("item.started", "item.completed"):
            return []
        item = event.get("item")
        if not isinstance(item, dict):
            item = {}
        item_id = _text(item.get("id"))
        itype = _text(item.get("type"))
        started = etype == "item.started"
        events = []
        if itype == "error":
            return self._emit_error(_text(_first(item, "message", "text")),
                                    {"source": "codex-exec"})
        if itype == "reasoning":
            if started:
                return []
            return self._emit_reasoning(_content_text(_first(item, "text", "content")),
                                        ident=item_id, source="codex-exec")
        if itype == "command_execution":
            command = _first(item, "command", "cmd")
            display = _clip(redact(_command_text(command)), MAX_CALL_TEXT)
            if started:
                self._tool_calls[item_id] = {"kind": "cmd", "display": display}
                return [self._emit(
                    "tool.call", kind="cmd", text=display,
                    data={"id": item_id, "tool": "command_execution",
                          "raw": _clip(_text(command), MAX_RAW_TEXT),
                          "status": "active", "source": "codex-exec"},
                )]
            if item_id and item_id not in self._tool_calls:
                self._tool_calls[item_id] = {"kind": "cmd", "display": display}
                events.append(self._emit(
                    "tool.call", kind="cmd", text=display,
                    data={"id": item_id, "tool": "command_execution",
                          "raw": _clip(_text(command), MAX_RAW_TEXT),
                          "status": "active", "source": "codex-exec"},
                ))
            output = _first(item, "aggregated_output", "aggregatedOutput", "output")
            exit_code = _first(item, "exit_code", "exitCode")
            ok = str(item.get("status", "completed")).lower() != "failed" and (
                exit_code is None or _as_int(exit_code, 0) == 0)
            tail = output_tail(output)
            events.append(self._emit(
                "tool.result", text="\n".join(tail),
                data={"id": item_id, "ok": ok,
                      "exit": None if exit_code is None else _as_int(exit_code),
                      "tail": tail, "hash": output_hash(output),
                      "kind": "cmd", "display": display, "command": display,
                      "source": "codex-exec"},
            ))
            return events
        if itype == "file_change":
            changes = _first(item, "changes", default=[])
            if not isinstance(changes, list):
                changes = [changes] if changes else []
            paths = [_text(_first(change, "path", "file")) if isinstance(change, dict)
                     else _text(change) for change in changes]
            display = ", ".join(_base(path) for path in paths if path)
            if display:
                self._tool_calls[item_id] = {"kind": "write", "display": display}
                events.append(self._emit(
                    "tool.call", kind="write", text=display,
                    data={"id": item_id, "tool": "file_change",
                          "status": "done" if not started else "active",
                          "paths": paths, "source": "codex-exec"},
                ))
            if not started:
                for change, path in zip(changes, paths):
                    if not path:
                        continue
                    events.append(self._emit(
                        "file.changed", path=path,
                        data={"add": _as_int(_first(change, "add", default=0))
                              if isinstance(change, dict) else 0,
                              "del": _as_int(_first(change, "del", default=0))
                              if isinstance(change, dict) else 0,
                              "kind": _first(change, "kind", default="update")
                              if isinstance(change, dict) else "update",
                              "source": "native"},
                    ))
            return events
        if itype == "agent_message":
            if started:
                return []
            return self._queue_message(_content_text(_first(item, "text", "content")),
                                       ident=item_id, native_id=item_id,
                                       source="codex-exec")
        if itype == "web_search":
            query = _text(_first(item, "query", "text"))
            return [self._emit(
                "tool.call", kind="search", text=query,
                data={"id": item_id, "tool": "web_search",
                      "status": "done" if not started else "active",
                      "source": "codex-exec"},
            )]
        if itype == "todo_list":
            count = len(_first(item, "items", default=[]) or [])
            display = "计划 %d 项" % count
            return [self._emit(
                "tool.call", kind="other", text=display,
                data={"id": item_id, "tool": "todo_list", "display_kind": "plan",
                      "count": count,
                      "status": "done" if not started else "active",
                      "source": "codex-exec"},
            )]
        if itype == "mcp_tool_call":
            tool = _text(_first(item, "name", "tool"))
            if started:
                self._tool_calls[item_id] = {"kind": "mcp", "display": tool}
                return [self._emit(
                    "tool.call", kind="mcp", text=tool,
                    data={"id": item_id, "tool": "mcp_tool_call",
                          "status": "active", "source": "codex-exec"},
                )]
            if item_id and item_id not in self._tool_calls:
                self._tool_calls[item_id] = {"kind": "mcp", "display": tool}
                events.append(self._emit(
                    "tool.call", kind="mcp", text=tool,
                    data={"id": item_id, "tool": "mcp_tool_call",
                          "status": "active", "source": "codex-exec"},
                ))
            return events
        return []

    # -- 内部：dsh headless ----------------------------------------------

    def _feed_dsh(self, event):
        etype = event.get("type")
        events = []
        if etype == "session":
            native = _text(event.get("sessionId"))
            self.native_session_id = native or self.native_session_id
            if not native or native in self._session_started:
                return []
            self._session_started.add(native)
            return [self._emit("session.started", data={
                "native_session_id": native, "driver": "dsh-headless",
                "cwd": event.get("cwd"),
            })]
        if etype == "status":
            phase = event.get("phase")
            if phase == "turn_start":
                events.extend(self._flush_stream(final=False))
                events.extend(self._flush_message(final=False))
                self._turn_seq += 1
                events.append(self._emit("turn.started", data={
                    "turn_seq": _as_int(event.get("turn"), self._turn_seq),
                    "driver": "dsh-headless"}))
            elif phase == "step_end":
                usage = event.get("usage")
                if usage:
                    events.extend(self._emit_usage(usage, "step"))
            elif phase == "turn_end":
                reason = event.get("reason") or {}
                status = _turn_status(_first(reason, "kind", default="completed"))
                events.append(self._emit("turn.completed", data={
                    "status": status, "source": "dsh-headless"}))
            return events
        if etype == "tool_call":
            events.extend(self._flush_stream(final=False))
            call_id = _text(_first(event, "callId", "call_id")) or "tool:%d" % (self._seq + 1)
            tool = _text(event.get("tool"))
            inp = event.get("input") or {}
            kind, display = self._dsh_tool(tool, inp)
            self._tool_calls[call_id] = {"kind": kind, "display": display}
            events.append(self._emit(
                "tool.call", kind=kind, text=display,
                data={"id": call_id, "tool": tool, "status": "active",
                      "input": inp, "source": "dsh-headless"},
            ))
            return events
        if etype == "tool_result":
            call_id = _text(_first(event, "callId", "call_id"))
            known = self._tool_calls.get(call_id) or {}
            kind = known.get("kind") or "other"
            display = known.get("display") or call_id
            output = _first(event, "result", "output")
            tail = output_tail(output)
            ok = str(event.get("status", "completed")).lower() == "completed"
            events.append(self._emit(
                "tool.result", text="\n".join(tail),
                data={"id": call_id, "ok": ok,
                      "exit": _first(event, "exitCode", "exit_code"),
                      "tail": tail, "hash": output_hash(output),
                      "kind": kind, "display": display, "command": display,
                      "source": "dsh-headless"},
            ))
            return events
        if etype == "thinking":
            return self._feed_stream("reasoning", _first(event, "text", default=""))
        if etype == "text":
            return self._feed_stream("message", _first(event, "text", default=""))
        if etype == "error":
            # 驱动在进程异常退出时补的错误行（stderr 尾巴），让看板看得到原因。
            events.extend(self._flush_stream(final=False))
            message = _text(_first(event, "message", "error", default="")) or "dsh 出错"
            events.extend(self._emit_error(message, data={"source": "dsh-headless",
                                                          "exit": event.get("exitCode")}))
            return events
        if etype == "final":
            final_text = _text(event.get("text"))
            if self._stream:
                if self._stream["kind"] == "reasoning":
                    events.extend(self._flush_stream(final=False))
                    if final_text:
                        self._message_seq += 1
                        events.extend(self._queue_message(
                            final_text, ident="final:%d" % self._message_seq,
                            source="dsh-headless"))
                        events.extend(self._flush_message(final=True))
                else:
                    events.extend(self._flush_stream(
                        final=True, text_override=final_text or None))
            elif final_text:
                self._message_seq += 1
                events.extend(self._queue_message(
                    final_text, ident="final:%d" % self._message_seq,
                    source="dsh-headless"))
                events.extend(self._flush_message(final=True))
            return events
        return []

    @staticmethod
    def _dsh_tool(tool, inp):
        inp = inp if isinstance(inp, dict) else {}
        if tool == "bash":
            return "cmd", _clip(redact(_first(inp, "command", default="")), MAX_CALL_TEXT)
        if tool in ("read", "view"):
            return "read", _text(_first(inp, "file_path", "path", "filePath"))
        if tool in ("write", "edit", "patch", "multi_edit"):
            path = _text(_first(inp, "file_path", "path", "filePath"))
            return "write", _base(path)
        if tool in ("grep", "rg", "search", "glob", "find"):
            query = _text(_first(inp, "pattern", "query", "path", default=""))
            return "search", query
        if tool in ("webfetch", "web_fetch", "websearch", "web_search"):
            query = _text(_first(inp, "url", "query", default=""))
            return "web", query
        return "other", tool

    # -- 内部：agy stream-json -------------------------------------------

    def _feed_agy(self, event):
        name = event.get("event")
        events = []
        if name == "init":
            native = _text(event.get("conversation_id"))
            self.native_session_id = native or self.native_session_id
            # 新进程（含 --conversation 续接）总是从空闲开始；上一个进程被杀时
            # 未完成的半截回复也一并丢弃。
            self._agy_turn_active = False
            self._agy_response = None
            init = event.get("init") or {}
            if not native or native in self._session_started:
                return []
            self._session_started.add(native)
            return [self._emit("session.started", data={
                "native_session_id": native, "driver": "agy-stream",
                "cwd": _first(init, "cwd"),
                "model": _first(init, "model"),
                "permission_mode": _first(init, "permission_mode"),
                "tool_count": len(init.get("tools") or []),
            })]
        if name == "step_update":
            step = event.get("step_update")
            if not isinstance(step, dict):
                step = {}
            if not self._agy_turn_active:
                self._agy_turn_active = True
                self._turn_seq += 1
                events.append(self._emit("turn.started", data={
                    "turn_seq": self._turn_seq, "driver": "agy-stream"}))
            idx = _first(step, "step_index", "stepIndex")
            state = str(_first(step, "state", default="ACTIVE")).upper()
            done = state in ("DONE", "COMPLETED", "FAILED", "ERROR", "CANCELED", "CANCELLED")
            stype = _first(step, "step_type", "stepType")
            if stype == "user_input":
                return events
            if stype == "agent_response":
                delta = _text(_first(step, "text_delta", "textDelta", default=""))
                # 不逐条 delta 落盘：整段回复先留在缓冲里，等下一步或 result
                # 到来时再决定它是中间消息还是最终回复。
                if self._agy_response and self._agy_response.get("idx") != idx:
                    events.extend(self._flush_agy_response(final=False))
                if delta:
                    if self._agy_response is None:
                        self._agy_response = {"idx": idx, "parts": []}
                    self._agy_response["parts"].append(delta)
                if done:
                    usage = step.get("usage")
                    if usage:
                        events.extend(self._emit_usage(usage, "step"))
                return events
            if stype == "tool":
                events.extend(self._flush_agy_response(final=False))
                tool = _text(_first(step, "tool_name", "toolName"))
                params = _first(step.get("tool_info") or {}, "parameters", default={}) or {}
                kind, display = self._agy_tool(tool, params)
                ident = "step:%s" % idx
                if not done:
                    self._tool_calls[ident] = {"kind": kind, "display": display}
                    events.append(self._emit(
                        "tool.call", kind=kind, text=display,
                        data={"id": ident, "tool": tool, "status": "active",
                              "source": "agy-stream"},
                    ))
                else:
                    if ident not in self._tool_calls:
                        self._tool_calls[ident] = {"kind": kind, "display": display}
                        events.append(self._emit(
                            "tool.call", kind=kind, text=display,
                            data={"id": ident, "tool": tool, "status": "active",
                                  "source": "agy-stream"},
                        ))
                    output = _first(step, "output", "result")
                    tail = output_tail(output)
                    ok = state == "DONE"
                    events.append(self._emit(
                        "tool.result", text="\n".join(tail),
                        data={"id": ident, "ok": ok, "exit": None, "tail": tail,
                              "hash": output_hash(output), "kind": kind,
                              "display": display, "command": display,
                              "source": "agy-stream"},
                    ))
                    usage = step.get("usage")
                    if usage:
                        events.extend(self._emit_usage(usage, "step"))
                return events
            if done:
                usage = step.get("usage")
                if usage:
                    events.extend(self._emit_usage(usage, "step"))
            return events
        if name == "result":
            self._agy_turn_active = False
            result = event.get("result")
            if not isinstance(result, dict):
                result = {}
            native = _text(result.get("conversation_id"))
            if native:
                self.native_session_id = native
            buffered = bool(self._agy_response and
                            "".join(self._agy_response.get("parts") or []))
            flushed = self._flush_agy_response(final=True)
            events.extend(flushed)
            response = _text(result.get("response"))
            # 已有流式文本时以缓冲为准，避免同一段回复出现两条 message。
            if response and not buffered and not any(
                    event.get("type") == "message" for event in flushed):
                self._message_seq += 1
                events.extend(self._queue_message(
                    response, ident="result:%d" % self._message_seq,
                    source="agy-stream"))
                events.extend(self._flush_message(final=True))
            usage = result.get("usage")
            if usage:
                events.extend(self._emit_usage(usage, "turn"))
            status = _turn_status(result.get("status") or "success")
            events.append(self._emit("turn.completed", data={
                "status": status, "native_turn_id": None,
                "num_turns": _first(result, "num_turns", default=1),
                "source": "agy-stream"}))
            return events
        if name == "error":
            message = _text(_first(event, "error", "message", default=event))
            return self._emit_error(message, {"source": "agy-stream"})
        return []

    def _flush_agy_response(self, final=False, state="DONE"):
        pending = self._agy_response
        if not pending:
            return []
        self._agy_response = None
        text = "".join(pending.get("parts") or [])
        if not text:
            return []
        if final:
            self._message_seq += 1
            return self._queue_message(
                text, ident="step:%s" % pending.get("idx"),
                source="agy-stream") + self._flush_message(final=True)
        data = {
            "id": "step:%s" % pending.get("idx"),
            "final": False,
            "status": "done" if state == "DONE" else "active",
            "source": "agy-stream",
            "chars": len(text),
        }
        return [self._emit("message", text=text, data=data)]

    @staticmethod
    def _agy_tool(tool, params):
        params = params if isinstance(params, dict) else {}
        if tool in ("read_file", "view_file"):
            path = _text(_first(params, "Path", "AbsolutePath", "TargetFile", "path"))
            return "read", _base(path)
        if tool in ("write_to_file", "replace_file_content", "multi_replace_file_content",
                    "sed_file", "notebook_edit"):
            path = _text(_first(params, "Path", "AbsolutePath", "TargetFile", "path"))
            return "write", _base(path)
        if tool == "run_command":
            command = _text(_first(params, "CommandLine", "command"))
            return "cmd", _clip(redact(command), MAX_CALL_TEXT)
        if tool in ("grep_search", "find_by_name", "list_dir", "search_web", "read_url_content"):
            value = _text(_first(params, "Query", "query", "Pattern", "pattern",
                                 "path", "Path", "url"))
            return ("web" if "url" in tool or "web" in tool else "search"), value
        return "other", tool

    # -- 内部：xay 侧车（控制/决定/信号/笔记） ----------------------------

    def _feed_control(self, event):
        etype = event.get("type")
        ts = event_ts(event, self.clock)
        if etype == "control.requested":
            action = _text(event.get("action"))
            return [self._emit(
                "control.requested", text=_text(event.get("text")) or action,
                by=event.get("by"),
                data={"action": action, "source": event.get("source"),
                      "expected_turn": event.get("expected_turn"),
                      "to_agent": event.get("to_agent")},
                ts=ts,
            )]
        if etype == "control.acked":
            action = _text(event.get("action"))
            accepted = _text(_first(event, "accepted_as", default=action))
            return [self._emit(
                "control.acked", text=accepted, by=event.get("by"),
                data={"action": action, "accepted_as": accepted,
                      "effective": event.get("effective"),
                      "source": event.get("source")},
                ts=ts,
            )]
        if etype == "control.failed":
            action = _text(event.get("action"))
            error = _text(_first(event, "error", "text", default="控制失败"))
            return [self._emit(
                "control.failed", text=error, by=event.get("by"),
                data={"action": action, "error": error,
                      "source": event.get("source")},
                ts=ts,
            )]
        if etype == "signal":
            kind = _text(event.get("kind")) or "unknown"
            text = _text(event.get("text"))
            return [self._emit(
                "signal", kind=kind, text=text,
                data={"level": event.get("level", "remind"),
                      "task": event.get("task"), "execution": event.get("execution")},
                ts=ts,
            )]
        if etype == "decision.created":
            ident = _text(event.get("id"))
            title = _text(_first(event, "title", "text"))
            return [self._emit(
                "decision.created", text=title,
                data={"id": ident, "kind": event.get("kind"),
                      "owner": event.get("owner", "lead"),
                      "title": title, "detail": event.get("detail"),
                      "options": event.get("options")},
                ts=ts,
            )]
        if etype == "decision.resolved":
            ident = _text(event.get("id"))
            option = _text(event.get("option"))
            return [self._emit(
                "decision.resolved", text=option,
                data={"id": ident, "option": option, "by": event.get("by"),
                      "text": event.get("text")},
                ts=ts,
            )]
        if etype == "note.updated":
            key = _text(_first(event, "key", default=self.ctx.get("key")))
            note = _text(event.get("text"))
            return [self._emit(
                "note.updated", text=note,
                data={"key": key, "path": event.get("path")},
                ts=ts,
            )]
        if etype == "warning":
            return self._emit_warning(_text(_first(event, "message", "text")),
                                      {"source": event.get("source", "xay")})
        if etype == "error":
            return self._emit_error(_text(_first(event, "message", "text", "error")),
                                    {"source": event.get("source", "xay")})
        return []

    # -- 内部：diff -------------------------------------------------------

    def _file_changed_from_diff(self, diff, source="native"):
        events = []
        for row in _diff_files(diff):
            events.append(self._emit(
                "file.changed", path=row["path"],
                data={"add": row["add"], "del": row["del"], "source": source},
            ))
        return events


# 兼容别名
EventNormalizer = Normalizer


# ---------------------------------------------------------------------------
# 落盘：delta 兜底过滤
# ---------------------------------------------------------------------------

def is_delta(event):
    """判断一条（可能已被误塞进来的）事件是否为 delta。"""
    if not isinstance(event, dict):
        return False
    etype = str(event.get("type") or "").lower()
    if "delta" in etype:
        return True
    data = event.get("data")
    if isinstance(data, dict) and data.get("delta") is not None:
        return True
    return False


class NormWriter:
    """把归一事件追加到 events.norm.jsonl；delta 永不落盘。"""

    def __init__(self, path, mode="a", assign_seq=False, encoding="utf-8"):
        self.path = os.path.abspath(os.path.expanduser(str(path)))
        parent = os.path.dirname(self.path)
        if parent:
            os.makedirs(parent, exist_ok=True)
        self._file = open(self.path, mode, encoding=encoding)
        self.assign_seq = bool(assign_seq)
        self._seq = 0
        self.written = 0
        self.skipped = 0

    def write(self, events):
        if isinstance(events, dict):
            events = [events]
        count = 0
        for event in events or []:
            if not isinstance(event, dict) or is_delta(event):
                self.skipped += 1
                continue
            if self.assign_seq:
                self._seq += 1
                if event.get("seq") is None:
                    event = dict(event)
                    event["seq"] = self._seq
            self._file.write(json.dumps(event, ensure_ascii=False, separators=(",", ":")) + "\n")
            self.written += 1
            count += 1
        if count:
            self._file.flush()
        return count

    def close(self):
        if not self._file.closed:
            self._file.close()

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.close()

    def __del__(self):
        try:
            self.close()
        except Exception:
            pass


def write_norm_jsonl(path, events, **kwargs):
    """一次性写入；返回实际写出的行数。"""
    with NormWriter(path, mode="w", **kwargs) as writer:
        return writer.write(events)


# ---------------------------------------------------------------------------
# 便捷函数
# ---------------------------------------------------------------------------

def normalize_event(source, event, **kwargs):
    norm = Normalizer(source, **kwargs)
    return norm.feed(event) + norm.close()


def normalize_line(source, line, **kwargs):
    return normalize_event(source, line, **kwargs)


def normalize_lines(source, lines, **kwargs):
    norm = Normalizer(source, **kwargs)
    events = norm.feed_lines(lines)
    events.extend(norm.close())
    return events


def normalize(source, raw, **kwargs):
    """一次归一化一条事件、一行或一组行。"""
    if isinstance(raw, str):
        if "\n" in raw.strip():
            return normalize_lines(source, raw.splitlines(), **kwargs)
        return normalize_line(source, raw, **kwargs)
    if isinstance(raw, (list, tuple)):
        norm = Normalizer(source, **kwargs)
        events = norm.feed(raw)
        events.extend(norm.close())
        return events
    return normalize_event(source, raw, **kwargs)


def normalize_file(source, path, **kwargs):
    with open(path, encoding="utf-8", errors="replace") as handle:
        return normalize_lines(source, handle, **kwargs)


def main(argv=None):
    """命令行入口：把一份原始 JSONL 归一成事件信封或 Action。"""
    import argparse

    parser = argparse.ArgumentParser(description="xay 事件归一（arch-v4 §3.4）")
    parser.add_argument("--source", required=True,
                        choices=[key for key in sorted(SOURCE_ALIASES) if key],
                        help="原始通道；codex 会自动区分 app-server 与 exec")
    parser.add_argument("--input", default="-", help="原始 JSONL；默认 stdin")
    parser.add_argument("--output", default="-", help="输出 JSONL；默认 stdout")
    parser.add_argument("--assign-seq", action="store_true", help="写本地序号")
    parser.add_argument("--actions", action="store_true",
                        help="输出 humanize 兼容的 Action，而不是事件信封")
    parser.add_argument("--no-redact", action="store_true", help="关闭脱敏（仅调试）")
    for field in ("ws", "session", "phase", "task", "key", "execution", "turn"):
        parser.add_argument("--" + field, default=None)
    args = parser.parse_args(argv)

    context = {field: getattr(args, field)
               for field in ("ws", "session", "phase", "task", "key",
                             "execution", "turn")}
    norm = Normalizer(args.source, assign_seq=args.assign_seq,
                      redact_secrets=not args.no_redact, **context)
    if args.input == "-":
        lines = sys.stdin
        events = norm.feed_lines(lines)
        events.extend(norm.close())
    else:
        events = normalize_file(args.source, args.input, assign_seq=args.assign_seq,
                                redact_secrets=not args.no_redact, **context)
    if args.actions:
        events = merge_actions(
            [action for action in (to_action(event) for event in events) if action])
    if args.output == "-":
        for event in events:
            sys.stdout.write(json.dumps(event, ensure_ascii=False,
                                        separators=(",", ":")) + "\n")
        sys.stdout.flush()
        return 0
    write_norm_jsonl(args.output, events, assign_seq=args.assign_seq)
    return 0


__all__ = [
    "ENVELOPE_FIELDS", "EVENT_TYPES", "TOOL_KINDS", "SOURCE_ALIASES",
    "Normalizer", "EventNormalizer", "NormWriter", "is_delta",
    "normalize", "normalize_event", "normalize_line", "normalize_lines",
    "normalize_file", "write_norm_jsonl", "to_action", "humanize_event",
    "merge_actions", "humanize", "redact", "output_tail", "output_hash",
    "detect_source",
    "main",
]


if __name__ == "__main__":
    sys.exit(main())
