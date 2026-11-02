import json
import os
import shutil
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

from tests.test_employee_panel_v3 import AGY, CODEX, CV, DSH, LEAD, NOW, T0, WS, LedgerCase
from xay.core import db as core_db
from xay.core import events as core_events
from xay.core import snapshot as core_snapshot
from xay.office import employee


class PartsSnapshotTests(LedgerCase):
    def A(self, typ, by, data, subject=None, ts=None, conv=None):
        return core_events.append(self.conn, typ, by, data, ws=WS, conv=conv or CV, subject=subject, ts=ts)

    def add_part(self, pid, title, after=(), key=None, check=None, ts=None, conv=None):
        data = {"title": title, "after": list(after)}
        if key is not None:
            data["key"] = key
        if check is not None:
            data["check"] = check
        c = conv or CV
        return self.A("part.added", "lead:" + c, data, pid, ts or T0 + 1, conv=c)

    def add_task(self, tk, title, owner=None, dept=None, part=None, status="planned", ts=None, conv=None):
        c = conv or CV
        lead = "lead:" + c
        self.A("task.added", lead, {"key": tk, "title": title, "owner": owner, "dept": dept, "part": part,
                                    "deps": [], "writes": [], "steps": [], "mode": "write", "reason": "r"},
               "tk_" + tk, ts or T0 + 10, conv=c)
        if status != "planned":
            if status == "ready":
                self.A("task.ready", "office", {}, "tk_" + tk, ts or T0 + 11, conv=c)
            elif status == "queued":
                self.A("task.queued", "office", {"owner": owner, "position": 1}, "tk_" + tk, ts or T0 + 11, conv=c)
            elif status == "working":
                self.A("turn.started", "station:" + (owner or CODEX), {"task": "tk_" + tk, "em": owner or CODEX, "kind": "first"},
                       "tn_" + tk, ts or T0 + 11, conv=c)
            elif status == "done":
                self.A("turn.started", "station:" + (owner or CODEX), {"task": "tk_" + tk, "em": owner or CODEX, "kind": "first"},
                       "tn_" + tk, ts or T0 + 11, conv=c)
                self.A("turn.ended", "station:" + (owner or CODEX), {"em": owner or CODEX, "status": "completed",
                                                                     "evidence": {"cli_status": "completed", "changed_files": ["a.py"]}},
                       "tn_" + tk, ts or T0 + 12, conv=c)
                self.A("task.landed", "station:" + (owner or CODEX), {"files": ["a.py"], "add": 1, "del": 0}, "tk_" + tk, ts or T0 + 13, conv=c)
            elif status == "failed":
                self.A("turn.started", "station:" + (owner or CODEX), {"task": "tk_" + tk, "em": owner or CODEX, "kind": "first"},
                       "tn_" + tk, ts or T0 + 11, conv=c)
                self.A("turn.ended", "station:" + (owner or CODEX), {"em": owner or CODEX, "status": "failed",
                                                                     "evidence": {"cli_status": "failed", "changed_files": []}},
                       "tn_" + tk, ts or T0 + 12, conv=c)
                self.A("task.failed", "station:" + (owner or CODEX), {"reason": "失败", "detail": "error"}, "tk_" + tk, ts or T0 + 13, conv=c)

    def test_parts_topology_col_and_row(self):
        # 两个串行 + 一个并行的小任务：
        # p1 (key=p1, after=[]) 与 p2 (key=p2, after=[]) 并行 → col=0, row=0 与 col=0, row=1
        # p3 (key=p3, after=[p1]) 串行接在 p1 后面 → col=1, row=0
        self.add_part("pt_1", "小任务1", after=[], key="p1", ts=T0 + 1)
        self.add_part("pt_2", "小任务2", after=[], key="p2", ts=T0 + 2)
        self.add_part("pt_3", "小任务3", after=["p1"], key="p3", ts=T0 + 3)

        self.A("dept.opened", LEAD, {"name": "页面部", "mission": "做页面", "owns": [], "head": "rules", "part": "p1"}, "fe", T0 + 4)
        self.add_task("t1", "任务1", owner=CODEX, dept="fe", part="pt_1", status="done", ts=T0 + 10)
        self.add_task("t2", "任务2", owner=DSH, part="p2", status="working", ts=T0 + 11)
        self.add_task("t3", "任务3", owner=AGY, part="p3", status="planned", ts=T0 + 12)

        snap = core_snapshot.build(self.conn, conv=CV, now=NOW)
        parts = snap["parts"]
        self.assertEqual(len(parts), 3)

        p1 = next(p for p in parts if p["id"] == "pt_1")
        p2 = next(p for p in parts if p["id"] == "pt_2")
        p3 = next(p for p in parts if p["id"] == "pt_3")

        self.assertEqual((p1["col"], p1["row"]), (0, 0))
        self.assertEqual((p2["col"], p2["row"]), (0, 1))
        self.assertEqual((p3["col"], p3["row"]), (1, 0))

        # 状态检查：p1 的任务 done → done；p2 的任务 working → working；p3 任务 planned 但 after(pt_1) 是 done → ready
        self.assertEqual(p1["status"], "done")
        self.assertEqual(p2["status"], "working")
        self.assertEqual(p3["status"], "ready")

        # tasks[] 每件多 part，depts[] 每个多 part
        self.assertEqual(p1["depts"], ["fe"])
        fe_dept = next(d for d in snap["depts"] if d["id"] == "fe")
        self.assertEqual(fe_dept.get("part"), "pt_1")   # 写的是简称，快照里换成编号
        t1_task = next(t for t in snap["tasks"] if t["id"] == "tk_t1")
        self.assertEqual(t1_task.get("part"), "pt_1")

    def test_loose_tasks_at_the_end(self):
        # 没写 part 的活归一个 id="" 的"零散的活"小任务，放在最后一列
        self.add_part("pt_1", "小任务1", after=[], key="p1", ts=T0 + 1)
        self.add_task("t1", "任务1", owner=CODEX, part="pt_1", status="done", ts=T0 + 10)
        self.add_task("loose1", "零散活1", owner=DSH, part=None, status="planned", ts=T0 + 11)

        snap = core_snapshot.build(self.conn, conv=CV, now=NOW)
        parts = snap["parts"]
        self.assertEqual(len(parts), 2)
        loose = parts[1]
        self.assertEqual(loose["id"], "")
        self.assertEqual(loose["title"], "零散的活")
        self.assertEqual((loose["col"], loose["row"]), (1, 0))
        self.assertEqual(loose["count"]["all"], 1)

    def test_old_conv_with_depts(self):
        # 没拆小任务的旧对话（即使有部门）不再按部门分，整个对话一个小任务（id = "all"，depts = []）
        OLD_CV = "cv_old_with_depts"
        lead = "lead:" + OLD_CV
        self.A("conv.begun", lead, {"title": "旧对话有部门", "lead_kind": "claude", "aliases": [], "cwd": "/tmp/x"}, OLD_CV, T0, conv=OLD_CV)
        self.A("dept.opened", lead, {"name": "前端部", "mission": "页面", "owns": [], "head": "rules"}, "fe", T0 + 1, conv=OLD_CV)
        self.A("dept.opened", lead, {"name": "后端部", "mission": "服务", "owns": [], "head": "rules"}, "be", T0 + 2, conv=OLD_CV)

        # 部门活与无部门活
        self.A("task.added", lead, {"key": "t_fe", "title": "前端活", "dept": "fe", "deps": [], "writes": [], "steps": [], "mode": "write", "reason": "r"}, "tk_t_fe", T0 + 10, conv=OLD_CV)
        self.A("task.added", lead, {"key": "t_be", "title": "后端活", "dept": "be", "deps": [], "writes": [], "steps": [], "mode": "write", "reason": "r"}, "tk_t_be", T0 + 11, conv=OLD_CV)
        self.A("task.added", lead, {"key": "t_loose", "title": "没部门活", "dept": None, "deps": [], "writes": [], "steps": [], "mode": "write", "reason": "r"}, "tk_t_loose", T0 + 12, conv=OLD_CV)

        snap = core_snapshot.build(self.conn, conv=OLD_CV, now=NOW)
        parts = snap["parts"]
        self.assertEqual(len(parts), 1)

        p_all = parts[0]
        self.assertEqual(p_all["id"], "all")
        self.assertEqual(p_all["title"], "旧对话有部门")
        self.assertEqual((p_all["col"], p_all["row"]), (0, 0))
        self.assertEqual(p_all["depts"], [])
        self.assertEqual(p_all["count"]["all"], 3)

    def test_old_conv_without_depts(self):
        # 一个部门都没有就整个对话一个小任务（id = "all"，title = 对话标题）
        NO_DEPT_CV = "cv_no_dept"
        lead = "lead:" + NO_DEPT_CV
        self.A("conv.begun", lead, {"title": "无部门对话测试", "lead_kind": "claude", "aliases": [], "cwd": "/tmp/x"}, NO_DEPT_CV, T0, conv=NO_DEPT_CV)
        self.A("task.added", lead, {"key": "t_alone", "title": "独立活", "deps": [], "writes": [], "steps": [], "mode": "write", "reason": "r"}, "tk_t_alone", T0 + 10, conv=NO_DEPT_CV)

        snap = core_snapshot.build(self.conn, conv=NO_DEPT_CV, now=NOW)
        parts = snap["parts"]
        self.assertEqual(len(parts), 1)
        p_all = parts[0]
        self.assertEqual(p_all["id"], "all")
        self.assertEqual(p_all["title"], "无部门对话测试")
        self.assertEqual((p_all["col"], p_all["row"]), (0, 0))
        self.assertEqual(p_all["depts"], [])
        self.assertEqual(p_all["count"]["all"], 1)

    def test_tree_parts_structure_and_mine_and_groups_exist(self):
        # /api/v2/employee 的 tree:
        # 保留现有的 groups 字段不变，新增 parts:[{id, title, col, row, status, check, mine, depts:[{id, name, items}], items}]
        self.add_part("pt_1", "架构设计", after=[], key="arch", ts=T0 + 1)
        self.add_part("pt_2", "代码开发", after=["arch"], key="code", ts=T0 + 2)
        self.A("dept.opened", LEAD, {"name": "研发部", "mission": "开发", "owns": [], "head": "rules", "part": "code"}, "dev", T0 + 3)

        # pt_1 里有 CODEX 的活（mine=True）
        self.add_task("tk1", "写方案", owner=CODEX, part="pt_1", status="done", ts=T0 + 10)
        # pt_2 里研发部的活交给 DSH（对 CODEX 而言 mine=False）
        self.add_task("tk2", "写代码", owner=DSH, dept="dev", part="code", status="working", ts=T0 + 11)
        # 零散活交给 CODEX（mine=True）
        self.add_task("tk3", "打杂", owner=CODEX, part=None, status="planned", ts=T0 + 12)

        data = employee.build(self.conn, CODEX, conv=CV, live_root=self.tmp / "v2" / "live", now=NOW, quota_home=self.tmp / "v2")
        tree = data["tree"]

        # 1. 验证 groups 照旧存在
        self.assertIn("groups", tree)
        self.assertTrue(len(tree["groups"]) > 0)

        # 2. 验证 parts 结构
        self.assertIn("parts", tree)
        parts = tree["parts"]
        self.assertEqual(len(parts), 3)  # pt_1, pt_2, 零散的活

        p1 = parts[0]
        p2 = parts[1]
        p_loose = parts[2]

        self.assertEqual(p1["id"], "pt_1")
        self.assertTrue(p1["mine"])  # CODEX 负责 tk1
        self.assertEqual(p1["depts"], [])
        self.assertEqual(len(p1["items"]), 1)
        self.assertEqual(p1["items"][0]["title"], "写方案")
        self.assertTrue(p1["items"][0]["mine"])
        self.assertIn("steps", p1["items"][0])
        self.assertIn("depth", p1["items"][0])

        self.assertEqual(p2["id"], "pt_2")
        self.assertFalse(p2["mine"])  # DSH 负责，CODEX 不负责
        self.assertEqual(len(p2["depts"]), 1)
        self.assertEqual(p2["depts"][0]["id"], "dev")
        self.assertEqual(p2["depts"][0]["name"], "研发部")
        self.assertEqual(len(p2["depts"][0]["items"]), 1)
        self.assertEqual(p2["depts"][0]["items"][0]["title"], "写代码")
        self.assertFalse(p2["depts"][0]["items"][0]["mine"])
        self.assertEqual(p2["items"], [])

        self.assertEqual(p_loose["id"], "")
        self.assertTrue(p_loose["mine"])  # CODEX 负责 tk3
        self.assertEqual(len(p_loose["items"]), 1)
        self.assertEqual(p_loose["items"][0]["title"], "打杂")


if __name__ == "__main__":
    unittest.main()
