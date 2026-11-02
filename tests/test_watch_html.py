#!/usr/bin/env python3
"""看板页面（scripts/xay-watch.html）的契约测试。

覆盖 arch-v4 §3 / ui-v4 §6.1 对 `?demo=1` 示例数据的要求，以及页面层面的
两条硬约束：不出现百分比进度条、不引用外部 URL（CDN）。

页面正在重写，这些断言按契约写，不按当前实现写。
"""

import json
import re
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
HTML_PATH = ROOT / "scripts" / "xay-watch.html"


# ---------------------------------------------------------------------------
# arch-v4 §3 枚举（冻结契约，不从页面里反推）
# ---------------------------------------------------------------------------

TOP_LEVEL_KEYS = {
    "seq",
    "now",
    "summary",
    "thresholds",
    "workspaces",
    "decisions",
    "agents",
    "host",
}

SESSION_STATUS = {"needs_you", "running", "waiting", "idle", "done"}
TASK_STATUS = {
    "waiting_deps",
    "waiting_slot",
    "starting",
    "running",
    "pausing",
    "paused",
    "needs_you",
    "finishing",
    "done",
    "failed",
    "canceled",
    "skipped",
    "orphaned",
}
# §3.2 Phase.status 写的是"同 Task 冒泡：needs_you > running > waiting >
# canceled > failed > done"，因此比 Task 多一个聚合值 waiting。
PHASE_STATUS = TASK_STATUS | {"waiting"}
EXECUTION_STATE = {
    "starting",
    "running",
    "pausing",
    "paused",
    "interrupting",
    "finishing",
    "done",
    "failed",
    "canceled",
    "orphaned",
}
LEAD_KIND = {"claude", "codex", "dsh", "agy", "unknown"}
TITLE_SOURCE = {"lead", "fallback"}
AGENTS = {"codex", "dsh", "agy"}
SAFETY_TIER = {"strict", "standard", "open"}
PHASE_KIND = {"dag", "single"}
FAIL_REASON = {"conflict", "crash", "timeout", "cli_error", "verify", "orphaned", ""}
LAND = {"applied", "conflict", "empty", "discarded", ""}
CARD_MODE = {"write", "readonly"}
CARD_SOURCE = {"sub", "relay", "auto"}
CARD_ISOLATE = {"auto", "on", "off"}

DRIVERS = {
    "codex-app-server",
    "codex-exec",
    "dsh-headless",
    "dsh-acp",
    "agy-stream",
    "agy-oneshot",
}
# §5.2 诚实版能力矩阵里与 driver 强相关的字段。
DRIVER_CAPABILITIES = {
    "codex-app-server": {
        "steer": "live",
        "interrupt": "soft",
        "resume": True,
        "approve": True,
        "file_events": True,
        "usage_granularity": "live",
    },
    "codex-exec": {
        "steer": "none",
        "interrupt": "kill",
        "resume": True,
        "approve": False,
        "file_events": True,
        "usage_granularity": "turn",
    },
    "dsh-headless": {
        "steer": "none",
        "interrupt": "kill",
        "resume": True,
        "approve": False,
        "file_events": False,
        "usage_granularity": "step",
    },
    "dsh-acp": {
        "steer": "none",
        "interrupt": "soft",
        "resume": True,
        "approve": True,
        "file_events": False,
        "usage_granularity": "step",
    },
    "agy-stream": {
        "steer": "none",
        "interrupt": "kill",
        "resume": True,
        "approve": False,
        "file_events": False,
        "usage_granularity": "turn",
    },
    "agy-oneshot": {
        "steer": "none",
        "interrupt": "kill",
        "resume": True,
        "approve": False,
        "file_events": False,
        "usage_granularity": "turn",
    },
}
CAPABILITY_KEYS = {
    "steer",
    "interrupt",
    "resume",
    "approve",
    "file_events",
    "usage_granularity",
    "readonly_sandbox",
    "structured_output",
}
CAPABILITY_ENUMS = {
    "steer": {"live", "none"},
    "interrupt": {"soft", "kill"},
    "usage_granularity": {"live", "step", "turn"},
}
CAPABILITY_BOOLS = {
    "resume",
    "approve",
    "file_events",
    "readonly_sandbox",
    "structured_output",
}

TURN_KIND = {"first", "rework", "resume", "reassign"}
TURN_STATUS = {"running", "completed", "interrupted", "failed"}
USAGE_GRANULARITY = {"live", "step", "turn"}

DECISION_KIND = {
    "conflict",
    "failed_final",
    "question",
    "approval",
    "orphaned",
    "integration_failed",
}
DECISION_OWNER = {"lead", "user"}


# ---------------------------------------------------------------------------
# 极简 JS 对象字面量解析器（只用标准库）。
#
# demo 数据是 JS 而不是 JSON：键可能不带引号、有注释/尾逗号、值可能是
# Math.floor(Date.now() / 1000) 这类表达式。结构校验只关心字面量，所以动态
# 表达式统一折叠成 _DYNAMIC 哨兵。
# ---------------------------------------------------------------------------

_DYNAMIC = object()


class JSParseError(ValueError):
    pass


