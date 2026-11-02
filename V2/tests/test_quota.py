"""agy 账号优选与自动切换单元测试（V2/xay/quota.py）。

全部用假账号数据和 mock，不联网、不调真实二进制。
"""
import json
import os
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

from xay import quota


class QuotaSelectionTests(unittest.TestCase):

    def test_choose_by_priority_and_remaining(self):
        accounts = [
            {"index": 1, "name": "低优先级满额", "priority": 100, "current": False, "status": "ok", "left": 100},
            {"index": 2, "name": "高优先级剩半", "priority": 1, "current": False, "status": "ok", "left": 50},
            {"index": 3, "name": "高优先级满额", "priority": 1, "current": False, "status": "ok", "left": 90},
            {"index": 4, "name": "当前账号", "priority": 1, "current": True, "status": "ok", "left": 100},
        ]
        # 应该挑 priority 最小且 left 最大的非 current 账号（序号 3）
        chosen = quota.choose_agy_account(accounts)
        self.assertIsNotNone(chosen)
        self.assertEqual(chosen["index"], 3)

    def test_choose_skips_blocked_and_exhausted(self):
        accounts = [
            {"index": 1, "name": "封禁账号", "priority": 1, "current": False, "status": "blocked", "left": 100},
            {"index": 2, "name": "用完账号", "priority": 1, "current": False, "status": "exhausted", "left": 0},
            {"index": 3, "name": "skip账号", "priority": 2, "current": False, "status": "ok", "left": 80},
            {"index": 4, "name": "可用账号", "priority": 3, "current": False, "status": "ok", "left": 80},
        ]
        chosen = quota.choose_agy_account(accounts, skip=["skip账号"])
        self.assertIsNotNone(chosen)
        self.assertEqual(chosen["index"], 4)

    def test_choose_falls_back_to_tight(self):
        accounts = [
            {"index": 1, "name": "用完1", "priority": 1, "current": False, "status": "exhausted", "left": 0},
            {"index": 2, "name": "偏紧账号", "priority": 2, "current": False, "status": "tight", "left": 15},
        ]
        chosen = quota.choose_agy_account(accounts)
        self.assertIsNotNone(chosen)
        self.assertEqual(chosen["index"], 2)

    def test_all_exhausted_returns_none(self):
        accounts = [
            {"index": 1, "name": "用完1", "priority": 1, "current": False, "status": "exhausted", "left": 0},
            {"index": 2, "name": "封禁1", "priority": 2, "current": False, "status": "blocked", "left": 50},
        ]
        chosen = quota.choose_agy_account(accounts)
        self.assertIsNone(chosen)


class QuotaSwitchTests(unittest.TestCase):

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.home = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    @mock.patch("xay.quota._run_tools")
    @mock.patch("xay.quota_live.agy_accounts")
    @mock.patch("xay.quota.agy_accounts")
    def test_switch_auto(self, mock_accounts, mock_live_accounts, mock_run):
        mock_run.return_value = (0, "ok")
        def side_accounts(*args, **kwargs):
            if mock_run.called:
                return [
                    {"index": 1, "name": "当前", "priority": 10, "current": False, "status": "ok", "left": 0},
                    {"index": 2, "name": "目标", "priority": 1, "current": True, "status": "ok", "left": 100},
                ]
            return [
                {"index": 1, "name": "当前", "priority": 10, "current": True, "status": "ok", "left": 0},
                {"index": 2, "name": "目标", "priority": 1, "current": False, "status": "ok", "left": 100},
            ]
        mock_accounts.side_effect = side_accounts
        mock_live_accounts.side_effect = side_accounts

        res = quota.switch_agy_account("测试切换", home=self.home, cooldown_s=10)
        self.assertTrue(res["ok"])
        self.assertTrue(res["switched"])
        self.assertEqual(res["to"], "目标")
        mock_run.assert_called_with(["switch", "2", "--target", "agy"])

    @mock.patch("xay.quota._run_tools")
    @mock.patch("xay.quota_live.agy_accounts")
    @mock.patch("xay.quota.agy_accounts")
    def test_switch_specified_target(self, mock_accounts, mock_live_accounts, mock_run):
        mock_run.return_value = (0, "ok")
        def side_accounts(*args, **kwargs):
            if mock_run.called:
                return [
                    {"index": 1, "name": "账号1", "priority": 1, "current": False, "status": "ok", "left": 100},
                    {"index": 2, "name": "账号2", "priority": 2, "current": True, "status": "ok", "left": 100},
                ]
            return [
                {"index": 1, "name": "账号1", "priority": 1, "current": True, "status": "ok", "left": 100},
                {"index": 2, "name": "账号2", "priority": 2, "current": False, "status": "ok", "left": 100},
            ]
        mock_accounts.side_effect = side_accounts
        mock_live_accounts.side_effect = side_accounts

        res = quota.switch_agy_account("手动指定", home=self.home, target="账号2")
        self.assertTrue(res["ok"])
        self.assertTrue(res["switched"])
        mock_run.assert_called_with(["switch", "2", "--target", "agy"])


