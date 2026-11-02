"""xay 看门狗纯函数单测（arch-v4 §8.1 / §3.8 第 4 条 / §7.3 第 2 层）。

覆盖四类规则：静默（含 reasoning delta 与 2 倍升级）、两型循环、
时长预算、git status 越界（含忽略列表）。同时断言看门狗只产出
signal/decision 数据，不引入任何控制通道。
"""

import ast
import importlib.util
import re
import sys
import unittest
from pathlib import Path

TESTS_DIR = Path(__file__).resolve().parent
PROJECT_ROOT = TESTS_DIR.parent
MODULE_PATH = PROJECT_ROOT / "scripts" / "xay_watchdog.py"

watchdog = None


def setUpModule():
    global watchdog
    if not MODULE_PATH.is_file():
        raise AssertionError("缺少待测实现文件: %s" % MODULE_PATH)
    spec = importlib.util.spec_from_file_location("xay_watchdog_under_test", MODULE_PATH)
    if spec is None or spec.loader is None:
        raise AssertionError("无法为 %s 创建 import spec" % MODULE_PATH)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    watchdog = module


def records_of(records, kind=None):
    if kind is None:
        return records
    return [record for record in records if record["data"].get("kind") == kind]


def feed_result(wd, ts, command, output, ok=True, exit_code=0):
    wd.feed({"type": "tool.call", "kind": "cmd", "text": command, "ts": ts})
    return wd.feed({
        "type": "tool.result",
        "kind": "cmd",
        "ok": ok,
        "exit": exit_code,
        "ts": ts + 0.5,
        "data": {"tail": output},
    })


class TestSilence(unittest.TestCase):
    def test_default_thresholds(self):
        self.assertEqual(watchdog.SILENCE_S, {"codex": 180, "dsh": 120, "agy": 240})
        for agent, expected in (("codex", 180), ("dsh", 120), ("agy", 240)):
            self.assertEqual(watchdog.Watchdog(agent).silence_s, expected)
        self.assertEqual(
            watchdog.Watchdog("dsh", thresholds={"dsh": 30}).silence_s, 30)

    def test_threshold_emits_one_stalled_signal(self):
        wd = watchdog.Watchdog("dsh", task="task_1", execution="xs_1")
        wd.feed({"type": "turn.started", "ts": 1000})
        self.assertEqual(wd.tick(1119), [])
        records = wd.tick(1120)
        self.assertEqual(len(records), 1)
        record = records[0]
        self.assertEqual(record["type"], "signal")
        self.assertEqual(record["data"]["kind"], "stalled")
        self.assertEqual(record["data"]["level"], "remind")
        self.assertIn("stalled", record["flags"])
        self.assertEqual(wd.tick(1120 + 10), [])

    def test_codex_threshold_is_180(self):
        wd = watchdog.Watchdog("codex")
        wd.feed({"type": "turn.started", "ts": 0})
        self.assertEqual(wd.tick(179), [])
        self.assertEqual(len(wd.tick(180)), 1)

    def test_reasoning_delta_counts_as_activity(self):
        wd = watchdog.Watchdog("dsh")
        wd.feed({"type": "turn.started", "ts": 0})
        wd.feed({"type": "reasoning", "text": "thinking", "ts": 100,
                 "data": {"delta": "…"}})
        self.assertEqual(wd.tick(219), [])
        records = wd.tick(220)
        self.assertEqual(len(records), 1)
        self.assertEqual(records[0]["data"]["kind"], "stalled")

    def test_double_threshold_escalates_to_failed_final(self):
        wd = watchdog.Watchdog("dsh", task="task_1", execution="xs_1",
                               ws="ws_1", session="ls_1", phase="ph_1")
        wd.feed({"type": "turn.started", "ts": 1000})
        self.assertEqual(records_of(wd.tick(1120), "stalled")[0]["data"]["level"], "remind")
        self.assertEqual(wd.tick(1239), [])
        records = wd.tick(1240)
        self.assertEqual(len(records), 1)
        decision = records[0]
        self.assertEqual(decision["type"], "decision")
        data = decision["data"]
        self.assertEqual(data["kind"], "failed_final")
        self.assertEqual(data["fail_reason"], "stalled")
        self.assertEqual(data["owner"], "lead")
        self.assertEqual(data["version"], 1)
        self.assertIsNone(data["resolved"])
        self.assertEqual(
            [option["id"] for option in data["options"]],
            ["rework", "reassign", "ignore", "handoff_lead"],
        )
        self.assertEqual(data["payload"]["threshold_s"], 120)
        self.assertEqual(data["payload"]["silence_s"], 240)
        self.assertEqual(wd.tick(2000), [])

    def test_activity_clears_stall_and_allows_new_episode(self):
        wd = watchdog.Watchdog("dsh")
        wd.feed({"type": "turn.started", "ts": 0})
        self.assertEqual(len(wd.tick(120)), 1)
        self.assertIn("stalled", wd.flags)
        wd.feed({"type": "tool.call", "kind": "read", "text": "README.md", "ts": 200})
        self.assertNotIn("stalled", wd.flags)
        self.assertEqual(wd.tick(319), [])
        self.assertEqual(len(wd.tick(320)), 1)


