"""员工详情（左侧面板）的测试：接口形状、大白话规则、旧 run 目录只读、Host 检查、直播条、部门前缀。

全部用临时目录；旧 run 目录是测试里现造的假目录，不碰真实的 ~/.xay 和项目里的 .xay。
"""
import json
import os
import shutil
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

from tests import test_office_web as W
from xay.core import db as core_db
from xay.core import events as core_events
from xay.core import humanize as core_humanize
from xay.core import snapshot as core_snapshot
from xay.office import employee, plain

WS, CV = "ws_e0000001", "cv_e1"
LEAD = "lead:" + CV
EM1, EM2, EM3 = "em_aa000001", "em_bb000001", "em_cc000001"
TK = "tk_e0000001"
T0 = 1793300000


# ---------------------------------------------------------------- 大白话规则（纯函数）

class PlainRulesTests(unittest.TestCase):
    def test_glossary_translates_jargon(self):
        out = plain.translate("先 commit，再 merge 到主分支；用 pytest 跑，看 diff，调 API 和 endpoint，改 schema，refactor，lint，CLI")
        for word in ("保存版本", "合并", "自动测试", "改动", "接口", "接口地址", "数据格式", "整理代码", "代码检查", "命令行"):
            self.assertIn(word, out)
        for jargon in ("commit", "merge", "pytest", "diff", "endpoint", "schema", "refactor", "lint", "CLI"):
            self.assertNotIn(jargon, out)
        self.assertIn("桌子", plain.translate("建了一个 worktree"))          # 2026-10-30 详情栏重做：worktree / desk 统一叫"桌子"
        self.assertIn("自动测试", plain.translate("python -m unittest"))

    def test_glossary_leaves_file_names_alone(self):
        self.assertEqual(plain.translate("看 merge3.py 和 test_diff.py 和 merge.py"), "看 merge3.py 和 test_diff.py 和 merge.py")

    def test_paths_become_bare_file_names(self):
        out = plain.bare_paths("改了 /home/you/.xay/worktrees/x-1/V2/web/lib/api.js 和 V2/docs/protocol.md，参考 https://example.com/a/b?c=1")
        self.assertIn("api.js", out)
        self.assertIn("protocol.md", out)
        self.assertIn("example.com", out)
        self.assertNotIn("/home", out)
        self.assertNotIn("/Users", out)
        self.assertNotIn("V2/docs", out)
        self.assertEqual(plain.bare_paths("要么 and/or 比例 0.5/1.0 第 3/4 步"), "要么 and/or 比例 0.5/1.0 第 3/4 步")
        self.assertEqual(plain.bare_name("V2/fixtures/golden/"), "golden 文件夹")

    def test_markdown_and_code_blocks_are_stripped(self):
        out = plain.plain("## 标题\n**重点**：看 [文档](http://x.y/z)\n```python\nprint('secret code')\n```\n- 第一项\n- `V2/a/b.py` 改好了")
        self.assertNotIn("```", out)
        self.assertNotIn("print", out)
        self.assertNotIn("**", out)
        self.assertNotIn("#", out)
        self.assertNotIn("http", out)
        self.assertIn("b.py 改好了", out)

    def test_inline_commands_and_json_never_show_raw(self):
        out = plain.plain("验证：`python3 -m unittest discover -s V2/tests`：通过；日志 `{\"ts\": 1, \"by\": \"x\"}` 已写")
        self.assertIn("跑自动测试：通过", out)
        self.assertNotIn("python3", out)
        self.assertNotIn("{", out)
        self.assertIn("一段数据", out)

    def test_secrets_are_redacted(self):
        out = plain.plain("key sk-abcdefghijklmnop1234 and API_KEY=hunter2 and Authorization: Bearer abc.def")
        for secret in ("sk-abcdefghijklmnop1234", "hunter2", "abc.def"):
            self.assertNotIn(secret, out)

    def test_commands_become_phrases(self):
        cases = {
            "python3 -m unittest discover -s V2/tests": ("test", "跑自动测试"),
            "cd /tmp/x && pytest -q tests/test_a.py": ("test", "跑自动测试"),
            "node --check V2/web/lib/app.js": ("check", "检查代码能否运行"),
            "git commit -am 'wip'": ("commit", "保存版本"),
            "git diff HEAD~1": ("git", "查看改动记录"),
            "pip install requests": ("install", "安装依赖"),
            "curl -s https://api.example.com/v1?x=1": ("fetch", "访问网址 api.example.com"),
            "mkdir -p out && ls out": ("files", "整理文件"),
            "rg -n foo src": ("search", "查找内容"),
            "python3 scripts/run_it.py --fast": ("script", "运行脚本 run_it.py"),
            "echo hi": ("noise", ""),
        }
        for cmd, (cat, text) in cases.items():
            got = plain.classify_cmd(cmd)
            self.assertEqual((got["cat"], got["text"]), (cat, text), cmd)
        read = plain.classify_cmd("pwd && cat .xay/context.md && sed -n '1,80p' /home/you/proj/README.md && head -5 a/b/c.txt")
        self.assertEqual((read["cat"], read["files"]), ("read", ["context.md", "README.md", "c.txt"]))
        write = plain.classify_cmd("cat > V2/xay/new_file.py <<'PY'\nprint('hi')\nPY")
        self.assertEqual((write["cat"], write["files"]), ("write", ["new_file.py"]))
        loop = plain.classify_cmd('for f in V2/x/*.json; do echo ==$f; cat "$f"; done')
        self.assertEqual(loop["cat"], "read")

    def _legacy_events(self):
        return [
            {"type": "turn.started", "ts": 1, "data": {}},
            {"type": "reasoning", "ts": 2, "text": "Let me look at the repo structure first and then plan.", "data": {}},
            {"type": "reasoning", "ts": 3, "text": "先看看项目有哪些文件。其次再想办法。", "data": {}},
            {"type": "tool.call", "ts": 4, "kind": "read", "text": "a.py", "data": {"id": "c1", "tool": "read"}},
            {"type": "tool.call", "ts": 5, "kind": "cmd", "text": "cat src/b.py && sed -n '1,40p' /Users/x/c.py", "data": {"id": "c2", "tool": "bash"}},
            {"type": "tool.result", "ts": 6, "data": {"id": "c2", "ok": True, "exit": 0}},
            {"type": "tool.call", "ts": 7, "kind": "write", "text": "index.html", "data": {"id": "c3", "tool": "edit"}},
            {"type": "tool.call", "ts": 8, "kind": "write", "text": "app.css", "data": {"id": "c3b", "tool": "edit"}},
            {"type": "tool.call", "ts": 9, "kind": "cmd", "text": "cd /tmp/x && python3 -m unittest tests.test_a", "data": {"id": "c4", "tool": "bash"}},
            {"type": "tool.result", "ts": 10, "data": {"id": "c4", "ok": False, "exit": 1}},
            {"type": "tool.call", "ts": 11, "kind": "cmd", "text": "python3 -m py_compile x.py", "data": {"id": "c5"}},
            {"type": "tool.result", "ts": 12, "data": {"id": "c5", "ok": True, "exit": 0}},
            {"type": "tool.call", "ts": 13, "kind": "cmd", "text": "git commit -am wip", "data": {"id": "c6"}},
            {"type": "tool.call", "ts": 14, "kind": "cmd", "text": "curl -s https://api.example.com/x?token=abc123 | jq .", "data": {}},
            {"type": "tool.call", "ts": 15, "kind": "web", "text": "如何给页面加缓存", "data": {"tool": "web_search"}},
            {"type": "message", "ts": 16, "text": "改完了，开始检查。", "data": {"final": False}},
            {"type": "message", "ts": 17, "text": "FINAL REPORT SHOULD NOT BE A STEP", "data": {"final": True}},
            {"type": "error", "ts": 18, "text": json.dumps({"message": "Selected model is at capacity. Please try again."}), "data": {}},
        ]

    def test_legacy_events_become_merged_plain_steps(self):
        steps = plain.steps_from_legacy_events(self._legacy_events())
        texts = [s["text"] for s in steps]
        self.assertEqual(texts, [
            "想：先看看项目有哪些文件",                         # 英文思考不显示；中文思考只留第一句
            "读了 3 个文件：a.py、b.py、c.py",                  # 连续的读合并（含 cat / sed 读文件）
            "改了 2 个文件：index.html、app.css",               # 连续的改合并
            "跑自动测试：没通过 ✗",
            "检查代码能否运行：通过 ✓",
            "保存版本",
            "访问网址 api.example.com",
            "查资料：如何给页面加缓存",
            "说：改完了，开始检查",
            "遇到问题：AI 那边太忙（满载了）",                  # 认得出的报错翻成人话（plain.explain_error）
        ])
        self.assertTrue(all(isinstance(s["ts"], int) and s["icon"] for s in steps))
        blob = json.dumps([s["text"] for s in steps], ensure_ascii=False)
        for raw in ("python3", "unittest", "{", "sed -n", "cat ", "token=abc123", "FINAL REPORT"):
            self.assertNotIn(raw, blob)

    def test_steps_are_capped_with_a_gap_marker(self):
        events = [{"type": "tool.call", "ts": i, "kind": "cmd", "text": "python3 -m unittest t%d" % i, "data": {"id": "t%d" % i}} for i in range(60)]
        steps = plain.steps_from_legacy_events(events)
        self.assertEqual(len(steps), plain.MAX_STEPS)
        self.assertIn("中间还有 31 步", [s["text"] for s in steps][plain.HEAD_STEPS])

    def test_ask_from_prompt_takes_the_task_paragraph(self):
        prompt = ("# 执行准则（codex）\n- 直接动手。\n\n# 任务\n背景：旧首页太挤。\n\n"
                  "任务：把首页改成两栏布局，并给 `src/index.html` 加上手机适配。注意不要动 worktree 之外的文件，也别 git commit。做完告诉我。第四句不该出现。\n\n"
                  "允许改的范围：src/**\n\n---\n规则：不要 git commit。")
        ask = plain.ask_from_prompt(prompt)
        self.assertTrue(ask.startswith("把首页改成两栏布局，并给 index.html 加上手机适配。"), ask)
        self.assertIn("桌子", ask)
        self.assertNotIn("规则", ask)
        self.assertNotIn("允许改的范围", ask)
        self.assertNotIn("旧首页太挤", ask)
        self.assertLessEqual(len(plain.sentences(ask)), 3)
        self.assertNotIn("第四句", ask)

    def test_summary_is_the_result_line_plus_meaningful_sentences(self):
        report = ("结果：完成。已把首页改成两栏布局。\n\n改动文件：\n- `src/index.html`\n- `src/app.css`\n\n"
                  "验证：\n- `python3 -m unittest tests.test_a`：通过\n- 手机宽度看过了\n\n问题与需要负责人决定的事：无\n")
        claim, summary = plain.summary_from_report(report)
        self.assertEqual(claim, "done")
        self.assertTrue(summary.startswith("完成。已把首页改成两栏布局。"), summary)
        self.assertIn("跑自动测试：通过", summary)
        self.assertNotIn("index.html", summary)               # 改动的文件不在摘要里，单独列
        self.assertNotIn("python3", summary)
        self.assertLessEqual(len(plain.sentences(summary)), 3)
        self.assertEqual(plain.summary_from_report("结果：没做完。还差测试。")[0], "partial")
        self.assertEqual(plain.summary_from_report("【结果】卡住：缺一个密码")[0], "blocked")

    def test_live_narrator_joins_fragments_and_never_shows_raw(self):
        n = plain.LiveNarrator()
        self.assertEqual(n.feed({"kind": "thinking", "text": "先看看"}), "先看看")
        self.assertEqual(n.feed({"kind": "thinking", "text": "文件结构。再改页面"}), "再改页面")
        self.assertEqual(n.feed({"kind": "tool", "text": "python3 -m unittest x", "tool_kind": "cmd"}), "跑自动测试")
        self.assertEqual(n.feed({"kind": "tool", "text": "/a/b/page.css", "tool_kind": "read"}), "读 page.css")
        self.assertEqual(n.feed({"kind": "thinking", "text": "Now I will write the implementation of the function"}), "正在思考……")
        self.assertIsNone(n.feed({"kind": "status", "text": "working"}))


