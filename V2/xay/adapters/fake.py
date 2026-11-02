"""脚本化的假适配器：给工位 / 管家 / 场景测试用，不起任何进程。

``FakeAdapter(agent="codex", script=[...])`` 假装成某一家（能力、窗口、压缩门槛都取自提供者表），
每次 ``send()`` 消费 script 里的一段"一轮的脚本"，按步骤发出和真适配器一样的统一事件。

一轮的脚本 = 步骤列表（也可以直接写一个字符串，等于 ``[{"text": 字符串}]``；也可以是
``callable(text, extra_context) -> 步骤列表``）。script 用完后，之后每一轮默认回复 "OK"。

步骤（dict，一步一个键）：
    {"thinking": "…"}                      思考片段
    {"text": "…"}                          回复片段
    {"tool": "cmd", "text": "ls", "output": "…", "ok": True}     一次工具调用（output 可省略）
    {"plan": [("写测试", "in_progress"), ("跑测试", "pending")]} 计划步骤
    {"usage": 12345}                       记忆条（token 数）；也可以 {"usage": {"used": N, "window": W}}
    {"usage_pct": 80}                      记忆条（百分数；按窗口换算）
    {"compaction": {"before": 80, "after": 20}}   这一轮里发生了自动压缩
    {"sleep": 0.05}                        停一会（线程模式）
    {"hold": True}                         停住，直到 release() 或 interrupt()
    {"fail": {"kind": "capacity", "error": "…"}}  这一轮失败（error 事件 + turn_ended failed）
    {"end": {"status": "failed", "final_text": "…"}}  自己指定怎么结束
    {"die": True}                          进程死了：error + offline，alive() 变 False
    {"resume_failed": True}                报告续接失败（error kind=resume_failed），这一轮不结束
    {"error": {"kind": "other", "text": "…"}}   报一次错但这一轮不结束（死循环测试用）

``lazy_session=True``：像 dsh 一样，新对话的对话号要到第一轮才知道（open 时 native_id 为 None）。

控制：``inject_usage()`` 随时注入记忆条（比如直接拉到 80%）；``compact()`` 按提供者表决定支不支持
原生压缩；``fail_next(kind)`` 让下一轮失败；``release()`` 放行 hold；``sent`` / ``steered`` /
``interrupts`` 记录收到过什么。``sync=True`` 时 send() 在当前线程里把整轮跑完（不能 hold / sleep）。
"""

from __future__ import annotations

import itertools
import threading
import time
from typing import Any, Dict, List, Optional

from .. import providers
from .base import Adapter, AdapterBusy, AdapterError, Capabilities, classify_error

_counter = itertools.count(1)


