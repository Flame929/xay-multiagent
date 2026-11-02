"""M4 管家自动化：选人三步规则、反应表、预算、总闸（V2/xay/office/{scheduler,rules,budget,reactions}.py）。

这里只在账本上模拟工位写的事件（不起任何进程，最后一组除外：总闸强制结束要一个真进程），用 Scheduler.tick(now=…)
控制时间。真起工位、用假 AI 跑完整流程的场景测试在 test_m4_scenarios.py。
"""
import json
import os
import signal
import subprocess
import sys
import time
import unittest
from unittest import mock

from xay.core import append, load_state, snapshot
from xay.core.ids import new_id
from xay.office import budget as bud
from xay.office import reactions
from xay.office import rules as rules_mod
from xay.office import web
from xay.station import station as st

from tests.test_scheduler import SchedulerCase


class RulesCase(SchedulerCase):
    def setUp(self):
        super().setUp()
        self.react({"failure": {"retry_delay_s": 0}})

    def react(self, over):
        self.home.mkdir(parents=True, exist_ok=True)
        (self.home / "reactions.json").write_text(json.dumps(over), encoding="utf-8")
        reactions._cache["cfg"] = None

    def sched(self, **kw):
        # 这里只在账本上模拟工位，没有真工位报心跳：把心跳看门狗放宽，免得"快进时间"时被当成崩溃
        kw.setdefault("heartbeat_timeout", 10 ** 9)
        return super().sched(**kw)

    # ---- 模拟工位写的事件 ----
    def turn(self, em, tk, *, started=None, ended=None, status="completed", files=(), error=None, usage=None,
             kind="first", conv=None):
        tn = new_id("tn_")
        conv = conv or self.conv
        append(self.conn, "turn.started", "station:" + em, {"task": tk, "em": em, "kind": kind},
               ws=self.ws, conv=conv, subject=tn, ts=started)
        if ended is not False:
            ev = {"cli_status": status, "changed_files": list(files), "diff_stat": {"files": len(files), "add": 1, "del": 0}}
            if error:
                ev["error"] = error
            data = {"em": em, "status": status, "evidence": ev}
            if usage:
                data["usage"] = usage
            append(self.conn, "turn.ended", "station:" + em, data, ws=self.ws, conv=conv, subject=tn, ts=ended)
        return tn

    def failed(self, em, tk, reason="这一轮失败了，也没有留下任何改动", error="测试挂了"):
        self.turn(em, tk, status="failed", error=error)
        append(self.conn, "task.failed", "station:" + em, {"reason": reason, "detail": error}, ws=self.ws, conv=self.conv,
               subject=tk)

    def done(self, tk):
        append(self.conn, "task.landed", "office", {"files": [], "add": 0, "del": 0}, ws=self.ws, conv=self.conv, subject=tk)
        append(self.conn, "task.accepted", self.lead, {}, ws=self.ws, conv=self.conv, subject=tk)

    def decisions(self, kind=None):
        return [d for d in load_state(self.conn)["decisions"].values() if kind is None or d["kind"] == kind]

    def answer(self, dc, option, by=None):
        append(self.conn, "decision.resolved", by or self.lead, {"option": option}, ws=self.ws, conv=self.conv, subject=dc)

    def add2(self, title, **kw):
        writes = kw.pop("writes", None)
        budget = kw.pop("budget", None)
        tk = self.add(title, **kw)
        fields = {}
        if writes is not None:
            fields["writes"] = writes
        if budget is not None:
            fields["budget"] = budget
        if fields:
            append(self.conn, "task.changed", self.lead, {"fields": fields, "reason": "测试"}, ws=self.ws, conv=self.conv,
                   subject=tk)
        return tk


# ====================================================================== 选人三步规则

