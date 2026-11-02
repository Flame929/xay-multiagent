"""agy（Google Antigravity CLI）适配器：一个常驻进程，输入保持打开，一轮一行。

移植自 scripts/xay_drivers/agy_stream.py，并修好已知问题：
- 输入不再在第一轮后关掉（旧驱动每轮结束就关 stdin）。V2/docs/probes.md：热续接 3/3 成功，
  第二轮 4~10 秒，所以 agy 员工默认热续接：同一个进程，stdin 一直开着，一轮一行
  ``{"event":"user","message":{"role":"user","content":"…"}}``。
- ``--add-dir`` 以前被丢掉；现在 card["add_dirs"] 里每个目录都带上。
- 额度 / 容量错误以前认不出来（agy 的原始事件用的键是 "event" 不是 "type"）；现在 error 事件和
  result.status 非 SUCCESS 都会分类。
- 模型只许 gemini-3.8-*（providers.validate_model 会抛 ModelNotAllowed）。

权限：非交互模式没人能点"允许"。每个进程生成一个临时 ``--settings`` 文件，把任务卡里的
``allow_commands`` 放行（``{"permissions":{"allow":["command(python3 hello.py)"]}}``，探针实测有效，
不改用户的 ~/.gemini 配置）。--settings 是进程启动时读的，所以允许的命令变了（``set_allow_commands``）
要在下一轮开始前用 ``--conversation`` 重启一次进程（相当于一次冷续接）。

放行命令以外，临时 settings 里总写一条 ``permissions.deny``（git commit / git push；deny 优先于 allow），
只在有放行命令或 full 模式时才传 --settings（其余情况无头模式本来就拒跑命令）。

续接失败：冷续接（open 时给了 native_id）后的第一轮，30 秒内既没有工具调用也没有文字，或者这一轮
没动作就结束了 -> 发 error kind="resume_failed"，由工位改走交接压缩、开新对话。
agy 没有思考文字（thinking_tokens 恒为 0），所以不会有 thinking 事件。

记忆条：已用 = ``step_update.usage.input_tokens + cache_read_tokens``（agy 的 input_tokens 只算新内容，
缓存读取另算；token-optimizer 调研在本机对话库里核对过：已用 132,434 ≈ 新输入 2,467 + 缓存 134,149），
窗口 256,000（三档一样，标估算）。

原生压缩（M3 大上下文实测好用：65,211 → 16,889，暗号还记得）：compact() 在输入里写一行 ``/compact``，
等到 checkpoint 步骤 + SUCCESS 的 result 才算成功；这一行不算"一轮"，不发 turn_started / turn_ended。
"""

from __future__ import annotations

import collections
import json
import os
import re
import shutil
import signal
import subprocess
import tempfile
import threading
from typing import Any, Dict, Iterable, List, Optional

from .. import providers
from .base import (
    Adapter, AdapterBusy, AdapterError, Capabilities, classify_error, clip, clip_tail,
    close_pipe, compose_prompt, stop_process, tool_kind, MAX_TOOL_TEXT,
)

RESUME_TIMEOUT_S = 30.0
INTERRUPT_GRACE = 5.0
COMPACT_TIMEOUT_S = 180.0
COMPACT_LINE = "/compact"
DENY_COMMANDS = ("command(git commit)", "command(git push)")
_DONE_STATES = ("DONE", "COMPLETED", "FAILED", "ERROR", "CANCELED", "CANCELLED")
_OK_STATES = ("DONE", "COMPLETED")
_EXIT_CODE_RE = re.compile(r"exited with code (\d+)")


def _first(mapping: Any, *names: str, default: Any = None) -> Any:
    if isinstance(mapping, dict):
        for name in names:
            if mapping.get(name) is not None:
                return mapping[name]
    return default


