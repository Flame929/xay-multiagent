"""四条线路的真实额度（2026-10-31，V2/xay/quota_live.py）：

- 解析：用旧系统 tests/test_quota.py 里的真实样例（已脱敏）+ 中转站"今天用完了、还能用 $0"的样子；
- 刷新：假的查询函数 → quota.updated（by=office）；没变不重复写；查不到不清掉旧读数；agy 慢也不挡别的线路；
  账本、快照里绝不出现 key、邮箱、登录信息；
- 快照：四条线路总在；工位写的和管家查的谁新用谁；工位在中转站上报的空窗口不盖掉管家查到的；换了 Gemini 账号旧读数作废；
- 管家决策：要换去的线路已经用完 → 工位不换、"需要你"里写明"中转站今天也用完了"；某种 AI 全用完了 → 活先排着 + 提醒；
- 页面：quota-view.js 的额度条（node 里给个假 DOM）。

全部用临时 XAY2_HOME / XAY_HOME / Antigravity Tools 目录，不联网、不起真 CLI。
"""
import json
import os
import shutil
import subprocess
import tempfile
import threading
import time
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest import mock

from xay import quota as agy_quota
from xay import quota_live as ql
from xay.core import append, load_state, open_db, since
from xay.core import snapshot as core_snapshot
from xay.office import employee, reactions

from tests.test_rules import RulesCase
from tests.test_station import Factory, StationCase

WEB = Path(__file__).resolve().parents[1] / "web"
SECRET = "SECRET-SHOULD-NOT-LEAK"


def win(key, left, resets_at=None, name=None, mins=None):
    w = {"key": key, "name": name or {"5h": "5 小时", "day": "今天", "week": "本周", "month": "本月"}.get(key, key),
         "remaining_pct": left, "resets_at": resets_at}
    if mins:
        w["mins"] = mins
    return w


def fake(windows, **extra):
    calls = []

    def f(timeout=None, now=None):
        calls.append(now)
        return dict({"windows": [dict(w) for w in windows], "source": "测试"}, **extra)
    f.calls = calls
    return f


def broken(text):
    def f(timeout=None, now=None):
        raise ql.LiveError(text)
    return f


