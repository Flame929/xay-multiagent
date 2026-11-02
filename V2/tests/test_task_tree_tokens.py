"""详情栏补的两块（2026-10-31，references/plans/ui-v2-employee-panel.md「总任务拆解树、token」）：
① 总任务拆解树（/api/v2/employee 的 tree）：总任务 → 部门 → 一件件活（重试的合成一件、撤回的不列）→ 步骤，依赖排层级；
② token 消耗 / 速度（person.tokens）：这件活 / 今天对着预算，正在干活时最近 60 秒的速度、最近 30 分钟每分钟的方格；
③ 工位把这一轮累计用量写进直播文件（kind=usage），直播推送和步骤都看不到这种行；
④ 页面的两个小模块（task-canvas.js / token-meter.js）在 node 里画一遍：详情栏"总任务"块只剩一行概括 + "展开任务画布"按钮，
   画布照时间轴导图摆（2026-10-31 第二版：一个小任务一块、能同时做的同一列；部门胶囊可选；AI 的活是叶子，点开看步骤）。

全部用临时目录（XAY2_HOME / XAY_HOME），不碰真实的 ~/.xay。
"""
import json
import os
import shutil
import subprocess
import time
import unittest
from pathlib import Path

from tests.test_employee_panel_v3 import AGY, CODEX, CV, DSH, LEAD, NOW, T0, WEB, LedgerCase
from tests.test_station import Factory, StationCase, writes
from xay.office import employee
from xay.office.live import LiveTail
from xay.station import live as station_live


def st(em):
    return "station:" + em


class TreeCase(LedgerCase):
    def setUp(self):
        super().setUp()
        A = self.A
        A("dept.opened", LEAD, {"name": "页面部", "mission": "做页面", "owns": [], "head": "rules"}, "fe", T0)
        A("dept.opened", LEAD, {"name": "文档部", "mission": "写文档", "owns": [], "head": "rules"}, "docs", T0)
        A("employee.joined", LEAD, {"dept": "fe"}, CODEX, T0)
        A("employee.joined", LEAD, {"dept": "docs"}, AGY, T0)

    def add(self, tk, title, owner=None, dept=None, deps=(), steps=(), ts=None):
        self.A("task.added", LEAD, {"key": tk, "title": title, "owner": owner, "dept": dept, "deps": list(deps), "writes": [],
                                    "steps": list(steps), "mode": "write", "reason": "r"}, "tk_" + tk, ts or T0 + 10)

    def turn(self, em, tk, tn, ts, end=None, usage=None, status="completed"):
        self.A("turn.started", st(em), {"task": "tk_" + tk, "em": em, "kind": "first"}, tn, ts)
        if end is not None:
            data = {"em": em, "status": status, "evidence": {"cli_status": status, "changed_files": []}}
            if usage is not None:
                data["usage"] = {"in": usage, "out": 0, "cached": 0, "total": usage}
            self.A("turn.ended", st(em), data, tn, end)

    def done(self, tk, ts):
        self.A("task.landed", st(CODEX), {"files": ["a.py"], "add": 1, "del": 0}, "tk_" + tk, ts)

    def build(self, em, live=None):
        return employee.build(self.conn, em, conv=CV, live_root=live or self.tmp / "v2" / "live", now=NOW, quota_home=self.tmp / "v2")


