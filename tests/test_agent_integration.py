"""WP8：假 app-server 与 xay-agent 集成测试。

两部分：
1. FakeAppServerTest：只测 tests/fake_appserver.py 的线协议与时序，
   不需要 WP1 的 xay_agent.py，现在就能跑。
2. AgentIntegrationTest：按 arch-v4 §3.5/§5 契约启动 scripts/xay_agent.py，
   让一个任务托管假 app-server，覆盖崩溃隔离、steer 时序、interrupt。

agent 的启动约定（WP1 实现若不同，可用环境变量覆盖，见 _agent_argv）：
    python3 scripts/xay_agent.py --run-dir <origin>/.xay/runs/<run>
agent 从 run_dir/start.json 与 run_dir/prompt.md 读取任务；驱动通过
XAY_CODEX_BIN 指向本测试的假服务（同时把假服务软链成 PATH 里的 codex）。

所有临时目录都用 mktemp 语义的 TemporaryDirectory，不碰真实 ~/.xay。
"""

import json
import os
import queue
import shlex
import signal
import socket
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path

TESTS_DIR = Path(__file__).resolve().parent
PROJECT_ROOT = TESTS_DIR.parent
FAKE = TESTS_DIR / "fake_appserver.py"
AGENT = PROJECT_ROOT / "scripts" / "xay_agent.py"

STEER_TEXT = "改成只回复 STEERED"


# ---------------------------------------------------------------------------
# 小工具
# ---------------------------------------------------------------------------

