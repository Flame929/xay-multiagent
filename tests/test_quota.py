"""额度读取和线路选择的本机记录测试。"""

import json
import os
import tempfile
import time
import unittest
from datetime import datetime, timezone
from pathlib import Path

from scripts import xay_quota


class QuotaTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="xay-quota-")
        self.home = Path(self.tmp.name)
        self.runs = self.home / "runs"
        self.runs.mkdir()
        self.registry = []

    def tearDown(self):
        self.tmp.cleanup()

    def add_run(self, run_id, agent, start, meta, events=(), raw=()):
        rd = self.runs / run_id
        rd.mkdir()
        (rd / "start.json").write_text(json.dumps({"agent": agent, **start}), encoding="utf-8")
        (rd / "meta.json").write_text(json.dumps({"agent": agent, **meta}), encoding="utf-8")
        (rd / "events.norm.jsonl").write_text("".join(json.dumps(x) + "\n" for x in events), encoding="utf-8")
        (rd / "events.raw").write_text("".join(json.dumps(x) + "\n" for x in raw), encoding="utf-8")
        self.registry.append({"kind": "run", "run": run_id, "run_dir": str(rd), "agent": agent})
        return rd

    def write_registry(self):
        (self.home / "registry.jsonl").write_text("".join(json.dumps(x) + "\n" for x in self.registry), encoding="utf-8")

    def line(self, data, name):
        return next(x for x in data if x["line"] == name)

    def test_usage_windows_and_cached_input_are_counted_once(self):
        now = 1_800_000_000
        self.add_run("dsh-a", "dsh", {"started": now - 100}, {"status": "done", "ended": now - 50}, [
            {"type": "usage", "ts": now - 1800, "data": {"in": 1000, "cached": 300, "out": 200, "granularity": "turn"}},
            {"type": "usage", "ts": now - 1800, "data": {"in": 1000, "cached": 300, "out": 200, "granularity": "turn"}},
            {"type": "usage", "ts": now - 7 * 86400 - 1, "data": {"in": 5000, "out": 500}},
        ])
        self.write_registry()
        data = xay_quota.collect(self.home, now)
        dsh = self.line(data, "dsh")
        self.assertEqual(dsh["used_5h"], 900)
        self.assertEqual(dsh["used_24h"], 900)
        self.assertEqual(dsh["burn_per_h"], 900)

    def test_codex_rate_limits_are_authoritative(self):
        now = 1_800_000_000
        self.add_run("codex-a", "codex", {"source": "sub", "started": now - 30}, {"status": "done", "ended": now - 10}, raw=[
            {"method": "account/rateLimits/updated", "params": {"rateLimits": {
                "primary": {"usedPercent": 26, "resetsAt": now + 900},
                "secondary": {"usedPercent": 35, "resetsAt": now + 8000},
            }}}
        ])
        self.write_registry()
        sub = self.line(xay_quota.collect(self.home, now), "codex-sub")
        # 最紧的窗口说了算：5 小时剩 74%，本周只剩 65%
        self.assertEqual(sub["remaining_pct"], 65)
        self.assertEqual(sub["window"], "week")
        self.assertEqual(sub["resets_at"], now + 8000)
        self.assertIn("5 小时剩 74%", sub["note"])
        self.assertIn("本周剩 65%", sub["note"])
        self.assertEqual(sub["source"], "live")

    def test_quota_error_exhausts_line_until_success_recovers(self):
        now = 1_800_000_000
        self.add_run("agy-fail", "agy", {"started": now - 300}, {"status": "failed", "ended": now - 200, "fail_reason": "usageLimitExceeded"})
        self.write_registry()
        self.assertEqual(self.line(xay_quota.collect(self.home, now), "agy")["status"], "exhausted")
        self.add_run("agy-ok", "agy", {"started": now - 100}, {"status": "done", "ended": now - 10})
        self.write_registry()
        self.assertEqual(self.line(xay_quota.collect(self.home, now), "agy")["status"], "unknown")

    def test_budget_share_limits_daily_relay_budget(self):
        now = 1_800_000_000
        (self.home / "quota.json").write_text(json.dumps({"lines": {"codex-relay": {"daily_tokens": 10000, "budget_share": 0.5}}}), encoding="utf-8")
        self.add_run("codex-relay", "codex", {"source": "relay", "started": now - 30}, {"status": "done", "ended": now - 10}, [
            {"type": "usage", "ts": now - 100, "data": {"in": 1000, "cached": 100, "out": 900}}
        ])
        self.write_registry()
        relay = self.line(xay_quota.collect(self.home, now), "codex-relay")
        self.assertEqual(relay["budget_share"], 0.5)
        self.assertEqual(relay["remaining_pct"], 64)

    def test_codex_auto_source_and_route_preference(self):
        quota = [
            {"line": "agy", "agent": "agy", "priority": 1, "status": "tight"},
            {"line": "codex-sub", "agent": "codex", "priority": 2, "status": "tight", "remaining_pct": 6},
            {"line": "codex-relay", "agent": "codex", "priority": 3, "status": "ok", "remaining_pct": 90},
            {"line": "dsh", "agent": "dsh", "priority": 4, "status": "ok"},
        ]
        self.assertEqual(xay_quota.choose_codex_source(quota), "relay")
        self.assertEqual(xay_quota.choose_route(quota, ["agy", "codex", "dsh"])["line"], "codex-relay")
        quota[2]["status"] = "exhausted"
        self.assertIsNone(xay_quota.choose_codex_source(quota))
        quota[1]["status"] = quota[2]["status"] = "exhausted"
        self.assertIsNone(xay_quota.choose_codex_source(quota))
        quota[1]["status"], quota[1]["remaining_pct"] = "unknown", None
        quota[2]["status"] = "ok"
        self.assertEqual(xay_quota.choose_codex_source(quota), "relay")


    def test_parse_four_official_sources(self):
        now = 1_800_000_000
        dsh = xay_quota.parse_opencode_usage({"usage": {
            "rolling": {"status": "ok", "percent": 10, "resetsAt": "2027-01-15T08:00:00.000Z"},
            "weekly": {"status": "ok", "percent": 46, "resetsAt": "2027-01-18T00:00:00.000Z"},
            "monthly": {"status": "ok", "percent": 42, "resetsAt": "2027-02-01T00:00:00.000Z"}}})
        self.assertEqual([w["remaining_pct"] for w in dsh["windows"]], [90, 54, 58])
        self.assertEqual(dsh["windows"][0]["key"], "5h")
        relay = xay_quota.parse_relay_usage({"isValid": True, "unit": "USD", "remaining": 355.9, "subscription": {
            "daily_limit_usd": 500, "daily_usage_usd": 144.09, "weekly_limit_usd": None, "monthly_limit_usd": None,
            "weekly_window_start": "2027-01-11T00:00:00+08:00", "expires_at": "2027-03-01T00:00:00+08:00"}}, now)
        self.assertEqual(len(relay["windows"]), 1)
        self.assertEqual(relay["windows"][0]["remaining_pct"], 71)
        self.assertGreater(relay["windows"][0]["resets_at"], now)
        self.assertLessEqual(relay["windows"][0]["resets_at"] - now, 86400)
        self.assertEqual(relay["balance"], "还能用 $356")
        with self.assertRaises(xay_quota.LiveError):
            xay_quota.parse_relay_usage({"isValid": False}, now)
        codex = xay_quota.parse_codex_rate_limits({"rateLimits": {
            "primary": {"usedPercent": 0, "windowDurationMins": 300, "resetsAt": now + 100},
            "secondary": {"usedPercent": 63, "windowDurationMins": 10080, "resetsAt": now + 9000}}})
        self.assertEqual([(w["key"], w["remaining_pct"]) for w in codex["windows"]], [("5h", 100), ("week", 37)])
        agy = xay_quota.parse_agy_usage(
            "Gemini Models\tWeekly Limit Remaining\t67%\t2027-01-15T16:26:57Z\n"
            "Gemini Models\tFive Hour Limit Remaining\t66%\t2027-01-15T06:28:05Z\n"
            "Claude and GPT models\tWeekly Limit Remaining\t3%\t2027-01-20T05:59:00Z\n"
            "Claude and GPT models\tFive Hour Limit Remaining\tDisabled\t\n")
        # xay 的 agy 只用 gemini，Claude/GPT 那组不算
        self.assertEqual([(w["key"], w["remaining_pct"]) for w in agy["windows"]], [("week", 67), ("5h", 66)])
        with self.assertRaises(xay_quota.LiveError):
            xay_quota.parse_agy_usage("没有额度")

    def test_live_reading_wins_and_failure_falls_back_to_estimate(self):
        now = 1_800_000_000
        self.add_run("dsh-a", "dsh", {"started": now - 100}, {"status": "done", "ended": now - 50}, [
            {"type": "usage", "ts": now - 1800, "data": {"in": 1000, "cached": 0, "out": 0}}])
        self.write_registry()
        live = {
            "dsh": {"ok": True, "fetched_at": now - 30, "source": "OpenCode 官方用量接口", "windows": [
                {"key": "5h", "name": "5 小时", "remaining_pct": 90, "resets_at": now + 600},
                {"key": "week", "name": "本周", "remaining_pct": 54, "resets_at": now + 86400}]},
            "agy": {"ok": False, "fetched_at": now - 10, "error": "agy /usage 超时"},
            "codex-relay": {"ok": False, "fetched_at": now - 10, "error": "连不上中转站用量接口", "last": {
                "ok": True, "fetched_at": now - 300, "windows": [{"key": "day", "name": "今天", "remaining_pct": 71, "resets_at": now + 3600}],
                "balance": "还能用 $356"}},
        }
        data = xay_quota.collect(self.home, now, live=live)
        dsh = self.line(data, "dsh")
        self.assertEqual((dsh["remaining_pct"], dsh["window"], dsh["status"], dsh["source"]), (54, "week", "ok", "live"))
        self.assertEqual(dsh["resets_at"], now + 86400)
        self.assertEqual(dsh["used_5h"], 1000)
        agy = self.line(data, "agy")
        self.assertEqual((agy["remaining_pct"], agy["status"], agy["source"]), (None, "unknown", "estimate"))
        self.assertIn("实时查询失败（agy /usage 超时）", agy["note"])
        relay = self.line(data, "codex-relay")
        self.assertEqual((relay["remaining_pct"], relay["source"]), (71, "live"))
        self.assertIn("还能用 $356", relay["note"])
        self.assertIn("最新一次查询失败", relay["note"])

    def test_live_reading_rules_reset_share_and_age(self):
        now = 1_800_000_000
        self.write_registry()
        live = {
            # 读数说用完了，但重置时间已过 → 当作满额
            "agy": {"ok": True, "fetched_at": now - 400, "windows": [{"key": "5h", "name": "5 小时", "remaining_pct": 0, "resets_at": now - 10}]},
            # 只让 xay 用一半：剩 45% 已经算偏紧，但还没用完
            "dsh": {"ok": True, "fetched_at": now - 30, "windows": [{"key": "week", "name": "本周", "remaining_pct": 45, "resets_at": now + 600}]},
            # 太旧的读数不再当真
            "codex-sub": {"ok": True, "fetched_at": now - xay_quota.LIVE_MAX_AGE - 1, "windows": [{"key": "5h", "name": "5 小时", "remaining_pct": 80, "resets_at": now + 600}]},
            "codex-relay": {"ok": True, "fetched_at": now - 30, "windows": [{"key": "day", "name": "今天", "remaining_pct": 0, "resets_at": now + 600}]},
        }
        data = xay_quota.collect(self.home, now, live=live)
        self.assertEqual((self.line(data, "agy")["remaining_pct"], self.line(data, "agy")["status"]), (100, "ok"))
        self.assertEqual(self.line(data, "dsh")["status"], "tight")
        self.assertEqual(self.line(data, "codex-sub")["source"], "estimate")
        relay = self.line(data, "codex-relay")
        self.assertEqual((relay["status"], relay["resets_at"]), ("exhausted", now + 600))

    def test_old_quota_error_does_not_override_newer_live_reading(self):
        now = 1_800_000_000
        self.add_run("agy-fail", "agy", {"started": now - 900}, {"status": "failed", "ended": now - 800, "fail_reason": "usageLimitExceeded"})
        self.write_registry()
        live = {"agy": {"ok": True, "fetched_at": now - 60, "windows": [{"key": "5h", "name": "5 小时", "remaining_pct": 60, "resets_at": now + 600}]}}
        self.assertEqual(self.line(xay_quota.collect(self.home, now, live=live), "agy")["status"], "ok")
        live["agy"]["fetched_at"] = now - 1000
        self.assertEqual(self.line(xay_quota.collect(self.home, now, live=live), "agy")["status"], "exhausted")

    def test_refresh_keeps_last_good_reading_and_never_stores_secrets(self):
        calls = []

        def ok():
            calls.append("ok")
            return {"windows": [{"key": "5h", "name": "5 小时", "remaining_pct": 50, "resets_at": None}], "source": "测试"}

        def bad():
            raise xay_quota.LiveError("连不上")

        xay_quota.refresh_live(self.home, ["dsh"], fetchers={"dsh": ok})
        xay_quota.refresh_live(self.home, ["dsh"], fetchers={"dsh": bad})
        cache = json.loads((self.home / xay_quota.LIVE_FILE).read_text(encoding="utf-8"))
        self.assertFalse(cache["dsh"]["ok"])
        self.assertEqual(cache["dsh"]["error"], "连不上")
        self.assertEqual(cache["dsh"]["last"]["windows"][0]["remaining_pct"], 50)
        self.assertEqual(calls, ["ok"])

    def test_live_queries_off_for_temporary_homes(self):
        old = os.environ.pop("XAY_QUOTA_LIVE", None)
        try:
            self.assertFalse(xay_quota._live_enabled(self.home))
            os.environ["XAY_QUOTA_LIVE"] = "1"
            self.assertTrue(xay_quota._live_enabled(self.home))
        finally:
            os.environ.pop("XAY_QUOTA_LIVE", None)
            if old is not None:
                os.environ["XAY_QUOTA_LIVE"] = old


