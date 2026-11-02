"""测试：需要你：说人话、附文件、交给监工或负责人（后端）（tk_571e9cd5）"""
import http.client
import json
import os
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from xay.core import append, events, humanize, mail, open_db, snapshot
from xay.core.evidence import explain
from xay.office import rules, web
from xay.station import station


class TestNeedsYouTexts(unittest.TestCase):
    """测试各 kind 的文字（title / detail / explain / options）说人话，不出现内部术语。"""

    def test_evidence_explain_no_internal_terms(self):
        for cli_status in ("completed", "failed", "interrupted"):
            for claim in ("done", "partial", "blocked", None):
                for has_changes in (True, False):
                    for mode in ("readonly", "write"):
                        exp = explain(cli_status, claim, has_changes, mode)
                        self.assertNotIn("桌子", exp)
                        self.assertNotIn("改动先保留", exp)

    def test_station_kept_options_and_title(self):
        labels = [o["label"] for o in station.KEPT_OPTIONS]
        self.assertIn("放进项目", labels)
        self.assertIn("让他接着做", labels)
        self.assertIn("不要了", labels)
        self.assertIn("我已经自己放好了", labels)
        for label in labels:
            self.assertNotIn("桌子", label)
            self.assertNotIn("land", label)

    def test_rules_delegate_defaults_coverage(self):
        for kind in ("mismatch", "failed_with_changes", "conflict", "interrupted",
                     "desk_unmerged", "failure", "repeated_failure", "silent",
                     "budget", "amnesia", "review", "check_failed", "error_loop"):
            self.assertIn(kind, rules.DELEGATE_RULES_DEFAULTS)
            opt, reason = rules.DELEGATE_RULES_DEFAULTS[kind]
            self.assertTrue(isinstance(opt, str) and opt)
            self.assertTrue(isinstance(reason, str) and reason)
            self.assertNotIn("桌子", reason)
            self.assertNotIn("改动先保留", reason)


class TestNeedsYouDelegationAndFiles(unittest.TestCase):
    """测试 files 字段、快照带出、委托处理与接口。"""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.db_path = Path(self.tmp) / "xay.db"
        self.conn = open_db(self.db_path)
        events.rebuild(self.conn)
        self.ws = "ws_test"
        self.conv = "cv_test"
        self.lead = "lead:" + self.conv
        append(self.conn, "conv.begun", self.lead, {"title": "测试对话"}, ws=self.ws, conv=self.conv, subject=self.conv)
        append(self.conn, "employee.hired", self.lead, {"agent": "codex"}, ws=self.ws, conv=self.conv, subject="em_1")

    def tearDown(self):
        self.conn.close()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_decision_files_and_snapshot(self):
        files_data = [
            {"path": "a.py", "kind": "changed"},
            {"path": "b.py", "kind": "landed"},
        ]
        dc_id = "dc_1"
        append(self.conn, "decision.opened", "rules", {
            "kind": "failed_with_changes",
            "title": "任务失败有改动",
            "detail": "改动还没放进项目",
            "options": [{"id": "merge", "label": "放进项目"}],
            "owner": "lead",
            "files": files_data,
        }, ws=self.ws, conv=self.conv, subject=dc_id)

        snap = snapshot.build(self.conn, ws=self.ws, conv=self.conv)
        self.assertEqual(len(snap["decisions"]), 1)
        d0 = snap["decisions"][0]
        self.assertEqual(d0["id"], dc_id)
        self.assertEqual(d0["files"], files_data)
        self.assertIsNone(d0["delegated"])

    def test_decision_delegated_event_and_snapshot(self):
        dc_id = "dc_2"
        append(self.conn, "decision.opened", "rules", {
            "kind": "silent",
            "title": "员工没动静",
            "detail": "很久没输出了",
            "options": [{"id": "wait", "label": "再等等"}],
            "owner": "user",
        }, ws=self.ws, conv=self.conv, subject=dc_id)

        append(self.conn, "decision.delegated", "user", {"to": "lead"},
               ws=self.ws, conv=self.conv, subject=dc_id)

        snap = snapshot.build(self.conn, ws=self.ws, conv=self.conv)
        d0 = next(d for d in snap["decisions"] if d["id"] == dc_id)
        self.assertEqual(d0["delegated"], "lead")

    def test_delegate_to_rules_auto_resolves(self):
        dc_id = "dc_3"
        append(self.conn, "decision.opened", "rules", {
            "kind": "silent",
            "title": "员工沉默了",
            "detail": "等一会儿",
            "options": [{"id": "wait", "label": "再等等"}],
            "owner": "lead",
        }, ws=self.ws, conv=self.conv, subject=dc_id)

        append(self.conn, "decision.delegated", "user", {"to": "rules"},
               ws=self.ws, conv=self.conv, subject=dc_id)

        class DummySched:
            def __init__(self, conn, home):
                self.conn = conn
                self.home = home

        r = rules.Rules(DummySched(self.conn, self.tmp))
        s = events.load_state(self.conn)
        n = r.tick(s)
        self.assertGreater(n, 0)

        s_after = events.load_state(self.conn)
        self.assertNotIn(dc_id, s_after.get("decisions", {}))

        row = self.conn.execute("SELECT by, data FROM events WHERE type='decision.resolved' AND subject=?",
                                (dc_id,)).fetchone()
        self.assertIsNotNone(row)
        self.assertEqual(row[0], "rules")
        d = json.loads(row[1])
        self.assertEqual(d["option"], "wait")
        self.assertIn("交给监工", d["text"])

    def test_do_decision_delegate_post_action(self):
        dc_id = "dc_4"
        append(self.conn, "decision.opened", "rules", {
            "kind": "failure",
            "title": "失败了",
            "detail": "要怎么办",
            "options": [{"id": "retry", "label": "再试一次"}],
            "owner": "lead",
        }, ws=self.ws, conv=self.conv, subject=dc_id)

        res = web.do_decision(self.conn, {"id": dc_id, "delegate": "rules"}, {})
        self.assertTrue(res.get("ok"))

        s = events.load_state(self.conn)
        self.assertEqual(s["decisions"][dc_id]["delegated"], "rules")


