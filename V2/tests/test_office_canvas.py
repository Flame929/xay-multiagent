"""公司页办公室画布（2026-10-31 画布改版）：

- 组织树连线：每个部门一条完整的线，从总经理下沿一直到房间上沿，不穿过任何房间，两条线不叠在一起（装了 node 才跑）。
- 页面脚本：工位、总经理、部门房间就地更新（不整块 innerHTML 重画），彩蛋要用的标记都还在。
- 房间 = 小任务（arch-v10-parts.md）：谁坐哪间房、房间里按部门分小区、手上没活的坐休息区；监工的信落在那间房的监工席上。
"""
import json
import shutil
import subprocess
import unittest
from pathlib import Path

WEB = Path(__file__).resolve().parents[1] / "web"


def _node_json(script):
    out = subprocess.run(["node", "--input-type=module", "-e", script], capture_output=True, text=True, timeout=30)
    if out.returncode != 0:
        raise AssertionError(out.stderr)
    return json.loads(out.stdout)


def _box(key, l, t, w, h, **extra):
    return dict(key=key, l=l, t=t, r=l + w, b=t + h, **extra)


@unittest.skipUnless(shutil.which("node"), "没装 node，跳过连线走法检查")
class ArcRouteTests(unittest.TestCase):
    def route(self, gm, boxes):
        url = (WEB / "lib" / "render-office.js").as_uri()
        script = "import { routeArcs } from %s;\nconsole.log(JSON.stringify(routeArcs(%s, %s)));" % (
            json.dumps(url), json.dumps(gm), json.dumps(boxes))
        return _node_json(script)

    def check(self, gm, boxes):
        routes = self.route(gm, boxes)
        targets = [b for b in boxes if b.get("target", True)]
        self.assertEqual(sorted(r["key"] for r in routes), sorted(b["key"] for b in targets))
        segs = []
        for r in routes:
            pts = r["pts"]
            box = next(b for b in targets if b["key"] == r["key"])
            self.assertAlmostEqual(pts[0][1], gm["b"], places=3)                      # 从总经理下沿出发
            self.assertTrue(gm["l"] <= pts[0][0] <= gm["r"])
            self.assertAlmostEqual(pts[-1][0], (box["l"] + box["r"]) / 2, places=3)   # 到房间正上方
            self.assertAlmostEqual(pts[-1][1], box["t"], places=3)
            for (x1, y1), (x2, y2) in zip(pts, pts[1:]):
                self.assertTrue(abs(x1 - x2) < 1e-6 or abs(y1 - y2) < 1e-6, "只走横竖线")
                segs.append((r["key"], x1, y1, x2, y2))
                for b in boxes:                                                       # 不穿过任何房间（自己房间的上沿除外）
                    inside_x = max(min(x1, x2), b["l"] + 1) < min(max(x1, x2), b["r"] - 1)
                    inside_y = max(min(y1, y2), b["t"] + 1) < min(max(y1, y2), b["b"] - 1)
                    if abs(x1 - x2) < 1e-6:
                        inside_x = b["l"] + 1 < x1 < b["r"] - 1
                    if abs(y1 - y2) < 1e-6:
                        inside_y = b["t"] + 1 < y1 < b["b"] - 1
                    self.assertFalse(inside_x and inside_y, "%s 的线穿过了 %s" % (r["key"], b["key"]))
        for i, a in enumerate(segs):                                                  # 两个部门的线不叠在一起
            for b in segs[i + 1:]:
                if a[0] == b[0]:
                    continue
                if abs(a[1] - a[3]) < 1e-6 and abs(b[1] - b[3]) < 1e-6 and abs(a[1] - b[1]) < 1:
                    lo, hi = max(min(a[2], a[4]), min(b[2], b[4])), min(max(a[2], a[4]), max(b[2], b[4]))
                    self.assertLess(hi - lo, 1, "%s 和 %s 的竖线叠在一起" % (a[0], b[0]))
                if abs(a[2] - a[4]) < 1e-6 and abs(b[2] - b[4]) < 1e-6 and abs(a[2] - b[2]) < 1:
                    lo, hi = max(min(a[1], a[3]), min(b[1], b[3])), min(max(a[1], a[3]), max(b[1], b[3]))
                    self.assertLess(hi - lo, 1, "%s 和 %s 的横线叠在一起" % (a[0], b[0]))
        return routes

    def test_one_row_three_rooms(self):
        gm = _box("gm", 600, 0, 360, 170)
        self.check(gm, [_box("a", 100, 230, 300, 280), _box("b", 460, 230, 300, 280), _box("c", 820, 230, 300, 280)])

    def test_rooms_below_first_row_go_through_gaps(self):
        gm = _box("gm", 600, 0, 360, 170)
        boxes = [_box("a", 100, 230, 420, 300), _box("b", 576, 230, 420, 260), _box("c", 1052, 230, 300, 300),
                 _box("d", 300, 586, 300, 280), _box("e", 656, 586, 500, 280),
                 _box("f", 500, 922, 300, 280),
                 _box("_past", 0, 1258, 1460, 44, target=False), _box("g", 100, 1358, 300, 280)]
        routes = self.check(gm, boxes)
        self.assertGreaterEqual(len(routes[0]["pts"]), 2)

    def test_single_room_right_below_goes_straight(self):
        gm = _box("gm", 300, 0, 360, 170)
        routes = self.check(gm, [_box("a", 330, 230, 300, 280)])
        self.assertEqual(len(routes[0]["pts"]), 2)                                    # 正下方：一条竖线


