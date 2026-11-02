"""账本并发测试：多个进程同时往同一本账里追加事件。

协议依据：V2/docs/protocol.md
- §0.2  append() 在同一个事务里写事件、跑 reducer、更新现状表
- §2    写事务用 BEGIN IMMEDIATE；dedup 冲突时返回已有那条的 seq（幂等，不报错）
- §0.3  rebuild 之后现状必须与重放前逐项一致

用 spawn 方式起进程（macOS 默认，也是最严格的），所以 worker 函数必须放在模块顶层，
并且自己把 V2 目录放进 sys.path 再导入 xay。全程只用临时目录，不碰真实 ~/.xay。

关于"数据库被锁"：worker 遇到 sqlite3.OperationalError: database is locked 会记一笔然后重试（append 在
BEGIN IMMEDIATE 失败时什么都没写，重试是安全的），这样账本正确性的检查不会被它挡住；
test_no_lock_timeouts 要求这个次数是 0（core 自己 busy_timeout=15000 + 有限次退避重试），并且 10×100 在 30 秒内写完。
"""
import json
import multiprocessing
import queue as queue_module
import random
import sqlite3
import sys
import tempfile
import time
import traceback
import unittest
from pathlib import Path

V2_ROOT = Path(__file__).resolve().parents[1]
if str(V2_ROOT) not in sys.path:
    sys.path.insert(0, str(V2_ROOT))

from xay.core import open_db, append, rebuild, max_seq  # noqa: E402
from xay.core.errors import PermissionError as XayPermissionError  # noqa: E402
from xay.core.events import load_state  # noqa: E402
from xay.core.snapshot import build  # noqa: E402

WS = 'ws_conc0001'
CONV = 'cv_conc'
LEAD = 'lead:' + CONV

WORKERS = 10
PER_WORKER = 100
# 每个 worker 的 100 条里：70 条开部门、30 条加任务（任务多一条计划历史，状态膨胀得更快，比例偏向部门是为了让整套测试跑得快）
DEPTS_PER_WORKER = 70
TASKS_PER_WORKER = 30
# 单次等所有 worker 完成的最长时间（秒）。正常情况下十几秒；这只是防挂死的护栏。
RUN_TIMEOUT = 90


# ---------------------------------------------------------------- 模块顶层的 worker（spawn 需要）

def _open_conv(db_path, conv=CONV):
    """负责人先开一个对话（seq=1），之后所有 worker 都往这个对话里写。"""
    conn = open_db(db_path)
    seq = append(conn, 'conv.begun', 'lead:' + conv,
                 {'title': '并发测试', 'lead_kind': 'claude', 'aliases': [], 'cwd': '/tmp'},
                 ws=WS, conv=conv, subject=conv, ts=1)
    conn.close()
    return seq


def _wait_barrier(barrier):
    # 让所有进程尽量同时开始抢写锁
    barrier.wait(timeout=60)


def _append_retrying(conn, stats, *args, **kwargs):
    """调用 core.append；遇到 "database is locked" 就在 stats['lock_errors'] 记一笔并重试。"""
    deadline = time.time() + 60
    while True:
        try:
            return append(conn, *args, **kwargs)
        except sqlite3.OperationalError as exc:
            if 'locked' not in str(exc) and 'busy' not in str(exc):
                raise
            stats['lock_errors'] += 1
            if time.time() > deadline:
                raise
            time.sleep(random.uniform(0.005, 0.05))


def _append_worker(idx, db_path, per_worker, barrier, out_q):
    """每个 worker 追加 per_worker 条合法事件：开独一无二的部门（7/10）或加独一无二的任务（3/10）。"""
    result = {'worker': idx, 'seqs': [], 'subjects': [], 'lock_errors': 0, 'error': None}
    try:
        conn = open_db(db_path)
        _wait_barrier(barrier)
        for j in range(per_worker):
            block, pos = divmod(j, 10)
            if pos < 7:
                n = block * 7 + pos
                subject = 'w%dd%d' % (idx, n)  # 部门短名：[a-z][a-z0-9_-]{0,15}
                seq = _append_retrying(
                    conn, result, 'dept.opened', LEAD,
                    {'name': '部门%d-%d' % (idx, n), 'mission': '并发测试', 'owns': [], 'head': 'rules'},
                    ws=WS, conv=CONV, subject=subject)
            else:
                n = block * 3 + (pos - 7)
                subject = 'tk_%02x%06x' % (idx, n)
                seq = _append_retrying(
                    conn, result, 'task.added', LEAD,
                    {'title': '任务%d-%d' % (idx, n), 'deps': [], 'writes': [], 'mode': 'write', 'reason': '并发测试'},
                    ws=WS, conv=CONV, subject=subject)
            result['seqs'].append(seq)
            result['subjects'].append(subject)
        conn.close()
    except BaseException:
        result['error'] = traceback.format_exc()
    out_q.put(result)


