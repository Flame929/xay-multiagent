"""Codex app-server 驱动。

app-server 是省略 ``jsonrpc`` 字段、按行传输的 JSON-RPC。每个任务单独启动一个
进程，避免继承用户配置；协议不兼容时交给 codex-exec 驱动。
"""

from __future__ import annotations

import hashlib
import json
import os
import queue
import subprocess
import tempfile
import threading
import time
from pathlib import Path
from typing import Any, Optional

from .base import (
    Capabilities,
    Driver,
    ExecHandle,
    config_args,
    resolve_provider,
    resolve_sandbox,
    safety_value,
    turn_sandbox_policy,
)


REQUIRED_METHODS = {
    "initialize",
    "thread/start",
    "thread/resume",
    "turn/start",
    "turn/steer",
    "turn/interrupt",
}
REQUIRED_NOTIFICATIONS = {
    "turn/completed",
    "item/started",
    "item/completed",
    "thread/tokenUsage/updated",
}


def _config_default_model(provider: str, codex_home: Optional[Path] = None) -> Optional[str]:
    """显式指定线路时 app-server 不再沿用 config.toml 的默认模型，这里补上。

    只在线路与配置里的 model_provider 一致时取顶层 model，避免把中转站的模型名用到订阅线路上。
    """
    try:
        import tomllib
        home = codex_home or Path(os.environ.get("CODEX_HOME") or Path.home() / ".codex")
        with open(home / "config.toml", "rb") as fh:
            cfg = tomllib.load(fh)
    except (OSError, ValueError, ImportError):
        return None
    if cfg.get("model_provider", "openai") == provider and isinstance(cfg.get("model"), str):
        return cfg["model"]
    return None


