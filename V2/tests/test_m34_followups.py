"""M3 / M4 合并后补的对接缺口（2026-10-30 M4 收尾）：

1. 额度失败认 rate_limit；工位补救过（Codex 换线路 / Gemini 换号）还失败就不再换、交负责人；限流没换过的等一会儿再排一次。
2. 预算的 token 线在一轮中途就管住（工位在 context.updated 里报 turn_tokens）。
3. 总闸强杀工位时，连心跳里报的命令行子进程组一起杀（只杀确实是这个工位起的、自己是组长的）。
5. 叫停后在"需要你"里选"让他接着改"（或 resume）会重新起工位；只剩"合并 / 丢掉"那一步的也照样起来做完。
6. 显示名自动发号：按项目 + AI 种类各自编，跳过本项目里已经被占用的名字（手动起的、别的 AI 的、已离职的），
   已有员工重放后名字不变。
7. 员工详情：对话号可以是 null；agy 的"正在想"写"这家看不到"。
8. "需要你"在页面上看得到的和人的状态一致；没挂对话的"需要你"也看得到。

全部用临时 XAY2_HOME / XAY_HOME，不碰 ~/.xay，也不开端口。
"""
import json
import os
import random
import shutil
import signal
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

from xay.core import append, load_state, open_db, rebuild, snapshot
from xay.core import events as core_events
from xay.core.ids import new_id
from xay.office import employee
from xay.office import scheduler as sched_mod
from xay.station import station as st

from tests.test_rules import RulesCase

WEB = Path(__file__).resolve().parents[1] / "web"


def _gone(pid, timeout=5.0):
    """进程没了（或者只剩僵尸）就算没了。"""
    deadline = time.time() + timeout
    while True:
        out = subprocess.run(["ps", "-p", str(pid), "-o", "stat="], capture_output=True, text=True).stdout.strip()
        if not out or out.startswith("Z"):
            return True
        if time.time() >= deadline:
            return False
        time.sleep(0.05)


# ====================================================================== 6. 显示名自动发号

class LabelTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="xay-label-test-"))
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.conn = open_db(str(self.tmp / "xay.db"))
        self.addCleanup(self.conn.close)
        self.n = 0

    def hire(self, agent, ws, label=None, by="office", note=None):
        self.n += 1
        em = "em_%08x" % self.n
        data = {"agent": agent}
        if label:
            data["label"] = label
        if note:
            data["note"] = note
        append(self.conn, "employee.hired", by, data, ws=ws, subject=em)
        return em

    def label(self, em):
        return load_state(self.conn)["employees"][em]["label"]

    def test_numbers_are_per_project_and_per_ai(self):
        a = [self.hire("dsh", "ws_a") for _ in range(4)]
        self.assertEqual([self.label(x) for x in a], ["DeepSeek 1", "DeepSeek 2", "DeepSeek 3", "DeepSeek 4"])
        self.assertEqual(self.label(self.hire("codex", "ws_a")), "Codex 1")
        # 新项目：每种 AI 都从 1 开始
        self.assertEqual(self.label(self.hire("dsh", "ws_b")), "DeepSeek 1")
        self.assertEqual(self.label(self.hire("agy", "ws_b")), "Gemini 1")

    def test_fired_numbers_are_not_reused(self):
        a, b = self.hire("dsh", "ws_a"), self.hire("dsh", "ws_a")
        append(self.conn, "employee.fired", "user", {"reason": "测试"}, ws="ws_a", subject=b)
        self.assertEqual(self.label(self.hire("dsh", "ws_a")), "DeepSeek 3")
        self.assertEqual(self.label(a), "DeepSeek 1")

    def test_auto_number_skips_names_already_taken(self):
        """负责人在演示时手动起过 "DeepSeek 1"：之后自动发号跳过它，不撞名、不报错。"""
        manual = self.hire("dsh", "ws_a", label="DeepSeek 1", by="lead:cv_x")
        auto = self.hire("dsh", "ws_a")
        self.assertEqual((self.label(manual), self.label(auto)), ("DeepSeek 1", "DeepSeek 2"))
        # 别的 AI 手动用了 "Gemini 1" 这个名字 → 第一个自动编号的 Gemini 跳过它
        self.hire("codex", "ws_a", label="Gemini 1", by="lead:cv_x")
        self.assertEqual(self.label(self.hire("agy", "ws_a")), "Gemini 2")
        # 手动起的人离职了：名字也不回收
        gone = self.hire("codex", "ws_a", label="Codex 1", by="lead:cv_x")
        append(self.conn, "employee.fired", "user", {"reason": "测试"}, ws="ws_a", subject=gone)
        self.assertEqual(self.label(self.hire("codex", "ws_a")), "Codex 2")
        # 手动起名撞上已有的名字照样拒绝（协议 §4.1）
        with self.assertRaises(ValueError):
            self.hire("dsh", "ws_a", label="DeepSeek 2", by="lead:cv_x")
        # 整本账重放一遍：名字都不变
        before = {em: e["label"] for em, e in load_state(self.conn)["employees"].items()}
        rebuild(self.conn)
        self.assertEqual({em: e["label"] for em, e in load_state(self.conn)["employees"].items()}, before)
        labels = [e["label"] for e in load_state(self.conn)["employees"].values() if e["ws"] == "ws_a"]
        self.assertEqual(len(labels), len(set(labels)))

    def test_existing_names_do_not_change_after_replay(self):
        """改发号规则不能改动已经存在的员工的名字：随机造一本"老规则下不会撞名"的账（自动 / 手动 / 离职 / 旧系统镜像、
        几个项目混着来），新规则给出的名字和老规则（本项目同种 AI 现有最大号 +1）逐个一样。"""
        prefix = {"codex": "Codex", "dsh": "DeepSeek", "agy": "Gemini"}
        rng = random.Random(20261030)
        old = {}                       # em -> (ws, agent, label)：老规则算出来的
        for _ in range(200):
            ws, agent = rng.choice(["ws_a", "ws_b", "ws_c"]), rng.choice(list(prefix))
            r = rng.random()
            nums = [int(l.split()[-1]) for (w, a, l) in old.values()
                    if w == ws and a == agent and l.startswith(prefix[agent] + " ") and l.split()[-1].isdigit()]
            if r < 0.15:               # 手动起名：同种 AI 的前缀、一个还没人用的号
                taken = {l for (w, _, l) in old.values() if w == ws}
                n = rng.randint(1, 30)
                label = "%s %d" % (prefix[agent], n)
                if label in taken:
                    continue
                em = self.hire(agent, ws, label=label, by="lead:cv_x")
                old[em] = (ws, agent, label)
                continue
            em = self.hire(agent, ws, note="旧系统 · 某组" if r < 0.4 else None, by="bridge" if r < 0.4 else "office")
            old[em] = (ws, agent, "%s %d" % (prefix[agent], max(nums or [0]) + 1))
            if r > 0.9:
                append(self.conn, "employee.fired", "user", {"reason": "测试"}, ws=ws, subject=em)
        got = {em: e["label"] for em, e in load_state(self.conn)["employees"].items()}
        self.assertEqual(got, {em: v[2] for em, v in old.items()})


# ====================================================================== 2 / 3 / 7 / 8：账本和快照