def read_jsonl(path):
    """读可能正在写的 JSONL；残缺的最后一行直接忽略。"""
    path = Path(path)
    if not path.is_file():
        return []
    rows = []
    try:
        with path.open(encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    rows.append(json.loads(line))
                except ValueError:
                    continue
    except OSError:
        return []
    return rows


def wait_until(predicate, timeout=10.0, interval=0.05, message="等待超时"):
    deadline = time.time() + timeout
    last = None
    while time.time() < deadline:
        try:
            last = predicate()
        except (OSError, ValueError):
            last = None
        if last:
            return last
        time.sleep(interval)
    raise AssertionError("%s（最后结果：%r）" % (message, last))


def trace_rows(path):
    return read_jsonl(path)


def trace_requests(path, method):
    return [
        row for row in trace_rows(path)
        if row.get("dir") == "recv"
        and (row.get("payload") or {}).get("method") == method
    ]


def trace_sends(path, method):
    return [
        row for row in trace_rows(path)
        if row.get("dir") == "send"
        and (row.get("payload") or {}).get("method") == method
    ]


def turn_id_from_trace(path):
    """从假服务返回的 turn/start 响应里取 native turn id。"""
    for row in trace_rows(path):
        if row.get("dir") != "send":
            continue
        payload = row.get("payload") or {}
        result = payload.get("result")
        if isinstance(result, dict):
            turn = result.get("turn")
            if isinstance(turn, dict) and turn.get("id"):
                return turn["id"]
    return None


def meta_status(meta):
    for key in ("status", "state", "task_status", "execution_state"):
        value = meta.get(key)
        if isinstance(value, str):
            return value
    return ""


def is_orphaned(meta, decisions, events):
    if meta.get("orphaned") is True:
        return True
    if meta_status(meta) == "orphaned":
        return True
    if meta.get("fail_reason") == "orphaned":
        return True
    if any(row.get("kind") == "orphaned" for row in decisions):
        return True
    for event in events:
        if event.get("type") == "decision.created":
            if (event.get("data") or {}).get("kind") == "orphaned":
                return True
        if "orphaned" in json.dumps(event.get("data") or {}, ensure_ascii=False):
            return True
    return False


def turn_status(event):
    data = event.get("data") if isinstance(event.get("data"), dict) else {}
    if event.get("type") != "turn.completed":
        return ""
    for holder in (data, event, data.get("turn") or {}):
        if isinstance(holder, dict) and isinstance(holder.get("status"), str):
            return holder["status"]
    return ""


def close_streams(proc):
    for name in ("stdout", "stderr"):
        stream = getattr(proc, name, None)
        if stream is not None:
            try:
                stream.close()
            except (OSError, ValueError):
                pass


def tail(path, limit=40):
    path = Path(path)
    try:
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return ""
    return "\n".join(lines[-limit:])


class JsonRpcClient(object):
    """stdio 上的 NDJSON JSON-RPC 客户端，读线程与请求线程分离。"""

    def __init__(self, proc):
        self.proc = proc
        self.lock = threading.RLock()
        self.next_id = 1
        self.messages = []
        self.queue = queue.Queue()
        self.reader = threading.Thread(target=self._read_loop, daemon=True)
        self.reader.start()

    def _read_loop(self):
        try:
            for line in self.proc.stdout:
                line = line.strip()
                if not line:
                    continue
                try:
                    message = json.loads(line)
                except ValueError:
                    continue
                self.queue.put(message)
        finally:
            self.queue.put(None)

    def send(self, message):
        line = json.dumps(message, ensure_ascii=False) + "\n"
        with self.lock:
            self.proc.stdin.write(line)
            self.proc.stdin.flush()

    def request(self, method, params=None, timeout=8.0):
        with self.lock:
            request_id = self.next_id
            self.next_id += 1
        message = {"id": request_id, "method": method,
                   "params": params if params is not None else {}}
        self.send(message)
        deadline = time.time() + timeout
        while True:
            remaining = deadline - time.time()
            if remaining <= 0:
                raise AssertionError("JSON-RPC 请求超时：%s" % method)
            try:
                item = self.queue.get(timeout=remaining)
            except queue.Empty:
                raise AssertionError("JSON-RPC 请求超时：%s" % method)
            if item is None:
                raise AssertionError("假 app-server 提前退出（等待 %s 响应）"
                                     % method)
            with self.lock:
                self.messages.append(item)
            if item.get("id") == request_id and (
                    "result" in item or "error" in item):
                return item

    def wait_for(self, predicate, timeout=8.0, message="等待通知超时"):
        deadline = time.time() + timeout
        while True:
            with self.lock:
                for item in self.messages:
                    if predicate(item):
                        return item
            remaining = deadline - time.time()
            if remaining <= 0:
                raise AssertionError(message)
            try:
                item = self.queue.get(timeout=remaining)
            except queue.Empty:
                raise AssertionError(message)
            if item is None:
                raise AssertionError(message + "（假 app-server 已退出）")
            with self.lock:
                self.messages.append(item)

    def close(self):
        try:
            self.proc.stdin.close()
        except OSError:
            pass


class FakeServerMixin(object):
    def launch(self, scenario=None, latency=0.0):
        scenario_path = Path(self.tmp.name) / ("scenario-%d.json" % time.time_ns())
        if scenario is not None:
            scenario_path.write_text(
                json.dumps(scenario, ensure_ascii=False), encoding="utf-8")
        trace_path = Path(self.tmp.name) / ("trace-%d.ndjson" % time.time_ns())
        env = os.environ.copy()
        env["PYTHONUNBUFFERED"] = "1"
        env["XAY_FAKE_TRACE"] = str(trace_path)
        env["XAY_FAKE_LATENCY"] = str(latency)
        env["XAY_FAKE_TURN_SECONDS"] = "0.2"
        if scenario is not None:
            env["XAY_FAKE_SCENARIO"] = str(scenario_path)
        else:
            env.pop("XAY_FAKE_SCENARIO", None)
        proc = subprocess.Popen(
            [sys.executable, str(FAKE), "app-server", "--listen", "stdio://"],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE,
            stderr=subprocess.PIPE, text=True, bufsize=1, env=env,
        )
        client = JsonRpcClient(proc)
        self._fake_procs = getattr(self, "_fake_procs", [])
        self._fake_procs.append(proc)
        return client, trace_path, proc

    def stop_fake(self, client, proc):
        client.close()
        try:
            proc.wait(timeout=3)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait(timeout=5)
        close_streams(proc)


# ---------------------------------------------------------------------------
# 1. 假服务自身的协议测试（现在即可运行）
# ---------------------------------------------------------------------------

class FakeAppServerTest(FakeServerMixin, unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="xay-fake-")

    def tearDown(self):
        for proc in getattr(self, "_fake_procs", []):
            if proc.poll() is None:
                proc.kill()
                try:
                    proc.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    pass
            close_streams(proc)
        self.tmp.cleanup()

    def _init_thread(self, client, cwd=None):
        init = client.request("initialize", {
            "clientInfo": {"name": "xay-test", "version": "0.0.0"},
            "capabilities": {"experimentalApi": False},
        })
        self.assertIn("result", init)
        started = client.request("thread/start", {
            "cwd": str(cwd or self.tmp.name),
            "sandbox": "workspace-write",
            "approvalPolicy": "never",
            "ephemeral": False,
        })
        self.assertIn("result", started)
        thread_id = started["result"]["thread"]["id"]
        client.wait_for(
            lambda m: m.get("method") == "thread/started"
            and m["params"]["thread"]["id"] == thread_id,
            message="没有收到 thread/started")
        return thread_id

    def test_handshake_thread_and_completed_turn(self):
        client, trace, proc = self.launch()
        try:
            thread_id = self._init_thread(client)
            turn = client.request("turn/start", {
                "threadId": thread_id,
                "input": [{"type": "text", "text": "回复 PONG"}],
            })
            self.assertIn("result", turn)
            turn_id = turn["result"]["turn"]["id"]
            client.wait_for(
                lambda m: m.get("method") == "turn/started"
                and m["params"]["turn"]["id"] == turn_id,
                message="没有收到 turn/started")
            completed = client.wait_for(
                lambda m: m.get("method") == "turn/completed"
                and m["params"]["turn"]["id"] == turn_id,
                message="没有收到 turn/completed")
            self.assertEqual(completed["params"]["turn"]["status"], "completed")
            items = [
                m["params"]["item"] for m in client.messages
                if m.get("method") == "item/completed"
                and m["params"]["turnId"] == turn_id
            ]
            self.assertTrue(
                any(i.get("type") == "userMessage"
                    and i["content"][0]["text"] == "回复 PONG" for i in items),
                "缺少最初 userMessage：%r" % items)
            self.assertTrue(
                any(i.get("type") == "agentMessage"
                    and i.get("text") == "FAKE-DONE" for i in items),
                "缺少 agentMessage：%r" % items)
            self.assertTrue(trace_requests(trace, "turn/start"))
            self.assertTrue(trace_sends(trace, "turn/completed"))
        finally:
            self.stop_fake(client, proc)

    def test_steer_user_message_comes_after_request_in_active_turn(self):
        scenario = {
            "steer_delay": 0.05,
            "turns": [{
                "final_text": "STEERED",
                "steps": [
                    {"type": "command", "command": "sleep 30",
                     "output": "running", "delay": 0},
                    {"type": "wait", "delay": 1.5},
                    {"type": "message", "text": "STEERED", "final": True},
                ],
            }],
        }
        client, trace, proc = self.launch(scenario)
        try:
            thread_id = self._init_thread(client)
            turn = client.request("turn/start", {
                "threadId": thread_id,
                "input": [{"type": "text", "text": "先跑命令"}],
            })
            turn_id = turn["result"]["turn"]["id"]
            wait_until(
                lambda: any(
                    row.get("dir") == "send"
                    and (row.get("payload") or {}).get("method") == "item/started"
                    and (row["payload"]["params"]["item"].get("type")
                         == "commandExecution")
                    for row in trace_rows(trace)),
                timeout=5, message="没有等到 commandExecution inProgress")

            bad = client.request("turn/steer", {
                "threadId": thread_id,
                "expectedTurnId": "turn_not_active",
                "input": [{"type": "text", "text": "错的"}],
            })
            self.assertIn("error", bad)

            steered = client.request("turn/steer", {
                "threadId": thread_id,
                "expectedTurnId": turn_id,
                "input": [{"type": "text", "text": STEER_TEXT}],
            })
            self.assertEqual(steered["result"]["turnId"], turn_id)

            user_message = client.wait_for(
                lambda m: m.get("method") == "item/completed"
                and m["params"]["turnId"] == turn_id
                and m["params"]["item"].get("type") == "userMessage"
                and m["params"]["item"]["content"][0]["text"] == STEER_TEXT,
                timeout=5, message="steer 后没有 userMessage")
            self.assertEqual(user_message["params"]["turnId"], turn_id)

            completed = client.wait_for(
                lambda m: m.get("method") == "turn/completed"
                and m["params"]["turn"]["id"] == turn_id,
                timeout=6, message="steer 后回合没有完成")
            self.assertEqual(completed["params"]["turn"]["status"], "completed")

            rows = trace_rows(trace)
            recv_steer = next(
                i for i, row in enumerate(rows)
                if row.get("dir") == "recv"
                and (row.get("payload") or {}).get("method") == "turn/steer"
                and (row["payload"].get("params") or {}).get("expectedTurnId")
                == turn_id)
            send_user = next(
                i for i, row in enumerate(rows)
                if row.get("dir") == "send"
                and (row.get("payload") or {}).get("method") == "item/completed"
                and (row["payload"]["params"]["item"].get("type")
                     == "userMessage")
                and (row["payload"]["params"]["item"]["content"][0]["text"]
                     == STEER_TEXT))
            send_done = next(
                i for i, row in enumerate(rows)
                if row.get("dir") == "send"
                and (row.get("payload") or {}).get("method") == "turn/completed"
                and (row["payload"]["params"]["turn"]["id"] == turn_id))
            self.assertLess(recv_steer, send_user,
                            "userMessage 不能早于 turn/steer 请求")
            self.assertLess(send_user, send_done,
                            "userMessage 必须落在当前活动回合内")
            self.assertEqual(
                len(trace_requests(trace, "turn/start")), 1,
                "steer 不应新建回合")
        finally:
            self.stop_fake(client, proc)

    def test_interrupt_completes_interrupted_and_allows_new_turn(self):
        scenario = {
            "turns": [{
                "steps": [
                    {"type": "command", "command": "sleep 60",
                     "output": "running", "delay": 0},
                    {"type": "wait", "delay": 8.0},
                ],
            }],
        }
        client, trace, proc = self.launch(scenario)
        try:
            thread_id = self._init_thread(client)
            turn = client.request("turn/start", {
                "threadId": thread_id,
                "input": [{"type": "text", "text": "跑很久"}],
            })
            turn_id = turn["result"]["turn"]["id"]
            wait_until(
                lambda: any(
                    row.get("dir") == "send"
                    and (row.get("payload") or {}).get("method") == "item/started"
                    and (row["payload"]["params"]["item"].get("type")
                         == "commandExecution")
                    for row in trace_rows(trace)),
                timeout=5, message="没有等到 commandExecution")

            began = time.time()
            response = client.request("turn/interrupt", {
                "threadId": thread_id, "turnId": turn_id})
            self.assertIn("result", response)
            completed = client.wait_for(
                lambda m: m.get("method") == "turn/completed"
                and m["params"]["turn"]["id"] == turn_id,
                timeout=2.0, message="interrupt 后没有 turn/completed")
            elapsed = time.time() - began
            self.assertEqual(completed["params"]["turn"]["status"], "interrupted")
            self.assertLess(elapsed, 2.0, "interrupt 超过 2 秒")

            # 中断后同一 thread 还能开新回合（上下文保留的协议前提）。
            resumed = client.request("turn/start", {
                "threadId": thread_id,
                "input": [{"type": "text", "text": "回复 RESUMED"}],
            })
            self.assertIn("result", resumed)
            new_turn_id = resumed["result"]["turn"]["id"]
            new_done = client.wait_for(
                lambda m: m.get("method") == "turn/completed"
                and m["params"]["turn"]["id"] == new_turn_id,
                timeout=5, message="interrupt 后新回合没有完成")
            self.assertEqual(new_done["params"]["turn"]["status"], "completed")
            self.assertEqual(len(trace_requests(trace, "turn/start")), 2)
        finally:
            self.stop_fake(client, proc)

    def test_thread_resume_survives_server_restart(self):
        client, trace, proc = self.launch()
        thread_id = None
        try:
            thread_id = self._init_thread(client)
            self.stop_fake(client, proc)
            self._fake_procs.remove(proc)

            client2, trace2, proc2 = self.launch()
            try:
                init = client2.request("initialize", {
                    "clientInfo": {"name": "xay-test", "version": "0.0.0"}})
                self.assertIn("result", init)
                resumed = client2.request("thread/resume", {
                    "threadId": thread_id})
                self.assertEqual(
                    resumed["result"]["thread"]["id"], thread_id)
                self.assertTrue(trace_requests(trace2, "thread/resume"))
            finally:
                self.stop_fake(client2, proc2)
        finally:
            if proc.poll() is None:
                self.stop_fake(client, proc)

    def test_crash_kills_process_without_completion(self):
        scenario = {
            "crash": {"on": "turn_start", "turn_index": 0,
                      "delay": 0.2, "exit_code": 9},
            "turns": [{"steps": [{"type": "wait", "delay": 10}]}],
        }
        client, trace, proc = self.launch(scenario)
        try:
            thread_id = self._init_thread(client)
            client.send({"id": 99, "method": "turn/start", "params": {
                "threadId": thread_id,
                "input": [{"type": "text", "text": "会崩"}],
            }})
            proc.wait(timeout=5)
            self.assertEqual(proc.returncode, 9)
            self.assertTrue(trace_requests(trace, "turn/start"))
            self.assertFalse(trace_sends(trace, "turn/completed"),
                             "崩溃前不应有 turn/completed")
        finally:
            if proc.poll() is None:
                proc.kill()
                proc.wait(timeout=5)
            close_streams(proc)

    def test_configurable_latency(self):
        client, trace, proc = self.launch(latency=0.2)
        try:
            began = time.time()
            client.request("initialize", {
                "clientInfo": {"name": "xay-test", "version": "0.0.0"}})
            elapsed = time.time() - began
            self.assertGreaterEqual(elapsed, 0.15,
                                    "配置的响应延迟没有生效")
        finally:
            self.stop_fake(client, proc)

    def test_version_and_schema_probe(self):
        version = subprocess.run(
            [sys.executable, str(FAKE), "--version"],
            capture_output=True, text=True, timeout=10)
        self.assertEqual(version.returncode, 0)
        self.assertIn("codex-cli", version.stdout)

        with tempfile.TemporaryDirectory(prefix="xay-fake-schema-") as out:
            proc = subprocess.run(
                [sys.executable, str(FAKE), "app-server",
                 "generate-json-schema", "--out", out],
                capture_output=True, text=True, timeout=10)
            self.assertEqual(proc.returncode, 0, proc.stderr)
            schema = json.loads(
                (Path(out) / "ClientRequest.json").read_text(encoding="utf-8"))
            text = json.dumps(schema)
            for method in ("initialize", "thread/start", "thread/resume",
                           "turn/start", "turn/steer", "turn/interrupt"):
                self.assertIn(method, text)
            notifications = json.loads(
                (Path(out) / "ServerNotification.json").read_text(
                    encoding="utf-8"))
            text = json.dumps(notifications)
            for method in ("turn/completed", "item/started",
                           "item/completed", "thread/tokenUsage/updated"):
                self.assertIn(method, text)


# ---------------------------------------------------------------------------
# 2. xay-agent 集成测试（WP1 落地后才运行）
# ---------------------------------------------------------------------------

AGENT_AVAILABLE = AGENT.is_file() or bool(os.environ.get("XAY_AGENT_CMD"))


@unittest.skipUnless(AGENT_AVAILABLE,
                     "scripts/xay_agent.py 尚未实现（WP1）；可用 XAY_AGENT_CMD 指向替身")
class AgentIntegrationTest(unittest.TestCase):
    def setUp(self):
        # /var/folders 下的临时路径 + 长 run id 会超过 AF_UNIX 的 104 字节上限。
        self.tmp = tempfile.TemporaryDirectory(prefix="xay-a-", dir="/tmp")
        self.root = Path(self.tmp.name)
        self.home = self.root / "xayhome"
        (self.home / "ctl").mkdir(parents=True)
        self.bindir = self.root / "bin"
        self.bindir.mkdir()
        try:
            os.chmod(str(FAKE), 0o755)
        except OSError:
            pass
        os.symlink(str(FAKE), str(self.bindir / "codex"))
        self.tasks = []
        self._seq = 0

    def tearDown(self):
        for task in self.tasks:
            proc = task["proc"]
            if proc.poll() is None:
                try:
                    os.killpg(proc.pid, signal.SIGKILL)
                except OSError:
                    proc.kill()
                try:
                    proc.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    pass
        self.tmp.cleanup()

    # -- 任务与 agent 启动 ---------------------------------------------------

    def _make_task(self, name, scenario):
        self._seq += 1
        base = self.root / name
        origin = base / "proj"
        origin.mkdir(parents=True)
        subprocess.run(["git", "-C", str(origin), "init", "-q"], check=True)
        (origin / "tracked.txt").write_text("base\n", encoding="utf-8")
        subprocess.run(["git", "-C", str(origin), "add", "-A"], check=True)
        subprocess.run(
            ["git", "-C", str(origin), "-c", "user.name=xay",
             "-c", "user.email=xay@localhost", "commit", "-q", "-m", "init"],
            check=True)

        run_id = "r%04d" % self._seq
        run_dir = origin / ".xay" / "runs" / run_id
        run_dir.mkdir(parents=True)
        prompt = "假任务：完成后只回复 FAKE-DONE。\n"
        (run_dir / "prompt.md").write_text(prompt, encoding="utf-8")
        start = {
            "run": run_id,
            "agent": "codex",
            "name": name,
            "title": "假任务 " + name,
            "effort": "low",
            "model": "gpt-5-codex",
            "readonly": False,
            "started": int(time.time()),
            "timeout": 30,
            "origin": str(origin),
            "workdir": str(origin),
            "run_dir": str(run_dir),
            "dag": "",
            "task": "",
            "pid": os.getpid(),
            "isolated": False,
            "base_commit": "",
            "resume": "",
            "worktree": "",
            "branch": "",
            "driver": "codex-app-server",
            "safety_tier": "standard",
            "source": "sub",
            "sandbox": "workspace-write",
            "net": False,
        }
        (run_dir / "start.json").write_text(
            json.dumps(start, ensure_ascii=False), encoding="utf-8")
        scenario_path = base / "scenario.json"
        scenario_path.write_text(
            json.dumps(scenario, ensure_ascii=False), encoding="utf-8")
        task = {
            "name": name,
            "base": base,
            "origin": origin,
            "run_id": run_id,
            "run_dir": run_dir,
            "scenario": scenario_path,
            "trace": base / "fake-trace.ndjson",
            "stdout": base / "agent.out",
            "stderr": base / "agent.err",
            "sock": self.home / "ctl" / (run_id + ".sock"),
            "proc": None,
        }
        self.tasks.append(task)
        return task

    def _agent_argv(self, task):
        override = os.environ.get("XAY_AGENT_CMD")
        values = {
            "agent": str(AGENT),
            "python": sys.executable,
            "run_dir": str(task["run_dir"]),
            "workdir": str(task["origin"]),
            "prompt_file": str(task["run_dir"] / "prompt.md"),
            "start_json": str(task["run_dir"] / "start.json"),
            "fake": str(FAKE),
        }
        if override:
            return [part.format(**values) for part in shlex.split(override)]
        return [sys.executable, str(AGENT), "--run-dir", str(task["run_dir"])]

    def _start_task(self, task):
        env = os.environ.copy()
        for key in list(env):
            if key.startswith("XAY_SLOTS_"):
                env.pop(key, None)
        env["XAY_HOME"] = str(self.home)
        env["XAY_NOTIFY"] = "0"
        env["XAY_CODEX_BIN"] = str(self.bindir / "codex")
        env["XAY_APP_SERVER_BIN"] = str(self.bindir / "codex")
        env["XAY_CODEX_SOURCE"] = "sub"
        env["XAY_SLOTS_CODEX"] = "4"
        env["XAY_FAKE_SCENARIO"] = str(task["scenario"])
        env["XAY_FAKE_TRACE"] = str(task["trace"])
        env["XAY_FAKE_LATENCY"] = "0.01"
        env["XAY_FAKE_TURN_SECONDS"] = "0.3"
        env["XAY_RUN_DIR"] = str(task["run_dir"])
        env["XAY_RUN"] = task["run_id"]
        env["XAY_AGENT"] = "codex"
        env["XAY_DRIVER"] = "codex-app-server"
        env["PATH"] = str(self.bindir) + os.pathsep + env.get("PATH", "")
        env.pop("XAY_ROLE", None)
        task["stdout"].write_text("", encoding="utf-8")
        task["stderr"].write_text("", encoding="utf-8")
        with task["stdout"].open("w", encoding="utf-8") as out, \
                task["stderr"].open("w", encoding="utf-8") as err:
            proc = subprocess.Popen(
                self._agent_argv(task),
                cwd=str(task["origin"]), env=env,
                stdin=subprocess.DEVNULL, stdout=out, stderr=err,
                start_new_session=True,
            )
        task["proc"] = proc
        return proc

    def _wait_ready(self, task, timeout=20.0):
        """等 ctl socket + 假服务收到 initialize，并检查 agent 没有早退。"""
        deadline = time.time() + timeout
        while time.time() < deadline:
            proc = task["proc"]
            if proc.poll() is not None:
                raise AssertionError(
                    "xay_agent.py 提前退出（exit=%s）。\n"
                    "测试约定：python3 scripts/xay_agent.py --run-dir ...，"
                    "或用 XAY_AGENT_CMD 覆盖。\nstdout:\n%s\nstderr:\n%s"
                    % (proc.returncode, tail(task["stdout"]),
                       tail(task["stderr"])))
            if task["sock"].exists() and trace_requests(
                    task["trace"], "initialize"):
                return
            time.sleep(0.05)
        raise AssertionError(
            "等待 agent/假 app-server 就绪超时（socket=%s，trace=%s）。\n"
            "若驱动没走 XAY_CODEX_BIN/PATH 的 codex，请让 WP1 驱动支持该注入点。\n"
            "stderr:\n%s"
            % (task["sock"], task["trace"], tail(task["stderr"])))

    def _ctl(self, task, payload, timeout=5.0):
        wait_until(lambda: task["sock"].exists(), timeout=10,
                   message="控制 socket 未出现：%s" % task["sock"])
        frame = (json.dumps(payload, ensure_ascii=False) + "\n").encode("utf-8")
        deadline = time.time() + timeout
        last_error = None
        while time.time() < deadline:
            client = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            client.settimeout(max(0.5, deadline - time.time()))
            try:
                client.connect(str(task["sock"]))
                client.sendall(frame)
                data = b""
                while b"\n" not in data:
                    chunk = client.recv(65536)
                    if not chunk:
                        break
                    data += chunk
                if b"\n" in data:
                    return json.loads(data.split(b"\n", 1)[0].decode("utf-8"))
                last_error = "socket 未返回完整一行：%r" % data
            except (OSError, ValueError) as exc:
                last_error = str(exc)
            finally:
                client.close()
            time.sleep(0.05)
        raise AssertionError("控制请求失败：%s（%s）" % (payload, last_error))

    def _events(self, task):
        return read_jsonl(task["run_dir"] / "events.norm.jsonl")

    def _wait_event(self, task, predicate, timeout=20.0, message=None):
        return wait_until(
            lambda: next((e for e in self._events(task) if predicate(e)), None),
            timeout=timeout,
            message=message or "没有等到归一事件（%s）\nstderr:\n%s"
            % (task["name"], tail(task["stderr"])))

    def _wait_meta(self, task, predicate, timeout=20.0):
        def read():
            path = task["run_dir"] / "meta.json"
            if not path.is_file():
                return None
            try:
                meta = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                return None
            return meta if predicate(meta) else None
        return wait_until(read, timeout=timeout,
                          message="没有等到 meta.json（%s）\nstderr:\n%s"
                          % (task["name"], tail(task["stderr"])))

    def _wait_exit(self, task, timeout=20.0):
        try:
            task["proc"].wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            raise AssertionError(
                "agent 没有退出（%s）\nstderr:\n%s"
                % (task["name"], tail(task["stderr"])))
        return task["proc"].returncode

    # -- 崩溃 -> orphaned，其他任务不受影响 ---------------------------------

    def test_crash_orphans_only_that_task(self):
        survivor = self._make_task("survivor", {
            "turns": [{
                "final_text": "SURVIVOR-DONE",
                "steps": [
                    {"type": "command", "command": "sleep 5",
                     "output": "still running", "delay": 0},
                    {"type": "wait", "delay": 3.0},
                    {"type": "message", "text": "SURVIVOR-DONE",
                     "final": True},
                ],
            }],
        })
        crashing = self._make_task("crashing", {
            "crash": {"on": "turn_start", "turn_index": 0,
                      "delay": 0.2, "exit_code": 9},
            "turns": [{"steps": [{"type": "wait", "delay": 10}]}],
        })
        self._start_task(survivor)
        self._start_task(crashing)
        self._wait_ready(survivor)
        self._wait_ready(crashing)
        wait_until(lambda: trace_requests(survivor["trace"], "turn/start"),
                   timeout=10, message="survivor 没有开始回合")
        wait_until(lambda: trace_requests(crashing["trace"], "turn/start"),
                   timeout=10, message="crashing 没有开始回合")

        self._wait_exit(crashing, timeout=10)
        self.assertNotEqual(crashing["proc"].returncode, 0,
                            "假 app-server 崩溃后 agent 不应正常退出")

        meta = self._wait_meta(crashing, lambda m: True, timeout=10)
        decisions = read_jsonl(crashing["run_dir"] / "decisions.jsonl")
        events = self._events(crashing)
        self.assertTrue(
            is_orphaned(meta, decisions, events),
            "崩溃后没有 orphaned 标记：%s"
            % json.dumps({"meta": meta, "decisions": decisions,
                          "events": events[-10:]}, ensure_ascii=False)[:2000])

        # 同一时刻另一个任务的 agent 与假服务必须还活着。
        self.assertIsNone(survivor["proc"].poll(),
                          "一个任务崩溃把另一个任务的 agent 带走了")
        self.assertFalse(trace_sends(survivor["trace"], "turn/completed"),
                         "survivor 不应提前结束")

        meta_b = self._wait_meta(survivor, lambda m: True, timeout=25)
        self.assertEqual(meta_status(meta_b), "done", meta_b)
        self.assertTrue(trace_sends(survivor["trace"], "turn/completed"),
                        "survivor 没有被影响，应正常收到 turn/completed")
        self._wait_exit(survivor, timeout=15)

    # -- steer -> 活动回合内出现 userMessage ---------------------------------

    def test_steer_timing_in_active_turn(self):
        task = self._make_task("steer", {
            "turn_start_notifications_before_response": True,
            "steer_delay": 0.05,
            "turns": [{
                "final_text": "STEERED",
                "steps": [
                    {"type": "reasoning", "text": "先想一下", "delay": 0.1},
                    {"type": "command", "command": "sleep 30",
                     "output": "running", "delay": 0},
                    {"type": "wait", "delay": 2.5},
                    {"type": "message", "text": "STEERED", "final": True},
                ],
            }],
        })
        self._start_task(task)
        self._wait_ready(task)
        wait_until(
            lambda: any(
                row.get("dir") == "send"
                and (row.get("payload") or {}).get("method") == "item/started"
                and (row["payload"]["params"]["item"].get("type")
                     == "commandExecution")
                for row in trace_rows(task["trace"])),
            timeout=10, message="没有等到当前回合的 commandExecution")
        turn_id = turn_id_from_trace(task["trace"])
        self.assertTrue(turn_id)

        response = self._ctl(task, {
            "op": "steer",
            "text": STEER_TEXT,
            "expected_turn": turn_id,
            "by": "lead:ls_test",
        })
        self.assertTrue(response.get("ok"), response)
        self.assertEqual(response.get("accepted_as"), "steer", response)
        self.assertIn(response.get("effective"), ("next_sample", None), response)

        user_message = wait_until(
            lambda: next((
                row for row in trace_rows(task["trace"])
                if row.get("dir") == "send"
                and (row.get("payload") or {}).get("method") == "item/completed"
                and (row["payload"]["params"]["item"].get("type")
                     == "userMessage")
                and (row["payload"]["params"]["item"]["content"][0]["text"]
                     == STEER_TEXT)), None),
            timeout=5, message="steer 后假服务没有收到 userMessage 注入")
        self.assertEqual(user_message["payload"]["params"]["turnId"], turn_id)
        self.assertEqual(
            len(trace_requests(task["trace"], "turn/start")), 1,
            "steer 不能新建回合")
        wait_until(
            lambda: trace_sends(task["trace"], "turn/completed"),
            timeout=8, message="steer 后回合没有完成")

        rows = trace_rows(task["trace"])
        recv_steer = next(
            i for i, row in enumerate(rows)
            if row.get("dir") == "recv"
            and (row.get("payload") or {}).get("method") == "turn/steer")
        send_user = next(
            i for i, row in enumerate(rows)
            if row.get("dir") == "send"
            and (row.get("payload") or {}).get("method") == "item/completed"
            and row["payload"]["params"]["item"].get("type") == "userMessage"
            and row["payload"]["params"]["item"]["content"][0]["text"]
            == STEER_TEXT)
        send_done = next(
            i for i, row in enumerate(rows)
            if row.get("dir") == "send"
            and (row.get("payload") or {}).get("method") == "turn/completed")
        self.assertLess(recv_steer, send_user)
        self.assertLess(send_user, send_done)

        # 归一事件：控制请求/回执必须出现，且在回合开始之后。
        events = self._events(task)
        controls = [
            e for e in events
            if e.get("type") in ("control.requested", "control.acked")
            and (e.get("data") or {}).get("action") == "steer"
        ]
        self.assertTrue(controls, "events.norm.jsonl 缺少 steer 控制事件：%r"
                        % events)
        self.assertTrue(any(e.get("type") == "turn.started" for e in events),
                        "events.norm.jsonl 缺少 turn.started")

        self._wait_meta(task, lambda m: "done" in json.dumps(
            m, ensure_ascii=False), timeout=20)
        self._wait_exit(task, timeout=5)

    # -- soft interrupt -> paused -> resume 同线程新回合 ---------------------

    def test_soft_interrupt_pauses_and_resume_starts_new_turn_same_thread(self):
        task = self._make_task("soft-resume", {
            "turn_start_notifications_before_response": True,
            "turns": [
                {"steps": [
                    {"type": "command", "command": "sleep 60",
                     "output": "running", "delay": 0},
                    {"type": "wait", "delay": 8.0},
                ]},
                {"final_text": "RESUMED", "steps": [
                    {"type": "message", "text": "RESUMED", "final": True},
                ]},
            ],
        })
        self._start_task(task)
        self._wait_ready(task)
        wait_until(
            lambda: any(
                row.get("dir") == "send"
                and (row.get("payload") or {}).get("method") == "item/started"
                and (row["payload"]["params"]["item"].get("type")
                     == "commandExecution")
                for row in trace_rows(task["trace"])),
            timeout=10, message="没有等到当前回合的 commandExecution")

        response = self._ctl(task, {"op": "interrupt", "by": "lead:ls_test"})
        self.assertTrue(response.get("ok"), response)
        self.assertEqual(response.get("accepted_as"), "interrupt", response)
        self._wait_event(
            task, lambda e: turn_status(e) == "interrupted", timeout=5.0,
            message="interrupt 后没有 turn.completed status=interrupted")
        wait_until(lambda: self._ctl(task, {"op": "status"}).get("state") == "paused",
                   timeout=5, message="soft interrupt 后状态不是 paused")
        self.assertTrue(task["sock"].exists(), "paused 期间控制 socket 必须活着")

        response = self._ctl(task, {
            "op": "resume", "text": "回复 RESUMED", "by": "lead:ls_test"})
        self.assertTrue(response.get("ok"), response)
        self.assertEqual(response.get("accepted_as"), "resume", response)

        self._wait_event(
            task,
            lambda e: e.get("type") == "message"
            and e.get("data", {}).get("final")
            and (e.get("text") or "").strip() == "RESUMED",
            timeout=15, message="resume 后没有收到新回合的最终回复")
        meta = self._wait_meta(task, lambda m: meta_status(m) == "done", timeout=20)
        self.assertEqual(meta.get("fail_reason", ""), "")
        self._wait_exit(task, timeout=5)

        requests = trace_requests(task["trace"], "turn/start")
        self.assertEqual(len(requests), 2, "resume 必须在同一 thread 上开第二个回合")
        thread_ids = {row["payload"]["params"].get("threadId") for row in requests}
        self.assertEqual(len(thread_ids), 1, "resume 不能换线程：%r" % thread_ids)
        self.assertEqual(trace_requests(task["trace"], "thread/resume"), [],
                         "软中断后应直接 turn/start，不需要 thread/resume")

    def test_interrupt_with_text_continues_immediately(self):
        task = self._make_task("interrupt-text", {
            "turns": [
                {"steps": [
                    {"type": "command", "command": "sleep 60",
                     "output": "running", "delay": 0},
                    {"type": "wait", "delay": 8.0},
                ]},
                {"final_text": "INTERRUPTED-CONTINUE", "steps": [
                    {"type": "message", "text": "INTERRUPTED-CONTINUE", "final": True},
                ]},
            ],
        })
        self._start_task(task)
        self._wait_ready(task)
        wait_until(
            lambda: any(
                row.get("dir") == "send"
                and (row.get("payload") or {}).get("method") == "item/started"
                and (row["payload"]["params"]["item"].get("type")
                     == "commandExecution")
                for row in trace_rows(task["trace"])),
            timeout=10, message="没有等到当前回合的 commandExecution")
        response = self._ctl(task, {
            "op": "interrupt", "text": "回复 INTERRUPTED-CONTINUE",
            "by": "lead:ls_test"})
        self.assertTrue(response.get("ok"), response)
        self._wait_event(
            task,
            lambda e: e.get("type") == "message"
            and e.get("data", {}).get("final")
            and (e.get("text") or "").strip() == "INTERRUPTED-CONTINUE",
            timeout=15, message="带文字的 interrupt 没有自动续接并完成")
        meta = self._wait_meta(task, lambda m: meta_status(m) == "done", timeout=20)
        self.assertTrue(meta.get("session_id"), "续接需保留原生会话 ID")
        self._wait_exit(task, timeout=15)
        requests = trace_requests(task["trace"], "turn/start")
        self.assertEqual(len(requests), 2)
        self.assertEqual(len({row["payload"]["params"].get("threadId")
                              for row in requests}), 1)

    def test_failed_turn_finalizes_and_preserves_session_id(self):
        task = self._make_task("failed-turn", {
            "turns": [{"status": "failed", "error": {"message": "test failure"},
                       "steps": [{"type": "complete", "status": "failed"}]}],
        })
        self._start_task(task)
        meta = self._wait_meta(task, lambda m: meta_status(m) == "failed", timeout=10)
        self.assertEqual(meta.get("exit"), 1)
        self.assertTrue(meta.get("session_id"), "失败后应保留原生会话 ID")
        self._wait_exit(task, timeout=5)

    # -- 额度用尽 -> 同进程换线路重试 ---------------------------------------

    def test_usage_limit_retries_other_provider_then_done(self):
        quota_error = {"codexErrorInfo": "usageLimitExceeded",
                       "message": "usage limit exceeded"}
        task = self._make_task("quota-fallback", {
            "by_provider": {
                "openai": {"turns": [{
                    "status": "failed", "error": quota_error,
                    "steps": [{"type": "message", "text": "额度用尽",
                               "final": True},
                              {"type": "complete", "status": "failed"}],
                }]},
                "custom": {"turns": [{
                    "final_text": "RECOVERED",
                    "steps": [{"type": "message", "text": "RECOVERED",
                               "final": True}],
                }]},
            },
        })
        self._start_task(task)
        self._wait_ready(task)
        self._wait_event(
            task,
            lambda e: e.get("type") == "message"
            and (e.get("text") or "").strip() == "RECOVERED",
            timeout=20, message="换线路后没有收到成功回复")
        meta = self._wait_meta(task, lambda m: meta_status(m) == "done", timeout=20)
        self.assertEqual(meta.get("fail_reason", ""), "", meta)
        self._wait_exit(task, timeout=15)

        providers = []
        for row in trace_requests(task["trace"], "thread/start"):
            params = (row.get("payload") or {}).get("params") or {}
            if params.get("modelProvider"):
                providers.append(params["modelProvider"])
        self.assertIn("openai", providers)
        self.assertIn("custom", providers, "没有切换到另一条线路")
        pids = {row.get("pid") for row in trace_rows(task["trace"])}
        self.assertGreaterEqual(len(pids), 2, "换线路必须在同一托管进程内重开 app-server")
        warning = [e for e in self._events(task)
                   if e.get("type") == "warning"
                   and (e.get("data") or {}).get("source") == "quota"]
        self.assertTrue(warning, "缺少额度降级的 warning 事件")

    def test_usage_limit_both_providers_fail_marks_quota_failed(self):
        quota_error = {"codexErrorInfo": "usageLimitExceeded",
                       "message": "usage limit exceeded"}
        failed_turn = {
            "status": "failed", "error": quota_error,
            "steps": [{"type": "message", "text": "额度用尽", "final": True},
                      {"type": "complete", "status": "failed"}],
        }
        task = self._make_task("quota-failed", {
            "by_provider": {
                "openai": {"turns": [failed_turn]},
                "custom": {"turns": [failed_turn]},
            },
        })
        self._start_task(task)
        self._wait_ready(task)
        meta = self._wait_meta(task, lambda m: meta_status(m) == "failed",
                               timeout=25)
        reason = str(meta.get("fail_reason") or "")
        self.assertIn("额度", reason)
        self.assertIn("usage_limit_exceeded", reason)
        self.assertNotEqual(self._wait_exit(task, timeout=15), 0)
        pids = {row.get("pid") for row in trace_rows(task["trace"])}
        self.assertGreaterEqual(len(pids), 2, "两条线路都应尝试过一次")

    # -- interrupt -> interrupted -------------------------------------------

    def test_interrupt_marks_turn_interrupted(self):
        task = self._make_task("interrupt", {
            "turns": [{
                "steps": [
                    {"type": "command", "command": "sleep 60",
                     "output": "running", "delay": 0},
                    {"type": "wait", "delay": 10.0},
                ],
            }],
        })
        self._start_task(task)
        self._wait_ready(task)
        wait_until(
            lambda: any(
                row.get("dir") == "send"
                and (row.get("payload") or {}).get("method") == "item/started"
                and (row["payload"]["params"]["item"].get("type")
                     == "commandExecution")
                for row in trace_rows(task["trace"])),
            timeout=10, message="没有等到当前回合的 commandExecution")
        turn_id = turn_id_from_trace(task["trace"])
        self.assertTrue(turn_id)

        began = time.time()
        response = self._ctl(task, {
            "op": "interrupt",
            "text": "别跑了，回复 STOPPED",
            "by": "lead:ls_test",
        })
        self.assertTrue(response.get("ok"), response)
        self.assertEqual(response.get("accepted_as"), "interrupt", response)

        completed = self._wait_event(
            task,
            lambda e: turn_status(e) == "interrupted",
            timeout=3.0,
            message="interrupt 后没有 turn.completed status=interrupted")
        self.assertEqual(turn_status(completed), "interrupted")
        self.assertLess(time.time() - began, 2.5, "interrupt 超过 2.5 秒")

        # 假服务侧也必须真的收到 turn/interrupt 并回 interrupted。
        self.assertTrue(trace_requests(task["trace"], "turn/interrupt"),
                        "agent 没有向 app-server 发 turn/interrupt")
        interrupted = [
            row for row in trace_sends(task["trace"], "turn/completed")
            if (row["payload"]["params"]["turn"].get("status")
                == "interrupted")
        ]
        self.assertTrue(interrupted, "假服务没有发出 interrupted 的 turn/completed")

        # meta 的 turns[] 若已写入，状态必须是 interrupted。
        try:
            meta = self._wait_meta(task, lambda m: True, timeout=15)
        except AssertionError:
            meta = None
        if meta and isinstance(meta.get("turns"), list) and meta["turns"]:
            self.assertIn(
                "interrupted",
                json.dumps(meta["turns"][0], ensure_ascii=False),
                "meta.turns[0] 不是 interrupted")


if __name__ == "__main__":
    unittest.main()
