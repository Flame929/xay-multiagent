"""旧看板换回以前的样子：页面、全局数据、推送和路由的测试。"""
import json
import os
import shutil
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

REPO_ROOT = Path(__file__).resolve().parents[2]
SRC_WATCH = REPO_ROOT / "scripts" / "xay-watch.html"
DST_WATCH = REPO_ROOT / "V2" / "web" / "watch.html"

# 原样复制 scripts/xay-watch.html 成 V2/web/watch.html
if SRC_WATCH.is_file() and not DST_WATCH.is_file():
    shutil.copyfile(SRC_WATCH, DST_WATCH)

from tests import test_office_web as W
from xay.core import events as core_events
from xay.office import oldboard
from xay.office import web


class OldboardStateTests(W.OfficeCase):
    seed = False

    def test_watch_html_file_copied(self):
        # 2026-10-31 起 V2 这份单独维护（演示数据去掉了品牌名），旧版 V1 冻结不改，所以不再要求逐字相同；
        # 只要求页面请求的还是同一组旧接口（管家照这组接口提供数据）
        self.assertTrue(DST_WATCH.is_file())
        src, dst = SRC_WATCH.read_text(encoding="utf-8"), DST_WATCH.read_text(encoding="utf-8")
        for api in ("/api/state", "/api/conversation", "/api/events", "/api/diff", "/api/control", "/api/decision"):
            self.assertIn(api, src)
            self.assertIn(api, dst)

    def test_root_returns_watch_html_and_board_returns_board_html(self):
        status, headers, data = self.req("GET", "/")
        self.assertEqual(status, 200)
        html_root = data.decode("utf-8")
        self.assertIn("<title>AI 团队看板</title>", html_root)
        self.assertIn('id="side-tree"', html_root)

        status, headers, data = self.req("GET", "/board")
        self.assertEqual(status, 200)
        html_board = data.decode("utf-8")
        self.assertIn("<title>xay 看板</title>", html_board)

    def test_workspaces_grouping_real_and_temp(self):
        A = core_events.append
        # 1. 真实项目文件夹 1
        real1 = self.tmp / "proj_alpha"
        real1.mkdir(parents=True, exist_ok=True)
        ws1 = "ws_alpha"
        A(self.conn, "conv.begun", "lead:cv_alpha", {"title": "阿尔法对话", "lead_kind": "claude", "cwd": str(real1)},
          ws=ws1, conv="cv_alpha", subject="cv_alpha")

        # 2. 真实项目文件夹 2
        real2 = self.tmp / "proj_beta"
        real2.mkdir(parents=True, exist_ok=True)
        ws2 = "ws_beta"
        A(self.conn, "conv.begun", "lead:cv_beta", {"title": "贝塔对话", "lead_kind": "codex", "cwd": str(real2)},
          ws=ws2, conv="cv_beta", subject="cv_beta")

        # 3. /tmp 下的对话
        tmp_path = "/tmp/fake_xay_test_dir_gamma"
        ws3 = "ws_gamma"
        A(self.conn, "conv.begun", "lead:cv_gamma", {"title": "临时对话", "lead_kind": "claude", "cwd": tmp_path},
          ws=ws3, conv="cv_gamma", subject="cv_gamma")

        # 4. 文件夹已删除的对话
        del_path = self.tmp / "proj_deleted"
        del_path.mkdir(parents=True, exist_ok=True)
        del_path.rmdir()
        ws4 = "ws_delta"
        A(self.conn, "conv.begun", "lead:cv_delta", {"title": "已删除项目对话", "lead_kind": "claude", "cwd": str(del_path)},
          ws=ws4, conv="cv_delta", subject="cv_delta")

        # 测试用的"真实文件夹"建在系统临时目录（/var/folders/…）里：这里只把 /tmp 当临时目录，免得它们也被归进"临时文件夹"
        with mock.patch.object(oldboard, "TEMP_PREFIXES", ("/tmp", "/private/tmp")):
            status, body = self.get_json("/api/state")
        self.assertEqual(status, 200)
        self.assertIn("workspaces", body)
        wss = body["workspaces"]

        # 前两个是真实文件夹，按文件夹名分组，legacy=False
        normal_wss = [w for w in wss if not w.get("legacy")]
        temp_wss = [w for w in wss if w.get("legacy")]

        self.assertEqual(len(normal_wss), 2)
        normal_names = {w["name"] for w in normal_wss}
        self.assertEqual(normal_names, {"proj_alpha", "proj_beta"})
        for w in normal_wss:
            self.assertFalse(w["legacy"])
            self.assertEqual(len(w["conversations"]), 1)
            self.assertFalse(w["conversations"][0]["legacy"])

        # 后两个进"临时文件夹"组且 legacy=True
        self.assertEqual(len(temp_wss), 1)
        temp_ws = temp_wss[0]
        self.assertEqual(temp_ws["name"], "临时文件夹")
        self.assertEqual(temp_ws["path"], "temp")
        self.assertTrue(temp_ws["legacy"])

        temp_cids = {c["id"] for c in temp_ws["conversations"]}
        self.assertEqual(temp_cids, {"cv_gamma", "cv_delta"})
        for c in temp_ws["conversations"]:
            self.assertTrue(c["legacy"])

    def test_quota_fields_and_unfetched_not_fabricated(self):
        status, body = self.get_json("/api/state")
        self.assertEqual(status, 200)
        self.assertIn("quota", body)
        quotas = body["quota"]
        self.assertEqual(len(quotas), 4)

        by_line = {q["line"]: q for q in quotas}
        self.assertIn("codex-sub", by_line)
        self.assertIn("codex-relay", by_line)
        self.assertIn("dsh", by_line)
        self.assertIn("agy", by_line)

        # 检查 label
        self.assertEqual(by_line["codex-sub"]["label"], "Codex · Plus")
        self.assertEqual(by_line["codex-relay"]["label"], "Codex · 中转")
        self.assertEqual(by_line["dsh"]["label"], "DeepSeek")
        self.assertTrue(by_line["agy"]["label"].startswith("Antigravity"))

        # 检查必需字段
        for q in quotas:
            for field in ("line", "agent", "label", "status", "remaining_pct", "windows",
                          "resets_at", "balance", "accounts", "source_label", "fetched_at", "live_error"):
                self.assertIn(field, q)
            # 未读到时不要瞎编数字
            if q["status"] == "unknown":
                self.assertIsNone(q["remaining_pct"])

    def test_unresolved_decision_appears_in_state(self):
        A = core_events.append
        ws = "ws_test"
        conv = "cv_test"
        A(self.conn, "conv.begun", "lead:cv_test", {"title": "测试对话", "cwd": str(self.tmp)}, ws=ws, conv=conv, subject=conv)
        A(self.conn, "decision.opened", "office", {
            "kind": "question",
            "title": "要不要加测试？",
            "detail": "细节说明",
            "options": [{"id": "yes", "label": "加"}, {"id": "no", "label": "不加"}],
            "owner": "user"
        }, ws=ws, conv=conv, subject="dc_test123")

        status, body = self.get_json("/api/state")
        self.assertEqual(status, 200)
        decs = body.get("decisions", [])
        target = next((d for d in decs if d.get("id") == "dc_test123"), None)
        self.assertIsNotNone(target)
        self.assertEqual(target["id"], "dc_test123")
        self.assertEqual(target["conversation"], conv)
        self.assertEqual(target["title"], "要不要加测试？")
        self.assertTrue(target["for_user"])
        self.assertEqual(target["options"], [{"id": "yes", "label": "加"}, {"id": "no", "label": "不加"}])

    def test_oldboard_conv_not_connected_returns_404(self):
        # oldboard_conv 还没接上时返回 404 {"error": "还没接上"}，不能报 500（模块已经加载过的话包上有同名属性，先拿掉）
        import xay.office as office_pkg
        saved = office_pkg.__dict__.pop("oldboard_conv", None)
        if saved is not None:
            self.addCleanup(setattr, office_pkg, "oldboard_conv", saved)
        with mock.patch.dict("sys.modules", {"xay.office.oldboard_conv": None}):
            status, body = self.get_json("/api/conversation?id=cv_test")
            self.assertEqual(status, 404)
            self.assertEqual(body.get("error"), "还没接上")

            status, body = self.get_json("/api/diff?execution=ex_1&path=a.py")
            self.assertEqual(status, 404)
            self.assertEqual(body.get("error"), "还没接上")

    def test_post_control_and_decision_auth(self):
        # 缺少 X-Xay-Action
        status, _, _ = self.post("/api/control", {"action": "stop_all"}, action=None)
        self.assertEqual(status, 403)

        # 错误的 X-Xay-Action
        status, _, _ = self.post("/api/control", {"action": "stop_all"}, action="wrong")
        self.assertEqual(status, 403)

        # 非同源 Origin
        status, _, _ = self.post("/api/control", {"action": "stop_all"}, action="control", origin="http://evil.com")
        self.assertEqual(status, 403)

        # decision 同样测试
        status, _, _ = self.post("/api/decision", {"id": "dc_1", "option": "yes"}, action=None)
        self.assertEqual(status, 403)

        status, _, _ = self.post("/api/decision", {"id": "dc_1", "option": "yes"}, action="wrong")
        self.assertEqual(status, 403)

        status, _, _ = self.post("/api/decision", {"id": "dc_1", "option": "yes"}, action="decide", origin="http://evil.com")
        self.assertEqual(status, 403)

    def test_events_stream_emits_thread_update_and_state_changed(self):
        A = core_events.append
        ws = "ws_ev"
        conv = "cv_ev"
        A(self.conn, "conv.begun", "lead:cv_ev", {"title": "事件测试对话", "cwd": str(self.tmp)}, ws=ws, conv=conv, subject=conv)

        def check(frames):
            names = [f["event"] for f in frames]
            return "thread.update" in names and "state.changed" in names

        def trigger():
            A(self.conn, "task.added", "lead:cv_ev", {"title": "新活", "writes": [], "card": "c", "deps": []},
              ws=ws, conv=conv, subject="tk_ev1")

        frames = self.sse("/api/events?since=0", after_open=trigger, until=check, timeout=3.0)

        events_seen = [f["event"] for f in frames]
        self.assertIn("thread.update", events_seen)
        self.assertIn("state.changed", events_seen)

        tu = next(f for f in frames if f["event"] == "thread.update")
        self.assertEqual(tu["data"]["conversation"], conv)
        self.assertEqual(tu["data"]["thread"], "")
        self.assertEqual(tu["data"]["patch"], {})


if __name__ == "__main__":
    unittest.main()
