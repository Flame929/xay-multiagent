"""xay_normalize 单元测试（arch-v4 §3.4 / §12.2 WP4）。

测试只使用 WP4 写集内的文件：scripts/xay_normalize.py、tests/fixtures/v4/**。
看板 v2 的 humanize() 由 importlib 按路径加载，用来验证 WP4 的“humanize 兼容”。
"""

import importlib.util
import json
import sys
import tempfile
import unittest
from pathlib import Path

TESTS_DIR = Path(__file__).resolve().parent
PROJECT_ROOT = TESTS_DIR.parent
FIXTURES = TESTS_DIR / "fixtures" / "v4"
NORMALIZE_PATH = PROJECT_ROOT / "scripts" / "xay_normalize.py"
WATCH_PATH = PROJECT_ROOT / "scripts" / "xay_watch.py"

normalize = None
EVENT_TYPES = None
ENVELOPE_FIELDS = None
Normalizer = None
NormWriter = None
to_action = None
merge_actions = None
humanize = None
watch_module = None

CTX = {
    "ws": "ws_a1b2c3d4",
    "session": "ls_20261029_abcd",
    "phase": "ph_20261029_ef01",
    "task": "task_20261029_2345",
    "key": "wire",
    "execution": "xs_20261029_6789",
    "turn": "turn_20261029_abcd",
}

# file -> 明确的来源（同时验证 source 别名不依赖自动探测）
FIXTURE_SOURCES = (
    ("codex.jsonl", "codex-exec"),
    ("dsh.jsonl", "dsh-headless"),
    ("agy.jsonl", "agy-stream"),
    ("appserver.jsonl", "codex-app-server"),
    ("control.jsonl", "xay-control"),
)

ALL_TYPES = {
    "session.started", "turn.started", "turn.completed", "reasoning", "message",
    "tool.call", "tool.result", "file.changed", "usage", "approval.requested",
    "question", "denied", "warning", "error", "control.requested",
    "control.acked", "control.failed", "signal", "decision.created",
    "decision.resolved", "note.updated",
}