class TestLoop(unittest.TestCase):
    def test_same_command_same_output_four_times(self):
        wd = watchdog.Watchdog("dsh", task="task_1", execution="xs_1")
        for index in range(3):
            self.assertEqual(records_of(feed_result(wd, 100 + index * 10, "pytest -q", "FAILED"), "loop"), [])
        records = feed_result(wd, 130, "pytest -q", "FAILED")
        loops = records_of(records, "loop")
        self.assertEqual(len(loops), 1)
        self.assertEqual(loops[0]["data"]["level"], "remind")
        self.assertIn("4", loops[0]["data"]["text"])
        self.assertIn("loop", loops[0]["flags"])
        self.assertEqual(records_of(feed_result(wd, 140, "pytest -q", "FAILED"), "loop"), [])

    def test_same_command_same_error_three_times(self):
        wd = watchdog.Watchdog("dsh")
        self.assertEqual(records_of(feed_result(wd, 100, "make", "No such file", ok=False, exit_code=2), "loop"), [])
        self.assertEqual(records_of(feed_result(wd, 110, "make", "No such file", ok=False, exit_code=2), "loop"), [])
        loops = records_of(feed_result(wd, 120, "make", "No such file", ok=False, exit_code=2), "loop")
        self.assertEqual(len(loops), 1)
        self.assertIn("3", loops[0]["data"]["text"])

    def test_different_output_does_not_trigger(self):
        wd = watchdog.Watchdog("dsh")
        outputs = ["1 passed", "2 passed", "3 passed", "4 passed", "5 passed"]
        records = []
        for index, output in enumerate(outputs):
            records.extend(feed_result(wd, 100 + index * 10, "pytest -q", output))
        self.assertEqual(records_of(records, "loop"), [])

    def test_different_errors_do_not_trigger(self):
        wd = watchdog.Watchdog("dsh")
        records = []
        for index, error in enumerate(["E1", "E2", "E3", "E4"]):
            records.extend(feed_result(wd, 100 + index * 10, "make", error, ok=False, exit_code=1))
        self.assertEqual(records_of(records, "loop"), [])

    def test_different_commands_do_not_trigger(self):
        wd = watchdog.Watchdog("dsh")
        records = []
        for index in range(4):
            records.extend(feed_result(wd, 100 + index * 10, "pytest -q -k %d" % index, "boom", ok=False))
        self.assertEqual(records_of(records, "loop"), [])

    def test_turn_started_resets_loop_counter(self):
        wd = watchdog.Watchdog("dsh")
        for index in range(3):
            feed_result(wd, 100 + index * 10, "pytest -q", "FAILED")
        wd.feed({"type": "turn.started", "ts": 200})
        self.assertNotIn("loop", wd.flags)
        for index in range(3):
            self.assertEqual(
                records_of(feed_result(wd, 201 + index * 10, "pytest -q", "FAILED"), "loop"), [])
        self.assertEqual(
            len(records_of(feed_result(wd, 240, "pytest -q", "FAILED"), "loop")), 1)

    def test_command_normalization(self):
        self.assertEqual(watchdog.normalize_command("cd /tmp &&  pytest   -q"), "pytest -q")
        wd = watchdog.Watchdog("dsh")
        feed_result(wd, 100, "cd /tmp/a && pytest -q", "FAILED")
        feed_result(wd, 110, "pytest -q", "FAILED")
        feed_result(wd, 120, "cd /tmp/b && pytest -q", "FAILED")
        self.assertEqual(len(records_of(feed_result(wd, 130, "pytest -q", "FAILED"), "loop")), 1)

    def test_output_hash_from_event_is_honored(self):
        wd = watchdog.Watchdog("dsh")
        wd.feed({"type": "tool.call", "kind": "cmd", "text": "make", "ts": 100})
        records = []
        for index in range(4):
            records.extend(wd.feed({
                "type": "tool.result", "ok": True, "ts": 101 + index,
                "data": {"command": "make", "hash": "same-hash",
                         "tail": "changing %d" % index},
            }))
        self.assertEqual(len(records_of(records, "loop")), 1)


