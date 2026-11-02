"""One-shot agy fallback using the same Driver contract."""

import json
import os
import signal
import subprocess
from dataclasses import dataclass, field

try:
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

try:
    from .agy_stream import DAILY_CAP, DailyCapError, _card_value, record_daily_call
except (ImportError, ModuleNotFoundError):
    import importlib.util
    _stream_path = os.path.join(os.path.dirname(__file__), "agy_stream.py")
    _spec = importlib.util.spec_from_file_location("xay_agy_stream", _stream_path)
    _stream = importlib.util.module_from_spec(_spec)
    _spec.loader.exec_module(_stream)
    DAILY_CAP = _stream.DAILY_CAP
    DailyCapError = _stream.DailyCapError
    _card_value = _stream._card_value
    record_daily_call = _stream.record_daily_call


class AgyOneshotDriver(Driver):
    name = "agy-oneshot"

    def __init__(self, command=None, daily_cap=DAILY_CAP, popen=None):
        self.command = command or os.environ.get("XAY_AGY_BIN") or "agy"
        self.daily_cap = daily_cap
        self._popen = popen or subprocess.Popen
        self._handle = None

    def probe(self):
        return Capabilities(steer="none", interrupt="kill", resume=True,
                            approve=False, file_events=False,
                            usage_granularity="turn", readonly_sandbox=True,
                            structured_output=True)

    def _argv(self, card, conversation=None, prompt=""):
        argv = [self.command, "--output-format", "stream-json", "--sandbox"]
        model = _card_value(card, "model")
        if model:
            argv += ["--model", str(model)]
        if conversation:
            argv += ["--conversation", str(conversation)]
        argv += ["-p", prompt]
        return argv

    def start(self, cwd, card, safety=None):
        handle = ExecHandle(driver=self, process=None, native_session_id="", cwd=str(cwd),
                          metadata={"card": card, "safety": safety, "driver": self.name})
        self._handle = handle
        return handle

    def resume(self, native_session_id, cwd, safety=None):
        handle = ExecHandle(driver=self, process=None, native_session_id=str(native_session_id), cwd=str(cwd),
                          metadata={"safety": safety, "driver": self.name})
        self._handle = handle
        return handle

    def prompt(self, handle, text, output_schema=None):
        record_daily_call(self.daily_cap)
        conversation = handle.native_session_id or None
        proc = self._popen(self._argv(handle.metadata.get("card", {}), conversation, text),
                           cwd=handle.cwd, stdin=subprocess.DEVNULL,
                           stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                           text=True, bufsize=1)
        handle.process = proc
        handle.metadata["started"] = True
        return None

    def steer(self, handle, text, expected_turn=None):
        return False

    def events(self, handle=None):
        handle = handle or self._handle
        if handle is None:
            return
        proc = handle.process
        if proc is None:
            return
        for line in iter(proc.stdout.readline, ""):
            if not line:
                break
            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                continue
            if event.get("event") == "init":
                handle.native_session_id = event.get("conversation_id", "")
            yield event
        proc.wait()

    def interrupt(self, handle, grace_s=5):
        proc = handle.process
        if proc is None or proc.poll() is not None:
            return "kill"
        proc.send_signal(signal.SIGINT)
        try:
            proc.wait(timeout=grace_s)
        except subprocess.TimeoutExpired:
            proc.terminate()
            try:
                proc.wait(timeout=1)
            except subprocess.TimeoutExpired:
                proc.kill()
        return "kill"

    def close(self, handle, discard=False):
        proc = handle.process
        if proc and proc.poll() is None:
            proc.terminate()
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                proc.kill()


DriverImpl = AgyOneshotDriver

