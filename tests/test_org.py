import json
import os
import subprocess
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
XAY = ROOT / "scripts" / "xay"


class OrgTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.env = {**os.environ, "XAY_HOME": self.tmp.name, "XAY_LEAD_SESSION": "test-lead", "XAY_NOTIFY": "0"}

    def tearDown(self):
        self.tmp.cleanup()

    def call(self, *args, env=None):
        return subprocess.run([str(XAY), *args], env=env or self.env, text=True, capture_output=True, check=False)

    def test_org_flow_and_executor_allowlist(self):
        self.assertEqual(self.call("dept", "create", "fe", "--name", "前端部", "--mission", "页面", "--owns", "web/**").returncode, 0)
        ask_env = {**self.env, "XAY_DEPT": "fe", "XAY_WORKER": "小A"}
        asked = self.call("ask", "be", "需要接口", env=ask_env)
        message_id = json.loads(asked.stdout)["id"]
        answer_env = {**self.env, "XAY_DEPT": "be", "XAY_WORKER": "小B"}
        self.assertEqual(self.call("answer", message_id, "accept", "按 v1", env=answer_env).returncode, 0)
        import sys
        from unittest.mock import patch
        sys.path.insert(0, str(ROOT / "scripts"))
        import xay_org
        with patch.dict(os.environ, ask_env):
            self.assertEqual(xay_org.wait_for(message_id, 0, "test-lead")["type"], "decision")
        self.assertEqual(self.call("escalate", "-", "请总经理裁决", env=ask_env).returncode, 0)
        executor = {**self.env, "XAY_ROLE": "executor", "XAY_DEPT": "fe"}
        self.assertEqual(self.call("ask", "gm", "可执行", env=executor).returncode, 0)
        self.assertNotEqual(self.call("answer", message_id, "accept", "越权", env=executor).returncode, 0)

    def test_projection_shape(self):
        self.call("dept", "create", "qa", "--name", "测试部", "--mission", "验收")
        import sys
        from unittest.mock import patch
        sys.path.insert(0, str(ROOT / "scripts"))
        import xay_org
        with patch.dict(os.environ, self.env):
            data = xay_org.projection(session="test-lead")
        for key in ("conversation", "gm", "departments", "unassigned", "messages", "contracts", "plan"):
            self.assertIn(key, data)
        self.assertEqual(data["departments"][0]["counts"]["members"], 0)

    def test_xay_run_dept_registers_worker_and_prompt(self):
        project = Path(self.tmp.name) / "project"
        project.mkdir()
        self.call("dept", "create", "fe", "--name", "前端部", "--mission", "页面交付", "--owns", "web/**")
        env = {**self.env, "XAY_ISOLATE": "off", "XAY_TEST_CMD": "printf '%s\\n' \"$XAY_DEPT:$XAY_WORKER\""}
        result = subprocess.run([str(ROOT / "scripts" / "xay-run"), "codex", "--as", "小A", "--dept", "fe", "-C", str(project), "实现页面"],
                                env=env, text=True, capture_output=True, timeout=20)
        self.assertEqual(result.returncode, 0, result.stderr)
        dept = json.loads((Path(self.tmp.name) / "org" / "test-lead" / "org.json").read_text())["departments"][0]
        self.assertIn("小A", dept["members"])
        run_dir = next((project / ".xay" / "runs").iterdir())
        prompt = (run_dir / "prompt.md").read_text()
        self.assertIn("需要别的部门配合", prompt)
        self.assertIn("前端部：交付页面交付", prompt)
        self.assertEqual(json.loads((Path(self.tmp.name) / "registry.jsonl").read_text().splitlines()[-1])["dept"], "fe")


if __name__ == "__main__":
    unittest.main()
