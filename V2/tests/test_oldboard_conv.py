"""旧看板（scripts/xay-watch.html）数据接口测试。

测试覆盖：
- conversation():
  - threads 数量与结构
  - Codex 任务（做完两轮，第二轮前有一次插话）：消息顺序与种类、做完的有 final 和 files
  - agy 任务（正在做，直播文件里有 3 条 tool 行）：streaming=true 且最后一步 active
  - 旧系统 bridge.run：消息结构与状态
- diff(): 对不存在的任务不抛异常，返回 {"diff": ""}
- control(): steer 写出 control.requested{action: steer}
- decide(): 正常解决决定
"""
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

HERE = Path(__file__).resolve().parent
V2 = HERE.parent
if str(V2) not in sys.path:
    sys.path.insert(0, str(V2))

from xay.core import db as core_db
from xay.core import events as core_events
from xay.office import oldboard_conv, paths
from xay.office.web import HttpError


class OldBoardConvTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="xay-oldboard-test-"))
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.env_patcher = mock.patch.dict(
            os.environ,
            {
                "XAY2_HOME": str(self.tmp / "v2"),
                "XAY_HOME": str(self.tmp / "old"),
            },
        )
        self.env_patcher.start()
        self.addCleanup(self.env_patcher.stop)

        self.db_path = self.tmp / "v2" / "ledger.db"
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = core_db.open_db(self.db_path)
        self.addCleanup(self.conn.close)

        self.ws = "ws_test01"
        self.conv = "cv_test01"
        self.t0 = 1793300000

        self._seed_data()

    def _append(self, typ, by, data, subject=None, ts=None):
        return core_events.append(
            self.conn, typ, by, data, ws=self.ws, conv=self.conv, subject=subject, ts=ts
        )

    def _seed_data(self):
        A = self._append
        lead = f"lead:{self.conv}"
        # 对话与部门
        A("conv.begun", lead, {"title": "老看板对话测试", "lead_kind": "claude", "cwd": "/tmp/ws"}, self.conv, self.t0)
        A("dept.opened", lead, {"name": "开发部", "mission": "研发功能", "owns": []}, "dev", self.t0 + 1)

        # 员工
        A("employee.hired", lead, {"agent": "codex", "label": "Codex 1"}, "em_codex", self.t0 + 2)
        A("employee.joined", lead, {"dept": "dev"}, "em_codex", self.t0 + 3)

        A("employee.hired", lead, {"agent": "agy", "label": "Gemini 1"}, "em_agy", self.t0 + 4)
        A("employee.joined", lead, {"dept": "dev"}, "em_agy", self.t0 + 5)

        # 决策
        A(
            "decision.opened",
            "office",
            {
                "kind": "question",
                "title": "是否开启缓存？",
                "options": [{"id": "yes", "label": "是"}, {"id": "no", "label": "否"}],
                "owner": "user",
                "for_user": True,
            },
            "dc_test01",
            self.t0 + 6,
        )

        # 1. Codex 任务（做完两轮，第二轮前有一次插话）
        self.tk_codex = "tk_codex01"
        A(
            "task.added",
            lead,
            {
                "title": "登录模块开发",
                "card": "# 任务\n开发用户登录系统并补全测试\n---",
                "owner": "em_codex",
                "dept": "dev",
            },
            self.tk_codex,
            self.t0 + 10,
        )

        # 第一轮
        A("turn.started", "station:em_codex", {"task": self.tk_codex, "em": "em_codex"}, "tn_c1", self.t0 + 12)
        A("task.progress", "station:em_codex", {"step": 1, "of": 2, "text": "编写登录逻辑"}, self.tk_codex, self.t0 + 15)
        A(
            "turn.ended",
            "station:em_codex",
            {
                "em": "em_codex",
                "status": "completed",
                "evidence": {
                    "claim": "partial",
                    "changed_files": ["login.py"],
                    "verify": [{"cmd": "python3 -m unittest", "ok": True}],
                },
            },
            "tn_c1",
            self.t0 + 20,
        )

        # 第二轮前插话
        A(
            "control.requested",
            "user",
            {"action": "steer", "by_source": "page", "text": "请补充密码哈希校验"},
            "em_codex",
            self.t0 + 25,
        )

        # 第二轮
        A("turn.started", "station:em_codex", {"task": self.tk_codex, "em": "em_codex"}, "tn_c2", self.t0 + 30)
        A("task.reported", "station:em_codex", {"claim": "done", "summary": "结果：完成。登录与密码哈希均已通过测试。"}, self.tk_codex, self.t0 + 40)
        A(
            "turn.ended",
            "station:em_codex",
            {
                "em": "em_codex",
                "status": "completed",
                "evidence": {
                    "claim": "done",
                    "changed_files": ["login.py", "hash.py"],
                    "verify": [{"cmd": "python3 -m unittest", "ok": True}],
                },
            },
            "tn_c2",
            self.t0 + 45,
        )
        A("task.landed", "station:em_codex", {"files": ["login.py", "hash.py"]}, self.tk_codex, self.t0 + 46)

        # 直播文件：Codex
        codex_live = self.tmp / "v2" / "live" / "em_codex"
        codex_live.mkdir(parents=True, exist_ok=True)
        codex_rows = [
            {"n": 1, "kind": "tool", "text": "cat login.py", "tool_kind": "cmd", "tool": "cat", "ts": self.t0 + 13},
            {"n": 2, "kind": "tool", "text": "python3 -m unittest", "tool_kind": "cmd", "tool": "test", "ts": self.t0 + 35},
            {"n": 3, "kind": "text", "text": "【结果】登录与密码哈希均已通过测试。", "ts": self.t0 + 38},
        ]
        (codex_live / "1.jsonl").write_text(
            "\n".join(json.dumps(r, ensure_ascii=False) for r in codex_rows) + "\n",
            encoding="utf-8",
        )

        # 2. agy 任务（正在做，直播文件里有 3 条 tool 行）
        self.tk_agy = "tk_agy01"
        A(
            "task.added",
            lead,
            {
                "title": "页面美化与重构",
                "card": "# 任务\n美化界面布局\n---",
                "owner": "em_agy",
                "dept": "dev",
            },
            self.tk_agy,
            self.t0 + 50,
        )
        A("turn.started", "station:em_agy", {"task": self.tk_agy, "em": "em_agy"}, "tn_a1", self.t0 + 55)

        # 直播文件：agy 3 条 tool 行
        agy_live = self.tmp / "v2" / "live" / "em_agy"
        agy_live.mkdir(parents=True, exist_ok=True)
        agy_rows = [
            {"n": 1, "kind": "tool", "text": "read app.css", "tool_kind": "read", "tool": "read_file", "ts": self.t0 + 56},
            {"n": 2, "kind": "tool", "text": "write app.css", "tool_kind": "write", "tool": "write_file", "ts": self.t0 + 58},
            {"n": 3, "kind": "tool", "text": "python3 build.py", "tool_kind": "cmd", "tool": "run_command", "ts": self.t0 + 60},
            {"n": 4, "kind": "thinking", "text": "思考样式细节", "ts": self.t0 + 61},
        ]
        (agy_live / "1.jsonl").write_text(
            "\n".join(json.dumps(r, ensure_ascii=False) for r in agy_rows) + "\n",
            encoding="utf-8",
        )

        # 3. 旧系统 bridge.run
        self.legacy_run_dir = "/tmp/test/.xay/runs/20261030-legacy-task"
        A(
            "bridge.run",
            "bridge",
            {
                "run_dir": self.legacy_run_dir,
                "agent": "dsh",
                "title": "旧系统任务归档",
                "status": "done",
                "started": self.t0 - 100,
                "ended": self.t0 - 50,
                "result_head": "结果：完成。迁移归档完成。",
                "dept": "dev",
            },
            "em_codex",
            self.t0 - 100,
        )

    def test_conversation_data_structure(self):
        data = oldboard_conv.conversation(self.conn, self.conv, now=self.t0 + 70)
        self.assertIsNotNone(data)
        conv = data["conversation"]
        threads = data["threads"]

        # 断言 threads 数
        self.assertEqual(len(threads), 3)

        # 找到各个 thread
        t_codex = next((t for t in threads if t["id"] == self.tk_codex), None)
        t_agy = next((t for t in threads if t["id"] == self.tk_agy), None)
        t_legacy = next((t for t in threads if "legacy" in t["id"]), None)

        self.assertIsNotNone(t_codex)
        self.assertIsNotNone(t_agy)
        self.assertIsNotNone(t_legacy)

        # 1. 断言 Codex 任务（两轮，第二轮前有插话）
        self.assertEqual(t_codex["agent"], "codex")
        self.assertEqual(t_codex["status"], "done")
        self.assertEqual(t_codex["capabilities"], {"steer": "live", "interrupt": "soft"})
        self.assertIsNone(t_codex["active_run"])

        # 消息顺序和种类
        msgs_codex = t_codex["messages"]
        self.assertEqual(len(msgs_codex), 4)
        self.assertEqual(msgs_codex[0]["kind"], "lead")
        self.assertEqual(msgs_codex[0]["source"], "task")
        self.assertIn("开发用户登录系统", msgs_codex[0]["text"])

        self.assertEqual(msgs_codex[1]["kind"], "ai")
        self.assertEqual(msgs_codex[1]["turn"], 1)
        self.assertEqual(msgs_codex[1]["status"], "completed")
        self.assertFalse(msgs_codex[1]["streaming"])

        self.assertEqual(msgs_codex[2]["kind"], "lead")
        self.assertEqual(msgs_codex[2]["source"], "steer")
        self.assertEqual(msgs_codex[2]["text"], "请补充密码哈希校验")

        self.assertEqual(msgs_codex[3]["kind"], "ai")
        self.assertEqual(msgs_codex[3]["turn"], 2)
        self.assertEqual(msgs_codex[3]["status"], "completed")
        self.assertFalse(msgs_codex[3]["streaming"])

        # 做完的有 final 和 files
        self.assertIsNotNone(msgs_codex[3]["final"])
        self.assertIn("完成", msgs_codex[3]["final"])
        self.assertTrue(len(msgs_codex[3]["files"]) > 0)
        self.assertIn("login.py", msgs_codex[3]["files"])
        self.assertIn("hash.py", msgs_codex[3]["files"])

        # 2. 断言 agy 任务（正在做，直播文件里有 3 条 tool 行）
        self.assertEqual(t_agy["agent"], "agy")
        self.assertEqual(t_agy["status"], "running")
        self.assertEqual(t_agy["capabilities"], {"steer": "none", "interrupt": "kill"})
        self.assertEqual(t_agy["active_run"], "tn_a1")

        msgs_agy = t_agy["messages"]
        self.assertEqual(len(msgs_agy), 2)
        self.assertEqual(msgs_agy[0]["kind"], "lead")
        self.assertEqual(msgs_agy[0]["source"], "task")

        ai_agy = msgs_agy[1]
        self.assertEqual(ai_agy["kind"], "ai")
        # 正在做那轮 streaming=true 且最后一步 active
        self.assertTrue(ai_agy["streaming"])
        self.assertEqual(ai_agy["status"], "running")
        self.assertIsNone(ai_agy["final"])
        self.assertEqual(len(ai_agy["steps"]), 3)
        self.assertEqual(ai_agy["steps"][0]["state"], "done")
        self.assertEqual(ai_agy["steps"][1]["state"], "done")
        self.assertEqual(ai_agy["steps"][2]["state"], "active")  # 正在做的最后一条 active

        # 3. 断言旧系统 bridge.run
        msgs_leg = t_legacy["messages"]
        self.assertEqual(len(msgs_leg), 2)
        self.assertEqual(msgs_leg[0]["kind"], "lead")
        self.assertEqual(msgs_leg[0]["source"], "task")
        self.assertEqual(msgs_leg[1]["kind"], "ai")
        self.assertFalse(msgs_leg[1]["streaming"])
        self.assertEqual(msgs_leg[1]["steps"], [])
        self.assertEqual(msgs_leg[1]["final"], "结果：完成。迁移归档完成。")

    def test_control_steer(self):
        # control 的 steer 写出了 control.requested{action:steer}
        res = oldboard_conv.control(
            self.conn,
            {"run": self.tk_agy, "op": "steer", "text": "把按钮改成圆角"},
            {},
        )
        self.assertTrue(res.get("ok"))
        row = self.conn.execute(
            "SELECT type, data, subject FROM events WHERE seq=?", (res["seq"],)
        ).fetchone()
        self.assertEqual(row["type"], "control.requested")
        self.assertEqual(row["subject"], "em_agy")
        data = json.loads(row["data"])
        self.assertEqual(data["action"], "steer")
        self.assertEqual(data["text"], "把按钮改成圆角")

    def test_control_stop(self):
        res = oldboard_conv.control(
            self.conn,
            {"run": self.tk_agy, "op": "stop"},
            {},
        )
        self.assertTrue(res.get("ok"))
        row = self.conn.execute(
            "SELECT type, data, subject FROM events WHERE seq=?", (res["seq"],)
        ).fetchone()
        self.assertEqual(row["type"], "control.requested")
        self.assertEqual(row["subject"], "em_agy")
        data = json.loads(row["data"])
        self.assertEqual(data["action"], "stop")

    def test_control_invalid_op_or_task(self):
        with self.assertRaises(HttpError):
            oldboard_conv.control(self.conn, {"run": "non_existent", "op": "steer", "text": "abc"}, {})
        with self.assertRaises(HttpError):
            oldboard_conv.control(self.conn, {"run": self.tk_agy, "op": "unsupported_op"}, {})

    def test_decide(self):
        # decide 解决了决定
        res = oldboard_conv.decide(
            self.conn,
            {"id": "dc_test01", "option": "yes"},
            {},
        )
        self.assertTrue(res.get("ok"))
        row = self.conn.execute(
            "SELECT type, data, subject FROM events WHERE seq=?", (res["seq"],)
        ).fetchone()
        self.assertEqual(row["type"], "decision.resolved")
        self.assertEqual(row["subject"], "dc_test01")
        data = json.loads(row["data"])
        self.assertEqual(data["option"], "yes")

    def test_conversation_not_found(self):
        self.assertIsNone(oldboard_conv.conversation(self.conn, "non_existent_conv"))
        self.assertIsNone(oldboard_conv.conversation(self.conn, None))

    def test_control_resume(self):
        res = oldboard_conv.control(
            self.conn,
            {"run": self.tk_codex, "op": "resume", "text": "续接新指示"},
            {},
        )
        self.assertTrue(res.get("ok"))
        row = self.conn.execute(
            "SELECT type, data, subject FROM events WHERE seq=?", (res["seq"],)
        ).fetchone()
        self.assertEqual(row["type"], "control.requested")
        self.assertEqual(row["subject"], "em_codex")
        data = json.loads(row["data"])
        self.assertEqual(data["action"], "steer")
        self.assertEqual(data["text"], "续接新指示")

    def test_decide_invalid(self):
        with self.assertRaises(HttpError):
            oldboard_conv.decide(self.conn, {"id": "dc_test01", "option": "invalid_opt"}, {})
        with self.assertRaises(HttpError):
            oldboard_conv.decide(self.conn, {"id": "non_existent_dc", "option": "yes"}, {})

    def test_diff_non_existent_task(self):
        # diff 对不存在的任务不抛异常，返回空 diff 和 note
        res = oldboard_conv.diff(self.conn, "non_existent_task", "some/path.py")
        self.assertEqual(res["diff"], "")
        self.assertEqual(res.get("note"), "拿不到这件小任务的改动")

    def test_diff_unsafe_paths_rejected(self):
        for bad_path in ("../x", "/etc/passwd", "..\\win", "\\abs\\path", ""):
            res = oldboard_conv.diff(self.conn, self.tk_codex, bad_path)
            self.assertEqual(res["diff"], "")
            self.assertEqual(res.get("note"), "拿不到这件小任务的改动")

    def test_diff_two_landed_tasks_isolated(self):
        desk_dir = self.tmp / "v2" / "desks" / self.ws / "em_codex"
        desk_dir.mkdir(parents=True, exist_ok=True)

        def g(*args):
            return subprocess.run(
                ["git", "-c", "user.name=t", "-c", "user.email=t@t", "-c", "commit.gpgsign=false", *args],
                cwd=str(desk_dir),
                capture_output=True,
                text=True,
                check=True,
            ).stdout.strip()

        g("init", "-q", "-b", "main")
        (desk_dir / "base.txt").write_text("initial\n", encoding="utf-8")
        g("add", "base.txt")
        g("commit", "-m", "init commit")

        # 任务 1: 改 file1.py 并落地
        tk_1 = "tk_codex_first"
        self._append("task.added", f"lead:{self.conv}", {"title": "任务一", "owner": "em_codex"}, tk_1, self.t0 + 20)
        (desk_dir / "file1.py").write_text("print('task 1')\n", encoding="utf-8")
        g("add", "file1.py")
        g("commit", "-m", f"xay: 任务 {tk_1} 已落地")
        self._append("task.landed", "office", {}, tk_1, self.t0 + 21)

        # 任务 2: 改 file2.py 并落地
        tk_2 = "tk_codex_second"
        self._append("task.added", f"lead:{self.conv}", {"title": "任务二", "owner": "em_codex"}, tk_2, self.t0 + 30)
        (desk_dir / "file2.py").write_text("print('task 2')\n", encoding="utf-8")
        g("add", "file2.py")
        g("commit", "-m", f"xay: 任务 {tk_2} 已落地")
        self._append("task.landed", "office", {}, tk_2, self.t0 + 31)

        # 验证两件小任务的 diff 彼此隔离
        diff_1_f1 = oldboard_conv.diff(self.conn, tk_1, "file1.py")
        self.assertIn("+print('task 1')", diff_1_f1["diff"])
        self.assertNotIn("print('task 2')", diff_1_f1["diff"])

        diff_1_f2 = oldboard_conv.diff(self.conn, tk_1, "file2.py")
        self.assertEqual(diff_1_f2["diff"], "")

        diff_2_f2 = oldboard_conv.diff(self.conn, tk_2, "file2.py")
        self.assertIn("+print('task 2')", diff_2_f2["diff"])
        self.assertNotIn("print('task 1')", diff_2_f2["diff"])

        diff_2_f1 = oldboard_conv.diff(self.conn, tk_2, "file1.py")
        self.assertEqual(diff_2_f1["diff"], "")

    def test_diff_unlanded_latest_and_non_latest_fallback(self):
        desk_dir = self.tmp / "v2" / "desks" / self.ws / "em_codex"
        desk_dir.mkdir(parents=True, exist_ok=True)

        def g(*args):
            return subprocess.run(
                ["git", "-c", "user.name=t", "-c", "user.email=t@t", "-c", "commit.gpgsign=false", *args],
                cwd=str(desk_dir),
                capture_output=True,
                text=True,
                check=True,
            ).stdout.strip()

        g("init", "-q", "-b", "main")
        (desk_dir / "base.txt").write_text("initial\n", encoding="utf-8")
        g("add", "base.txt")
        c0 = g("commit", "-m", "init commit")

        # 写入 desk.json
        (desk_dir / "desk.json").write_text(
            json.dumps({"v": 1, "ws": self.ws, "em": "em_codex", "branch": "main", "base": c0, "landed_upto": c0}),
            encoding="utf-8",
        )

        tk_old_unlanded = "tk_old_unlanded"
        self._append("task.added", f"lead:{self.conv}", {"title": "旧未落地任务", "owner": "em_codex"}, tk_old_unlanded, self.t0 + 40)

        tk_new_unlanded = "tk_new_unlanded"
        self._append("task.added", f"lead:{self.conv}", {"title": "最新未落地任务", "owner": "em_codex"}, tk_new_unlanded, self.t0 + 50)

        # 在工作区新建未跟踪文件 new_file.py
        (desk_dir / "new_file.py").write_text("new uncommitted code\n", encoding="utf-8")

        # 最新未落地任务能拿到工作区 diff
        diff_latest = oldboard_conv.diff(self.conn, tk_new_unlanded, "new_file.py")
        self.assertIn("+new uncommitted code", diff_latest["diff"])

        # 已被 git 管着、又没改的文件：没有改动，不能整份显示成新文件
        self.assertEqual(oldboard_conv.diff(self.conn, tk_new_unlanded, "base.txt")["diff"], "")

        # 已被 git 管着、改了的文件：只显示改的那几行
        (desk_dir / "base.txt").write_text("initial\nchanged\n", encoding="utf-8")
        changed = oldboard_conv.diff(self.conn, tk_new_unlanded, "base.txt")["diff"]
        self.assertIn("+changed", changed)
        self.assertNotIn("+initial", changed)

        # 旧的未落地任务拿不到改动，返回 note
        diff_old = oldboard_conv.diff(self.conn, tk_old_unlanded, "new_file.py")
        self.assertEqual(diff_old["diff"], "")
        self.assertEqual(diff_old.get("note"), "拿不到这件小任务的改动")

    def test_diff_root_commit_marker(self):
        desk_dir = self.tmp / "v2" / "desks" / self.ws / "em_agy"
        desk_dir.mkdir(parents=True, exist_ok=True)

        def g(*args):
            return subprocess.run(
                ["git", "-c", "user.name=t", "-c", "user.email=t@t", "-c", "commit.gpgsign=false", *args],
                cwd=str(desk_dir),
                capture_output=True,
                text=True,
                check=True,
            ).stdout.strip()

        g("init", "-q", "-b", "main")
        tk_root = "tk_root_task"
        self._append("task.added", f"lead:{self.conv}", {"title": "根任务", "owner": "em_agy"}, tk_root, self.t0 + 60)
        (desk_dir / "root.txt").write_text("root file\n", encoding="utf-8")
        g("add", "root.txt")
        g("commit", "-m", f"xay: 任务 {tk_root} 已落地")

        res = oldboard_conv.diff(self.conn, tk_root, "root.txt")
        self.assertIn("+root file", res["diff"])


if __name__ == "__main__":
    unittest.main()
