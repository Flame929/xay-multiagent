"""codex exec 降级驱动。

它没有 app-server 的实时 steer；每次 prompt 都是一个一次性进程，中断后只能
结束进程，再用 ``exec resume`` 带着原会话继续。
"""

from __future__ import annotations

import json
import os
import queue
import signal
import subprocess
import tempfile
import threading
import time
from pathlib import Path
from typing import Any, Optional

from .base import Capabilities, Driver, ExecHandle, config_args, resolve_provider, resolve_sandbox


class CodexExecDriver(Driver):
    name = "codex-exec"
    capabilities = Capabilities(
        steer="none",
        interrupt="kill",
        resume=True,
        approve=False,
        file_events=True,
        usage_granularity="turn",
        readonly_sandbox=True,
        structured_output=True,
    )

    def __init__(self, command: str = "codex"):
        self.command = command
        self._event_queue: queue.Queue = queue.Queue()
        self._handles: list[ExecHandle] = []

    def probe(self) -> Capabilities:
        try:
            out = subprocess.check_output([self.command, "--version"], text=True, stderr=subprocess.STDOUT)
            self.capabilities.cli_version = out.strip().splitlines()[0] if out.strip() else ""
        except (OSError, subprocess.CalledProcessError):
            self.capabilities.cli_version = ""
        return self.capabilities

    def start(self, cwd, card, safety) -> ExecHandle:
        handle = ExecHandle(self, cwd=str(cwd), card=dict(card or {}), safety=safety)
        handle.metadata["sandbox"] = resolve_sandbox(card or {}, safety)
        handle.metadata["provider"] = resolve_provider(card or {}, safety)
        self._handles.append(handle)
        return handle

    def resume(self, native_session_id, cwd, safety) -> ExecHandle:
        handle = self.start(cwd, {}, safety)
        handle.native_session_id = native_session_id
        handle.metadata["resume"] = True
        return handle

    def _launch(self, handle: ExecHandle, text: str, output_schema=None) -> None:
        argv = [self.command, "exec"] + config_args(handle.card, handle.safety) + ["--json"]
        schema_path = None
        if output_schema is not None:
            file = tempfile.NamedTemporaryFile("w", encoding="utf-8", suffix=".json", delete=False)
            json.dump(output_schema, file, ensure_ascii=False)
            file.close()
            schema_path = file.name
            argv += ["--output-schema", schema_path]
        if handle.native_session_id:
            argv += ["resume", handle.native_session_id, str(text)]
        else:
            argv += [str(text)]
        proc = subprocess.Popen(
            argv,
            cwd=handle.cwd,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            bufsize=1,
        )
        handle.process = proc
        handle.metadata["schema_path"] = schema_path
        threading.Thread(target=self._reader, args=(handle,), daemon=True).start()

    def _reader(self, handle: ExecHandle) -> None:
        proc = handle.process
        assert proc.stdout is not None
        for line in proc.stdout:
            if not line.strip():
                continue
            try:
                event = json.loads(line)
                if event.get("type") == "thread.started":
                    handle.native_session_id = event.get("thread_id") or event.get("threadId")
                elif event.get("type") == "turn.started":
                    handle.native_turn_id = event.get("turn_id") or event.get("turnId")
                self._event_queue.put(event)
            except json.JSONDecodeError:
                self._event_queue.put({"type": "raw", "text": line.rstrip("\n")})
        proc.wait()
        self._event_queue.put(None)

    def prompt(self, handle, text, output_schema=None):
        self._launch(handle, text, output_schema)
        # exec 的 thread.started 事件才是 native session id 的来源；prompt 本身没有 turn id。
        return None

    def steer(self, handle, text, expected_turn) -> bool:
        return False

    def interrupt(self, handle, grace_s=5) -> str:
        proc = handle.process
        if proc is None or proc.poll() is not None:
            return "kill"
        proc.send_signal(signal.SIGINT)
        try:
            proc.wait(timeout=max(0, grace_s))
        except subprocess.TimeoutExpired:
            proc.terminate()
            try:
                proc.wait(timeout=1)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait()
        return "kill"

    def close(self, handle, discard=False):
        proc = handle.process
        if proc is not None and proc.poll() is None:
            proc.terminate()
            try:
                proc.wait(timeout=2)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait()
        schema_path = handle.metadata.get("schema_path")
        if schema_path:
            try:
                Path(schema_path).unlink()
            except FileNotFoundError:
                pass
        handle.closed = True

    def events(self):
        while True:
            event = self._event_queue.get()
            if event is None:
                return
            yield event


CodexExec = CodexExecDriver