def _load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    if spec is None or spec.loader is None:
        raise AssertionError("无法为 %s 创建 import spec" % path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def setUpModule():
    global normalize, EVENT_TYPES, ENVELOPE_FIELDS, Normalizer, NormWriter
    global to_action, merge_actions, humanize, watch_module
    if not NORMALIZE_PATH.is_file():
        raise AssertionError("缺少待测实现文件: %s" % NORMALIZE_PATH)
    if not WATCH_PATH.is_file():
        raise AssertionError("缺少 humanize 来源文件: %s" % WATCH_PATH)
    module = _load("xay_normalize_under_test", NORMALIZE_PATH)
    watch_module = _load("xay_watch_under_test_for_normalize", WATCH_PATH)
    for attr in ("normalize", "normalize_file", "Normalizer", "NormWriter",
                 "to_action", "merge_actions", "EVENT_TYPES", "ENVELOPE_FIELDS"):
        if not hasattr(module, attr):
            raise AssertionError("实现缺少接口: %s" % attr)
    normalize = module
    EVENT_TYPES = tuple(module.EVENT_TYPES)
    ENVELOPE_FIELDS = tuple(module.ENVELOPE_FIELDS)
    Normalizer = module.Normalizer
    NormWriter = module.NormWriter
    to_action = module.to_action
    merge_actions = module.merge_actions
    humanize = watch_module.humanize


def events_for(filename, source, **kwargs):
    ctx = dict(CTX)
    ctx.update(kwargs)
    return normalize.normalize_file(source, FIXTURES / filename, **ctx)


def events_by_type(events, etype):
    return [event for event in events if event.get("type") == etype]


def fixture_lines(filename):
    return (FIXTURES / filename).read_text(encoding="utf-8").splitlines()


# ---------------------------------------------------------------------------
# 信封与固定样本
# ---------------------------------------------------------------------------

class FixtureCoverageTest(unittest.TestCase):
    def test_three_legacy_fixtures_are_copied_verbatim(self):
        for name in ("codex.jsonl", "dsh.jsonl", "agy.jsonl"):
            with self.subTest(name=name):
                origin = TESTS_DIR / "fixtures" / name
                self.assertEqual((FIXTURES / name).read_bytes(), origin.read_bytes())

    def test_manifest_marks_constructed_samples(self):
        manifest = json.loads((FIXTURES / "manifest.json").read_text(encoding="utf-8"))
        self.assertFalse(manifest["codex.jsonl"]["constructed"])
        self.assertFalse(manifest["dsh.jsonl"]["constructed"])
        self.assertFalse(manifest["agy.jsonl"]["constructed"])
        self.assertTrue(manifest["appserver.jsonl"]["constructed"])
        self.assertTrue(manifest["control.jsonl"]["constructed"])
        self.assertIn("codex.md", manifest["appserver.jsonl"]["basis"])
        self.assertTrue((FIXTURES / "README.md").is_file())

    def test_all_sources_normalize_without_error(self):
        for filename, source in FIXTURE_SOURCES:
            with self.subTest(filename=filename):
                events = events_for(filename, source)
                self.assertTrue(events, "%s 没有归一事件" % filename)

    def test_every_event_matches_envelope(self):
        for filename, source in FIXTURE_SOURCES:
            for event in events_for(filename, source):
                with self.subTest(filename=filename, event=event.get("type")):
                    self.assertEqual(set(event), set(ENVELOPE_FIELDS))
                    self.assertIn(event["type"], EVENT_TYPES)
                    self.assertIn(event["type"], ALL_TYPES)
                    self.assertIsInstance(event["ts"], float)
                    self.assertIsNone(event["seq"])  # 全局 seq 由看板分配
                    self.assertIsInstance(event["data"], dict)

    def test_context_is_carried_on_every_event(self):
        for filename, source in FIXTURE_SOURCES:
            for event in events_for(filename, source):
                for field in ("ws", "session", "phase", "task", "key",
                              "execution", "turn"):
                    self.assertEqual(event[field], CTX[field], field)

    def test_all_contract_types_have_samples(self):
        types = set()
        for filename, source in FIXTURE_SOURCES:
            types.update(event["type"] for event in events_for(filename, source))
        self.assertEqual(types, ALL_TYPES)
        self.assertEqual(set(EVENT_TYPES), ALL_TYPES)

    def test_assign_seq(self):
        events = events_for("codex.jsonl", "codex-exec", assign_seq=True)
        self.assertEqual([event["seq"] for event in events],
                         list(range(1, len(events) + 1)))

    def test_source_alias_and_auto_detect(self):
        # source="codex" 时按事件形状分流：app-server 的 method 帧不会被当成 exec。
        norm = Normalizer("codex", **CTX)
        first = norm.feed_line(fixture_lines("appserver.jsonl")[0])
        self.assertEqual(first[0]["type"], "session.started")
        self.assertEqual(first[0]["data"]["driver"], "codex-app-server")
        self.assertEqual(normalize.detect_source({"method": "turn/started"}),
                         "codex-app-server")
        self.assertEqual(normalize.detect_source({"type": "thinking"}),
                         "dsh-headless")
        self.assertEqual(normalize.detect_source({"event": "init"}),
                         "agy-stream")

    def test_unknown_lines_are_ignored(self):
        norm = Normalizer("codex-exec", **CTX)
        self.assertEqual(norm.feed_line("not json"), [])
        self.assertEqual(norm.feed_line(""), [])
        self.assertEqual(norm.feed({"type": "something.else"}), [])


# ---------------------------------------------------------------------------
# delta 不落盘
# ---------------------------------------------------------------------------

class DeltaTest(unittest.TestCase):
    def test_no_delta_events_anywhere(self):
        for filename, source in FIXTURE_SOURCES:
            for event in events_for(filename, source):
                self.assertNotIn("delta", event["type"].lower(),
                                 "%s 泄漏了 delta 事件" % filename)
                self.assertNotIn("delta", json.dumps(event.get("data") or {},
                                                     ensure_ascii=False).lower())

    def test_appserver_agent_message_deltas_are_aggregated_once(self):
        events = events_for("appserver.jsonl", "codex-app-server")
        messages = [event for event in events
                    if event["type"] == "message"
                    and event["data"].get("id") == "item_7"]
        self.assertEqual(len(messages), 1)
        self.assertTrue(messages[0]["data"]["final"])
        self.assertEqual(messages[0]["text"], "结果：完成。\n改动文件：slug.py")

    def test_appserver_command_output_delta_not_persisted(self):
        events = events_for("appserver.jsonl", "codex-app-server")
        serialized = json.dumps(events, ensure_ascii=False)
        # 单条 delta 的文本是 "# slugify 规格\n"，完整输出在 tool.result 的 tail 里；
        # 这里断言没有独立的 delta 事件，且 tail 不超过 5 行。
        results = events_by_type(events, "tool.result")
        cmd = next(event for event in results
                   if event["data"].get("id") == "item_2")
        self.assertLessEqual(len(cmd["data"]["tail"]), 5)
        self.assertNotIn('"delta"', serialized)

    def test_dsh_stream_chunks_become_single_messages(self):
        events = events_for("dsh.jsonl", "dsh-headless")
        messages = events_by_type(events, "message")
        self.assertEqual(len(messages), 2)  # 开场说明 + 最终回复
        self.assertFalse(messages[0]["data"]["final"])
        self.assertTrue(messages[1]["data"]["final"])

    def test_agy_text_deltas_become_single_message(self):
        events = events_for("agy.jsonl", "agy-stream")
        messages = events_by_type(events, "message")
        self.assertEqual(len(messages), 1)
        self.assertTrue(messages[0]["data"]["final"])
        self.assertGreater(messages[0]["data"]["chars"], 100)

    def test_norm_writer_skips_delta_events(self):
        events = events_for("codex.jsonl", "codex-exec")
        with tempfile.TemporaryDirectory(prefix="xay-norm-") as tmp:
            path = Path(tmp) / "events.norm.jsonl"
            bad = {"seq": None, "ts": 1.0, "type": "message.delta",
                   "data": {"delta": "x"}, "text": "x"}
            with NormWriter(path, mode="w") as writer:
                written = writer.write(events + [bad])
                self.assertEqual(writer.skipped, 1)
            lines = path.read_text(encoding="utf-8").splitlines()
            self.assertEqual(written, len(events))
            self.assertEqual(len(lines), len(events))
            self.assertNotIn("delta", path.read_text(encoding="utf-8").lower())

    def test_is_delta(self):
        self.assertTrue(normalize.is_delta({"type": "item/agentMessage/delta"}))
        self.assertTrue(normalize.is_delta({"type": "message", "data": {"delta": "x"}}))
        self.assertFalse(normalize.is_delta({"type": "message", "data": {}}))


# ---------------------------------------------------------------------------
# 各通道
# ---------------------------------------------------------------------------

class AppserverUsageTest(unittest.TestCase):
    def test_token_usage_counts_each_request_once(self):
        def update(total_in, total_cached, total_out, last_in, last_cached, last_out):
            return json.dumps({"method": "thread/tokenUsage/updated", "params": {"tokenUsage": {
                "total": {"inputTokens": total_in, "cachedInputTokens": total_cached, "outputTokens": total_out},
                "last": {"inputTokens": last_in, "cachedInputTokens": last_cached, "outputTokens": last_out}}}})
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "raw.jsonl"
            path.write_text(update(1000, 0, 100, 1000, 0, 100) + "\n"
                            + update(3000, 800, 300, 2000, 800, 200) + "\n", encoding="utf-8")
            usage = events_by_type(normalize.normalize_file("codex-app-server", path, **CTX), "usage")
        self.assertEqual([u["data"]["granularity"] for u in usage], ["step", "step"])
        self.assertEqual([u["data"]["in"] for u in usage], [1000, 2000])
        self.assertEqual(usage[1]["data"]["cached"], 800)

class AppserverRetryTest(unittest.TestCase):
    def test_will_retry_error_is_only_a_warning(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "raw.jsonl"
            path.write_text(json.dumps({"method": "error", "params": {"message": "stream disconnected", "willRetry": True}}) + "\n"
                            + json.dumps({"method": "error", "params": {"message": "boom"}}) + "\n", encoding="utf-8")
            events = normalize.normalize_file("codex-app-server", path, **CTX)
        self.assertEqual([e["type"] for e in events if e["type"] in ("error", "warning")], ["warning", "error"])

class CodexExecTest(unittest.TestCase):
    def setUp(self):
        self.events = events_for("codex.jsonl", "codex-exec")

    def test_session_and_envelope_do_not_mix_native_id(self):
        started = events_by_type(self.events, "session.started")[0]
        self.assertEqual(started["data"]["native_session_id"],
                         "01a181e6-f381-7910-9a8f-7536e2dcf3c9")
        self.assertEqual(started["session"], CTX["session"])

    def test_error_item_is_diagnostic_not_action(self):
        errors = events_by_type(self.events, "error")
        self.assertEqual(len(errors), 1)
        self.assertIn("unrecognized configuration", errors[0]["text"])
        actions = [to_action(event) for event in self.events]
        self.assertFalse(any(action and action["kind"] == "error" for action in actions))

    def test_tool_call_and_result(self):
        calls = [event for event in events_by_type(self.events, "tool.call")
                 if event["data"].get("id") == "item_2"]
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0]["kind"], "cmd")
        self.assertEqual(calls[0]["text"], "cat spec.md")
        self.assertEqual(calls[0]["data"]["raw"], "/bin/zsh -lc 'cat spec.md'")
        result = next(event for event in events_by_type(self.events, "tool.result")
                      if event["data"].get("id") == "item_2")
        self.assertTrue(result["data"]["ok"])
        self.assertEqual(result["data"]["exit"], 0)
        self.assertEqual(result["data"]["tail"], ["# slugify spec"])
        self.assertTrue(result["data"]["hash"])

    def test_failed_command_tail_and_ok(self):
        result = next(event for event in events_by_type(self.events, "tool.result")
                      if event["data"].get("id") == "item_5")
        self.assertFalse(result["data"]["ok"])
        self.assertEqual(result["data"]["exit"], 1)
        self.assertEqual(result["data"]["tail"], ["FAILED (failures=1)"])

    def test_file_change_emits_file_changed(self):
        changed = events_by_type(self.events, "file.changed")
        self.assertEqual(len(changed), 1)
        self.assertEqual(changed[0]["path"], "/tmp/demo/slug.py")
        self.assertEqual(changed[0]["data"]["source"], "native")

    def test_usage_and_turn_completed(self):
        usage = events_by_type(self.events, "usage")[0]
        self.assertEqual((usage["data"]["in"], usage["data"]["out"],
                          usage["data"]["cached"]), (25705, 512, 9088))
        self.assertEqual(usage["data"]["granularity"], "turn")
        completed = events_by_type(self.events, "turn.completed")[0]
        self.assertEqual(completed["data"]["status"], "completed")

    def test_final_message(self):
        messages = events_by_type(self.events, "message")
        self.assertEqual(len(messages), 1)
        self.assertTrue(messages[0]["data"]["final"])
        self.assertEqual(messages[0]["text"], "结果：完成。\n改动文件：slug.py")


