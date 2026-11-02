"""Antigravity stream-json driver.

The driver deliberately keeps a process only while a rework turn is being
read.  A killed process is resumed with its conversation id.
"""

import datetime as _dt
import json
import os
import signal
import subprocess
import time
from dataclasses import dataclass, field
from pathlib import Path

try:  # WP2 supplies the real contract; tests may load this file first.
    from .base import Capabilities, Driver, ExecHandle
except (ImportError, ModuleNotFoundError):
    @dataclass
    class Capabilities:
        steer: str = "none"
        interrupt: str = "kill"
        resume: bool = True
        approve: bool = False
        file_events: bool = False
        usage_granularity: str = "turn"
        readonly_sandbox: bool = True
        structured_output: bool = True

    class Driver:
        pass

    @dataclass
    class ExecHandle:
        process: object = None
        native_session_id: str = ""
        cwd: str = ""
        metadata: dict = field(default_factory=dict)
        driver: object = None


DAILY_CAP = 40


class DailyCapError(RuntimeError):
    """Raised before an agy call would exceed the daily allowance."""


def _today():
    return _dt.date.today().isoformat()


def _registry_path():
    return Path(os.environ.get("XAY_HOME", os.path.expanduser("~/.xay"))) / "registry.jsonl"


def _read_calls(path, day):
    if not path.is_file():
        return 0
    used = 0
    for line in path.read_text(encoding="utf-8").splitlines():
        try:
            row = json.loads(line)
        except json.JSONDecodeError:
            continue
        if row.get("kind") == "agy_call" and row.get("date") == day:
            used += int(row.get("calls", 1))
    return used


def record_daily_call(cap=DAILY_CAP, registry=None):
    """Count one invocation in the shared registry and enforce the hard cap."""
    path = Path(registry) if registry else _registry_path()
    day = _today()
    path.parent.mkdir(parents=True, exist_ok=True)
    # The registry is append-only by contract.  The small in-process/OS lock
    # is enough for the per-task drivers without adding a dependency.
    import fcntl
    with path.open("a+", encoding="utf-8") as fh:
        fcntl.flock(fh.fileno(), fcntl.LOCK_EX)
        fh.seek(0)
        used = _read_calls(path, day)
        if used >= cap:
            fcntl.flock(fh.fileno(), fcntl.LOCK_UN)
            raise DailyCapError("agy 每日调用上限已达到 %d 次" % cap)
        row = {"kind": "agy_call", "agent": "agy", "date": day,
               "calls": 1, "used": used + 1, "max": cap, "ts": time.time()}
        fh.seek(0, os.SEEK_END)
        fh.write(json.dumps(row, ensure_ascii=False) + "\n")
        fh.flush()
        os.fsync(fh.fileno())
        fcntl.flock(fh.fileno(), fcntl.LOCK_UN)
    return row


def _card_value(card, key, default=None):
    if isinstance(card, dict):
        return card.get(key, default)
    return getattr(card, key, default)


def _message_line(text):
    # agy requires event; message is the verified minimal payload.
    return json.dumps({"event": "user", "message": {"role": "user", "content": text}},
                      ensure_ascii=False) + "\n"