class LedgerFollowupTests(unittest.TestCase):
    WS, CV, CV2 = "ws_f0000001", "cv_f1", "cv_f2"

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="xay-m34-test-"))
        self.addCleanup(shutil.rmtree, self.tmp, True)
        env = mock.patch.dict(os.environ, {"XAY2_HOME": str(self.tmp / "v2"), "XAY_HOME": str(self.tmp / "old")})
        env.start()
        self.addCleanup(env.stop)
        self.conn = open_db(str(self.tmp / "v2" / "xay.db"))
        self.addCleanup(self.conn.close)
        for cv in (self.CV, self.CV2):
            append(self.conn, "conv.begun", "lead:" + cv, {"title": "测试", "lead_kind": "claude", "aliases": [], "cwd": "/tmp/x"},
                   ws=self.WS, conv=cv, subject=cv, ts=1)
        self.em = "em_f0000001"
        append(self.conn, "employee.hired", "lead:" + self.CV, {"agent": "agy"}, ws=self.WS, conv=self.CV, subject=self.em, ts=2)
        append(self.conn, "task.added", "lead:" + self.CV, {"title": "活", "owner": self.em, "deps": [], "writes": [], "mode": "write",
                                                            "reason": "r"}, ws=self.WS, conv=self.CV, subject="tk_f0000001", ts=3)

    def A(self, typ, by, data, subject, conv=None, ts=None):
        return append(self.conn, typ, by, data, ws=self.WS, conv=conv or self.CV, subject=subject, ts=ts)

    def test_turn_tokens_are_kept_on_the_turn(self):
        S = "station:" + self.em
        self.A("turn.started", S, {"task": "tk_f0000001", "em": self.em, "kind": "first"}, "tn_f0000001", ts=10)
        ctx = {"used": 1000, "window": 256000, "pct": 0.4, "source": "agy_input", "estimated": True}
        self.A("context.updated", S, dict(ctx, turn="tn_f0000001", turn_tokens=20000), self.em, ts=11)
        self.A("context.updated", S, dict(ctx, turn="tn_f0000001", turn_tokens=40000), self.em, ts=12)
        self.assertEqual(load_state(self.conn)["turns"]["tn_f0000001"]["turn_tokens"], 40000)
        self.A("context.updated", S, dict(ctx, turn="tn_nope", turn_tokens=99), self.em, ts=13)        # 不认识的轮：不管
        self.A("turn.ended", S, {"em": self.em, "status": "completed", "evidence": {"cli_status": "completed", "changed_files": []},
                                 "usage": {"in": 1, "out": 1, "cached": 0, "total": 45000}}, "tn_f0000001", ts=14)
        self.A("context.updated", S, dict(ctx, turn="tn_f0000001", turn_tokens=45000), self.em, ts=15)    # 收工后再报一次最终数
        tr = load_state(self.conn)["turns"]["tn_f0000001"]
        self.assertEqual((tr["turn_tokens"], tr["usage"]["total"], tr["status"]), (45000, 45000, "completed"))
        before = load_state(self.conn)
        rebuild(self.conn)
        self.assertEqual(load_state(self.conn), before)

    def test_heartbeat_keeps_cli_pids_until_offline(self):
        S = "station:" + self.em
        self.A("station.heartbeat", S, {"pid": 4242, "cli_pids": [5151, 6161, "x", True, 1]}, self.em)
        self.assertEqual(load_state(self.conn)["employees"][self.em]["cli_pids"], [5151, 6161])
        self.A("station.heartbeat", S, {"pid": 4242}, self.em)                    # 旧工位不带：保留上一条报的
        self.assertEqual(load_state(self.conn)["employees"][self.em]["cli_pids"], [5151, 6161])
        self.A("employee.offline", S, {"reason": "idle"}, self.em)
        self.assertNotIn("cli_pids", load_state(self.conn)["employees"][self.em])

    def test_null_native_id_does_not_break_the_panel_or_the_page(self):
        S = "station:" + self.em
        self.A("employee.session", S, {"native_id": None, "segment": 2, "reason": "resume_failed"}, self.em, ts=20)
        self.A("turn.started", S, {"task": "tk_f0000001", "em": self.em, "kind": "first"}, "tn_f0000002", ts=21)
        e = load_state(self.conn)["employees"][self.em]
        self.assertIsNone(e["native_id"])
        snap = snapshot.build(self.conn, conv=self.CV, now=30)
        self.assertEqual(snap["people"][self.em]["context"]["segment"], 2)
        body = employee.build(self.conn, self.em, conv=self.CV, live_root=self.tmp / "v2" / "live", now=30)
        self.assertEqual((body["person"]["agent"], body["person"]["state"]), ("agy", "working"))
        self.assertNotIn("native_id", json.dumps(body))
        self.A("employee.session", S, {"native_id": "conv-123", "segment": 2, "reason": "resume_failed"}, self.em, ts=22)
        self.assertEqual(load_state(self.conn)["employees"][self.em]["context"]["segment"], 2)

    def test_decisions_on_the_page_match_who_is_waiting(self):
        """页面上的"需要你"和人的"等人答复"说的是同一件事：没挂对话的（员工的预算）也看得到；
        别的对话的不算进这一页；处理完（你选了 / 管家自动关掉）就都不见了。"""
        dc_none, dc_other = "dc_f0000001", "dc_f0000002"
        append(self.conn, "decision.opened", "rules", {"kind": "budget", "title": "今天的预算用完了", "detail": "", "em": self.em,
                                                       "options": [{"id": "extend", "label": "加"}, {"id": "rest", "label": "歇"}],
                                                       "owner": "lead", "budget": {"scope": "employee", "dim": "turns"}},
               ws=self.WS, subject=dc_none)
        snap = snapshot.build(self.conn, conv=self.CV, now=30)
        self.assertEqual([d["id"] for d in snap["decisions"]], [dc_none])
        self.assertEqual(snap["people"][self.em]["state"], "blocked")
        self.assertEqual([d["id"] for d in snapshot.build(self.conn, conv=self.CV2, now=30)["decisions"]], [dc_none])
        append(self.conn, "decision.resolved", "rules", {"option": "rest", "text": "管家自动关掉：已经过了一天"}, ws=self.WS,
               subject=dc_none)
        snap = snapshot.build(self.conn, conv=self.CV, now=30)
        self.assertEqual((snap["decisions"], snap["people"][self.em]["state"]), ([], "idle"))
        # 别的对话里的"需要你"：那一页有，这一页没有，人在这一页也不显示"等人答复"
        self.A("decision.opened", "station:" + self.em, {"kind": "interrupted", "title": "改动先保留了", "detail": "", "task": "tk_f0000001",
                                                       "em": self.em, "options": [], "owner": "lead"}, dc_other, conv=self.CV2)
        mine, other = snapshot.build(self.conn, conv=self.CV, now=30), snapshot.build(self.conn, conv=self.CV2, now=30)
        self.assertEqual((mine["decisions"], mine["people"][self.em]["state"]), ([], "idle"))
        self.assertEqual(([d["id"] for d in other["decisions"]], other["people"][self.em]["state"]), ([dc_other], "blocked"))
        web_js = (WEB / "lib" / "app.js").read_text(encoding="utf-8")
        self.assertIn("(data.decisions || []).filter(d => d && !d.delegated).length", web_js)   # 顶栏"待答需求"也数上"需要你"（交出去的不算，office-spec.md 二½）



