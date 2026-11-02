"""xay 命令行的会话、等待和名额来源验收。"""

import json
import os
import subprocess
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
XAY = ROOT / "scripts" / "xay"


class XayCliTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="xay-cli-")
        self.home = Path(self.tmp.name)
        self.env = {**os.environ, "XAY_HOME": str(self.home), "XAY_NOTIFY": "0"}

    def tearDown(self):
        self.tmp.cleanup()

    def call_xay(self, *args, env=None):
        return subprocess.run([str(XAY), *args], cwd=self.home, env=env or self.env,
                              text=True, capture_output=True, timeout=10)

    def test_session_begin_records_explicit_identity(self):
        result = self.call_xay("session", "begin", "--lead", "codex", "--title", "验收")
        self.assertEqual(result.returncode, 0, result.stderr)
        session_id = result.stdout.strip()
        data = json.loads((self.home / "sessions" / (session_id + ".json")).read_text())
        self.assertEqual(data["lead_kind"], "codex")
        self.assertEqual(data["lead_key"], "explicit:" + session_id)
        self.assertEqual(data["title"], "验收")

    def test_wait_timeout_uses_contract_text(self):
        result = self.call_xay("wait", "--session", "ls_missing", "--timeout", "0")
        self.assertEqual(result.returncode, 0)
        self.assertEqual(result.stdout.strip(), "无新信号，继续等")

    def test_doctor_is_slot_defaults_source(self):
        env = {**self.env, "XAY_SLOTS_CODEX": "7", "XAY_SLOTS_DSH": "5", "XAY_SLOTS_AGY": "1"}
        result = self.call_xay("doctor", env=env)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout)["slots"], {"codex": 7, "dsh": 5, "agy": 1})


if __name__ == "__main__":
    unittest.main()
