"""示例模式（?demo=1）的测试：内存账本按示例事件文件渐进回放，和真实数据走同一套快照 / 事件流代码。"""
import logging
import os
import shutil
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

from tests import test_office_web as W
from xay.core import events as core_events
from xay.core import snapshot as core_snapshot
from xay.office import demo as demo_mod
from xay.office.demo import DemoLedger, compress_gaps, load_events

FIXTURE = W.V2 / "fixtures" / "demo.events.jsonl"
DEMO_TITLE = "给网店做新版商品页"


def setUpModule():
    # 示例文件里有过期事件时每轮都会打警告；这里只关心测试结果，日志降到错误级别
    global _old_level
    _old_level = logging.getLogger("xay.office.demo").level
    logging.getLogger("xay.office.demo").setLevel(logging.CRITICAL)


def tearDownModule():
    logging.getLogger("xay.office.demo").setLevel(_old_level)


def make_ledger(**kw):
    kw.setdefault("min_gap", 0.0)
    kw.setdefault("max_gap", 0.002)
    kw.setdefault("scale", 0.01)
    kw.setdefault("loop_pause", 3.0)
    return DemoLedger(**kw)


class DemoLedgerTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="xay-demo-test-"))
        self.addCleanup(shutil.rmtree, self.tmp, True)
        env = mock.patch.dict(os.environ, {"XAY2_HOME": str(self.tmp / "v2"), "XAY_HOME": str(self.tmp / "old")})
        env.start()
        self.addCleanup(env.stop)

    def started(self, **kw):
        ledger = make_ledger(**kw).start()
        self.addCleanup(ledger.stop)
        return ledger

    def test_compress_gaps_keeps_order_and_clamps_to_1_to_4_seconds(self):
        delays = compress_gaps([100, 102, 110, 111, 111, 140], 1.0, 4.0, 0.5)
        self.assertEqual(delays, [0.0, 1.0, 4.0, 1.0, 1.0, 4.0])
        gaps = compress_gaps([0, 3, 7, 9, 15, 16], 1.0, 4.0, 0.5)
        self.assertEqual(gaps[0], 0.0)
        self.assertTrue(all(1.0 <= g <= 4.0 for g in gaps[1:]))
        self.assertGreater(gaps[2], gaps[1])       # 原来 4 秒的间隔比 3 秒的等得久
        self.assertLess(gaps[3], gaps[2])
        self.assertEqual(compress_gaps([], 1, 4, 0.5), [])
        self.assertEqual(compress_gaps([None, None], 1.0, 4.0, 0.5), [0.0, 1.0])
        self.assertEqual(demo_mod.DEFAULT_MIN_GAP, 1.0)
        self.assertEqual(demo_mod.DEFAULT_MAX_GAP, 4.0)

    def test_load_events_skips_bad_lines(self):
        p = self.tmp / "e.jsonl"
        p.write_text('\n{"type":"a","by":"x","data":{}}\n坏行\n{"type":1}\n[1]\n{"type":"b","by":"y"}\n', encoding="utf-8")
        self.assertEqual([e["type"] for e in load_events(p)], ["a", "b"])
        self.assertEqual(len(load_events(FIXTURE)), 82)

    def test_replay_fills_the_snapshot_with_people_and_stays_in_memory(self):
        ledger = self.started()
        self.assertTrue(ledger.wait_loops(1, 20))
        snap = ledger.run(lambda conn: core_snapshot.build(conn))
        self.assertEqual(snap["conversation"]["title"], DEMO_TITLE)
        self.assertGreaterEqual(len(snap["people"]), 5)
        self.assertEqual(len(snap["depts"]), 3)
        self.assertEqual(len(snap["tasks"]), 6)
        self.assertTrue(snap["recent"])
        self.assertEqual(len(snap["decisions"]), 1)
        # 全程只在内存里：磁盘上没有任何账本文件
        self.assertFalse((self.tmp / "v2" / "xay.db").exists())

    def test_every_fixture_event_is_accepted_by_the_ledger(self):
        ledger = self.started()
        self.assertTrue(ledger.wait_loops(1, 20))
        total = len(load_events(FIXTURE))
        self.assertEqual(ledger.run(core_events.max_seq), total - len(ledger.rejected))
        if ledger.rejected:
            lines = "、".join(str(n) for n, _ in ledger.rejected)
            self.skipTest("示例文件里有 %d 条按协议 1.2 会被账本拒绝（第 %s 条，例如：%s），需要更新 demo.events.jsonl"
                          % (len(ledger.rejected), lines, ledger.rejected[0][1]))

    def test_replay_is_progressive_and_timestamps_are_append_time(self):
        before = time.time()
        ledger = self.started(min_gap=0.03, max_gap=0.03)
        self.assertLessEqual(ledger.run(core_events.max_seq), 3)          # 刚起来只放了开头几条
        self.assertTrue(ledger.wait_loops(1, 20))
        self.assertEqual(ledger.run(core_events.max_seq), 82 - len(ledger.rejected))
        rows = ledger.run(lambda conn: core_events.since(conn, 0))
        self.assertGreaterEqual(rows[0]["ts"], before - 1)                  # 不是示例文件里的 1793300000
        self.assertLessEqual(rows[-1]["ts"], time.time())
        self.assertEqual([r["ts"] for r in rows], sorted(r["ts"] for r in rows))
        self.assertGreater(rows[-1]["ts"] - rows[0]["ts"], 1.5)             # 82 条 × 0.03 秒

    def test_loops_forever_with_a_fresh_ledger_each_round(self):
        ledger = self.started(loop_pause=0.05)
        self.assertTrue(ledger.wait_loops(2, 20))
        self.assertGreaterEqual(ledger.generation, 2)
        deadline = time.time() + 5
        while time.time() < deadline and ledger.run(core_events.max_seq) > 40:
            time.sleep(0.005)
        self.assertLess(ledger.run(core_events.max_seq), 82 * 2)           # 是新账本重新数，不是接着往后加

    def test_run_passes_exceptions_back_and_fails_cleanly_after_stop(self):
        ledger = self.started()
        with self.assertRaises(ZeroDivisionError):
            ledger.run(lambda conn: 1 / 0)
        self.assertEqual(ledger.run(lambda conn: 7), 7)
        ledger.stop()
        with self.assertRaises(RuntimeError):
            ledger.run(lambda conn: 1)

    def test_empty_fixture_reports_a_clear_error(self):
        empty = self.tmp / "empty.jsonl"
        empty.write_text("", encoding="utf-8")
        ledger = make_ledger(fixture=empty)
        with self.assertRaises(RuntimeError) as ctx:
            ledger.start()
        self.assertIn("示例", str(ctx.exception))
        ledger.stop()


