"""DeepSeek Harness（dsh）headless 驱动。

每一轮起一个 ``dsh --profile headless --json`` 进程，stdout 是 JSONL 事件：
session / status(turn_start|step_start|step_end|turn_end) / tool_call /
tool_result / thinking / text / final。归一化由 ``scripts/xay_normalize`` 的
dsh-headless 分支完成，文字增量由 xay_agent 写进 stream.jsonl。

能力：``steer: none``、``interrupt: kill``。中断 = 杀掉本轮进程（进程组），
续接 = 用 ``--session-id <id>`` 再起一轮。headless 没有沙箱：只读靠任务卡
里的提示词约束（xay-run 拼接），并关闭隔离，这一点与第三版一致。
"""

from __future__ import annotations

import collections
import json
import os
import shutil
import signal
import subprocess
import tempfile
import threading
import time

from .base import Capabilities, Driver, ExecHandle

# dsh 只允许这个模型（与第三版 xay-run 的约束一致）。
DSH_MODEL = "deepseek-v4.1-flash"
EFFORTS = ("off", "minimal", "low", "medium", "high", "xhigh", "max")


class DshModelError(ValueError):
    """任务卡指定了 dsh 不允许的模型。"""


def _card_value(card, key, default=None):
    if isinstance(card, dict):
        return card.get(key, default)
    return getattr(card, key, default)


def resolve_model(card) -> str:
    model = _card_value(card, "model") or ""
    if model in ("", "默认", DSH_MODEL):
        return DSH_MODEL
    raise DshModelError("dsh 只允许使用 %s（收到：%s）" % (DSH_MODEL, model))


def resolve_effort(card) -> str:
    # 第三版把 xay 的强度原样写进 reasoningEffort，这里保持不变。
    effort = str(_card_value(card, "effort") or "medium")
    return effort if effort in EFFORTS else "medium"


def patch_text(card) -> str:
    provider = os.environ.get("XAY_DSH_PROVIDER") or "opencode-xay"
    return ("- id: agent-default-model\n"
            "  name: '@deepseek-ai/dsh-agent-default-model'\n"
            "  config:\n"
            "    provider: %s\n"
            "    model: %s\n"
            "    reasoningEffort: %s\n") % (provider, resolve_model(card), resolve_effort(card))


def default_command() -> list[str]:
    """XAY_DSH_BIN（测试/自定义安装）> dsh-run（带凭据的包装）> dsh --profile headless。"""
    custom = os.environ.get("XAY_DSH_BIN")
    if custom:
        return [custom]
    wrapper = shutil.which("dsh-run")
    if wrapper:
        return [wrapper]
    return [shutil.which("dsh") or "dsh", "--profile", "headless"]


