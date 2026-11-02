"""公司页共用的计划模型（V2/web/lib/plan-model.js，2026-10-31；层级：大任务 → 中任务（部门 / 组）→ 小任务 → 步骤（AI 自己拆））：

用户："画布和任务关联度以及任务拆解、计划版本之间存在很大的不同……看上去根本不是一个任务，我希望的是把当前所有的任务都要进行颗粒度对齐。"
- 同一份快照（+ 任务树）整理成：总任务 → 小任务（编号 1 / 2-A / 2-B / 零散、颜色、状态、验收说法）→ 部门（可选）→ AI 的活 → 步骤；
- 公司页房间（render-office.js roomPlan）、任务画布（task-canvas.js）、任务拆解 / 计划版本（render-plan.js）都从这份取，编号、名字、颜色一模一样；
- 计划版本里每条改动挂上所属小任务，小任务的增改翻成大白话；
- 详情抽屉（plan-detail.js）的内容：活的任务卡、验收命令、汇报（有大白话 plain 先放大白话，原文折进"技术细节"）、文件、时间、为什么交给他；
- 在岗时间的说法（utils.dutyText）。
装了 node 才跑。
"""
import json
import shutil
import subprocess
import unittest
from pathlib import Path

WEB = Path(__file__).resolve().parents[1] / "web"


def _snap():
    T = lambda id, title, part, owner, status, dept=None, deps=(), key=None, **kw: dict(
        id=id, key=key, title=title, part=part, owner=owner, status=status, dept=dept, deps=list(deps), writes=[], progress=kw.pop("progress", {}),
        result=kw.pop("result", ""), assigned_reason=kw.pop("why", ""), check=kw.pop("check", {"state": "none", "round": 0, "text": "", "by": ""}), **kw)
    P = lambda key, label, agent, dept=None: {"key": key, "label": label, "agent": agent, "state": "idle", "display_state": "idle", "queue": [],
                                              "dept": dept, "duty": {"online_since": 1793300000 - 7800, "today_work_s": 2880, "total_work_s": 9000}}
    return {
        "now": 1793300000, "conversation": {"id": "cv_x", "title": "做一个商品详情页（演示）"},
        "people": {"em_c1": P("em_c1", "Codex 1", "codex", "fe"), "em_d1": P("em_d1", "DeepSeek 1", "dsh"), "em_g1": P("em_g1", "Gemini 1", "agy", "be")},
        "former": [{"key": "em_old", "label": "Codex 9", "agent": "codex"}],
        "depts": [{"id": "fe", "name": "前端组", "members": ["em_c1"], "head": "rules", "counts": {}, "part": "pt_2"},
                  {"id": "be", "name": "后端组", "members": ["em_g1"], "head": "rules", "counts": {}, "part": "pt_2"}],
        "parts": [
            {"id": "pt_1", "key": "p1", "title": "定需求", "col": 0, "row": 0, "after": [], "status": "done", "check": {"kind": "rules"}, "depts": []},
            {"id": "pt_2", "key": "p2", "title": "做页面和接口", "col": 1, "row": 0, "after": ["pt_1"], "status": "working",
             "check": {"kind": "ai", "reviewer": "codex", "text": "接口对得上"}, "depts": ["fe", "be"]},
            {"id": "pt_3", "key": "p3", "title": "写说明", "col": 1, "row": 1, "after": ["pt_1"], "status": "working",
             "check": {"kind": "lead", "text": "大白话"}, "depts": []},
            {"id": "pt_4", "key": "p4", "title": "上线", "col": 2, "row": 0, "after": ["pt_2", "pt_3"], "status": "planned",
             "check": {"kind": "rules", "commands": ["npm test"]}, "depts": []},
            {"id": "", "title": "零散的活", "col": 3, "row": 0, "after": [], "status": "ready", "check": {"kind": "rules"}, "depts": []}],
        "tasks": [
            T("tk_need", "列清单。汇报时写清楚", "pt_1", "em_c1", "done", key="need", result="清单写在 docs/a.md", why="他熟"),
            T("tk_frame", "搭框架", "pt_2", "em_c1", "working", dept="fe", deps=["need"], key="frame", progress={"step": 2, "of": 4, "text": "【进度 2/4】正在放商品图"},
              planned=["1. 建页面文件", "2. 放商品图和价格", "3. 加购买按钮", "4. 手机上看一遍"]),
            T("tk_api1", "写接口", "pt_2", "em_old", "failed", dept="be", key="api1"),
            T("tk_api2", "写接口", "p2", "em_g1", "done", dept="be", key="api2", check={"state": "passed", "round": 1, "text": "", "by": "Codex 2"},
              plain="商品接口做好了，页面能拿到价格和库存", result="改了 server/products.py 的 get_product()，补了 tests/test_products.py"),
            T("tk_doc", "写安装说明", "pt_3", "em_d1", "queued", deps=["frame"], key="doc"),
            T("tk_ads", "写文案", "pt_3", "em_d1", "canceled", key="ads"),
            T("tk_e2e", "跑整体测试", "pt_4", None, "planned", deps=["api2", "tk_doc"], key="e2e"),
            T("tk_misc", "顺手修个错字", None, "em_d1", "done", key="misc")],
        "plan": {"version": 6, "history": [
            {"version": 1, "by": "lead:cv_x", "ts": 1, "reason": "开工", "changes": [
                {"op": "part_add", "part": "pt_1", "title": "定需求"}, {"op": "part_add", "part": "pt_3", "title": "写说明"},
                {"op": "add", "task": "tk_need", "title": "列清单。汇报时写清楚", "after": {"title": "列清单", "owner": "em_c1", "part": "p1"}}]},
            {"version": 2, "by": "gm", "ts": 2, "reason": "改先后", "changes": [
                {"op": "part_change", "part": "pt_4", "title": "上线", "before": {"after": ["pt_2"]}, "after": {"after": ["pt_2", "p3"]}}]},
            {"version": 3, "by": "gm", "ts": 3, "reason": "说明要总经理看", "changes": [
                {"op": "part_change", "part": "pt_3", "title": "写说明", "before": {"check": {"kind": "rules"}}, "after": {"check": {"kind": "lead"}}},
                {"op": "part_change", "part": "pt_2", "title": "做页面和接口", "before": {"title": "做页面"}, "after": {"title": "做页面和接口"}}]},
            {"version": 4, "by": "gm", "ts": 4, "reason": "不做文案", "changes": [{"op": "remove", "task": "tk_ads", "title": "写文案"}]},
            {"version": 5, "by": "gm", "ts": 5, "reason": "不调研了", "changes": [{"op": "part_remove", "part": "pt_9", "title": "竞品调研"}]},
            {"version": 6, "by": "gm", "ts": 6, "reason": "换人", "changes": [
                {"op": "assign", "task": "tk_e2e", "title": "跑整体测试", "before": {"owner": None}, "after": {"owner": "em_g1"}}],
             "change_texts": ["把「跑整体测试」交给 Gemini 1"]}]}}


