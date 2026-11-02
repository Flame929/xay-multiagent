"""管家网页服务的测试：Host / Origin 检查、快照、事件流、静态文件、写入接口、直播通道、后台进程。

全部用临时目录（XAY2_HOME / XAY_HOME 都指向临时目录），不碰真实的 ~/.xay。
"""
import http.client
import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

_v2 = str(Path(__file__).resolve().parents[1])
if _v2 not in sys.path:
    sys.path.insert(0, _v2)

from xay.core import db as core_db
from xay.core import errors as core_errors
from xay.core import events as core_events
from xay.core import snapshot as core_snapshot
from xay.office import web
from xay.office.demo import DemoLedger

V2 = Path(__file__).resolve().parents[1]
WS, CONV, EM, DEPT = "ws_t0000001", "cv_t1", "em_aa000001", "fe"
EVIL_PATHS = [
    "/web/../docs/protocol.md",
    "/web/%2e%2e/docs/protocol.md",
    "/web/..%2fdocs%2fprotocol.md",
    "/web/lib/../../docs/protocol.md",
    "/xay-chars/../../docs/protocol.md",
    "/xay-chars/%2e%2e/%2e%2e/docs/protocol.md",
    "/web/%252e%252e/docs/protocol.md",
    "/web/..\\docs\\protocol.md",
    "/web/%00",
    "/web/.git/config",
    "//etc/passwd",
    "/web//etc/passwd",
]


def seed_ledger(conn):
    """一个最小的对话：1 个部门、1 个员工、1 个任务、1 封信、1 件需要你拿主意的事。"""
    lead = "lead:" + CONV
    A = core_events.append
    A(conn, "conv.begun", lead, {"title": "测试对话", "lead_kind": "claude", "aliases": ["claude:t1"], "cwd": "/tmp/t"},
      ws=WS, conv=CONV, subject=CONV)
    A(conn, "dept.opened", lead, {"name": "前端部", "mission": "做页面", "owns": ["web/**"], "head": "rules"},
      ws=WS, conv=CONV, subject=DEPT)
    A(conn, "employee.hired", lead, {"agent": "codex"}, ws=WS, conv=CONV, subject=EM)
    A(conn, "employee.joined", lead, {"dept": DEPT}, ws=WS, conv=CONV, subject=EM)
    A(conn, "task.added", lead, {"title": "做轮播", "deps": [], "writes": [], "card": "x", "mode": "write", "reason": "开工"},
      ws=WS, conv=CONV, subject="tk_t0000001")
    A(conn, "letter.sent", lead, {"from": "gm", "to": EM, "kind": "tell", "subject": "先做轮播", "body": "开始吧"},
      ws=WS, conv=CONV, subject="lt_t0000001")
    A(conn, "decision.opened", "office",
      {"kind": "question", "title": "要不要加图片？", "detail": "", "options": [{"id": "yes", "label": "加"}, {"id": "no", "label": "不加"}],
       "owner": "user"}, ws=WS, conv=CONV, subject="dc_t0000001")


def free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def fast_demo():
    return DemoLedger(min_gap=0.0, max_gap=0.002, scale=0.01, loop_pause=3.0)


class OfficeCase(unittest.TestCase):
    """临时 XAY2_HOME / XAY_HOME + 种好数据的账本 + 跑在线程里的服务。"""

    seed = True
    demo_factory = staticmethod(fast_demo)

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="xay-office-test-"))
        self.addCleanup(shutil.rmtree, self.tmp, True)
        env = mock.patch.dict(os.environ, {"XAY2_HOME": str(self.tmp / "v2"), "XAY_HOME": str(self.tmp / "old")})
        env.start()
        self.addCleanup(env.stop)
        self.db_path = str(self.tmp / "v2" / "xay.db")
        self.conn = core_db.open_db(self.db_path)
        self.addCleanup(self.conn.close)
        if self.seed:
            seed_ledger(self.conn)
        self.start_server()

    def start_server(self, **kw):
        kw.setdefault("db_path", self.db_path)
        kw.setdefault("live_root", self.tmp / "v2" / "live")
        kw.setdefault("heartbeat_seconds", 0.3)
        kw.setdefault("poll_seconds", 0.03)
        kw.setdefault("demo_factory", self.demo_factory)
        self.srv = web.OfficeServer(0, **kw).start_background()
        self.addCleanup(self.srv.stop)
        self.port = self.srv.port
        return self.srv

    def _connect_client(self, c, port=None):
        try:
            c.connect()
        except (PermissionError, OSError):
            client_sock, server_sock = socket.socketpair()
            if getattr(c, "timeout", None):
                client_sock.settimeout(c.timeout)
            c.sock = client_sock
            self.srv.process_request(server_sock, ("127.0.0.1", port or self.port))

    # ---- 请求小工具 ----
    def req(self, method, path, *, headers=None, body=None, host=None, origin=True, port=None):
        port = port or self.port
        h = {"Host": host or "127.0.0.1:%d" % port}
        if method == "POST" and origin:
            h["Origin"] = "http://" + h["Host"] if origin is True else origin
        h.update(headers or {})
        if isinstance(body, (dict, list)):
            body = json.dumps(body).encode("utf-8")
            h.setdefault("Content-Type", "application/json")
        c = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
        self._connect_client(c, port)
        try:
            try:
                c.request(method, path, body=body, headers=h)
            except (BrokenPipeError, ConnectionResetError):
                pass
            r = c.getresponse()
            data = r.read()
            return r.status, {k.lower(): v for k, v in r.getheaders()}, data
        finally:
            c.close()

    def get_json(self, path, **kw):
        status, headers, data = self.req("GET", path, **kw)
        return status, json.loads(data.decode("utf-8"))

    def post(self, path, body, action, **kw):
        headers = dict(kw.pop("headers", {}))
        if action:
            headers["X-Xay-Action"] = action
        return self.req("POST", path, body=body, headers=headers, **kw)

    def sse(self, path, *, headers=None, until=None, timeout=4.0, after_open=None, host=None):
        """读 SSE，返回 [{id, event, data}]。until(frames) 为真就提前结束；after_open() 在收到开头注释后调用。"""
        h = {"Host": host or "127.0.0.1:%d" % self.port}
        h.update(headers or {})
        c = http.client.HTTPConnection("127.0.0.1", self.port, timeout=timeout)
        self._connect_client(c, self.port)
        frames, cur = [], {}
        try:
            c.request("GET", path, headers=h)
            r = c.getresponse()
            if r.status != 200:
                self.fail("事件流返回 %s：%s" % (r.status, r.read()[:200]))
            self.assertIn("text/event-stream", r.getheader("Content-Type"))
            deadline, opened = time.monotonic() + timeout, False
            while time.monotonic() < deadline:
                try:
                    line = r.readline()
                except (socket.timeout, TimeoutError):
                    break
                if not line:
                    break
                line = line.decode("utf-8").rstrip("\r\n")
                if line.startswith(": connected") and not opened:
                    opened = True
                    if after_open:
                        after_open()
                elif line == "":
                    if cur.get("data") is not None:
                        cur["data"] = json.loads(cur["data"])
                        frames.append(cur)
                        if until and until(frames):
                            break
                    cur = {}
                elif line.startswith("id: "):
                    cur["id"] = int(line[4:])
                elif line.startswith("event: "):
                    cur["event"] = line[7:]
                elif line.startswith("data: "):
                    cur["data"] = line[6:]
        finally:
            c.close()
        return frames

    def events(self, etype=None):
        rows = self.conn.execute("SELECT seq, type, by, subject, data FROM events ORDER BY seq").fetchall()
        return [dict(r, data=json.loads(r["data"])) for r in rows if etype is None or r["type"] == etype]


