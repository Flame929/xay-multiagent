"""M4 信件送达 + 改计划申请闭环 + xay2 wait / brief 收尾（场景测试，M4 验收的一部分）。

- 用假 AI（xay.adapters.fake）跑真工位、用子进程跑真 xay2 命令；需要"造"的事件直接写进同一本临时账本。
- 全部在临时 XAY2_HOME + 临时 git 仓库里，不碰 ~/.xay/v2，也不开 7879 端口。

场景：
1. 一个申请把计划从第 2 版改成第 3 版（员工在发件箱写 xay2 plan propose → 负责人 wait 看到 → answer accept）。
2. 一封信在员工下一轮开工时送达（放在这一轮输入的最开头），他用【回信】标记回复，原来那封"要回复"的信关掉。
3. Codex 员工干活中途收到信，被当场插话（steer）送达；dsh 员工插不进去，下一轮开工再送。
"""
import json
import os
import subprocess
import sys
import tempfile
import threading
import unittest
from pathlib import Path

V2_ROOT = Path(__file__).resolve().parents[1]
if str(V2_ROOT) not in sys.path:
    sys.path.insert(0, str(V2_ROOT))

from xay import cli  # noqa: E402
from xay.core import append, load_state, open_db, rebuild  # noqa: E402
from xay.core import mail  # noqa: E402
from xay.core.snapshot import build  # noqa: E402
from xay.station import markers  # noqa: E402
from test_station import Factory, StationCase, writes  # noqa: E402

BIN = V2_ROOT / "bin" / "xay2"
LEAD_ENV_DROP = ("XAY_ROLE", "XAY2_TASK", "XAY2_TASK_FILE", "XAY2_OUTBOX", "XAY2_EM", "CLAUDE_CODE_SESSION_ID",
                 "CODEX_THREAD_ID")
CAP = [{"sleep": 0.1}, {"fail": {"kind": "capacity", "error": "Selected model is at capacity"}}]


class LetterCase(StationCase):
    """工位测试的底子（临时账本 + 临时仓库 + 员工 Codex 1）+ 跑负责人命令 / 执行者命令的小工具。"""

    def xay(self, *args):
        env = {k: v for k, v in os.environ.items() if k not in LEAD_ENV_DROP}
        env.update(XAY2_CONV=self.conv, PYTHONDONTWRITEBYTECODE="1")
        return subprocess.run([str(BIN), *args], cwd=str(self.repo), env=env, capture_output=True, text=True, timeout=60)

    def ok(self, *args):
        r = self.xay(*args)
        self.assertEqual(r.returncode, 0, "xay2 %s：rc=%s\n%s\n%s" % (" ".join(args), r.returncode, r.stdout, r.stderr))
        return r.stdout.strip()

    @staticmethod
    def executor(a, *args):
        """假员工在自己桌子里跑执行者命令（只写发件箱，不碰账本）。"""
        out = subprocess.run([str(BIN), *args], env=dict(os.environ, **a.card["env"]), cwd=a.cwd,
                             capture_output=True, text=True, timeout=60)
        assert out.returncode == 0, out.stderr

    def snap(self):
        return build(self.conn, conv=self.conv)

    def letter(self, lt):
        return next(x for x in self.snap()["letters"] if x["id"] == lt)

    def delivered(self, lt):
        return [e for e in self.events("letter.delivered") if e["subject"] == lt]

    def top(self):
        return max(e["seq"] for e in self.events())

    def assert_rebuild_same(self):
        before = json.dumps(load_state(self.conn), sort_keys=True, ensure_ascii=False)
        rebuild(self.conn)
        self.assertEqual(before, json.dumps(load_state(self.conn), sort_keys=True, ensure_ascii=False))


# =============================================================== 场景 1：申请把计划从第 2 版改成第 3 版