@unittest.skipUnless(shutil.which("node"), "没装 node，跳过信件虚线的规则检查")
class MailModelTests(unittest.TestCase):
    """画布上的信件：谁和谁之间画虚线（最近 24 小时、同一对人一条线）、谁身上挂"还没送到"的信。"""

    def model(self, letters, depts, now):
        url = (WEB / "lib" / "render-office.js").as_uri()
        script = ("import { mailModel } from %s;\nconst m = mailModel(%s, %s, %s);\n"
                  "console.log(JSON.stringify({pairs: m.pairs.map(p => [p.key, p.n, p.fresh, p.last.id]), unread: [...m.unread]}));") % (
            json.dumps(url), json.dumps(letters), json.dumps(depts), json.dumps(now))
        return _node_json(script)

    def test_pairs_unread_and_age(self):
        now = 1793300000
        L = lambda i, f, t, ago, **kw: dict(id="lt_%d" % i, ts=now - ago, **{"from": f, "to": t}, kind=kw.pop("kind", "tell"), **kw)
        letters = [
            L(1, "office", "em_a", 7200, delivery={"state": "delivered", "to": "em_a"}),
            L(2, "em_a", "office", 7100, reply_to="lt_1", delivery={"state": "none", "to": "office"}),
            L(3, "gm", "em_b", 60, delivery={"state": "waiting", "to": "em_b"}),
            L(4, "em_c", "fe", 120, kind="request", delivered_to="em_b", delivery={"state": "delivered", "to": "em_b"}),
            L(5, "em_c", "qa", 30, delivery={"state": "waiting", "to": None}),
            L(6, "user", "gm", 10, delivery={"state": "none"}),                       # 用户在画布上没有位置：不画线
            L(7, "gm", "em_a", 3 * 86400, delivery={"state": "delivered"}),           # 太久以前的：不画线
        ]
        out = self.model(letters, [{"id": "fe"}, {"id": "qa"}], now)
        pairs = {p[0]: p for p in out["pairs"]}
        self.assertEqual(pairs["em_a~office"][1:], [2, False, "lt_2"])            # 一来一回是同一条线，两封
        self.assertEqual(pairs["em_b~gm"][1:], [1, True, "lt_3"])
        self.assertIn("em_b~em_c", pairs)                                          # 写给部门、送到了谁就连到谁
        self.assertIn("dept:qa~em_c", pairs)                                       # 还没送到某个人：连到部门牌子
        self.assertNotIn("em_a~gm", pairs)
        self.assertEqual(len(out["pairs"]), 4)
        unread = dict(out["unread"])
        self.assertEqual(unread["em_b"], {"unread": 1, "letter": "lt_3"})
        self.assertEqual(unread["dept:qa"]["unread"], 1)
        self.assertNotIn("em_a", unread)

    def test_supervisor_letters_start_at_the_department_head_desk(self):
        """后台自动写的信（office / rules）页面上是监工发的：虚线连收信员工所在部门的监工席；
        对方没分部门、部门设了不要监工、或是总经理时，才连到总经理右边那位监工（'office'）。"""
        now = 1793300000
        L = lambda i, f, t, **kw: dict(id="lt_%d" % i, ts=now - 60 * i, **{"from": f, "to": t}, kind=kw.pop("kind", "notice"), **kw)
        letters = [
            L(1, "office", "em_a"),                                  # 页面部的员工：从页面部的监工席出
            L(2, "em_a", "office", kind="tell"),                    # 他的回信：落在同一个监工席，同一条线
            L(3, "rules", "em_b"),                                   # rules 也是监工
            L(4, "office", "em_c"),                                  # 部门设了不要监工：落到总经理右边那位监工
            L(5, "office", "em_loose"),                              # 没分部门
            L(6, "office", "qa", delivery={"state": "waiting", "to": None}),   # 写给部门、还没送到人：那个部门的监工席 → 部门牌子
        ]
        depts = [{"id": "fe", "head": "rules", "members": ["em_a"]}, {"id": "qa", "head": "claude", "members": ["em_b"]},
                 {"id": "ops", "head": "none", "members": ["em_c"]}]
        pairs = {p[0]: p for p in self.model(letters, depts, now)["pairs"]}
        self.assertEqual(pairs["em_a~head:fe"][1], 2)
        self.assertIn("em_b~head:qa", pairs)
        self.assertIn("em_c~office", pairs)
        self.assertIn("em_loose~office", pairs)
        self.assertIn("dept:qa~head:qa", pairs)
        self.assertEqual(len(pairs), 5)
        self.assertFalse([k for k in pairs if "rules" in k])


