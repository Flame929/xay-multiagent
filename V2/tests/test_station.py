"""工位（V2/xay/station）测试：临时 XAY2_HOME + 临时 git 仓库 + 假适配器，不起任何真 CLI。"""
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

_v2 = str(Path(__file__).resolve().parents[1])
if _v2 not in sys.path:
    sys.path.insert(0, _v2)

from xay.adapters.fake import FakeAdapter
from xay.core import append, load_state, open_db, since
from xay.core.ids import new_id, ws_for
from xay.core.snapshot import build
from xay.station import markers, memo
from xay.station import station as station_mod
from xay.station.station import Station

V2 = Path(__file__).resolve().parents[1]
GIT_ENV = {"GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t", "GIT_COMMITTER_NAME": "t",
           "GIT_COMMITTER_EMAIL": "t@t", "GIT_CONFIG_GLOBAL": os.devnull, "GIT_CONFIG_NOSYSTEM": "1"}
CLEAR_ENV = ("XAY2_FAKE_ADAPTER", "XAY_ROLE", "XAY2_IDLE_SECONDS", "XAY2_HEARTBEAT_S", "XAY2_TASK", "XAY2_TASK_FILE",
             "XAY2_OUTBOX", "XAY2_CONV", "XAY2_EM", "XAY2_HEARTBEAT_TIMEOUT", "XAY2_AGY_DAILY_CAP", "XAY_DSH_WINDOW",
             "XAY2_COMPACT_AT_DSH", "XAY2_COMPACT_AT_AGY", "XAY2_COMPACT_AT_CODEX", "XAY2_COMPACT_TURNS_AGY",
             "XAY2_COMPACT_TOKENS_DSH", "XAY_AGY_TOOLS_HOME")   # XAY2_PLAIN_CHECK 不能放这里：setUp 先设好再按这个名单清空
for _k in CLEAR_ENV:
    os.environ.pop(_k, None)
MEMO = "\n".join(["## 1. 目标", "- 给项目加 a、b 两个文件", "## 2. 已经定下来的事", "- a.txt 已合并",
                  "## 3. 改过的文件", "- a.txt:1", "## 4. 验收命令和最近一次的结果", "- cat a.txt → A",
                  "## 5. 踩过的坑和解决办法", "- 无", "## 6. 还没做完的和下一步", "- 加 b.txt",
                  "## 7. 不要重复做的事", "- 不要再改 a.txt"])


def make_repo(root):
    root.mkdir(parents=True)
    run = lambda *a: subprocess.run(["git", *a], cwd=str(root), check=True, capture_output=True)
    run("init", "-q", "-b", "main", ".")
    (root / "README.md").write_text("hello\n")
    run("add", "-A")
    run("-c", "commit.gpgsign=false", "commit", "-q", "-m", "init")
    return root


def writes(name, content, steps):
    """假员工这一轮：先在桌子里写一个文件，再按 steps 发事件。"""
    def item(a, text, extra):
        p = Path(a.cwd) / name
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content)
        return steps
    return item


class Factory:
    """每次工位要开原生对话就造一个新的假适配器；scripts[i] 是第 i 个适配器的轮脚本。"""

    def __init__(self, scripts=None, native_compact=None, agent=None, adapter_cls=FakeAdapter, **fake_kw):
        self.scripts = list(scripts or [])
        self.native_compact = native_compact
        self.agent = agent
        self.adapter_cls = adapter_cls
        self.fake_kw = fake_kw
        self.instances = []

    def __call__(self, agent):
        a = self.adapter_cls(self.agent or agent, **self.fake_kw)
        if self.native_compact is not None:
            a.capabilities.native_compact = self.native_compact
        script = self.scripts.pop(0) if self.scripts else []
        a.script = [self._bind(a, x) for x in script]
        self.instances.append(a)
        return a

    @staticmethod
    def _bind(a, item):
        if callable(item):
            return lambda text, extra: item(a, text, extra)
        return item


class StationCase(unittest.TestCase):
    agent = "codex"

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="xay-station-test-"))
        self.addCleanup(shutil.rmtree, self.tmp, True)
        # 旧系统账本（agy 每天调用次数合计）和 Antigravity Tools 都指到测试目录里，绝不碰本机真的
        patcher = mock.patch.dict(os.environ, dict(GIT_ENV, XAY2_HOME=str(self.tmp / "v2"), XAY_HOME=str(self.tmp / "oldxay"),
                                                   XAY_AGY_TOOLS_BIN=str(self.tmp / "no-antigravity-tools"),
                                                   XAY2_PLAIN_CHECK="0"))   # 这些老用例写在"大白话把关"之前；把关单独在 test_plain_report.py 里测
        patcher.start()
        self.addCleanup(patcher.stop)
        for k in CLEAR_ENV:
            os.environ.pop(k, None)
        self.repo = make_repo(self.tmp / "proj")
        self.conn = open_db()
        self.addCleanup(self.conn.close)
        self.ws, self.conv = ws_for(self.repo), "cv_test0001"
        self.lead = "lead:" + self.conv
        append(self.conn, "conv.begun", self.lead, {"title": "测试", "lead_kind": "claude", "aliases": [],
                                                    "cwd": str(self.repo)}, ws=self.ws, conv=self.conv, subject=self.conv)
        self.em = "em_aaaa0001"
        append(self.conn, "employee.hired", self.lead, {"agent": self.agent}, ws=self.ws, conv=self.conv, subject=self.em)
        self.desk = self.tmp / "v2" / "desks" / self.ws / self.em

    # ---- helpers

    def add_task(self, title, card="", mode="write", queue=True):
        tk = new_id("tk_")
        append(self.conn, "task.added", self.lead, {"title": title, "card": card, "deps": [], "writes": [], "mode": mode,
                                                     "reason": "测试", "owner": self.em}, ws=self.ws, conv=self.conv, subject=tk)
        if queue:
            self.queue(tk)
        return tk

    def queue(self, tk):
        append(self.conn, "task.ready", "office", {}, ws=self.ws, conv=self.conv, subject=tk)
        n = len(load_state(self.conn)["employees"][self.em]["queue"]) + 1
        append(self.conn, "task.queued", "office", {"owner": self.em, "position": n}, ws=self.ws, conv=self.conv, subject=tk)

    def station(self, factory, **kw):
        kw.setdefault("poll_s", 0.02)
        return Station(self.em, adapter_factory=factory, **kw)

    def run_station(self, factory, **kw):
        st = self.station(factory, **kw)
        rc = st.run(once=True)
        self.assertEqual(rc, 0)
        self.assertEqual(st.rejected_writes, [])
        return st

    def events(self, typ=None, **match):
        out = []
        for e in since(self.conn, 0, limit=100000):
            if typ and e["type"] != typ:
                continue
            if all(e["data"].get(k) == v for k, v in match.items()):
                out.append(e)
        return out

    def state(self):
        return load_state(self.conn)

    def wait_for(self, fn, timeout=10.0):
        deadline = time.time() + timeout
        while time.time() < deadline:
            v = fn()
            if v:
                return v
            time.sleep(0.02)
        self.fail("等了 %.0f 秒还没等到" % timeout)


class SameConversationTests(StationCase):
    def test_three_related_tasks_reuse_one_native_session_and_land_each_time(self):
        f = Factory([[writes("a.txt", "A\n", [{"thinking": "先看看项目结构\n"}, {"text": "加好了\n【结果】完成：加了 a.txt"}]),
                      writes("b.txt", "B\n", [{"text": "【结果】完成：加了 b.txt"}]),
                      writes("c.txt", "C\n", [{"text": "【结果】完成：加了 c.txt"}])]])
        tks = [self.add_task("加 a 文件"), self.add_task("加 b 文件"), self.add_task("加 c 文件")]
        self.run_station(f)

        self.assertEqual(len(f.instances), 1)                       # 一直是同一段原生对话
        native = f.instances[0].native_id
        sessions = self.events("employee.session")
        self.assertEqual([(e["data"]["native_id"], e["data"]["reason"], e["data"]["segment"]) for e in sessions],
                         [(native, "new", 1)])
        self.assertEqual(len(f.instances[0].sent), 3)
        s = self.state()
        landed = self.events("task.landed")
        self.assertEqual([e["subject"] for e in landed], tks)
        self.assertEqual([e["data"]["files"] for e in landed], [["a.txt"], ["b.txt"], ["c.txt"]])  # 每件活只带自己的改动
        for tk, name, text in zip(tks, "abc", "ABC"):
            self.assertEqual(s["tasks"][tk]["status"], "reported")
            self.assertEqual((self.repo / ("%s.txt" % name)).read_text(), text + "\n")   # 放回项目（未提交）
        self.assertTrue(self.desk.is_dir())
        kinds = [e["data"]["kind"] for e in self.events("turn.started")]
        self.assertEqual(kinds, ["first", "continue", "continue"])
        # 任务卡带上了标记用法，执行者环境按 §6
        text, _extra = f.instances[0].sent[0]
        self.assertIn("【任务】加 a 文件（编号 %s）" % tks[0], text)
        self.assertIn("【进度 2/4】", text)
        env = f.instances[0].card["env"]
        self.assertEqual((env["XAY_ROLE"], env["XAY2_EM"], env["XAY2_CONV"]), ("executor", self.em, self.conv))
        self.assertEqual(env["XAY2_OUTBOX"], str(self.desk / ".xay" / "out" / "outbox.jsonl"))
        self.assertEqual(f.instances[0].card["line"], "codex-sub")
        # 最近一句思考：写过一条 employee.thought，一轮结束后快照里清空
        self.assertEqual([e["data"]["text"] for e in self.events("employee.thought")], ["先看看项目结构"])
        snap = build(self.conn, conv=self.conv)
        self.assertEqual(snap["people"][self.em]["thought"], "")
        self.assertEqual(snap["people"][self.em]["state"], "offline")
        # 直播通道、工作笔记、上下线
        live = [json.loads(x) for x in (self.tmp / "v2" / "live" / self.em / "1.jsonl").read_text().splitlines()]
        self.assertIn("thinking", [x["kind"] for x in live])
        self.assertEqual([x["n"] for x in live], list(range(1, len(live) + 1)))
        notes = s["notes"][self.em]["active"]
        for title in ("加 a 文件", "加 b 文件", "加 c 文件"):
            self.assertIn(title, notes)
        self.assertEqual(self.events("employee.offline")[-1]["data"]["reason"], "idle")
        self.assertEqual([e["data"]["claim"] for e in self.events("task.reported")], ["done"] * 3)

    def test_thought_shows_in_snapshot_while_working(self):
        f = Factory([[[{"thinking": "**想想怎么拆**\n先读 README"}, {"hold": True}, {"text": "【结果】完成：好"}]]])
        self.add_task("读一下")
        st = self.station(f)
        th = threading.Thread(target=st.run, kwargs={"once": True}, daemon=True)
        th.start()
        self.wait_for(lambda: self.events("employee.thought"))
        snap = build(self.conn, conv=self.conv)
        self.assertEqual(snap["people"][self.em]["thought"], "先读 README")
        self.assertEqual(snap["people"][self.em]["state"], "working")
        self.wait_for(lambda: f.instances)
        f.instances[0].release()
        th.join(10)
        self.assertFalse(th.is_alive())


class VersionTests(StationCase):
    def test_cli_version_recorded_and_drift_raises_reminder(self):
        f = Factory([[writes("v.txt", "v", [{"text": "【结果】完成：v"}])]])
        self.add_task("看版本")
        self.run_station(f, version_probe=lambda agent: "0.166.0")
        ses = self.events("employee.session")[-1]["data"]
        self.assertEqual(ses["cli_version"], "0.166.0")
        self.assertEqual(f.instances[0].capabilities.cli_version, "0.166.0")
        sig = self.events("signal.raised", kind="cli_version_changed")
        self.assertEqual([(e["data"]["level"]) for e in sig], ["remind"])
        self.assertIn("0.165.0", sig[0]["data"]["text"])
        self.run_station(Factory(), version_probe=lambda agent: "0.166.0")      # 同一个版本只提醒一次
        self.assertEqual(len(self.events("signal.raised", kind="cli_version_changed")), 1)

    def test_codex_auto_compaction_pinned_near_90_percent(self):
        from xay.adapters.codex import CodexAdapter
        cfg = CodexAdapter.resolve_config({"line": "codex-sub"})
        self.assertEqual(cfg["auto_compact_limit"], int(0.9 * 475000))
        a = CodexAdapter()
        a._cfg = cfg
        self.assertIn("model_auto_compact_token_limit=427500", a._config_args())
        a._cfg = CodexAdapter.resolve_config({"line": "codex-sub", "auto_compact_limit": 0})
        self.assertFalse([x for x in a._config_args() if "auto_compact" in x])


class NoResumeFake(FakeAdapter):
    """换了线路就接不上原来的 thread（协议报错）。"""

    def open(self, cwd, native_id=None, card=None):
        if native_id:
            from xay.adapters.base import AdapterError
            raise AdapterError("thread not found for this provider")
        super().open(cwd, native_id=native_id, card=card)