class DshTest(unittest.TestCase):
    def setUp(self):
        self.events = events_for("dsh.jsonl", "dsh-headless")

    def test_session_native_id(self):
        started = events_by_type(self.events, "session.started")[0]
        self.assertEqual(started["data"]["native_session_id"],
                         "session-e4b56672-2f98-447c-9979-8c0f62be1d74")

    def test_tools_and_usage(self):
        calls = events_by_type(self.events, "tool.call")
        self.assertEqual([event["text"] for event in calls],
                         ["ls -la", ".xay/context.md", "spec.md"])
        self.assertEqual([event["kind"] for event in calls], ["cmd", "read", "read"])
        results = events_by_type(self.events, "tool.result")
        self.assertEqual(len(results), 3)
        self.assertTrue(all(event["data"]["ok"] for event in results))
        usage = events_by_type(self.events, "usage")
        self.assertEqual(len(usage), 3)
        self.assertTrue(all(event["data"]["granularity"] == "step" for event in usage))
        self.assertEqual(usage[-1]["data"]["total"], 21809)

    def test_final_message_matches_final_event(self):
        messages = events_by_type(self.events, "message")
        final_line = json.loads(fixture_lines("dsh.jsonl")[-1])
        self.assertTrue(messages[-1]["data"]["final"])
        self.assertEqual(messages[-1]["text"], final_line["text"])
        self.assertEqual(len(events_by_type(self.events, "turn.started")), 1)
        self.assertEqual(events_by_type(self.events, "turn.completed")[0]["data"]["status"],
                         "completed")