def _dedup_worker(idx, db_path, keys, rounds, barrier, out_q):
    """每个 worker 按自己的顺序（打乱）追加同一批 dedup 键，每个键重复 rounds 次。"""
    result = {'worker': idx, 'by_key': {}, 'lock_errors': 0, 'error': None}
    try:
        conn = open_db(db_path)
        order = list(keys)
        random.Random(idx).shuffle(order)
        _wait_barrier(barrier)
        for _ in range(rounds):
            for key in order:
                seq = _append_retrying(
                    conn, result, 'signal.raised', 'office',
                    {'kind': 'silent', 'level': 'remind', 'text': '来自进程%d：%s' % (idx, key)},
                    ws=WS, conv=CONV, subject='em_00000001', dedup=key)
                result['by_key'].setdefault(key, []).append(seq)
        conn.close()
    except BaseException:
        result['error'] = traceback.format_exc()
    out_q.put(result)


def _mixed_worker(idx, db_path, per_worker, barrier, out_q):
    """合法事件和会被拒绝的事件交替写：被拒的事务回滚后不能占 seq、不能改现状。"""
    result = {'worker': idx, 'seqs': [], 'subjects': [], 'permission_rejected': 0, 'value_rejected': 0,
              'lock_errors': 0, 'error': None}
    try:
        conn = open_db(db_path)
        _wait_barrier(barrier)
        for j in range(per_worker):
            subject = 'tk_%02x%06x' % (idx, j)
            seq = _append_retrying(
                conn, result, 'task.added', LEAD,
                {'title': '任务%d-%d' % (idx, j), 'deps': [], 'writes': [], 'mode': 'write', 'reason': '混合并发'},
                ws=WS, conv=CONV, subject=subject)
            result['seqs'].append(seq)
            result['subjects'].append(subject)
            try:  # 越权：别的对话的负责人
                _append_retrying(conn, result, 'task.added', 'lead:cv_other', {'title': '越权'},
                                 ws=WS, conv=CONV, subject='tk_bad%05d' % j)
            except XayPermissionError:
                result['permission_rejected'] += 1
            try:  # 改一个不存在的任务
                _append_retrying(conn, result, 'task.changed', LEAD, {'fields': {'title': 'x'}, 'reason': 'x'},
                                 ws=WS, conv=CONV, subject='tk_missing%02d' % idx)
            except ValueError:
                result['value_rejected'] += 1
        conn.close()
    except BaseException:
        result['error'] = traceback.format_exc()
    out_q.put(result)


# ---------------------------------------------------------------- 公共工具

def run_workers(target, arg_sets, timeout=RUN_TIMEOUT):
    """用 spawn 起一组进程；每组参数后面自动补上 (barrier, out_q)。返回每个 worker 的结果（按 worker 号排序）。"""
    ctx = multiprocessing.get_context('spawn')
    barrier = ctx.Barrier(len(arg_sets))
    out_q = ctx.Queue()
    procs = [ctx.Process(target=target, args=tuple(args) + (barrier, out_q)) for args in arg_sets]
    for p in procs:
        p.start()
    results = []
    deadline = time.time() + timeout
    try:
        while len(results) < len(procs):
            left = deadline - time.time()
            if left <= 0:
                raise AssertionError('等 worker 超时（%d 秒），已收到 %d/%d 个结果' % (timeout, len(results), len(procs)))
            try:
                results.append(out_q.get(timeout=min(left, 1)))
            except queue_module.Empty:
                if all(not p.is_alive() for p in procs) and out_q.empty():
                    raise AssertionError('worker 都退出了但结果不齐：%d/%d' % (len(results), len(procs)))
        for p in procs:
            p.join(timeout=30)
    finally:
        for p in procs:
            if p.is_alive():
                p.terminate()
                p.join(5)
    return sorted(results, key=lambda r: r['worker'])