class TestBudget(unittest.TestCase):
    def test_eighty_percent_reminds_once(self):
        wd = watchdog.Watchdog("dsh", budget_s=100, started=0, silence_s=10000)
        self.assertEqual(wd.tick(79), [])
        records = wd.tick(80)
        self.assertEqual(len(records), 1)
        self.assertEqual(records[0]["data"]["kind"], "over_time")
        self.assertIn("80%", records[0]["data"]["text"])
        self.assertIn("over_time", wd.flags)
        self.assertEqual(wd.tick(99), [])
        self.assertEqual(wd.tick(100), [])
        self.assertEqual(wd.tick(150), [])

    def test_budget_minutes(self):
        wd = watchdog.Watchdog("dsh", budget_minutes=2, started=0, silence_s=10000)
        self.assertEqual(wd.tick(95), [])
        self.assertEqual(len(wd.tick(96)), 1)

    def test_no_budget_no_signal(self):
        wd = watchdog.Watchdog("dsh", silence_s=10000)
        wd.feed({"type": "turn.started", "ts": 0})
        self.assertEqual(wd.tick(9000), [])

    def test_started_defaults_to_first_event(self):
        wd = watchdog.Watchdog("dsh", budget_s=100, silence_s=10000)
        wd.feed({"type": "turn.started", "ts": 500})
        self.assertEqual(wd.started, 500)
        self.assertEqual(wd.tick(579), [])
        self.assertEqual(len(wd.tick(580)), 1)


class TestScope(unittest.TestCase):
    def test_parse_porcelain(self):
        text = "\n".join([
            "## main...origin/main",
            " M src/a.py",
            "?? tests/new file.py",
            "R  old.py -> src/b.py",
        ])
        self.assertEqual(
            watchdog.parse_git_status(text),
            ["src/a.py", "tests/new file.py", "src/b.py"],
        )

    def test_parse_porcelain_quoted(self):
        self.assertEqual(
            watchdog.parse_git_status('?? "dir/a b.py"\n'),
            ["dir/a b.py"],
        )

    def test_parse_porcelain_quoted_non_ascii(self):
        # git 把非 ASCII 路径写成 UTF-8 字节的八进制转义。
        self.assertEqual(
            watchdog.parse_git_status('?? "\\344\\270\\255.py"\n'),
            ["中.py"],
        )

    def test_parse_porcelain_z_rename(self):
        text = " M src/a.py\0?? new.py\0R  src/new.py\0src/old.py\0"
        self.assertEqual(
            watchdog.parse_git_status(text),
            ["src/a.py", "new.py", "src/new.py"],
        )

    def test_default_ignore_list(self):
        for path in (
            "__pycache__/x.pyc",
            "a/.pytest_cache/v/x",
            "coverage/x.html",
            "node_modules/pkg/index.js",
            "dist/app.js",
            "build/out.o",
            ".xay/runs/run1/meta.json",
            ".git/index",
        ):
            self.assertTrue(watchdog.is_ignored(path), path)
        self.assertFalse(watchdog.is_ignored("src/a.py"))

    def test_extra_ignore_pattern(self):
        self.assertTrue(watchdog.is_ignored("logs/a.log", ignore=("*.log",)))
        self.assertFalse(watchdog.is_ignored("src/a.py", ignore=("*.log",)))

    def test_check_scope_exact_directory_and_glob(self):
        writes = ["src/a.py", "scripts/xay_watchdog.py", "tests/**"]
        paths = [
            "src/a.py",
            "src/b.py",
            "scripts/xay_watchdog.py",
            "tests/export/test_api.py",
            "__pycache__/x.pyc",
        ]
        self.assertEqual(watchdog.check_scope(paths, writes), ["src/b.py"])

    def test_check_scope_none_disables_and_empty_forbids_all(self):
        self.assertEqual(watchdog.check_scope(["src/a.py"], None), [])
        self.assertEqual(watchdog.check_scope(["src/a.py"], []), ["src/a.py"])

    def test_check_scope_dedupes(self):
        self.assertEqual(
            watchdog.check_scope(["src/a.py", "src/a.py"], ["src/b.py"]),
            ["src/a.py"],
        )

    def test_feed_git_status_emits_once_per_new_violation(self):
        wd = watchdog.Watchdog("dsh", task="task_1", execution="xs_1",
                               writes=["src/a.py"])
        snapshot = " M src/a.py\n M src/b.py\n?? src/c.py\n?? __pycache__/x.pyc\n"
        records = wd.feed_git_status(snapshot, now=100)
        self.assertEqual(len(records), 1)
        signal = records[0]["data"]
        self.assertEqual(signal["kind"], "scope")
        self.assertIn("src/b.py", signal["text"])
        self.assertIn("src/c.py", signal["text"])
        self.assertNotIn("src/a.py", signal["text"])
        self.assertNotIn("__pycache__", signal["text"])
        self.assertIn("scope", records[0]["flags"])
        self.assertEqual(wd.feed_git_status(snapshot, now=110), [])
        self.assertEqual(wd.feed_git_status(" M src/a.py\n", now=120), [])
        self.assertNotIn("scope", wd.flags)
        self.assertEqual(len(wd.feed_git_status(snapshot, now=130)), 1)

    def test_feed_git_status_accepts_path_list(self):
        wd = watchdog.Watchdog("dsh", writes=["src/a.py"])
        records = wd.feed_git_status(["src/a.py", "src/z.py"], now=100)
        self.assertEqual(len(records), 1)
        self.assertIn("src/z.py", records[0]["data"]["text"])