class CodexAppServerDriver(Driver):
    name = "codex-app-server"

    def __init__(self, command: Optional[str] = None, xay_home: Optional[str] = None, lock_path: Optional[str] = None):
        self.command = command or os.environ.get("XAY_APP_SERVER_BIN") or os.environ.get("XAY_CODEX_BIN") or "codex"
        self.codex_home = Path(os.environ.get("CODEX_HOME") or Path.home() / ".codex").expanduser().resolve()
        home = Path(xay_home or os.environ.get("XAY_HOME", str(Path.home() / ".xay")))
        self.lock_path = Path(lock_path) if lock_path else home / "drivers" / "codex-app-server.lock.json"
        self.capabilities = Capabilities(
            steer="live",
            interrupt="soft",
            resume=True,
            approve=True,
            file_events=True,
            usage_granularity="live",
            readonly_sandbox=True,
            structured_output=True,
        )
        self._handles: list[ExecHandle] = []
        self._event_queue: queue.Queue = queue.Queue()
        self._next_id = 1
        self._id_lock = threading.Lock()

    def _version(self) -> str:
        out = subprocess.check_output([self.command, "--version"], text=True, stderr=subprocess.STDOUT)
        return out.strip().splitlines()[0] if out.strip() else ""

    @staticmethod
    def _collect_strings(value: Any, found: set[str]) -> None:
        if isinstance(value, str):
            found.add(value)
        elif isinstance(value, dict):
            for item in value.values():
                CodexAppServerDriver._collect_strings(item, found)
        elif isinstance(value, list):
            for item in value:
                CodexAppServerDriver._collect_strings(item, found)

    def _schema(self) -> tuple[bool, str]:
        with tempfile.TemporaryDirectory(prefix="xay-codex-schema-") as out_dir:
            subprocess.run(
                [self.command, "app-server", "generate-json-schema", "--experimental", "--out", out_dir],
                check=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
            )
            strings: set[str] = set()
            digest = hashlib.sha256()
            for path in sorted(Path(out_dir).rglob("*.json")):
                data = path.read_bytes()
                digest.update(str(path.relative_to(out_dir)).encode("utf-8"))
                digest.update(data)
                self._collect_strings(json.loads(data.decode("utf-8")), strings)
        needed = REQUIRED_METHODS | REQUIRED_NOTIFICATIONS
        return needed.issubset(strings), digest.hexdigest()

    def _write_lock(self, version: str, schema_sha256: str, schema_valid: bool) -> None:
        self.lock_path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "cli_version": version,
            "schema_sha256": schema_sha256,
            "probed_at": int(time.time()),
            "capabilities": self.capabilities.as_dict(),
            "schema_valid": schema_valid,
        }
        self.lock_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    def probe(self) -> Capabilities:
        version = self._version()
        schema_valid, schema_sha256 = self._schema()
        self.capabilities.cli_version = version
        self.capabilities.schema_sha256 = schema_sha256
        self.capabilities.schema_valid = schema_valid
        self.capabilities.degraded = not schema_valid
        self.capabilities.degraded_reason = "app-server schema 缺少必需方法或通知" if not schema_valid else ""
        self._write_lock(version, schema_sha256, schema_valid)
        return self.capabilities

    def _compatibility(self) -> tuple[bool, str]:
        current = self._version()
        try:
            lock = json.loads(self.lock_path.read_text(encoding="utf-8"))
        except (FileNotFoundError, json.JSONDecodeError):
            cap = self.probe()
            return cap.schema_valid, cap.degraded_reason
        expected = str(lock.get("cli_version", ""))
        if expected == current:
            self.capabilities.cli_version = current
            self.capabilities.schema_sha256 = str(lock.get("schema_sha256", ""))
            self.capabilities.schema_valid = bool(lock.get("schema_valid", True))
            return self.capabilities.schema_valid, ""
        valid, digest = self._schema()
        self.capabilities.cli_version = current
        self.capabilities.schema_sha256 = digest
        self.capabilities.schema_valid = valid
        reason = f"版本锁不匹配：需要 {expected or '未知'}，当前 {current}"
        if not valid:
            reason += "；schema 缺少必需方法或通知"
        return valid, reason

    def _fallback(self, cwd, card, safety, reason) -> ExecHandle:
        from .codex_exec import CodexExecDriver

        fallback = CodexExecDriver(command=self.command)
        handle = fallback.start(cwd, card, safety)
        handle.metadata["driver_degraded"] = reason
        return handle

    def _new_process(self, cwd: str, card: dict, safety: Any) -> ExecHandle:
        argv = [self.command, "app-server", "--listen", "stdio://"] + config_args(card, safety)
        env = os.environ.copy()
        env["CODEX_HOME"] = str(self.codex_home)
        proc = subprocess.Popen(
            argv,
            cwd=str(cwd),
            env=env,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            bufsize=1,
        )
        handle = ExecHandle(self, process=proc, cwd=str(cwd), card=dict(card or {}), safety=safety)
        # 每个进程一个事件队列：换线路/续接后旧进程的 EOF 不会误伤新进程的读取线程。
        handle.metadata["events"] = queue.Queue()
        reader = threading.Thread(target=self._reader, args=(handle,), daemon=True)
        reader.start()
        self._rpc(handle, "initialize", {
            "clientInfo": {"name": "xay", "version": "4"},
            "capabilities": {"experimentalApi": True},
        })
        self._notify(handle, "initialized", {})
        self._handles.append(handle)
        handle.metadata["provider"] = resolve_provider(card or {}, safety)
        return handle

    def _reader(self, handle: ExecHandle) -> None:
        proc = handle.process
        assert proc.stdout is not None
        for line in proc.stdout:
            if not line.strip():
                continue
            try:
                message = json.loads(line)
            except json.JSONDecodeError:
                continue
            pending = getattr(handle, "_pending", {})
            msg_id = message.get("id")
            if msg_id is not None and msg_id in pending:
                pending[msg_id].put(message)
                continue
            if msg_id is not None and message.get("method"):
                # approvalPolicy=never 时通常不会有审批请求；保留事件并回一个明确的空答复，
                # 这样不会让协议因无人回包而挂住。
                self._events_for(handle).put(message)
                method = message.get("method", "")
                if method.endswith("requestUserInput"):
                    self._send_response(handle, msg_id, {"answers": []})
                elif "Approval" in method or method.endswith("requestApproval"):
                    self._send_response(handle, msg_id, {"decision": "decline"})
                else:
                    self._send_response(handle, msg_id, {})
                continue
            self._events_for(handle).put(message)
        self._events_for(handle).put(None)

    def _events_for(self, handle: ExecHandle) -> queue.Queue:
        value = (getattr(handle, "metadata", None) or {}).get("events")
        return value if isinstance(value, queue.Queue) else self._event_queue

    def _send_response(self, handle: ExecHandle, msg_id: Any, result: dict) -> None:
        self._send(handle, {"id": msg_id, "result": result})

    def _send(self, handle: ExecHandle, payload: dict) -> None:
        if handle.process is None or handle.process.stdin is None:
            raise RuntimeError("Codex app-server 进程没有 stdin")
        handle.process.stdin.write(json.dumps(payload, ensure_ascii=False) + "\n")
        handle.process.stdin.flush()

    def _notify(self, handle: ExecHandle, method: str, params: dict) -> None:
        self._send(handle, {"method": method, "params": params})

    def _rpc(self, handle: ExecHandle, method: str, params: dict, timeout: float = 30) -> dict:
        with self._id_lock:
            msg_id = self._next_id
            self._next_id += 1
        pending = getattr(handle, "_pending", None)
        if pending is None:
            pending = {}
            handle._pending = pending
        reply_queue: queue.Queue = queue.Queue(maxsize=1)
        pending[msg_id] = reply_queue
        try:
            self._send(handle, {"id": msg_id, "method": method, "params": params})
            reply = reply_queue.get(timeout=timeout)
        finally:
            pending.pop(msg_id, None)
        if "error" in reply:
            raise RuntimeError(f"Codex app-server {method} 失败: {reply['error']}")
        return reply.get("result") or {}

    @staticmethod
    def _thread_id(result: dict) -> Optional[str]:
        thread = result.get("thread")
        if isinstance(thread, dict):
            return thread.get("id")
        return result.get("threadId") or result.get("id")

    def _provider_default_model(self, handle: ExecHandle) -> Optional[str]:
        result = self._rpc(handle, "model/list", {})
        models = result.get("data") or result.get("models") or []
        for model in models:
            if isinstance(model, dict) and model.get("isDefault"):
                return model.get("model") or model.get("id")
        return None

    @staticmethod
    def _turn_id(result: dict) -> Optional[str]:
        turn = result.get("turn")
        if isinstance(turn, dict):
            return turn.get("id")
        return result.get("turnId") or result.get("id")

    def start(self, cwd, card, safety) -> ExecHandle:
        compatible, reason = self._compatibility()
        if not compatible:
            return self._fallback(cwd, card, safety, reason)
        handle = self._new_process(cwd, card or {}, safety)
        provider = resolve_provider(card or {}, safety)
        model = (card or {}).get("model") or _config_default_model(provider, self.codex_home)
        if model is None and provider == "openai":
            model = self._provider_default_model(handle)
        params = {
            "cwd": str(cwd),
            "model": model,
            "modelProvider": provider,
            "sandbox": resolve_sandbox(card or {}, safety),
            "approvalPolicy": "never",
            "ephemeral": False,
        }
        result = self._rpc(handle, "thread/start", {k: v for k, v in params.items() if v is not None})
        handle.native_session_id = self._thread_id(result)
        return handle

    def resume(self, native_session_id, cwd, safety) -> ExecHandle:
        compatible, reason = self._compatibility()
        if not compatible:
            from .codex_exec import CodexExecDriver
            return CodexExecDriver(command=self.command).resume(native_session_id, cwd, safety)
        handle = self._new_process(cwd, {}, safety)
        result = self._rpc(handle, "thread/resume", {
            "threadId": native_session_id,
            "cwd": str(cwd),
            "sandbox": resolve_sandbox({}, safety),
            "approvalPolicy": "never",
        })
        handle.native_session_id = self._thread_id(result) or native_session_id
        return handle

    def prompt(self, handle, text, output_schema=None):
        if handle.driver is not self:
            return handle.driver.prompt(handle, text, output_schema)
        params = {
            "threadId": handle.native_session_id,
            "input": [{"type": "text", "text": str(text)}],
            "cwd": handle.cwd,
            "approvalPolicy": "never",
            "sandboxPolicy": turn_sandbox_policy(handle.card, handle.safety),
        }
        if output_schema is not None:
            params["outputSchema"] = output_schema
        result = self._rpc(handle, "turn/start", params)
        handle.native_turn_id = self._turn_id(result)
        return handle.native_turn_id

    def steer(self, handle, text, expected_turn) -> bool:
        if handle.driver is not self:
            return bool(handle.driver.steer(handle, text, expected_turn))
        try:
            self._rpc(handle, "turn/steer", {
                "threadId": handle.native_session_id,
                "expectedTurnId": expected_turn,
                "input": [{"type": "text", "text": str(text)}],
            })
        except (RuntimeError, queue.Empty):
            return False
        return True

    def interrupt(self, handle, grace_s=5) -> str:
        if handle.driver is not self:
            return handle.driver.interrupt(handle, grace_s)
        try:
            self._rpc(handle, "turn/interrupt", {
                "threadId": handle.native_session_id,
                "turnId": handle.native_turn_id,
            })
            return "soft"
        except (RuntimeError, queue.Empty):
            return "kill"

    def close(self, handle, discard=False):
        if handle.driver is not self:
            return handle.driver.close(handle, discard)
        proc = handle.process
        if proc is not None and proc.poll() is None:
            proc.terminate()
            try:
                proc.wait(timeout=3)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait()
        handle.closed = True

    def retry_other_provider(self, handle, prompt_text, cwd=None, safety=None):
        """额度/容量失败时换另一条线路重开同一任务（同一托管进程内）。"""
        if getattr(handle, "driver", self) is not self:
            fn = getattr(getattr(handle, "driver", None), "retry_other_provider", None)
            return fn(handle, prompt_text, cwd, safety) if callable(fn) else None
        safety = safety if safety is not None else handle.safety
        card = dict(handle.card or {})
        current = (handle.metadata or {}).get("provider") or resolve_provider(card, safety)
        other = "openai" if current != "openai" else "custom"
        confidential = bool(safety_value(safety, "confidential", False)
                            or card.get("confidential"))
        strict = safety_value(safety, "safety_tier", safety_value(safety, "tier", "standard")) == "strict"
        if other == "custom" and (confidential or strict):
            return None
        card["source"] = "sub" if other == "openai" else "relay"
        try:
            self.close(handle, discard=True)
        except Exception:
            pass
        new = self.start(cwd or handle.cwd or ".", card, safety)
        new.metadata["provider"] = other
        new.metadata["provider_fallback_from"] = current
        if prompt_text:
            self.prompt(new, prompt_text)
        return new

    def events(self, handle=None):
        queue_obj = self._events_for(handle) if handle is not None else self._event_queue
        while True:
            event = queue_obj.get()
            if event is None:
                return
            yield event


CodexAppserverDriver = CodexAppServerDriver