class LedgerAssertions(unittest.TestCase):
    """几个测试类共用的断言。本类自己没有测试。"""

    def assert_no_worker_errors(self, results):
        errors = [r['error'] for r in results if r.get('error')]
        self.assertFalse(errors, 'worker 抛了异常：\n' + '\n'.join(errors))

    def assert_seqs_contiguous(self, conn, expected_max):
        seqs = [r['seq'] for r in conn.execute('SELECT seq FROM events ORDER BY seq')]
        self.assertEqual(max_seq(conn), expected_max)
        self.assertEqual(seqs, list(range(1, expected_max + 1)), 'seq 必须从 1 连续到最大值，不能有空洞或重复')

    def assert_rebuild_identical(self, conn):
        def dump():
            return (json.dumps(load_state(conn), ensure_ascii=False, sort_keys=True),
                    json.dumps(build(conn, ws=WS, conv=CONV, now=2000), ensure_ascii=False, sort_keys=True))
        before_state, before_snap = dump()
        rebuild(conn)
        after_state, after_snap = dump()
        self.assertEqual(before_state, after_state, 'rebuild 之后现状与重放前不一致')
        self.assertEqual(before_snap, after_snap, 'rebuild 之后快照与重放前不一致')


# ---------------------------------------------------------------- 10 个进程 × 100 条

class TenWayAppendTests(LedgerAssertions):
    """setUpClass 里只跑一次 10 进程 × 100 条的负载，下面几个测试各查一个方面。"""

    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        cls.db_path = str(Path(cls.tmp.name) / 'xay.db')
        assert _open_conv(cls.db_path) == 1
        started = time.time()
        cls.results = run_workers(_append_worker, [(i, cls.db_path, PER_WORKER) for i in range(WORKERS)])
        cls.elapsed = time.time() - started
        cls.conn = open_db(cls.db_path)

    @classmethod
    def tearDownClass(cls):
        cls.conn.close()
        cls.tmp.cleanup()

    def test_every_event_stored_once_with_contiguous_seq(self):
        self.assert_no_worker_errors(self.results)
        total = WORKERS * PER_WORKER
        self.assertEqual(total, 1000)
        self.assertEqual(DEPTS_PER_WORKER + TASKS_PER_WORKER, PER_WORKER)

        # 每个 worker 自己拿到的 seq 严格递增；所有 worker 拿到的 seq 合起来无重复、正好是 2..1001
        returned = []
        for r in self.results:
            self.assertEqual(len(r['seqs']), PER_WORKER)
            self.assertEqual(r['seqs'], sorted(r['seqs']), 'worker %d 自己写的事件 seq 应严格递增' % r['worker'])
            returned.extend(r['seqs'])
        self.assertEqual(len(returned), len(set(returned)), '不同进程拿到了相同的 seq')
        self.assertEqual(sorted(returned), list(range(2, total + 2)))

        conn = self.conn
        self.assertEqual(conn.execute('PRAGMA integrity_check').fetchone()[0], 'ok')
        self.assert_seqs_contiguous(conn, 1 + total)

        # 账本里每条事件正是 worker 以为自己写的那条，没有串号
        by_seq = {r['seq']: r['subject'] for r in conn.execute('SELECT seq, subject FROM events')}
        for r in self.results:
            for seq, subject in zip(r['seqs'], r['subjects']):
                self.assertEqual(by_seq[seq], subject)
        subjects = [r['subject'] for r in conn.execute("SELECT subject FROM events WHERE type IN ('dept.opened','task.added')")]
        self.assertEqual(len(subjects), total)
        self.assertEqual(len(set(subjects)), total, '账本里有重复的对象编号')

    def test_projection_has_all_thousand_objects(self):
        """没有丢更新：1000 个对象、计划版本和历史一个不少。"""
        self.assert_no_worker_errors(self.results)
        conn = self.conn
        total = WORKERS * PER_WORKER
        state = load_state(conn)
        expected_depts = {'w%dd%d' % (i, n) for i in range(WORKERS) for n in range(DEPTS_PER_WORKER)}
        expected_tasks = {'tk_%02x%06x' % (i, n) for i in range(WORKERS) for n in range(TASKS_PER_WORKER)}
        # 部门编号只在对话内唯一（协议 1.5）：现状里按对话分开存，记录里的 id 仍是短名
        self.assertEqual({d['id'] for d in state['depts'].values()}, expected_depts)
        self.assertEqual({d['conv'] for d in state['depts'].values()}, {CONV})
        self.assertEqual(set(state['tasks']), expected_tasks)
        self.assertEqual(len(state['depts']) + len(state['tasks']), total)
        self.assertEqual(state['seq'], 1 + total)
        # 每个任务让计划版本 +1（读-改-写的计数器，最容易暴露丢更新）；历史里版本号连续
        self.assertEqual(state['convs'][CONV]['plan_version'], len(expected_tasks))
        history = state['plan_history'][CONV]
        self.assertEqual([h['version'] for h in history], list(range(1, len(expected_tasks) + 1)))
        self.assertEqual({h['changes'][0]['task'] for h in history}, expected_tasks)

        snap = build(conn, ws=WS, conv=CONV, now=2000)
        self.assertEqual(len(snap['tasks']), len(expected_tasks))
        self.assertEqual(len(snap['depts']), len(expected_depts))
        self.assertEqual(snap['plan']['version'], len(expected_tasks))
        self.assertEqual(snap['seq'], 1 + total)

    def test_rebuild_reproduces_identical_state(self):
        self.assert_no_worker_errors(self.results)
        self.assert_rebuild_identical(self.conn)

    def test_no_lock_timeouts(self):
        """10 路连续猛写时，core.append 一次 "database is locked" 都不能漏给调用方，而且要快（协议 §2）。"""
        self.assert_no_worker_errors(self.results)
        per_worker = [r['lock_errors'] for r in self.results]
        self.assertEqual(sum(per_worker), 0,
                         'append 抛了 sqlite3.OperationalError: database is locked：各 worker 次数 %s，本次总耗时 %.1f 秒'
                         % (per_worker, self.elapsed))
        self.assertLess(self.elapsed, 30, '10 个进程 × 100 条应在 30 秒内写完，实际 %.1f 秒' % self.elapsed)


