import tempfile
import unittest
import os
import subprocess
from pathlib import Path

from xay.core import open_db, append, since, max_seq, rebuild, resolve_conv
from xay.core.errors import PermissionError
from xay.core.evidence import verdict
from xay.core.events import load_state
from xay.core.snapshot import build
from xay.core.ids import ws_for


class CoreBehaviorTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.conn = open_db(str(Path(self.tmp.name) / 'xay.db'))
        self.ws, self.conv = 'ws_test', 'cv_test'
        append(self.conn, 'conv.begun', 'lead:cv_test', {'title': '测试', 'lead_kind': 'codex', 'aliases': ['codex:t1'], 'cwd': '/tmp'}, ws=self.ws, conv=self.conv, subject=self.conv, ts=1)
        append(self.conn, 'employee.hired', 'lead:cv_test', {'agent': 'codex'}, ws=self.ws, conv=self.conv, subject='em_00000001', ts=2)

    def tearDown(self):
        self.conn.close()
        self.tmp.cleanup()

    def test_identity_scope_dedup_and_rebuild(self):
        append(self.conn, 'task.added', 'lead:cv_test', {'title': '任务', 'owner': 'em_00000001', 'deps': [], 'writes': [], 'mode': 'write', 'reason': '测试'}, ws=self.ws, conv=self.conv, subject='tk_00000001', ts=3)
        append(self.conn, 'task.progress', 'employee:em_00000001', {'step': 1, 'of': 2, 'text': '开始', 'source': 'outbox'}, ws=self.ws, conv=self.conv, subject='tk_00000001', dedup='same', ts=4)
        seq = append(self.conn, 'task.progress', 'employee:em_00000001', {'step': 2, 'of': 2, 'text': '重复行', 'source': 'outbox'}, ws=self.ws, conv=self.conv, subject='tk_00000001', dedup='same', ts=5)
        self.assertEqual(seq, 4)
        with self.assertRaises(PermissionError):
            append(self.conn, 'task.progress', 'employee:em_00000001', {'step': 1, 'of': 2, 'text': '越权', 'source': 'outbox'}, ws=self.ws, conv=self.conv, subject='tk_other', ts=6)
        with self.assertRaises(PermissionError):
            append(self.conn, 'task.added', 'lead:cv_other', {'title': '越权'}, ws=self.ws, conv=self.conv, subject='tk_other', ts=7)
        before = build(self.conn, ws=self.ws, conv=self.conv, now=10)
        rebuild(self.conn)
        self.assertEqual(before, build(self.conn, ws=self.ws, conv=self.conv, now=10))
        self.assertEqual(max_seq(self.conn), 4)
        self.assertEqual(since(self.conn, 0)[-1]['data']['text'], '开始')

    def test_alias_resolution_and_sleeping(self):
        self.assertEqual(resolve_conv(self.conn, env={'CODEX_THREAD_ID': 't1'}), self.conv)
        self.assertEqual(build(self.conn, ws=self.ws, conv=self.conv, now=2000)['people']['em_00000001']['display_state'], 'sleeping')

    def test_evidence_verdict_table(self):
        # verdict(cli_status, claim, has_changes, mode) -> ("landed", None) | ("kept", reason) | ("failed", reason)；表见 protocol §4.3 v1.3
        self.assertEqual(verdict('completed', 'done', True, 'write'), ('landed', None))
        self.assertEqual(verdict('completed', 'done', False, 'write'), ('kept', 'mismatch'))
        self.assertEqual(verdict('completed', 'done', False, 'readonly'), ('landed', None))
        self.assertEqual(verdict('completed', 'done', True, 'readonly'), ('kept', 'mismatch'))
        self.assertEqual(verdict('failed', 'partial', True, 'write'), ('kept', 'failed_with_changes'))
        self.assertEqual(verdict('failed', 'done', False, 'write'), ('kept', 'mismatch'))
        self.assertEqual(verdict('failed', 'blocked', False, 'write')[0], 'failed')
        self.assertEqual(verdict('interrupted', 'partial', False, 'write'), ('kept', 'interrupted'))

    def test_wait_exit_codes(self):
        env = dict(os.environ, XAY2_HOME=self.tmp.name, XAY2_CONV=self.conv)
        root = Path(__file__).parents[1]
        timeout = subprocess.run([str(root / 'bin' / 'xay2'), 'wait', '--timeout', '0'], cwd=self.tmp.name, env=env, text=True, capture_output=True)
        self.assertEqual(timeout.returncode, 3)
        append(self.conn, 'signal.raised', 'office', {'kind': 'offline', 'level': 'warn', 'text': '测试提醒'}, ws=ws_for(self.tmp.name), conv=self.conv, subject='em_00000001', ts=9)
        ready = subprocess.run([str(root / 'bin' / 'xay2'), 'wait', '--timeout', '1'], cwd=self.tmp.name, env=env, text=True, capture_output=True)
        self.assertEqual(ready.returncode, 0)
        self.assertIn('游标', ready.stdout)