class ProposalScenarioTests(LetterCase):
    def test_employee_proposal_moves_plan_from_v2_to_v3(self):
        em2 = "em_bbbb0002"
        append(self.conn, "employee.hired", self.lead, {"agent": "codex"}, ws=self.ws, conv=self.conv, subject=em2)
        tk = self.add_task("做结算页")                       # 第 1 版
        fee = self.add_task("算运费", queue=False)            # 第 2 版（先也交给 Codex 1，不排队）
        self.assertEqual(self.snap()["plan"]["version"], 2)

        def turn(a, text, extra):
            self.executor(a, "plan", "propose", "--reason", "Codex 2 更熟运费规则", "move", fee, "--to", em2)
            (Path(a.cwd) / "pay.txt").write_text("pay\n")
            return [{"text": "【结果】完成：结算页做好了"}]
        self.run_station(Factory([[turn]]))

        made = self.events("proposal.made")
        self.assertEqual(len(made), 1)
        pp = made[0]["subject"]
        self.assertEqual((made[0]["by"], made[0]["data"]["from"], made[0]["data"]["to"]), ("employee:" + self.em, self.em, "gm"))
        self.assertEqual(made[0]["data"]["changes"], [{"op": "assign", "task": fee, "fields": {"owner": em2}}])
        self.assertEqual(self.snap()["plan"]["version"], 2, "申请还没批：计划不动")
        self.assertEqual([p["id"] for p in self.snap()["proposals"]], [pp])

        # 负责人：wait 等到这条申请（一行写清改什么、理由、怎么答复），brief 里也列在"需要你决定的"
        w = self.xay("wait", "--since", "0", "--timeout", "0")
        self.assertEqual(w.returncode, 0, w.stderr)
        line = next(x for x in w.stdout.splitlines() if pp in x)
        self.assertIn("Codex 1 申请改计划：把「算运费」从 Codex 1 改给 Codex 2；理由：Codex 2 更熟运费规则", line)
        self.assertIn("选项：accept / reject", line)
        self.assertIn(pp, self.ok("brief"))

        out = self.ok("answer", pp, "accept", "好，交给 Codex 2")
        self.assertIn("计划改到第 3 版：把「算运费」从 Codex 1 改给 Codex 2", out)
        snap = self.snap()
        self.assertEqual((snap["conversation"]["plan_version"], snap["plan"]["version"]), (3, 3))
        self.assertEqual(len(snap["plan"]["history"]), 3, "申请通过只加一版")
        h = snap["plan"]["history"][-1]
        self.assertEqual((h["version"], h["by"], h["reason"]), (3, self.em, "Codex 2 更熟运费规则（申请 %s 通过）" % pp))
        self.assertEqual((h["proposal"], h["approved_by"]), (pp, "gm"))
        self.assertEqual(h["changes"], [{"op": "assign", "task": fee, "title": "算运费",
                                         "before": {"owner": self.em}, "after": {"owner": em2}}])
        self.assertEqual(h["summary"], "把「算运费」从 Codex 1 改给 Codex 2")     # 页面"计划版本"栏照着显示
        self.assertEqual(h["change_texts"], ["把「算运费」从 Codex 1 改给 Codex 2"])
        task = next(t for t in snap["tasks"] if t["id"] == fee)
        self.assertEqual((task["owner"], task["assigned_reason"]), (em2, "Codex 2 更熟运费规则"))
        self.assertEqual(snap["proposals"], [])
        # 申请人收到一封通知信（下一轮开工时送到）
        notice = [x for x in snap["letters"] if x["to"] == self.em and x["kind"] == "notice"]
        self.assertEqual(len(notice), 1)
        self.assertIn("通过了，计划改到第 3 版", notice[0]["body"])
        self.assertIn("好，交给 Codex 2", notice[0]["body"])
        self.assertEqual(notice[0]["delivery"]["state"], "waiting")
        # 同一个申请不能再处理一次
        again = self.xay("answer", pp, "reject")
        self.assertEqual(again.returncode, 2)
        self.assertIn("已经处理过了", again.stderr)
        self.assertEqual(self.snap()["plan"]["version"], 3)
        self.assert_rebuild_same()

    def test_proposal_about_a_missing_task_is_refused_and_reject_only_closes_it(self):
        tk = self.add_task("做结算页", queue=False)
        with self.assertRaises(ValueError):
            append(self.conn, "proposal.made", "employee:" + self.em,
                   {"from": self.em, "to": "gm", "reason": "x", "changes": [{"op": "remove", "task": "tk_nothere"}]},
                   ws=self.ws, conv=self.conv, subject="pp_bad00001")
        with self.assertRaises(ValueError):       # 新增任务没有标题
            append(self.conn, "proposal.made", "employee:" + self.em,
                   {"from": self.em, "to": "gm", "reason": "x", "changes": [{"op": "add", "task": "tk_new00001", "fields": {}}]},
                   ws=self.ws, conv=self.conv, subject="pp_bad00002")
        self.assertEqual(self.state()["proposals"], {})
        append(self.conn, "proposal.made", "employee:" + self.em,
               {"from": self.em, "to": "gm", "reason": "不想做了", "changes": [{"op": "remove", "task": tk}]},
               ws=self.ws, conv=self.conv, subject="pp_ok000001")
        self.ok("answer", "pp_ok000001", "reject", "还是要做")
        s = self.snap()
        self.assertEqual((s["plan"]["version"], s["tasks"][0]["status"], s["proposals"]), (1, "planned", []))
        self.assertEqual(self.state()["proposals"]["pp_ok000001"]["status"], "rejected")
        notice = [x for x in s["letters"] if x["to"] == self.em]
        self.assertEqual([x["subject"] for x in notice], ["你的改计划申请没通过"])
        self.assertIn("还是要做", notice[0]["body"])