# ---------------------------------------------------------------- dedup 与回滚

class DedupAndRollbackTests(LedgerAssertions):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db_path = str(Path(self.tmp.name) / 'xay.db')
        self.assertEqual(_open_conv(self.db_path), 1)
        self.conn = None

    def tearDown(self):
        if self.conn is not None:
            self.conn.close()
        self.tmp.cleanup()

    def open_ledger(self):
        self.conn = open_db(self.db_path)
        return self.conn

    def test_same_dedup_key_from_many_processes_is_stored_once(self):
        n = 8
        results = run_workers(_dedup_worker, [(i, self.db_path, ['same-key'], 3) for i in range(n)])
        self.assert_no_worker_errors(results)
        seqs = {seq for r in results for seq in r['by_key']['same-key']}
        self.assertEqual(len(seqs), 1, '同一个 dedup 键必须所有进程拿到同一个 seq：%s' % sorted(seqs))
        for r in results:
            self.assertEqual(len(r['by_key']['same-key']), 3)

        conn = self.open_ledger()
        rows = conn.execute("SELECT seq FROM events WHERE dedup='same-key'").fetchall()
        self.assertEqual(len(rows), 1, '同一个 dedup 键只应存一条事件')
        self.assertEqual(rows[0]['seq'], seqs.pop())
        self.assert_seqs_contiguous(conn, 2)  # conv.begun + 那一条
        self.assertEqual(len(load_state(conn)['signals']), 1, 'reducer 只能对这条事件跑一次')
        self.assert_rebuild_identical(conn)

    def test_overlapping_dedup_keys_agree_across_processes(self):
        keys = ['k%02d' % i for i in range(30)]
        n = 6
        results = run_workers(_dedup_worker, [(i, self.db_path, keys, 2) for i in range(n)])
        self.assert_no_worker_errors(results)
        for key in keys:
            seqs = {seq for r in results for seq in r['by_key'][key]}
            self.assertEqual(len(seqs), 1, '键 %s 在不同进程拿到了不同 seq：%s' % (key, sorted(seqs)))
        distinct = {r['by_key'][key][0] for r in results for key in keys}
        self.assertEqual(len(distinct), len(keys), '不同的键必须拿到不同的 seq')

        conn = self.open_ledger()
        self.assert_seqs_contiguous(conn, 1 + len(keys))
        self.assertEqual(conn.execute('SELECT COUNT(DISTINCT dedup) FROM events WHERE dedup IS NOT NULL').fetchone()[0], len(keys))
        self.assertEqual(len(load_state(conn)['signals']), len(keys))
        self.assert_rebuild_identical(conn)

    def test_rejected_writes_interleaved_do_not_leak_seq_or_state(self):
        n, per = 4, 25
        results = run_workers(_mixed_worker, [(i, self.db_path, per) for i in range(n)])
        self.assert_no_worker_errors(results)
        for r in results:
            self.assertEqual(r['permission_rejected'], per)
            self.assertEqual(r['value_rejected'], per)

        conn = self.open_ledger()
        self.assert_seqs_contiguous(conn, 1 + n * per)  # 被拒的写入不占 seq
        state = load_state(conn)
        self.assertEqual(len(state['tasks']), n * per)
        self.assertFalse([k for k in state['tasks'] if k.startswith('tk_bad') or k.startswith('tk_missing')],
                         '被拒的事件不能留下任何现状')
        self.assertEqual(state['convs'][CONV]['plan_version'], n * per)
        self.assert_rebuild_identical(conn)


if __name__ == '__main__':
    unittest.main()
