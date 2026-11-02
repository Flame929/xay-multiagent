"""员工详情栏重做（2026-10-30）的测试：短标题、同一件事试几次合成一行、今天的数字、更多大白话规则，
以及页面里工位小清单用的 JS 合并规则和服务端是同一条（装了 node 才跑）。

场景照着真机冒烟（对话 cv_20261030-184948_7e77）造：第一次 Plus 满载没做成、第二件派了又撤回、各自重派后做成。
全部用临时目录，不碰真实的 ~/.xay。
"""
import json
import os
import shutil
import subprocess
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

from xay.core import db as core_db
from xay.core import events as core_events
from xay.office import employee, plain

WS, CV = "ws_p0000001", "cv_p1"
LEAD = "lead:" + CV
EM = "em_ab000001"
ST = "station:" + EM
T0 = time.mktime((2026, 10, 30, 10, 0, 0, 0, 0, -1))      # 本机时间当天上午 10 点
NOW = T0 + 7200
SUB = "给 calc.py 加一个减法函数 sub(a, b)，并写一个最小测试 test_calc.py"
DOC = "给 calc.py 里所有函数补上中文说明（docstring）。汇报时第一句先说：你上一件活改了哪些文件"
WEB = Path(__file__).resolve().parents[1] / "web"


# ---------------------------------------------------------------- 大白话规则（纯函数）

