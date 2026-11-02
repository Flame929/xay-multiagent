"""小任务命令行与分组展示测试（V2/xay/cli.py）：
1. plan part 写 part.added（subject pt_ + 8位，check 解析）
2. plan part-edit 写 part.changed
3. plan part-drop 写 part.removed（里面还有活时拒绝并原样报错）
4. plan add --part / dept open --part 写进对应的 part 字段
5. brief 有小任务时按小任务分组展示（做完 x/y），保持 <= 25 行；没有小任务时照旧
"""
import json
import unittest

from tests.test_cli_smoke import CliCase


class PartsCliTests(CliCase):
    def test_plan_part_creation_and_check_options(self):
        conv = self.ok('begin', '--title', '测试小任务', conv=False)
        self.conv = conv

        # 默认 check 为 rules
        pt1 = self.ok('plan', 'part', '第一阶段', '--key', 'p1', '--reason', '先做这个', '--why', '做完能看到效果')
        self.assertRegex(pt1, r'^pt_[0-9a-f]{8}$')

        evs = self.events()
        added1 = next(e for e in evs if e['type'] == 'part.added' and e['subject'] == pt1)
        self.assertEqual(added1['by'], 'lead:' + conv)
        self.assertEqual(added1['data']['title'], '第一阶段')
        self.assertEqual(added1['data']['key'], 'p1')
        self.assertEqual(added1['data']['after'], [])
        self.assertEqual(added1['data']['check'], {'kind': 'rules'})
        self.assertEqual(added1['data']['reason'], '先做这个')

        # ai:codex check
        pt2 = self.ok('plan', 'part', '第二阶段', '--after', 'p1', '--check', 'ai:codex', '--check-text', '检查无Bug', '--why', '做完能看到效果')
        added2 = next(e for e in self.events() if e['type'] == 'part.added' and e['subject'] == pt2)
        self.assertEqual(added2['data']['after'], ['p1'])
        self.assertEqual(added2['data']['check'], {'kind': 'ai', 'reviewer': 'codex', 'text': '检查无Bug'})

        # lead check
        pt3 = self.ok('plan', 'part', '第三阶段', '--check', 'lead', '--check-text', '总经理审', '--why', '做完能看到效果')
        added3 = next(e for e in self.events() if e['type'] == 'part.added' and e['subject'] == pt3)
        self.assertEqual(added3['data']['check'], {'kind': 'lead', 'text': '总经理审'})

    def test_plan_part_edit(self):
        self.seed_conv('小任务修改')
        pt = self.ok('plan', 'part', '初始标题', '--key', 'p_edit', '--why', '做完能看到效果')

        # 按编号修改
        self.ok('plan', 'part-edit', pt, '--title', '新标题1', '--after', 'k1,k2', '--check', 'rules', '--check-text', '说明')
        ch1 = next(e for e in reversed(self.events()) if e['type'] == 'part.changed' and e['subject'] == pt)
        self.assertEqual(ch1['data']['fields']['title'], '新标题1')
        self.assertEqual(ch1['data']['fields']['after'], ['k1', 'k2'])
        self.assertEqual(ch1['data']['fields']['check'], {'kind': 'rules', 'text': '说明'})

        # 按 key 修改
        self.ok('plan', 'part-edit', 'p_edit', '--title', '按Key改名')
        ch2 = next(e for e in reversed(self.events()) if e['type'] == 'part.changed' and e['subject'] == pt)
        self.assertEqual(ch2['data']['fields']['title'], '按Key改名')

        # 按标题修改
        self.ok('plan', 'part-edit', '按Key改名', '--title', '按标题改名')
        ch3 = next(e for e in reversed(self.events()) if e['type'] == 'part.changed' and e['subject'] == pt)
        self.assertEqual(ch3['data']['fields']['title'], '按标题改名')

        # 没有要改的内容时报错
        r = self.xay('plan', 'part-edit', pt)
        self.assertEqual(r.returncode, 2)
        self.assertIn('没有要改的内容', r.stderr)

    def test_plan_part_drop_and_rejection_with_tasks(self):
        self.seed_conv('小任务删除')
        pt1 = self.ok('plan', 'part', '空小任务', '--key', 'empty_pt', '--why', '做完能看到效果')

        # 没有活的小任务直接删除成功
        self.ok('plan', 'part-drop', 'empty_pt', '--reason', '不要了')
        rm1 = next(e for e in reversed(self.events()) if e['type'] == 'part.removed' and e['subject'] == pt1)
        self.assertEqual(rm1['data']['reason'], '不要了')

        # 有未撤回任务的小任务删除被拒绝，原样报错
        pt2 = self.ok('plan', 'part', '有活小任务', '--key', 'busy_pt', '--why', '做完能看到效果')
        tk = self.ok('plan', 'add', '活一件', '--part', 'busy_pt')

        r = self.xay('plan', 'part-drop', 'busy_pt')
        self.assertEqual(r.returncode, 2)
        self.assertIn('里还有未撤回的任务，不能删除', r.stderr)
        self.assertNotIn(pt2, [e['subject'] for e in self.events() if e['type'] == 'part.removed'])

        # 撤回任务后可以正常删除
        self.ok('plan', 'drop', tk, '--reason', '先撤掉任务')
        self.ok('plan', 'part-drop', 'busy_pt', '--reason', '任务撤完后删除小任务')
        self.assertIn(pt2, [e['subject'] for e in self.events() if e['type'] == 'part.removed'])

    def test_plan_add_and_dept_open_with_part(self):
        self.seed_conv('归属小任务')
        pt = self.ok('plan', 'part', '核心小任务', '--key', 'core', '--why', '做完能看到效果')

        # plan add --part
        tk = self.ok('plan', 'add', '核心活', '--part', 'core')
        t_event = next(e for e in self.events() if e['type'] == 'task.added' and e['subject'] == tk)
        self.assertEqual(t_event['data']['part'], pt)

        # dept open --part
        self.ok('dept', 'open', 'fe', '--name', '前端部', '--part', 'core')
        d_event = next(e for e in self.events() if e['type'] == 'dept.opened' and e['subject'] == 'fe')
        self.assertEqual(d_event['data']['part'], pt)

    def test_brief_grouping(self):
        self.seed_conv('多阶段排程')
        p1 = self.ok('plan', 'part', '阶段一', '--key', 'p1', '--why', '做完能看到效果')
        p2 = self.ok('plan', 'part', '阶段二', '--key', 'p2', '--why', '做完能看到效果')

        t1 = self.ok('plan', 'add', '活一', '--part', 'p1', '--key', 't1')
        t2 = self.ok('plan', 'add', '活二', '--part', 'p1', '--key', 't2')
        t3 = self.ok('plan', 'add', '活三', '--part', 'p2', '--key', 't3')
        t0 = self.ok('plan', 'add', '零散活', '--key', 't0')

        brief = self.ok('brief')
        lines = brief.splitlines()
        self.assertLessEqual(len(lines), 25)

        self.assertIn('中任务：阶段一（做完 0/2）', brief)
        self.assertIn('中任务：阶段二（做完 0/1）', brief)
        self.assertIn('中任务：零散的小任务（做完 0/1）', brief)
        self.assertIn('活一', brief)
        self.assertIn('活二', brief)
        self.assertIn('活三', brief)
        self.assertIn('零散活', brief)

        # 活一完成后变为 做完 1/2
        self.inject('task.landed', 'office', {'files': [], 'add': 0, 'del': 0}, t1)
        self.ok('accept', 't1')
        brief2 = self.ok('brief')
        self.assertIn('中任务：阶段一（做完 1/2）', brief2)

    def test_brief_without_parts_stays_as_before(self):
        self.seed_conv('无小任务对话')
        self.ok('plan', 'add', '普通活', '--key', 'normal')
        brief = self.ok('brief')
        self.assertNotIn('中任务：', brief)   # 2026-10-31 层级改名：分组行叫中任务
        self.assertIn('任务（共 1 件）：', brief)
        self.assertIn('普通活', brief)