# ---------------------------------------------------------------- 接口

RESULT_MD = ("结果：完成。已把首页改成两栏布局。\n\n改动文件：\n- `src/index.html`\n\n"
             "验证：\n- `python3 -m unittest tests.test_a`：通过\n\n问题与需要负责人决定的事：无\n")
PROMPT_MD = ("# 执行准则（codex）\n- 直接动手。\n\n# 任务\n背景：旧首页太挤。\n\n"
             "任务：把首页改成两栏布局，并给 `src/index.html` 加上手机适配。注意不要动 worktree 之外的文件。\n\n允许改的范围：src/**\n\n---\n规则：不要 git commit。")


class EmployeeApiCase(W.OfficeCase):
    seed = False

    def setUp(self):
        super().setUp()
        self.seed_people()

    def A(self, typ, by, data, subject=None, ts=None, conv=CV):
        return core_events.append(self.conn, typ, by, data, ws=WS, conv=conv, subject=subject, ts=ts)

    def seed_people(self):
        A = self.A
        A("conv.begun", LEAD, {"title": "员工详情测试", "lead_kind": "claude", "aliases": [], "cwd": "/tmp/t"}, CV, T0)
        A("dept.opened", LEAD, {"name": "页面部", "mission": "做页面", "owns": [], "head": "rules"}, "fe", T0 + 1)
        for em, agent in ((EM1, "codex"), (EM2, "dsh"), (EM3, "agy")):
            A("employee.hired", LEAD, {"agent": agent}, em, T0 + 2)
        A("employee.joined", LEAD, {"dept": "fe"}, EM1, T0 + 3)
        # EM1：一件 V2 自己的活
        A("task.added", LEAD, {"key": "page", "title": "做商品页框架", "dept": "fe", "owner": EM1, "deps": [], "writes": [], "steps": [],
                               "card": "# 任务\n背景：新页面。\n\n任务：做新版商品页的页面框架，手机和电脑都要能看。先搭骨架，再看一遍。\n\n---\n规则：不要 git commit。",
                               "mode": "write", "reason": "开工"}, TK, T0 + 10)
        A("turn.started", "station:" + EM1, {"task": TK, "em": EM1, "kind": "first"}, "tn_e0000001", T0 + 20)
        A("task.progress", "station:" + EM1, {"step": 1, "of": 3, "text": "先搭骨架", "source": "marker"}, TK, T0 + 40)
        A("task.reported", "station:" + EM1, {"claim": "done", "summary": RESULT_MD}, TK, T0 + 100)
        A("turn.ended", "station:" + EM1, {"em": EM1, "status": "completed", "evidence": {
            "cli_status": "completed", "claim": "done", "changed_files": ["/work/wt/web/product/index.html", "web/product/page.css"],
            "diff_stat": {"files": 2, "add": 10, "del": 1}, "verify": [{"cmd": "python3 -m unittest tests.test_product", "ok": True}]}}, "tn_e0000001", T0 + 110)
        A("task.landed", "station:" + EM1, {"files": ["/work/wt/web/product/index.html", "web/product/page.css"], "add": 10, "del": 1}, TK, T0 + 112)
        # 直播文件：落在这一轮的时间窗口内
        live = self.tmp / "v2" / "live" / EM1
        live.mkdir(parents=True, exist_ok=True)
        rows = [{"n": 1, "kind": "thinking", "text": "先看看现有的页面，", "ts": T0 + 21}, {"n": 2, "kind": "thinking", "text": "再决定怎么搭。", "ts": T0 + 22},
                {"n": 3, "kind": "tool", "text": "cat web/product/index.html", "ts": T0 + 25, "tool_kind": "cmd"},
                {"n": 4, "kind": "tool", "text": "web/product/index.html", "ts": T0 + 50, "tool_kind": "write"},
                {"n": 5, "kind": "tool", "text": "python3 -m unittest tests.test_product", "ts": T0 + 90, "tool_kind": "cmd"},
                {"n": 6, "kind": "tool", "text": "窗口之外的活动", "ts": T0 + 5000}]
        (live / "1.jsonl").write_text("\n".join(json.dumps(r, ensure_ascii=False) for r in rows) + "\n", encoding="utf-8")

    def make_run(self, name, *, events=None, prompt=PROMPT_MD, result=RESULT_MD, meta=None, root="proj"):
        d = self.tmp / root / ".xay" / "runs" / name
        d.mkdir(parents=True)
        if prompt is not None:
            (d / "prompt.md").write_text(prompt, encoding="utf-8")
        if result is not None:
            (d / "result.md").write_text(result, encoding="utf-8")
        evs = events if events is not None else PlainRulesTests._legacy_events(None)
        (d / "events.norm.jsonl").write_text("\n".join(json.dumps(e, ensure_ascii=False) for e in evs) + "\n", encoding="utf-8")
        m = {"title": "改首页", "status": "done", "started": T0 + 200, "duration_s": 60,
             "changed_files": ["/work/wt/src/index.html", "tests/test_a.py"], "fail_reason": ""}
        m.update(meta or {})
        (d / "meta.json").write_text(json.dumps(m, ensure_ascii=False), encoding="utf-8")
        return d

    def bridge_run(self, em, run_dir, *, status="done", title="改首页", dept=None, head="结果：完成。", agent="codex", ts=None):
        data = {"run_dir": str(run_dir), "agent": agent, "title": title, "status": status, "started": T0 + 200, "ended": T0 + 260, "result_head": head}
        if dept:
            data["dept"] = dept
        return self.A("bridge.run", "bridge", data, em, ts or T0 + 200)

    def employee(self, em, query=""):
        return self.get_json("/api/v2/employee?em=%s&conv=%s%s" % (em, CV, query))

    # ---- 形状 ----
    def test_v2_native_employee_shape(self):
        status, body = self.employee(EM1)
        self.assertEqual(status, 200)
        p = body["person"]
        self.assertEqual((p["key"], p["label"], p["agent"], p["dept"], p["dept_name"]), (EM1, "Codex 1", "codex", "fe", "页面部"))
        for key in ("state", "context", "progress", "online", "notes"):
            self.assertIn(key, p)
        self.assertEqual(len(body["timeline"]), 1)
        card = body["timeline"][0]
        t = card["task"]
        self.assertEqual((t["id"], t["title"], t["legacy"], t["dept"], t["dept_name"]), (TK, "做商品页框架", False, "fe", "页面部"))
        self.assertEqual(t["started"], T0 + 20)
        self.assertEqual(t["ended"], T0 + 110)
        self.assertEqual(card["ask"], "做新版商品页的页面框架，手机和电脑都要能看。先搭骨架，再看一遍。")
        texts = [s["text"] for s in card["steps"]]
        self.assertEqual(texts[0], "想：先看看现有的页面，再决定怎么搭")
        self.assertIn("读了 index.html", texts)
        self.assertIn("改了 index.html", texts)
        self.assertIn("第 1 步 / 共 3 步：先搭骨架", texts)
        self.assertIn("跑自动测试：通过 ✓", texts)                        # 直播里那步被证据里的结果补全，不重复
        self.assertEqual(texts.count("跑自动测试"), 0)
        self.assertNotIn("窗口之外的活动", json.dumps(card, ensure_ascii=False))
        self.assertEqual(card["steps"], sorted(card["steps"], key=lambda s: s["ts"]))
        r = card["result"]
        self.assertEqual(r["claim"], "done")
        self.assertTrue(r["summary"].startswith("完成。已把首页改成两栏布局。"))
        self.assertIn("python3 -m unittest", r["detail"])               # 原文留着，供"看技术细节"
        self.assertNotIn("python3", r["summary"])
        self.assertEqual(card["files"], ["index.html", "page.css"])      # 只给文件名

    def test_no_raw_commands_or_json_leak_into_plain_fields(self):
        _, body = self.employee(EM1)
        card = body["timeline"][0]
        blob = json.dumps([card["ask"], [s["text"] for s in card["steps"]], card["result"]["summary"], card["files"]], ensure_ascii=False)
        for raw in ("python3", "cat web", "/work/wt", "```", "{"):
            self.assertNotIn(raw, blob)

    def test_legacy_employee_reads_the_old_run_dir_and_adds_department(self):
        run = self.make_run("20261030-120000-codex-demo")
        self.bridge_run(EM1, run, dept="fe")
        _, body = self.employee(EM1)
        self.assertEqual(body["total"], 2)
        legacy = next(c for c in body["timeline"] if c["task"]["legacy"])
        self.assertEqual(legacy["task"]["id"], "run:20261030-120000-codex-demo")
        self.assertEqual((legacy["task"]["status"], legacy["task"]["dept"], legacy["task"]["dept_name"]), ("done", "fe", "页面部"))
        self.assertTrue(legacy["ask"].startswith("把首页改成两栏布局，并给 index.html 加上手机适配。"), legacy["ask"])
        self.assertIn("桌子", legacy["ask"])
        self.assertNotIn("规则", legacy["ask"])
        texts = [s["text"] for s in legacy["steps"]]
        self.assertIn("读了 3 个文件：a.py、b.py、c.py", texts)
        self.assertIn("跑自动测试：没通过 ✗", texts)
        self.assertIn("访问网址 api.example.com", texts)
        blob = json.dumps(legacy, ensure_ascii=False)
        self.assertNotIn("token=abc123", blob)
        self.assertEqual(legacy["result"]["claim"], "done")
        self.assertTrue(legacy["result"]["summary"].startswith("完成。已把首页改成两栏布局。"))
        self.assertIn("python3 -m unittest tests.test_a", legacy["result"]["detail"])
        self.assertEqual(legacy["files"], ["index.html", "test_a.py"])
        # 新的在前：旧 run 比 V2 任务开始得晚
        self.assertEqual([c["task"]["legacy"] for c in body["timeline"]], [True, False])

    def test_failed_legacy_run_says_so_honestly(self):
        run = self.make_run("20261030-130000-agy-bad", result="结果：完成。我觉得都做好了。", meta={"status": "failed", "fail_reason": "cli_error"})
        self.bridge_run(EM3, run, status="failed", agent="agy", head="")
        _, body = self.employee(EM3)
        card = body["timeline"][0]
        self.assertEqual(card["result"]["claim"], "failed")
        self.assertIn("系统记为没做成", card["result"]["summary"])
        self.assertIn("cli_error", card["result"]["detail"])
        self.assertEqual((card["task"]["status"], card["task"]["dept"], card["task"]["dept_name"]), ("failed", None, ""))

    def test_run_dir_outside_old_layout_is_never_read(self):
        evil = self.tmp / "elsewhere" / "evil-run"
        evil.mkdir(parents=True)
        (evil / "prompt.md").write_text("# 任务\n任务：这段话不该被读到 SECRET-FROM-OUTSIDE\n", encoding="utf-8")
        (evil / "result.md").write_text("结果：完成。SECRET-FROM-OUTSIDE", encoding="utf-8")
        self.bridge_run(EM2, evil, agent="dsh", head="结果：完成。头一段", title="外面的活")
        _, body = self.employee(EM2)
        blob = json.dumps(body, ensure_ascii=False)
        self.assertNotIn("SECRET-FROM-OUTSIDE", blob)
        self.assertEqual(body["timeline"][0]["steps"], [])
        self.assertEqual(body["timeline"][0]["result"]["summary"], "完成。头一段。")        # 只剩账本里记的那一段

    def test_symlinked_run_files_pointing_outside_are_not_followed(self):
        run = self.make_run("20261030-140000-dsh-link", result=None, prompt=None)
        outside = self.tmp / "outside-secret.md"
        outside.write_text("结果：完成。SECRET-VIA-LINK", encoding="utf-8")
        os.symlink(outside, run / "result.md")
        self.bridge_run(EM2, run, agent="dsh", head="")
        _, body = self.employee(EM2)
        self.assertNotIn("SECRET-VIA-LINK", json.dumps(body, ensure_ascii=False))

    def test_missing_run_dir_still_gives_a_card(self):
        self.bridge_run(EM2, self.tmp / "gone" / ".xay" / "runs" / "nope", agent="dsh", head="结果：完成。旧目录已经被删了")
        _, body = self.employee(EM2)
        card = body["timeline"][0]
        self.assertEqual((card["steps"], card["files"]), ([], []))
        self.assertEqual(card["result"]["summary"], "完成。旧目录已经被删了。")

    def test_secrets_in_old_logs_are_redacted_in_detail(self):
        run = self.make_run("20261030-150000-codex-sec", result="结果：完成。\n用了 sk-abcdefghijklmnop1234 和 API_KEY=hunter2")
        self.bridge_run(EM1, run)
        _, body = self.employee(EM1)
        blob = json.dumps(body, ensure_ascii=False)
        self.assertNotIn("sk-abcdefghijklmnop1234", blob)
        self.assertNotIn("hunter2", blob)

    def test_person_without_work_has_empty_timeline(self):
        status, body = self.employee(EM2)
        self.assertEqual((status, body["timeline"], body["total"]), (200, [], 0))

    def test_fired_employee_is_still_readable(self):
        self.A("employee.fired", LEAD, {"reason": "test"}, EM2, T0 + 300, conv=None)
        status, body = self.employee(EM2)
        self.assertEqual(status, 200)
        self.assertTrue(body["person"]["fired"])

    def test_memory_fields(self):
        # 1. 初始状态：无 notes，无 context.compacted
        status, body = self.employee(EM2)
        self.assertEqual(status, 200)
        mem = body.get("memory")
        self.assertIsNotNone(mem)
        self.assertEqual(mem["notes"], "")
        self.assertIsNone(mem["notes_ts"])
        self.assertIsNone(mem["notes_by"])
        self.assertIsNone(mem["handoff"])
        self.assertEqual(mem["compactions"], [])

        # 2. 工位写笔记
        self.A("notes.updated", "station:" + EM2, {"active": "工位整理的笔记"}, EM2, T0 + 50)
        _, body = self.employee(EM2)
        mem = body["memory"]
        self.assertEqual(mem["notes"], "工位整理的笔记")
        self.assertEqual(mem["notes_ts"], T0 + 50)
        self.assertEqual(mem["notes_by"], "工位")

        # 3. 负责人改笔记
        self.A("notes.updated", LEAD, {"active": "负责人改的笔记"}, EM2, T0 + 60)
        _, body = self.employee(EM2)
        self.assertEqual(body["memory"]["notes"], "负责人改的笔记")
        self.assertEqual(body["memory"]["notes_by"], "负责人")

        # 4. 用户改笔记
        self.A("notes.updated", "user", {"active": "用户改的笔记"}, EM2, T0 + 70)
        _, body = self.employee(EM2)
        self.assertEqual(body["memory"]["notes"], "用户改的笔记")
        self.assertEqual(body["memory"]["notes_by"], "用户")

        # 5. 员工改笔记
        self.A("notes.updated", "employee:" + EM2, {"active": "员工自己写的笔记"}, EM2, T0 + 80)
        _, body = self.employee(EM2)
        self.assertEqual(body["memory"]["notes"], "员工自己写的笔记")
        self.assertEqual(body["memory"]["notes_by"], "员工")

        # 6. context.compacted method=native（非 handoff/machine，handoff 应为 None）
        self.A("context.compacted", "station:" + EM2, {"method": "native", "before_pct": 80, "after_pct": 30}, EM2, T0 + 90)
        _, body = self.employee(EM2)
        mem = body["memory"]
        self.assertIsNone(mem["handoff"])
        self.assertEqual(len(mem["compactions"]), 1)
        self.assertEqual(mem["compactions"][0], {
            "n": 1, "ts": T0 + 90, "method": "native", "before_pct": 80, "after_pct": 30
        })

        # 7. 写交接笔记，然后 context.compacted method=handoff
        self.A("notes.updated", "station:" + EM2, {"active": "这是交接摘要全文"}, EM2, T0 + 100)
        self.A("context.compacted", "station:" + EM2, {"method": "handoff", "before_pct": 85, "after_pct": 20}, EM2, T0 + 105)
        _, body = self.employee(EM2)
        mem = body["memory"]
        self.assertEqual(mem["handoff"], "这是交接摘要全文")
        self.assertEqual(len(mem["compactions"]), 2)
        self.assertEqual(mem["compactions"][1]["n"], 2)
        self.assertEqual(mem["compactions"][1]["method"], "handoff")

        # 8. context.compacted method=machine
        self.A("context.compacted", "station:" + EM2, {"method": "machine", "before_pct": 90, "after_pct": 15}, EM2, T0 + 110)
        _, body = self.employee(EM2)
        mem = body["memory"]
        self.assertEqual(mem["handoff"], "这是交接摘要全文")
        self.assertEqual(len(mem["compactions"]), 3)
        self.assertEqual(mem["compactions"][2]["n"], 3)

    # ---- 检查 ----
    def test_unknown_or_bad_employee(self):
        status, body = self.get_json("/api/v2/employee?em=em_nope0000&conv=" + CV)
        self.assertEqual(status, 404)
        self.assertIn("找不到", body["error"])
        for q in ("", "?em=", "?em=../x", "?em=a/b", "?em=" + "a" * 80):
            status, _, _ = self.req("GET", "/api/v2/employee" + q)
            self.assertEqual(status, 422, q)

    def test_host_check_and_methods(self):
        path = "/api/v2/employee?em=%s&conv=%s" % (EM1, CV)
        for host in ("evil.example.com", "127.0.0.1:1", "127.0.0.1.evil.com:%d" % self.port):
            status, _, _ = self.req("GET", path, host=host)
            self.assertEqual(status, 403, host)
        status, _, _ = self.req("GET", path, host="localhost:%d" % self.port)
        self.assertEqual(status, 200)
        status, _, _ = self.post("/api/v2/employee", {"em": EM1}, "control")
        self.assertIn(status, (404, 405))
        status, headers, _ = self.req("GET", path)
        self.assertNotIn("access-control-allow-origin", headers)
        self.assertEqual(headers["cache-control"], "no-store")

    def test_default_conversation_when_conv_is_omitted(self):
        status, body = self.get_json("/api/v2/employee?em=" + EM1)
        self.assertEqual((status, body["conv"], len(body["timeline"])), (200, CV, 1))

    # ---- 直播条 ----
    def test_live_plain_frames(self):
        d = self.tmp / "v2" / "live" / EM2
        d.mkdir(parents=True, exist_ok=True)
        rows = [{"n": 1, "kind": "thinking", "text": "先看看", "ts": 1}, {"n": 2, "kind": "thinking", "text": "配置文件。再改页面", "ts": 2},
                {"n": 3, "kind": "tool", "text": "python3 -m unittest x", "ts": 3, "tool_kind": "cmd"}, {"n": 4, "kind": "status", "text": "working", "ts": 4}]
        (d / "1.jsonl").write_text("\n".join(json.dumps(r, ensure_ascii=False) for r in rows) + "\n", encoding="utf-8")
        frames = self.sse("/api/v2/live?em=%s&plain=1" % EM2, until=lambda f: f[-1]["id"] >= 4)
        live = [f["data"] for f in frames if f["event"] == "live"]
        self.assertEqual([x.get("plain") for x in live], ["先看看", "再改页面", "跑自动测试", None])
        self.assertEqual({k: live[2][k] for k in ("n", "kind", "text", "ts")}, {"n": 3, "kind": "tool", "text": "python3 -m unittest x", "ts": 3})
        plain_off = self.sse("/api/v2/live?em=%s" % EM2, until=lambda f: f[-1]["id"] >= 4)
        self.assertTrue(all("plain" not in f["data"] for f in plain_off if f["event"] == "live"))

    # ---- 快照附加的最近动静 ----
    def test_snapshot_activity_for_tasks_and_departments(self):
        run = self.make_run("20261030-160000-codex-act")
        self.bridge_run(EM1, run, dept="fe")
        run2 = self.make_run("20261030-160100-dsh-live")
        self.bridge_run(EM2, run2, status="running", agent="dsh", dept="old_dept", title="还在跑")
        status, snap = self.get_json("/api/v2/snapshot?conv=" + CV)
        self.assertEqual(status, 200)
        act = snap["activity"]
        self.assertEqual(act["tasks"][TK], T0 + 112)                                       # V2 的活：最近一条事件
        self.assertEqual(act["tasks"]["run:20261030-160000-codex-act"], T0 + 260)          # 旧的活：它自己的结束时间，不是入账时间
        self.assertGreater(act["tasks"]["run:20261030-160100-dsh-live"], time.time() - 60)  # 还在跑的算现在
        self.assertEqual(act["depts"]["fe"], T0 + 260)
        self.assertIn("old_dept", act["depts"])
        direct = core_snapshot.build(self.conn, conv=CV, now=snap["now"])
        self.assertEqual(snap["tasks"], direct["tasks"])                                    # 原有字段一个不动
        self.assertEqual(snap["depts"], direct["depts"])

    # ---- 示例模式 ----
    def test_demo_mode_serves_employee_without_live_or_old_runs(self):
        deadline = time.time() + 15
        em = None
        while time.time() < deadline and not em:
            _, snap = self.get_json("/api/v2/snapshot?demo=1")
            em = next(iter(snap.get("people") or {}), None)
            if not em:
                time.sleep(0.1)
        self.assertTrue(em, "示例账本没有员工")
        status, body = self.get_json("/api/v2/employee?demo=1&em=" + em)
        self.assertEqual(status, 200)
        self.assertEqual(body["person"]["key"], em)