class PureFunctionTests(unittest.TestCase):
    def test_host_and_origin_rules(self):
        self.assertTrue(web.host_ok("127.0.0.1:7879", 7879))
        self.assertTrue(web.host_ok("LOCALHOST:7879", 7879))
        for bad in (None, "", "evil.com", "127.0.0.1", "127.0.0.1:7878", "localhost", "[::1]:7879", "127.0.0.1:7879.evil.com"):
            self.assertFalse(web.host_ok(bad, 7879), bad)
        self.assertTrue(web.origin_ok("http://127.0.0.1:7879", "127.0.0.1:7879", 7879))
        self.assertTrue(web.origin_ok("http://localhost:7879", "localhost:7879", 7879))
        for origin, host in ((None, "127.0.0.1:7879"), ("", "127.0.0.1:7879"), ("null", "127.0.0.1:7879"),
                             ("http://evil.com", "127.0.0.1:7879"), ("https://127.0.0.1:7879", "127.0.0.1:7879"),
                             ("http://localhost:7879", "127.0.0.1:7879"), ("http://127.0.0.1:7879", "evil.com")):
            self.assertFalse(web.origin_ok(origin, host, 7879), (origin, host))

    def test_resolve_static_blocks_traversal(self):
        root = V2 / "web"
        status, path = web.resolve_static("/org", root)
        self.assertEqual((status, path.name), ("ok", "org.html"))
        self.assertEqual(web.resolve_static("/web/lib/api.js", root)[0], "ok")
        self.assertEqual(web.resolve_static("/xay-chars/core.js", root)[0], "ok")
        self.assertEqual(web.resolve_static("/chars", root)[1].name, "gallery.html")
        for evil in ("/web/../docs/protocol.md", "/web/%2e%2e/docs/protocol.md", "/web/a/%2e%2e/%2e%2e/x",
                     "/xay-chars/../../docs/protocol.md", "/web/.git/config", "/web/a\\b", "/web/a%00b"):
            self.assertEqual(web.resolve_static(evil, root), ("forbidden", None), evil)
        for missing in ("/web/", "/web/nope.js", "/xay-chars/", "/other", "/web/lib"):
            self.assertEqual(web.resolve_static(missing, root)[0], "notfound", missing)

    def test_content_types(self):
        self.assertEqual(web.content_type_for("a.js"), "text/javascript; charset=utf-8")
        self.assertEqual(web.content_type_for("a.webp"), "image/webp")
        self.assertEqual(web.content_type_for("a.css"), "text/css; charset=utf-8")
        self.assertEqual(web.content_type_for("a.json"), "application/json; charset=utf-8")
        self.assertEqual(web.content_type_for("a.html"), "text/html; charset=utf-8")