class QuotaReportTests(StationCase):
    """详情栏第 3 版：适配器报的 Codex 额度（rate_limits）由工位写成 quota.updated；变化不到 1 个百分点不重复写。"""

    def test_rate_limits_become_quota_updated_and_small_changes_are_skipped(self):
        def turn(a, text, extra):
            for used in (10, 10.4, 12):
                a.emit("rate_limits", line="codex-sub",
                       windows=[{"key": "5h", "mins": 300, "used_pct": used, "resets_at": 1793363679},
                                {"key": "week", "mins": 10080, "used_pct": 64, "resets_at": 1793778606}])
            Path(a.cwd, "q.txt").write_text("q\n")
            return [{"text": "【结果】完成：q"}]
        tk = self.add_task("看额度")
        self.run_station(Factory([[turn]]))
        got = self.events("quota.updated")
        self.assertEqual([[w["used_pct"] for w in e["data"]["windows"]] for e in got], [[10, 64], [12, 64]])
        self.assertEqual({(e["subject"], e["by"], e["conv"], e["data"]["line"]) for e in got}, {(self.em, "station:" + self.em, self.conv, "codex-sub")})
        q = build(self.conn, conv=self.conv, now=1793360000)["quota"]
        self.assertEqual([(x["line"], x["remaining_pct"]) for x in q if x["remaining_pct"] is not None], [("codex-sub", 36)])
        self.assertEqual(self.state()["tasks"][tk]["status"], "reported")


class LineSwitchTests(StationCase):
    CAP = [{"fail": {"kind": "capacity", "error": "Selected model is at capacity"}}]

    def sessions(self):
        return [(e["data"]["native_id"], e["data"]["segment"], e["data"]["reason"], e["data"].get("line"))
                for e in self.events("employee.session")]

    def test_capacity_on_plus_switches_to_relay_and_resumes_same_thread(self):
        f = Factory([[self.CAP], [writes("r.txt", "r\n", [{"text": "【结果】完成：r"}]),
                                  writes("n.txt", "n\n", [{"text": "【结果】完成：n"}])]])
        tk, t2 = self.add_task("满载也要做完"), self.add_task("下一件")
        self.run_station(f)
        a1, a2 = f.instances
        self.assertEqual((a1.card["line"], a2.card["line"], a2.card.get("line_switch")), ("codex-sub", "codex-relay", True))
        self.assertEqual(a2.native_id, a1.native_id)                    # 同一个 thread
        self.assertEqual(self.sessions(), [(a1.native_id, 1, "new", None), (a1.native_id, 1, "line_switch", "codex-relay")])
        self.assertEqual(self.events("task.failed"), [])                # 第一次不记失败
        self.assertEqual([e["data"]["status"] for e in self.events("turn.ended")], ["failed", "completed", "completed"])
        self.assertEqual([e["data"]["kind"] for e in self.events("turn.started")], ["first", "continue", "continue"])
        self.assertEqual([e["subject"] for e in self.events("task.landed")], [tk, t2])
        self.assertTrue(a2.sent[0][0].startswith("（上一次因为额度线路满载"))
        sig = self.events("signal.raised", kind="quota")
        self.assertEqual([(e["data"]["level"], e["data"]["text"]) for e in sig], [("remind", "Codex Plus 满载，已换中转站接着做")])
        self.assertEqual(self.state()["employees"][self.em]["line"], "codex-relay")
        # 下班后再来活：留在中转站
        self.add_task("第三件")
        g = Factory([[writes("t.txt", "t", [{"text": "【结果】完成：t"}])]])
        self.run_station(g)
        self.assertEqual(g.instances[0].card["line"], "codex-relay")
        self.assertEqual(len(self.events("employee.session")), 2)

    def test_relay_also_fails_then_task_failed_and_only_one_switch(self):
        f = Factory([[self.CAP], [self.CAP]])
        tk = self.add_task("两条线路都满")
        self.run_station(f)
        self.assertEqual(len(f.instances), 2)
        self.assertEqual([e["data"]["reason"] for e in self.events("employee.session")], ["new", "line_switch"])
        self.assertEqual([e["subject"] for e in self.events("task.failed")], [tk])
        self.assertEqual(self.state()["tasks"][tk]["status"], "failed")

    def test_relay_cannot_resume_thread_starts_new_one_with_notes(self):
        append(self.conn, "notes.updated", "station:" + self.em, {"active": "- 之前改过 old.txt"}, ws=self.ws, subject=self.em)
        classes = [FakeAdapter, NoResumeFake, FakeAdapter]
        scripts = [[self.CAP], [], [writes("r.txt", "r", [{"text": "在做 换线路的活，改过 old.txt\n【结果】完成：r"}])]]

        class F(Factory):
            def __call__(self, agent):
                cls = classes.pop(0)
                a = cls(agent)
                a.script = [self._bind(a, x) for x in scripts.pop(0)]
                self.instances.append(a)
                return a
        f = F()
        tk = self.add_task("换线路的活")
        self.run_station(f)
        a1, _a2, a3 = f.instances
        self.assertNotEqual(a3.native_id, a1.native_id)
        self.assertEqual(self.sessions()[-1], (a3.native_id, 2, "line_switch", "codex-relay"))
        retry = self.events("turn.started")[-1]["data"]
        self.assertEqual(retry["kind"], "recall")                       # 新 thread：带着工作笔记、先复述
        self.assertIn("old.txt", a3.sent[0][1]["xay_work_notes"])
        self.assertEqual(self.state()["tasks"][tk]["status"], "reported")

    def test_codex_resume_on_relay_uses_relay_default_model(self):
        """真适配器 + 假 app-server：换线路续接时问 model/list，用中转站的默认模型，provider 换成 custom。"""
        from xay.adapters.codex import CodexAdapter
        from tests.test_adapters import FIX, make_exec
        bindir = self.tmp / "bin"
        bindir.mkdir()
        log = self.tmp / "fake_codex.log"
        card = {"bin": make_exec(bindir, "codex", FIX / "fake_codex.py"), "env": {"FAKE_LOG": str(log)},
                "line": "codex-relay", "line_switch": True}
        a = CodexAdapter(rpc_timeout=10)
        from tests.test_adapters import _terminate_and_kill
        def _cleanup():
            try:
                a.close()
            except Exception:
                pass
            p = getattr(a, "_proc", None)
            if p is not None:
                _terminate_and_kill(p, timeout=2.0)
        self.addCleanup(_cleanup)
        a.open(str(self.tmp), native_id="thr-old-1", card=card)
        msgs = [json.loads(x)["in"] for x in log.read_text().splitlines() if '"in"' in x]
        methods = [m.get("method") for m in msgs]
        self.assertLess(methods.index("model/list"), methods.index("thread/resume"))
        resume = [m for m in msgs if m.get("method") == "thread/resume"][0]["params"]
        self.assertEqual((resume["threadId"], resume["modelProvider"], resume["model"]), ("thr-old-1", "custom", "gpt-fake"))
        self.assertEqual(a.native_id, "thr-old-1")

    # 真机演示撞上的原话：中转站每日额度用完时报的是 403 + DAILY_LIMIT_EXCEEDED
    RELAY_QUOTA = "unexpected status 403 Forbidden: DAILY_LIMIT_EXCEEDED 您当前每日订阅额度不足"

    def test_relay_daily_limit_is_quota_not_auth(self):
        from xay.adapters.base import classify_error
        for code in (None, "other", "responseStreamConnectionFailed", "httpConnectionFailed"):
            self.assertEqual(classify_error(self.RELAY_QUOTA, code), "quota", code)
        self.assertEqual(classify_error("403 DAILY_LIMIT_EXCEEDED / 您当前每日订阅额度不足"), "quota")

    def test_relay_out_of_quota_switches_back_to_plus_in_same_thread(self):
        """双向：Plus 满载 → 中转站做完第一件；第二件在中转站报每日额度用完 → 换回 Plus、同一个 thread 重做，之后留在 Plus。"""
        quota = [{"end": {"status": "failed", "error": self.RELAY_QUOTA}}]     # 不写 kind：按错误文字归类
        f = Factory([[self.CAP],
                     [writes("one.txt", "1\n", [{"text": "【结果】完成：one"}]), quota],
                     [writes("two.txt", "2\n", [{"text": "【结果】完成：two"}])]])
        t1, t2 = self.add_task("第一件"), self.add_task("第二件")
        self.run_station(f)
        a1, a2, a3 = f.instances
        self.assertEqual([a.card["line"] for a in (a1, a2, a3)], ["codex-sub", "codex-relay", "codex-sub"])
        self.assertTrue(a3.card.get("line_switch"))
        self.assertEqual({a1.native_id, a2.native_id, a3.native_id}, {a1.native_id})   # 一直是同一个 thread
        self.assertEqual(self.sessions(), [(a1.native_id, 1, "new", None), (a1.native_id, 1, "line_switch", "codex-relay"),
                                           (a1.native_id, 1, "line_switch", "codex-sub")])
        ended = self.events("turn.ended")
        self.assertEqual([(e["data"]["status"], e["data"]["evidence"].get("error_kind")) for e in ended],
                         [("failed", "capacity"), ("completed", None), ("failed", "quota"), ("completed", None)])
        self.assertEqual(self.events("task.failed"), [])                # 两次都是第一次失败，不记
        self.assertEqual([e["subject"] for e in self.events("task.landed")], [t1, t2])
        self.assertTrue(a3.sent[0][0].startswith("（上一次因为额度线路满载或额度用完"))
        self.assertEqual([e["data"]["text"] for e in self.events("signal.raised", kind="quota")],
                         ["Codex Plus 满载，已换中转站接着做", "Codex 中转站额度用完了，已换回 Plus 接着做"])
        self.assertEqual(self.state()["employees"][self.em]["line"], "codex-sub")
        # 下一件活留在 Plus
        self.add_task("第三件")
        g = Factory([[writes("three.txt", "3", [{"text": "【结果】完成：three"}])]])
        self.run_station(g)
        self.assertEqual(g.instances[0].card["line"], "codex-sub")

    def test_relay_quota_then_plus_also_fails_switches_only_once(self):
        os.environ["XAY2_CODEX_LINE"] = "codex-relay"          # 这个员工一开始就走中转站
        self.addCleanup(os.environ.pop, "XAY2_CODEX_LINE", None)
        quota = [{"end": {"status": "failed", "error": self.RELAY_QUOTA}}]
        f = Factory([[quota], [self.CAP]])
        tk = self.add_task("两条线路都不行")
        self.run_station(f)
        self.assertEqual([a.card["line"] for a in f.instances], ["codex-relay", "codex-sub"])
        self.assertEqual([e["data"]["reason"] for e in self.events("employee.session")], ["new", "line_switch"])
        self.assertEqual([e["subject"] for e in self.events("task.failed")], [tk])

    def test_no_switch_when_turn_left_changes(self):
        f = Factory([[writes("half.txt", "h", self.CAP)]])
        self.add_task("改了一半就满载")
        self.run_station(f)
        self.assertEqual(len(f.instances), 1)
        self.assertEqual([e["data"]["reason"] for e in self.events("task.kept")], ["failed_with_changes"])
        self.assertEqual(self.events("signal.raised", kind="quota"), [])


