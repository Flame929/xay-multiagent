"""WP1：xay-agent 控制面和生命周期的最小真实行为测试。"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import threading
import time
import textwrap
import unittest
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "scripts"))

from xay_agent import XayAgent, load_driver  # noqa: E402
from xay_ctl import CtlClient  # noqa: E402


class FakeHandle:
    # 故意使用一个不存在的 pid，避免把测试进程误认为执行者。
    pid = 999999
    session_id = "fake-session"


class FakeDriver:
    name = "fake"

    def __init__(self):
        self.closed = threading.Event()
        self.calls = []

    def start(self, cwd, card, safety):
        self.calls.append(("start", cwd))
        return FakeHandle()

    def prompt(self, handle, text, output_schema=None):
        self.calls.append(("prompt", text))
        return "turn-fake"

    def steer(self, handle, text, expected_turn):
        self.calls.append(("steer", text, expected_turn))
        return True

    def interrupt(self, handle, grace_s=5):
        self.calls.append(("interrupt", grace_s))
        return "soft"

    def resume(self, native_session_id, cwd, safety):
        self.calls.append(("resume", native_session_id))
        return FakeHandle()

    def close(self, handle, discard=False):
        self.calls.append(("close", discard))
        self.closed.set()

    def events(self, handle):
        yield {"type": "session.started", "session": "fake-session"}
        yield {"type": "turn.started", "turn": "turn-fake"}
        while not self.closed.wait(0.02):
            yield {"type": "reasoning", "text": "working"}


def make_run(root: Path, name: str = "run-1") -> tuple[Path, Path]:
    run_dir = root / "project" / ".xay" / "runs" / name
    run_dir.mkdir(parents=True)
    start = {"run": name, "agent": "fake", "workdir": str(root / "project"), "task": "wp1"}
    (run_dir / "start.json").write_text(json.dumps(start), encoding="utf-8")
    return run_dir, root / "home"


class XayAgentTests(unittest.TestCase):
    def test_four_control_ops_and_event_files(self):
        with tempfile.TemporaryDirectory() as temp:
            run_dir, home = make_run(Path(temp))
            driver = FakeDriver()
            agent = XayAgent(run_dir, driver=driver, xay_home=home)
            thread = threading.Thread(target=agent.run)
            thread.start()
            self.assertTrue(self._wait_for(home / "ctl" / "run-1.sock"))
            client = CtlClient("run-1", xay_home=home)
            self.assertEqual(client.steer("请继续", "turn-fake")["accepted_as"], "steer")
            self.assertEqual(client.interrupt()["accepted_as"], "interrupt")
            self.assertEqual(client.resume("继续")["accepted_as"], "resume")
            self.assertEqual(client.stop()["accepted_as"], "stop")
            thread.join(3)
            self.assertFalse(thread.is_alive())
            self.assertTrue((run_dir / "events.norm.jsonl").is_file())
            meta = json.loads((run_dir / "meta.json").read_text(encoding="utf-8"))
            self.assertEqual(meta["status"], "canceled")
            self.assertTrue((home / "ctl" / "run-1.audit.jsonl").is_file())
            events = [json.loads(line) for line in (run_dir / "events.norm.jsonl").read_text().splitlines()]
            self.assertEqual([event["seq"] for event in events], list(range(1, len(events) + 1)))

    def test_driver_loader_decisions_signals_and_normalizer(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            driver_dir = root / "xay_drivers"
            driver_dir.mkdir()
            (driver_dir / "fake.py").write_text(
                "class Driver:\n"
                "    name = 'fake-loaded'\n"
                "    def start(self, cwd, card, safety): return object()\n",
                encoding="utf-8",
            )
            self.assertEqual(load_driver("fake", package="test_drivers", base_dir=driver_dir).name, "fake-loaded")

            run_dir, home = make_run(root, "run-files")

            class Normalizer:
                @staticmethod
                def normalize_event(raw, start):
                    return {"type": "message", "text": raw["text"], "task": start["task"]}

            agent = XayAgent(run_dir, driver=FakeDriver(), xay_home=home, normalizer=Normalizer())
            event = agent._normalize({"text": "normalized"})[0]
            self.assertEqual(event["text"], "normalized")
            self.assertEqual(event["task"], "wp1")
            agent._record_event(event)
            signal_item = agent.write_signal({"kind": "stalled", "text": "quiet"})
            decision = agent.write_decision({"kind": "question", "title": "need input"})
            self.assertTrue(signal_item["id"].startswith("sig_"))
            self.assertTrue(decision["id"].startswith("dec_"))
            self.assertEqual(len((run_dir / "signals.jsonl").read_text().splitlines()), 1)
            self.assertEqual(len((run_dir / "decisions.jsonl").read_text().splitlines()), 1)
            events = [json.loads(line) for line in (run_dir / "events.norm.jsonl").read_text().splitlines()]
            self.assertEqual([event["seq"] for event in events], [1, 2, 3])

    def test_second_agent_on_same_run_keeps_seq_and_skips_started_duplicates(self):
        class ReplayDriver(FakeDriver):
            def events(self, handle):
                yield {"type": "session.started", "session": "fake-session",
                       "data": {"native_session_id": "fake-session"}}
                yield {"type": "turn.started", "data": {"turn_seq": 1}}
                yield {"type": "usage", "data": {"in": 1, "out": 1, "cached": 0,
                                                 "total": 2}}
                yield {"type": "turn.completed", "data": {"status": "completed"}}

        with tempfile.TemporaryDirectory() as temp:
            run_dir, home = make_run(Path(temp), "run-restart")
            old_grace = os.environ.get("XAY_DONE_GRACE_S")
            os.environ["XAY_DONE_GRACE_S"] = "0"
            try:
                for _ in range(2):
                    # 不接真实归一化器：这些事件本身已经是信封，直接走兜底路径。
                    agent = XayAgent(run_dir, driver=ReplayDriver(), xay_home=home,
                                     normalizer=object())
                    agent.run()
            finally:
                if old_grace is None:
                    os.environ.pop("XAY_DONE_GRACE_S", None)
                else:
                    os.environ["XAY_DONE_GRACE_S"] = old_grace
            events = [json.loads(line) for line in
                      (run_dir / "events.norm.jsonl").read_text().splitlines()]
            seqs = [event["seq"] for event in events]
            self.assertEqual(seqs, sorted(seqs), "seq 必须单调")
            self.assertEqual(len(seqs), len(set(seqs)), "seq 不能重复")
            self.assertEqual(sum(1 for e in events
                                 if e.get("type") == "session.started"), 1)
            self.assertEqual(sum(1 for e in events
                                 if e.get("type") == "turn.started"), 1)
            self.assertEqual(sum(1 for e in events
                                 if e.get("type") == "usage"), 1)

    def test_executor_ancestor_is_rejected_and_audited(self):
        with tempfile.TemporaryDirectory() as temp:
            run_dir, home = make_run(Path(temp), "run-sec")
            driver = FakeDriver()
            agent = XayAgent(run_dir, driver=driver, xay_home=home, executor_pids={os.getpid()})
            thread = threading.Thread(target=agent.run)
            thread.start()
            self.assertTrue(self._wait_for(home / "ctl" / "run-sec.sock"))
            response = CtlClient("run-sec", xay_home=home).status()
            self.assertFalse(response["ok"])
            self.assertEqual(response["code"], "security")
            audit = (home / "ctl" / "run-sec.audit.jsonl").read_text(encoding="utf-8")
            self.assertIn('"kind":"security"', audit)
            driver.closed.set()
            thread.join(3)

    def test_killed_agent_writes_orphaned_meta(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            run_dir, home = make_run(root, "run-orphan")
            scripts = str(PROJECT_ROOT / "scripts")
            code = textwrap.dedent(
                f"""
                import sys, time
                sys.path.insert(0, {scripts!r})
                from xay_agent import XayAgent
                class H:
                    pid = 999999
                class D:
                    name = 'fake'
                    def start(self, *args):
                        return H()
                    def events(self, handle):
                        while True:
                            time.sleep(.05)
                            yield {{'type': 'message', 'text': 'x'}}
                    def close(self, *args):
                        pass
                XayAgent({str(run_dir)!r}, driver=D(), xay_home={str(home)!r}).run()
                """
            )
            process = subprocess.Popen([sys.executable, "-c", code])
            try:
                time.sleep(0.25)
                process.terminate()
                process.wait(timeout=3)
            finally:
                if process.poll() is None:
                    process.kill()
            meta = json.loads((run_dir / "meta.json").read_text(encoding="utf-8"))
            self.assertEqual(meta["status"], "orphaned")
            self.assertTrue(meta["orphaned"])

    @staticmethod
    def _wait_for(path: Path, timeout: float = 3.0) -> bool:
        deadline = time.time() + timeout
        while time.time() < deadline:
            if path.exists():
                return True
            time.sleep(0.01)
        return False


if __name__ == "__main__":
    unittest.main()