class TaskTreeTests(TreeCase):
    def seed(self):
        A = self.A
        # 页面部：搭骨架（做完）→ 接着两件：写样式（Codex 正在做，第 2 步 / 共 3 步）、写脚本（DeepSeek 排队）
        self.add("frame", "搭页面骨架", CODEX, "fe", ts=T0 + 10)
        self.turn(CODEX, "frame", "tn_t1", T0 + 20, T0 + 60, usage=100_000)
        self.done("frame", T0 + 61)
        self.add("style", "写样式。汇报时第一句先说改了哪些文件", CODEX, "fe", ["frame"], ["定颜色", "排版", "暗色"], ts=T0 + 11)
        self.add("script", "写脚本", DSH, "fe", ["tk_frame"], ts=T0 + 12)
        A("task.queued", "office", {"owner": DSH, "position": 1}, "tk_script", T0 + 13)
        self.turn(CODEX, "style", "tn_t2", T0 + 100)
        A("task.progress", st(CODEX), {"step": 1, "of": 3, "text": "【进度 1/3】第1步：先定颜色", "source": "marker"}, "tk_style", T0 + 110)
        A("task.progress", st(CODEX), {"step": 2, "of": 3, "text": "第2步：排版，先排标题", "source": "marker"}, "tk_style", T0 + 120)
        # 文档部：写说明（Gemini，接在写样式后）；核对文字第一次没做成、又派了一次（合成一件）
        self.add("readme", "写说明", AGY, "docs", ["style"], ts=T0 + 14)
        self.add("check", "核对文字", AGY, "docs", ts=T0 + 15)
        self.turn(AGY, "check", "tn_t3", T0 + 30, T0 + 40, status="failed")
        A("task.failed", st(AGY), {"reason": "这一轮失败了", "detail": "at capacity"}, "tk_check", T0 + 40)
        self.add("check2", "核对文字", AGY, "docs", ts=T0 + 50)
        # 撤回的不列；没写部门的挂到负责人的部门；没人接的放"未分部门"
        self.add("drop", "先不做的事", CODEX, "fe", ts=T0 + 16)
        A("task.removed", LEAD, {"reason": "不做了"}, "tk_drop", T0 + 17)
        self.add("loose", "补一张图", CODEX, None, ts=T0 + 18)
        self.add("orphan", "以后再说的事", None, None, ts=T0 + 19)

    def test_tree_groups_merges_and_nests(self):
        self.seed()
        body = self.build(CODEX)
        tree = body["tree"]
        self.assertEqual(tree["title"], "三家员工")
        self.assertEqual([g["name"] for g in tree["groups"]], ["页面部", "文档部", "未分部门"])
        fe, docs, loose = tree["groups"]
        self.assertEqual([(i["title"], i["depth"], i["chained"]) for i in fe["items"]],
                         [("搭页面骨架", 0, False), ("写样式", 1, True), ("写脚本", 1, True), ("补一张图", 0, False)])
        self.assertTrue(fe["mine"])
        self.assertEqual(fe["owners"], ["Codex 1", "DeepSeek 1"])
        self.assertEqual((fe["count"]["all"], fe["count"]["done"], fe["count"]["working"], fe["count"]["queued"]), (4, 1, 1, 2))   # 排队的和还没派的都算排队
        style = fe["items"][1]
        self.assertEqual((style["status"], style["step"], style["of"], style["mine"]), ("working", 2, 3, True))
        self.assertEqual([(s["n"], s["text"], s["state"]) for s in style["steps"]],
                         [(1, "定颜色", "done"), (2, "排版", "cur"), (3, "暗色", "todo")])       # 任务卡写好的步骤名优先
        frame = fe["items"][0]
        self.assertEqual((frame["step"], frame["of"], frame["steps"]), (None, None, []))          # 没报过进度：不画步骤
        self.assertFalse(fe["items"][2]["mine"])
        # 文档部：重试的合成一件（试了 1 次——第二次还没开工），依赖别的部门的写在 after
        self.assertFalse(docs["mine"])
        self.assertEqual([(i["title"], i["status"]) for i in docs["items"]], [("写说明", "planned"), ("核对文字", "planned")])
        self.assertEqual(docs["items"][0]["after"], [{"id": "tk_style", "title": "写样式", "group": "页面部"}])
        self.assertEqual(docs["items"][1]["tries"], 1)
        self.assertEqual([(i["title"], i["owner_name"]) for i in loose["items"]], [("以后再说的事", "")])
        # 全部：撤回的不算；合成的算一件
        self.assertEqual(tree["count"]["all"], 7)
        self.assertNotIn("先不做的事", json.dumps(tree, ensure_ascii=False))
        # 树上标成"他的"件数 = 他的清单件数（同一条合并规则）
        for em in (CODEX, DSH, AGY):
            b = self.build(em)
            mine = sum(1 for g in b["tree"]["groups"] for i in g["items"] if i["mine"])
            self.assertEqual(mine, b["count"]["all"], em)

    def test_done_task_counts_every_step_and_stopped_step_is_marked(self):
        self.add("a", "写测试", CODEX, "fe")
        self.turn(CODEX, "a", "tn_a", T0 + 20)
        self.A("task.progress", st(CODEX), {"step": 2, "of": 4, "text": "跑一遍", "source": "marker"}, "tk_a", T0 + 30)
        self.A("turn.ended", st(CODEX), {"em": CODEX, "status": "completed", "evidence": {"cli_status": "completed", "changed_files": ["a.py"]}},
               "tn_a", T0 + 40)
        self.done("a", T0 + 41)
        item = self.build(CODEX)["tree"]["groups"][0]["items"][0]
        self.assertEqual([s["state"] for s in item["steps"]], ["done"] * 4)
        self.add("b", "写文档", CODEX, "fe", ts=T0 + 50)
        self.turn(CODEX, "b", "tn_b", T0 + 60)
        self.A("task.progress", st(CODEX), {"step": 3, "of": 5, "text": "写到一半", "source": "marker"}, "tk_b", T0 + 70)
        self.A("turn.ended", st(CODEX), {"em": CODEX, "status": "failed", "evidence": {"cli_status": "failed", "changed_files": []}}, "tn_b", T0 + 80)
        self.A("task.failed", st(CODEX), {"reason": "x", "detail": "y"}, "tk_b", T0 + 80)
        item = self.build(CODEX)["tree"]["groups"][0]["items"][1]
        self.assertEqual([s["state"] for s in item["steps"]], ["done", "done", "stop", "todo", "todo"])
        self.assertEqual(item["steps"][2]["text"], "写到一半")

    def test_no_tasks_gives_an_empty_tree(self):
        tree = self.build(CODEX)["tree"]
        self.assertEqual((tree["groups"], tree["count"]["all"]), ([], 0))


