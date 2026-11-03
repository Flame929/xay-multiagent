"""员工看任务板测试（tk_c3da8d1e / arch-v11 §6）。

覆盖：
1. 工位开工传递环境变量 XAY2_BOARD_FILE（指向 .xay/out/board.md）。
2. 工位开工每轮将当前计划树写出至 board.md（≤40 行，标出'你在做的是这件'）。
3. 计划超长时自动截断至 40 行以内。
4. 执行者命令 xay2 board 直接打印 board.md。
5. 员工手册包含配合先看任务板与派活规则的两句话。
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

from xay.adapters.fake import FakeAdapter
from xay.cli import render_board
from xay.core import append, open_db
from xay.core.ids import new_id, ws_for
from xay.station.station import Station
from tests.test_rules import RulesCase


class ExecBoardTests(RulesCase):
    def hire(self, agent="codex", label=None):
        em = new_id("em_")
        data = {"agent": agent}
        if label:
            data["label"] = label
        append(self.conn, "employee.hired", self.lead, data, ws=self.ws, conv=self.conv, subject=em)
        return em

    def setUp(self):
        super().setUp()
        self.em1 = self.hire("codex", label="Codex 1")
        self.em2 = self.hire("agy", label="Gemini 1")

    def _add_part(self, title, check=None, key=None):
        pid = new_id("pt_")
        check = check or {"kind": "rules"}
        append(self.conn, "part.added", self.lead,
               {"title": title, "after": [], "check": check, "key": key},
               ws=self.ws, conv=self.conv, subject=pid)
        return pid

    def _add_task(self, title, owner=None, part=None, key=None, progress=None):
        tk = new_id("tk_")
        data = {
            "title": title, "deps": [], "writes": [], "mode": "write",
            "reason": "测试创建", "card": title
        }
        if owner: data["owner"] = owner
        if part: data["part"] = part
        if key: data["key"] = key
        if progress: data["progress"] = progress
        append(self.conn, "task.added", self.lead, data, ws=self.ws, conv=self.conv, subject=tk)
        return tk

    def test_env_xay2_board_file_in_card(self):
        """工位准备给员工的 card.env 必须包含 XAY2_BOARD_FILE，且与 XAY2_TASK_FILE 放在同一目录"""
        st = Station(self.em1, home=self.home)
        st.conn = self.conn
        st.ws = self.ws
        st.agent = "codex"
        cwd = Path(self.home) / "workspace"
        card = st._card(cwd, "tk_test01", self.conv)
        env = card.get("env") or {}
        self.assertIn("XAY2_BOARD_FILE", env)
        board_file = Path(env["XAY2_BOARD_FILE"])
        task_file = Path(env["XAY2_TASK_FILE"])
        self.assertEqual(board_file.name, "board.md")
        self.assertEqual(board_file.parent, task_file.parent)

    def test_station_writes_board_file_on_turn(self):
        """工位每轮开工时写 board.md 到输出目录，并标出'你在做的是这件'"""
        pt1 = self._add_part("第一阶段：基础框架")
        tk1 = self._add_task("编写核心接口", owner=self.em1, part=pt1, progress={"step": 2, "of": 4})
        tk2 = self._add_task("辅助数据整理", owner=self.em2, part=pt1)

        st = Station(self.em1, home=self.home)
        st.conn = self.conn
        st.ws = self.ws
        st.agent = "codex"
        st._refresh()

        desk_path = st._desk_path()
        out_dir = desk_path / ".xay" / "out"
        out_dir.mkdir(parents=True, exist_ok=True)
        board_file = out_dir / "board.md"

        # 触发写 board 文件
        st._write_board_file(tk1, self.conv)
        self.assertTrue(board_file.exists())

        content = board_file.read_text(encoding="utf-8")
        lines = content.splitlines()

        # 验证行数 ≤ 40 行
        self.assertLessEqual(len(lines), 40)
        # 验证标出'你在做的是这件'
        self.assertIn("你在做的是这件", content)

        # 确认当前正在做的任务行包含提示
        matched = [ln for ln in lines if "编写核心接口" in ln]
        self.assertTrue(len(matched) > 0)
        self.assertIn("你在做的是这件", matched[0])

        # 其他未在做的任务行不包含提示
        other = [ln for ln in lines if "辅助数据整理" in ln]
        self.assertTrue(len(other) > 0)
        self.assertNotIn("你在做的是这件", other[0])

    def test_render_board_max_lines_limit(self):
        """任务超多时，看板严格限制在 40 行以内并截断"""
        pt = self._add_part("超大批量任务集")
        curr_tk = None
        for i in range(50):
            tk = self._add_task(f"子任务编号_{i}", part=pt)
            if i == 5:
                curr_tk = tk

        st = Station(self.em1, home=self.home)
        st.conn = self.conn
        st.ws = self.ws
        st.agent = "codex"
        st._refresh()

        board_text = render_board(st.state, self.conv, current_task=curr_tk, max_lines=40)
        lines = board_text.splitlines()
        self.assertLessEqual(len(lines), 40)
        self.assertTrue(any("还有" in ln and "件" in ln for ln in lines))
        self.assertIn("你在做的是这件", board_text)

    def test_executor_cli_xay2_board(self):
        """执行者使用 xay2 board 命令直接打印 board.md"""
        with tempfile.TemporaryDirectory() as td:
            out_dir = Path(td) / ".xay" / "out"
            out_dir.mkdir(parents=True, exist_ok=True)
            board_file = out_dir / "board.md"
            board_file.write_text("大任务：测试项目\n第 1 步：实现功能\n  - tk_01：实现功能 · Codex 1 · 在做 · （你在做的是这件）\n", encoding="utf-8")

            env = dict(os.environ)
            env["XAY_ROLE"] = "executor"
            env["XAY2_EM"] = self.em1
            env["XAY2_BOARD_FILE"] = str(board_file)

            cmd = [str(V2_DIR / "bin" / "xay2"), "board"]
            res = subprocess.run(cmd, env=env, capture_output=True, text=True)
            self.assertEqual(res.returncode, 0, res.stderr)
            self.assertIn("大任务：测试项目", res.stdout)
            self.assertIn("你在做的是这件", res.stdout)

            # 当 board.md 不存在时打印友好提示
            board_file.unlink()
            res2 = subprocess.run(cmd, env=env, capture_output=True, text=True)
            self.assertEqual(res2.returncode, 0)
            self.assertIn("目前还没有任务板", res2.stdout)

    def test_employee_manual_has_instructions(self):
        """员工手册中包含关于 xay2 board 和 xay2 plan add 的两句指引"""
        manual_path = V2_DIR / "docs" / "员工手册.md"
        self.assertTrue(manual_path.exists())
        text = manual_path.read_text(encoding="utf-8")
        self.assertIn("xay2 board", text)
        self.assertIn("xay2 plan add", text)


if __name__ == "__main__":
    unittest.main()
