"""EventParser 单元测试。

只依据 .xay/context.md 约定 4 和 tests/fixtures/ 下的真实事件样例。
实现脚本由别人编写，这里用 importlib 按路径加载，不做任何 import 路径假设。
"""

import importlib.util
import json
import sys
import unittest
from pathlib import Path

TESTS_DIR = Path(__file__).resolve().parent
PROJECT_ROOT = TESTS_DIR.parent
MODULE_PATH = PROJECT_ROOT / "scripts" / "xay_watch.py"
FIXTURES = TESTS_DIR / "fixtures"

EventParser = None


def setUpModule():
    global EventParser
    if not MODULE_PATH.is_file():
        raise AssertionError("缺少待测实现文件: %s" % MODULE_PATH)
    spec = importlib.util.spec_from_file_location("xay_watch_under_test", MODULE_PATH)
    if spec is None or spec.loader is None:
        raise AssertionError("无法为 %s 创建 import spec" % MODULE_PATH)
    module = importlib.util.module_from_spec(spec)
    # dataclasses 等标准库机制会按 __module__ 反查 sys.modules。
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    EventParser = module.EventParser


def _fixture_lines(agent):
    text = (FIXTURES / (agent + ".jsonl")).read_text(encoding="utf-8")
    return text.splitlines()


def parse_fixture(agent):
    parser = EventParser(agent)
    for line in _fixture_lines(agent):
        parser.feed(line)
    return parser.snapshot()


def actions_of(snapshot):
    return [(a["kind"], a["text"], a["state"]) for a in snapshot["actions"]]


# 以下期望值均由 tests/fixtures/ 的样例手工计算得出。

CODEX_SESSION = "01a181e6-f381-7910-9a8f-7536e2dcf3c9"
CODEX_FINAL = "结果：完成。\n改动文件：slug.py"
CODEX_TOKENS = 25705 + 512  # turn.completed.usage 的 input_tokens + output_tokens

DSH_SESSION = "session-e4b56672-2f98-447c-9979-8c0f62be1d74"
DSH_FINAL = (
    "结果：完成 — spec.md 规定的是 `slug.py` 中 `slugify(text, max_len=50)` 的行为："
    "转小写、保留英文数字与中文、其余连续字符并为一个 `-`、去首尾 `-`、超长截断并再去尾 `-`、"
    "空输入返回空串，并给出两条示例。\n\n改动文件：无\n\n"
    "验证：只读取了 .xay/context.md 与 spec.md，未运行命令（只读任务）\n\n"
    "问题与需要负责人决定的事：无"
)
DSH_SAY = (
    "结果：完成 — spec.md 规定的是 `slug.py` 中 `slugify(text, max_len=50)` 的行为："
    "转小写、保留英文数字与中文、其"
)
DSH_TOKENS = 21809  # 最后一个 step_end.usage.totalTokens

AGY_SESSION = "e885c499-e9db-489e-ad15-73d5dc71fc9a"
AGY_FINAL = (
    "[README.md](file:///private/tmp/claude-501/-Users-mac-Library-Application-Support-"
    "Claude-scratch-workspaces-e33993ad-4816-4146-b219-e95a0db5ee19-d0425c97-f9fd-485c-"
    "aa57-0bb93900d6a2-scratch-2026-10-28-27d4d3/0199c7ac-7eb4-4a7f-b429-1b3364cbe076/"
    "scratchpad/agyprobe/README.md#L1) 的第一行是：\n`# hello probe`\n\n好。\n"
)
AGY_SAY = "[README.md](file:///private/tmp/claude-501/-Users-mac-Library-Application-Suppor"
AGY_TOKENS = 11678 + 11950  # 各 DONE 步 usage.total_tokens 之和；result.usage 也是 23628


class CodexParserTest(unittest.TestCase):
    def test_snapshot(self):
        snap = parse_fixture("codex")

        self.assertEqual(snap["session"], CODEX_SESSION)
        self.assertEqual(snap["final"], CODEX_FINAL)
        self.assertEqual(snap["tokens"], CODEX_TOKENS)

        # 非 error item 共 7 个（item_1..item_7），每个只算一步；
        # item_0 是 error 配置警告，被忽略。
        self.assertEqual(snap["step"], 7)

        self.assertEqual(
            actions_of(snap),
            [
                ("think", "**Reading the spec**", "done"),
                ("cmd", "cat spec.md", "done"),
                ("plan", "计划 2 项", "active"),
                ("edit", "slug.py", "done"),
                ("cmd", "python3 -m unittest", "failed"),
                ("search", "python unicode slugify", "done"),
                ("say", CODEX_FINAL, "done"),
            ],
        )

    def test_error_item_ignored(self):
        snap = parse_fixture("codex")
        texts = [a["text"] for a in snap["actions"]]
        self.assertFalse(any("unrecognized configuration" in t for t in texts))
        self.assertNotIn("error", [a["kind"] for a in snap["actions"]])

    def test_started_then_completed_updates_in_place(self):
        snap = parse_fixture("codex")
        cat_cmds = [a for a in snap["actions"] if a["text"] == "cat spec.md"]
        self.assertEqual(len(cat_cmds), 1)
        self.assertEqual(cat_cmds[0]["state"], "done")

        test_cmds = [a for a in snap["actions"] if a["text"] == "python3 -m unittest"]
        self.assertEqual(len(test_cmds), 1)
        self.assertEqual(test_cmds[0]["state"], "failed")