class AgyTest(unittest.TestCase):
    def setUp(self):
        self.events = events_for("agy.jsonl", "agy-stream")

    def test_session_and_single_turn(self):
        started = events_by_type(self.events, "session.started")[0]
        self.assertEqual(started["data"]["native_session_id"],
                         "e885c499-e9db-489e-ad15-73d5dc71fc9a")
        self.assertEqual(len(events_by_type(self.events, "turn.started")), 1)
        completed = events_by_type(self.events, "turn.completed")[0]
        self.assertEqual(completed["data"]["status"], "completed")

    def test_tool_read_uses_basename_like_watch(self):
        call = events_by_type(self.events, "tool.call")[0]
        self.assertEqual(call["kind"], "read")
        self.assertEqual(call["text"], "README.md")

    def test_final_message_and_usage(self):
        messages = events_by_type(self.events, "message")
        self.assertEqual(len(messages), 1)
        self.assertTrue(messages[0]["data"]["final"])
        self.assertIn("# hello probe", messages[0]["text"])
        usage = events_by_type(self.events, "usage")
        self.assertEqual(usage[-1]["data"]["granularity"], "turn")
        self.assertEqual(usage[-1]["data"]["total"], 23628)


class AppServerTest(unittest.TestCase):
    def setUp(self):
        self.events = events_for("appserver.jsonl", "codex-app-server")

    def test_session_and_native_turn_ids(self):
        started = events_by_type(self.events, "session.started")[0]
        self.assertEqual(started["data"]["native_session_id"], "th_9f1c2a")
        self.assertEqual(started["data"]["driver"], "codex-app-server")
        turns = events_by_type(self.events, "turn.started")
        self.assertEqual([event["data"]["native_turn_id"] for event in turns],
                         ["nturn_1", "nturn_2", "nturn_3"])
        self.assertEqual(turns[0]["session"], CTX["session"])

    def test_reasoning_command_and_tail(self):
        reasoning = events_by_type(self.events, "reasoning")[0]
        self.assertEqual(reasoning["text"], "先读规格，再改 slugify。")
        call = next(event for event in events_by_type(self.events, "tool.call")
                    if event["data"].get("id") == "item_2")
        self.assertEqual(call["text"], "sed -n 1,80p spec.md")
        result = next(event for event in events_by_type(self.events, "tool.result")
                      if event["data"].get("id") == "item_2")
        self.assertTrue(result["data"]["ok"])
        self.assertEqual(result["data"]["tail"],
                         ["# slugify 规格", "函数 slugify(text, max_len=50)"])

    def test_file_change_and_mcp(self):
        changed = events_by_type(self.events, "file.changed")[0]
        self.assertEqual(changed["path"], "/tmp/xay-wire/slug.py")
        self.assertEqual((changed["data"]["add"], changed["data"]["del"]), (3, 0))
        mcp = next(event for event in events_by_type(self.events, "tool.call")
                   if event["data"].get("tool") == "mcpToolCall")
        self.assertEqual(mcp["kind"], "mcp")
        self.assertEqual(mcp["text"], "query_inventory")
        self.assertEqual(mcp["data"]["server"], "lingxing")

    def test_approval_question_denied(self):
        approval = events_by_type(self.events, "approval.requested")[0]
        self.assertEqual(approval["data"]["request_id"], 7)
        self.assertEqual(approval["text"], "rm -rf build")
        denied = events_by_type(self.events, "denied")[0]
        self.assertEqual(denied["text"], "rm -rf build")
        self.assertEqual(denied["data"]["request_id"], 7)
        question = events_by_type(self.events, "question")[0]
        self.assertEqual(question["text"], "要保留旧接口吗？")
        self.assertEqual(question["data"]["request_id"], 8)

    def test_warning_error_and_dedupe(self):
        warnings = events_by_type(self.events, "warning")
        self.assertEqual([event["text"] for event in warnings],
                         ["3 个无效配置项被忽略"])
        errors = events_by_type(self.events, "error")
        # "model overloaded" 同时出现在 error 通知与 failed turn 里，只保留一条。
        self.assertEqual([event["text"] for event in errors],
                         ["MCP lingxing 返回 401", "model overloaded"])

    def test_usage_granularity_and_turn_statuses(self):
        usage = events_by_type(self.events, "usage")
        self.assertEqual([event["data"]["granularity"] for event in usage],
                         ["live", "turn"])
        statuses = [event["data"]["status"] for event in
                    events_by_type(self.events, "turn.completed")]
        self.assertEqual(statuses, ["completed", "interrupted", "failed"])

    def test_turn_diff_updated_emits_file_changed(self):
        diff = (
            "--- a/slug.py\n"
            "+++ b/slug.py\n"
            "@@ -1,2 +1,3 @@\n"
            "-old\n"
            "+new\n"
            "+extra\n"
        )
        norm = Normalizer("codex-app-server", **CTX)
        events = norm.feed({"method": "turn/diff/updated",
                            "params": {"diff": diff}})
        self.assertEqual([event["type"] for event in events], ["file.changed"])
        self.assertEqual(events[0]["path"], "slug.py")
        self.assertEqual((events[0]["data"]["add"], events[0]["data"]["del"]),
                         (2, 1))

    def test_close_flushes_pending_final_message(self):
        norm = Normalizer("codex-app-server", **CTX)
        lines = fixture_lines("appserver.jsonl")
        events = []
        for line in lines:
            events.extend(norm.feed_line(line))
            if ('"item_7"' in line and '"method":"item/completed"' in line
                    and '"agentMessage"' in line):
                break
        events.extend(norm.close())
        messages = [event for event in events if event["type"] == "message"]
        self.assertEqual(len(messages), 1)
        self.assertTrue(messages[0]["data"]["final"])
        self.assertIn("结果：完成", messages[0]["text"])


