"""详情栏第 3 版（2026-10-30，references/plans/ui-v2-employee-panel.md）的测试：
Codex 额度从 app-server 一路进账本（适配器 rate_limits → 工位 quota.updated → 快照 quota → 接口 person.quota），
"第几件 / 共几件"、"现在"块显示哪一件，Gemini 今天还能用几次，以及页面上的终端窗口（cli-view.js）只读、签名不变。

全部用临时目录（XAY2_HOME / XAY_HOME），不碰真实的 ~/.xay。
"""
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

V2_ROOT = Path(__file__).resolve().parents[1]
if str(V2_ROOT) not in sys.path:                 # 单独跑这一个文件（python3 -m unittest V2.tests.test_employee_panel_v3）也能导入 xay
    sys.path.insert(0, str(V2_ROOT))

from xay.adapters.codex import CodexAdapter  # noqa: E402
from xay.core import db as core_db  # noqa: E402
from xay.core import errors as core_errors  # noqa: E402
from xay.core import events as core_events  # noqa: E402
from xay.core import snapshot as core_snapshot  # noqa: E402
from xay.office import employee  # noqa: E402

WS, CV = "ws_q0000001", "cv_q1"
LEAD = "lead:" + CV
CODEX, DSH, AGY = "em_qc000001", "em_qd000001", "em_qa000001"
T0 = time.mktime((2026, 10, 30, 10, 0, 0, 0, 0, -1))
NOW = T0 + 3600
WEB = Path(__file__).resolve().parents[1] / "web"


def rate(primary=None, secondary=None, limit_id="codex", reached=None):
    out = {"limitId": limit_id, "limitName": None, "primary": primary, "secondary": secondary, "credits": None,
           "planType": "plus" if primary else None}
    if reached is not False:
        out["rateLimitReachedType"] = reached
    return {"rateLimits": out}


class CodexRateLimitTests(unittest.TestCase):
    """适配器：account/rateLimits/updated 是稀疏更新——null 的窗口保留以前的值；别的 limitId 不管。"""

    def adapter(self, line="codex-sub"):
        a = CodexAdapter()
        a._cfg = {"line": line}
        return a

    def events(self, a):
        return [e for e in a.drain() if e.kind == "rate_limits"]

    def test_windows_and_sparse_merge(self):
        a = self.adapter()
        a._on_notification("account/rateLimits/updated", rate({"usedPercent": 7, "windowDurationMins": 300, "resetsAt": 1793363679},
                                                               {"usedPercent": 64, "windowDurationMins": 10080, "resetsAt": 1793778606}))
        a._on_notification("account/rateLimits/updated", rate(None, {"usedPercent": 65, "windowDurationMins": 10080, "resetsAt": 1793778606}))
        evs = self.events(a)
        self.assertEqual(len(evs), 2)
        self.assertEqual(evs[0].data["line"], "codex-sub")
        self.assertEqual(evs[0].data["windows"], [{"key": "5h", "mins": 300, "used_pct": 7.0, "resets_at": 1793363679},
                                                  {"key": "week", "mins": 10080, "used_pct": 64.0, "resets_at": 1793778606}])
        self.assertEqual([w["used_pct"] for w in evs[1].data["windows"]], [7.0, 65.0])     # 5 小时窗没给：保留上一次的 7
        self.assertNotIn("reached", evs[1].data)

    def test_relay_nulls_give_empty_windows_and_other_limit_ids_are_ignored(self):
        a = self.adapter("codex-relay")
        a._on_notification("account/rateLimits/updated", rate(None, None))
        a._on_notification("account/rateLimits/updated", rate({"usedPercent": 50}, None, limit_id="some_other_model"))
        evs = self.events(a)
        self.assertEqual([(e.data["line"], e.data["windows"]) for e in evs], [("codex-relay", [])])

    def test_reached_is_reported(self):
        a = self.adapter()
        a._on_notification("account/rateLimits/updated", rate({"usedPercent": 100, "windowDurationMins": 300}, None, reached="rate_limit_reached"))
        self.assertEqual(self.events(a)[0].data["reached"], "rate_limit_reached")