class _LiteralParser:
    _IDENT_RE = re.compile(r"[A-Za-z_$][\w$]*")
    _NUMBER_RE = re.compile(r"[-+]?(?:\d+\.?\d*|\.\d+)(?:[eE][-+]?\d+)?")

    def __init__(self, text, pos=0):
        self.text = text
        self.pos = pos
        self.length = len(text)

    # -- 基础 ---------------------------------------------------------------
    def _peek(self, offset=0):
        index = self.pos + offset
        return self.text[index] if index < self.length else ""

    def _skip_space(self):
        while self.pos < self.length:
            char = self.text[self.pos]
            if char in " \t\r\n\ufeff":
                self.pos += 1
            elif self.text.startswith("//", self.pos):
                end = self.text.find("\n", self.pos)
                self.pos = self.length if end < 0 else end + 1
            elif self.text.startswith("/*", self.pos):
                end = self.text.find("*/", self.pos + 2)
                if end < 0:
                    raise JSParseError("未闭合的块注释")
                self.pos = end + 2
            else:
                break

    def _read_string(self, quote):
        assert self._peek() == quote
        self.pos += 1
        chunks = []
        escapes = {
            "n": "\n",
            "t": "\t",
            "r": "\r",
            "b": "\b",
            "f": "\f",
            "v": "\v",
            "0": "\0",
        }
        while self.pos < self.length:
            char = self.text[self.pos]
            self.pos += 1
            if char == quote:
                return "".join(chunks)
            if char == "\\":
                if self.pos >= self.length:
                    break
                esc = self.text[self.pos]
                self.pos += 1
                if esc in escapes:
                    chunks.append(escapes[esc])
                elif esc in "\\/'\"`":
                    chunks.append(esc)
                elif esc == "x" and re.match(
                    r"[0-9a-fA-F]{2}", self.text[self.pos : self.pos + 2]
                ):
                    chunks.append(chr(int(self.text[self.pos : self.pos + 2], 16)))
                    self.pos += 2
                elif esc == "u" and re.match(
                    r"[0-9a-fA-F]{4}", self.text[self.pos : self.pos + 4]
                ):
                    chunks.append(chr(int(self.text[self.pos : self.pos + 4], 16)))
                    self.pos += 4
                elif esc == "\n":
                    continue  # 行续接
                else:
                    chunks.append(esc)
                continue
            chunks.append(char)
        raise JSParseError("未闭合的字符串")

    def _read_template(self):
        assert self._peek() == "`"
        self.pos += 1
        chunks = []
        while self.pos < self.length:
            char = self.text[self.pos]
            self.pos += 1
            if char == "`":
                return "".join(chunks)
            if char == "\\":
                if self.pos >= self.length:
                    break
                chunks.append(self.text[self.pos])
                self.pos += 1
                continue
            if self.text.startswith("${", self.pos - 1):
                self.pos += 1
                self._skip_braced_expression()
                chunks.append("__expr__")
                continue
            chunks.append(char)
        raise JSParseError("未闭合的模板字符串")

    def _skip_braced_expression(self):
        depth = 1
        while self.pos < self.length:
            char = self.text[self.pos]
            if char in "'\"":
                self._read_string(char)
            elif char == "`":
                self._read_template()
            elif self.text.startswith("//", self.pos):
                self._skip_space()
            elif self.text.startswith("/*", self.pos):
                self._skip_space()
            elif char == "{":
                depth += 1
                self.pos += 1
            elif char == "}":
                depth -= 1
                self.pos += 1
                if depth == 0:
                    return
            else:
                self.pos += 1
        raise JSParseError("未闭合的模板表达式")

    def _skip_expression(self):
        """跳过无法静态求值的表达式，直到当前层的 , } ] 为止。"""
        depth = 0
        while self.pos < self.length:
            char = self.text[self.pos]
            if char in "'\"":
                self._read_string(char)
                continue
            if char == "`":
                self._read_template()
                continue
            if self.text.startswith("//", self.pos):
                self._skip_space()
                continue
            if self.text.startswith("/*", self.pos):
                self._skip_space()
                continue
            if char in "([{":
                depth += 1
                self.pos += 1
                continue
            if char in ")]}":
                if depth == 0:
                    return
                depth -= 1
                self.pos += 1
                continue
            if char == "," and depth == 0:
                return
            self.pos += 1

    # -- 值 -----------------------------------------------------------------
    def parse_value(self):
        self._skip_space()
        char = self._peek()
        if not char:
            raise JSParseError("意外结尾")
        if char == "{":
            return self._parse_object()
        if char == "[":
            return self._parse_array()
        if char in "'\"":
            return self._read_string(char)
        if char == "`":
            return self._read_template()
        if char.isdigit() or (
            char in "+-" and self._peek(1).isdigit()
        ):
            match = self._NUMBER_RE.match(self.text, self.pos)
            if not match:
                raise JSParseError("非法数字")
            self.pos = match.end()
            raw = match.group(0)
            return float(raw) if any(c in raw for c in ".eE") else int(raw)
        if char.isalpha() or char in "_$":
            match = self._IDENT_RE.match(self.text, self.pos)
            name = match.group(0)
            self.pos = match.end()
            if name == "true":
                return True
            if name == "false":
                return False
            if name == "null":
                return None
            if name == "undefined":
                return None
            self._skip_expression()
            return _DYNAMIC
        # 其他表达式（函数调用、括号、正则等）
        self._skip_expression()
        return _DYNAMIC

    def parse_object_with_end(self):
        """从当前位置的对象开始解析，返回 (对象, 结束位置)。"""
        value = self._parse_object()
        return value, self.pos

    def _parse_object(self):
        assert self._peek() == "{"
        self.pos += 1
        result = {}
        while True:
            self._skip_space()
            if self._peek() == "}":
                self.pos += 1
                return result
            if not self._peek():
                raise JSParseError("未闭合的对象")
            if self.text.startswith("...", self.pos):
                self.pos += 3
                value = self.parse_value()
                if isinstance(value, dict):
                    result.update(value)
            else:
                key = self._parse_key()
                self._skip_space()
                if self._peek() == ":":
                    self.pos += 1
                    value = self.parse_value()
                else:
                    value = _DYNAMIC  # 简写属性
                result[key] = value
            self._skip_space()
            char = self._peek()
            if char == ",":
                self.pos += 1
                continue
            if char == "}":
                self.pos += 1
                return result
            raise JSParseError(f"对象里出现意外字符 {char!r}")

    def _parse_key(self):
        char = self._peek()
        if char in "'\"":
            return self._read_string(char)
        if char == "[":
            # 计算属性名：静态结构校验用占位名
            self.pos += 1
            self._skip_expression()
            if self._peek() == "]":
                self.pos += 1
            return "__computed__"
        match = self._IDENT_RE.match(self.text, self.pos)
        if match:
            self.pos = match.end()
            return match.group(0)
        match = self._NUMBER_RE.match(self.text, self.pos)
        if match:
            self.pos = match.end()
            return match.group(0)
        raise JSParseError(f"非法对象键 {char!r}")

    def _parse_array(self):
        assert self._peek() == "["
        self.pos += 1
        result = []
        while True:
            self._skip_space()
            if self._peek() == "]":
                self.pos += 1
                return result
            if not self._peek():
                raise JSParseError("未闭合的数组")
            if self.text.startswith("...", self.pos):
                self.pos += 3
                value = self.parse_value()
                if isinstance(value, list):
                    result.extend(value)
            else:
                result.append(self.parse_value())
            self._skip_space()
            char = self._peek()
            if char == ",":
                self.pos += 1
                continue
            if char == "]":
                self.pos += 1
                return result
            raise JSParseError(f"数组里出现意外字符 {char!r}")


