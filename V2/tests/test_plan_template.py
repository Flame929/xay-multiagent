"""负责人拆活模板与防卡死提醒测试（V2/tests/test_plan_template.py）：
1. xay2 plan template 打印合法 JSON 模板，带 _说明 中文注释，示范大白话、why、check、after 等，--title 自定义标题
2. plan apply --dry-run 填好示例值后通过，且忽略所有以 _ 开头的键
3. xay2 plan add 不带 --part 打印零散活提示
4. 防卡死提醒：plan add --part / plan edit --part 把未完成小任务放进已全部完成的中任务时，
   若后置中任务还有没开工的小任务，提示并要求加 --force 才写。
"""
import json
import unittest
from pathlib import Path

from tests.test_cli_smoke import CliCase
from xay.core import append, open_db, load_state


class PlanTemplateTests(CliCase):
    def test_plan_template_default_output(self):
        # 无需对话也可以生成模板
        r = self.xay('plan', 'template', conv=False)
        self.assertEqual(r.returncode, 0, f"plan template 失败: {r.stderr}")
        tpl = json.loads(r.stdout)
        self.assertIn("title", tpl)
        self.assertIn("_说明", tpl)
        self.assertIn("groups", tpl)
        self.assertEqual(len(tpl["groups"]), 2)

        # 检查两个示例中任务
        g1, g2 = tpl["groups"]
        self.assertTrue(g1.get("key"))
        self.assertTrue(g1.get("title"))
        self.assertTrue(g1.get("why"))
        self.assertIn("tasks", g1)
        self.assertEqual(len(g1["tasks"]), 2)
        self.assertIn("_说明", g1)

        self.assertTrue(g2.get("key"))
        self.assertTrue(g2.get("title"))
        self.assertTrue(g2.get("why"))
        self.assertIn("after", g2)
        self.assertIn(g1["key"], g2["after"])
        self.assertIn("tasks", g2)
        self.assertEqual(len(g2["tasks"]), 2)
        self.assertIn("_说明", g2)

        # 检查小任务字段
        for g in (g1, g2):
            for t in g["tasks"]:
                self.assertTrue(t.get("title"))
                self.assertIn(t.get("agent"), ("codex", "dsh", "agy"))
                self.assertTrue(t.get("writes"))
                self.assertTrue(t.get("verify"))
                self.assertTrue(t.get("card"))
                self.assertIn("_说明", t)

    def test_plan_template_custom_title(self):
        r = self.xay('plan', 'template', '--title', '构建高性能数据传输模块', conv=False)
        self.assertEqual(r.returncode, 0, f"plan template --title 失败: {r.stderr}")
        tpl = json.loads(r.stdout)
        self.assertEqual(tpl.get("title"), "构建高性能数据传输模块")

    def test_plan_template_apply_dry_run_success(self):
        conv = self.ok('begin', '--title', '测试模板导入', conv=False)
        self.conv = conv

        r = self.xay('plan', 'template')
        self.assertEqual(r.returncode, 0)
        tpl_path = self.repo / 'plan.json'
        tpl_path.write_text(r.stdout, encoding='utf-8')

        # dry-run 检查
        r_dry = self.xay('plan', 'apply', str(tpl_path), '--dry-run')
        self.assertEqual(r_dry.returncode, 0, f"dry-run 失败: {r_dry.stderr}")
        self.assertIn("大任务：", r_dry.stdout)
        self.assertIn("第 1 步：", r_dry.stdout)
        self.assertIn("第 2 步：", r_dry.stdout)

    def test_plan_apply_ignores_underscore_keys(self):
        conv = self.ok('begin', '--title', '测试忽略下划线键', conv=False)
        self.conv = conv

        r = self.xay('plan', 'template')
        tpl = json.loads(r.stdout)
        # 增加额外的 _ 开头字段
        tpl["_author"] = "负责人"
        tpl["_version_note"] = "第一版规划"
        tpl["groups"][0]["_group_comment"] = "后端分组注释"
        tpl["groups"][0]["tasks"][0]["_task_hint"] = "任务提示"

        tpl_path = self.repo / 'plan_with_underscores.json'
        tpl_path.write_text(json.dumps(tpl, ensure_ascii=False, indent=2), encoding='utf-8')

        r_apply = self.xay('plan', 'apply', str(tpl_path))
        self.assertEqual(r_apply.returncode, 0, f"apply 失败: {r_apply.stderr}")

        conn = open_db(self.home / 'xay.db')
        try:
            state = load_state(conn)
            parts = [p for p in state.get('parts', {}).values() if p.get('conv') == conv]
            tasks = [t for t in state.get('tasks', {}).values() if t.get('conv') == conv]
            self.assertEqual(len(parts), 2)
            self.assertEqual(len(tasks), 4)
            for p in parts:
                self.assertNotIn('_说明', p)
                self.assertNotIn('_group_comment', p)
            for t in tasks:
                self.assertNotIn('_说明', t)
                self.assertNotIn('_task_hint', t)
        finally:
            conn.close()

    def test_plan_add_without_part_notice(self):
        conv = self.ok('begin', '--title', '测试零散活提示', conv=False)
        self.conv = conv

        r = self.xay('plan', 'add', '实现用户基础注册逻辑', '--agent', 'codex')
        self.assertEqual(r.returncode, 0, f"plan add 失败: {r.stderr}")
        tk_id = r.stdout.strip()
        self.assertTrue(tk_id.startswith('tk_'))
        self.assertIn(
            "这件小任务不属于任何中任务，页面上会放进'零散的活'。建议先 xay2 plan part 开一个中任务，再用 --part 放进去（或用 xay2 plan template / plan apply 一次建好整份计划）",
            r.stderr
        )

    def test_deadlock_prevention_plan_add(self):
        conv = self.ok('begin', '--title', '测试防卡死plan_add', conv=False)
        self.conv = conv

        # 创建中任务 P1 和 P2（P2 依赖 P1）
        pt1 = self.ok('plan', 'part', '打通基础接口服务', '--why', '提供底层数据能力', '--key', 'p1')
        pt2 = self.ok('plan', 'part', '构建页面展示模块', '--why', '提供界面呈现', '--key', 'p2', '--after', 'p1')

        # 在 P1 建任务 T1，在 P2 建任务 T2
        tk1 = self.ok('plan', 'add', '编写接口处理函数', '--part', pt1, '--agent', 'codex')
        tk2 = self.ok('plan', 'add', '搭建前端展示组件', '--part', pt2, '--agent', 'agy')

        # 模拟 T1 已经完成（status = done）
        conn = open_db(self.home / 'xay.db')
        try:
            append(conn, 'task.landed', 'office', {'files': ['api.py']}, ws=self.ws, conv=conv, subject=tk1)
            append(conn, 'task.accepted', 'lead:' + conv, {}, ws=self.ws, conv=conv, subject=tk1)
        finally:
            conn.close()

        # 此时 P1 已经全部做完，P2 排在 P1 后面且 T2 尚未开工（status=planned）
        # 尝试往 P1 塞进未做完的新任务，应触发防卡死拦截
        r_add = self.xay('plan', 'add', '临时新增接口检查', '--part', pt1, '--agent', 'codex')
        self.assertEqual(r_add.returncode, 2)
        self.assertIn("这样会让『构建页面展示模块』里的小任务一直等着，建议放进还没做完的中任务", r_add.stderr)

        # 加 --force 允许强制写入
        r_forced = self.xay('plan', 'add', '临时新增接口检查', '--part', pt1, '--agent', 'codex', '--force')
        self.assertEqual(r_forced.returncode, 0, f"带 --force 应成功: {r_forced.stderr}")
        self.assertTrue(r_forced.stdout.strip().startswith('tk_'))

    def test_deadlock_prevention_plan_edit(self):
        conv = self.ok('begin', '--title', '测试防卡死plan_edit', conv=False)
        self.conv = conv

        pt1 = self.ok('plan', 'part', '打通基础接口服务', '--why', '提供底层数据能力', '--key', 'p1')
        pt2 = self.ok('plan', 'part', '构建页面展示模块', '--why', '提供界面呈现', '--key', 'p2', '--after', 'p1')

        tk1 = self.ok('plan', 'add', '编写接口处理函数', '--part', pt1, '--agent', 'codex')
        tk2 = self.ok('plan', 'add', '搭建前端展示组件', '--part', pt2, '--agent', 'agy')

        # 模拟 T1 完成
        conn = open_db(self.home / 'xay.db')
        try:
            append(conn, 'task.landed', 'office', {'files': ['api.py']}, ws=self.ws, conv=conv, subject=tk1)
            append(conn, 'task.accepted', 'lead:' + conv, {}, ws=self.ws, conv=conv, subject=tk1)
        finally:
            conn.close()

        # 建一个未完成的零散小任务
        tk_loose = self.ok('plan', 'add', '零散的数据校验任务', '--agent', 'codex')

        # 尝试 edit 把 tk_loose 移入 P1，应触发拦截
        r_edit = self.xay('plan', 'edit', tk_loose, '--part', pt1)
        self.assertEqual(r_edit.returncode, 2)
        self.assertIn("这样会让『构建页面展示模块』里的小任务一直等着，建议放进还没做完的中任务", r_edit.stderr)

        # 加 --force 允许移入
        r_edit_force = self.xay('plan', 'edit', tk_loose, '--part', pt1, '--force')
        self.assertEqual(r_edit_force.returncode, 0, f"带 --force 移入应成功: {r_edit_force.stderr}")

        # 如果一个任务已经 done 了，移入已完成的 P1 不应被拦截
        tk_done = self.ok('plan', 'add', '已经做完的配置任务', '--agent', 'codex')
        conn = open_db(self.home / 'xay.db')
        try:
            append(conn, 'task.landed', 'office', {'files': ['conf.py']}, ws=self.ws, conv=conv, subject=tk_done)
            append(conn, 'task.accepted', 'lead:' + conv, {}, ws=self.ws, conv=conv, subject=tk_done)
        finally:
            conn.close()

        r_edit_done = self.xay('plan', 'edit', tk_done, '--part', pt1)
        self.assertEqual(r_edit_done.returncode, 0, f"已做完的任务移入不应拦截: {r_edit_done.stderr}")


if __name__ == '__main__':
    unittest.main()