class LedgerCase(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="xay-panel3-test-"))
        self.addCleanup(shutil.rmtree, self.tmp, True)
        env = mock.patch.dict(os.environ, {"XAY2_HOME": str(self.tmp / "v2"), "XAY_HOME": str(self.tmp / "old")})
        env.start()
        self.addCleanup(env.stop)
        os.environ.pop("XAY2_AGY_DAILY_CAP", None)
        self.conn = core_db.open_db(str(self.tmp / "v2" / "xay.db"))
        self.addCleanup(self.conn.close)
        A = self.A
        A("conv.begun", LEAD, {"title": "三家员工", "lead_kind": "claude", "aliases": [], "cwd": "/tmp/x"}, CV, T0)
        for em, agent in ((CODEX, "codex"), (DSH, "dsh"), (AGY, "agy")):
            A("employee.hired", LEAD, {"agent": agent}, em, T0)

    def A(self, typ, by, data, subject=None, ts=None):
        return core_events.append(self.conn, typ, by, data, ws=WS, conv=CV, subject=subject, ts=ts)

    def quota(self, em, data, ts):
        return self.A("quota.updated", "station:" + em, data, em, ts)


class QuotaLedgerTests(LedgerCase):
    def test_only_the_own_station_or_the_office_may_write(self):
        for by, sub in (("rules", CODEX), ("station:" + DSH, CODEX), ("user", CODEX)):
            with self.assertRaises(core_errors.PermissionError):
                self.A("quota.updated", by, {"line": "codex-sub", "windows": []}, sub, T0)
        self.quota(CODEX, {"line": "codex-sub", "windows": []}, T0)               # 自己的工位可以
        self.A("quota.updated", "office", {"line": "dsh", "windows": []}, None, T0)   # 管家定时查四条线路（2026-10-31）

    def test_snapshot_quota_takes_the_newest_per_line_and_drops_expired_windows(self):
        empty = core_snapshot.build(self.conn, conv=CV, now=NOW)["quota"]       # 四条线路总是都在，没读到就是 unknown
        self.assertEqual([(x["line"], x["status"], x["remaining_pct"]) for x in empty],
                         [("codex-sub", "unknown", None), ("codex-relay", "unknown", None), ("dsh", "unknown", None), ("agy", "unknown", None)])
        self.quota(CODEX, {"line": "codex-sub", "windows": [{"key": "5h", "mins": 300, "used_pct": 10, "resets_at": NOW + 600}]}, T0 + 1)
        self.quota(CODEX, {"line": "codex-sub", "windows": [{"key": "5h", "mins": 300, "used_pct": 20.4, "resets_at": NOW - 5},
                                                             {"key": "week", "mins": 10080, "used_pct": 36, "resets_at": NOW + 86400}]}, T0 + 2)
        self.quota(CODEX, {"line": "codex-relay", "windows": []}, T0 + 3)
        q = core_snapshot.build(self.conn, conv=CV, now=NOW)["quota"]
        self.assertEqual([x["line"] for x in q], ["codex-sub", "codex-relay", "dsh", "agy"])
        sub = q[0]
        self.assertEqual([(w["key"], w["name"], w["remaining_pct"]) for w in sub["windows"]], [("week", "本周", 64)])   # 5 小时窗已重置：不知道现在多少
        self.assertEqual((sub["remaining_pct"], sub["label"], sub["source"], sub["em"]), (64, "Codex · Plus", "live", CODEX))
        # 读数是一小时前的：还给页面看（stale），但不再当真（status=unknown，不拦活）
        self.assertEqual((sub["status"], sub["stale"], sub["source_label"], sub["short"]), ("unknown", True, "Codex 运行时上报", "Plus"))
        self.assertIsNone(q[1]["remaining_pct"])          # 工位在中转站上时通知里两个窗口都是 null：什么都没说
        # 不进现状表（reducer 跳过），重建前后一样
        self.assertNotIn("quota", json.dumps(core_events.load_state(self.conn)["employees"][CODEX]))