class PanelPlainRulesTests(unittest.TestCase):
    def test_short_title_keeps_only_what_a_person_needs(self):
        self.assertEqual(plain.short_title(DOC), "给 calc.py 里所有函数补上中文说明")             # 去掉写给 AI 的要求和括号
        self.assertEqual(plain.short_title(SUB), "给 calc.py 加一个减法函数 sub，并写一个最小测试 test_calc.py")
        self.assertEqual(plain.short_title("修登录页，不要改别的文件"), "修登录页")
        self.assertEqual(plain.short_title("【进度 2/4】改一下 `V2/web/lib/app.js`"), "改一下 app.js")
        self.assertEqual(plain.short_title("做商品页框架"), "做商品页框架")
        self.assertEqual(plain.short_title(""), "")
        self.assertLessEqual(len(plain.short_title("很长" * 80)), plain.TITLE_MAX)

    def test_progress_text_drops_markers_and_repeated_step_numbers(self):
        self.assertEqual(plain.progress_text("第3步：说明已补好，测试通过；正在清理缓存。上一件活改了这些文件：`calc.py`、`test_calc.py`。"),
                         "说明已补好，检查通过；正在清理缓存")
        self.assertEqual(plain.progress_text("【进度 3/4】第2步已完成：`sub(a, b)` 已加入"), "sub(a, b) 已加入")
        self.assertEqual(plain.progress_text("先搭骨架"), "先搭骨架")
        for raw in ("第3步：写测试", "【进度 1/2】先看看"):
            out = plain.progress_text(raw)
            self.assertNotIn("第", out[:2])
            self.assertNotIn("【", out)

    def test_more_jargon_becomes_plain_words(self):
        cases = {
            "补上中文 docstring": "补上中文函数说明",
            "测试通过了": "检查通过了",
            "已经通过测试": "已经通过了检查",
            "看一下 diff 再 merge": "看一下改动再合并",
            "用了很多 token": "用了很多记忆用量",
            "在 worktree 里改": "在桌子里改",
            "工作区里一个文件都没改": "桌子里一个文件都没改",
            "清掉 __pycache__": "清掉临时缓存",
            "上下文快满了": "记忆快满了",
        }
        for raw, want in cases.items():
            self.assertEqual(plain.translate(raw), want, raw)
        self.assertIn("TOKEN=<已隐藏>", plain.plain("TOKEN=abc123"))                               # 变量名不翻，密钥照样抹掉
        self.assertEqual(plain.translate("看 test_calc.py 和 desk.py"), "看 test_calc.py 和 desk.py")   # 文件名不翻

    def test_progress_and_result_markers_never_show(self):
        self.assertEqual(plain.plain("【进度 3/4】第2步：好了【结果】完成"), "第2步：好了完成")
        self.assertEqual(plain.summary_from_report("【结果】完成：已为 `calc.py` 补上 docstring，并通过测试。")[1],
                         "完成：已为 calc.py 补上函数说明，并通过了检查。")

    def test_known_errors_are_explained(self):
        self.assertEqual(plain.explain_error("Selected model is at capacity. Please try a different model."), "AI 那边太忙（满载了）")
        self.assertEqual(plain.explain_error("429 Too Many Requests"), "用得太快，被暂时限流了")
        self.assertEqual(plain.explain_error("You exceeded your current quota"), "额度用完了")
        self.assertEqual(plain.explain_error("something odd"), "")
        self.assertEqual(plain.why_failed("something odd happened in the engine"), "")         # 看不懂的英文不硬翻
        self.assertEqual(plain.why_failed("这一轮失败了，也没有留下任何改动"), "这一轮失败了，也没有留下任何改动")

    def test_live_tool_prefixes_from_the_station_are_understood(self):
        self.assertEqual(plain.classify_loose("改文件：calc.py"), {"cat": "write", "text": "改了文件", "files": ["calc.py"]})
        self.assertEqual(plain.classify_loose("读文件：/a/b/page.css")["files"], ["page.css"])
        self.assertEqual(plain.classify_loose("运行命令：python3 -m unittest -v test_calc.py && git diff --check")["cat"], "test")

    def test_live_steps_skip_progress_markers_that_the_ledger_already_has(self):
        rows = [{"kind": "text", "text": "上一件活改了 `calc.py`。", "ts": 1}, {"kind": "text", "text": "【进度 2/4】第1步：先看看", "ts": 2},
                {"kind": "tool", "text": "改文件：calc.py", "ts": 3}, {"kind": "text", "text": "【进度 3/4】第2步：写好了", "ts": 4}]
        texts = [s["text"] for s in plain.steps_from_live_rows(rows, skip_progress=True)]
        self.assertEqual(texts, ["说：上一件活改了 calc.py", "改了 calc.py"])
        loose = [s["text"] for s in plain.steps_from_live_rows(rows)]
        self.assertFalse([t for t in loose if "【" in t], loose)                                      # 不跳过时标记也会被去掉

    def test_english_bold_headlines_in_thinking_do_not_stick_to_chinese(self):
        n = plain.LiveNarrator()
        self.assertEqual(n.feed({"kind": "thinking", "text": "**Planning to add mul function**"}), "正在思考……")
        self.assertEqual(n.feed({"kind": "thinking", "text": "先看看现有的两个函数怎么写的，照同样的格式加乘法。"}),
                         "先看看现有的两个函数怎么写的，照同样的格式加乘法")
        self.assertEqual(n.feed({"kind": "thinking", "text": "**Next step"}), "先看看现有的两个函数怎么写的，照同样的格式加乘法")   # 半截小标题不显示
        self.assertEqual(plain.drop_english_headers("**整理一下**再说"), "**整理一下**再说")                     # 中文小标题留着
        rows = [{"kind": "thinking", "text": "**Planning to add mul function**先看看现有写法。", "ts": 1}]
        self.assertEqual([s["text"] for s in plain.steps_from_live_rows(rows)], ["想：先看看现有写法"])

    def test_one_line_skips_a_bare_claim(self):
        self.assertEqual(plain.one_line("完成。已把首页改成两栏布局。跑自动测试：通过。"), "已把首页改成两栏布局。")
        self.assertEqual(plain.one_line("完成。"), "完成。")
        self.assertEqual(plain.one_line(""), "")

    def test_key_steps_prefer_the_employees_own_progress(self):
        steps = [{"icon": plain.ICONS["read"], "text": "读了 a.py"}, {"icon": plain.ICONS["write"], "text": "改了 a.py"},
                 {"icon": plain.ICONS["think"], "text": "想：怎么改"}, {"icon": plain.ICONS["test"], "text": "跑自动测试：通过 ✓"}]
        self.assertEqual(plain.key_steps(["先看", "再改", "再改"], steps), ["先看", "再改"])
        self.assertEqual(plain.key_steps(["1", "2", "3", "4", "5"], steps), ["1", "4", "5"])
        self.assertEqual(plain.key_steps([], steps), ["读了 a.py", "改了 a.py", "跑自动测试：通过 ✓"])

    def test_notes_drop_ids_and_use_short_titles(self):
        notes = ("- 「%s」（tk_2ce2b6e8）：没做成；没有改文件\n- 「%s」（tk_123cd5e2）：已合并回项目；汇报：已为 `calc.py` 补充 docstring，并通过测试。"
                 % (SUB, DOC))
        out = plain.notes_text(notes)
        self.assertNotIn("tk_", out)
        self.assertNotIn("汇报时第一句", out)
        self.assertNotIn("docstring", out)
        self.assertIn("「给 calc.py 里所有函数补上中文说明」：已合并回项目", out)