class StationPrecheckTests(unittest.TestCase):

    @mock.patch("xay.quota.switch_agy_account")
    def test_precheck_switches_when_exhausted(self, mock_switch):
        from xay.station.station import Station
        # 伪造一个 station 对象
        station = mock.MagicMock()
        station.agent = "agy"
        station.home = Path("/tmp")
        station._label = mock.MagicMock(return_value="Gemini 1")
        station.adapter = mock.MagicMock()

        # 模拟当前账号已耗尽，候选账号充足
        mock_switch.return_value = {"ok": True, "switched": True, "text": "换到 账号2"}
        with mock.patch("xay.quota_live.lines_now", return_value=[]), \
             mock.patch("xay.quota.merged_agy_accounts", return_value=[
                 {"index": 1, "name": "账号1", "priority": 10, "current": True, "status": "exhausted", "left": 0},
                 {"index": 2, "name": "账号2", "priority": 1, "current": False, "status": "ok", "left": 100},
             ]):
            Station._agy_precheck(station, "tk_1", "cv_1")
            mock_switch.assert_called_once()
            station._signal.assert_called_once()
            station._close_adapter.assert_called_once()


class MergedAgyAccountsFreshTests(unittest.TestCase):

    @mock.patch("xay.quota_live.agy_accounts")
    @mock.patch("xay.quota.agy_accounts")
    def test_fresh_overrides_when_newer(self, mock_cli_accs, mock_live_accs):
        mock_cli_accs.return_value = [
            {"index": 1, "name": "账号A", "current": True, "status": "正常", "ok": True},
            {"index": 2, "name": "账号B", "current": False, "status": "正常", "ok": True},
        ]
        mock_live_accs.return_value = [
            {"index": 1, "name": "账号A", "priority": 1, "left": 100, "status": "ok", "checked_at": 1000.0, "source": "Tools 记录"},
            {"index": 2, "name": "账号B", "priority": 2, "left": 100, "status": "ok", "checked_at": None, "source": "Tools 记录"},
        ]
        fresh = [
            {"name": "账号A", "left": 34, "status": "tight", "resets_at": 123456, "checked_at": 2000.0, "source": "agy 官方 /usage"},
            {"name": "账号B", "left": 50, "status": "ok", "resets_at": None, "checked_at": 1500.0, "source": "agy 官方 /usage"},
        ]
        merged = quota.merged_agy_accounts(fresh=fresh)
        a_map = {a["name"]: a for a in merged}

        # 账号A checked_at (2000 > 1000)：被 fresh 覆盖
        self.assertEqual(a_map["账号A"]["left"], 34)
        self.assertEqual(a_map["账号A"]["status"], "tight")
        self.assertEqual(a_map["账号A"]["checked_at"], 2000.0)
        self.assertEqual(a_map["账号A"]["source"], "agy 官方 /usage")
        self.assertEqual(a_map["账号A"]["resets_at"], 123456)

        # 账号B 原本 checked_at 为 None：被 fresh 覆盖
        self.assertEqual(a_map["账号B"]["left"], 50)
        self.assertEqual(a_map["账号B"]["checked_at"], 1500.0)
        self.assertEqual(a_map["账号B"]["source"], "agy 官方 /usage")

    @mock.patch("xay.quota_live.agy_accounts")
    @mock.patch("xay.quota.agy_accounts")
    def test_fresh_does_not_override_when_older(self, mock_cli_accs, mock_live_accs):
        mock_cli_accs.return_value = [
            {"index": 1, "name": "账号A", "current": True, "status": "正常", "ok": True},
        ]
        mock_live_accs.return_value = [
            {"index": 1, "name": "账号A", "priority": 1, "left": 90, "status": "ok", "checked_at": 3000.0, "source": "新 Tools 记录"},
        ]
        fresh = [
            {"name": "账号A", "left": 20, "status": "tight", "checked_at": 1000.0, "source": "旧账本记录"},
        ]
        merged = quota.merged_agy_accounts(fresh=fresh)
        self.assertEqual(merged[0]["left"], 90)
        self.assertEqual(merged[0]["status"], "ok")
        self.assertEqual(merged[0]["checked_at"], 3000.0)
        self.assertEqual(merged[0]["source"], "新 Tools 记录")

    @mock.patch("xay.quota_live.agy_accounts")
    @mock.patch("xay.quota.agy_accounts")
    def test_disabled_by_cli_always_blocked(self, mock_cli_accs, mock_live_accs):
        mock_cli_accs.return_value = [
            {"index": 1, "name": "停用号", "current": False, "status": "停用", "ok": False},
        ]
        mock_live_accs.return_value = [
            {"index": 1, "name": "停用号", "priority": 1, "left": 100, "status": "ok", "checked_at": 1000.0},
        ]
        fresh = [
            {"name": "停用号", "left": 100, "status": "ok", "checked_at": 2000.0},
        ]
        merged = quota.merged_agy_accounts(fresh=fresh)
        self.assertEqual(merged[0]["status"], "blocked")
        self.assertFalse(merged[0]["ok"])

        # choose_agy_account 绝不选停用号
        chosen = quota.choose_agy_account(merged)
        self.assertIsNone(chosen)

    @mock.patch("xay.quota._run_tools")
    @mock.patch("xay.quota_live.agy_accounts")
    @mock.patch("xay.quota.agy_accounts")
    def test_switch_skips_tools_ok_but_fresh_exhausted(self, mock_cli_accs, mock_live_accs, mock_run):
        mock_run.return_value = (0, "ok")
        def side_cli(*args, **kwargs):
            is_switched = mock_run.called
            return [
                {"index": 1, "name": "当前号", "current": not is_switched, "status": "正常", "ok": True},
                {"index": 2, "name": "已耗尽号", "current": False, "status": "正常", "ok": True},
                {"index": 3, "name": "充足候选号", "current": is_switched, "status": "正常", "ok": True},
            ]
        mock_cli_accs.side_effect = side_cli
        mock_live_accs.return_value = [
            {"index": 1, "name": "当前号", "priority": 1, "left": 0, "status": "exhausted", "checked_at": 1000.0},
            {"index": 2, "name": "已耗尽号", "priority": 1, "left": 100, "status": "ok", "checked_at": 1000.0},
            {"index": 3, "name": "充足候选号", "priority": 2, "left": 80, "status": "ok", "checked_at": 1000.0},
        ]
        fresh = [
            {"name": "已耗尽号", "left": 0, "status": "exhausted", "checked_at": 2000.0},
            {"name": "充足候选号", "left": 80, "status": "ok", "checked_at": 2000.0},
        ]
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            accounts = quota.merged_agy_accounts(fresh=fresh)
            # choose_agy_account 跳过已耗尽号
            chosen = quota.choose_agy_account(accounts)
            self.assertIsNotNone(chosen)
            self.assertEqual(chosen["index"], 3)
            self.assertEqual(chosen["name"], "充足候选号")

            # switch_agy_account 传入 accounts
            res = quota.switch_agy_account("额度测试", home=home, cooldown_s=0, accounts=accounts)
            self.assertTrue(res["ok"])
            self.assertTrue(res["switched"])
            self.assertEqual(res["to"], "充足候选号")
            mock_run.assert_called_with(["switch", "3", "--target", "agy"])

    @mock.patch("xay.quota.agy_accounts")
    def test_cli_agy_accounts_reads_fresh_from_db(self, mock_cli_accs):
        import io
        from contextlib import redirect_stdout
        from xay.cli import _agy_accounts_cmd
        from xay.core import append, open_db

        mock_cli_accs.return_value = [
            {"index": 1, "name": "账号A", "current": True, "status": "正常", "ok": True},
        ]
        with tempfile.TemporaryDirectory() as tmp:
            with mock.patch.dict(os.environ, {"XAY2_HOME": tmp}):
                conn = open_db()
                append(conn, "quota.updated", "office", {
                    "line": "agy",
                    "source": "agy 官方 /usage",
                    "accounts": [
                        {"name": "账号A", "left": 34, "status": "tight", "checked_at": 1730332800.0}
                    ]
                })
                conn.close()

                args = mock.MagicMock()
                args.json = False
                buf = io.StringIO()
                with redirect_stdout(buf):
                    _agy_accounts_cmd(args)
                out = buf.getvalue()
                self.assertIn("34%", out)
                self.assertIn("偏紧", out)
                self.assertIn("agy 官方 /usage", out)