# =============================================================== 场景 2：下一轮开工时送到，回信关掉原信

class NextTurnLetterTests(LetterCase):
    def test_letter_waits_for_next_turn_then_reply_marker_closes_it(self):
        lt = self.ok("ask", "Codex 1", "手机页面按多宽做？", "--subject", "手机宽度")
        self.assertTrue(lt.startswith("lt_"))
        self.assertEqual(self.letter(lt)["delivery"], {"state": "waiting", "text": "还没送到：等 Codex 1 下次开工", "to": self.em})
        self.assertIn("还没送到的信 1 封：给 Codex 1 的 1 封", self.ok("brief"))
        cursor = self.top()

        seen = {}

        def turn(a, text, extra):
            seen["text"] = text
            (Path(a.cwd) / "m.txt").write_text("375\n")
            return [{"text": "好的\n【回信 %s】按 375 宽做，再往宽了放大\n【结果】完成：按 375 做了\n" % lt}]
        tk = self.add_task("做手机页面")
        self.run_station(Factory([[turn]]))

        text = seen["text"]
        self.assertTrue(text.startswith("【你有 1 封信】"), text[:80])
        self.assertIn("负责人 写给你（要你回复，%s）：手机宽度" % lt, text)
        self.assertIn("手机页面按多宽做？", text)
        self.assertIn("【回信 lt_…】", text)
        self.assertLess(text.index(lt), text.index("【任务】做手机页面"), "信在这一轮输入的最开头")
        self.assertEqual([(e["by"], e["data"]) for e in self.delivered(lt)],
                         [("station:" + self.em, {"how": "next_turn", "em": self.em, "task": tk})])
        replies = [e for e in self.events("letter.sent") if e["data"].get("reply_to") == lt]
        self.assertEqual(len(replies), 1)
        reply = replies[0]
        self.assertEqual((reply["data"]["from"], reply["data"]["to"]), (self.em, "gm"))
        self.assertIn("按 375 宽做", reply["data"]["body"])
        s = self.state()["letters"][lt]
        self.assertEqual((s["status"], s["answered_by"]), ("closed", reply["subject"]))
        self.assertEqual(self.letter(lt)["delivery"]["text"], "已交给 Codex 1（开工时一起交代）")

        # 负责人 wait 收到回信；打印出来就记成"负责人已看到"，再等一次不会重复记
        w = self.xay("wait", "--since", str(cursor), "--timeout", "0")
        self.assertEqual(w.returncode, 0, w.stderr)
        self.assertIn("Codex 1 来信：按 375 宽做", w.stdout)
        self.assertIn(reply["subject"], w.stdout)
        self.assertEqual([e["data"] for e in self.delivered(reply["subject"])], [{"how": "inbox"}])
        self.assertEqual(self.letter(reply["subject"])["delivery"], {"state": "delivered", "text": "负责人已看到", "to": "gm"})
        self.xay("wait", "--since", str(cursor), "--timeout", "0")
        self.assertEqual(len(self.delivered(reply["subject"])), 1)
        self.assertIn("信：都送到了", self.ok("brief"))
        # 下一轮不会再交代同一封信
        seen.clear()
        self.add_task("再改一下")
        self.run_station(Factory([[lambda a, t, e: (seen.update(text=t), [{"text": "【结果】完成：好"}])[1]]]))
        self.assertNotIn("【你有", seen["text"])
        self.assert_rebuild_same()

    def test_employee_question_blocks_until_lead_replies_and_the_reply_reaches_him(self):
        def ask(a, text, extra):
            self.executor(a, "ask", "gm", "接口要不要改成 snake_case？", "--subject", "接口命名")
            return [{"text": "【结果】完成：先按原样写了"}]
        self.add_task("写接口", mode="readonly")
        self.run_station(Factory([[ask]]))
        q = next(e for e in self.events("letter.sent") if e["data"]["from"] == self.em)
        self.assertEqual((q["data"]["to"], q["data"]["kind"]), ("gm", "request"))
        self.assertEqual(self.snap()["people"][self.em]["state"], "blocked")      # 等人答复
        brief = self.ok("brief")
        self.assertIn('%s：Codex 1 问「接口命名」（xay2 reply %s "…"）' % (q["subject"], q["subject"]), brief)

        w = self.xay("wait", "--since", "0", "--timeout", "0")
        self.assertIn('Codex 1 来信：接口命名——接口要不要改成 snake_case？（%s，等你回复：xay2 reply %s "…"）'
                      % (q["subject"], q["subject"]), w.stdout)
        ans = self.ok("reply", q["subject"], "不用改，保持 camelCase")
        self.assertEqual(self.state()["letters"][q["subject"]]["status"], "closed")
        self.assertNotEqual(self.snap()["people"][self.em]["state"], "blocked")
        self.assertEqual(self.letter(ans)["delivery"]["state"], "waiting")

        seen = {}
        self.add_task("接着写接口", mode="readonly")
        self.run_station(Factory([[lambda a, t, e: (seen.update(text=t), [{"text": "【结果】完成：好"}])[1]]]))
        self.assertIn("负责人 回复了你的信「接口命名」（%s）" % ans, seen["text"])
        self.assertIn("不用改，保持 camelCase", seen["text"])
        self.assertEqual(self.delivered(ans)[0]["data"]["how"], "next_turn")

    def test_letters_given_in_a_turn_that_failed_on_capacity_are_given_again_on_the_relay(self):
        lt = self.ok("tell", "Codex 1", "记得写注释", "--subject", "注释")
        f = Factory([[CAP], [writes("r.txt", "r\n", [{"text": "【结果】完成：r"}])]])
        self.add_task("满载也要做完")
        self.run_station(f)
        a1, a2 = f.instances
        self.assertTrue(a1.sent[0][0].startswith("【你有 1 封信】"))
        relay = a2.sent[0][0]
        self.assertIn("【你有 1 封信】（其中 1 封上一轮已经交代过，上一轮没做成，再说一次）", relay)
        self.assertIn("记得写注释", relay)
        self.assertEqual(len(self.delivered(lt)), 1)


