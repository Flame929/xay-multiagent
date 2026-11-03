"""xay 自己的 agy 换号（xay/agy_login.py）：假钥匙串（普通文件）+ 假 Antigravity Tools 清单，不碰真实登录。

2026-10-31 真机问题：Tools 说已经切到 账号 A，agy 认的钥匙串里却还是桌面 App 登录的 账号 B；V2 按 Tools 的"当前号"
记读数，于是 账号 B 一个号的额度被依次记到了五个号头上，又不停地"换号"。
"""
import base64
import json
import os
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

from xay import agy_login, quota, quota_live

SECRET = "SECRET-LOGIN-DO-NOT-LEAK"


class AgyLoginCase(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory(prefix="xay-agy-login-")
        self.addCleanup(tmp.cleanup)
        root = Path(tmp.name)
        self.home, self.tools = root / "v2", root / "tools"
        self.keychain, self.file = root / "keychain", root / "oauth-token"
        env = mock.patch.dict(os.environ, {"XAY2_HOME": str(self.home), "XAY_AGY_TOOLS_HOME": str(self.tools),
                                           "XAY_AGY_KEYCHAIN_FILE": str(self.keychain), "XAY_AGY_LOGIN_FILE": str(self.file)})
        env.start()
        self.addCleanup(env.stop)
        (self.tools / "accounts").mkdir(parents=True)
        index = {"current_agy_account_id": "a2", "accounts": []}
        for aid, name, prio in (("a1", "主号", 2), ("a2", "备用甲", 1), ("a3", "备用乙", 3)):
            index["accounts"].append({"id": aid, "name": name, "email": aid + "@example.com", "disabled": False})
            (self.tools / "accounts" / (aid + ".json")).write_text(json.dumps({
                "id": aid, "email": aid + "@example.com", "name": name, "priority": prio,
                "token": {"access_token": SECRET + aid, "refresh_token": "rt-" + aid + SECRET, "expiry_timestamp": time.time() + 3000,
                          "expires_in": 3599, "token_type": "Bearer"},
                "quota": {"last_updated": time.time() - 60, "quota_groups": []}}), encoding="utf-8")
        (self.tools / "accounts.json").write_text(json.dumps(index), encoding="utf-8")
        self.put_keychain("a1")             # agy 实际登录的是主号，Tools 却记着备用甲

    def put_keychain(self, aid, with_email=True):
        payload = {"token": {"access_token": SECRET + aid, "token_type": "Bearer", "refresh_token": "rt-" + aid + SECRET,
                             "expiry": "2026-01-01T00:00:00Z", "expires_in": 3599}, "auth_method": "consumer"}
        if with_email:
            payload["id_token_email"] = aid + "@example.com"
        self.keychain.write_text(agy_login.PREFIX + base64.b64encode(json.dumps(payload).encode()).decode(), encoding="utf-8")

    def test_current_account_follows_agy_login_not_tools_record(self):
        self.assertEqual(agy_login.current_account_id(), "a1")
        self.assertEqual(quota_live.agy_current_id(), "a1")
        cur = [a["name"] for a in quota_live.agy_accounts() if a["current"]]
        self.assertEqual(cur, ["主号"])
        # 登录里没写邮箱：拿 refresh_token 对出是谁
        self.put_keychain("a3", with_email=False)
        self.assertEqual(agy_login.current_account_id(), "a3")
        # 钥匙串里什么都没有：才退回 Tools 自己记的
        self.keychain.unlink()
        self.assertEqual(quota_live.agy_current_id(), "a2")

    def test_switch_writes_agy_login_and_checks_it(self):
        ok, text = agy_login.switch_to("a3", self.home)
        self.assertTrue(ok, text)
        self.assertEqual(agy_login.current_account_id(), "a3")
        stored = agy_login._decode(self.keychain.read_text(encoding="utf-8"))
        self.assertEqual(stored["id_token_email"], "a3@example.com")
        self.assertEqual(stored["token"]["refresh_token"], "rt-a3" + SECRET)
        self.assertEqual(json.loads(self.file.read_text(encoding="utf-8"))["id_token_email"], "a3@example.com")
        self.assertEqual(oct(self.file.stat().st_mode & 0o777), "0o600")
        # 换走之前那份（主号）存进了 xay 自己的存档，以后不靠 Tools 也能换回去
        self.assertIn("a1@example.com", agy_login._saved(self.home))
        for path in (self.home / "agy-logins").glob("*.json"):
            self.assertEqual(oct(path.stat().st_mode & 0o777), "0o600")
        self.assertNotIn(SECRET, text)
        self.assertNotIn("example.com", text)

    def test_failed_write_puts_old_login_back(self):
        real_write = agy_login.keychain_write
        calls = []

        def broken_once(payload):
            calls.append(payload.get("id_token_email"))
            if len(calls) == 1:
                self.keychain.write_text("坏掉的内容", encoding="utf-8")      # 模拟写坏
                return False
            return real_write(payload)

        with mock.patch.object(agy_login, "keychain_write", side_effect=broken_once):
            ok, text = agy_login.switch_to("a3", self.home)
        self.assertFalse(ok)
        self.assertIn("已放回原来的号", text)
        self.assertEqual(agy_login.current_account_id(), "a1")

    def test_ensure_desired_rewrites_when_someone_changes_it_back(self):
        agy_login.switch_to("a3", self.home)
        self.assertIsNone(agy_login.ensure_desired(self.home))
        time.sleep(0.01)
        self.put_keychain("a1")                      # 桌面 App 把它改回了主号
        msg = agy_login.ensure_desired(self.home)
        self.assertIn("已改回「备用乙」", msg)
        self.assertEqual(agy_login.current_account_id(), "a3")
        self.assertNotIn("example.com", msg)

    def test_ensure_desired_respects_manual_switch_in_tools(self):
        agy_login.switch_to("a3", self.home)
        time.sleep(0.02)
        # 用户自己在 Antigravity Tools 里换到了主号：Tools 的记录和 agy 的登录都变成主号
        index = json.loads((self.tools / "accounts.json").read_text(encoding="utf-8"))
        index["current_agy_account_id"] = "a1"
        (self.tools / "accounts.json").write_text(json.dumps(index), encoding="utf-8")
        self.put_keychain("a1")
        self.assertIsNone(agy_login.ensure_desired(self.home))
        self.assertEqual(agy_login.desired_account_id(self.home), "a1")

    def test_quota_switch_uses_own_login_without_tools_program(self):
        with mock.patch("xay.quota._run_tools") as tools:
            res = quota.switch_agy_account("测试", home=self.home, target="备用乙")
        tools.assert_not_called()
        self.assertTrue(res["ok"] and res["switched"], res)
        self.assertEqual(res["to"], "备用乙")
        self.assertIn("从「主号」换到「备用乙」", res["text"])
        self.assertEqual(agy_login.current_account_id(), "a3")
        self.assertNotIn("example.com", json.dumps(res, ensure_ascii=False))

    def test_readings_are_filed_under_the_real_account(self):
        """10-31 的错：Tools 记着备用甲，读数却来自主号。现在 /usage 的读数记到 agy 实际登录的号名下。"""
        fake = mock.MagicMock()
        fake.returncode = 0
        fake.communicate.return_value = ("Gemini Models\tWeekly Limit Remaining\t16%\t2030-01-01T00:00:00Z\n", "")
        with mock.patch("subprocess.Popen", return_value=fake):
            reading = quota_live.fetch_agy()
        self.assertEqual(reading["account_id"], "a1")


class CodexRelayKeepsItsOwnQuotaTests(unittest.TestCase):
    def test_relay_line_never_takes_plus_percentages(self):
        from xay.station.station import Station
        st = mock.MagicMock()
        st._current_line.return_value = "codex-relay"
        st._quota_last = (None, 0.0)
        written = []
        st._w.side_effect = lambda typ, data, conv=None: written.append(data) or 1
        plus = {"line": "codex-relay", "windows": [{"key": "5h", "mins": 300, "used_pct": 40.0, "resets_at": 2000000000}],
                "reached": "primary"}
        Station._on_rate_limits(st, plus)
        Station._on_rate_limits(st, plus)            # 一样的事件：不重复写
        self.assertEqual(written, [{"line": "codex-relay", "windows": []}])
        st2 = mock.MagicMock()
        st2._quota_last = (None, 0.0)
        got = []
        st2._w.side_effect = lambda typ, data, conv=None: got.append(data) or 1
        Station._on_rate_limits(st2, dict(plus, line="codex-sub"))
        self.assertEqual(got[0]["windows"][0]["used_pct"], 40.0)   # Plus 线路照常记


if __name__ == "__main__":
    unittest.main()


class StationPriorityAndRestartTests(unittest.TestCase):
    """开工前预检：按 Tools 的优先级换回更靠前的号；agy 进程是用旧号开的就重启。"""

    def station(self):
        from xay.station.station import Station
        st = mock.MagicMock()
        st.agent = "agy"
        st.home = Path(tempfile.mkdtemp(prefix="xay-st-"))
        self.addCleanup(__import__("shutil").rmtree, st.home, True)
        st._real_agy.return_value = False
        return Station, st

    def run_precheck(self, accounts, switch_result=None):
        Station, st = self.station()
        with mock.patch("xay.quota_live.lines_now", return_value=[]), \
             mock.patch("xay.quota.merged_agy_accounts", return_value=accounts), \
             mock.patch("xay.quota.ensure_agy_login", return_value=None), \
             mock.patch("xay.quota.switch_agy_account", return_value=switch_result or {"ok": True, "switched": True, "text": "换好了"}) as sw:
            Station._agy_precheck(st, "tk_1", "cv_1")
        return st, sw

    def test_switches_up_to_higher_priority_account_with_enough_quota(self):
        st, sw = self.run_precheck([
            {"index": 1, "name": "排第一但停用", "priority": 1, "current": False, "status": "blocked", "left": 100},
            {"index": 2, "name": "排第二", "priority": 2, "current": False, "status": "ok", "left": 90},
            {"index": 3, "name": "当前号", "priority": 4, "current": True, "status": "ok", "left": 60}])
        sw.assert_called_once()
        self.assertEqual(sw.call_args.kwargs["target"], 2)
        self.assertIn("优先级更靠前", sw.call_args.args[0])
        st._close_adapter.assert_called_once()

    def test_stays_when_higher_priority_account_is_tight_or_current_is_best(self):
        _st, sw = self.run_precheck([
            {"index": 2, "name": "排第二", "priority": 2, "current": False, "status": "tight", "left": 10},
            {"index": 3, "name": "当前号", "priority": 4, "current": True, "status": "ok", "left": 60}])
        sw.assert_not_called()
        _st, sw = self.run_precheck([
            {"index": 2, "name": "当前号", "priority": 2, "current": True, "status": "ok", "left": 60},
            {"index": 3, "name": "排后面满额", "priority": 4, "current": False, "status": "ok", "left": 100}])
        sw.assert_not_called()

    def test_restarts_agy_process_started_under_another_login(self):
        Station, st = self.station()
        st._real_agy.return_value = True
        st._agy_login = "old"
        with mock.patch("xay.agy_login.current_account_id", return_value="new"), \
             mock.patch("xay.quota.ensure_agy_login", return_value=None), \
             mock.patch("xay.quota_live.lines_now", side_effect=RuntimeError("到这里就够了")):
            Station._agy_precheck(st, "tk_1", "cv_1")
        st._close_adapter.assert_called_once()
        st.live.add.assert_any_call("status", "Gemini 换过账号了，重启进程用新账号（还是同一个对话）")