class PageShellTests(W.OfficeCase):
    """页面外壳：原版看板入口、左侧面板的位置、整身立绘路径、给附加效果留的稳定标记。"""
    seed = False

    def test_org_html_has_board_link_and_left_panel(self):
        _, _, data = self.req("GET", "/org")
        html = data.decode("utf-8")
        self.assertIn('<a href="/" id="back-link"', html)                     # "看板"按钮（M5）：跳到 V2 看板
        self.assertNotIn("127.0.0.1:7878", html)
        self.assertIn('id="emp-panel"', html)
        self.assertIn('id="emp-close-btn"', html)
        self.assertLess(html.index('id="emp-panel"'), html.index('id="office-viewport"'))      # 在办公室左边
        self.assertIn("/web/desk-fx.css", html)                                                # 别人加的两行还在
        self.assertIn("/web/lib/desk-fx.js", html)

    def test_panel_script_uses_full_body_image_with_calm_fallback(self):
        _, _, data = self.req("GET", "/web/lib/employee-panel.js")
        js = data.decode("utf-8")
        self.assertIn("-full.webp", js)
        self.assertIn("-calm.webp", js)
        self.assertIn("/api/v2/employee", self.req("GET", "/web/lib/api.js")[2].decode("utf-8"))
        status, _, _ = self.req("GET", "/xay-chars/img/codex-full.webp")
        self.assertIn(status, (200, 404))                                                      # 整身图由别人出；没有也不会出错

    def test_station_card_keeps_stable_markers_for_add_ons(self):
        _, _, data = self.req("GET", "/web/lib/station.js")
        js = data.decode("utf-8")
        for marker in ('class="station-card"', 'id="station-${', 'data-worker-key="', 'data-dept="', 'data-em="', 'data-agent="',
                       'data-state="', 'data-task-title="'):
            self.assertIn(marker, js)
        _, _, app = self.req("GET", "/web/lib/app.js")
        self.assertIn("xay:rendered", app.decode("utf-8"))