class ParseTests(unittest.TestCase):
    """旧系统的真实样例（tests/test_quota.py，已脱敏），移植后结果一样。"""

    def test_four_official_sources(self):
        now = 1_800_000_000
        dsh = ql.parse_opencode_usage({"usage": {
            "rolling": {"status": "ok", "percent": 10, "resetsAt": "2027-01-15T08:00:00.000Z"},
            "weekly": {"status": "ok", "percent": 46, "resetsAt": "2027-01-18T00:00:00.000Z"},
            "monthly": {"status": "ok", "percent": 42, "resetsAt": "2027-02-01T00:00:00.000Z"}}})
        self.assertEqual([(w["key"], w["remaining_pct"]) for w in dsh["windows"]], [("5h", 90), ("week", 54), ("month", 58)])
        relay = ql.parse_relay_usage({"isValid": True, "unit": "USD", "remaining": 355.9, "subscription": {
            "daily_limit_usd": 500, "daily_usage_usd": 144.09, "weekly_limit_usd": None, "monthly_limit_usd": None,
            "weekly_window_start": "2027-01-11T00:00:00+08:00", "expires_at": "2027-03-01T00:00:00+08:00"}}, now)
        self.assertEqual([(w["key"], w["name"], w["remaining_pct"]) for w in relay["windows"]], [("day", "今天", 71)])
        self.assertIsNone(relay["windows"][0]["resets_at"])          # 中转站不给"今天"的重置时间：不猜
        self.assertEqual((relay["balance"], relay["balance_usd"]), ("还能用 $356", 355.9))
        with self.assertRaises(ql.LiveError):
            ql.parse_relay_usage({"isValid": False}, now)
        codex = ql.parse_codex_rate_limits({"rateLimits": {
            "primary": {"usedPercent": 0, "windowDurationMins": 300, "resetsAt": now + 100},
            "secondary": {"usedPercent": 63, "windowDurationMins": 10080, "resetsAt": now + 9000}}})
        self.assertEqual([(w["key"], w["remaining_pct"], w["mins"]) for w in codex["windows"]], [("5h", 100, 300), ("week", 37, 10080)])
        agy = ql.parse_agy_usage(
            "Gemini Models\tWeekly Limit Remaining\t67%\t2027-01-15T16:26:57Z\n"
            "Gemini Models\tFive Hour Limit Remaining\t66%\t2027-01-15T06:28:05Z\n"
            "Claude and GPT models\tWeekly Limit Remaining\t3%\t2027-01-20T05:59:00Z\n"
            "Claude and GPT models\tFive Hour Limit Remaining\tDisabled\t\n")
        self.assertEqual([(w["key"], w["remaining_pct"]) for w in agy["windows"]], [("week", 67), ("5h", 66)])   # 只算 Gemini 那组
        with self.assertRaises(ql.LiveError):
            ql.parse_agy_usage("没有额度")

    def test_relay_used_up_for_today(self):
        """真机 2026-10-31：中转站今天的额度用完了、还能用 $0（早上 9 点左右才重置，所以不写重置时间）。"""
        now = time.mktime((2026, 10, 31, 15, 0, 0, 0, 0, -1))
        relay = ql.parse_relay_usage({"isValid": True, "unit": "USD", "remaining": 0, "subscription": {
            "daily_limit_usd": 100, "daily_usage_usd": 100.4, "weekly_limit_usd": None, "monthly_limit_usd": None}}, now)
        self.assertEqual([(w["key"], w["remaining_pct"]) for w in relay["windows"]], [("day", 0)])
        self.assertIsNone(relay["windows"][0]["resets_at"])
        self.assertEqual(relay["balance"], "还能用 $0")
        data = ql.to_ledger("codex-relay", dict(relay, source="中转站用量接口"))
        self.assertEqual(data["windows"], [{"key": "day", "mins": 1440, "used_pct": 100.0}])   # 不知道重置时间就不写
        self.assertEqual((data["balance"], data["balance_usd"]), ("还能用 $0", 0.0))

    def test_errors_never_carry_keys(self):
        class HTTPErr(Exception):
            pass
        with mock.patch("urllib.request.urlopen", side_effect=ql.urllib.error.HTTPError("u", 401, "no", {}, None)):
            with self.assertRaises(ql.LiveError) as cm:
                ql._http_json("https://example.invalid/usage", SECRET, "中转站用量接口")
        self.assertEqual(str(cm.exception), "中转站用量接口返回 HTTP 401（key 无效或无权限）")
        self.assertNotIn(SECRET, str(cm.exception))