class PickTests(RulesCase):
    def owner(self, tk):
        return load_state(self.conn)["tasks"][tk]["owner"]

    def reason(self, tk):
        return load_state(self.conn)["tasks"][tk]["assigned_reason"]

    def test_dependency_goes_to_who_did_the_previous_one(self):
        a, b = self.hire(), self.hire()          # Codex 1、Codex 2，都空着
        first = self.add("先做接口", owner=b, key="api")
        s = self.sched()
        s.tick()
        self.turn(b, first)
        self.done(first)
        nxt = self.add("接着做页面", agent="codex", deps=["api"])
        s.tick()
        s.tick()
        self.assertEqual(self.owner(nxt), b)
        self.assertIn("交给 Codex 2：这件活接着他做的「先做接口」", self.reason(nxt))

    def test_overlapping_files_go_to_same_employee(self):
        a, b = self.hire(), self.hire()
        t1 = self.add2("改首页", owner=b, writes=["web/index.html"])
        s = self.sched()
        s.tick()
        self.turn(b, t1, files=["web/index.html", "web/app.js"])
        self.done(t1)
        t2 = self.add2("首页加按钮", agent="codex", writes=["web/app.js"])
        s.tick()
        self.assertEqual(self.owner(t2), b)
        self.assertIn("他最近做的「改首页」改过同一批文件（web/app.js）", self.reason(t2))
        # 只看最近 3 件：他又做了 3 件别的，就不再算
        for i in range(3):
            tk = self.add2("别的活%d" % i, owner=b, writes=["docs/%d.md" % i])
            s.tick()
            self.turn(b, tk, files=["docs/%d.md" % i])
            self.done(tk)
        t3 = self.add2("首页再改", agent="codex", writes=["web/app.js"])
        s.tick()
        self.assertIn("交给空闲的 Codex 1", self.reason(t3))

    def test_same_department(self):
        append(self.conn, "dept.opened", self.lead, {"name": "页面部", "mission": "", "owns": ["web/**"], "head": "rules"},
               ws=self.ws, conv=self.conv, subject="fe")
        a, b = self.hire(), self.hire()
        append(self.conn, "employee.joined", self.lead, {"dept": "fe"}, ws=self.ws, conv=self.conv, subject=b)
        tk = self.add2("改样式", agent="codex", writes=["web/org.css"])
        self.sched().tick()
        self.assertEqual(self.owner(tk), b)
        self.assertIn("交给 Codex 2：他是页面部的", self.reason(tk))

    def test_busy_old_hand_long_wait_hires_short_wait_queues(self):
        self.react({"pick": {"default_task_minutes": 30}})     # 没有历史时一件活按 30 分钟估
        a = self.hire()
        t1 = self.add2("改首页", owner=a, writes=["web/index.html"])
        s = self.sched()
        s.tick()                                   # t1 排进 Codex 1 的队列 → 他正忙
        t2 = self.add2("首页加按钮", agent="codex", deps=[t1])
        append(self.conn, "task.changed", self.lead, {"fields": {"deps": []}, "reason": "测试"}, ws=self.ws, conv=self.conv,
               subject=t2)
        # 依赖没了，但 writes 也没有：没有接得上的理由 → 没人空着 → 招新人
        s.tick()
        self.assertNotEqual(self.owner(t2), a)
        self.assertIn("招了 Codex 2", self.reason(t2))
        # 同一批文件、他正忙、没有历史（按默认 10 分钟估）→ 等 ≥10 分钟、有空名额 → 先给空着的 Codex 2
        self.done(t2)
        t3 = self.add2("首页再加一个按钮", agent="codex", writes=["web/index.html"])
        self.turn(a, t1, files=["web/index.html"], ended=False)     # Codex 1 正在做 t1
        s.tick()
        self.assertEqual(self.owner(t3), self.owner(t2))
        self.assertIn("Codex 1 正忙（预计还要等", self.reason(t3))
        self.assertIn("交给空着的 Codex 2", self.reason(t3))

    def test_busy_old_hand_short_wait_gets_queued(self):
        a = self.hire()
        now = time.time()
        old = self.add2("改首页", owner=a, writes=["web/index.html"])
        s = self.sched()
        s.tick(now)
        self.turn(a, old, started=now - 120, ended=now - 60, files=["web/index.html"])   # 他做一件活大概 1 分钟
        self.done(old)
        cur = self.add2("改别处", owner=a, writes=["docs/x.md"])
        s.tick(now)
        nxt = self.add2("首页再改", agent="codex", writes=["web/index.html"])
        s.tick(now)
        self.assertEqual(self.owner(nxt), a)
        self.assertIn("交给 Codex 1（他正忙，排进他的队列，预计还要等 1 分钟）", self.reason(nxt))
        self.assertEqual(load_state(self.conn)["employees"][a]["queue"], [cur, nxt])

    def test_exclusions(self):
        a, b = self.hire(), self.hire()
        # Codex 1 连续失败 2 次：不选
        for i in range(2):
            tk = self.add("会失败的活%d" % i, owner=a)
            self.sched().tick()
            self.failed(a, tk)
        self.react({"failure": {"retry": 0}})
        t = self.add("新活", agent="codex")
        s = self.sched()
        s.tick()
        self.assertEqual(self.owner(t), b)
        # Codex 2 记忆 90%、压缩过也还在 85%：不选 → 招新人，理由里说清楚
        append(self.conn, "context.compacted", "station:" + b, {"method": "native", "before_pct": 95, "after_pct": 85,
                                                               "segment": 1}, ws=self.ws, subject=b)
        append(self.conn, "context.updated", "station:" + b, {"used": 9, "window": 10, "pct": 90, "source": "codex_last",
                                                             "estimated": False}, ws=self.ws, subject=b)
        self.done(t)
        t2 = self.add("又一件", agent="codex")
        s.tick()
        self.assertNotIn(self.owner(t2), (a, b))
        self.assertIn("招了 Codex 3", self.reason(t2))
        # --to 直接给他，不管排除规则
        t3 = self.add("指定给他", owner=a)
        s.tick()
        self.assertEqual(self.status(t3), "queued")


# ====================================================================== 反应表

