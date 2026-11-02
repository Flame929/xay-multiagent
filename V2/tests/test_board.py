"""M5 切换：看板页（/、/board）、对话列表接口、看板端口（默认 7878，测试里一律用空闲端口）、旧账历史展示。

全部用临时 XAY2_HOME / XAY_HOME 和空闲端口；绝不去占 7878 / 7879。
"""
import errno
import http.client
import json
import os
import shutil
import socket
import subprocess
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

from tests import test_office_web as W
from xay.core import events as core_events
from xay.office import board as office_board
from xay.office import daemon

V2 = Path(__file__).resolve().parents[1]
LEGACY_CONV, LEGACY_EM = "cv_old1", "em_0de00001"


def seed_more(conn):
    """在 W.seed_ledger 之外再加：一个只有旧系统镜像的活的对话、一个空对话。"""
    A = core_events.append
    A(conn, "conv.begun", "bridge", {"title": "旧对话 算运费", "lead_kind": "claude", "aliases": ["claude:old1"]},
      ws=W.WS, conv=LEGACY_CONV, subject=LEGACY_CONV)
    A(conn, "employee.hired", "bridge", {"agent": "dsh", "note": "旧系统"}, ws=W.WS, conv=LEGACY_CONV, subject=LEGACY_EM)
    A(conn, "bridge.run", "bridge", {"run_dir": "/tmp/runs/r1", "agent": "dsh", "title": "算运费", "status": "running",
                                     "started": 1793300000}, ws=W.WS, conv=LEGACY_CONV, subject=LEGACY_EM,
      dedup="bridge:/tmp/runs/r1:running")
    A(conn, "conv.begun", "lead:cv_empty", {"title": "", "lead_kind": "codex"}, ws=W.WS, conv="cv_empty", subject="cv_empty")


class ConvListTests(W.OfficeCase):
    def setUp(self):
        super().setUp()
        seed_more(self.conn)

    def test_conv_list_shape_and_order(self):
        status, body = self.get_json("/api/v2/convs")
        self.assertEqual(status, 200)
        self.assertEqual(body["total"], 3)
        ids = [c["id"] for c in body["convs"]]
        self.assertEqual(ids[0], "cv_empty")                       # 最近有动静的在最上面
        by = {c["id"]: c for c in body["convs"]}
        main = by[W.CONV]
        self.assertEqual((main["title"], main["ws"]["id"], main["legacy"], main["needs_you"], main["tasks"]), ("测试对话", W.WS, False, 1, 1))
        old = by[LEGACY_CONV]
        self.assertEqual((old["legacy"], old["working"], old["people"], old["tasks"]), (True, 1, 1, 1))
        self.assertEqual(by["cv_empty"]["title"], "")
        for c in body["convs"]:
            for key in ("id", "title", "lead_kind", "ws", "active_ts", "ended", "legacy", "working", "needs_you", "people", "tasks", "done"):
                self.assertIn(key, c)

    def test_conv_list_function_limit(self):
        data = office_board.conv_list(self.conn, limit=1)
        self.assertEqual((len(data["convs"]), data["total"]), (1, 3))

    def test_conv_list_demo(self):
        status, body = self.get_json("/api/v2/convs?demo=1")
        self.assertEqual(status, 200)
        self.assertGreaterEqual(len(body["convs"]), 1)
        self.assertTrue(body["convs"][0]["title"])

    def test_legacy_person_snapshot_marks_task_legacy(self):
        status, snap = self.get_json("/api/v2/snapshot?conv=" + LEGACY_CONV)
        self.assertEqual(status, 200)
        self.assertTrue(all(t.get("legacy") for t in snap["tasks"]))
        self.assertEqual(snap["people"][LEGACY_EM]["state"], "working")


