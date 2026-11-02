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


class OrgMessagesTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.home = Path(self.tmp.name)
        self.session = "lead-messages"
        self.env = {**os.environ, "XAY_HOME": str(self.home), "XAY_LEAD_SESSION": self.session,
                    "XAY_NOTIFY": "0", "XAY_TEST_CMD": "true"}
        with patch.dict(os.environ, self.env):
            xay_org.create_dept("fe", "前端部", "页面", session=self.session)
            xay_org.create_dept("be", "后端部", "接口", session=self.session)
            for dept, worker in (("fe", "小A"), ("fe", "小B"), ("be", "小C")):
                xay_org.add_member(dept, worker, self.session)

    def tearDown(self):
        self.tmp.cleanup()

    def tell_as(self, sender, target, text):
        with patch.dict(os.environ, {**self.env, "XAY_DEPT": "fe", "XAY_WORKER": sender,
                                     "XAY_ROLE": "executor"}):
            return xay_org.tell(target, text, self.session)

    def make_run(self, worker="小B", agent="codex", status="running", capabilities=None, driver="codex-app-server"):
        run_dir = self.home / "runs" / worker
        run_dir.mkdir(parents=True)
        (run_dir / "start.json").write_text(json.dumps({"pid": os.getpid(), "worker": worker, "dept": "fe",
            "agent": agent, "driver": driver}), encoding="utf-8")
        (run_dir / "meta.json").write_text(json.dumps({"status": status, "worker": worker, "dept": "fe",
            "agent": agent, "driver": driver, "capabilities": capabilities or {}}), encoding="utf-8")
        (run_dir / "events.norm.jsonl").write_text(json.dumps({"type": "turn.started", "data": {"native_turn_id": "turn-1"}}) + "\n", encoding="utf-8")
        row = {"kind": "run", "run": worker, "run_dir": str(run_dir), "worker": worker,
               "dept": "fe", "agent": agent, "lead_session": self.session}
        with (self.home / "registry.jsonl").open("a", encoding="utf-8") as out:
            out.write(json.dumps(row) + "\n")
        return run_dir

    def messages(self):
        with patch.dict(os.environ, self.env):
            return xay_org.messages(self.session)

    def test_employee_can_tell_same_department_and_cli_allowlist_accepts_it(self):
        result = subprocess.run([str(ROOT / "scripts" / "xay"), "tell", "小B", "请看一下页面"],
                                env={**self.env, "XAY_DEPT": "fe", "XAY_WORKER": "小A", "XAY_ROLE": "executor"},
                                text=True, capture_output=True, check=False)
        self.assertEqual(result.returncode, 0, result.stderr)
        message = json.loads(result.stdout)
        self.assertEqual(message["type"], "tell")
        self.assertEqual(message["to_worker"], "小B")
        reply = subprocess.run([str(ROOT / "scripts" / "xay"), "reply", message["id"], "收到并检查"],
                               env={**self.env, "XAY_DEPT": "fe", "XAY_WORKER": "小B", "XAY_ROLE": "executor"},
                               text=True, capture_output=True, check=False)
        self.assertEqual(reply.returncode, 0, reply.stderr)

    def test_cross_department_tell_is_rejected(self):
        with patch.dict(os.environ, {**self.env, "XAY_DEPT": "fe", "XAY_WORKER": "小A", "XAY_ROLE": "executor"}):
            with self.assertRaises(PermissionError):
                xay_org.tell("小C", "请帮忙", self.session)

    def test_third_topic_message_escalates_to_department_head(self):
        first = self.tell_as("小A", "小B", "先检查按钮")
        with patch.dict(os.environ, {**self.env, "XAY_DEPT": "fe", "XAY_WORKER": "小B", "XAY_ROLE": "executor"}):
            second = xay_org.reply(first["id"], "按钮有问题", self.session)
        with patch.dict(os.environ, {**self.env, "XAY_DEPT": "fe", "XAY_WORKER": "小A", "XAY_ROLE": "executor"}):
            third = xay_org.reply(second["id"], "请负责人决定", self.session)
        self.assertEqual(third["type"], "escalation")
        self.assertEqual(third["to"], "fe")
        self.assertNotIn("to_worker", third)

    def test_running_codex_receives_live_steer_and_delivery_record(self):
        self.make_run(capabilities={"steer": "live"})
        self.tell_as("小A", "小B", "实时消息")
        with patch.dict(os.environ, self.env), patch("xay_ctl.CtlClient.steer", return_value={"ok": True}) as steer:
            self.assertEqual(xay_org.supervise("fe", once=True, session=self.session), 1)
        self.assertIn("来自 fe 部门 小A 的留言：实时消息", steer.call_args.args[0])
        self.assertEqual(steer.call_args.kwargs["expected_turn"], "turn-1")
        message = next(m for m in self.messages() if m.get("type") == "tell")
        self.assertTrue(message["delivered"])

    def test_busy_non_live_worker_waits_then_receives_xay_send(self):
        run_dir = self.make_run(agent="dsh", driver="dsh-cli")
        self.tell_as("小A", "小B", "排队消息")
        runner = lambda argv, env: True
        with patch.dict(os.environ, self.env):
            self.assertEqual(xay_org.supervise("fe", once=True, session=self.session, action_runner=runner), 0)
        message = next(m for m in self.messages() if m.get("type") == "tell")
        self.assertFalse(message["delivered"])
        (run_dir / "meta.json").write_text(json.dumps({"status": "done", "agent": "dsh", "driver": "dsh-cli"}), encoding="utf-8")
        with patch.dict(os.environ, self.env):
            with patch.object(xay_org, "_run_action", return_value=True) as send:
                self.assertEqual(xay_org.supervise("fe", once=True, session=self.session), 1)
                self.assertEqual(xay_org.supervise("fe", once=True, session=self.session), 0)
        argv = send.call_args.args[0]
        self.assertEqual(argv[argv.index("send") + 1], "小B")
        self.assertIn("来自 fe 部门 小A 的留言：排队消息", argv)
        self.assertIn("--session", argv)
        message = next(m for m in self.messages() if m.get("type") == "tell")
        self.assertTrue(message["delivered"])

    def test_department_decision_is_delivered_to_original_asking_employee(self):
        with patch.dict(os.environ, {**self.env, "XAY_DEPT": "fe", "XAY_WORKER": "小A"}):
            request = xay_org.ask("be", "需要接口", session=self.session)
        with patch.dict(os.environ, {**self.env, "XAY_DEPT": "be", "XAY_WORKER": "小C"}):
            decision = xay_org.answer(request["id"], "accept", "按约定完成", self.session)
        self.assertEqual(decision["to_worker"], "小A")
        self.make_run(worker="小A", status="done")
        with patch.dict(os.environ, self.env), patch.object(xay_org, "_run_action", return_value=True) as send:
            self.assertEqual(xay_org.supervise("fe", once=True, session=self.session), 1)
        self.assertIn("小A", send.call_args.args[0])
        delivered = next(m for m in self.messages() if m.get("type") == "decision")
        self.assertTrue(delivered["delivered"])


if __name__ == "__main__":
    unittest.main()