class QuotaFailureTests(RulesCase):
    def fail_with(self, em, tk, kind, error="出错了"):
        tn = new_id("tn_")
        append(self.conn, "turn.started", "station:" + em, {"task": tk, "em": em, "kind": "first"}, ws=self.ws, conv=self.conv, subject=tn)
        append(self.conn, "turn.ended", "station:" + em, {"em": em, "status": "failed", "evidence": {
            "cli_status": "failed", "changed_files": [], "diff_stat": {"files": 0, "add": 0, "del": 0}, "error": error,
            "error_kind": kind}}, ws=self.ws, conv=self.conv, subject=tn)
        append(self.conn, "task.failed", "station:" + em, {"reason": "这一轮失败了，也没有留下任何改动", "detail": error},
               ws=self.ws, conv=self.conv, subject=tk)

    def retries(self, tk):
        return [e["data"]["retry"] for e in self.events("task.queued") if e["subject"] == tk and e["data"].get("retry")]

    def test_rate_limit_counts_as_quota_and_after_a_line_switch_goes_to_lead(self):
        em = self.hire("codex")
        tk = self.add("被限流的活", owner=em)
        s = self.sched()
        s.tick()
        self.turn(em, tk, status="failed", error="Rate limit reached for requests")     # 第一轮被限流
        # 工位先自己换了线路（只写了 employee.session{line_switch}，这里不靠提醒认），同一个对话再做一轮，还是被限流
        append(self.conn, "employee.session", "station:" + em, {"native_id": "th_1", "segment": 1, "reason": "line_switch",
                                                                "line": "codex-relay"}, ws=self.ws, subject=em)
        self.fail_with(em, tk, "rate_limit", "Rate limit reached for requests")
        s.tick()
        self.assertEqual(self.retries(tk), [])
        dec = self.decisions("failure")
        self.assertEqual(len(dec), 1)
        self.assertEqual(dec[0]["cause"], "quota")
        self.assertIn("已经换过额度线路", dec[0]["detail"])
        self.assertEqual(self.events("letter.sent"), [])

    def test_unswitched_rate_limit_waits_and_requeues_once(self):
        self.react({"failure": {"retry_delay_s": 0, "rate_limit_wait_s": 60}})
        g = self.hire("agy")                                        # Gemini 只在"额度用完"时换号，限流不换
        tk = self.add("Gemini 被限流", owner=g)
        s = self.sched()
        now = time.time()
        s.tick(now)
        self.fail_with(g, tk, "rate_limit", "429 Too Many Requests")
        s.tick(now + 5)
        self.assertEqual((self.status(tk), self.retries(tk)), ("failed", []))     # 先等一会儿
        s.tick(now + 70)
        self.assertEqual((self.status(tk), self.retries(tk)), ("queued", ["env"]))
        self.assertEqual(self.events("letter.sent"), [])                           # 不是员工的错，不写信
        # 再限流一次：交负责人
        self.fail_with(g, tk, "rate_limit", "429 Too Many Requests")
        s.tick(time.time() + 70)
        dec = self.decisions("failure")
        self.assertEqual([d["cause"] for d in dec], ["quota"])
        self.assertIn("被限流", dec[0]["detail"])
        self.assertIn("没有别的线路或账号可换", dec[0]["detail"])

    def test_agy_account_switch_counts_as_remedied_but_daily_cap_note_does_not(self):
        g = self.hire("agy")
        tk = self.add("Gemini 额度用完", owner=g)
        s = self.sched()
        s.tick()
        # "今天 40 次到了"那条提醒（幂等键 agy-cap:）不算换过号
        append(self.conn, "signal.raised", "station:" + g, {"kind": "quota", "level": "warn", "text": "今天 Gemini 已经调用了 40 次"},
               ws=self.ws, conv=self.conv, subject=g, dedup="agy-cap:%s:%s" % (g, time.strftime("%Y-%m-%d")))
        self.fail_with(g, tk, "quota", "RESOURCE_EXHAUSTED: quota")
        s.tick()
        dec = self.decisions("failure")
        self.assertEqual(len(dec), 1)
        self.assertIn("没有别的线路或账号可换", dec[0]["detail"])
        # 另一件：工位换过号（remind）又额度用完 → "已经换过 Gemini 账号"
        tk2 = self.add("Gemini 又额度用完", owner=g)
        s.tick()
        tn = new_id("tn_")
        append(self.conn, "turn.started", "station:" + g, {"task": tk2, "em": g, "kind": "first"}, ws=self.ws, conv=self.conv, subject=tn)
        append(self.conn, "signal.raised", "station:" + g, {"kind": "quota", "level": "remind",
                                                            "text": "Gemini 已从「甲」换到「乙」，同一个对话接着做"},
               ws=self.ws, conv=self.conv, subject=g)
        append(self.conn, "turn.ended", "station:" + g, {"em": g, "status": "failed", "evidence": {
            "cli_status": "failed", "changed_files": [], "error": "quota", "error_kind": "quota"}}, ws=self.ws, conv=self.conv, subject=tn)
        self.fail_with(g, tk2, "quota", "RESOURCE_EXHAUSTED: quota")
        s.tick()
        dec2 = [d for d in self.decisions("failure") if d["task"] == tk2]
        self.assertEqual(len(dec2), 1)
        self.assertIn("已经换过 Gemini 账号", dec2[0]["detail"])
        self.assertEqual(self.retries(tk2), [])
        # 第三件：工位想换号没换成（warn）→ 也交负责人，说清楚是"没换成"
        tk3 = self.add("Gemini 换号没换成", owner=g)
        s.tick()
        tn = new_id("tn_")
        append(self.conn, "turn.started", "station:" + g, {"task": tk3, "em": g, "kind": "first"}, ws=self.ws, conv=self.conv, subject=tn)
        append(self.conn, "signal.raised", "station:" + g, {"kind": "quota", "level": "warn",
                                                            "text": "Gemini 额度用完，自动换账号没成功：Antigravity Tools 里没有别的可用 Gemini 账号"},
               ws=self.ws, conv=self.conv, subject=g)
        append(self.conn, "turn.ended", "station:" + g, {"em": g, "status": "failed", "evidence": {
            "cli_status": "failed", "changed_files": [], "error": "quota", "error_kind": "quota"}}, ws=self.ws, conv=self.conv, subject=tn)
        append(self.conn, "task.failed", "station:" + g, {"reason": "这一轮失败了，也没有留下任何改动", "detail": "RESOURCE_EXHAUSTED"},
               ws=self.ws, conv=self.conv, subject=tk3)
        s.tick()
        dec3 = [d for d in self.decisions("failure") if d["task"] == tk3]
        self.assertEqual(len(dec3), 1)
        self.assertIn("试着换 Gemini 账号没换成", dec3[0]["detail"])
        self.assertEqual(self.retries(tk3), [])

    def test_codex_already_on_relay_has_nothing_to_switch_to(self):
        em = self.hire("codex")
        append(self.conn, "employee.session", "station:" + em, {"native_id": "th_1", "segment": 1, "reason": "line_switch",
                                                                "line": "codex-relay"}, ws=self.ws, subject=em)
        tk = self.add("中转站也满了", owner=em)
        s = self.sched()
        s.tick()
        self.fail_with(em, tk, "capacity", "Selected model is at capacity")
        s.tick()
        dec = self.decisions("failure")
        self.assertEqual(len(dec), 1)
        self.assertIn("已经在中转站上了", dec[0]["detail"])

    def test_daily_cap_failure_is_requeued_without_a_letter(self):
        g = self.hire("agy")
        tk = self.add("Gemini 的活", owner=g)
        s = self.sched()
        s.tick()
        self.fail_with(g, tk, "daily_cap", "今天的 Gemini 调用次数到上限了，这一轮没发出去")
        s.tick()
        self.assertEqual((self.status(tk), self.retries(tk)), ("queued", ["env"]))
        self.assertEqual(self.events("letter.sent"), [])


