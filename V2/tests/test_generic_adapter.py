"""通用命令行员工适配器测试：text/jsonl 输出、续接对话、只读参数、错误归类、拒绝密钥声明。"""

from __future__ import annotations

import json
import os
import stat
import sys
import tempfile
import time
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
V2 = HERE.parent
if str(V2) not in sys.path:
    sys.path.insert(0, str(V2))

from xay import providers
from xay.adapters import (
    AdapterBusy, AdapterConfigError, AdapterError, GenericAdapter, make_adapter,
)

FAKE_CLI = HERE / "fixtures_adapters" / "generic_fake_cli.py"


class GenericAdapterTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="xay-generic-test-"))
        self.cwd = self.tmp / "desk"
        self.cwd.mkdir(parents=True)
        self.log_file = self.tmp / "fake_cli.jsonl"
        self.env_patch = {
            "FAKE_GENERIC_LOG": str(self.log_file),
            "XAY2_HOME": str(self.tmp / "v2_home"),
        }
        (self.tmp / "v2_home").mkdir(parents=True)
        self._orig_env = dict(os.environ)
        os.environ.update(self.env_patch)

        # 确保假 CLI 脚本具备可执行权限
        if FAKE_CLI.exists():
            mode = FAKE_CLI.stat().st_mode
            FAKE_CLI.chmod(mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)

    def tearDown(self):
        os.environ.clear()
        os.environ.update(self._orig_env)

    def read_invocations(self):
        if not self.log_file.exists():
            return []
        with open(self.log_file, "r", encoding="utf-8") as fh:
            return [json.loads(line) for line in fh if line.strip()]

    def test_jsonl_output(self):
        spec = {
            "label": "TestJSONL",
            "exe": sys.executable,
            "args": [str(FAKE_CLI), "--format", "json", "{prompt}"],
            "output": "jsonl",
            "session_field": "session_id",
            "text_field": "text",
            "usage_field": "usage.total_tokens",
            "context_window": 200000,
        }
        adapter = GenericAdapter("test_jsonl", spec=spec)
        adapter.open(str(self.cwd))
        adapter.send("请编写代码")

        events = list(adapter.events(timeout=4.0))
        adapter.close()

        kinds = [e.kind for e in events]
        self.assertIn("turn_started", kinds)
        self.assertIn("session", kinds)
        self.assertIn("text", kinds)
        self.assertIn("usage", kinds)
        self.assertIn("turn_ended", kinds)

        session_ev = next(e for e in events if e.kind == "session")
        self.assertEqual(session_ev.data.get("native_id"), "generic-sess-999")

        ended_ev = next(e for e in events if e.kind == "turn_ended")
        self.assertEqual(ended_ev.data.get("status"), "completed")
        self.assertIn("已完成代码编写与测试验证", ended_ev.data.get("final_text", ""))

        usage_ev = next(e for e in events if e.kind == "usage")
        self.assertEqual(usage_ev.data.get("used"), 128)

    def test_text_output(self):
        spec = {
            "label": "TestText",
            "exe": sys.executable,
            "args": [str(FAKE_CLI), "{prompt}"],
            "output": "text",
        }
        adapter = GenericAdapter("test_text", spec=spec)
        adapter.open(str(self.cwd))
        adapter.send("[[text]] 纯文本运行")

        events = list(adapter.events(timeout=4.0))
        adapter.close()

        kinds = [e.kind for e in events]
        self.assertIn("turn_started", kinds)
        self.assertIn("session", kinds)
        self.assertIn("text", kinds)
        self.assertIn("turn_ended", kinds)

        ended_ev = next(e for e in events if e.kind == "turn_ended")
        self.assertEqual(ended_ev.data.get("status"), "completed")
        self.assertIn("【纯文本输出】任务执行完毕", ended_ev.data.get("final_text", ""))

    def test_resume_args(self):
        spec = {
            "label": "TestResume",
            "exe": sys.executable,
            "args": [str(FAKE_CLI), "--format", "json", "{prompt}"],
            "resume_args": [str(FAKE_CLI), "--format", "json", "--session", "{session}", "{prompt}"],
            "output": "jsonl",
            "session_field": "session_id",
            "text_field": "text",
        }
        adapter = GenericAdapter("test_resume", spec=spec)
        adapter.open(str(self.cwd))

        # 第一轮
        adapter.send("第一轮开工")
        events1 = list(adapter.events(timeout=4.0))
        ended1 = next(e for e in events1 if e.kind == "turn_ended")
        self.assertEqual(ended1.data.get("status"), "completed")
        self.assertEqual(adapter.native_id, "generic-sess-999")

        # 第二轮接着做（续接）
        adapter.send("第二轮接着干")
        events2 = list(adapter.events(timeout=4.0))
        ended2 = next(e for e in events2 if e.kind == "turn_ended")
        self.assertEqual(ended2.data.get("status"), "completed")
        adapter.close()

        invocations = self.read_invocations()
        self.assertGreaterEqual(len(invocations), 2)
        second_argv = invocations[1]["argv"]
        self.assertIn("--session", second_argv)
        sess_idx = second_argv.index("--session")
        self.assertEqual(second_argv[sess_idx + 1], "generic-sess-999")

    def test_readonly_args(self):
        spec = {
            "label": "TestReadonly",
            "exe": sys.executable,
            "args": [str(FAKE_CLI), "{prompt}"],
            "readonly_args": ["--readonly"],
            "output": "text",
        }
        adapter = GenericAdapter("test_readonly", spec=spec)
        adapter.open(str(self.cwd), card={"readonly": True})
        adapter.send("[[text]] 只读模式测试")

        events = list(adapter.events(timeout=4.0))
        adapter.close()
        ended = next(e for e in events if e.kind == "turn_ended")
        self.assertEqual(ended.data.get("status"), "completed")

        invocations = self.read_invocations()
        self.assertTrue(len(invocations) >= 1)
        self.assertIn("--readonly", invocations[0]["argv"])

        # 测试不具备 readonly_args 时拒绝只读任务
        spec_no_ro = {
            "label": "TestNoRO",
            "exe": sys.executable,
            "args": [str(FAKE_CLI), "{prompt}"],
            "output": "text",
        }
        adapter_bad = GenericAdapter("test_no_ro", spec=spec_no_ro)
        adapter_bad.open(str(self.cwd), card={"readonly": True})
        with self.assertRaises(AdapterConfigError):
            adapter_bad.send("尝试执行")
        adapter_bad.close()

    def test_error_classification(self):
        cases = [
            ("[[quota]] 额度测试", "quota"),
            ("[[rate_limit]] 限流测试", "rate_limit"),
            ("[[auth]] 认证失败测试", "auth"),
            ("[[network]] 网络异常测试", "network"),
            ("[[other]] 其它未知错误", "other"),
        ]
        spec = {
            "label": "TestErrors",
            "exe": sys.executable,
            "args": [str(FAKE_CLI), "{prompt}"],
            "output": "text",
        }
        for prompt, expected_kind in cases:
            with self.subTest(expected_kind=expected_kind):
                adapter = GenericAdapter("test_err", spec=spec)
                adapter.open(str(self.cwd))
                adapter.send(prompt)
                events = list(adapter.events(timeout=4.0))
                adapter.close()

                ended = next(e for e in events if e.kind == "turn_ended")
                self.assertEqual(ended.data.get("status"), "failed")
                self.assertEqual(ended.data.get("error_kind"), expected_kind)

    def test_secret_rejection(self):
        # 1. env 含有 sk- 密钥值被拒绝
        bad_spec_1 = {
            "label": "BadAI",
            "exe": "bad",
            "env": {"OPENAI_API_KEY": "sk-" + "1234567890abcdef"},
        }
        with self.assertRaises(ValueError) as cm:
            providers.validate_agent_spec("bad_ai", bad_spec_1)
        self.assertIn("密钥值", str(cm.exception))

        # 2. env 含有非合法变量名的值（直接写了具体 token）
        bad_spec_2 = {
            "label": "BadAI",
            "exe": "bad",
            "env": {"MY_TOKEN": "Bearer xyz1234567890"},
        }
        with self.assertRaises(ValueError):
            providers.validate_agent_spec("bad_ai", bad_spec_2)

    def test_providers_and_make_adapter(self):
        # 验证 agent_kinds
        kinds = providers.agent_kinds()
        self.assertIn("codex", kinds)
        self.assertIn("dsh", kinds)
        self.assertIn("agy", kinds)
        self.assertIn("opencode", kinds)
        self.assertIn("devin", kinds)
        self.assertIn("zcode", kinds)
        self.assertIn("claude", kinds)

        # 验证 get()
        p = providers.get("opencode")
        self.assertEqual(p.agent, "opencode")
        self.assertEqual(p.max_concurrent, 2)
        self.assertIsNone(p.daily_call_cap)
        self.assertEqual(p.steer, "none")
        self.assertEqual(p.interrupt, "kill")

        # 验证 make_adapter
        adapter = make_adapter("opencode")
        self.assertIsInstance(adapter, GenericAdapter)
        self.assertEqual(adapter.agent, "opencode")
        self.assertEqual(adapter.capabilities.steer, "none")
        self.assertTrue(adapter.capabilities.resume)

        # 验证 claude make_adapter 成功
        claude_adapter = make_adapter("claude")
        self.assertIsInstance(claude_adapter, GenericAdapter)
        self.assertEqual(claude_adapter.agent, "claude")

        # 验证未知 AI 报错
        with self.assertRaises(ValueError):
            make_adapter("unknown_xyz")

    def test_claude_preset_commands_stream_json_and_env(self):
        """测试 Claude Code 预设：命令行拼接（新/续接/只读）、stream-json解析、登录状态与环境隔离。"""
        # 准备假 Claude CLI 脚本
        fake_claude = self.tmp / "fake_claude.py"
        claude_log = self.tmp / "fake_claude_log.jsonl"
        fake_claude_code = f"""#!/usr/bin/env python3
import json, os, sys

log_file = r"{str(claude_log)}"
rec = {{
    "argv": sys.argv[1:],
    "CLAUDE_CONFIG_DIR": os.environ.get("CLAUDE_CONFIG_DIR", ""),
    "ANTHROPIC_API_KEY": os.environ.get("ANTHROPIC_API_KEY", ""),
}}
with open(log_file, "a", encoding="utf-8") as fh:
    fh.write(json.dumps(rec, ensure_ascii=False) + "\\n")

# 解析 session
sess = "claude-sess-auto-100"
for i, a in enumerate(sys.argv[1:]):
    if a == "--resume" and i + 1 < len(sys.argv[1:]):
        sess = sys.argv[1:][i + 1]

# 回放一段 stream-json
sys.stdout.write(json.dumps({{"type": "system", "session_id": sess}}, ensure_ascii=False) + "\\n")
sys.stdout.write(json.dumps({{"type": "assistant", "message": "正在审查代码..."}}, ensure_ascii=False) + "\\n")
sys.stdout.write(json.dumps({{"type": "result", "result": "Claude 完成了预留接口适配", "usage": 288}}, ensure_ascii=False) + "\\n")
sys.stdout.flush()
"""
        fake_claude.write_text(fake_claude_code, encoding="utf-8")
        fake_claude.chmod(0o755)

        # 引导 GenericAdapter 指向假 Claude 脚本
        os.environ["XAY_CLAUDE_BIN"] = str(fake_claude)

        # 1. 登录状态与可用性检查
        # 1.1 未设 ANTHROPIC_API_KEY 时算未登录
        os.environ.pop("ANTHROPIC_API_KEY", None)
        p_unlogged = providers.get("claude")
        self.assertFalse(p_unlogged.available)
        self.assertFalse(providers.is_available("claude"))
        self.assertIn("未配置 API 登录", p_unlogged.status_text or "")

        # 1.2 设了 ANTHROPIC_API_KEY 时算可用
        os.environ["ANTHROPIC_API_KEY"] = "sk-ant-test-key-999"
        p_logged = providers.get("claude")
        self.assertTrue(p_logged.available)
        self.assertTrue(providers.is_available("claude"))

        # 2. 新对话命令行与 stream-json 回放解析
        adapter = GenericAdapter("claude")
        adapter.open(str(self.cwd))
        adapter.send("新对话任务")

        events = list(adapter.events(timeout=4.0))
        adapter.close()

        # 校验 stream-json 解析事件
        sess_ev = next(e for e in events if e.kind == "session")
        self.assertEqual(sess_ev.data.get("native_id"), "claude-sess-auto-100")

        ended_ev = next(e for e in events if e.kind == "turn_ended")
        self.assertEqual(ended_ev.data.get("status"), "completed")
        self.assertIn("Claude 完成了预留接口适配", ended_ev.data.get("final_text", ""))

        usage_ev = next(e for e in events if e.kind == "usage")
        self.assertEqual(usage_ev.data.get("used"), 288)

        # 检查新对话命令行参数与独立配置目录
        with open(claude_log, "r", encoding="utf-8") as fh:
            logs = [json.loads(line) for line in fh if line.strip()]
        self.assertGreaterEqual(len(logs), 1)
        argv_new = logs[0]["argv"]
        self.assertIn("-p", argv_new)
        self.assertIn("--output-format", argv_new)
        self.assertIn("stream-json", argv_new)
        self.assertIn("--verbose", argv_new)
        self.assertIn("--permission-mode", argv_new)
        self.assertIn("bypassPermissions", argv_new)
        self.assertNotIn("--resume", argv_new)

        # 环境变量检查：CLAUDE_CONFIG_DIR 指向独立目录，不共用负责人 ~/.claude
        expected_config_dir = str(providers.v2_home() / "claude-employee")
        self.assertEqual(logs[0]["CLAUDE_CONFIG_DIR"], expected_config_dir)

        # 3. 续接会话命令行
        adapter_resume = GenericAdapter("claude")
        adapter_resume.open(str(self.cwd), native_id="claude-sess-prior-555")
        adapter_resume.send("续接上一轮对话")
        list(adapter_resume.events(timeout=4.0))
        adapter_resume.close()

        with open(claude_log, "r", encoding="utf-8") as fh:
            logs = [json.loads(line) for line in fh if line.strip()]
        self.assertGreaterEqual(len(logs), 2)
        argv_resume = logs[1]["argv"]
        self.assertIn("--resume", argv_resume)
        res_idx = argv_resume.index("--resume")
        self.assertEqual(argv_resume[res_idx + 1], "claude-sess-prior-555")

        # 4. 只读模式命令行
        adapter_ro = GenericAdapter("claude")
        adapter_ro.open(str(self.cwd), card={"readonly": True})
        adapter_ro.send("只读检查任务")
        list(adapter_ro.events(timeout=4.0))
        adapter_ro.close()

        with open(claude_log, "r", encoding="utf-8") as fh:
            logs = [json.loads(line) for line in fh if line.strip()]
        self.assertGreaterEqual(len(logs), 3)
        argv_ro = logs[2]["argv"]
        self.assertIn("--permission-mode", argv_ro)
        self.assertIn("plan", argv_ro)


if __name__ == "__main__":
    unittest.main()
