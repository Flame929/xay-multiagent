"""WP6 看板服务的最小真实行为测试。"""

import importlib.util
import json
import os
import socket
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("xay_watch_v4", ROOT / "scripts" / "xay_watch.py")
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)


def write_json(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False), encoding="utf-8")


class WatchV4Test(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="xay-watch-v4-")
        self.home = Path(self.tmp.name) / "xay"
        self.origin = Path(self.tmp.name) / "project"
        self.run = self.origin / ".xay" / "runs" / "run-1"
        self.run.mkdir(parents=True)
        (self.home / "registry.jsonl").parent.mkdir(parents=True)
        (self.home / "registry.jsonl").write_text(json.dumps({
            "kind": "run", "run_dir": str(self.run), "agent": "codex", "ws": "ws_test",
            "session": "ls_test", "session_title": "测试会话", "xs": "xs_test",
            "phase": "ph_test", "task": "task_test", "title": "写代码", "time": time.time(),
        }, ensure_ascii=False) + "\n", encoding="utf-8")
        write_json(self.run / "start.json", {"agent": "codex", "started": int(time.time()) - 2, "pid": os.getpid(), "origin": str(self.origin), "session_id": "native"})
        write_json(self.run / "meta.json", {"status": "running", "started": int(time.time()) - 2, "origin": str(self.origin)})
        (self.run / "events.raw").write_text(json.dumps({"type": "thread.started", "thread_id": "native"}) + "\n", encoding="utf-8")
        self.watch = MODULE.WatchData(str(self.home))

    def tearDown(self):
        self.tmp.cleanup()

    def test_state_shape_and_incremental_tail(self):
        first = self.watch.state()
        self.assertEqual(first["workspaces"][0]["id"], "ws_test")
        self.assertEqual(first["workspaces"][0]["sessions"][0]["id"], "ls_test")
        self.assertIn("thresholds", first)
        self.assertIn("slots", first["agents"]["codex"])
        seq = first["seq"]
        with (self.home / "registry.jsonl").open("a", encoding="utf-8") as fh:
            fh.write(json.dumps({"kind": "run", "run_dir": str(self.run), "agent": "codex", "ws": "ws_test", "session": "ls_test", "xs": "xs_test", "phase": "ph_test", "task": "task_test", "title": "更新"}) + "\n")
        second = self.watch.state()
        self.assertGreaterEqual(second["seq"], seq)

    def test_sse_since_and_host_check(self):
        self.watch.state()
        old_data, old_html = MODULE.Handler.data, MODULE.Handler.html_path
        MODULE.Handler.data = self.watch
        MODULE.Handler.html_path = str(ROOT / "scripts" / "xay-watch.html")
        server = ThreadingHTTPServer(("127.0.0.1", 0), MODULE.Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            url = "http://127.0.0.1:%d/api/events?since=0" % server.server_port
            req = urllib.request.Request(url, headers={"Host": "127.0.0.1:%d" % server.server_port})
            with urllib.request.urlopen(req, timeout=3) as response:
                body = response.read().decode("utf-8")
            self.assertIn("event: execution.upsert", body)
            bad = urllib.request.Request(url, headers={"Host": "evil.test:7878"})
            with self.assertRaises(urllib.error.HTTPError) as caught:
                urllib.request.urlopen(bad, timeout=3)
            self.assertEqual(caught.exception.code, 403)
        finally:
            server.shutdown(); server.server_close(); thread.join(timeout=3)
            MODULE.Handler.data, MODULE.Handler.html_path = old_data, old_html

    def test_watch_control_uses_xay_ctl_client(self):
        sock_path = str(self.home / "ctl.sock")
        listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        listener.bind(sock_path)
        listener.listen(1)
        received = []

        def serve_one():
            conn, _ = listener.accept()
            with conn:
                received.append(json.loads(conn.recv(65536).decode("utf-8")))
                conn.sendall(b'{"ok":true,"accepted_as":"steer"}\n')

        worker = threading.Thread(target=serve_one)
        worker.start()
        old_data = MODULE.Handler.data

        class StubData:
            @staticmethod
            def _read_json(path, default):
                return {"ctl_sock": sock_path}

        MODULE.Handler.data = StubData()
        try:
            handler = object.__new__(MODULE.Handler)
            result = handler._ctl(str(self.run), {
                "op": "steer", "text": "继续", "expected_turn": "turn_1"
            })
            worker.join(timeout=3)
            self.assertEqual(result["accepted_as"], "steer")
            self.assertEqual(received[0]["op"], "steer")
            self.assertEqual(received[0]["by"], "user")
            self.assertEqual(received[0]["expected_turn"], "turn_1")
        finally:
            MODULE.Handler.data = old_data
            listener.close()


if __name__ == "__main__":
    unittest.main()