class FailureRuleTests(RulesCase):
    def test_retry_once_then_lead_then_user(self):
        em = self.hire()
        tk = self.add("会挂的活", owner=em)
        s = self.sched()
        s.tick()
        self.failed(em, tk, error="单元测试没过：test_x")
        s.tick()
        # 第一次：写信给他、重新排给他
        letters = [e for e in self.events("letter.sent") if e["data"]["to"] == em]
        self.assertEqual(len(letters), 1)
        self.assertEqual((letters[0]["by"], letters[0]["data"]["from"], letters[0]["data"]["kind"]), ("rules", "office", "system"))
        self.assertIn("单元测试没过：test_x", letters[0]["data"]["body"])
        q = [e for e in self.events("task.queued") if e["data"].get("retry")]
        self.assertEqual([(e["by"], e["data"]["retry"]) for e in q], [("office", "auto")])
        self.assertEqual(self.status(tk), "queued")
        s.tick()
        self.assertEqual(len(self.events("letter.sent")), 1)            # 重复的拍子不重复写
        # 第二次：交负责人
        self.failed(em, tk)
        s.tick()
        self.assertEqual(self.status(tk), "failed")
        dec = self.decisions("failure")
        self.assertEqual(len(dec), 1)
        self.assertEqual((dec[0]["owner"], dec[0]["task"]), ("lead", tk))
        self.assertIn("又没做成（第 2 次）", dec[0]["title"])
        self.assertEqual([o["id"] for o in dec[0]["options"]], ["retry", "other", "drop"])
        # 负责人选"再让他试一次" → 管家重排 + 写信
        self.answer(dec[0]["id"], "retry")
        s.tick()
        self.assertEqual(self.status(tk), "queued")
        self.assertEqual(load_state(self.conn)["tasks"][tk]["retries"], {"auto": 1, "manual": 1})
        # 第三次：写给用户
        self.failed(em, tk)
        s.tick()
        rep = self.decisions("repeated_failure")
        self.assertEqual([(d["owner"], d["task"]) for d in rep], [("user", tk)])
        self.assertIn("已经失败 3 次了", rep[0]["title"])
        self.assertEqual(self.decisions("failure"), [])                  # 第 2 次那条已经处理过

    def test_quota_failure_goes_to_lead_without_retry(self):
        em = self.hire()
        tk = self.add("额度不够的活", owner=em)
        s = self.sched()
        s.tick()
        # 第一轮满载 → 工位换过线路（signal quota）→ 第二轮还是额度不够
        self.turn(em, tk, status="failed", error="Selected model is at capacity")
        append(self.conn, "signal.raised", "station:" + em, {"kind": "quota", "level": "remind", "text": "Codex Plus 满载，已换中转站接着做"},
               ws=self.ws, conv=self.conv, subject=em)
        self.failed(em, tk, error="You've hit your usage limit")
        s.tick()
        self.assertEqual(self.status(tk), "failed")
        self.assertEqual([e for e in self.events("task.queued") if e["data"].get("retry")], [])
        dec = self.decisions("failure")
        self.assertEqual(len(dec), 1)
        self.assertEqual(dec[0]["cause"], "quota")
        self.assertIn("已经换过额度线路", dec[0]["detail"])

    def test_old_failures_are_left_alone(self):
        """管家停了一阵再起来 / 第一次接上旧账本：很久以前的失败不翻出来自动重试。"""
        em = self.hire()
        tk = self.add("昨天挂的活", owner=em)
        s = self.sched()
        s.tick()
        self.failed(em, tk)
        s.tick(time.time() + 2 * 3600)
        self.assertEqual(self.status(tk), "failed")
        self.assertEqual(self.events("letter.sent"), [])
        self.assertEqual(self.decisions(), [])

    def test_env_failure_requeued_without_letter(self):
        em = self.hire()
        tk = self.add("对不齐桌子", owner=em)
        s = self.sched()
        s.tick()
        append(self.conn, "task.failed", "station:" + em, {"reason": "开工前没能对齐桌子", "detail": "git 出错"},
               ws=self.ws, conv=self.conv, subject=tk)
        s.tick()
        self.assertEqual(self.status(tk), "queued")
        self.assertEqual(self.events("letter.sent"), [])
        self.assertEqual(load_state(self.conn)["tasks"][tk]["retries"], {"env": 1})

    def test_error_loop_is_not_redispatched(self):
        em = self.hire()
        tk = self.add("死循环的活", owner=em)
        s = self.sched()
        s.tick()
        tn = self.turn(em, tk, ended=False)
        append(self.conn, "signal.raised", "station:" + em, {"kind": "error_loop", "level": "warn",
                                                             "text": "连续 3 次报错、中间没用工具，先停下"},
               ws=self.ws, conv=self.conv, subject=em)
        s.tick()
        self.assertEqual(self.decisions("error_loop"), [])               # 这一轮还没收尾：先不动
        append(self.conn, "turn.ended", "station:" + em, {"em": em, "status": "failed", "evidence": {
            "cli_status": "failed", "changed_files": [], "diff_stat": {"files": 0, "add": 0, "del": 0}}},
               ws=self.ws, conv=self.conv, subject=tn)
        append(self.conn, "task.failed", "station:" + em, {"reason": "这一轮失败了，也没有留下任何改动", "detail": ""},
               ws=self.ws, conv=self.conv, subject=tk)
        s.tick()
        s.tick()
        self.assertEqual(self.status(tk), "failed")
        self.assertEqual([e for e in self.events("task.queued") if e["data"].get("retry")], [])
        dec = self.decisions("error_loop")
        self.assertEqual(len(dec), 1)
        self.assertEqual(self.decisions("failure"), [])
        # 你决定让他重做 → 重排，不再当成死循环
        self.answer(dec[0]["id"], "retry", by="user")
        s.tick()
        self.assertEqual(self.status(tk), "queued")
        self.assertNotIn("error_loop", load_state(self.conn)["tasks"][tk])

    def test_executor_commit_warning_is_not_an_error_loop(self):
        em = self.hire()
        tk = self.add("活", owner=em)
        self.sched().tick()
        self.turn(em, tk, ended=False)
        append(self.conn, "signal.raised", "station:" + em, {"kind": "error_loop", "level": "warn",
                                                             "text": "Codex 1 自己做了 git commit（规定不许），改动照样按证据处理"},
               ws=self.ws, conv=self.conv, subject=em)
        self.assertNotIn("error_loop", load_state(self.conn)["tasks"][tk])

    def test_conflict_merged_again_once(self):
        em = self.hire()
        tk = self.add("会冲突的活", owner=em)
        s = self.sched()
        s.tick()
        now = time.time()

        def conflict():
            append(self.conn, "task.kept", "station:" + em, {"reason": "conflict", "detail": "合并冲突"}, ws=self.ws,
                   conv=self.conv, subject=tk)
            append(self.conn, "decision.opened", "station:" + em, {"kind": "conflict", "title": "冲突", "detail": "",
                                                                   "task": tk, "em": em, "options": [], "owner": "lead"},
                   ws=self.ws, conv=self.conv, subject=new_id("dc_"))
        conflict()
        s.tick(now)
        self.assertEqual(len(self.decisions("conflict")), 1)               # 还没到 30 秒
        s.tick(now + 31)
        self.assertEqual(self.decisions("conflict"), [])
        st_ = load_state(self.conn)["tasks"][tk]
        self.assertEqual((st_["status"], st_["desk_action"], st_["auto_merges"]), ("reported", "merge", 1))
        merged = [e for e in self.events("decision.resolved") if e["by"] == "rules"]
        self.assertEqual([e["data"]["option"] for e in merged], ["merge"])
        # 再冲突：不再自动合并，留给人
        conflict()
        s.tick(now + 120)
        self.assertEqual(len(self.decisions("conflict")), 1)
        # 规则监工不能替人选别的
        dc = self.decisions("conflict")[0]["id"]
        with self.assertRaises(Exception):
            append(self.conn, "decision.resolved", "rules", {"option": "discard"}, ws=self.ws, conv=self.conv, subject=dc)


