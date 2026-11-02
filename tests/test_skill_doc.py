"""SKILL.md（M5 起用 xay2）：硬性约束都还在、篇幅没涨回去、里面写的 xay2 命令都是真有的。"""
import re
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SKILL = (ROOT / "SKILL.md").read_text(encoding="utf-8")


class SkillDocTests(unittest.TestCase):
    def test_hard_rules_are_kept(self):
        for text in ("gemini-3.8-*", "deepseek-v4.1-flash", "git commit", "git push", "密钥", "token", "cookie",
                     "Antigravity Tools", "不要删掉这部分功能", "不要用负责人自己的子 Agent", "XAY_V1=1",
                     "references/legacy-v1.md", "references/scorecard.md"):
            self.assertIn(text, SKILL, text)

    def test_sections_keep_their_order(self):
        heads = re.findall(r"^## (\d)\.", SKILL, flags=re.M)
        self.assertEqual(heads, list("012345678"))

    def test_short_enough_to_read_every_time(self):
        self.assertLess(len(SKILL.encode("utf-8")), 9813)          # M5 前是 19626 字节，要短一半以上

    def test_xay2_commands_in_the_doc_exist(self):
        sys.path.insert(0, str(ROOT / "V2"))
        try:
            from xay.cli import _parser
        finally:
            sys.path.pop(0)
        known = set(_parser()._subparsers._group_actions[0].choices) | {"office"}
        used = set(re.findall(r"xay2 ([a-z]+)", SKILL))
        self.assertTrue(used)
        self.assertEqual(used - known, set())

    def test_legacy_doc_has_the_old_flow(self):
        legacy = (ROOT / "references" / "legacy-v1.md").read_text(encoding="utf-8")
        for text in ("xay session begin", "xay-dag", "xay wait --session", "xay ctl", "XAY_V1=1", "board.off"):
            self.assertIn(text, legacy, text)


if __name__ == "__main__":
    unittest.main()