# ---------------------------------------------------------------- 接口：合并、今天、短标题

class PanelApiTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="xay-panel-test-"))
        self.addCleanup(shutil.rmtree, self.tmp, True)
        env = mock.patch.dict(os.environ, {"XAY2_HOME": str(self.tmp / "v2")})
        env.start()
        self.addCleanup(env.stop)
        self.conn = core_db.open_db(str(self.tmp / "v2" / "xay.db"))
        self.addCleanup(self.conn.close)
        self.seed()

    def A(self, typ, by, data, subject=None, ts=None):
        return core_events.append(self.conn, typ, by, data, ws=WS, conv=CV, subject=subject, ts=ts)

    def task(self, tk, title, ts, key):
        self.A("task.added", LEAD, {"key": key, "title": title, "owner": EM, "deps": [], "writes": [], "steps": [], "mode": "write", "reason": "r"}, tk, ts)

    def seed(self):
        A = self.A
        A("conv.begun", LEAD, {"title": "冒烟", "lead_kind": "claude", "aliases": [], "cwd": "/tmp/x"}, CV, T0)
        A("employee.hired", LEAD, {"agent": "codex"}, EM, T0)
        # 第 1 件第一次：Plus 满载，没做成
        self.task("tk_sub00001", SUB, T0 + 1, "sub")
        A("turn.started", ST, {"task": "tk_sub00001", "em": EM, "kind": "first"}, "tn_00000001", T0 + 2)
        A("turn.ended", ST, {"em": EM, "status": "failed", "evidence": {"cli_status": "failed", "changed_files": [],
                                                                        "error": "Selected model is at capacity."}}, "tn_00000001", T0 + 10)
        A("task.failed", ST, {"reason": "这一轮失败了，也没有留下任何改动", "detail": "Selected model is at capacity. Please try a different model."},
          "tk_sub00001", T0 + 10)
        # 第 2 件：派了还没开始就撤回
        self.task("tk_doc00001", DOC, T0 + 5, "doc")
        A("task.removed", LEAD, {"reason": "第一件因 Plus 满载没做成，重新派"}, "tk_doc00001", T0 + 60)
        # 第 1 件第二次：做成，合并，验收
        self.task("tk_sub00002", SUB, T0 + 61, "sub2")
        A("turn.started", ST, {"task": "tk_sub00002", "em": EM, "kind": "continue"}, "tn_00000002", T0 + 62)
        A("task.progress", ST, {"step": 2, "of": 4, "text": "第1步：先检查 `calc.py` 的写法。", "source": "marker"}, "tk_sub00002", T0 + 70)
        A("turn.ended", ST, {"em": EM, "status": "completed", "evidence": {"cli_status": "completed", "changed_files": ["calc.py", "test_calc.py"]}},
          "tn_00000002", T0 + 100)
        A("task.landed", ST, {"files": ["calc.py", "test_calc.py"], "add": 16, "del": 0}, "tk_sub00002", T0 + 101)
        A("task.accepted", LEAD, {"note": "验收：sub 和测试都在"}, "tk_sub00002", T0 + 102)
        # 第 2 件重派：做成，汇报里带标记和行话
        self.task("tk_doc00002", DOC, T0 + 61, "doc2")
        A("turn.started", ST, {"task": "tk_doc00002", "em": EM, "kind": "continue"}, "tn_00000003", T0 + 200)
        A("task.progress", ST, {"step": 4, "of": 4, "text": "第3步：说明已补好，测试通过；正在清理。上一件活改了这些文件：`calc.py`、`test_calc.py`。",
                                "source": "marker"}, "tk_doc00002", T0 + 230)
        A("task.reported", ST, {"claim": "done", "summary": "【结果】完成：已为 `calc.py` 里的所有函数补充中文 docstring，并通过测试。"}, "tk_doc00002", T0 + 240)
        A("turn.ended", ST, {"em": EM, "status": "completed", "evidence": {"cli_status": "completed", "changed_files": ["calc.py"]}}, "tn_00000003", T0 + 241)
        A("task.landed", ST, {"files": ["calc.py"], "add": 2, "del": 0}, "tk_doc00002", T0 + 242)
        # 同一个标题，但前一次已经做成：是两件活，不合并
        for i, tk in enumerate(("tk_chk00001", "tk_chk00002")):
            self.task(tk, "跑一遍检查", T0 + 300 + i * 100, "chk%d" % i)
            A("turn.started", ST, {"task": tk, "em": EM, "kind": "continue"}, "tn_0000001%d" % i, T0 + 301 + i * 100)
            A("turn.ended", ST, {"em": EM, "status": "completed", "evidence": {"cli_status": "completed", "changed_files": []}}, "tn_0000001%d" % i,
              T0 + 310 + i * 100)
            A("task.landed", ST, {"files": [], "add": 0, "del": 0}, tk, T0 + 311 + i * 100)
        # 只撤回过、没有重派的活
        self.task("tk_idea0001", "临时想法：顺便看看首页", T0 + 500, "idea")
        A("task.removed", LEAD, {"reason": "先不做了"}, "tk_idea0001", T0 + 501)

    def build(self):
        return employee.build(self.conn, EM, conv=CV, live_root=self.tmp / "v2" / "live", now=NOW)

    def row(self, body, title):
        hits = [r for r in body["timeline"] if r["task"]["short_title"] == title]
        self.assertEqual(len(hits), 1, [r["task"]["short_title"] for r in body["timeline"]])
        return hits[0]

    def test_retries_are_merged_into_one_row(self):
        body = self.build()
        titles = [r["task"]["short_title"] for r in body["timeline"]]
        self.assertEqual(body["total"], 5)
        self.assertEqual(titles.count("给 calc.py 加一个减法函数 sub，并写一个最小测试 test_calc.py"), 1)
        self.assertEqual(titles.count("给 calc.py 里所有函数补上中文说明"), 1)
        self.assertEqual(titles.count("跑一遍检查"), 2)                                      # 做成之后再派的是新活
        sub = self.row(body, "给 calc.py 加一个减法函数 sub，并写一个最小测试 test_calc.py")
        self.assertEqual((sub["task"]["id"], sub["task"]["status"], sub["tries"]), ("tk_sub00002", "done", 2))
        self.assertEqual([a["id"] for a in sub["attempts"]], ["tk_sub00001", "tk_sub00002"])
        self.assertEqual(sub["attempts"][0]["note"], "没做成：AI 那边太忙（满载了）。")
        self.assertEqual(sub["attempts"][1]["note"], "做完了。")
        self.assertEqual(sub["task"]["first_started"], T0 + 2)
        self.assertIn("负责人看过了：sub 和测试都在", sub["result"]["summary"])
        self.assertEqual(sub["result"]["short"], "做完了，改了 2 个文件，已经放进项目。")
        self.assertEqual(sub["files"], ["calc.py", "test_calc.py"])

    def test_a_retry_after_an_unstarted_cancel_is_not_counted_as_a_try(self):
        doc = self.row(self.build(), "给 calc.py 里所有函数补上中文说明")
        self.assertEqual((doc["task"]["id"], doc["task"]["status"], doc["tries"]), ("tk_doc00002", "reported", 1))
        first = doc["attempts"][0]
        self.assertEqual((first["id"], first["status"], first["ran"]), ("tk_doc00001", "canceled", False))
        self.assertEqual(first["note"], "派了又撤回了：第一件因 Plus 满载没做成，重新派。")

    def test_plain_fields_carry_no_markers_backticks_or_instructions(self):
        body = self.build()
        doc = self.row(body, "给 calc.py 里所有函数补上中文说明")
        self.assertEqual(doc["result"]["summary"], "完成：已为 calc.py 里的所有函数补充中文函数说明，并通过了检查。")
        self.assertEqual(doc["key_steps"], ["说明已补好，检查通过；正在清理"])
        visible = [body["person"]["now"], body["person"]["progress"].get("text"), body["person"]["notes"]]
        for r in body["timeline"]:
            visible += [r["task"]["short_title"], r["ask"], r["result"]["summary"], r["result"]["short"], r["key_steps"],
                        [a["note"] for a in r["attempts"]], [s["text"] for s in r["steps"]]]
        blob = json.dumps(visible, ensure_ascii=False)
        for bad in ("【", "`", "docstring", "汇报时第一句", "tk_", "Selected model"):
            self.assertNotIn(bad, blob)
        self.assertIn("`calc.py`", doc["result"]["detail"])                                  # 原话留着，只在"看他的原话"里

    def test_person_progress_is_one_clean_sentence(self):
        p = self.build()["person"]
        self.assertEqual(p["progress"]["text"], "说明已补好，检查通过；正在清理")
        self.assertEqual((p["progress"]["step"], p["progress"]["of"]), (4, 4))

    def test_canceled_only_work_stays_as_its_own_row(self):
        idea = self.row(self.build(), "临时想法：顺便看看首页")
        self.assertEqual((idea["task"]["status"], idea["tries"], idea["result"]["claim"]), ("canceled", 0, "canceled"))
        self.assertEqual(idea["result"]["short"], "这件事撤回了：先不做了。")

    def test_today_counts_rows_not_attempts(self):
        body = self.build()
        self.assertEqual(body["today"], {"done": 4, "failed": 0, "needs_you": 0, "working": 0, "queued": 0})
        later = employee.build(self.conn, EM, conv=CV, now=NOW + 86400)                      # 第二天再看：昨天的不算今天
        self.assertEqual(later["today"]["done"], 0)

    def test_newest_first_by_the_latest_attempt(self):
        ids = [r["task"]["id"] for r in self.build()["timeline"]]
        self.assertEqual(ids, ["tk_idea0001", "tk_chk00002", "tk_chk00001", "tk_doc00002", "tk_sub00002"])