class SilenceRecallOverlapTests(RulesCase):
    def test_codex_silence_steers_then_needs_you_then_moot(self):
        em = self.hire("codex")
        tk = self.add("沉默的活", owner=em)
        s = self.sched()
        now = time.time()
        s.tick(now)
        tn = self.turn(em, tk, started=now, ended=False)
        s.tick(now + 9 * 60)
        self.assertEqual(self.events("control.requested"), [])
        s.tick(now + 11 * 60)
        steer = self.events("control.requested")
        self.assertEqual([(e["by"], e["subject"], e["data"]["action"], e["data"]["by_source"]) for e in steer],
                         [("rules", em, "steer", "rules")])
        self.assertIn("11 分钟没有任何输出", steer[0]["data"]["text"])
        s.tick(now + 21 * 60)
        dec = self.decisions("silent")
        self.assertEqual(len(dec), 1)
        self.assertEqual(dec[0]["target"], {"em": em, "turn": tn})
        snap = snapshot.build(self.conn, conv=self.conv)
        self.assertEqual(snap["people"][em]["state"], "working")          # 不把在干活的人显示成"等人答复"
        s.tick(now + 30 * 60)
        self.assertEqual(len(self.events("control.requested")), 1)        # 每轮只问一次
        append(self.conn, "turn.ended", "station:" + em, {"em": em, "status": "completed", "evidence": {
            "cli_status": "completed", "changed_files": [], "diff_stat": {"files": 0, "add": 0, "del": 0}}},
               ws=self.ws, conv=self.conv, subject=tn)
        s.tick(now + 31 * 60)
        self.assertEqual(self.decisions("silent"), [])                    # 那一轮结束了：自动关掉

    def test_other_ai_silence_writes_reminder_and_activity_resets(self):
        em = self.hire("dsh")
        tk = self.add("沉默的活", owner=em)
        s = self.sched()
        now = time.time()
        s.tick(now)
        self.turn(em, tk, started=now, ended=False)
        append(self.conn, "task.progress", "station:" + em, {"step": 1, "of": 2, "text": "写", "source": "marker"},
               ws=self.ws, conv=self.conv, subject=tk, ts=now + 10 * 60)
        s.tick(now + 20 * 60)
        self.assertEqual([e for e in self.events("signal.raised") if e["data"]["kind"] == "silent"], [])
        s.tick(now + 26 * 60)
        sig = [e for e in self.events("signal.raised") if e["data"]["kind"] == "silent"]
        self.assertEqual(len(sig), 1)
        self.assertEqual(sig[0]["by"], "rules")
        self.assertEqual(self.events("control.requested"), [])

    def test_recall_retry_then_amnesia(self):
        em = self.hire()
        s = self.sched()
        append(self.conn, "recall.checked", "station:" + em, {"ok": False, "missing": ["task"]}, ws=self.ws, subject=em)
        s.tick()
        req = self.events("recall.requested")
        self.assertEqual([(e["by"], e["subject"]) for e in req], [("rules", em)])
        e = load_state(self.conn)["employees"][em]
        self.assertTrue(e["recall_due"])
        s.tick()
        self.assertEqual(len(self.events("recall.requested")), 1)
        # 第二次没过：不再开 decision.opened、员工不是 blocked、改写一条提醒 signal.raised
        append(self.conn, "recall.checked", "station:" + em, {"ok": False, "missing": ["file"]}, ws=self.ws, subject=em)
        s.tick()
        self.assertEqual(self.decisions("amnesia"), [])
        sig = [e for e in self.events("signal.raised") if e["data"].get("kind") == "remind"]
        self.assertEqual(len(sig), 1)
        self.assertEqual(sig[0]["data"]["level"], "remind")
        self.assertIn("整理记忆后两次都没复述对，可能忘了些细节；已经把工作笔记和任务卡重新给他，接着干", sig[0]["data"]["text"])
        snap = snapshot.build(self.conn, conv=self.conv)
        self.assertNotEqual(snap["people"][em]["state"], "blocked")

        # 旧的没处理的 amnesia 决定：管家启动后自动关掉（decision.resolved by=rules，option ok，text "复述检查不再拦人"）
        dc = new_id("dc_")
        append(self.conn, "decision.opened", "rules",
               {"kind": "amnesia", "title": "旧失忆决定", "detail": "旧详情",
                "options": [{"id": "ok", "label": "好的"}], "owner": "lead", "em": em},
               ws=self.ws, conv=self.conv, subject=dc)
        self.assertEqual(len(self.decisions("amnesia")), 1)
        self.assertEqual(snapshot.build(self.conn, conv=self.conv)["people"][em]["state"], "blocked")
        s.tick()
        self.assertEqual(self.decisions("amnesia"), [])
        res = [e for e in self.events("decision.resolved") if e["subject"] == dc]
        self.assertEqual(len(res), 1)
        self.assertEqual(res[0]["by"], "rules")
        self.assertEqual(res[0]["data"]["option"], "ok")
        self.assertEqual(res[0]["data"]["text"], "复述检查不再拦人")
        self.assertNotEqual(snapshot.build(self.conn, conv=self.conv)["people"][em]["state"], "blocked")

        # 后来复述对了：计数清零
        append(self.conn, "recall.checked", "station:" + em, {"ok": True, "missing": []}, ws=self.ws, subject=em)
        self.assertEqual(load_state(self.conn)["employees"][em]["recall_retries"], 0)

    def test_overlapping_writes_warn_both_and_lead(self):
        a, b = self.hire(), self.hire()
        t1 = self.add2("改首页", owner=a, writes=["web/**"])
        t2 = self.add2("改样式", owner=b, writes=["web/org.css"])
        t3 = self.add2("改文档", owner=b, writes=["docs/**"])
        s = self.sched()
        s.tick()
        s.tick()
        sig = [e for e in self.events("signal.raised") if e["data"]["kind"] == "overlap"]
        self.assertEqual(len(sig), 1)
        self.assertEqual((sig[0]["by"], sig[0]["data"]["level"]), ("rules", "warn"))
        self.assertIn("web/org.css", sig[0]["data"]["text"])
        to = sorted(e["data"]["to"] for e in self.events("letter.sent"))
        self.assertEqual(to, sorted([a, b]))