class ControlSidecarTest(unittest.TestCase):
    def setUp(self):
        self.events = events_for("control.jsonl", "xay-control")

    def test_control_events_keep_by_and_source(self):
        requested = events_by_type(self.events, "control.requested")
        self.assertEqual([event["by"] for event in requested],
                         ["lead:ls_20261029_abcd", "user"])
        self.assertEqual(requested[0]["data"]["action"], "steer")
        self.assertEqual(requested[0]["data"]["source"], "socket:pid=4321")
        acked = events_by_type(self.events, "control.acked")[0]
        self.assertEqual(acked["data"]["accepted_as"], "steer")
        self.assertEqual(acked["data"]["effective"], "next_sample")
        failed = events_by_type(self.events, "control.failed")[0]
        self.assertEqual(failed["data"]["action"], "interrupt")
        self.assertIn("已经结束", failed["text"])

    def test_signal_decision_note(self):
        signal = events_by_type(self.events, "signal")[0]
        self.assertEqual(signal["kind"], "stalled")
        self.assertEqual(signal["data"]["level"], "remind")
        created = events_by_type(self.events, "decision.created")[0]
        self.assertEqual(created["data"]["id"], "dec_20261029_0001")
        self.assertEqual(created["data"]["kind"], "conflict")
        resolved = events_by_type(self.events, "decision.resolved")[0]
        self.assertEqual(resolved["data"]["option"], "handoff_lead")
        self.assertEqual(resolved["data"]["by"], "user")
        note = events_by_type(self.events, "note.updated")[0]
        self.assertEqual(note["data"]["key"], "wire")
        self.assertIn("UTF-8", note["text"])

    def test_control_action_projection(self):
        requested = events_by_type(self.events, "control.requested")[0]
        action = to_action(requested)
        self.assertEqual(action["kind"], "steer")
        self.assertEqual(action["by"], "lead:ls_20261029_abcd")
        acked = to_action(events_by_type(self.events, "control.acked")[0])
        self.assertEqual(acked["kind"], "control")
        self.assertEqual(acked["state"], "done")
        failed = to_action(events_by_type(self.events, "control.failed")[0])
        self.assertEqual(failed["kind"], "error")
        self.assertEqual(failed["state"], "error")