class CompactionTests(StationCase):
    def test_native_compaction_before_next_task_then_recall(self):
        (self.repo / ".xay").mkdir()
        (self.repo / ".xay" / "context.md").write_text("这是一个测试项目。")
        f = Factory([[writes("a.txt", "A\n", [{"text": "【结果】完成：a"}, {"usage_pct": 80}]),
                      writes("b.txt", "B\n", [{"text": "1. 我在做：加 b 文件\n2. 改过：a.txt\n3. 下一步：写 b.txt\n"},
                                              {"text": "【结果】完成：b"}])]])
        t1, t2 = self.add_task("加 a 文件"), self.add_task("加 b 文件")
        self.run_station(f)

        types = [e["type"] for e in since(self.conn, 0, limit=100000)]
        req = self.events("context.compact_requested")
        self.assertEqual([(e["data"]["reason"], e["data"]["method"]) for e in req], [("threshold", "native")])
        comp = self.events("context.compacted")
        self.assertEqual(len(comp), 1)
        self.assertEqual((comp[0]["data"]["method"], comp[0]["data"]["before_pct"], comp[0]["data"]["segment"]),
                         ("native", 80.0, 1))
        self.assertLess(comp[0]["data"]["after_pct"], 60)
        second = [e for e in self.events("turn.started") if e["data"]["task"] == t2][0]
        self.assertLess(comp[0]["seq"], second["seq"])                 # 先整理记忆，再做第二件
        self.assertEqual(second["data"]["kind"], "recall")
        self.assertEqual(len(f.instances), 1)                          # 自带压缩：还是同一段对话
        self.assertEqual(len(self.events("employee.session")), 1)
        text, extra = f.instances[0].sent[1]
        self.assertTrue(text.startswith("【先复述】"))
        self.assertIn("加 a 文件", extra["xay_work_notes"])           # 工作笔记、项目背景走额外背景
        self.assertIn("这是一个测试项目", extra["xay_project_context"])
        self.assertIn("这是一个测试项目", f.instances[0].sent[0][1]["xay_project_context"])
        rc = self.events("recall.checked")
        self.assertEqual([(e["data"]["ok"], e["data"]["missing"]) for e in rc], [(True, [])])
        snap = build(self.conn, conv=self.conv)
        p = snap["people"][self.em]
        self.assertEqual((p["context"]["compactions"], p["amnesia"]), (1, False))
        self.assertEqual(self.state()["tasks"][t2]["status"], "reported")
        self.assertIn("context.updated", types)

    def test_handoff_when_no_native_compaction_new_segment_notes_and_recall(self):
        f = Factory([[writes("a.txt", "A\n", [{"text": "【结果】完成：a"}, {"usage_pct": 80}]), MEMO],
                     [writes("b.txt", "B\n", [{"text": "复述：在做 加 b 文件；改过 a.txt；下一步写 b\n【结果】完成：b"}])]],
                    native_compact=False)
        t1, t2 = self.add_task("加 a 文件"), self.add_task("加 b 文件")
        self.run_station(f)

        self.assertEqual(len(f.instances), 2)
        a1, a2 = f.instances
        self.assertTrue(a1._closed)
        self.assertEqual([(e["data"]["reason"], e["data"]["method"]) for e in self.events("context.compact_requested")],
                         [("threshold", "handoff")])
        memo_turn = [e for e in self.events("turn.started") if e["data"]["kind"] == "handoff_write"]
        self.assertEqual(len(memo_turn), 1)
        self.assertTrue(a1.sent[1][0].startswith("【整理记忆】"))
        sessions = [(e["data"]["native_id"], e["data"]["segment"], e["data"]["reason"]) for e in self.events("employee.session")]
        self.assertEqual(sessions, [(a1.native_id, 1, "new"), (a2.native_id, 2, "handoff")])
        self.assertNotEqual(a1.native_id, a2.native_id)
        comp = self.events("context.compacted")
        self.assertEqual([(e["data"]["method"], e["data"]["segment"]) for e in comp], [("handoff", 2)])
        s = self.state()
        self.assertTrue(s["notes"][self.em]["active"].startswith("## 1. 目标"))
        self.assertTrue(any("加 a 文件" in x for x in s["notes"][self.em]["archived"]))
        self.assertEqual(s["tasks"][t1]["status"], "reported")         # 写摘要那一轮不动任务状态
        notes_lines = a2.sent[0][1]["xay_work_notes"].splitlines()
        self.assertEqual(notes_lines[1], memo.FENCE)                         # 旧记录先加"只作参考"的围栏
        self.assertEqual(notes_lines[2], "## 1. 目标")                       # 新对话第一轮带着摘要
        self.assertIn(memo.FACTS_TITLE, s["notes"][self.em]["active"])       # 摘要后面附了工位记的事实
        self.assertIn("a.txt", s["notes"][self.em]["active"].split(memo.FACTS_TITLE)[1])
        self.assertEqual([(e["data"]["ok"]) for e in self.events("recall.checked")], [True])
        self.assertTrue((self.tmp / "v2" / "live" / self.em / "2.jsonl").exists())
        p = build(self.conn, conv=self.conv)["people"][self.em]
        self.assertEqual((p["context"]["segment"], p["context"]["compactions"]), (2, 1))
        self.assertEqual(s["employees"][self.em]["native_id"], a2.native_id)

    def test_machine_memo_when_employee_cannot_write_one(self):
        f = Factory([[writes("a.txt", "A\n", [{"text": "【结果】完成：a"}, {"usage_pct": 85}]),
                      [{"fail": {"kind": "context_overflow", "error": "上下文超了"}}]],
                     [[{"text": "我在做别的事\n【结果】完成：b"}]]], native_compact=False)
        self.add_task("加 a 文件")
        self.add_task("加 b 文件")
        self.run_station(f)
        comp = self.events("context.compacted")
        self.assertEqual([e["data"]["method"] for e in comp], ["machine"])
        active = self.state()["notes"][self.em]["active"]
        self.assertIn("工位自动拼", active)
        self.assertIn("a.txt", active)
        rc = self.events("recall.checked")      # 复述里没提到任务和改过的文件 → 可能失忆（什么都没提到仍判失忆）
        self.assertEqual([(e["data"]["ok"], e["data"]["missing"]) for e in rc], [(False, ["task", "file"])])
        self.assertTrue(build(self.conn, conv=self.conv)["people"][self.em]["amnesia"])

    def test_recall_check_passes_with_filename_only_lenient(self):
        """复述只写文件名（带反引号、去掉路径、不分大小写）→ 通过。"""
        f = Factory([[writes("V2/web/lib/api.js", "export const x = 1;\n", [{"text": "【结果】完成：push"}, {"usage_pct": 80}]), MEMO],
                     [writes("b.txt", "B\n", [{"text": "1. 我在做什么：做当前任务 tk_b\n2. 我改过哪些文件：`api.js`\n3. 下一步是什么：写 b.txt\n【结果】完成：b"}])]],
                    native_compact=False)
        self.add_task("页面推送")
        self.add_task("当前任务")
        self.run_station(f)
        rc = self.events("recall.checked")
        self.assertEqual([(e["data"]["ok"], e["data"]["missing"]) for e in rc], [(True, [])])

    def test_recall_check_passes_with_last_task_title_only_lenient(self):
        """复述没提到文件，但提到了最近一件活的标题或关键词 → 通过。"""
        f = Factory([[writes("V2/web/lib/api.js", "export const x = 1;\n", [{"text": "【结果】完成：push"}, {"usage_pct": 80}]), MEMO],
                     [writes("b.txt", "B\n", [{"text": "1. 我在做什么：做当前任务 tk_b\n2. 我改过哪些文件：刚才完成了页面推送\n3. 下一步是什么：写 b.txt\n【结果】完成：b"}])]],
                    native_compact=False)
        self.add_task("页面推送")
        self.add_task("当前任务")
        self.run_station(f)
        rc = self.events("recall.checked")
        self.assertEqual([(e["data"]["ok"], e["data"]["missing"]) for e in rc], [(True, [])])


class CompactRequestTests(StationCase):
    """别人（负责人 / 管家 / 规则监工）要求某员工整理记忆：工位在两件活之间按 method 整理一次，只处理一次。"""

    def request(self, by, method, reason="manual"):
        data = {"reason": reason, "method": method} if method else {"reason": reason}
        conv = self.conv if by.startswith("lead:") else None
        return append(self.conn, "context.compact_requested", by, data, ws=self.ws, conv=conv, subject=self.em)

    def reqs(self):
        return [(e["by"], e["data"]["reason"], e["data"].get("method")) for e in self.events("context.compact_requested")]

    def test_lead_asks_native_between_tasks_and_it_happens_once_even_after_restart(self):
        f = Factory([[writes("a.txt", "A\n", [{"text": "【结果】完成：a"}])]])
        self.add_task("加 a 文件")
        self.run_station(f)
        self.assertEqual(self.events("context.compacted"), [])            # 记忆不多，自己不会整理
        self.request(self.lead, "native")
        t2 = self.add_task("加 b 文件")
        g = Factory([[writes("b.txt", "B\n", [{"text": "1. 在做 加 b 文件\n2. 改过 a.txt\n3. 写 b\n【结果】完成：b"}])]])
        self.run_station(g)

        # 工位上线时员工状态回到了空闲，所以工位自己再标一次"整理记忆中"（写明真正用的方法）
        self.assertEqual(self.reqs(), [(self.lead, "manual", "native"), ("station:" + self.em, "manual", "native")])
        comp = self.events("context.compacted")
        self.assertEqual([(e["data"]["method"], e["data"]["segment"]) for e in comp], [("native", 1)])
        second = [e for e in self.events("turn.started") if e["data"]["task"] == t2][0]
        self.assertLess(comp[0]["seq"], second["seq"])                   # 先整理，再做下一件
        self.assertEqual(second["data"]["kind"], "recall")
        self.assertEqual(len(g.instances), 1)                             # 自带压缩：还是原来那个对话
        self.assertEqual(g.instances[0].native_id, f.instances[0].native_id)
        self.assertEqual(self.state()["tasks"][t2]["status"], "reported")

        # 同一个请求只处理一次：工位重启（没有新活）不会再整理
        h = Factory()
        self.run_station(h)
        self.assertEqual(len(self.events("context.compacted")), 1)
        self.assertEqual(h.instances, [])

    def test_request_while_working_waits_for_the_turn_then_hands_off(self):
        """规则监工在他干活时要求交接：不打断这一轮，做完、合并后再交接、开新对话。"""
        f = Factory([[writes("a.txt", "A\n", [{"text": "开始\n"}, {"hold": True},
                                              {"text": "【结果】完成：a\n【给老板看】给项目加好了初始文件"}]), MEMO], []])
        tk = self.add_task("长活")
        st = self.station(f)
        th = threading.Thread(target=st.run, kwargs={"once": True}, daemon=True)
        th.start()
        self.wait_for(lambda: "开始" in (st.turn or {}).get("buf", ""))
        self.request("rules", "handoff")
        time.sleep(0.3)                                                   # 给工位好几次轮询的机会
        self.assertEqual(f.instances[0].interrupts, 0)
        self.assertEqual(self.events("turn.ended"), [])
        self.assertEqual(self.events("context.compacted"), [])
        f.instances[0].release()
        th.join(10)
        self.assertFalse(th.is_alive())
        self.assertEqual(st.rejected_writes, [])

        self.assertEqual(self.state()["tasks"][tk]["status"], "reported")
        landed = self.events("task.landed")[0]
        comp = self.events("context.compacted")
        self.assertEqual([(e["data"]["method"], e["data"]["segment"]) for e in comp], [("handoff", 2)])
        self.assertLess(landed["seq"], comp[0]["seq"])
        self.assertEqual([e["data"]["kind"] for e in self.events("turn.started")], ["first", "handoff_write"])
        self.assertEqual(self.reqs(), [("rules", "manual", "handoff"), ("station:" + self.em, "manual", "handoff")])
        a1, a2 = f.instances
        self.assertEqual([e["data"]["reason"] for e in self.events("employee.session")], ["new", "handoff"])
        self.assertEqual(self.state()["employees"][self.em]["native_id"], a2.native_id)
        self.assertTrue(self.state()["notes"][self.em]["active"].startswith("## 1. 目标"))

    def test_auto_or_missing_method_lets_the_station_choose(self):
        f = Factory([[writes("a.txt", "A\n", [{"text": "【结果】完成：a"}])]])
        self.add_task("加 a 文件")
        self.run_station(f)
        self.request("office", None)                                      # 没写 method = auto
        self.run_station(Factory())
        self.assertEqual([e["data"]["method"] for e in self.events("context.compacted")], ["native"])   # Codex 能原生

    def test_nothing_to_compact_when_no_work_since_last_time(self):
        """还没干过活（或上次整理之后没干过活）的员工：请求不用处理，也不会等他干完下一件再补一次。"""
        self.request(self.lead, "handoff")
        f = Factory()
        self.run_station(f)
        self.assertEqual(f.instances, [])
        self.add_task("加 a 文件")
        g = Factory([[writes("a.txt", "A\n", [{"text": "【结果】完成：a"}])]])
        self.run_station(g)
        self.assertEqual(self.events("context.compacted"), [])
        self.assertEqual([e["data"]["kind"] for e in self.events("turn.started")], ["first"])


class CompactRequestDshTests(StationCase):
    agent = "dsh"

    def test_native_request_on_dsh_falls_back_to_handoff(self):
        """dsh 的 /compact 在 headless 下不能用（probes.md）：要求原生也只能交接。"""
        f = Factory([[writes("a.txt", "A\n", [{"text": "【结果】完成：a"}])]], lazy_session=True)
        self.add_task("加 a 文件")
        self.run_station(f)
        append(self.conn, "context.compact_requested", self.lead, {"reason": "manual", "method": "native"},
               ws=self.ws, conv=self.conv, subject=self.em)
        g = Factory([[MEMO], []], lazy_session=True)       # 续接原会话写交接摘要，再开一个新会话
        self.run_station(g)
        self.assertEqual(g.instances[0].native_id, f.instances[0].native_id)
        self.assertEqual([(e["by"], e["data"]["method"]) for e in self.events("context.compact_requested")],
                         [(self.lead, "native"), ("station:" + self.em, "handoff")])
        self.assertEqual([(e["data"]["method"], e["data"]["segment"]) for e in self.events("context.compacted")],
                         [("handoff", 2)])