_ASSIGN_OBJECT_RE = re.compile(
    r"(?:const|let|var)\s+([A-Za-z_$][\w$]*)\s*=\s*\{"
    r"|(?:window\.)?([A-Za-z_$][\w$]*)\s*=\s*\{"
)
_JSON_SCRIPT_RE = re.compile(
    r"<script\b([^>]*)>(.*?)</script\s*>", re.IGNORECASE | re.DOTALL
)


def inline_scripts(html):
    """返回 (属性, 内容) 的内联 <script> 列表（忽略带 src 的外链脚本）。"""
    scripts = []
    for match in _JSON_SCRIPT_RE.finditer(html):
        attrs = match.group(1) or ""
        if re.search(r"\bsrc\s*=", attrs, re.IGNORECASE):
            continue
        scripts.append((attrs, match.group(2)))
    return scripts


def _script_blocks(html):
    """只取可执行脚本（排除 application/json 数据块）。"""
    blocks = []
    for attrs, body in inline_scripts(html):
        if re.search(r"type\s*=\s*[\"']application/json", attrs, re.IGNORECASE):
            continue
        blocks.append(body)
    return blocks


def find_demo_state(html):
    """定位 ?demo=1 使用的示例数据对象。

    优先识别名字里含 demo 的 const/let/var 对象字面量，也兼容
    <script type="application/json" id="demo-...">；兜底取任何带
    workspaces 顶层键的对象。返回 (对象名, dict)。
    """
    errors = []
    candidates = []

    for attrs, body in inline_scripts(html):
        is_json = bool(
            re.search(r"type\s*=\s*[\"']application/json", attrs, re.IGNORECASE)
        )
        ident = re.search(r"\bid\s*=\s*[\"']([^\"']+)", attrs)
        name = ident.group(1) if ident else ""
        if not is_json:
            continue
        try:
            parsed = json.loads(body)
        except ValueError as exc:
            errors.append(f"{name or '<json script>'}: JSON 解析失败: {exc}")
            continue
        if isinstance(parsed, dict):
            candidates.append((name, parsed))

    for block in _script_blocks(html):
        for match in _ASSIGN_OBJECT_RE.finditer(block):
            name = match.group(1) or match.group(2)
            brace = match.end() - 1
            try:
                parsed, _ = _LiteralParser(block, brace).parse_object_with_end()
            except JSParseError as exc:
                if "demo" in name.lower():
                    errors.append(f"{name}: {exc}")
                continue
            if isinstance(parsed, dict):
                candidates.append((name, parsed))

    if not candidates:
        detail = "\n".join(errors) if errors else "页面里没有找到对象字面量示例数据"
        raise AssertionError("找不到 ?demo=1 的示例数据对象：\n" + detail)

    for name, value in candidates:
        if "demo" in name.lower() and "workspaces" in value:
            return name, value
    for name, value in candidates:
        if "demo" in name.lower():
            return name, value
    for name, value in candidates:
        if "workspaces" in value:
            return name, value
    return candidates[0]


# ---------------------------------------------------------------------------
# 结构校验（把问题一次性收集起来，方便页面作者一次改完）
# ---------------------------------------------------------------------------


def _is_int(value):
    return isinstance(value, int) and not isinstance(value, bool)


def _is_str(value):
    return isinstance(value, str)


def _type_name(value):
    if value is _DYNAMIC:
        return "动态表达式(无法静态校验)"
    return type(value).__name__


