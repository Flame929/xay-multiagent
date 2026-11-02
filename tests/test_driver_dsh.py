"""真实 dsh_headless 驱动 + 假 dsh 可执行文件的端到端测试（不用桩驱动）。

覆盖：
  1. 一轮正常完成 -> done，事件归一、usage、stream.jsonl 流式文字；
  2. 进行中 interrupt -> paused -> resume 新指示（--session-id 续接）-> done；
  3. 完成后 interrupt 被拒绝，仍是 done；
  4. 进程异常退出 -> failed，错误原因可见；模型约束与强度补丁。

假 dsh 通过 XAY_DSH_BIN 注入；它把收到的参数写进 FAKE_DSH_LOG 便于断言。
"""

from __future__ import annotations

import json
import os
import signal
import sys
import tempfile
import textwrap
import threading
import time
import unittest
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

from scripts.xay_agent import XayAgent, load_driver  # noqa: E402
from scripts.xay_ctl import CtlClient  # noqa: E402
from scripts.xay_drivers import dsh_headless  # noqa: E402


FAKE_DSH = textwrap.dedent(
    """\
    #!/usr/bin/env python3
    import json, os, sys, time

    MODE = os.environ.get("FAKE_DSH_MODE", "complete")
    argv = sys.argv[1:]
    patch = argv[argv.index("--patch") + 1] if "--patch" in argv else ""
    with open(os.environ["FAKE_DSH_LOG"], "a", encoding="utf-8") as fh:
        fh.write(json.dumps({"argv": argv,
                             "patch": open(patch, encoding="utf-8").read() if patch else ""},
                            ensure_ascii=False) + "\\n")
    session = argv[argv.index("--session-id") + 1] if "--session-id" in argv else None
    text = argv[-1]

    def emit(obj):
        sys.stdout.write(json.dumps(obj, ensure_ascii=False) + "\\n")
        sys.stdout.flush()

    if MODE == "crash":
        sys.stderr.write("fatal: credentials missing\\n")
        raise SystemExit(3)

    sid = session or "session-fake-1"
    emit({"type": "session", "sessionId": sid, "cwd": os.getcwd()})
    emit({"type": "status", "phase": "turn_start", "turn": 1})
    emit({"type": "status", "phase": "step_start", "turn": 1, "step": 1})

    def finish(reply):
        emit({"type": "text", "text": reply})
        emit({"type": "status", "phase": "step_end", "turn": 1, "step": 2,
              "usage": {"inputTokens": 10, "outputTokens": 3, "totalTokens": 13}})
        emit({"type": "status", "phase": "turn_end", "turn": 1, "reason": {"kind": "completed"}})
        emit({"type": "final", "text": reply})

    if session:
        finish("第二轮：" + text)
        raise SystemExit(0)

    emit({"type": "thinking", "text": "先看看文件"})
    emit({"type": "text", "text": "我先读取 README。"})
    emit({"type": "tool_call", "callId": "c1", "tool": "read", "input": {"file_path": "README.md"}})
    emit({"type": "tool_result", "callId": "c1", "status": "completed", "result": "hello"})
    emit({"type": "status", "phase": "step_end", "turn": 1, "step": 1,
          "usage": {"inputTokens": 100, "outputTokens": 9, "totalTokens": 109}})
    if MODE == "complete":
        emit({"type": "status", "phase": "step_start", "turn": 1, "step": 2})
        finish("第一轮完成")
        raise SystemExit(0)
    try:  # slow：回合保持进行中，等 interrupt 的 SIGINT
        while True:
            if os.getppid() == 1:
                raise SystemExit(0)
            time.sleep(0.1)
    except KeyboardInterrupt:
        raise SystemExit(130)
    """
)


def read_jsonl(path: Path):
    rows = []
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return rows
    for line in text.splitlines():
        try:
            rows.append(json.loads(line))
        except ValueError:
            continue
    return rows


def wait_until(predicate, timeout=15.0, message="等待超时"):
    deadline = time.time() + timeout
    last = None
    while time.time() < deadline:
        try:
            last = predicate()
        except (OSError, ValueError):
            last = None
        if last:
            return last
        time.sleep(0.03)
    raise AssertionError("%s（最后一个值：%r）" % (message, last))


