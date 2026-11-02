"""dsh（DeepSeek Harness）适配器：每一轮起一个 ``dsh --json`` 进程，用 ``--session-id`` 接着上一轮。

移植自 scripts/xay_drivers/dsh_headless.py。要点（V2/docs/probes.md）：
- 工作目录固定：dsh 的会话记住的是"在哪个目录干活"，换目录等于换记忆，旧目录被删了续接直接报错。
  所以 cwd 在 open() 时固定，每一轮都在同一个目录里起进程（一人一张桌子）。
- 记忆条 = 最近一步 ``step_end.usage.totalTokens`` ÷ 窗口。不能只看 inputTokens，它不含缓存，会少算约 4 倍。
  窗口 = **dsh 自己认为的窗口**：开工前跑一次 ``dsh --patch <我们的补丁> --dump-config``（约 0.1 秒、不调模型），
  从用户自己的路由配置里读（本机 opencode-xay 没写，按 pi-ai 默认 262,144；M3 查清，见 probes.md）。
- 同一次预检顺便守住硬性约束：stderr 里出现"我们的补丁 patch: entry … not found"、或者生效的模型不是
  deepseek-v4.1-flash、或者我们的补丁根本没挂上 → 抛 AdapterConfigError，拒绝开工（dsh 自己只是警告一句接着跑，
  模型限制会悄悄失效）。每一轮的 stderr 也照样盯着，出现就杀掉这一轮，error_kind=config。
- dsh 的 ``/compact`` 在 headless 下只是普通文字，没有压缩事件 -> compact() 返回 False（走交接压缩）。
  dsh 自己会在约 196,608 token（本机窗口的 75%）自动压缩 / 裁剪工具输出：两步之间 totalTokens 掉了 10% 以上、
  而且掉之前已经用了 40% 以上，就当它自己压过一次，发 ``compacted``（本机会话日志里 125 次全部命中）。
- 续接（--session-id）时 dsh 不认这个会话号（"was recorded in <旧目录>" 等）→ 发 error kind=resume_failed，
  由工位交接、开新会话、同一件活重做一次。
- dsh 先发 turn_end 再发 final：把 turn_end 压到 final 之后，最终回复才算这一轮的。
- 模型只许 deepseek-v4.1-flash（providers.validate_model 会抛 ModelNotAllowed）。
"""

from __future__ import annotations

import collections
import json
import os
import re
import signal
import subprocess
import tempfile
import threading
from typing import Any, Dict, Optional

from .. import providers
from .base import (
    Adapter, AdapterBusy, AdapterConfigError, AdapterError, Capabilities, classify_error, clip, clip_tail,
    close_pipe, compose_prompt, redact, stop_process, tool_kind, MAX_TOOL_TEXT,
)

# 两步之间掉了 10% 以上、而且掉之前已经用了 40% 以上 -> 判定 dsh 自己压缩（或裁剪工具输出）了一次。
# arch-v10 原写 30%；本机 dsh 会话日志里 70 次真实压缩有 42 次按"上一步"算只掉了 12%~29%（触发看的是
# 加上新工具输出之后的压力），55 次只裁剪工具输出掉 16%~27%；没有压缩却掉 10% 以上的只有 1 次（2.9 万处）。
AUTO_COMPACT_DROP = 0.10
AUTO_COMPACT_MIN_PCT = 40.0
INTERRUPT_GRACE = 5.0
PREFLIGHT_TIMEOUT = 60.0
_RESUME_FAIL_RE = re.compile(
    r"was recorded in|no (stored |persisted )?session|unknown session|unusable --session-id|cannot (adopt|resume)"
    r"|session[^\n]{0,80}(not found|does not exist|no log)|refus\w*[^\n]{0,40}session", re.I)


def _first(mapping: Any, *names: str, default: Any = None) -> Any:
    if isinstance(mapping, dict):
        for name in names:
            if mapping.get(name) is not None:
                return mapping[name]
    return default