@unittest.skipUnless(shutil.which("node"), "没装 node，跳过房间摆法的检查")
class RoomPlanTests(unittest.TestCase):
    """公司页按小任务摆房间：一个小任务一间房；员工坐在他手上（正在做 → 排队 → 最近做的）那件活所在的房间；没活的坐休息区。"""

    def plan(self, snap, letters=None):
        url = (WEB / "lib" / "render-office.js").as_uri()
        script = ("import { roomPlan, mailModel } from %s;\nconst snap = %s;\nconst p = roomPlan(snap);\n"
                  "const m = mailModel(%s, snap.depts, snap.now, p.rooms);\n"
                  "console.log(JSON.stringify({mode: p.mode, rest: p.rest, rooms: p.rooms.map(r => ({key: r.key, title: r.title, n: r.n, "
                  "keys: r.keys, head: r.head, zones: r.zones, folded: r.folded, tasks: r.tasks.map(t => t.id)})), sleepers: [...(p.sleepers || [])], "
                  "pairs: m.pairs.map(x => x.key)}));") % (
            json.dumps(url), json.dumps(snap, ensure_ascii=False), json.dumps(letters or []))
        return _node_json(script)

    @staticmethod
    def person(key, label, state="idle", queue=(), dept=None):
        return {"key": key, "label": label, "agent": "codex", "state": state, "display_state": state, "queue": list(queue), "dept": dept}

    def test_new_conversation_rooms_follow_parts(self):
        now = 1793300000
        P = self.person
        people = {k: P(k, k.upper(), *rest) for k, *rest in (
            ("em_a", "working", (), "fe"), ("em_b", "queued", ("tk_q",)), ("em_c",), ("em_d", "idle", (), "be"), ("em_e",), ("em_f",))}
        T = lambda id, part, owner, status, dept=None: {"id": id, "part": part, "owner": owner, "status": status, "dept": dept, "title": id}
        snap = {"now": now, "conversation": {"id": "cv", "title": "做商品页"}, "people": people, "former": [{"key": "em_f"}],
                "depts": [{"id": "fe", "name": "前端组", "members": ["em_a"], "head": "rules", "counts": {}},
                          {"id": "be", "name": "后端组", "members": ["em_d"], "head": "rules", "counts": {}}],
                "parts": [{"id": "pt_1", "title": "定需求", "col": 0, "row": 0, "status": "done", "check": {"kind": "rules"}, "depts": []},
                          {"id": "pt_2", "key": "k2", "title": "做页面", "col": 1, "row": 0, "status": "working",
                           "check": {"kind": "ai", "reviewer": "codex"}, "depts": ["fe", "be"]},
                          {"id": "pt_3", "title": "写说明", "col": 1, "row": 1, "status": "working", "check": {"kind": "lead"}, "depts": []},
                          {"id": "", "title": "零散的活", "col": 2, "row": 0, "status": "ready", "check": {"kind": "rules"}, "depts": []}],
                "tasks": [T("tk_1", "pt_1", "em_b", "done"), T("tk_2", "pt_2", "em_a", "working", "fe"), T("tk_q", "pt_3", "em_b", "queued"),
                          T("tk_old", "pt_1", "em_c", "done"), T("tk_new", "k2", "em_c", "failed"), T("tk_e", None, "em_e", "planned"),
                          T("tk_f", "pt_1", "em_f", "done")],
                "activity": {"tasks": {"tk_old": now - 600, "tk_new": now - 60}}}
        letters = [dict(id="lt_1", ts=now - 60, kind="notice", **{"from": "office", "to": "em_a"}),
                   dict(id="lt_2", ts=now - 50, kind="notice", **{"from": "office", "to": "em_d"}),
                   dict(id="lt_3", ts=now - 40, kind="request", delivery={"state": "waiting", "to": None}, **{"from": "office", "to": "be"})]
        out = self.plan(snap, letters)
        self.assertEqual(out["mode"], "parts")
        rooms = {r["key"]: r for r in out["rooms"]}
        self.assertEqual([r["key"] for r in out["rooms"]], ["pt_1", "pt_2", "pt_3", "_loose"])     # 按列、再按行
        self.assertEqual([r["n"] for r in out["rooms"]], [1, 2, 2, 3])
        self.assertEqual(rooms["pt_2"]["keys"], ["em_a", "em_c"])          # 正在做的那件；最近做的（没做成）那件，part 写的是简称 k2 也认得
        self.assertEqual(rooms["pt_3"]["keys"], ["em_b"])                  # 排队的比做完的优先
        self.assertEqual(rooms["pt_1"]["keys"], [])                        # 人都被派去别的中任务干活了
        self.assertTrue(rooms["pt_1"]["folded"])                           # 一个人都不剩：收起成一行（office-spec.md 二.6）
        self.assertFalse(rooms["pt_1"]["head"])                            # 监工也一起下班
        self.assertEqual(rooms["_loose"]["keys"], ["em_e"])                # 没写小任务的活：零散的活那间
        self.assertEqual(out["rest"], ["em_d"])                            # 部门里但手上没活：休息区；离职的不画
        self.assertTrue(all(r["zones"] is None for r in out["rooms"]))   # 中任务里不再按部门分小区，大家坐一起（部门就是中任务）
        self.assertTrue(all(r["head"] for r in out["rooms"] if not r["folded"]))   # 有人的房间都有监工席
        self.assertEqual(rooms["pt_2"]["tasks"], ["tk_2", "tk_new"])
        pairs = set(out["pairs"])
        self.assertIn("em_a~head:pt_2", pairs)                             # 监工的信从他那间房的监工席出
        self.assertIn("em_d~office", pairs)                                # 休息区的人：总经理右边那位监工
        self.assertIn("dept:be~head:pt_2", pairs)                          # 写给部门还没送到：那间房的监工席 → 部门小区牌子

    def test_done_people_stay_and_sleep_until_sent_elsewhere(self):
        """谁坐哪（office-spec.md 二.6）：做完了留在原房间睡觉；还没轮到的活（planned）不算"派去干活"，不搬；
        又被派到别的中任务里正在做 / 排队了才搬过去；房间一个人都不剩 → 收起、监工下班。"""
        P = self.person
        T = lambda id, part, owner, status: {"id": id, "part": part, "owner": owner, "status": status, "dept": None, "title": id}
        snap = {"now": 1793300000, "conversation": {"id": "cv", "title": "x"}, "former": [], "depts": [],
                "people": {k: P(k, k.upper()) for k in ("em_a", "em_b", "em_c")},
                "parts": [{"id": "pt_1", "title": "一", "col": 0, "row": 0, "status": "done", "check": {"kind": "rules"}, "depts": []},
                          {"id": "pt_2", "title": "二", "col": 0, "row": 1, "status": "done", "check": {"kind": "lead"}, "depts": []},
                          {"id": "pt_3", "title": "三", "col": 1, "row": 0, "status": "working", "check": {"kind": "rules"}, "depts": []}],
                "tasks": [T("t1", "pt_1", "em_a", "done"), T("t2", "pt_3", "em_a", "planned"),        # A：后面的还没轮到 → 留在一号房睡觉
                          T("t3", "pt_2", "em_b", "reported"), T("t4", "pt_3", "em_b", "working"),    # B：又被派去三号房干活 → 搬过去
                          T("t5", "pt_2", "em_c", "done"), T("t6", "pt_3", "em_c", "queued")]}        # C：排队也算派过去了
        out = self.plan(snap)
        rooms = {r["key"]: r for r in out["rooms"]}
        self.assertEqual((rooms["pt_1"]["keys"], rooms["pt_2"]["keys"], rooms["pt_3"]["keys"]), (["em_a"], [], ["em_b", "em_c"]))
        self.assertEqual(out["sleepers"], ["em_a"])
        self.assertEqual([(k, rooms[k]["folded"], rooms[k]["head"]) for k in ("pt_1", "pt_2", "pt_3")],
                         [("pt_1", False, True), ("pt_2", True, False), ("pt_3", False, True)])

    def test_old_conversation_is_one_room_not_one_per_department(self):
        """没拆小任务的对话（以前按部门当小任务的旧对话也一样）：按任务拆，不按部门拆——整个对话一间房（全部的活），不分部门小区。
        用户："我要的是根据任务拆解，不是根据部门拆解！" 编号 / 颜色 / 名字和画布、任务拆解同一份（plan-model.js）。"""
        P = self.person
        snap = {"now": 1793300000, "conversation": {"id": "cv", "title": "旧对话"},
                "people": {"em_a": P("em_a", "A", "working", (), "tools"), "em_b": P("em_b", "B", "idle", (), "tools"), "em_c": P("em_c", "C")},
                "former": [],
                "depts": [{"id": "tools", "name": "工具部", "members": ["em_b", "em_a"], "head": "rules", "counts": {}},
                          {"id": "docs", "name": "文档部", "members": [], "head": "none", "counts": {}}],
                "parts": [{"id": "dept:tools", "title": "工具部", "col": 0, "row": 0, "status": "working", "check": {"kind": "rules"}, "depts": ["tools"]},
                          {"id": "dept:docs", "title": "文档部", "col": 0, "row": 1, "status": "done", "check": {"kind": "rules"}, "depts": ["docs"]}],
                "tasks": [{"id": "tk_1", "owner": "em_a", "status": "working", "dept": "tools", "title": "x"},
                          {"id": "tk_2", "owner": "em_c", "status": "done", "dept": "docs", "title": "y"}]}
        for case in (snap, dict(snap, parts=[]),
                     dict(snap, parts=[{"id": "all", "title": "旧对话", "col": 0, "row": 0, "status": "working", "check": {"kind": "rules"}, "depts": []}])):
            out = self.plan(case)
            self.assertEqual(out["mode"], "parts")
            self.assertEqual([(r["key"], r["title"], r["n"], r["keys"], r["zones"], r["tasks"]) for r in out["rooms"]],
                             [("_all", "全部的小任务", 1, ["em_a", "em_c"], None, ["tk_1", "tk_2"])])
            self.assertEqual(out["rest"], ["em_b"])                        # 部门里但这个对话手上没活：休息区

    def test_page_has_no_forced_rooms(self):
        office = (WEB / "lib" / "render-office.js").read_text(encoding="utf-8")
        for bad in ("未分部门", "项目组", "LOOSE_ROOM_ID"):
            self.assertNotIn(bad, office)
        for need in ("rest-area", "step-mark", "step-line", "data-step-toggle", "step-donechips", "room-fold", "lead-desk", "head-check",
                     "export function roomPlan"):            # 从上往下排：时间线、做完 / 没开工的步收成一行、正在做的步里做完的组是芯片
            self.assertIn(need, office)
        app = (WEB / "lib" / "app.js").read_text(encoding="utf-8")
        self.assertIn("roomPlan(data, pm)", app)                           # 房间和任务拆解、画布用同一份计划模型
        self.assertIn("buildPlan(data", app)
        self.assertIn("plan.rooms", app)
        self.assertIn("partChipHtml(", office)                             # 房间牌子上的编号标签和画布、任务拆解同一个


