"""测试所有地方都认新接入的 AI（tk_fbe24a68）。"""
import io
import json
import os
import shutil
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch, MagicMock

V2_ROOT = Path(__file__).resolve().parents[1]
if str(V2_ROOT) not in sys.path:
    sys.path.insert(0, str(V2_ROOT))

from xay import providers  # noqa: E402
from xay.cli import main  # noqa: E402
from xay.core import open_db, load_state, append  # noqa: E402
from xay.office import scheduler as sched_mod  # noqa: E402
from xay.office import rules as rules_mod  # noqa: E402
from xay.office import oldboard  # noqa: E402
from tests.test_cli_smoke import CliCase  # noqa: E402


class TestAgentKinds(CliCase):
    def setUp(self):
        super().setUp()
        self.home.mkdir(parents=True, exist_ok=True)
        self._env_patch = patch.dict(os.environ, {"XAY2_HOME": str(self.home)})
        self._env_patch.start()
        self.addCleanup(self._env_patch.stop)

    def test_plan_add_generic_agent_and_hire(self):
        """用临时 XAY2_HOME 写一份 agents.json，plan add --agent 新种类能排上、管家能给它招人。"""
        agents_cfg = {
            "agents": {
                "custombot": {
                    "label": "CustomBot",
                    "exe": "custombot",
                    "args": ["-p", "{prompt}"]
                }
            }
        }
        (self.home / "agents.json").write_text(json.dumps(agents_cfg, ensure_ascii=False), encoding="utf-8")

        # 启动对话并添加小任务
        self.seed_conv("测试通用员工对话")
        self.ok("plan", "part", "核心功能", "--why", "打通流程")
        snap = self.snap()
        parts = snap["parts"]
        part_id = parts[0]["id"] if isinstance(parts, list) else list(parts.keys())[0]

        # plan add --agent custombot 能排上
        stdout = self.ok("plan", "add", "由新AI处理的任务", "--part", part_id, "--agent", "custombot")
        self.assertIn("tk_", stdout)

        snap = self.snap()
        tasks = {t["id"]: t for t in snap["tasks"]} if isinstance(snap["tasks"], list) else snap["tasks"]
        target_task = next(t for t in tasks.values() if t.get("title") == "由新AI处理的任务")
        self.assertEqual(target_task.get("agent"), "custombot")

        # 验证管家调度：_ready 将 planned 推进到 ready，_queue 能给它招入 custombot 员工并指派
        conn = open_db(self.db_path())
        try:
            scheduler = sched_mod.Scheduler(conn, home=self.home)
            with patch("shutil.which", return_value="/usr/local/bin/custombot"):
                s = load_state(conn)
                scheduler._ready(s)
                s = load_state(conn)
                n = scheduler._queue(s)
                self.assertGreaterEqual(n, 1)

            # 刷新账本状态核验
            s_after = load_state(conn)
            tk_after = s_after["tasks"][target_task["id"]]
            self.assertEqual(tk_after.get("status"), "queued")
            self.assertIsNotNone(tk_after.get("owner"))
            owner_em = s_after["employees"][tk_after["owner"]]
            self.assertEqual(owner_em.get("agent"), "custombot")
            self.assertIn("custombot", owner_em.get("label", "").lower())
        finally:
            conn.close()

    def test_unconfigured_agent_not_auto_picked(self):
        """测试：没配登录的通用员工不会被自动挑中。"""
        # claude 预设了 required_env: ANTHROPIC_API_KEY，在未配登录时不可用
        env_without_anthropic = {k: v for k, v in os.environ.items() if k != "ANTHROPIC_API_KEY"}
        with patch.dict(os.environ, env_without_anthropic, clear=True):
            self.assertFalse(providers.is_available("claude"))
            state = {
                "tasks": {},
                "employees": {},
                "parts": {},
                "depts": {}
            }
            fake_task = {"id": "tk_test", "conv": "cv_test", "ws": "ws_test"}
            # 无论哪些已安装，未配置登录的 claude 绝不能被 pick_agent_kind 选中
            with patch("shutil.which", return_value="/usr/local/bin/claude"):
                chosen = sched_mod.pick_agent_kind(state, fake_task)
                self.assertNotEqual(chosen, "claude")

    def test_plan_add_unavailable_agent_reports_error(self):
        """负责人用 plan add --agent claude 时如果不可用，命令直接报大白话错误：Claude 员工还没配置 API 登录，先在本机配好再派。"""
        self.seed_conv("测试不可用AI报错对话")
        self.ok("plan", "part", "测试部分", "--why", "验证报错")
        snap = self.snap()
        parts = snap["parts"]
        part_id = parts[0]["id"] if isinstance(parts, list) else list(parts.keys())[0]

        r = self.xay("plan", "add", "需要Claude的任务", "--part", part_id, "--agent", "claude",
                     ANTHROPIC_API_KEY=None)
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("Claude 员工还没配置 API 登录，先在本机配好再派", r.stderr)

    def test_prefer_order_generic_placed_last(self):
        """pick_agent_kind 的 PREFER_ORDER 里没有的新种类排在最后。"""
        agents_cfg = {
            "agents": {
                "zbot": {
                    "label": "ZBot",
                    "exe": "zbot"
                }
            }
        }
        (self.home / "agents.json").write_text(json.dumps(agents_cfg, ensure_ascii=False), encoding="utf-8")

        state = {
            "tasks": {},
            "employees": {},
            "parts": {},
            "depts": {}
        }
        fake_task = {"id": "tk_test", "conv": "cv_test", "ws": "ws_test"}

        # 模拟 codex 和自定义新员工 zbot 同时可用
        # PREFER_ORDER 为 ("dsh", "agy", "codex")，zbot 不在 PREFER_ORDER 里，应排在 codex 之后
        def fake_which(cmd):
            name = Path(cmd).name
            return f"/usr/local/bin/{name}" if name in ("codex", "zbot") else None

        with patch("shutil.which", side_effect=fake_which):
            chosen = sched_mod.pick_agent_kind(state, fake_task)
            # 优先选 PREFER_ORDER 中的 codex，而不是 zbot
            self.assertEqual(chosen, "codex")

            # 当 codex 被排除（满载）时，才轮到不在 PREFER_ORDER 的 zbot
            chosen2 = sched_mod.pick_agent_kind(state, fake_task, lines_out={"codex": "满载"})
            self.assertEqual(chosen2, "zbot")

    def test_oldboard_and_rules_recognize_agent_kinds(self):
        """验证 oldboard 和 rules 识别 agent_kinds 并把不可用种类记入 lines_out。"""
        self.assertIn("codex", oldboard.KNOWN_AGENTS)
        conn = open_db(self.db_path())
        try:
            scheduler = sched_mod.Scheduler(conn, home=self.home)
            rules = rules_mod.Rules(scheduler)
            s = load_state(conn)
            # 未配登录的 claude 会被 rules.update_caps 识别为不可用并记录在 lines_out
            env_without_anthropic = {k: v for k, v in os.environ.items() if k != "ANTHROPIC_API_KEY"}
            with patch.dict(os.environ, env_without_anthropic, clear=True):
                rules.update_caps(s)
                self.assertIn("claude", rules.lines_out)
        finally:
            conn.close()


if __name__ == "__main__":
    unittest.main()
