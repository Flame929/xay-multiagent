"""测试结果主动送到负责人手上（lead_notify 与 hook）。"""
import io
import json
import os
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from xay import cli
from xay.core import append, open_db
from xay.core.ids import ws_for
from xay.lead_notify import (
    HOOK_COMMAND,
    find_project_root,
    hook_cmd,
    hook_install,
    hook_status,
    hook_uninstall,
    notify_hook,
    settings_path,
)


class TestLeadNotify(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="xay_test_notify_")
        self.home_dir = Path(self.tmp) / "home"
        self.home_dir.mkdir(parents=True, exist_ok=True)
        self.proj_dir = Path(self.tmp) / "proj"
        self.proj_dir.mkdir(parents=True, exist_ok=True)
        self.env_patch = patch.dict(os.environ, {
            "XAY2_HOME": str(self.home_dir),
            "XAY_ROLE": "lead",
        })
        self.env_patch.start()
        self.db_path = self.home_dir / "xay.db"

    def tearDown(self):
        self.env_patch.stop()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_hook_install_uninstall_status_idempotent(self):
        """测试 hook install/uninstall/status 幂等且不破坏其他设置。"""
        claude_dir = self.proj_dir / ".claude"
        claude_dir.mkdir(parents=True, exist_ok=True)
        settings_file = claude_dir / "settings.local.json"

        # 初始写入包含已有设置的配置
        existing_cfg = {
            "permissions": {"allow": ["Bash", "Read"]},
            "hooks": {
                "PreToolUse": [
                    {"matcher": "Bash", "hooks": [{"type": "command", "command": "echo check"}]}
                ]
            },
            "custom_key": 12345
        }
        settings_file.write_text(json.dumps(existing_cfg, indent=2), encoding="utf-8")

        # 1. 检查 status: 此时未安装 xay 钩子
        out = io.StringIO()
        with patch("sys.stdout", out):
            rc = hook_status(self.proj_dir)
        self.assertEqual(rc, 0)
        self.assertIn("未安装", out.getvalue())

        # 2. 安装 hook
        out = io.StringIO()
        with patch("sys.stdout", out):
            rc = hook_install(self.proj_dir)
        self.assertEqual(rc, 0)
        self.assertIn("已成功安装", out.getvalue())

        # 验证配置文件：保留了原有字段，并添加了 PostToolUse
        saved = json.loads(settings_file.read_text(encoding="utf-8"))
        self.assertEqual(saved["custom_key"], 12345)
        self.assertEqual(saved["permissions"]["allow"], ["Bash", "Read"])
        self.assertEqual(len(saved["hooks"]["PreToolUse"]), 1)
        post_hooks = saved["hooks"].get("PostToolUse", [])
        self.assertEqual(len(post_hooks), 1)
        self.assertEqual(post_hooks[0]["hooks"][0]["command"], HOOK_COMMAND)

        # 3. 再次安装：幂等，不重复添加
        out = io.StringIO()
        with patch("sys.stdout", out):
            rc = hook_install(self.proj_dir)
        self.assertEqual(rc, 0)
        self.assertIn("已安装过钩子，无需重复安装", out.getvalue())
        saved_again = json.loads(settings_file.read_text(encoding="utf-8"))
        self.assertEqual(len(saved_again["hooks"]["PostToolUse"]), 1)

        # 4. status 确认已安装
        out = io.StringIO()
        with patch("sys.stdout", out):
            rc = hook_status(self.proj_dir)
        self.assertEqual(rc, 0)
        self.assertIn("已安装", out.getvalue())

        # 5. 卸载 hook
        out = io.StringIO()
        with patch("sys.stdout", out):
            rc = hook_uninstall(self.proj_dir)
        self.assertEqual(rc, 0)
        self.assertIn("已成功从", out.getvalue())

        # 验证卸载后原有设置保留，PostToolUse 中已被移除
        saved_un = json.loads(settings_file.read_text(encoding="utf-8"))
        self.assertEqual(saved_un["custom_key"], 12345)
        self.assertEqual(saved_un["permissions"]["allow"], ["Bash", "Read"])
        self.assertEqual(len(saved_un["hooks"]["PreToolUse"]), 1)
        self.assertEqual(len(saved_un["hooks"]["PostToolUse"]), 0)

        # 6. 再次卸载：幂等
        out = io.StringIO()
        with patch("sys.stdout", out):
            rc = hook_uninstall(self.proj_dir)
        self.assertEqual(rc, 0)
        self.assertIn("未在 .claude/settings.local.json 中发现 xay 钩子", out.getvalue())

    def test_hook_cmd_dispatch(self):
        """测试 hook_cmd 转调及非法子命令。"""
        self.assertEqual(hook_cmd("status", self.proj_dir), 0)
        self.assertEqual(hook_cmd("install", self.proj_dir), 0)
        self.assertEqual(hook_cmd("uninstall", self.proj_dir), 0)
        err = io.StringIO()
        with patch("sys.stderr", err):
            self.assertEqual(hook_cmd("invalid_sub", self.proj_dir), 2)

    def test_notify_hook_workflow(self):
        """测试 notify --hook：没新事时不输出、有新事输出合法 JSON、游标前进后不重复。"""
        conn = open_db(self.db_path)
        ws = ws_for(str(self.proj_dir))
        conv_id = "cv_test_001"

        # 1. 对话开始，但没有要紧事
        append(conn, "conv.begun", "lead:" + conv_id, {
            "title": "测试主任务",
            "lead_kind": "claude",
            "cwd": str(self.proj_dir),
            "aliases": []
        }, ws=ws, conv=conv_id, subject=conv_id)

        # 此时没有要紧事，notify --hook 应该没有任何输出，退出码 0
        out = io.StringIO()
        with patch("sys.stdout", out):
            rc = notify_hook(conn, cwd=self.proj_dir)
        self.assertEqual(rc, 0)
        self.assertEqual(out.getvalue(), "")

        # 2. 注入一个要紧事事件（例如 signal.raised warn）
        append(conn, "signal.raised", "office", {
            "level": "warn",
            "text": "额度即将用尽提醒",
            "kind": "quota"
        }, ws=ws, conv=conv_id, subject="sig_001")

        # 此时有新事：应输出合法 JSON，包含 hookSpecificOutput.additionalContext
        out = io.StringIO()
        with patch("sys.stdout", out):
            rc = notify_hook(conn, cwd=self.proj_dir)
        self.assertEqual(rc, 0)
        raw_output = out.getvalue().strip()
        self.assertTrue(len(raw_output) > 0)
        payload = json.loads(raw_output)
        self.assertIn("hookSpecificOutput", payload)
        self.assertIn("additionalContext", payload["hookSpecificOutput"])
        self.assertIn("额度即将用尽提醒", payload["hookSpecificOutput"]["additionalContext"])

        # 游标文件应当已创建
        cursor_file = self.home_dir / "notify" / conv_id
        self.assertTrue(cursor_file.is_file())
        saved_cursor = int(cursor_file.read_text(encoding="utf-8").strip())
        self.assertGreater(saved_cursor, 0)

        # 3. 再次执行 notify --hook：因为游标已推进，不再重复输出
        out_again = io.StringIO()
        with patch("sys.stdout", out_again):
            rc = notify_hook(conn, cwd=self.proj_dir)
        self.assertEqual(rc, 0)
        self.assertEqual(out_again.getvalue(), "")

        # 4. 对话如果 ended，则不再被匹配
        append(conn, "conv.ended", "lead:" + conv_id, {}, ws=ws, conv=conv_id, subject=conv_id)
        out_ended = io.StringIO()
        with patch("sys.stdout", out_ended):
            rc = notify_hook(conn, cwd=self.proj_dir)
        self.assertEqual(rc, 0)
        self.assertEqual(out_ended.getvalue(), "")
        conn.close()

    def test_wait_timeout_validation(self):
        """测试 wait 的 --timeout 上限放宽到 3600，超过 3600 则报错。"""
        parser = cli._parser()
        args_over = parser.parse_args(["wait", "--timeout", "3601"])
        with self.assertRaises(cli.CliError) as cm:
            cli._wait(None, args_over)
        self.assertIn("最多 3600 秒", str(cm.exception))


if __name__ == "__main__":
    unittest.main()
