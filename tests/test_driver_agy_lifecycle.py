"""真实 agy_stream 驱动 + 假 agy 可执行文件的端到端生命周期测试（不用桩驱动）。

覆盖 arch-v4 §3.3/§3.5 的三条路径：
  1. 一轮正常完成 -> done；
  2. 进行中 interrupt -> paused（托管进程与 socket 都还在）-> resume --text
     -> 驱动用 --conversation 续接 -> 第二轮完成 -> done；
  3. 完成后 interrupt 被拒绝，任务仍是 done。

假 agy 通过 XAY_AGY_BIN 注入，驱动本身是 scripts/xay_drivers/agy_stream.py 里的
真实类，没有替换 events/prompt/interrupt。
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


FAKE_AGY = textwrap.dedent(
    """\
    #!/usr/bin/env python3
    import json, os, sys, time

    MODE = os.environ.get("FAKE_AGY_MODE", "slow")
    argv = sys.argv[1:]
    conversation = None
    if "--conversation" in argv:
        conversation = argv[argv.index("--conversation") + 1]
    conv = conversation or "conv-fake-1"

    def emit(obj):
        sys.stdout.write(json.dumps(obj, ensure_ascii=False) + "\\n")
        sys.stdout.flush()

    emit({"event": "init", "conversation_id": conv,
          "init": {"model": "fake-agy", "cwd": os.getcwd(),
                   "permission_mode": "read-only", "tools": []}})

    line = sys.stdin.readline()
    text = ""
    if line:
        try:
            text = json.loads(line).get("message", {}).get("content", "")
        except ValueError:
            text = ""

    if conversation:
        # resume 的回合：立即完成，回复里带上新指示，便于断言。
        emit({"event": "step_update", "step_update": {
            "conversation_id": conv, "step_index": 10, "state": "ACTIVE",
            "step_type": "agent_response", "text_delta": "第二轮："}})
        emit({"event": "step_update", "step_update": {
            "conversation_id": conv, "step_index": 10, "state": "DONE",
            "step_type": "agent_response", "text_delta": text,
            "usage": {"input_tokens": 5, "output_tokens": 5, "total_tokens": 10}}})
        emit({"event": "result", "result": {
            "conversation_id": conv, "status": "SUCCESS",
            "response": "第二轮：" + text, "num_turns": 1,
            "usage": {"input_tokens": 5, "output_tokens": 5, "total_tokens": 10}}})
        raise SystemExit(0)

    if MODE == "complete":
        emit({"event": "step_update", "step_update": {
            "conversation_id": conv, "step_index": 1, "state": "DONE",
            "step_type": "agent_response", "text_delta": "第一轮完成",
            "usage": {"input_tokens": 3, "output_tokens": 4, "total_tokens": 7}}})
        emit({"event": "result", "result": {
            "conversation_id": conv, "status": "SUCCESS",
            "response": "第一轮完成", "num_turns": 1,
            "usage": {"input_tokens": 3, "output_tokens": 4, "total_tokens": 7}}})
        raise SystemExit(0)

    # slow：保持回合进行中，等待 interrupt 的 SIGINT。
    emit({"event": "step_update", "step_update": {
        "conversation_id": conv, "step_index": 2, "state": "ACTIVE",
        "step_type": "tool", "tool_name": "read_file",
        "tool_info": {"parameters": {"Path": "README.md"}}}})
    try:
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


class AgyLifecycleTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="xay-al-", dir="/tmp")
        self.root = Path(self.tmp.name)
        self.home = self.root / "home"
        self.home.mkdir()
        self.project = self.root / "proj"
        self.project.mkdir()
        self.fake = self.root / "fake-agy"
        self.fake.write_text(FAKE_AGY, encoding="utf-8")
        self.fake.chmod(0o755)
        self.saved_env = {key: os.environ.get(key) for key in
                          ("XAY_HOME", "XAY_AGY_BIN", "FAKE_AGY_MODE",
                           "XAY_DONE_GRACE_S", "XAY_PAUSE_TIMEOUT_S")}
        os.environ["XAY_HOME"] = str(self.home)
        os.environ["XAY_AGY_BIN"] = str(self.fake)
        os.environ["XAY_DONE_GRACE_S"] = "6"
        os.environ["XAY_PAUSE_TIMEOUT_S"] = "1.5"
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

    def _start(self, mode: str):
        os.environ["FAKE_AGY_MODE"] = mode
        run_id = "agy-" + mode
        run_dir = self.project / ".xay" / "runs" / run_id
        run_dir.mkdir(parents=True)
        start = {
            "run": run_id, "agent": "agy", "driver": "agy-stream",
            "workdir": str(self.project), "task": "lifecycle",
            "card": {"prompt": "只读检查 README", "model": "fake-model",
                     "mode": "readonly"},
            "safety_tier": "standard", "started": int(time.time()),
        }
        (run_dir / "start.json").write_text(
            json.dumps(start, ensure_ascii=False), encoding="utf-8")
        (run_dir / "prompt.md").write_text("只读检查 README\n", encoding="utf-8")
        # 只用环境变量注入，故意不向驱动构造函数传 command。
        driver = load_driver("agy-stream")
        orig_popen = driver._popen
        def tracked_popen(*args, **kwargs):
            p = orig_popen(*args, **kwargs)
            self.procs.append(p)
            return p
        driver._popen = tracked_popen
        agent = XayAgent(run_dir, driver=driver, xay_home=self.home, start=start)
        thread = threading.Thread(target=agent.run, daemon=True)
        self.agents.append((thread, agent))
        thread.start()
        wait_until(lambda: (self.home / "ctl" / (run_id + ".sock")).exists(),
                   timeout=10, message="控制 socket 没出现")
        return agent, thread, run_dir

    def _events(self, run_dir: Path):
        return read_jsonl(run_dir / "events.norm.jsonl")

    def _meta(self, run_dir: Path):
        path = run_dir / "meta.json"
        if not path.is_file():
            return None
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None

    def test_turn_completed_then_interrupt_is_rejected_and_done(self):
        agent, thread, run_dir = self._start("complete")
        wait_until(lambda: next((e for e in self._events(run_dir)
                                if e.get("type") == "turn.completed"), None),
                   timeout=15, message="第一轮没有完成")
        client = CtlClient(agent.run_id, xay_home=self.home)
        response = client.interrupt(by="lead:ls_fake")
        self.assertFalse(response.get("ok"), response)
        self.assertIn("没有进行中的轮次", response.get("error", ""))
        self.assertEqual(client.status().get("state"), "done")
        thread.join(timeout=10)
        self.assertFalse(thread.is_alive(), "done 之后托管进程应正常退出")
        meta = self._meta(run_dir)
        self.assertEqual(meta.get("status"), "done", meta)
        events = self._events(run_dir)
        self.assertEqual([e["seq"] for e in events], list(range(1, len(events) + 1)))
        self.assertEqual(sum(1 for e in events if e.get("type") == "turn.completed"), 1)
        self.assertEqual([e.get("data", {}).get("status") for e in events
                          if e.get("type") == "turn.completed"], ["completed"])

    def test_interrupt_then_resume_runs_second_turn_and_done(self):
        agent, thread, run_dir = self._start("slow")
        client = CtlClient(agent.run_id, xay_home=self.home)
        wait_until(lambda: next((e for e in self._events(run_dir)
                                if e.get("type") == "tool.call"), None),
                   timeout=15, message="第一轮没有进入进行中状态")

        response = client.interrupt(by="lead:ls_fake")
        self.assertTrue(response.get("ok"), response)
        self.assertEqual(response.get("accepted_as"), "kill", response)
        self.assertEqual(client.status().get("state"), "paused")
        self.assertTrue((self.home / "ctl" / (agent.run_id + ".sock")).exists(),
                        "paused 期间控制 socket 必须可用")
        first_turn = [e for e in self._events(run_dir)
                      if e.get("type") == "turn.completed"]
        self.assertEqual(first_turn, [], "中断不应产生完成事件")

        resumed = client.resume("请只回复 PONG", by="lead:ls_fake")
        self.assertTrue(resumed.get("ok"), resumed)
        self.assertEqual(resumed.get("accepted_as"), "resume")

        messages = wait_until(
            lambda: [e for e in self._events(run_dir)
                     if e.get("type") == "message" and e.get("data", {}).get("final")
                     and "PONG" in (e.get("text") or "")],
            timeout=20, message="resume 之后没有收到新回复")
        self.assertTrue(messages)
        wait_until(lambda: (self._meta(run_dir) or {}).get("status") == "done",
                   timeout=15, message="第二轮没有以 done 收尾")

        # 完成后 interrupt 只能是拒绝，不能改变 done。
        response = client.interrupt(by="lead:ls_fake")
        self.assertFalse(response.get("ok"), response)
        self.assertIn("没有进行中的轮次", response.get("error", ""))
        self.assertEqual(client.status().get("state"), "done")
        thread.join(timeout=12)
        self.assertFalse(thread.is_alive())
        meta = self._meta(run_dir)
        self.assertEqual(meta.get("status"), "done", meta)

        events = self._events(run_dir)
        self.assertEqual([e["seq"] for e in events], list(range(1, len(events) + 1)),
                         "seq 必须单调且无重复")
        self.assertEqual(sum(1 for e in events if e.get("type") == "session.started"), 1,
                         "续接不能重复写 session.started")
        started = [e for e in events if e.get("type") == "turn.started"]
        self.assertEqual([e["data"]["turn_seq"] for e in started], [1, 2])
        self.assertEqual(len([e for e in events
                              if e.get("type") == "turn.completed"]), 1)


if __name__ == "__main__":
    unittest.main()