# ---------------------------------------------------------------------------
# humanize 兼容（与看板 v2 EventParser 的最终显示逐条对比）
# ---------------------------------------------------------------------------

class HumanizeCompatTest(unittest.TestCase):
    def _watch_actions(self, filename, agent):
        parser = watch_module.EventParser(agent)
        for line in fixture_lines(filename):
            parser.feed(line)
        return [humanize(action) for action in parser.snapshot()["actions"]]

    def _normalized_actions(self, filename, source):
        return merge_actions([to_action(event)
                              for event in events_for(filename, source)])

    def test_three_legacy_fixtures_match_watch_humanize(self):
        cases = (
            ("codex.jsonl", "codex-exec", "codex"),
            ("dsh.jsonl", "dsh-headless", "dsh"),
            ("agy.jsonl", "agy-stream", "agy"),
        )
        for filename, source, agent in cases:
            with self.subTest(filename=filename):
                expected = [(action["kind"], action["text"], action["state"])
                            for action in self._watch_actions(filename, agent)]
                actual = [(action["kind"], action["text"], action["state"])
                          for action in self._normalized_actions(filename, source)]
                self.assertEqual(actual, expected)

    def test_action_shape(self):
        events = events_for("codex.jsonl", "codex-exec")
        action = to_action(events_by_type(events, "tool.call")[0])
        self.assertEqual(set(action), {"kind", "text", "raw", "state",
                                       "id", "execution", "turn", "by"})
        self.assertEqual(action["execution"], CTX["execution"])
        self.assertEqual(action["turn"], CTX["turn"])
        self.assertEqual(action["id"], "item_2")

    def test_non_action_events_return_none(self):
        events = events_for("codex.jsonl", "codex-exec")
        for etype in ("session.started", "turn.started", "turn.completed",
                      "usage", "file.changed", "error"):
            with self.subTest(etype=etype):
                event = events_by_type(events, etype)[0]
                self.assertIsNone(to_action(event))

    def test_merge_actions_by_id(self):
        events = events_for("codex.jsonl", "codex-exec")
        actions = [to_action(event) for event in events]
        merged = merge_actions(actions)
        ids = [action["id"] for action in merged]
        self.assertEqual(len(ids), len(set(ids)))
        cat = next(action for action in merged if action["id"] == "item_2")
        self.assertEqual(cat["state"], "done")
        self.assertEqual(cat["text"], "查看 spec.md")