# =============================================================== 场景 3：干活中途收到信

class SteerLetterTests(LetterCase):
    def test_codex_gets_a_letter_mid_turn_by_steer(self):
        f = Factory([[writes("s.txt", "s", [{"text": "开始\n"}, {"hold": True}, {"text": "【结果】完成：好"}])]])
        self.add_task("长活")
        st = self.station(f)
        th = threading.Thread(target=st.run, kwargs={"once": True}, daemon=True)
        th.start()
        self.wait_for(lambda: self.events("turn.started"))
        lt = self.ok("tell", "Codex 1", "注释写成中文", "--subject", "注释")
        self.wait_for(lambda: f.instances and f.instances[0].steered)
        steered = f.instances[0].steered[0]
        self.assertTrue(steered.startswith("【你有 1 封信】不用停下手头的活"), steered)
        self.assertIn("负责人 写给你（%s）：注释" % lt, steered)
        self.assertIn("注释写成中文", steered)
        f.instances[0].release()
        th.join(10)
        self.assertFalse(th.is_alive())
        self.assertEqual(st.rejected_writes, [])
        self.assertEqual([e["data"]["how"] for e in self.delivered(lt)], ["steer"])
        self.assertEqual(self.letter(lt)["delivery"]["text"], "已当场告诉 Codex 1")
        self.assertEqual(len(f.instances[0].steered), 1, "同一封信只插话一次")
        self.assertEqual(len(self.events("task.landed")), 1)

    def test_department_letter_goes_to_the_member_working_on_the_department_task(self):
        em2 = "em_bbbb0002"
        append(self.conn, "employee.hired", self.lead, {"agent": "codex"}, ws=self.ws, conv=self.conv, subject=em2)
        self.ok("dept", "open", "fe", "--name", "前端部")
        for em in (em2, self.em):      # 名单里 Codex 2 在前，但 Codex 1 正在做前端部的活
            append(self.conn, "employee.joined", self.lead, {"dept": "fe"}, ws=self.ws, conv=self.conv, subject=em)
        tk = self.add_task("做首页", queue=False)
        append(self.conn, "task.changed", self.lead, {"fields": {"dept": "fe"}, "reason": "归前端部"}, ws=self.ws, conv=self.conv, subject=tk)
        self.queue(tk)
        f = Factory([[writes("h.txt", "h", [{"hold": True}, {"text": "【结果】完成：好"}])]])
        st = self.station(f)
        th = threading.Thread(target=st.run, kwargs={"once": True}, daemon=True)
        th.start()
        self.wait_for(lambda: self.events("turn.started"))
        lt = self.ok("ask", "fe", "首页的空状态图谁来画？")
        self.wait_for(lambda: f.instances and f.instances[0].steered)
        self.assertIn("负责人 写给前端部，转给你（要你回复，%s）" % lt, f.instances[0].steered[0])
        f.instances[0].release()
        th.join(10)
        self.assertEqual(self.delivered(lt)[0]["data"]["em"], self.em)


