"""命令行冒烟测试：用子进程驱动 V2/bin/xay2，核对它和 V2/docs/protocol.md 是否一致。

- 每个测试一个临时 XAY2_HOME + 一个临时 git 仓库当工作目录；环境变量里的真实会话变量
  （CLAUDE_CODE_SESSION_ID / CODEX_THREAD_ID / XAY2_CONV / XAY_ROLE / XAY2_TASK）一律先清掉，绝不碰真实 ~/.xay。
- 需要"造"一些只有管家/工位才会写的事件时，直接用 core.append 在测试进程里写进同一个临时账本。
- 以前写成"已知差异"（skipTest）的 12 处，协议 1.4 定稿、命令行修好后都改成了正常断言
  （测试名里还留着 gap，方便对照当时的问题）。
"""
import json
import os
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

V2_ROOT = Path(__file__).resolve().parents[1]
if str(V2_ROOT) not in sys.path:
    sys.path.insert(0, str(V2_ROOT))

from xay.core import open_db, append, since, max_seq  # noqa: E402
from xay.core.ids import ws_for, new_conv  # noqa: E402

BIN = V2_ROOT / 'bin' / 'xay2'
# 这些变量会让命令行"猜"到当前对话/角色，必须先清掉
SCRUBBED_ENV = ('XAY2_HOME', 'XAY2_CONV', 'XAY2_TASK', 'XAY_ROLE', 'CLAUDE_CODE_SESSION_ID', 'CODEX_THREAD_ID',
                'XAY2_TASK_FILE', 'XAY2_OUTBOX', 'XAY2_EM')
OUTBOX_TYPES = {'progress', 'report', 'letter', 'proposal', 'notes'}  # 协议 §6