class AgyStreamDriver(Driver):
    name = "agy-stream"

    def __init__(self, command=None, daily_cap=DAILY_CAP, popen=None):
        # XAY_AGY_BIN 是测试和自定义安装的注入点；显式参数优先。
        self.command = command or os.environ.get("XAY_AGY_BIN") or "agy"
        self.daily_cap = daily_cap
        self._popen = popen or subprocess.Popen
        self._handle = None
        self._card = {}

    def probe(self):
        return Capabilities(steer="none", interrupt="kill", resume=True,
                            approve=False, file_events=False,
                            usage_granularity="turn", readonly_sandbox=True,
                            structured_output=True)

    def _argv(self, cwd, card, conversation=None):
        argv = [self.command, "--input-format", "stream-json",
                "--output-format", "stream-json", "--sandbox"]
        model = _card_value(card, "model")
        if model:
            argv += ["--model", str(model)]
        # 打印模式下没人能点"允许"：只读用 plan；--full 自动批准（仍在 --sandbox 里）；其余允许改文件
        if _card_value(card, "mode") == "readonly":
            argv += ["--mode", "plan"]
        elif _card_value(card, "full"):
            argv += ["--dangerously-skip-permissions"]
        else:
            argv += ["--mode", "accept-edits"]
        if conversation:
            argv += ["--conversation", str(conversation)]
        argv += ["-p="]
        return argv

    def start(self, cwd, card, safety=None):
        record_daily_call(self.daily_cap)
        self._card = dict(card or {})
        proc = self._popen(self._argv(cwd, card), cwd=str(cwd), stdin=subprocess.PIPE,
                           stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
                           bufsize=1)
        handle = ExecHandle(driver=self, process=proc, native_session_id="", cwd=str(cwd),
                          metadata={"card": card, "safety": safety, "stdin_held": True,
                                    "close_after_result": _card_value(card, "kind", "first") != "rework",
                                    "driver": self.name})
        self._handle = handle
        return handle

    def resume(self, native_session_id, cwd, safety=None):
        record_daily_call(self.daily_cap)
        proc = self._popen(self._argv(cwd, self._card, conversation=native_session_id), cwd=str(cwd),
                           stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                           stderr=subprocess.PIPE, text=True, bufsize=1)
        handle = ExecHandle(driver=self, process=proc, native_session_id=str(native_session_id), cwd=str(cwd),
                          metadata={"safety": safety, "stdin_held": True, "driver": self.name})
        self._handle = handle
        return handle

    def prompt(self, handle, text, output_schema=None):
        handle.process.stdin.write(_message_line(text))
        handle.process.stdin.flush()
        return None

    def steer(self, handle, text, expected_turn=None):
        return False

    def events(self, handle=None):
        """Yield parsed NDJSON until the current result, then close stdin."""
        handle = handle or self._handle
        if handle is None:
            return
        for line in iter(handle.process.stdout.readline, ""):
            if not line:
                break
            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                continue
            if event.get("event") == "init":
                handle.native_session_id = event.get("conversation_id", "")
                handle.metadata["conversation_id"] = handle.native_session_id
            yield event
            if event.get("event") == "result":
                # No warm idle process: only a caller explicitly doing a
                # rework prompt may keep stdin open until this result.
                if handle.metadata.get("close_after_result", True):
                    self._close_stdin(handle)
                break

    @staticmethod
    def _close_streams(handle):
        proc = getattr(handle, "process", None)
        if proc is None:
            return
        for name in ("stdin", "stdout", "stderr"):
            stream = getattr(proc, name, None)
            if stream is not None:
                try:
                    stream.close()
                except (OSError, ValueError):
                    pass

    def close(self, handle, discard=False):
        self._close_stdin(handle)
        proc = handle.process
        if proc and proc.poll() is None:
            proc.terminate()
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                proc.kill()
        self._close_streams(handle)

    def interrupt(self, handle, grace_s=5):
        proc = handle.process
        if proc is None or proc.poll() is not None:
            return "kill"
        self._close_stdin(handle)
        proc.send_signal(signal.SIGINT)
        try:
            proc.wait(timeout=grace_s)
        except subprocess.TimeoutExpired:
            proc.terminate()
            try:
                proc.wait(timeout=1)
            except subprocess.TimeoutExpired:
                proc.kill()
        self._close_streams(handle)
        return "kill"

    @staticmethod
    def _close_stdin(handle):
        if handle.metadata.get("stdin_held") and handle.process.stdin:
            try:
                handle.process.stdin.close()
            except (BrokenPipeError, OSError):
                pass
            handle.metadata["stdin_held"] = False


# Short alias used by the driver registry.
DriverImpl = AgyStreamDriver