class LedgerCoreTests(unittest.TestCase):
    """协议 1.4/1.5 的内核行为：计划批次、部门按对话分开、旧系统镜像、默认对话、recent、缓存与存档点。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = str(Path(self.tmp.name) / 'xay.db')
        self.conn = open_db(self.path)
        self.ws = 'ws_core'

    def tearDown(self):
        self.conn.close()
        self.tmp.cleanup()

    def begin(self, conv, ts):
        append(self.conn, 'conv.begun', 'lead:' + conv, {'title': conv, 'lead_kind': 'claude', 'aliases': [], 'cwd': '/tmp'},
               ws=self.ws, conv=conv, subject=conv, ts=ts)

    def add(self, conv, tk, ts, **extra):
        data = dict({'title': tk, 'deps': [], 'writes': [], 'mode': 'write', 'reason': extra.pop('reason', '加')}, **extra)
        return append(self.conn, 'task.added', 'lead:' + conv, data, ws=self.ws, conv=conv, subject=tk, ts=ts)

    def assert_rebuild_same(self):
        import json
        before = json.dumps(load_state(self.conn), sort_keys=True, ensure_ascii=False)
        rebuild(self.conn)
        self.assertEqual(before, json.dumps(load_state(self.conn), sort_keys=True, ensure_ascii=False))

    def test_batch_shares_one_version_until_another_plan_event(self):
        self.begin('cv_a', 1)
        self.add('cv_a', 'tk_1', 2, batch='b1', reason='一批')
        append(self.conn, 'employee.hired', 'office', {'agent': 'codex'}, ws=self.ws, subject='em_1', ts=3)    # 别的事件不打断
        append(self.conn, 'signal.raised', 'office', {'kind': 'silent', 'level': 'remind', 'text': 'x'}, ws=self.ws, conv='cv_a', subject='em_1', ts=3)
        self.add('cv_a', 'tk_2', 4, batch='b1', reason='第二条的理由不用')
        append(self.conn, 'task.assigned', 'lead:cv_a', {'owner': 'em_1', 'reason': '分给他', 'batch': 'b1'}, ws=self.ws, conv='cv_a', subject='tk_1', ts=5)
        self.add('cv_a', 'tk_3', 6, reason='单独一件')            # 没有 batch：新的一版
        self.add('cv_a', 'tk_4', 7, batch='b1', reason='又一批')   # 同名 batch 但不连续：新的一版
        snap = build(self.conn, conv='cv_a', now=10)
        self.assertEqual(snap['plan']['version'], 3)
        h = snap['plan']['history']
        self.assertEqual([x['version'] for x in h], [1, 2, 3])
        self.assertEqual((h[0]['reason'], [(c['op'], c['task']) for c in h[0]['changes']]),
                         ('一批', [('add', 'tk_1'), ('add', 'tk_2'), ('assign', 'tk_1')]))
        self.assertEqual([c['task'] for c in h[1]['changes']], ['tk_3'])
        self.assertEqual([c['task'] for c in h[2]['changes']], ['tk_4'])
        self.assertNotIn('batch', next(t for t in snap['tasks'] if t['id'] == 'tk_1'))
        self.assert_rebuild_same()

    def test_proposal_accept_is_one_version_by_the_proposer(self):
        self.begin('cv_a', 1)
        append(self.conn, 'employee.hired', 'lead:cv_a', {'agent': 'codex'}, ws=self.ws, conv='cv_a', subject='em_1', ts=2)
        self.add('cv_a', 'tk_1', 3)
        self.add('cv_a', 'tk_2', 4)
        append(self.conn, 'proposal.made', 'employee:em_1', {'from': 'em_1', 'to': 'gm', 'reason': '调整', 'changes': [
            {'op': 'add', 'task': 'tk_3', 'fields': {'title': '新活', 'key': 'k3', 'owner': 'em_1'}},
            {'op': 'change', 'task': 'tk_1', 'fields': {'title': '改名', 'status': 'done'}},
            {'op': 'remove', 'task': 'tk_2'}]}, ws=self.ws, conv='cv_a', subject='pp_1', ts=5)
        append(self.conn, 'proposal.decided', 'lead:cv_a', {'verdict': 'accept'}, ws=self.ws, conv='cv_a', subject='pp_1', ts=6)
        snap = build(self.conn, conv='cv_a', now=10)
        self.assertEqual(snap['plan']['version'], 3)
        h = snap['plan']['history'][-1]
        self.assertEqual((h['by'], h['reason']), ('em_1', '调整（申请 pp_1 通过）'))
        self.assertEqual([c['op'] for c in h['changes']], ['add', 'change', 'remove'])
        tasks = {t['id']: t for t in snap['tasks']}
        self.assertEqual((tasks['tk_3']['key'], tasks['tk_3']['owner']), ('k3', 'em_1'))
        self.assertEqual((tasks['tk_1']['title'], tasks['tk_1']['status']), ('改名', 'planned'), 'change 只改可改字段')
        self.assertEqual(tasks['tk_2']['status'], 'canceled')
        self.assert_rebuild_same()

    def test_same_dept_id_in_two_conversations_does_not_collide(self):
        self.begin('cv_a', 1)
        self.begin('cv_b', 2)
        append(self.conn, 'employee.hired', 'office', {'agent': 'codex'}, ws=self.ws, subject='em_1', ts=3)
        for conv, name in (('cv_a', '前端部'), ('cv_b', '前台组')):
            append(self.conn, 'dept.opened', 'lead:' + conv, {'name': name, 'mission': conv, 'owns': [], 'head': 'rules'}, ws=self.ws, conv=conv, subject='fe', ts=4)
        append(self.conn, 'employee.joined', 'lead:cv_a', {'dept': 'fe'}, ws=self.ws, conv='cv_a', subject='em_1', ts=5)
        append(self.conn, 'letter.sent', 'user', {'from': 'user', 'to': 'fe', 'kind': 'request', 'subject': '问', 'body': 'x'}, ws=self.ws, conv='cv_b', subject='lt_1', ts=6)
        a, b = build(self.conn, conv='cv_a', now=10), build(self.conn, conv='cv_b', now=10)
        self.assertEqual([(d['id'], d['name'], d['members'], d['counts']['open_requests']) for d in a['depts']], [('fe', '前端部', ['em_1'], 0)])
        self.assertEqual([(d['id'], d['name'], d['members'], d['counts']['open_requests']) for d in b['depts']], [('fe', '前台组', [], 1)])
        self.assertEqual((a['people']['em_1']['dept'], b['people']['em_1']['dept']), ('fe', None))
        append(self.conn, 'dept.closed', 'lead:cv_b', {}, ws=self.ws, conv='cv_b', subject='fe', ts=7)
        self.assertEqual(build(self.conn, conv='cv_b', now=10)['depts'], [])
        a = build(self.conn, conv='cv_a', now=10)
        self.assertEqual([d['id'] for d in a['depts']], ['fe'], '关掉乙对话的 fe 不影响甲对话的 fe')
        self.assertEqual(a['people']['em_1']['dept'], 'fe')
        self.assert_rebuild_same()

    def test_legacy_runs_and_letters_show_on_the_page(self):
        from xay.core.humanize import text
        self.begin('cv_a', 1)
        append(self.conn, 'employee.hired', 'bridge', {'agent': 'codex', 'note': '旧系统'}, ws=self.ws, conv='cv_a', subject='em_1', ts=2)
        run = {'run_dir': '/old/runs/r-001', 'agent': 'codex', 'worker': 'w1', 'title': '修登录页', 'started': 3, 'lead_session': 'u1'}
        s1 = append(self.conn, 'bridge.run', 'bridge', dict(run, status='running'), ws=self.ws, conv='cv_a', subject='em_1', ts=3)
        append(self.conn, 'bridge.run', 'bridge', {'run_dir': '/old/runs/r-002', 'agent': 'dsh', 'title': '查日志', 'status': 'timeout', 'started': 3, 'ended': 4},
               ws=self.ws, conv='cv_a', subject='r-002', ts=4)
        append(self.conn, 'bridge.letter', 'bridge', {'id': 'm_1', 'from': {'dept': 'fe', 'worker': 'w1', 'run': ''}, 'to': 'gm', 'type': 'request',
                                                     'subject': '要不要改接口', 'body': '…'}, ws=self.ws, conv='cv_a', subject='m_1', ts=5)
        snap = build(self.conn, conv='cv_a', now=6)
        legacy = [t for t in snap['tasks'] if t.get('legacy')]
        self.assertEqual([(t['id'], t['title'], t['owner'], t['status']) for t in legacy],
                         [('run:r-001', '修登录页', 'em_1', 'working'), ('run:r-002', '查日志', None, 'failed')])
        self.assertEqual((snap['people']['em_1']['state'], snap['people']['em_1']['now']), ('working', '修登录页'))
        letter = next(l for l in snap['letters'] if l['id'] == 'm_1')
        self.assertEqual((letter['legacy'], letter['from'], letter['to'], letter['kind'], letter['status']), (True, 'w1', 'gm', 'request', 'open'))
        texts = [r['text'] for r in snap['recent']]
        self.assertIn('（旧系统）Codex 1 开始做「修登录页」', texts)
        self.assertIn('（旧系统）DeepSeek 临时工 没做成「查日志」', texts)
        self.assertIn('（旧系统）w1 给 负责人 写信：要不要改接口', texts)
        append(self.conn, 'bridge.run', 'bridge', dict(run, status='done', ended=7, result_head='改好了'), ws=self.ws, conv='cv_a', subject='em_1', ts=7)
        append(self.conn, 'bridge.letter', 'bridge', {'id': 'm_2', 'from': {'dept': 'gm', 'worker': 'gm'}, 'to': 'fe', 'type': 'reply', 'reply_to': 'm_1',
                                                     'verdict': 'accept', 'subject': '可以', 'body': '改吧'}, ws=self.ws, conv='cv_a', subject='m_2', ts=8)
        snap = build(self.conn, conv='cv_a', now=9)
        t = next(t for t in snap['tasks'] if t['id'] == 'run:r-001')
        self.assertEqual((t['status'], t['result']), ('done', '改好了'))
        self.assertEqual(len([x for x in snap['tasks'] if x.get('legacy')]), 2, '同一个 run 目录的多次状态合并成一件')
        self.assertEqual(snap['people']['em_1']['state'], 'idle')
        self.assertEqual(next(l for l in snap['letters'] if l['id'] == 'm_1')['status'], 'closed')
        self.assertEqual(snap['recent'][-2]['text'], '（旧系统）Codex 1 做完了「修登录页」')
        self.assertEqual(text({'type': 'bridge.run', 'subject': 'em_1', 'data': dict(run, status='queued')}, self.conn), None)
        self.assertGreater(s1, 0)
        self.assert_rebuild_same()

    def test_bridge_may_title_a_conversation_and_re_adopt_a_run_silently(self):
        from xay.core.errors import PermissionError as Denied
        from xay.core.humanize import text
        self.begin('cv_a', 1)
        append(self.conn, 'conv.titled', 'bridge', {'title': '外面的标题'}, ws=self.ws, conv='cv_a', subject='cv_a', ts=2)
        self.assertEqual(build(self.conn, conv='cv_a', now=3)['conversation']['title'], '外面的标题')
        with self.assertRaises(Denied):
            append(self.conn, 'conv.ended', 'bridge', {}, ws=self.ws, conv='cv_a', subject='cv_a', ts=3)
        # 老记录补认员工：重发的那条不进大白话；其余字段进现状，任务有了负责人
        append(self.conn, 'employee.hired', 'bridge', {'agent': 'dsh', 'label': 'DeepSeek 临时工', 'note': '旧系统'}, ws=self.ws, conv='cv_a', subject='em_t', ts=4)
        run = {'run_dir': '/old/runs/r-9', 'agent': 'dsh', 'title': '老活', 'status': 'done', 'started': 1, 'ended': 2, 'lead_session': 'u1'}
        append(self.conn, 'bridge.run', 'bridge', run, ws=self.ws, conv='cv_a', subject='r-9', ts=2)
        append(self.conn, 'bridge.run', 'bridge', dict(run, adopt=True), ws=self.ws, conv='cv_a', subject='em_t', ts=2, dedup='bridge:/old/runs/r-9:adopt')
        snap = build(self.conn, conv='cv_a', now=5)
        task = next(t for t in snap['tasks'] if t['id'] == 'run:r-9')
        self.assertEqual((task['owner'], task['status'], task['legacy']), ('em_t', 'done', True))
        self.assertEqual([r['text'] for r in snap['recent'] if '老活' in r['text']], ['（旧系统）DeepSeek 临时工 做完了「老活」'])
        self.assertIsNone(text({'type': 'bridge.run', 'subject': 'em_t', 'data': dict(run, adopt=True)}, self.conn))
        self.assert_rebuild_same()

    def test_default_conversation_is_the_one_with_the_newest_event(self):
        from xay.core.snapshot import pick_conv
        self.begin('cv_a', 1)
        self.begin('cv_b', 2)
        self.assertEqual(build(self.conn, now=10)['conversation']['id'], 'cv_b')
        self.add('cv_a', 'tk_1', 3)
        self.assertEqual(build(self.conn, now=10)['conversation']['id'], 'cv_a')
        self.assertEqual(build(self.conn, ws=self.ws, now=10)['conversation']['id'], 'cv_a')
        self.assertEqual(pick_conv(load_state(self.conn), ws='ws_other'), None)
        append(self.conn, 'conv.begun', 'lead:cv_c', {'title': 'c', 'lead_kind': 'claude', 'aliases': [], 'cwd': '/'}, ws='ws_two', conv='cv_c', subject='cv_c', ts=4)
        self.assertEqual(build(self.conn, now=10)['conversation']['id'], 'cv_c', '不传 ws：最近有事件的 ws')
        self.assertEqual(build(self.conn, ws=self.ws, now=10)['conversation']['id'], 'cv_a')

    def test_recent_is_the_last_twenty_humanized_events_of_this_conversation(self):
        self.begin('cv_a', 1)
        self.begin('cv_b', 2)
        for i in range(25):
            self.add('cv_a', 'tk_a%02d' % i, 10 + i)
        append(self.conn, 'employee.hired', 'office', {'agent': 'codex'}, ws=self.ws, subject='em_1', ts=40)
        for i in range(70):   # 没有大白话的事件，而且排在最后
            append(self.conn, 'station.heartbeat', 'station:em_1', {'pid': 1}, ws=self.ws, conv='cv_a', subject='em_1', ts=50 + i)
        for i in range(30):   # 别的对话的事件
            self.add('cv_b', 'tk_b%02d' % i, 200 + i)
        recent = build(self.conn, conv='cv_a', now=300)['recent']
        self.assertEqual(len(recent), 20)
        self.assertEqual([r['seq'] for r in recent], sorted(r['seq'] for r in recent))
        self.assertTrue(all(r['type'] == 'task.added' for r in recent))
        self.assertEqual(recent[-1]['text'], '负责人把「tk_a24」排进计划')
        self.assertEqual(recent[0]['text'], '负责人把「tk_a05」排进计划')

    def test_failed_append_leaves_the_cache_equal_to_the_ledger(self):
        import json
        self.begin('cv_a', 1)
        append(self.conn, 'employee.hired', 'lead:cv_a', {'agent': 'codex', 'label': '小明'}, ws=self.ws, conv='cv_a', subject='em_1', ts=2)
        self.add('cv_a', 'tk_1', 3, batch='b')
        before = json.dumps(load_state(self.conn), sort_keys=True)
        top = max_seq(self.conn)
        with self.assertRaises(ValueError):   # reducer 里才发现的错误（显示名重名）：已经改了一半缓存
            append(self.conn, 'employee.hired', 'lead:cv_a', {'agent': 'codex', 'label': '小明'}, ws=self.ws, conv='cv_a', subject='em_2', ts=4)
        self.assertEqual(max_seq(self.conn), top)
        self.assertEqual(json.dumps(load_state(self.conn), sort_keys=True), before)
        other = open_db(self.path)   # 同一进程里的另一个连接看到的也一样
        try:
            self.assertEqual(json.dumps(load_state(other), sort_keys=True), before)
            self.add('cv_a', 'tk_2', 5, batch='b')   # 另一个连接写的，这个连接马上能看到；批次照常延续
        finally:
            other.close()
        self.assertEqual(build(self.conn, conv='cv_a', now=10)['plan']['version'], 1)
        self.assert_rebuild_same()

    def test_checkpoint_cold_start_matches_full_replay(self):
        import json
        from xay.core import events as core_events
        self.begin('cv_a', 1)
        for i in range(450):
            self.add('cv_a', 'tk_%04d' % i, 2 + i)
        rows = self.conn.execute('SELECT seq FROM checkpoint').fetchall()
        self.assertEqual([r['seq'] for r in rows], [400], '每 200 条存一次，只留最近一份')
        warm = json.dumps(load_state(self.conn), sort_keys=True)
        core_events._REGISTRY.clear()          # 模拟新进程：从存档点 + 之后的 51 条事件重建
        fresh = open_db(self.path)
        try:
            self.assertEqual(json.dumps(load_state(fresh), sort_keys=True), warm)
        finally:
            fresh.close()
        rebuild(self.conn)
        self.assertEqual([r['seq'] for r in self.conn.execute('SELECT seq FROM checkpoint')], [451])
        self.assertEqual(json.dumps(load_state(self.conn), sort_keys=True), warm)

    def test_load_state_is_a_private_copy(self):
        self.begin('cv_a', 1)
        s = load_state(self.conn)
        s['convs']['cv_a']['title'] = '被调用方改了'
        snap = build(self.conn, conv='cv_a', now=10)
        snap['plan']['history'].append('垃圾')
        self.assertEqual(load_state(self.conn)['convs']['cv_a']['title'], 'cv_a')
        self.assertEqual(build(self.conn, conv='cv_a', now=10)['plan']['history'], [])

    def test_decision_resolved_manual_marks_reported_and_settle(self):
        self.begin('cv_a', 1)
        append(self.conn, 'employee.hired', 'lead:cv_a', {'agent': 'codex'}, ws=self.ws, conv='cv_a', subject='em_1', ts=2)
        self.add('cv_a', 'tk_1', 3)
        append(self.conn, 'task.assigned', 'lead:cv_a', {'owner': 'em_1', 'reason': '分活'}, ws=self.ws, conv='cv_a', subject='tk_1', ts=4)
        append(self.conn, 'turn.started', 'station:em_1', {'task': 'tk_1', 'em': 'em_1'}, ws=self.ws, conv='cv_a', subject='tn_1', ts=5)
        append(self.conn, 'turn.ended', 'station:em_1', {'em': 'em_1', 'status': 'completed', 'evidence': {}}, ws=self.ws, conv='cv_a', subject='tn_1', ts=6)
        append(self.conn, 'task.kept', 'station:em_1', {'reason': 'conflict', 'detail': '合并冲突'}, ws=self.ws, conv='cv_a', subject='tk_1', ts=7)
        append(self.conn, 'decision.opened', 'station:em_1',
               {'kind': 'conflict', 'title': '冲突', 'detail': '冲突详情', 'task': 'tk_1', 'em': 'em_1',
                'options': [{'id': 'merge'}, {'id': 'rework'}, {'id': 'discard'}, {'id': 'manual'}], 'owner': 'lead'},
               ws=self.ws, conv='cv_a', subject='dc_1', ts=8)
        s = load_state(self.conn)
        self.assertEqual(s['tasks']['tk_1']['status'], 'needs_you')
        self.assertIn('dc_1', s['decisions'])

        # 负责人选 manual: 我已经手动合好了
        append(self.conn, 'decision.resolved', 'lead:cv_a', {'option': 'manual'}, ws=self.ws, conv='cv_a', subject='dc_1', ts=9)
        s = load_state(self.conn)
        self.assertNotIn('dc_1', s['decisions'])
        self.assertEqual(s['tasks']['tk_1']['status'], 'reported')
        self.assertEqual(s['tasks']['tk_1']['desk_action'], 'settle')
        self.assertEqual(s['employees']['em_1']['state'], 'idle')
        self.assert_rebuild_same()


if __name__ == '__main__':
    unittest.main()