class MidTurnBudgetTests(RulesCase):
    def ctx(self, em, tn, tokens):
        append(self.conn, "context.updated", "station:" + em, {"used": 1, "window": 10, "pct": 10, "source": "codex_last",
                                                              "estimated": False, "turn": tn, "turn_tokens": tokens},
               ws=self.ws, subject=em)

    def test_task_tokens_wrap_up_at_90_and_interrupt_at_100_mid_turn(self):
        em = self.hire()
        tk = self.add2("费 token 的活", owner=em, budget={"tokens": 100000})
        s = self.sched()
        now = time.time()
        s.tick(now)
        tn = self.turn(em, tk, started=now, ended=False)
        self.ctx(em, tn, 60000)
        s.tick(now + 1)
        self.assertEqual(self.events("budget.warned"), [])
        self.ctx(em, tn, 92000)
        s.tick(now + 2)
        ctl = [(e["data"]["action"], e["subject"]) for e in self.events("control.requested")]
        self.assertEqual(ctl, [("steer", em)])
        self.assertIn("请收尾", self.events("control.requested")[0]["data"]["text"])
        self.assertIn("token 92k/100k", self.events("control.requested")[0]["data"]["text"])
        self.ctx(em, tn, 101000)
        s.tick(now + 3)
        ctl = [(e["data"]["action"], e["subject"]) for e in self.events("control.requested")]
        self.assertEqual(ctl, [("steer", em), ("interrupt", em)])               # 这一轮还没结束就打断
        self.assertEqual([(e["subject"], e["data"]["dim"]) for e in self.events("budget.stopped")], [(tk, "tokens")])
        self.assertEqual(load_state(self.conn)["turns"][tn]["status"], "working")

    def test_employee_tokens_stop_mid_turn(self):
        self.react({"failure": {"retry_delay_s": 0}, "budget": {"employee": {"tokens": 50000, "minutes": 0, "turns": 0}}})
        em = self.hire()
        tk = self.add("活", owner=em)
        s = self.sched()
        s.tick()
        tn = self.turn(em, tk, ended=False)
        self.ctx(em, tn, 51000)
        s.tick()
        ctl = [(e["data"]["action"], e["subject"]) for e in self.events("control.requested")]
        self.assertEqual(ctl, [("stop", em)])
        dec = self.decisions("budget")
        self.assertEqual((dec[0]["em"], dec[0]["budget"]["dim"], dec[0]["conv"]), (em, "tokens", self.conv))