class EvidenceTests(StationCase):
    def test_failed_turn_with_changes_is_kept_and_desk_untouched(self):
        f = Factory([[writes("half.txt", "half\n", [{"text": "做了一半"}, {"fail": {"kind": "other", "error": "编译挂了"}}])]])
        tk = self.add_task("会失败的活")
        self.run_station(f)
        s = self.state()
        self.assertEqual(s["tasks"][tk]["status"], "needs_you")
        kept = self.events("task.kept")
        self.assertEqual([e["data"]["reason"] for e in kept], ["failed_with_changes"])
        dec = list(s["decisions"].values())
        self.assertEqual([(d["kind"], d["task"], d["em"], d["owner"]) for d in dec], [("failed_with_changes", tk, self.em, "lead")])
        self.assertEqual([o["id"] for o in dec[0]["options"]], ["merge", "rework", "discard", "manual"])
        ended = self.events("turn.ended")[-1]["data"]
        self.assertEqual((ended["status"], ended["evidence"]["changed_files"]), ("failed", ["half.txt"]))
        self.assertFalse((self.repo / "half.txt").exists())             # 项目没动
        self.assertEqual((self.desk / "half.txt").read_text(), "half\n")  # 桌子保留

        # 下一件活：上一件的改动还等人决定 → 先等着，桌子一个字节不动
        t2 = self.add_task("下一件")
        g = Factory([[writes("x.txt", "x", [{"text": "【结果】完成：x"}])]])
        self.run_station(g)
        self.assertEqual(self.state()["tasks"][t2]["status"], "queued")
        self.assertEqual(g.instances, [])
        self.assertEqual((self.desk / "half.txt").read_text(), "half\n")

        # 人决定"合并" → 工位把桌子上的改动放回项目，然后接着做下一件
        notes = self.state()["notes"][self.em]["active"]
        self.assertIn("（%s）：%s" % (tk, memo.KEPT_NOTE), notes)
        append(self.conn, "decision.resolved", self.lead, {"option": "merge"}, ws=self.ws, conv=self.conv, subject=dec[0]["id"])
        self.run_station(g)
        s = self.state()
        self.assertEqual((self.repo / "half.txt").read_text(), "half\n")
        self.assertEqual(s["tasks"][tk]["status"], "reported")
        self.assertEqual(s["tasks"][t2]["status"], "reported")
        self.assertEqual((self.repo / "x.txt").read_text(), "x")
        # 工作笔记里那一行跟着改成了最后的结果，不再说"等负责人决定"
        line = [x for x in s["notes"][self.em]["active"].splitlines() if "（%s）" % tk in x]
        self.assertEqual(len(line), 1)
        self.assertIn("改动先保留，后来决定合并，已合并回项目", line[0])
        self.assertNotIn("等负责人决定", line[0])

    def test_discard_decision_resets_desk_and_rework_continues_on_kept_changes(self):
        f = Factory([[writes("w.txt", "draft\n", [{"text": "【结果】没做完：还差测试"}])]])
        tk = self.add_task("写 w")
        self.run_station(f)
        self.assertEqual([e["data"]["reason"] for e in self.events("task.kept")], ["mismatch"])
        dc = list(self.state()["decisions"])[0]
        append(self.conn, "decision.resolved", self.lead, {"option": "rework", "text": "把测试补上"}, ws=self.ws, conv=self.conv, subject=dc)
        g = Factory([[writes("w_test.txt", "ok\n", [{"text": "【结果】完成：补了测试"}])]])
        self.run_station(g)
        text = g.instances[0].sent[0][0]
        self.assertIn("【接着改】", text)
        self.assertIn("把测试补上", text)
        self.assertEqual(self.events("desk.synced")[-1]["data"]["mode"], "kept_conflict")
        landed = self.events("task.landed")[-1]
        self.assertEqual(sorted(landed["data"]["files"]), ["w.txt", "w_test.txt"])   # 上次保留的 + 这次的
        self.assertEqual(self.state()["tasks"][tk]["status"], "reported")
        lines = [x for x in self.state()["notes"][self.em]["active"].splitlines() if "（%s）" % tk in x]
        self.assertEqual(len(lines), 2)                                  # 保留那一行改写了，接着改的结果另记一行
        self.assertIn("改动先保留，后来让接着改（负责人的意见：把测试补上）", lines[0])
        self.assertNotIn("等负责人决定", lines[0])
        self.assertIn("已合并回项目", lines[1])

        # 丢掉：另一件活失败留下改动 → discard → 桌子回到开工时
        h = Factory([[writes("junk.txt", "junk\n", [{"fail": {"kind": "other", "error": "x"}}])]])
        t3 = self.add_task("会被丢掉的活")
        self.run_station(h)
        dc = [d for d in self.state()["decisions"].values() if d["task"] == t3][0]["id"]
        append(self.conn, "decision.resolved", self.lead, {"option": "discard"}, ws=self.ws, conv=self.conv, subject=dc)
        self.run_station(Factory())
        self.assertFalse((self.desk / "junk.txt").exists())
        self.assertFalse((self.repo / "junk.txt").exists())
        self.assertEqual(self.state()["tasks"][t3]["status"], "canceled")
        self.assertIsNone(self.state()["tasks"][t3].get("desk_action"))
        line = [x for x in self.state()["notes"][self.em]["active"].splitlines() if "（%s）" % t3 in x]
        self.assertEqual(len(line), 1)
        self.assertIn("改动先保留，后来决定丢掉，改动已经丢掉了", line[0])

    def test_conflict_manual_decision_marks_landed_and_next_task_aligns_cleanly(self):
        # 准备一个已有文件 conflict.txt，并在项目里提交
        (self.repo / "conflict.txt").write_text("line 1: origin\n")
        subprocess.run(["git", "add", "conflict.txt"], cwd=str(self.repo), check=True, capture_output=True)
        subprocess.run(["git", "-c", "commit.gpgsign=false", "commit", "-q", "-m", "add conflict.txt"],
                       cwd=str(self.repo), check=True, capture_output=True)

        # 假员工这一轮改 conflict.txt
        def edit_conflict(a, text, extra):
            p = Path(a.cwd) / "conflict.txt"
            p.write_text("line 1: desk edit\n")
            # 在落回项目前，项目里的文件被别人修改了，造成三方合并冲突
            (self.repo / "conflict.txt").write_text("line 1: concurrent edit\n")
            return [{"text": "【结果】完成：改了 conflict"}]

        f = Factory([[edit_conflict]])
        tk = self.add_task("改 conflict")
        self.run_station(f)

        # 冲突后改动保留，开甲类"需要你"
        self.assertEqual(self.state()["tasks"][tk]["status"], "needs_you")
        self.assertEqual(self.state()["tasks"][tk]["kept_reason"], "conflict")
        dc_item = list(self.state()["decisions"].values())[0]
        self.assertEqual(dc_item["task"], tk)
        self.assertEqual(dc_item["kind"], "conflict")
        # 选项里有 manual
        opt_ids = [o["id"] for o in dc_item["options"]]
        self.assertIn("manual", opt_ids)

        # 负责人选 manual：我已经手动合好了
        append(self.conn, "decision.resolved", self.lead, {"option": "manual"},
               ws=self.ws, conv=self.conv, subject=dc_item["id"])
        self.assertEqual(self.state()["tasks"][tk]["status"], "reported")
        self.assertEqual(self.state()["tasks"][tk]["desk_action"], "settle")

        # 工位处理 settle
        self.run_station(Factory())
        self.assertEqual(self.state()["tasks"][tk]["status"], "reported")
        self.assertIsNone(self.state()["tasks"][tk].get("desk_action"))

        landed_events = self.events("task.landed")
        self.assertEqual(len(landed_events), 1)
        landed = landed_events[0]
        self.assertEqual(landed["subject"], tk)
        self.assertTrue(landed["data"].get("manual"))
        self.assertIn("conflict.txt", landed["data"]["files"])

        # 下一件活开工：不再报桌子有没合并的改动（desk_unmerged）
        t2 = self.add_task("下一件活")
        g = Factory([[writes("next.txt", "next\n", [{"text": "【结果】完成：下个"}])]])
        self.run_station(g)

        self.assertEqual(self.state()["tasks"][t2]["status"], "reported")
        self.assertEqual([e for e in self.events("task.kept") if e["data"]["reason"] == "desk_unmerged"], [])


class SettleNoteTests(unittest.TestCase):
    """memo.settle_note：保留下来的活有了结果，笔记里那句"等负责人决定"改成结果。"""

    def test_rewrites_the_kept_line_in_place(self):
        notes = "- 「加 a」（tk_1）：已合并回项目；改了 1 个文件：a\n- 「加 b」（tk_2）：%s；改了 1 个文件：b" % memo.KEPT_NOTE
        active, archived = memo.settle_note(notes, "tk_2", "加 b", "后来决定合并，已合并回项目")
        self.assertEqual(active.splitlines()[1], "- 「加 b」（tk_2）：改动先保留，后来决定合并，已合并回项目；改了 1 个文件：b")
        self.assertEqual(active.splitlines()[0], notes.splitlines()[0])
        self.assertEqual(archived, [])

    def test_conflict_again_then_discard_rewrites_the_latest_pending_phrase(self):
        notes = "- 「加 b」（tk_2）：%s；改了 b" % memo.KEPT_NOTE
        active, _ = memo.settle_note(notes, "tk_2", "加 b", "后来决定合并，但合并时又冲突了，" + memo.KEPT_NOTE)
        active, _ = memo.settle_note(active, "tk_2", "加 b", "后来决定丢掉，改动已经丢掉了")
        self.assertEqual(active, "- 「加 b」（tk_2）：改动先保留，后来决定合并，但合并时又冲突了，改动先保留，"
                                 "后来决定丢掉，改动已经丢掉了；改了 b")

    def test_facts_block_line_after_handoff_is_rewritten_too(self):
        facts = memo.facts_block([{"id": "tk_2", "title": "加 b", "status": "needs_you", "files": ["b"]}])
        active, _ = memo.settle_note("## 1. 目标\n- 加 b\n" + facts, "tk_2", "加 b", "后来决定合并，已合并回项目")
        self.assertIn("- 「加 b」（tk_2）：后来决定合并，已合并回项目；改了 b", active)
        self.assertNotIn("等你决定", active)

    def test_missing_line_appends_or_leaves_alone(self):
        notes = "## 1. 目标\n- 别的事"
        active, _ = memo.settle_note(notes, "tk_9", "加 c", "后来决定丢掉，改动已经丢掉了")
        self.assertEqual(active.splitlines()[-1], "- 「加 c」（tk_9）：后来决定丢掉，改动已经丢掉了")
        self.assertIsNone(memo.settle_note(notes, "tk_9", "加 c", "后来让接着改", append=False))


