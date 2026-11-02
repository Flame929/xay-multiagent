#!/usr/bin/env python3
"""xay 第四版看门狗（arch-v4 §8.1）。

纯函数：输入归一事件与 `git status` 快照，输出 `signal` / `decision` 数据。
不读写文件、不起进程、不碰 socket；阶段 1 永远不产生控制动作（§6.7）。
调用方负责把返回值落盘：`type == "signal"` 写 signals.jsonl，
`type == "decision"` 写 decisions.jsonl，并按 `flags` 更新 Execution.flags。

    wd = Watchdog(agent="dsh", task=task_id, execution=xs_id,
                  writes=card["writes"], budget_s=card["budget"]["minutes"] * 60)
    for rec in wd.feed(event):        # 每个归一事件调用一次
        ...
    for rec in wd.tick(now):          # 没有事件时每秒/每 10 秒调用一次
        ...
    for rec in wd.feed_git_status(text, now):   # 每 10 秒轮询一次
        ...
"""

import fnmatch
import hashlib
import re
import time
from datetime import datetime

SILENCE_S = {"codex": 180, "dsh": 120, "agy": 240}
DEFAULT_IGNORE = (
    "__pycache__/",
    ".pytest_cache/",
    "coverage/",
    "node_modules/",
    "dist/",
    "build/",
    ".xay/",
    ".git/",
)
LOOP_SAME_OUTPUT = 4
LOOP_SAME_ERROR = 3
BUDGET_REMIND_RATIO = 0.8
FAILED_FINAL_OPTIONS = (
    {"id": "rework", "label": "让同一个 AI 返工", "danger": False},
    {"id": "reassign", "label": "换个 AI 重做", "danger": False},
    {"id": "ignore", "label": "先不管", "danger": False},
    {"id": "handoff_lead", "label": "交给负责人", "danger": False},
)

_LOOP_GLOBS = set("*?[")
_QUIET_TYPES = {"signal", "decision.created", "decision.resolved", "note.updated"}
_CMD_PREFIX_RE = re.compile(r"^(?:cd\s+[^;&|]+\s*(?:&&|;)\s*)+")
_C_ESCAPES = {"n": 10, "t": 9, "r": 13, '"': 34, "\\": 92}


# --------------------------------------------------------------------------
# 纯判定函数（单测直接覆盖）
# --------------------------------------------------------------------------

def silence_stage(elapsed, threshold):
    """静默阶段：0 正常，1 达到阈值，2 达到 2 倍阈值。"""
    if not threshold or threshold <= 0:
        return 0
    if elapsed >= 2 * threshold:
        return 2
    if elapsed >= threshold:
        return 1
    return 0


def budget_ratio(started, now, budget_s):
    """已用时长比例；没有预算或尚未开始返回 None。"""
    if not budget_s or budget_s <= 0 or started is None:
        return None
    return max(0.0, (now - started) / float(budget_s))


def normalize_command(text):
    """归一化命令，用于判断"同命令"：去掉 cd 前缀、折叠空白。"""
    command = " ".join(str(text or "").split())
    while True:
        stripped = _CMD_PREFIX_RE.sub("", command, count=1).strip()
        if stripped == command:
            return command
        command = stripped


def result_ok(event):
    """tool.result 是否成功：优先 data.ok，其次 exit，缺省视为成功。"""
    data = event.get("data") or {}
    if "ok" in data:
        ok = data.get("ok")
    elif "ok" in event:
        ok = event.get("ok")
    else:
        exit_code = data.get("exit", event.get("exit"))
        return exit_code in (None, 0, "0")
    if isinstance(ok, str):
        return ok.lower() not in ("false", "0", "no", "")
    return bool(ok)


def output_hash(event):
    """tool.result 的输出哈希；事件没带哈希时对输出尾部做哈希。"""
    data = event.get("data") or {}
    for key in ("output_hash", "out_hash", "hash", "sha1", "digest"):
        value = data.get(key, event.get(key))
        if value:
            return str(value)
    tail = data.get("tail", data.get("output", event.get("text")))
    return hashlib.sha1(_stable_text(tail).encode("utf-8")).hexdigest()


def is_ignored(path, ignore=DEFAULT_IGNORE):
    """路径是否在忽略列表中（按路径分量或 glob 匹配）。"""
    path = _normalize_path(path)
    if not path:
        return True
    parts = [part for part in path.split("/") if part]
    for pattern in tuple(DEFAULT_IGNORE) + tuple(ignore or ()):
        pattern = str(pattern or "").strip().strip("/")
        if not pattern:
            continue
        if not any(char in _LOOP_GLOBS for char in pattern):
            if pattern in parts:
                return True
            continue
        if fnmatch.fnmatch(path, pattern) or any(fnmatch.fnmatch(part, pattern) for part in parts):
            return True
    return False


