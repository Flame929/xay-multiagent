import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
import xay_org


class SuperviseTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.home = Path(self.tmp.name)
        self.env = {**os.environ, "XAY_HOME": str(self.home), "XAY_LEAD_SESSION": "lead-test",
                    "XAY_NOTIFY": "0", "XAY_TEST_CMD": "exit 1"}
        with patch.dict(os.environ, self.env):
            xay_org.create_dept("fe", "前端部", "交付页面", session="lead-test")

    def tearDown(self):
        self.tmp.cleanup()

    def make_run(self, name="r1", worker="小A", agent="codex", status="failed", fail_reason="cli_error", **meta):
        run_dir = self.home / "runs" / name
        run_dir.mkdir(parents=True)
        started = 1000
        row = {"kind": "run", "run": name, "run_dir": str(run_dir), "worker": worker, "dept": "fe",
               "agent": agent, "name": worker, "title": "实现页面", "task": "same-task",
               "lead_session": "lead-test", "origin": str(self.home), "time": ""}
        (run_dir / "start.json").write_text(json.dumps({"worker": worker, "agent": agent, "dept": "fe",
            "name": worker, "title": "实现页面", "task": "same-task", "origin": str(self.home),
            "started": started, "source": "sub", "driver": "codex-app-server",
            "card": {"prompt": "实现页面"}, **meta}), encoding="utf-8")
        (run_dir / "meta.json").write_text(json.dumps({"status": status, "fail_reason": fail_reason,
            "started": started, "origin": str(self.home), "land": "", **meta}), encoding="utf-8")
        with (self.home / "registry.jsonl").open("a", encoding="utf-8") as out:
            out.write(json.dumps(row) + "\n")
        with patch.dict(os.environ, self.env):
            xay_org.add_member("fe", worker, "lead-test")
        return row, run_dir

    def messages(self):
        with patch.dict(os.environ, self.env):
            return xay_org.messages("lead-test")

    def test_non_quota_failure_retries_once_then_escalates_and_deduplicates(self):
        self.make_run()
        with patch.dict(os.environ, self.env), patch.object(xay_org, "_send_retry", return_value=True) as retry:
            self.assertEqual(xay_org.supervise("fe", once=True, session="lead-test"), 1)
            self.assertEqual(xay_org.supervise("fe", once=True, session="lead-test"), 0)
        retry.assert_called_once()
        autos = [m for m in self.messages() if m["type"] == "auto"]
        self.assertEqual(len(autos), 1)
        self.assertEqual(autos[0]["to"], "fe")

    def test_failure_after_one_retry_escalates_to_department_head(self):
        self.make_run("r1")
        with patch.dict(os.environ, self.env), patch.object(xay_org, "_send_retry", return_value=True):
            xay_org.supervise("fe", once=True, session="lead-test")
        self.make_run("r2")
        with patch.dict(os.environ, self.env):
            self.assertEqual(xay_org.supervise("fe", once=True, session="lead-test"), 1)
        self.assertTrue(any(m["type"] == "escalation" and m["to"] == "fe" for m in self.messages()))

    def test_quota_failure_uses_line_reassignment(self):
        self.make_run(fail_reason="usage_limit_exceeded", codex_source="sub")
        with patch.dict(os.environ, self.env), patch.object(xay_org, "_run_action", return_value=True) as run:
            self.assertEqual(xay_org.supervise("fe", once=True, session="lead-test"), 1)
        self.assertIn("--source", run.call_args.args[0])
        self.assertEqual(run.call_args.args[0][run.call_args.args[0].index("--source") + 1], "auto")  # 由 xay-run 按额度选 Plus / 中转
        self.assertTrue(any(m["type"] == "auto" and "额度" in m["subject"] for m in self.messages()))

    def test_other_agent_quota_failure_uses_next_preference(self):
        self.make_run(agent="dsh", fail_reason="usage_limit_exceeded", prefer=["agy", "codex", "dsh"])
        with patch.dict(os.environ, self.env), patch.object(xay_org, "_run_action", return_value=True) as run:
            self.assertEqual(xay_org.supervise("fe", once=True, session="lead-test"), 1)
        argv = run.call_args.args[0]
        self.assertEqual(argv[1], "agy")
        self.assertEqual(argv[argv.index("--as") + 1], "小A-agy")

    def test_conflict_retries_land_once(self):
        row, run_dir = self.make_run(status="done", fail_reason="", land="conflict")
        with patch.dict(os.environ, self.env), patch.object(xay_org, "_patch_already_applied", return_value=False), \
                patch.object(xay_org, "_run_action", return_value=True) as run:
            self.assertEqual(xay_org.supervise("fe", once=True, session="lead-test"), 1)
            self.assertEqual(xay_org.supervise("fe", once=True, session="lead-test"), 0)
        self.assertIn(str(run_dir), run.call_args.args[0])
        self.assertTrue(any(m["type"] == "auto" and m["subject"] == "冲突自动处理" for m in self.messages()))

    def test_silent_live_worker_gets_progress_prompt(self):
        row, run_dir = self.make_run(status="running", fail_reason="", capabilities={"steer": "live"})
        (run_dir / "meta.json").write_text(json.dumps({"status": "running", "started": 1000,
            "capabilities": {"steer": "live"}, "driver": "codex-app-server"}), encoding="utf-8")
        (run_dir / "events.norm.jsonl").write_text(json.dumps({"type": "turn.started", "ts": 1000}) + "\n", encoding="utf-8")
        with patch.dict(os.environ, self.env), patch.object(xay_org, "_send_retry", return_value=True) as remind:
            self.assertEqual(xay_org.supervise("fe", once=True, session="lead-test", now=2000), 1)
            self.assertEqual(xay_org.supervise("fe", once=True, session="lead-test", now=2000), 0)
        self.assertEqual(remind.call_args.args[3], "汇报一下进度")
        self.assertTrue(remind.call_args.kwargs["now"])

    def test_three_failures_write_user_decision(self):
        self.make_run("r1", worker="小A", fail_reason="cli_error")
        self.make_run("r2", worker="小A", fail_reason="cli_error")
        _, last = self.make_run("r3", worker="小A", fail_reason="cli_error")
        with patch.dict(os.environ, self.env):
            self.assertEqual(xay_org.supervise("fe", once=True, session="lead-test"), 1)
        decision = json.loads((last / "decisions.jsonl").read_text().splitlines()[0])
        self.assertEqual(decision["owner"], "user")
        self.assertTrue(any(m["type"] == "auto" and "反复失败" in m["subject"] for m in self.messages()))

    def test_direction_change_writes_user_decision(self):
        _, run_dir = self.make_run("direction", status="done", fail_reason="", direction_changed=True)
        with patch.dict(os.environ, self.env):
            self.assertEqual(xay_org.supervise("fe", once=True, session="lead-test"), 1)
            self.assertEqual(xay_org.supervise("fe", once=True, session="lead-test"), 0)
        decision = json.loads((run_dir / "decisions.jsonl").read_text().splitlines()[0])
        self.assertEqual(decision["owner"], "user")
        self.assertTrue(any(m["type"] == "auto" and "方向变更" in m["subject"] for m in self.messages()))

    def test_xay_test_cmd_generates_fake_failure_metadata(self):
        project = self.home / "project"
        project.mkdir()
        with patch.dict(os.environ, self.env):
            result = subprocess.run([str(ROOT / "scripts" / "xay-run"), "codex", "--as", "小B", "--dept", "fe",
                                     "--isolate", "off", "-C", str(project), "模拟失败"],
                                    env=self.env, text=True, capture_output=True, timeout=20)
        self.assertNotEqual(result.returncode, 0)
        entries = [json.loads(line) for line in (self.home / "registry.jsonl").read_text().splitlines()]
        run = next(row for row in entries if row.get("worker") == "小B")
        meta = json.loads((Path(run["run_dir"]) / "meta.json").read_text())
        self.assertEqual(meta["status"], "failed")
        self.assertEqual(meta["worker"], "小B")


if __name__ == "__main__":
    unittest.main()
