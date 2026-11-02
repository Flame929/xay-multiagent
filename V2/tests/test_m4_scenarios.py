"""M4 验收的场景测试（管家自动化这一半）：真起工位进程，员工用假 AI（XAY2_FAKE_ADAPTER），管家一拍一拍地跑。

覆盖：相似的活自动交给同一个员工；失败自动重试一次；额度失败后交负责人；一直报错（error_loop）不再重派而是"需要你"；
预算 90% 让他收尾、100% 打断；总闸能停下所有活（桌子保留）；同一件事失败 3 次写给用户。
全部用临时 XAY2_HOME 和临时 git 仓库，不碰 ~/.xay/v2，也不开任何端口。
"""
import json
import time
import unittest

from xay.core import append, load_state, snapshot
from xay.office import reactions
from xay.office import web
from xay.station import station as st

from tests import test_scheduler as ts


class M4Scenarios(ts.SchedulerCase):
    # 借用 test_scheduler 里真起工位的那几个辅助函数（不继承、也不导入它的测试类，免得它的测试在这里再跑一遍）
    spec = ts.LiveStationTests.spec
    live_sched = ts.LiveStationTests.live_sched
    kill_all = ts.LiveStationTests.kill_all
    tick_until = ts.LiveStationTests.tick_until

    def setUp(self):
        super().setUp()
        self.react({"failure": {"retry_delay_s": 0}})

    def react(self, over):
        self.home.mkdir(parents=True, exist_ok=True)
        (self.home / "reactions.json").write_text(json.dumps(over), encoding="utf-8")
        reactions._cache["cfg"] = None

    def change(self, tk, **fields):
        append(self.conn, "task.changed", self.lead, {"fields": fields, "reason": "测试"}, ws=self.ws, conv=self.conv, subject=tk)

    def decisions(self, kind):
        return [d for d in load_state(self.conn)["decisions"].values() if d["kind"] == kind]

    def retries(self, tk):
        return [e["data"]["retry"] for e in self.events("task.queued") if e["subject"] == tk and e["data"].get("retry")]

    DONE = [{"write": {"path": "web/{task}.txt", "text": "done {task}"}}, {"text": "【结果】完成：好了\n【给老板看】页面做好了"}]   # 带上大白话，监工不会让他补一轮

    # ------------------------------------------------------------------

    def test_similar_work_goes_to_the_same_employee(self):
        a, b = self.hire(), self.hire()                    # Codex 1、Codex 2
        opens = self.tmp / "opens.jsonl"
        s = self.live_sched(self.spec(default=self.DONE, open_log=str(opens)))
        t1 = self.add("改首页", owner=b)
        self.change(t1, writes=["web/**"])
        self.tick_until(s, lambda: self.status(t1) == "reported", what="第一件做完")
        t2 = self.add("首页加按钮", agent="codex")
        self.change(t2, writes=["web/index.html"])
        self.tick_until(s, lambda: self.status(t2) == "reported", what="第二件做完")
        state = load_state(self.conn)
        self.assertEqual(state["tasks"][t2]["owner"], b)       # Codex 1 也空着，但 Codex 2 刚改过同一批文件
        self.assertIn("交给 Codex 2：他最近做的「改首页」改过同一批文件", state["tasks"][t2]["assigned_reason"])
        rows = [json.loads(x) for x in opens.read_text().splitlines()]
        self.assertEqual(len(rows), 1)                           # 同一个工位、同一个原生对话接着做
        turns = [tr for tr in state["turns"].values() if tr["em"] == b]
        self.assertEqual(sorted(tr["task"] for tr in turns), sorted([t1, t2]))
        self.assertEqual([e["data"]["reason"] for e in self.events("employee.session") if e["subject"] == b], ["new"])
        self.assertNotIn(a, [tr["em"] for tr in state["turns"].values()])

    def test_failure_is_retried_once_automatically(self):
        em = self.hire()
        s = self.live_sched(self.spec(turns=[[{"fail": {"kind": "other", "error": "单元测试没过：test_cart"}}], self.DONE]))
        tk = self.add("修购物车", owner=em)
        self.tick_until(s, lambda: self.status(tk) == "reported", what="重试后做完")
        t = load_state(self.conn)["tasks"][tk]
        self.assertEqual((t["fails"], t["retries"]), (1, {"auto": 1}))
        letters = [e for e in self.events("letter.sent") if e["data"]["to"] == em]
        self.assertEqual(len(letters), 1)
        self.assertIn("单元测试没过：test_cart", letters[0]["data"]["body"])
        self.assertEqual(self.decisions("failure"), [])
        self.assertTrue((self.repo / "web" / ("%s.txt" % tk)).exists())

    def test_quota_failure_after_line_switch_goes_to_lead(self):
        em = self.hire()
        s = self.live_sched(self.spec(turns=[[{"fail": {"kind": "capacity", "error": "Selected model is at capacity"}}],
                                             [{"fail": {"kind": "quota", "error": "You've hit your usage limit"}}]],
                                      default=self.DONE))
        tk = self.add("额度不够的活", owner=em)
        self.tick_until(s, lambda: self.decisions("failure"), what="交负责人")
        self.assertEqual(self.status(tk), "failed")
        sessions = [e["data"] for e in self.events("employee.session") if e["subject"] == em]
        self.assertEqual([x["reason"] for x in sessions], ["new", "line_switch"])     # 工位先自己换了线路
        dec = self.decisions("failure")[0]
        self.assertEqual((dec["owner"], dec["cause"], dec["task"]), ("lead", "quota", tk))
        self.assertIn("已经换过额度线路", dec["detail"])
        for _ in range(5):
            s.tick()
        self.assertEqual(self.retries(tk), [])                                         # 不自动重试
        self.assertEqual(self.status(tk), "failed")

    def test_error_loop_is_not_redispatched_but_needs_you(self):
        em = self.hire()
        s = self.live_sched(self.spec(turns=[[{"sleep": 1.5}, {"fail": {"kind": "other", "error": "又报错了"}}]], default=self.DONE))
        tk = self.add("一直报错的活", owner=em)
        self.tick_until(s, lambda: self.events("turn.started"), what="开工")
        # M3 工位在"连续 3 次报错、中间没用工具"时会写这一条并停下；这里替它写
        append(self.conn, "signal.raised", "station:" + em, {"kind": "error_loop", "level": "warn",
                                                             "text": "连续 3 次报错、中间没用任何工具，先停下"},
               ws=self.ws, conv=self.conv, subject=em)
        self.tick_until(s, lambda: self.decisions("error_loop"), what="需要你")
        for _ in range(5):
            s.tick()
            time.sleep(0.05)
        self.assertEqual(self.status(tk), "failed")
        self.assertEqual(self.retries(tk), [])
        self.assertEqual(self.decisions("failure"), [])
        self.assertEqual(self.decisions("error_loop")[0]["task"], tk)

    def test_budget_wraps_up_at_90_and_interrupts_at_100(self):
        em = self.hire()
        s = self.live_sched(self.spec(turns=[[{"hold": True}]], default=self.DONE))
        tk = self.add("预算很少的活", owner=em)
        self.change(tk, budget={"minutes": 0.2})               # 12 秒：10.8 秒时让他收尾，12 秒时打断
        self.tick_until(s, lambda: self.status(tk) == "needs_you", timeout=30, what="打断后保留")
        ctl = [(e["by"], e["data"]["action"]) for e in self.events("control.requested")]
        self.assertEqual(ctl, [("rules", "steer"), ("rules", "interrupt")])
        steer = self.events("control.requested")[0]
        self.assertIn("预算快用完了", steer["data"]["text"])
        done = {e["data"]["action"]: e["data"] for e in self.events("control.done") if e["by"] == "station:" + em}
        self.assertEqual(done["steer"]["result"], "ok")          # Codex 当场收到了"请收尾"
        self.assertEqual(done["interrupt"]["result"], "ok")
        self.assertEqual([e["data"]["dim"] for e in self.events("budget.stopped")], ["minutes"])
        self.assertEqual([e["data"]["status"] for e in self.events("turn.ended")], ["interrupted"])
        self.assertEqual(len(self.decisions("budget")), 1)
        warned = sorted(e["data"]["pct"] for e in self.events("budget.warned"))
        self.assertTrue(warned and warned[0] >= 70)

    def test_kill_switch_stops_everything_and_keeps_desks(self):
        a, b = self.hire(), self.hire()
        s = self.live_sched(self.spec(default=[{"hold": True}]))
        ta = self.add("活一", owner=a)
        tb = self.add("活二", owner=b)
        self.tick_until(s, lambda: len(self.events("turn.started")) == 2, what="两个人都开工")
        web.do_control(self.conn, {"action": "stop_all"}, {})          # 页面上的红按钮
        later = self.add("总闸之后又排的活", owner=a)
        self.tick_until(s, lambda: len([e for e in self.events("employee.offline") if e["data"]["reason"] == "stopped"]) == 2,
                        what="两个工位都下班")
        for _ in range(10):
            s.tick()
            time.sleep(0.05)
        state = load_state(self.conn)
        self.assertEqual((state["tasks"][ta]["status"], state["tasks"][tb]["status"]), ("queued", "queued"))
        self.assertEqual(self.retries(ta), ["paused"])
        self.assertEqual(self.retries(tb), ["paused"])
        self.assertEqual(state["tasks"][later]["status"], "queued")      # 排着的活原样留着
        self.assertFalse(st.station_running(self.home, a) or st.station_running(self.home, b))
        self.assertEqual(sorted(e["data"]["status"] for e in self.events("turn.ended")), ["interrupted", "interrupted"])
        for em in (a, b):
            self.assertTrue((self.home / "desks" / self.ws / em / ".git").exists())   # 桌子都在
        snap = snapshot.build(self.conn, conv=self.conv)
        self.assertEqual(snap["halt"]["by"], "user")
        self.assertEqual(len(self.events("employee.online")), 2)         # 拉着总闸：没再起工位
        web.do_control(self.conn, {"action": "resume"}, {})
        self.assertIsNone(load_state(self.conn)["halt"])

    def test_halt_task_without_changes_resumes_and_completes(self):
        em = self.hire()
        # 第一轮 hold 住等待总闸停下；第二轮执行 DONE 并做完
        s = self.live_sched(self.spec(turns=[[{"hold": True}], self.DONE]))
        tk = self.add("没改动的活", owner=em)
        self.tick_until(s, lambda: len(self.events("turn.started")) == 1, what="开工")
        web.do_control(self.conn, {"action": "stop_all"}, {})
        self.tick_until(s, lambda: any(e["data"].get("reason") == "stopped" for e in self.events("employee.offline")),
                        what="工位因总闸下班")
        for _ in range(10):
            s.tick()
            time.sleep(0.05)
        state = load_state(self.conn)
        self.assertNotEqual(state["tasks"][tk]["status"], "failed")
        self.assertEqual(state["tasks"][tk]["status"], "queued")
        self.assertEqual(self.retries(tk), ["paused"])
        self.assertFalse(st.station_running(self.home, em))
        # 恢复总闸：工位重新启动，接着干并做完
        web.do_control(self.conn, {"action": "resume"}, {})
        self.tick_until(s, lambda: self.status(tk) == "reported", timeout=30, what="恢复后重新开工并做完")
        state = load_state(self.conn)
        self.assertEqual(state["tasks"][tk]["status"], "reported")

    def test_three_failures_are_written_to_the_user(self):
        em = self.hire()
        s = self.live_sched(self.spec(default=[{"fail": {"kind": "other", "error": "一直挂"}}]))
        tk = self.add("总是失败的活", owner=em)
        self.tick_until(s, lambda: self.decisions("failure"), what="自动重试一次后交负责人")
        self.assertEqual(self.retries(tk), ["auto"])
        dec = self.decisions("failure")[0]
        append(self.conn, "decision.resolved", self.lead, {"option": "retry"}, ws=self.ws, conv=self.conv, subject=dec["id"])
        self.tick_until(s, lambda: self.decisions("repeated_failure"), what="写给用户")
        rep = self.decisions("repeated_failure")[0]
        self.assertEqual((rep["owner"], rep["task"]), ("user", tk))
        self.assertEqual(load_state(self.conn)["tasks"][tk]["fails"], 3)
        self.assertEqual(self.retries(tk), ["auto", "manual"])


if __name__ == "__main__":
    unittest.main()