class GroupAttemptsUnitTests(unittest.TestCase):
    @staticmethod
    def e(tk, title, status, ts, ran=True, legacy=False):
        return ({"task": {"id": tk, "title": title, "short_title": title, "status": status, "legacy": legacy, "started": ts, "ended": ts + 1},
                 "_ran": ran, "_note": status}, ts)

    def test_rules(self):
        out = employee.group_attempts([
            self.e("a1", "改首页", "failed", 1), self.e("a2", "改首页", "failed", 2), self.e("a3", "改首页", "working", 3),
            self.e("b1", "修登录", "done", 4), self.e("b2", "修登录", "failed", 5),
            self.e("c1", "旧活", "failed", 6, legacy=True), self.e("c2", "旧活", "done", 7),
        ])
        got = [(item["task"]["id"], [a["id"] for a in item["attempts"]], item["tries"]) for item, _ in out]
        self.assertEqual(got, [("a3", ["a1", "a2", "a3"], 3), ("b1", ["b1"], 1), ("b2", ["b2"], 1), ("c1", ["c1"], 1), ("c2", ["c2"], 1)])
        self.assertTrue(all(not k.startswith("_") for item, _ in out for k in item))         # 内部字段不外泄


# ---------------------------------------------------------------- 页面：骨架和 JS 规则