class StopThenReworkTests(RulesCase):
    def stop_mid_task(self, em, tk, s, now):
        """叫停：工位打断这一轮、记"已停下"、开甲类"需要你"、下班（这里替工位写）。"""
        tn = self.turn(em, tk, started=now, ended=False)
        append(self.conn, "control.requested", self.lead, {"action": "stop", "by_source": "cli"}, ws=self.ws, conv=self.conv,
               subject=em)
        append(self.conn, "turn.ended", "station:" + em, {"em": em, "status": "interrupted", "evidence": {
            "cli_status": "interrupted", "changed_files": ["a.txt"], "diff_stat": {"files": 1, "add": 1, "del": 0}}},
               ws=self.ws, conv=self.conv, subject=tn)
        append(self.conn, "task.kept", "station:" + em, {"reason": "paused", "detail": "叫停了"}, ws=self.ws, conv=self.conv, subject=tk)
        dc = new_id("dc_")
        append(self.conn, "decision.opened", "station:" + em, {"kind": "failed_with_changes", "title": "改动先保留了", "detail": "",
                                                               "task": tk, "em": em, "owner": "lead",
                                                               "options": [{"id": "merge", "label": "合并改动"},
                                                                           {"id": "rework", "label": "让他接着改"},
                                                                           {"id": "discard", "label": "丢掉改动", "danger": True}]},
               ws=self.ws, conv=self.conv, subject=dc)
        append(self.conn, "employee.offline", "station:" + em, {"reason": "stopped"}, ws=self.ws, subject=em)
        self.spawned.clear()
        s.tick(now + 20)
        self.assertNotIn(em, self.spawned)
        return dc

    def test_rework_after_stop_restarts_the_station(self):
        em = self.hire()
        tk = self.add("要叫停的活", owner=em)
        s = self.sched()
        now = time.time()
        s.tick(now)
        self.assertEqual(self.spawned, [em])
        dc = self.stop_mid_task(em, tk, s, now)
        self.answer(dc, "rework", by="user")                       # 页面上选"让他接着改"
        self.assertEqual(self.status(tk), "queued")
        s.tick(now + 60)
        self.assertEqual(self.spawned, [em])
        # 管家重启（从头扫一遍账本）也一样
        self.spawned.clear()
        self.sched().tick(now + 80)
        self.assertEqual(self.spawned, [em])

    def test_resume_after_stop_restarts_the_station(self):
        em = self.hire()
        tk = self.add("要叫停的活", owner=em)
        s = self.sched()
        now = time.time()
        s.tick(now)
        self.stop_mid_task(em, tk, s, now)
        t2 = self.add2("排着的活", owner=em)
        append(self.conn, "control.requested", self.lead, {"action": "stop", "by_source": "cli"}, ws=self.ws, conv=self.conv,
               subject=em)
        s.tick(now + 40)
        self.assertEqual((self.status(t2), self.spawned), ("queued", []))
        append(self.conn, "control.requested", self.lead, {"action": "resume", "by_source": "cli"}, ws=self.ws, conv=self.conv,
               subject=em)                                         # xay2 resume <员工>
        s.tick(now + 60)
        self.assertEqual(self.spawned, [em])

    def test_merge_after_stop_runs_just_that_step(self):
        em = self.hire()
        tk = self.add("要叫停的活", owner=em)
        s = self.sched()
        now = time.time()
        s.tick(now)
        dc = self.stop_mid_task(em, tk, s, now)
        self.answer(dc, "merge", by="user")
        self.assertEqual(load_state(self.conn)["tasks"][tk]["desk_action"], "merge")
        s.tick(now + 40)
        self.assertEqual(self.spawned, [em])                       # 只做合并这一步（没有排着的活）
        # 还有排着的活：叫停照旧，不起
        em2 = self.hire()
        tk2 = self.add("另一个人的活", owner=em2)
        s.tick(now + 60)
        dc2 = self.stop_mid_task(em2, tk2, s, now + 60)
        t3 = self.add("他排着的下一件", owner=em2)
        append(self.conn, "control.requested", self.lead, {"action": "stop", "by_source": "cli"}, ws=self.ws, conv=self.conv,
               subject=em2)
        self.answer(dc2, "merge", by="user")
        self.spawned.clear()
        s.tick(now + 100)
        self.assertNotIn(em2, self.spawned)
        self.assertEqual(self.status(t3), "queued")


