#!/usr/bin/env python3
"""xay v4 的单任务托管进程。

这个模块只做胶水工作：启动统一 Driver、把事件写成两份日志、提供一个
NDJSON Unix socket，并在结束时把运行结果写入 meta.json。具体 AI 通道由
``scripts/xay_drivers`` 提供；该目录尚未存在时，导入和事件归一化都会温和
失败，方便先用测试假驱动验证控制平面。
"""

from __future__ import annotations

import argparse
import importlib
import importlib.util
import json
import os
import queue
import re
import signal
import socket
import struct
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Any, Dict, Iterable, Iterator, Optional

_PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))


KNOWN_EVENT_TYPES = {
    "session.started", "turn.started", "turn.completed", "reasoning", "message",
    "tool.call", "tool.result", "file.changed", "usage", "approval.requested",
    "question", "denied", "warning", "error", "control.requested", "control.acked",
    "control.failed", "signal", "decision.created", "decision.resolved", "note.updated",
}


_PUMP_END = object()

# ui-v5-api §4：<run_dir>/stream.jsonl 只保留当前一轮的文字增量，最多 512KB。
STREAM_MAX_BYTES = 512 * 1024


def stream_delta(raw: Any) -> tuple[str, str | None] | None:
    """从驱动原始事件里取出“回复文字增量”；不是文字增量返回 None。"""
    if isinstance(raw, bytes):
        raw = raw.decode("utf-8", "replace")
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except ValueError:
            return None
    if not isinstance(raw, dict):
        return None
    method = raw.get("method")
    if method == "item/agentMessage/delta":  # codex app-server
        params = raw.get("params") if isinstance(raw.get("params"), dict) else {}
        text = params.get("delta") or params.get("text")
        return (str(text), str(params.get("itemId") or "") or None) if text else None
    if raw.get("event") == "step_update":  # agy stream-json
        step = raw.get("step_update") if isinstance(raw.get("step_update"), dict) else {}
        if step.get("step_type", step.get("stepType")) != "agent_response":
            return None
        text = step.get("text_delta") or step.get("textDelta")
        index = step.get("step_index", step.get("stepIndex"))
        return (str(text), "step:%s" % index if index is not None else None) if text else None
    if raw.get("type") == "text" and isinstance(raw.get("text"), str) and raw.get("text"):  # dsh headless
        return raw["text"], None
    return None


class _PumpError(Exception):
    pass


def _now() -> float:
    return time.time()


def _read_json(path: Path, default: Any = None) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, ValueError, TypeError):
        return default


def _write_json_atomic(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(value, ensure_ascii=False, separators=(",", ":")) + "\n", encoding="utf-8")
    os.replace(tmp, path)


def _append_jsonl(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(value, ensure_ascii=False, separators=(",", ":")) + "\n")
        handle.flush()


def _as_dict(value: Any) -> Dict[str, Any]:
    if isinstance(value, dict):
        return dict(value)
    if hasattr(value, "__dict__"):
        return dict(vars(value))
    return {"value": value}


def _safe_call(fn: Any, *args: Any, **kwargs: Any) -> Any:
    """兼容已经写好和仍在开发中的 Driver 签名。"""

    if not callable(fn):
        return None
    try:
        return fn(*args, **kwargs)
    except TypeError as first:
        # 统一接口是位置参数；测试假驱动常只收一个参数。只在签名不匹配时重试。
        if kwargs:
            try:
                return fn(*args)
            except TypeError:
                pass
        if len(args) > 1:
            try:
                return fn(args[0])
            except TypeError:
                pass
        raise first


def _driver_module_names(name: str) -> list[str]:
    clean = str(name or "").strip().replace("-", "_")
    aliases = {
        "codex_app_server": ["codex_appserver", "codex_app_server"],
        "codex_appserver": ["codex_appserver", "codex_app_server"],
        "agy_stream": ["agy_stream"],
        "agy_oneshot": ["agy_oneshot"],
        "dsh_headless": ["dsh_headless"],
        "dsh": ["dsh_headless"],
    }
    return aliases.get(clean, [clean])


def load_driver(name: str, *, package: str = "scripts.xay_drivers", base_dir: str | os.PathLike[str] | None = None) -> Any:
    """从统一驱动目录加载一个驱动实例。

    支持 ``create()``、``get_driver()``、``Driver`` 和 ``driver`` 四种常见
    导出形式，后续驱动作者可以选择最自然的一种。
    """

    errors: list[str] = []
    for module_name in _driver_module_names(name):
        full_name = f"{package}.{module_name}"
        try:
            module = importlib.import_module(full_name)
        except (ImportError, ModuleNotFoundError) as exc:
            errors.append(str(exc))
            module = None
        if module is None and base_dir:
            path = Path(base_dir) / (module_name + ".py")
            if path.is_file():
                spec = importlib.util.spec_from_file_location(full_name, path)
                if spec and spec.loader:
                    module = importlib.util.module_from_spec(spec)
                    spec.loader.exec_module(module)
        if module is None:
            continue
        for attr in ("create", "get_driver"):
            factory = getattr(module, attr, None)
            if callable(factory):
                return factory()
        klass = getattr(module, "Driver", None)
        if isinstance(klass, type) and klass.__module__ == module.__name__:
            return klass()
        for candidate in vars(module).values():
            if not isinstance(candidate, type):
                continue
            if candidate.__module__ != module.__name__:
                continue
            if candidate.__name__ in ("Driver", "Capabilities", "ExecHandle"):
                continue
            if callable(getattr(candidate, "start", None)):
                return candidate()
        candidate = getattr(module, "driver", None)
        if candidate is not None:
            return candidate
    detail = "; ".join(errors[-2:])
    raise ImportError(f"找不到驱动 {name!r}{(': ' + detail) if detail else ''}")


class _FallbackWatchdog:
    def feed(self, event: Dict[str, Any]) -> Any:  # pragma: no cover - 只是预留钩子
        return None