class DshLetterTests(LetterCase):
    agent = "dsh"

    def test_dsh_cannot_be_steered_so_the_letter_waits_for_the_next_turn(self):
        b = writes("b.txt", "b", [{"text": "【结果】完成：b\n【给老板看】第二件做好了"}])   # 带上大白话，监工不会让他补一轮
        f = Factory([[writes("a.txt", "a", [{"hold": True}, {"text": "【结果】完成：a\n【给老板看】第一件做好了"}]), b], [b]])
        self.add_task("第一件")
        st = self.station(f)
        th = threading.Thread(target=st.run, kwargs={"once": True}, daemon=True)
        th.start()
        self.wait_for(lambda: self.events("turn.started"))
        lt = self.ok("tell", "DeepSeek 1", "第二件先别急")
        self.add_task("第二件")
        self.wait_for(lambda: f.instances)
        f.instances[0].release()
        th.join(15)
        self.assertFalse(th.is_alive())
        self.assertEqual(sum(len(a.steered) for a in f.instances), 0)
        self.assertEqual([e["data"]["how"] for e in self.delivered(lt)], ["next_turn"])
        texts = [text for a in f.instances for text, _extra in a.sent]
        self.assertEqual(len(texts), 2)
        self.assertNotIn("【你有", texts[0])
        self.assertTrue(texts[1].startswith("【你有 1 封信】"), texts[1][:60])