class PanelFieldsTests(LedgerCase):
    def task(self, tk, title, ts, owner, status_events=()):
        self.A("task.added", LEAD, {"key": tk, "title": title, "owner": owner, "deps": [], "writes": [], "steps": [], "mode": "write",
                                    "reason": "r"}, tk, ts)

    def run_turn(self, em, tk, tn, ts, ok=True, files=("a.py",)):
        st = "station:" + em
        self.A("turn.started", st, {"task": tk, "em": em, "kind": "first"}, tn, ts)
        if ok:
            self.A("turn.ended", st, {"em": em, "status": "completed", "evidence": {"cli_status": "completed", "changed_files": list(files)}}, tn, ts + 5)
            self.A("task.landed", st, {"files": list(files), "add": 1, "del": 0}, tk, ts + 6)
        else:
            self.A("turn.ended", st, {"em": em, "status": "failed", "evidence": {"cli_status": "failed", "changed_files": []}}, tn, ts + 5)
            self.A("task.failed", st, {"reason": "这一轮失败了", "detail": "Selected model is at capacity."}, tk, ts + 5)

    def build(self, em):
        return employee.build(self.conn, em, conv=CV, live_root=self.tmp / "v2" / "live", now=NOW, quota_home=self.tmp / "v2")

    def test_numbers_count_and_focus(self):
        # 第 1 件：先失败、重试做成（算一件）；第 2 件：撤回（不算）；第 3 件：正在做；第 4 件：排着队
        self.task("tk_a1", "写乘法", T0 + 1, CODEX)
        self.run_turn(CODEX, "tk_a1", "tn_a1", T0 + 2, ok=False)
        self.task("tk_a2", "写乘法", T0 + 20, CODEX)
        self.run_turn(CODEX, "tk_a2", "tn_a2", T0 + 21)
        self.task("tk_b1", "先不做的事", T0 + 30, CODEX)
        self.A("task.removed", LEAD, {"reason": "不做了"}, "tk_b1", T0 + 31)
        self.task("tk_c1", "写除法", T0 + 40, CODEX)
        self.task("tk_d1", "写取余", T0 + 41, CODEX)
        for i, tk in enumerate(("tk_c1", "tk_d1")):
            self.A("task.ready", "office", {}, tk, T0 + 42 + i)
            self.A("task.queued", "office", {"owner": CODEX, "position": i + 1}, tk, T0 + 42 + i)
        body = self.build(CODEX)
        rows = {r["task"]["short_title"]: r for r in body["timeline"]}
        self.assertEqual({t: r["n"] for t, r in rows.items()}, {"写乘法": 1, "先不做的事": None, "写除法": 2, "写取余": 3})
        self.assertEqual(rows["写乘法"]["tries"], 2)
        self.assertEqual(body["count"], {"all": 3, "done": 1, "working": 0, "queued": 2, "failed": 0, "needs_you": 0, "canceled": 1})
        self.assertEqual(body["focus"], 2)                       # 他在排队：现在块显示下一件（第 2 件）
        self.A("turn.started", "station:" + CODEX, {"task": "tk_c1", "em": CODEX, "kind": "continue"}, "tn_c1", T0 + 50)
        self.A("task.progress", "station:" + CODEX, {"step": 2, "of": 4, "text": "写测试", "source": "marker"}, "tk_c1", T0 + 51)
        body = self.build(CODEX)
        self.assertEqual(body["focus"], 2)
        self.assertEqual(body["count"]["working"], 1)
        self.assertEqual(body["person"]["progress"]["step"], 2)

    def test_focus_is_the_latest_finished_when_resting(self):
        self.task("tk_e1", "写文档", T0 + 1, DSH)
        self.run_turn(DSH, "tk_e1", "tn_e1", T0 + 2)
        self.task("tk_e2", "补测试", T0 + 10, DSH)
        self.run_turn(DSH, "tk_e2", "tn_e2", T0 + 11, ok=False)
        body = self.build(DSH)
        self.assertEqual(body["focus"], 2)
        self.assertEqual((body["count"]["done"], body["count"]["failed"]), (1, 1))
        self.assertIsNone(self.build(AGY)["focus"])              # 没派过活
        self.assertEqual(self.build(AGY)["count"]["all"], 0)

    def test_quota_for_each_kind_of_employee(self):
        # DeepSeek：管家还没查到 → 还没读到（不再是"这家看不到"，四条线路都有真数，见 test_quota_live.py）
        self.assertEqual(self.build(DSH)["person"]["quota"], {"kind": "none", "why": "还没读到", "line": "dsh", "line_name": "DeepSeek"})
        self.assertTrue(self.build(DSH)["person"]["sees_thinking"])
        # Codex：Plus 还没读到 → 读到了 → 自己报过中转站
        q = self.build(CODEX)["person"]["quota"]
        self.assertEqual((q["kind"], q["why"], q["line"], q["line_name"]), ("none", "还没读到", "codex-sub", "Plus"))
        self.quota(CODEX, {"line": "codex-sub", "windows": [{"key": "5h", "mins": 300, "used_pct": 1, "resets_at": NOW + 99},
                                                                             {"key": "week", "mins": 10080, "used_pct": 64, "resets_at": NOW + 9999}]}, T0 + 5)
        q = self.build(CODEX)["person"]["quota"]
        self.assertEqual((q["kind"], q["left"]), ("pct", 36))
        self.assertEqual([(w["name"], w["left"]) for w in q["windows"]], [("5 小时", 99), ("本周", 36)])
        self.quota(CODEX, {"line": "codex-relay", "windows": []}, T0 + 6)
        q = self.build(CODEX)["person"]["quota"]
        self.assertEqual((q["kind"], q["why"], q["line"], q["line_name"]), ("none", "还没读到", "codex-relay", "中转站"))
        # Gemini：今天用了几次（V2 的小账 + 旧系统的登记，只读；设了上限才显示）
        qdir = self.tmp / "v2" / "quota"
        qdir.mkdir(parents=True, exist_ok=True)
        today = time.strftime("%Y-%m-%d")
        with open(qdir / "agy-calls.jsonl", "w", encoding="utf-8") as fh:
            for _ in range(7):
                fh.write(json.dumps({"date": today, "em": AGY, "calls": 1}) + "\n")
            fh.write(json.dumps({"date": "2000-01-01", "em": AGY, "calls": 1}) + "\n")
        old = self.tmp / "old"
        old.mkdir(parents=True, exist_ok=True)
        (old / "registry.jsonl").write_text(json.dumps({"date": today, "kind": "agy_call", "calls": 2}) + "\n", encoding="utf-8")
        person = self.build(AGY)["person"]
        self.assertEqual(person["quota"]["kind"], "none")       # 默认不限次数（2026-10-31）：没有"今天还剩几次"
        with mock.patch.dict(os.environ, {"XAY2_AGY_DAILY_CAP": "40"}):
            person = self.build(AGY)["person"]
        self.assertEqual(person["quota"], {"kind": "calls", "left": 31, "used": 9, "cap": 40, "line": "agy", "line_name": "Gemini",
                                           "why": "还没读到"})   # 设了上限、管家还没查到 Gemini 的额度：先看今天的调用次数
        self.assertFalse(person["sees_thinking"])
        # 示例模式不读本机的次数
        demo = employee.build(self.conn, AGY, conv=CV, now=NOW)
        self.assertEqual(demo["person"]["quota"]["kind"], "none")