class ReportTests(StationCase):
    def test_outbox_progress_and_marker_progress_letter(self):
        def turn(a, text, extra):
            env = dict(os.environ, **a.card["env"])
            out = subprocess.run([str(V2 / "bin" / "xay2"), "progress", "1/4", "读代码"], env=env, cwd=a.cwd,
                                 capture_output=True, text=True)
            assert out.returncode == 0, out.stderr
            with open(env["XAY2_OUTBOX"], "a") as fh:      # 一条写错任务的行：丢弃并提醒
                fh.write(json.dumps({"v": 1, "task": "tk_deadbeef", "type": "progress",
                                     "data": {"step": 1, "of": 2, "text": "别人的"}}) + "\n")
            (Path(a.cwd) / "d.txt").write_text("d")
            return [{"sleep": 0.2}, {"text": "【进度 2/4】写"}, {"text": "测试\n【写信 给 总经理】接口命名：要不要改成 snake_case？\n"},
                    {"plan": [("读代码", "completed"), ("写测试", "in_progress"), ("跑测试", "pending")]},
                    {"text": "【结果】完成：测试写好了"}]
        tk = self.add_task("写测试")
        self.run_station(Factory([[turn]]))
        prog = [(e["by"], e["data"]["source"], e["data"]["step"], e["data"]["of"], e["data"]["text"])
                for e in self.events("task.progress")]
        self.assertIn(("employee:" + self.em, "outbox", 1, 4, "读代码"), prog)
        self.assertIn(("employee:" + self.em, "marker", 2, 4, "写测试"), prog)
        self.assertIn(("station:" + self.em, "native_plan", 2, 3, "写测试"), prog)
        self.assertEqual(self.state()["tasks"][tk]["progress"]["text"], "写测试")
        letters = self.events("letter.sent")
        self.assertEqual([(e["data"]["from"], e["data"]["to"], e["data"]["subject"]) for e in letters],
                         [(self.em, "gm", "接口命名")])
        sig = self.events("signal.raised", kind="outbox_rejected")
        self.assertEqual(len(sig), 1)
        self.assertIn("不是这一轮在做的任务", sig[0]["data"]["text"])
        self.assertEqual((self.desk / ".xay" / "out" / "task").read_text(), tk)
        # 发件箱的幂等键按行号，重读不会重复记
        self.assertEqual(len([e for e in self.events("task.progress") if e["data"]["source"] == "outbox"]), 1)

    def test_outbox_notes_set_add_remove_and_remind(self):
        def turn(a, text, extra):
            env = dict(os.environ, **a.card["env"])
            # 1. set 替换整份笔记（即使 task 为空）
            env_no_task = dict(env)
            env_no_task.pop("XAY2_TASK", None)
            env_no_task.pop("XAY2_TASK_FILE", None)
            out = subprocess.run([str(V2 / "bin" / "xay2"), "notes", "Line 1\nLine 2 Foo"], env=env_no_task, cwd=a.cwd,
                                 capture_output=True, text=True)
            assert out.returncode == 0, out.stderr

            # 2. add 追加一条
            out = subprocess.run([str(V2 / "bin" / "xay2"), "notes", "--add", "Line 3 Bar"], env=env, cwd=a.cwd,
                                 capture_output=True, text=True)
            assert out.returncode == 0, out.stderr

            # 3. remove 删掉包含关键词的行（不分大小写，foo 删掉 Line 2 Foo）
            out = subprocess.run([str(V2 / "bin" / "xay2"), "notes", "--remove", "foo"], env=env, cwd=a.cwd,
                                 capture_output=True, text=True)
            assert out.returncode == 0, out.stderr

            # 4. remove 一行都没删到：笔记不变并发出提醒
            out = subprocess.run([str(V2 / "bin" / "xay2"), "notes", "--remove", "NotFoundWord"], env=env, cwd=a.cwd,
                                 capture_output=True, text=True)
            assert out.returncode == 0, out.stderr

            # 5. 别人的笔记被拒绝
            with open(env["XAY2_OUTBOX"], "a", encoding="utf-8") as fh:
                fh.write(json.dumps({"v": 1, "task": "", "type": "notes", "em": "em_other",
                                     "data": {"kind": "notes", "op": "set", "text": "偷改", "em": "em_other"}}) + "\n")

            (Path(a.cwd) / "done.txt").write_text("ok")
            return [{"text": "【结果】完成：搞定"}]

        tk = self.add_task("测试笔记")
        self.run_station(Factory([[turn]]))

        # 检查笔记状态（发件箱处理后的笔记内容，收工后末尾会附上任务合并结算行）
        notes = self.state()["notes"][self.em]["active"]
        self.assertEqual(notes.splitlines()[:2], ["Line 1", "- Line 3 Bar"])

        # 检查事件
        note_updates = self.events("notes.updated")
        self.assertEqual(len(note_updates), 4)  # set, add, remove 各一次，收工结算一次
        self.assertEqual(note_updates[0]["data"]["active"], "Line 1\nLine 2 Foo")
        self.assertEqual(note_updates[1]["data"]["active"], "Line 1\nLine 2 Foo\n- Line 3 Bar")
        self.assertEqual(note_updates[2]["data"]["active"], "Line 1\n- Line 3 Bar")
        self.assertTrue(note_updates[3]["data"]["active"].startswith("Line 1\n- Line 3 Bar\n- 「测试笔记」"))

        # 检查未删到的提醒
        sig = [e for e in self.events("signal.raised") if e["data"].get("kind") == "outbox_rejected"]
        remind_sig = [e for e in sig if e["data"].get("text") == "笔记里没有找到要删的内容"]
        self.assertEqual(len(remind_sig), 1)
        self.assertEqual(remind_sig[0]["data"]["level"], "remind")

        # 检查别人笔记被拒的提醒
        other_sig = [e for e in sig if "写的不是自己的笔记" in e["data"].get("text", "")]
        self.assertEqual(len(other_sig), 1)

    def test_marker_scan_is_stable_while_streaming(self):
        buf = "【进度 1/3】搭结构\n正文【结果】完成：好了"
        self.assertEqual([x[0] for x in markers.scan(buf)], [0])               # 最后一个还在流式输出
        self.assertEqual([x[1] for x in markers.scan(buf, final=True)], ["progress", "result"])
        self.assertEqual(markers.parse_result("没做完：还差一步"), ("partial", "还差一步"))
        self.assertEqual(markers.parse_result("卡住：没权限"), ("blocked", "没权限"))
        with self.assertRaises(markers.Rejected):
            markers.validate_outbox('{"v":1,"task":"tk_a","type":"progress","data":{"step":5,"of":4}}', "tk_a", self.em)
        self.assertEqual(memo.recall_check("在做 tk_x，改过 src/a.py", "tk_x", "", ["src/a.py"]), (True, []))
        self.assertEqual(memo.recall_check("在做 tk_x，改过 a.py", "tk_x", "", ["src/a.py"]), (True, []))
        # M3 真机冒烟：dsh 把"给 calc.py 加乘方"复述成带反引号的改写，意思对，不能判成失忆
        real = "1. 我在做什么：给 `calc.py` 加 `power(a, b)` 乘方函数，并在 `test_calc.py` 补对应测试"
        self.assertEqual(memo.recall_check(real, "tk_5246ef1e", "给 calc.py 加乘方", ["calc.py"]), (True, []))
        self.assertEqual(memo.recall_check("我在做别的事", "tk_x", "给 calc.py 加乘方", []), (False, ["task"]))
        # agy 把"加元音计数"复述成"实现 count_vowels(s)"：任务卡里的代码名字也算提到了任务
        agy_real = "1. 我在做什么：在 [strutil.py](file:///x/strutil.py) 中实现 `count_vowels(s)` 函数并补齐单元测试。"
        card = "在 strutil.py 里加 count_vowels(s)（数 aeiou，不分大小写），并在 test_strutil.py 里补测试。"
        self.assertEqual(memo.recall_check(agy_real, "tk_39ee5592", "加元音计数", ["strutil.py"], card), (True, []))
        self.assertEqual(memo.recall_check(agy_real, "tk_39ee5592", "加元音计数", ["strutil.py"]), (False, ["task"]))
        self.assertEqual(memo.card_names(card), ["count_vowels"])
        self.assertEqual(memo.recall_check("在修 README 的错别字", "tk_x", "加 b 文件", []), (False, ["task"]))
        # tk_efd2dd30：复述自检别太严（文件名对上就算、提标题也算、只读不查）
        self.assertEqual(memo.recall_check("1. 做 tk_x\n2. 改过 `api.js`\n3. 下一步", "tk_x", "", ["V2/web/lib/api.js"], last_title="页面推送"), (True, []))
        self.assertEqual(memo.recall_check("1. 做 tk_x\n2. 改过 API.JS\n3. 下一步", "tk_x", "", ["V2/web/lib/api.js"], last_title="页面推送"), (True, []))
        self.assertEqual(memo.recall_check("1. 做 tk_x\n2. 刚才做了 页面推送\n3. 下一步", "tk_x", "", ["V2/web/lib/api.js"], last_title="页面推送"), (True, []))
        self.assertEqual(memo.recall_check("1. 做 tk_x\n2. 完成了推送\n3. 下一步", "tk_x", "", ["V2/web/lib/api.js"], last_title="页面推送"), (True, []))
        self.assertEqual(memo.recall_check("我在做别的事\n【结果】完成：b", "tk_x", "下一活", ["V2/web/lib/api.js"], last_title="页面推送"), (False, ["task", "file"]))
        self.assertEqual(memo.recall_check("1. 做 tk_x\n2. 改过：无\n3. 下一步", "tk_x", "", [], last_title="只读活"), (True, []))
        self.assertFalse(memo.valid_memo("我不知道"))
        self.assertTrue(memo.valid_memo(MEMO))


class ControlTests(StationCase):
    def test_steer_reaches_adapter_during_turn(self):
        f = Factory([[writes("s.txt", "s", [{"text": "开始\n"}, {"hold": True}, {"text": "【结果】完成：好"}])]])
        self.add_task("长活")
        st = self.station(f)
        th = threading.Thread(target=st.run, kwargs={"once": True}, daemon=True)
        th.start()
        self.wait_for(lambda: self.events("turn.started"))
        append(self.conn, "control.requested", self.lead, {"action": "steer", "text": "注释写成中文", "by_source": "cli"},
               ws=self.ws, conv=self.conv, subject=self.em)
        self.wait_for(lambda: f.instances and f.instances[0].steered)
        self.assertEqual(f.instances[0].steered, ["注释写成中文"])
        f.instances[0].release()
        th.join(10)
        done = self.events("control.done")
        self.assertEqual([(e["data"]["action"], e["data"]["result"]) for e in done], [("steer", "ok")])
        self.assertEqual(len(self.events("task.landed")), 1)

    def test_stop_interrupts_turn_keeps_changes_and_goes_offline(self):
        f = Factory([[writes("s.txt", "s", [{"hold": True}])]])
        tk = self.add_task("长活")
        self.add_task("后面一件")
        st = self.station(f)
        th = threading.Thread(target=st.run, kwargs={"once": True}, daemon=True)
        th.start()
        self.wait_for(lambda: self.events("turn.started"))
        append(self.conn, "control.requested", self.lead, {"action": "stop", "by_source": "cli"},
               ws=self.ws, conv=self.conv, subject=self.em)
        th.join(10)
        self.assertFalse(th.is_alive())
        self.assertEqual(f.instances[0].interrupts, 1)
        self.assertEqual(self.events("turn.ended")[-1]["data"]["status"], "interrupted")
        # 叫停的活记"已停下"（paused），"需要你"仍是甲类（合并 / 接着改 / 丢掉）
        self.assertEqual([e["data"]["reason"] for e in self.events("task.kept")], ["paused"])
        self.assertEqual([e["data"]["kind"] for e in self.events("decision.opened")], ["failed_with_changes"])
        self.assertEqual(self.events("employee.offline")[-1]["data"]["reason"], "stopped")
        self.assertEqual(len(self.events("turn.started")), 1)            # 后面那件没开工
        self.assertEqual(self.state()["tasks"][tk]["status"], "needs_you")

    def test_idle_steer_starts_chat_turn_and_no_repeat_after_restart(self):
        # 1. 空闲员工没有活，收到插话
        append(self.conn, "control.requested", self.lead, {"action": "steer", "text": "闲着吗？聊两句", "by_source": "cli"},
               ws=self.ws, conv=self.conv, subject=self.em)
        f1 = Factory([[{"text": "在的，有什么指示？"}]])
        self.run_station(f1)

        # 检查是否在原对话里开了一轮 chat
        starts = self.events("turn.started")
        self.assertEqual(len(starts), 1)
        self.assertEqual(starts[0]["data"]["kind"], "chat")
        self.assertIsNone(starts[0]["data"]["task"])
        self.assertEqual(starts[0]["data"]["em"], self.em)

        # 检查假 AI 实例收到的输入文本包含指定前缀和插话原文
        a1 = f1.instances[0]
        sent_text = a1.sent[0][0]
        self.assertIn("这是负责人在你没活的时候跟你说的话", sent_text)
        self.assertIn("必须运行 xay2 notes", sent_text)                    # 让他记住的事要真的写进笔记，不能只嘴上答应
        self.assertIn("不要改项目文件", sent_text)
        self.assertIn("闲着吗？聊两句", sent_text)

        # 这一轮正常结束并写 turn.ended，回到空闲（工位执行结束以 idle 正常下班）
        ends = self.events("turn.ended")
        self.assertEqual(len(ends), 1)
        off = [e for e in self.events("employee.offline") if e["data"].get("reason") == "idle"]
        self.assertTrue(len(off) >= 1)

        # 2. 工位重启后不重复说
        f2 = Factory([])
        self.run_station(f2)
        self.assertEqual(len(self.events("turn.started")), 1, "重启后已处理的插话不重复开启新轮次")

    def test_chat_turn_changed_files_triggers_warning_and_not_landed(self):
        append(self.conn, "control.requested", self.lead, {"action": "steer", "text": "别改代码", "by_source": "cli"},
               ws=self.ws, conv=self.conv, subject=self.em)
        f = Factory([[writes("illegal.txt", "illegal", [{"text": "我改了文件"}])]])
        self.run_station(f)
        sig = [e for e in self.events("signal.raised") if e["data"].get("kind") == "chat_changed_files"]
        self.assertEqual(len(sig), 1)
        self.assertEqual(sig[0]["data"]["level"], "warn")
        self.assertFalse((self.repo / "illegal.txt").exists(), "改动不合并回项目")