def path_in_writes(path, writes):
    """路径是否落在任务卡 writes 内：支持精确文件、目录前缀、glob。"""
    path = _normalize_path(path)
    for raw in writes or ():
        pattern = _normalize_path(raw)
        if not pattern:
            continue
        if any(char in _LOOP_GLOBS for char in pattern):
            if fnmatch.fnmatch(path, pattern):
                return True
            continue
        if path == pattern or path.startswith(pattern + "/"):
            return True
    return False


def check_scope(paths, writes=None, ignore=DEFAULT_IGNORE):
    """返回越界路径。writes=None 表示不检查；writes=[] 表示什么都不允许改。"""
    if writes is None:
        return []
    offending = []
    for path in paths or ():
        normalized = _normalize_path(path)
        if not normalized or is_ignored(normalized, ignore):
            continue
        if not path_in_writes(normalized, writes):
            offending.append(normalized)
    return _dedupe(offending)


def parse_git_status(output):
    """解析 `git status --porcelain`（普通或 -z）为相对路径列表。"""
    text = str(output or "")
    if not text:
        return []
    if "\0" in text:
        paths = _parse_git_status_z(text)
    else:
        paths = []
        for line in text.splitlines():
            if len(line) < 4 or line.startswith("##"):
                continue
            body = line[3:].strip()
            if " -> " in body:
                body = body.split(" -> ", 1)[1]
            paths.append(_unquote(body))
    return _dedupe(_normalize_path(path) for path in paths if _normalize_path(path))


# --------------------------------------------------------------------------
# 有状态聚合器：仍然是纯计算，只是记住判定所需的游标
# --------------------------------------------------------------------------