def tool_text(tool: str, inp: Any) -> str:
    """一次工具调用给人看的那一行。"""
    inp = inp if isinstance(inp, dict) else {}
    kind = tool_kind(tool)
    if kind == "cmd":
        return clip(_first(inp, "command", default=""), MAX_TOOL_TEXT)
    if kind == "read":
        return str(_first(inp, "file_path", "path", "filePath", default=""))
    if kind == "write":
        return os.path.basename(str(_first(inp, "file_path", "path", "filePath", default=""))) or tool
    if kind == "search":
        return str(_first(inp, "pattern", "query", "path", default=""))
    if kind == "web":
        return str(_first(inp, "url", "query", default=""))
    return tool


class DshAdapter(Adapter):
    agent = "dsh"

    def __init__(self, command=None) -> None:
        super().__init__()
        self._command = command
        self.interrupt_grace = INTERRUPT_GRACE
        self.capabilities = Capabilities(
            steer="none", interrupt="kill", resume=True, native_compact=False,
            usage="step", thinking=True, plan=False, persistent_process=False)
        self._model: Optional[str] = None
        self._effort = "medium"
        self._window = 0
        self._window_estimated = False
        self._patch_path: Optional[str] = None
        self._proc: Optional[subprocess.Popen] = None
        self._reader: Optional[threading.Thread] = None
        self._stderr_tail: collections.deque = collections.deque(maxlen=40)
        self._turn_active = False
        self._killing = False
        self._session_emitted = False
        self._prev_used: Optional[int] = None   # 跨轮保留：用来识别 dsh 自己的压缩
        self._turn_seq = 0
        self._resume_pending = False            # 用 --session-id 续接、还没有一轮成功：会话号可能不认
        self._config_error: Optional[str] = None
        self.limits: Dict[str, Any] = {}        # 预检读到的：生效模型、窗口、dsh 自己的自动压缩点

    # ------------------------------------------------------------------

    def open(self, cwd: str, native_id: Optional[str] = None, card: Optional[dict] = None) -> None:
        with self._lock:
            if self._opened:
                raise AdapterError("dsh 适配器已经打开")
        card = dict(card or {})
        # 先校验再做别的：不允许的模型尽早失败（硬性约束）
        model = providers.resolve_model("dsh", card)
        effort = providers.resolve_effort("dsh", card)
        provider = str(card.get("provider") or os.environ.get("XAY_DSH_PROVIDER") or "opencode-xay")
        if not providers.is_safe_token(provider):
            raise ValueError("dsh provider 写法不合法：%r" % provider)
        if not os.path.isdir(str(cwd)):
            raise AdapterError("dsh 的工作目录不存在（桌子被删了？）：%s" % cwd)
        self.card, self.cwd = card, str(cwd)
        self._model, self._effort = model, effort
        self._window, self._window_estimated = providers.context_window("dsh", model)
        self.native_id = str(native_id) if native_id else None
        fd, path = tempfile.mkstemp(prefix="xay-dsh-", suffix=".patch.yml")
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(self.patch_text(provider, model, effort))
        self._patch_path = path
        try:
            self._preflight(provider, model)
        except Exception:
            self._remove_patch()
            raise
        with self._lock:
            self._opened = True
        if self.native_id:
            self._session_emitted = True
            self._resume_pending = True
            self.emit("session", native_id=self.native_id, resumed=True)
        self.set_state("idle")

    @staticmethod
    def patch_text(provider: str, model: str, effort: str) -> str:
        if model != providers.DSH_MODEL:  # 再查一遍：写进补丁的永远只能是允许的模型
            raise providers.ModelNotAllowed("dsh 只允许使用 %s（收到：%s）" % (providers.DSH_MODEL, model))
        return ("- id: agent-default-model\n"
                "  name: '@deepseek-ai/dsh-agent-default-model'\n"
                "  config:\n"
                "    provider: %s\n"
                "    model: %s\n"
                "    reasoningEffort: %s\n") % (provider, model, effort)

    def _command_prefix(self) -> list:
        command = self._command or providers.resolve_command("dsh", self.card)
        return [command] if isinstance(command, str) else list(command)

    def build_argv(self, prompt: str) -> list:
        argv = self._command_prefix() + ["--patch", str(self._patch_path), "--json"]
        if self.native_id:
            argv += ["--session-id", self.native_id]
        if prompt.startswith("-"):
            prompt = " " + prompt  # 别让提示词被当成命令行选项
        return argv + [prompt]

    def _env(self) -> Dict[str, str]:
        env = dict(os.environ)
        env.setdefault("XAY_ROLE", "executor")
        env.update({str(k): str(v) for k, v in (self.card.get("env") or {}).items()})
        return env

    def _preflight(self, provider: str, model: str) -> None:
        """开工前：dsh --patch <我们的补丁> --dump-config（不调模型）。核对补丁生效、模型合规，读出窗口。

        配置的内容只在内存里解析（里面有用户自己的路由设置），不落盘、不打印。"""
        if self.card.get("preflight") is False:
            return
        argv = self._command_prefix() + ["--patch", str(self._patch_path), "--dump-config"]
        try:
            proc = subprocess.run(argv, cwd=self.cwd, env=self._env(), stdin=subprocess.DEVNULL,
                                  capture_output=True, text=True, encoding="utf-8", errors="replace",
                                  timeout=PREFLIGHT_TIMEOUT, start_new_session=True)
        except subprocess.TimeoutExpired:
            raise AdapterError("dsh 读配置超时（%g 秒）" % PREFLIGHT_TIMEOUT)
        except OSError as exc:
            raise AdapterError("dsh 启动失败：%s" % exc)
        bad = providers.dsh_patch_warnings(proc.stderr, str(self._patch_path))
        if bad:
            raise AdapterConfigError("dsh 补丁里的条目「%s」没找到，模型限制可能没生效，拒绝开工" % "、".join(bad[:3]))
        if proc.returncode != 0:
            tail = [x for x in (proc.stderr or "").splitlines() if x.strip() and "patch: entry" not in x][-2:]
            raise AdapterError("dsh 读不出配置（退出码 %s）%s" % (
                proc.returncode, ("：" + redact(" | ".join(tail))[:300]) if tail else ""))
        info = providers.parse_dsh_dump(proc.stdout, provider, model, str(self._patch_path))
        if info.get("model") != providers.DSH_MODEL:
            raise AdapterConfigError("dsh 实际要用的模型是 %s，不是规定的 %s，拒绝开工"
                                     % (info.get("model") or "（读不出）", providers.DSH_MODEL))
        if not info.get("patched_by_us"):
            raise AdapterConfigError("dsh 没有用上 xay 的补丁（模型设置可能不是 xay 定的），拒绝开工")
        self.limits = dict(info)
        if not providers._env_int("XAY_DSH_WINDOW") and info.get("window"):
            self._window, self._window_estimated = int(info["window"]), False

    def _remove_patch(self) -> None:
        if self._patch_path:
            try:
                os.unlink(self._patch_path)
            except OSError:
                pass
            self._patch_path = None

    # ------------------------------------------------------------------

    def send(self, text: str, extra_context: Any = None) -> None:
        self._require_open()
        with self._lock:
            if self._turn_active:
                raise AdapterBusy("dsh 正在做上一轮")
            self._turn_active = True
            self._killing = False
        prompt = compose_prompt(text, extra_context)
        self._config_error = None
        try:
            proc = subprocess.Popen(
                self.build_argv(prompt), cwd=self.cwd, env=self._env(), stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, encoding="utf-8",
                errors="replace", bufsize=1, start_new_session=True)
        except OSError as exc:
            with self._lock:
                self._turn_active = False
            raise AdapterError("dsh 启动失败：%s" % exc)
        self._proc = proc
        self._stderr_tail.clear()
        self.set_state("working")
        threading.Thread(target=self._drain_stderr, args=(proc,), daemon=True).start()
        self._reader = threading.Thread(target=self._run_turn, args=(proc,), daemon=True)
        self._reader.start()

    def interrupt(self) -> str:
        proc = self._proc
        if proc is None or proc.poll() is not None:
            return "kill"
        self._killing = True
        stop_process(proc, grace=self.interrupt_grace, first=signal.SIGINT)
        return "kill"

    def close(self) -> None:
        with self._lock:
            if self._closed:
                return
            self._closed = True
        self._killing = True
        stop_process(self._proc, grace=2.0)
        reader = self._reader
        if reader is not None and reader is not threading.current_thread():
            reader.join(timeout=5.0)
        self._remove_patch()
        self.set_state("offline")
        self._finish_events()

    def alive(self) -> bool:
        """dsh 没有常驻进程：打开了、没关就算活着。"""
        return self._opened and not self._closed

    # ------------------------------------------------------------------

    def _drain_stderr(self, proc: subprocess.Popen) -> None:
        try:
            for line in proc.stderr:
                text = line.rstrip("\n")
                if not text.strip():
                    continue
                bad = providers.dsh_patch_warnings(text, self._patch_path) if self._patch_path else []
                if bad:
                    # 我们的补丁没生效：模型限制可能失效 -> 这一轮立刻停下（预检之后又出现，说明配置被改了）
                    self._config_error = "dsh 补丁里的条目「%s」没找到，模型限制可能没生效，已经停下" % "、".join(bad)
                    threading.Thread(target=stop_process, args=(proc,), kwargs={"grace": 1.0}, daemon=True).start()
                    continue
                if "patch: entry" in text:
                    continue          # 用户自己 profile 里的同类警告：和我们无关，也不进失败原因
                self._stderr_tail.append(text[:300])
        except (OSError, ValueError):
            pass
        finally:
            close_pipe(proc.stderr)

    def _classify(self, message: str) -> str:
        if self._resume_pending and _RESUME_FAIL_RE.search(message or ""):
            return "resume_failed"
        return classify_error(message)

    def _run_turn(self, proc: subprocess.Popen) -> None:
        state: Dict[str, Any] = {"held_end": None, "final": None, "text_parts": [], "tools": {},
                                 "error": None, "usage": {"in": 0, "out": 0, "cached": 0, "total": 0},
                                 "usage_seen": False}
        try:
            for line in proc.stdout:
                line = line.strip()
                if not line:
                    continue
                try:
                    event = json.loads(line)
                except ValueError:
                    continue
                if isinstance(event, dict):
                    try:
                        self._on_event(event, state)
                    except Exception as exc:
                        self.emit("error", kind="other", text="适配器解析出错：%s" % exc)
        except (OSError, ValueError):
            pass
        try:
            code = proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            code = None
        close_pipe(proc.stdout)
        self._finish_turn(state, code, deliberate=self._killing or self._closed)

    def _on_event(self, event: dict, state: Dict[str, Any]) -> None:
        etype = event.get("type")
        if etype == "session":
            sid = str(event.get("sessionId") or "")
            if sid and (sid != self.native_id or not self._session_emitted):
                self.native_id = sid
                self._session_emitted = True
                self.emit("session", native_id=sid)
        elif etype == "status":
            phase = event.get("phase")
            if phase == "turn_start":
                self._turn_seq += 1
                self.emit("turn_started", native_turn=str(event.get("turn") or self._turn_seq))
            elif phase == "step_end":
                self._on_step_end(event, state)
            elif phase == "turn_end":
                state["held_end"] = event  # 压到 final 之后再处理
        elif etype == "tool_call":
            call_id = str(_first(event, "callId", "call_id", default=""))
            tool = str(event.get("tool") or "")
            kind = tool_kind(tool)
            state["tools"][call_id] = kind
            self.emit("tool", kind=kind, text=tool_text(tool, event.get("input")))
        elif etype == "tool_result":
            output = _first(event, "result", "output", default="")
            if not isinstance(output, str):
                output = json.dumps(output, ensure_ascii=False)
            ok = str(event.get("status", "completed")).lower() == "completed"
            self.emit("tool_output", text=clip_tail(output), ok=ok)
        elif etype == "thinking":
            text = str(event.get("text") or "")
            if text:
                self.emit("thinking", text=text)
        elif etype == "text":
            text = str(event.get("text") or "")
            if text:
                state["text_parts"].append(text)
                self.emit("text", text=text)
        elif etype == "final":
            state["final"] = str(event.get("text") or "")
        elif etype == "error":
            message = str(_first(event, "message", "error", default="") or "dsh 出错")
            state["error"] = message
            self.emit("error", kind=self._classify(message), text=message)

    def _on_step_end(self, event: dict, state: Dict[str, Any]) -> None:
        usage = event.get("usage")
        if not isinstance(usage, dict):
            return
        state["usage_seen"] = True
        state["usage"]["in"] += int(usage.get("inputTokens") or 0)
        state["usage"]["out"] += int(usage.get("outputTokens") or 0)
        state["usage"]["cached"] += int(usage.get("cacheReadTokens") or 0)
        state["usage"]["total"] = state["usage"]["in"] + state["usage"]["out"] + state["usage"]["cached"]
        total = usage.get("totalTokens")
        if isinstance(total, int) and not isinstance(total, bool):
            prev = self._prev_used
            self._prev_used = total
            # dsh 口径：probes.md §15 与 fixtures_adapters/dsh_turn_ok.jsonl：
            # step_end 的 inputTokens 不含缓存，totalTokens = inputTokens + outputTokens + cacheReadTokens；
            # 因此 turn_fresh 直接取累计的 in，turn_out 取累计的 out。
            self.emit_usage(total, self._window, self._window_estimated, "dsh_total",
                            turn_total=state["usage"]["total"],
                            turn_out=state["usage"]["out"],
                            turn_fresh=state["usage"]["in"])
            if prev and total < prev * (1.0 - AUTO_COMPACT_DROP) and \
                    providers.pct_of(prev, self._window) >= AUTO_COMPACT_MIN_PCT:
                # dsh 自己压缩了（约 75% 的自动压缩 / 裁剪工具输出）：记一笔
                data: Dict[str, Any] = {"auto": True, "method": "native_auto", "detected": "usage_drop",
                                        "before_used": prev, "after_used": total}
                if self._window:
                    data.update(before=providers.pct_of(prev, self._window),
                                after=providers.pct_of(total, self._window))
                self.emit("compacted", **data)

    def _finish_turn(self, state: Dict[str, Any], code: Optional[int], deliberate: bool) -> None:
        end = state["held_end"]
        reason = end.get("reason") if isinstance(end, dict) and isinstance(end.get("reason"), dict) else {}
        message = ""
        if self._config_error:
            status, message = "failed", self._config_error
        elif deliberate:
            status = "interrupted"
        elif end is not None:
            kind = str(reason.get("kind") or "completed").lower()
            if kind in ("completed", "complete", "success", "done", "ok"):
                status = "completed"
            elif kind in ("interrupted", "interrupt", "canceled", "cancelled"):
                status = "interrupted"
            else:
                status = "failed"
                message = str(reason.get("message") or state["error"] or "dsh 这一轮失败了")
        elif code in (0, None) and (state["final"] is not None or state["text_parts"]):
            status = "completed"
        else:
            status = "failed"
            tail = " | ".join(list(self._stderr_tail)[-3:])
            message = state["error"] or tail or "dsh 退出码 %s" % code
        text = state["final"] if state["final"] is not None else "".join(state["text_parts"])
        data: Dict[str, Any] = {"status": status, "final_text": text}
        if state["usage_seen"]:
            data["usage"] = dict(state["usage"])
        if status == "failed":
            kind = "config" if self._config_error else self._classify(message)
            if not state["error"] or self._config_error:  # dsh 自己没发 error 行：补一条，让页面看得到原因
                self.emit("error", kind=kind, text=message)
            data.update(error_kind=kind, error=message)
        elif status == "completed":
            self._resume_pending = False      # 会话号认了
        # 先回到空闲再报"这一轮结束"：工位收到 turn_ended 马上发下一轮时，状态不会被迟到的 idle 盖掉
        with self._lock:
            self._turn_active = False
        if not self._closed:
            self.set_state("idle")
        self.emit("turn_ended", **data)
