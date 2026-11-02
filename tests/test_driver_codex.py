import json
import os
import signal
import stat
import tempfile
import textwrap
import time
import unittest
from pathlib import Path

from scripts.xay_drivers.base import config_args, resolve_provider, resolve_sandbox
from scripts.xay_drivers.codex_appserver import CodexAppServerDriver
from scripts.xay_drivers.codex_exec import CodexExecDriver


class DriverConfigTest(unittest.TestCase):
    def test_safety_and_provider_are_explicit(self):
        card = {"mode": "write", "source": "auto", "effort": "low"}
        args = config_args(card, {"safety_tier": "standard", "confidential": False})
        joined = " ".join(args)
        self.assertIn('model_provider="custom"', joined)
        self.assertIn('sandbox_mode="workspace-write"', joined)
        self.assertIn("approval_policy=\"never\"", joined)
        self.assertIn("mcp_servers.lingxing.enabled=false", joined)
        self.assertIn("mcp_servers.node_repl.enabled=false", joined)

        confidential = config_args(card, {"safety_tier": "standard", "confidential": True})
        self.assertNotIn("model_provider=\"custom\"", " ".join(confidential))
        self.assertEqual(resolve_sandbox({"mode": "readonly"}, "standard"), "read-only")
        self.assertEqual(resolve_sandbox({"mode": "write"}, "open"), "danger-full-access")
        self.assertEqual(resolve_provider(card, {"safety_tier": "strict"}), "openai")


def _terminate_and_kill(proc, timeout=2.0):
    if proc is None:
        return
    pid = proc.pid if hasattr(proc, "pid") else proc
    if not isinstance(pid, int):
        return
    try:
        pgid = os.getpgid(pid)
    except (ProcessLookupError, OSError):
        pgid = None

    def _sig(s):
        if pgid is not None and pgid != os.getpgrp():
            try:
                os.killpg(pgid, s)
            except (ProcessLookupError, OSError):
                pass
        try:
            os.kill(pid, s)
        except (ProcessLookupError, OSError):
            pass

    if hasattr(proc, "poll") and proc.poll() is not None:
        return

    _sig(signal.SIGTERM)
    deadline = time.time() + timeout
    while time.time() < deadline:
        if hasattr(proc, "poll") and proc.poll() is not None:
            return
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return
        except OSError:
            break
        time.sleep(0.05)
    _sig(signal.SIGKILL)
    if hasattr(proc, "wait"):
        try:
            proc.wait(timeout=1.0)
        except Exception:
            pass


class AppServerProtocolTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="xay-driver-test-")
        self.root = Path(self.tmp.name)
        self.argv_file = self.root / "argv.json"
        self.fake = self.root / "fake-codex"
        self.handles = []
        self.fake.write_text(
            textwrap.dedent(
                """
                #!/usr/bin/env python3
                import json, os, sys
                if sys.argv[1:] == ['--version']:
                    print('codex-cli 0.165.0')
                    raise SystemExit
                if len(sys.argv) > 2 and sys.argv[1:3] == ['app-server', 'generate-json-schema']:
                    out = sys.argv[sys.argv.index('--out') + 1]
                    os.makedirs(out, exist_ok=True)
                    names = ['initialize', 'thread/start', 'thread/resume', 'turn/start', 'turn/steer', 'turn/interrupt', 'turn/completed', 'item/started', 'item/completed', 'thread/tokenUsage/updated']
                    with open(os.path.join(out, 'schema.json'), 'w') as f:
                        json.dump(names, f)
                    raise SystemExit
                open(os.environ['XAY_ARGV_FILE'], 'w').write(json.dumps({'argv': sys.argv[1:], 'codex_home': os.environ.get('CODEX_HOME')}))
                for line in sys.stdin:
                    if os.getppid() == 1:
                        break
                    msg = json.loads(line)
                    if 'id' not in msg:
                        continue
                    method = msg.get('method')
                    result = {}
                    if method == 'model/list': result = {'data': [{'id': 'gpt-test-default', 'isDefault': True}]}
                    if method == 'thread/start': result = {'thread': {'id': 'thread-test'}}
                    elif method == 'thread/resume': result = {'thread': {'id': msg['params']['threadId']}}
                    elif method == 'turn/start': result = {'turn': {'id': 'turn-test'}}
                    elif method == 'turn/steer': result = {'turnId': 'turn-test'}
                    print(json.dumps({'id': msg['id'], 'result': result}), flush=True)
                """
            ).lstrip(),
            encoding="utf-8",
        )
        self.fake.chmod(self.fake.stat().st_mode | stat.S_IXUSR)

    def tearDown(self):
        try:
            for driver, handle in self.handles:
                try:
                    driver.close(handle)
                except Exception:
                    pass
                proc = getattr(handle, "process", None)
                if proc is not None:
                    _terminate_and_kill(proc, timeout=2.0)
        finally:
            self.tmp.cleanup()

    def test_start_prompt_and_steer(self):
        os.environ["XAY_ARGV_FILE"] = str(self.argv_file)
        driver = CodexAppServerDriver(command=str(self.fake), xay_home=str(self.root / "xayhome"))
        handle = driver.start(self.root, {"mode": "write", "source": "sub", "effort": "low"}, {"safety_tier": "standard"})
        self.handles.append((driver, handle))
        self.assertEqual(handle.native_session_id, "thread-test")
        self.assertEqual(driver.prompt(handle, "回复 PONG"), "turn-test")
        self.assertTrue(driver.steer(handle, "改为回复 STEERED", "turn-test"))
        captured = json.loads(self.argv_file.read_text(encoding="utf-8"))
        argv = captured["argv"]
        self.assertIn("sandbox_mode=\"workspace-write\"", " ".join(argv))
        self.assertIn("model_provider=\"openai\"", " ".join(argv))
        self.assertIn("mcp_servers.xay.enabled=false", " ".join(argv))
        self.assertEqual(captured["codex_home"], str(driver.codex_home))
        driver.close(handle)

    def test_explicit_model_is_shared_by_exec_and_app_server(self):
        args = config_args({"source": "relay", "model": "gpt-test"}, {"safety_tier": "standard"})
        self.assertIn('model="gpt-test"', " ".join(args))


class ExecDriverTest(unittest.TestCase):
    def test_steer_is_not_claimed(self):
        driver = CodexExecDriver(command="codex")
        handle = driver.start(".", {}, {"safety_tier": "standard"})
        self.assertFalse(driver.steer(handle, "继续", None))
        self.assertEqual(driver.capabilities.interrupt, "kill")


if __name__ == "__main__":
    unittest.main()

