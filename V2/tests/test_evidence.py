"""证据规则（protocol §4.3 v1.3 的表）测试。

表从上往下取第一条匹配的行。这里用"谓词列表 + 第一条匹配"独立再写一遍表，
把 3 × 4 × 2 × 2 = 48 种组合逐个和 verdict() 比对，并确认每种组合都至少落在一行上。
"""
import itertools
import subprocess
import sys
import unittest
from pathlib import Path

from xay.core import evidence as core_evidence
from xay.core.evidence import explain, verdict
from xay.desk import evidence as desk_evidence
from xay.desk.evidence import verdict_from_evidence

STATUSES = ("completed", "failed", "interrupted")
CLAIMS = ("done", "partial", "blocked", None)
DONE_LIKE = (None, "done")      # 表里的 "done / 无"
NOT_DONE = ("partial", "blocked", None)   # 表里的 "非 done"

# (行号, 谓词(status, claim, has, mode), 期望结论)；期望的 reason 为 None 的 failed 行只要求 kind。
ROWS = [
    (1, lambda s, c, h, m: s == "completed" and c in DONE_LIKE and h and m == "write", ("landed", None)),
    (2, lambda s, c, h, m: s == "completed" and h and m == "readonly", ("kept", "mismatch")),
    (3, lambda s, c, h, m: s == "completed" and c in DONE_LIKE and not h and m == "readonly", ("landed", None)),
    (4, lambda s, c, h, m: s == "completed" and c in DONE_LIKE and not h and m == "write", ("kept", "mismatch")),
    (5, lambda s, c, h, m: s == "completed" and c in ("partial", "blocked"), ("kept", "mismatch")),
    (6, lambda s, c, h, m: s in ("failed", "interrupted") and c == "done" and h, ("kept", "mismatch")),
    (7, lambda s, c, h, m: s in ("failed", "interrupted") and c == "done" and not h, ("kept", "mismatch")),
    (8, lambda s, c, h, m: s in ("failed", "interrupted") and c in NOT_DONE and h, ("kept", "failed_with_changes")),
    (9, lambda s, c, h, m: s == "failed" and c in NOT_DONE and not h, ("failed", None)),
    (10, lambda s, c, h, m: s == "interrupted" and c in NOT_DONE and not h, ("kept", "interrupted")),
]
ALL = list(itertools.product(STATUSES, CLAIMS, (True, False), ("readonly", "write")))


def first_match(combo):
    for no, pred, expect in ROWS:
        if pred(*combo):
            return no, expect
    return None, None


class VerdictTableTests(unittest.TestCase):
    def test_every_combination_matches_a_row_and_verdict_agrees(self):
        for combo in ALL:
            with self.subTest(combo=combo):
                row, expect = first_match(combo)
                self.assertIsNotNone(row, "表漏了这种组合：%r" % (combo,))
                got = verdict(*combo)
                if expect[0] == "failed":
                    self.assertEqual(got[0], "failed")
                    self.assertTrue(isinstance(got[1], str) and got[1], "failed 要带一句大白话原因")
                else:
                    self.assertEqual(got, expect, "第 %d 行" % row)

    def test_each_row_is_reachable(self):
        hit = {first_match(c)[0] for c in ALL}
        self.assertEqual(hit, {no for no, _, _ in ROWS}, "有的行被前面的行完全盖住了")

    def test_row_by_row_examples(self):
        # 第 1 行
        self.assertEqual(verdict("completed", "done", True, "write"), ("landed", None))
        self.assertEqual(verdict("completed", None, True, "write"), ("landed", None))
        # 第 2 行：只读任务却改了文件，不管怎么汇报
        for claim in ("done", "partial", "blocked", None):
            self.assertEqual(verdict("completed", claim, True, "readonly"), ("kept", "mismatch"), claim)
        # 第 3 行
        self.assertEqual(verdict("completed", "done", False, "readonly"), ("landed", None))
        self.assertEqual(verdict("completed", None, False, "readonly"), ("landed", None))
        # 第 4 行
        self.assertEqual(verdict("completed", "done", False, "write"), ("kept", "mismatch"))
        self.assertEqual(verdict("completed", None, False, "write"), ("kept", "mismatch"))
        # 第 5 行
        for has, mode in itertools.product((True, False), ("readonly", "write")):
            for claim in ("partial", "blocked"):
                self.assertEqual(verdict("completed", claim, has, mode), ("kept", "mismatch"), (claim, has, mode))
        # 第 6、7 行
        for st, has, mode in itertools.product(("failed", "interrupted"), (True, False), ("readonly", "write")):
            self.assertEqual(verdict(st, "done", has, mode), ("kept", "mismatch"), (st, has, mode))
        # 第 8 行
        for st, claim, mode in itertools.product(("failed", "interrupted"), NOT_DONE, ("readonly", "write")):
            self.assertEqual(verdict(st, claim, True, mode), ("kept", "failed_with_changes"), (st, claim, mode))
        # 第 9 行
        for claim, mode in itertools.product(NOT_DONE, ("readonly", "write")):
            kind, reason = verdict("failed", claim, False, mode)
            self.assertEqual(kind, "failed")
            self.assertTrue(reason)
        # 第 10 行
        for claim, mode in itertools.product(NOT_DONE, ("readonly", "write")):
            self.assertEqual(verdict("interrupted", claim, False, mode), ("kept", "interrupted"), (claim, mode))

    def test_empty_string_claim_is_no_claim(self):
        self.assertEqual(verdict("completed", "", True, "write"), ("landed", None))
        self.assertEqual(verdict("failed", "", False, "write")[0], "failed")

    def test_has_changes_is_truthiness(self):
        self.assertEqual(verdict("completed", "done", ["a.txt"], "write"), ("landed", None))
        self.assertEqual(verdict("completed", "done", [], "write"), ("kept", "mismatch"))

    def test_bad_inputs_raise(self):
        for bad in (("ok", "done", True, "write"), ("completed", "finished", True, "write"),
                    ("completed", "done", True, "rw"), (None, None, True, "write")):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                verdict(*bad)


