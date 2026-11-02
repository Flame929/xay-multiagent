"""M5 切换：scripts/xay 默认转给新版 V2，但旧会话、旧执行者照旧走旧版（scripts/xay_switch.py）。"""
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
import xay_switch  # noqa: E402

XAY = ROOT / "scripts" / "xay"
CLEAN = ("XAY_V1", "XAY_ROLE", "XAY_LEAD_SESSION", "CLAUDE_CODE_SESSION_ID", "CODEX_THREAD_ID",
         "XAY2_TASK_FILE", "XAY2_TASK", "XAY2_OUTBOX", "XAY2_EM", "XAY2_CONV", "PYTHONPATH")


class RouteRules(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.env = {"XAY_HOME": self.tmp.name}

    def go(self, *argv, **env):
        target, rest, _, guessed = xay_switch.route(list(argv), {**self.env, **env})
        return target, rest, guessed

    def target(self, *argv, **env):
        return self.go(*argv, **env)[0]

    def test_old_only_commands_stay_on_old_system(self):
        for argv in (["session", "begin", "--lead", "codex"], ["ctl", "run_x", "steer", "--text", "hi"],
                     ["decide", "r", "rework", "--id", "d1", "--version", "1"], ["doctor"], ["status", "--follow", "60"],
                     ["quota"], ["route"], ["agy-accounts"], ["agy-switch", "2"], ["workers"], ["send", "小A", "接着做"],
                     ["inbox", "--gm"], ["contract", "show", "api"], ["supervise", "--once"], ["escalate", "-", "x"],
                     ["report", "x"], ["lead"], ["worker", "resolve", "--name", "小A"]):
            self.assertEqual(self.target(*argv), "v1", argv)

    def test_new_only_commands_go_to_v2(self):
        for argv in (["begin", "--title", "做页面"], ["plan", "add", "写接口", "--agent", "codex"], ["accept", "k1"],
                     ["brief"], ["snapshot", "--json"], ["events", "--since", "3"], ["office", "status"], ["rebuild"],
                     ["steer", "em_x", "先别改"], ["interrupt", "em_x"], ["stop", "--all"], ["resume"], ["hire", "codex"],
                     ["progress", "1/2", "x"], ["done", "好了"], ["notes", "x"], ["compact", "em_x"], ["something-new"]):
            self.assertEqual(self.target(*argv), "v2", argv)
        self.assertEqual(self.target("--conv", "cv_1", "wait"), "v2")
        self.assertEqual(self.target("--conv=cv_1", "brief"), "v2")

    def test_new_commands_go_to_v2_even_in_an_old_session(self):
        self.assertEqual(self.target("brief", XAY_LEAD_SESSION="ls_1"), "v2")
        self.assertEqual(self.target("plan", "add", "x", XAY_LEAD_SESSION="ls_1"), "v2")

    def test_wait_by_arguments(self):
        self.assertEqual(self.go("wait", "--session", "ls_1", "--timeout", "240"), ("v1", ["wait", "--session", "ls_1", "--timeout", "240"], False))
        self.assertEqual(self.target("wait", "--session=ls_1"), "v1")
        self.assertEqual(self.target("wait", "--phase", "x", "--session", "ls_1"), "v1")
        self.assertEqual(self.go("wait", "--since", "12", "--timeout", "540")[::2], ("v2", False))
        self.assertEqual(self.go("wait", "--timeout", "240")[::2], ("v2", True))       # 猜的：没跑成功要提示旧版写法

    def test_dept_by_arguments(self):
        self.assertEqual(self.target("dept", "create", "fe", "--name", "前端部"), "v1")
        self.assertEqual(self.target("dept", "list"), "v1")
        self.assertEqual(self.target("dept", "open", "fe", "--name", "前端部"), "v2")
        self.assertEqual(self.target("dept", "open", "fe", "--name", "x", XAY_LEAD_SESSION="ls_1"), "v2")
        self.assertEqual(self.target("dept", "close", "fe", "--json"), "v1")
        self.assertEqual(self.go("dept", "close", "fe")[::2], ("v2", True))
        self.assertEqual(self.go("dept", "close", "fe", XAY_LEAD_SESSION="ls_1")[::2], ("v1", True))

    def test_answer_and_reply_by_id(self):
        self.assertEqual(self.target("answer", "m_1793300000_ab12", "accept", "好"), "v1")
        for i in ("dc_x1", "pp_x1", "lt_x1"):
            self.assertEqual(self.target("answer", i, "accept", XAY_LEAD_SESSION="ls_1"), "v2", i)
        self.assertEqual(self.target("answer", "x1", "merge"), "v2")                    # merge / rework 只有新版认
        self.assertEqual(self.go("answer", "-", "accept", "好", XAY_LEAD_SESSION="ls_1")[::2], ("v1", True))
        self.assertEqual(self.target("reply", "m_1_ab", "收到"), "v1")
        self.assertEqual(self.target("reply", "lt_1", "收到", XAY_LEAD_SESSION="ls_1"), "v2")
        self.assertEqual(self.target("reply", "x", "收到", "--subject", "回信"), "v2")

    def test_ask_and_tell(self):
        self.assertEqual(self.target("ask", "be", "要接口", "--wait", "600"), "v1")
        self.assertEqual(self.target("ask", "be", "要接口", "--contract", "api"), "v1")
        self.assertEqual(self.target("ask", "be", "要接口", "--subject", "接口", XAY_LEAD_SESSION="ls_1"), "v2")
        self.assertEqual(self.go("ask", "be", "要接口")[::2], ("v2", True))
        self.assertEqual(self.go("ask", "be", "要接口", XAY_LEAD_SESSION="ls_1")[::2], ("v1", True))
        self.assertEqual(self.target("tell", "em_c0de0001", "先别改", XAY_LEAD_SESSION="ls_1"), "v2")
        self.assertEqual(self.go("tell", "小B", "看一下")[::2], ("v2", True))
        self.assertEqual(self.go("tell", "小B", "看一下", "--session", "ls_1")[::2], ("v1", True))

    def test_open_old_session_of_this_claude_conversation_counts_as_old(self):
        sessions = Path(self.tmp.name) / "sessions"
        sessions.mkdir()
        (sessions / "ls_20261031-000000_ab12.json").write_text(json.dumps({"id": "ls_20261031-000000_ab12", "lead_key": "claude:abc", "ended": None}))
        (sessions / "ls_20261030-000000_cd34.json").write_text(json.dumps({"id": "ls_20261030-000000_cd34", "lead_key": "claude:old", "ended": 5}))
        self.assertEqual(self.target("tell", "小B", "x", CLAUDE_CODE_SESSION_ID="abc"), "v1")
        self.assertEqual(self.target("tell", "小B", "x", CLAUDE_CODE_SESSION_ID="old"), "v2")      # 旧会话已结束
        self.assertEqual(self.target("tell", "小B", "x", CLAUDE_CODE_SESSION_ID="other"), "v2")
        self.assertEqual(self.target("brief", CLAUDE_CODE_SESSION_ID="abc"), "v2")

    def test_executors(self):
        old = {"XAY_ROLE": "executor", "XAY_DEPT": "fe", "XAY_WORKER": "小A"}
        for argv in (["ask", "be", "要接口"], ["tell", "小B", "x"], ["reply", "m_1_ab", "收到"], ["inbox"], ["progress", "1/2", "x"]):
            self.assertEqual(self.target(*argv, **old), "v1", argv)
        new = {"XAY_ROLE": "executor", "XAY2_EM": "em_x", "XAY2_TASK_FILE": "/tmp/t"}
        for argv in (["ask", "fe", "要接口"], ["tell", "em_y", "x"], ["reply", "lt_1", "收到"], ["progress", "1/2", "x"], ["done", "好了"]):
            self.assertEqual(self.target(*argv, **new), "v2", argv)

    def test_overrides(self):
        self.assertEqual(self.go("v1", "dept", "close", "fe"), ("v1", ["dept", "close", "fe"], False))
        self.assertEqual(self.go("v2", "tell", "小B", "x", XAY_LEAD_SESSION="ls_1"), ("v2", ["tell", "小B", "x"], False))
        for value in ("1", "true", "yes", "on"):
            self.assertEqual(self.target("brief", XAY_V1=value), "v1")
            self.assertEqual(self.target("begin", "--title", "x", XAY_V1=value), "v1")
        self.assertEqual(self.target("brief", XAY_V1="0"), "v2")
        self.assertEqual(self.target(), "help")
        self.assertEqual(self.target("--help"), "help")


class EndToEnd(unittest.TestCase):
    """真的跑 scripts/xay：临时 XAY_HOME / XAY2_HOME，不碰本机的账本和端口。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.env = {k: v for k, v in os.environ.items() if k not in CLEAN}
        self.env.update(XAY_HOME=str(Path(self.tmp.name) / "old"), XAY2_HOME=str(Path(self.tmp.name) / "v2"), XAY_NOTIFY="0")

    def xay(self, *args, **env):
        return subprocess.run([str(XAY), *args], env={**self.env, **env}, cwd=self.tmp.name,
                              text=True, capture_output=True, timeout=60)

    def test_old_only_command_runs_old_cli(self):
        out = self.xay("doctor")
        self.assertEqual(out.returncode, 0, out.stderr)
        self.assertIn("slots", json.loads(out.stdout))

    def test_new_command_runs_v2(self):
        out = self.xay("begin", "--title", "切换冒烟")
        self.assertEqual(out.returncode, 0, out.stderr)
        conv = out.stdout.strip().splitlines()[-1]
        self.assertTrue(conv.startswith("cv_"), out.stdout)
        out = self.xay("--conv", conv, "brief")
        self.assertEqual(out.returncode, 0, out.stderr)
        self.assertIn("切换冒烟", out.stdout)
        self.assertTrue((Path(self.tmp.name) / "v2" / "xay.db").exists())

    def test_xay_v1_sends_everything_to_old_cli(self):
        out = self.xay("brief", XAY_V1="1")
        self.assertEqual(out.returncode, 2)
        self.assertIn("invalid choice", out.stderr)               # 旧版没有 brief
        self.assertIn("提示：旧版（V1）已标为 legacy，只作退路，不再更新。", out.stderr)
        self.assertFalse((Path(self.tmp.name) / "v2" / "xay.db").exists())

    def test_explicit_v1_prints_legacy_notice_to_stderr(self):
        out = self.xay("v1", "brief")
        self.assertEqual(out.returncode, 2)
        self.assertIn("提示：旧版（V1）已标为 legacy，只作退路，不再更新。", out.stderr)
        self.assertNotIn("提示：旧版（V1）", out.stdout)

    def test_guessed_route_prints_other_side_on_failure(self):
        out = self.xay("wait", "--timeout", "0")                   # 新版找不到对话 → 提示旧版写法
        self.assertEqual(out.returncode, 2)
        self.assertIn("xay v1 wait", out.stderr)
        self.assertIn("--session", out.stderr)

    def test_which_and_help(self):
        out = self.xay("--which", "dept", "create", "fe")
        self.assertEqual((out.returncode, out.stdout.split("：")[0]), (0, "旧版"))
        out = self.xay()
        self.assertEqual(out.returncode, 0)
        self.assertIn("XAY_V1=1", out.stdout)


if __name__ == "__main__":
    unittest.main()
