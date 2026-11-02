import copy
import os
import shutil
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

from tests.test_employee_panel_v3 import AGY, CODEX, CV, DSH, LEAD, NOW, T0, WS, LedgerCase
from tests.test_station import CLEAR_ENV, GIT_ENV, Factory, StationCase
from xay.core import db as core_db
from xay.core import events as core_events
from xay.core import snapshot as core_snapshot
from xay.office import employee
from xay.station import markers
from xay.station.fakes import FakeAdapter
from xay.station.station import Station, v2_root


class SelfStepsTests(LedgerCase):
    def A(self, typ, by, data, subject=None, ts=None, conv=None):
        return core_events.append(self.conn, typ, by, data, ws=WS, conv=conv or CV, subject=subject, ts=ts)

    def test_first_turn_has_manual_second_turn_not(self):
        """第一轮输入带手册、第二轮不带；未写计划第二轮开头提醒。"""
        A = self.A
        A("dept.opened", LEAD, {"name": "开发部", "mission": "干活", "owns": []}, "dev", T0)
        A("employee.joined", LEAD, {"dept": "dev"}, CODEX, T0)
        A("task.added", LEAD, {"key": "tk_a", "title": "测试活", "owner": CODEX, "dept": "dev",
                               "deps": [], "writes": ["a.py"], "steps": [], "mode": "write", "reason": "r"},
          "tk_a", T0 + 1)

        station = Station(CODEX, home=self.tmp / "v2")
        station.conn = self.conn
        station.ws = WS
        station._refresh(force=True)

        t = station.state["tasks"]["tk_a"]
        origin = str(self.tmp)

        # 第 1 轮
        text1, _ = station._turn_input(t, "tk_a", "first", False, origin)
        self.assertIn("员工手册（给所有 AI 执行者）", text1)
        self.assertIn("开工第一件事", text1)
        # 第 1 轮还没有结束/未做完，第二轮开头有提醒
        self.assertNotIn("先用【计划】把你的步骤列出来", text1)

        # 第 2 轮（还没写过计划）
        text2, _ = station._turn_input(t, "tk_a", "continue", False, origin)
        self.assertNotIn("员工手册（给所有 AI 执行者）", text2)
        self.assertIn("先用【计划】把你的步骤列出来", text2)

        # 若已写入了 task.planned，则第二轮不再提醒
        self.A("task.planned", "station:" + CODEX, {"steps": [{"n": 1, "text": "读代码"}]}, "tk_a", T0 + 2)
        station._refresh(force=True)
        station._task_turn_counts["tk_b"] = 1  # 模拟 tk_b 已经进入第 2 轮
        A("task.added", LEAD, {"key": "tk_b", "title": "已计划活", "owner": CODEX, "dept": "dev",
                               "deps": [], "writes": [], "steps": [], "mode": "write", "reason": "r"},
          "tk_b", T0 + 3)
        self.A("task.planned", "station:" + CODEX, {"steps": [{"n": 1, "text": "已拆好步骤"}]}, "tk_b", T0 + 4)
        station._refresh(force=True)
        t_b = station.state["tasks"]["tk_b"]
        text3, _ = station._turn_input(t_b, "tk_b", "continue", False, origin)
        self.assertNotIn("员工手册（给所有 AI 执行者）", text3)
        self.assertNotIn("先用【计划】把你的步骤列出来", text3)

    def test_plan_marker_scan_and_parse(self):
        """【计划】解析成 task.planned 结构。"""
        buf = (
            "我明白了，先拆解任务。\n\n"
            "【计划】\n"
            "1. 查阅项目结构与相关代码\n"
            "2. 编写核心功能实现\n"
            "3. 补充完整单元测试并验证\n\n"
            "【进度 1/3】查阅项目结构与相关代码\n"
        )
        scanned = markers.scan(buf, final=True)
        kinds = [x[1] for x in scanned]
        self.assertIn("plan", kinds)
        self.assertIn("progress", kinds)

        plan_item = next(x for x in scanned if x[1] == "plan")
        steps = plan_item[2]["steps"]
        self.assertEqual(len(steps), 3)
        self.assertEqual(steps[0], {"n": 1, "text": "查阅项目结构与相关代码"})
        self.assertEqual(steps[1], {"n": 2, "text": "编写核心功能实现"})
        self.assertEqual(steps[2], {"n": 3, "text": "补充完整单元测试并验证"})

        # 测试 extract_plan
        extracted = markers.extract_plan(buf)
        self.assertEqual(extracted, steps)

    def test_replan_overwrites_previous_plan_and_records_round(self):
        """重新写【计划】覆盖旧计划，并更新 planned_round。"""
        A = self.A
        A("dept.opened", LEAD, {"name": "开发部", "mission": "干活", "owns": []}, "dev", T0)
        A("employee.joined", LEAD, {"dept": "dev"}, CODEX, T0)
        A("task.added", LEAD, {"key": "tk_replan", "title": "重计划活", "owner": CODEX, "dept": "dev",
                               "deps": [], "writes": [], "steps": [], "mode": "write", "reason": "r"},
          "tk_replan", T0 + 1)

        # 第一次计划
        steps1 = [{"n": 1, "text": "最初第1步"}, {"n": 2, "text": "最初第2步"}]
        A("task.planned", "station:" + CODEX, {"steps": steps1, "round": 1}, "tk_replan", T0 + 2)

        st = core_events.load_state(self.conn)
        task = st["tasks"]["tk_replan"]
        self.assertEqual(task["planned_steps"], steps1)
        self.assertEqual(task["planned_round"], 1)

        # 进度被记录
        A("task.progress", "station:" + CODEX, {"step": 1, "of": 2, "text": "最初第1步"}, "tk_replan", T0 + 3)
        st = core_events.load_state(self.conn)
        self.assertEqual(st["tasks"]["tk_replan"]["progress"]["of"], 2)

        # 第二次重写计划（覆盖）
        steps2 = [{"n": 1, "text": "全新第1步"}, {"n": 2, "text": "全新第2步"}, {"n": 3, "text": "全新第3步"}]
        A("task.planned", "station:" + CODEX, {"steps": steps2, "round": 2}, "tk_replan", T0 + 4)

        st = core_events.load_state(self.conn)
        task = st["tasks"]["tk_replan"]
        self.assertEqual(task["planned_steps"], steps2)
        self.assertEqual(task["planned_round"], 2)
        # 进度 of 自动更新为最新计划步数 3
        self.assertEqual(task["progress"]["of"], 3)

    def test_tree_steps_uses_ai_plan_and_matches_progress(self):
        """树里的 steps 用 AI 的计划、进度对上第几步；snapshot tasks[] 多 planned。"""
        A = self.A
        A("dept.opened", LEAD, {"name": "前端部", "mission": "页面", "owns": []}, "fe", T0)
        A("employee.joined", LEAD, {"dept": "fe"}, CODEX, T0)
        # 任务卡虽然有 steps，但 AI 拆了 planned_steps 时优先用 planned_steps
        A("task.added", LEAD, {"key": "tk_demo", "title": "实现组件", "owner": CODEX, "dept": "fe",
                               "deps": [], "writes": [], "steps": ["卡片步1", "卡片步2"], "mode": "write", "reason": "r"},
          "tk_demo", T0 + 10)
        A("turn.started", "station:" + CODEX, {"task": "tk_demo", "em": CODEX, "kind": "first"}, "tn_1", T0 + 11)

        ai_steps = [{"n": 1, "text": "分析接口"}, {"n": 2, "text": "编写组件"}, {"n": 3, "text": "对齐样式"}]
        A("task.planned", "station:" + CODEX, {"steps": ai_steps, "round": 1}, "tk_demo", T0 + 12)

        # 报进度：step 2，of 即使写成 10 也以计划总步数 3 为准
        A("task.progress", "station:" + CODEX, {"step": 2, "of": 10, "text": "编写组件中", "source": "marker"}, "tk_demo", T0 + 13)

        body = employee.build(self.conn, CODEX, conv=CV, live_root=self.tmp / "v2" / "live", now=NOW, quota_home=self.tmp / "v2")
        tree = body["tree"]
        fe_group = next(g for g in tree["groups"] if g["name"] == "前端部")
        item = fe_group["items"][0]

        # 检查总步数与当前步
        self.assertEqual(item["step"], 2)
        self.assertEqual(item["of"], 3)
        # 步骤列表优先使用 planned_steps，状态按进度算
        self.assertEqual([(s["n"], s["text"], s["state"]) for s in item["steps"]],
                         [(1, "分析接口", "done"), (2, "编写组件", "cur"), (3, "对齐样式", "todo")])

        # 检查 detail 里的 planned 字段
        self.assertEqual(item["detail"]["planned"], ["分析接口", "编写组件", "对齐样式"])

        # 检查快照 tasks[] 中的 planned
        snap = core_snapshot.build(self.conn, conv=CV, now=NOW)
        snap_task = next(t for t in snap["tasks"] if t["id"] == "tk_demo")
        self.assertEqual(snap_task["planned"], ["分析接口", "编写组件", "对齐样式"])


class StationWorkflowTests(StationCase):
    def test_station_turn_executes_plan_and_m_inconsistency_corrected(self):
        """工位运行过程中，回复中的【计划】和【进度】解析并正确覆盖 m。"""
        reply = (
            "收到任务，开工！\n\n"
            "【计划】\n"
            "1. 调研方案\n"
            "2. 动手改文件\n"
            "3. 验证命令\n\n"
            "【进度 1/9】调研方案\n"
            "【给老板看】做好了第一阶段。\n"
            "【结果】完成：测试成功"
        )
        factory = Factory([[reply]])
        tk = self.add_task("做个示例")

        # 跑一轮
        self.run_station(factory)

        st = core_events.load_state(self.conn)
        t = st["tasks"][tk]
        self.assertEqual(len(t.get("planned_steps") or []), 3)
        self.assertEqual(t["planned_steps"][0]["text"], "调研方案")
        # 进度里的 of 9 被修正为 3
        self.assertEqual(t["progress"]["of"], 3)
        self.assertEqual(t["progress"]["step"], 1)
