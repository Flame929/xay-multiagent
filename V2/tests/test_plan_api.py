"""GET /api/v2/plan 对话任务拆解树接口与依赖关系字段的测试。

测试内容：
- 起 OfficeServer；
- /api/v2/plan 返回 tree.parts，mine 全是 false；
- items 带 deps 和 part 字段；
- 不存在的对话返回 404；没传 conv 时沿用默认对话；
- /api/v2/employee 原来结果不变，tree 里也多了 deps / part 两个字段。
"""
import unittest

from tests.test_office_web import CONV, DEPT, EM, WS, OfficeCase
from xay.core import events as core_events


class PlanApiTests(OfficeCase):
    def setUp(self):
        super().setUp()
        lead = "lead:" + CONV
        A = core_events.append
        # 添加小任务 pt_p000001
        A(self.conn, "part.added", lead, {"title": "前端模块", "after": [], "order": 1},
          ws=WS, conv=CONV, subject="pt_p000001")
        # 为已有任务 tk_t0000001 设定 owner 为 EM
        A(self.conn, "task.assigned", lead, {"owner": EM, "reason": "安排"},
          ws=WS, conv=CONV, subject="tk_t0000001")
        # 添加依赖 tk_t0000001 的任务 tk_t0000002，属于小任务 pt_p000001
        A(self.conn, "task.added", lead,
          {"title": "写单元测试", "owner": EM, "dept": DEPT, "part": "pt_p000001",
           "deps": ["tk_t0000001"], "writes": [], "card": "card2", "mode": "write", "reason": "测试"},
          ws=WS, conv=CONV, subject="tk_t0000002")
        # 添加零散任务 tk_t0000003（无 part），依赖 tk_t0000002
        A(self.conn, "task.added", lead,
          {"title": "零散补充任务", "owner": None, "dept": None, "part": None,
           "deps": ["tk_t0000002"], "writes": [], "card": "card3", "mode": "write", "reason": "零散"},
          ws=WS, conv=CONV, subject="tk_t0000003")

    def test_plan_api_structure_and_mine_false(self):
        status, body = self.get_json(f"/api/v2/plan?conv={CONV}")
        self.assertEqual(status, 200)
        self.assertEqual(body["conv"], CONV)
        self.assertEqual(body["title"], "测试对话")
        tree = body["tree"]
        self.assertIn("parts", tree)
        self.assertIn("groups", tree)

        # 检查所有 part 的 mine 均为 False
        for p in tree["parts"]:
            self.assertFalse(p["mine"], f"part {p['id']} 的 mine 应该为 False")

        # 检查 groups 和 parts 中的所有 item，mine 全为 False，且都包含 deps 和 part
        all_items = []
        for g in tree["groups"]:
            self.assertFalse(g["mine"])
            all_items.extend(g["items"])
        for p in tree["parts"]:
            all_items.extend(p.get("items", []))
            for d in p.get("depts", []):
                all_items.extend(d.get("items", []))

        item_map = {it["id"]: it for it in all_items}
        self.assertIn("tk_t0000001", item_map)
        self.assertIn("tk_t0000002", item_map)
        self.assertIn("tk_t0000003", item_map)

        for it in all_items:
            self.assertFalse(it["mine"], f"item {it['id']} 的 mine 应该为 False")
            self.assertIn("deps", it)
            self.assertIn("part", it)

        # 检查具体字段值
        t1 = item_map["tk_t0000001"]
        self.assertEqual(t1["deps"], [])
        self.assertEqual(t1["part"], "")

        t2 = item_map["tk_t0000002"]
        self.assertEqual(t2["deps"], ["tk_t0000001"])
        self.assertEqual(t2["part"], "pt_p000001")

        t3 = item_map["tk_t0000003"]
        self.assertEqual(t3["deps"], ["tk_t0000002"])
        self.assertEqual(t3["part"], "")

    def test_plan_api_default_conv(self):
        # 没传 conv 时沿用默认对话
        status, body = self.get_json("/api/v2/plan")
        self.assertEqual(status, 200)
        self.assertEqual(body["conv"], CONV)
        self.assertEqual(body["title"], "测试对话")
        self.assertIn("tree", body)

    def test_plan_api_not_found(self):
        status, body = self.get_json("/api/v2/plan?conv=cv_nonexistent")
        self.assertEqual(status, 404)

    def test_employee_api_has_deps_and_part_and_mine_preserved(self):
        status, body = self.get_json(f"/api/v2/employee?em={EM}&conv={CONV}")
        self.assertEqual(status, 200)
        tree = body.get("tree")
        self.assertIsNotNone(tree)
        self.assertIn("groups", tree)
        self.assertIn("parts", tree)

        # 检验 employee 接口下，分配给 EM 的任务其 mine 为 True
        emp_items = []
        for g in tree["groups"]:
            emp_items.extend(g["items"])
        for p in tree["parts"]:
            emp_items.extend(p.get("items", []))
            for d in p.get("depts", []):
                emp_items.extend(d.get("items", []))

        item_map = {it["id"]: it for it in emp_items}
        self.assertTrue(item_map["tk_t0000001"]["mine"])
        self.assertTrue(item_map["tk_t0000002"]["mine"])
        self.assertFalse(item_map["tk_t0000003"]["mine"])

        # deps 与 part 字段依然存在
        self.assertEqual(item_map["tk_t0000001"]["deps"], [])
        self.assertEqual(item_map["tk_t0000002"]["deps"], ["tk_t0000001"])
        self.assertEqual(item_map["tk_t0000002"]["part"], "pt_p000001")


if __name__ == "__main__":
    unittest.main()