# ---------------------------------------------------------------- 部门前缀（核心大白话）

class DepartmentPrefixTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="xay-emp-test-"))
        self.addCleanup(shutil.rmtree, self.tmp, True)
        env = mock.patch.dict(os.environ, {"XAY2_HOME": str(self.tmp / "v2")})
        env.start()
        self.addCleanup(env.stop)
        self.conn = core_db.open_db(str(self.tmp / "v2" / "xay.db"))
        self.addCleanup(self.conn.close)
        self.seed()

    def A(self, typ, by, data, subject=None, ts=None):
        return core_events.append(self.conn, typ, by, data, ws=WS, conv=CV, subject=subject, ts=ts)

    def seed(self):
        A = self.A
        A("conv.begun", LEAD, {"title": "t", "lead_kind": "claude", "aliases": [], "cwd": "/tmp"}, CV, T0)
        A("dept.opened", LEAD, {"name": "页面部", "mission": "m", "owns": [], "head": "rules"}, "fe", T0 + 1)
        A("employee.hired", LEAD, {"agent": "codex"}, EM1, T0 + 2)
        A("employee.hired", LEAD, {"agent": "dsh"}, EM2, T0 + 2)
        A("employee.joined", LEAD, {"dept": "fe"}, EM1, T0 + 3)
        for tk, owner, dept in (("tk_p0000001", EM1, "fe"), ("tk_p0000002", EM2, None), ("tk_p0000003", EM1, None)):
            A("task.added", LEAD, {"title": "做" + tk[-1], "dept": dept, "owner": owner, "deps": [], "writes": [], "card": "x", "mode": "write", "reason": "r"}, tk, T0 + 10)

    def texts(self):
        return [r["text"] for r in core_snapshot.build(self.conn, conv=CV)["recent"]]

    def test_finished_texts_carry_the_department_name(self):
        self.A("task.landed", "station:" + EM1, {"files": [], "add": 0, "del": 0}, "tk_p0000001", T0 + 20)
        self.A("task.accepted", LEAD, {}, "tk_p0000001", T0 + 21)
        texts = self.texts()
        self.assertIn("页面部 · Codex 1 做完了「做1」，改动已合并", texts)
        self.assertIn("页面部 · 负责人验收通过了「做1」", texts)

    def test_no_department_means_no_prefix_and_no_stray_space(self):
        self.A("task.landed", "office", {"files": [], "add": 0, "del": 0}, "tk_p0000002", T0 + 20)
        self.assertIn("DeepSeek 1 做完了「做2」，改动已合并", self.texts())

    def test_person_department_is_used_when_the_task_has_none(self):
        self.A("task.landed", "office", {"files": [], "add": 0, "del": 0}, "tk_p0000003", T0 + 20)
        self.assertIn("页面部 · Codex 1 做完了「做3」，改动已合并", self.texts())

    def test_legacy_run_end_texts_and_start_texts(self):
        run = {"run_dir": "/old/.xay/runs/r-1", "agent": "codex", "title": "修登录", "started": T0 + 30, "dept": "fe"}
        self.A("bridge.run", "bridge", dict(run, status="running"), EM1, T0 + 30)
        self.A("bridge.run", "bridge", dict(run, status="done", ended=T0 + 40), EM1, T0 + 40)
        run2 = {"run_dir": "/old/.xay/runs/r-2", "agent": "dsh", "title": "查日志", "started": T0 + 31, "ended": T0 + 41, "dept": "unknown_old_dept"}
        self.A("bridge.run", "bridge", dict(run2, status="failed"), EM2, T0 + 41)
        texts = self.texts()
        self.assertIn("（旧系统）页面部 · Codex 1 开始做「修登录」", texts)
        self.assertIn("（旧系统）页面部 · Codex 1 做完了「修登录」", texts)
        self.assertIn("（旧系统）DeepSeek 1 没做成「查日志」", texts)             # 认不出的旧部门编号不显示

    def test_bridge_group_events_are_silent_and_joined_uses_the_department_name(self):
        for typ in ("dept.opened", "dept.closed", "employee.joined", "employee.hired"):
            self.assertIsNone(core_humanize.text({"type": typ, "by": "bridge", "subject": "x", "conv": CV, "data": {"name": "项目组", "dept": "fe"}}, state={}), typ)
        self.A("employee.hired", "bridge", {"agent": "agy", "note": "旧系统"}, "em_dd000001", T0 + 50)
        self.A("employee.joined", "bridge", {"dept": "fe"}, "em_dd000001", T0 + 51)
        self.A("employee.joined", LEAD, {"dept": "ghost_dept"}, EM2, T0 + 52)
        texts = self.texts()
        self.assertIn("Codex 1 加入了页面部", texts)                      # 部门名，不是内部编号 fe
        self.assertFalse([x for x in texts if "ghost_dept" in x or "Gemini" in x or "加入团队" in x and "Gemini" in x], texts)
        self.assertFalse([x for x in texts if x.endswith("加入了fe") or "加入了ghost" in x])

    def test_progress_and_start_texts_name_the_employee_and_task(self):
        self.A("turn.started", "station:" + EM1, {"task": "tk_p0000001", "em": EM1, "kind": "first"}, "tn_p0000001", T0 + 20)
        self.A("task.progress", "station:" + EM1, {"step": 1, "of": 2, "text": "开头"}, "tk_p0000001", T0 + 21)
        texts = self.texts()
        self.assertIn("Codex 1 开始做「做1」", texts)
        self.assertIn("Codex 1：第1步/共2步，开头", texts)


if __name__ == "__main__":
    unittest.main()