class AgyAccountsTest(unittest.TestCase):
    """agy 多账号：读 Antigravity Tools 的清单，按优先级挑，经它的命令行切换。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="xay-agy-acc-")
        root = Path(self.tmp.name)
        self.home, self.tools = root / "xay", root / "tools"
        (self.home / "runs").mkdir(parents=True)
        (self.tools / "accounts").mkdir(parents=True)
        (self.home / "registry.jsonl").write_text("", encoding="utf-8")
        self.now = int(time.time())  # collect() 和 agy_precheck() 用真实时间
        iso = lambda ts: datetime.fromtimestamp(ts, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        accounts = [
            ("a1", "主号", 2, 0.05, False),      # 当前账号，本周只剩 5%
            ("a2", "备用甲", 1, 0.90, False),
            ("a3", "备用乙", 1, 0.95, True),     # 被停用
            ("a4", "备用丙", 1, 1.00, False),
        ]
        index = {"version": "2.0", "current_agy_account_id": "a1", "accounts": []}
        for aid, name, prio, frac, disabled in accounts:
            index["accounts"].append({"id": aid, "name": name, "email": f"{aid}@example.com", "disabled": disabled})
            (self.tools / "accounts" / f"{aid}.json").write_text(json.dumps({
                "id": aid, "email": f"{aid}@example.com", "name": name, "priority": prio,
                "token": {"access_token": "SECRET-SHOULD-NOT-LEAK"},
                "quota": {"last_updated": self.now - 7200, "is_forbidden": False, "quota_groups": [
                    {"display_name": "Gemini Models", "buckets": [
                        {"window": "weekly", "remaining_fraction": frac, "reset_time": iso(self.now + 86400)},
                        {"window": "5h", "remaining_fraction": 0.0, "reset_time": iso(self.now - 60)}]},
                    {"display_name": "Claude and GPT models", "buckets": [
                        {"window": "weekly", "remaining_fraction": 0.0, "reset_time": iso(self.now + 86400)}]}]}}), encoding="utf-8")
        (self.tools / "accounts.json").write_text(json.dumps(index), encoding="utf-8")
        # 假的 antigravity-tools：switch <邮箱> --target agy → 改 current_agy_account_id
        self.calls = root / "calls.log"
        fake = root / "antigravity-tools"
        fake.write_text("#!/usr/bin/env python3\n"
                        "import json, sys\n"
                        f"open({str(self.calls)!r}, 'a').write(' '.join(sys.argv[1:]) + '\\n')\n"
                        f"p = {str(self.tools / 'accounts.json')!r}\n"
                        "d = json.load(open(p))\n"
                        "hit = [a for a in d['accounts'] if a['email'] == sys.argv[2]]\n"
                        "if not hit or sys.argv[3:5] != ['--target', 'agy']: sys.exit(1)\n"
                        "d['current_agy_account_id'] = hit[0]['id']\n"
                        "json.dump(d, open(p, 'w'))\n", encoding="utf-8")
        fake.chmod(0o755)
        self.env = {k: os.environ.get(k) for k in ("XAY_AGY_TOOLS_HOME", "XAY_AGY_TOOLS_BIN", "XAY_QUOTA_LIVE")}
        os.environ.update({"XAY_AGY_TOOLS_HOME": str(self.tools), "XAY_AGY_TOOLS_BIN": str(fake), "XAY_QUOTA_LIVE": "0"})

    def tearDown(self):
        for key, value in self.env.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
        self.tmp.cleanup()

    def test_accounts_merge_records_and_pick_by_priority(self):
        live = {"agy_accounts": {"a2": {"ok": True, "fetched_at": self.now - 60, "source": "agy 官方 /usage",
                                         "windows": [{"key": "week", "name": "本周", "remaining_pct": 30, "resets_at": self.now + 3600}]}}}
        accounts = {a["id"]: a for a in xay_quota.agy_accounts(self.home, self.now, live)}
        self.assertTrue(accounts["a1"]["current"])
        self.assertEqual((accounts["a1"]["remaining_pct"], accounts["a1"]["status"]), (5, "tight"))
        # xay 自己刚查到的比 Tools 的旧记录新，用它
        self.assertEqual((accounts["a2"]["remaining_pct"], accounts["a2"]["source"]), (30, "agy 官方 /usage"))
        self.assertEqual(accounts["a3"]["status"], "blocked")
        # 已过重置时间的 5 小时窗按满额算；Claude/GPT 那组不算
        self.assertEqual(accounts["a4"]["remaining_pct"], 100)
        self.assertNotIn("SECRET", json.dumps(list(accounts.values()), ensure_ascii=False))
        self.assertNotIn("example.com", json.dumps(list(accounts.values()), ensure_ascii=False))
        # 优先级 1 的两个可用账号里挑剩得多的；停用的不挑；当前账号排除
        self.assertEqual(xay_quota.choose_agy_account(list(accounts.values()), exclude="a1")["id"], "a4")

    def test_switch_goes_through_tools_and_resets_agy_reading(self):
        (self.home / xay_quota.LIVE_FILE).write_text(json.dumps({"agy": {"ok": True, "fetched_at": self.now, "account_id": "a1", "windows": []}}), encoding="utf-8")
        ok, message = xay_quota.switch_agy_account("备用丙", self.home)
        self.assertTrue(ok, message)
        self.assertIn("切到「备用丙」", message)
        self.assertEqual(self.calls.read_text(encoding="utf-8").split(), ["switch", "a4@example.com", "--target", "agy"])
        cache = json.loads((self.home / xay_quota.LIVE_FILE).read_text(encoding="utf-8"))
        self.assertEqual(cache["agy"]["fetched_at"], 0)
        self.assertIn("agy_switched_at", cache)
        log = (self.home / "agy-switch.log").read_text(encoding="utf-8")
        self.assertIn("备用丙", log)
        self.assertNotIn("example.com", message + log)
        self.assertFalse(xay_quota.switch_agy_account("不存在的号", self.home)[0])

    def test_precheck_switches_when_current_account_is_used_up(self):
        rd = self.home / "runs" / "agy-fail"
        rd.mkdir()
        (rd / "start.json").write_text(json.dumps({"agent": "agy", "started": time.time() - 300}), encoding="utf-8")
        (rd / "meta.json").write_text(json.dumps({"agent": "agy", "status": "failed", "ended": time.time() - 200, "fail_reason": "usageLimitExceeded"}), encoding="utf-8")
        (self.home / "registry.jsonl").write_text(json.dumps({"kind": "run", "run_dir": str(rd), "agent": "agy"}) + "\n", encoding="utf-8")
        data = xay_quota.collect(self.home)
        agy = next(x for x in data if x["line"] == "agy")
        self.assertEqual(agy["status"], "exhausted")
        self.assertEqual(agy["label"], "Antigravity · 主号")
        self.assertEqual(agy["alt_account"]["id"], "a4")
        self.assertIn("派活时会自动换", agy["note"])
        # 当前号用完但有别的号，路线推荐仍然选 agy
        self.assertEqual(xay_quota.choose_route(data, ["agy", "codex"])["line"], "agy")
        message = xay_quota.agy_precheck(self.home)
        self.assertIn("切到「备用丙」", message)
        # 切换之后，旧账号的"用完"报错不再算到新账号头上
        agy = next(x for x in xay_quota.collect(self.home) if x["line"] == "agy")
        self.assertNotEqual(agy["status"], "exhausted")
        self.assertEqual(xay_quota.agy_precheck(self.home), "")


if __name__ == "__main__":
    unittest.main()