# =============================================================== 送信规则（纯函数）

class RoutingTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.conn = open_db(str(Path(self.tmp.name) / "xay.db"))
        self.addCleanup(self.conn.close)
        self.ws, self.conv = "ws_mail", "cv_mail"
        self.lead = "lead:" + self.conv
        self.w("conv.begun", self.lead, {"title": "送信", "lead_kind": "claude", "aliases": [], "cwd": "/tmp"}, self.conv)
        for em in ("em_a", "em_b", "em_c"):
            self.w("employee.hired", self.lead, {"agent": "codex"}, em)
        self.w("dept.opened", self.lead, {"name": "前端部", "mission": "", "owns": [], "head": "rules"}, "fe")
        self.w("dept.opened", self.lead, {"name": "产品部", "mission": "", "owns": [], "head": "claude"}, "pm")
        for em in ("em_a", "em_b"):
            self.w("employee.joined", self.lead, {"dept": "fe"}, em)
        self.w("task.added", self.lead, {"title": "做首页", "dept": "fe", "owner": "em_a", "deps": [], "writes": [], "mode": "write", "reason": ""}, "tk_fe")
        self.w("task.added", self.lead, {"title": "杂活", "owner": "em_b", "deps": [], "writes": [], "mode": "write", "reason": ""}, "tk_x")
        self.w("task.added", self.lead, {"title": "做详情页", "dept": "fe", "owner": "em_b", "deps": [], "writes": [], "mode": "write", "reason": ""}, "tk_fe2")
        self.ts = 100

    def w(self, typ, by, data, subject, ts=None):
        return append(self.conn, typ, by, data, ws=self.ws, conv=self.conv, subject=subject, ts=ts)

    def letter(self, lt, to, frm="user", kind="request"):
        self.w("letter.sent", "user" if frm == "user" else self.lead if frm == "gm" else "employee:" + frm,
               {"from": frm, "to": to, "kind": kind, "subject": "问", "body": "问一下"}, lt)
        return load_state(self.conn)["letters"][lt]

    def turn(self, tn, em, tk, end=True):
        self.ts += 10
        self.w("turn.started", "station:" + em, {"task": tk, "em": em, "kind": "first"}, tn, ts=self.ts)
        if end:
            self.ts += 10
            self.w("turn.ended", "station:" + em, {"em": em, "status": "completed", "evidence": {"cli_status": "completed", "changed_files": [], "diff_stat": {}}}, tn, ts=self.ts)

    def to(self, lt, starting=None):
        s = load_state(self.conn)
        return mail.recipient(s, s["letters"][lt], starting)

    def test_reply_to_the_office_needs_no_delivery(self):
        """员工回管家自动写的信（to=office）：和写给用户的一样不用投递，不算"还没送到"，名字显示"监工"（页面上后台自动写信的一律叫监工）。"""
        self.letter("lt_o", "office", frm="em_a", kind="tell")
        self.assertEqual(self.to("lt_o"), "office")
        s = load_state(self.conn)
        self.assertEqual(mail.delivery(s, s["letters"]["lt_o"])["state"], "none")
        self.assertEqual(mail.who(s, self.conv, "office"), "监工")
        self.assertEqual(mail.who(s, self.conv, "rules"), "监工")

    def test_department_letter_picks_the_most_relevant_member(self):
        self.letter("lt_1", "fe")
        self.assertEqual(self.to("lt_1"), "em_a", "谁都没干过活：名单里靠前的")
        self.turn("tn_b1", "em_b", "tk_x")
        self.assertEqual(self.to("lt_1"), "em_b", "最近干过活的")
        self.assertEqual(self.to("lt_1", ("em_a", "tk_fe")), "em_a", "马上开工做本部门任务的")
        self.turn("tn_a1", "em_a", "tk_fe", end=False)
        self.assertEqual(self.to("lt_1"), "em_a", "正在做本部门任务的")
        self.turn("tn_b2", "em_b", "tk_x", end=False)
        self.assertEqual(self.to("lt_1"), "em_a", "做本部门任务的比做别的活的更相关")
        s = load_state(self.conn)
        self.assertEqual([x["id"] for x in mail.pending_for(s, "em_a")], ["lt_1"])
        self.assertEqual(mail.pending_for(s, "em_b"), [])

    def test_sender_head_and_fallbacks(self):
        self.letter("lt_self", "fe", frm="em_a")
        self.assertEqual(self.to("lt_self"), "em_b", "写信的人自己不算")
        self.w("employee.fired", self.lead, {"reason": "测试"}, "em_b")
        self.assertEqual(self.to("lt_self"), "gm", "部门里没有别人 → 总经理")
        self.letter("lt_pm", "pm")
        self.assertEqual(self.to("lt_pm"), "gm", "部门负责人是 Claude → 本对话的负责人")
        self.letter("lt_user", "user", frm="gm", kind="tell")
        self.assertEqual(self.to("lt_user"), "user")
        s = load_state(self.conn)
        self.assertEqual(mail.delivery(s, s["letters"]["lt_user"])["state"], "none")
        self.assertEqual(mail.delivery(s, s["letters"]["lt_pm"])["text"], "还没送到：等负责人来看")
        self.letter("lt_gone", "em_b", frm="gm", kind="tell")
        s = load_state(self.conn)
        self.assertEqual(mail.delivery(s, s["letters"]["lt_gone"]), {"state": "stuck", "text": "送不到：Codex 2 已经离职", "to": "em_b"})
        # 写给 Claude 负责人部门的信叫醒 wait；写给员工的不叫醒
        ev = {"type": "letter.sent", "subject": "lt_pm", "conv": self.conv, "data": s["letters"]["lt_pm"]}
        self.assertTrue(cli._wakes(ev, s))
        ev = {"type": "letter.sent", "subject": "lt_gone", "conv": self.conv, "data": s["letters"]["lt_gone"]}
        self.assertFalse(cli._wakes(ev, s))

    def test_reply_closes_request_only_when_someone_else_answers(self):
        self.letter("lt_q", "gm", frm="em_a")
        self.w("letter.sent", "employee:em_a", {"from": "em_a", "to": "gm", "kind": "tell", "subject": "补充", "body": "再补一句", "reply_to": "lt_q"}, "lt_more")
        self.assertEqual(load_state(self.conn)["letters"]["lt_q"]["status"], "open", "自己追加一句不算答复")
        self.w("letter.sent", self.lead, {"from": "gm", "to": "em_a", "kind": "reply", "subject": "好", "body": "可以", "reply_to": "lt_q"}, "lt_ans")
        self.assertEqual(load_state(self.conn)["letters"]["lt_q"]["status"], "closed")

    def test_reply_marker(self):
        found = markers.scan("先说一句\n【回信 lt_12ab34cd】按 375 宽做\n【结果】完成：好", final=True)
        self.assertEqual(found[0], (0, "letter", {"kind": "reply", "reply_to": "lt_12ab34cd", "subject": "按 375 宽做", "body": "按 375 宽做"}))
        self.assertEqual(found[1][1], "result")