def tool_info(name: str, params: Any) -> tuple:
    """agy 的工具 -> (统一的 kind, 给人看的那一行)。"""
    params = params if isinstance(params, dict) else {}
    kind = tool_kind(name)
    if kind == "cmd":
        return kind, clip(_first(params, "CommandLine", "command", default=""), MAX_TOOL_TEXT)
    if kind in ("read", "write"):
        path = str(_first(params, "Path", "AbsolutePath", "TargetFile", "path", default=""))
        return kind, os.path.basename(path) or name
    if kind in ("search", "web"):
        return kind, str(_first(params, "Query", "query", "Pattern", "pattern", "path", "Path", "url", "Url", default=name))
    return kind, name


def settings_for(allow_commands: Iterable[str], deny: Iterable[str] = DENY_COMMANDS) -> dict:
    allow = []
    for cmd in allow_commands:
        cmd = str(cmd).strip()
        if not cmd:
            continue
        allow.append(cmd if re.match(r"^\w+\(.*\)$", cmd, re.S) else "command(%s)" % cmd)
    perms = {"allow": allow}
    deny = [str(d) for d in deny if str(d).strip()]
    if deny:
        perms["deny"] = deny
    return {"permissions": perms}


def user_line(text: str) -> str:
    # agy 要求带 "event"；message 是验证过的最小载荷（旧驱动 / 探针一致）
    return json.dumps({"event": "user", "message": {"role": "user", "content": text}}, ensure_ascii=False) + "\n"


