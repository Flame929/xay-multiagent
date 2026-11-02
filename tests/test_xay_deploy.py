"""M5：发布脚本也跑 V2 的测试、把 V2/ 一起发到安装副本。

用临时的"开发仓库"和"安装副本"跑真脚本（不带 --restart-board，不碰本机的看板和管家）。
"""
import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
GIT_ENV = {"GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@example.com", "GIT_COMMITTER_NAME": "t",
           "GIT_COMMITTER_EMAIL": "t@example.com", "GIT_CONFIG_GLOBAL": os.devnull, "GIT_CONFIG_NOSYSTEM": "1"}
PASS = "import unittest\nclass T(unittest.TestCase):\n    def test_ok(self):\n        pass\n"
FAIL = "import unittest\nclass T(unittest.TestCase):\n    def test_bad(self):\n        self.fail('x')\n"


class DeployTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="xay-deploy-test-"))
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.env = {**os.environ, **GIT_ENV, "XAY_NOTIFY": "0"}
        self.env.pop("XAY_V1", None)
        self.src, self.dst = self.tmp / "src", self.tmp / "dst"
        (self.src / "scripts").mkdir(parents=True)
        shutil.copy2(ROOT / "scripts" / "xay-deploy", self.src / "scripts" / "xay-deploy")
        (self.src / "V2" / "bin").mkdir(parents=True)
        stub = self.src / "V2" / "bin" / "xay2"
        stub.write_text("#!/bin/sh\necho xay2 \"$@\"\n", encoding="utf-8")
        stub.chmod(0o755)
        for d in ("tests", "V2/tests"):
            (self.src / d).mkdir(parents=True, exist_ok=True)
            (self.src / d / "test_ok.py").write_text(PASS, encoding="utf-8")
        self.git(self.src, "init", "-q", "-b", "main")
        self.commit(self.src, "第一版")
        subprocess.run(["git", "clone", "-q", str(self.src), str(self.dst)], check=True, env=self.env, capture_output=True)

    def git(self, repo, *args):
        return subprocess.run(["git", "-C", str(repo), *args], check=True, env=self.env, capture_output=True, text=True)

    def commit(self, repo, msg):
        self.git(repo, "add", "-A")
        self.git(repo, "commit", "-q", "-m", msg)

    def deploy(self, *args):
        return subprocess.run([str(self.src / "scripts" / "xay-deploy"), *args], env={**self.env, "XAY_INSTALL_DIR": str(self.dst)},
                              capture_output=True, text=True, timeout=120)

    def test_publishes_v2_together(self):
        (self.src / "V2" / "web").mkdir()
        (self.src / "V2" / "web" / "board.html").write_text("<title>xay 看板</title>", encoding="utf-8")
        self.commit(self.src, "加看板")
        out = self.deploy()
        self.assertEqual(out.returncode, 0, out.stderr + out.stdout)
        self.assertIn("跑测试中（V2）", out.stdout)
        self.assertIn("已发布", out.stdout)
        self.assertTrue((self.dst / "V2" / "web" / "board.html").exists())
        self.assertTrue(os.access(self.dst / "V2" / "bin" / "xay2", os.X_OK))

    def test_failing_v2_tests_block_the_release(self):
        (self.src / "V2" / "tests" / "test_bad.py").write_text(FAIL, encoding="utf-8")
        self.commit(self.src, "坏测试")
        before = self.git(self.dst, "rev-parse", "HEAD").stdout
        out = self.deploy()
        self.assertEqual(out.returncode, 2)
        self.assertIn("V2 的测试没通过", out.stderr)
        self.assertEqual(self.git(self.dst, "rev-parse", "HEAD").stdout, before)

    def test_missing_xay2_is_reported(self):
        self.git(self.src, "rm", "-q", "V2/bin/xay2")
        self.commit(self.src, "去掉 xay2")
        out = self.deploy("--skip-tests")
        self.assertEqual(out.returncode, 2)
        self.assertIn("没有 V2/bin/xay2", out.stderr)

    def test_help_mentions_v2_board(self):
        out = self.deploy("--help")
        self.assertEqual(out.returncode, 0)
        self.assertIn("V2 管家", out.stdout)
        self.assertIn("XAY_V1=1", out.stdout)


if __name__ == "__main__":
    unittest.main()
