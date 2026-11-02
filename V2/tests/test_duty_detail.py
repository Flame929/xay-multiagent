"""员工在岗时间、活的详情数据、没小任务的对话不按部门分的测试。"""
import time
import unittest
from pathlib import Path

from tests.test_employee_panel_v3 import AGY, CODEX, CV, DSH, LEAD, NOW, T0, WS, LedgerCase
from xay.core import events as core_events
from xay.core import snapshot as core_snapshot
from xay.office import budget as bud
from xay.office import duty as office_duty
from xay.office import employee


class DutyDetailTests(LedgerCase):
    def A(self, typ, by, data, subject=None, ts=None, conv=None):
        return core_events.append(self.conn, typ, by, data, ws=WS, conv=conv or CV, subject=subject, ts=ts)

    def test_duty_calculation_online_offline_cross_midnight_and_working(self):
        # 设定今天 0 点为基准
        now = NOW
        t0 = bud.day_start(now)
        # 1. 昨天 23:00 上线，今天 01:00 下线（跨 0 点）
        y_online = t0 - 3600
        t_offline1 = t0 + 3600
        self.A("employee.online", "station:" + CODEX, {}, CODEX, y_online)
        self.A("employee.offline", "station:" + CODEX, {"reason": "idle"}, CODEX, t_offline1)

        # 此时查看 duty：下线状态，online_since 为 None；今天在岗时间为 00:00~01:00（3600s）
        d1 = office_duty.calc_duty(self.conn, core_events.load_state(self.conn), CODEX, now=t_offline1 + 100)
        self.assertIsNone(d1["online_since"])
        self.assertEqual(d1["today_s"], 3600.0)

        # 2. 今天 02:00 再次上线，当前时间设定为 02:30（还在岗）
        t_online2 = t0 + 7200
        t_cur = t0 + 9000  # 02:30
        self.A("employee.online", "station:" + CODEX, {}, CODEX, t_online2)

        # 3. 轮次数据：
        # - 昨天 23:30 ~ 今天 00:30（3600s，其中今天 1800s）普通干活轮 tn_prev
        self.A("turn.started", "station:" + CODEX, {"task": "tk_t1", "em": CODEX, "kind": "first"}, "tn_prev", t0 - 1800)
        self.A("turn.ended", "station:" + CODEX, {"em": CODEX, "status": "completed", "evidence": {}}, "tn_prev", t0 + 1800)

        # - 整理记忆的轮（02:05 ~ 02:15，600s）：kind='compact'，不计入干活时长
        self.A("turn.started", "station:" + CODEX, {"task": "tk_t1", "em": CODEX, "kind": "compact"}, "tn_memo", t0 + 7500)
        self.A("turn.ended", "station:" + CODEX, {"em": CODEX, "status": "completed", "evidence": {}}, "tn_memo", t0 + 8100)

        # - 当前正在做的一轮（02:15 开始，持续到 t_cur 02:30，900s）
        self.A("turn.started", "station:" + CODEX, {"task": "tk_t1", "em": CODEX, "kind": "first"}, "tn_cur", t0 + 8100)

        state = core_events.load_state(self.conn)
        d2 = office_duty.calc_duty(self.conn, state, CODEX, now=t_cur)

        # 验证 online_since 为第二次上线时间
        self.assertEqual(d2["online_since"], t_online2)
        # today_s: 第一段 3600s + 第二段 (t_cur - t_online2 = 1800s) = 5400s
        self.assertEqual(d2["today_s"], 5400.0)
        # today_work_s: 第一轮今天部分 1800s + 正在做这轮 900s = 2700s
        self.assertEqual(d2["today_work_s"], 2700.0)
        # total_work_s: 第一轮全量 3600s + 正在做这轮 900s = 4500s
        self.assertEqual(d2["total_work_s"], 4500.0)
        # hired_at 为入职时间
        self.assertEqual(d2["hired_at"], T0)

        # 验证快照 people 和 employee person 接口均带出相同的 duty
        snap = core_snapshot.build(self.conn, conv=CV, now=t_cur)
        self.assertIn("duty", snap["people"][CODEX])
        self.assertEqual(snap["people"][CODEX]["duty"]["today_s"], 5400.0)

        emp_data = employee.build(self.conn, CODEX, conv=CV, now=t_cur, quota_home=self.tmp / "v2")
        self.assertIn("duty", emp_data["person"])
        self.assertEqual(emp_data["person"]["duty"]["today_work_s"], 2700.0)

    def test_task_and_part_detail_fields(self):
        # 创建小任务 pt_1 和 pt_2（pt_2 after pt_1）
        self.A("part.added", "lead:" + CV,
               {"title": "小任务一", "after": [], "key": "p1", "note": "重要一期", "check": {"kind": "rules"}},
               "pt_1", T0 + 1)
        self.A("part.added", "lead:" + CV,
               {"title": "小任务二", "after": ["p1"], "key": "p2", "note": "重要二期", "check": {"kind": "lead"}},
               "pt_2", T0 + 2)

        # 任务卡含有 text 和 verify 命令
        card_obj = {"text": "请实现重要功能说明" * 200, "verify": ["python3 -m unittest", "node --check a.js"]}
        self.A("task.added", "lead:" + CV,
               {"title": "实现任务一", "owner": CODEX, "part": "pt_1", "card": card_obj, "reason": "安排给核心研发",
                "deps": [], "writes": [], "steps": [], "mode": "write"},
               "tk_1", T0 + 10)

        # 开工、报进度、汇报结果、合并
        t_start = T0 + 20
        t_end = T0 + 100
        self.A("turn.started", "station:" + CODEX, {"task": "tk_1", "em": CODEX, "kind": "first"}, "tn_1", t_start)
        self.A("task.reported", "station:" + CODEX, {"summary": "完成了核心功能代码编写与自测"}, "tk_1", t_end - 10)
        self.A("turn.ended", "station:" + CODEX, {"em": CODEX, "status": "completed",
                                                  "evidence": {"cli_status": "completed", "changed_files": ["a.py", "b.py"]}},
               "tn_1", t_end)
        self.A("task.landed", "station:" + CODEX, {"files": ["a.py", "b.py"], "add": 10, "del": 2}, "tk_1", t_end)

        # 检查 tree
        emp_res = employee.build(self.conn, CODEX, conv=CV, now=NOW, quota_home=self.tmp / "v2")
        tree = emp_res["tree"]

        # 1. 验证 parts[] 的 detail
        p1 = next(p for p in tree["parts"] if p["id"] == "pt_1")
        self.assertIn("detail", p1)
        p1_dt = p1["detail"]
        self.assertEqual(p1_dt["note"], "重要一期")
        self.assertEqual(p1_dt["check"]["kind"], "rules")
        self.assertEqual(p1_dt["after_titles"], [])
        self.assertEqual(p1_dt["started"], t_start)
        self.assertEqual(p1_dt["ended"], t_end)

        p2 = next(p for p in tree["parts"] if p["id"] == "pt_2")
        self.assertIn("detail", p2)
        p2_dt = p2["detail"]
        self.assertEqual(p2_dt["note"], "重要二期")
        self.assertEqual(p2_dt["check"]["kind"], "lead")
        self.assertEqual(p2_dt["after_titles"], ["小任务一"])
        self.assertIsNone(p2_dt["started"])
        self.assertIsNone(p2_dt["ended"])

        # 2. 验证 items 里的 detail
        t1_item = p1["items"][0]
        self.assertIn("detail", t1_item)
        dt = t1_item["detail"]
        # card 最多 1500 字，取 text
        self.assertTrue(len(dt["card"]) <= 1500)
        self.assertTrue(dt["card"].startswith("请实现重要功能说明"))
        self.assertEqual(dt["verify"], ["python3 -m unittest", "node --check a.js"])
        self.assertEqual(dt["summary"], "完成了核心功能代码编写与自测")
        self.assertEqual(dt["files"], ["a.py", "b.py"])
        self.assertEqual(dt["started"], t_start)
        self.assertEqual(dt["ended"], t_end)
        self.assertEqual(dt["work_s"], round(t_end - t_start, 1))
        self.assertEqual(dt["tries"], 1)
        self.assertEqual(dt["assigned_reason"], "安排给核心研发")
        self.assertIn("state", dt["check"])

        # 验证 groups 里的 item 同样带 detail
        g_item = tree["groups"][0]["items"][0]
        self.assertIn("detail", g_item)
        self.assertEqual(g_item["detail"]["summary"], "完成了核心功能代码编写与自测")

    def test_conv_without_parts_is_all_and_no_depts(self):
        # 没有 part 事件的旧对话：有部门（页面部、文档部），但不按部门拆解
        self.A("dept.opened", LEAD, {"name": "页面部", "mission": "做页面", "owns": [], "head": "rules"}, "fe", T0 + 1)
        self.A("dept.opened", LEAD, {"name": "文档部", "mission": "写文档", "owns": [], "head": "rules"}, "docs", T0 + 2)

        self.A("task.added", LEAD, {"title": "页面任务", "owner": CODEX, "dept": "fe",
                                    "deps": [], "writes": [], "steps": [], "mode": "write"},
               "tk_fe", T0 + 10)
        self.A("task.added", LEAD, {"title": "文档任务", "owner": AGY, "dept": "docs",
                                    "deps": [], "writes": [], "steps": [], "mode": "write"},
               "tk_doc", T0 + 11)

        # 1. 快照 parts 只有一个 id="all"，depts 为空列表
        snap = core_snapshot.build(self.conn, conv=CV, now=NOW)
        parts = snap["parts"]
        self.assertEqual(len(parts), 1)
        self.assertEqual(parts[0]["id"], "all")
        self.assertEqual(parts[0]["title"], "三家员工")
        self.assertEqual(parts[0]["depts"], [])
        self.assertEqual(parts[0]["count"]["all"], 2)
        self.assertIn("detail", parts[0])

        # 2. employee.task_tree 也是只有一个小任务，所有活在它的 items 里，depts 为空
        emp_res = employee.build(self.conn, CODEX, conv=CV, now=NOW, quota_home=self.tmp / "v2")
        tree_parts = emp_res["tree"]["parts"]
        self.assertEqual(len(tree_parts), 1)
        self.assertEqual(tree_parts[0]["id"], "all")
        self.assertEqual(tree_parts[0]["depts"], [])
        self.assertEqual(len(tree_parts[0]["items"]), 2)
        item_titles = [it["title"] for it in tree_parts[0]["items"]]
        self.assertIn("页面任务", item_titles)
        self.assertIn("文档任务", item_titles)


if __name__ == "__main__":
    unittest.main()