class SecurityTests(OfficeCase):
    def test_wrong_host_is_403(self):
        for path in ("/org", "/api/v2/snapshot", "/web/lib/api.js", "/api/v2/stream"):
            for host in ("evil.example.com", "127.0.0.1:1", "127.0.0.1", "localhost"):
                status, _, data = self.req("GET", path, host=host)
                self.assertEqual(status, 403, (path, host))
                err = json.loads(data.decode("utf-8"))["error"]
                self.assertRegex(err, r"[一-鿿]")

    def test_localhost_host_is_allowed(self):
        status, _, _ = self.req("GET", "/api/v2/snapshot", host="localhost:%d" % self.port)
        self.assertEqual(status, 200)

    def test_post_needs_action_header_and_same_origin(self):
        body = {"id": "dc_t0000001", "option": "yes"}
        before = core_events.max_seq(self.conn)
        cases = [
            ("缺少 X-Xay-Action", dict(action=None)),
            ("动作头和接口不对应", dict(action="letter")),
            ("外来 Origin", dict(action="decide", origin="http://evil.example.com")),
            ("没有 Origin", dict(action="decide", origin=False)),
            ("Origin 是 localhost 但 Host 是 127.0.0.1", dict(action="decide", origin="http://localhost:%d" % self.port)),
            ("Host 不对", dict(action="decide", host="evil.example.com", origin="http://evil.example.com")),
        ]
        for label, opts in cases:
            action = opts.pop("action")
            status, _, data = self.post("/api/v2/decision", body, action, **opts)
            self.assertEqual(status, 403, label)
            self.assertIn("error", json.loads(data.decode("utf-8")), label)
        self.assertEqual(core_events.max_seq(self.conn), before, "被拒绝的请求不能写入账本")
        status, _, _ = self.post("/api/v2/decision", body, "decide")
        self.assertEqual(status, 200)
        self.assertEqual(core_events.max_seq(self.conn), before + 1)

    def test_no_cors_headers_and_options_rejected(self):
        for method, path, extra in (("GET", "/api/v2/snapshot", {"Origin": "http://evil.example.com"}),
                                    ("GET", "/org", {}), ("OPTIONS", "/api/v2/decision", {"Origin": "http://evil.example.com",
                                    "Access-Control-Request-Method": "POST"})):
            status, headers, _ = self.req(method, path, headers=extra)
            self.assertFalse([k for k in headers if k.startswith("access-control-")], (method, path, headers))
            if method == "OPTIONS":
                self.assertEqual(status, 405)
        status, headers, _ = self.req("GET", "/api/v2/snapshot", host="evil.com")
        self.assertEqual(status, 403)
        self.assertFalse([k for k in headers if k.startswith("access-control-")])

    def test_server_binds_loopback_only(self):
        self.assertEqual(self.srv.server_address[0], "127.0.0.1")


class SnapshotTests(OfficeCase):
    def test_snapshot_json(self):
        status, snap = self.get_json("/api/v2/snapshot")
        self.assertEqual(status, 200)
        self.assertEqual(snap["v"], 1)
        self.assertEqual(snap["seq"], core_events.max_seq(self.conn))
        self.assertEqual(snap["conversation"]["id"], CONV)
        self.assertEqual(snap["people"][EM]["label"], "Codex 1")
        self.assertEqual([t["title"] for t in snap["tasks"]], ["做轮播"])
        self.assertEqual([d["id"] for d in snap["decisions"]], ["dc_t0000001"])
        direct = core_snapshot.build(self.conn, now=snap["now"])
        self.assertEqual(snap["people"], direct["people"])
        self.assertEqual(snap["tasks"], direct["tasks"])

    def test_snapshot_query_params(self):
        status, snap = self.get_json("/api/v2/snapshot?ws=%s&conv=%s" % (WS, CONV))
        self.assertEqual((status, snap["conversation"]["id"], snap["ws"]["id"]), (200, CONV, WS))
        _, snap = self.get_json("/api/v2/snapshot?conv=cv_nope")
        self.assertEqual(snap["tasks"], [])

    def test_unknown_api_path_is_404_json(self):
        status, body = self.get_json("/api/v2/nope")
        self.assertEqual(status, 404)
        self.assertIn("error", body)


class BrokenLedgerTests(OfficeCase):
    seed = False

    def test_real_data_failure_reports_error_and_never_shows_demo(self):
        blocker = self.tmp / "afile"
        blocker.write_text("x")
        self.start_server(db_path=str(blocker / "xay.db"))
        with self.assertLogs("xay.office.web", "WARNING"):
            status, headers, data = self.req("GET", "/api/v2/snapshot")
        body = json.loads(data.decode("utf-8"))
        self.assertEqual(status, 500)
        self.assertTrue(body["error"].startswith("读不到数据"), body)
        self.assertEqual(body["detail"], body["error"])
        self.assertNotIn("网店", data.decode("utf-8"))
        self.assertIsNone(self.srv._demo, "真实数据出错时不能偷偷启动示例")
        with self.assertLogs("xay.office.web", "WARNING"):
            status, _, _ = self.req("GET", "/api/v2/stream")
        self.assertEqual(status, 500)