class HaltKillsCliGroupsTests(RulesCase):
    def test_cli_process_groups_are_killed_with_the_station(self):
        self.react({"stop": {"term_after_s": 0.5, "kill_after_s": 0.5}})
        em = self.hire()
        lock = st.lock_path(self.home, em)
        lock.parent.mkdir(parents=True, exist_ok=True)
        ready, child_file = self.tmp / "ready", self.tmp / "child.pid"
        # 假工位：忽略 SIGTERM，自己再起一个"命令行"子进程（start_new_session，自己是组长，也忽略 SIGTERM）
        child_code = "import signal,time\nsignal.signal(signal.SIGTERM, signal.SIG_IGN)\ntime.sleep(60)\n"
        code = ("import fcntl,os,signal,subprocess,sys,time\n"
                "signal.signal(signal.SIGTERM, signal.SIG_IGN)\n"
                "fh=open(sys.argv[1],'a+'); fcntl.flock(fh, fcntl.LOCK_EX)\n"
                "open(sys.argv[2],'w').write(str(os.getpid()))\n"
                "c=subprocess.Popen([sys.executable,'-c',sys.argv[5]], start_new_session=True)\n"
                "open(sys.argv[4],'w').write(str(c.pid))\n"
                "open(sys.argv[3],'w').write('ok')\n"
                "time.sleep(60)\n")
        proc = subprocess.Popen([sys.executable, "-c", code, str(lock), str(st.pid_path(self.home, em)), str(ready), str(child_file),
                                 child_code, "xay.station", "run", "--em", em], start_new_session=True)
        self.addCleanup(proc.wait)
        self.addCleanup(lambda: proc.poll() is None and os.killpg(proc.pid, signal.SIGKILL))
        # 不相干的进程组组长（pid 可能被复用成这样的进程）：报在 cli_pids 里也不能误杀
        decoy = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"], start_new_session=True)
        self.addCleanup(decoy.wait)
        self.addCleanup(lambda: decoy.poll() is None and os.killpg(decoy.pid, signal.SIGKILL))
        deadline = time.time() + 10
        while not ready.exists() and time.time() < deadline:
            time.sleep(0.05)
        child = int(child_file.read_text())
        self.addCleanup(lambda: _gone(child, 0) or os.killpg(child, signal.SIGKILL))
        append(self.conn, "station.heartbeat", "station:" + em, {"pid": proc.pid, "cli_pids": [child, decoy.pid]}, ws=self.ws,
               subject=em)
        append(self.conn, "control.requested", self.lead, {"action": "stop_all", "by_source": "cli"}, ws=self.ws, conv=self.conv)
        s = self.sched()
        t0 = time.time()
        s.tick(t0)
        s.tick(t0 + 0.6)                     # 强制结束：工位和命令行都不理 SIGTERM
        self.assertIsNone(proc.poll())
        self.assertFalse(_gone(child, 0.3))
        s.tick(t0 + 1.2)                     # 直接杀掉：连命令行的进程组一起
        proc.wait(timeout=5)
        self.assertEqual(proc.returncode, -signal.SIGKILL)
        self.assertTrue(_gone(child), "命令行子进程还活着")
        self.assertIsNone(decoy.poll())       # 不是这个工位起的：没动它

    def test_targets_require_group_leader_and_station_parent(self):
        s = self.sched()
        child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])        # 不是组长
        self.addCleanup(child.wait)
        self.addCleanup(lambda: child.poll() is None and child.kill())
        state = {"employees": {"em_x": {"cli_pids": [child.pid, os.getpid(), 1, 999999]}}}
        self.assertEqual(s._cli_targets(state, "em_x", os.getpid()), [])
        self.assertEqual(sched_mod._ps(999999), None)


if __name__ == "__main__":
    unittest.main()