class OneImplementationTests(unittest.TestCase):
    def test_desk_evidence_reexports_core(self):
        self.assertIs(desk_evidence.verdict, core_evidence.verdict)
        self.assertIs(desk_evidence.explain, core_evidence.explain)

    def test_core_does_not_import_desk(self):
        root = Path(__file__).resolve().parents[1]
        code = "import sys, xay.core.evidence; sys.exit(1 if any(m == 'xay.desk' or m.startswith('xay.desk.') for m in sys.modules) else 0)"
        r = subprocess.run([sys.executable, "-c", code], cwd=root, capture_output=True, text=True)
        self.assertEqual(r.returncode, 0, "core 不能 import desk：" + r.stderr)


class EvidenceDictTests(unittest.TestCase):
    def test_verdict_from_turn_ended_evidence(self):
        ev = {"cli_status": "completed", "claim": "done", "changed_files": ["a.py"],
              "diff_stat": {"files": 1, "add": 3, "del": 0}}
        self.assertEqual(verdict_from_evidence(ev, "write"), ("landed", None))
        self.assertEqual(verdict_from_evidence(ev, "readonly"), ("kept", "mismatch"))
        ev["changed_files"] = []
        self.assertEqual(verdict_from_evidence(ev, "write"), ("kept", "mismatch"))
        self.assertEqual(verdict_from_evidence(ev, "readonly"), ("landed", None))
        self.assertEqual(verdict_from_evidence({"cli_status": "failed", "error": "x", "changed_files": []}, "write")[0],
                         "failed")


class ExplainTests(unittest.TestCase):
    def test_protocol_quoted_sentences(self):
        self.assertEqual(explain("completed", "done", False, "write"), "说做完了但没有改动")
        # 1.33 起"需要你"说人话：句首照旧是协议里的那句，后面补上"你可以怎么办"
        self.assertTrue(explain("completed", "done", True, "readonly").startswith("只读任务却改了文件"))
        self.assertTrue(explain("completed", "partial", True, "readonly").startswith("只读任务却改了文件"), "第 2 行排在第 5 行前面")
        self.assertEqual(explain("failed", "done", False, "write"), "说做完了，但这一轮没正常结束，也没有改动")
        self.assertEqual(explain("interrupted", "done", False, "write"), "说做完了，但这一轮没正常结束，也没有改动")

    def test_landed_is_empty_and_everything_else_is_plain_chinese(self):
        for combo in ALL:
            text = explain(*combo)
            with self.subTest(combo=combo):
                if verdict(*combo)[0] == "landed":
                    self.assertEqual(text, "")
                else:
                    self.assertTrue(text and any(ord(c) > 127 for c in text), "要用大白话中文：%r" % text)


if __name__ == "__main__":
    unittest.main()