# ====================================================================== 预算

class BudgetTests(RulesCase):
    def test_task_minutes_warn_wrap_stop_then_extend(self):
        self.react({"silent": {"minutes": {"codex": 60}}})      # 这里只看预算，别让"沉默超时"也来问一句
        em = self.hire()
        tk = self.add2("要预算的活", owner=em, budget={"minutes": 10})
        s = self.sched()
        now = time.time()
        s.tick(now)
        tn = self.turn(em, tk, started=now, ended=False)
        s.tick(now + 6 * 60)
        self.assertEqual(self.events("budget.warned"), [])
        s.tick(now + 7.5 * 60)
        w = self.events("budget.warned")
        self.assertEqual([(e["by"], e["subject"], e["data"]["dim"]) for e in w], [("rules", tk, "minutes")])
        self.assertEqual(snapshot.build(self.conn, conv=self.conv, now=now + 7.5 * 60)["people"][em]["budget"],
                         {"pct": 75, "dim": "minutes", "scope": "task"})
        s.tick(now + 9.5 * 60)
        steer = [e for e in self.events("control.requested") if e["data"]["action"] == "steer"]
        self.assertEqual(len(steer), 1)
        self.assertIn("请收尾", steer[0]["data"]["text"])
        s.tick(now + 10.5 * 60)
        ctl = [(e["data"]["action"], e["subject"]) for e in self.events("control.requested")]
        self.assertEqual(ctl, [("steer", em), ("interrupt", em)])
        stopped = self.events("budget.stopped")
        self.assertEqual([(e["subject"], e["data"]["dim"]) for e in stopped], [(tk, "minutes")])
        dec = self.decisions("budget")
        self.assertEqual(len(dec), 1)
        self.assertEqual((dec[0]["owner"], dec[0]["task"], dec[0]["budget"]), ("lead", tk, {"scope": "task", "dim": "minutes"}))
        s.tick(now + 11 * 60)
        self.assertEqual(len(self.events("control.requested")), 2)
        # 加一半预算 → 上限 15 分钟，这一轮还在做也不再打断
        self.answer(dec[0]["id"], "extend")
        self.assertEqual(load_state(self.conn)["tasks"][tk]["budget_boost"], 1)
        s.tick(now + 12 * 60)
        self.assertEqual(len(self.events("control.requested")), 2)
        self.assertEqual(snapshot.build(self.conn, conv=self.conv, now=now + 12 * 60)["people"][em]["budget"]["pct"], 80)
        self.assertTrue(tn)

    def test_budget_interrupted_does_not_open_duplicate_failure_decision(self):
        """同一件事只问一次：预算用完被叫停后，这一轮以失败结束，不再多开 kind=failure 的需要你。"""
        self.react({"failure": {"retry_delay_s": 0}, "silent": {"minutes": {"codex": 60}}})
        em = self.hire()
        tk = self.add2("限额的活", owner=em, budget={"minutes": 10})
        s = self.sched()
        now = time.time()
        s.tick(now)
        tn = self.turn(em, tk, started=now, ended=False)
        # 用量直接到 105%（10.5 分钟），管家在 _budget 里开 kind=budget 决策，并发出 interrupt
        s.tick(now + 10.5 * 60)
        self.assertEqual(len(self.decisions("budget")), 1)
        self.assertEqual([e["data"]["action"] for e in self.events("control.requested")], ["interrupt"])
        # 工位收到 interrupt 结束这一轮，记录 task.failed
        ev = {"cli_status": "failed", "changed_files": [], "diff_stat": {"files": 0, "add": 0, "del": 0}, "error": "被打断了"}
        append(self.conn, "turn.ended", "station:" + em, {"em": em, "status": "failed", "evidence": ev},
               ws=self.ws, conv=self.conv, subject=tn, ts=now + 10.6 * 60)
        append(self.conn, "task.failed", "station:" + em,
               {"reason": "被打断了", "detail": "这件活的预算用完了"},
               ws=self.ws, conv=self.conv, subject=tk, ts=now + 10.6 * 60)
        # 下一拍：管家跑 _failures
        s.tick(now + 11 * 60)
        # 验证：绝不另开 kind=failure 的 decision，仍然只有原来那个 kind=budget decision
        self.assertEqual(len(self.decisions("failure")), 0)
        self.assertEqual(len(self.decisions("budget")), 1)

    def test_jump_over_90_to_100_has_one_warned_and_one_stopped(self):
        """90% 提醒收尾：一次就越过 90% 直接到 100% 时，打断前也补一条 budget.warned（pct 用实际值），warned 和 stopped 各一条。"""
        self.react({"silent": {"minutes": {"codex": 60}}})
        em = self.hire()
        tk = self.add2("大步跳预算的活", owner=em, budget={"minutes": 10})
        s = self.sched()
        now = time.time()
        s.tick(now)
        tn = self.turn(em, tk, started=now, ended=False)
        # 直接从 0% 跳到 105%（10.5 分钟）
        s.tick(now + 10.5 * 60)
        w = self.events("budget.warned")
        st = self.events("budget.stopped")
        self.assertEqual(len(w), 1)
        self.assertEqual(len(st), 1)
        self.assertEqual(w[0]["data"]["pct"], 105)
        self.assertEqual(st[0]["data"]["pct"], 105)
        # 并且打断前发了 interrupt
        ctl = [e for e in self.events("control.requested") if e["data"]["action"] == "interrupt"]
        self.assertEqual(len(ctl), 1)

    def test_turns_budget_pauses_requeued_task(self):
        em = self.hire()
        tk = self.add2("只给 1 轮的活", owner=em, budget={"turns": 1})
        s = self.sched()
        s.tick()
        self.turn(em, tk, status="failed", error="挂了")
        append(self.conn, "task.failed", "station:" + em, {"reason": "这一轮失败了，也没有留下任何改动", "detail": "挂了"},
               ws=self.ws, conv=self.conv, subject=tk)
        s.tick()
        # 预算已经用完：不自动重试，交负责人
        self.assertEqual(self.status(tk), "failed")
        dec = self.decisions("failure")
        self.assertEqual(dec[0]["cause"], "budget")
        # 负责人还是让他再试：排进队列后又被预算挡住（先停下，等加预算）
        self.answer(dec[0]["id"], "retry")
        s.tick()
        s.tick()
        t = load_state(self.conn)["tasks"][tk]
        self.assertEqual((t["status"], t["kept_reason"]), ("needs_you", "paused"))
        bdec = self.decisions("budget")
        self.assertEqual(len(bdec), 1)
        self.answer(bdec[0]["id"], "extend")
        s.tick()
        self.assertEqual(self.status(tk), "queued")

    def test_tokens_come_from_turn_usage(self):
        em = self.hire()
        tk = self.add2("费 token 的活", owner=em, budget={"tokens": 1000})
        self.sched().tick()
        self.turn(em, tk, usage={"in": 500, "out": 300, "cached": 0, "total": 800})
        u = bud.task_usage(load_state(self.conn), tk, time.time())
        self.assertEqual((u["tokens"], u["turns"]), (800, 1))
        cfg = reactions.load(self.home)
        self.assertEqual(bud.task_pct(cfg, load_state(self.conn), tk, time.time()), (80.0, "tokens"))

    def test_employee_daily_budget_stops_and_extends(self):
        self.react({"failure": {"retry_delay_s": 0}, "budget": {"employee": {"turns": 2, "minutes": 0, "tokens": 0}}})
        em = self.hire()
        tasks = [self.add("活%d" % i, owner=em) for i in range(3)]
        s = self.sched()
        s.tick()
        self.turn(em, tasks[0])
        self.turn(em, tasks[1])
        tn = self.turn(em, tasks[2], ended=False)
        s.tick()
        ctl = [(e["data"]["action"], e["subject"]) for e in self.events("control.requested")]
        self.assertEqual(ctl, [("stop", em)])
        self.assertEqual([e["subject"] for e in self.events("budget.stopped")], [em])
        dec = self.decisions("budget")
        self.assertEqual((dec[0]["em"], dec[0]["budget"]["scope"]), (em, "employee"))
        # 预算用完的人：自动选人时不选他
        t4 = self.add("新活", agent="codex")
        s.tick()
        self.assertNotEqual(load_state(self.conn)["tasks"][t4]["owner"], em)
        self.answer(dec[0]["id"], "extend")
        s.tick()
        ctl = [(e["data"]["action"], e["subject"]) for e in self.events("control.requested")]
        self.assertEqual(ctl[-1], ("resume", em))
        self.assertTrue(tn)

    def test_agy_daily_cap(self):
        """agy 每天的次数和工位是同一本账（xay.quota）：V2 自己记的 + 旧系统当天登记的合起来算，账本里的轮数不另算。"""
        from xay import quota
        self.react({"failure": {"retry_delay_s": 0}, "agy_daily_calls": 2})
        g = self.hire("agy")
        t1 = self.add("Gemini 的活一", owner=g)
        s = self.sched()
        s.tick()
        self.assertIn(g, self.spawned)
        for _ in range(3):                                                 # 账本里开工了 3 轮，但小账上还没记：不算
            self.turn(g, t1)
        s.tick()
        self.assertFalse(s.agy_capped)
        quota.agy_take_call(g, "turn", home=self.home)                     # V2 记 1 次
        legacy = self.tmp / "oldxay" / "registry.jsonl"                    # 旧系统当天也用了 1 次（同一批账号）
        legacy.parent.mkdir(parents=True, exist_ok=True)
        legacy.write_text(json.dumps({"date": time.strftime("%Y-%m-%d"), "kind": "agy_call", "calls": 1}) + "\n",
                          encoding="utf-8")
        t2 = self.add("Gemini 的活二", owner=g)
        s2 = self.sched()
        s2.tick()
        self.assertEqual(self.status(t2), "ready")                        # 今天用完了：先不排
        self.assertEqual(self.spawned, [g])                                # 也不再给他起工位
        sig = [e for e in self.events("signal.raised") if e["data"]["kind"] == "quota"]
        self.assertEqual(len(sig), 1)
        self.assertIn("每天最多 2 次，新旧系统合计", sig[0]["data"]["text"])
        self.assertIn("调用了 2 次", sig[0]["data"]["text"])
        self.assertTrue(s2.agy_capped)
        # 第二天：重新算
        s2.tick(time.time() + 86400)
        self.assertEqual(self.status(t2), "queued")

    def test_agy_daily_cap_follows_the_station_number(self):
        """reactions.json 不写（默认 0）：上限就是工位用的那个数（XAY2_AGY_DAILY_CAP / 提供者表）。"""
        from xay import quota
        g = self.hire("agy")
        with mock.patch.dict(os.environ, {"XAY2_AGY_DAILY_CAP": "1"}):
            s = self.sched()
            s.tick()
            self.assertFalse(s.agy_capped)
            quota.agy_take_call(g, "turn", home=self.home)
            t = self.add("Gemini 的活", owner=g)
            s.tick()
            self.assertTrue(s.agy_capped)
            self.assertEqual(self.status(t), "ready")