# ---------------------------------------------------------------------------
# 脱敏、输出裁剪、writer 序号
# ---------------------------------------------------------------------------

class RedactionTest(unittest.TestCase):
    def test_secrets_are_redacted(self):
        line = json.dumps({
            "type": "item.completed",
            "item": {
                "id": "item_secret", "type": "command_execution",
                "command": "/bin/zsh -lc 'API_KEY=abcd1234 curl -H \"Authorization: Bearer zzz\" https://x'",
                "aggregated_output": (
                    '{"token":"abcdef"}\n'
                    "sk-abcdefgh\n"
                    "-----BEGIN RSA PRIVATE KEY-----\nMIIE\n-----END RSA PRIVATE KEY-----\n"
                ),
                "exit_code": 0, "status": "completed",
            },
        }, ensure_ascii=False)
        events = normalize.normalize_line("codex-exec", line, **CTX)
        serialized = json.dumps(events, ensure_ascii=False)
        for secret in ("abcd1234", "zzz", "abcdef", "sk-abcdefgh", "MIIE"):
            self.assertNotIn(secret, serialized)
        self.assertIn("redacted", serialized)

    def test_output_tail_limits_lines(self):
        text = "\n".join("line-%d" % index for index in range(20))
        tail = normalize.output_tail(text)
        self.assertEqual(len(tail), 5)
        self.assertEqual(tail[-1], "line-19")
        self.assertTrue(normalize.output_hash(text))