def _check_keys(errors, value, required, where, exact=False):
    if not isinstance(value, dict):
        errors.append(f"{where}: 应为 object，实际 {_type_name(value)}")
        return
    missing = sorted(set(required) - set(value))
    if missing:
        errors.append(f"{where}: 缺少键 {missing}")
    if exact:
        extra = sorted(set(value) - set(required))
        if extra:
            errors.append(f"{where}: 多出契约外键 {extra}")


def _check_enum(errors, value, allowed, where):
    if value is _DYNAMIC:
        errors.append(f"{where}: 不能是动态表达式，必须是枚举字面量")
    elif value not in allowed:
        errors.append(f"{where}: {value!r} 不在枚举 {sorted(allowed)} 内")


def _check_int(errors, value, where):
    # 时间戳通常是 Math.floor(Date.now() / 1000) 之类的动态表达式，静态结构
    # 测试无法求值，只跳过值检查（键是否存在由 _check_keys 负责）。
    if value is _DYNAMIC:
        return
    if not _is_int(value):
        errors.append(f"{where}: 应为整数，实际 {_type_name(value)}")


def _check_bool(errors, value, where):
    if value is _DYNAMIC:
        return
    if not isinstance(value, bool):
        errors.append(f"{where}: 应为 bool，实际 {_type_name(value)}")


def _check_str(errors, value, where):
    if value is _DYNAMIC:
        return
    if not _is_str(value):
        errors.append(f"{where}: 应为 string，实际 {_type_name(value)}")


def _check_list(errors, value, where):
    if not isinstance(value, list):
        errors.append(f"{where}: 应为 array，实际 {_type_name(value)}")
        return False
    return True


def validate_demo_state(state):
    """按 arch-v4 §3 / ui-v4 §6.1 校验 demo 快照，返回错误列表。"""
    errors = []

    if not isinstance(state, dict):
        return [f"demo 数据: 顶层应为 object，实际 {_type_name(state)}"]

    _check_keys(errors, state, TOP_LEVEL_KEYS, "demo", exact=True)
    if set(TOP_LEVEL_KEYS) - set(state):
        return errors

    _check_int(errors, state["seq"], "demo.seq")
    _check_int(errors, state["now"], "demo.now")

    summary = state["summary"]
    if isinstance(summary, dict):
        _check_keys(
            errors,
            summary,
            {"needs_you", "running", "waiting", "done_unread"},
            "demo.summary",
        )
        for key in ("needs_you", "running", "waiting", "done_unread"):
            if key in summary:
                _check_int(errors, summary[key], f"demo.summary.{key}")
    else:
        errors.append(f"demo.summary: 应为 object，实际 {_type_name(summary)}")

    thresholds = state["thresholds"]
    if isinstance(thresholds, dict):
        silence = thresholds.get("silence_s")
        if isinstance(silence, dict):
            for agent in ("codex", "dsh", "agy"):
                if agent not in silence:
                    errors.append(f"demo.thresholds.silence_s: 缺少 {agent}")
                else:
                    _check_int(
                        errors,
                        silence[agent],
                        f"demo.thresholds.silence_s.{agent}",
                    )
        else:
            errors.append("demo.thresholds.silence_s: 应为 object")
        if "wait_default_s" in thresholds:
            _check_int(
                errors, thresholds["wait_default_s"], "demo.thresholds.wait_default_s"
            )
        else:
            errors.append("demo.thresholds: 缺少 wait_default_s")
    else:
        errors.append(f"demo.thresholds: 应为 object，实际 {_type_name(thresholds)}")

    _validate_workspaces(errors, state["workspaces"])
    _validate_decisions(errors, state["decisions"])
    _validate_agents(errors, state["agents"])
    _validate_host(errors, state["host"])

    return errors


def _validate_workspaces(errors, workspaces):
    if not _check_list(errors, workspaces, "demo.workspaces"):
        return
    for wi, ws in enumerate(workspaces):
        where = f"demo.workspaces[{wi}]"
        _check_keys(
            errors,
            ws,
            {"id", "root", "name", "safety_tier", "confidential", "sessions"},
            where,
        )
        if not isinstance(ws, dict):
            continue
        if "id" in ws:
            _check_str(errors, ws["id"], where + ".id")
            if _is_str(ws["id"]) and not ws["id"].startswith("ws_"):
                errors.append(f"{where}.id: 应以 ws_ 开头，实际 {ws['id']!r}")
        _check_str(errors, ws.get("root", _DYNAMIC), where + ".root")
        _check_str(errors, ws.get("name", _DYNAMIC), where + ".name")
        if "safety_tier" in ws:
            _check_enum(errors, ws["safety_tier"], SAFETY_TIER, where + ".safety_tier")
        if "confidential" in ws:
            _check_bool(errors, ws["confidential"], where + ".confidential")
        sessions = ws.get("sessions")
        if not _check_list(errors, sessions, where + ".sessions"):
            continue
        for si, session in enumerate(sessions):
            _validate_session(errors, session, ws.get("id"), f"{where}.sessions[{si}]")