class PageTests(unittest.TestCase):
    def test_terminal_view_is_a_plain_script_whose_only_write_is_the_say_box(self):
        js = (WEB / "lib" / "cli-view.js").read_text(encoding="utf-8")
        for marker in ("window.XayCliView", "open: open", "mini: mini", "stop: function", "/api/v2/live?ems=", "&tail=", "等待输出…",
                       "Escape", "z-index:1000"):
            self.assertIn(marker, js)
        self.assertNotIn("plain=", js)                                      # 原样，不翻成大白话
        self.assertNotIn("plain:", js)
        for bad in ("postAction", "\nimport ", "export "):
            self.assertNotIn(bad, js)                                       # 普通脚本，不是模块
        # 唯一的写操作：放大窗口底部的输入框，把话当插话发出去（POST /api/v2/control，action=steer）；小屏、看板的列只读
        self.assertEqual(js.count("method: 'POST'"), 1)
        self.assertEqual(re.findall(r"fetch\('(/api/v2/[a-z]+)'", js), ["/api/v2/control"])
        self.assertIn("action: 'steer'", js)
        self.assertNotIn("xcv-say", js.split("function mini(")[1].split("function labelOf(")[0])
        html = (WEB / "org.html").read_text(encoding="utf-8")
        self.assertIn('<script src="/web/lib/cli-view.js"></script>', html)

    def test_panel_has_no_explaining_sentences(self):
        js = (WEB / "lib" / "employee-panel.js").read_text(encoding="utf-8")
        for gone in ("还很清醒", "还没整理过", "他自己记的，接着干活前会先看", "手上没有排着的活", "现在没在干活", "上一件做完了",
                     "派活之后，这里会实时显示", "这家 AI 看不到记忆用了多少", "工作笔记"):
            self.assertNotIn(gone, js)
        for need in ("XayCliView.open", "第 ${it.n} 件", "共 ${c.all} 件", "这家看不到", "他记住的事", "st.data.count", "st.data.focus",
                     "person.quota", "看他的屏幕"):
            self.assertIn(need, js)