class DshHeadlessDriver(Driver):
    name = "dsh-headless"
    capabilities = Capabilities(steer="none", interrupt="kill", resume=True,
                                approve=False, file_events=False,
                                usage_granularity="step", readonly_sandbox=False,
                                structured_output=False)

    def __init__(self, command=None, popen=None):
        if isinstance(command, str):
            command = [command]
        self.command = list(command) if command else default_command()
        self._popen = popen or subprocess.Popen
        self._card: dict = {}
        self._safety = None
        self._patch_path: str | None = None
        self._last_turn = 0          # 跨进程单调的轮次号（每个新进程 dsh 可能又从 1 数）
        self._lock = threading.Lock()

    # ---- 统一接口 ------------------------------------------------------

    def probe(self):
        return self.capabilities

    def configure(self, card, safety=None):
        """xay_agent 在 start/resume 之前调用：续接时驱动也要知道强度与模型。"""
        self._card = dict(card or {})
        self._safety = safety
        resolve_model(self._card)  # 不允许的模型尽早失败

    def start(self, cwd, card=None, safety=None):
        if card is not None:
            self.configure(card, safety)
        return self._new_handle(None, cwd, safety)

    def resume(self, native_session_id, cwd, safety=None):
        return self._new_handle(str(native_session_id or "") or None, cwd, safety)

    def prompt(self, handle, text, output_schema=None):
        """headless 一轮一个进程：在这里真正启动 dsh。"""
        if handle.process is not None:
            raise RuntimeError("dsh headless 一个进程只能跑一轮")
        argv = list(self.command) + ["--patch", self._ensure_patch(), "--json"]
        if handle.native_session_id:
            argv += ["--session-id", str(handle.native_session_id)]
        argv.append(str(text))
        env = dict(os.environ)
        env.setdefault("XAY_ROLE", "executor")
        proc = self._popen(argv, cwd=str(handle.cwd), stdin=subprocess.DEVNULL,
                           stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
                           bufsize=1, env=env, start_new_session=True)
        handle.process = proc
        handle.cli_pid = getattr(proc, "pid", None)
        handle.metadata["argv"] = argv[:-1] + ["<prompt>"]
        threading.Thread(target=self._drain_stderr, args=(handle,), daemon=True).start()
        handle.metadata["launched"].set()
        return None

    def steer(self, handle, text, expected_turn=None):
        return False

    def events(self, handle=None):
        """等本轮进程启动后逐行读 stdout；进程异常退出时补一条失败的 turn_end。"""
        if handle is None:
            return
        launched = handle.metadata["launched"]
        while not launched.wait(0.1):
            if handle.closed:
                return
        proc = handle.process
        if proc is None:
            return
        offset = None
        saw_turn_end = False
        held = None  # dsh 先发 turn_end 再发 final：把 turn_end 压到 final 之后，最终回复才属于这一轮
        for line in iter(proc.stdout.readline, ""):
            if not line:
                break
            try:
                event = json.loads(line)
            except ValueError:
                continue
            if not isinstance(event, dict):
                continue
            etype = event.get("type")
            if etype == "session" and event.get("sessionId"):
                handle.native_session_id = str(event["sessionId"])
                handle.metadata["session_id"] = handle.native_session_id
            elif etype == "status":
                offset = self._renumber(event, offset)
                if event.get("phase") == "turn_end":
                    saw_turn_end = True
                    held = event
                    continue
            yield event
            if held is not None:
                yield held
                held = None
        if held is not None:
            yield held
        try:
            code = proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            code = None
        self._close_stream(proc.stdout)
        if handle.metadata.get("killed") or handle.closed:
            return
        if code not in (0, None) and not saw_turn_end:
            message = self._stderr_tail(handle) or "dsh 退出码 %s" % code
            # error 行供 xay_agent 识别额度问题；turn_end failed 让任务以 failed 收尾。
            yield {"type": "error", "message": message, "exitCode": code}
            yield {"type": "status", "phase": "turn_end", "turn": self._last_turn or 1,
                   "reason": {"kind": "error", "message": message}}

    def interrupt(self, handle, grace_s=5):
        """kill 型：SIGINT → 等待 → SIGTERM → SIGKILL（整个进程组）。"""
        proc = getattr(handle, "process", None)
        if proc is None or proc.poll() is not None:
            return "kill"
        handle.metadata["killed"] = True
        self._kill_group(proc, grace_s)
        return "kill"

    def close(self, handle, discard=False):
        handle.closed = True
        launched = handle.metadata.get("launched")
        if launched is not None:
            launched.set()
        proc = getattr(handle, "process", None)
        if proc is not None and proc.poll() is None:
            handle.metadata["killed"] = True
            self._kill_group(proc, 2)
        if self._patch_path:
            try:
                os.unlink(self._patch_path)
            except OSError:
                pass
            self._patch_path = None

    # ---- 内部 ----------------------------------------------------------

    def _new_handle(self, native_session_id, cwd, safety):
        handle = ExecHandle(driver=self, native_session_id=native_session_id, cwd=str(cwd),
                            card=dict(self._card), safety=safety if safety is not None else self._safety,
                            metadata={"driver": self.name, "launched": threading.Event(),
                                      "stderr": collections.deque(maxlen=40)})
        handle.cli_pid = None
        return handle

    def _ensure_patch(self) -> str:
        with self._lock:
            if self._patch_path and os.path.exists(self._patch_path):
                return self._patch_path
            fd, path = tempfile.mkstemp(prefix="xay-dsh-", suffix=".patch.yml")
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                handle.write(patch_text(self._card))
            self._patch_path = path
            return path

    def _renumber(self, event, offset):
        """保证 turn 号跨进程单调：续接进程若又从 1 数，就接在上一轮之后。"""
        raw = event.get("turn")
        if not isinstance(raw, int):
            return offset
        if event.get("phase") == "turn_start" or offset is None:
            offset = 0 if raw > self._last_turn else self._last_turn + 1 - raw
        event["turn"] = raw + offset
        if event.get("phase") == "turn_start":
            self._last_turn = event["turn"]
        return offset

    @staticmethod
    def _drain_stderr(handle):
        proc = handle.process
        tail = handle.metadata["stderr"]
        try:
            for line in iter(proc.stderr.readline, ""):
                if not line:
                    break
                tail.append(line.rstrip("\n"))
                try:  # 托管进程的 stderr 由 xay-run 重定向到 agent.log
                    os.write(2, line.encode("utf-8", "replace"))
                except OSError:
                    pass
        except (OSError, ValueError):
            pass
        DshHeadlessDriver._close_stream(proc.stderr)

    @staticmethod
    def _close_stream(stream):
        try:
            if stream is not None:
                stream.close()
        except (OSError, ValueError):
            pass

    @staticmethod
    def _stderr_tail(handle, lines=5):
        tail = [line for line in list(handle.metadata.get("stderr") or ()) if line.strip()]
        return "\n".join(tail[-lines:])

    @staticmethod
    def _kill_group(proc, grace_s):
        def send(sig):
            try:
                os.killpg(proc.pid, sig)
            except (ProcessLookupError, PermissionError, OSError):
                try:
                    proc.send_signal(sig)
                except (ProcessLookupError, OSError):
                    pass

        send(signal.SIGINT)
        try:
            proc.wait(timeout=max(0.1, float(grace_s)))
            return
        except subprocess.TimeoutExpired:
            pass
        send(signal.SIGTERM)
        try:
            proc.wait(timeout=1)
        except subprocess.TimeoutExpired:
            send(signal.SIGKILL)
            try:
                proc.wait(timeout=1)
            except subprocess.TimeoutExpired:
                pass


DriverImpl = DshHeadlessDriver


def create():
    return DshHeadlessDriver()
