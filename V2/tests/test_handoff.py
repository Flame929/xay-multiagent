"""中途交接与负责人变更（references/plans/arch-v11-router-parity.md §3）测试。"""
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

from tests.test_cli_smoke import CliCase, BIN
from xay.core import events as core_events
from xay.core import humanize as core_humanize
from xay.core import snapshot as core_snapshot
from xay.core import load_state, open_db


class HandoffTests(CliCase):
    def setUp(self):
        super().setUp()
        # 创建一个测试对话
        self.conv = self.ok('begin', '--title', '三家员工', conv=False)

    def test_handoff_elements_and_line_limit(self):
        # 1. 建立小任务与任务
        self.ok('plan', 'part', '第一阶段', '--key', 'pt_1', '--why', '完成基础接口')
        self.ok('hire', 'codex', '--label', 'Codex 1')
        self.ok('hire', 'dsh', '--label', 'DeepSeek 1')
        self.ok('plan', 'add', '开发核心接口', '--to', 'Codex 1', '--part', 'pt_1')
        self.ok('plan', 'add', '编写单元测试', '--to', 'DeepSeek 1', '--part', 'pt_1')

        # 模拟执行与进展
        conn = open_db(self.db_path())
        try:
            state = load_state(conn)
            tk_map = {t['title']: t['id'] for t in state['tasks'].values()}
            t1 = tk_map['开发核心接口']
            t2 = tk_map['编写单元测试']
            em_c = state['tasks'][t1]['owner']
            em_d = state['tasks'][t2]['owner']

            # t1 完成
            core_events.append(conn, "turn.started", "station:" + em_c, {"task": t1, "em": em_c, "kind": "first"},
                               subject="tn_t1", ws=self.ws, conv=self.conv)
            core_events.append(conn, "turn.ended", "station:" + em_c, {
                "em": em_c, "status": "completed", "evidence": {"cli_status": "completed", "changed_files": ["a.py"]}
            }, subject="tn_t1", ws=self.ws, conv=self.conv)
            core_events.append(conn, "task.landed", "station:" + em_c, {"files": ["a.py"], "add": 10, "del": 0},
                               subject=t1, ws=self.ws, conv=self.conv)

            # t2 在做第 1/2 步
            core_events.append(conn, "turn.started", "station:" + em_d, {"task": t2, "em": em_d, "kind": "first"},
                               subject="tn_t2", ws=self.ws, conv=self.conv)
            core_events.append(conn, "task.progress", "station:" + em_d, {"step": 1, "of": 2, "text": "写测试"},
                               subject=t2, ws=self.ws, conv=self.conv)

            # 建立待处理的“需要你”
            core_events.append(conn, "decision.opened", "office", {
                "title": "是否合并改动", "detail": "请确认", "kind": "mismatch", "options": ["merge", "discard"]
            }, subject="dc_1", ws=self.ws, conv=self.conv)
        finally:
            conn.close()

        # 执行 xay2 handoff
        out = self.ok("handoff")
        lines = out.strip().splitlines()

        # 必须 ≤ 60 行
        self.assertLessEqual(len(lines), 60, f"交接单行数超过 60 行，实际为 {len(lines)} 行")

        # 检查各要素
        self.assertIn("【负责人交接单】", out)
        self.assertIn("大任务：三家员工", out)
        self.assertIn("总进度：", out)
        self.assertIn("待处理事项：", out)
        self.assertIn("需要你决定：是否合并改动", out)
        self.assertIn("任务与分工：", out)
        self.assertIn("第一阶段", out)
        self.assertIn("开发核心接口", out)
        self.assertIn("编写单元测试", out)
        self.assertIn("最近动向：", out)
        self.assertIn("当前游标：", out)
        self.assertIn(f"xay2 wait --conv {self.conv} --since", out)

    def test_handoff_with_to_lead_change(self):
        # 初始负责人是 claude
        conn = open_db(self.db_path())
        try:
            state = load_state(conn)
            self.assertEqual(state["convs"][self.conv]["lead_kind"], "claude")
        finally:
            conn.close()

        # 执行 handoff --to codex
        out = self.ok("handoff", "--to", "codex")
        self.assertIn("负责人：已移交给 Codex", out)

        # 验证 state 中的 lead_kind 已变更
        conn = open_db(self.db_path())
        try:
            state_after = load_state(conn)
            self.assertEqual(state_after["convs"][self.conv]["lead_kind"], "codex")

            # 验证生成的事件 conversation.lead_changed
            rows = conn.execute("SELECT seq, type, by, data FROM events WHERE type='conversation.lead_changed'").fetchall()
            self.assertEqual(len(rows), 1)
            import json
            ev_data = json.loads(rows[0][3])
            self.assertEqual(ev_data, {"from": "claude", "to": "codex"})

            # 验证 humanize 文案
            ev = {"type": "conversation.lead_changed", "data": ev_data, "by": "lead:" + self.conv, "conv": self.conv}
            tx = core_humanize.text(ev, conn, state=state_after)
            self.assertEqual(tx, "负责人从 Claude 换成了 Codex")
        finally:
            conn.close()

    def test_brief_first_line_includes_progress_pct(self):
        # 增加一个完成任务，总进度应当为 100%
        self.ok('hire', 'codex', '--label', 'Codex 1')
        self.ok('plan', 'add', '单个任务', '--to', 'Codex 1')
        conn = open_db(self.db_path())
        try:
            state = load_state(conn)
            tk_id = list(state['tasks'].keys())[0]
            em = state['tasks'][tk_id]['owner']
            core_events.append(conn, "turn.started", "station:" + em, {"task": tk_id, "em": em, "kind": "first"},
                               subject="tn_1", ws=self.ws, conv=self.conv)
            core_events.append(conn, "turn.ended", "station:" + em, {
                "em": em, "status": "completed", "evidence": {"cli_status": "completed", "changed_files": []}
            }, subject="tn_1", ws=self.ws, conv=self.conv)
            core_events.append(conn, "task.landed", "station:" + em, {"files": [], "add": 0, "del": 0},
                               subject=tk_id, ws=self.ws, conv=self.conv)
        finally:
            conn.close()

        brief = self.ok("brief")
        first_line = brief.splitlines()[0]
        self.assertIn("总进度 100%", first_line)

    def test_events_permission_and_validation(self):
        conn = open_db(self.db_path())
        lead = "lead:" + self.conv
        try:
            # 非法情况：缺少 to
            with self.assertRaises(ValueError):
                core_events.append(conn, "conversation.lead_changed", lead, {"from": "claude"},
                                   ws=self.ws, conv=self.conv, subject=self.conv)
            # 非法情况：对话不存在
            with self.assertRaises(ValueError):
                core_events.append(conn, "conversation.lead_changed", "lead:cv_nonexistent",
                                   {"from": "claude", "to": "codex"},
                                   ws=self.ws, conv="cv_nonexistent", subject="cv_nonexistent")
        finally:
            conn.close()


if __name__ == "__main__":
    unittest.main()