def _terminate_and_kill(proc, timeout=2.0):
    if proc is None:
        return
    pid = proc.pid if hasattr(proc, "pid") else proc
    if not isinstance(pid, int):
        return
    try:
        pgid = os.getpgid(pid)
    except (ProcessLookupError, OSError):
        pgid = None

    def _sig(s):
        if pgid is not None and pgid != os.getpgrp():
            try:
                os.killpg(pgid, s)
            except (ProcessLookupError, OSError):
                pass
        try:
            os.kill(pid, s)
        except (ProcessLookupError, OSError):
            pass

    if hasattr(proc, "poll") and proc.poll() is not None:
        return

    _sig(signal.SIGTERM)
    deadline = time.time() + timeout
    while time.time() < deadline:
        if hasattr(proc, "poll") and proc.poll() is not None:
            return
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return
        except OSError:
            break
        time.sleep(0.05)
    _sig(signal.SIGKILL)
    if hasattr(proc, "wait"):
        try:
            proc.wait(timeout=1.0)
        except Exception:
            pass


class DshDriverTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="xay-dsh-", dir="/tmp")
        self.root = Path(self.tmp.name)
        self.home = self.root / "home"
        self.home.mkdir()
        self.project = self.root / "proj"
        self.project.mkdir()
        self.fake = self.root / "fake-dsh"
        self.fake.write_text(FAKE_DSH, encoding="utf-8")
        self.fake.chmod(0o755)
        self.log = self.root / "dsh-calls.jsonl"
        keys = ("XAY_HOME", "XAY_DSH_BIN", "FAKE_DSH_MODE", "FAKE_DSH_LOG",
                "XAY_DONE_GRACE_S", "XAY_PAUSE_TIMEOUT_S", "XAY_DSH_PROVIDER")
        self.saved_env = {key: os.environ.get(key) for key in keys}
        os.environ["XAY_HOME"] = str(self.home)
        os.environ["XAY_DSH_BIN"] = str(self.fake)
        os.environ["FAKE_DSH_LOG"] = str(self.log)
        os.environ["XAY_DONE_GRACE_S"] = "6"
        os.environ["XAY_PAUSE_TIMEOUT_S"] = "1.5"
        os.environ.pop("XAY_DSH_PROVIDER", None)
        self.agents = []
        self.procs = []

    def tearDown(self):
        try:
            for thread, agent in self.agents:
                if thread.is_alive():
                    try:
                        CtlClient(agent.run_id, xay_home=self.home, timeout=1).stop()
                    except Exception:
                        pass
                    thread.join(timeout=3)
        finally:
            for p in self.procs:
                _terminate_and_kill(p, timeout=2.0)
            for key, value in self.saved_env.items():
                if value is None:
                    os.environ.pop(key, None)
                else:
                    os.environ[key] = value
            self.tmp.cleanup()

    def _start(self, mode: str, card_extra=None, wait_socket=True):
        os.environ["FAKE_DSH_MODE"] = mode
        run_id = "dsh-" + mode
        run_dir = self.project / ".xay" / "runs" / run_id
        run_dir.mkdir(parents=True)
        card = {"prompt": "只读检查 README", "effort": "high", "mode": "readonly",
                "model": "deepseek-v4.1-flash"}
        card.update(card_extra or {})
        # session 是 xay 负责人会话（信封字段），不能被当成 dsh 的 --session-id。
        start = {"run": run_id, "agent": "dsh", "driver": "dsh-headless", "session": "ls_lead_fake",
                 "workdir": str(self.project), "task": "lifecycle", "card": card,
                 "safety_tier": "standard", "started": int(time.time())}
        (run_dir / "start.json").write_text(json.dumps(start, ensure_ascii=False), encoding="utf-8")
        driver = load_driver("dsh-headless")
        orig_popen = driver._popen
        def tracked_popen(*args, **kwargs):
            p = orig_popen(*args, **kwargs)
            self.procs.append(p)
            return p
        driver._popen = tracked_popen
        self.assertIsInstance(driver, dsh_headless.DshHeadlessDriver)
        agent = XayAgent(run_dir, driver=driver, xay_home=self.home, start=start)
        thread = threading.Thread(target=agent.run, daemon=True)
        self.agents.append((thread, agent))
        thread.start()
        if wait_socket:
            wait_until(lambda: (self.home / "ctl" / (run_id + ".sock")).exists(),
                       timeout=10, message="控制 socket 没出现")
        return agent, thread, run_dir

    def _events(self, run_dir):
        return read_jsonl(run_dir / "events.norm.jsonl")

    def _meta(self, run_dir):
        try:
            return json.loads((run_dir / "meta.json").read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None

    def _calls(self):
        return read_jsonl(self.log)

    def test_capabilities_and_registry(self):
        driver = load_driver("dsh")
        caps = driver.probe().as_dict()
        self.assertEqual(caps["steer"], "none")
        self.assertEqual(caps["interrupt"], "kill")
        self.assertTrue(caps["resume"])
        self.assertEqual(caps["usage_granularity"], "step")

    def test_model_constraint_and_effort_patch(self):
        with self.assertRaises(dsh_headless.DshModelError):
            dsh_headless.resolve_model({"model": "deepseek-chat"})
        self.assertEqual(dsh_headless.resolve_model({"model": "默认"}), "deepseek-v4.1-flash")
        text = dsh_headless.patch_text({"effort": "xhigh"})
        self.assertIn("model: deepseek-v4.1-flash", text)
        self.assertIn("reasoningEffort: xhigh", text)
        self.assertIn("provider: opencode-xay", text)

    def test_one_turn_completes_with_stream(self):
        agent, thread, run_dir = self._start("complete")
        thread.join(timeout=20)
        self.assertFalse(thread.is_alive(), "done 之后托管进程应正常退出")
        meta = self._meta(run_dir)
        self.assertEqual(meta.get("status"), "done", meta)
        self.assertEqual(meta.get("native_session_id"), "session-fake-1")
        events = self._events(run_dir)
        types = [e["type"] for e in events]
        for needed in ("session.started", "turn.started", "reasoning", "tool.call",
                       "tool.result", "usage", "turn.completed", "message"):
            self.assertIn(needed, types)
        self.assertEqual([e["seq"] for e in events], list(range(1, len(events) + 1)))
        final = [e for e in events if e["type"] == "message" and e.get("data", {}).get("final")]
        self.assertEqual(final[-1]["text"], "第一轮完成")
        # dsh 先发 turn_end 再发 final：驱动要把最终回复排在 turn.completed 之前，看板才不会多出一轮。
        self.assertLess(types.index("message"), types.index("turn.completed"))
        self.assertEqual(types[-1], "turn.completed")
        call = next(e for e in events if e["type"] == "tool.call")
        self.assertEqual(call.get("kind"), "read")
        # 流式文字：两段文字（被工具调用隔开）各算一条。
        stream = read_jsonl(run_dir / "stream.jsonl")
        self.assertEqual("".join(r["text_delta"] for r in stream), "我先读取 README。第一轮完成")
        self.assertEqual(len({r["item"] for r in stream}), 2)
        # 强度、模型、只读提示走补丁与提示词；首轮不带 --session-id。
        calls = self._calls()
        self.assertEqual(len(calls), 1)
        self.assertIn("--json", calls[0]["argv"])
        self.assertNotIn("--session-id", calls[0]["argv"])
        self.assertIn("reasoningEffort: high", calls[0]["patch"])
        self.assertEqual(calls[0]["argv"][-1], "只读检查 README")

    def test_interrupt_pause_resume_done_then_interrupt_rejected(self):
        agent, thread, run_dir = self._start("slow")
        client = CtlClient(agent.run_id, xay_home=self.home)
        wait_until(lambda: next((e for e in self._events(run_dir) if e["type"] == "tool.result"), None),
                   timeout=15, message="第一轮没有进入进行中状态")
        wait_until(lambda: read_jsonl(run_dir / "stream.jsonl"), timeout=5,
                   message="进行中没有流式文字")

        response = client.interrupt(by="lead:ls_fake")
        self.assertTrue(response.get("ok"), response)
        self.assertEqual(response.get("accepted_as"), "kill")
        self.assertEqual(client.status().get("state"), "paused")
        self.assertEqual((self._meta(run_dir) or {}).get("state"), "paused")
        self.assertEqual([e for e in self._events(run_dir) if e["type"] == "turn.completed"], [])

        resumed = client.resume("请只回复 PONG", by="lead:ls_fake")
        self.assertTrue(resumed.get("ok"), resumed)
        self.assertEqual(resumed.get("accepted_as"), "resume")
        wait_until(lambda: [e for e in self._events(run_dir)
                            if e["type"] == "message" and e.get("data", {}).get("final")
                            and "PONG" in (e.get("text") or "")],
                   timeout=20, message="resume 之后没有收到新回复")
        wait_until(lambda: (self._meta(run_dir) or {}).get("status") == "done",
                   timeout=15, message="第二轮没有以 done 收尾")

        response = client.interrupt(by="lead:ls_fake")
        self.assertFalse(response.get("ok"), response)
        self.assertIn("没有进行中的轮次", response.get("error", ""))
        self.assertEqual(client.status().get("state"), "done")
        thread.join(timeout=12)
        self.assertFalse(thread.is_alive())
        self.assertEqual(self._meta(run_dir).get("status"), "done")

        calls = self._calls()
        self.assertEqual(len(calls), 2)
        second = calls[1]["argv"]
        self.assertEqual(second[second.index("--session-id") + 1], "session-fake-1")
        self.assertEqual(second[-1], "请只回复 PONG")
        self.assertIn("reasoningEffort: high", calls[1]["patch"])

        events = self._events(run_dir)
        self.assertEqual([e["seq"] for e in events], list(range(1, len(events) + 1)))
        self.assertEqual(sum(1 for e in events if e["type"] == "session.started"), 1)
        self.assertEqual([e["data"]["turn_seq"] for e in events if e["type"] == "turn.started"], [1, 2])
        self.assertEqual(len([e for e in events if e["type"] == "turn.completed"]), 1)
        # 看板投影：恰好两轮 AI 消息，第二轮带最终回复（不能拆出空的一轮）
        sys.path.insert(0, str(PROJECT_ROOT / "scripts"))
        import xay_watch
        tail = xay_watch.RunTail(str(run_dir))
        tail.update()
        self.assertEqual(sorted(tail.turns), [1, 2])
        self.assertEqual(tail.turns[2]["final"], "第二轮：请只回复 PONG")
        self.assertEqual(tail.turns[2]["status"], "completed")
        # 第二轮开始时 stream.jsonl 被清空，只剩这一轮的文字。
        stream = read_jsonl(run_dir / "stream.jsonl")
        self.assertEqual("".join(r["text_delta"] for r in stream), "第二轮：请只回复 PONG")
        self.assertTrue(all(r["turn"] == 2 for r in stream), stream)
        # 带指示的控制请求进账本（看板据此显示负责人消息）。
        requested = [e for e in events if e["type"] == "control.requested"
                     and e.get("data", {}).get("action") == "resume"]
        self.assertEqual(requested[0]["data"].get("text"), "请只回复 PONG")

    def test_crash_is_failed_with_reason(self):
        agent, thread, run_dir = self._start("crash")
        thread.join(timeout=15)
        self.assertFalse(thread.is_alive())
        meta = self._meta(run_dir)
        self.assertEqual(meta.get("status"), "failed", meta)
        errors = [e for e in self._events(run_dir) if e["type"] == "error"]
        self.assertTrue(any("credentials missing" in (e.get("text") or "") for e in errors), errors)

    def test_disallowed_model_fails_before_spawn(self):
        agent, thread, run_dir = self._start("complete", {"model": "deepseek-chat"}, wait_socket=False)
        thread.join(timeout=10)
        self.assertEqual(self._meta(run_dir).get("status"), "failed")
        self.assertEqual(self._calls(), [])


if __name__ == "__main__":
    unittest.main()