class TestDataShapeAndNoControl(unittest.TestCase):
    def test_signal_and_decision_ids(self):
        wd = watchdog.Watchdog("dsh", task="task_1", execution="xs_1")
        wd.feed({"type": "turn.started", "ts": 1000})
        signal = wd.tick(1120)[0]
        self.assertRegex(signal["data"]["id"], r"^sig_\d{8}_[0-9a-f]{4}$")
        self.assertIsInstance(signal["data"]["created"], int)
        self.assertFalse(signal["data"]["acked"])
        self.assertEqual(signal["data"]["task"], "task_1")
        decision = wd.tick(1240)[0]
        self.assertRegex(decision["data"]["id"], r"^dec_\d{8}_[0-9a-f]{4}$")

    def test_records_are_data_only(self):
        wd = watchdog.Watchdog("dsh", task="task_1", execution="xs_1", writes=[])
        wd.feed({"type": "turn.started", "ts": 1000})
        records = wd.tick(1240) + wd.feed_git_status("?? out.txt\n", now=1300)
        self.assertTrue(records)
        for record in records:
            self.assertIn(record["type"], ("signal", "decision"))
            self.assertIsInstance(record["data"], dict)
            self.assertIsInstance(record["flags"], list)

    def test_module_imports_no_control_channel(self):
        tree = ast.parse(MODULE_PATH.read_text(encoding="utf-8"), filename=str(MODULE_PATH))
        imported = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imported.update(alias.name.split(".")[0] for alias in node.names)
            elif isinstance(node, ast.ImportFrom) and node.module:
                imported.add(node.module.split(".")[0])
        for forbidden in ("subprocess", "socket", "os", "http", "urllib", "requests"):
            self.assertNotIn(forbidden, imported)
        for forbidden in ("steer", "interrupt", "pause", "resume", "stop", "spawn", "kill"):
            self.assertFalse(hasattr(watchdog.Watchdog, forbidden), forbidden)

    def test_flags_are_current_state(self):
        wd = watchdog.Watchdog("dsh", budget_s=100, started=0, silence_s=10000, writes=[])
        wd.tick(80)
        self.assertEqual(wd.flag_list(), ["over_time"])
        wd.feed_git_status("?? x.py\n", now=90)
        self.assertEqual(wd.flag_list(), ["over_time", "scope"])


if __name__ == "__main__":
    unittest.main()