class AgyAdapter(Adapter):
    agent = "agy"

    def __init__(self, command=None) -> None:
        super().__init__()
        self._command = command
        self.interrupt_grace = INTERRUPT_GRACE
        self.capabilities = Capabilities(
            steer="none", interrupt="kill", resume=True,
            native_compact=providers.get("agy").native_compaction,
            usage="step", thinking=False, plan=False, persistent_process=True)
        self.compact_timeout = COMPACT_TIMEOUT_S
        self._compacting = False
        self._compact_checkpoint = False
        self._compact_status: Optional[str] = None
        self._compact_done = threading.Event()
        self._mode_key: Optional[str] = None
        self._model: Optional[str] = None
        self._window = 0
        self._window_estimated = True
        self._allow: List[str] = []
        self._settings_dir: Optional[str] = None
        self._settings_n = 0
        self._settings_dirty = False
        self._proc: Optional[subprocess.Popen] = None
        self._reader: Optional[threading.Thread] = None
        self._stderr_tail: collections.deque = collections.deque(maxlen=40)
        self._killing = False
        self._session_emitted = False
        # 一轮的状态
        self._turn_active = False
        self._turn_started = False
        self._turn_gen = 0
        self._turn_seq = 0
        self._seen_action = False
        self._parts: List[str] = []
        self._tools_seen: set = set()
        self._last_error: Optional[str] = None
        self._turn_usage: Dict[str, int] = {"in": 0, "out": 0, "cached": 0, "total": 0}
        self._turn_usage_seen = False
        # 冷续接：等第一轮证明对话真的接上了
        self._cold_pending = False
        self.resume_failed = False   # 工位可以读：这个适配器报告过续接失败

    # ------------------------------------------------------------------
    # 打开 / 启动进程
    # ------------------------------------------------------------------

    def open(self, cwd: str, native_id: Optional[str] = None, card: Optional[dict] = None) -> None:
        with self._lock:
            if self._opened:
                raise AdapterError("agy 适配器已经打开")
        card = dict(card or {})
        model = providers.resolve_model("agy", card)  # 不允许的模型尽早失败（硬性约束）
        self._model = model
        self._window, self._window_estimated = providers.context_window("agy", model)
        self.card, self.cwd = card, str(cwd)
        if not os.path.isdir(self.cwd):
            raise AdapterError("agy 的工作目录不存在（桌子被删了？）：%s" % self.cwd)
        self._allow = [str(c) for c in (card.get("allow_commands") or [])]
        self.native_id = str(native_id) if native_id else None
        with self._lock:
            self._opened = True
            if self.native_id:
                # 先记下"要接哪个对话"，再起进程：init 可能比这里先到，换了对话号要能认出来
                self._cold_pending = True
                self._session_emitted = True
        if self.native_id:
            self.emit("session", native_id=self.native_id, model=self._model, resumed=True)
        try:
            self._launch(self.native_id)
        except Exception:
            with self._lock:
                self._opened = False
            self._cleanup_settings()
            raise
        self.set_state("idle")

    def _readonly(self) -> bool:
        return str(self.card.get("mode") or "").lower() in ("readonly", "read-only", "ro")

    def build_argv(self, conversation: Optional[str], settings_path: Optional[str]) -> List[str]:
        model = providers.validate_model("agy", self._model)
        if not model:
            raise providers.ModelNotAllowed("agy 没有可用的模型")
        command = self._command or providers.resolve_command("agy", self.card)
        if isinstance(command, str):
            command = [command]
        argv = list(command) + ["--model", model, "--input-format", "stream-json",
                                 "--output-format", "stream-json", "--sandbox"]
        if self._readonly():
            argv += ["--mode", "plan"]                      # 只读：计划模式
        elif self.card.get("full"):
            argv += ["--dangerously-skip-permissions"]      # 仍在 --sandbox 里
        else:
            argv += ["--mode", "accept-edits"]              # 打印模式没人点"允许"：允许改文件
        if settings_path:
            argv += ["--settings", settings_path]
        dirs = self.card.get("add_dirs") or self.card.get("add_dir") or []
        if isinstance(dirs, str):
            dirs = [dirs]
        for directory in dirs:
            argv += ["--add-dir", str(directory)]
        if conversation:
            argv += ["--conversation", str(conversation)]
        return argv + ["-p="]  # -p= 必须放最后，否则后面的参数会被当成提示词

    def _write_settings(self) -> Optional[str]:
        if not self._allow and not self.card.get("full"):
            return None          # 没有放行命令：无头模式本来就拒跑命令，不必传 --settings
        if self._settings_dir is None:
            self._settings_dir = tempfile.mkdtemp(prefix="xay-agy-")
        self._settings_n += 1
        path = os.path.join(self._settings_dir, "settings-%d.json" % self._settings_n)
        with open(path, "w", encoding="utf-8") as fh:
            json.dump(settings_for(self._allow), fh, ensure_ascii=False, indent=2)
            fh.write("\n")
        return path

    def _cleanup_settings(self) -> None:
        if self._settings_dir:
            shutil.rmtree(self._settings_dir, ignore_errors=True)
            self._settings_dir = None

    def _launch(self, conversation: Optional[str]) -> None:
        settings_path = self._write_settings()
        argv = self.build_argv(conversation, settings_path)
        env = dict(os.environ)
        env.update({str(k): str(v) for k, v in (self.card.get("env") or {}).items()})
        try:
            proc = subprocess.Popen(
                argv, cwd=self.cwd, env=env, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                stderr=subprocess.PIPE, text=True, encoding="utf-8", errors="replace",
                bufsize=1, start_new_session=True)
        except OSError as exc:
            raise AdapterError("agy 启动失败：%s" % exc)
        self._stderr_tail.clear()
        self._killing = False
        self._settings_dirty = False
        self._proc = proc
        self._reader = threading.Thread(target=self._read_loop, args=(proc,), daemon=True)
        self._reader.start()
        threading.Thread(target=self._drain_stderr, args=(proc,), daemon=True).start()

    # ------------------------------------------------------------------
    # 控制
    # ------------------------------------------------------------------

    def set_allow_commands(self, commands: Iterable[str], mode: Optional[str] = None) -> bool:
        """换一批放行的命令（以及只读 / 可写）。变了返回 True：下一次 send() 会先用 --conversation 重启进程
        （--settings、--mode 只在启动时读；待真机验证 agy 会不会每轮重读，如果会这里就不用重启）。"""
        new = [str(c) for c in commands]
        with self._lock:
            mode_changed = mode is not None and (mode == "readonly") != self._readonly()
            if new == self._allow and not mode_changed:
                return False
            self._allow = new
            if mode is not None:
                self.card["mode"] = "readonly" if mode == "readonly" else "write"
            self._settings_dirty = True
        return True

    def compact(self, timeout: Optional[float] = None) -> bool:
        """原生压缩：输入里写一行 /compact，等 checkpoint 步骤 + SUCCESS 的 result（一直等，最多 compact_timeout 秒）。

        不支持 / 进程不在 / 没出现 checkpoint / 失败 / 超时都返回 False（工位改走交接压缩）。"""
        self._require_open()
        if not self.capabilities.native_compact:
            return False
        with self._lock:
            if self._turn_active:
                raise AdapterBusy("agy 正在做一轮，不能压缩")
            proc = self._proc
            if proc is None or proc.poll() is not None or proc.stdin is None or self._settings_dirty:
                return False
            self._turn_active = True
            self._compacting = True
            self._compact_checkpoint = False
            self._compact_status = None
            self._compact_done.clear()
        before = (self.last_usage or {}).get("pct")
        self.set_state("compacting")
        self.emit("compaction_started", auto=False)
        try:
            proc.stdin.write(user_line(COMPACT_LINE))
            proc.stdin.flush()
        except (BrokenPipeError, OSError, ValueError):
            with self._lock:
                self._turn_active = self._compacting = False
            self.set_state("idle")
            return False
        finished = self._compact_done.wait(timeout if timeout is not None else self.compact_timeout)
        with self._lock:
            self._compacting = False
            self._turn_active = False
            ok = finished and self._compact_checkpoint and self._compact_status in ("SUCCESS", "OK", "COMPLETED", "DONE")
            cold_confirmed = ok
        if not finished:
            stop_process(proc, grace=2.0)          # 压了 3 分钟还没完：这个进程不可信了
            return False
        if cold_confirmed:
            self._mark_action()                    # 冷续接后先压缩也算接上了
        if ok:
            data: Dict[str, Any] = {"auto": False, "method": "native"}
            if before is not None:
                data["before"] = before
            self.emit("compacted", **data)          # 压完的用量要等下一轮第一步才知道（页面先"等校准"）
        if self.alive():
            self.set_state("idle")
        return ok

    def send(self, text: str, extra_context: Any = None) -> None:
        self._require_open()
        with self._lock:
            if self._turn_active:
                raise AdapterBusy("agy 正在做上一轮")
            self._turn_active = True
            self._turn_gen += 1
            gen = self._turn_gen
            self._turn_started = False
            self._seen_action = False
            self._parts = []
            self._tools_seen = set()
            self._last_error = None
            self._turn_usage = {"in": 0, "out": 0, "cached": 0, "total": 0}
            self._turn_usage_seen = False
        self.set_state("working")  # 必须在写输入之前：输出线程可能比这里先跑完整个一轮
        try:
            if self._settings_dirty:
                self._restart_for_settings()
            proc = self._proc
            if proc is None or proc.poll() is not None or proc.stdin is None:
                raise AdapterError("agy 进程已经退出，请重新 open（可以用 native_id 冷续接）")
            line = user_line(compose_prompt(text, extra_context))
            try:
                proc.stdin.write(line)
                proc.stdin.flush()
            except (BrokenPipeError, OSError, ValueError) as exc:
                raise AdapterError("写 agy 输入失败：%s" % exc)
        except Exception:
            with self._lock:
                self._turn_active = False
            self.set_state("idle")
            raise
        if self._cold_pending:
            self._arm_resume_watchdog(gen)

    def _restart_for_settings(self) -> None:
        """放行的命令变了：停掉旧进程，用 --conversation 接着原来的对话重启。"""
        old = self._proc
        self._proc = None   # 先摘掉：旧进程的输出线程退出时不会被当成"意外退出"
        self._killing = True
        try:
            if old is not None and old.stdin is not None:
                old.stdin.close()
        except (OSError, ValueError):
            pass
        stop_process(old, grace=2.0)
        old_reader = self._reader
        if old_reader is not None and old_reader is not threading.current_thread():
            old_reader.join(timeout=3.0)
        self._launch(self.native_id)
        if self.native_id:
            self._cold_pending = True   # 重启后的第一轮也要证明对话接上了

    def interrupt(self) -> str:
        proc = self._proc
        if proc is None or proc.poll() is not None:
            return "kill"
        self._killing = True
        try:
            if proc.stdin is not None:
                proc.stdin.close()
        except (OSError, ValueError):
            pass
        stop_process(proc, grace=self.interrupt_grace, first=signal.SIGINT)
        return "kill"

    def close(self) -> None:
        with self._lock:
            if self._closed:
                return
            self._closed = True
        self._killing = True
        proc = self._proc
        if proc is not None and proc.poll() is None:
            try:
                if proc.stdin is not None:
                    proc.stdin.close()   # 输入关掉 = 正常收工
            except (OSError, ValueError):
                pass
            try:
                proc.wait(timeout=3.0)
            except subprocess.TimeoutExpired:
                stop_process(proc, grace=2.0)
        reader = self._reader
        if reader is not None and reader is not threading.current_thread():
            reader.join(timeout=5.0)
        self._cleanup_settings()
        self.set_state("offline")
        self._finish_events()

    def alive(self) -> bool:
        return self._opened and not self._closed and self._proc is not None and self._proc.poll() is None

    # ------------------------------------------------------------------
    # 续接失败检测
    # ------------------------------------------------------------------

    def _arm_resume_watchdog(self, gen: int) -> None:
        timeout = float(self.card.get("resume_timeout_s", RESUME_TIMEOUT_S))
        timer = threading.Timer(timeout, self._resume_timeout, args=(gen,))
        timer.daemon = True
        timer.start()

    def _resume_timeout(self, gen: int) -> None:
        with self._lock:
            stale = gen != self._turn_gen or not self._turn_active or not self._cold_pending or self._seen_action
        if not stale:
            self._report_resume_failed("冷续接后 %g 秒内既没有调用工具也没有输出文字"
                                       % float(self.card.get("resume_timeout_s", RESUME_TIMEOUT_S)))

    def _report_resume_failed(self, text: str) -> None:
        with self._lock:
            if not self._cold_pending:
                return
            self._cold_pending = False
            self.resume_failed = True
        self.emit("error", kind="resume_failed", text=text)

    # ------------------------------------------------------------------
    # 读输出
    # ------------------------------------------------------------------

    def _drain_stderr(self, proc: subprocess.Popen) -> None:
        try:
            for line in proc.stderr:
                text = line.rstrip("\n")
                if text.strip():
                    self._stderr_tail.append(text[:300])
        except (OSError, ValueError):
            pass
        finally:
            close_pipe(proc.stderr)

    def _read_loop(self, proc: subprocess.Popen) -> None:
        try:
            for line in proc.stdout:
                line = line.strip()
                if not line:
                    continue
                try:
                    event = json.loads(line)
                except ValueError:
                    continue
                if isinstance(event, dict) and proc is self._proc:
                    try:
                        self._on_event(event)
                    except Exception as exc:
                        self.emit("error", kind="other", text="适配器解析出错：%s" % exc)
        except (OSError, ValueError):
            pass
        finally:
            try:
                self._on_exit(proc)
            finally:
                close_pipe(proc.stdout)
                close_pipe(proc.stdin)

    def _on_exit(self, proc: subprocess.Popen) -> None:
        if proc is not self._proc:
            return  # 已经被新进程取代（重启）
        try:
            code = proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            code = None
        deliberate = self._killing or self._closed
        with self._lock:
            if self._compacting:              # 压缩到一半进程没了：只告诉 compact() 结束了（算失败）
                self._compact_status = "EXITED"
                self._compact_done.set()
                was_active = False
            else:
                was_active = self._turn_active
            self._turn_active = False
            cold = self._cold_pending and not self._seen_action
        tail = " | ".join(list(self._stderr_tail)[-3:])
        if was_active:
            if deliberate:
                self.emit("turn_ended", status="interrupted", final_text="".join(self._parts))
            else:
                message = self._last_error or tail or "agy 进程意外退出（退出码 %s）" % code
                if cold:
                    self._report_resume_failed("冷续接失败：进程退出了。" + clip(message, 200))
                if not self._last_error:
                    self.emit("error", kind=classify_error(message), text=message)
                self.emit("turn_ended", status="failed", final_text="".join(self._parts),
                          error_kind=classify_error(message), error=message)
        elif not deliberate:
            self.emit("error", kind="other", text="agy 进程意外退出" + (("：" + tail) if tail else ""))
        self.set_state("offline")

    def _on_event(self, event: dict) -> None:
        name = event.get("event")
        if name == "init":
            cid = str(event.get("conversation_id") or "")
            init = event.get("init") if isinstance(event.get("init"), dict) else {}
            if cid and (cid != self.native_id or not self._session_emitted):
                # 续接时 agy 悄悄换成了别的对话号：把原来的号一起报出去，工位可以据此判断续接没接上
                previous = self.native_id if (self._session_emitted and self.native_id and cid != self.native_id) else None
                self.native_id = cid
                self._session_emitted = True
                extra = {"previous": previous} if previous else {}
                self.emit("session", native_id=cid, model=init.get("model"),
                          permission_mode=init.get("permission_mode"), **extra)
                if previous and self._cold_pending:
                    # 要接 A 对话，agy 却开了 B：新对话里没有之前的记忆，等于续接失败
                    self._report_resume_failed("冷续接时 agy 换成了另一个对话号，原来的对话没接上")
        elif name == "step_update":
            self._on_step(event.get("step_update") if isinstance(event.get("step_update"), dict) else {})
        elif name == "result":
            self._on_result(event.get("result") if isinstance(event.get("result"), dict) else {})
        elif name == "error":
            err = event.get("error")
            message = _first(err, "message", default=None) if isinstance(err, dict) else err
            message = str(message or event.get("message") or "agy 出错")
            self._last_error = message
            self.emit("error", kind=classify_error(message), text=message)

    def _ensure_turn_started(self) -> None:
        with self._lock:
            if self._turn_started:
                return
            self._turn_started = True
            self._turn_seq += 1
            seq = self._turn_seq
            if not self._turn_active:  # 没有人 send 过却来了动作（比如残留的输出）：也当一轮开始
                self._turn_active = True
        self.emit("turn_started", native_turn="agy-%d" % seq)

    def _mark_action(self) -> None:
        with self._lock:
            self._seen_action = True
            self._cold_pending = False  # 有动作 = 对话真的接上了

    def _usage_used(self, usage: Any) -> Optional[int]:
        """记忆条的"已用"：新输入 + 缓存读取（agy 的 input_tokens 不含缓存）。"""
        if not isinstance(usage, dict):
            return None
        used = usage.get("input_tokens")
        if not isinstance(used, int) or isinstance(used, bool):
            return None
        cached = usage.get("cache_read_tokens")
        if isinstance(cached, int) and not isinstance(cached, bool):
            used += cached
        return used

    def _on_step(self, step: dict) -> None:
        if self._compacting:                     # /compact 那一行：只认 checkpoint，不算一轮
            if step.get("step_type") == "checkpoint":
                self._compact_checkpoint = True
            used = self._usage_used(step.get("usage"))
            if used is not None:
                self.emit_usage(used, self._window, self._window_estimated, "agy_input")
            return
        self._ensure_turn_started()
        stype = step.get("step_type")
        state = str(step.get("state") or "ACTIVE").upper()
        if stype == "agent_response":
            delta = step.get("text_delta")
            if delta:
                self._mark_action()
                self._parts.append(str(delta))
                self.emit("text", text=str(delta))
        elif stype == "tool":
            idx = step.get("step_index")
            name = str(step.get("tool_name") or "")
            info = step.get("tool_info") if isinstance(step.get("tool_info"), dict) else {}
            kind, text = tool_info(name, info.get("parameters"))
            if idx not in self._tools_seen:
                self._tools_seen.add(idx)
                self._mark_action()
                self.emit("tool", kind=kind, text=text)
            if state in _DONE_STATES:
                output = _first(info, "output", default=_first(step, "output", "result", default=""))
                if not isinstance(output, str):
                    output = json.dumps(output, ensure_ascii=False)
                ok = state in _OK_STATES
                match = _EXIT_CODE_RE.search(output)
                if match and match.group(1) != "0":
                    ok = False
                self.emit("tool_output", text=clip_tail(output), ok=ok)
        # user_input / system_message / checkpoint 等步骤只看有没有用量
        used = self._usage_used(step.get("usage"))
        if used is not None:
            u = step["usage"]
            tu = self._turn_usage
            tu["in"] += int(u.get("input_tokens") or 0)
            tu["out"] += int(u.get("output_tokens") or 0) + int(u.get("thinking_tokens") or 0)
            tu["cached"] += int(u.get("cache_read_tokens") or 0)
            tu["total"] = tu["in"] + tu["out"] + tu["cached"]
            self._turn_usage_seen = True
            # agy 口径：agy 的 input_tokens 只算新读内容（见 probes.md 与 fixtures_adapters/agy_turn.jsonl），
            # 缓存读取由 cache_read_tokens 另算；因此 turn_fresh 直接取累计的 tu["in"]，turn_out 取累计写出的 tu["out"]。
            self.emit_usage(used, self._window, self._window_estimated, "agy_input",
                            turn_total=tu["total"], turn_out=tu["out"], turn_fresh=tu["in"])

    def _on_result(self, result: dict) -> None:
        if self._compacting:
            with self._lock:
                self._compact_status = str(result.get("status") or "SUCCESS").upper()
            self._compact_done.set()
            return
        self._ensure_turn_started()
        response = str(result.get("response") or "")
        if response.strip():
            self._mark_action()
        raw_status = str(result.get("status") or "SUCCESS").upper()
        if raw_status in ("SUCCESS", "OK", "COMPLETED", "DONE"):
            status = "completed"
        elif raw_status in ("CANCELLED", "CANCELED", "INTERRUPTED"):
            status = "interrupted"
        else:
            status = "failed"
        # result.usage 是**这个进程里到现在的累计**（M3 探针：第 3 轮 82,100 = 65,211 + 16,889），不是这一轮的，
        # 更不是当前上下文——热续接时直接拿它当这一轮的用量会越算越多。这一轮的用量改用各步 usage 相加
        # （单轮时两者相等：探针里 3 步 12,974 + 13,481 + 13,622 = 40,077）；没有任何步骤带 usage 时才退回它。
        usage = result.get("usage") if isinstance(result.get("usage"), dict) else {}
        # 冷续接后这一轮没动作就结束了 -> 续接失败（旧系统的"30 秒空转就结束"）
        with self._lock:
            cold_no_action = self._cold_pending and not self._seen_action
        if cold_no_action:
            self._report_resume_failed("冷续接后这一轮没有任何动作就结束了")
        text = response or "".join(self._parts)
        data: Dict[str, Any] = {"status": status, "final_text": text}
        if self._turn_usage_seen:
            data["usage"] = dict(self._turn_usage)
        elif usage and self._turn_seq <= 1:
            data["usage"] = {"in": int(usage.get("input_tokens") or 0), "out": int(usage.get("output_tokens") or 0),
                             "cached": int(usage.get("cache_read_tokens") or 0),
                             "total": int(usage.get("total_tokens") or 0)}
        denied = result.get("denied_actions")
        if denied:
            data["denied"] = [clip(item, 240) for item in (denied if isinstance(denied, list) else [denied])]
        if status == "failed":
            message = self._last_error or str(_first(result, "error", "message", default="") or "") \
                or response or "agy 这一轮失败了（%s）" % raw_status
            if not self._last_error:
                self.emit("error", kind=classify_error(message), text=clip(message, 500))
            data.update(error_kind=classify_error(message), error=clip(message, 500))
        with self._lock:
            self._turn_active = False
        self.set_state("idle")  # 先回到空闲再报"这一轮结束"（同 dsh / codex）
        self.emit("turn_ended", **data)