@unittest.skipUnless(shutil.which("node"), "没装 node，跳过终端窗口的行为检查")
class CliViewBehaviourTests(unittest.TestCase):
    """在 node 里给 cli-view.js 一个最小的假 DOM：gm / 不认识的 key 安全返回，小屏画"等待输出…"，stop() 可重复调用。"""

    def test_gm_and_unknown_keys_are_safe(self):
        script = r"""
const fs = require('fs');
function el(tag) {
  return { tag, children: [], className: '', innerHTML: '', parentNode: null, attrs: {},
    appendChild(c) { c.parentNode = this; this.children.push(c); return c; },
    removeChild(c) { this.children = this.children.filter(x => x !== c); c.parentNode = null; },
    setAttribute(k, v) { this.attrs[k] = v; } };
}
const head = el('head');
global.document = { head, body: el('body'), getElementById: () => null, createElement: el, querySelector: () => null,
                    activeElement: null, contains: () => false };
const opened = [];
global.EventSource = function (url) { opened.push(url); this.close = () => {}; this.addEventListener = () => {}; };
global.window = { location: { search: '' }, addEventListener() {}, removeEventListener() {} };
global.URLSearchParams = URLSearchParams;
eval(fs.readFileSync(process.argv[1], 'utf8'));
const V = window.XayCliView;
const box = el('div');
const out = {};
out.openGm = V.open('gm'); out.openBad = V.open('../x'); out.openEmpty = V.open('');
const a = V.mini('gm', box);
out.gmText = box.children[0] && box.children[0].innerHTML;
a.stop(); a.stop();
out.afterStop = box.children.length;
const b = V.mini('em_aa000001', box);
out.emText = box.children[0] && box.children[0].innerHTML;
out.urls = opened;
b.stop();
out.nullBox = typeof V.mini('em_aa000001', null).stop;
console.log(JSON.stringify(out));
"""
        res = subprocess.run(["node", "-e", script, str(WEB / "lib" / "cli-view.js")], capture_output=True, text=True, timeout=30)
        self.assertEqual(res.returncode, 0, res.stderr)
        out = json.loads(res.stdout)
        self.assertEqual((out["openGm"], out["openBad"], out["openEmpty"]), (False, False, False))
        self.assertIn("等待输出…", out["gmText"])
        self.assertEqual(out["afterStop"], 0)
        self.assertIn("等待输出…", out["emText"])
        self.assertEqual(out["urls"], ["/api/v2/live?ems=em_aa000001&tail=200"])    # gm 不连；小屏没连接时马上连；员工的不带 plain
        self.assertEqual(out["nullBox"], "function")

    def test_one_shared_connection_for_every_column(self):
        """office-spec 第「二¾」节第 6 条：整个页面共用一条直播连接；要看的人变了先断开旧的再连新的，带上已经看到的位置；
        行按 em 分给各自的列；出错自己按最新位置重连；全部停掉就没有打开的连接。"""
        script = r"""
const fs = require('fs');
// 假时钟：cli-view.js 里的等待（合并 200 毫秒、出错重连）都由 tick() 推进
let now = 0, tq = [], tid = 1;
global.setTimeout = (fn, ms) => { const id = tid++; tq.push({ id, at: now + (ms || 0), fn }); return id; };
global.clearTimeout = id => { tq = tq.filter(t => t.id !== id); };
global.setInterval = () => 0; global.clearInterval = () => {};
function tick(ms) {
  const end = now + ms;
  for (;;) {
    tq.sort((a, b) => a.at - b.at);
    const t = tq[0];
    if (!t || t.at > end) break;
    tq.shift(); now = t.at; t.fn();
  }
  now = end;
}
function el(tag) {
  return { tag, children: [], className: '', innerHTML: '', textContent: '', parentNode: null, attrs: {}, q: {},
    scrollTop: 0, scrollHeight: 0, clientHeight: 0, classList: { toggle() {}, add() {}, remove() {} },
    appendChild(c) { c.parentNode = this; this.children.push(c); return c; },
    insertBefore(c, ref) { c.parentNode = this; const i = this.children.indexOf(ref);
      if (i < 0) this.children.push(c); else this.children.splice(i, 0, c); return c; },
    removeChild(c) { this.children = this.children.filter(x => x !== c); c.parentNode = null; },
    remove() { if (this.parentNode) this.parentNode.removeChild(this); },
    setAttribute(k, v) { this.attrs[k] = v; }, addEventListener() {},
    querySelector(sel) { return this.q[sel] || (this.q[sel] = el(sel)); } };
}
global.document = { head: el('head'), body: el('body'), getElementById: () => null, createElement: el, querySelector: () => null,
                    activeElement: null, contains: () => false };
const all = [];
let openWhenCreated = [];
global.EventSource = function (url) {
  openWhenCreated.push(all.filter(x => !x.closed).length);     // 新连接建起来的那一刻，还开着几条旧的
  this.url = url; this.closed = false; this.ls = {};
  this.addEventListener = (ev, fn) => { (this.ls[ev] = this.ls[ev] || []).push(fn); };
  this.close = () => { this.closed = true; };
  this.emit = (ev, data) => (this.ls[ev] || []).forEach(fn => fn({ data: JSON.stringify(data || {}) }));
  all.push(this);
};
global.window = { location: { search: '?conv=cv_1&demo=1' }, addEventListener() {}, removeEventListener() {},
                  requestAnimationFrame(fn) { fn(); return 1; } };
global.URLSearchParams = URLSearchParams;
eval(fs.readFileSync(process.argv[1], 'utf8'));
const V = window.XayCliView;
const A = 'em_aa000001', B = 'em_bb000001', C = 'em_cc000001';
const boxes = { [A]: el('div'), [B]: el('div'), [C]: el('div') };
const textOf = em => (boxes[em].children[0] ? boxes[em].children[0].q['.xcv-body'].children : []).map(c => c.innerHTML).join('\n');
const opened = () => all.filter(x => !x.closed);
const ems = es => new URL(es.url, 'http://x').searchParams.get('ems');
const out = {};
const panes = { [A]: V.pane(A, boxes[A]), [B]: V.pane(B, boxes[B]), [C]: V.pane(C, boxes[C]) };
out.beforeWait = all.length;                  // 看板一次铺好三列：等一下合并成一条
tick(250);
out.created1 = all.length;
out.url1 = all[0].url;
const es1 = all[0];
es1.emit('live', { em: A, n: 5, seg: 1, kind: 'text', text: 'A 的话', ts: 1000 });
es1.emit('live', { em: B, n: 11, seg: 3, kind: 'text', text: '只给 B 的话', ts: 1001 });
es1.emit('live', { em: B, n: 12, seg: 3, kind: 'tool', text: '运行命令：ls', ts: 1002 });
es1.emit('live', { em: B, n: 12, seg: 3, kind: 'tool', text: '运行命令：重复的一行', ts: 1003 });
es1.emit('live', { em: 'em_zz000001', n: 1, seg: 0, kind: 'text', text: '没人看的人', ts: 1004 });
out.textA = textOf(A); out.textB = textOf(B); out.textC = textOf(C);
out.lastTs = [panes[A].lastTs(), panes[B].lastTs(), panes[C].lastTs()];
panes[C].stop();
out.afterStopC = { es1Closed: es1.closed, created: all.length };   // 还没到合并的时间：先不动
tick(250);
out.es1Closed = es1.closed;
out.created2 = all.length;
const es2 = all[1];
out.url2 = es2.url;
es1.emit('live', { em: B, n: 13, seg: 3, kind: 'text', text: '旧连接晚到的一行', ts: 1005 });
out.textBAfterOld = textOf(B);
// 出错：自己关掉，2 秒后按最新位置重连（不靠浏览器带着旧网址重连）；连续失败间隔加长
es2.emit('live', { em: B, n: 13, seg: 3, kind: 'text', text: 'B 的新一行', ts: 1006 });
es2.emit('error');
out.es2ClosedOnError = es2.closed;
tick(1900); out.createdBeforeRetry = all.length;
tick(200); out.createdAfterRetry = all.length;
const es3 = all[all.length - 1];
out.url3 = es3.url;
es3.emit('error');
tick(3900); out.createdBeforeRetry2 = all.length;
tick(200); out.createdAfterRetry2 = all.length;
panes[A].stop(); panes[B].stop(); panes[B].stop();
out.openAfterStopAll = opened().length;
tick(60000);
out.createdAtEnd = all.length;
out.openAtEnd = opened().length;
out.openWhenCreated = openWhenCreated;
out.ems = all.map(ems);
console.log(JSON.stringify(out));
"""
        res = subprocess.run(["node", "-e", script, str(WEB / "lib" / "cli-view.js")], capture_output=True, text=True, timeout=30)
        self.assertEqual(res.returncode, 0, res.stderr)
        out = json.loads(res.stdout)
        # 三列同时开着：只建了一条连接，三个人都在 ems 里，示例参数照旧带上
        self.assertEqual((out["beforeWait"], out["created1"]), (0, 1))
        self.assertEqual(out["url1"], "/api/v2/live?ems=em_aa000001,em_bb000001,em_cc000001&tail=200&demo=1")
        # 行按 em 分给各自的列：B 的只进 B；重复的、没人看的人的、已经换掉的旧连接晚到的都不显示
        self.assertIn("A 的话", out["textA"])
        self.assertNotIn("B 的话", out["textA"])
        self.assertIn("只给 B 的话", out["textB"])
        self.assertIn("ls", out["textB"])
        self.assertNotIn("重复的一行", out["textB"])
        self.assertNotIn("没人看的人", out["textA"] + out["textB"] + out["textC"])
        self.assertNotIn("B 的话", out["textC"])
        self.assertEqual(out["lastTs"], [1000, 1002, 0])
        self.assertNotIn("旧连接晚到的一行", out["textBAfterOld"])
        # 关掉一列：等一下再换；旧的先断开，新的只剩两人、带上已经看到的位置
        self.assertEqual(out["afterStopC"], {"es1Closed": False, "created": 1})
        self.assertTrue(out["es1Closed"])
        self.assertEqual(out["created2"], 2)
        self.assertEqual(out["url2"], "/api/v2/live?ems=em_aa000001@1.5,em_bb000001@3.12&tail=200&demo=1")
        # 出错：马上关掉，2 秒后用最新位置重连；再出错等 4 秒
        self.assertTrue(out["es2ClosedOnError"])
        self.assertEqual((out["createdBeforeRetry"], out["createdAfterRetry"]), (2, 3))
        self.assertEqual(out["url3"], "/api/v2/live?ems=em_aa000001@1.5,em_bb000001@3.13&tail=200&demo=1")
        self.assertEqual((out["createdBeforeRetry2"], out["createdAfterRetry2"]), (3, 4))
        # 每次建新连接时旧的都已经关了：任何时候最多一条
        self.assertEqual(out["openWhenCreated"], [0, 0, 0, 0])
        # 全部停掉：没有打开的连接，之后也不再连
        self.assertEqual(out["openAfterStopAll"], 0)
        self.assertEqual((out["createdAtEnd"], out["openAtEnd"]), (4, 0))


if __name__ == "__main__":
    unittest.main()