class CliCase(unittest.TestCase):
    """提供：临时账本 + 临时 git 仓库 + 跑 xay2 的小工具。本类自己没有测试。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        root = Path(self.tmp.name).resolve()
        self.home = root / 'xay2-home'
        self.repo = root / 'repo'
        self.repo.mkdir()
        subprocess.run(['git', 'init', '-q', str(self.repo)], check=True, capture_output=True)
        self.assertNotEqual(self.home, Path.home() / '.xay' / 'v2')  # 保险：绝不是真实账本
        self.conv = None
        self.ws = None

    # ---- 跑命令
    def env(self, conv=True, **extra):
        env = {k: v for k, v in os.environ.items() if k not in SCRUBBED_ENV}
        env['XAY2_HOME'] = str(self.home)
        env['PYTHONDONTWRITEBYTECODE'] = '1'
        if conv and self.conv:
            env['XAY2_CONV'] = self.conv
        for k, v in extra.items():
            if v is None:
                env.pop(k, None)
            else:
                env[k] = v
        return env

    def xay(self, *args, conv=True, timeout=30, **env_extra):
        return subprocess.run([str(BIN), *args], cwd=str(self.repo), env=self.env(conv, **env_extra),
                              text=True, capture_output=True, timeout=timeout)

    def ok(self, *args, **kw):
        r = self.xay(*args, **kw)
        self.assertEqual(r.returncode, 0, 'xay2 %s 应成功，实际 rc=%s\nstdout=%s\nstderr=%s' % (' '.join(args), r.returncode, r.stdout, r.stderr))
        return r.stdout.strip()

    def snap(self):
        return json.loads(self.ok('snapshot', '--json'))

    # ---- 直接看/写临时账本
    def db_path(self):
        return str(self.home / 'xay.db')

    def events(self):
        conn = open_db(self.db_path())
        try:
            return since(conn, 0, limit=100000)
        finally:
            conn.close()

    def ledger_max_seq(self):
        conn = open_db(self.db_path())
        try:
            return max_seq(conn)
        finally:
            conn.close()

    def inject(self, typ, by, data, subject, conv='__current__', ts=None):
        """在测试进程里往同一个临时账本写一条事件（造管家/工位才会写的事件）。"""
        conn = open_db(self.db_path())
        try:
            return append(conn, typ, by, data, ws=self.ws, conv=self.conv if conv == '__current__' else conv, subject=subject, ts=ts)
        finally:
            conn.close()

    def seed_conv(self, title='种子对话'):
        """不走命令行直接开一个对话（省时间），并把它设为当前对话。"""
        self.ws = ws_for(self.repo)
        conv = new_conv()
        self.inject('conv.begun', 'lead:' + conv,
                    {'title': title, 'lead_kind': 'claude', 'aliases': [], 'cwd': str(self.repo)}, conv, conv=conv)
        self.conv = conv
        return conv

    def seed_employee_and_task(self, em='em_000000a1', tk='tk_000000b1', key='carousel', title='做商品图轮播'):
        lead = 'lead:' + self.conv
        self.inject('employee.hired', lead, {'agent': 'codex'}, em)
        self.inject('task.added', lead, {'key': key, 'title': title, 'deps': [], 'writes': [], 'mode': 'write', 'reason': '种子'}, tk)
        self.inject('task.assigned', lead, {'owner': em, 'reason': '种子'}, tk)
        return em, tk

    # ---- 发件箱
    def outbox_lines(self):
        path = self.repo / '.xay' / 'out' / 'outbox.jsonl'
        if not path.exists():
            return []
        return [json.loads(line) for line in path.read_text(encoding='utf-8').splitlines() if line.strip()]


# =============================================================== 主流程

class CliFlowTests(CliCase):
    def test_full_flow(self):
        # --- begin
        conv = self.ok('begin', '--title', '冒烟测试', conv=False)
        self.assertRegex(conv, r'^cv_\d{8}-\d{6}_[0-9a-f]{4}$')
        self.conv = conv
        self.assertEqual([e['type'] for e in self.events()], ['conv.begun'])

        # --- dept open
        self.ok('dept', 'open', 'fe', '--name', '前端部', '--mission', '做商品页', '--owns', 'web/**')

        # --- plan add × 2（第二个用 --after 指向第一个的 key）
        tk1 = self.ok('plan', 'add', '做商品图轮播', '--key', 'carousel', '--dept', 'fe', '--writes', 'web/**',
                      '--step', '写页面', '--step', '写样式', '--reason', '先做最重要的一块')
        tk2 = self.ok('plan', 'add', '给轮播写测试', '--key', 'carousel-test', '--dept', 'fe',
                      '--after', 'carousel', '--reason', '轮播做完再补测试')
        self.assertRegex(tk1, r'^tk_[0-9a-f]{8}$')
        self.assertRegex(tk2, r'^tk_[0-9a-f]{8}$')
        self.assertNotEqual(tk1, tk2)

        s = self.snap()
        self.assertEqual(s['conversation'], {'id': conv, 'title': '冒烟测试', 'lead_kind': 'claude', 'plan_version': 2})
        self.assertEqual(s['plan']['version'], 2)
        self.assertEqual([h['version'] for h in s['plan']['history']], [1, 2])
        h1, h2 = s['plan']['history']
        self.assertEqual((h1['by'], h1['reason']), ('gm', '先做最重要的一块'))
        self.assertEqual(h1['changes'], [{'op': 'add', 'task': tk1, 'title': '做商品图轮播',
                                          'after': {'title': '做商品图轮播', 'owner': None, 'dept': 'fe', 'deps': []}}])
        self.assertEqual((h2['by'], h2['reason']), ('gm', '轮播做完再补测试'))
        self.assertEqual(h2['changes'][0]['op'], 'add')
        self.assertEqual(h2['changes'][0]['task'], tk2)
        self.assertIn(h2['changes'][0]['after']['deps'], (['carousel'], [tk1]))  # deps 可以是 key 或 tk（§4.2）
        tasks = {t['id']: t for t in s['tasks']}
        self.assertEqual(set(tasks), {tk1, tk2})
        self.assertEqual((tasks[tk1]['key'], tasks[tk1]['status'], tasks[tk1]['dept'], tasks[tk1]['writes']),
                         ('carousel', 'planned', 'fe', ['web/**']))
        self.assertIn(tasks[tk2]['deps'], (['carousel'], [tk1]))
        self.assertEqual([(d['id'], d['name'], d['mission']) for d in s['depts']], [('fe', '前端部', '做商品页')])

        # --- 招一个员工，再 plan move（带 --reason）
        em = self.ok('hire', 'codex')
        self.assertRegex(em, r'^em_[0-9a-f]{8}$')
        self.ok('plan', 'move', 'carousel', '--to', em, '--reason', 'Codex 擅长前端')
        s = self.snap()
        self.assertEqual((s['people'][em]['label'], s['people'][em]['agent'], s['people'][em]['state']), ('Codex 1', 'codex', 'idle'))
        self.assertEqual(s['plan']['version'], 3)
        h3 = s['plan']['history'][2]
        self.assertEqual((h3['version'], h3['by'], h3['reason']), (3, 'gm', 'Codex 擅长前端'))
        self.assertEqual(h3['changes'], [{'op': 'assign', 'task': tk1, 'title': '做商品图轮播',
                                          'before': {'owner': None}, 'after': {'owner': em}}])
        t1 = next(t for t in s['tasks'] if t['id'] == tk1)
        self.assertEqual((t1['owner'], t1['assigned_reason']), (em, 'Codex 擅长前端'))

        # --- 执行者：只写桌子里的发件箱，不写账本（§6）
        seq_before_executor = self.ledger_max_seq()
        self.ok('progress', '1/2', '写页面', XAY_ROLE='executor', XAY2_TASK=tk1)
        self.assertEqual(self.outbox_lines(), [{'v': 1, 'task': tk1, 'type': 'progress', 'data': {'step': 1, 'of': 2, 'text': '写页面'}}])
        r = self.xay('plan', 'propose', '--reason', '让轮播改由别人做', 'assign', 'carousel', '--to', 'em_00000000',
                     XAY_ROLE='executor', XAY2_TASK=tk1)
        self.assertEqual(self.ledger_max_seq(), seq_before_executor, '执行者的命令（含被拒的 plan propose）不能写账本：%r' % r.stderr)
        self.assertEqual([e for e in self.events() if e['type'] == 'proposal.made'], [])

        # --- 负责人的申请流程：写进账本 → answer accept
        pp = self.ok('plan', 'propose', '--reason', 'Codex 更适合写测试', 'assign', 'carousel-test', '--to', em)
        self.assertRegex(pp, r'^pp_[0-9a-f]{8}$')
        s = self.snap()
        self.assertEqual([p['id'] for p in s['proposals']], [pp])
        self.assertEqual(s['proposals'][0]['status'], 'open')
        self.assertEqual(s['plan']['version'], 3, '申请还没通过，计划版本不能动')

        self.ok('answer', pp, 'accept', '同意')
        s = self.snap()
        self.assertEqual(s['proposals'], [])
        self.assertEqual(s['plan']['version'], 4)
        self.assertEqual(s['conversation']['plan_version'], 4)
        self.assertEqual(len(s['plan']['history']), 4)
        h4 = s['plan']['history'][3]
        self.assertEqual((h4['version'], h4['by']), (4, 'gm'))
        self.assertEqual(h4['reason'], 'Codex 更适合写测试（申请 %s 通过）' % pp)
        self.assertEqual(h4['changes'], [{'op': 'assign', 'task': tk2, 'title': '给轮播写测试',
                                          'before': {'owner': None}, 'after': {'owner': em}}])
        self.assertEqual(next(t for t in s['tasks'] if t['id'] == tk2)['owner'], em)

        # --- 被拒绝的申请：只关申请，不动计划
        pp2 = self.ok('plan', 'propose', '--reason', '这个测试不用了', 'remove', 'carousel-test')
        self.ok('answer', pp2, 'reject', '先留着')
        s = self.snap()
        self.assertEqual(s['proposals'], [])
        self.assertEqual(s['plan']['version'], 4)
        self.assertEqual(len(s['plan']['history']), 4)
        self.assertEqual(next(t for t in s['tasks'] if t['id'] == tk2)['status'], 'planned')

        # --- 账本里的事件正好是这些命令该写的那些
        events = self.events()
        self.assertEqual([e['seq'] for e in events], list(range(1, 11)))
        self.assertEqual([e['type'] for e in events],
                         ['conv.begun', 'dept.opened', 'task.added', 'task.added', 'employee.hired', 'task.assigned',
                          'proposal.made', 'proposal.decided', 'proposal.made', 'proposal.decided'])
        self.assertEqual({e['by'] for e in events}, {'lead:' + conv})
        self.assertEqual({e['conv'] for e in events}, {conv})
        self.assertEqual(len({e['ws'] for e in events}), 1)
        self.assertEqual([e['data']['verdict'] for e in events if e['type'] == 'proposal.decided'], ['accept', 'reject'])

        # --- wait：没有新事件 → 退出码 3 + "无新信号"
        cursor = s['seq']
        self.assertEqual(cursor, 10)
        started = time.time()
        r = self.xay('wait', '--since', str(cursor), '--timeout', '1', timeout=20)
        self.assertEqual(r.returncode, 3, 'wait 超时无事件应退出 3：%r %r' % (r.stdout, r.stderr))
        self.assertIn('无新信号', r.stdout)
        self.assertGreaterEqual(time.time() - started, 0.8, 'wait 应该真的阻塞到超时')

        # --- brief：≤25 行纯文本，且是对话现状
        brief = self.ok('brief')
        lines = brief.splitlines()
        self.assertLessEqual(len(lines), 25)
        self.assertIn('冒烟测试', brief)
        self.assertIn('做商品图轮播', brief)
        self.assertFalse(brief.lstrip().startswith('{'), 'brief 应是纯文本，不是 JSON')

        # --- events 命令：逐行 JSON，seq 连续
        out = self.ok('events', '--since', '0')
        self.assertEqual([json.loads(line)['seq'] for line in out.splitlines()], list(range(1, 11)))

        # --- rebuild：重放后快照与重放前一致（§0.3）
        before = self.snap()
        self.assertIn('已重建', self.ok('rebuild'))
        after = self.snap()
        for x in (before, after):
            x.pop('now')
        self.assertEqual(before, after)


class CliPlanTests(CliCase):
    def test_brief_team_only_lists_people_in_this_conversation(self):
        conv_a = self.seed_conv('甲对话')
        em_a, _ = self.seed_employee_and_task(em='em_aaaaaaa1', tk='tk_aaaaaaa1')
        idle = 'em_aaaaaaa2'
        self.inject('employee.hired', 'lead:' + conv_a, {'agent': 'codex', 'label': '没活员工'}, idle)
        self.inject('dept.opened', 'lead:' + conv_a, {'name': '前端部', 'mission': '', 'owns': [], 'head': 'rules'}, 'fe')
        member = 'em_aaaaaaa3'
        self.inject('employee.hired', 'lead:' + conv_a, {'agent': 'codex', 'label': '本部门成员'}, member)
        self.inject('employee.joined', 'lead:' + conv_a, {'dept': 'fe'}, member)

        conv_b = self.seed_conv('乙对话')
        em_b, _ = self.seed_employee_and_task(em='em_bbbbbbb1', tk='tk_bbbbbbb1', key='other', title='另一对话的活')
        self.assertNotEqual(em_a, em_b)

        out = self.ok('--conv', conv_a, 'brief', conv=False)
        team_line = next(line for line in out.splitlines() if line.startswith('团队：'))
        self.assertIn('Codex 1', team_line)
        self.assertIn('本部门成员', team_line)
        self.assertNotIn('没活员工', team_line)
        self.assertNotIn('Codex 2', team_line)

    def test_plan_edit_and_drop_write_history(self):
        self.seed_conv('改计划')
        tk = self.ok('plan', 'add', '旧标题', '--key', 'k1', '--writes', 'a/**', '--reason', '先加')
        self.ok('plan', 'edit', 'k1', '--title', '新标题', '--reason', '换个说法')
        s = self.snap()
        self.assertEqual(s['plan']['version'], 2)
        h = s['plan']['history'][1]
        self.assertEqual((h['by'], h['reason']), ('gm', '换个说法'))
        self.assertEqual(h['changes'], [{'op': 'change', 'task': tk, 'title': '新标题',
                                         'before': {'title': '旧标题'}, 'after': {'title': '新标题'}}])
        self.assertEqual(s['tasks'][0]['writes'], ['a/**'], 'plan edit 只改给出的字段')
        self.ok('plan', 'drop', 'k1', '--reason', '不做了')
        s = self.snap()
        self.assertEqual(s['plan']['version'], 3)
        self.assertEqual(s['plan']['history'][2]['changes'],
                         [{'op': 'remove', 'task': tk, 'title': '新标题', 'before': {'status': 'planned'}}])
        self.assertEqual(s['tasks'][0]['status'], 'canceled')

    def test_brief_stays_within_25_lines_for_a_big_plan(self):
        self.seed_conv('大计划')
        plan = [{'key': 'k%02d' % i, 'title': '任务%02d' % i, 'deps': [], 'writes': [], 'mode': 'write', 'reason': '批量'} for i in range(30)]
        plan_file = Path(self.tmp.name) / 'plan.json'
        plan_file.write_text(json.dumps(plan, ensure_ascii=False), encoding='utf-8')
        self.ok('plan', 'apply', str(plan_file))
        s = self.snap()
        self.assertEqual(len(s['tasks']), 30)
        brief = self.ok('brief')
        self.assertLessEqual(len(brief.splitlines()), 25, 'brief 必须 ≤25 行，实际 %d 行' % len(brief.splitlines()))
        self.assertIn('大计划', brief)

    def test_gap_plan_apply_adds_a_single_plan_version(self):
        """§7：plan apply 一批 task.added，一次只加一次版本。"""
        self.seed_conv('批量加')
        plan = [{'key': 'k%d' % i, 'title': '任务%d' % i, 'deps': [], 'writes': [], 'mode': 'write', 'reason': '批量'} for i in range(3)]
        plan_file = Path(self.tmp.name) / 'plan.json'
        plan_file.write_text(json.dumps(plan, ensure_ascii=False), encoding='utf-8')
        self.ok('plan', 'apply', str(plan_file))
        s = self.snap()
        self.assertEqual(len(s['tasks']), 3)
        self.assertEqual(s['plan']['version'], 1, 'plan apply 一批只加一次版本')
        self.assertEqual(len(s['plan']['history']), 1)
        h = s['plan']['history'][0]
        self.assertEqual((h['version'], h['by'], h['reason']), (1, 'gm', '批量'))
        self.assertEqual([c['op'] for c in h['changes']], ['add', 'add', 'add'])
        self.assertEqual([c['title'] for c in h['changes']], ['任务0', '任务1', '任务2'])
        added = [e for e in self.events() if e['type'] == 'task.added']
        self.assertEqual(len({e['data']['batch'] for e in added}), 1, '三条 task.added 带同一个 data.batch')
        # 之后单独加一件：新的一版
        self.ok('plan', 'add', '单独加', '--reason', '再加一件')
        self.assertEqual(self.snap()['plan']['version'], 2)

    def test_gap_propose_move_is_recorded_as_a_valid_op(self):
        """§7 写的是 `plan propose … move <任务> --to <…>`，§4.2 的 op 只有 add|change|remove|assign。"""
        self.seed_conv()
        em, tk = self.seed_employee_and_task()
        pp = self.ok('plan', 'propose', '--reason', '换人', 'move', 'carousel', '--to', 'em_000000a2')
        proposal = next(e for e in self.events() if e['type'] == 'proposal.made' and e['subject'] == pp)
        self.assertEqual(proposal['data']['changes'], [{'op': 'assign', 'task': tk, 'fields': {'owner': 'em_000000a2'}}],
                         '§7：move 到员工 = op assign')
        self.ok('answer', pp, 'accept')
        s = self.snap()
        self.assertEqual(next(t for t in s['tasks'] if t['id'] == tk)['owner'], 'em_000000a2')
        h = s['plan']['history'][-1]
        self.assertEqual(h['changes'], [{'op': 'assign', 'task': tk, 'title': '做商品图轮播',
                                         'before': {'owner': em}, 'after': {'owner': 'em_000000a2'}}])

    def test_propose_move_to_a_department_is_a_dept_change(self):
        """§7：move 到部门 = op change{dept}；plan move 到部门 = task.changed{dept}。"""
        self.seed_conv()
        em, tk = self.seed_employee_and_task()
        self.ok('dept', 'open', 'fe', '--name', '前端部')
        pp = self.ok('plan', 'propose', '--reason', '归前端部', 'move', 'carousel', '--to', 'fe')
        proposal = next(e for e in self.events() if e['subject'] == pp)
        self.assertEqual(proposal['data']['changes'], [{'op': 'change', 'task': tk, 'fields': {'dept': 'fe'}}])
        self.ok('dept', 'open', 'be', '--name', '后端部')
        self.ok('plan', 'move', 'carousel', '--to', 'be', '--reason', '其实是后端的活')
        last = self.events()[-1]
        self.assertEqual((last['type'], last['data']), ('task.changed', {'fields': {'dept': 'be'}, 'reason': '其实是后端的活'}))
        self.assertEqual(next(t for t in self.snap()['tasks'] if t['id'] == tk)['dept'], 'be')

    def test_gap_plan_add_new_and_agent_flags(self):
        """§7：`plan add … [--to Codex1|--new] [--agent codex|dsh|agy|auto]`，--new 应招一个新员工来接。"""
        self.seed_conv('招人')
        tk = self.ok('plan', 'add', '新活', '--new', '--agent', 'codex', '--reason', '要新人')
        events = self.events()
        self.assertEqual([e['type'] for e in events][-2:], ['employee.hired', 'task.added'])
        hired = events[-2]
        self.assertEqual((hired['by'], hired['data']), ('lead:' + self.conv, {'agent': 'codex'}))
        s = self.snap()
        task = next(t for t in s['tasks'] if t['id'] == tk)
        self.assertEqual(task['owner'], hired['subject'])
        self.assertEqual(s['people'][hired['subject']]['label'], 'Codex 1')
        # --to 用显示名找已有员工
        tk2 = self.ok('plan', 'add', '再来一件', '--to', 'Codex 1')
        self.assertEqual(next(t for t in self.snap()['tasks'] if t['id'] == tk2)['owner'], hired['subject'])
        # 只说用哪种 AI（不配 --new / --to）：任务记下 agent、不定人，交给管家按选人规则挑（M4）
        tk3 = self.ok('plan', 'add', '交给管家挑人', '--agent', 'dsh')
        added = self.events()[-1]
        self.assertEqual((added['type'], added['subject'], added['data'].get('agent'), added['data'].get('owner')), ('task.added', tk3, 'dsh', None))
        # 不说用哪个 AI 就招人、--to 和 --agent 一起写、找不到人：报错，什么都不写
        before = self.ledger_max_seq()
        for args in (('plan', 'add', 'x', '--new'), ('plan', 'add', 'x', '--to', 'Codex 1', '--agent', 'dsh'), ('plan', 'add', 'x', '--to', 'Codex 9')):
            r = self.xay(*args)
            self.assertEqual(r.returncode, 2, args)
        self.assertEqual(self.ledger_max_seq(), before)

    def test_resume_and_brief_show_the_kill_switch(self):
        """总闸（M4）：负责人拉的，负责人能 xay2 resume；用户在页面上拉的，命令行不能替他解除；brief 第二行写明总闸。"""
        self.seed_conv('总闸')
        r = self.xay('resume')
        self.assertEqual(r.returncode, 2)                          # 没拉下，不用恢复
        self.ok('stop', '--all')
        self.assertIn('总闸：已拉下', self.ok('brief').splitlines()[1])
        self.ok('resume')
        self.assertIsNone(self.snap().get('halt'))
        self.inject('control.requested', 'user', {'action': 'stop_all', 'by_source': 'page'}, None)
        before = self.ledger_max_seq()
        r = self.xay('resume')
        self.assertEqual(r.returncode, 2)
        self.assertIn('页面', r.stderr)
        self.assertEqual(self.ledger_max_seq(), before)
        self.assertIn('用户在页面上点', self.ok('brief'))

    def test_gap_plan_add_card_reads_file(self):
        """§7：`--card 文件|-`，任务卡内容来自文件或标准输入。"""
        self.seed_conv('任务卡')
        card = Path(self.tmp.name) / 'card.md'
        card.write_text('# 任务卡正文\n请做这件事。\n', encoding='utf-8')
        self.ok('plan', 'add', '带卡的活', '--card', str(card), '--reason', '测卡')
        added = [e for e in self.events() if e['type'] == 'task.added'][0]
        self.assertEqual(added['data']['card'], '# 任务卡正文\n请做这件事。\n')
        # --card - 从标准输入读
        r = subprocess.run([str(BIN), 'plan', 'add', '标准输入的卡', '--card', '-'], cwd=str(self.repo), env=self.env(),
                           input='来自标准输入的卡', text=True, capture_output=True, timeout=30)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(self.events()[-1]['data']['card'], '来自标准输入的卡')
        # 文件不存在：报错，不写
        before = self.ledger_max_seq()
        self.assertEqual(self.xay('plan', 'add', 'x', '--card', str(card) + '.nope').returncode, 2)
        self.assertEqual(self.ledger_max_seq(), before)


class CliCompactTests(CliCase):
    def test_compact_by_employee_label_writes_manual_request(self):
        self.seed_conv()
        em, _ = self.seed_employee_and_task()
        self.ok('compact', 'Codex 1', '--method', 'handoff', '--reason', '上下文太长')
        event = self.events()[-1]
        self.assertEqual(event['type'], 'context.compact_requested')
        self.assertEqual(event['subject'], em)
        self.assertEqual(event['data'], {'reason': 'manual', 'method': 'handoff', 'note': '上下文太长'})

    def test_compact_unknown_employee_writes_nothing(self):
        self.seed_conv()
        before = self.ledger_max_seq()
        r = self.xay('compact', '不存在的人')
        self.assertEqual(r.returncode, 2)
        self.assertIn('找不到员工', r.stderr)
        self.assertEqual(self.ledger_max_seq(), before)


class CliCompactExecutorTests(CliCase):
    def setUp(self):
        super().setUp()
        self.seed_conv()
        self.em, self.tk = self.seed_employee_and_task()
        self.seq = self.ledger_max_seq()

    def test_executor_cannot_compact(self):
        r = self.xay('compact', self.em, XAY_ROLE='executor', XAY2_TASK=self.tk)
        self.assertEqual(r.returncode, 2)
        self.assertIn('执行者只能用', r.stderr)
        self.assertEqual(self.ledger_max_seq(), self.seq)
        self.assertEqual(self.outbox_lines(), [])


class CliNotesSetTests(CliCase):
    def test_notes_set_from_file_and_stdin_with_label_and_em(self):
        self.seed_conv()
        em, _ = self.seed_employee_and_task()

        # 1. 员工显示名 + 文件
        note_file = Path(self.tmp.name) / 'note.txt'
        note_file.write_text('这是从文件读进来的笔记内容', encoding='utf-8')
        out = self.ok('notes-set', 'Codex 1', str(note_file))
        self.assertIn('Codex 1 更新了工作笔记', out)
        event = self.events()[-1]
        self.assertEqual(event['type'], 'notes.updated')
        self.assertEqual(event['subject'], em)
        self.assertEqual(event['by'], 'lead:' + self.conv)
        self.assertEqual(event['data'], {'active': '这是从文件读进来的笔记内容'})

        # 2. 员工编号 + 标准输入
        r = subprocess.run([str(BIN), 'notes-set', em, '-'], cwd=str(self.repo), env=self.env(),
                           input='来自标准输入的完整笔记', text=True, capture_output=True, timeout=30)
        self.assertEqual(r.returncode, 0, r.stderr)
        event2 = self.events()[-1]
        self.assertEqual(event2['type'], 'notes.updated')
        self.assertEqual(event2['subject'], em)
        self.assertEqual(event2['data'], {'active': '来自标准输入的完整笔记'})

        # 3. 员工不存在
        before = self.ledger_max_seq()
        r = self.xay('notes-set', '不存在的人', str(note_file))
        self.assertEqual(r.returncode, 2)
        self.assertIn('找不到员工', r.stderr)
        self.assertEqual(self.ledger_max_seq(), before)

        # 4. 文件不存在
        r = self.xay('notes-set', em, str(note_file) + '.nope')
        self.assertEqual(r.returncode, 2)
        self.assertIn('读不到笔记文件', r.stderr)
        self.assertEqual(self.ledger_max_seq(), before)

    def test_executor_cannot_notes_set(self):
        self.seed_conv()
        em, tk = self.seed_employee_and_task()
        seq = self.ledger_max_seq()
        note_file = Path(self.tmp.name) / 'note.txt'
        note_file.write_text('x', encoding='utf-8')
        r = self.xay('notes-set', em, str(note_file), XAY_ROLE='executor', XAY2_TASK=tk)
        self.assertEqual(r.returncode, 2)
        self.assertIn('执行者只能用', r.stderr)
        self.assertEqual(self.ledger_max_seq(), seq)


# =============================================================== 执行者：只写发件箱

class CliExecutorTests(CliCase):
    def setUp(self):
        super().setUp()
        self.seed_conv()
        self.em, self.tk = self.seed_employee_and_task()
        self.seq = self.ledger_max_seq()

    def run_exec(self, *args, **extra):
        env = {'XAY_ROLE': 'executor', 'XAY2_TASK': self.tk}
        env.update(extra)
        return self.xay(*args, **env)

    def test_allowed_commands_append_one_outbox_line_each_and_never_touch_the_ledger(self):
        cases = [
            (('progress', '2/4', '写测试'), 'progress', {'step': 2, 'of': 4, 'text': '写测试'}),
            (('done', '做完了'), 'report', {'claim': 'done', 'summary': '做完了'}),
            (('notes', '全量笔记内容'), 'notes', {'kind': 'notes', 'op': 'set', 'text': '全量笔记内容', 'em': self.em}),
            (('notes', '--add', '追加一条'), 'notes', {'kind': 'notes', 'op': 'add', 'text': '追加一条', 'em': self.em}),
            (('notes', '--remove', '待删内容'), 'notes', {'kind': 'notes', 'op': 'remove', 'text': '待删内容', 'em': self.em}),
            (('tell', 'fe', '提醒一下', '--subject', '小提醒'), 'letter', {'to': 'fe', 'kind': 'tell', 'subject': '小提醒', 'body': '提醒一下'}),
            (('ask', 'gm', '能不能改接口', '--subject', '问一下'), 'letter', {'to': 'gm', 'kind': 'request', 'subject': '问一下', 'body': '能不能改接口'}),
            (('reply', 'lt_00000001', '好的'), 'letter', {'reply_to': 'lt_00000001', 'kind': 'reply', 'body': '好的'}),
        ]
        for n, (args, kind, expected) in enumerate(cases, 1):
            with self.subTest(command=args[0]):
                extra = {'XAY2_EM': self.em} if args[0] == 'notes' else {}
                r = self.run_exec(*args, **extra)
                self.assertEqual(r.returncode, 0, r.stderr)
                lines = self.outbox_lines()
                self.assertEqual(len(lines), n, '每个命令恰好追加一行')
                line = lines[-1]
                self.assertEqual((line['v'], line['task'], line['type']), (1, self.tk, kind))
                self.assertIn(line['type'], OUTBOX_TYPES)
                for k, v in expected.items():
                    self.assertEqual(line['data'].get(k), v, '%s 的 data.%s' % (args[0], k))
                self.assertEqual(self.ledger_max_seq(), self.seq, '执行者命令不能写账本')
        raw = (self.repo / '.xay' / 'out' / 'outbox.jsonl').read_text(encoding='utf-8')
        self.assertTrue(raw.endswith('\n'))
        self.assertTrue(all(len(line.encode('utf-8')) <= 4096 for line in raw.splitlines()), '单行 ≤4 KB')

    def test_other_commands_are_refused_for_executors(self):
        forbidden = [('begin', '--title', 'x'), ('hire', 'codex'), ('plan', 'add', '越权加任务'),
                     ('accept', 'carousel'), ('answer', 'pp_00000001', 'accept')]
        for args in forbidden:
            with self.subTest(command=' '.join(args)):
                r = self.run_exec(*args)
                self.assertNotEqual(r.returncode, 0, '执行者不该能跑：%s' % ' '.join(args))
                self.assertEqual(self.ledger_max_seq(), self.seq)
                self.assertEqual(self.outbox_lines(), [])

    def test_executor_without_task_writes_nothing(self):
        r = self.xay('progress', '1/2', 'x', XAY_ROLE='executor')
        self.assertNotEqual(r.returncode, 0)
        self.assertEqual(self.outbox_lines(), [])
        self.assertEqual(self.ledger_max_seq(), self.seq)

    def test_notes_without_task_succeeds_but_other_commands_refused(self):
        """没有任务时 notes 只要有 XAY2_EM 就能写发件箱；其他执行者命令没有任务照旧被拒。"""
        # 没有任务但有 XAY2_EM：notes 可以正常写发件箱，task 为空
        r = self.xay('notes', '空闲时记笔记', XAY_ROLE='executor', XAY2_EM=self.em)
        self.assertEqual(r.returncode, 0, r.stderr)
        lines = self.outbox_lines()
        self.assertEqual(len(lines), 1)
        self.assertEqual(lines[0]['task'], '')
        self.assertEqual(lines[0]['em'], self.em)
        self.assertEqual(lines[0]['data'], {'kind': 'notes', 'op': 'set', 'text': '空闲时记笔记', 'em': self.em})

        # 没有任务也没有 XAY2_EM：notes 报错
        r = self.xay('notes', '谁写的', XAY_ROLE='executor')
        self.assertEqual(r.returncode, 2)
        self.assertIn('不知道你是谁', r.stderr)
        self.assertEqual(len(self.outbox_lines()), 1)

        # 没有任务时其他执行者命令照旧被拒
        other_cmds = [
            ('progress', '1/2', '做活'),
            ('done', '做完'),
            ('tell', 'fe', '话'),
            ('ask', 'gm', '问'),
            ('reply', 'lt_00000001', '回'),
        ]
        for cmd in other_cmds:
            with self.subTest(cmd=cmd[0]):
                res = self.xay(*cmd, XAY_ROLE='executor', XAY2_EM=self.em)
                self.assertEqual(res.returncode, 2)
                self.assertIn('不知道你在做哪件事', res.stderr)
                self.assertEqual(len(self.outbox_lines()), 1, '被拒的命令不写发件箱')

    def test_gap_executor_plan_propose_goes_to_the_outbox(self):
        """§3/§7：执行者可用 propose，只写发件箱（type=proposal），不写账本。"""
        r = self.run_exec('plan', 'propose', '--reason', '这活更适合别人', 'assign', 'carousel', '--to', 'em_000000a2')
        # 前置（现在就成立）：账本没被写
        self.assertEqual(self.ledger_max_seq(), self.seq)
        self.assertEqual([e for e in self.events() if e['type'] == 'proposal.made'], [])
        lines = self.outbox_lines()
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(lines, [{'v': 1, 'task': self.tk, 'type': 'proposal',
                                  'data': {'changes': [{'op': 'assign', 'task': 'carousel', 'fields': {'owner': 'em_000000a2'}}],
                                           'reason': '这活更适合别人', 'to': 'gm'}}])
        # 有 XAY2_EM 时带上 from；move 到部门短名 = change{dept}
        r = self.xay('plan', 'propose', '--reason', '归前端', 'move', 'carousel', '--to', 'fe',
                     XAY_ROLE='executor', XAY2_TASK=self.tk, XAY2_EM=self.em)
        self.assertEqual(r.returncode, 0, r.stderr)
        line = self.outbox_lines()[-1]
        self.assertEqual(line['data']['changes'], [{'op': 'change', 'task': 'carousel', 'fields': {'dept': 'fe'}}])
        self.assertEqual(line['data']['from'], self.em)
        self.assertEqual(self.ledger_max_seq(), self.seq)

    def test_executor_outbox_env_and_missing_task_message(self):
        """§6：XAY2_OUTBOX 指定发件箱位置；没有 XAY2_TASK 报"不知道你在做哪件事"。"""
        box = Path(self.tmp.name) / 'desk' / '.xay' / 'out' / 'outbox.jsonl'
        r = self.xay('done', '好了', XAY_ROLE='executor', XAY2_TASK=self.tk, XAY2_OUTBOX=str(box))
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(json.loads(box.read_text(encoding='utf-8'))['data'], {'claim': 'done', 'summary': '好了'})
        self.assertEqual(self.outbox_lines(), [], '给了 XAY2_OUTBOX 就不写 git 根下的发件箱')
        r = self.xay('progress', '1/2', 'x', XAY_ROLE='executor')
        self.assertEqual(r.returncode, 2)
        self.assertIn('不知道你在做哪件事', r.stderr)

    def test_gap_executor_commands_do_not_open_the_ledger(self):
        """§3：执行者命令只写发件箱，不碰数据库。全新的 XAY2_HOME 下跑一条，不应出现 xay.db。"""
        fresh = Path(self.tmp.name) / 'fresh-home'
        r = subprocess.run([str(BIN), 'progress', '1/2', '不碰库'], cwd=str(self.repo), text=True, capture_output=True, timeout=30,
                           env=dict(self.env(conv=False), XAY2_HOME=str(fresh), XAY_ROLE='executor', XAY2_TASK=self.tk))
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(len(self.outbox_lines()), 1)
        self.assertFalse(fresh.exists(), '执行者命令不能打开（更不能新建）账本：%s' % (sorted(p.name for p in fresh.glob('*')) if fresh.exists() else []))
        # 被拒的命令同样不碰账本
        r = subprocess.run([str(BIN), 'brief'], cwd=str(self.repo), text=True, capture_output=True, timeout=30,
                           env=dict(self.env(conv=False), XAY2_HOME=str(fresh), XAY_ROLE='executor', XAY2_TASK=self.tk))
        self.assertEqual(r.returncode, 2)
        self.assertFalse(fresh.exists())


# =============================================================== 谁是当前对话（§1）

class CliConversationTests(CliCase):
    def test_no_conversation_at_all_is_an_error_that_points_to_begin(self):
        r = self.xay('brief', conv=False)
        self.assertNotEqual(r.returncode, 0)
        self.assertIn('xay2 begin', r.stdout + r.stderr)

    def test_explicit_conv_beats_env_and_env_is_honoured(self):
        a = self.seed_conv('甲对话')
        b = self.seed_conv('乙对话')
        self.assertNotEqual(a, b)
        self.assertIn('甲对话', self.ok('--conv', a, 'brief', conv=False, XAY2_CONV=b))
        self.assertIn('乙对话', self.ok('brief', conv=False, XAY2_CONV=b))
        self.assertIn('甲对话', self.ok('brief', conv=False, XAY2_CONV=a))

    def test_gap_begin_registers_session_aliases(self):
        """§1：别名 claude:<CLAUDE_CODE_SESSION_ID> / codex:<CODEX_THREAD_ID> 能找回对话。"""
        a = self.ok('begin', '--title', '甲对话', conv=False, CLAUDE_CODE_SESSION_ID='sess-A', CODEX_THREAD_ID='thr-A')
        b = self.ok('begin', '--title', '乙对话', conv=False, CLAUDE_CODE_SESSION_ID='sess-B', CODEX_THREAD_ID='thr-B')
        self.assertNotEqual(a, b)
        self.ok('--conv', a, 'brief', conv=False)  # 前置：显式指定一直可用
        by_claude = self.xay('brief', conv=False, CLAUDE_CODE_SESSION_ID='sess-A')
        by_codex = self.xay('brief', conv=False, CODEX_THREAD_ID='thr-A')
        self.assertEqual(self.events()[0]['data']['aliases'], ['claude:sess-A', 'codex:thr-A'])
        self.assertEqual(by_claude.returncode, 0, by_claude.stderr)
        self.assertIn('甲对话', by_claude.stdout)
        self.assertEqual(by_codex.returncode, 0, by_codex.stderr)
        self.assertIn('甲对话', by_codex.stdout)
        self.assertIn('乙对话', self.ok('brief', conv=False, CLAUDE_CODE_SESSION_ID='sess-B'))
        # 别名已被别的对话占用：提示，不登记
        c = self.xay('begin', '--title', '丙对话', conv=False, CLAUDE_CODE_SESSION_ID='sess-A')
        self.assertEqual(c.returncode, 0, c.stderr)
        self.assertIn('claude:sess-A', c.stderr)
        self.assertEqual(self.events()[-1]['data']['aliases'], [])
        self.assertIn('甲对话', self.ok('brief', conv=False, CLAUDE_CODE_SESSION_ID='sess-A'))

    def test_gap_ambiguous_current_conversation_is_an_error(self):
        """§1：--conv / XAY2_CONV / 别名都没有 → 报错并提示先 xay2 begin；§0.7 不猜。"""
        self.seed_conv('甲对话')
        self.seed_conv('乙对话')
        r = self.xay('brief', conv=False)
        self.assertEqual(r.returncode, 2, r.stdout)
        self.assertIn('xay2 begin', r.stdout + r.stderr)
        # 只有一个对话也不猜
        self.assertEqual(self.xay('plan', 'add', 'x', conv=False).returncode, 2)
        # 不认识的会话变量 / XAY2_CONV：照样报错
        self.assertEqual(self.xay('brief', conv=False, CLAUDE_CODE_SESSION_ID='nobody').returncode, 2)
        self.assertEqual(self.xay('brief', conv=False, XAY2_CONV='cv_nope').returncode, 2)

    def test_latest_flag_picks_the_conversation_with_the_newest_event(self):
        """§1：snapshot/brief 可用 --latest 显式要最近的对话（= 最近有动静的那个，§8 v1.5）。"""
        a = self.seed_conv('甲对话')
        self.seed_conv('乙对话')
        self.assertIn('乙对话', self.ok('brief', '--latest', conv=False))
        self.inject('conv.titled', 'lead:' + a, {'title': '甲对话（刚改过）'}, a, conv=a)
        self.assertIn('甲对话（刚改过）', self.ok('brief', '--latest', conv=False))
        self.assertEqual(json.loads(self.ok('snapshot', '--json', '--latest', conv=False))['conversation']['id'], a)


# =============================================================== answer / accept

class CliAnswerTests(CliCase):
    def test_answer_decision_merge_moves_task_to_reported(self):
        self.seed_conv()
        em, tk = self.seed_employee_and_task()
        self.inject('task.kept', 'office', {'reason': 'mismatch', 'detail': '说做完了但没有改动'}, tk)
        self.inject('decision.opened', 'office',
                    {'kind': 'mismatch', 'title': '说做完了但没有改动', 'detail': '看看', 'task': tk, 'em': em, 'owner': 'lead',
                     'options': [{'id': 'merge', 'label': '合并改动'}, {'id': 'rework', 'label': '让他接着改'},
                                 {'id': 'discard', 'label': '丢掉改动', 'danger': True}]}, 'dc_00000001')
        s = self.snap()
        self.assertEqual([d['id'] for d in s['decisions']], ['dc_00000001'])
        self.assertEqual(s['tasks'][0]['status'], 'needs_you')
        self.assertEqual(s['people'][em]['state'], 'blocked')
        self.ok('answer', 'dc_00000001', 'merge')
        s = self.snap()
        self.assertEqual(s['decisions'], [])
        self.assertEqual(s['tasks'][0]['status'], 'reported')
        self.assertEqual(self.events()[-1]['type'], 'decision.resolved')
        self.assertEqual(self.events()[-1]['data']['option'], 'merge')

    def test_answer_letter_closes_request(self):
        self.seed_conv()
        em, tk = self.seed_employee_and_task()
        self.inject('letter.sent', 'user', {'from': em, 'to': 'gm', 'kind': 'request', 'subject': '问', 'body': '能改吗'}, 'lt_00000001')
        self.assertEqual(self.snap()['letters'][0]['status'], 'open')
        self.ok('answer', 'lt_00000001', 'accept', '可以')
        letter = self.snap()['letters'][0]
        self.assertEqual((letter['status'], letter['verdict']), ('closed', 'accept'))

    def test_accept_moves_reported_task_to_done(self):
        self.seed_conv()
        em, tk = self.seed_employee_and_task()
        self.inject('task.landed', 'office', {'files': ['a.py'], 'add': 3, 'del': 0}, tk)
        self.assertEqual(self.snap()['tasks'][0]['status'], 'reported')
        self.ok('accept', 'carousel', '--note', '看过了')
        self.assertEqual(self.snap()['tasks'][0]['status'], 'done')
        last = self.events()[-1]
        self.assertEqual((last['type'], last['by'], last['data']['note']), ('task.accepted', 'lead:' + self.conv, '看过了'))


# =============================================================== wait（§7 约定）

class CliWaitTests(CliCase):
    def setUp(self):
        super().setUp()
        self.seed_conv()
        self.em, self.tk = self.seed_employee_and_task()
        self.since = self.ledger_max_seq()

    def wait(self, since=None, timeout=1):
        return self.xay('wait', '--since', str(self.since if since is None else since), '--timeout', str(timeout), timeout=30)

    # ---- 会叫醒 wait 的事件：输出格式
    def test_wait_prints_protocol_lines_and_cursor(self):
        s_dc = self.inject('decision.opened', 'office',
                           {'kind': 'mismatch', 'title': '说做完了但没有改动', 'detail': '看看', 'task': self.tk, 'em': self.em, 'owner': 'lead',
                            'options': [{'id': 'merge', 'label': '合并改动'}, {'id': 'rework', 'label': '让他接着改'},
                                        {'id': 'discard', 'label': '丢掉改动', 'danger': True}]}, 'dc_00000001')
        s_landed = self.inject('task.landed', 'office', {'files': ['a.py', 'b.py', 'c.py'], 'add': 9, 'del': 1}, self.tk)
        s_signal = self.inject('signal.raised', 'office', {'kind': 'silent', 'level': 'warn', 'text': 'Codex 1 很久没动静了'}, self.em)
        s_pp = self.inject('proposal.made', 'employee:' + self.em,
                           {'from': self.em, 'to': 'gm', 'changes': [{'op': 'remove', 'task': self.tk}], 'reason': '这活不用做了'}, 'pp_00000001')
        s_lt = self.inject('letter.sent', 'employee:' + self.em,
                           {'from': self.em, 'to': 'gm', 'kind': 'request', 'subject': '问个事', 'body': '内容'}, 'lt_00000001')
        r = self.wait()
        self.assertEqual(r.returncode, 0, r.stderr)
        lines = r.stdout.splitlines()
        self.assertEqual(lines[0], '#%d 需要你：说做完了但没有改动（dc_00000001，选项：merge / rework / discard）' % s_dc)
        self.assertEqual(lines[1], '#%d 已合并：做商品图轮播（Codex 1，3 个文件）' % s_landed)
        self.assertEqual(lines[2], '#%d 提醒：Codex 1 很久没动静了' % s_signal)
        self.assertTrue(lines[3].startswith('#%d ' % s_pp), lines[3])
        self.assertTrue(lines[4].startswith('#%d ' % s_lt), lines[4])
        self.assertEqual(lines[-1], '游标 %d' % s_lt)
        self.assertEqual(len(lines), 6)
        # 用游标再等：没有新事件
        again = self.wait(since=s_lt)
        self.assertEqual(again.returncode, 3)
        self.assertIn('无新信号', again.stdout)

    def test_wait_ignores_other_conversations_and_non_wake_up_events(self):
        """别的对话的事件、以及不在 §7 清单里的事件（任务进度、上线等）都不叫醒 wait。"""
        other = new_conv()
        self.inject('conv.begun', 'lead:' + other, {'title': '别的对话', 'lead_kind': 'claude', 'aliases': [], 'cwd': str(self.repo)}, other, conv=other)
        self.inject('signal.raised', 'office', {'kind': 'silent', 'level': 'warn', 'text': '别的对话的提醒'}, 'em_other', conv=other)
        self.inject('task.progress', 'employee:' + self.em, {'step': 1, 'of': 2, 'text': '开始', 'source': 'outbox'}, self.tk)
        self.inject('employee.online', 'office', {}, self.em)
        r = self.wait()
        self.assertEqual(r.returncode, 3, '不该叫醒：%r' % r.stdout)

    # ---- 差异：不该叫醒负责人的事件，现在也叫醒了
    def _assert_not_woken_by(self, description, typ, by, data, subject):
        seq = self.inject(typ, by, data, subject)
        events = [e for e in self.events() if e['seq'] == seq]
        self.assertEqual(len(events), 1)  # 前置：事件确实写进去了
        r = self.wait()
        self.assertEqual(r.returncode, 3, '%s 不该叫醒 `xay2 wait --since %d`（§7 只在 %s 时叫醒），输出 %r'
                         % (typ + ' ' + description[0], self.since, description[1], r.stdout.splitlines()[:1]))

    def test_gap_wait_ignores_decisions_owned_by_the_user(self):
        self._assert_not_woken_by(('data.owner=user', 'decision.opened 的 owner=lead'), 'decision.opened', 'office',
                                  {'kind': 'question', 'title': '要不要上线', 'detail': '', 'owner': 'user',
                                   'options': [{'id': 'yes', 'label': '上线'}]}, 'dc_00000002')

    def test_gap_wait_ignores_remind_level_signals(self):
        self._assert_not_woken_by(('level=remind', 'signal.raised 的 level=warn'), 'signal.raised', 'office',
                                  {'kind': 'silent', 'level': 'remind', 'text': '小提醒'}, self.em)

    def test_gap_wait_ignores_proposals_not_addressed_to_gm(self):
        self._assert_not_woken_by(('to=fe', 'proposal.made 的 to=gm'), 'proposal.made', 'employee:' + self.em,
                                  {'from': self.em, 'to': 'fe', 'changes': [{'op': 'remove', 'task': self.tk}], 'reason': '给前端部的申请'}, 'pp_00000002')

    def test_gap_wait_ignores_letters_not_addressed_to_gm(self):
        self._assert_not_woken_by(('to=<员工>', 'letter.sent 的 to=gm'), 'letter.sent', 'user',
                                  {'from': 'user', 'to': self.em, 'kind': 'tell', 'subject': '给员工的信', 'body': 'x'}, 'lt_00000002')

    # ---- 差异：最多 15 条，但游标不能越过没显示的事件
    def test_gap_wait_cursor_does_not_skip_events_beyond_the_15_line_cap(self):
        # 20 条 warn 提醒，每条隔 11 分钟（"同类 10 分钟内合并"合并不到一起）
        base = 1_800_000_000
        seqs = [self.inject('signal.raised', 'office', {'kind': 'silent', 'level': 'warn', 'text': '警告%02d' % i}, self.em, ts=base + i * 660)
                for i in range(20)]
        first = self.wait()
        self.assertEqual(first.returncode, 0, first.stderr)
        shown = [ln for ln in first.stdout.splitlines() if ln.startswith('#')]
        self.assertLessEqual(len(shown), 15, '最多 15 条')
        self.assertGreaterEqual(len(shown), 1)
        self.assertTrue(first.stdout.splitlines()[-1].startswith('游标 '))
        cursor = int(first.stdout.splitlines()[-1].split()[-1])
        self.assertEqual(len(shown), 15)
        self.assertEqual(cursor, seqs[14], '游标 = 最后一条打印出来的事件')
        second = self.wait(since=cursor)
        self.assertEqual(second.returncode, 0, second.stdout)
        rest = [ln for ln in second.stdout.splitlines() if ln.startswith('#')]
        self.assertEqual([int(ln.split()[0][1:]) for ln in rest], seqs[15:])
        self.assertIn('警告19', second.stdout)
        self.assertEqual(second.stdout.splitlines()[-1], '游标 %d' % seqs[-1])

    def test_wait_merges_same_kind_within_ten_minutes_and_timeout_zero_returns_at_once(self):
        """同一员工同一种提醒 10 分钟内合并成一行，游标越过被合并的；--timeout 0 只查一次。"""
        base = 1_800_000_000
        seqs = [self.inject('signal.raised', 'office', {'kind': 'silent', 'level': 'warn', 'text': '很久没动静%d' % i}, self.em, ts=base + i * 60)
                for i in range(3)]
        r = self.wait()
        self.assertEqual(r.returncode, 0, r.stderr)
        lines = r.stdout.splitlines()
        self.assertEqual(len(lines), 2, lines)
        self.assertTrue(lines[0].startswith('#%d 提醒：很久没动静0' % seqs[0]), lines[0])
        self.assertIn('共 3 条', lines[0])
        self.assertEqual(lines[1], '游标 %d' % seqs[-1])
        started = time.time()
        r = self.xay('wait', '--since', str(seqs[-1]), '--timeout', '0', timeout=30)
        self.assertEqual(r.returncode, 3)
        self.assertIn('无新信号', r.stdout)
        self.assertLess(time.time() - started, 5, '--timeout 0 只查一次立刻返回')


if __name__ == '__main__':
    unittest.main()