class CliTest(unittest.TestCase):
    def test_cli_writes_norm_jsonl_and_returns_zero(self):
        with tempfile.TemporaryDirectory(prefix="xay-norm-cli-") as tmp:
            path = Path(tmp) / "events.norm.jsonl"
            code = normalize.main([
                "--source", "appserver",
                "--input", str(FIXTURES / "appserver.jsonl"),
                "--output", str(path),
                "--execution", CTX["execution"],
            ])
            self.assertEqual(code, 0)
            text = path.read_text(encoding="utf-8")
            self.assertNotIn('"delta"', text)
            rows = [json.loads(line) for line in text.splitlines()]
            self.assertTrue(rows)
            self.assertTrue(all(row["execution"] == CTX["execution"]
                                for row in rows))


class WriterTest(unittest.TestCase):
    def test_writer_appends_valid_jsonl_and_can_assign_seq(self):
        events = events_for("control.jsonl", "xay-control")
        with tempfile.TemporaryDirectory(prefix="xay-norm-") as tmp:
            path = Path(tmp) / "events.norm.jsonl"
            with NormWriter(path, mode="w", assign_seq=True) as writer:
                self.assertEqual(writer.write(events), len(events))
            rows = [json.loads(line) for line in
                    path.read_text(encoding="utf-8").splitlines()]
            self.assertEqual(len(rows), len(events))
            self.assertEqual([row["seq"] for row in rows],
                             list(range(1, len(rows) + 1)))
            self.assertEqual(rows[0]["type"], "control.requested")

    def test_convenience_normalize_accepts_lines_and_list(self):
        lines = fixture_lines("codex.jsonl")
        by_lines = normalize.normalize("codex-exec", "\n".join(lines), **CTX)
        by_list = normalize.normalize("codex-exec", lines, **CTX)
        self.assertEqual([event["type"] for event in by_lines],
                         [event["type"] for event in by_list])
        self.assertEqual(len(by_lines), len(events_for("codex.jsonl", "codex-exec")))


if __name__ == "__main__":
    unittest.main()