class Watchdog:
    """聚合归一事件，输出 Signal/Decision 数据；不发出任何控制动作。"""

    def __init__(
        self,
        agent="dsh",
        *,
        thresholds=None,
        silence_s=None,
        budget_s=None,
        budget_minutes=None,
        writes=None,
        ignore=(),
        task=None,
        execution=None,
        ws=None,
        session=None,
        phase=None,
        started=None,
    ):
        table = dict(SILENCE_S)
        for name, value in (thresholds or {}).items():
            if value:
                table[str(name)] = int(value)
        self.agent = str(agent or "dsh")
        self.thresholds = table
        self.silence_s = int(silence_s or table.get(self.agent, SILENCE_S["dsh"]))
        if budget_s is None and budget_minutes is not None:
            budget_s = float(budget_minutes) * 60
        self.budget_s = float(budget_s) if budget_s else None
        self.writes = None if writes is None else list(writes)
        self.ignore = tuple(DEFAULT_IGNORE) + tuple(ignore or ())
        self.task = task
        self.execution = execution
        self.ws = ws
        self.session = session
        self.phase = phase

        self.flags = set()
        self.started = float(started) if started is not None else None
        self._last_activity = self.started
        self._last_ts = self.started
        self._silence_stage = 0
        self._budget_notified = False

        self._pending_cmd = None
        self._last_pair = None
        self._pair_run = 0
        self._pair_notified = False
        self._last_error = None
        self._error_run = 0
        self._error_notified = False

        self._scope_seen = set()
        self._seq = 0

    # ----- 入口 -----------------------------------------------------------

    def feed(self, event):
        """喂一个归一事件，返回本次新产生的记录列表。"""
        if not isinstance(event, dict):
            return []
        event_type = str(event.get("type") or event.get("event") or "")
        if not event_type or _is_quiet(event_type):
            return []
        now = _event_ts(event)
        if now is None:
            now = self._last_ts if self._last_ts is not None else time.time()

        records = self._check_silence(now)
        self._mark_activity(now)
        self._last_ts = max(self._last_ts, now) if self._last_ts is not None else now

        if event_type == "turn.started" or event_type == "turn.completed":
            self._reset_loop()
        elif event_type.startswith("reasoning"):
            # reasoning delta 也是活动，但不参与循环判定。
            pass
        elif event_type == "tool.call":
            self._on_tool_call(event)
        elif event_type == "tool.result":
            records.extend(self._on_tool_result(event, now))

        records.extend(self._check_budget(now))
        return records

    def tick(self, now=None):
        """没有事件时推进时间：检查静默与时长预算。"""
        now = time.time() if now is None else float(now)
        records = self._check_silence(now)
        records.extend(self._check_budget(now))
        return records

    def feed_git_status(self, status, now=None):
        """喂 `git status --porcelain` 文本（或路径列表），返回 scope 提醒。"""
        paths = parse_git_status(status) if isinstance(status, str) else list(status or ())
        now = time.time() if now is None else float(now)
        offending = check_scope(paths, self.writes, self.ignore)
        if offending:
            self.flags.add("scope")
        else:
            self.flags.discard("scope")
        fresh = [path for path in offending if path not in self._scope_seen]
        self._scope_seen = set(offending)
        if not fresh:
            return []
        shown = "、".join(fresh[:5])
        if len(fresh) > 5:
            shown += " 等 %d 个" % len(fresh)
        return [self._signal("scope", "scope", "改了范围外的文件：%s" % shown, now)]

    # 兼容调用方的不同命名。
    poll_git_status = feed_git_status
    check_git_status = feed_git_status

    # ----- 静默 -----------------------------------------------------------

    def _check_silence(self, now):
        if self._last_activity is None:
            return []
        elapsed = now - self._last_activity
        stage = silence_stage(elapsed, self.silence_s)
        if stage <= self._silence_stage or stage == 0:
            return []
        if stage == 1:
            self._silence_stage = 1
            self.flags.add("stalled")
            text = "已静默 %.0f 秒（阈值 %d 秒），没有新动作" % (elapsed, self.silence_s)
            return [self._signal("stalled", "silence", text, now)]
        self._silence_stage = 2
        self.flags.add("stalled")
        return [self._silence_decision(elapsed, now)]

    def _silence_decision(self, elapsed, now):
        detail = (
            "已静默 %.0f 秒，达到阈值 %d 秒的 2 倍。"
            "可以返工、换个 AI 重做、先不管，或交给负责人。"
            % (elapsed, self.silence_s)
        )
        return self._decision(
            kind="failed_final",
            fail_reason="stalled",
            title="很久没动静了",
            detail=detail,
            payload={
                "fail_reason": "stalled",
                "silence_s": int(elapsed),
                "threshold_s": self.silence_s,
                "last_activity": int(self._last_activity),
            },
            now=now,
        )

    # ----- 循环 -----------------------------------------------------------

    def _on_tool_call(self, event):
        command = _command_of(event)
        if command:
            self._pending_cmd = command
            return
        # 非命令工具调用打断"连续同命令"的计数。
        self._pending_cmd = None
        self._reset_loop()

    def _on_tool_result(self, event, now):
        command = _command_of(event, allow_text=False) or self._pending_cmd
        self._pending_cmd = None
        if not command:
            return []
        digest = output_hash(event)
        if result_ok(event):
            key = (command, digest)
            if key == self._last_pair:
                self._pair_run += 1
            else:
                self._last_pair = key
                self._pair_run = 1
                self._pair_notified = False
                self.flags.discard("loop")
            self._error_run = 0
            self._last_error = None
            if self._pair_run >= LOOP_SAME_OUTPUT and not self._pair_notified:
                self._pair_notified = True
                self.flags.add("loop")
                text = "同一命令连续 %d 次输出相同：%s" % (self._pair_run, _preview(command))
                return [self._signal("loop", "loop", text, now)]
            return []
        error_key = (command, digest)
        if error_key == self._last_error:
            self._error_run += 1
        else:
            self._last_error = error_key
            self._error_run = 1
            self._error_notified = False
            self.flags.discard("loop")
        self._pair_run = 0
        self._last_pair = None
        if self._error_run >= LOOP_SAME_ERROR and not self._error_notified:
            self._error_notified = True
            self.flags.add("loop")
            text = "同一命令连续 %d 次报同样的错：%s" % (self._error_run, _preview(command))
            return [self._signal("loop", "loop", text, now)]
        return []

    def _reset_loop(self):
        self._pending_cmd = None
        self._last_pair = None
        self._pair_run = 0
        self._pair_notified = False
        self._last_error = None
        self._error_run = 0
        self._error_notified = False
        self.flags.discard("loop")

    # ----- 预算 -----------------------------------------------------------

    def _check_budget(self, now):
        ratio = budget_ratio(self.started, now, self.budget_s)
        if ratio is None or ratio < BUDGET_REMIND_RATIO or self._budget_notified:
            return []
        self._budget_notified = True
        self.flags.add("over_time")
        text = "已用 %d%% 时长预算（%s / %s）" % (
            round(ratio * 100),
            _human_seconds(now - self.started),
            _human_seconds(self.budget_s),
        )
        return [self._signal("over_time", "budget", text, now)]

    # ----- 记录 -----------------------------------------------------------

    def _mark_activity(self, now):
        if self._last_activity is None or now >= self._last_activity:
            if self._last_activity is None and self.started is None:
                self.started = now
            self._last_activity = now
            self._silence_stage = 0
            self.flags.discard("stalled")

    def _signal(self, kind, rule, text, now, level="remind"):
        created = int(now)
        data = {
            "id": self._new_id("sig", created),
            "kind": kind,
            "level": level,
            "task": self.task,
            "execution": self.execution,
            "text": text,
            "created": created,
            "acked": False,
        }
        return {"type": "signal", "rule": rule, "data": data, "flags": self.flag_list()}

    def _decision(self, kind, fail_reason, title, detail, payload, now):
        created = int(now)
        data = {
            "id": self._new_id("dec", created),
            "kind": kind,
            "fail_reason": fail_reason,
            "owner": "lead",
            "version": 1,
            "ws": self.ws,
            "session": self.session,
            "phase": self.phase,
            "task": self.task,
            "execution": self.execution,
            "title": title,
            "detail": detail,
            "options": [dict(option) for option in FAILED_FINAL_OPTIONS],
            "payload": payload,
            "created": created,
            "resolved": None,
        }
        return {"type": "decision", "rule": "silence_escalation", "data": data,
                "flags": self.flag_list()}

    def _new_id(self, prefix, created):
        self._seq += 1
        day = datetime.fromtimestamp(created).strftime("%Y%m%d") if created else "00000000"
        raw = "|".join(str(part) for part in (
            prefix, self.task, self.execution, created, self._seq))
        digest = hashlib.sha1(raw.encode("utf-8")).hexdigest()[:4]
        return "%s_%s_%s" % (prefix, day, digest)

    def flag_list(self):
        return sorted(self.flags)


