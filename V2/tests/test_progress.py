"""总进度条与各 AI 份额（references/plans/arch-v11-router-parity.md §8）测试。"""
import os
import shutil
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

V2_ROOT = Path(__file__).resolve().parents[1]
if str(V2_ROOT) not in sys.path:
    sys.path.insert(0, str(V2_ROOT))

from tests.test_employee_panel_v3 import AGY, CODEX, CV, DSH, LEAD, NOW, T0, WS, LedgerCase
from xay.core import events as core_events
from xay.core import snapshot as core_snapshot
from xay.office import oldboard


class ProgressTests(LedgerCase):
    def add_task(self, tk, title, owner=None, dept=None, part=None, status="planned", progress=None, ts=None):
        lead = "lead:" + CV
        core_events.append(self.conn, "task.added", lead, {
            "key": tk, "title": title, "owner": owner, "dept": dept, "part": part,
            "deps": [], "writes": [], "steps": [], "mode": "write", "reason": "test"
        }, subject="tk_" + tk, ws=WS, conv=CV, ts=ts or T0 + 10)

        em = owner or CODEX
        station = "station:" + em

        if status == "ready":
            core_events.append(self.conn, "task.ready", "office", {}, subject="tk_" + tk, ws=WS, conv=CV, ts=ts or T0 + 11)
        elif status == "queued":
            core_events.append(self.conn, "task.queued", "office", {"owner": em, "position": 1}, subject="tk_" + tk, ws=WS, conv=CV, ts=ts or T0 + 11)
        elif status == "working":
            core_events.append(self.conn, "turn.started", station, {"task": "tk_" + tk, "em": em, "kind": "first"},
                               subject="tn_" + tk, ws=WS, conv=CV, ts=ts or T0 + 11)
            if progress:
                core_events.append(self.conn, "task.progress", station, progress, subject="tk_" + tk, ws=WS, conv=CV, ts=ts or T0 + 12)
        elif status == "done":
            core_events.append(self.conn, "turn.started", station, {"task": "tk_" + tk, "em": em, "kind": "first"},
                               subject="tn_" + tk, ws=WS, conv=CV, ts=ts or T0 + 11)
            core_events.append(self.conn, "turn.ended", station, {
                "em": em, "status": "completed", "evidence": {"cli_status": "completed", "changed_files": ["a.py"]}
            }, subject="tn_" + tk, ws=WS, conv=CV, ts=ts or T0 + 12)
            core_events.append(self.conn, "task.landed", station, {"files": ["a.py"], "add": 1, "del": 0},
                               subject="tk_" + tk, ws=WS, conv=CV, ts=ts or T0 + 13)
        elif status == "kept":
            core_events.append(self.conn, "turn.started", station, {"task": "tk_" + tk, "em": em, "kind": "first"},
                               subject="tn_" + tk, ws=WS, conv=CV, ts=ts or T0 + 11)
            core_events.append(self.conn, "task.kept", station, {"reason": "failed_with_changes", "detail": "保留改动"},
                               subject="tk_" + tk, ws=WS, conv=CV, ts=ts or T0 + 12)
        elif status == "canceled":
            core_events.append(self.conn, "task.canceled", lead, {"reason": "用户撤回"},
                               subject="tk_" + tk, ws=WS, conv=CV, ts=ts or T0 + 11)

    def test_no_tasks(self):
        snap = core_snapshot.build(self.conn, conv=CV, now=NOW)
        self.assertIn("progress", snap)
        p = snap["progress"]
        self.assertEqual(p["pct"], 0)
        self.assertEqual(p["all"], 0)
        self.assertEqual(p["done"], 0)
        self.assertEqual(p["working"], 0)
        self.assertEqual(p["by_agent"], [])
        self.assertEqual(p["text"], "0 件活做完 0 件，0 件在做")

    def test_all_done(self):
        self.add_task("t1", "第一件活", owner=CODEX, status="done")
        self.add_task("t2", "第二件活", owner=DSH, status="done")

        snap = core_snapshot.build(self.conn, conv=CV, now=NOW)
        p = snap["progress"]
        self.assertEqual(p["pct"], 100)
        self.assertEqual(p["all"], 2)
        self.assertEqual(p["done"], 2)
        self.assertEqual(p["working"], 0)
        self.assertEqual(p["text"], "2 件活做完 2 件，0 件在做")
        self.assertEqual(sum(a["pct"] for a in p["by_agent"]), 100)
        # 两个各占 50%
        agents = {a["agent"]: a["pct"] for a in p["by_agent"]}
        self.assertEqual(agents.get("codex"), 50)
        self.assertEqual(agents.get("dsh"), 50)

    def test_working_step_2_of_4(self):
        # 正在做第 2/4 步 -> min(0.9, 2/4) = 0.5
        self.add_task("t1", "任务1", owner=CODEX, status="working", progress={"step": 2, "of": 4, "text": "第2步"})

        snap = core_snapshot.build(self.conn, conv=CV, now=NOW)
        p = snap["progress"]
        self.assertEqual(p["pct"], 50)
        self.assertEqual(p["all"], 1)
        self.assertEqual(p["done"], 0)
        self.assertEqual(p["working"], 1)
        self.assertEqual(p["text"], "1 件活做完 0 件，1 件在做")
        self.assertEqual(len(p["by_agent"]), 1)
        self.assertEqual(p["by_agent"][0], {"agent": "codex", "label": "Codex", "pct": 50})

    def test_working_without_step_gives_point_one(self):
        # 正在做但没有步数时取 0.1
        self.add_task("t1", "任务1", owner=DSH, status="working", progress=None)

        snap = core_snapshot.build(self.conn, conv=CV, now=NOW)
        p = snap["progress"]
        self.assertEqual(p["pct"], 10)
        self.assertEqual(p["working"], 1)
        self.assertEqual(p["by_agent"][0], {"agent": "dsh", "label": "DeepSeek", "pct": 10})

    def test_kept_task_gives_point_nine(self):
        # 改动保留（kept）= 0.9
        self.add_task("t1", "任务1", owner=AGY, status="kept")

        snap = core_snapshot.build(self.conn, conv=CV, now=NOW)
        p = snap["progress"]
        self.assertEqual(p["pct"], 90)
        self.assertEqual(p["all"], 1)
        self.assertEqual(p["by_agent"][0], {"agent": "agy", "label": "Gemini", "pct": 90})

    def test_canceled_tasks_excluded(self):
        # 撤回的活不算进 all，也不算进份额
        self.add_task("t1", "正常完成的活", owner=CODEX, status="done")
        self.add_task("t2", "撤回的活", owner=DSH, status="canceled")

        snap = core_snapshot.build(self.conn, conv=CV, now=NOW)
        p = snap["progress"]
        self.assertEqual(p["all"], 1)
        self.assertEqual(p["done"], 1)
        self.assertEqual(p["pct"], 100)
        self.assertEqual(p["text"], "1 件活做完 1 件，0 件在做")
        self.assertEqual(len(p["by_agent"]), 1)
        self.assertEqual(p["by_agent"][0]["agent"], "codex")

    def test_multiple_agents_sum_equals_total_pct_with_remainder(self):
        # 3 种 AI 分别完成 1 件，总计 3 件全做完
        # 100 / 3 = 33.333... -> 33 + 33 + (100 - 66 = 34) = 100
        self.add_task("t1", "Codex任务", owner=CODEX, status="done")
        self.add_task("t2", "Dsh任务", owner=DSH, status="done")
        self.add_task("t3", "Agy任务", owner=AGY, status="done")

        snap = core_snapshot.build(self.conn, conv=CV, now=NOW)
        p = snap["progress"]
        self.assertEqual(p["pct"], 100)
        self.assertEqual(p["all"], 3)
        self.assertEqual(p["done"], 3)
        self.assertEqual(sum(a["pct"] for a in p["by_agent"]), p["pct"])
        pcts = [a["pct"] for a in p["by_agent"]]
        self.assertEqual(sorted(pcts), [33, 33, 34])   # 只约定每段差不超过 1、加起来等于总数，不约定零头落在哪一段

        # 混合多种状态与 AI
        # t1: CODEX done (1.0)
        # t2: DSH working 2/4 (0.5)
        # t3: AGY kept (0.9)
        # t4: CODEX planned (0.0)
        # 总分 = 2.4, 总数 = 4 -> pct = round(100 * 2.4 / 4) = 60
        # codex: 1.0 / 4 = 25% -> 25
        # dsh: 0.5 / 4 = 12.5% -> round(12.5) = 12 或 13
        # agy: 补差 60 - 25 - dsh = 23 或 22
        # 和严格等于 60
        self.add_task("t4", "排队活", owner=CODEX, status="planned")
        # 修改 t2 为 working 2/4
        core_events.append(self.conn, "turn.started", "station:" + DSH, {"task": "tk_t2", "em": DSH, "kind": "first"},
                           subject="tn_t2_2", ws=WS, conv=CV, ts=T0 + 30)
        core_events.append(self.conn, "task.progress", "station:" + DSH, {"step": 2, "of": 4},
                           subject="tk_t2", ws=WS, conv=CV, ts=T0 + 31)
        # 修改 t3 为 kept
        core_events.append(self.conn, "task.kept", "station:" + AGY, {"reason": "conflict", "detail": "冲突保留"},
                           subject="tk_t3", ws=WS, conv=CV, ts=T0 + 32)

        p2 = core_snapshot.calc_progress(core_events._reading(self.conn).__enter__(), CV)
        self.assertEqual(sum(a["pct"] for a in p2["by_agent"]), p2["pct"])

    def test_oldboard_api_state_contains_progress(self):
        self.add_task("t1", "任务1", owner=CODEX, status="done")
        st = oldboard.state(self.conn, now=NOW)
        self.assertIn("workspaces", st)
        found_conv = None
        for ws in st["workspaces"]:
            for c in ws["conversations"]:
                if c["id"] == CV:
                    found_conv = c
                    break
        self.assertIsNotNone(found_conv)
        self.assertIn("progress", found_conv)
        self.assertEqual(found_conv["progress"]["pct"], 100)
        self.assertEqual(found_conv["progress"]["all"], 1)

    def test_by_part_progress(self):
        # 两个小任务：pt_1 全部完成 (100%)，pt_2 进行中 (50%)
        core_events.append(self.conn, "part.added", LEAD, {"title": "小任务一", "after": []},
                           subject="pt_1", ws=WS, conv=CV, ts=T0 + 1)
        core_events.append(self.conn, "part.added", LEAD, {"title": "小任务二", "after": []},
                           subject="pt_2", ws=WS, conv=CV, ts=T0 + 2)

        self.add_task("t1", "任务1", owner=CODEX, part="pt_1", status="done")
        self.add_task("t2", "任务2", owner=DSH, part="pt_2", status="working", progress={"step": 1, "of": 2})

        snap = core_snapshot.build(self.conn, conv=CV, now=NOW)
        p = snap["progress"]
        self.assertIn("by_part", p)
        by_part = {x["id"]: x["pct"] for x in p["by_part"]}
        self.assertEqual(by_part.get("pt_1"), 100)
        self.assertEqual(by_part.get("pt_2"), 50)


if __name__ == "__main__":
    unittest.main()