@unittest.skipUnless(shutil.which("node"), "没装 node，跳过计划模型的检查")
class PlanModelTests(unittest.TestCase):
    def node(self, body, snap=None, tree=None):
        script = ("import * as M from './plan-model.js';\nimport { roomPlan } from './render-office.js';\nimport { canvasModel } from './task-canvas.js';\n"
                  "import * as U from './utils.js';\nconst snap = %s, tree = %s;\n%s") % (
            json.dumps(snap if snap is not None else _snap(), ensure_ascii=False), json.dumps(tree, ensure_ascii=False), body)
        res = subprocess.run(["node", "--input-type=module", "-e", script], capture_output=True, text=True, timeout=30, cwd=str(WEB / "lib"))
        self.assertEqual(res.returncode, 0, res.stderr)
        return json.loads(res.stdout)

    def test_parts_items_and_numbering(self):
        out = self.node("""
const m = M.buildPlan(snap, tree);
console.log(JSON.stringify({
  parts: m.parts.map(p => ({id: p.id, code: p.code, n: p.n, title: p.title, color: p.color, word: p.word, tone: p.tone, check: p.check.word,
    after: p.after, depts: p.depts.map(d => [d.name, d.items.map(i => i.id)]), items: p.items.map(i => i.id), all: p.all, done: p.done})),
  items: m.items.map(i => ({id: i.id, ids: i.ids, title: i.title, name: i.name, word: i.word, sub: i.sub, part: i.part, dept: i.dept, deps: i.deps,
    left: i.left, tries: i.tries, step: i.step, of: i.of, steps: i.steps, tag: i.deptTag, plain: i.plain, selfSplit: i.selfSplit})),
  apiHtml: M.itemDetailHtml(m.itemOf('tk_api2'), m),
  counts: m.counts, steps: m.steps, ofAttempt: m.itemOf('tk_api1').id, colors: M.PART_COLORS}));
""")
        parts = {p["id"]: p for p in out["parts"]}
        self.assertEqual([p["code"] for p in out["parts"]], ["1", "2-A", "2-B", "3", "零散"])            # 第几步；同一步能同时做的 2-A / 2-B
        self.assertEqual([p["color"] for p in out["parts"][:4]], out["colors"][:4])                       # 颜色按先后轮着取
        self.assertEqual(parts[""]["color"], "var(--label-3)")                                          # 零散的活是灰的
        self.assertEqual([p["check"] for p in out["parts"]], ["自动检查", "Codex 审查", "负责人亲自审", "按负责人的规则", "自动检查"])
        self.assertEqual([p["word"] for p in out["parts"]], ["做完了", "正在做", "正在做", "还不能开工", "可以开工"])
        self.assertEqual(parts["pt_4"]["after"], ["pt_2", "pt_3"])
        self.assertEqual(parts["pt_2"]["depts"], [])                                                    # 中任务里不再分部门（部门就是中任务）
        self.assertEqual(parts["pt_2"]["items"], ["tk_frame", "tk_api2"])
        self.assertEqual(parts["pt_3"]["items"], ["tk_doc"])                                            # 撤回的活不算
        self.assertEqual(parts[""]["items"], ["tk_misc"])                                               # 没写小任务的：零散的活
        self.assertEqual((parts["pt_2"]["all"], parts["pt_2"]["done"]), (2, 1))                          # 没做成又换人重做的合成一件
        items = {i["id"]: i for i in out["items"]}
        self.assertEqual(items["tk_api2"]["ids"], ["tk_api1", "tk_api2"])
        self.assertEqual(items["tk_api2"]["tries"], 2)
        self.assertEqual(out["ofAttempt"], "tk_api2")                                                   # 任何一次的编号都找得到这件活
        self.assertEqual(items["tk_api2"]["part"], "pt_2")                                              # part 写的是简称 p2 也认得
        self.assertEqual(items["tk_need"]["title"], "列清单")                                           # 名字用短标题（四处一样）
        self.assertEqual(items["tk_frame"]["deps"], ["tk_need"])                                        # 依赖写的是任务简称也认得
        self.assertEqual(items["tk_e2e"]["deps"], ["tk_api2", "tk_doc"])
        self.assertEqual((items["tk_frame"]["sub"], items["tk_frame"]["step"], items["tk_frame"]["of"]), ("正在做 · 第 2/4 步", 2, 4))
        self.assertEqual([s["state"] for s in items["tk_frame"]["steps"]], ["done", "cur", "todo", "todo"])  # 没有任务树：按进度只写编号
        self.assertEqual([s["text"] for s in items["tk_frame"]["steps"]], ["建页面文件", "放商品图和价格", "加购买按钮", "手机上看一遍"])  # AI 自己拆的步骤
        self.assertTrue(items["tk_frame"]["selfSplit"])
        self.assertFalse(items["tk_need"]["selfSplit"])
        self.assertEqual((items["tk_frame"]["tag"], items["tk_api2"]["tag"]), ("前端组", "后端组"))     # 旧数据的部门：小任务上的小标签
        self.assertEqual(items["tk_api2"]["plain"], "商品接口做好了，页面能拿到价格和库存")
        api = out["apiHtml"]
        self.assertIn("做成了什么", api)                                                                # 先放大白话
        self.assertLess(api.index("商品接口做好了"), api.index("技术细节（员工汇报原文）"))               # 原文折进"技术细节"
        self.assertIn("server/products.py", api)
        self.assertEqual((items["tk_e2e"]["name"], items["tk_e2e"]["word"]), ("", "还没开始"))
        self.assertEqual([s["n"] for s in out["steps"]], [1, 2, 3])
        self.assertEqual(out["counts"]["all"], 6)

    def test_four_places_share_numbers_names_and_colors(self):
        """公司页房间、画布、任务拆解（同一个模型）：每个小任务的编号、名字、颜色、验收说法一模一样。"""
        out = self.node("""
const m = M.buildPlan(snap, tree);
const rooms = roomPlan(snap, m).rooms.map(r => [r.part, r.code, r.title, r.color, r.checkInfo.word, r.all, r.done]);
const canvas = canvasModel(m).parts.map(cp => [cp.p.id, cp.p.code, cp.p.title, cp.p.color, cp.p.check.word, cp.p.all, cp.p.done]);
const plan = m.parts.map(p => [p.id, p.code, p.title, p.color, p.check.word, p.all, p.done]);
const chip = M.partChipHtml(m.partById.get('pt_2'));
console.log(JSON.stringify({rooms, canvas, plan, chip, roomsAlone: roomPlan(snap).rooms.map(r => [r.part, r.code, r.color])}));
""")
        self.assertEqual(out["rooms"], out["plan"])
        self.assertEqual(out["canvas"], out["plan"])
        self.assertEqual(out["roomsAlone"], [r[:2] + [r[3]] for r in out["plan"]])                     # 不传模型也算得一样
        self.assertIn("2-A", out["chip"])
        self.assertIn("做页面和接口", out["chip"])
        self.assertIn("--pc:", out["chip"])

    def test_history_hangs_each_change_on_its_part(self):
        out = self.node("""
const m = M.buildPlan(snap, tree);
console.log(JSON.stringify(m.history.map(v => ({v: v.version, parts: v.parts, items: v.items, gone: v.gone,
  changes: v.changes.map(c => [c.op, c.part, c.text])}))));
""")
        v = {x["v"]: x for x in out}
        self.assertEqual(v[1]["changes"], [
            ["part_add", "pt_1", "新开中任务「定需求」，第 1 步，自动检查"],
            ["part_add", "pt_3", "新开中任务「写说明」，第 2 步（2-B），负责人亲自审"],
            ["add", "pt_1", "在「定需求」里加了「列清单」，交给 Codex 1"]])
        self.assertEqual(v[1]["items"], ["tk_need"])
        self.assertEqual(v[2]["changes"], [["part_change", "pt_4", "「上线」改了先后：排在「做页面和接口」（2-A）、「写说明」（2-B）后面"]])
        self.assertEqual([c[2] for c in v[3]["changes"]], ["「写说明」的验收方式改成负责人亲自审", "「做页面」改名为「做页面和接口」"])
        self.assertEqual(v[3]["parts"], ["pt_3", "pt_2"])
        self.assertEqual(v[4]["changes"], [["remove", "pt_3", "从「写说明」里撤回了「写文案」"]])         # 撤回的活：挂在它原来的小任务上
        self.assertEqual((v[4]["items"], v[4]["gone"]), ([], ["写文案"]))
        self.assertEqual(v[5]["changes"], [["part_remove", None, "撤掉中任务「竞品调研」"]])
        self.assertEqual(v[5]["gone"], ["竞品调研"])
        self.assertEqual(v[6]["changes"], [["assign", "pt_4", "把「跑整体测试」交给 Gemini 1"]])
        self.assertEqual((v[6]["parts"], v[6]["items"]), (["pt_4"], ["tk_e2e"]))                       # "在画布上看"高亮这些

    def test_tree_adds_steps_details_and_deps(self):
        tree = {"title": "x", "parts": [{"id": "pt_2", "title": "做页面和接口", "col": 1, "row": 0, "status": "working", "check": {"kind": "rules"},
                                         "detail": {"note": "页面和接口一起交", "after_titles": ["定需求"], "started": 1793290000, "ended": None},
                                         "depts": [{"id": "fe", "name": "前端组", "items": [{
                                             "id": "tk_frame", "title": "搭框架", "status": "working", "owner": "em_c1", "owner_name": "Codex 1",
                                             "step": 2, "of": 3, "steps": [{"n": 1, "text": "建文件", "state": "done"}, {"n": 2, "text": "放图", "state": "cur"},
                                                                           {"n": 3, "text": "收尾", "state": "todo"}],
                                             "deps": ["tk_misc"], "part": "pt_2",
                                             "detail": {"card": "在 shop/product.html 搭框架", "verify": ["npm test"], "summary": "改了 shop/a.js 的 render()",
                                                        "plain": "页面框架搭好了，能看到商品图和价格", "files": ["shop/a.js"], "started": 1793296000,
                                                        "ended": None, "work_s": 2460, "tries": 1, "assigned_reason": "他熟前端", "check": {"state": "none"}}}]}],
                                         "items": []}]}
        out = self.node("""
const m = M.buildPlan(snap, tree);
const it = m.itemOf('tk_frame');
const p = m.partById.get('pt_2');
console.log(JSON.stringify({steps: it.steps, of: it.of, deps: it.deps, detail: it.detail, html: M.itemDetailHtml(it, m), part: M.partDetailHtml(p, m),
  rows: M.stepsHtml(it), partNote: p.detail.note}));
""", tree=tree)
        self.assertEqual([s["text"] for s in out["steps"]], ["建文件", "放图", "收尾"])                    # 步骤名字来自任务树
        self.assertEqual(out["of"], 3)
        self.assertEqual(out["deps"], ["tk_need", "tk_misc"])                                          # 快照的依赖 + 任务树的依赖
        html = out["html"]
        for need in ("搭框架", "2-A", "在 shop/product.html 搭框架", "npm test", "页面框架搭好了", "技术细节", "改了 shop/a.js",
                     "合并回项目的文件", "shop/a.js", "41 分", "他熟前端", "他自己拆的步骤", "放图", "要先等这几件做完", "在画布上看这件小任务",
                     "做成了什么", "前端组"):
            self.assertIn(need, html)
        self.assertLess(html.index("页面框架搭好了"), html.index("改了 shop/a.js"))                      # 先大白话，技术原文折在后面
        part = out["part"]
        for need in ("做页面和接口", "页面和接口一起交", "排在这些后面", "定需求", "后面接着", "上线", "前端组", "在画布上看这个中任务"):
            self.assertIn(need, part)
        self.assertIn("st-cur", out["rows"])

    def test_old_parts_and_duty_words(self):
        old = dict(_snap(), parts=[{"id": "dept:fe", "title": "前端组", "col": 0, "row": 0, "after": [], "status": "working", "check": {"kind": "rules"}, "depts": ["fe"]},
                                   {"id": "", "title": "零散的活", "col": 1, "row": 0, "after": [], "status": "", "check": {"kind": "rules"}, "depts": []}])
        out = self.node("""
const m = M.buildPlan(snap, tree);
const now = 1793300000;
console.log(JSON.stringify({parts: m.parts.map(p => [p.id, p.code, p.title, p.depts.length, p.all, p.word]), old: m.old,
  duty: [U.dutyText({online_since: now - 7800, today_work_s: 2880}, now), U.dutyText({online_since: null, today_work_s: 0}, now),
         U.dutyText(null, now), U.spanText(30), U.spanText(3 * 86400 + 7200)],
  thoughts: ['I need to check the config loader first', '看一下 render() 和 shop/a.js', '先把价格放上去'].map(t => U.thoughtWords(t)),
  techy: ['页面框架搭好了，能看到商品图和价格', '改了 shop/a.js 的 render()，补了 test_products.py'].map(U.isTechy)}));
""", snap=old)
        self.assertEqual(out["parts"], [["all", "1", "全部的小任务", 0, 6, "正在做"]])                       # 按任务拆，不按部门拆
        self.assertTrue(out["old"])
        self.assertEqual(out["duty"], ["在岗 2 小时 10 分 · 今天干活 48 分", "已下班 · 今天还没干活", "", "不到 1 分", "3 天 2 小时"])
        self.assertEqual([t["text"] for t in out["thoughts"]], ["正在琢磨怎么做", "正在琢磨怎么做", "先把价格放上去"])  # 英文 / 像代码的"在想"换成大白话
        self.assertEqual(out["thoughts"][0]["raw"], "I need to check the config loader first")                          # 原文留着放悬停
        self.assertEqual(out["techy"], [False, True])