def _validate_session(errors, session, ws_id, where):
    _check_keys(
        errors,
        session,
        {
            "id",
            "ws",
            "lead_kind",
            "title",
            "title_source",
            "status",
            "started",
            "ended",
            "last_activity",
            "pinned",
            "archived",
            "phases",
        },
        where,
    )
    if not isinstance(session, dict):
        return
    if "id" in session:
        _check_str(errors, session["id"], where + ".id")
        if _is_str(session["id"]) and not session["id"].startswith("ls_"):
            errors.append(f"{where}.id: 应以 ls_ 开头，实际 {session['id']!r}")
    if "ws" in session:
        _check_str(errors, session["ws"], where + ".ws")
        if ws_id is not None and session["ws"] != ws_id:
            errors.append(f"{where}.ws: {session['ws']!r} 与所属工作区 {ws_id!r} 不一致")
    if "lead_kind" in session:
        _check_enum(errors, session["lead_kind"], LEAD_KIND, where + ".lead_kind")
    _check_str(errors, session.get("title", _DYNAMIC), where + ".title")
    if "title_source" in session:
        _check_enum(
            errors, session["title_source"], TITLE_SOURCE, where + ".title_source"
        )
    if "status" in session:
        _check_enum(errors, session["status"], SESSION_STATUS, where + ".status")
    for key in ("started", "last_activity"):
        if key in session:
            _check_int(errors, session[key], f"{where}.{key}")
    if "ended" in session and session["ended"] is not None:
        _check_int(errors, session["ended"], where + ".ended")
    for key in ("pinned", "archived"):
        if key in session:
            _check_bool(errors, session[key], f"{where}.{key}")
    phases = session.get("phases")
    if not _check_list(errors, phases, where + ".phases"):
        return
    for pi, phase in enumerate(phases):
        _validate_phase(errors, phase, session.get("id"), f"{where}.phases[{pi}]")


def _validate_phase(errors, phase, session_id, where):
    _check_keys(
        errors,
        phase,
        {"id", "session", "seq", "kind", "title", "status"},
        where,
    )
    if not isinstance(phase, dict):
        return
    if "id" in phase:
        _check_str(errors, phase["id"], where + ".id")
        # §3.2：single 阶段的 id 就是 run 的 xs_。
        if _is_str(phase["id"]) and not phase["id"].startswith(("ph_", "xs_")):
            errors.append(f"{where}.id: 应以 ph_（或 single 时的 xs_）开头，实际 {phase['id']!r}")
    if "session" in phase and session_id is not None:
        if phase["session"] != session_id:
            errors.append(
                f"{where}.session: {phase['session']!r} 与所属会话 {session_id!r} 不一致"
            )
    if "seq" in phase:
        _check_int(errors, phase["seq"], where + ".seq")
    if "kind" in phase:
        _check_enum(errors, phase["kind"], PHASE_KIND, where + ".kind")
    _check_str(errors, phase.get("title", _DYNAMIC), where + ".title")
    if "status" in phase:
        _check_enum(errors, phase["status"], PHASE_STATUS, where + ".status")
    if "tasks" in phase:
        tasks = phase["tasks"]
        if _check_list(errors, tasks, where + ".tasks"):
            for ti, task in enumerate(tasks):
                _validate_task(errors, task, phase.get("id"), f"{where}.tasks[{ti}]")
    elif "counts" in phase:
        # §6.1：非当前阶段的 tasks 只给 counts。
        if not isinstance(phase["counts"], dict):
            errors.append(f"{where}.counts: 应为 object")
    else:
        errors.append(f"{where}: 缺少 tasks（当前阶段）或 counts（非当前阶段）")


def _validate_task(errors, task, phase_id, where):
    _check_keys(
        errors,
        task,
        {
            "id",
            "key",
            "phase",
            "batch",
            "deps",
            "agent",
            "title",
            "status",
            "fail_reason",
            "card",
            "executions",
            "land",
            "notice",
            "changed_files",
        },
        where,
    )
    if not isinstance(task, dict):
        return
    if "id" in task:
        _check_str(errors, task["id"], where + ".id")
        if _is_str(task["id"]) and not task["id"].startswith("task_"):
            errors.append(f"{where}.id: 应以 task_ 开头，实际 {task['id']!r}")
    _check_str(errors, task.get("key", _DYNAMIC), where + ".key")
    if "phase" in task and phase_id is not None and task["phase"] != phase_id:
        errors.append(
            f"{where}.phase: {task['phase']!r} 与所属阶段 {phase_id!r} 不一致"
        )
    if "batch" in task:
        _check_int(errors, task["batch"], where + ".batch")
    if "deps" in task:
        if _check_list(errors, task["deps"], where + ".deps"):
            for di, dep in enumerate(task["deps"]):
                _check_str(errors, dep, f"{where}.deps[{di}]")
    if "agent" in task:
        _check_enum(errors, task["agent"], AGENTS, where + ".agent")
    _check_str(errors, task.get("title", _DYNAMIC), where + ".title")
    if "status" in task:
        _check_enum(errors, task["status"], TASK_STATUS, where + ".status")
    if "fail_reason" in task:
        _check_enum(errors, task["fail_reason"], FAIL_REASON, where + ".fail_reason")
    if "card" in task:
        _validate_card(errors, task["card"], where + ".card")
    if "executions" in task:
        if _check_list(errors, task["executions"], where + ".executions"):
            for ei, execution_id in enumerate(task["executions"]):
                _check_str(errors, execution_id, f"{where}.executions[{ei}]")
    if "land" in task:
        _check_enum(errors, task["land"], LAND, where + ".land")
    _check_str(errors, task.get("notice", _DYNAMIC), where + ".notice")
    if "changed_files" in task:
        if _check_list(errors, task["changed_files"], where + ".changed_files"):
            for fi, filename in enumerate(task["changed_files"]):
                _check_str(errors, filename, f"{where}.changed_files[{fi}]")
    if task.get("execution") is not None:
        _validate_execution(errors, task["execution"], task.get("id"), where + ".execution")