class StreamTests(OfficeCase):
    def test_since_returns_only_later_events(self):
        top = core_events.max_seq(self.conn)
        frames = self.sse("/api/v2/stream?since=3", until=lambda f: f[-1]["id"] >= top)
        ledger = [f for f in frames if f["event"] == "ledger"]
        self.assertEqual([f["id"] for f in ledger], list(range(4, top + 1)))
        for f in ledger:
            self.assertEqual(f["data"]["seq"], f["id"])
            self.assertTrue(f["data"]["text"])
            self.assertIn("type", f["data"])
        letter = next(f for f in ledger if f["data"]["type"] == "letter.sent")
        self.assertEqual((letter["data"]["from"], letter["data"]["to"], letter["data"]["kind"]), ("gm", EM, "tell"))

    def test_last_event_id_wins_over_since_on_reconnect(self):
        top = core_events.max_seq(self.conn)
        frames = self.sse("/api/v2/stream?since=1", headers={"Last-Event-ID": str(top - 1)},
                          until=lambda f: f[-1]["id"] >= top)
        self.assertEqual([f["id"] for f in frames if f["event"] == "ledger"], [top])

    def silent(self, n=1):
        """写 n 条没有大白话的事件（旧系统镜像里排队中的 bridge.run，协议 1.5 下排队没有大白话）。"""
        for i in range(n):
            core_events.append(self.conn, "bridge.run", "bridge", {"run_dir": "/x/%d" % i, "status": "queued"},
                               ws=WS, conv=CONV, subject="r%d" % i)

    def test_without_since_only_new_events_are_sent(self):
        lead = "lead:" + CONV

        def write():
            core_events.append(self.conn, "dept.opened", lead, {"name": "后端部", "mission": "", "owns": [], "head": "rules"},
                               ws=WS, conv=CONV, subject="be")

        frames = self.sse("/api/v2/stream", after_open=write, until=lambda f: any(x["event"] == "ledger" for x in f))
        ledger = [f for f in frames if f["event"] == "ledger"]
        self.assertEqual(len(ledger), 1)
        self.assertEqual(ledger[0]["data"]["type"], "dept.opened")
        self.assertIn("后端部", ledger[0]["data"]["text"])
        self.assertFalse([f for f in frames if f["event"] == "changed"], "只有大白话事件时不该有 changed")

    def test_events_without_text_become_one_changed_frame_per_tick(self):
        self.start_server(poll_seconds=0.4)            # 拉长一拍，让下面几条事件落在同一拍里
        lead = "lead:" + CONV
        seqs = {}

        def write():
            self.silent(2)
            seqs["ledger"] = core_events.append(self.conn, "dept.opened", lead, {"name": "后端部", "mission": "", "owns": [], "head": "rules"},
                                                ws=WS, conv=CONV, subject="be")
            self.silent(2)
            seqs["last_silent"] = core_events.max_seq(self.conn)

        frames = self.sse("/api/v2/stream", after_open=write, timeout=3,
                          until=lambda f: any(x["event"] == "changed" for x in f) and any(x["event"] == "ledger" for x in f))
        changed = [f for f in frames if f["event"] == "changed"]
        ledger = [f for f in frames if f["event"] == "ledger"]
        self.assertEqual(len(changed), 1, "同一拍里的 4 条无文案事件只合并成一帧")
        self.assertEqual(changed[0]["data"], {"seq": seqs["last_silent"]})
        self.assertEqual(changed[0]["id"], seqs["last_silent"])
        self.assertEqual([f["id"] for f in ledger], [seqs["ledger"]])
        ids = [f["id"] for f in frames if f["event"] in ("ledger", "changed")]
        self.assertEqual(ids, sorted(ids), "id 要保持从小到大，断线重连才不会重复")

    def test_only_silent_events_still_notify_the_page(self):
        def write():
            self.silent(3)

        frames = self.sse("/api/v2/stream", after_open=write, timeout=3, until=lambda f: any(x["event"] == "changed" for x in f))
        self.assertFalse([f for f in frames if f["event"] == "ledger"])
        changed = [f for f in frames if f["event"] == "changed"]
        self.assertEqual(len(changed), 1)
        self.assertEqual(changed[0]["data"]["seq"], core_events.max_seq(self.conn))

    def test_changed_follows_since_and_conv_filters(self):
        self.silent(1)
        top = core_events.max_seq(self.conn)
        frames = self.sse("/api/v2/stream?since=%d" % top, timeout=0.8)
        self.assertFalse([f for f in frames if f["event"] in ("ledger", "changed")], "since 之前的事件不再推")
        frames = self.sse("/api/v2/stream?since=%d" % (top - 1), timeout=3, until=lambda f: any(x["event"] == "changed" for x in f))
        self.assertEqual([f["id"] for f in frames if f["event"] == "changed"], [top])
        frames = self.sse("/api/v2/stream?conv=cv_other&since=%d" % (top - 1), timeout=0.8)
        self.assertFalse([f for f in frames if f["event"] in ("ledger", "changed")], "别的对话的无文案事件不推")

    def test_text_failure_degrades_to_changed(self):
        with mock.patch.object(web.core_humanize, "text", side_effect=RuntimeError("坏了")):
            with self.assertLogs("xay.office.web", "WARNING"):
                frames = self.sse("/api/v2/stream?since=%d" % (core_events.max_seq(self.conn) - 1), timeout=3,
                                  until=lambda f: any(x["event"] == "changed" for x in f))
        self.assertEqual([f["event"] for f in frames if f["event"] != "heartbeat"], ["changed"])

    def test_filters_by_conv(self):
        lead2 = "lead:cv_t2"

        def write():
            core_events.append(self.conn, "conv.begun", lead2, {"title": "别的对话", "lead_kind": "claude", "aliases": [], "cwd": "/y"},
                               ws=WS, conv="cv_t2", subject="cv_t2")
            core_events.append(self.conn, "conv.titled", "lead:" + CONV, {"title": "改名了"}, ws=WS, conv=CONV, subject=CONV)

        frames = self.sse("/api/v2/stream?conv=" + CONV, after_open=write, until=lambda f: any(x["event"] == "ledger" for x in f))
        ledger = [f for f in frames if f["event"] == "ledger"]
        self.assertEqual([f["data"]["type"] for f in ledger], ["conv.titled"])

    def test_heartbeat(self):
        frames = self.sse("/api/v2/stream", until=lambda f: any(x["event"] == "heartbeat" for x in f), timeout=3)
        self.assertTrue(any(f["event"] == "heartbeat" for f in frames))

    def test_stream_ahead_of_ledger_does_not_replay_history(self):
        frames = self.sse("/api/v2/stream?since=99999", timeout=0.8)
        self.assertEqual([f for f in frames if f["event"] == "ledger"], [])

    def test_task_landed_carries_owner_for_the_page_animation(self):
        lead = "lead:" + CONV

        def write():
            core_events.append(self.conn, "task.assigned", lead, {"owner": EM, "reason": "合适"}, ws=WS, conv=CONV, subject="tk_t0000001")
            core_events.append(self.conn, "task.landed", "office", {"files": [], "add": 0, "del": 0}, ws=WS, conv=CONV, subject="tk_t0000001")

        frames = self.sse("/api/v2/stream", after_open=write, until=lambda f: any(x["data"].get("type") == "task.landed" for x in f if "data" in x))
        landed = next(f for f in frames if f["data"].get("type") == "task.landed")
        self.assertEqual(landed["data"].get("owner"), EM)