# ====================================================================== 总闸

class HaltTests(RulesCase):
    def test_user_halt_is_not_released_by_lead_planning(self):
        em = self.hire()
        append(self.conn, "control.requested", "user", {"action": "stop_all", "by_source": "page"}, ws=self.ws, conv=self.conv)
        self.add("活", owner=em)
        s = self.sched()
        s.tick()
        self.assertEqual(self.spawned, [])
        snap = snapshot.build(self.conn, conv=self.conv)
        self.assertEqual((snap["halt"]["by"], snap["halt"]["source"]), ("user", "page"))
        info = web.halt_info(self.conn)
        self.assertEqual(info["halt"]["by"], "user")
        # 页面上的"恢复"（resume，不指定员工）
        web.do_control(self.conn, {"action": "resume"}, {})
        self.assertIsNone(load_state(self.conn)["halt"])
        s.tick()
        self.assertEqual(self.spawned, [em])
        r = self.events("control.requested")[-1]
        self.assertEqual((r["by"], r["subject"], r["data"]["action"]), ("user", None, "resume"))

    def test_stuck_station_is_terminated_then_killed(self):
        self.react({"stop": {"term_after_s": 0.5, "kill_after_s": 0.5}})
        em = self.hire()
        lock = st.lock_path(self.home, em)
        lock.parent.mkdir(parents=True, exist_ok=True)
        ready = self.tmp / "ready"
        code = ("import fcntl,os,signal,sys,time\n"
                "signal.signal(signal.SIGTERM, signal.SIG_IGN)\n"
                "fh=open(sys.argv[4],'a+'); fcntl.flock(fh, fcntl.LOCK_EX)\n"
                "with open(sys.argv[5],'w') as f: f.write(str(os.getpid()) + '\\n')\n"
                "with open(sys.argv[6],'w') as f: f.write('ok\\n')\n"
                "time.sleep(60)\n")
        proc = subprocess.Popen([sys.executable, "-c", code, "xay.station", "--em", em,
                                 str(lock), str(st.pid_path(self.home, em)), str(ready)], start_new_session=True)
        self.addCleanup(lambda: proc.poll() is None and os.killpg(proc.pid, signal.SIGKILL))
        deadline = time.time() + 10
        while not ready.exists() and time.time() < deadline:
            time.sleep(0.05)
        self.assertTrue(st.station_running(self.home, em))
        append(self.conn, "control.requested", self.lead, {"action": "stop_all", "by_source": "cli"}, ws=self.ws, conv=self.conv)
        s = self.sched()
        s.procs[em] = proc
        t0 = time.time()
        s.tick(t0)
        s.tick(t0 + 0.6)                     # 不理"请停下"（忽略 SIGTERM）
        self.assertIsNone(proc.poll())
        s.tick(t0 + 1.2)                     # → 直接杀掉
        proc.wait(timeout=10)
        self.assertEqual(proc.returncode, -signal.SIGKILL)
        done = [e["data"]["detail"] for e in self.events("control.done") if e["by"] == "office"]
        self.assertEqual(len(done), 2)
        self.assertIn("强制结束", done[0])
        self.assertIn("直接杀掉", done[1])