class PageWiringTests(unittest.TestCase):
    def test_four_places_use_the_shared_model(self):
        lib = WEB / "lib"
        src = {f: (lib / f).read_text(encoding="utf-8") for f in ("app.js", "render-plan.js", "render-office.js", "task-canvas.js", "plan-detail.js",
                                                                   "employee-panel.js", "plan-model.js")}
        self.assertIn("buildPlan(data", src["app.js"])
        self.assertIn("/api/v2/plan?", src["app.js"])                                                  # 整个对话的任务树
        self.assertIn("openPlanCanvas", src["app.js"])
        for f in ("render-plan.js", "render-office.js", "task-canvas.js"):
            self.assertIn("./plan-model.js", src[f])
            self.assertIn("partChipHtml(", src[f])                                                     # 同一个小任务标签
        self.assertIn("openDetail(", src["render-plan.js"])                                            # 任务拆解点一行 = 画布点一件活：同一个详情
        self.assertIn("openDetail(", src["task-canvas.js"])
        self.assertIn("data-plan-version", src["render-plan.js"])                                      # 计划版本"在画布上看"
        self.assertIn("dutyText(", src["render-office.js"])                                            # 工位卡片上的在岗时间
        self.assertIn("dutyText(", src["employee-panel.js"])
        for f in ("plan-model.js", "plan-detail.js"):
            for bad in ("fetch(", "localStorage", "postAction"):
                self.assertNotIn(bad, src[f])
        html = (WEB / "org.html").read_text(encoding="utf-8")
        self.assertIn('id="canvas-btn"', html)                                                         # 右上角"任务画布"按钮
        css = (WEB / "org.css").read_text(encoding="utf-8")
        for need in (".pm-chip", ".pm-ic", ".pd-drawer", ".pl-group", ".pl-change", ".station-duty"):
            self.assertIn(need, css)


    def test_page_words_follow_big_middle_small(self):
        """用户 2026-10-31 纠正层级：页面上一律"大任务 / 中任务 / 小任务 / 步骤"（中任务 = 部门 / 组，各有监工；中任务里不再分部门）。"""
        import re
        lib = WEB / "lib"
        files = ("plan-model.js", "task-canvas.js", "render-plan.js", "render-office.js", "plan-detail.js", "employee-panel.js", "token-meter.js",
                 "utils.js", "decisions.js", "app.js", "office-view.js")
        strings = {}
        for f in files:
            src = (lib / f).read_text(encoding="utf-8")
            code = "\n".join(line for line in src.splitlines() if not line.strip().startswith(("//", "*", "/*")))
            strings[f] = "".join(re.findall(r"'[^'\n]*'|`[^`]*`|\"[^\"\n]*\"", code))      # 只看给人看的字（字符串里）
        for f, text in strings.items():
            for bad in ("AI 的活", "这件活", "里面的活", "零散的活", "全部的活", "总任务", "总经理"):          # 主 AI 一律叫"负责人"（office-spec.md 二.10）
                self.assertNotIn(bad, text, "%s 里还写着「%s」" % (f, bad))
        self.assertIn("大任务", strings["task-canvas.js"])
        self.assertIn("中任务", strings["task-canvas.js"])
        self.assertIn("他自己拆的步骤", strings["plan-model.js"])                                     # 步骤是 AI 自己拆的
        self.assertIn("做成了什么", strings["plan-model.js"])                                          # 大白话 plain 先放
        self.assertIn("这件小任务花了", strings["token-meter.js"])                                     # token 新口径
        self.assertIn("每秒写出约", strings["token-meter.js"])
        self.assertNotIn("speed.per_s", (lib / "token-meter.js").read_text(encoding="utf-8"))           # 老口径 per_s 不再用
        for f in ("render-office.js", "employee-panel.js"):
            self.assertIn("thoughtWords(", (lib / f).read_text(encoding="utf-8"))                    # "在想什么"英文 / 像代码的换大白话
        self.assertIn("中任务", (WEB / "org.html").read_text(encoding="utf-8"))                       # 顶栏数中任务，不数部门


if __name__ == "__main__":
    unittest.main()