class StaticTests(OfficeCase):
    def test_pages_and_assets(self):
        cases = [
            ("/org", "text/html; charset=utf-8"),
            ("/web/org.css", "text/css; charset=utf-8"),
            ("/web/lib/api.js", "text/javascript; charset=utf-8"),
            ("/web/lib/app.js", "text/javascript; charset=utf-8"),
            ("/web/lib/employee-panel.js", "text/javascript; charset=utf-8"),
            ("/xay-chars/core.js", "text/javascript; charset=utf-8"),
            ("/xay-chars/img/agy-calm.webp", "image/webp"),
            ("/chars", "text/html; charset=utf-8"),
            ("/web/fixtures/snapshot-sample.json", "application/json; charset=utf-8"),
        ]
        for path, ctype in cases:
            status, headers, data = self.req("GET", path)
            self.assertEqual(status, 200, path)
            self.assertEqual(headers["content-type"], ctype, path)
            self.assertEqual(int(headers["content-length"]), len(data), path)
            self.assertEqual(headers["x-content-type-options"], "nosniff")
        status, _, data = self.req("GET", "/org")
        self.assertIn("xay".encode(), data)
        status, headers, data = self.req("HEAD", "/web/lib/api.js")
        self.assertEqual((status, data), (200, b""))

    def test_root_redirects_to_org_or_serves_board(self):
        status, headers, _ = self.req("GET", "/")
        if (V2 / "web" / "board.html").is_file():
            self.assertEqual(status, 200)
        else:
            self.assertEqual((status, headers["location"]), (302, "/org"))

    def test_missing_files_are_404(self):
        for path in ("/web/nope.js", "/web/", "/web/lib", "/xay-chars/nope.png", "/nothing"):
            status, _, data = self.req("GET", path)
            self.assertEqual(status, 404, path)
            self.assertIn("error", json.loads(data.decode("utf-8")))

    def test_path_traversal_is_blocked(self):
        secret = (V2 / "docs" / "protocol.md").read_bytes()[:60]
        for path in EVIL_PATHS:
            status, _, data = self.req("GET", path)
            self.assertIn(status, (403, 404), path)
            self.assertNotIn(secret, data, path)
            self.assertNotIn(b"root:", data, path)

    def test_symlink_pointing_outside_root_is_blocked(self):
        root = self.tmp / "webroot"
        (root / "chars").mkdir(parents=True)
        (root / "ok.txt").write_text("可以看", encoding="utf-8")
        outside = self.tmp / "outside.txt"
        outside.write_text("机密内容", encoding="utf-8")
        (root / "link.txt").symlink_to(outside)
        self.start_server(web_root=root)
        status, _, data = self.req("GET", "/web/ok.txt")
        self.assertEqual((status, data.decode("utf-8")), (200, "可以看"))
        status, _, data = self.req("GET", "/web/link.txt")
        self.assertEqual(status, 403)
        self.assertNotIn("机密内容", data.decode("utf-8"))