class XayAgent:
    """一个运行目录对应一个托管进程。"""

    def __init__(self, run_dir: str | os.PathLike[str], *, driver: Any = None,
                 driver_name: str | None = None, xay_home: str | os.PathLike[str] | None = None,
                 start: Dict[str, Any] | None = None, watchdog: Any = None,
                 normalizer: Any = None, executor_pids: Iterable[int] | None = None):
        self.run_dir = Path(run_dir).expanduser().resolve()
        self.run_dir.mkdir(parents=True, exist_ok=True)
        self.start_data = dict(start or _read_json(self.run_dir / "start.json", {}) or {})
        self.run_id = str(self.start_data.get("run") or self.run_dir.name)
        self.xay_home = Path(xay_home or os.environ.get("XAY_HOME", str(Path.home() / ".xay"))).expanduser().resolve()
        self.ctl_dir = self.xay_home / "ctl"
        self.ctl_sock = self.ctl_dir / (self.run_id + ".sock")
        self.audit_path = self.ctl_dir / (self.run_id + ".audit.jsonl")
        self.driver_name = driver_name or self.start_data.get("driver") or self.start_data.get("agent") or ""
        self.driver = driver
        self.watchdog = watchdog or _FallbackWatchdog()
        self.normalizer = normalizer
        self.executor_pids = {int(pid) for pid in (executor_pids or ()) if str(pid).isdigit()}
        self.handle: Any = None
        self.native_session_id = self.start_data.get("native_session_id") or self.start_data.get("session_id")
        self.current_turn: Any = self.start_data.get("turn")
        self.state = "starting"
        self.started_at = _now()
        self.ended_at: float | None = None
        self.exit_code = 0
        self.fail_reason = ""
        self.stop_requested = False
        self.orphaned = False
        self._signal_received: int | None = None
        self._finalized = False
        self._socket_stop = threading.Event()
        self._server_thread: threading.Thread | None = None
        self._control_threads: list[threading.Thread] = []
        self._control_lock = threading.RLock()
        self._handle_lock = threading.RLock()
        self._event_lock = threading.RLock()
        self._local_seq = 0
        self._normalizer_state: Any = None
        # ---- 回合/暂停状态（§3.3） -------------------------------------
        self._active_turn = False
        self._interrupt_pending = False
        self._pausing_since: float | None = None
        self._interrupting = threading.Event()
        self._interrupt_mode = "kill"
        self._pause_timeout = max(0.2, float(os.environ.get("XAY_PAUSE_TIMEOUT_S", "2.0") or 2.0))
        self._done_at: float | None = None
        self._done_grace = max(0.0, float(os.environ.get("XAY_DONE_GRACE_S", "2.0") or 0))
        self._pending_text = ""
        self._last_prompt_text = ""
        self._quota_retried = False
        self._quota_exhausted = False
        # 事件泵：驱动读取放在线程里，控制 socket 才能在回合进行中响应。
        self._evt_queue: "queue.Queue[Any]" = queue.Queue()
        self._pump_gen = 0
        self._pump_thread: threading.Thread | None = None
        self._meta_lock = threading.RLock()
        self._seen_start_sessions: set[str] = set()
        self._event_fingerprints: set[str] = set()
        self._turn_seq_seen = 0
        self._stream_lock = threading.Lock()
        self._stream_size = 0
        self._stream_segment = 0
        self._stream_last_text = False
        self._load_event_history()
        self._seen_decisions: dict[str, Dict[str, Any]] = {}
        self._load_decisions()
        self._old_handlers: dict[int, Any] = {}

    @property
    def workdir(self) -> str:
        return str(self.start_data.get("workdir") or self.start_data.get("cwd") or self.run_dir.parent.parent)

    @property
    def safety(self) -> Dict[str, Any]:
        value = self.start_data.get("safety") or self.start_data.get("workspace") or {}
        if isinstance(value, dict):
            result = dict(value)
        else:
            result = {"safety_tier": value}
        # xay-run 把 safety_tier/confidential 写在 start.json 顶层。
        for key in ("safety_tier", "confidential"):
            if self.start_data.get(key) is not None:
                result.setdefault(key, self.start_data[key])
        return result

    @property
    def card(self) -> Dict[str, Any]:
        value = self.start_data.get("card")
        if isinstance(value, dict):
            card = dict(value)
        else:
            card = {}
        prompt = card.get("prompt")
        if prompt is None:
            prompt = self.start_data.get("prompt")
        if prompt is None:
            try:
                prompt = (self.run_dir / "prompt.md").read_text(encoding="utf-8")
            except OSError:
                prompt = ""
        card.setdefault("prompt", prompt)
        for key in ("effort", "model", "source", "mode", "net", "confidential"):
            if self.start_data.get(key) is not None:
                card.setdefault(key, self.start_data[key])
        return card

    # ---- 事件历史：seq 连续 + 重启/续接不重复 --------------------------

    _DEDUPE_TYPES = ("session.started", "turn.started", "usage")

    @staticmethod
    def _fingerprint(event: Dict[str, Any]) -> str:
        payload = {
            "type": event.get("type"),
            "kind": event.get("kind"),
            "path": event.get("path"),
            "text": event.get("text"),
            "data": event.get("data"),
        }
        return json.dumps(payload, ensure_ascii=False, sort_keys=True, default=str)

    def _load_event_history(self) -> None:
        """从已有 events.norm.jsonl 恢复序号和幂等事件，避免第二个进程从 1 重写。"""
        path = self.run_dir / "events.norm.jsonl"
        try:
            lines = path.read_text(encoding="utf-8").splitlines()
        except OSError:
            return
        for line in lines:
            try:
                event = json.loads(line)
            except (ValueError, TypeError):
                continue
            if not isinstance(event, dict):
                continue
            try:
                self._local_seq = max(self._local_seq, int(event.get("seq") or 0))
            except (TypeError, ValueError):
                pass
            typ = event.get("type")
            data = event.get("data") if isinstance(event.get("data"), dict) else {}
            if typ == "session.started":
                native = data.get("native_session_id") or event.get("session")
                if native:
                    self._seen_start_sessions.add(str(native))
            elif typ == "turn.started":
                try:
                    self._turn_seq_seen = max(self._turn_seq_seen, int(data.get("turn_seq") or 0))
                except (TypeError, ValueError):
                    pass
            if typ in self._DEDUPE_TYPES:
                self._event_fingerprints.add(self._fingerprint(event))

    def _seed_normalizer(self, normalizer: Any) -> None:
        started = getattr(normalizer, "_session_started", None)
        if isinstance(started, set):
            started.update(self._seen_start_sessions)
        if hasattr(normalizer, "_turn_seq"):
            try:
                normalizer._turn_seq = max(int(getattr(normalizer, "_turn_seq", 0) or 0),
                                           self._turn_seq_seen)
            except (TypeError, ValueError):
                pass

    def _write_meta_soon(self) -> None:
        """状态变化时落盘，便于 xay ctl / 看板在 paused 期间看到真实状态。"""
        try:
            self._write_meta()
        except OSError:
            pass

    def _load_decisions(self) -> None:
        path = self.run_dir / "decisions.jsonl"
        try:
            lines = path.read_text(encoding="utf-8").splitlines()
        except OSError:
            return
        for line in lines:
            try:
                item = json.loads(line)
            except (ValueError, TypeError):
                continue
            if isinstance(item, dict) and item.get("id"):
                self._seen_decisions[str(item["id"])] = item

    def _driver_pid(self) -> int | None:
        for value in (getattr(self.handle, "cli_pid", None), getattr(self.handle, "pid", None),
                      getattr(self.driver, "cli_pid", None), getattr(self.driver, "pid", None)):
            try:
                if value is not None:
                    return int(value)
            except (TypeError, ValueError):
                pass
        return None

    def _update_session(self, value: Any) -> None:
        if value is None:
            return
        if isinstance(value, dict):
            value = value.get("native_session_id") or value.get("session_id") or value.get("thread_id") or value.get("conversation_id")
        if value is not None and not isinstance(value, (dict, list)):
            self.native_session_id = value

    def _normalize_module(self) -> Any:
        if self.normalizer is not None:
            return self.normalizer
        try:
            return importlib.import_module("scripts.xay_normalize")
        except (ImportError, ModuleNotFoundError):
            return None

    def _fallback_normalize(self, raw: Any) -> Dict[str, Any]:
        event = _as_dict(raw)
        if "event" in event and isinstance(event["event"], dict):
            event = dict(event["event"])
        if "type" not in event:
            # 驱动事件常用 event 字段；没有类型时保留为 warning，数据仍可追溯。
            event["type"] = event.get("kind") if event.get("kind") in KNOWN_EVENT_TYPES else "message"
        event["type"] = str(event.get("type") or "message")
        event.setdefault("data", _as_dict(raw))
        return event

    def _assign_seq(self, event: Dict[str, Any]) -> None:
        with self._event_lock:
            try:
                candidate = int(event.get("seq")) if event.get("seq") is not None else None
            except (TypeError, ValueError):
                candidate = None
            if candidate is not None and candidate >= self._local_seq:
                self._local_seq = candidate
                return
            # 驱动自带的序号不可信（尤其是重启后的 1）；本地序号必须单调。
            self._local_seq += 1
            event["seq"] = self._local_seq

    def _normalize(self, raw: Any) -> list[Dict[str, Any]]:
        module = self._normalize_module()
        result: Any = None
        if module is not None:
            if module is importlib.import_module("scripts.xay_normalize") and hasattr(module, "Normalizer"):
                context = {
                    "ws": self.start_data.get("ws") or self.start_data.get("workspace"),
                    "session": self.start_data.get("session"),
                    "phase": self.start_data.get("phase"),
                    "task": self.start_data.get("task"),
                    "key": self.start_data.get("key"),
                    "execution": self.start_data.get("execution") or self.run_id,
                    "turn": self.start_data.get("turn"),
                }
                source = getattr(self.driver, "name", None) or self.driver_name or "auto"
                if self._normalizer_state is None:
                    self._normalizer_state = module.Normalizer(
                        source, context=context, assign_seq=False
                    )
                    self._seed_normalizer(self._normalizer_state)
                result = self._normalizer_state.feed(raw)
            else:
                for name in ("normalize_event", "normalize", "event_to_envelope", "to_envelope"):
                    fn = getattr(module, name, None)
                    if not callable(fn):
                        continue
                    try:
                        result = fn(raw, self.start_data)
                    except TypeError:
                        try:
                            result = fn(raw)
                        except TypeError:
                            continue
                    break
        if result is None:
            result = self._fallback_normalize(raw)
        if isinstance(result, (list, tuple)):
            items = list(result)
        else:
            items = [result]
        output: list[Dict[str, Any]] = []
        for item in items:
            if item is None:
                continue
            envelope = _as_dict(item)
            # seq 由 _record_event 统一分配：被去重/过滤的事件不能占用序号。
            envelope.setdefault("ts", _now())
            for key in ("ws", "session", "phase", "task", "key", "execution", "turn"):
                if envelope.get(key) is None:
                    source_key = {"ws": "workspace", "session": "session_id", "execution": "run"}.get(key, key)
                    value = envelope.get(source_key, self.start_data.get(source_key))
                    if key == "execution" and value is None:
                        value = self.run_id
                    if value is not None:
                        envelope[key] = value
            if envelope.get("turn") is not None:
                self.current_turn = envelope["turn"]
            typ = str(envelope.get("type") or "message")
            envelope["type"] = typ
            output.append(envelope)
        return output

    def _flush_normalizer(self) -> None:
        normalizer = self._normalizer_state
        if normalizer is None:
            return
        close = getattr(normalizer, "close", None)
        if not callable(close):
            return
        for event in close() or ():
            if isinstance(event, dict):
                self._record_event(event)

    def _write_raw(self, raw: Any) -> None:
        path = self.run_dir / "events.raw"
        path.parent.mkdir(parents=True, exist_ok=True)
        if isinstance(raw, bytes):
            text = raw.decode("utf-8", "replace")
        elif isinstance(raw, str):
            text = raw
        else:
            text = json.dumps(raw, ensure_ascii=False, separators=(",", ":"))
        with path.open("a", encoding="utf-8") as handle:
            handle.write(text.rstrip("\n") + "\n")
            handle.flush()

    def _reset_stream(self) -> None:
        """新一轮开始：清空 stream.jsonl（上一轮的文字已在 events.norm.jsonl 里）。"""
        with self._stream_lock:
            try:
                (self.run_dir / "stream.jsonl").write_text("", encoding="utf-8")
            except OSError:
                pass
            self._stream_size = 0
            self._stream_segment = 0
            self._stream_last_text = False

    def _write_stream(self, raw: Any) -> None:
        try:
            delta = stream_delta(raw)
        except Exception:
            delta = None
        with self._stream_lock:
            if delta is None:
                self._stream_last_text = False
                return
            text, item = delta
            if item is None:  # dsh 没有消息 ID：被工具调用隔开的每段文字算一条
                if not self._stream_last_text:
                    self._stream_segment += 1
                item = "seg:%d" % self._stream_segment
            self._stream_last_text = True
            line = json.dumps({"ts": _now(), "turn": self._turn_seq_seen or None, "item": item,
                               "text_delta": text}, ensure_ascii=False) + "\n"
            size = len(line.encode("utf-8"))
            if self._stream_size + size > STREAM_MAX_BYTES:
                return  # 超过上限后本轮不再追加；完整回复仍会进 events.norm.jsonl
            try:
                with (self.run_dir / "stream.jsonl").open("a", encoding="utf-8") as handle:
                    handle.write(line)
                self._stream_size += size
            except OSError:
                pass

    def _feed_watchdog(self, event: Dict[str, Any]) -> None:
        try:
            result = self.watchdog.feed(event)
        except (AttributeError, TypeError):
            return
        if result is None:
            return
        values = result if isinstance(result, (list, tuple)) else [result]
        for item in values:
            if not isinstance(item, dict):
                continue
            if item.get("kind") in ("stalled", "loop", "over_time", "scope", "deviation", "low_confidence") or item.get("signal"):
                self.write_signal(item)
            elif item.get("decision") or item.get("options") or item.get("owner"):
                decision = item.get("decision") if isinstance(item.get("decision"), dict) else item
                self.write_decision(decision)

    def _is_duplicate_event(self, event: Dict[str, Any]) -> bool:
        typ = event.get("type")
        data = event.get("data") if isinstance(event.get("data"), dict) else {}
        if typ == "session.started":
            native = data.get("native_session_id") or event.get("session")
            if native and str(native) in self._seen_start_sessions:
                return True
            return False
        if typ in self._DEDUPE_TYPES:
            return self._fingerprint(event) in self._event_fingerprints
        return False

    def _record_event(self, event: Dict[str, Any], *, write_raw: bool = False,
                      feed_watchdog: bool = True) -> None:
        with self._event_lock:
            typ = event.get("type")
            if self._is_duplicate_event(event):
                return
            self._assign_seq(event)
            event.setdefault("ts", _now())
            event.setdefault("execution", self.run_id)
            data = event.get("data") if isinstance(event.get("data"), dict) else {}
            if typ == "session.started":
                native = data.get("native_session_id") or event.get("session")
                if native:
                    self._seen_start_sessions.add(str(native))
            if typ in self._DEDUPE_TYPES:
                self._event_fingerprints.add(self._fingerprint(event))
            try:
                if typ == "turn.started" and data.get("turn_seq") is not None:
                    self._turn_seq_seen = max(self._turn_seq_seen, int(data.get("turn_seq")))
            except (TypeError, ValueError):
                pass
            if write_raw:
                self._write_raw(event)
            _append_jsonl(self.run_dir / "events.norm.jsonl", event)
        if feed_watchdog:
            self._feed_watchdog(event)
        if typ == "session.started":
            # 信封里的 session 是 xay 负责人会话（ls_…），不是执行者的原生会话；续接必须用 data 里的原生 ID。
            self._update_session(data.get("native_session_id") or event.get("native_session_id")
                                 or data.get("session_id") or data.get("thread_id"))
        elif typ == "turn.started":
            self._reset_stream()
            self.state = "running"
            self._active_turn = True
            self.current_turn = event.get("turn") or event.get("id") or self.current_turn
        elif typ == "turn.completed":
            status = event.get("status") if isinstance(event.get("status"), str) else data.get("status")
            self._active_turn = False
            self._interrupt_pending = False
            if status in ("interrupted", "paused"):
                self.state = "paused"
                self._pausing_since = None
                self._write_meta_soon()
            elif status in ("failed", "error"):
                self.state = "failed"
                self.exit_code = 1
                if self._quota_exhausted:
                    self.fail_reason = self.fail_reason or "usage_limit_exceeded：两条线路额度都已用尽"
                else:
                    self.fail_reason = self.fail_reason or "cli_error"
            elif status in ("completed", "complete", "success", "succeeded", "done"):
                self.state = "done"
                self._done_at = time.monotonic()
                self._write_meta_soon()
            else:
                self.state = "running"
        elif typ == "error":
            if self._quota_exhausted:
                self.state = "failed"
                self.exit_code = 1
                self.fail_reason = self.fail_reason or "usage_limit_exceeded：两条线路额度都已用尽"
            elif not self._quota_retried:
                self.state = "failed"
                self.exit_code = 1
                self.fail_reason = self.fail_reason or "cli_error"

    def write_signal(self, signal_data: Dict[str, Any]) -> Dict[str, Any]:
        item = dict(signal_data)
        item.setdefault("id", "sig_" + time.strftime("%Y%m%d-%H%M%S") + "-" + os.urandom(2).hex())
        item.setdefault("created", _now())
        item.setdefault("task", self.start_data.get("task"))
        item.setdefault("execution", self.run_id)
        item.setdefault("acked", False)
        _append_jsonl(self.run_dir / "signals.jsonl", item)
        self._record_event({"type": "signal", "kind": item.get("kind", "warning"), "data": item},
                           write_raw=False, feed_watchdog=False)
        return item

    def write_decision(self, decision_data: Dict[str, Any]) -> Dict[str, Any]:
        item = dict(decision_data)
        item.pop("decision", None)
        item.setdefault("id", "dec_" + time.strftime("%Y%m%d-%H%M%S") + "-" + os.urandom(2).hex())
        item.setdefault("kind", "question")
        item.setdefault("owner", "lead")
        item.setdefault("version", 1)
        item.setdefault("created", _now())
        item.setdefault("resolved", None)
        item.setdefault("task", self.start_data.get("task"))
        item.setdefault("execution", self.run_id)
        self._seen_decisions[str(item["id"])] = item
        _append_jsonl(self.run_dir / "decisions.jsonl", item)
        self._record_event({"type": "decision.created", "data": {"id": item["id"]}},
                           write_raw=False, feed_watchdog=False)
        return item

    # ---- 控制 socket -------------------------------------------------

    def _peer_pid(self, conn: socket.socket) -> int | None:
        """读取 Unix socket 对端 pid；不同系统没有该能力时返回 None。"""

        local_peer_pid = getattr(socket, "LOCAL_PEERPID", 2)
        try:
            raw = conn.getsockopt(getattr(socket, "SOL_LOCAL", 0), local_peer_pid, 4)
            if isinstance(raw, int):
                return raw
            if len(raw) >= 4:
                return struct.unpack("i", raw[:4])[0]
        except OSError:
            pass
        # Linux 测试环境使用 SO_PEERCRED。
        try:
            raw = conn.getsockopt(socket.SOL_SOCKET, getattr(socket, "SO_PEERCRED", 17), 12)
            if len(raw) >= 12:
                return struct.unpack("3i", raw[:12])[2]
        except OSError:
            pass
        return None

    def _parent_pid(self, pid: int) -> int | None:
        try:
            raw = subprocess.check_output(["ps", "-o", "ppid=", "-p", str(pid)], stderr=subprocess.DEVNULL, text=True).strip()
            return int(raw) if raw else None
        except (OSError, ValueError, subprocess.SubprocessError):
            return None

    def _ancestor_chain(self, pid: int | None) -> list[int]:
        if pid is None:
            return []
        chain: list[int] = []
        seen: set[int] = set()
        current = int(pid)
        while current > 0 and current not in seen:
            chain.append(current)
            seen.add(current)
            parent = self._parent_pid(current)
            if parent is None or parent == current:
                break
            current = parent
        return chain

    def _peer_is_executor(self, pid: int | None, ancestors: list[int]) -> bool:
        executor_pids = set(self.executor_pids)
        driver_pid = self._driver_pid()
        if driver_pid:
            executor_pids.add(driver_pid)
        if executor_pids.intersection(ancestors):
            return True
        # 在 Linux 上补看环境变量；macOS 没有 /proc 时自然跳过。
        if pid:
            try:
                env = Path(f"/proc/{pid}/environ").read_bytes().split(b"\0")
                if b"XAY_ROLE=executor" in env:
                    return True
            except OSError:
                pass
        return False

    def _audit(self, request: Dict[str, Any], *, peer_pid: int | None, ancestors: list[int],
               ok: bool, error: str | None = None) -> None:
        item = {
            "ts": _now(), "pid": os.getpid(), "peer_pid": peer_pid,
            "ancestors": ancestors, "request": request, "ok": bool(ok),
        }
        if error:
            item["error"] = error
        if not ok and error == "executor_peer":
            item["kind"] = "security"
        _append_jsonl(self.audit_path, item)

    def _send_response(self, conn: socket.socket, result: Dict[str, Any]) -> None:
        conn.sendall((json.dumps(result, ensure_ascii=False, separators=(",", ":")) + "\n").encode("utf-8"))

    def _handle_connection(self, conn: socket.socket) -> None:
        with conn:
            peer_pid = self._peer_pid(conn)
            ancestors = self._ancestor_chain(peer_pid)
            buffer = b""
            try:
                while b"\n" not in buffer and len(buffer) < 1024 * 1024:
                    chunk = conn.recv(65536)
                    if not chunk:
                        break
                    buffer += chunk
                line = buffer.split(b"\n", 1)[0]
                request = json.loads(line.decode("utf-8")) if line else {}
                if not isinstance(request, dict):
                    raise ValueError("请求不是 JSON 对象")
            except (OSError, UnicodeError, ValueError, json.JSONDecodeError) as exc:
                error = f"invalid_request: {exc}"
                self._audit({}, peer_pid=peer_pid, ancestors=ancestors, ok=False, error=error)
                try:
                    self._send_response(conn, {"ok": False, "error": error})
                except OSError:
                    pass
                return
            if self._peer_is_executor(peer_pid, ancestors):
                self._audit(request, peer_pid=peer_pid, ancestors=ancestors, ok=False, error="executor_peer")
                self._send_response(conn, {"ok": False, "error": "执行者进程不能控制托管任务", "code": "security"})
                return
            try:
                result = self.handle_control(request)
                self._audit(request, peer_pid=peer_pid, ancestors=ancestors, ok=bool(result.get("ok")),
                            error=result.get("error"))
            except Exception as exc:  # 控制面不能因一个坏请求杀掉托管进程
                result = {"ok": False, "error": str(exc)}
                self._audit(request, peer_pid=peer_pid, ancestors=ancestors, ok=False, error=str(exc))
            try:
                self._send_response(conn, result)
            except OSError:
                pass

    def _socket_loop(self, listener: socket.socket) -> None:
        listener.settimeout(0.25)
        while not self._socket_stop.is_set():
            try:
                conn, _ = listener.accept()
            except socket.timeout:
                continue
            except OSError:
                break
            thread = threading.Thread(target=self._handle_connection, args=(conn,), daemon=True)
            self._control_threads.append(thread)
            thread.start()

    def start_control_socket(self) -> None:
        self.ctl_dir.mkdir(parents=True, exist_ok=True)
        try:
            self.ctl_sock.unlink()
        except FileNotFoundError:
            pass
        listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        listener.bind(str(self.ctl_sock))
        os.chmod(self.ctl_sock, 0o600)
        listener.listen(16)
        self._server_thread = threading.Thread(target=self._socket_loop, args=(listener,), daemon=True)
        self._server_thread.start()
        self._listener = listener

    def stop_control_socket(self) -> None:
        self._socket_stop.set()
        listener = getattr(self, "_listener", None)
        if listener is not None:
            try:
                listener.close()
            except OSError:
                pass
        for thread in list(self._control_threads):
            thread.join(timeout=0.2)
        try:
            self.ctl_sock.unlink()
        except FileNotFoundError:
            pass

    # ---- 事件泵与额度降级 ---------------------------------------------

    def _driver_events(self, handle: Any) -> Any:
        if self.driver is None:
            return iter(())
        events = getattr(self.driver, "events", None)
        if callable(events):
            try:
                result = events(handle)
            except TypeError:
                result = events()
            if result is not None:
                return result
        handle_events = getattr(handle, "events", None)
        return handle_events() if callable(handle_events) else iter(())

    def _start_event_pump(self, handle: Any = None) -> int:
        handle = handle if handle is not None else self.handle
        with self._handle_lock:
            self._pump_gen += 1
            gen = self._pump_gen
        thread = threading.Thread(target=self._pump_events, args=(gen, handle), daemon=True)
        self._pump_thread = thread
        thread.start()
        return gen

    def _invalidate_pump(self) -> None:
        with self._handle_lock:
            self._pump_gen += 1

    def _pump_events(self, gen: int, handle: Any) -> None:
        try:
            for raw in self._driver_events(handle):
                if gen != self._pump_gen:
                    break
                self._evt_queue.put((gen, raw))
        except Exception as exc:  # 驱动异常不能悄悄让托管进程挂住
            self._evt_queue.put((gen, _PumpError(str(exc))))
        finally:
            self._evt_queue.put((gen, _PUMP_END))

    _QUOTA_PATTERN = re.compile(
        r"at capacity|rate.?limit|usage.?limit|quota|429|too many requests", re.IGNORECASE)

    def _quota_info(self, raw: Any) -> str:
        """识别 codex app-server 的额度/容量失败；返回原因短语或空串。"""
        if not isinstance(raw, dict):
            return ""
        params = raw.get("params") if isinstance(raw.get("params"), dict) else {}
        turn = params.get("turn") if isinstance(params.get("turn"), dict) else {}
        error = params.get("error") or turn.get("error")
        method = str(raw.get("method") or raw.get("type") or "")
        info = ""
        message = ""
        if isinstance(error, dict):
            info = str(error.get("codexErrorInfo") or error.get("codex_error_info") or "")
            message = str(error.get("message") or "")
        elif error:
            message = str(error)
        if info.lower() in ("usagelimitexceeded", "usage_limit_exceeded",
                            "ratelimitexceeded", "rate_limit_exceeded",
                            "quotaexceeded", "quota_exceeded"):
            return info
        status = str(params.get("status") or turn.get("status") or "")
        if method in ("turn/completed", "turn/failed", "error"):
            if method == "turn/completed" and status not in ("failed", "error", ""):
                return ""
            blob = " ".join((message, info, json.dumps(raw, ensure_ascii=False, default=str)))
            match = self._QUOTA_PATTERN.search(blob)
            if match:
                return match.group(0)
        return ""

    def _retry_prompt(self) -> str:
        base = str(self.card.get("prompt") or "")
        last = str(self._last_prompt_text or "")
        if last and last != base:
            return (base + "\n\n补充指示：" + last).strip()
        return base or last

    def _quota_failed(self, detail: str) -> None:
        self._quota_exhausted = True
        self.state = "failed"
        self.exit_code = 1
        self.fail_reason = "usage_limit_exceeded：两条线路额度都已用尽（%s）" % (detail or "quota")
        self._write_meta_soon()

    def _handle_quota(self, raw: Any) -> bool:
        info = self._quota_info(raw)
        if not info:
            return False
        if self._quota_retried:
            self._quota_failed(info)
            self._record_event({"type": "error", "text": self.fail_reason,
                                "data": {"source": "quota", "codexErrorInfo": info}},
                               feed_watchdog=False)
            return True
        retry = getattr(self.driver, "retry_other_provider", None)
        if not callable(retry):
            self._quota_retried = True
            self._quota_failed("该驱动没有备用线路")
            return True
        self._quota_retried = True
        prompt_text = self._retry_prompt()
        new_handle = None
        try:
            new_handle = retry(self.handle, prompt_text, self.workdir, self.safety)
        except Exception as exc:
            self._record_event({"type": "warning", "text": "切换线路失败：%s" % exc,
                                "data": {"source": "quota", "codexErrorInfo": info}},
                               feed_watchdog=False)
        if new_handle is None:
            self._quota_failed("%s；备用线路不可用" % info)
            self._record_event({"type": "error", "text": self.fail_reason,
                                "data": {"source": "quota", "codexErrorInfo": info}},
                               feed_watchdog=False)
            return True
        with self._handle_lock:
            self.handle = new_handle
        self._update_session(getattr(new_handle, "native_session_id", None)
                             or getattr(new_handle, "session_id", None))
        self.current_turn = getattr(new_handle, "native_turn_id", None) or self.current_turn
        self.state = "running"
        self._active_turn = True
        self.fail_reason = ""
        self._start_event_pump(new_handle)
        self._record_event({
            "type": "warning",
            "text": "额度用尽，已自动切换线路重试",
            "data": {"source": "quota", "codexErrorInfo": info,
                     "from": (getattr(new_handle, "metadata", {}) or {}).get("provider_fallback_from"),
                     "to": (getattr(new_handle, "metadata", {}) or {}).get("provider")},
        }, feed_watchdog=False)
        self._write_meta_soon()
        return True

    def _wait_paused(self, timeout: float = 2.5) -> None:
        deadline = time.monotonic() + max(0.0, timeout)
        while self.state == "pausing" and time.monotonic() < deadline:
            time.sleep(0.02)
        if self.state == "pausing":
            self.state = "paused"
            self._write_meta_soon()

    def _control_event(self, typ: str, request: Dict[str, Any], **data: Any) -> None:
        payload = dict(data)
        payload.setdefault("action", request.get("op"))
        payload.setdefault("by", request.get("by") or "lead")
        payload.setdefault("source", "ctl")
        self._record_event({"type": typ, "data": payload, "by": payload.get("by"), "ts": _now()})

    def _prompt(self, text: str | None) -> Any:
        if not text or self.driver is None or self.handle is None:
            return None
        prompt = getattr(self.driver, "prompt", None)
        if not callable(prompt):
            return None
        with self._handle_lock:
            handle = self.handle
            self._last_prompt_text = str(text)
            self._active_turn = True
            self._done_at = None
            self.state = "running"
            result = _safe_call(prompt, handle, text, self.card.get("output_schema"))
        if result is not None:
            self.current_turn = result
        return result

    def _prime_driver_card(self) -> None:
        """续接前把本轮任务卡交给驱动：resume 不带卡片参数，驱动靠它决定权限模式、模型等。"""
        card = self.start_data.get("card") or {}
        if card and self.driver is not None and hasattr(self.driver, "_card"):
            try:
                self.driver._card = dict(card)
            except Exception:
                pass

    def _spawn_resume(self, text: str) -> Any:
        """通过驱动的 resume 重新拉起通道（agy 用 --conversation）。"""
        self._prime_driver_card()
        resume = getattr(self.driver, "resume", None) if self.driver is not None else None
        if not callable(resume) or not self.native_session_id:
            raise RuntimeError("该通道无法续接会话")
        with self._handle_lock:
            new_handle = _safe_call(resume, self.native_session_id, self.workdir, self.safety)
            if new_handle is None:
                raise RuntimeError("驱动没有返回新的句柄")
            self.handle = new_handle
        self._update_session(getattr(new_handle, "native_session_id", None)
                             or getattr(new_handle, "session_id", None))
        self._start_event_pump(new_handle)
        self._prompt(text)
        return new_handle

    def _resume_handle(self, text: str | None = None) -> Any:
        """兼容旧调用：暂停后继续；进程还活着就在同线程开新回合。"""
        with self._handle_lock:
            handle = self.handle
        process = getattr(handle, "process", None) if handle is not None else None
        alive = process is not None and process.poll() is None
        if alive:
            self._prompt(text or "继续")
            return self.handle
        return self._spawn_resume(text or "继续")

    def handle_control(self, request: Dict[str, Any]) -> Dict[str, Any]:
        op = str(request.get("op") or request.get("action") or "").lower()
        # 插话/续接/带指示中断的原话要进账本，看板据此显示负责人消息（ui-v5-api §2）
        extra = {"text": str(request["text"])} if request.get("text") and op in ("steer", "resume", "interrupt", "pause", "reassign") else {}
        self._control_event("control.requested", request, **extra)
        if op == "status":
            return {"ok": True, "state": self.state, "accepted_as": "status",
                    "native_session_id": self.native_session_id,
                    "active_turn": bool(self._active_turn)}
        if op == "steer":
            steer = getattr(self.driver, "steer", None) if self.driver is not None else None
            if not callable(steer):
                result = {"ok": False, "error": "该通道不支持不打断插话", "alternatives": ["interrupt"]}
                self._control_event("control.failed", request, error=result["error"])
                return result
            if self.state in ("done", "failed", "canceled", "orphaned"):
                result = {"ok": False, "error": "没有进行中的轮次", "alternatives": ["resume"]}
                self._control_event("control.failed", request, error=result["error"])
                return result
            accepted = _safe_call(steer, self.handle, request.get("text", ""), request.get("expected_turn"))
            if accepted is False:
                result = {"ok": False, "error": "steer 未被驱动接受", "alternatives": ["interrupt"]}
                self._control_event("control.failed", request, error=result["error"])
                return result
            result = {"ok": True, "accepted_as": "steer", "effective": "next_sample"}
            self._control_event("control.acked", request, accepted_as="steer", effective="next_sample")
            return result
        if op in ("interrupt", "pause"):
            interrupt = getattr(self.driver, "interrupt", None) if self.driver is not None else None
            if not callable(interrupt):
                result = {"ok": False, "error": "该通道不支持中断"}
                self._control_event("control.failed", request, error=result["error"])
                return result
            # §3.3：turn.completed 后没有活动回合，interrupt 必须是 no-op/拒绝，
            # 不能杀进程，也不能把正常完成的任务改成 failed。
            if (not self._active_turn
                    and self.state not in ("starting", "running", "pausing")):
                result = {"ok": False, "error": "没有进行中的轮次"}
                self._control_event("control.failed", request, error=result["error"])
                return result
            if self.state in ("done", "failed", "canceled", "orphaned"):
                result = {"ok": False, "error": "没有进行中的轮次"}
                self._control_event("control.failed", request, error=result["error"])
                return result
            caps = getattr(self.driver, "capabilities", None) if self.driver is not None else None
            self._interrupt_mode = str(getattr(caps, "interrupt", "kill") or "kill")
            self._interrupting.set()
            try:
                accepted = _safe_call(interrupt, self.handle, 5)
                accepted_as = "interrupt" if accepted in (None, True, "soft", "interrupt") else str(accepted)
                if accepted_as not in ("interrupt", "kill"):
                    accepted_as = "interrupt"
                text = str(request.get("text") or "").strip()
                if text:
                    self._pending_text = text
                if accepted_as == "kill":
                    # 先切状态再解除 _interrupting：SIGINT 让 agy 退出前可能发一条
                    # result status=FAILED，不能让它把 paused 改成 failed。
                    self.state = "paused"
                    self._active_turn = False
                    self._interrupt_pending = False
                    self._invalidate_pump()
                elif self.state != "paused":  # 软中断可能已先收到 turn.completed=interrupted
                    self.state = "pausing"
                    self._pausing_since = time.monotonic()
                    self._interrupt_pending = True
                self._write_meta_soon()
                if accepted_as != "kill":
                    # 让 "xay ctl interrupt" 的返回紧跟着 paused，而不是瞬态 pausing。
                    self._wait_paused(2.5)
                if text and self.state == "paused":
                    self._resume_handle(text)
                    self._pending_text = ""
            finally:
                self._interrupting.clear()
            effective = "after_restart" if accepted_as == "kill" else "now"
            result = {"ok": True, "accepted_as": accepted_as, "effective": effective}
            self._control_event("control.acked", request, accepted_as=accepted_as, effective=effective)
            return result
        if op == "resume":
            if self.state == "pausing":
                self._wait_paused(2.5)
            if self.state in ("failed", "canceled", "orphaned"):
                result = {"ok": False, "error": "任务已结束，无法续接"}
                self._control_event("control.failed", request, error=result["error"])
                return result
            if self.state == "done":
                result = {"ok": False, "error": "任务已完成"}
                self._control_event("control.failed", request, error=result["error"])
                return result
            if self.state != "paused":
                result = {"ok": False, "error": "任务没有暂停"}
                self._control_event("control.failed", request, error=result["error"])
                return result
            text = request.get("text") or self._pending_text or "继续"
            try:
                self._resume_handle(str(text))
            except Exception as exc:
                result = {"ok": False, "error": "续接失败：%s" % exc}
                self._control_event("control.failed", request, error=result["error"])
                return result
            self._pending_text = ""
            result = {"ok": True, "accepted_as": "resume", "effective": "now"}
            self._control_event("control.acked", request, accepted_as="resume", effective="now")
            self._write_meta_soon()
            return result
        if op == "stop":
            if self.stop_requested or self.state in ("done", "failed", "canceled", "orphaned"):
                result = {"ok": True, "accepted_as": "noop", "effective": "now"}
                self._control_event("control.acked", request, accepted_as="noop", effective="now")
                return result
            self.stop_requested = True
            self.state = "canceled"
            self._active_turn = False
            self._invalidate_pump()
            close = getattr(self.driver, "close", None) if self.driver is not None else None
            if callable(close):
                _safe_call(close, self.handle, True)
            result = {"ok": True, "accepted_as": "stop", "effective": "now"}
            self._control_event("control.acked", request, accepted_as="stop", effective="now")
            self._write_meta_soon()
            return result
        if op == "reassign":
            result = {"ok": False, "error": "reassign 由上层 xay-run 处理"}
            self._control_event("control.failed", request, error=result["error"])
            return result
        if op == "decide":
            decision_id = str(request.get("id") or "")
            current = self._seen_decisions.get(decision_id)
            if current is None:
                return {"ok": False, "error": "找不到 decision"}
            try:
                version = int(request.get("version"))
            except (TypeError, ValueError):
                return {"ok": False, "error": "缺少 version"}
            if int(current.get("version", 1)) != version:
                return {"ok": False, "error": "version conflict", "decision": current, "status": 409}
            resolved = dict(current)
            resolved["version"] = version + 1
            resolved["resolved"] = {"ts": _now(), "by": request.get("by") or "lead", "option": request.get("option"), "text": request.get("text")}
            self._seen_decisions[decision_id] = resolved
            _append_jsonl(self.run_dir / "decisions.jsonl", resolved)
            self._record_event({"type": "decision.resolved", "data": {"id": decision_id, "option": request.get("option")}})
            return {"ok": True, "accepted_as": "decided", "effective": "now", "decision": resolved}
        result = {"ok": False, "error": f"未知控制动作：{op}"}
        self._control_event("control.failed", request, error=result["error"])
        return result

    # ---- Driver 生命周期 ---------------------------------------------

    def _install_signal_handlers(self) -> None:
        def handler(signum: int, _frame: Any) -> None:
            self._signal_received = signum
            self.orphaned = True
            self.stop_requested = True
            self.state = "orphaned"
            self.exit_code = 128 + signum
            close = getattr(self.driver, "close", None) if self.driver is not None else None
            if callable(close) and self.handle is not None:
                try:
                    _safe_call(close, self.handle, True)
                except Exception:
                    pass

        for signum in (signal.SIGTERM, signal.SIGINT, signal.SIGHUP):
            try:
                self._old_handlers[signum] = signal.getsignal(signum)
                signal.signal(signum, handler)
            except (ValueError, OSError):
                pass

    def _restore_signal_handlers(self) -> None:
        for signum, previous in self._old_handlers.items():
            try:
                signal.signal(signum, previous)
            except (ValueError, OSError):
                pass

    def _start_driver(self) -> None:
        if self.driver is None:
            if not self.driver_name:
                raise RuntimeError("start.json 没有 driver，且没有传入测试驱动")
            self.driver = load_driver(self.driver_name, base_dir=Path(__file__).parent / "xay_drivers")
        # 一轮一进程的驱动（dsh headless）续接时也要知道任务卡的强度与模型。
        configure = getattr(self.driver, "configure", None)
        if callable(configure):
            configure(self.card, self.safety)
        if self.start_data.get("resume") or self.native_session_id:
            resume = getattr(self.driver, "resume", None)
            if callable(resume) and self.native_session_id:
                self._prime_driver_card()
                self.handle = _safe_call(resume, self.native_session_id, self.workdir, self.safety)
            else:
                self.handle = _safe_call(self.driver.start, self.workdir, self.card, self.safety)
        else:
            self.handle = _safe_call(self.driver.start, self.workdir, self.card, self.safety)
        if self.handle is None:
            raise RuntimeError("驱动没有返回执行句柄")
        self._update_session(getattr(self.handle, "native_session_id", None) or getattr(self.handle, "session_id", None))
        self._start_event_pump(self.handle)
        self.state = "running"
        prompt = self.card.get("prompt")
        if prompt:
            self._prompt(str(prompt))

    def _event_iterator(self) -> Iterator[Any]:
        if self.driver is None:
            return iter(())
        events = getattr(self.driver, "events", None)
        if callable(events):
            try:
                result = events(self.handle)
            except TypeError:
                result = events()
            if result is not None:
                return iter(result)
        handle_events = getattr(self.handle, "events", None)
        if callable(handle_events):
            return iter(handle_events())
        return iter(())

    def _write_meta(self) -> Dict[str, Any]:
        with self._meta_lock:
            if self._finalized or self.ended_at is None:
                self.ended_at = _now()
            meta = dict(self.start_data)
            meta.update({
                "run": self.run_id,
                "agent": self.start_data.get("agent", self.driver_name),
                "driver": getattr(self.driver, "name", None) or self.driver_name,
                "status": "orphaned" if self.orphaned else self.state,
                "state": self.state,
                "orphaned": bool(self.orphaned),
                "fail_reason": self.fail_reason,
                "exit": self.exit_code,
                "started": self.start_data.get("started", self.started_at),
                "ended": self.ended_at,
                "duration_s": max(0, int(self.ended_at - float(self.start_data.get("started", self.started_at)))),
                "pid": os.getpid(),
                "cli_pid": self._driver_pid(),
                "ctl_sock": str(self.ctl_sock),
                "run_dir": str(self.run_dir),
                "workdir": self.workdir,
                "session_id": self.native_session_id,
                "native_session_id": self.native_session_id,
                "turn": self.current_turn,
            })
            _write_json_atomic(self.run_dir / "meta.json", meta)
            return meta

    def _close_driver(self) -> None:
        close = getattr(self.driver, "close", None) if self.driver is not None else None
        if callable(close) and self.handle is not None:
            try:
                _safe_call(close, self.handle, self.stop_requested)
            except Exception:
                pass

    def _mark_orphaned(self) -> None:
        if self.orphaned:
            return
        self.orphaned = True
        self.state = "orphaned"
        self.fail_reason = "orphaned"
        process = getattr(self.handle, "process", None)
        code = process.poll() if process is not None else None
        self.exit_code = int(code) if isinstance(code, int) and code != 0 else 1
        self.write_decision({
            "kind": "orphaned",
            "title": "执行者进程意外退出",
            "detail": "驱动进程在回合完成前退出",
        })

    def _on_pump_end(self, gen: int) -> None:
        if gen != self._pump_gen:
            return
        # kill 型 interrupt 会先让驱动进程退出，再回到控制线程改状态；这条 EOF
        # 是预期内的，不能把托管进程判成 orphaned。
        if self._interrupting.is_set():
            return
        if self.state in ("paused", "pausing", "done", "failed", "canceled", "orphaned"):
            return
        self._mark_orphaned()

    def _should_stop_loop(self) -> bool:
        if self._signal_received is not None:
            return True
        if self.stop_requested:
            return True
        if self.state == "failed" and self._interrupting.is_set():
            return False
        if self.state in ("failed", "orphaned", "canceled"):
            return True
        if (self.state == "pausing" and self._pausing_since is not None
                and time.monotonic() - self._pausing_since >= self._pause_timeout):
            # 驱动没有回 interrupted 时也不能永远卡在瞬态；控制面仍可 resume。
            self.state = "paused"
            self._pausing_since = None
            self._write_meta_soon()
        if self.state == "done" and self._done_at is not None:
            return (time.monotonic() - self._done_at) >= self._done_grace
        return False

    def run(self) -> Dict[str, Any]:
        self._install_signal_handlers()
        try:
            self.start_control_socket()
            self._start_driver()
            while not self._should_stop_loop():
                try:
                    gen, raw = self._evt_queue.get(timeout=0.1)
                except queue.Empty:
                    continue
                if gen != self._pump_gen:
                    continue
                if self._interrupting.is_set() and self._interrupt_mode != "soft":
                    # kill 型中断期间驱动进程会吐出收尾事件（agy 甚至给 FAILED
                    # result）；这些不能覆盖 paused。软中断的 interrupted
                    # 完成事件则必须保留。
                    continue
                if raw is _PUMP_END:
                    self._on_pump_end(gen)
                    continue
                if isinstance(raw, _PumpError):
                    self.state = "failed"
                    self.fail_reason = self.fail_reason or "cli_error"
                    self.exit_code = 1
                    self._record_event({"type": "error", "text": str(raw),
                                        "data": {"error": str(raw)}}, feed_watchdog=False)
                    if not self._interrupting.is_set():
                        break
                    continue
                self._write_raw(raw)
                for event in self._normalize(raw):
                    self._record_event(event)
                self._write_stream(raw)
                self._handle_quota(raw)
            self._flush_normalizer()
            process = getattr(self.handle, "process", None)
            if (not self.orphaned and not self.stop_requested
                    and self.state not in ("done", "failed", "paused", "pausing", "canceled")
                    and process is not None and process.poll() not in (None, 0)):
                self._mark_orphaned()
            if self.orphaned:
                self.state = "orphaned"
            elif self.stop_requested and self.state != "canceled":
                self.state = "canceled"
            elif self.state not in ("failed", "canceled", "paused"):
                self.state = "done"
        except KeyboardInterrupt:
            self.orphaned = True
            self.state = "orphaned"
            self.exit_code = 130
        except Exception as exc:
            self.state = "failed"
            self.fail_reason = self.fail_reason or "cli_error"
            self.exit_code = 1
            try:
                self._record_event({"type": "error", "text": str(exc),
                                    "data": {"error": str(exc)}}, feed_watchdog=False)
            except Exception:
                pass
        finally:
            self._close_driver()
            self.stop_control_socket()
            self._finalized = True
            meta = self._write_meta()
            self._restore_signal_handlers()
        return meta

    start = run


Agent = XayAgent


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="托管一个 xay 执行者任务")
    parser.add_argument("run_dir", nargs="?", help="任务目录；默认从 --run-dir 读取")
    parser.add_argument("--run-dir", dest="run_dir_opt")
    parser.add_argument("--driver")
    parser.add_argument("--xay-home")
    parser.add_argument("--cwd")
    args = parser.parse_args(argv)
    run_dir = args.run_dir or args.run_dir_opt
    if not run_dir:
        parser.error("需要 run_dir")
    start = _read_json(Path(run_dir) / "start.json", {}) or {}
    if args.cwd:
        start["workdir"] = args.cwd
    agent = XayAgent(run_dir, driver_name=args.driver, xay_home=args.xay_home, start=start)
    meta = agent.run()
    return 0 if meta.get("status") in ("done", "paused") else int(meta.get("exit") or 1)


if __name__ == "__main__":
    raise SystemExit(main())
