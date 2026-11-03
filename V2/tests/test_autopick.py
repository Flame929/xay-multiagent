"""测试没指定用哪种 AI 时的自动挑选逻辑（pick_agent_kind 与管家排程联动）。"""
import unittest
from unittest import mock

from xay.office.scheduler import Scheduler, pick_agent_kind



# 现在还有预留的通用员工（opencode 等），这里的“都装了”只指三种原有的 AI
BASE3 = lambda a: a in ("codex", "dsh", "agy")

class TestAutoPick(unittest.TestCase):
    def setUp(self):
        self.conv = "cv_test_01"
        self.ws = "ws_test_01"

    def test_rule1_same_part_most_used_agent(self):
        """规则 1：同一个中任务（part）里别的活用过的 AI，用得最多的那种。撤回的任务不算。"""
        state = {
            "convs": {self.conv: {"id": self.conv, "ws": self.ws}},
            "parts": {"pt_01": {"id": "pt_01", "key": "backend", "conv": self.conv}},
            "employees": {
                "em_1": {"key": "em_1", "ws": self.ws, "agent": "agy", "state": "idle"},
                "em_2": {"key": "em_2", "ws": self.ws, "agent": "dsh", "state": "idle"},
            },
            "tasks": {
                # 别的活 1 用了 agy（通过 owner）
                "tk_p1": {"id": "tk_p1", "conv": self.conv, "part": "pt_01", "owner": "em_1", "status": "done"},
                # 别的活 2 用了 agy（通过 agent 字段）
                "tk_p2": {"id": "tk_p2", "conv": self.conv, "part": "pt_01", "agent": "agy", "status": "working"},
                # 别的活 3 用了 dsh
                "tk_p3": {"id": "tk_p3", "conv": self.conv, "part": "pt_01", "agent": "dsh", "status": "ready"},
                # 撤回的活用了 codex：不算
                "tk_p4": {"id": "tk_p4", "conv": self.conv, "part": "pt_01", "agent": "codex", "status": "canceled"},
            }
        }
        # 新任务属于同一个 part
        task = {"id": "tk_new", "conv": self.conv, "ws": self.ws, "part": "pt_01", "status": "ready"}
        chosen = pick_agent_kind(state, task, is_available=BASE3)
        self.assertEqual(chosen, "agy")

    def test_rule2_idle_employee_preference(self):
        """规则 2：有空着的员工按 dsh -> agy -> codex 顺序优先挑选。"""
        # 1. dsh 和 agy 都有空员工：选 dsh
        state = {
            "convs": {self.conv: {"id": self.conv, "ws": self.ws}},
            "employees": {
                "em_dsh": {"key": "em_dsh", "ws": self.ws, "agent": "dsh", "state": "idle"},
                "em_agy": {"key": "em_agy", "ws": self.ws, "agent": "agy", "state": "idle"},
                "em_codex": {"key": "em_codex", "ws": self.ws, "agent": "codex", "state": "idle"},
            },
            "tasks": {}
        }
        task = {"id": "tk_new", "conv": self.conv, "ws": self.ws, "status": "ready"}
        self.assertEqual(pick_agent_kind(state, task, is_available=BASE3), "dsh")

        # 2. 只有 agy 和 codex 有空闲员工：选 agy
        state["employees"].pop("em_dsh")
        self.assertEqual(pick_agent_kind(state, task, is_available=BASE3), "agy")

        # 3. 只有 codex 有空闲员工：选 codex
        state["employees"].pop("em_agy")
        self.assertEqual(pick_agent_kind(state, task, is_available=BASE3), "codex")

    def test_readonly_task_never_picks_codex(self):
        """只读活（readonly）绝不选 codex。"""
        state = {
            "convs": {self.conv: {"id": self.conv, "ws": self.ws}},
            "employees": {
                "em_codex": {"key": "em_codex", "ws": self.ws, "agent": "codex", "state": "idle"},
            },
            "tasks": {}
        }
        # 只有 codex 可用，但任务是只读的
        task_ro = {"id": "tk_ro", "conv": self.conv, "ws": self.ws, "status": "ready", "readonly": True}
        # 即使命令行可用，由于只读排除 codex，返回 None
        self.assertIsNone(pick_agent_kind(state, task_ro, is_available=lambda a: a == "codex"))

        # 如果同时有 dsh，即使同 part 历史是 codex，只读活也绝不选 codex，选 dsh
        state["parts"] = {"pt_01": {"id": "pt_01", "conv": self.conv}}
        state["tasks"]["tk_old"] = {"id": "tk_old", "conv": self.conv, "part": "pt_01", "agent": "codex", "status": "done"}
        task_ro["part"] = "pt_01"
        chosen = pick_agent_kind(state, task_ro, is_available=lambda a: a in ("codex", "dsh"))
        self.assertEqual(chosen, "dsh")

    def test_excluded_when_quota_exhausted_or_capped(self):
        """额度线路全用完、被停、命令行未装时被排除。"""
        state = {
            "convs": {self.conv: {"id": self.conv, "ws": self.ws}},
            "employees": {
                "em_dsh": {"key": "em_dsh", "ws": self.ws, "agent": "dsh", "state": "idle"},
                "em_agy": {"key": "em_agy", "ws": self.ws, "agent": "agy", "state": "idle"},
            },
            "tasks": {}
        }
        task = {"id": "tk_new", "conv": self.conv, "ws": self.ws, "status": "ready"}

        # 1. dsh 线路额度用完：排除 dsh，回落选 agy
        chosen = pick_agent_kind(state, task, lines_out={"dsh": "额度用尽"}, is_available=lambda a: a in ("dsh", "agy"))
        self.assertEqual(chosen, "agy")

        # 2. 额度用完或调用次数用完导致全部排除：返回 None
        chosen = pick_agent_kind(state, task, agy_capped=True, lines_out={"dsh": "额度用尽", "codex": "额度用尽"}, is_available=BASE3)
        self.assertIsNone(chosen)

        # 3. 命令行未装：排除不可用的，选可用的
        chosen = pick_agent_kind(state, task, is_available=lambda a: a == "codex")
        self.assertEqual(chosen, "codex")

    def test_rule3_slots_free_for_hiring(self):
        """规则 3：没有空闲员工时，有空名额招新的种类按 dsh -> agy -> codex 顺序优先选择。"""
        # 所有员工都在忙
        state = {
            "convs": {self.conv: {"id": self.conv, "ws": self.ws}},
            "employees": {
                "em_dsh": {"key": "em_dsh", "ws": self.ws, "agent": "dsh", "state": "working", "turn": "tr_1"},
                "em_agy": {"key": "em_agy", "ws": self.ws, "agent": "agy", "state": "working", "turn": "tr_2"},
            },
            "tasks": {}
        }
        task = {"id": "tk_new", "conv": self.conv, "ws": self.ws, "status": "ready"}
        # 还有空名额（总忙碌 2 < 8）：按顺序挑能招新的 dsh
        chosen = pick_agent_kind(state, task, is_available=BASE3)
        self.assertEqual(chosen, "dsh")

    def test_rule4_shortest_queue_when_full(self):
        """规则 4：都满了时，选择排队最短的那种 AI。"""
        state = {
            "convs": {self.conv: {"id": self.conv, "ws": self.ws}},
            "employees": {
                # 8 个员工占满名额
                "em_dsh1": {"key": "em_dsh1", "ws": self.ws, "agent": "dsh", "state": "working", "turn": "tr_1", "queue": ["tk_q1", "tk_q2"]},
                "em_dsh2": {"key": "em_dsh2", "ws": self.ws, "agent": "dsh", "state": "working", "turn": "tr_2", "queue": ["tk_q3"]}, # dsh 共 3 件排队
                "em_agy1": {"key": "em_agy1", "ws": self.ws, "agent": "agy", "state": "working", "turn": "tr_3", "queue": ["tk_q4"]}, # agy 仅 1 件排队
                "em_agy2": {"key": "em_agy2", "ws": self.ws, "agent": "agy", "state": "working", "turn": "tr_4", "queue": []},
                "em_c1": {"key": "em_c1", "ws": self.ws, "agent": "codex", "state": "working", "turn": "tr_5", "queue": ["tk_q5", "tk_q6"]},
                "em_c2": {"key": "em_c2", "ws": self.ws, "agent": "codex", "state": "working", "turn": "tr_6", "queue": ["tk_q7", "tk_q8"]},
                "em_c3": {"key": "em_c3", "ws": self.ws, "agent": "codex", "state": "working", "turn": "tr_7", "queue": []},
                "em_c4": {"key": "em_c4", "ws": self.ws, "agent": "codex", "state": "working", "turn": "tr_8", "queue": []},
            },
            "tasks": {
                f"tk_q{i}": {"id": f"tk_q{i}", "status": "queued"} for i in range(1, 9)
            }
        }
        # mock scheduler 报告 slots_free 均为 False
        sched_mock = mock.Mock()
        sched_mock._slots_free.return_value = False
        sched_mock.line_out.return_value = None
        sched_mock.agy_capped = False
        sched_mock._busy.return_value = True
        sched_mock._excluded.return_value = None

        task = {"id": "tk_new", "conv": self.conv, "ws": self.ws, "status": "ready"}
        chosen = pick_agent_kind(state, task, scheduler=sched_mock, is_available=BASE3)
        # agy 队列排队任务数最少（1 件），应选择 agy
        self.assertEqual(chosen, "agy")


if __name__ == "__main__":
    unittest.main()