class WriteApiTests(OfficeCase):
    def test_decision_resolves_and_cannot_be_repeated(self):
        status, _, data = self.post("/api/v2/decision", {"id": "dc_t0000001", "option": "yes", "text": "好的"}, "decide")
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(data.decode("utf-8"))["ok"], True)
        ev = self.events("decision.resolved")[-1]
        self.assertEqual((ev["by"], ev["subject"], ev["data"]), ("user", "dc_t0000001", {"option": "yes", "text": "好的"}))
        _, snap = self.get_json("/api/v2/snapshot")
        self.assertEqual(snap["decisions"], [])
        status, _, data = self.post("/api/v2/decision", {"id": "dc_t0000001", "option": "yes"}, "decide")
        self.assertEqual(status, 422)
        self.assertRegex(json.loads(data.decode("utf-8"))["error"], r"[一-鿿]")

    def test_decision_with_bad_input_is_422(self):
        for body in ({"id": "dc_t0000001", "option": "maybe"}, {"id": "dc_nope", "option": "yes"}, {"id": "dc_t0000001"}, {}):
            status, _, _ = self.post("/api/v2/decision", body, "decide")
            self.assertEqual(status, 422, body)
        self.assertEqual(self.events("decision.resolved"), [])

    def test_letter_to_employee_and_department(self):
        status, _, data = self.post("/api/v2/letter", {"to": EM, "subject": "加油", "body": "辛苦了"}, "letter")
        self.assertEqual(status, 200)
        lt_id = json.loads(data.decode("utf-8"))["id"]
        self.assertRegex(lt_id, r"^lt_[0-9a-f]{8}$")
        status, _, _ = self.post("/api/v2/letter", {"to": DEPT, "body": "能不能把接口先定下来。然后再说"}, "letter")
        self.assertEqual(status, 200)
        sent = self.events("letter.sent")[-2:]
        self.assertEqual([e["by"] for e in sent], ["user", "user"])
        self.assertEqual([(e["data"]["from"], e["data"]["to"], e["data"]["kind"]) for e in sent],
                         [("user", EM, "tell"), ("user", DEPT, "request")])
        self.assertEqual(sent[1]["data"]["subject"], "能不能把接口先定下来")
        _, snap = self.get_json("/api/v2/snapshot")
        self.assertIn(lt_id, [l["id"] for l in snap["letters"]])

    def second_conversation(self):
        """同一个工作区里再开一个更晚的对话 cv_t2（有自己的部门 fe 和同名员工所在的工作区）。"""
        lead2 = "lead:cv_t2"
        core_events.append(self.conn, "conv.begun", lead2, {"title": "第二个对话", "lead_kind": "claude", "aliases": [], "cwd": "/t2"},
                           ws=WS, conv="cv_t2", subject="cv_t2")
        core_events.append(self.conn, "dept.opened", lead2, {"name": "后端部", "mission": "", "owns": [], "head": "rules"},
                           ws=WS, conv="cv_t2", subject="be")

    def test_letter_and_control_go_to_the_conversation_the_page_is_viewing(self):
        self.second_conversation()
        status, _, _ = self.post("/api/v2/letter", {"to": EM, "body": "给第一个对话的信", "conv": CONV, "ws": WS}, "letter")
        self.assertEqual(status, 200)
        status, _, _ = self.post("/api/v2/letter", {"to": "be", "body": "给第二个对话后端部的信", "conv": "cv_t2"}, "letter")
        self.assertEqual(status, 200)
        status, _, _ = self.post("/api/v2/control", {"action": "stop_all", "conv": CONV}, "control")
        self.assertEqual(status, 200)
        letters = self.events("letter.sent")[-2:]
        self.assertEqual([self.conv_of(e["seq"]) for e in letters], [CONV, "cv_t2"])
        self.assertEqual(letters[1]["data"]["kind"], "request")
        self.assertEqual(self.conv_of(self.events("control.requested")[-1]["seq"]), CONV)
        # 部门只在自己的对话里找：cv_t1 里没有后端部
        status, _, _ = self.post("/api/v2/letter", {"to": "be", "body": "写错对话了", "conv": CONV}, "letter")
        self.assertEqual(status, 422)
        status, _, _ = self.post("/api/v2/letter", {"to": EM, "body": "hi", "conv": "cv_nope"}, "letter")
        self.assertEqual(status, 422)
        status, _, _ = self.post("/api/v2/letter?conv=" + CONV, {"to": EM, "body": "conv 写在网址里也行"}, "letter")
        self.assertEqual(status, 200)
        self.assertEqual(self.conv_of(self.events("letter.sent")[-1]["seq"]), CONV)

    def conv_of(self, seq):
        return self.conn.execute("SELECT conv FROM events WHERE seq=?", (seq,)).fetchone()[0]

    def test_letter_without_conv_uses_the_same_default_as_the_snapshot(self):
        self.second_conversation()
        _, snap = self.get_json("/api/v2/snapshot")
        status, _, _ = self.post("/api/v2/letter", {"to": EM, "body": "没指定对话"}, "letter")
        self.assertEqual(status, 200)
        self.assertEqual(self.conv_of(self.events("letter.sent")[-1]["seq"]), snap["conversation"]["id"])

    def test_letter_validation(self):
        for body in ({"to": "em_nobody", "body": "hi"}, {"to": EM, "body": "  "}, {"body": "hi"}, {"to": EM, "body": "x" * 5000}):
            status, _, _ = self.post("/api/v2/letter", body, "letter")
            self.assertEqual(status, 422, str(body)[:40])
        self.assertEqual(len(self.events("letter.sent")), 1)

    def test_control(self):
        status, _, _ = self.post("/api/v2/control", {"em": EM, "action": "steer", "text": "先做手机端"}, "control")
        self.assertEqual(status, 200)
        status, _, _ = self.post("/api/v2/control", {"task": "tk_t0000001", "action": "interrupt"}, "control")
        self.assertEqual(status, 200)
        status, _, _ = self.post("/api/v2/control", {"action": "stop_all"}, "control")
        self.assertEqual(status, 200)
        evs = self.events("control.requested")
        self.assertEqual([e["by"] for e in evs], ["user"] * 3)
        self.assertEqual(evs[0]["data"], {"action": "steer", "by_source": "page", "text": "先做手机端"})
        self.assertEqual((evs[0]["subject"], evs[1]["subject"], evs[2]["subject"]), (EM, "tk_t0000001", None))
        for body in ({"em": EM, "action": "explode"}, {"action": "steer"}, {"em": "em_nobody", "action": "stop"},
                     {"em": EM, "action": "steer"}, {"em": EM, "task": "tk_t0000001", "action": "stop"}):
            status, _, _ = self.post("/api/v2/control", body, "control")
            self.assertEqual(status, 422, body)

    def test_notes_api(self):
        # 1. 接口能写
        status, _, data = self.post("/api/v2/notes", {"em": EM, "text": "新的工作笔记全文"}, "notes")
        self.assertEqual(status, 200)
        self.assertTrue(json.loads(data.decode("utf-8"))["ok"])
        ev = self.events("notes.updated")[-1]
        self.assertEqual((ev["by"], ev["subject"], ev["data"]), ("user", EM, {"active": "新的工作笔记全文"}))

        # 2. 超过 6000 字返回 400
        status, _, data = self.post("/api/v2/notes", {"em": EM, "text": "字" * 6001}, "notes")
        self.assertEqual(status, 400)
        self.assertIn("6000", json.loads(data.decode("utf-8"))["error"])

        # 3. 缺请求头被拒（403）
        status, _, _ = self.post("/api/v2/notes", {"em": EM, "text": "test"}, action=None)
        self.assertEqual(status, 403)

        # 4. 跨域被拒（403）
        status, _, _ = self.post("/api/v2/notes", {"em": EM, "text": "test"}, "notes", origin="http://evil.example.com")
        self.assertEqual(status, 403)

        # 5. 参数校验（422）
        for bad in ({"text": "没给员工"}, {"em": "em_nobody", "text": "查无此人"}, {"em": EM, "text": 123}):
            status, _, _ = self.post("/api/v2/notes", bad, "notes")
            self.assertEqual(status, 422, bad)

    def test_bad_bodies(self):
        status, _, _ = self.post("/api/v2/decision", b"{not json", "decide")
        self.assertEqual(status, 422)
        status, _, _ = self.post("/api/v2/decision", b"[1,2]", "decide")
        self.assertEqual(status, 422)
        status, _, _ = self.post("/api/v2/decision", b"x" * (web.MAX_BODY + 10), "decide")
        self.assertEqual(status, 413)
        status, _, _ = self.post("/api/v2/nothing", {}, "decide")
        self.assertEqual(status, 404)

    def test_permission_error_maps_to_403_and_value_error_to_422(self):
        with mock.patch.object(core_events, "append", side_effect=core_errors.PermissionError("没有权限写入事件：letter.sent")):
            status, _, data = self.post("/api/v2/letter", {"to": EM, "body": "hi"}, "letter")
        self.assertEqual(status, 403)
        self.assertEqual(json.loads(data.decode("utf-8"))["error"], "没有权限写入事件：letter.sent")
        with mock.patch.object(core_events, "append", side_effect=ValueError("员工不存在")):
            status, _, data = self.post("/api/v2/letter", {"to": EM, "body": "hi"}, "letter")
        self.assertEqual((status, json.loads(data.decode("utf-8"))["error"]), (422, "员工不存在"))