# ====================================================================== 杂项

class MiscTests(RulesCase):
    def test_reactions_override_and_bad_values(self):
        self.react({"failure": {"retry": 3, "retry_delay_s": "x"}, "budget": {"task": {"turns": 20}}, "nope": 1})
        cfg = reactions.load(self.home)
        self.assertEqual(cfg["failure"]["retry"], 3)
        self.assertEqual(cfg["failure"]["retry_delay_s"], reactions.DEFAULTS["failure"]["retry_delay_s"])
        self.assertEqual(cfg["budget"]["task"]["turns"], 20)
        self.assertEqual(cfg["budget"]["task"]["minutes"], reactions.DEFAULTS["budget"]["task"]["minutes"])
        (self.home / "reactions.json").write_text("{坏的", encoding="utf-8")
        reactions._cache["cfg"] = None
        self.assertEqual(reactions.load(self.home), reactions.DEFAULTS)

    def test_writes_overlap(self):
        self.assertEqual(rules_mod.writes_overlap(["web/**"], ["web/a.js"]), ["web/a.js"])
        self.assertEqual(rules_mod.writes_overlap(["web/"], ["./web/x/y.css"]), ["web/x/y.css"])
        self.assertEqual(rules_mod.writes_overlap(["web/*.css"], ["docs/*.md"]), [])

    def test_plain_texts(self):
        from xay.core.humanize import text
        em = self.hire()
        tk = self.add("活", owner=em)
        s = load_state(self.conn)

        def t(typ, by, data, subject=None):
            return text({"type": typ, "by": by, "data": data, "subject": subject, "conv": self.conv}, state=s)
        self.assertEqual(t("control.requested", "user", {"action": "stop_all"}), "你拉了总闸：所有员工都停下，改动都保留")
        self.assertEqual(t("control.requested", "rules", {"action": "steer", "text": "还在吗"}, em), "监工对 Codex 1 说：还在吗")
        self.assertEqual(t("budget.stopped", "rules", {"dim": "turns", "pct": 100}, tk), "「活」的预算用完了（轮数），已经让他停下")
        self.assertEqual(t("task.queued", "office", {"owner": em, "retry": "auto"}, tk), "「活」没做成，监工让 Codex 1 接着修一次")
        self.assertIsNone(t("decision.resolved", "rules", {"option": "wait"}, "dc_x"))
        self.assertEqual(t("letter.sent", "rules", {"from": "office", "to": em, "subject": "请接着修"}, "lt_x"), "监工 给 Codex 1 写信：请接着修")
        self.assertEqual(t("task.queued", "rules", {"owner": em, "retry": "env"}, tk), "「活」开工前出了点状况，监工重新排给 Codex 1")
        self.assertEqual(t("letter.sent", "rules", {"from": em, "to": "rules", "subject": "收到"}, "lt_y"), "Codex 1 给 监工 写信：收到")


if __name__ == "__main__":
    unittest.main()


class AgyNoTokenLineTests(unittest.TestCase):
    """agy 报的 token 是每步重读整段对话的累计数，不按 token 刹车；别家照旧。"""
    def test_agy_task_and_employee_have_no_token_line(self):
        from xay.office import budget as bud
        cfg = {"budget": {"task": {"tokens": 100, "minutes": 10, "turns": 5}, "employee": {"tokens": 100, "minutes": 60}}}
        state = {"employees": {"em_a": {"agent": "agy"}, "em_c": {"agent": "codex"}}}
        self.assertEqual(set(bud.task_limits(cfg, {"owner": "em_a"}, state)), {"minutes", "turns"})
        self.assertEqual(set(bud.task_limits(cfg, {"agent": "agy"})), {"minutes", "turns"})
        self.assertIn("tokens", bud.task_limits(cfg, {"owner": "em_c"}, state))
        self.assertEqual(set(bud.employee_limits(cfg, {"agent": "agy"}, 0)), {"minutes"})
        self.assertIn("tokens", bud.employee_limits(cfg, {"agent": "dsh"}, 0))