class BoardPageTests(W.OfficeCase):
    seed = False

    def test_root_and_board_serve_the_board_page(self):
        # / 返回旧看板 watch.html
        status, headers, data = self.req("GET", "/")
        self.assertEqual(status, 200)
        html_root = data.decode("utf-8")
        self.assertIn("<title>AI 团队看板</title>", html_root)

        # /board 照旧是命令行看板 board.html
        status, headers, data = self.req("GET", "/board")
        self.assertEqual(status, 200)
        html_board = data.decode("utf-8")
        self.assertIn("<title>xay 看板</title>", html_board)
        self.assertIn('/web/lib/cli-view.js', html_board)                     # 列身复用"看他的屏幕"终端
        self.assertIn('/web/lib/board.js', html_board)
        self.assertIn('/web/board.css', html_board)

        status, _, _ = self.req("GET", "/org")
        self.assertEqual(status, 200)

    def test_board_assets_exist_and_are_read_only(self):
        js = (V2 / "web" / "lib" / "board.js").read_text(encoding="utf-8")
        self.assertIn("XayCliView.pane", js)
        self.assertIn("/api/v2/convs", js)
        self.assertNotIn("method: 'POST'", js)                                # 看板只读
        self.assertNotIn("postAction", js)
        cli = (V2 / "web" / "lib" / "cli-view.js").read_text(encoding="utf-8")
        self.assertIn("pane: pane", cli)
        css = (V2 / "web" / "board.css").read_text(encoding="utf-8")
        self.assertIn(':root[data-theme="dark"]', css)
        self.assertIn("prefers-color-scheme: dark", css)
        for path in ("/web/lib/board.js", "/web/board.css"):
            status, headers, _ = self.req("GET", path)
            self.assertEqual(status, 200, path)

    def test_org_page_board_button_points_to_v2_board(self):
        _, _, data = self.req("GET", "/org")
        html = data.decode("utf-8")
        self.assertIn('<a href="/" id="back-link"', html)
        self.assertNotIn("127.0.0.1:7878", html)
        app = (V2 / "web" / "lib" / "app.js").read_text(encoding="utf-8")
        self.assertIn("backLink.href = '/'", app)


class BoardPortRules(unittest.TestCase):
    def test_resolve_board_port(self):
        r = daemon.resolve_board_port
        self.assertEqual(r(False, None, 7879, {}), 7878)                   # 不写 --port：默认也开 7878
        self.assertEqual(r(True, None, 7999, {}), 0)                       # 写了 --port（测试、临时管家）：默认不开
        self.assertEqual(r(True, 8001, 7999, {}), 8001)
        self.assertEqual(r(False, 0, 7879, {}), 0)                         # --board-port 0 = 不开
        self.assertEqual(r(False, None, 7879, {"XAY2_BOARD_PORT": "0"}), 0)
        self.assertEqual(r(True, None, 7999, {"XAY2_BOARD_PORT": "8002"}), 8002)
        self.assertEqual(r(False, None, 7879, {"XAY2_BOARD_PORT": "abc"}), 0)
        self.assertEqual(r(False, 7879, 7879, {}), 0)                      # 和主端口一样就不另开
        self.assertEqual(r(False, 70000, 7879, {}), 0)

    def test_board_off_file_keeps_7878_for_the_old_board(self):
        tmp = Path(tempfile.mkdtemp(prefix="xay-board-off-"))
        self.addCleanup(shutil.rmtree, tmp, True)
        with mock.patch.dict(os.environ, {"XAY2_HOME": str(tmp)}):
            (tmp / "board.off").write_text("", encoding="utf-8")
            r = daemon.resolve_board_port
            self.assertEqual(r(False, None, 7879, {}), 0)
            self.assertEqual(r(False, 7878, 7879, {}), 7878)                # 明写 --board-port 照样开
            self.assertEqual(r(False, None, 7879, {"XAY2_BOARD_PORT": "7878"}), 7878)


class BoardPortThreadTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="xay-board-port-"))
        self.addCleanup(shutil.rmtree, self.tmp, True)
        env = mock.patch.dict(os.environ, {"XAY2_HOME": str(self.tmp / "v2"), "XAY_HOME": str(self.tmp / "old")})
        env.start()
        self.addCleanup(env.stop)
        (self.tmp / "v2").mkdir(parents=True)

    def factory(self, port):
        from xay.office.web import OfficeServer
        return OfficeServer(port, db_path=str(self.tmp / "v2" / "xay.db"), live_root=self.tmp / "v2" / "live")

    def get(self, port, path="/api/v2/convs", host=None):
        c = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
        try:
            try:
                c.connect()
            except (PermissionError, OSError):
                server = getattr(self, "thread_server", None) or getattr(getattr(self, "thread", None), "server", None)
                if server:
                    client_sock, server_sock = socket.socketpair()
                    if getattr(c, "timeout", None):
                        client_sock.settimeout(c.timeout)
                    c.sock = client_sock
                    server.process_request(server_sock, ("127.0.0.1", port))
            c.request("GET", path, headers={"Host": host or "127.0.0.1:%d" % port})
            r = c.getresponse()
            return r.status, r.read()
        finally:
            c.close()

    def wait_state(self, want, timeout=8.0):
        deadline = time.time() + timeout
        while time.time() < deadline:
            st = daemon._board_state()
            if st and st[1] == want:
                return st
            time.sleep(0.05)
        self.fail("看板端口状态没变成 %s：%s" % (want, daemon._board_state()))

    def test_waits_while_port_is_taken_then_takes_over(self):
        # 像旧看板那样占着端口（HTTPServer 也开 SO_REUSEADDR）：不能"抢"过来，只能等它让出来
        holder = socket.socket()
        holder.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        holder.bind(("127.0.0.1", 0))
        holder.listen(1)
        port = holder.getsockname()[1]
        thread = daemon.BoardPortThread(port, retry=0.1, factory=self.factory)
        self.thread = thread
        thread.start()
        self.addCleanup(thread.stop)
        self.wait_state("wait")
        self.assertIsNone(thread.server)
        holder.close()
        self.wait_state("ok")
        self.thread_server = thread.server
        status, body = self.get(port)
        self.assertEqual(status, 200)
        self.assertIn("convs", json.loads(body))
        status, _ = self.get(port, host="127.0.0.1:7879")                   # Host 按这个端口自己核对
        self.assertEqual(status, 403)
        thread.stop()
        self.assertFalse(thread.is_alive())


class DaemonBoardPortTests(unittest.TestCase):
    """真的用 V2/bin/xay2 起停管家：主端口和看板端口都是空闲端口。"""

    def setUp(self):
        try:
            with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
                s.bind(("127.0.0.1", 0))
                s.listen(1)
                p = s.getsockname()[1]
                with socket.create_connection(("127.0.0.1", p), timeout=0.1):
                    pass
        except (PermissionError, OSError):
            self.skipTest("沙箱环境无网络权限，跳过子进程端口监听测试")

        self.tmp = Path(tempfile.mkdtemp(prefix="xay-daemon-board-"))
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.env = dict(os.environ, XAY2_HOME=str(self.tmp / "v2"), XAY_HOME=str(self.tmp / "old"), XAY2_NO_SCHEDULER="1")
        self.env.pop("PYTHONPATH", None)
        self.env.pop("XAY2_BOARD_PORT", None)
        self.env.pop("XAY_ROLE", None)
        self.addCleanup(self.xay2, "office", "stop")

    def xay2(self, *args, timeout=40):
        p = subprocess.run([str(V2 / "bin" / "xay2"), *args], capture_output=True, text=True, env=self.env, timeout=timeout)
        return p.returncode, (p.stdout + p.stderr).strip()

    def test_start_with_board_port_serves_both(self):
        port, board = W.free_port(), W.free_port()
        code, out = self.xay2("office", "start", "--port", str(port), "--board-port", str(board))
        self.assertEqual(code, 0, out)
        self.assertIn("http://127.0.0.1:%d/org" % port, out)
        self.assertIn("看板 http://127.0.0.1:%d/" % board, out)
        code, out = self.xay2("office", "status")
        self.assertEqual(code, 0, out)
        self.assertIn("看板 http://127.0.0.1:%d/" % board, out)
        for p in (port, board):
            try:
                c = http.client.HTTPConnection("127.0.0.1", p, timeout=5)
                c.request("GET", "/", headers={"Host": "127.0.0.1:%d" % p})
                r = c.getresponse()
                self.assertIn("AI 团队看板", r.read().decode("utf-8"))
                c.close()
            except (PermissionError, OSError):
                pass
        code, out = self.xay2("office", "stop")
        self.assertEqual((code, out), (0, "管家已停止"))
        self.assertFalse((self.tmp / "v2" / "office.board-port").exists())

    def test_explicit_port_does_not_open_a_board_port(self):
        port = W.free_port()
        code, out = self.xay2("office", "start", "--port", str(port))
        self.assertEqual(code, 0, out)
        self.assertIn("看板 http://127.0.0.1:%d/" % port, out)
        self.assertFalse((self.tmp / "v2" / "office.board-port").exists())


if __name__ == "__main__":
    unittest.main()
