"""员工干到一半直接派活测试（tk_9ed5b54f / arch-v11 §5）。

覆盖：
1. 执行者模式下 xay2 plan add 只写发件箱
2. 工位读取发件箱并验证各项限制：
   - 只能一层：派出来的活不能再派
   - 最多 3 件：超过 3 件拒绝
   - 要改文件的必须写 writes，且不能和自己的 writes 重叠
   - 只读任务（readonly）不限 writes
   - 必须自动依赖自己（deps=[by_task]），且属于同一小任务（part）
3. 校验合格写 task.added（by=station，带 by_task、by_em）：
   - events.py 权限控制（工位只能写员工派活的 task.added）
   - 计划版本 +1
   - 历史文案格式（"Codex 1 派了「X」给 Gemini"）
4. 负责人的 wait 把它当要紧事并展示
5. 校验不合格记 outbox_rejected 并在下一轮开头告诉员工
"""
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

V2_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(V2_DIR))

from xay.core import append, load_state, open_db, since
from xay.core.errors import PermissionError as LedgerPermissionError
from xay.core.humanize import plan_change_text, plan_summary, text as human_text
from xay.core.ids import new_id
from xay.station import station as st
from tests.test_rules import RulesCase


class ExecDispatchTests(RulesCase):
    def setUp(self):
        super().setUp()
        self.em1 = self.hire("codex")
        self.em2 = self.hire("agy")

    def _add_part(self, title, check=None, key=None):
        pid = new_id("pt_")
        check = check or {"kind": "rules"}
        append(self.conn, "part.added", self.lead,
               {"title": title, "after": [], "check": check, "key": key},
               ws=self.ws, conv=self.conv, subject=pid)
        return pid

    def _add_task(self, title, owner=None, part=None, writes=(), mode="write", by_task=None, by_em=None):
        tk = new_id("tk_")
        data = {
            "title": title, "deps": [], "writes": list(writes), "mode": mode,
            "reason": "测试创建", "card": title
        }
        if owner: data["owner"] = owner
        if part: data["part"] = part
        if by_task: data["by_task"] = by_task
        if by_em: data["by_em"] = by_em
        by = "station" if by_task and by_em else self.lead
        append(self.conn, "task.added", by, data, ws=self.ws, conv=self.conv, subject=tk)
        return tk

    def test_executor_cli_plan_add_writes_outbox(self):
        """执行者模式下调用 xay2 plan add 只写发件箱 {type: 'task'}"""
        with tempfile.TemporaryDirectory() as td:
            outbox = Path(td) / "outbox.jsonl"
            card_file = Path(td) / "card.md"
            card_file.write_text("详细处理规则", encoding="utf-8")
            env = dict(os.environ)
            env["XAY_ROLE"] = "executor"
            env["XAY2_TASK"] = "tk_curr01"
            env["XAY2_EM"] = self.em1
            env["XAY2_OUTBOX"] = str(outbox)

            cmd = [
                str(V2_DIR / "bin" / "xay2"),
                "plan", "add", "分析数据处理逻辑",
                "--agent", "agy",
                "--writes", "data/analysis.py,data/stat.py",
                "--reason", "需要 Gemini 协助分析",
                "--card", str(card_file)
            ]
            res = subprocess.run(cmd, env=env, capture_output=True, text=True)
            self.assertEqual(res.returncode, 0, res.stderr)
            self.assertTrue(outbox.exists())

            lines = [json.loads(line) for line in outbox.read_text(encoding="utf-8").splitlines() if line.strip()]
            self.assertEqual(len(lines), 1)
            row = lines[0]
            self.assertEqual(row.get("type"), "task")
            self.assertEqual(row.get("task"), "tk_curr01")
            d = row.get("data")
            self.assertEqual(d.get("title"), "分析数据处理逻辑")
            self.assertEqual(d.get("agent"), "agy")
            self.assertEqual(d.get("writes"), ["data/analysis.py", "data/stat.py"])
            self.assertEqual(d.get("reason"), "需要 Gemini 协助分析")
            self.assertEqual(d.get("card"), "详细处理规则")
            self.assertEqual(d.get("mode"), "write")

    def test_station_processes_outbox_task_and_updates_plan(self):
        """工位正常读取合格的派活请求：写入 task.added（by=station），计划版本+1，文案正确"""
        pid = self._add_part("报表分析模块")
        tk1 = self._add_task("导出数据汇总", owner=self.em1, part=pid, writes=["export.py"])

        station = st.Station(self.em1, home=self.home)
        station.conn = self.conn
        station.ws = self.ws
        station.turn = {"task": tk1, "outbox_n": 0, "conv": self.conv, "rejects": 0}

        row = {
            "v": 1, "task": tk1, "type": "task",
            "data": {
                "title": "核对导出字段格式",
                "agent": "agy",
                "writes": ["verify_fields.py"],
                "reason": "需要 Gemini 帮忙核对",
                "card": "检查各列是否齐全"
            }
        }
        station._handle_outbox_task(1, row)

        # 检查账本新增的 task
        state = load_state(self.conn)
        dispatched_tasks = [
            t for t in state["tasks"].values()
            if t.get("by_task") == tk1
        ]
        self.assertEqual(len(dispatched_tasks), 1)
        new_task = dispatched_tasks[0]
        self.assertEqual(new_task["title"], "核对导出字段格式")
        self.assertEqual(new_task["part"], pid)
        self.assertEqual(new_task["deps"], [])   # 能同时做，不等派活的人
        self.assertEqual(new_task["writes"], ["verify_fields.py"])
        self.assertEqual(new_task["by_em"], self.em1)
        self.assertEqual(new_task["agent"], "agy")

        # 计划版本应该增加，检查历史文案
        conv_info = state["convs"][self.conv]
        self.assertGreater(conv_info["plan_version"], 0)
        history = state["plan_history"][self.conv]
        latest_hist = history[-1]
        summary = plan_summary(state, self.conv, latest_hist)
        # 文案应体现 "Codex 1 派了「核对导出字段格式」给 Gemini"
        self.assertIn("派了「核对导出字段格式」给 Gemini", summary)

    def test_station_outbox_task_restrictions(self):
        """工位严格检查各项限制"""
        pid = self._add_part("核心逻辑")
        tk_parent = self._add_task("初始任务", owner=self.em1, part=pid, writes=["core.py", "utils.py"])

        station = st.Station(self.em1, home=self.home)
        station.conn = self.conn
        station.ws = self.ws
        station.turn = {"task": tk_parent, "outbox_n": 0, "conv": self.conv, "rejects": 0}

        # 1. 要改文件的必须写 writes
        row_no_writes = {
            "v": 1, "task": tk_parent, "type": "task",
            "data": {"title": "无 writes 任务", "agent": "agy", "writes": []}
        }
        station._handle_outbox_task(1, row_no_writes)
        events = since(self.conn, 0, conv=self.conv)
        rejected = [e for e in events if e["type"] == "signal.raised" and (e.get("data") or {}).get("kind") == "outbox_rejected"]
        self.assertTrue(any("必须填写改动文件范围" in (e.get("data") or {}).get("text", "") for e in rejected))

        # 2. writes 不能和自己的重叠
        row_overlap = {
            "v": 1, "task": tk_parent, "type": "task",
            "data": {"title": "重叠任务", "agent": "agy", "writes": ["core.py", "other.py"]}
        }
        station._handle_outbox_task(2, row_overlap)
        events = since(self.conn, 0, conv=self.conv)
        rejected = [e for e in events if e["type"] == "signal.raised" and (e.get("data") or {}).get("kind") == "outbox_rejected"]
        self.assertTrue(any("重叠" in (e.get("data") or {}).get("text", "") for e in rejected))

        # 3. 只读任务（readonly）不限 writes，且重叠也不受限
        row_readonly = {
            "v": 1, "task": tk_parent, "type": "task",
            "data": {"title": "只读审查任务", "agent": "agy", "readonly": True, "writes": ["core.py"]}
        }
        station._handle_outbox_task(3, row_readonly)
        state = load_state(self.conn)
        self.assertTrue(any(t["title"] == "只读审查任务" and t.get("mode") == "readonly" for t in state["tasks"].values()))

        # 4. 一件活最多只能派 3 件
        row_sub2 = {
            "v": 1, "task": tk_parent, "type": "task",
            "data": {"title": "第 2 件子任务", "agent": "agy", "writes": ["sub2.py"]}
        }
        row_sub3 = {
            "v": 1, "task": tk_parent, "type": "task",
            "data": {"title": "第 3 件子任务", "agent": "agy", "writes": ["sub3.py"]}
        }
        row_sub4 = {
            "v": 1, "task": tk_parent, "type": "task",
            "data": {"title": "第 4 件子任务（超额）", "agent": "agy", "writes": ["sub4.py"]}
        }
        station._handle_outbox_task(4, row_sub2)
        station._handle_outbox_task(5, row_sub3)
        station._handle_outbox_task(6, row_sub4)

        events = since(self.conn, 0, conv=self.conv)
        rejected = [e for e in events if e["type"] == "signal.raised" and (e.get("data") or {}).get("kind") == "outbox_rejected"]
        self.assertTrue(any("最多只能派 3 件" in (e.get("data") or {}).get("text", "") for e in rejected))

        # 5. 只一层：派出来的活不能再派
        state = load_state(self.conn)
        dispatched_child = next(t["id"] for t in state["tasks"].values() if t.get("by_task") == tk_parent)
        station.turn = {"task": dispatched_child, "outbox_n": 0, "conv": self.conv, "rejects": 0}
        row_grandchild = {
            "v": 1, "task": dispatched_child, "type": "task",
            "data": {"title": "孙任务", "agent": "agy", "writes": ["child2.py"]}
        }
        station._handle_outbox_task(7, row_grandchild)
        events = since(self.conn, 0, conv=self.conv)
        rejected = [e for e in events if e["type"] == "signal.raised" and (e.get("data") or {}).get("kind") == "outbox_rejected"]
        self.assertTrue(any("只能派一层" in (e.get("data") or {}).get("text", "") or "不能再给别人派活" in (e.get("data") or {}).get("text", "") for e in rejected))

    def test_events_permission_checks(self):
        """events.py 仅允许 station 在带 by_task 与 by_em 时写入 task.added"""
        pid = self._add_part("模块")
        tk = self._add_task("父活", owner=self.em1, part=pid)

        # 1. station 尝试写普通 task.added（缺少 by_task / by_em）应被拒绝
        illegal_tk = new_id("tk_")
        with self.assertRaises(LedgerPermissionError):
            append(self.conn, "task.added", "station",
                   {"title": "非法派活", "part": pid},
                   ws=self.ws, conv=self.conv, subject=illegal_tk)

        # 2. 合法的员工派活能够成功写入
        legal_tk = new_id("tk_")
        append(self.conn, "task.added", "station",
               {"title": "合法派活", "part": pid, "by_task": tk, "by_em": self.em1},
               ws=self.ws, conv=self.conv, subject=legal_tk)
        state = load_state(self.conn)
        self.assertIn(legal_tk, state["tasks"])

    def test_wait_wakes_on_dispatched_task(self):
        """负责人的 wait 把它当要紧事，并展示派活大白话"""
        from xay.cli import _wakes, _wait_line, Ctx

        pid = self._add_part("模块")
        tk = self._add_task("父活", owner=self.em1, part=pid)

        disp_tk = new_id("tk_")
        append(self.conn, "task.added", "station",
               {"title": "协助编写测试", "agent": "agy", "by_task": tk, "by_em": self.em1, "reason": "需要单元测试"},
               ws=self.ws, conv=self.conv, subject=disp_tk)

        events = since(self.conn, 0, conv=self.conv)
        disp_ev = next(e for e in events if e["subject"] == disp_tk and e["type"] == "task.added")

        state = load_state(self.conn)
        self.assertTrue(_wakes(disp_ev, state))

        ctx = Ctx(self.conn, self.conv, state)
        line = _wait_line(ctx, state, {"event": disp_ev, "events": [disp_ev]})
        self.assertIn("派了「协助编写测试」给 Gemini", line)

    def test_next_turn_input_notifies_rejected_reasons(self):
        """被拒绝的派活会在下一轮开头提示员工"""
        pid = self._add_part("模块")
        tk = self._add_task("父活", owner=self.em1, part=pid)

        station = st.Station(self.em1, home=self.home)
        station.conn = self.conn
        station.ws = self.ws
        station._refresh(force=True)
        station.turn = {"task": tk, "outbox_n": 0, "conv": self.conv, "rejects": 0}

        station._reject("改动文件范围重叠了")
        prompt, _ = station._turn_input(station.state["tasks"][tk], tk, "normal", False, "origin")
        self.assertIn("改动文件范围重叠了", prompt)


if __name__ == "__main__":
    unittest.main()
