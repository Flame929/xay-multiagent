"""监工审查流程测试（tk_62c2f63f / protocol §1.24）。

覆盖：
1. ai 审查合格 → passed
2. 不合格一次 → 发回重做、round 变 2、再审合格
3. 不合格两次 → 负责人决定
4. lead 用 xay2 check 结论（关掉 review 决定）
5. 审查活不再被审
6. verdict 只在审查活里有效
"""
import json
import unittest
from pathlib import Path
from unittest import mock

from xay.core import append, load_state, snapshot
from xay.core.ids import new_id
from xay.office import rules as rules_mod
from xay.station import station as st

from tests.test_rules import RulesCase


class CheckFlowTests(RulesCase):
    def setUp(self):
        super().setUp()
        self.em1 = self.hire("codex")
        self.em2 = self.hire("codex")

    def add_task(self, title, owner=None, part=None, writes=(), mode="write", review_of=None):
        tk = new_id("tk_")
        data = {"title": title, "deps": [], "writes": list(writes), "mode": mode,
                "reason": "测试", "card": title}
        if owner: data["owner"] = owner
        if part: data["part"] = part
        if review_of: data["review_of"] = review_of
        append(self.conn, "task.added", self.lead, data, ws=self.ws, conv=self.conv, subject=tk)
        return tk

    def _add_part(self, title, check, key=None):
        pid = new_id("pt_")
        append(self.conn, "part.added", self.lead,
               {"title": title, "after": [], "check": check, "key": key},
               ws=self.ws, conv=self.conv, subject=pid)
        return pid

    def test_ai_check_pass(self):
        """ai 审查合格 → passed"""
        pid = self._add_part("做轮播", {"kind": "ai", "reviewer": "codex", "text": "注意内存泄漏"})
        tk = self.add_task("实现轮播图", owner=self.em1, part=pid, writes=["web/banner.js"])

        # 任务合并
        append(self.conn, "task.landed", "office", {"files": ["web/banner.js"]},
               ws=self.ws, conv=self.conv, subject=tk)

        s = load_state(self.conn)
        self.assertEqual(s["tasks"][tk]["check"]["state"], "waiting")
        self.assertEqual(s["tasks"][tk]["check"]["round"], 1)

        # 管家一拍：开出审查活
        sched = self.sched()
        sched.tick()

        s = load_state(self.conn)
        rev_tasks = [t for t in s["tasks"].values() if t.get("review_of") == tk]
        self.assertEqual(len(rev_tasks), 1)
        rtk = rev_tasks[0]
        self.assertEqual(rtk["mode"], "readonly")
        self.assertIn("审查重点：注意内存泄漏", rtk["card"])
        self.assertIn("web/banner.js", rtk["card"])
        self.assertEqual(rtk["part"], pid)

        # 审查员交结论（合格）
        append(self.conn, "check.result", "station:" + self.em2,
               {"task": tk, "ok": True, "text": "代码规范，通过"},
               ws=self.ws, conv=self.conv, subject=tk)

        s = load_state(self.conn)
        self.assertEqual(s["tasks"][tk]["check"]["state"], "passed")
        self.assertEqual(s["tasks"][tk]["check"]["text"], "代码规范，通过")

        snap = snapshot.build(self.conn, ws=self.ws, conv=self.conv)
        snap_task = next(t for t in snap["tasks"] if t["id"] == tk)
        self.assertEqual(snap_task["check"]["state"], "passed")

    def test_check_reject_once_and_rework_round_2(self):
        """不合格一次 → 发回重做、round 变 2、再审合格"""
        pid = self._add_part("做轮播", {"kind": "ai", "reviewer": "codex"})
        tk = self.add_task("实现轮播图", owner=self.em1, part=pid)

        append(self.conn, "task.landed", "office", {}, ws=self.ws, conv=self.conv, subject=tk)

        # 审查不合格（round 1）
        append(self.conn, "check.result", "station:" + self.em2,
               {"task": tk, "ok": False, "text": "缺少单元测试"},
               ws=self.ws, conv=self.conv, subject=tk)

        s = load_state(self.conn)
        self.assertEqual(s["tasks"][tk]["check"]["state"], "failed")

        # 管家一拍：写信告知并 task.queued{retry: "check"}
        self.sched().tick()

        s = load_state(self.conn)
        self.assertEqual(s["tasks"][tk]["status"], "queued")
        self.assertEqual(s["tasks"][tk].get("retries", {}).get("check"), 1)

        letters = [l for l in s["letters"].values() if l.get("to") == self.em1]
        self.assertTrue(any("验收未通过" in l["subject"] for l in letters))
        self.assertTrue(any("缺少单元测试" in l["body"] for l in letters))

        # 员工修改后再次合并
        append(self.conn, "task.landed", "office", {}, ws=self.ws, conv=self.conv, subject=tk)

        s = load_state(self.conn)
        self.assertEqual(s["tasks"][tk]["check"]["state"], "waiting")
        self.assertEqual(s["tasks"][tk]["check"]["round"], 2)

        # 第二轮审查通过
        append(self.conn, "check.result", "station:" + self.em2,
               {"task": tk, "ok": True, "text": "补全了测试，通过"},
               ws=self.ws, conv=self.conv, subject=tk)

        s = load_state(self.conn)
        self.assertEqual(s["tasks"][tk]["check"]["state"], "passed")
        self.assertEqual(s["tasks"][tk]["check"]["round"], 2)

    def test_check_reject_twice_opens_decision(self):
        """不合格两次 → 负责人决定"""
        pid = self._add_part("做轮播", {"kind": "ai", "reviewer": "codex"})
        tk = self.add_task("实现轮播图", owner=self.em1, part=pid)

        # 第 1 轮 landed 并打回
        append(self.conn, "task.landed", "office", {}, ws=self.ws, conv=self.conv, subject=tk)
        append(self.conn, "check.result", "station:" + self.em2,
               {"task": tk, "ok": False, "text": "第一次不合格"}, ws=self.ws, conv=self.conv, subject=tk)

        sched = self.sched()
        sched.tick()

        # 第 2 轮 landed 并再次打回
        append(self.conn, "task.landed", "office", {}, ws=self.ws, conv=self.conv, subject=tk)
        append(self.conn, "check.result", "station:" + self.em2,
               {"task": tk, "ok": False, "text": "第二次仍不合格"}, ws=self.ws, conv=self.conv, subject=tk)

        s = load_state(self.conn)
        self.assertEqual(s["tasks"][tk]["check"]["round"], 2)
        self.assertEqual(s["tasks"][tk]["check"]["state"], "failed")

        # 管家一拍：开出乙类决定交负责人
        sched.tick()

        s = load_state(self.conn)
        decs = [d for d in s["decisions"].values() if d.get("kind") == "check_failed" and d.get("task") == tk]
        self.assertEqual(len(decs), 1)
        opt_ids = [o["id"] for o in decs[0]["options"]]
        self.assertIn("retry", opt_ids)
        self.assertIn("other", opt_ids)
        self.assertIn("lead", opt_ids)

    def test_lead_check_cli(self):
        """lead 验收方式，开 review 决定，负责人用 xay2 check 结论关闭决定"""
        from xay import cli
        pid = self._add_part("核心逻辑", {"kind": "lead", "text": "看核心算法"})
        tk = self.add_task("算力调度算法", owner=self.em1, part=pid)

        append(self.conn, "task.landed", "office", {}, ws=self.ws, conv=self.conv, subject=tk)

        self.sched().tick()

        s = load_state(self.conn)
        decs = [d for d in s["decisions"].values() if d.get("kind") == "review" and d.get("task") == tk]
        self.assertEqual(len(decs), 1)

        # 负责人使用 cli check
        args = mock.Mock(task=tk, verdict="pass", text="亲自审过，没有问题")
        ctx = cli.Ctx(self.conn, self.conv, s)
        cli._check(ctx, args)

        s = load_state(self.conn)
        self.assertEqual(s["tasks"][tk]["check"]["state"], "passed")
        self.assertEqual(s["tasks"][tk]["check"]["by"], "lead:" + self.conv)

        # 对应的 review 决定应当被关闭（从 decisions 弹出）
        self.assertNotIn(decs[0]["id"], s["decisions"])

    def test_lead_review_answered_on_page_records_result(self):
        """在页面上点"不合格"（或 xay2 answer）回答 review 决定：结论照样记下，按不合格打回一次"""
        pid = self._add_part("核心逻辑", {"kind": "lead"})
        tk = self.add_task("算力调度算法", owner=self.em1, part=pid)
        append(self.conn, "task.landed", "office", {}, ws=self.ws, conv=self.conv, subject=tk)
        self.sched().tick()
        s = load_state(self.conn)
        dc = next(d["id"] for d in s["decisions"].values() if d.get("kind") == "review" and d.get("task") == tk)
        append(self.conn, "decision.resolved", "user", {"option": "fail", "text": "边界没处理"}, ws=self.ws, conv=self.conv, subject=dc)
        self.sched().tick()
        s = load_state(self.conn)
        self.assertEqual(s["tasks"][tk]["check"]["state"], "failed")
        self.assertEqual(s["tasks"][tk]["check"]["text"], "边界没处理")

    def test_review_task_itself_not_reviewed(self):
        """审查活本身做完不需要再审"""
        pid = self._add_part("做轮播", {"kind": "ai", "reviewer": "codex"})
        orig_tk = self.add_task("原任务", owner=self.em1, part=pid)

        # 创建一个审查活
        rev_tk = self.add_task("审查「原任务」", owner=self.em2, part=pid, mode="readonly", review_of=orig_tk)
        append(self.conn, "task.landed", "office", {}, ws=self.ws, conv=self.conv, subject=rev_tk)

        s = load_state(self.conn)
        self.assertEqual(s["tasks"][rev_tk]["check"]["state"], "none")

        self.sched().tick()
        s = load_state(self.conn)
        # 不会为 rev_tk 再建任何审查活
        self.assertFalse(any(t.get("review_of") == rev_tk for t in s["tasks"].values()))

    def test_verdict_only_valid_for_review_task(self):
        """verdict 只在审查活里有效"""
        normal_tk = self.add_task("普通任务", owner=self.em1)
        rev_tk = self.add_task("审查活", owner=self.em2, review_of=normal_tk)

        # 模拟普通任务发 verdict -> 拒绝
        fake_station = mock.Mock()
        fake_station.em = self.em1
        fake_station.turn = {"task": normal_tk, "outbox_n": 0, "conv": self.conv}
        fake_station.state = load_state(self.conn)
        fake_station._reject = mock.Mock()
        fake_station._refresh = lambda: None

        raw = json.dumps({"type": "verdict", "data": {"ok": True, "text": "通过"}}).encode("utf-8")
        st.Station._outbox_line(fake_station, 1, raw)
        fake_station._reject.assert_called_with("当前任务不是审查活，不能交 verdict")

        # 模拟审查活发 verdict -> 成功写入 check.result
        station2 = mock.Mock()
        station2.em = self.em2
        station2.by = "station:" + self.em2
        station2.conn = self.conn
        station2.ws = self.ws
        station2.turn = {"task": rev_tk, "outbox_n": 0, "conv": self.conv}
        station2.state = load_state(self.conn)
        station2._reject = mock.Mock()
        station2._refresh = lambda: None
        station2._conv = lambda: self.conv
        station2._w = lambda *args, **kw: st.Station._w(station2, *args, **kw)

        st.Station._outbox_line(station2, 2, raw)
        station2._reject.assert_not_called()

        s = load_state(self.conn)
        self.assertEqual(s["tasks"][normal_tk]["check"]["state"], "passed")
        self.assertEqual(s["tasks"][normal_tk]["check"]["by"], "station:" + self.em2)


if __name__ == "__main__":
    unittest.main()