class TestFileAndDiffAPI(unittest.TestCase):
    """测试 GET /api/v2/file 与 GET /api/v2/diff 接口。"""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="xay-file-test-"))
        self.v2_home = self.tmp / "v2"
        self.v2_home.mkdir(parents=True)
        self.db_path = self.v2_home / "xay.db"

        self.proj_dir = self.tmp / "proj"
        self.proj_dir.mkdir(parents=True)
        (self.proj_dir / "hello.txt").write_text("hello project")
        (self.proj_dir / ".env").write_text("SECRET=123")
        (self.proj_dir / "cert.key").write_text("PRIVATE KEY")
        (self.proj_dir / "sub").mkdir(parents=True)
        (self.proj_dir / "sub" / ".env.local").write_text("ENV")

        # 软链接逃出
        outside_file = self.tmp / "secret.txt"
        outside_file.write_text("outside secret")
        try:
            os.symlink(str(outside_file), str(self.proj_dir / "escape_symlink"))
        except OSError:
            pass

        # 超大文件
        large = self.proj_dir / "large.txt"
        large.write_bytes(b"x" * (205 * 1024))

        self.ws = "ws_test"
        self.conv = "cv_test"
        self.lead = "lead:" + self.conv

        conn = open_db(self.db_path)
        events.rebuild(conn)
        append(conn, "conv.begun", self.lead,
               {"title": "测试对话", "lead_kind": "claude", "cwd": str(self.proj_dir)},
               ws=self.ws, conv=self.conv, subject=self.conv)
        append(conn, "employee.hired", self.lead, {"agent": "codex"}, ws=self.ws, conv=self.conv, subject="em_1")
        append(conn, "task.added", self.lead, {"title": "做测试", "writes": ["hello.txt"], "card": ""},
               ws=self.ws, conv=self.conv, subject="tk_1")
        append(conn, "task.assigned", "office", {"owner": "em_1", "reason": "安排"},
               ws=self.ws, conv=self.conv, subject="tk_1")
        conn.close()

        # 桌子目录设置
        desk_dir = self.v2_home / "desks" / self.ws / "em_1"
        desk_dir.mkdir(parents=True)
        (desk_dir / "desk_file.txt").write_text("desk text content")

        self.env_patch = mock.patch.dict(os.environ, {"XAY2_HOME": str(self.v2_home)})
        self.env_patch.start()

        self.server = web.OfficeServer(0, db_path=str(self.db_path), live_root=self.v2_home / "live")
        self.server.start_background()

    def tearDown(self):
        self.server.stop()
        self.env_patch.stop()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _connect_client(self, c, port=None):
        try:
            c.connect()
        except (PermissionError, OSError):
            import socket
            client_sock, server_sock = socket.socketpair()
            if getattr(c, "timeout", None):
                client_sock.settimeout(c.timeout)
            c.sock = client_sock
            self.server.process_request(server_sock, ("127.0.0.1", port or self.server.port))

    def _get(self, path):
        conn = http.client.HTTPConnection("127.0.0.1", self.server.port, timeout=5)
        self._connect_client(conn, self.server.port)
        conn.request("GET", path)
        res = conn.getresponse()
        raw = res.read().decode("utf-8")
        status = res.status
        conn.close()
        try:
            data = json.loads(raw)
        except Exception:
            data = raw
        return status, data

    def test_file_api_project_success(self):
        status, data = self._get(f"/api/v2/file?conv={self.conv}&path=hello.txt")
        self.assertEqual(status, 200)
        self.assertEqual(data["path"], "hello.txt")
        self.assertEqual(data["text"], "hello project")
        self.assertFalse(data["truncated"])
        self.assertEqual(data["from"], "project")

    def test_file_api_desk_success(self):
        status, data = self._get(f"/api/v2/file?conv={self.conv}&task=tk_1&path=desk_file.txt")
        self.assertEqual(status, 200)
        self.assertEqual(data["path"], "desk_file.txt")
        self.assertEqual(data["text"], "desk text content")
        self.assertEqual(data["from"], "desk")

    def test_file_api_security_dotdot(self):
        status, _ = self._get(f"/api/v2/file?conv={self.conv}&path=../secret.txt")
        self.assertEqual(status, 403)

    def test_file_api_security_env(self):
        status, _ = self._get(f"/api/v2/file?conv={self.conv}&path=.env")
        self.assertEqual(status, 403)
        status2, _ = self._get(f"/api/v2/file?conv={self.conv}&path=sub/.env.local")
        self.assertEqual(status2, 403)

    def test_file_api_security_key(self):
        status, _ = self._get(f"/api/v2/file?conv={self.conv}&path=cert.key")
        self.assertEqual(status, 403)

    def test_file_api_security_symlink_escape(self):
        if (self.proj_dir / "escape_symlink").exists():
            status, _ = self._get(f"/api/v2/file?conv={self.conv}&path=escape_symlink")
            self.assertEqual(status, 403)

    def test_file_api_truncation(self):
        status, data = self._get(f"/api/v2/file?conv={self.conv}&path=large.txt")
        self.assertEqual(status, 200)
        self.assertTrue(data["truncated"])
        self.assertEqual(len(data["text"].encode("utf-8")), 200 * 1024)

    def test_diff_api(self):
        status, data = self._get("/api/v2/diff?task=tk_1&path=hello.txt")
        self.assertEqual(status, 200)
        self.assertIn("diff", data)


class TestNamingAndHumanize(unittest.TestCase):
    """测试主 AI 叫法改为“负责人”，不出现“总经理”。"""

    def test_who_and_event_texts_no_general_manager(self):
        s = {"employees": {}, "depts": {}, "tasks": {}, "turns": {}}
        self.assertEqual(humanize._who(s, "cv", "gm"), "负责人")
        self.assertEqual(mail.who(s, "cv", "gm"), "负责人")

        ev = {"type": "task.added", "data": {"title": "某活"}, "subject": "tk_1", "by": "lead"}
        t = humanize.text(ev, state=s)
        self.assertIn("负责人", t)
        self.assertNotIn("总经理", t)

        ev2 = {"type": "letter.answered", "by": "lead:cv", "data": {"verdict": "accept"}, "subject": "lt_1"}
        t2 = humanize.text(ev2, state=s)
        self.assertIn("负责人", t2)
        self.assertNotIn("总经理", t2)


if __name__ == "__main__":
    unittest.main()