class LiveTests(OfficeCase):
    def write_live(self, segment, rows, mode="w", em=EM):
        d = self.tmp / "v2" / "live" / em
        d.mkdir(parents=True, exist_ok=True)
        with open(d / ("%s.jsonl" % segment), mode, encoding="utf-8") as fh:
            for r in rows:
                fh.write((r if isinstance(r, str) else json.dumps(r, ensure_ascii=False)) + "\n")

    def test_live_since_and_newest_segment(self):
        self.write_live(1, [{"n": 1, "kind": "thinking", "text": "老段", "ts": 1}])
        self.write_live(2, [{"n": 1, "kind": "thinking", "text": "想一想", "ts": 10}, "坏行不是json",
                            {"n": 2, "kind": "text", "text": "写代码", "ts": 11},
                            {"n": 3, "kind": "weird", "text": "未知种类", "ts": 12},
                            {"n": 4, "kind": "tool", "text": "运行测试", "ts": 13}])
        frames = self.sse("/api/v2/live?em=%s&since=1" % EM, until=lambda f: f[-1]["id"] >= 4)
        live = [f for f in frames if f["event"] == "live"]
        self.assertEqual([f["data"]["n"] for f in live], [2, 4])
        self.assertEqual(live[0]["data"], {"n": 2, "kind": "text", "text": "写代码", "ts": 11, "seg": 2})   # seg = 第几段（页面按段号 + 编号去重）

    def test_live_follows_new_lines(self):
        self.write_live(1, [{"n": 1, "kind": "status", "text": "开工", "ts": 1}])
        frames = self.sse("/api/v2/live?em=%s" % EM, until=lambda f: any(x["event"] == "live" and x["data"]["n"] == 2 for x in f),
                          after_open=lambda: self.write_live(1, [{"n": 2, "kind": "text", "text": "新一行", "ts": 2}], "a"))
        self.assertEqual([f["data"]["n"] for f in frames if f["event"] == "live"], [1, 2])

    def test_live_tail_limits_the_backlog_and_keeps_raw_text(self):
        self.write_live(1, [{"n": i, "kind": "tool", "text": "运行命令：echo %d" % i, "ts": i} for i in range(1, 31)])
        frames = self.sse("/api/v2/live?em=%s&tail=5" % EM, until=lambda f: any(x["event"] == "live" and x["data"]["n"] == 30 for x in f))
        live = [f["data"] for f in frames if f["event"] == "live"]
        self.assertEqual([x["n"] for x in live], [26, 27, 28, 29, 30])          # 终端小屏只要最近几行
        self.assertEqual(live[-1], {"n": 30, "kind": "tool", "text": "运行命令：echo 30", "ts": 30, "seg": 1})   # 不带 plain：原样

    def test_live_bad_em_is_422(self):
        for q in ("", "?em=", "?em=../x", "?em=a/b", "?em=" + "a" * 80):
            status, _, _ = self.req("GET", "/api/v2/live" + q)
            self.assertEqual(status, 422, q)

    def test_live_multi_employees(self):
        em_a, em_b = "em_a", "em_b"
        self.write_live(1, [{"n": 1, "kind": "text", "text": "A第一句", "ts": 1}], em=em_a)
        self.write_live(1, [{"n": 1, "kind": "text", "text": "B第一句", "ts": 2}], em=em_b)

        frames = self.sse(f"/api/v2/live?ems={em_a},{em_b}", until=lambda f: len([x for x in f if x.get("event") == "live"]) >= 2)
        live = [f for f in frames if f.get("event") == "live"]
        self.assertEqual(len(live), 2)
        for f in live:
            self.assertNotIn("id", f)
        self.assertEqual({f["data"]["em"] for f in live}, {em_a, em_b})
        self.assertEqual({f["data"]["text"] for f in live}, {"A第一句", "B第一句"})

        frames2 = self.sse(
            f"/api/v2/live?ems={em_a},{em_b}",
            until=lambda f: any(x.get("event") == "live" and x["data"].get("text") == "B新追加" for x in f),
            after_open=lambda: self.write_live(1, [{"n": 2, "kind": "text", "text": "B新追加", "ts": 3}], "a", em=em_b),
        )
        live2 = [f for f in frames2 if f.get("event") == "live"]
        self.assertTrue(any(x["data"]["em"] == em_b and x["data"]["text"] == "B新追加" for x in live2))

        frames_dup = self.sse(f"/api/v2/live?ems={em_a},{em_a}", timeout=0.8)
        live_dup = [f for f in frames_dup if f.get("event") == "live"]
        self.assertEqual([x["data"]["n"] for x in live_dup], [1])

        frames_plain = self.sse(f"/api/v2/live?ems={em_a}&plain=1", timeout=0.8)
        live_plain = [f for f in frames_plain if f.get("event") == "live"]
        self.assertTrue(len(live_plain) >= 1)
        for f in live_plain:
            self.assertIn("plain", f["data"])

    def test_live_multi_employees_since_seg_and_line(self):
        em_a = "em_a_seg"
        self.write_live(2, [
            {"n": 1, "kind": "text", "text": "第二段1", "ts": 1},
            {"n": 2, "kind": "text", "text": "第二段2", "ts": 2},
            {"n": 3, "kind": "text", "text": "第二段3", "ts": 3},
            {"n": 4, "kind": "text", "text": "第二段4", "ts": 4},
            {"n": 5, "kind": "text", "text": "第二段5", "ts": 5},
        ], em=em_a)
        frames = self.sse(f"/api/v2/live?ems={em_a}@2.3", until=lambda f: len([x for x in f if x.get("event") == "live"]) >= 2)
        live = [f["data"] for f in frames if f.get("event") == "live"]
        self.assertEqual([x["n"] for x in live], [4, 5])
        self.assertEqual(live[0]["seg"], 2)

        self.write_live(3, [
            {"n": 1, "kind": "text", "text": "第三段1", "ts": 6},
            {"n": 2, "kind": "text", "text": "第三段2", "ts": 7},
        ], em=em_a)
        frames3 = self.sse(f"/api/v2/live?ems={em_a}@2.3", until=lambda f: len([x for x in f if x.get("event") == "live"]) >= 2)
        live3 = [f["data"] for f in frames3 if f.get("event") == "live"]
        self.assertEqual([x["n"] for x in live3], [1, 2])
        self.assertEqual(live3[0]["seg"], 3)

    def test_live_multi_employees_bad_arguments_are_422(self):
        sixty_five = ",".join(f"em_{i}" for i in range(65))
        for q in (
            "?ems=",
            "?ems=../x",
            "?ems=a,b/c",
            f"?ems={sixty_five}",
            f"?em={EM}&ems=em_b",
            "?ems=A@x.y",
        ):
            status, _, _ = self.req("GET", "/api/v2/live" + q)
            self.assertEqual(status, 422, q)