class M4HooksTests(StationCase):
    """管家自动化那边要工位补的：错误种类、一轮中途的 token、命令行 pid、管家提醒、提醒种类。"""

    def test_turn_tokens_are_reported_while_the_turn_runs(self):
        f = Factory([[writes("t.txt", "t", [{"usage": {"used": 10_000, "turn_total": 5_000}},
                                            {"usage": {"used": 10_200, "turn_total": 8_000}},    # 涨 3000：不单独写
                                            {"usage": {"used": 10_500, "turn_total": 10_000}},   # 涨满 5000：写
                                            {"usage": {"used": 10_700, "turn_total": 13_000}},   # 涨 3000：不单独写
                                            {"usage": {"used": 11_000, "turn_total": 15_000}},   # 涨满 5000：写
                                            {"text": "【结果】完成：t"}])]])
        self.add_task("费 token 的活")
        self.run_station(f)
        tn = self.events("turn.started")[0]["subject"]
        rows = [(e["data"].get("turn"), e["data"].get("turn_tokens")) for e in self.events("context.updated")]
        self.assertEqual([r for r in rows if r[0]], [(tn, 5_000), (tn, 10_000), (tn, 15_000)])

    def test_cli_pids_ride_on_heartbeats(self):
        class PidFake(FakeAdapter):
            def child_pids(self):
                return [424242] if self.alive() else []
        f = Factory()
        f.__class__ = type("PF", (Factory,), {"__call__": lambda self, agent: self.instances.append(PidFake(agent)) or self.instances[-1]})
        self.add_task("看 pid")
        self.run_station(f)
        beats = [e["data"] for e in self.events("station.heartbeat")]
        self.assertIn([424242], [b.get("cli_pids") for b in beats])
        self.assertEqual(beats[0]["cli_pids"], [])                       # 刚上线还没起命令行

    def test_rules_steer_is_shown_as_office_reminder(self):
        f = Factory([[writes("s.txt", "s", [{"text": "开始\n"}, {"hold": True}, {"text": "【结果】完成：好"}])]])
        self.add_task("长活")
        st = self.station(f)
        th = threading.Thread(target=st.run, kwargs={"once": True}, daemon=True)
        th.start()
        self.wait_for(lambda: self.events("turn.started"))
        append(self.conn, "control.requested", "rules", {"action": "steer", "text": "预算快用完了，收尾吧", "by_source": "rules"},
               ws=self.ws, conv=self.conv, subject=self.em)
        self.wait_for(lambda: f.instances and f.instances[0].steered)
        self.assertEqual(f.instances[0].steered, ["【监工提醒】预算快用完了，收尾吧"])
        f.instances[0].release()
        th.join(10)
        live = (self.tmp / "v2" / "live" / self.em / "1.jsonl").read_text()
        self.assertIn("监工提醒：预算快用完了", live)
        self.assertNotIn("负责人插话：预算", live)

    def test_queued_steers_keep_who_said_them(self):
        st = self.station(Factory())
        st.pending_steers = [("负责人插话", "注释写中文"), ("监工提醒", "别超预算")]
        st.conn, st.ws, st.agent = self.conn, self.ws, "dsh"
        st._refresh(force=True)
        text, _ = st._turn_input({"title": "x"}, "tk_x", "continue", False, None)
        self.assertIn("【负责人插话】注释写中文", text)
        self.assertIn("【监工提醒】别超预算", text)
        self.assertEqual(st._lead_words, ["插话：注释写中文"])           # 监工的提醒不算负责人原话

    def test_executor_commit_gets_its_own_signal_kind(self):
        def turn(a, text, extra):
            (Path(a.cwd) / "c.txt").write_text("c")
            subprocess.run(["git", "add", "c.txt"], cwd=a.cwd, check=True, capture_output=True)
            subprocess.run(["git", "-c", "commit.gpgsign=false", "commit", "-q", "-m", "偷偷提交"], cwd=a.cwd,
                           check=True, capture_output=True)
            return [{"text": "【结果】完成：c"}]
        tk = self.add_task("会自己提交的活")
        self.run_station(Factory([[turn]]))
        self.assertEqual(self.events("signal.raised", kind="error_loop"), [])
        sig = self.events("signal.raised", kind="executor_commit")
        self.assertEqual([(e["data"]["level"], e["data"]["task"]) for e in sig], [("warn", tk)])

    def test_error_kinds_are_split(self):
        from xay.adapters.base import classify_error
        cases = {"429 Too Many Requests": "rate_limit", "RESOURCE_EXHAUSTED: 429 quota exceeded": "quota",
                 "Selected model is at capacity": "capacity", "401 Unauthorized: invalid api key": "auth",
                 "fetch failed: ECONNRESET": "network", "prompt is too long": "context_overflow",
                 "API error (attempt 1): request failed: Post https://example.com: EOF": "network",
                 "something odd": "other"}
        for text, kind in cases.items():
            self.assertEqual(classify_error(text), kind, text)
        self.assertEqual(classify_error("", "rateLimitExceeded"), "rate_limit")


class RecoveryTests(StationCase):
    def test_restart_closes_turn_left_open_by_dead_station(self):
        tk = self.add_task("被打断的活")
        tn = "tn_dead0001"
        append(self.conn, "employee.online", "station:" + self.em, {"pid": 1}, ws=self.ws, subject=self.em)
        append(self.conn, "turn.started", "station:" + self.em, {"task": tk, "em": self.em, "kind": "first"},
               ws=self.ws, conv=self.conv, subject=tn)
        self.run_station(Factory())
        ended = [e for e in self.events("turn.ended") if e["subject"] == tn]
        self.assertEqual(ended[0]["data"]["status"], "interrupted")
        self.assertEqual([e["data"]["reason"] for e in self.events("task.kept")], ["interrupted"])
        self.assertEqual(self.state()["tasks"][tk]["status"], "needs_you")
        self.assertEqual(len(self.events("decision.opened")), 1)


# ===========================================================================
# M3：dsh / agy 员工
# ===========================================================================

def sessions_of(case):
    return [(e["data"]["native_id"], e["data"]["segment"], e["data"]["reason"]) for e in case.events("employee.session")]


class DshEmployeeTests(StationCase):
    agent = "dsh"

    def test_two_related_tasks_same_session_and_resume_after_restart(self):
        """dsh 的会话号第一轮才知道：报上来就记进账本，下班再上班还接得回同一个会话。"""
        f = Factory([[writes("calc.py", "def add(a, b):\n    return a + b\n", [{"text": "【结果】完成：加了 add"}]),
                      writes("calc.py", "def add(a, b):\n    return a + b\n\ndef sub(a, b):\n    return a - b\n",
                             [{"text": "上一件改了 calc.py\n【结果】完成：加了 sub"}])]], lazy_session=True)
        t1, t2 = self.add_task("写加法"), self.add_task("再写减法")
        self.run_station(f)
        self.assertEqual(len(f.instances), 1)
        sid = f.instances[0].native_id
        self.assertTrue(sid)
        self.assertEqual(sessions_of(self), [(sid, 1, "new")])          # 不写 native_id 为空的 session
        self.assertEqual([e["data"]["kind"] for e in self.events("turn.started")], ["first", "continue"])
        self.assertEqual([e["subject"] for e in self.events("task.landed")], [t1, t2])
        self.assertEqual(self.state()["employees"][self.em]["native_id"], sid)
        # 下班后再来活：新工位按账本里的会话号续接
        t3 = self.add_task("再写乘法")
        g = Factory([[writes("mul.py", "x", [{"text": "【结果】完成：乘法"}])]], lazy_session=True)
        self.run_station(g)
        self.assertEqual(g.instances[0].native_id, sid)
        self.assertEqual(sessions_of(self), [(sid, 1, "new")])          # 接上了，不另记
        self.assertEqual(self.state()["tasks"][t3]["status"], "reported")

    def test_handoff_compaction_new_session_and_recall(self):
        f = Factory([[writes("a.txt", "A\n", [{"text": "【结果】完成：a"}, {"usage_pct": 56}]), MEMO],
                     [writes("b.txt", "B\n", [{"text": "1. 在做 加 b 文件\n2. 改过 a.txt\n3. 下一步写 b\n【结果】完成：b"}])]],
                    lazy_session=True)
        t1, t2 = self.add_task("加 a 文件"), self.add_task("加 b 文件")
        self.run_station(f)
        a1, a2 = f.instances
        self.assertEqual([(e["data"]["reason"], e["data"]["method"]) for e in self.events("context.compact_requested")],
                         [("threshold", "handoff")])
        # 新会话的号第一轮才有：先写一条清掉旧号，号报上来再补一条（同一段）
        self.assertEqual(sessions_of(self), [(a1.native_id, 1, "new"), (None, 2, "handoff"), (a2.native_id, 2, "handoff")])
        self.assertEqual([(e["data"]["method"], e["data"]["segment"]) for e in self.events("context.compacted")],
                         [("handoff", 2)])
        self.assertEqual([e["data"]["kind"] for e in self.events("turn.started")], ["first", "handoff_write", "recall"])
        self.assertEqual([(e["data"]["ok"], e["data"]["missing"]) for e in self.events("recall.checked")], [(True, [])])
        notes = self.state()["notes"][self.em]["active"]
        self.assertTrue(notes.startswith("## 1. 目标"))
        self.assertIn(memo.FACTS_TITLE, notes)
        self.assertIn(memo.FENCE, a2.sent[0][1]["xay_work_notes"])
        self.assertEqual(self.state()["tasks"][t2]["status"], "reported")

    def test_handoff_then_station_restart_keeps_the_reason_for_the_new_session(self):
        f = Factory([[writes("a.txt", "A\n", [{"text": "【结果】完成：a"}, {"usage_pct": 56}]), MEMO], []], lazy_session=True)
        self.add_task("加 a 文件")
        self.run_station(f)                       # 交接完就下班：新会话号还没报上来
        self.assertEqual(sessions_of(self)[-1], (None, 2, "handoff"))
        self.add_task("加 b 文件")
        g = Factory([[writes("b.txt", "B", [{"text": "在做 加 b 文件，改过 a.txt\n【结果】完成：b"}])]], lazy_session=True)
        self.run_station(g)
        self.assertIsNone(g.instances[0].card.get("native_id"))
        self.assertEqual(sessions_of(self)[-1], (g.instances[0].native_id, 2, "handoff"))   # 不说成 new
        self.assertEqual([e["data"]["ok"] for e in self.events("recall.checked")], [True])

    def test_absolute_token_cap_triggers_before_percentage(self):
        """dsh 门槛：55% 或 30 万 token 谁先到算谁（窗口哪天变成 1M 时 55% 就太大了）。"""
        f = Factory([[writes("a.txt", "A", [{"text": "【结果】完成：a"}, {"usage": {"used": 310_000, "window": 1_000_000}}]),
                      MEMO]], lazy_session=True)
        self.add_task("加 a 文件")
        self.run_station(f)
        req = self.events("context.compact_requested")
        self.assertEqual([(e["data"]["reason"], e["data"]["method"]) for e in req], [("threshold", "handoff")])
        self.assertIn(31.0, [e["data"]["pct"] for e in self.events("context.updated")])

    def test_patch_not_applied_refuses_to_work_and_raises_reminder(self):
        from xay.adapters.base import AdapterConfigError

        class Broken(Factory):
            def __call__(self, agent):
                a = super().__call__(agent)
                a.open_error = "x"
                a.open = lambda *x, **k: (_ for _ in ()).throw(
                    AdapterConfigError("dsh 补丁里的条目「agent-default-modle」没找到，模型限制可能没生效，拒绝开工"))
                return a
        tk = self.add_task("会被拒绝的活")
        self.run_station(Broken())
        t = self.state()["tasks"][tk]
        self.assertEqual((t["status"], t["failure_reason"]), ("failed", "设置不对，拒绝开工"))
        sig = self.events("signal.raised", kind="config")
        self.assertEqual([e["data"]["level"] for e in sig], ["warn"])
        self.assertIn("没找到", sig[0]["data"]["text"])
        self.assertEqual(self.events("turn.started"), [])

    def test_task_card_model_must_be_allowed(self):
        tk = new_id("tk_")
        append(self.conn, "task.added", self.lead, {"title": "换模型", "card": {"text": "x", "model": "deepseek-v4-pro"},
                                                     "deps": [], "writes": [], "mode": "write", "reason": "测试",
                                                     "owner": self.em}, ws=self.ws, conv=self.conv, subject=tk)
        self.queue(tk)
        f = Factory()
        self.run_station(f)
        self.assertEqual(f.instances, [])                                # 连适配器都没造
        self.assertEqual(self.state()["tasks"][tk]["failure_reason"], "设置不对，拒绝开工")
        self.assertEqual(len(self.events("signal.raised", kind="config")), 1)


def fake_tools(tmp, current=2, n=3, fail=False):
    """假的 antigravity-tools：list 打表、switch <序号> --target agy 换当前账号（状态存文件，调用记日志）。"""
    state = tmp / "tools-state.json"
    state.write_text(json.dumps({"current": current, "n": n}))
    log = tmp / "tools.log"
    script = tmp / "fake_tools.py"
    script.write_text(
        "import json, sys\n"
        "state = json.load(open(%r))\n"
        "open(%r, 'a').write(json.dumps(sys.argv[1:]) + '\\n')\n"
        "if sys.argv[1:2] == ['list']:\n"
        "    print('序号 | 激活 | 邮箱 | 别名 | 状态')\n"
        "    for i in range(1, state['n'] + 1):\n"
        "        print('%%d | %%s | u%%d@example.com | 账号%%d | \\x1b[32m正常\\x1b[0m' %% (i, '👉' if i == state['current'] else '', i, i))\n"
        "elif sys.argv[1:2] == ['switch']:\n"
        "    if %r: sys.exit(1)\n"
        "    state['current'] = int(sys.argv[2]); json.dump(state, open(%r, 'w'))\n"
        % (str(state), str(log), fail, str(state)))
    exe = tmp / "antigravity-tools"
    exe.write_text('#!/bin/sh\nexec "%s" "%s" "$@"\n' % (sys.executable, script))
    exe.chmod(0o755)
    (tmp / "tools-home").mkdir(exist_ok=True)
    os.environ["XAY_AGY_TOOLS_HOME"] = str(tmp / "tools-home")   # 不读本机真的 Antigravity Tools 记录
    return exe, log, state