# =============================================================== wait / brief 收尾（§7）

class WaitBriefTests(LetterCase):
    def setUp(self):
        super().setUp()
        self.tk = self.add_task("做轮播", queue=False)

    def test_kept_and_its_decision_print_as_one_line_and_landed_merge(self):
        base = 1_800_000_000
        since = self.top()
        A = lambda typ, by, data, sub, ts: append(self.conn, typ, by, data, ws=self.ws, conv=self.conv, subject=sub, ts=ts)
        A("task.kept", "office", {"reason": "mismatch", "detail": "说做完了但没有改动"}, self.tk, base)
        dc = A("decision.opened", "office", {"kind": "mismatch", "title": "「做轮播」的改动先保留了，要怎么办？", "detail": "说做完了但没有改动",
                                            "task": self.tk, "em": self.em, "owner": "lead",
                                            "options": [{"id": "merge", "label": "合并改动"}, {"id": "rework", "label": "让他接着改"},
                                                        {"id": "discard", "label": "丢掉改动", "danger": True}]}, "dc_00000001", base + 1)
        others = [self.add_task("活%d" % i, queue=False) for i in range(3)]
        landed = [A("task.landed", "office", {"files": ["a%d.py" % i], "add": 1, "del": 0}, tk, base + 60 * (i + 1)) for i, tk in enumerate(others)]
        r = self.xay("wait", "--since", str(since), "--timeout", "0")
        self.assertEqual(r.returncode, 0, r.stderr)
        lines = r.stdout.splitlines()
        self.assertEqual(lines[0], "#%d 需要你：「做轮播」的改动先保留了，要怎么办？——结果对不上（dc_00000001，选项：merge / rework / discard）" % dc)
        self.assertEqual(lines[1], "#%d 已合并 3 件：活0（Codex 1，1 个文件）、活1（Codex 1，1 个文件）、活2（Codex 1，1 个文件）" % landed[0])
        self.assertEqual(lines[2], "游标 %d" % landed[-1])
        self.assertEqual(len(lines), 3)
        for line in lines:
            self.assertLessEqual(len(line), 160, "每条不超过两行")

    def test_department_letter_routed_to_the_lead_wakes_wait(self):
        self.ok("dept", "open", "pm", "--name", "产品部", "--head", "claude")
        since = self.top()
        append(self.conn, "letter.sent", "employee:" + self.em, {"from": self.em, "to": "pm", "kind": "request",
                                                                 "subject": "需求不清楚", "body": "第二步到底要不要做？"},
               ws=self.ws, conv=self.conv, subject="lt_pm000001")
        r = self.xay("wait", "--since", str(since), "--timeout", "0")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("Codex 1 来信（写给产品部）：需求不清楚——第二步到底要不要做？（lt_pm000001，等你回复", r.stdout)
        self.assertEqual(self.letter("lt_pm000001")["delivery"]["text"], "负责人已看到")

    def test_brief_has_every_section_within_25_lines(self):
        for i in range(12):
            append(self.conn, "decision.opened", "office", {"kind": "question", "title": "问题%d" % i, "detail": "", "owner": "lead",
                                                             "options": [{"id": "yes", "label": "好"}]}, ws=self.ws, conv=self.conv, subject="dc_q%07d" % i)
        for i in range(30):
            self.add_task("任务%02d" % i, queue=False)
        working = self.add_task("正在做的", queue=False)
        append(self.conn, "turn.started", "station:" + self.em, {"task": working, "em": self.em, "kind": "first"},
               ws=self.ws, conv=self.conv, subject="tn_w0000001")
        append(self.conn, "task.progress", "employee:" + self.em, {"step": 2, "of": 4, "text": "写测试", "source": "outbox"},
               ws=self.ws, conv=self.conv, subject=working)
        self.ok("tell", "Codex 1", "回头看看")
        brief = self.ok("brief")
        lines = brief.splitlines()
        self.assertLessEqual(len(lines), 25, brief)
        self.assertTrue(lines[0].startswith("对话：测试（%s，计划第 32 版）" % self.conv), lines[0])
        self.assertTrue(lines[1].startswith("团队：Codex 1 在做「正在做的」（第 2/4 步）"), lines[1])
        self.assertIn("需要你决定 12 件：", brief)
        self.assertIn("…还有 7 件（xay2 wait 会一条条列出来）", brief)
        self.assertIn("还没送到的信 1 封：给 Codex 1 的 1 封", brief)
        self.assertIn("- 正在做的：在做，第 2/4 步：写测试（Codex 1）", brief, "在做的先列出来")
        self.assertIn("…另外", lines[-1])


if __name__ == "__main__":
    unittest.main()