class FakeAdapter(Adapter):
    is_fake = True

    def __init__(self, agent: str = "codex", script: Optional[List[Any]] = None, *, sync: bool = False,
                 window: Optional[int] = None, estimated: Optional[bool] = None,
                 tokens_per_turn: int = 2000, compact_ratio: float = 0.2,
                 open_error: Optional[str] = None, fail_resume: bool = False, lazy_session: bool = False) -> None:
        super().__init__()
        provider = providers.get(agent)
        self.agent = agent
        self.provider = provider
        self.capabilities = Capabilities(
            steer=provider.steer, interrupt=provider.interrupt, resume=True,
            native_compact=provider.native_compaction, usage="live" if agent == "codex" else "step",
            thinking=agent != "agy", plan=agent == "codex", persistent_process=agent != "dsh")
        self.script: List[Any] = list(script or [])
        self.sync = sync
        self.tokens_per_turn = tokens_per_turn
        self.compact_ratio = compact_ratio
        self.open_error = open_error
        self.fail_resume = fail_resume
        self.lazy_session = lazy_session
        self._window_arg, self._estimated_arg = window, estimated
        self.window, self.estimated = 0, False
        self.used = 0
        self.sent: List[tuple] = []
        self.steered: List[str] = []
        self.interrupts = 0
        self.turns = 0
        self._release = threading.Event()
        self._interrupted = threading.Event()
        self._worker: Optional[threading.Thread] = None
        self._turn_active = False
        self._fail_next: Optional[Dict[str, Any]] = None
        self._dead = False
        self._cold = False

    # ------------------------------------------------------------------

    def open(self, cwd: str, native_id: Optional[str] = None, card: Optional[dict] = None) -> None:
        with self._lock:
            if self._opened:
                raise AdapterError("假适配器已经打开")
        card = dict(card or {})
        if self.agent in (providers.DSH, providers.AGY):
            providers.validate_model(self.agent, card.get("model"))  # 和真适配器一样守住模型限制
        if self.open_error:
            raise AdapterError(self.open_error)
        self.card, self.cwd = card, str(cwd)
        model = providers.resolve_model(self.agent, card)
        if self._window_arg:
            self.window, self.estimated = int(self._window_arg), bool(self._estimated_arg)
        else:
            self.window, self.estimated = providers.context_window(self.agent, model)
        self._model_name = model
        if native_id or not self.lazy_session:
            self.native_id = native_id or "fake-%s-%d" % (self.agent, next(_counter))
        self._cold = bool(native_id)
        with self._lock:
            self._opened = True
        if self.native_id:
            self.emit("session", native_id=self.native_id, model=model, resumed=bool(native_id))
        self.set_state("idle")

    def alive(self) -> bool:
        return self._opened and not self._closed and not self._dead

    def close(self) -> None:
        with self._lock:
            if self._closed:
                return
            self._closed = True
        self._interrupted.set()
        self._release.set()
        worker = self._worker
        if worker is not None and worker is not threading.current_thread():
            worker.join(timeout=3.0)
        self.set_state("offline")
        self._finish_events()

    # ------------------------------------------------------------------

    def send(self, text: str, extra_context: Any = None) -> None:
        self._require_open()
        if self._dead:
            raise AdapterError("假适配器的进程已经\"死\"了")
        with self._lock:
            if self._turn_active:
                raise AdapterBusy("假适配器正在做上一轮")
            self._turn_active = True
        self.sent.append((text, extra_context))
        steps = self._next_steps(text, extra_context)
        self._interrupted.clear()
        self._release.clear()
        self.set_state("working")
        if self.sync:
            self._run_turn(steps)
        else:
            self._worker = threading.Thread(target=self._run_turn, args=(steps,), daemon=True)
            self._worker.start()

    def _next_steps(self, text: str, extra_context: Any) -> List[Dict[str, Any]]:
        item: Any = self.script.pop(0) if self.script else [{"text": "OK"}]
        if callable(item):
            item = item(text, extra_context)
        if isinstance(item, str):
            item = [{"text": item}]
        steps = [dict(s) for s in item]
        if self._fail_next is not None:
            steps = [{"fail": self._fail_next}] + steps
            self._fail_next = None
        if self._cold and self.fail_resume:
            steps = [{"resume_failed": True}] + steps
        self._cold = False
        return steps

    def fail_next(self, kind: str = "other", error: Optional[str] = None) -> None:
        self._fail_next = {"kind": kind, "error": error or "假的失败：%s" % kind}

    def release(self) -> None:
        """放行 {"hold": True}。"""
        self._release.set()

    def steer(self, text: str) -> bool:
        if self.capabilities.steer != "live" or not self._turn_active or self._closed:
            return False
        self.steered.append(text)
        return True

    def interrupt(self) -> str:
        self.interrupts += 1
        self._interrupted.set()
        self._release.set()
        return "soft" if self.capabilities.interrupt == "soft" else "kill"

    def inject_usage(self, used: Optional[int] = None, pct: Optional[float] = None,
                     window: Optional[int] = None, turn_total: Optional[int] = None,
                     turn_out: Optional[int] = None, turn_fresh: Optional[int] = None) -> None:
        """随时注入一条记忆条（给 used 或 pct 之一；turn_total / turn_out / turn_fresh 可选）。"""
        window = int(window or self.window)
        if used is None:
            if pct is None:
                raise ValueError("inject_usage 需要 used 或 pct")
            used = int(window * float(pct) / 100.0)
        self.used = int(used)
        self.emit_usage(self.used, window, self.estimated, self._source(),
                        turn_total=turn_total, turn_out=turn_out, turn_fresh=turn_fresh)

    def _source(self) -> str:
        return {"codex": "codex_last", "dsh": "dsh_total", "agy": "agy_input"}[self.agent]

    def compact(self) -> bool:
        """原生压缩：只有提供者表说支持的那家（codex）才成功；其余返回 False，工位改走交接压缩。"""
        self._require_open()
        if not self.capabilities.native_compact:
            return False
        with self._lock:
            if self._turn_active:
                raise AdapterBusy("正在做一轮，不能压缩")
        before_used = self.used
        self.set_state("compacting")
        self.emit("compaction_started", auto=False)
        self.used = int(before_used * self.compact_ratio)
        after = self.emit_usage(self.used, self.window, self.estimated, self._source())
        self.emit("compacted", auto=False, method="native",
                  before=round(100.0 * before_used / self.window, 1), before_used=before_used,
                  after=after.data["pct"], after_used=self.used)
        self.set_state("idle")
        return True

    # ------------------------------------------------------------------

    def _run_turn(self, steps: List[Dict[str, Any]]) -> None:
        self.turns += 1
        if not self.native_id:     # lazy_session：第一轮才有对话号（dsh 就是这样）
            self.native_id = "fake-%s-%d" % (self.agent, next(_counter))
            self.emit("session", native_id=self.native_id, model=getattr(self, "_model_name", None))
        self.emit("turn_started", native_turn="fake-turn-%d" % self.turns)
        texts: List[str] = []
        status, error, error_kind = "completed", None, None
        final_override: Optional[str] = None
        explicit_usage = False
        died = False
        for step in steps:
            if self._interrupted.is_set():
                status = "interrupted"
                break
            if "thinking" in step:
                self.emit("thinking", text=str(step["thinking"]))
            elif "text" in step and "tool" not in step:
                texts.append(str(step["text"]))
                self.emit("text", text=str(step["text"]))
            elif "tool" in step:
                self.emit("tool", kind=str(step["tool"]), text=str(step.get("text", "")))
                if "output" in step:
                    self.emit("tool_output", text=str(step["output"]), ok=bool(step.get("ok", True)))
            elif "plan" in step:
                steps_out = []
                for item in step["plan"]:
                    if isinstance(item, dict):
                        steps_out.append({"text": str(item["text"]), "status": item.get("status", "pending")})
                    else:
                        steps_out.append({"text": str(item[0]), "status": item[1]})
                self.emit("plan", steps=steps_out)
            elif "usage" in step or "usage_pct" in step:
                explicit_usage = True
                value = step.get("usage")
                turn_total = step.get("turn_total")
                turn_out = step.get("turn_out")
                turn_fresh = step.get("turn_fresh")
                if isinstance(value, dict):
                    self.inject_usage(used=value.get("used"), pct=value.get("pct"), window=value.get("window"),
                                      turn_total=value.get("turn_total", turn_total),
                                      turn_out=value.get("turn_out", turn_out),
                                      turn_fresh=value.get("turn_fresh", turn_fresh))
                elif value is not None:
                    self.inject_usage(used=int(value), turn_total=turn_total, turn_out=turn_out, turn_fresh=turn_fresh)
                else:
                    self.inject_usage(pct=float(step["usage_pct"]), turn_total=turn_total, turn_out=turn_out, turn_fresh=turn_fresh)
            elif "compaction" in step:
                info = step["compaction"] or {}
                self.emit("compaction_started", auto=True)
                after_pct = float(info.get("after", 20))
                self.used = int(self.window * after_pct / 100.0)
                self.emit_usage(self.used, self.window, self.estimated, self._source())
                data: Dict[str, Any] = {"auto": True, "method": "native_auto", "after": after_pct}
                if "before" in info:
                    data["before"] = float(info["before"])
                self.emit("compacted", **data)
            elif "sleep" in step:
                if self.sync:
                    raise ValueError("sync 模式不能 sleep")
                self._interrupted.wait(float(step["sleep"]))
            elif "hold" in step:
                if self.sync:
                    raise ValueError("sync 模式不能 hold")
                while not (self._release.is_set() or self._interrupted.is_set()):
                    time.sleep(0.005)
                if self._interrupted.is_set():
                    status = "interrupted"
                    break
            elif "fail" in step:
                info = step["fail"] or {}
                error_kind = info.get("kind", "other")
                error = info.get("error") or "假的失败"
                status = "failed"
                self.emit("error", kind=error_kind, text=error)
                break
            elif "end" in step:
                info = step["end"] or {}
                status = info.get("status", "completed")
                final_override = info.get("final_text")
                if status == "failed":
                    error = info.get("error") or "假的失败"
                    error_kind = info.get("kind") or classify_error(error)
                break
            elif "resume_failed" in step:
                self.emit("error", kind="resume_failed", text="假的续接失败")
            elif "error" in step:
                info = step["error"] or {}
                self.emit("error", kind=info.get("kind", "other"), text=info.get("text") or "假的报错")
            elif "die" in step:
                died = True
                break
        if died:
            self._dead = True
            self.emit("error", kind="other", text="假适配器的进程死了")
            with self._lock:
                self._turn_active = False
            self.emit("turn_ended", status="failed", final_text="".join(texts),
                      error_kind="other", error="假适配器的进程死了")
            self.set_state("offline")
            return
        if not explicit_usage and status != "interrupted":
            self.inject_usage(used=self.used + self.tokens_per_turn)
        data = {"status": status, "final_text": final_override if final_override is not None else "".join(texts)}
        if status == "failed":
            data.update(error_kind=error_kind or "other", error=error or "假的失败")
        with self._lock:
            self._turn_active = False
        self.set_state("idle")
        self.emit("turn_ended", **data)
