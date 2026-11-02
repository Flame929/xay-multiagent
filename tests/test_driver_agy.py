import importlib.util
import io
import json
import os
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


stream = load("agy_stream_under_test", ROOT / "scripts/xay_drivers/agy_stream.py")
oneshot = load("agy_oneshot_under_test", ROOT / "scripts/xay_drivers/agy_oneshot.py")


class FakeProcess:
    def __init__(self, output):
        self.stdin = io.StringIO()
        self.stdout = io.StringIO(output)
        self.stderr = io.StringIO()
        self.returncode = None
        self.signals = []

    def poll(self):
        return self.returncode

    def wait(self, timeout=None):
        self.returncode = 0
        return 0

    def terminate(self):
        self.signals.append("TERM")
        self.returncode = 0

    def kill(self):
        self.signals.append("KILL")
        self.returncode = -9

    def send_signal(self, sig):
        self.signals.append(sig)


class AgyDriverTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.home = Path(self.tmp.name) / "xay"
        os.environ["XAY_HOME"] = str(self.home)
        self.procs = []

    def tearDown(self):
        os.environ.pop("XAY_HOME", None)
        self.tmp.cleanup()

    def popen(self, argv, **kwargs):
        output = (
            '{"event":"init","conversation_id":"conv-1"}\n'
            '{"event":"result","result":{"conversation_id":"conv-1","status":"SUCCESS"}}\n'
        )
        proc = FakeProcess(output)
        self.procs.append((argv, kwargs, proc))
        return proc

    def test_stream_uses_message_payload_and_sandbox(self):
        driver = stream.AgyStreamDriver(command="agy-test", popen=self.popen)
        handle = driver.start(Path("."), {"model": "gemini-test"})
        driver.prompt(handle, "只读检查")
        sent = json.loads(self.procs[0][2].stdin.getvalue())
        events = list(driver.events(handle))
        self.assertEqual(events[-1]["event"], "result")
        self.assertEqual(sent["event"], "user")
        self.assertEqual(sent["message"], {"role": "user", "content": "只读检查"})
        self.assertNotIn("user", sent)
        self.assertIn("--sandbox", self.procs[0][0])
        self.assertIn("--input-format", self.procs[0][0])

    def test_rework_keeps_stdin_until_explicit_close_and_resume_uses_conversation(self):
        driver = stream.AgyStreamDriver(command="agy-test", popen=self.popen)
        handle = driver.start(Path("."), {"kind": "rework"})
        list(driver.events(handle))
        self.assertFalse(handle.process.stdin.closed)
        driver.close(handle)
        resumed = driver.resume("conv-killed", Path("."))
        self.assertIn("--conversation", self.procs[-1][0])
        self.assertEqual(self.procs[-1][0][self.procs[-1][0].index("--conversation") + 1], "conv-killed")
        self.assertIn("--sandbox", self.procs[-1][0])
        self.assertEqual(resumed.native_session_id, "conv-killed")

    def test_daily_cap_is_written_and_enforced(self):
        driver = stream.AgyStreamDriver(command="agy-test", daily_cap=1, popen=self.popen)
        driver.start(Path("."), {})
        row = json.loads((self.home / "registry.jsonl").read_text().splitlines()[0])
        self.assertEqual(row["kind"], "agy_call")
        self.assertEqual(row["used"], 1)
        with self.assertRaises(stream.DailyCapError):
            driver.resume("conv", Path("."))

    def test_oneshot_starts_only_when_prompt_is_sent(self):
        driver = oneshot.AgyOneshotDriver(command="agy-test", popen=self.popen)
        handle = driver.start(Path("."), {})
        self.assertEqual(self.procs, [])
        driver.prompt(handle, "一次只读")
        self.assertEqual(list(driver.events(handle))[-1]["event"], "result")
        self.assertIn("--output-format", self.procs[0][0])
        self.assertIn("--sandbox", self.procs[0][0])


if __name__ == "__main__":
    unittest.main()