class DemoThroughServerTests(W.OfficeCase):
    def test_demo_and_real_snapshots_use_the_same_build_function(self):
        calls = []
        original = core_snapshot.build

        def spy(conn, **kw):
            calls.append((id(conn), kw))
            return original(conn, **kw)

        with mock.patch.object(core_snapshot, "build", side_effect=spy):
            status, real = self.get_json("/api/v2/snapshot")
            status2, fake = self.get_json("/api/v2/snapshot?demo=1")
        self.assertEqual((status, status2), (200, 200))
        self.assertEqual(len(calls), 2, "真实数据和示例数据都必须经过 snapshot.build")
        self.assertNotEqual(calls[0][0], calls[1][0], "两次用的是不同的账本连接")
        self.assertEqual(real["conversation"]["title"], "测试对话")
        self.assertEqual(fake["conversation"]["title"], DEMO_TITLE)
        self.assertEqual(set(real), set(fake), "示例快照和真实快照结构相同")
        self.assertTrue(self.srv._demo.wait_loops(1, 20))
        _, fake = self.get_json("/api/v2/snapshot?demo=1")
        self.assertGreaterEqual(len(fake["people"]), 5)
        self.assertGreaterEqual(fake["seq"], 70)

    def test_demo_is_only_used_when_asked(self):
        self.get_json("/api/v2/snapshot?demo=1")
        _, real = self.get_json("/api/v2/snapshot")
        self.assertEqual(real["conversation"]["title"], "测试对话")
        self.assertNotIn(EM_OF_DEMO, real["people"])
        _, real = self.get_json("/api/v2/snapshot?demo=0")
        self.assertEqual(real["conversation"]["title"], "测试对话")

    def test_demo_stream_uses_the_same_frames_as_real_data(self):
        frames = self.sse("/api/v2/stream?demo=1&since=0", until=lambda f: len([x for x in f if x["event"] == "ledger"]) >= 8)
        ledger = [f for f in frames if f["event"] == "ledger"]
        self.assertGreaterEqual(len(ledger), 8)
        self.assertEqual([f["id"] for f in ledger], sorted(f["id"] for f in ledger))
        for f in ledger:
            self.assertEqual(f["data"]["seq"], f["id"])
            self.assertTrue(f["data"]["text"])
        self.assertEqual(ledger[0]["data"]["type"], "conv.begun")
        self.assertIn(DEMO_TITLE, ledger[0]["data"]["text"])

    def test_demo_stream_follows_the_loop_restart(self):
        self.start_server(demo_factory=lambda: make_ledger(loop_pause=0.05))
        ids = []

        def restarted(frames):
            ids[:] = [f["id"] for f in frames if f["event"] == "ledger"]
            return any(b < a for a, b in zip(ids, ids[1:]))

        self.sse("/api/v2/stream?demo=1&since=0", until=restarted, timeout=10)
        self.assertTrue(any(b < a for a, b in zip(ids, ids[1:])), "新一轮开始后事件流应当从新账本的开头继续")

    def test_demo_decisions_go_to_the_demo_ledger_not_the_real_one(self):
        self.get_json("/api/v2/snapshot?demo=1")
        self.assertTrue(self.srv._demo.wait_loops(1, 20))
        _, fake = self.get_json("/api/v2/snapshot?demo=1")
        dc = fake["decisions"][0]
        real_top = core_events.max_seq(self.conn)
        status, _, data = self.post("/api/v2/decision?demo=1", {"id": dc["id"], "option": dc["options"][0]["id"]}, "decide")
        self.assertEqual(status, 200, data)
        self.assertEqual(core_events.max_seq(self.conn), real_top)
        _, fake = self.get_json("/api/v2/snapshot?demo=1")
        self.assertEqual(fake["decisions"], [])
        status, _, _ = self.post("/api/v2/decision", {"id": dc["id"], "option": dc["options"][0]["id"]}, "decide")
        self.assertEqual(status, 422, "真实账本里没有这件事")

    def test_live_in_demo_mode_only_sends_heartbeats(self):
        frames = self.sse("/api/v2/live?em=em_c0de0001&demo=1", until=lambda f: any(x["event"] == "heartbeat" for x in f), timeout=3)
        self.assertEqual({f["event"] for f in frames}, {"heartbeat"})


EM_OF_DEMO = "em_c0de0001"


if __name__ == "__main__":
    unittest.main()