def _validate_card(errors, card, where):
    _check_keys(
        errors,
        card,
        {"mode", "writes", "net", "contract", "budget"},
        where,
        exact=False,
    )
    if not isinstance(card, dict):
        return
    if "mode" in card:
        _check_enum(errors, card["mode"], CARD_MODE, where + ".mode")
    if "writes" in card and _check_list(errors, card["writes"], where + ".writes"):
        for wi, pattern in enumerate(card["writes"]):
            _check_str(errors, pattern, f"{where}.writes[{wi}]")
    if "net" in card:
        _check_bool(errors, card["net"], where + ".net")
    if "source" in card:
        _check_enum(errors, card["source"], CARD_SOURCE, where + ".source")
    if "isolate" in card:
        _check_enum(errors, card["isolate"], CARD_ISOLATE, where + ".isolate")
    contract = card.get("contract")
    if contract is not None:
        _check_keys(errors, contract, {"id", "version"}, where + ".contract")
        if isinstance(contract, dict):
            _check_str(errors, contract.get("id", _DYNAMIC), where + ".contract.id")
            if "version" in contract:
                _check_int(errors, contract["version"], where + ".contract.version")
    budget = card.get("budget")
    if isinstance(budget, dict):
        if "minutes" in budget:
            _check_int(errors, budget["minutes"], where + ".budget.minutes")
        tokens = budget.get("tokens")
        if tokens is not None:
            _check_int(errors, tokens, where + ".budget.tokens")
    elif "budget" in card:
        errors.append(f"{where}.budget: 应为 object")


def _validate_execution(errors, execution, task_id, where):
    _check_keys(
        errors,
        execution,
        {
            "id",
            "task",
            "agent",
            "driver",
            "safety_tier",
            "capabilities",
            "state",
            "flags",
            "started",
            "last_event_ts",
            "usage",
            "now",
            "turns",
        },
        where,
    )
    if not isinstance(execution, dict):
        return
    if "id" in execution:
        _check_str(errors, execution["id"], where + ".id")
        if _is_str(execution["id"]) and not execution["id"].startswith("xs_"):
            errors.append(f"{where}.id: 应以 xs_ 开头，实际 {execution['id']!r}")
    if "task" in execution and task_id is not None and execution["task"] != task_id:
        errors.append(
            f"{where}.task: {execution['task']!r} 与所属任务 {task_id!r} 不一致"
        )
    agent = execution.get("agent")
    if "agent" in execution:
        _check_enum(errors, agent, AGENTS, where + ".agent")
    driver = execution.get("driver")
    if "driver" in execution:
        _check_enum(errors, driver, DRIVERS, where + ".driver")
        if _is_str(agent) and _is_str(driver) and not driver.startswith(agent + "-"):
            errors.append(
                f"{where}.driver: {driver!r} 与 agent {agent!r} 不匹配"
            )
    if "safety_tier" in execution:
        _check_enum(
            errors, execution["safety_tier"], SAFETY_TIER, where + ".safety_tier"
        )
    if "capabilities" in execution:
        _validate_capabilities(errors, execution["capabilities"], driver, where)
    if "state" in execution:
        _check_enum(errors, execution["state"], EXECUTION_STATE, where + ".state")
    if "flags" in execution and _check_list(errors, execution["flags"], where + ".flags"):
        for fi, flag in enumerate(execution["flags"]):
            _check_str(errors, flag, f"{where}.flags[{fi}]")
    for key in ("started", "last_event_ts"):
        if key in execution:
            _check_int(errors, execution[key], f"{where}.{key}")
    if "usage" in execution:
        usage = execution["usage"]
        if isinstance(usage, dict):
            for key in ("in", "out", "cached"):
                if key in usage:
                    _check_int(errors, usage[key], f"{where}.usage.{key}")
            if "granularity" in usage:
                _check_enum(
                    errors,
                    usage["granularity"],
                    USAGE_GRANULARITY,
                    where + ".usage.granularity",
                )
        else:
            errors.append(f"{where}.usage: 应为 object")
    if "now" in execution and execution["now"] is not None:
        now = execution["now"]
        if isinstance(now, dict):
            _check_keys(errors, now, {"kind", "text", "raw", "state"}, where + ".now")
        else:
            errors.append(f"{where}.now: 应为 object 或 null")
    turns = execution.get("turns")
    if _check_list(errors, turns, where + ".turns"):
        for ti, turn in enumerate(turns):
            _validate_turn(errors, turn, f"{where}.turns[{ti}]")


def _validate_capabilities(errors, caps, driver, where):
    _check_keys(errors, caps, CAPABILITY_KEYS, where + ".capabilities", exact=True)
    if not isinstance(caps, dict):
        return
    for key, allowed in CAPABILITY_ENUMS.items():
        if key in caps:
            _check_enum(
                errors, caps[key], allowed, f"{where}.capabilities.{key}"
            )
    for key in CAPABILITY_BOOLS:
        if key in caps:
            _check_bool(errors, caps[key], f"{where}.capabilities.{key}")
    expected = DRIVER_CAPABILITIES.get(driver) if _is_str(driver) else None
    if expected:
        for key, want in expected.items():
            if key in caps and caps[key] is not _DYNAMIC and caps[key] != want:
                errors.append(
                    f"{where}.capabilities.{key}: {caps[key]!r} 与 §5.2 "
                    f"{driver} 矩阵不一致（应为 {want!r}）"
                )


