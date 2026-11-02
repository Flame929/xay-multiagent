"""xay_merge3：三方合并要么全部写入，要么一个字节都不改。"""
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "xay_merge3.py"


def git(cwd, *args):
    return subprocess.run(["git", "-C", str(cwd), *args], capture_output=True, text=True, check=True).stdout.strip()


class Merge3Test(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="xay-merge3-")
        root = Path(self.tmp.name)
        self.repo, self.wt = root / "repo", root / "wt"
        self.repo.mkdir()
        git(self.repo, "init", "-q")
        (self.repo / "a.txt").write_text("".join("line %d\n" % i for i in range(1, 21)), encoding="utf-8")
        (self.repo / "b.txt").write_text("b1\nb2\n", encoding="utf-8")
        git(self.repo, "add", "-A")
        git(self.repo, "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-q", "-m", "base")
        self.base = git(self.repo, "rev-parse", "HEAD")
        git(self.repo, "worktree", "add", "-q", "-b", "w", str(self.wt), self.base)

    def tearDown(self):
        self.tmp.cleanup()

    def run_merge(self):
        git(self.wt, "add", "-A")
        proc = subprocess.run([sys.executable, str(SCRIPT), str(self.repo), str(self.wt), self.base], capture_output=True, text=True)
        return proc.returncode, json.loads(proc.stdout)

    def test_non_overlapping_edits_merge(self):
        a = (self.repo / "a.txt").read_text(encoding="utf-8").replace("line 2\n", "line 2 ours\n")
        (self.repo / "a.txt").write_text(a, encoding="utf-8")
        w = (self.wt / "a.txt").read_text(encoding="utf-8").replace("line 18\n", "line 18 theirs\n")
        (self.wt / "a.txt").write_text(w, encoding="utf-8")
        (self.wt / "new.txt").write_text("new\n", encoding="utf-8")
        (self.wt / "b.txt").unlink()
        rc, out = self.run_merge()
        self.assertEqual(rc, 0, out)
        merged = (self.repo / "a.txt").read_text(encoding="utf-8")
        self.assertIn("line 2 ours", merged)
        self.assertIn("line 18 theirs", merged)
        self.assertTrue((self.repo / "new.txt").exists())
        self.assertFalse((self.repo / "b.txt").exists())

    def test_conflict_changes_nothing(self):
        (self.repo / "a.txt").write_text((self.repo / "a.txt").read_text(encoding="utf-8").replace("line 5\n", "ours 5\n"), encoding="utf-8")
        (self.wt / "a.txt").write_text((self.wt / "a.txt").read_text(encoding="utf-8").replace("line 5\n", "theirs 5\n"), encoding="utf-8")
        (self.wt / "clean.txt").write_text("x\n", encoding="utf-8")
        before = (self.repo / "a.txt").read_text(encoding="utf-8")
        rc, out = self.run_merge()
        self.assertEqual(rc, 1)
        self.assertEqual(out["conflicts"], ["a.txt"])
        self.assertEqual((self.repo / "a.txt").read_text(encoding="utf-8"), before)
        self.assertFalse((self.repo / "clean.txt").exists(), "有冲突时其他文件也不能写入")


if __name__ == "__main__":
    unittest.main()