class TokenTests(TreeCase):
    def live_rows(self, em, rows):
        d = self.tmp / "v2" / "live" / em
        d.mkdir(parents=True, exist_ok=True)
        with open(d / "1.jsonl", "a", encoding="utf-8") as fh:
            for i, r in enumerate(rows):
                fh.write(json.dumps(dict({"n": i + 1}, **r), ensure_ascii=False) + "\n")

    def test_task_and_today_against_the_budget(self):
        today0 = time.mktime(time.localtime(NOW)[:3] + (0, 0, 0, 0, 0, -1))
        self.add("a", "写乘法", CODEX, "fe", ts=today0 - 7200)
        self.turn(CODEX, "a", "tn_y", today0 - 3600, today0 - 3500, usage=900_000)        # 昨天那一轮：算这件活，不算今天
        self.A("task.failed", st(CODEX), {"reason": "x", "detail": "y"}, "tk_a", today0 - 3500)
        self.add("a2", "写乘法", CODEX, "fe", ts=NOW - 600)
        self.turn(CODEX, "a2", "tn_z", NOW - 500, NOW - 400, usage=100_000)
        self.done("a2", NOW - 399)
        tok = self.build(CODEX)["person"]["tokens"]
        self.assertEqual(tok["task"], {"used": 1_000_000, "limit": 5_000_000, "pct": 20.0, "partial": False,
                                       "out": 0, "fresh": 1_000_000, "cached": 0, "spent": 1_000_000})   # 重试的几次加起来
        self.assertEqual(tok["today"], {"used": 100_000, "limit": 50_000_000, "pct": 0.2, "partial": False,
                                        "out": 0, "fresh": 100_000, "cached": 0, "spent": 100_000})
        self.assertNotIn("speed", tok)
        self.assertNotIn("minutes", tok)
        # 有一轮没报用量：前面写"约"
        self.add("b", "写除法", CODEX, "fe", ts=NOW - 300)
        self.turn(CODEX, "b", "tn_b1", NOW - 290, NOW - 280, status="failed")
        self.turn(CODEX, "b", "tn_b2", NOW - 200, NOW - 100, usage=50_000)
        tok = self.build(CODEX)["person"]["tokens"]
        self.assertEqual((tok["task"]["used"], tok["task"]["partial"]), (50_000, True))
        self.assertEqual((tok["task"]["out"], tok["task"]["fresh"], tok["task"]["cached"], tok["task"]["spent"]),
                         (0, 50_000, 0, 50_000))
        # 没干过活：今天是 0，"这件活"没有
        tok = self.build(DSH)["person"]["tokens"]
        self.assertEqual(tok, {"today": {"used": 0, "limit": 50_000_000, "pct": 0.0, "partial": False,
                                         "out": 0, "fresh": 0, "cached": 0, "spent": 0}})
        # Gemini 不按 token 管预算：只给数，不给上限和占比（页面不画圆环）
        self.add("g", "写说明", AGY, "docs", ts=NOW - 300)
        self.turn(AGY, "g", "tn_g", NOW - 200, NOW - 100, usage=3_000_000)
        tok = self.build(AGY)["person"]["tokens"]
        self.assertEqual(tok["task"], {"used": 3_000_000, "limit": None, "pct": None, "partial": False,
                                       "out": 0, "fresh": 3_000_000, "cached": 0, "spent": 3_000_000})
        self.assertEqual((tok["today"]["used"], tok["today"]["limit"]), (3_000_000, None))
        self.assertEqual((tok["today"]["out"], tok["today"]["fresh"], tok["today"]["cached"], tok["today"]["spent"]),
                         (0, 3_000_000, 0, 3_000_000))

    def test_speed_and_minute_cells_come_from_live_usage_rows(self):
        self.add("a", "写页面", CODEX, "fe", ts=NOW - 900)
        self.turn(CODEX, "a", "tn_live", NOW - 600)
        self.live_rows(CODEX, [
            {"kind": "thinking", "text": "想一想", "ts": NOW - 590},
            {"kind": "usage", "turn": "tn_live", "tokens": 40_000, "out": 400, "fresh": 30_000, "ts": NOW - 300},
            {"kind": "usage", "turn": "tn_live", "tokens": 100_000, "out": 1_000, "fresh": 80_000, "ts": NOW - 90},     # 60 秒窗口开始前的最后一条
            {"kind": "usage", "turn": "tn_live", "tokens": 130_000, "out": 1_300, "fresh": 100_000, "ts": NOW - 40},
            {"kind": "usage", "turn": "tn_live", "tokens": 160_000, "out": 1_600, "fresh": 120_000, "ts": NOW - 10},
            {"kind": "usage", "turn": "tn_old", "tokens": 999, "out": 10, "fresh": 500, "ts": NOW - 7200},       # 别的轮、30 分钟以外
            {"kind": "usage", "turn": "tn_live", "tokens": "坏的", "ts": NOW - 5},
        ])
        body = self.build(CODEX)
        tok = body["person"]["tokens"]
        self.assertEqual(tok["speed"], {"out_per_s": 10.0, "fresh_per_s": 666.7, "window_s": 60})                 # (1600 - 1000) / 60 = 10.0, (120000 - 80000) / 60 = 666.7
        self.assertEqual(tok["task"]["used"], 160_000)                                    # 一轮中途：直播里的数比账本新
        self.assertEqual((tok["task"]["out"], tok["task"]["fresh"], tok["task"]["cached"], tok["task"]["spent"]),
                         (1_600, 120_000, 160_000 - 1_600 - 120_000, 121_600))
        cells = tok["minutes"]
        self.assertEqual((len(cells), sum(cells)), (30, 121_600))
        self.assertEqual(tok["minutes_from"] % 60, 0)
        self.assertEqual(cells[int((NOW - 300 - tok["minutes_from"]) // 60)], 30_400)
        # 用量行不混进步骤
        self.assertNotIn("usage", json.dumps([s for r in body["timeline"] for s in r["steps"]], ensure_ascii=False))
        # 收工以后：不给速度，方格还在
        self.A("turn.ended", st(CODEX), {"em": CODEX, "status": "completed", "evidence": {"cli_status": "completed", "changed_files": []},
                                         "usage": {"in": 120_000, "out": 1_600, "cached": 38_400, "total": 160_000}},
               "tn_live", NOW - 1)
        tok = self.build(CODEX)["person"]["tokens"]
        self.assertNotIn("speed", tok)
        self.assertEqual(sum(tok["minutes"]), 121_600)

    def test_speed_needs_a_few_seconds_and_counts_from_the_turn_start(self):
        self.add("a", "写页面", CODEX, "fe", ts=NOW - 100)
        self.turn(CODEX, "a", "tn_new", NOW - 20)
        self.live_rows(CODEX, [{"kind": "usage", "turn": "tn_new", "tokens": 50_000, "out": 500, "fresh": 10_000, "ts": NOW - 2}])
        self.assertEqual(self.build(CODEX)["person"]["tokens"]["speed"], {"out_per_s": 25.0, "fresh_per_s": 500.0, "window_s": 20})
        self.add("b", "写脚本", DSH, "fe", ts=NOW - 50)
        self.turn(DSH, "b", "tn_fresh", NOW - 2)
        self.live_rows(DSH, [{"kind": "usage", "turn": "tn_fresh", "tokens": 9_000, "out": 100, "fresh": 2_000, "ts": NOW - 1}])
        self.assertNotIn("speed", self.build(DSH)["person"]["tokens"])                    # 刚开工 2 秒：还算不出
        # 没有 out 数据的旧用量行不算速度
        self.add("c", "老活", CODEX, "fe", ts=NOW - 100)
        self.turn(CODEX, "c", "tn_old_no_out", NOW - 20)
        self.live_rows(CODEX, [{"kind": "usage", "turn": "tn_old_no_out", "tokens": 50_000, "ts": NOW - 2}])
        self.assertNotIn("speed", self.build(CODEX)["person"]["tokens"])

    def test_speed_ignores_cache_tokens_and_only_counts_out(self):
        # 一轮里 tokens 从 10 万涨到 40 万（全是缓存）、out 只涨 3000，60 秒内 → out_per_s = 50，不是几千
        self.add("a", "跑大缓存任务", AGY, "docs", ts=NOW - 100)
        self.turn(AGY, "a", "tn_cache", NOW - 80)
        self.live_rows(AGY, [
            {"kind": "usage", "turn": "tn_cache", "tokens": 100_000, "out": 1_000, "fresh": 10_000, "ts": NOW - 70},  # 60 秒窗口开始前
            {"kind": "usage", "turn": "tn_cache", "tokens": 400_000, "out": 4_000, "fresh": 10_000, "ts": NOW - 10},
        ])
        body = self.build(AGY)
        tok = body["person"]["tokens"]
        self.assertEqual(tok["speed"]["out_per_s"], 50.0)
        self.assertEqual(tok["speed"]["fresh_per_s"], 0.0)
        self.assertEqual(tok["speed"]["window_s"], 60)
        self.assertNotIn("per_s", tok["speed"])


class LiveUsageRowTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(__import__("tempfile").mkdtemp(prefix="xay-live-usage-"))
        self.addCleanup(shutil.rmtree, self.tmp, True)

    def test_writer_dedupes_throttles_and_readers_skip_usage_rows(self):
        w = station_live.LiveWriter(self.tmp, "em_aa000001")
        w.usage("tn_1", 10)                               # 还没设段：不写
        w.set_segment(1)
        w.add("status", "开始")
        w.usage("tn_1", 100)
        w.usage("tn_1", 100)                              # 没变：不写
        w.usage("tn_1", 200)                              # 隔不到 1 秒：先攒着
        w.usage("tn_1", 300)
        w.add("tool", "运行命令：ls")                     # 写别的行时顺手把攒着的补上
        w.usage("tn_2", 5)                                # 换了一轮：马上写
        w.usage("tn_2", 9)
        w._usage_last = (w._usage_last[0], w._usage_last[1], 0.0)
        w.tick()                                          # 过了 1 秒：tick 补写
        rows = [json.loads(x) for x in (self.tmp / "em_aa000001" / "1.jsonl").read_text().splitlines()]
        self.assertEqual([(r["kind"], r.get("turn"), r.get("tokens")) for r in rows],
                         [("status", None, None), ("usage", "tn_1", 100), ("usage", "tn_1", 300), ("tool", None, None),
                          ("usage", "tn_2", 5), ("usage", "tn_2", 9)])
        self.assertEqual([r["n"] for r in rows], list(range(1, 7)))
        # 直播推送（终端窗口、在想 / 在做）看不到用量行
        self.assertEqual([x["kind"] for x in LiveTail(self.tmp, "em_aa000001").poll()], ["status", "tool"])
        live_rows, usage = employee.scan_live(self.tmp, "em_aa000001")
        self.assertEqual([r["kind"] for r in live_rows], ["status", "tool"])
        self.assertEqual([(u["turn"], u["tokens"]) for u in usage], [("tn_1", 100), ("tn_1", 300), ("tn_2", 5), ("tn_2", 9)])


class StationUsageTests(StationCase):
    def test_station_writes_turn_usage_into_the_live_file(self):
        f = Factory([[writes("t.txt", "t", [{"usage": {"used": 10_000, "turn_total": 5_000, "turn_out": 500, "turn_fresh": 4_000}},
                                            {"usage": {"used": 11_000, "turn_total": 30_000, "turn_out": 3_000, "turn_fresh": 20_000}},
                                            {"usage": {"used": 12_000, "turn_total": 60_000, "turn_out": 6_000, "turn_fresh": 40_000}},
                                            {"text": "【结果】完成：t"}])]])
        self.add_task("费 token 的活")
        self.run_station(f)
        tn = self.events("turn.started")[0]["subject"]
        rows = [json.loads(x) for x in (self.tmp / "v2" / "live" / self.em / "1.jsonl").read_text().splitlines()]
        usage = [r for r in rows if r["kind"] == "usage"]
        self.assertEqual((usage[0]["turn"], usage[0]["tokens"], usage[0].get("out"), usage[0].get("fresh")),
                         (tn, 5_000, 500, 4_000))
        self.assertEqual((usage[-1]["tokens"], usage[-1].get("out"), usage[-1].get("fresh")),
                         (60_000, 6_000, 40_000))                       # 攒着的最新数在这一轮结束前补写了
        self.assertEqual([r["n"] for r in rows], list(range(1, len(rows) + 1)))


def _n(v):
    """和 JS 模板字符串里数字的写法一样（整数不带 .0）。"""
    return str(int(v)) if float(v).is_integer() else repr(float(v))


@unittest.skipUnless(shutil.which("node"), "没装 node，跳过页面小模块的检查")
class PageModuleTests(unittest.TestCase):
    def run_node(self, script):
        res = subprocess.run(["node", "--input-type=module", "-e", script], capture_output=True, text=True, timeout=30,
                             cwd=str(WEB / "lib"))
        self.assertEqual(res.returncode, 0, res.stderr)
        return json.loads(res.stdout)

    def test_canvas_model_lays_parts_depts_and_ai_work(self):
        """任务画布第三版（多条线）：大任务 → 中任务（按先后一列一列，能同时做的各占一条轨道，分叉再汇合）
        → 小任务（按依赖连成一串，互不相关的并行；旧数据的部门只当小标签）→ 点开看详情。只给任务树（员工接口的 tree）也能画。"""
        def item(id, title, status, owner, name, mine=False, tries=1, steps=(), of=None, step=None, deps=(), former=False, check=None):
            out = {"id": id, "title": title, "status": status, "legacy": False, "owner": owner, "owner_name": name, "former": former,
                   "mine": mine, "tries": tries, "step": step, "of": of, "depth": 0, "chained": False, "after": [], "deps": list(deps),
                   "steps": [{"n": i + 1, "text": t, "state": s} for i, (t, s) in enumerate(steps)]}
            if check:
                out["check"] = check
            return out
        many = [item("tk_m%d" % i, "零碎活 %d" % i, "done", "em_g", "Gemini 1") for i in range(3)]
        tree = {"title": "做商品页<b>", "count": {"all": 9, "done": 5, "working": 1, "needs_you": 0},
                "parts": [
                    {"id": "pt_2", "title": "做页面", "col": 1, "row": 0, "status": "working",
                     "check": {"kind": "ai", "reviewer": "codex", "text": "接口对得上"}, "mine": True,
                     "detail": {"after_titles": ["定需求"]},
                     "depts": [{"id": "fe", "name": "前端组", "items": [
                                   item("tk_x", "写样式", "failed", "em_d", "DeepSeek 1", tries=2, steps=[("定颜色", "stop")]),
                                   item("tk_b", "写样式", "working", "em_c", "Codex 1", mine=True, of=3, step=2,
                                        steps=[("定颜色", "done"), ("排版", "cur"), ("暗色", "todo")],
                                        check={"state": "failed", "round": 2, "text": "按钮没对齐", "by": "Codex 2"})]},
                               {"id": "be", "name": "后端组", "items": [
                                   item("tk_c", "写接口", "done", "em_g", "Gemini 1", of=2, step=2, check={"state": "passed", "round": 1}),
                                   item("tk_e", "接库存", "working", "em_g", "Gemini 1", deps=["tk_c"])]}],
                     "items": []},
                    {"id": "pt_1", "title": "定需求", "col": 0, "row": 0, "status": "done", "check": {"kind": "rules"}, "mine": False,
                     "depts": [], "items": [item("tk_a", "列清单", "done", "em_c", "Codex 1", of=3, step=3,
                                                 steps=[("", "done"), ("写结构", "done"), ("收尾", "done")])]},
                    {"id": "pt_3", "title": "写说明", "col": 1, "row": 1, "status": "ready", "check": {"kind": "lead"}, "mine": False,
                     "depts": [], "items": [item("tk_d", "写安装说明", "planned", None, "", deps=["tk_e"])]},
                    {"id": "pt_4", "title": "收尾", "col": 2, "row": 0, "status": "planned",
                     "check": {"kind": "rules", "commands": ["npm test"]}, "mine": False, "detail": {"after_titles": ["做页面", "写说明"]},
                     "depts": [], "items": many},
                    {"id": "", "title": "零散的活", "col": 3, "row": 0, "status": "working", "check": {"kind": "rules"}, "mine": False,
                     "depts": [], "items": []}]}
        old = {"title": "旧对话", "count": {"all": 2, "done": 1},
               "parts": [{"id": "dept:tools", "title": "工具部", "col": 0, "row": 0, "status": "working", "check": {"kind": "rules"},
                          "depts": [{"id": "tools", "name": "工具部", "items": [item("tk_o", "老活", "done", "em_c", "Codex 1"),
                                                                                 item("tk_p", "新活", "working", "em_c", "Codex 1")]}],
                          "items": []},
                         {"id": "dept:docs", "title": "文档部", "col": 0, "row": 1, "status": "done", "check": {"kind": "rules"},
                          "depts": [{"id": "docs", "name": "文档部", "items": [item("tk_q", "写文档", "done", "em_d", "DeepSeek 1")]}],
                          "items": []}]}
        groups = {"title": "只有 groups", "groups": [{"id": "fe", "name": "页面部", "items": [item("tk_g", "搭骨架", "done", "em_c", "Codex 1")]}]}
        out = self.run_node("""
import { canvasModel, canvasLayout, summaryHtml, canvasCounts } from './task-canvas.js';
import { buildPlan, stepRows } from './plan-model.js';
const tree = %s, old = %s, groups = %s;
const pm = buildPlan(null, tree, {em: 'em_c'});
const cm = canvasModel(pm);
const lay = canvasLayout(cm, k => k === 'root' ? {w: 240, h: 120} : k[0] === 'p' ? {w: 212, h: 100} : k[0] === 's' ? {w: 80, h: 26} : {w: 236, h: 50});
const pos = Object.fromEntries([...lay.pos.entries()]);
const P = cp => ({id: cp.p.id, code: cp.p.code, title: cp.p.title, color: cp.p.color, lane: cp.lane, ccol: cp.ccol, check: cp.p.check.word,
  word: cp.p.word, done: cp.p.done, all: cp.p.all, hot: cp.hot, dim: cp.dim, k: cp.k, groups: cp.groups.map(g => [g.name, g.label]),
  rows: cp.rows.map(r => r.map(it => it.id)), levels: cp.items.map(x => [x.it.id, x.level, x.row]), cross: cp.cross, preds: cp.preds.map(a => a.p.id)});
const b = pm.itemOf('tk_x');
console.log(JSON.stringify({parts: cm.parts.map(P), mode: cm.mode, steps: cm.steps, pos, steplines: lay.steplines, wires: lay.wires.map(w => ({cls: w.cls, c: w.c, d: w.d})),
  ends: lay.ends, b: {id: b.id, ids: b.ids, name: b.name, mine: b.mine, sub: b.sub, tries: b.tries, check: b.check, rows: stepRows(b)},
  a: stepRows(pm.itemOf('tk_a')).map(r => r.text), d: pm.itemOf('tk_d'),
  sum: summaryHtml(tree), counts: canvasCounts(tree),
  old: buildPlan(null, old).parts.map(p => [p.id, p.title, p.depts.length, p.items.map(i => i.id)]),
  groups: buildPlan(null, groups).parts.map(p => p.id),
  empty: summaryHtml({title: 'x', parts: []}), nil: summaryHtml(null), emptyModel: canvasModel(buildPlan(null, {parts: []})).parts.length}));
""" % (json.dumps(tree, ensure_ascii=False), json.dumps(old, ensure_ascii=False), json.dumps(groups, ensure_ascii=False)))
        parts = {p["id"]: p for p in out["parts"]}
        self.assertEqual([p["id"] for p in out["parts"]], ["pt_1", "pt_2", "pt_3", "pt_4"])          # 按列、再按行；空的零散的活不画
        self.assertEqual([p["code"] for p in out["parts"]], ["1", "2-A", "2-B", "3"])                  # 同一步能同时做的写 2-A / 2-B
        self.assertEqual(len({p["color"] for p in out["parts"]}), 4)                                     # 每个小任务一个颜色
        self.assertEqual([p["check"] for p in out["parts"]], ["自动检查", "Codex 审查", "负责人亲自审", "按负责人的规则"])
        self.assertEqual([(p["word"], p["done"], p["all"]) for p in out["parts"]],
                         [("做完了", 1, 1), ("正在做", 1, 3), ("可以开工", 0, 1), ("还不能开工", 3, 3)])
        # 多条线：2-A 接着 1 走同一条轨道，2-B 分出去另起一条；3 汇合回第一条
        self.assertEqual([(p["ccol"], p["lane"]) for p in out["parts"]], [(0, 0), (1, 0), (1, 1), (2, 0)])
        self.assertEqual(parts["pt_4"]["preds"], ["pt_2", "pt_3"])                                     # 树里没有 after：按 after_titles 对上
        self.assertEqual([s["n"] for s in out["steps"]], [1, 2, 3])
        self.assertEqual([l["n"] for l in out["steplines"]], [2, 3])                                   # 不同步之间一条竖向虚线（office-spec.md 三）
        for l in out["steplines"]:                                                                       # 线画在两列之间的缝里
            self.assertTrue(all(abs(out["pos"][cp["k"]]["x"] - l["x"]) > 20 for cp in out["parts"]))
        self.assertTrue(out["steps"][1]["multi"])
        # 小任务里的活：有部门就分组（底色写部门名）；有先后的同一行连成一串，互不相关的各占一行
        p2 = parts["pt_2"]
        self.assertEqual(p2["groups"], [["", False]])                                                   # 中任务里不再分部门（部门就是中任务）
        self.assertEqual(p2["rows"], [["tk_b"], ["tk_c", "tk_e"]])                                      # 前一个人没做成、换人重做的合成一件
        self.assertEqual(sorted(p2["levels"]), [["tk_b", 0, 0], ["tk_c", 0, 1], ["tk_e", 1, 1]])
        self.assertEqual(parts["pt_3"]["cross"], [["tk_e", "tk_d"]])                                   # 跨小任务的依赖：细虚线
        self.assertEqual(len(parts["pt_4"]["rows"]), 3)                                                 # 互不相关：三条并行短线
        # 摆位置：同一列的上下叠、不同轨道；汇合进 3 的线保留各自的颜色
        pos = out["pos"]
        k = {p["id"]: p["k"] for p in out["parts"]}
        self.assertEqual(pos[k["pt_2"]]["x"], pos[k["pt_3"]]["x"])
        self.assertLess(pos[k["pt_2"]]["y"], pos[k["pt_3"]]["y"])
        self.assertEqual(pos[k["pt_2"]]["y"], pos[k["pt_4"]]["y"])
        self.assertGreater(pos[k["pt_4"]]["x"], pos[k["pt_2"]]["x"])
        p4 = pos[k["pt_4"]]
        into4 = [w for w in out["wires"] if w["d"].endswith("%s %s" % (_n(p4["x"]), _n(p4["y"] + p4["h"] / 2))) or w["d"].endswith("H%s" % _n(p4["x"]))]
        self.assertEqual(sorted(w["c"] for w in into4), sorted([parts["pt_2"]["color"], parts["pt_3"]["color"]]))
        self.assertTrue(all("w-plan" in w["cls"] for w in into4))                                       # 3 还不能开工：虚线
        self.assertEqual(len([w for w in out["wires"] if "w-cross" in w["cls"]]), 1)
        self.assertEqual(len(out["ends"]), 3)                                                           # 最后一个小任务：每串各到头，不再拉长线汇到一处
        # 从员工详情打开：他的活高亮，别的稍淡
        self.assertEqual(out["mode"], "mine")
        self.assertEqual((parts["pt_1"]["hot"], parts["pt_2"]["hot"], parts["pt_3"]["dim"]), (True, True, True))
        b = out["b"]
        self.assertEqual((b["id"], b["ids"], b["name"], b["mine"], b["sub"], b["tries"]), ("tk_b", ["tk_x", "tk_b"], "Codex 1", True, "正在做 · 第 2/3 步", 3))
        self.assertEqual([(x["text"], x["state"]) for x in b["rows"]],
                         [("第 1–2 次 · DeepSeek 1没做成", "fail"), ("这是第 3 次做", "note"), ("定颜色", "done"), ("排版", "cur"), ("暗色", "todo")])
        self.assertEqual((b["check"]["word"], b["check"]["tone"]), ("监工打回 · 第 2 轮", "err"))
        self.assertIn("按钮没对齐", b["check"]["tip"])
        self.assertEqual(out["a"], ["第 1 步", "写结构", "收尾"])
        self.assertEqual((out["d"]["name"], out["d"]["none"], out["d"]["deps"]), ("", True, ["tk_e"]))
        # 按任务拆，不按部门拆：老的"一个部门一个小任务"合成一个（全部的活），里面不分部门
        self.assertEqual(out["old"], [["all", "全部的小任务", 0, ["tk_o", "tk_p", "tk_q"]]])
        self.assertEqual(out["groups"], ["all"])
        self.assertEqual(out["counts"], {"all": 9, "done": 5, "needs": 0, "mine": 1})
        sum_ = out["sum"]
        self.assertIn("小任务 9 件 · 做完 5 · 他负责 1 件", sum_)                                           # 一行概括 + 按钮
        self.assertIn('data-act="canvas"', sum_)
        self.assertIn("展开任务画布", sum_)
        self.assertIn("4 个中任务 · 做商品页&lt;b&gt;", sum_)
        self.assertNotIn("disabled", sum_)
        for empty in (out["empty"], out["nil"]):                                                         # 没有活：按钮不能点
            self.assertIn("还没有任务", empty)
            self.assertIn("disabled", empty)
            self.assertNotIn("展开任务画布", empty)
        self.assertEqual(out["emptyModel"], 0)

    def test_token_html_shows_only_what_it_has(self):
        """token 新口径（协议 1.28）：大字"这件小任务花了 X"（spent = 新读 + 写出），小字"写出 a、新读 b，另有 c 是重复读的缓存（便宜）"，
        速度"每秒写出约 N 个"（out_per_s）。旧记录只有总数、没分开记的：按全部读写算、前面加"约"，不写"花了 0"。"""
        out = self.run_node("""
import { tokenHtml, tokenText } from './token-meter.js';
const full = {task: {used: 1418000, limit: 5000000, pct: 28.4, partial: true, spent: 218000, out: 31000, fresh: 187000, cached: 1200000},
              today: {used: 5518762, limit: 50000000, pct: 11, spent: 969284, out: 210728, fresh: 758556, cached: 4549478},
              speed: {out_per_s: 31.4, fresh_per_s: 120, window_s: 60}, minutes: [0, 5, 10, 0], minutes_from: 1793379120};
const old = {task: {used: 3782990, limit: null, pct: null, partial: false, out: 0, fresh: 0, cached: 3782990, spent: 0}};
console.log(JSON.stringify({
  nums: [tokenText(9600), tokenText(12345), tokenText(5518762), tokenText(50000000), tokenText(123456789)],
  working: tokenHtml(full, true), resting: tokenHtml(full, false), none: tokenHtml({}, true), nil: tokenHtml(null, true),
  oldSpeed: tokenHtml({task: full.task, speed: {per_s: 17234.5, window_s: 60}}, true), old: tokenHtml(old, false),
  noLimit: tokenHtml({today: {used: 0, limit: null, pct: null, spent: 0}}, false)}));
""")
        self.assertEqual(out["nums"], ["9600", "1.2万", "552万", "5000万", "1.2亿"])
        w = out["working"]
        for need in ("token 消耗", "这件小任务花了", "约", "21.8万", "写出 3.1万、新读 18.7万，另有 120万 是重复读的缓存（便宜）",
                     "预算 500万", "28%", "今天一共花了", "96.9万", "每秒写出约", "31", "tk-ring", "4 分钟前", "最近 1 分钟"):
            self.assertIn(need, w)
        self.assertIn("lv4", w)
        self.assertNotIn("/秒", w)
        self.assertNotIn("每秒写出", out["resting"])        # 不在干活：不写速度，方格还在
        self.assertIn("tk-cells", out["resting"])
        self.assertNotIn("每秒写出", out["oldSpeed"])        # 老字段 per_s 不再认（不编数）
        self.assertIn("378万", out["old"])                   # 旧记录：按全部读写算
        self.assertIn("没分开记新读和写出", out["old"])
        self.assertEqual((out["none"], out["nil"]), ("", ""))
        self.assertNotIn("tk-ring", out["noLimit"])      # 没有预算：只写数，不画圆环

    def test_panel_wires_the_two_blocks(self):
        js = (WEB / "lib" / "employee-panel.js").read_text(encoding="utf-8")
        for need in ("./task-canvas.js", "./token-meter.js", "addTile('emp-tree'", "addTile('emp-tokens'", "person.tokens", "st.data.tree",
                     "act === 'canvas'", "openTaskCanvas(", "updateTaskCanvas(", "closeTaskCanvas(", "syncPoll"):
            self.assertIn(need, js)
        self.assertNotIn("task-tree", js)
        self.assertFalse((WEB / "lib" / "task-tree.js").exists())                         # 旧的拆解树删掉了
        self.assertIn('<link rel="stylesheet" href="/web/task-canvas.css">', (WEB / "org.html").read_text(encoding="utf-8"))
        self.assertNotIn(".tt-", (WEB / "org.css").read_text(encoding="utf-8"))
        canvas = (WEB / "lib" / "task-canvas.js").read_text(encoding="utf-8")
        for need in ("Escape", "stopImmediatePropagation", "适应屏幕", "100%", "'wheel'", "pointerdown", "<svg", "openDetail("):
            self.assertIn(need, canvas)                                                  # Esc 只关画布；能拖、能缩放；连线用 SVG；点一件活看详情
        css = (WEB / "task-canvas.css").read_text(encoding="utf-8")
        for need in (':root[data-theme="dark"]', ':root[data-theme="light"]', "prefers-color-scheme: dark", "var(--ai-agy)", "var(--accent)"):
            self.assertIn(need, css)                                                     # 跟 org.css 的明暗开关和颜色走
        for f in ("task-canvas.js", "token-meter.js", "plan-model.js", "plan-detail.js"):
            src = (WEB / "lib" / f).read_text(encoding="utf-8")
            for bad in ("fetch(", "method:", "postAction", "X-Xay-Action", "localStorage"):
                self.assertNotIn(bad, src)                # 只画，不发请求、不写东西


if __name__ == "__main__":
    unittest.main()