class DshParserTest(unittest.TestCase):
    def test_snapshot(self):
        snap = parse_fixture("dsh")

        self.assertEqual(snap["session"], DSH_SESSION)
        self.assertEqual(snap["final"], DSH_FINAL)
        self.assertEqual(snap["tokens"], DSH_TOKENS)
        self.assertEqual(snap["step"], 3)  # 3 次 step_start

        self.assertEqual(
            actions_of(snap),
            [
                ("say", "我先读取项目上下文和 spec 文件。", "done"),
                ("cmd", "ls -la", "done"),
                ("read", ".xay/context.md", "done"),
                ("read", "spec.md", "done"),
                ("think", "One-line answer.", "done"),
                ("say", DSH_SAY, "done"),  # text 只取前 80 字
            ],
        )

    def test_callid_pairing_marks_failed(self):
        parser = EventParser("dsh")
        parser.feed(json.dumps({
            "type": "status", "phase": "step_start", "turn": 1, "step": 1,
        }))
        parser.feed(json.dumps({
            "type": "tool_call", "callId": "call_x", "tool": "bash",
            "input": {"command": "false"},
        }, ensure_ascii=False))
        self.assertEqual(
            actions_of(parser.snapshot()),
            [("cmd", "false", "active")],
        )

        parser.feed(json.dumps({
            "type": "tool_result", "callId": "call_x", "status": "failed",
        }))
        self.assertEqual(
            actions_of(parser.snapshot()),
            [("cmd", "false", "failed")],
        )

        # 未知 callId 不应报错，也不应产生新动作。
        parser.feed(json.dumps({
            "type": "tool_result", "callId": "call_unknown", "status": "completed",
        }))
        self.assertEqual(
            actions_of(parser.snapshot()),
            [("cmd", "false", "failed")],
        )

    def test_consecutive_thinking_and_text_are_merged(self):
        parser = EventParser("dsh")
        parser.feed(json.dumps({"type": "status", "phase": "step_start", "turn": 1, "step": 1}))
        parser.feed(json.dumps({"type": "text", "text": "第一段"}, ensure_ascii=False))
        parser.feed(json.dumps({"type": "text", "text": "第二段"}, ensure_ascii=False))
        parser.feed(json.dumps({
            "type": "tool_call", "callId": "call_1", "tool": "bash",
            "input": {"command": "echo hi"},
        }))
        parser.feed(json.dumps({
            "type": "tool_result", "callId": "call_1", "status": "completed",
        }))
        parser.feed(json.dumps({"type": "thinking", "text": "先想"}, ensure_ascii=False))
        parser.feed(json.dumps({"type": "thinking", "text": "再想"}, ensure_ascii=False))

        snap = parser.snapshot()
        self.assertEqual(snap["step"], 1)
        self.assertEqual(
            actions_of(snap),
            [
                ("say", "第一段第二段", "done"),
                ("cmd", "echo hi", "done"),
                ("think", "先想再想", "done"),
            ],
        )


class AgyParserTest(unittest.TestCase):
    def test_snapshot(self):
        snap = parse_fixture("agy")

        self.assertEqual(snap["session"], AGY_SESSION)
        self.assertEqual(snap["final"], AGY_FINAL)
        self.assertEqual(snap["tokens"], AGY_TOKENS)
        self.assertEqual(snap["step"], 3)  # 最大 step_index；user_input 的 0 被忽略

        self.assertEqual(
            actions_of(snap),
            [
                ("read", "README.md", "done"),  # 路径只取 basename
                ("say", AGY_SAY, "done"),        # 同一步的 text_delta 累积后取前 80 字
            ],
        )

    def test_text_delta_accumulates_within_step(self):
        parser = EventParser("agy")
        for delta, state in (("你", "ACTIVE"), ("好", "ACTIVE"), ("呀", "DONE")):
            parser.feed(json.dumps({
                "event": "step_update",
                "step_update": {
                    "step_index": 1,
                    "state": state,
                    "step_type": "agent_response",
                    "text_delta": delta,
                },
            }, ensure_ascii=False))

        snap = parser.snapshot()
        self.assertEqual(snap["step"], 1)
        self.assertEqual(actions_of(snap), [("say", "你好呀", "done")])

    def test_read_file_path_is_basename(self):
        snap = parse_fixture("agy")
        reads = [a for a in snap["actions"] if a["kind"] == "read"]
        self.assertEqual(len(reads), 1)
        self.assertEqual(reads[0]["text"], "README.md")
        self.assertNotIn("/", reads[0]["text"])


class BadInputTest(unittest.TestCase):
    def test_blank_lines_and_bad_json_are_silently_ignored(self):
        for agent in ("codex", "dsh", "agy"):
            clean = parse_fixture(agent)

            parser = EventParser(agent)
            for junk in ("", "   ", "\t", "not json", "{", '{"type":'):
                parser.feed(junk)
            for line in _fixture_lines(agent):
                parser.feed(line)
                parser.feed("")

            self.assertEqual(parser.snapshot(), clean, agent)

    def test_empty_parser_snapshot(self):
        for agent in ("codex", "dsh", "agy"):
            snap = EventParser(agent).snapshot()
            self.assertEqual(snap["step"], 0)
            self.assertIsNone(snap["tokens"])
            self.assertEqual(snap["actions"], [])
            self.assertIsNone(snap["final"])
            self.assertIsNone(snap["session"])


if __name__ == "__main__":
    unittest.main()