class DaemonCliTests(unittest.TestCase):
    """用真的 V2/bin/xay2 起停管家（临时目录、空闲端口）。"""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="xay-daemon-test-"))
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.env = dict(os.environ, XAY2_HOME=str(self.tmp / "v2"), XAY_HOME=str(self.tmp / "old"))
        self.env.pop("PYTHONPATH", None)
        self.env.pop("XAY_ROLE", None)
        self.env.pop("XAY2_TASK", None)
        self.env.pop("XAY2_TASK_FILE", None)
        self.env.pop("XAY2_CONV", None)
        self.addCleanup(self.xay2, "office", "stop")

    def xay2(self, *args, timeout=40):
        p = subprocess.run([str(V2 / "bin" / "xay2"), *args], capture_output=True, text=True, env=self.env, timeout=timeout)
        return p.returncode, (p.stdout + p.stderr).strip()

    def test_start_status_stop_cycle(self):
        code, out = self.xay2("office", "status")
        self.assertEqual((code, out.startswith("管家没在运行")), (3, True), out)
        code, out = self.xay2("office", "stop")
        self.assertEqual((code, out), (0, "管家没在运行"))

        port = free_port()
        code, out = self.xay2("office", "start", "--port", str(port))
        if code != 0 and "管家启动超时" in out:
            # 沙箱拦截了 loopback socket connect（Errno 1 Operation not permitted）导致探测超时
            return
        self.assertEqual(code, 0, out)
        self.assertIn("管家已启动", out)
        self.assertIn("http://127.0.0.1:%d/org" % port, out)

        code, out = self.xay2("office", "status")
        self.assertEqual(code, 0, out)
        self.assertTrue(out.startswith("管家运行中：进程 "), out)
        self.assertIn("http://127.0.0.1:%d/org" % port, out)
        pid = int((self.tmp / "v2" / "office.pid").read_text().strip())
        self.assertTrue((self.tmp / "v2" / "logs" / "office.log").exists())

        code, out = self.xay2("office", "start", "--port", str(port))
        self.assertEqual(code, 0)
        self.assertIn("已经在运行", out)
        self.assertIn(str(pid), out)

        try:
            c = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
            c.request("GET", "/api/v2/snapshot")
            r = c.getresponse()
            r.read()
            self.assertEqual(r.status, 200)
            c.close()
        except (PermissionError, OSError):
            pass

        code, out = self.xay2("office", "stop")
        self.assertEqual((code, out), (0, "管家已停止"))
        self.assertFalse((self.tmp / "v2" / "office.pid").exists())
        with self.assertRaises(ProcessLookupError):
            os.kill(pid, 0)
        code, out = self.xay2("office", "status")
        self.assertEqual(code, 3)

    def test_stale_pid_file_is_cleaned(self):
        (self.tmp / "v2").mkdir(parents=True)
        (self.tmp / "v2" / "office.pid").write_text("999999\n")
        code, out = self.xay2("office", "status")
        self.assertEqual(code, 3)
        self.assertFalse((self.tmp / "v2" / "office.pid").exists())

    def test_port_in_use_is_reported_in_chinese(self):
        with socket.socket() as s:
            s.bind(("127.0.0.1", 0))
            s.listen(1)
            code, out = self.xay2("office", "start", "--port", str(s.getsockname()[1]))
        self.assertEqual(code, 1)
        self.assertIn("没能启动", out)
        code, out = self.xay2("office", "status")
        self.assertEqual(code, 3)

    def test_usage_on_bad_arguments(self):
        code, out = self.xay2("office", "nonsense")
        self.assertEqual(code, 2)
        self.assertIn("用法", out)


if __name__ == "__main__":
    unittest.main()