class PanelPageTests(unittest.TestCase):
    def test_org_html_has_the_bento_tiles(self):
        html = (WEB / "org.html").read_text(encoding="utf-8")
        # 第 3 版（ui-v2-employee-panel.md）："今天"并进清单的计数，加了"额度"
        for tile in ('id="emp-id"', 'class="emp-art"', 'id="emp-now"', 'id="emp-quota"', 'id="emp-mem"', 'id="emp-work"', 'id="emp-notes"'):
            self.assertIn(tile, html)

    def test_panel_uses_server_wording_and_keeps_raw_words_folded(self):
        js = (WEB / "lib" / "employee-panel.js").read_text(encoding="utf-8")
        for marker in ("short_title", "attempts", "key_steps", "收起原话", "撤回 ${canceled.length} 件", "-full.webp", "-calm.webp"):
            self.assertIn(marker, js)
        self.assertNotIn("emp-step-icon", js)                                                # 不再把带表情的步骤直接摆出来

    def test_station_list_uses_the_same_merge_rule(self):
        js = (WEB / "lib" / "station.js").read_text(encoding="utf-8")
        self.assertIn("mergeAttempts(tasks)", js)
        self.assertNotIn("点开看全部", js)
        css = (WEB / "org.css").read_text(encoding="utf-8")
        self.assertNotIn("line-through", css)                                                # 撤回的不再划线堆着