class GmRecentTests(unittest.TestCase):
    """快照 gm.recent：总经理最近做的事，只取 by=lead:* 的、有大白话的，最多 5 条、从新到旧。"""

    def setUp(self):
        import os
        import tempfile
        from unittest import mock
        from xay.core import db as core_db
        self.tmp = Path(tempfile.mkdtemp(prefix="xay-canvas-test-"))
        self.addCleanup(shutil.rmtree, self.tmp, True)
        env = mock.patch.dict(os.environ, {"XAY2_HOME": str(self.tmp / "v2")})
        env.start()
        self.addCleanup(env.stop)
        self.conn = core_db.open_db(str(self.tmp / "v2" / "xay.db"))
        self.addCleanup(self.conn.close)

    def test_lead_actions_newest_first(self):
        from xay.core import events as core_events
        from xay.core import snapshot as core_snapshot
        ws, cv, em = "ws_c0000001", "cv_c1", "em_cc000001"
        lead = "lead:" + cv
        t0 = 1793300000

        def A(typ, by, data, subject, ts):
            return core_events.append(self.conn, typ, by, data, ws=ws, conv=cv, subject=subject, ts=ts)

        A("conv.begun", lead, {"title": "画布测试", "lead_kind": "claude", "aliases": [], "cwd": "/tmp/t"}, cv, t0)
        A("dept.opened", lead, {"name": "页面部", "mission": "做页面", "owns": [], "head": "rules"}, "fe", t0 + 1)
        A("employee.hired", lead, {"agent": "codex"}, em, t0 + 2)
        A("employee.joined", lead, {"dept": "fe"}, em, t0 + 3)
        A("task.added", lead, {"key": "page", "title": "做商品页框架", "dept": "fe", "deps": [], "writes": [], "steps": [],
                               "mode": "write", "reason": "开工"}, "tk_c0000001", t0 + 10)
        A("task.assigned", "office", {"owner": em, "reason": "他空着"}, "tk_c0000001", t0 + 11)      # 管家派的：不算总经理的动作
        A("letter.sent", "employee:" + em, {"from": em, "to": "gm", "kind": "request", "subject": "要多宽", "body": "手机页面要多宽？"},
          "lt_c0000001", t0 + 20)
        A("letter.delivered", lead, {"how": "inbox"}, "lt_c0000001", t0 + 21)
        A("letter.sent", lead, {"from": "gm", "to": em, "kind": "reply", "subject": "按 375 宽", "body": "按 375 做",
                                "reply_to": "lt_c0000001"}, "lt_c0000002", t0 + 22)
        snap = core_snapshot.build(self.conn, ws=ws, conv=cv, now=t0 + 30)
        acts = snap["gm"]["recent"]
        self.assertEqual(len(acts), 5)
        self.assertEqual([a["type"] for a in acts], ["letter.sent", "letter.delivered", "task.added", "employee.joined", "employee.hired"])
        self.assertTrue(acts[0]["reply"])
        self.assertEqual(acts[0]["to"], em)
        self.assertIn("看了", acts[1]["text"])
        self.assertIn("要多宽", acts[1]["text"])
        self.assertIn("做商品页框架", acts[2]["text"])
        self.assertNotIn("task.assigned", [a["type"] for a in acts])
        self.assertEqual(snap["gm"]["state"], "idle")

    def test_no_actions_is_empty(self):
        from xay.core import snapshot as core_snapshot
        snap = core_snapshot.build(self.conn, ws=None, conv=None, now=1793300000)
        self.assertEqual(snap["gm"].get("recent"), [])


class CanvasScriptTests(unittest.TestCase):
    def test_station_markers_for_desk_fx_stay(self):
        js = (WEB / "lib" / "station.js").read_text(encoding="utf-8")
        for marker in ('class="station-card"', 'data-worker-key="', 'station-state-badge state-', 'station-worker-name name-',
                       'class="station-svg-wrap"', 'class="station-svg"', 'station-thought-bubble', 'step-badge', 'step-text'):
            self.assertIn(marker, js)
        office = (WEB / "lib" / "render-office.js").read_text(encoding="utf-8")
        self.assertIn('gm-desk-wrap', office)
        self.assertIn('id="node-head-', office)
        self.assertNotIn("<path class=\"org-line\"", office)                            # 不再有灰色主干


if __name__ == "__main__":
    unittest.main()