class IneligibleAccountTests(unittest.TestCase):
    """2026-10-31 真机：预检按优先级换到 FlameY，结果"not eligible"，两次开工即失败。"""

    def test_classify_ineligible(self):
        from xay.adapters.base import classify_error, QUOTA_LIKE
        msg = "Eligibility check failed: Your current account is not eligible for Antigravity, because it is not currently available in your location"
        self.assertEqual(classify_error(msg), "ineligible")
        self.assertIn("ineligible", QUOTA_LIKE)

    def test_blocked_account_is_skipped(self):
        with tempfile.TemporaryDirectory() as tmp:
            quota.mark_agy_blocked("FlameY", "not eligible", home=tmp)
            self.assertIn("FlameY", quota.agy_blocked(home=tmp))
            base = [{"index": 1, "name": "FlameY", "current": False, "ok": True, "status": "正常"},
                    {"index": 2, "name": "OLYN", "current": True, "ok": True, "status": "正常"},
                    {"index": 3, "name": "Smith", "current": False, "ok": True, "status": "正常"}]
            rich = [{"index": 1, "name": "FlameY", "priority": 1, "status": "ok", "left": 100},
                    {"index": 2, "name": "OLYN", "priority": 4, "status": "tight", "left": 10},
                    {"index": 3, "name": "Smith", "priority": 5, "status": "ok", "left": 100}]
            with mock.patch("xay.quota.agy_accounts", return_value=base), mock.patch("xay.quota_live.agy_accounts", return_value=rich):
                merged = quota.merged_agy_accounts(home=tmp)
            self.assertEqual(next(a for a in merged if a["name"] == "FlameY")["status"], "blocked")
            self.assertEqual(quota.choose_agy_account(merged)["name"], "Smith")     # 优先级最高的 FlameY 用不了，挑下一个


if __name__ == "__main__":
    unittest.main()