@unittest.skipUnless(shutil.which("node"), "没装 node，跳过 JS 规则对照")
class JsRuleParityTests(unittest.TestCase):
    """utils.js 的 shortTitle / mergeAttempts 和服务端的 short_title / group_attempts 对同样的输入给出同样的结果。"""

    def run_js(self, code):
        url = (WEB / "lib" / "utils.js").as_uri()
        script = "import * as U from %s;\n%s" % (json.dumps(url), code)
        out = subprocess.run(["node", "--input-type=module", "-e", script], capture_output=True, text=True, timeout=30)
        self.assertEqual(out.returncode, 0, out.stderr)
        return json.loads(out.stdout)

    def test_short_title_matches_the_server(self):
        titles = [DOC, SUB, "修登录页，不要改别的文件", "【进度 2/4】改一下 `V2/web/lib/app.js`", "做商品页框架", "第 3/4 步 and/or 都留着", "很长" * 80]
        js = self.run_js("console.log(JSON.stringify(%s.map(t => U.shortTitle(t))))" % json.dumps(titles, ensure_ascii=False))
        self.assertEqual(js, [plain.short_title(t) for t in titles])

    def test_progress_line_matches_the_server_on_plain_chinese(self):
        texts = ["第3步：说明已补好；正在清理缓存。上一件活改了这些文件：`calc.py`、`test_calc.py`。",
                 "【进度 3/4】第2步已完成：`sub(a, b)` 已加入", "先搭骨架", "第 2 步 / 共 4 步：改 `V2/web/a.js`"]
        js = self.run_js("console.log(JSON.stringify(%s.map(t => U.progressLine(t))))" % json.dumps(texts, ensure_ascii=False))
        self.assertEqual(js, [plain.progress_text(t) for t in texts])

    def test_merge_attempts_matches_the_server(self):
        tasks = [{"id": "a1", "title": SUB, "status": "failed"}, {"id": "b1", "title": DOC, "status": "canceled"},
                 {"id": "a2", "title": SUB, "status": "done"}, {"id": "b2", "title": DOC, "status": "reported"},
                 {"id": "c1", "title": "跑一遍检查", "status": "done"}, {"id": "c2", "title": "跑一遍检查", "status": "working"}]
        js = self.run_js("console.log(JSON.stringify(U.mergeAttempts(%s).map(g => [g.id, g.ids, g.status])))" % json.dumps(tasks, ensure_ascii=False))
        entries = [({"task": {"id": t["id"], "title": t["title"], "short_title": plain.short_title(t["title"]), "status": t["status"],
                              "legacy": False, "started": i, "ended": None}, "_ran": True, "_note": ""}, i) for i, t in enumerate(tasks)]
        py = [[item["task"]["id"], [a["id"] for a in item["attempts"]], item["task"]["status"]] for item, _ in employee.group_attempts(entries)]
        self.assertEqual(js, py)
        self.assertEqual([g[0] for g in js], ["a2", "b2", "c1", "c2"])


if __name__ == "__main__":
    unittest.main()
