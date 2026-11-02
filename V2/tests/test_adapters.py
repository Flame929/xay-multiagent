"""V2 适配器测试：codex / dsh / agy 三家 + 脚本化假适配器 + 提供者表。

绝不启动真的 CLI：codex / dsh / agy 都用 tests/fixtures_adapters/fake_*.py 假进程顶替
（用 card["bin"] 或 XAY_*_BIN 环境变量指过去），回放的是从 ~/.xay/v2/probes 真实录像里截出来
并脱敏的片段。

运行：cd V2 && python3 -m unittest tests.test_adapters -v
"""

from __future__ import annotations

import json
import os
import signal
import stat
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

HERE = Path(__file__).resolve().parent
V2 = HERE.parent
if str(V2) not in sys.path:
    sys.path.insert(0, str(V2))
FIX = HERE / "fixtures_adapters"

from xay import providers  # noqa: E402
from xay.adapters import (  # noqa: E402
    AdapterBusy, AdapterConfigError, AdapterError, AgyAdapter, CodexAdapter, DshAdapter, FakeAdapter, make_adapter, redact,
)

NO_CATALOG = {"XAY_DSH_CATALOG": "/nonexistent/xay-catalog.json"}   # 测试里不读本机真实的 dsh 目录


def make_exec(directory: Path, name: str, script: Path) -> str:
    """写一个小的 shell 可执行文件，转去用当前 Python 跑 fixtures 里的假 CLI。"""
    path = directory / name
    path.write_text('#!/bin/sh\nexec "%s" "%s" "$@"\n' % (sys.executable, script), encoding="utf-8")
    path.chmod(path.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    return str(path)


def wait_for(predicate, timeout=5.0, step=0.02):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        value = predicate()
        if value:
            return value
        time.sleep(step)
    return predicate()


def kinds(events):
    return [e.kind for e in events]


def of(events, kind):
    return [e for e in events if e.kind == kind]


def _terminate_and_kill(proc, timeout=2.0):
    if proc is None:
        return
    pid = proc.pid if hasattr(proc, "pid") else proc
    if not isinstance(pid, int):
        return
    try:
        pgid = os.getpgid(pid)
    except (ProcessLookupError, OSError):
        pgid = None

    def _sig(s):
        if pgid is not None and pgid != os.getpgrp():
            try:
                os.killpg(pgid, s)
            except (ProcessLookupError, OSError):
                pass
        try:
            os.kill(pid, s)
        except (ProcessLookupError, OSError):
            pass

    if hasattr(proc, "poll") and proc.poll() is not None:
        return

    _sig(signal.SIGTERM)
    deadline = time.time() + timeout
    while time.time() < deadline:
        if hasattr(proc, "poll") and proc.poll() is not None:
            return
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return
        except OSError:
            break
        time.sleep(0.05)
    _sig(signal.SIGKILL)
    if hasattr(proc, "wait"):
        try:
            proc.wait(timeout=1.0)
        except Exception:
            pass


class AdapterCase(unittest.TestCase):
    """每个测试一个临时目录：工作目录 + 假 CLI 的日志。"""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory(prefix="xay-adapter-test-")
        self.addCleanup(self._tmp.cleanup)
        self.tmp = Path(self._tmp.name)
        self.work = self.tmp / "desk"
        self.work.mkdir()
        self.bin_dir = self.tmp / "bin"
        self.bin_dir.mkdir()
        self.log = self.tmp / "fake.log"
        self.env = {"FAKE_LOG": str(self.log)}
        self._adapters = []
        patcher = mock.patch.dict(os.environ, NO_CATALOG)
        patcher.start()
        self.addCleanup(patcher.stop)

    def tearDown(self):
        for adapter in list(self._adapters):
            self._cleanup_adapter(adapter)
        # 兜底：日志里记录过的所有假 CLI pid，确保进程和进程组完全退出
        for r in self.records():
            pid = r.get("pid")
            if isinstance(pid, int) and pid != os.getpid():
                _terminate_and_kill(pid, timeout=2.0)

    def _cleanup_adapter(self, adapter):
        try:
            adapter.close()
        except Exception:
            pass
        proc = getattr(adapter, "_proc", None)
        if proc is not None:
            _terminate_and_kill(proc, timeout=2.0)

    def records(self):
        if not self.log.exists():
            return []
        return [json.loads(line) for line in self.log.read_text(encoding="utf-8").splitlines() if line.strip()]

    def argv_records(self, n=1, timeout=5.0):
        """等假 CLI 把"启动记录"写进日志（agy 的 open() 不等进程）。"""
        wait_for(lambda: len([r for r in self.records() if "argv" in r]) >= n, timeout)
        return [r for r in self.records() if "argv" in r]

    def card(self, bin_path, **extra):
        card = {"bin": bin_path, "env": dict(self.env)}
        card.update(extra)
        return card

    def opened(self, adapter, cwd=None, native_id=None, card=None):
        self._adapters.append(adapter)
        self.addCleanup(self._cleanup_adapter, adapter)
        adapter.open(str(cwd or self.work), native_id=native_id, card=card)
        return adapter

    def run_turn(self, adapter, text, timeout=15.0):
        """发一轮并读事件，直到这一轮结束。"""
        adapter.send(text)
        events = adapter.collect(until="turn_ended", timeout=timeout)
        self.assertEqual(events[-1].kind, "turn_ended", "没等到 turn_ended：%s" % kinds(events))
        return events


# ===========================================================================
# 提供者表
# ===========================================================================

class ProvidersTests(AdapterCase):
    def test_model_limits_are_enforced(self):
        self.assertEqual(providers.validate_model("agy", "gemini-3.8-flash-high"), "gemini-3.8-flash-high")
        for bad in ("claude-sonnet-4-6", "gpt-oss-120b-medium", "gemini-3.7-flash", "gemini-3.8-", "Gemini-3.8-Flash"):
            with self.assertRaises(providers.ModelNotAllowed, msg=bad):
                providers.validate_model("agy", bad)
        self.assertEqual(providers.validate_model("dsh", "deepseek-v4.1-flash"), "deepseek-v4.1-flash")
        self.assertIsNone(providers.validate_model("dsh", "默认"))
        for bad in ("deepseek-v4-pro", "gpt-5", "deepseek-v4.1-flash-x"):
            with self.assertRaises(providers.ModelNotAllowed, msg=bad):
                providers.validate_model("dsh", bad)
        self.assertEqual(providers.validate_model("codex", "gpt-6.1-sol"), "gpt-6.1-sol")
        with self.assertRaises(providers.ModelNotAllowed):   # 不能把引号塞进 -c model="…"
            providers.validate_model("codex", 'x" -c evil=1 "')

    def test_default_model_per_effort(self):
        expect = {"off": "low", "minimal": "low", "low": "low", "medium": "medium",
                  "high": "high", "xhigh": "high", "max": "high"}
        for effort, tier in expect.items():
            self.assertEqual(providers.default_model("agy", effort), "gemini-3.8-flash-" + tier)
        self.assertEqual(providers.default_model("agy", None), "gemini-3.8-flash-medium")
        self.assertEqual(providers.default_model("dsh", "high"), "deepseek-v4.1-flash")
        self.assertIsNone(providers.default_model("codex", "high"))
        self.assertEqual(providers.resolve_model("agy", {"effort": "high"}), "gemini-3.8-flash-high")
        self.assertEqual(providers.resolve_model("agy", {"effort": "low", "model": "gemini-3.8-flash-medium"}),
                         "gemini-3.8-flash-medium")
        self.assertEqual(providers.resolve_effort("dsh", {"effort": "bogus"}), "medium")

    def test_context_window_sources(self):
        self.assertEqual(providers.context_window("codex", reported=475000), (475000, False))
        self.assertEqual(providers.context_window("codex"), (475000, True))          # 通知里没带 -> 表里的值，标估算
        # agy：token-optimizer 调研核对的真实窗口 256,000（stream-json 里没有，仍标估算）
        self.assertEqual(providers.context_window("agy", "gemini-3.8-flash-low"), (256_000, True))
        # dsh：没读到配置 -> pi-ai 默认 262,144（估算）；适配器读到配置后作为 reported 传进来；环境变量可以钉
        self.assertEqual(providers.context_window("dsh", "deepseek-v4.1-flash"), (262_144, True))
        self.assertEqual(providers.context_window("dsh", "deepseek-v4.1-flash", reported=500_000), (500_000, False))
        with mock.patch.dict(os.environ, {"XAY_DSH_WINDOW": "123456"}):
            self.assertEqual(providers.context_window("dsh", "deepseek-v4.1-flash"), (123456, False))

    def test_dsh_dump_config_window_and_auto_compaction_point(self):
        """M3 查清的 dsh 门槛：窗口按 dsh 自己的路由配置，自动压缩点 = floor(min(W×0.8, W−O−65,536))。"""
        patch = "/tmp/xay-dsh-ours.patch.yml"
        base = ["# == @deepseek-ai/dsh-base, patched by /u/cordis.patch.yml, %s" % patch,
                "- id: agent-default-model", "  config:", "    provider: opencode-xay",
                "    model: deepseek-v4.1-flash", "    reasoningEffort: low",
                "# == @deepseek-ai/dsh-base", "- id: llm-pi-ai", "  config:", "    providers:", "      opencode-xay:",
                "        models:", "          - id: deepseek-v4-pro", "            contextWindow: 999",
                "          - id: deepseek-v4.1-flash", "            name: x"]
        info = providers.parse_dsh_dump("\n".join(base), "opencode-xay", "deepseek-v4.1-flash", patch)
        self.assertEqual((info["model"], info["effort"], info["patched_by_us"]), ("deepseek-v4.1-flash", "low", True))
        self.assertEqual((info["window"], info["auto_compact_tokens"]), (262_144, 196_608))   # 本机实测就是这个点
        self.assertFalse(providers.parse_dsh_dump("\n".join(base), "opencode-xay", "deepseek-v4.1-flash",
                                                  "/tmp/other.yml")["patched_by_us"])
        with_ctx = base + ["            contextWindow: 1000000", "        defaultContextWindow: 300000"]
        info = providers.parse_dsh_dump("\n".join(with_ctx), "opencode-xay", "deepseek-v4.1-flash", patch)
        self.assertEqual((info["window"], info["auto_compact_tokens"]), (1_000_000, 800_000))
        default_route = base[:-2] + ["        defaultContextWindow: 300000"]
        self.assertEqual(providers.parse_dsh_dump("\n".join(default_route), "opencode-xay", "deepseek-v4.1-flash")["window"],
                         300_000)
        catalog = self.tmp / "opencode-go.json"                      # dsh 自带目录：一个路由一个文件
        catalog.write_text(json.dumps({"openai-completions": {"chat:deepseek-v4.1-flash": {
            "id": "deepseek-v4.1-flash", "contextWindow": 777777}}}), encoding="utf-8")
        with mock.patch.dict(os.environ, {"XAY_DSH_CATALOG": str(catalog)}):
            self.assertEqual(providers.parse_dsh_dump("\n".join(base), "opencode-go", "deepseek-v4.1-flash")["window"],
                             777777)
        self.assertIsNone(providers.dsh_auto_compact_tokens(262_144, max_output=256_000))   # 输出预留太大：dsh 不主动压
        warn = ('dsh: [/u/cordis.patch.yml] patch: entry "ui-chat" not found\n'
                'dsh: [%s] patch: entry "agent-default-modle" not found\n' % patch)
        self.assertEqual(providers.dsh_patch_warnings(warn, patch), ["agent-default-modle"])   # 用户自己的警告不算

    def test_real_dsh_catalog_if_present_agrees_with_probe(self):
        with mock.patch.dict(os.environ):
            os.environ.pop("XAY_DSH_CATALOG", None)
            found = providers.dsh_catalog_window()
        if found is not None:   # 本机装了 dsh 才有；probes.md 实测 1,000,000
            self.assertEqual(found, 1_000_000)

    def test_compaction_thresholds(self):
        r = providers.compaction_reason
        self.assertIsNone(r("codex", 59.9))
        self.assertEqual(r("codex", 60.0), "threshold")
        self.assertIsNone(r("dsh", 54.9))
        self.assertEqual(r("dsh", 55.0), "threshold")
        self.assertIsNone(r("agy", 49.9, turns_in_segment=2))
        self.assertEqual(r("agy", 50.0), "threshold")
        self.assertEqual(r("agy", 10.0, turns_in_segment=3), "rounds")
        self.assertIsNone(r("codex", 10.0, turns_in_segment=50))        # 只有 agy 按轮数
        self.assertEqual(r("agy", 70.0, midtask=True), "threshold")
        self.assertIsNone(r("agy", 69.0, midtask=True))
        self.assertIsNone(r("codex", 99.0, midtask=True))               # 中途交给 Codex 自己的自动压缩
        self.assertIsNone(r("dsh", 99.0, midtask=True))
        # 绝对上限：dsh 30 万 token，和百分比谁先到算谁
        self.assertEqual(r("dsh", 30.0, used=300_000), "threshold")
        self.assertIsNone(r("dsh", 30.0, used=299_999))
        self.assertIsNone(r("codex", 30.0, used=10_000_000))            # Codex 没有绝对上限
        # 真机冒烟用的环境变量：临时调低门槛
        with mock.patch.dict(os.environ, {"XAY2_COMPACT_AT_DSH": "5", "XAY2_COMPACT_TURNS_AGY": "2"}):
            self.assertEqual(r("dsh", 5.0), "threshold")
            self.assertEqual(r("agy", 1.0, turns_in_segment=2), "rounds")
        self.assertIsNone(r("dsh", 5.0))

    def test_table_values(self):
        self.assertEqual([providers.get(a).steer for a in providers.AGENTS], ["live", "none", "none"])
        self.assertEqual([providers.get(a).native_compaction for a in providers.AGENTS], [True, False, True])
        self.assertEqual([providers.get(a).idle_offline_minutes for a in providers.AGENTS], [15, 0, 10])
        self.assertEqual(providers.get("agy").max_concurrent, 2)
        self.assertIsNone(providers.get("agy").daily_call_cap)   # 2026-10-31 起默认不限次数
        with self.assertRaises(providers.UnknownAgent):
            providers.get("claude")

    def test_codex_lines(self):
        self.assertEqual(providers.codex_provider_for_line("codex-sub"), "openai")
        self.assertEqual(providers.codex_provider_for_line("codex-relay"), "custom")
        for bad in (None, "", "agy", "codex"):
            with self.assertRaises(providers.UnknownLine):
                providers.codex_provider_for_line(bad)

    def test_resolve_command_precedence(self):
        with mock.patch.dict(os.environ, {"XAY_AGY_BIN": "/env/agy"}):
            self.assertEqual(providers.resolve_command("agy"), ["/env/agy"])
            self.assertEqual(providers.resolve_command("agy", {"bin": "/card/agy"}), ["/card/agy"])
            self.assertEqual(providers.resolve_command("agy", {"bin": ["py", "x.py"]}), ["py", "x.py"])
        with mock.patch.dict(os.environ, {"XAY_DSH_BIN": "/env/dsh"}):
            self.assertEqual(providers.resolve_command("dsh"), ["/env/dsh"])   # 自定义整个替换，不再加 --profile

    def test_probe_versions_and_drift(self):
        script = self.tmp / "ver.py"
        script.write_text("import sys\nprint(sys.argv[1] if len(sys.argv) > 1 else '')\n")
        # 每个 AI 一个"打印版本"的假可执行文件
        bins = {}
        for agent, text in (("codex", "codex-cli 0.165.0"), ("dsh", "0.2.1-rc.2")):
            path = self.bin_dir / ("ver-" + agent)
            path.write_text('#!/bin/sh\necho "%s"\n' % text)
            path.chmod(0o755)
            bins[agent] = str(path)
        broken = self.bin_dir / "ver-agy"
        broken.write_text("#!/bin/sh\nexit 3\n")
        broken.chmod(0o755)
        bins["agy"] = str(broken)
        versions = providers.probe_versions(bins=bins, timeout=10)
        self.assertEqual(versions, {"codex": "0.165.0", "dsh": "0.2.1-rc.2", "agy": None})
        self.assertEqual(providers.version_drift(versions), {"agy": ("1.4.11", None)})
        missing = providers.probe_versions(["agy"], bins={"agy": "/nonexistent/agy"})
        self.assertEqual(missing, {"agy": None})
        self.assertEqual(providers.version_drift({"codex": "0.166.0"}), {"codex": ("0.165.0", "0.166.0")})


# ===========================================================================
# 脱敏
# ===========================================================================

class RedactTests(AdapterCase):
    def test_redact_patterns(self):
        cases = {
            "OPENAI_API_KEY=sk-abcdefgh12345678": "OPENAI_API_KEY=<redacted>",
            "use sk-proj-abcdefgh_12345678 now": "use <redacted> now",
            "Authorization: Bearer abc.def.ghi": "Authorization: <redacted>",
            '{"token": "abc123"}': '{"token":"<redacted>"}',
            "ghp_abcdefghijklmnopqrstuvwxyz0123": "<redacted>",
            "-----BEGIN RSA PRIVATE KEY-----\nxx\n-----END RSA PRIVATE KEY-----": "<redacted-private-key>",
        }
        for raw, expected in cases.items():
            self.assertEqual(redact(raw), expected)
        self.assertEqual(redact("task-switching-benchmark"), "task-switching-benchmark")   # 不误伤普通词
        self.assertEqual(redact("session-7afd1e7f-5f18-4792"), "session-7afd1e7f-5f18-4792")

    def test_events_are_redacted_but_ids_are_not(self):
        fake = self.opened(FakeAdapter("codex", [[{"text": "token=abcd1234 and sk-abcdefgh1234"},
                                                   {"tool": "cmd", "text": "export API_KEY=hunter2", "output": "Bearer xyz"}]],
                                        sync=True),
                           card={})
        events = fake.collect(until="session", timeout=2)
        self.assertTrue(events[0].data["native_id"].startswith("fake-codex-"))
        fake.send("hi")
        got = fake.drain()
        blob = json.dumps([e.as_dict() for e in got], ensure_ascii=False)
        for secret in ("abcd1234", "sk-abcdefgh1234", "hunter2", "xyz"):
            self.assertNotIn(secret, blob)
        self.assertIn("<redacted>", blob)


# ===========================================================================
# Codex
# ===========================================================================

class CodexAdapterTests(AdapterCase):
    def setUp(self):
        super().setUp()
        self.codex = make_exec(self.bin_dir, "codex", FIX / "fake_codex.py")

    def adapter(self, line="codex-sub", native_id=None, **extra):
        card = self.card(self.codex, line=line, effort="low", **extra)
        return self.opened(CodexAdapter(rpc_timeout=10, compact_timeout=20), native_id=native_id, card=card)

    def fixture_messages(self, name):
        return [json.loads(l) for l in (FIX / name).read_text(encoding="utf-8").splitlines() if l.strip()]

    # ---- 正常一轮 -------------------------------------------------------

    def test_new_thread_session_usage_and_turn(self):
        a = self.adapter()
        self.assertTrue(a.alive())
        self.assertEqual(a.native_id, "thr-fake-1")
        events = self.run_turn(a, "hello")
        ks = kinds(events)
        self.assertEqual(ks[0], "session")
        self.assertEqual(events[0].data["native_id"], "thr-fake-1")
        self.assertEqual(events[0].data["provider"], "openai")
        self.assertIn("turn_started", ks)
        self.assertEqual("".join(e.data["text"] for e in of(events, "text")), "OK")
        usage = of(events, "usage")[-1].data
        # 27394 = last.totalTokens；窗口 475000 来自通知里的 modelContextWindow
        self.assertEqual((usage["used"], usage["window"], usage["pct"], usage["estimated"], usage["source"]),
                         (27394, 475000, 5.8, False, "codex_last"))
        self.assertEqual((usage["turn_total"], usage["turn_out"], usage["turn_fresh"]), (27394, 5, 18301))
        end = events[-1]
        self.assertEqual(end.data["status"], "completed")
        self.assertEqual(end.data["final_text"], "OK")
        self.assertNotIn("error_kind", end.data)
        self.assertEqual(end.data["usage"]["total"], 27394)
        # 状态：先 working，回到 idle 要在 turn_ended 之前（工位收到 turn_ended 马上发下一轮不会被迟到的 idle 盖住）
        states = [e.data["state"] for e in of(events, "status")]
        self.assertEqual(states[-2:], ["working", "idle"])
        self.assertLess(ks.index("status", len(ks) - 4), ks.index("turn_ended"))
        self.assertEqual(a.state, "idle")

    def test_argv_and_thread_params(self):
        fake_cfg = self.work / "fake_codex_home"
        fake_cfg.mkdir(parents=True, exist_ok=True)
        (fake_cfg / "config.toml").write_text('[mcp_servers.example-erp]\ntype="stdio"\n', encoding="utf-8")
        with mock.patch.dict(os.environ, {"CODEX_HOME": str(fake_cfg)}):
            a = self.adapter(line="codex-sub", net=True)
            argv = self.records()[0]["argv"]
            self.assertEqual(argv[:3], ["app-server", "--listen", "stdio://"])
            for expected in ('model_provider="openai"', 'approval_policy="never"', 'sandbox_mode="workspace-write"',
                             'model_reasoning_effort="low"', 'model_reasoning_summary="detailed"',
                             "mcp_servers.example-erp.enabled=false", "mcp_servers.computer-use.enabled=false",
                             "mcp_servers.node_repl.enabled=false"):
                self.assertIn(expected, argv)
            # 旧驱动传的 .type 覆盖 Codex 会忽略（5 条告警），不再传
            self.assertFalse([x for x in argv if x.startswith("mcp_servers.") and ".type" in x])
            self.assertFalse([x for x in argv if "type=" in x and x.startswith("mcp_servers")])
            start = [r["in"] for r in self.records() if r.get("in", {}).get("method") == "thread/start"][0]["params"]
            self.assertEqual((start["modelProvider"], start["sandbox"], start["approvalPolicy"], start["ephemeral"]),
                             ("openai", "workspace-write", "never", False))
            self.assertEqual(start["cwd"], str(self.work))
            self.run_turn(a, "hello")
            turn = [r["in"] for r in self.records() if r.get("in", {}).get("method") == "turn/start"][0]["params"]
            self.assertEqual(turn["sandboxPolicy"], {"type": "workspaceWrite", "networkAccess": True})
            self.assertEqual((turn["effort"], turn["summary"], turn["approvalPolicy"]), ("low", "detailed", "never"))

        # 测试第 1 条：没有配置时只关两个默认的
        empty_cfg = self.work / "empty_codex_home"
        empty_cfg.mkdir(parents=True, exist_ok=True)
        with mock.patch.dict(os.environ, {"CODEX_HOME": str(empty_cfg)}):
            from xay.adapters.codex import disabled_mcp_servers
            self.assertEqual(disabled_mcp_servers(), ("computer-use", "node_repl"))

    def test_readonly_card_uses_read_only_sandbox(self):
        a = self.adapter(mode="readonly")
        self.assertIn('sandbox_mode="read-only"', self.records()[0]["argv"])
        self.run_turn(a, "look")
        turn = [r["in"] for r in self.records() if r.get("in", {}).get("method") == "turn/start"][0]["params"]
        self.assertEqual(turn["sandboxPolicy"], {"type": "readOnly", "networkAccess": False})
        with self.assertRaises(ValueError):
            CodexAdapter().open(str(self.work), card=self.card(self.codex, line="codex-sub", sandbox="danger-full-access"))

    def test_usage_is_last_not_total_and_tools_are_normalized(self):
        a = self.adapter()
        events = self.run_turn(a, "do things [[tools]]")
        messages = self.fixture_messages("codex_turn_tools.jsonl")
        usages = [m["params"]["tokenUsage"] for m in messages if m["method"] == "thread/tokenUsage/updated"]
        self.assertTrue(any(u["total"]["totalTokens"] != u["last"]["totalTokens"] for u in usages))   # 录像里确实不同
        self.assertEqual([e.data["used"] for e in of(events, "usage")], [u["last"]["totalTokens"] for u in usages])
        self.assertTrue(all(e.data["pct"] < 100 for e in of(events, "usage")))   # 用 total 会超过 100%
        tools = of(events, "tool")
        self.assertEqual({t.data["kind"] for t in tools}, {"cmd", "write"})
        self.assertEqual(tools[0].data["text"], "pwd && rg --files -g 'hello.py' -g '*test*' -g 'pyproject.toml' -g 'pytest.ini' -g 'setup.cfg'")
        self.assertIn("hello.py, test_hello.py", [t.data["text"] for t in tools if t.data["kind"] == "write"])
        outputs = of(events, "tool_output")
        self.assertEqual(len(outputs), len(tools))
        self.assertTrue(all(o.data["ok"] for o in outputs))
        end = events[-1].data
        self.assertTrue(end["final_text"].startswith("已完成 3 步"))    # 取 phase=final_answer 那条，不是中间的 commentary
        self.assertEqual(end["usage"]["total"], sum(u["last"]["totalTokens"] for u in usages))
        # 思考条目摘要是空的（probes.md）-> 不会有 thinking 事件
        self.assertEqual(of(events, "thinking"), [])

    def test_thinking_deltas_and_plan(self):
        a = self.adapter()
        events = self.run_turn(a, "[[reasoning]]")
        self.assertEqual("".join(e.data["text"] for e in of(events, "thinking")), "Thinking about the plan.")
        self.assertEqual(of(events, "plan")[0].data["steps"],
                         [{"text": "write test", "status": "in_progress"}, {"text": "run test", "status": "pending"}])

    def test_extra_context_goes_through_additional_context(self):
        a = self.adapter()
        a.send("task", extra_context="notes here")
        a.collect(until="turn_ended", timeout=15)
        turn = [r["in"] for r in self.records() if r.get("in", {}).get("method") == "turn/start"][0]["params"]
        self.assertEqual(turn["additionalContext"], {"xay": {"kind": "application", "value": "notes here"}})
        self.assertEqual(turn["input"], [{"type": "text", "text": "task"}])    # 不混进任务文字
        a.send("again", extra_context={"notes": "N", "ctx": "C"})
        a.collect(until="turn_ended", timeout=15)
        turn2 = [r["in"] for r in self.records() if r.get("in", {}).get("method") == "turn/start"][1]["params"]
        self.assertEqual(set(turn2["additionalContext"]), {"notes", "ctx"})

    # ---- 错误 -----------------------------------------------------------

    def test_capacity_error_kind(self):
        a = self.adapter()
        events = self.run_turn(a, "[[capacity]]")
        err = of(events, "error")
        self.assertEqual(len(err), 1)
        self.assertEqual(err[0].data["kind"], "capacity")
        self.assertIn("at capacity", err[0].data["text"])
        end = events[-1].data
        self.assertEqual((end["status"], end["error_kind"]), ("failed", "capacity"))
        self.assertEqual(a.state, "idle")
        # 失败之后还能再发下一轮
        events2 = self.run_turn(a, "retry")
        self.assertEqual(events2[-1].data["status"], "completed")

    # ---- 压缩 -----------------------------------------------------------

    def test_compaction_waits_for_its_turn_to_complete(self):
        a = self.adapter(env={"FAKE_LOG": str(self.log), "FAKE_COMPACT_DELAY": "0.6"})
        first = self.run_turn(a, "hello")
        before_pct = of(first, "usage")[-1].data["pct"]
        compact_messages = self.fixture_messages("codex_compaction.jsonl")
        after_used = [m["params"]["tokenUsage"]["last"]["totalTokens"] for m in compact_messages
                      if m["method"] == "thread/tokenUsage/updated"][-1]

        self.assertTrue(a.compact(wait=False))
        self.assertEqual(a.state, "compacting")
        second = {}

        def later():
            try:
                a.send("after compaction")   # 压缩这一轮没结束，send() 必须自己等
                second["sent"] = time.monotonic()
            except Exception as exc:         # pragma: no cover - 失败时让主线程能看到
                second["error"] = exc

        t = threading.Thread(target=later, daemon=True)
        t.start()
        events = a.collect(until="turn_ended", timeout=20)
        t.join(5)
        self.assertNotIn("error", second, second.get("error"))
        ks = kinds(events)
        self.assertEqual(ks.count("turn_ended"), 1)           # 压缩那一轮不算一件活的一轮
        self.assertEqual(ks.count("turn_started"), 1)
        self.assertLess(ks.index("compaction_started"), ks.index("compacted"))
        self.assertLess(ks.index("compacted"), ks.index("turn_started"))   # 先压缩完，下一轮才开始
        compacted = of(events, "compacted")[0].data
        self.assertEqual(compacted["before"], before_pct)
        self.assertEqual(compacted["after_used"], after_used)
        self.assertFalse(compacted["auto"])
        self.assertIn("compacting", [e.data["state"] for e in of(events, "status")])
        # 假进程的账：turn/start 一定在压缩那一轮的 turn/completed 之后收到（否则它会拒绝）
        names = [(r.get("in") or {}).get("method") or r.get("event") for r in self.records()]
        turn_starts = [i for i, n in enumerate(names) if n == "turn/start"]
        self.assertEqual(len(turn_starts), 2)
        self.assertLess(names.index("compact_completing"), turn_starts[1])
        self.assertEqual(events[-1].data["status"], "completed")   # 没有被"ActiveTurnNotSteerable"拒绝

    def test_codex_own_auto_compaction_inside_a_turn_is_reported(self):
        a = self.adapter()
        first = self.run_turn(a, "hello")
        before_pct = of(first, "usage")[-1].data["pct"]
        events = self.run_turn(a, "work [[autocompact]]")
        ks = kinds(events)
        self.assertEqual(ks.count("compaction_started"), 1)
        self.assertTrue(of(events, "compaction_started")[0].data["auto"])
        self.assertLess(ks.index("compaction_started"), ks.index("compacted"))
        compacted = of(events, "compacted")[0].data
        after_used = [m["params"]["tokenUsage"]["last"]["totalTokens"]
                      for m in self.fixture_messages("codex_compaction.jsonl")
                      if m["method"] == "thread/tokenUsage/updated"][-1]
        self.assertEqual((compacted["auto"], compacted["method"], compacted["before"], compacted["after_used"]),
                         (True, "native_auto", before_pct, after_used))
        self.assertEqual(events[-1].data["status"], "completed")     # 这是一件活的一轮，正常结束
        self.assertEqual(ks.count("turn_ended"), 1)
        self.assertEqual(self.run_turn(a, "next")[-1].data["status"], "completed")

    def test_blocking_compact_returns_true_after_completion(self):
        a = self.adapter(env={"FAKE_LOG": str(self.log), "FAKE_COMPACT_DELAY": "0.2"})
        self.run_turn(a, "hello")
        self.assertTrue(a.compact())
        self.assertEqual(a.state, "idle")
        self.assertIn("compacted", kinds(a.drain()))
        events = self.run_turn(a, "next")
        self.assertEqual(events[-1].data["status"], "completed")

    def test_compact_refused_while_turn_active(self):
        a = self.adapter()
        a.send("[[slow]]")
        a.collect(until="turn_started", timeout=10)
        with self.assertRaises(AdapterBusy):
            a.compact()
        with self.assertRaises(AdapterBusy):
            a.send("second")
        a.interrupt()
        a.collect(until="turn_ended", timeout=10)

    # ---- 续接 -----------------------------------------------------------

    def test_resume_keeps_provider_model_and_effort(self):
        # 旧驱动续接时传了空卡片，悄悄切到了中转站 -> 现在无论线路是哪条，续接都保持原样
        for line, provider in (("codex-sub", "openai"), ("codex-relay", "custom")):
            with self.subTest(line=line):
                self.log.unlink() if self.log.exists() else None
                a = self.opened(CodexAdapter(rpc_timeout=10), native_id="thr-old-" + provider,
                                card=self.card(self.codex, line=line, effort="high", model="gpt-keep"))
                argv = self.records()[0]["argv"]
                self.assertIn('model_provider="%s"' % provider, argv)
                self.assertIn('model_reasoning_effort="high"', argv)
                self.assertIn('model="gpt-keep"', argv)
                self.assertNotIn('model_provider="%s"' % ("custom" if provider == "openai" else "openai"), argv)
                resume = [r["in"] for r in self.records() if r.get("in", {}).get("method") == "thread/resume"][0]["params"]
                self.assertEqual((resume["threadId"], resume["modelProvider"], resume["model"], resume["sandbox"]),
                                 ("thr-old-" + provider, provider, "gpt-keep", "workspace-write"))
                self.assertFalse([r for r in self.records() if r.get("in", {}).get("method") == "thread/start"])
                session = a.collect(until="session", timeout=5)[-1].data
                self.assertEqual((session["native_id"], session["provider"], session["model"], session["effort"], session["resumed"]),
                                 ("thr-old-" + provider, provider, "gpt-keep", "high", True))
                self.run_turn(a, "continue")
                turn = [r["in"] for r in self.records() if r.get("in", {}).get("method") == "turn/start"][0]["params"]
                self.assertEqual((turn["effort"], turn["model"]), ("high", "gpt-keep"))
                a.close()

    def test_line_is_required(self):
        a = CodexAdapter()
        with self.assertRaises(providers.UnknownLine):
            a.open(str(self.work), card=self.card(self.codex))     # 没写 line：不许悄悄回落到中转站
        self.assertEqual(self.records(), [])                       # 连进程都没起
        with self.assertRaises(providers.UnknownLine):
            CodexAdapter().open(str(self.work), card=self.card(self.codex, line="agy"))

    def test_resume_error_is_reported(self):
        broken = self.bin_dir / "broken-codex"
        broken.write_text("#!/bin/sh\nexit 2\n")
        broken.chmod(0o755)
        a = CodexAdapter(rpc_timeout=3)
        with self.assertRaises(AdapterError):
            a.open(str(self.work), native_id="x", card={"bin": str(broken), "line": "codex-sub"})
        self.assertFalse(a.alive())

    # ---- 控制 -----------------------------------------------------------

    def test_steer_live_and_noop_when_idle(self):
        a = self.adapter()
        self.assertFalse(a.steer("nothing running"))
        a.send("[[slow]]")
        a.collect(until="turn_started", timeout=10)
        self.assertTrue(a.steer("go left"))
        steer = [r["in"] for r in self.records() if r.get("in", {}).get("method") == "turn/steer"][0]["params"]
        self.assertEqual((steer["expectedTurnId"], steer["input"]), ("turn-fake-1", [{"type": "text", "text": "go left"}]))
        events = a.collect(until="turn_ended", timeout=10)
        self.assertEqual(events[-1].data["status"], "completed")
        self.assertEqual(events[-1].data["final_text"], "working")

    def test_interrupt_is_soft(self):
        a = self.adapter()
        self.assertEqual(a.interrupt(), "soft")     # 没有要打断的也是 soft（什么都没发生）
        a.send("[[slow]]")
        a.collect(until="turn_started", timeout=10)
        self.assertEqual(a.interrupt(), "soft")
        events = a.collect(until="turn_ended", timeout=10)
        self.assertEqual(events[-1].data["status"], "interrupted")
        self.assertTrue(a.alive())                  # soft：进程还在
        self.assertEqual(self.run_turn(a, "again")[-1].data["status"], "completed")

    def test_approval_requests_are_declined(self):
        a = self.adapter()
        events = self.run_turn(a, "[[approval]]")
        answered = [r["in"] for r in self.records() if r.get("in", {}).get("id") == "srv-1"]
        self.assertEqual(answered[0]["result"], {"decision": "decline"})
        self.assertIn("已自动拒绝", of(events, "error")[0].data["text"])

    def test_process_death_mid_turn(self):
        a = self.adapter()
        a.send("[[slow]]")
        a.collect(until="turn_started", timeout=10)
        a._proc.kill()
        events = a.collect(until="status", timeout=10, predicate=lambda e: e.data["state"] == "offline")
        end = of(events, "turn_ended")[0].data
        self.assertEqual((end["status"], end["error_kind"]), ("failed", "other"))
        self.assertTrue(of(events, "error"))
        self.assertFalse(a.alive())
        with self.assertRaises(AdapterError):
            a.send("dead")

    def test_close_terminates_event_stream(self):
        a = self.adapter()
        a.close()
        a.close()   # 可以重复调用
        events = list(a.events(timeout=5))
        self.assertEqual(kinds(events)[-1], "status")
        self.assertEqual(events[-1].data["state"], "offline")
        self.assertFalse(a.alive())
        self.assertEqual(list(a.events(timeout=1)), [])    # 关了之后再迭代立刻结束
        self.assertTrue(wait_for(lambda: any(r.get("event") == "stdin_eof" for r in self.records())))


# ===========================================================================
# dsh
# ===========================================================================

class DshAdapterTests(AdapterCase):
    def setUp(self):
        super().setUp()
        self.dsh = make_exec(self.bin_dir, "dsh", FIX / "fake_dsh.py")
        self.env["FAKE_STATE"] = str(self.tmp)

    def adapter(self, **extra):
        card = self.card(self.dsh, effort="low", **extra)
        return self.opened(DshAdapter(), card=card)

    def test_model_restriction_raises_before_any_process(self):
        for bad in ("deepseek-v4-pro", "gpt-5", "gemini-3.8-flash-low"):
            with self.assertRaises(providers.ModelNotAllowed, msg=bad):
                DshAdapter().open(str(self.work), card=self.card(self.dsh, model=bad))
        self.assertEqual(self.records(), [])                        # 连预检都没跑
        a = self.adapter(model="deepseek-v4.1-flash")
        self.assertEqual(a._model, "deepseek-v4.1-flash")
        with self.assertRaises(providers.ModelNotAllowed):          # 补丁文本那一层也再守一次
            DshAdapter.patch_text("p", "deepseek-v4-pro", "low")
        self.assertEqual([r for r in self.records() if "argv" in r], [])   # 只跑了预检，没起干活的进程

    def test_preflight_refuses_when_our_patch_is_not_applied_or_model_is_wrong(self):
        """dsh 只警告"patch: entry not found"然后照跑，模型限制会悄悄失效 -> 当成错误，拒绝开工。"""
        for env, words in (({"FAKE_DSH_BAD_PATCH": "1"}, "没找到"), ({"FAKE_DSH_MODEL": "deepseek-v4-pro"}, "不是规定的")):
            with mock.patch.dict(os.environ, env):
                with self.assertRaises(AdapterConfigError) as ctx:
                    DshAdapter().open(str(self.work), card=self.card(self.dsh))
            self.assertIn(words, str(ctx.exception))
        self.assertEqual([r for r in self.records() if "argv" in r], [])
        self.assertEqual(len([r for r in self.records() if "dump" in r]), 2)
        a = self.adapter()                                          # 用户自己 profile 的同类警告不算
        self.assertTrue(a.limits["patched_by_us"])
        self.assertEqual((a.limits["window"], a.limits["auto_compact_tokens"]), (262_144, 196_608))
        with mock.patch.dict(os.environ, {"FAKE_DSH_CTX": "1000000"}):
            b = self.adapter()
        self.assertEqual((b._window, b._window_estimated), (1_000_000, False))

    def test_patch_warning_during_a_turn_stops_it_as_config_error(self):
        a = self.adapter()
        events = self.run_turn(a, "[[patch-warn]]")
        end = events[-1].data
        self.assertEqual((end["status"], end["error_kind"]), ("failed", "config"))
        self.assertIn("没找到", end["error"])
        self.assertEqual([e.data["kind"] for e in of(events, "error")], ["config"])

    def test_unknown_session_on_resume_is_resume_failed(self):
        a = self.opened(DshAdapter(), native_id="session-gone-1", card=self.card(self.dsh))
        events = self.run_turn(a, "continue")
        self.assertEqual([e.data["kind"] for e in of(events, "error")], ["resume_failed"])
        end = events[-1].data
        self.assertEqual((end["status"], end["error_kind"]), ("failed", "resume_failed"))
        self.assertIn("was recorded in", end["error"])

    def test_turn_events_session_continuation_and_fixed_cwd(self):
        a = self.adapter()
        self.assertEqual(a.state, "idle")
        events = self.run_turn(a, "hello")
        ks = kinds(events)
        session = of(events, "session")[0].data["native_id"]
        self.assertTrue(session.startswith("session-fake-"))
        self.assertEqual(a.native_id, session)
        self.assertEqual(of(events, "turn_started")[0].data["native_turn"], "1")
        self.assertEqual("".join(e.data["text"] for e in of(events, "text")), "OK")
        usage = of(events, "usage")[0].data
        # totalTokens 20528（inputTokens 只有 4142，不含缓存）；窗口 = dsh 配置里的（路由没写 -> pi-ai 默认 262,144）
        self.assertEqual((usage["used"], usage["window"], usage["pct"], usage["estimated"], usage["source"]),
                         (20528, 262_144, 7.8, False, "dsh_total"))
        self.assertEqual((usage["turn_total"], usage["turn_out"], usage["turn_fresh"]), (20528, 2, 4142))
        end = events[-1].data
        self.assertEqual((end["status"], end["final_text"]), ("completed", "OK"))
        self.assertEqual(end["usage"], {"in": 4142, "out": 2, "cached": 16384, "total": 20528})
        self.assertLess(ks.index("text"), ks.index("turn_ended"))      # turn_end 压到 final 之后
        self.assertEqual(a.state, "idle")

        events2 = self.run_turn(a, "second")
        self.assertEqual(of(events2, "turn_started")[0].data["native_turn"], "2")
        self.assertEqual(of(events2, "session"), [])                  # 同一个会话号不重复报

        runs = [r for r in self.records() if "argv" in r]
        self.assertEqual(len(runs), 2)
        self.assertNotIn("--session-id", runs[0]["argv"])
        self.assertEqual(runs[1]["argv"][runs[1]["argv"].index("--session-id") + 1], session)
        self.assertEqual(runs[0]["cwd"], runs[1]["cwd"])
        self.assertEqual(os.path.realpath(runs[0]["cwd"]), os.path.realpath(self.work))
        self.assertIn("--json", runs[0]["argv"])
        self.assertEqual(runs[0]["role"], "executor")
        for run in runs:
            self.assertIn("model: deepseek-v4.1-flash", run["patch"])
            self.assertIn("reasoningEffort: low", run["patch"])
            self.assertIn("provider: opencode-xay", run["patch"])

    def test_resume_with_native_id_uses_session_id_from_first_turn(self):
        a = self.opened(DshAdapter(), native_id="session-old-1", card=self.card(self.dsh))
        events = a.collect(until="session", timeout=2)
        self.assertEqual(events[0].data, {"native_id": "session-old-1", "resumed": True})
        self.run_turn(a, "continue")
        run = [r for r in self.records() if "argv" in r][0]
        self.assertEqual(run["argv"][run["argv"].index("--session-id") + 1], "session-old-1")

    def test_thinking_and_tools(self):
        a = self.adapter()
        events = self.run_turn(a, "[[think]]")
        self.assertTrue(of(events, "thinking"))
        self.assertIn("compact", of(events, "thinking")[0].data["text"])
        events = self.run_turn(a, "list [[tools]]")
        tool = of(events, "tool")[0].data
        self.assertEqual((tool["kind"], tool["text"]), ("cmd", "ls -la"))
        out = of(events, "tool_output")[0].data
        self.assertTrue(out["ok"])
        self.assertIn("README.md", out["text"])
        self.assertEqual(events[-1].data["final_text"], "目录里有 README.md。")

    def test_capacity_from_stderr_and_nonzero_exit(self):
        a = self.adapter()
        events = self.run_turn(a, "[[capacity]]")
        err = of(events, "error")
        self.assertEqual([e.data["kind"] for e in err], ["capacity"])
        end = events[-1].data
        self.assertEqual((end["status"], end["error_kind"]), ("failed", "capacity"))
        self.assertIn("at capacity", end["error"])
        self.assertEqual(a.state, "idle")

    def test_turn_end_error_reason(self):
        a = self.adapter()
        events = self.run_turn(a, "[[error-event]]")
        end = events[-1].data
        self.assertEqual((end["status"], end["error_kind"], end["error"]), ("failed", "other", "provider returned an error"))
        self.assertEqual(len(of(events, "error")), 1)

    def test_auto_compaction_detected_from_usage_drop(self):
        a = self.adapter()                                # 窗口 262,144
        e1 = self.run_turn(a, "[[total=196000]]")
        self.assertEqual(of(e1, "usage")[0].data["pct"], 74.8)
        self.assertEqual(of(e1, "compacted"), [])
        e2 = self.run_turn(a, "[[total=180000]]")        # 掉了 8%：不算
        self.assertEqual(of(e2, "compacted"), [])
        e3 = self.run_turn(a, "[[total=150000]]")        # 掉了 17%（真实会话里只裁剪工具输出就是这么多）：算
        compacted = of(e3, "compacted")[0].data
        self.assertEqual((compacted["before"], compacted["after"], compacted["auto"]), (68.7, 57.2, True))
        self.assertEqual(compacted["method"], "native_auto")
        self.run_turn(a, "[[total=30000]]")
        e5 = self.run_turn(a, "[[total=20000]]")         # 掉了 33%，但掉之前才 11%：dsh 不会在这里压缩，不算
        self.assertEqual(of(e5, "compacted"), [])

    def test_interrupt_kills_the_turn_process_and_adapter_stays_usable(self):
        a = self.adapter()
        a.interrupt_grace = 1.0
        a.send("[[slow]]")
        a.collect(until="turn_started", timeout=10)
        self.assertEqual(a.interrupt(), "kill")
        events = a.collect(until="turn_ended", timeout=10)
        self.assertEqual(events[-1].data["status"], "interrupted")
        self.assertTrue(a.alive())
        self.assertEqual(self.run_turn(a, "again")[-1].data["status"], "completed")

    def test_unsupported_controls(self):
        a = self.adapter()
        self.assertFalse(a.compact())          # headless /compact 不工作 -> 交接压缩
        self.assertFalse(a.steer("hi"))
        self.assertEqual(a.capabilities.steer, "none")
        self.assertFalse(a.capabilities.native_compact)

    def test_extra_context_is_prepended_to_the_prompt(self):
        a = self.adapter()
        a.send("do it", extra_context="NOTES")
        a.collect(until="turn_ended", timeout=15)
        prompt = [r for r in self.records() if "argv" in r][0]["prompt"]
        self.assertTrue(prompt.startswith("<context>\nNOTES\n</context>"))
        self.assertTrue(prompt.endswith("do it"))

    def test_busy_and_missing_directory(self):
        a = self.adapter()
        a.interrupt_grace = 1.0
        a.send("[[slow]]")
        a.collect(until="turn_started", timeout=10)
        with self.assertRaises(AdapterBusy):
            a.send("second")
        a.interrupt()
        a.collect(until="turn_ended", timeout=10)
        with self.assertRaises(AdapterError):
            DshAdapter().open(str(self.tmp / "gone"), card=self.card(self.dsh))

    def test_close_removes_patch_and_ends_events(self):
        a = self.adapter()
        patch = a._patch_path
        self.assertTrue(os.path.exists(patch))
        a.close()
        self.assertFalse(os.path.exists(patch))
        self.assertEqual(list(a.events(timeout=3))[-1].data, {"state": "offline"})


# ===========================================================================
# agy
# ===========================================================================

class AgyAdapterTests(AdapterCase):
    def setUp(self):
        super().setUp()
        self.agy = make_exec(self.bin_dir, "agy", FIX / "fake_agy.py")

    def adapter(self, native_id=None, **extra):
        card = self.card(self.agy, effort="low", **extra)
        return self.opened(AgyAdapter(), native_id=native_id, card=card)

    def test_stdin_stays_open_across_two_turns(self):
        a = self.adapter()
        e1 = self.run_turn(a, "first")
        e2 = self.run_turn(a, "second")
        records = self.records()
        starts = [r for r in records if "argv" in r]
        stdin = [r for r in records if "stdin" in r]
        self.assertEqual(len(starts), 1)                                   # 只起了一个进程
        self.assertEqual(len(stdin), 2)
        self.assertEqual(stdin[0]["pid"], stdin[1]["pid"])                 # 同一个进程收到两行
        self.assertEqual(stdin[0]["stdin"], {"event": "user", "message": {"role": "user", "content": "first"}})
        self.assertFalse([r for r in records if r.get("event") == "stdin_eof"])   # 两轮之间输入没被关
        self.assertTrue(a.alive())
        self.assertEqual(e1[-1].data["status"], "completed")
        self.assertEqual(e2[-1].data["status"], "completed")
        a.close()
        self.assertTrue(wait_for(lambda: any(r.get("event") == "stdin_eof" for r in self.records())))

    def test_events_session_usage_and_text(self):
        a = self.adapter()
        events = self.run_turn(a, "hello")
        conv = of(events, "session")[0].data["native_id"]
        self.assertTrue(conv.startswith("conv-fake-"))
        self.assertEqual(a.native_id, conv)
        self.assertEqual(of(events, "turn_started")[0].data["native_turn"], "agy-1")
        self.assertEqual("".join(e.data["text"] for e in of(events, "text")), "OK\n")
        usage = of(events, "usage")[0].data
        self.assertEqual((usage["used"], usage["pct"], usage["estimated"], usage["source"]), (12971, 5.1, True, "agy_input"))
        self.assertEqual((usage["turn_total"], usage["turn_out"], usage["turn_fresh"]), (12972, 1, 12971))
        self.assertGreater(usage["window"], 0)
        end = events[-1].data
        self.assertEqual((end["status"], end["final_text"]), ("completed", "OK\n"))
        self.assertEqual(end["usage"]["in"], 12971)
        self.assertEqual(of(events, "thinking"), [])                       # agy 看不到思考
        self.assertEqual(a.state, "idle")

    def test_session_arrives_late_when_init_is_lazy(self):
        a = self.adapter(env={"FAKE_LOG": str(self.log), "FAKE_AGY_INIT": "lazy"})
        self.assertIsNone(a.native_id)                                     # open 不阻塞等 init
        events = self.run_turn(a, "hello")
        self.assertTrue(of(events, "session")[0].data["native_id"].startswith("conv-fake-"))

    def test_settings_file_allows_commands_and_add_dirs_are_kept(self):
        extra = self.tmp / "extra"
        extra.mkdir()
        a = self.adapter(allow_commands=["python3 hello.py", "command(python3 -m unittest)"],
                         add_dirs=[str(extra), str(self.tmp / "other")])
        self.run_turn(a, "go")
        start = [r for r in self.records() if "argv" in r][0]
        argv = start["argv"]
        self.assertEqual(start["settings"], {"permissions": {"allow": ["command(python3 hello.py)",
                                                                       "command(python3 -m unittest)"],
                                                             "deny": ["command(git commit)", "command(git push)"]}})
        self.assertEqual(argv[-1], "-p=")                                  # -p= 必须是最后一个
        self.assertEqual(argv[argv.index("--model") + 1], "gemini-3.8-flash-low")
        self.assertEqual(argv[argv.index("--input-format") + 1], "stream-json")
        self.assertEqual(argv[argv.index("--output-format") + 1], "stream-json")
        self.assertIn("--sandbox", argv)
        self.assertEqual([argv[i + 1] for i, x in enumerate(argv) if x == "--add-dir"], [str(extra), str(self.tmp / "other")])
        self.assertEqual(argv[argv.index("--mode") + 1], "accept-edits")
        self.assertNotIn("--conversation", argv)
        settings_path = argv[argv.index("--settings") + 1]
        a.close()
        self.assertFalse(os.path.exists(settings_path))                    # 临时文件收工就删

    def test_no_settings_flag_without_allow_list_and_readonly_mode(self):
        a = self.adapter(mode="readonly")
        self.run_turn(a, "go")
        argv = [r for r in self.records() if "argv" in r][0]["argv"]
        self.assertNotIn("--settings", argv)
        self.assertEqual(argv[argv.index("--mode") + 1], "plan")
        a2 = self.opened(AgyAdapter(), card=self.card(self.agy, full=True))
        argv2 = self.argv_records(2)[-1]["argv"]
        self.assertIn("--dangerously-skip-permissions", argv2)
        self.assertNotIn("--mode", argv2)
        a2.close()

    def test_model_restriction_raises_before_any_process(self):
        for bad in ("claude-sonnet-4-6", "gpt-oss-120b-medium", "gemini-3.7-flash", "gemini-2.5-pro"):
            with self.assertRaises(providers.ModelNotAllowed, msg=bad):
                AgyAdapter().open(str(self.work), card=self.card(self.agy, model=bad))
        self.assertEqual(self.records(), [])
        a = self.adapter(model="gemini-3.8-flash-high")
        argv = self.argv_records(1)[0]["argv"]
        self.assertEqual(argv[argv.index("--model") + 1], "gemini-3.8-flash-high")
        with self.assertRaises(providers.ModelNotAllowed):                 # 命令行那一层也再守一次
            a._model = "claude-opus-4-6-thinking"
            a.build_argv(None, None)

    def test_cold_resume_passes_conversation_and_confirms_on_first_action(self):
        a = self.adapter(native_id="conv-old-1")
        argv = self.argv_records(1)[0]["argv"]
        self.assertEqual(argv[argv.index("--conversation") + 1], "conv-old-1")
        self.assertLess(argv.index("--conversation"), argv.index("-p="))
        first = a.collect(until="session", timeout=2)[0].data
        self.assertEqual((first["native_id"], first["resumed"]), ("conv-old-1", True))
        events = self.run_turn(a, "continue")
        self.assertEqual([e for e in of(events, "error") if e.data["kind"] == "resume_failed"], [])
        self.assertFalse(a.resume_failed)

    def test_resume_failed_when_cold_turn_ends_without_any_action(self):
        a = self.adapter(native_id="conv-old-2")
        events = self.run_turn(a, "[[idle]]")                              # 旧系统的"空转就结束"
        errors = of(events, "error")
        self.assertEqual([e.data["kind"] for e in errors], ["resume_failed"])
        self.assertTrue(a.resume_failed)

    def test_resume_failed_after_timeout_with_no_tool_or_text(self):
        a = self.adapter(native_id="conv-old-3", resume_timeout_s=0.4)
        a.send("[[silent]]")
        events = a.collect(until="error", timeout=5)
        self.assertEqual(events[-1].data["kind"], "resume_failed")
        self.assertEqual(of(events, "turn_ended"), [])                     # 进程还在跑，是否放弃由工位决定
        self.assertTrue(a.alive())
        self.assertEqual(a.interrupt(), "kill")
        end = a.collect(until="turn_ended", timeout=10)[-1].data
        self.assertEqual(end["status"], "interrupted")

    def test_no_resume_failed_for_a_new_conversation_or_after_confirmation(self):
        a = self.adapter()
        events = self.run_turn(a, "[[idle]]")
        self.assertEqual(of(events, "error"), [])                          # 不是冷续接，空回复不算续接失败
        b = self.opened(AgyAdapter(), native_id="conv-old-4", card=self.card(self.agy, effort="low", resume_timeout_s=0.3))
        self.run_turn(b, "hello")                                          # 有文字 -> 续接成功
        time.sleep(0.5)
        b.send("[[silent]]")
        time.sleep(0.6)                                                    # 已经确认过，之后沉默不再算续接失败
        self.assertEqual([e for e in b.drain() if e.kind == "error"], [])

    def test_tool_turn_normalization_and_usage_semantics(self):
        a = self.adapter()
        events = self.run_turn(a, "run it [[tools]]")
        tools = of(events, "tool")
        self.assertEqual([(t.data["kind"], t.data["text"]) for t in tools],
                         [("cmd", "python3 hello.py"), ("cmd", "python3 hello.py")])   # ACTIVE 才发 tool，DONE 只补输出
        outs = of(events, "tool_output")
        self.assertEqual([o.data["ok"] for o in outs], [False, True])   # "Command exited with code 1" 判失败
        self.assertIn("exited with code 1", outs[0].data["text"])
        used = [e.data["used"] for e in of(events, "usage")]
        self.assertEqual(used, [12974, 13481, 13622])                    # 记忆条跟着最近一步的 input_tokens
        end = events[-1].data
        self.assertEqual(end["usage"]["in"], 40077)                      # result.usage 是整轮合计，只放 turn_ended
        self.assertEqual(end["final_text"], "HELLO-00b2db22\n")

    def test_usage_override_for_memory_meter(self):
        a = self.adapter()
        events = self.run_turn(a, "[[total=600000]]")
        usage = of(events, "usage")[0].data
        self.assertEqual(usage["used"], 600000)
        self.assertEqual(providers.compaction_reason("agy", usage["pct"]), "threshold")

    def test_quota_and_capacity_errors_are_recognised(self):
        a = self.adapter()
        quota = self.run_turn(a, "[[quota]]")
        self.assertEqual([e.data["kind"] for e in of(quota, "error")], ["quota"])
        self.assertEqual((quota[-1].data["status"], quota[-1].data["error_kind"]), ("failed", "quota"))
        overload = self.run_turn(a, "[[overload]]")
        self.assertEqual([e.data["kind"] for e in of(overload, "error")], ["capacity"])
        self.assertEqual(overload[-1].data["error_kind"], "capacity")
        self.assertEqual(self.run_turn(a, "fine")[-1].data["status"], "completed")   # 同一个进程还能继续

    def test_changing_allow_commands_restarts_with_the_same_conversation(self):
        a = self.adapter(allow_commands=["python3 a.py"])
        e1 = self.run_turn(a, "one")
        conv = a.native_id
        self.assertFalse(a.set_allow_commands(["python3 a.py"]))           # 没变：不重启
        self.run_turn(a, "two")
        self.assertEqual(len([r for r in self.records() if "argv" in r]), 1)
        self.assertTrue(a.set_allow_commands(["python3 a.py", "make test"]))
        events = self.run_turn(a, "three")
        starts = [r for r in self.records() if "argv" in r]
        self.assertEqual(len(starts), 2)
        self.assertEqual(starts[1]["argv"][starts[1]["argv"].index("--conversation") + 1], conv)
        self.assertEqual(starts[1]["settings"]["permissions"]["allow"], ["command(python3 a.py)", "command(make test)"])
        self.assertTrue(a.set_allow_commands(["python3 a.py", "make test"], mode="readonly"))   # 只读 / 可写变了也要重启
        self.run_turn(a, "four")
        starts = [r for r in self.records() if "argv" in r]
        self.assertEqual(starts[2]["argv"][starts[2]["argv"].index("--mode") + 1], "plan")
        self.assertEqual(starts[2]["argv"][starts[2]["argv"].index("--conversation") + 1], conv)
        self.assertNotEqual(starts[0]["pid"], starts[1]["pid"])
        self.assertEqual(events[-1].data["status"], "completed")
        self.assertEqual([e for e in of(events, "error")], [])             # 旧进程的退出不能被当成意外
        self.assertEqual([e for e in of(events, "turn_ended") if e.data["status"] != "completed"], [])

    def test_interrupt_kills_the_process(self):
        a = self.adapter()
        a.interrupt_grace = 1.0
        a.send("[[silent]]")
        a.collect(until="turn_started", timeout=1) if False else time.sleep(0.3)
        self.assertEqual(a.interrupt(), "kill")
        events = a.collect(until="status", timeout=10, predicate=lambda e: e.data["state"] == "offline")
        self.assertEqual(of(events, "turn_ended")[0].data["status"], "interrupted")
        self.assertFalse(a.alive())
        with self.assertRaises(AdapterError):
            a.send("dead")

    def test_busy_and_not_open(self):
        a = self.adapter()
        a.send("[[silent]]")
        with self.assertRaises(AdapterBusy):
            a.send("second")
        with self.assertRaises(AdapterError):
            AgyAdapter().send("not open")
        with self.assertRaises(AdapterBusy):                               # 正在做一轮：不能压缩
            a.compact()
        self.assertEqual(a.steer("x"), False)

    def test_native_compact_waits_for_checkpoint_and_is_not_a_turn(self):
        """M3 实测：/compact 出 checkpoint 步骤 + SUCCESS 的空 result；这一行不算一轮。"""
        a = self.adapter()
        self.run_turn(a, "[[total=150000]]")
        self.assertTrue(a.compact(timeout=10))
        events = a.drain()
        self.assertEqual(of(events, "turn_started") + of(events, "turn_ended"), [])
        compacted = of(events, "compacted")[0].data
        self.assertEqual((compacted["auto"], compacted["method"], compacted["before"]), (False, "native", 58.6))
        self.assertNotIn("after", compacted)                               # 压完多少要等下一轮第一步
        self.assertEqual([e.data["state"] for e in of(events, "status")], ["compacting", "idle"])
        stdin = [r["stdin"]["message"]["content"] for r in self.records() if "stdin" in r]
        self.assertEqual(stdin[-1], "/compact")
        self.assertEqual(self.run_turn(a, "after")[-1].data["status"], "completed")   # 同一个进程接着干
        with mock.patch.dict(os.environ, {"FAKE_AGY_COMPACT": "fail"}):
            b = self.adapter()
        self.assertFalse(b.compact(timeout=10))                            # 没有 checkpoint -> 改走交接

    def test_memory_meter_counts_cache_reads(self):
        """agy 的 input_tokens 只算新内容，缓存读取另算：已用 = 两者相加；窗口 256,000。"""
        a = self.adapter()
        events = self.run_turn(a, "[[total=2467]] [[cache=134149]]")
        usage = of(events, "usage")[0].data
        self.assertEqual((usage["used"], usage["window"], usage["pct"]), (136616, 256_000, 53.4))
        self.assertEqual(providers.compaction_reason("agy", usage["pct"]), "threshold")   # 50% 门槛真的能触发了

    def test_cold_resume_that_opens_another_conversation_is_resume_failed(self):
        a = self.adapter(native_id="conv-old-9", env={"FAKE_LOG": str(self.log), "FAKE_AGY_NEW_CONV": "1"})
        errors = a.collect(until="error", timeout=5)
        self.assertEqual(errors[-1].data["kind"], "resume_failed")
        session = [e.data for e in of(errors, "session")]
        self.assertEqual(session[-1]["previous"], "conv-old-9")

    def test_full_mode_still_denies_git_commit_and_push(self):
        a = self.opened(AgyAdapter(), card=self.card(self.agy, full=True))
        start = self.argv_records(1)[0]
        self.assertIn("--dangerously-skip-permissions", start["argv"])
        self.assertEqual(start["settings"]["permissions"]["deny"], ["command(git commit)", "command(git push)"])
        a.close()

    def test_env_override_for_binary(self):
        with mock.patch.dict(os.environ, {"XAY_AGY_BIN": self.agy}):
            a = self.opened(AgyAdapter(), card={"effort": "low", "env": dict(self.env)})
            self.assertEqual(self.run_turn(a, "hi")[-1].data["status"], "completed")


# ===========================================================================
# 假适配器
# ===========================================================================

class FakeAdapterTests(AdapterCase):
    def test_scripted_turn_events(self):
        script = [[{"thinking": "hmm"}, {"text": "he"}, {"text": "llo"},
                   {"tool": "cmd", "text": "ls", "output": "a\nb", "ok": True},
                   {"plan": [("one", "in_progress"), ("two", "pending")]},
                   {"usage": {"used": 1000, "window": 10000}}]]
        fake = self.opened(FakeAdapter("codex", script, sync=True), card={})
        fake.send("go", extra_context="ctx")
        events = fake.collect(until="turn_ended", timeout=2)
        ks = kinds(events)
        self.assertEqual(ks[0], "session")
        self.assertEqual(ks[1], "status")
        self.assertEqual(ks[-1], "turn_ended")
        self.assertEqual("".join(e.data["text"] for e in of(events, "text")), "hello")
        self.assertEqual(of(events, "usage")[0].data["pct"], 10.0)
        self.assertEqual(events[-1].data, {"status": "completed", "final_text": "hello"})
        self.assertEqual(fake.sent, [("go", "ctx")])
        self.assertEqual(of(events, "plan")[0].data["steps"][0], {"text": "one", "status": "in_progress"})

    def test_threaded_mode_hold_release_steer_interrupt(self):
        fake = self.opened(FakeAdapter("codex", [[{"hold": True}, {"text": "done"}], [{"hold": True}]]), card={})
        fake.send("a")
        fake.collect(until="turn_started", timeout=2)
        with self.assertRaises(AdapterBusy):
            fake.send("b")
        self.assertTrue(fake.steer("left"))
        fake.release()
        self.assertEqual(fake.collect(until="turn_ended", timeout=2)[-1].data["final_text"], "done")
        fake.send("c")
        fake.collect(until="turn_started", timeout=2)
        self.assertEqual(fake.interrupt(), "soft")
        self.assertEqual(fake.collect(until="turn_ended", timeout=2)[-1].data["status"], "interrupted")
        self.assertEqual(fake.steered, ["left"])

    def test_failures_and_fail_next(self):
        fake = self.opened(FakeAdapter("agy", [[{"fail": {"kind": "quota", "error": "no more"}}]], sync=True), card={})
        fake.send("x")
        events = fake.collect(until="turn_ended", timeout=2)
        self.assertEqual(of(events, "error")[0].data, {"kind": "quota", "text": "no more"})
        self.assertEqual((events[-1].data["status"], events[-1].data["error_kind"]), ("failed", "quota"))
        fake.fail_next("capacity")
        fake.send("y")
        events = fake.collect(until="turn_ended", timeout=2)
        self.assertEqual(events[-1].data["error_kind"], "capacity")
        fake.send("z")   # script 用完 -> 默认回复 OK
        self.assertEqual(fake.collect(until="turn_ended", timeout=2)[-1].data["final_text"], "OK")

    def test_memory_meter_and_native_compaction_follow_the_provider_table(self):
        codex = self.opened(FakeAdapter("codex", sync=True), card={})
        codex.inject_usage(pct=80)
        usage = codex.drain()[-1]
        self.assertEqual((usage.kind, usage.data["pct"]), ("usage", 80.0))
        self.assertTrue(codex.compact())
        events = codex.drain()
        self.assertEqual([e.data["state"] for e in of(events, "status")], ["compacting", "idle"])
        compacted = of(events, "compacted")[0].data
        self.assertEqual((compacted["before"], compacted["after"]), (80.0, 16.0))
        self.assertEqual(codex.last_usage["pct"], 16.0)
        dsh = self.opened(FakeAdapter("dsh", sync=True), card={})
        dsh.inject_usage(pct=60)
        self.assertFalse(dsh.compact())                              # dsh 没有可调的原生压缩 -> 工位走交接压缩
        self.assertFalse(dsh.steer("x"))
        agy_fake = self.opened(FakeAdapter("agy", sync=True), card={})
        agy_fake.inject_usage(pct=60)
        self.assertTrue(agy_fake.compact())                          # M3 实测：agy 的 /compact 好用 -> 先原生
        self.assertFalse(agy_fake.steer("x"))
        agy = FakeAdapter("agy")
        self.assertTrue(agy.provider.max_concurrent == 2)

    def test_model_restrictions_are_enforced_like_the_real_adapters(self):
        with self.assertRaises(providers.ModelNotAllowed):
            FakeAdapter("agy").open(str(self.work), card={"model": "claude-sonnet-4-6"})
        with self.assertRaises(providers.ModelNotAllowed):
            FakeAdapter("dsh").open(str(self.work), card={"model": "deepseek-v4-pro"})
        FakeAdapter("dsh").open(str(self.work), card={"model": "deepseek-v4.1-flash"})

    def test_resume_die_and_open_error(self):
        fake = self.opened(FakeAdapter("agy", sync=True, fail_resume=True), native_id="conv-x", card={})
        self.assertEqual(fake.native_id, "conv-x")
        fake.send("hi")
        self.assertIn("resume_failed", [e.data["kind"] for e in of(fake.drain(), "error")])
        dying = self.opened(FakeAdapter("codex", [[{"text": "partial"}, {"die": True}]], sync=True), card={})
        dying.send("x")
        events = dying.drain()
        self.assertEqual(events[-1].data, {"state": "offline"})
        self.assertFalse(dying.alive())
        with self.assertRaises(AdapterError):
            dying.send("again")
        with self.assertRaises(AdapterError):
            FakeAdapter("codex", open_error="cannot start").open(str(self.work))

    def test_sync_mode_rejects_blocking_steps_and_end_step(self):
        fake = self.opened(FakeAdapter("codex", [[{"end": {"status": "failed", "error": "bad", "final_text": "x"}}]], sync=True), card={})
        fake.send("x")
        end = fake.collect(until="turn_ended", timeout=2)[-1].data
        self.assertEqual((end["status"], end["final_text"], end["error"]), ("failed", "x", "bad"))
        with self.assertRaises(ValueError):
            fake2 = self.opened(FakeAdapter("codex", [[{"hold": True}]], sync=True), card={})
            fake2.send("y")

    def test_make_adapter_factory(self):
        self.assertIsInstance(make_adapter("codex"), CodexAdapter)
        self.assertIsInstance(make_adapter("dsh"), DshAdapter)
        self.assertIsInstance(make_adapter("agy"), AgyAdapter)
        with self.assertRaises(ValueError):
            make_adapter("claude")


if __name__ == "__main__":
    unittest.main()