class AgyEmployeeTests(StationCase):
    agent = "agy"

    def test_two_related_tasks_hot_resume_and_native_compaction_with_recall(self):
        f = Factory([[writes("a.txt", "A\n", [{"text": "【结果】完成：a"}, {"usage_pct": 52}]),
                      writes("b.txt", "B\n", [{"text": "1. 在做 加 b 文件\n2. 改过 a.txt\n3. 下一步写 b\n【结果】完成：b"}])]])
        t1, t2 = self.add_task("加 a 文件"), self.add_task("加 b 文件")
        self.run_station(f)
        self.assertEqual(len(f.instances), 1)                            # 热续接：同一个进程、同一个对话
        self.assertEqual([(e["data"]["reason"], e["data"]["method"]) for e in self.events("context.compact_requested")],
                         [("threshold", "native")])                      # 先用自带 /compact
        self.assertEqual([(e["data"]["method"], e["data"]["segment"]) for e in self.events("context.compacted")],
                         [("native", 1)])
        self.assertEqual([e["data"]["kind"] for e in self.events("turn.started")], ["first", "recall"])
        self.assertEqual([(e["data"]["ok"]) for e in self.events("recall.checked")], [True])
        self.assertEqual(len(self.events("employee.session")), 1)
        self.assertEqual([e["subject"] for e in self.events("task.landed")], [t1, t2])

    def test_native_compaction_twice_in_one_segment_then_handoff(self):
        f = Factory([[writes("a.txt", "A", [{"text": "【结果】完成：a"}, {"usage_pct": 55}]),
                      writes("b.txt", "B", [{"text": "加 b 文件，改过 a.txt\n【结果】完成：b"}, {"usage_pct": 55}]),
                      writes("c.txt", "C", [{"text": "加 c 文件，改过 b.txt\n【结果】完成：c"}, {"usage_pct": 55}]),
                      MEMO], []])
        for name in "abc":
            self.add_task("加 %s 文件" % name)
        self.run_station(f)
        self.assertEqual([e["data"]["method"] for e in self.events("context.compact_requested")],
                         ["native", "native", "handoff"])
        self.assertEqual([e["data"]["method"] for e in self.events("context.compacted")], ["native", "native", "handoff"])
        self.assertEqual(len(f.instances), 2)

    def test_cold_resume_failure_hands_off_and_redoes_the_task(self):
        """冷续接没接上：当场停下 → 机器版交接（员工接不上旧对话就不让他写）→ 新对话 → 同一件活重做，账本有 resume_failed。"""
        append(self.conn, "notes.updated", "station:" + self.em, {"active": "- 之前改过 old.txt"}, ws=self.ws, subject=self.em)
        append(self.conn, "employee.session", "station:" + self.em, {"native_id": "conv-gone", "segment": 1, "reason": "new"},
               ws=self.ws, subject=self.em)
        classes = [dict(fail_resume=True), {}]
        scripts = [[[{"hold": True}]],
                   [writes("r.txt", "r\n", [{"text": "在做 冷续接的活，改过 old.txt\n【结果】完成：r"}])]]

        class F(Factory):
            def __call__(self, agent):
                a = FakeAdapter(agent, **classes.pop(0))
                a.script = [self._bind(a, x) for x in scripts.pop(0)]
                self.instances.append(a)
                return a
        f = F()
        tk = self.add_task("冷续接的活")
        t0 = time.time()
        self.run_station(f)
        a1, a2 = f.instances
        self.assertEqual(a1.native_id, "conv-gone")
        self.assertEqual(a1.interrupts, 1)                               # 当场停下，没等这一轮
        self.assertEqual(sessions_of(self), [("conv-gone", 1, "new"), (a2.native_id, 2, "resume_failed")])
        self.assertEqual([(e["data"]["reason"], e["data"]["method"]) for e in self.events("context.compact_requested")],
                         [("resume_failed", "handoff")])
        self.assertEqual([e["data"]["method"] for e in self.events("context.compacted")], ["machine"])
        kinds = [e["data"]["kind"] for e in self.events("turn.started")]
        self.assertEqual(kinds, ["first", "recall"])                     # 没有让接不上的旧对话写摘要
        ended = self.events("turn.ended")
        self.assertEqual((ended[0]["data"]["status"], ended[0]["data"]["evidence"]["error_kind"]), ("interrupted", "resume_failed"))
        self.assertTrue(a2.sent[0][0].startswith(station_mod.RETRY_PREFIX["resume"]))
        self.assertIn("old.txt", a2.sent[0][1]["xay_work_notes"])         # 新对话带着笔记
        self.assertEqual(self.events("task.failed"), [])
        self.assertEqual(self.state()["tasks"][tk]["status"], "reported")
        resumed = self.events("employee.session", reason="resume_failed")[0]
        first = self.events("turn.started")[0]
        self.assertLess(resumed["ts"] - first["ts"], 30)                 # 30 秒内交接完
        self.assertLess(time.time() - t0, 30)

    def test_error_loop_is_stopped_signalled_and_handed_off(self):
        f = Factory([[[{"error": {"text": "AGY_ERROR: 模型出错"}}, {"error": {"text": "AGY_ERROR: 模型出错"}},
                       {"error": {"text": "AGY_ERROR: 模型出错"}}, {"hold": True}]], []])
        tk = self.add_task("会死循环的活")
        self.run_station(f)
        self.assertEqual(f.instances[0].interrupts, 1)
        sig = self.events("signal.raised", kind="error_loop")
        self.assertEqual([(e["data"]["level"], e["data"]["task"]) for e in sig], [("warn", tk)])   # 带上是哪件活
        ended = self.events("turn.ended")[0]["data"]
        self.assertEqual((ended["status"], ended["evidence"]["error_kind"]), ("failed", "error_loop"))
        self.assertEqual(self.state()["tasks"][tk]["status"], "failed")
        self.assertEqual([(e["data"]["reason"], e["data"]["method"]) for e in self.events("context.compact_requested")],
                         [("error_loop", "handoff")])
        self.assertEqual([e["data"]["method"] for e in self.events("context.compacted")], ["machine"])
        self.assertNotIn("handoff_write", [e["data"]["kind"] for e in self.events("turn.started")])

    def test_errors_with_tools_in_between_are_not_a_loop(self):
        f = Factory([[writes("x.txt", "x", [{"error": {"text": "e1"}}, {"error": {"text": "e2"}},
                                            {"tool": "cmd", "text": "ls", "output": "x.txt"},
                                            {"error": {"text": "e3"}}, {"error": {"text": "e4"}},
                                            {"text": "【结果】完成：x"}])]])
        self.add_task("报错但在干活")
        self.run_station(f)
        self.assertEqual(self.events("signal.raised", kind="error_loop"), [])
        self.assertEqual(len(self.events("task.landed")), 1)

    def test_quota_switches_account_through_antigravity_tools_and_redoes_in_same_conversation(self):
        exe, log, state = fake_tools(self.tmp, current=2, n=3)
        os.environ["XAY_AGY_TOOLS_BIN"] = str(exe)
        quota_fail = [{"fail": {"kind": "quota", "error": "RESOURCE_EXHAUSTED: You have exhausted your capacity"}}]
        f = Factory([[quota_fail], [writes("q.txt", "q\n", [{"text": "【结果】完成：q"}])]])
        tk = self.add_task("额度用完也要做完")
        self.run_station(f)
        a1, a2 = f.instances
        self.assertEqual(a2.native_id, a1.native_id)                     # 同一个对话（换号后冷续接）
        self.assertEqual(json.loads(state.read_text())["current"], 3)    # 换到下一个正常的账号
        calls = [json.loads(x) for x in log.read_text().splitlines()]
        self.assertIn(["switch", "3", "--target", "agy"], calls)
        self.assertFalse([c for c in calls if "--continue" in c])        # 不让 Tools 自己去拉起 agy
        sig = self.events("signal.raised", kind="quota")
        self.assertEqual([e["data"]["level"] for e in sig], ["remind"])
        self.assertIn("账号3", sig[0]["data"]["text"])
        self.assertNotIn("@", sig[0]["data"]["text"])                    # 邮箱不进账本
        self.assertEqual(self.events("task.failed"), [])
        self.assertEqual(self.state()["tasks"][tk]["status"], "reported")
        self.assertTrue(a2.sent[0][0].startswith(station_mod.RETRY_PREFIX["account"]))
        self.assertEqual(len(self.events("employee.session")), 1)
        # 第二次又额度用完（2 分钟冷却内）：不再换号，只重做一次；还不行就照常失败
        t2 = self.add_task("又用完了")
        g = Factory([[quota_fail], [quota_fail]])
        self.run_station(g)
        self.assertEqual(json.loads(state.read_text())["current"], 3)
        self.assertEqual(len(g.instances), 2)                            # 只重做了一次
        self.assertEqual(self.state()["tasks"][t2]["status"], "failed")
        ended = [e for e in self.events("turn.ended") if e["data"]["evidence"].get("error_kind") == "quota"]
        self.assertEqual(len(ended), 3)

    def test_ineligible_account_is_blocked_and_switched(self):
        """真机 2026-10-31：换到的号"not eligible"（地区用不了）→ 记成不能用、换下一个号、同一个对话重做。"""
        exe, log, state = fake_tools(self.tmp, current=2, n=3)
        os.environ["XAY_AGY_TOOLS_BIN"] = str(exe)
        bad = [{"fail": {"kind": "ineligible", "error": "Eligibility check failed: Your current account is not eligible for Antigravity"}}]
        f = Factory([[bad], [writes("e.txt", "e\n", [{"text": "【结果】完成：e"}])]])
        tk = self.add_task("换到用不了的号也要做完")
        self.run_station(f)
        from xay import quota
        self.assertIn("账号2", quota.agy_blocked(home=self.tmp / "v2"))         # 用不了的号记下来了
        self.assertEqual(json.loads(state.read_text())["current"], 3)
        self.assertEqual(self.state()["tasks"][tk]["status"], "reported")

    def test_quota_switch_failure_fails_the_task_with_warning(self):
        exe, log, state = fake_tools(self.tmp, current=1, n=1)           # 只有一个账号
        os.environ["XAY_AGY_TOOLS_BIN"] = str(exe)
        f = Factory([[[{"fail": {"kind": "quota", "error": "429 Too Many Requests"}}]]])
        tk = self.add_task("没得换")
        self.run_station(f)
        self.assertEqual(len(f.instances), 1)
        self.assertEqual(self.state()["tasks"][tk]["status"], "failed")
        sig = self.events("signal.raised", kind="quota")
        self.assertEqual([e["data"]["level"] for e in sig], ["warn"])
        self.assertIn("没有别的可用", sig[0]["data"]["text"])

    def test_daily_cap_counts_old_and_new_calls_and_holds_the_task(self):
        os.environ["XAY2_AGY_DAILY_CAP"] = "3"
        old = self.tmp / "oldxay"
        old.mkdir()
        today = time.strftime("%Y-%m-%d")
        (old / "registry.jsonl").write_text(json.dumps({"kind": "agy_call", "date": today, "calls": 1}) + "\n")  # 旧系统用了 1 次
        f = Factory([[writes("a.txt", "a", [{"text": "【结果】完成：a"}]), writes("b.txt", "b", [{"text": "【结果】完成：b"}])]])
        t1, t2, t3 = self.add_task("一"), self.add_task("二"), self.add_task("三")
        self.run_station(f)
        s = self.state()
        self.assertEqual([s["tasks"][x]["status"] for x in (t1, t2, t3)], ["reported", "reported", "queued"])
        sig = self.events("signal.raised", kind="quota")
        self.assertEqual([e["data"]["level"] for e in sig], ["warn"])
        self.assertIn("3 次", sig[0]["data"]["text"])
        rows = (self.tmp / "v2" / "quota" / "agy-calls.jsonl").read_text().splitlines()
        self.assertEqual(len(rows), 2)
        self.assertEqual(len(f.instances[0].sent), 2)

    def test_compound_verify_command_is_allowed_piece_by_piece(self):
        """`cd V2 && python3 -m unittest …`：agy 要求每一段都放行，所以拆开一段一段放。"""
        f = Factory([[writes("m.py", "x", [{"text": "【结果】完成：m"}])], []], native_compact=False)
        self.add_task("写 m", card="加一个函数。\n验收命令：`cd V2 && python3 -m unittest tests.test_x -q`")
        self.run_station(f)
        allow = f.instances[0].card["allow_commands"]
        for need in ("cd V2 && python3 -m unittest tests.test_x -q", "cd V2", "python3 -m unittest tests.test_x -q"):
            self.assertIn(need, allow)

    def test_verify_commands_from_card_are_allowed_and_failures_land_in_handoff_facts(self):
        card = "加一个函数。\n验收命令：`python3 -m unittest -q`"
        f = Factory([[writes("m.py", "x", [{"tool": "cmd", "text": "python3 -m unittest -q", "output": "x\nFAILED (failures=1)",
                                            "ok": False}, {"text": "【结果】完成：m"}, {"usage_pct": 51}]), MEMO], []],
                    native_compact=False)
        self.add_task("写 m", card=card)
        self.run_station(f)
        self.assertEqual(f.instances[0].card["allow_commands"][0], "python3 -m unittest -q")
        self.assertIn("ls", f.instances[0].card["allow_commands"])              # 几个只读的常用命令也放行（agy 按开头匹配）
        self.assertIn("git grep", f.instances[0].card["allow_commands"])
        self.assertIn("xay2", f.instances[0].card["allow_commands"])            # 员工身份的 xay2 只写发件箱（聊天里改笔记要用）
        self.assertEqual(f.instances[0].card["mode"], "write")
        self.assertIn("只能运行这些命令：`python3 -m unittest -q`", f.instances[0].sent[0][0])   # 事先告诉它权限
        facts = self.state()["notes"][self.em]["active"].split(memo.FACTS_TITLE)[1]
        self.assertIn("验收 `python3 -m unittest -q`：没通过，FAILED (failures=1)", facts)
        self.assertIn("最近失败的命令", facts)
        self.assertIn("负责人原话：任务「写 m」", facts)

    def test_denied_command_auto_retries_in_same_conversation_and_lands(self):
        """第一轮有被拒操作且没改动 → 同一对话自动补一轮（kind=denied_retry）→ 第二轮做完正常合并。"""
        class DeniedFake(FakeAdapter):
            def __init__(self, *args, denied_list=None, **kwargs):
                super().__init__(*args, **kwargs)
                self.denied_list = list(denied_list or [])

            def emit(self, event_kind, /, **data):
                if event_kind == "turn_ended":
                    d = self.denied_list.pop(0) if self.denied_list else None
                    if d:
                        data["denied"] = d
                return super().emit(event_kind, **data)

        class DeniedFactory(Factory):
            def __init__(self, scripts, denied_list):
                super().__init__(scripts)
                self.denied_list = list(denied_list)

            def __call__(self, agent):
                a = DeniedFake(self.agent or agent, denied_list=self.denied_list, **self.fake_kw)
                script = self.scripts.pop(0) if self.scripts else []
                a.script = [self._bind(a, x) for x in script]
                self.instances.append(a)
                return a

        f = DeniedFactory(
            [[[{"text": "【结果】完成：好了"}],
              writes("ok.txt", "content", [{"text": "【结果】完成：这次真好了"}])]],
            denied_list=[["git grep foo"]]
        )
        tk = self.add_task("测试被拒后补一轮")
        self.run_station(f)
        self.assertEqual(len(f.instances), 1)                          # 同一个对话
        self.assertEqual(len(f.instances[0].sent), 2)                  # 补发了一轮
        kinds = [e["data"]["kind"] for e in self.events("turn.started")]
        self.assertEqual(kinds, ["first", "denied_retry"])
        self.assertIn("刚才这些操作被系统拒绝了：git grep foo", f.instances[0].sent[1][0])
        self.assertIn("找代码用你自带的搜索工具", f.instances[0].sent[1][0])
        self.assertEqual(self.state()["tasks"][tk]["status"], "reported")
        self.assertEqual([e["subject"] for e in self.events("task.landed")], [t1 if 't1' in locals() else tk])
        ended = self.events("turn.ended")
        self.assertEqual(ended[0]["data"]["evidence"]["denied"], ["git grep foo"])

    def test_denied_command_second_denied_is_kept_without_further_retry(self):
        """第二轮还被拒 → 不再补，照旧按证据保留（task.kept）。"""
        class DeniedFake(FakeAdapter):
            def __init__(self, *args, denied_list=None, **kwargs):
                super().__init__(*args, **kwargs)
                self.denied_list = list(denied_list or [])

            def emit(self, event_kind, /, **data):
                if event_kind == "turn_ended":
                    d = self.denied_list.pop(0) if self.denied_list else None
                    if d:
                        data["denied"] = d
                return super().emit(event_kind, **data)

        class DeniedFactory(Factory):
            def __init__(self, scripts, denied_list):
                super().__init__(scripts)
                self.denied_list = list(denied_list)

            def __call__(self, agent):
                a = DeniedFake(self.agent or agent, denied_list=self.denied_list, **self.fake_kw)
                script = self.scripts.pop(0) if self.scripts else []
                a.script = [self._bind(a, x) for x in script]
                self.instances.append(a)
                return a

        f = DeniedFactory(
            [[[{"text": "【结果】完成：好了"}],
              [{"text": "【结果】完成：又被拒了"}]]],
            denied_list=[["git grep foo"], ["git grep bar"]]
        )
        tk = self.add_task("测试两次被拒")
        self.run_station(f)
        self.assertEqual(len(f.instances), 1)
        self.assertEqual(len(f.instances[0].sent), 2)                  # 每件活最多自动补一次
        kinds = [e["data"]["kind"] for e in self.events("turn.started")]
        self.assertEqual(kinds, ["first", "denied_retry"])
        self.assertEqual(self.state()["tasks"][tk]["status"], "needs_you")
        self.assertEqual(self.state()["tasks"][tk]["kept_reason"], "mismatch")
        self.assertEqual(self.events("task.landed"), [])

    def test_denied_with_changes_but_no_done_claim_continues_in_same_conversation(self):
        """真机 2026-10-31：命令被拒，agy 这一轮直接结束——改了文件、没报做完。不能按"有改动"直接合并，要在原对话里接着做。"""
        class DeniedFake(FakeAdapter):
            def __init__(self, *args, denied_list=None, **kwargs):
                super().__init__(*args, **kwargs)
                self.denied_list = list(denied_list or [])

            def emit(self, event_kind, /, **data):
                if event_kind == "turn_ended":
                    d = self.denied_list.pop(0) if self.denied_list else None
                    if d:
                        data["denied"] = d
                return super().emit(event_kind, **data)

        class DeniedFactory(Factory):
            def __init__(self, scripts, denied_list):
                super().__init__(scripts)
                self.denied_list = list(denied_list)

            def __call__(self, agent):
                a = DeniedFake(self.agent or agent, denied_list=self.denied_list, **self.fake_kw)
                script = self.scripts.pop(0) if self.scripts else []
                a.script = [self._bind(a, x) for x in script]
                self.instances.append(a)
                return a

        f = DeniedFactory(
            [[writes("half.txt", "half\n", [{"text": "先改了一半"}]),
              writes("rest.txt", "rest\n", [{"text": "【结果】完成：全部做完了"}])]],
            denied_list=[["./bin/xay2 wait --timeout 0"]]
        )
        tk = self.add_task("被拒时只做了一半")
        self.run_station(f)
        self.assertEqual(len(f.instances), 1)                          # 同一个对话
        kinds = [e["data"]["kind"] for e in self.events("turn.started")]
        self.assertEqual(kinds, ["first", "denied_retry"])
        self.assertIn("你已经改过的文件都还在", f.instances[0].sent[1][0])
        landed = self.events("task.landed")
        self.assertEqual(len(landed), 1)                               # 做完才合并，只合并一次
        self.assertEqual(sorted(landed[0]["data"]["files"]), ["half.txt", "rest.txt"])
        self.assertIn("./bin/xay2", station_mod.AGY_SAFE_COMMANDS)

    def test_network_confirm_after_done_claim_retries_and_lands(self):
        """第一轮报了做完、有改动、以网络错误失败 → 同一对话补一轮（kind=network_confirm）→ 第二轮正常结束就合并。"""
        f = Factory(
            [[writes("res.txt", "content\n", [
                {"text": "【结果】完成：写好了"},
                {"fail": {"kind": "network", "error": "API error (attempt 1): request failed: Post https://...: EOF"}}
              ]),
              [{"text": "【结果】完成：验收通过，搞定"}]]],
            agent="agy"
        )
        tk = self.add_task("测试网络断开补一轮")
        self.run_station(f)
        self.assertEqual(len(f.instances), 1)                          # 同一个对话
        self.assertEqual(len(f.instances[0].sent), 2)                  # 补发了一轮
        kinds = [e["data"]["kind"] for e in self.events("turn.started")]
        self.assertEqual(kinds, ["first", "network_confirm"])
        self.assertIn("刚才网络断了一下，这一轮没正常收尾", f.instances[0].sent[1][0])
        self.assertIn("请跑一遍任务卡里的验收命令", f.instances[0].sent[1][0])
        self.assertEqual(self.state()["tasks"][tk]["status"], "reported")
        self.assertEqual([e["subject"] for e in self.events("task.landed")], [tk])

    def test_network_confirm_second_network_error_is_kept_without_further_retry(self):
        """第二轮还是网络错误 → 保留等负责人决定，不再补。"""
        f = Factory(
            [[writes("res.txt", "content\n", [
                {"text": "【结果】完成：写好了"},
                {"fail": {"kind": "network", "error": "API error (attempt 1): request failed: Post https://...: EOF"}}
              ]),
              [{"fail": {"kind": "network", "error": "connection reset by peer"}}]]],
            agent="agy"
        )
        tk = self.add_task("测试网络断开两次")
        self.run_station(f)
        self.assertEqual(len(f.instances), 1)
        self.assertEqual(len(f.instances[0].sent), 2)                  # 每件活最多补一次
        kinds = [e["data"]["kind"] for e in self.events("turn.started")]
        self.assertEqual(kinds, ["first", "network_confirm"])
        self.assertEqual(self.state()["tasks"][tk]["status"], "needs_you")
        self.assertEqual(self.state()["tasks"][tk]["kept_reason"], "failed_with_changes")
        self.assertEqual(self.events("task.landed"), [])

    def test_network_continue_after_incomplete_retries_and_lands(self):
        """假 agy：第一轮改了文件、网络错误失败、没报做完 → 补一轮 → 第二轮做完合并。"""
        f = Factory(
            [[writes("half.txt", "half\n", [
                {"fail": {"kind": "network", "error": "API error (attempt 1): request failed: Post https://...: EOF"}}
              ]),
              writes("rest.txt", "rest\n", [
                {"text": "【结果】完成：剩下的也做完了"}
              ])]],
            agent="agy"
        )
        tk = self.add_task("测试网络断开未做完接着做")
        self.run_station(f)
        self.assertEqual(len(f.instances), 1)                          # 同一个对话
        self.assertEqual(len(f.instances[0].sent), 2)                  # 补发了一轮
        kinds = [e["data"]["kind"] for e in self.events("turn.started")]
        self.assertEqual(kinds, ["first", "network_continue"])
        self.assertIn("刚才网络断了一下，这一轮没做完。你改过的文件都还在", f.instances[0].sent[1][0])
        self.assertIn("接着把任务卡里剩下的做完，跑过验收命令后按格式汇报【结果】。", f.instances[0].sent[1][0])
        self.assertEqual(self.state()["tasks"][tk]["status"], "reported")
        landed = self.events("task.landed")
        self.assertEqual(len(landed), 1)
        self.assertEqual([e["subject"] for e in landed], [tk])
        self.assertEqual(sorted(landed[0]["data"]["files"]), ["half.txt", "rest.txt"])

    def test_network_continue_three_network_errors_is_kept_after_two_retries(self):
        """假 agy：连续 3 次网络错误 → 补 2 次后保留。"""
        f = Factory(
            [[writes("half.txt", "half\n", [
                {"fail": {"kind": "network", "error": "API error (attempt 1): request failed: Post https://...: EOF"}}
              ]),
              [{"fail": {"kind": "network", "error": "API error (attempt 2): request failed: Post https://...: EOF"}}],
              [{"fail": {"kind": "network", "error": "API error (attempt 3): request failed: Post https://...: EOF"}}]]],
            agent="agy"
        )
        tk = self.add_task("测试网络断开连续三次")
        with mock.patch.dict(os.environ, {"XAY2_COMPACT_TURNS_AGY": "10"}):
            self.run_station(f)
        self.assertEqual(len(f.instances), 1)
        self.assertEqual(len(f.instances[0].sent), 3)                  # 第一轮 + 补 2 次
        kinds = [e["data"]["kind"] for e in self.events("turn.started")]
        self.assertEqual(kinds, ["first", "network_continue", "network_continue"])
        self.assertEqual(self.state()["tasks"][tk]["status"], "needs_you")
        self.assertEqual(self.state()["tasks"][tk]["kept_reason"], "failed_with_changes")
        self.assertEqual(self.events("task.landed"), [])


if __name__ == "__main__":
    unittest.main()