# --------------------------------------------------------------------------
# 小工具
# --------------------------------------------------------------------------

def _event_ts(event):
    value = event.get("ts", event.get("created"))
    try:
        return float(value) if value is not None else None
    except (TypeError, ValueError):
        return None


def _is_quiet(event_type):
    return event_type in _QUIET_TYPES or event_type.startswith("control.")


def _command_of(event, allow_text=True):
    data = event.get("data") or {}
    values = [data.get("command"), event.get("command"), data.get("cmd")]
    if allow_text:
        values.extend([event.get("text"), data.get("raw")])
    for value in values:
        if value:
            return normalize_command(value)
    return ""


def _stable_text(value):
    if isinstance(value, (list, tuple)):
        value = "\n".join(str(line) for line in value)
    return "\n".join(line.rstrip() for line in str(value or "").splitlines()).strip()


def _preview(text, limit=80):
    text = str(text or "")
    return text if len(text) <= limit else text[:limit] + "…"


def _human_seconds(value):
    value = float(value or 0)
    if value >= 120:
        return "%.1f 分钟" % (value / 60.0)
    return "%.0f 秒" % value


def _normalize_path(path):
    path = str(path or "").strip().replace("\\", "/")
    while path.startswith("./"):
        path = path[2:]
    return path.strip("/")


def _dedupe(items):
    seen = set()
    result = []
    for item in items:
        if item not in seen:
            seen.add(item)
            result.append(item)
    return result


def _parse_git_status_z(text):
    tokens = text.split("\0")
    paths = []
    index = 0
    while index < len(tokens):
        token = tokens[index]
        index += 1
        if len(token) < 4:
            continue
        code = token[:2]
        body = token[3:]
        if code[0] in "RC" or code[1] in "RC":
            index += 1  # -z 的重命名条目后面紧跟旧路径
        paths.append(_unquote(body))
    return paths


def _unquote(path):
    path = str(path or "").strip()
    if len(path) < 2 or not (path.startswith('"') and path.endswith('"')):
        return path
    inner = path[1:-1]
    raw = bytearray()
    index = 0
    while index < len(inner):
        char = inner[index]
        if char != "\\":
            raw.extend(char.encode("utf-8"))
            index += 1
            continue
        code = inner[index + 1:index + 2]
        if code in _C_ESCAPES:
            raw.append(_C_ESCAPES[code])
            index += 2
        elif code and code in "01234567" and len(inner) >= index + 4:
            raw.append(int(inner[index + 1:index + 4], 8))
            index += 4
        else:
            raw.extend(code.encode("utf-8"))
            index += 2
    return raw.decode("utf-8", "replace")


__all__ = [
    "DEFAULT_IGNORE",
    "FAILED_FINAL_OPTIONS",
    "LOOP_SAME_ERROR",
    "LOOP_SAME_OUTPUT",
    "SILENCE_S",
    "Watchdog",
    "budget_ratio",
    "check_scope",
    "is_ignored",
    "normalize_command",
    "output_hash",
    "parse_git_status",
    "path_in_writes",
    "result_ok",
    "silence_stage",
]