def _validate_turn(errors, turn, where):
    _check_keys(
        errors,
        turn,
        {"id", "seq", "kind", "status"},
        where,
    )
    if not isinstance(turn, dict):
        return
    _check_str(errors, turn.get("id", _DYNAMIC), where + ".id")
    if "seq" in turn:
        _check_int(errors, turn["seq"], where + ".seq")
    if "kind" in turn:
        _check_enum(errors, turn["kind"], TURN_KIND, where + ".kind")
    if "status" in turn:
        _check_enum(errors, turn["status"], TURN_STATUS, where + ".status")
    if turn.get("native_turn_id") is not None:
        _check_str(errors, turn["native_turn_id"], where + ".native_turn_id")


def _validate_decisions(errors, decisions):
    if not _check_list(errors, decisions, "demo.decisions"):
        return
    for di, decision in enumerate(decisions):
        where = f"demo.decisions[{di}]"
        _check_keys(
            errors,
            decision,
            {
                "id",
                "kind",
                "owner",
                "version",
                "ws",
                "session",
                "phase",
                "task",
                "execution",
                "title",
                "detail",
                "options",
                "created",
                "resolved",
            },
            where,
        )
        if not isinstance(decision, dict):
            continue
        if "id" in decision:
            _check_str(errors, decision["id"], where + ".id")
            if _is_str(decision["id"]) and not decision["id"].startswith("dec_"):
                errors.append(f"{where}.id: 应以 dec_ 开头，实际 {decision['id']!r}")
        if "kind" in decision:
            _check_enum(errors, decision["kind"], DECISION_KIND, where + ".kind")
        if "owner" in decision:
            _check_enum(errors, decision["owner"], DECISION_OWNER, where + ".owner")
        if "version" in decision:
            _check_int(errors, decision["version"], where + ".version")
        for key in ("ws", "session", "phase", "task", "execution"):
            _check_str(errors, decision.get(key, _DYNAMIC), f"{where}.{key}")
        _check_str(errors, decision.get("title", _DYNAMIC), where + ".title")
        _check_str(errors, decision.get("detail", _DYNAMIC), where + ".detail")
        options = decision.get("options")
        if _check_list(errors, options, where + ".options"):
            if not options:
                errors.append(f"{where}.options: 至少要有 1 个选项")
            for oi, option in enumerate(options):
                _check_keys(
                    errors,
                    option,
                    {"id", "label", "danger"},
                    f"{where}.options[{oi}]",
                )
                if isinstance(option, dict):
                    _check_str(
                        errors,
                        option.get("id", _DYNAMIC),
                        f"{where}.options[{oi}].id",
                    )
                    _check_str(
                        errors,
                        option.get("label", _DYNAMIC),
                        f"{where}.options[{oi}].label",
                    )
                    if "danger" in option:
                        _check_bool(
                            errors,
                            option["danger"],
                            f"{where}.options[{oi}].danger",
                        )
        if "created" in decision:
            _check_int(errors, decision["created"], where + ".created")
        resolved = decision.get("resolved")
        if resolved is not None:
            _check_keys(
                errors, resolved, {"ts", "by", "option", "text"}, where + ".resolved"
            )


def _validate_agents(errors, agents):
    if not isinstance(agents, dict):
        errors.append(f"demo.agents: 应为 object，实际 {_type_name(agents)}")
        return
    missing = sorted(AGENTS - set(agents))
    if missing:
        errors.append(f"demo.agents: 缺少 AI 成员 {missing}")
    extra = sorted(set(agents) - AGENTS)
    if extra:
        errors.append(f"demo.agents: 多出契约外成员 {extra}")
    for name, agent in agents.items():
        where = f"demo.agents.{name}"
        if not isinstance(agent, dict):
            errors.append(f"{where}: 应为 object")
            continue
        _check_keys(
            errors,
            agent,
            {"name", "role", "model", "slots", "memory_mb"},
            where,
        )
        for key in ("name", "role", "model"):
            _check_str(errors, agent.get(key, _DYNAMIC), f"{where}.{key}")
        slots = agent.get("slots")
        if isinstance(slots, dict):
            for key in ("used", "max"):
                if key in slots:
                    _check_int(errors, slots[key], f"{where}.slots.{key}")
            if "used" in slots and "max" in slots:
                if _is_int(slots["used"]) and _is_int(slots["max"]):
                    if slots["used"] > slots["max"]:
                        errors.append(f"{where}.slots: used 大于 max")
        else:
            errors.append(f"{where}.slots: 应为 object")
        if "memory_mb" in agent:
            _check_int(errors, agent["memory_mb"], where + ".memory_mb")
        if "current" in agent and _check_list(
            errors, agent["current"], where + ".current"
        ):
            for ci, current in enumerate(agent["current"]):
                _check_keys(
                    errors,
                    current,
                    {"ws", "session", "task", "title", "now", "elapsed_s"},
                    f"{where}.current[{ci}]",
                )
        if name == "agy" and "daily_cap" in agent:
            daily = agent["daily_cap"]
            if isinstance(daily, dict):
                for key in ("used", "max"):
                    if key in daily:
                        _check_int(errors, daily[key], f"{where}.daily_cap.{key}")
            else:
                errors.append(f"{where}.daily_cap: 应为 object")


def _validate_host(errors, host):
    if not isinstance(host, dict):
        errors.append(f"demo.host: 应为 object，实际 {_type_name(host)}")
        return
    _check_keys(
        errors, host, {"processes", "memory_mb", "xay_disk_mb"}, "demo.host"
    )
    for key in ("processes", "memory_mb", "xay_disk_mb"):
        if key in host:
            _check_int(errors, host[key], f"demo.host.{key}")