class LedgerCase(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="xay-quota-live-"))
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.tools = self.tmp / "tools"
        env = mock.patch.dict(os.environ, {"XAY2_HOME": str(self.tmp / "v2"), "XAY_HOME": str(self.tmp / "old"),
                                           "XAY_AGY_TOOLS_HOME": str(self.tools)})
        env.start()
        self.addCleanup(env.stop)
        os.environ.pop("XAY2_QUOTA_LIVE", None)
        self.conn = open_db()
        self.addCleanup(self.conn.close)
        self.now = time.time()

    def office(self, data, ts=None):
        return append(self.conn, "quota.updated", "office", data, ts=ts)

    def lines(self, now=None):
        return {q["line"]: q for q in core_snapshot.quota_lines(self.conn, self.now if now is None else now)}

    def ledger_text(self):
        return json.dumps([e["data"] for e in since(self.conn, 0, limit=100000)], ensure_ascii=False)

    def make_tools(self, current="a1", accounts=None):
        """假的 Antigravity Tools 清单：账号文件里故意带邮箱和登录信息，看它们一个字都不进账本。"""
        iso = lambda ts: datetime.fromtimestamp(ts, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        accounts = accounts or [("a1", "主号", 2, 0.60, False), ("a2", "备用甲", 1, 0.0, False), ("a3", "备用乙", 1, 0.95, True)]
        (self.tools / "accounts").mkdir(parents=True, exist_ok=True)
        index = {"current_agy_account_id": current, "accounts": []}
        for aid, name, prio, frac, disabled in accounts:
            index["accounts"].append({"id": aid, "name": name, "email": aid + "@example.com", "disabled": disabled})
            (self.tools / "accounts" / (aid + ".json")).write_text(json.dumps({
                "id": aid, "email": aid + "@example.com", "name": name, "priority": prio,
                "token": {"access_token": SECRET, "refresh_token": SECRET},
                "quota": {"last_updated": self.now - 600, "quota_groups": [
                    {"display_name": "Gemini Models", "buckets": [
                        {"window": "weekly", "remaining_fraction": frac, "reset_time": iso(self.now + 86400)}]},
                    {"display_name": "Claude and GPT models", "buckets": [
                        {"window": "weekly", "remaining_fraction": 0.0, "reset_time": iso(self.now + 86400)}]}]}}), encoding="utf-8")
        (self.tools / "accounts.json").write_text(json.dumps(index), encoding="utf-8")

    def switch_to(self, aid):
        p = self.tools / "accounts.json"
        d = json.loads(p.read_text(encoding="utf-8"))
        d["current_agy_account_id"] = aid
        p.write_text(json.dumps(d), encoding="utf-8")


class RefreshTests(LedgerCase):
    def fetchers(self, **over):
        f = {"codex-sub": fake([win("5h", 99, self.now + 3600, mins=300), win("week", 34, self.now + 4 * 86400, mins=10080)]),
             "codex-relay": fake([win("day", 0, self.now + 3600)], balance="还能用 $0", balance_usd=0.0),
             "dsh": fake([win("5h", 100, self.now + 3600), win("week", 52, self.now + 86400), win("month", 57, self.now + 9 * 86400)]),
             "agy": fake([win("week", 60, self.now + 86400), win("5h", 99, self.now + 3600)], account_id="a1")}
        f.update(over)
        return f

    def test_four_lines_land_in_the_snapshot(self):
        self.make_tools()
        r = ql.Refresher(self.conn, fetchers=self.fetchers())
        self.assertEqual(r.run_once(self.now), 4)
        rows = [e for e in since(self.conn, 0) if e["type"] == "quota.updated"]
        self.assertEqual({(e["by"], e["subject"]) for e in rows}, {("office", None)})
        q = self.lines()
        self.assertEqual(list(q), ["codex-sub", "codex-relay", "dsh", "agy"])
        self.assertEqual([(w["name"], w["remaining_pct"]) for w in q["codex-sub"]["windows"]], [("5 小时", 99), ("本周", 34)])
        self.assertEqual((q["codex-sub"]["remaining_pct"], q["codex-sub"]["status"]), (34, "ok"))
        relay = q["codex-relay"]
        self.assertEqual((relay["remaining_pct"], relay["status"], relay["balance"]), (0, "exhausted", "还能用 $0"))
        self.assertEqual([(w["name"], w["remaining_pct"]) for w in q["dsh"]["windows"]], [("5 小时", 100), ("本周", 52), ("本月", 57)])
        agy = q["agy"]
        self.assertEqual((agy["remaining_pct"], agy["account"]), (60, "主号"))
        self.assertEqual([(a["name"], a["current"], a["status"]) for a in agy["accounts"]],
                         [("主号", True, "ok"), ("备用甲", False, "exhausted"), ("备用乙", False, "blocked")])
        # 账本、快照里只有百分比、重置时间、账号显示名：没有 key、邮箱、登录信息、账号编号
        for text in (self.ledger_text(), json.dumps(list(q.values()), ensure_ascii=False)):
            for bad in (SECRET, "example.com", "access_token", '"a1"', "a2"):
                self.assertNotIn(bad, text)

    def test_unchanged_readings_are_not_rewritten(self):
        r = ql.Refresher(self.conn, fetchers=self.fetchers())
        r.run_once(self.now)
        self.assertEqual(r.run_once(self.now + 10), 0)                          # 一样的读数：不写
        r.fetchers["dsh"] = fake([win("5h", 99.6, self.now + 3600), win("week", 52, self.now + 86400), win("month", 57, self.now + 9 * 86400)])
        self.assertEqual(r.run_once(self.now + 20, lines=["dsh"]), 0)            # 差不到 1 个百分点：不写
        r.fetchers["dsh"] = fake([win("5h", 97, self.now + 3600), win("week", 52, self.now + 86400), win("month", 57, self.now + 9 * 86400)])
        self.assertEqual(r.run_once(self.now + 30, lines=["dsh"]), 1)
        self.assertEqual(r.run_once(self.now + 30 + 901, lines=["dsh"]), 1)      # 一直没变：每 15 分钟也写一次
        # 管家重启：从账本里接上，不重复写
        r2 = ql.Refresher(self.conn, fetchers={"dsh": r.fetchers["dsh"]})
        self.assertEqual(r2.run_once(self.now + 940, lines=["dsh"]), 0)

    def test_failure_keeps_the_last_good_reading(self):
        r = ql.Refresher(self.conn, fetchers=self.fetchers())
        r.run_once(self.now)
        r.fetchers["codex-relay"] = broken("连不上中转站用量接口")
        self.assertEqual(r.run_once(self.now + 60, lines=["codex-relay"]), 1)
        self.assertEqual(r.run_once(self.now + 120, lines=["codex-relay"]), 0)    # 同样的原因：不重复写
        relay = self.lines(self.now + 130)["codex-relay"]
        self.assertEqual((relay["remaining_pct"], relay["status"], relay["error"]), (0, "exhausted", "连不上中转站用量接口"))
        # 一直查不到、读数太旧了：还给页面看，但不再当真
        old = self.lines(self.now + core_snapshot.QUOTA_MAX_AGE + 5)["codex-relay"]
        self.assertEqual((old["remaining_pct"], old["status"], old["stale"]), (0, "unknown", True))
        # 意外的错误：只写一句大白话，不写异常原文
        def boom(timeout=None, now=None):
            raise RuntimeError("Authorization: Bearer " + SECRET)
        r.fetchers["dsh"] = boom
        r.run_once(self.now + 200, lines=["dsh"])
        self.assertEqual(self.lines(self.now + 210)["dsh"]["error"], "查询时出了意外错误")
        self.assertNotIn(SECRET, self.ledger_text())

    def test_slow_agy_does_not_hold_up_other_lines(self):
        gate = threading.Event()

        def slow(timeout=None, now=None):
            gate.wait(5)
            return {"windows": [win("week", 60, self.now + 86400)]}
        r = ql.Refresher(self.conn, fetchers=self.fetchers(agy=slow))
        r.start(["dsh", "agy"], self.now, 30)
        deadline = time.time() + 5
        while "dsh" in r.busy and time.time() < deadline:
            r.collect(self.now)
            time.sleep(0.01)
        self.assertNotIn("dsh", r.busy)
        self.assertIn("agy", r.busy)                                             # agy 还在查：不等它
        self.assertEqual(self.lines()["dsh"]["status"], "ok")
        self.assertEqual(r.due(self.now + 200, ql.load_cfg()), ["codex-sub", "codex-relay"])   # 查着的不重复开
        r.due(self.now + 211, ql.load_cfg())
        self.assertNotIn("agy", r.busy)                                          # 查了太久（超时的两倍多）：当它卡死了，到点重新查
        gate.set()

    def test_due_intervals_and_agy_account_switch(self):
        self.make_tools()
        r = ql.Refresher(self.conn, fetchers=self.fetchers())
        cfg = ql.load_cfg()
        self.assertEqual((cfg["refresh_s"], cfg["agy_refresh_s"]), (300, 600))
        r.run_once(self.now)
        self.assertEqual(r.due(self.now + 100, cfg), [])
        self.assertEqual(r.due(self.now + 301, cfg), ["codex-sub", "codex-relay", "dsh"])
        self.assertEqual(r.due(self.now + 601, cfg), ["codex-sub", "codex-relay", "dsh", "agy"])
        self.switch_to("a2")                                                     # 工位刚换了 Gemini 账号
        self.assertEqual(r.due(self.now + 100, cfg), ["agy"])
        # 新账号的读数还没查到之前：旧账号的读数不算数
        r.fetchers["agy"] = broken("agy /usage 超时")
        r.run_once(self.now + 110, lines=["agy"])
        agy = self.lines(self.now + 120)["agy"]
        self.assertEqual((agy["account"], agy["remaining_pct"], agy["status"]), ("备用甲", None, "unknown"))
        self.assertEqual(agy["error"], "agy /usage 超时")
        # reactions.json 可以改间隔
        (self.tmp / "v2" / "reactions.json").write_text(json.dumps({"quota": {"refresh_s": 60, "agy_refresh_s": 0}}), encoding="utf-8")
        reactions._cache["cfg"] = None
        cfg = ql.load_cfg()
        self.assertEqual((cfg["refresh_s"], cfg["agy_refresh_s"]), (60, 0))
        self.assertEqual(r.due(self.now + 150, cfg), ["codex-sub", "codex-relay", "dsh"])
        self.assertIn("agy", r.due(self.now + 171, cfg))                            # agy_refresh_s=0：跟着 refresh_s

    def test_reading_taken_while_switching_accounts_is_dropped(self):
        self.make_tools()
        r = ql.Refresher(self.conn, fetchers=self.fetchers(agy=fake([win("week", 60, self.now + 86400)])))   # 没带 account_id
        r.run_once(self.now, lines=["agy"])
        agy = self.lines()["agy"]
        self.assertEqual((agy["remaining_pct"], agy["error"]), (None, "查的时候刚好换了账号，等下一次"))

    def test_live_queries_off_for_temporary_homes_and_thread_runs_when_forced(self):
        self.assertFalse(ql.live_enabled())
        with mock.patch.dict(os.environ, {"XAY2_QUOTA_LIVE": "1"}):
            self.assertTrue(ql.live_enabled())
            t = ql.QuotaThread(db_path=str(self.tmp / "v2" / "xay.db"), fetchers=self.fetchers(), tick_s=0.05)
            t.start()
            try:
                deadline = time.time() + 10
                while time.time() < deadline and self.lines(time.time())["agy"]["status"] == "unknown":
                    time.sleep(0.05)
            finally:
                t.stop()
        self.assertEqual([q["status"] for q in self.lines(time.time()).values()], ["ok", "exhausted", "ok", "ok"])


class SnapshotMergeTests(LedgerCase):
    def station(self, em, data, ts=None):
        append(self.conn, "employee.hired", "office", {"agent": "codex"}, ws="ws_q", subject=em) if em not in load_state(self.conn)["employees"] else None
        return append(self.conn, "quota.updated", "station:" + em, data, subject=em, ts=ts)

    def test_newest_wins_and_relay_nulls_do_not_erase(self):
        em = "em_qq000001"
        self.office({"line": "codex-sub", "windows": [{"key": "5h", "mins": 300, "used_pct": 1}], "source": "Codex 官方额度查询"}, self.now - 100)
        self.station(em, {"line": "codex-sub", "windows": [{"key": "5h", "mins": 300, "used_pct": 40}]}, self.now - 50)
        q = self.lines()["codex-sub"]
        self.assertEqual((q["remaining_pct"], q["source_label"], q["em"]), (60, "Codex 运行时上报", em))
        self.office({"line": "codex-sub", "windows": [{"key": "5h", "mins": 300, "used_pct": 45}], "source": "Codex 官方额度查询"}, self.now - 10)
        q = self.lines()["codex-sub"]
        self.assertEqual((q["remaining_pct"], q["source_label"], q["em"]), (55, "Codex 官方额度查询", None))
        # 工位在中转站上：通知里两个窗口都是 null → 不盖掉管家查到的中转站额度
        self.office({"line": "codex-relay", "windows": [{"key": "day", "mins": 1440, "used_pct": 100, "resets_at": self.now + 3600}],
                     "balance": "还能用 $0", "balance_usd": 0}, self.now - 20)
        self.station(em, {"line": "codex-relay", "windows": []}, self.now - 5)
        relay = self.lines()["codex-relay"]
        self.assertEqual((relay["status"], relay["balance"]), ("exhausted", "还能用 $0"))
        # 过了重置时间：窗口不列，不再算用完
        self.assertEqual(self.lines(self.now + 3601)["codex-relay"]["status"], "unknown")

    def test_person_quota_uses_the_employees_line(self):
        ws, cv = "ws_q0000002", "cv_q2"
        lead = "lead:" + cv
        append(self.conn, "conv.begun", lead, {"title": "额度", "lead_kind": "claude", "aliases": [], "cwd": "/tmp/x"}, ws=ws, conv=cv, subject=cv)
        ems = {"codex": "em_qx000001", "dsh": "em_qx000002", "agy": "em_qx000003"}
        for agent, em in ems.items():
            append(self.conn, "employee.hired", lead, {"agent": agent}, ws=ws, conv=cv, subject=em)
        t = self.now
        self.office({"line": "codex-sub", "windows": [{"key": "5h", "mins": 300, "used_pct": 1, "resets_at": t + 3600},
                                                       {"key": "week", "mins": 10080, "used_pct": 66, "resets_at": t + 86400}]}, t)
        self.office({"line": "codex-relay", "windows": [{"key": "day", "mins": 1440, "used_pct": 100, "resets_at": t + 3600}],
                     "balance": "还能用 $0", "balance_usd": 0}, t)
        self.office({"line": "dsh", "windows": [{"key": "5h", "mins": 300, "used_pct": 0}, {"key": "week", "mins": 10080, "used_pct": 48},
                                                 {"key": "month", "mins": 43200, "used_pct": 43}]}, t)
        self.office({"line": "agy", "windows": [{"key": "week", "mins": 10080, "used_pct": 40, "resets_at": t + 86400}], "account": "主号",
                     "accounts": [{"index": 1, "name": "主号", "current": True, "status": "ok", "left": 60},
                                  {"index": 2, "name": "备用甲", "current": False, "status": "exhausted", "left": 0}]}, t)

        def pq(agent):
            return employee.build(self.conn, ems[agent], conv=cv, now=t + 1, quota_home=self.tmp / "v2")["person"]["quota"]
        q = pq("codex")
        self.assertEqual((q["kind"], q["line_name"], q["left"], [w["name"] for w in q["windows"]]), ("pct", "Plus", 34, ["5 小时", "本周"]))
        append(self.conn, "employee.session", "station:" + ems["codex"], {"native_id": "th1", "segment": 1, "reason": "line_switch",
                                                                          "line": "codex-relay"}, ws=ws, subject=ems["codex"])
        q = pq("codex")
        self.assertEqual((q["line_name"], q["left"], q["status"], q["balance"], q["windows"][0]["name"]), ("中转站", 0, "exhausted", "还能用 $0", "今天"))
        q = pq("dsh")
        self.assertEqual((q["kind"], q["left"], [w["name"] for w in q["windows"]]), ("pct", 52, ["5 小时", "本周", "本月"]))
        q = pq("agy")
        self.assertEqual((q["kind"], q["left"], q["account"], len(q["accounts"])), ("pct", 60, "主号", 2))
        self.assertNotIn("calls", q)          # Gemini 默认不限每天调用次数（2026-10-31）
        body = json.dumps([pq(a) for a in ems], ensure_ascii=False)
        self.assertNotIn("这家看不到", body)
        self.assertNotIn("中转站看不到", body)

    def test_decision_helpers(self):
        t = self.now
        self.office({"line": "codex-relay", "windows": [{"key": "day", "mins": 1440, "used_pct": 100, "resets_at": t + 3600}]}, t)
        quota = core_snapshot.quota_lines(self.conn, t)
        self.assertTrue(ql.line_gone_text(self.conn, "codex-relay", t).startswith("中转站今天也用完了（"))
        self.assertIsNone(ql.line_gone_text(self.conn, "codex-sub", t))
        self.assertIsNone(ql.agent_out(quota, "codex", t))                          # Plus 还不知道：不拦
        self.office({"line": "codex-sub", "windows": [{"key": "week", "mins": 10080, "used_pct": 100, "resets_at": t + 7200}]}, t)
        quota = core_snapshot.quota_lines(self.conn, t)
        self.assertTrue(ql.agent_out(quota, "codex", t).startswith("Codex 两条线路都用完了：Plus本周用完了"))
        self.assertTrue(ql.agent_out(quota, "codex", t, confidential=True).startswith("Codex Plus都用完了"))
        self.office({"line": "agy", "windows": [{"key": "week", "mins": 10080, "used_pct": 100, "resets_at": t + 7200}], "account": "主号",
                     "accounts": [{"name": "主号", "current": True, "status": "exhausted", "left": 0},
                                  {"name": "备用甲", "current": False, "status": "ok", "left": 80}]}, t)
        quota = core_snapshot.quota_lines(self.conn, t)
        self.assertIsNone(ql.agent_out(quota, "agy", t))                            # 还有别的号能换
        self.assertEqual(ql.used_up_accounts(ql.entry(quota, "agy")), ["主号"])


class AgySkipTests(unittest.TestCase):
    def test_pick_next_skips_used_up_accounts(self):
        accounts = [{"index": 1, "current": True, "name": "主号", "ok": True}, {"index": 2, "current": False, "name": "备用甲", "ok": True},
                    {"index": 3, "current": False, "name": "备用乙", "ok": True}]
        self.assertEqual(agy_quota._pick_next(accounts)["name"], "备用甲")
        self.assertEqual(agy_quota._pick_next(accounts, skip=["备用甲"])["name"], "备用乙")
        self.assertIsNone(agy_quota._pick_next(accounts, skip=["备用甲", "备用乙"]))


class OfficeDecisionTests(RulesCase):
    def office(self, line, used, resets_in=3600, key=None):
        key = key or {"codex-sub": "week", "codex-relay": "day", "dsh": "week", "agy": "week"}[line]
        append(self.conn, "quota.updated", "office", {"line": line, "windows": [
            {"key": key, "mins": ql.WINDOW_MINS[key], "used_pct": used, "resets_at": int(time.time() + resets_in)}]})

    def test_quota_failure_says_the_other_line_is_used_up_too(self):
        em = self.hire()
        tk = self.add("额度不够的活", owner=em)
        s = self.sched()
        s.tick()
        self.office("codex-relay", 100)                     # 中转站今天也用完了：工位没换过去
        self.failed(em, tk, error="You've hit your usage limit")
        s.tick()
        dec = self.decisions("failure")
        self.assertEqual(len(dec), 1)
        self.assertEqual(dec[0]["cause"], "quota")
        self.assertIn("中转站今天也用完了", dec[0]["detail"])
        self.assertIn("没有换过去", dec[0]["detail"])
        self.assertEqual([e for e in self.events("task.queued") if e["data"].get("retry")], [])

    def test_all_codex_lines_used_up_holds_the_work_and_warns_once(self):
        em = self.hire()
        self.office("codex-sub", 100)
        self.office("codex-relay", 100)
        tk = self.add("要排着的活", owner=em)
        t2 = self.add("没指定人的活", agent="codex")
        s = self.sched()
        s.tick()
        s.tick()
        self.assertEqual((self.status(tk), self.status(t2)), ("ready", "ready"))     # 先排着：不派、不起工位
        self.assertEqual(self.spawned, [])
        sig = [e for e in self.events("signal.raised") if e["by"] == "rules" and e["data"]["kind"] == "quota"]
        self.assertEqual(len(sig), 1)
        self.assertEqual(sig[0]["data"]["level"], "warn")
        self.assertIn("Codex 两条线路都用完了", sig[0]["data"]["text"])
        self.assertIn("先排着", sig[0]["data"]["text"])
        # 额度恢复了（Plus 新读数）→ 接着派
        self.office("codex-sub", 30)
        s.tick()
        self.assertEqual((self.status(tk), self.status(t2)), ("queued", "queued"))
        self.assertIn(em, self.spawned)

    def test_dsh_used_up_holds_its_queue(self):
        em = self.hire("dsh")
        self.office("dsh", 100)
        tk = self.add("DeepSeek 的活", owner=em)
        s = self.sched()
        s.tick()
        self.assertEqual(self.status(tk), "ready")
        self.assertEqual(self.spawned, [])
        sig = [e for e in self.events("signal.raised") if e["by"] == "rules"]
        self.assertIn("DeepSeek 额度用完了", sig[0]["data"]["text"])


class StationQuotaTests(StationCase):
    CAP = [{"fail": {"kind": "capacity", "error": "Selected model is at capacity"}}]

    def office(self, line, used, key):
        append(self.conn, "quota.updated", "office", {"line": line, "windows": [
            {"key": key, "mins": ql.WINDOW_MINS[key], "used_pct": used, "resets_at": int(time.time() + 3600)}]})

    def test_no_switch_to_a_used_up_line(self):
        self.office("codex-relay", 100, "day")
        f = Factory([[self.CAP]])
        tk = self.add_task("满载了，中转站也用完了")
        self.run_station(f)
        self.assertEqual(len(f.instances), 1)                                        # 没开第二条线路
        self.assertEqual([e["data"]["reason"] for e in self.events("employee.session")], ["new"])
        self.assertEqual(self.state()["tasks"][tk]["status"], "failed")

    def test_all_lines_used_up_the_task_waits(self):
        self.office("codex-relay", 100, "day")
        self.office("codex-sub", 100, "week")
        f = Factory([[]])
        tk = self.add_task("先排着")
        self.run_station(f)
        self.assertEqual(f.instances, [])                                             # 没开工
        self.assertEqual(self.state()["tasks"][tk]["status"], "queued")
        self.assertEqual(self.events("turn.started"), [])


@unittest.skipUnless(shutil.which("node"), "没装 node，跳过额度条的页面检查")
class QuotaStripPageTests(unittest.TestCase):
    """quota-view.js：四个小格，用完的标红，鼠标停留（title）里有重置时间；没有快照数据时整条藏起来。"""

    def test_strip(self):
        tmp = Path(tempfile.mkdtemp(prefix="xay-quota-js-"))
        self.addCleanup(shutil.rmtree, tmp, True)
        for name in ("utils.js", "quota-view.js"):
            shutil.copy(WEB / "lib" / name, tmp / name)
        (tmp / "package.json").write_text('{"type": "module"}', encoding="utf-8")
        now = 1793300000
        quota = [
            {"line": "codex-sub", "agent": "codex", "label": "Codex · Plus", "short": "Plus", "status": "ok", "remaining_pct": 34,
             "windows": [{"key": "5h", "name": "5 小时", "remaining_pct": 99, "resets_at": now + 3600},
                         {"key": "week", "name": "本周", "remaining_pct": 34, "resets_at": now + 4 * 86400}], "fetched_at": now - 120,
             "source_label": "Codex 官方额度查询"},
            {"line": "codex-relay", "agent": "codex", "label": "Codex · 中转站", "short": "中转站", "status": "exhausted", "remaining_pct": 0,
             "windows": [{"key": "day", "name": "今天", "remaining_pct": 0, "resets_at": now + 3600}], "balance": "还能用 $0", "fetched_at": now - 60},
            {"line": "dsh", "agent": "dsh", "label": "DeepSeek", "short": "DeepSeek", "status": "unknown", "remaining_pct": None, "windows": [],
             "error": "连不上 OpenCode 用量接口"},
            {"line": "agy", "agent": "agy", "label": "Gemini", "short": "Gemini", "status": "tight", "remaining_pct": 12, "account": "主号",
             "windows": [{"key": "week", "name": "本周", "remaining_pct": 12, "resets_at": now + 86400}],
             "accounts": [{"name": "主号", "current": True, "status": "tight", "left": 12}, {"name": "备用<甲>", "current": False, "status": "ok", "left": 90}]},
        ]
        script = """
import { renderQuotaStrip, resetText } from './quota-view.js';
const el = { innerHTML: 'x', hidden: true };
renderQuotaStrip(el, %s, %d);
const empty = { innerHTML: 'x', hidden: false };
renderQuotaStrip(empty, [], %d);
console.log(JSON.stringify({ html: el.innerHTML, hidden: el.hidden, emptyHidden: empty.hidden, emptyHtml: empty.innerHTML,
  same: resetText(%d, %d), tomorrow: resetText(%d, %d) }));
""" % (json.dumps(quota, ensure_ascii=False), now, now, now + 60, now, now + 86400, now)
        (tmp / "run.mjs").write_text(script, encoding="utf-8")
        out = subprocess.run(["node", str(tmp / "run.mjs")], capture_output=True, text=True, timeout=30)
        self.assertEqual(out.returncode, 0, out.stderr)
        got = json.loads(out.stdout)
        html = got["html"]
        self.assertFalse(got["hidden"])
        self.assertTrue(got["emptyHidden"])
        self.assertEqual(got["emptyHtml"], "")
        self.assertEqual(html.count('class="q-cell'), 4)
        for need in ('q-cell q-ok', 'q-cell q-exhausted', 'q-cell q-unknown', 'q-cell q-tight', '>Plus<', '>中转站<', '>34%<', '>0%<', '>—<',
                     "本周剩 34%（", "今天剩 0%（", "还能用 $0", "最近一次没查到：连不上 OpenCode 用量接口", "Gemini · 主号", "备用&lt;甲&gt;"):
            self.assertIn(need, html)
        self.assertTrue(got["tomorrow"].startswith("明天 "))
        self.assertNotIn("明天", got["same"])

    def test_page_wiring(self):
        html = (WEB / "org.html").read_text(encoding="utf-8")
        self.assertIn('id="quota-strip"', html)
        app = (WEB / "lib" / "app.js").read_text(encoding="utf-8")
        self.assertIn("renderQuotaStrip(document.getElementById('quota-strip'), data.quota, data.now)", app)
        panel = (WEB / "lib" / "employee-panel.js").read_text(encoding="utf-8")
        for need in ("q.balance", "q.account", "accountsHtml", "data-act=\"accts\"", "今天还能调"):
            self.assertIn(need, panel)
        css = (WEB / "org.css").read_text(encoding="utf-8")
        for need in (".quota-strip", ".q-exhausted", ".emp-accts"):
            self.assertIn(need, css)


if __name__ == "__main__":
    unittest.main()