# ---------------------------------------------------------------------------
# 测试
# ---------------------------------------------------------------------------


class WatchHtmlContractTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        if not HTML_PATH.exists():
            raise unittest.SkipTest(f"页面不存在：{HTML_PATH}")
        cls.html = HTML_PATH.read_text(encoding="utf-8")

    def test_demo_object_is_discoverable(self):
        # v5：页面支持 ?demo=1，示例数据按 ui-v5-api.md 组织为 工作区 → 负责人对话
        self.assertRegex(self.html, r"get\(\s*['\"]demo['\"]\s*\)", "页面必须支持 ?demo=1 示例模式")
        self.assertIn("conversations", self.html, "示例数据应按 工作区 → conversations 组织")
        self.assertIn("EventSource", self.html, "页面应使用 SSE 实时更新")

    def test_scripts_are_syntactically_valid(self):
        node = shutil.which("node")
        if not node:
            self.skipTest("本机没有 node，跳过 node --check")
        blocks = _script_blocks(self.html)
        self.assertTrue(blocks, "页面里没有内联 <script> 可检查")
        for index, block in enumerate(blocks):
            with self.subTest(script=index):
                with tempfile.NamedTemporaryFile(
                    "w", suffix=".js", encoding="utf-8", delete=False
                ) as handle:
                    handle.write(block)
                    path = handle.name
                try:
                    result = subprocess.run(
                        [node, "--check", path],
                        capture_output=True,
                        text=True,
                        timeout=60,
                    )
                finally:
                    Path(path).unlink(missing_ok=True)
                self.assertEqual(
                    result.returncode,
                    0,
                    f"内联 <script> #{index} 语法错误：\n{result.stderr.strip()}",
                )

    def test_demo_state_matches_arch_v4_section_3(self):
        self.skipTest("v5 页面的示例数据按 ui-v5-api.md 组织，v4 结构校验不再适用；接口结构由 test_v5_conversation 覆盖")

    def test_no_percentage_progress_bar(self):
        html = self.html
        self.assertNotRegex(
            html,
            r"<\s*progress\b",
            "页面不允许使用 <progress> 百分比进度条",
        )
        self.assertNotRegex(
            html,
            r"role\s*=\s*[\"']progressbar[\"']",
            "页面不允许 role=progressbar",
        )
        self.assertNotRegex(
            html,
            r"aria-valuenow\s*=",
            "页面不允许 aria-valuenow（百分比进度语义）",
        )
        # 名为 progress/progressbar/meter 的容器里用百分比宽度，也是进度条。
        for tag in re.findall(r"<[^>]+>", html):
            if not re.search(
                r"(?:class|id)\s*=\s*[\"'][^\"']*"
                r"(?:progress|progressbar|meter)[^\"']*[\"']",
                tag,
                re.IGNORECASE,
            ):
                continue
            style = re.search(
                r"style\s*=\s*[\"']([^\"']*)[\"']", tag, re.IGNORECASE
            )
            if style and re.search(
                r"width\s*:\s*[^;\"']*%", style.group(1), re.IGNORECASE
            ):
                self.fail(f"发现百分比进度条：{tag[:160]}")
        # <style> 里针对进度填充条写百分比宽度，同样不允许。
        styles = "\n".join(
            re.findall(r"<style\b[^>]*>(.*?)</style\s*>", html, re.IGNORECASE | re.DOTALL)
        )
        for rule in re.finditer(r"([^{}]+)\{([^{}]*)\}", styles):
            selector, declarations = rule.group(1), rule.group(2)
            if not re.search(
                r"(?:progress|meter)[^,{]*?(?:bar|fill|value|percent)",
                selector,
                re.IGNORECASE,
            ):
                continue
            if re.search(
                r"width\s*:[^;}]*%",
                declarations,
                re.IGNORECASE,
            ):
                self.fail(
                    "发现百分比进度条："
                    + re.sub(r"\s+", " ", rule.group(0))[:160]
                )

    def test_no_external_urls(self):
        html = self.html
        patterns = {
            "外链 <script src>": r"<script\b[^>]*\bsrc\s*=",
            "外链样式/资源 <link href>": (
                r"<link\b[^>]*\bhref\s*=\s*[\"']?\s*(?:https?:)?//"
            ),
            "外链 <img src>": (
                r"<img\b[^>]*\bsrc\s*=\s*[\"']?\s*(?:https?:)?//"
            ),
            "外链 <iframe src>": (
                r"<iframe\b[^>]*\bsrc\s*=\s*[\"']?\s*(?:https?:)?//"
            ),
            "CSS @import": (
                r"@import\s+(?:url\(\s*)?[\"']?\s*(?:https?:)?//"
            ),
            "CSS url()": r"url\(\s*[\"']?\s*(?:https?:)?//",
            "fetch 外链": r"fetch\(\s*[\"']\s*(?:https?:)?//",
            "JS import 外链": (
                r"(?:\bfrom|\bimport\s*\()\s*[\"']\s*(?:https?:)?//"
            ),
            "XHR 外链": (
                r"\.open\(\s*[\"'][A-Za-z]+[\"']\s*,\s*[\"']\s*(?:https?:)?//"
            ),
        }
        for label, pattern in patterns.items():
            match = re.search(pattern, html, re.IGNORECASE)
            self.assertIsNone(
                match,
                f"页面引用了外部 URL（{label}）：{match.group(0) if match else ''}",
            )


if __name__ == "__main__":
    unittest.main()
