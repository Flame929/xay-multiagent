"""公司页怎么摆（references/plans/office-spec.md 第二节）：在真浏览器里打开管家的 /org 页面量一量。

- 从上往下一步一步排（用户选定"当前步展开、做完的折叠"）：左边步骤时间线（✓ 做完、● 正在做、○ 还没开工）；
  做完的步、还没开工的步默认收成一行（"✓ 第 1 步 · 3 组都做完" + 芯片 + ▸），点开展开房间、再点收起；
  正在做的步展开成房间网格，一行最多 2 个（屏幕够宽 3 个），这一步里做完的组用芯片放在最上面；打开页面默认 100%，看得见正在做的那一步；
- 房间按人数：1 人房间比 2 人房间窄，没有占位空框；
- 负责人：验收方式是"负责人亲自审"时，监工席正下方一个一模一样的格子（头像换成负责人）；页面上不再出现"总经理"；
- 谁坐哪：做完了留在原房间睡觉（休眠）；一个人都不剩的房间收起成一行（监工下班），点开看小任务清单；
- 连线：负责人 → 时间线 → 每一行展开的房间，收成一行的组不画线；
- 能拖动：按住空白处拖动平移、滚轮缩放，角上"适应屏幕""100%"；左右留边距；左上角"看板 / 公司页 / 任务画布"切换；
- "需要你"弹条：只显示还没交出去的；列出相关文件，点一下在页面里打开（看全文 / 看改了哪几行）；"交给监工""交给负责人"、右上角"关掉"（= 交给负责人）；
- 不抖：同一份快照再画两遍，节点是同一批、页面高度和视图（平移、缩放）不变；数据小改（某人进度变了）也只改那一处，节点不换；
- 画布：有高亮时 Esc 先取消高亮，再按才关；刷新时节点按编号复用、视图不动，数据变一处只换那一处。

要本机装了 node 和 Chrome / Chromium 才跑（用 Chrome 的远程调试接口驱动无头浏览器），否则跳过。全部用临时目录，不碰真实账本。
"""
import json
import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from xay.core import db as core_db
from xay.core import events as core_events
from xay.office import web

CHROME_CANDIDATES = [
    os.environ.get("XAY_TEST_CHROME", ""),
    "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
    "/Applications/Chromium.app/Contents/MacOS/Chromium",
    shutil.which("google-chrome") or "", shutil.which("chromium") or "", shutil.which("chromium-browser") or "",
]
CHROME = next((c for c in CHROME_CANDIDATES if c and os.path.exists(c)), "")

WS, CV = "ws_lay00001", "cv_layout"
LEAD = "lead:" + CV
EM = {"codex1": ("em_lc000001", "codex"), "agy1": ("em_la000001", "agy"), "dsh1": ("em_ld000001", "dsh"), "dsh2": ("em_ld000002", "dsh")}


def seed(conn):
    """第 1 步三组（a 定需求 / b 画草图 / c 写说明初稿，c 负责人亲自审）全做完；第 2 步三组同时做（d 做页面 / e 写说明终稿（负责人亲自审）/ f 加埋点）；
    第 3 步 g 上线还没开工。Codex 1 做完 a 去 d 干活 → a 没人了；Gemini 1 做完 b 留下睡觉；DeepSeek 1 做完 c 又排到 e → c 没人了；
    e 里 DeepSeek 2 在做、DeepSeek 1 排队（2 人）；d 只有 Codex 1（1 人）；f 的活还没人开工（没人，收起成一行）。"""
    t = [1793400000.0]

    def A(typ, by, data, subject=None):
        t[0] += 1
        return core_events.append(conn, typ, by, data, ws=WS, conv=CV, subject=subject, ts=t[0])

    A("conv.begun", LEAD, {"title": "公司页摆法测试", "lead_kind": "claude", "aliases": [], "cwd": "/tmp/layout"}, CV)
    for em, agent in EM.values():
        A("employee.hired", LEAD, {"agent": agent}, em)
    step1 = ["pt_lay0001", "pt_lay0002", "pt_lay0003"]
    step2 = ["pt_lay0004", "pt_lay0005", "pt_lay0006"]
    for pid, key, title, after, check in (
            ("pt_lay0001", "a", "定需求", [], {"kind": "rules"}), ("pt_lay0002", "b", "画草图", [], {"kind": "ai", "reviewer": "codex"}),
            ("pt_lay0003", "c", "写说明初稿", [], {"kind": "lead", "text": "看是不是大白话"}),
            ("pt_lay0004", "d", "做页面", step1, {"kind": "rules"}), ("pt_lay0005", "e", "写说明终稿", step1, {"kind": "lead"}),
            ("pt_lay0006", "f", "加埋点", step1, {"kind": "rules"}), ("pt_lay0007", "g", "上线", step2, {"kind": "rules"})):
        A("part.added", LEAD, {"key": key, "title": title, "after": after, "check": check, "batch": "b1"}, pid)

    def task(key, title, part, who):
        A("task.added", LEAD, {"key": key, "title": title, "owner": EM[who][0] if who else None, "part": part, "deps": [], "writes": [],
                               "steps": [], "mode": "write", "reason": "测试", "batch": "b1"}, "tk_" + key)

    def start(key, who):
        A("task.assigned", "office", {"owner": EM[who][0], "reason": "空着"}, "tk_" + key)
        A("turn.started", "station:" + EM[who][0], {"task": "tk_" + key, "em": EM[who][0], "kind": "first"}, "tn_" + key)

    def finish(key, who):
        em = EM[who][0]
        A("turn.ended", "station:" + em, {"em": em, "status": "completed", "evidence": {"cli_status": "completed", "changed_files": ["a.md"]}}, "tn_" + key)
        A("task.landed", "station:" + em, {"files": ["a.md"], "add": 1, "del": 0}, "tk_" + key)

    task("need", "列需求", "pt_lay0001", "codex1")
    task("sketch", "画草图", "pt_lay0002", "agy1")
    task("doc1", "写说明初稿", "pt_lay0003", "dsh1")
    task("page", "搭页面", "pt_lay0004", "codex1")
    task("doc2", "写常见问题", "pt_lay0005", "dsh2")
    task("faq", "写安装步骤", "pt_lay0005", "dsh1")
    task("track", "加埋点", "pt_lay0006", "agy1")
    task("golive", "写上线清单", "pt_lay0007", None)
    for key, who in (("need", "codex1"), ("sketch", "agy1"), ("doc1", "dsh1")):
        start(key, who)
        finish(key, who)
    start("doc2", "dsh2")
    start("page", "codex1")
    A("task.assigned", "office", {"owner": EM["dsh1"][0], "reason": "排上了"}, "tk_faq")
    A("task.queued", "office", {"owner": EM["dsh1"][0], "position": 1}, "tk_faq")
    # "需要你"：一条还没交出去（带两个文件），一条已经交给负责人了（不该出现在弹条里）
    A("decision.opened", "office", {"kind": "interrupted", "title": "DeepSeek 2 做「写常见问题」时网络断了，改好的 2 个文件还没放进项目",
                                    "detail": "你可以：放进项目 / 让他接着做 / 不要了。", "owner": "user", "task": "tk_doc2", "em": EM["dsh2"][0],
                                    "options": [{"id": "land", "label": "放进项目"}, {"id": "retry", "label": "让他接着做"}],
                                    "files": [{"path": "docs/faq.md", "kind": "change"}, {"path": "docs/index.md", "kind": "change"}]}, "dc_lay0001")
    A("decision.opened", "office", {"kind": "question", "title": "已经交出去的一条", "detail": "", "owner": "user", "delegated": "lead",
                                    "options": [{"id": "yes", "label": "好"}]}, "dc_lay0002")
    A("task.progress", "station:" + EM["dsh2"][0], {"step": 1, "of": 3, "text": "列好了要写的问题", "source": "marker"}, "tk_doc2")
    A("task.progress", "station:" + EM["codex1"][0], {"step": 1, "of": 2, "text": "页面骨架搭好了", "source": "marker"}, "tk_page")


# 在页面里跑的检查：返回一个大字典，Python 这边逐条断言
CHECKS = r"""
async () => {
  const sleep = ms => new Promise(r => setTimeout(r, ms));
  const $$ = s => [...document.querySelectorAll(s)];
  const box = el => el.getBoundingClientRect();
  const out = {};
  const vp = document.getElementById('office-viewport');
  const vpb = box(vp);
  out.view0 = window.__xayOffice.view();                 // 打开时的视图：默认 100%
  const bandInfo = () => $$('#dept-grid > .step-band').map(b => ({
    state: ['done', 'now', 'wait'].find(x => b.classList.contains('s-' + x)), open: b.classList.contains('open'),
    line: b.querySelector('.step-line') ? b.querySelector('.step-line').textContent.replace(/\s+/g, ' ').replace(/ ?[▸▾]$/, '').trim() : '',
    lineTag: b.querySelector('.step-line') ? b.querySelector('.step-line').tagName : '',
    chips: [...b.querySelectorAll('.step-head .step-chip b')].map(x => x.textContent.trim()),
    mark: !!b.querySelector('.step-mark > i'),
    markLeft: Math.round(box(b.querySelector('.step-mark > i')).left - vpb.left),
    roomsHidden: b.querySelector('.step-rooms').hidden,
    rooms: [...b.querySelector('.step-rooms').children].map(el => {
      const head = el.querySelector('.head-desk:not(.lead-desk)'), lead = el.querySelector('.lead-desk'), card = el.querySelector('.station-card');
      return {
        key: el.getAttribute('data-dept-id'), fold: el.classList.contains('room-fold'), top: Math.round(box(el).top), left: Math.round(box(el).left), width: Math.round(box(el).width),
        people: el.querySelectorAll('.station-card').length, empty: el.querySelectorAll('.dept-empty, .zone-empty').length,
        head: !!el.querySelector('.head-desk:not(.lead-desk)'), lead: !!lead, oldGm: !!el.querySelector('.head-gm'),
        leadW: lead ? Math.round(box(lead).width) : 0, cardW: card ? Math.round(box(card).width) : 0,
        leadUnderHead: !!(lead && head && Math.abs(box(lead).left - box(head).left) < 1 && box(lead).top >= box(head).bottom && box(lead).top - box(head).bottom < 24),
        headW: head ? Math.round(box(head).width) : 0, leadDesk: lead ? lead.querySelectorAll('.station-svg-wrap, svg.desk, .desk-svg').length : 0,
        leadText: lead ? [...lead.querySelectorAll('.head-title, .head-check')].map(x => x.textContent.trim()).join(' ') : '',
        text: el.textContent.replace(/\s+/g, ' ').trim().slice(0, 200)
      };
    }),
    top: Math.round(box(b).top), bottom: Math.round(box(b).bottom)
  }));
  out.bands = bandInfo();
  out.nowTop = Math.round(box(document.querySelector('#dept-grid .step-band.s-now')).top - vpb.top);
  out.vpH = Math.round(vpb.height);
  out.drops = $$('#arc-canvas g.arc-lines path.org-branch:not(.org-trunk)').map(x => x.getAttribute('data-room')).sort();
  out.visibleRooms = $$('#dept-grid .step-band.open .dept-card.room').map(x => x.getAttribute('data-dept-id')).sort();
  out.trunk = $$('#arc-canvas g.arc-lines path.org-trunk').length;
  // 点开做完的第 1 步：房间露出来（做完留下睡觉的人、没人了的收起成一行）；再点收起
  const first = document.querySelector('#dept-grid .step-band.s-done .step-line');
  if (first) { first.click(); await sleep(400); }
  out.expanded = bandInfo()[0];
  out.sleeping = $$('#dept-grid .station-card').filter(c => c.querySelector('.station-state-badge.state-sleeping')).map(c => c.getAttribute('data-worker-key'));
  out.dropsExpanded = $$('#arc-canvas g.arc-lines path.org-branch:not(.org-trunk)').length;
  const again = document.querySelector('#dept-grid .step-band.s-done .step-line');
  if (again) { again.click(); await sleep(400); }
  out.collapsedAgain = bandInfo()[0];
  out.rails = $$('#dept-grid .step-rail, #dept-grid .step-sep').length;
  out.awayText = document.getElementById('dept-grid').textContent.includes('人去别处了');
  // 负责人：顶部也叫负责人，整页不再出现"总经理"
  out.gmName = (document.querySelector('#gm-zone .gm-name') || {}).textContent || '';
  out.bossWord = document.body.innerText.includes('总经理');
  // 左上角切换：看板 / 公司页 / 任务画布；右上角不再有画布按钮
  const sw = document.querySelector('header .view-switch');
  out.switch = sw ? { left: Math.round(box(sw).left), labels: [...sw.children].map(c => c.textContent.trim()), active: (sw.querySelector('.active') || {}).textContent,
    canvasInSwitch: !!sw.querySelector('#canvas-btn'), rightCanvas: $$('.top-actions *').some(e => e.textContent.trim() === '任务画布') } : null;
  // "需要你"弹条
  const cards = $$('#decisions-bar .decision-card');
  out.decisions = cards.map(c => ({ id: c.getAttribute('data-decision-id'), files: [...c.querySelectorAll('.decision-file')].map(f => f.getAttribute('data-file')),
    delegate: [...c.querySelectorAll('.btn-delegate')].map(b => b.textContent.trim()), close: !!c.querySelector('.decision-close[data-delegate="lead"]'),
    options: [...c.querySelectorAll('.btn-decision')].map(b => b.textContent.trim()) }));
  // 正在做的步里没人的那一组（收起成一行）点开：看得到它的小任务清单
  const fold = document.querySelector('#dept-grid .step-band.s-now .room-fold .room-fold-head');
  if (fold) { fold.click(); await sleep(300); }
  const f2 = document.querySelector('#dept-grid .step-band.s-now .room-fold');
  out.foldOpen = f2 ? { open: f2.classList.contains('open'), list: f2.querySelector('.room-fold-list').textContent.replace(/\s+/g, ' ').trim() } : null;
  // 不抖：同一份快照再画两遍；再画一份小改过的（有人进度变了）。视图（平移、缩放）和页面高度都不动
  const pick = () => $$('#dept-grid .step-band, #dept-grid .dept-card.room, #dept-grid .room-fold, #dept-grid .station-card, #dept-grid .head-desk, #dept-grid .lead-desk, #dept-grid .room-head');
  const canvas = document.getElementById('office-canvas');
  const before = pick(), h0 = canvas.offsetHeight, v0 = JSON.stringify(window.__xayOffice.view()), tf0 = canvas.style.transform;
  window.__xayOffice.rerender(true); await sleep(200);
  window.__xayOffice.rerender(true); await sleep(200);
  const after = pick();
  out.same = { n: before.length, same: after.length === before.length && after.every((x, i) => x === before[i]),
    h: [h0, canvas.offsetHeight], view: [v0, JSON.stringify(window.__xayOffice.view())], tf: [tf0, canvas.style.transform] };
  const snap = window.__xayOffice.snapshot();
  Object.values(snap.people).forEach(p => { if (p.progress && p.progress.step) p.progress = Object.assign({}, p.progress, { step: 2, text: '写到第二个问题了' }); });
  snap.tasks.forEach(t => { if (t.progress && t.progress.step) t.progress = Object.assign({}, t.progress, { step: 2 }); });
  window.__xayOffice.render(snap); await sleep(200);
  const changed = pick();
  out.changed = { same: changed.length === before.length && changed.every((x, i) => x === before[i]),
    text: document.getElementById('dept-grid').textContent.includes('写到第二个问题了') };
  // 能拖动：按住空白处（内容左边那条留白）拖；滚轮缩放；适应屏幕；100%
  const home = window.__xayOffice.view();
  out.home = home;
  const x0 = vpb.left + 12, y0 = vpb.bottom - 40;
  const target = document.elementFromPoint(x0, y0);
  const pe = (type, x, y) => target.dispatchEvent(new PointerEvent(type, { bubbles: true, pointerId: 9, button: 0, isPrimary: true, pointerType: 'mouse', clientX: x, clientY: y }));
  pe('pointerdown', x0, y0); for (let i = 1; i <= 5; i++) pe('pointermove', x0 + 40 * i, y0 - 20 * i); pe('pointerup', x0 + 200, y0 - 100);
  out.dragged = window.__xayOffice.view();
  vp.dispatchEvent(new WheelEvent('wheel', { bubbles: true, cancelable: true, deltaY: -240, clientX: vpb.left + vpb.width / 2, clientY: vpb.top + vpb.height / 2 }));
  out.zoomed = window.__xayOffice.view();
  document.querySelector('.of-zoom [data-ov="fit"]').click(); await sleep(100);
  out.fit = Object.assign(window.__xayOffice.view(), { cw: Math.round(box(canvas).width), vw: Math.round(vpb.width), left: Math.round(box(canvas).left - vpb.left) });
  document.querySelector('.of-zoom [data-ov="one"]').click(); await sleep(100);
  out.one = window.__xayOffice.view();
  out.zoomLabels = $$('.of-zoom button').map(b => b.textContent.trim());
  // 弹条里的文件：点一下在页面里打开（看全文 / 看改了哪几行）
  const fileBtn = document.querySelector('#decisions-bar .decision-file');
  if (fileBtn) { fileBtn.click(); await sleep(400); }
  const fv = document.querySelector('.fv-overlay');
  out.viewer = fv ? { title: fv.querySelector('.fv-title').textContent.replace(/\s+/g, ' ').trim(), tabs: [...fv.querySelectorAll('[data-fv-tab]')].map(t => t.textContent.trim()) } : null;
  if (fv) { window.dispatchEvent(new KeyboardEvent('keydown', { key: 'Escape', bubbles: true })); await sleep(100); }
  out.viewerClosed = !document.querySelector('.fv-overlay');
  // 画布：有高亮时 Esc 先取消高亮，再按才关；刷新不抖（节点按编号复用、视图不动），数据变一处只换那一处
  window.__xayOffice.openCanvas({ parts: ['pt_lay0003'], items: [], label: '测试高亮' });
  for (let i = 0; i < 30 && !document.querySelector('.tc-overlay .tc-world > .tc-node'); i++) await sleep(100);
  await sleep(500);
  const cnodes = () => $$('.tc-overlay .tc-world > .tc-node');
  const c0 = cnodes(), cv0 = JSON.stringify(window.__xayOffice.canvasView());
  out.canvas = { n: c0.length, focus: !!document.querySelector('.tc-overlay.mode-focus') };
  window.__xayOffice.rerender(true); await sleep(250);
  window.__xayOffice.rerender(true); await sleep(250);
  const c1 = cnodes();
  out.canvas.same = c1.length === c0.length && c1.every((x, i) => x === c0[i]);
  out.canvas.view1 = [cv0, JSON.stringify(window.__xayOffice.canvasView())];
  const csnap = window.__xayOffice.snapshot();
  csnap.tasks.forEach(t => { if (t.id === 'tk_page') t.status = 'needs_you'; });
  window.__xayOffice.render(csnap); await sleep(300);
  const c2 = cnodes();
  out.canvas.changed = c2.filter(x => !c0.includes(x)).map(x => x.dataset.k);
  out.canvas.gone = c0.filter(x => !x.isConnected).length;
  out.canvas.view2 = JSON.stringify(window.__xayOffice.canvasView());
  window.dispatchEvent(new KeyboardEvent('keydown', { key: 'Escape', bubbles: true })); await sleep(250);
  out.canvas.esc1 = { open: !!document.querySelector('.tc-overlay'), focus: !!document.querySelector('.tc-overlay.mode-focus') };
  window.dispatchEvent(new KeyboardEvent('keydown', { key: 'Escape', bubbles: true })); await sleep(250);
  out.canvas.esc2 = { open: !!document.querySelector('.tc-overlay') };
  window.__xayOffice.render(window.__xayOffice.snapshot()); await sleep(200);
  // 交出去：拦下请求看发了什么（不真的写账本）
  const sent = [];
  const realFetch = window.fetch;
  window.fetch = async (u, o) => {
    if (String(u).includes('/api/v2/decision') && o && o.method === 'POST') {
      sent.push({ url: String(u), action: (o.headers || {})['X-Xay-Action'], body: JSON.parse(o.body) });
      return new Response('{"ok": true}', { status: 200, headers: { 'Content-Type': 'application/json' } });
    }
    return realFetch(u, o);
  };
  const card = document.querySelector('#decisions-bar .decision-card');
  if (card) { card.querySelector('.btn-delegate[data-delegate="rules"]').click(); await sleep(300); }
  const card2 = document.querySelector('#decisions-bar .decision-card');
  if (card2) { card2.querySelector('.decision-close').click(); await sleep(300); }
  window.fetch = realFetch;
  out.sent = sent.map(x => ({ action: x.action, id: x.body.id, delegate: x.body.delegate }));
  return out;
}
"""

# 用 Chrome 远程调试接口驱动无头浏览器：打开页面 → 等画好 → 跑检查 → 打印 JSON
DRIVER = r"""
import { spawn } from 'node:child_process';
import { mkdtempSync, rmSync } from 'node:fs';
import { join } from 'node:path';
import { tmpdir } from 'node:os';
const [chrome, url, checks] = [process.env.CHROME, process.env.URL, process.env.CHECKS];
const port = 9500 + Math.floor(Math.random() * 400);
const profile = mkdtempSync(join(tmpdir(), 'xay-lay-'));
const proc = spawn(chrome, ['--headless=new', `--remote-debugging-port=${port}`, `--user-data-dir=${profile}`, '--no-first-run',
  '--no-default-browser-check', '--hide-scrollbars', '--force-device-scale-factor=1', '--window-size=1440,900', 'about:blank'], { stdio: 'ignore' });
const sleep = ms => new Promise(r => setTimeout(r, ms));
const done = code => { try { proc.kill(); } catch (_) {} try { rmSync(profile, { recursive: true, force: true }); } catch (_) {} process.exit(code); };
let page = null;
for (let i = 0; i < 100 && !page; i++) {
  try { page = (await (await fetch(`http://127.0.0.1:${port}/json`)).json()).find(t => t.type === 'page'); } catch (_) {}
  if (!page) await sleep(150);
}
if (!page) { console.error('chrome 没起来'); done(2); }
const ws = new WebSocket(page.webSocketDebuggerUrl);
await new Promise(r => ws.addEventListener('open', r, { once: true }));
let id = 0; const pending = new Map(); const errors = [];
ws.addEventListener('message', ev => {
  const m = JSON.parse(ev.data);
  if (m.id && pending.has(m.id)) { pending.get(m.id)(m); pending.delete(m.id); }
  if (m.method === 'Runtime.exceptionThrown') errors.push(m.params.exceptionDetails.exception?.description || m.params.exceptionDetails.text);
});
const send = (method, params = {}) => new Promise(r => { const i = ++id; pending.set(i, r); ws.send(JSON.stringify({ id: i, method, params })); });
const evaluate = async expr => {
  const r = await send('Runtime.evaluate', { expression: expr, awaitPromise: true, returnByValue: true });
  if (r.result?.exceptionDetails) throw new Error(r.result.exceptionDetails.exception?.description || r.result.exceptionDetails.text);
  return r.result?.result?.value;
};
await send('Page.enable'); await send('Runtime.enable');
await send('Emulation.setDeviceMetricsOverride', { width: 1440, height: 900, deviceScaleFactor: 1, mobile: false });
await send('Page.navigate', { url });
let ready = false;
for (let i = 0; i < 80 && !ready; i++) {
  await sleep(150);
  try { ready = await evaluate("!!(window.__xayOffice && window.__xayOffice.ready() && document.querySelector('#dept-grid .step-band'))"); } catch (_) {}
}
if (!ready) { console.error('页面没画出来 ' + errors.join(' | ')); done(3); }
await sleep(500);
try {
  const out = await evaluate(`(${checks})()`);
  out.errors = errors;
  console.log(JSON.stringify(out));
  done(0);
} catch (e) { console.error(String(e)); done(4); }
"""


@unittest.skipUnless(shutil.which("node") and CHROME, "没装 node 或 Chrome，跳过真浏览器里的摆法检查")
class OfficeLayoutBrowserTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = Path(tempfile.mkdtemp(prefix="xay-layout-test-"))
        cls.env = mock.patch.dict(os.environ, {"XAY2_HOME": str(cls.tmp / "v2"), "XAY_HOME": str(cls.tmp / "old")})
        cls.env.start()
        db_path = str(cls.tmp / "v2" / "xay.db")
        conn = core_db.open_db(db_path)
        seed(conn)
        conn.close()
        cls.srv = web.OfficeServer(0, db_path=db_path, live_root=cls.tmp / "v2" / "live", heartbeat_seconds=0.5, poll_seconds=0.05).start_background()
        env = dict(os.environ, CHROME=CHROME, URL="http://127.0.0.1:%d/org?conv=%s" % (cls.srv.port, CV), CHECKS=CHECKS.strip())
        res = subprocess.run(["node", "--input-type=module", "-e", DRIVER], capture_output=True, text=True, timeout=90, env=env)
        cls.err = res.stderr
        cls.out = json.loads(res.stdout) if res.returncode == 0 and res.stdout.strip() else None

    @classmethod
    def tearDownClass(cls):
        cls.srv.stop()
        cls.env.stop()
        shutil.rmtree(cls.tmp, True)

    def setUp(self):
        if self.out is None:
            self.skipTest("页面检查没跑起来（可能是沙箱环境限制）：%s" % (self.err or "")[-300:])
        self.assertEqual(self.out["errors"], [])

    def rooms(self, bands=None):
        return {r["key"]: r for b in (bands or self.out["bands"]) for r in b["rooms"]}

    def test_steps_go_top_down_done_ones_fold_into_a_line(self):
        bands = self.out["bands"]
        self.assertEqual([b["state"] for b in bands], ["done", "now", "wait"])                                    # ✓ 做完、● 正在做、○ 还没开工
        self.assertTrue(all(b["mark"] for b in bands))                                                             # 左边时间线，每一步一个点
        self.assertEqual(len({b["markLeft"] for b in bands}), 1)
        self.assertTrue(all(a["bottom"] <= b["top"] + 1 for a, b in zip(bands, bands[1:])))                       # 从上往下一步一步排
        done, now, wait = bands
        self.assertEqual((done["open"], done["roomsHidden"], done["rooms"]), (False, True, []))                   # 做完的步默认收成一行
        self.assertTrue(done["line"].startswith("第 1 步 · 3 组都做完"), done["line"])
        self.assertEqual(done["chips"], ["1-A", "1-B", "1-C"])
        self.assertEqual(done["lineTag"], "BUTTON")                                                                # 点得开
        self.assertEqual((wait["open"], wait["rooms"]), (False, []))
        self.assertTrue(wait["line"].startswith("第 3 步 · 还没开工 · 等第 2 步"), wait["line"])
        self.assertEqual(wait["chips"], ["3"])
        self.assertTrue(now["open"])
        self.assertEqual([r["key"] for r in now["rooms"]], ["pt_lay0004", "pt_lay0005", "fold:pt_lay0006"])
        self.assertEqual(self.out["rails"], 0)                                                                     # 原来的竖轴、横向虚线都不要了
        # 点开第 1 步：房间露出来；再点收起
        exp = self.out["expanded"]
        self.assertEqual((exp["open"], exp["roomsHidden"]), (True, False))
        self.assertEqual([r["key"] for r in exp["rooms"]], ["fold:pt_lay0001", "pt_lay0002", "fold:pt_lay0003"])
        self.assertEqual(self.out["sleeping"], [EM["agy1"][0]])                                                    # 做完了留在原房间睡觉
        self.assertEqual((self.out["collapsedAgain"]["open"], self.out["collapsedAgain"]["rooms"]), (False, []))

    def test_current_step_is_a_grid_and_opens_at_100(self):
        now = self.out["bands"][1]
        rooms = now["rooms"]
        self.assertLessEqual(len({r["left"] for r in rooms}), 3)                                                  # 一行最多 3 个（这里屏幕不够宽：2 个）
        self.assertLessEqual(len({r["left"] for r in rooms}), 2)
        self.assertEqual(rooms[0]["top"], rooms[1]["top"])                                                         # 前两间同一行
        self.assertGreater(rooms[2]["top"], rooms[0]["top"])                                                       # 第三间换到下一行
        self.assertEqual(self.out["view0"]["s"], 1)                                                                # 默认 100%
        self.assertGreater(self.out["nowTop"], 0)                                                                  # 打开就看得见正在做的那一步
        self.assertLess(self.out["nowTop"], self.out["vpH"] * 0.7)
        self.assertEqual(self.out["drops"], self.out["visibleRooms"])                                              # 连线：只连展开的房间
        self.assertEqual(self.out["trunk"], 2)                                                                     # 负责人 → 时间线、时间线 → 那一行
        self.assertEqual(self.out["dropsExpanded"], 3)                                                             # 点开第 1 步后，那一步露出来的房间也连上

    def test_room_width_follows_people_and_no_empty_boxes(self):
        r = self.rooms()
        one, two = r["pt_lay0004"], r["pt_lay0005"]
        self.assertEqual((one["people"], two["people"]), (1, 2))
        self.assertLess(one["width"], two["width"])                                                                # 1 人房间比 2 人房间窄
        self.assertLess(one["width"], 360)                                                                         # 监工 + 一个工位那么宽
        self.assertEqual(sum(x["empty"] for x in r.values()), 0)                                                   # 没有占位空框
        self.assertFalse(self.out["awayText"])                                                                     # 不再写"人去别处了"

    def test_supervisor_desk_and_who_sits_where(self):
        r = self.rooms()
        lead = r["pt_lay0005"]                                                                                     # 负责人亲自审：监工席正下方一个一模一样的格子
        self.assertTrue(lead["lead"])
        self.assertFalse(lead["oldGm"])
        self.assertTrue(lead["leadUnderHead"])                                                                     # 在监工下面、左边对齐
        self.assertEqual(lead["leadW"], lead["headW"])                                                             # 和监工格子同宽
        self.assertEqual(lead["leadDesk"], 0)                                                                      # 没有电脑桌
        self.assertEqual(lead["leadText"], "负责人 亲自审这一组")
        self.assertFalse(r["pt_lay0004"]["lead"])
        self.assertEqual(self.out["gmName"].strip(), "负责人")
        self.assertFalse(self.out["bossWord"])                                                                     # 页面上不再出现"总经理"
        fold = r["fold:pt_lay0006"]                                                                                # 这一组还没人：收起成一行，监工不上班
        self.assertTrue(fold["fold"])
        self.assertFalse(fold["head"])
        self.assertEqual(fold["people"], 0)
        self.assertIn("做完 0/1 件", fold["text"])
        self.assertTrue(self.out["foldOpen"]["open"])                                                              # 点开看小任务清单
        self.assertIn("加埋点", self.out["foldOpen"]["list"])
        exp = self.rooms([self.out["expanded"]])
        self.assertIn("做完 1/1 件", exp["fold:pt_lay0001"]["text"])                                               # 做完了、人都搬走了：收起一行，监工下班
        self.assertFalse(exp["fold:pt_lay0001"]["head"])
        self.assertEqual(exp["pt_lay0002"]["people"], 1)                                                           # Gemini 1 做完留在原房间

    def test_rerender_keeps_the_same_nodes(self):
        same = self.out["same"]
        self.assertGreater(same["n"], 10)
        self.assertTrue(same["same"])                                                                              # 同一份快照再画：节点是同一批
        self.assertEqual(same["h"][0], same["h"][1])                                                               # 页面高度不变
        self.assertEqual(same["view"][0], same["view"][1])                                                         # 视图（平移、缩放）不变
        self.assertEqual(same["tf"][0], same["tf"][1])
        self.assertTrue(self.out["changed"]["same"])                                                               # 数据小改：节点不换，只改那一处
        self.assertTrue(self.out["changed"]["text"])

    def test_office_pans_zooms_and_keeps_margins(self):
        o = self.out
        self.assertEqual((o["home"]["s"], o["one"]["s"]), (1, 1))
        self.assertGreaterEqual(o["home"]["x"], 40)                                                                # 左右留边距：房间不贴边
        self.assertGreaterEqual(min(b["markLeft"] for b in o["bands"]), 39)
        self.assertGreater(o["dragged"]["x"], o["home"]["x"] + 100)                                               # 按住空白处拖：跟着手走
        self.assertLess(o["dragged"]["y"], o["home"]["y"])
        self.assertGreater(o["zoomed"]["s"], 1)                                                                    # 滚轮：缩放
        self.assertLessEqual(o["fit"]["s"], 1)                                                                     # 适应屏幕：整个放进来
        self.assertLessEqual(o["fit"]["cw"], o["fit"]["vw"])
        self.assertGreaterEqual(o["fit"]["left"], 20)
        self.assertEqual(o["zoomLabels"], ["−", "+", "适应屏幕", "100%"])

    def test_top_left_switch(self):
        sw = self.out["switch"]
        self.assertIsNotNone(sw)
        self.assertLess(sw["left"], 60)                                                                            # 左上角
        self.assertEqual(sw["labels"], ["看板", "公司页", "任务画布"])
        self.assertEqual(sw["active"], "公司页")
        self.assertTrue(sw["canvasInSwitch"])
        self.assertFalse(sw["rightCanvas"])                                                                        # 右上角不再放画布按钮

    def test_needs_you_bar_files_and_hand_off(self):
        decs = self.out["decisions"]
        self.assertEqual([d["id"] for d in decs], ["dc_lay0001"])                                                  # 交出去了的不显示
        d = decs[0]
        self.assertEqual(d["files"], ["docs/faq.md", "docs/index.md"])                                             # 列出相关文件
        self.assertEqual(d["delegate"], ["交给监工", "交给负责人"])
        self.assertTrue(d["close"])                                                                                # 右上角"关掉" = 交给负责人
        self.assertEqual(d["options"], ["放进项目", "让他接着做"])
        self.assertIsNotNone(self.out["viewer"])                                                                   # 点文件：在页面里打开
        self.assertIn("docs/faq.md", self.out["viewer"]["title"])
        self.assertEqual(self.out["viewer"]["tabs"], ["看全文", "看改了哪几行"])
        self.assertTrue(self.out["viewerClosed"])                                                                  # Esc 关掉
        self.assertEqual(self.out["sent"], [{"action": "decide", "id": "dc_lay0001", "delegate": "rules"},
                                            {"action": "decide", "id": "dc_lay0001", "delegate": "lead"}])

    def test_canvas_escape_and_no_jitter(self):
        c = self.out["canvas"]
        self.assertGreater(c["n"], 8)
        self.assertTrue(c["focus"])
        self.assertTrue(c["same"])                                                                                 # 同一份数据再刷两次：节点是同一批
        self.assertEqual(c["view1"][0], c["view1"][1])                                                             # 视图位置和缩放不变
        self.assertEqual(c["changed"], ["i:tk_page"])                                                              # 数据变一处：只换那一个节点
        self.assertEqual(c["gone"], 1)
        self.assertEqual(c["view2"], c["view1"][0])
        self.assertEqual(c["esc1"], {"open": True, "focus": False})                                                # 有高亮：Esc 先取消高亮
        self.assertEqual(c["esc2"], {"open": False})                                                               # 再按 Esc 才关画布



# ===================== 看板（office-spec.md 二¾）：三个页面左上角切换同一个位置、右上角工具、修改文件 =====================

# 每页都先放进去的小工具
PAGE_HELPERS = r"""
const sleep = ms => new Promise(r => setTimeout(r, ms));
const $$ = s => [...document.querySelectorAll(s)];
const rect = el => { if (typeof el === 'string') el = document.querySelector(el); if (!el) return null; const b = el.getBoundingClientRect(); return { left: b.left, top: b.top, width: b.width, height: b.height }; };
const sw = () => { const s = document.querySelector('.view-switch'); return s ? { rect: rect(s), labels: [...s.children].map(c => c.textContent.trim()),
  active: (s.querySelector('.active') || {}).textContent || '', hrefs: [...s.children].map(c => c.getAttribute('href')) } : null; };
// 切换条以外、看得见的、字里带"公司"的按钮 / 链接（左边对话列表里的对话标题不算）
const companyButtons = () => $$('a, button').filter(e => !e.closest('.view-switch, .side-tree') && e.getClientRects().length && e.textContent.includes('公司')).map(e => e.textContent.trim());
const tools = () => $$('.conv-bar .tools > *').filter(e => e.getClientRects().length).map(e => e.textContent.trim());
const waitFor = async (fn, ms = 4000) => { for (let i = 0; i < ms / 50; i++) { try { const v = fn(); if (v) return v; } catch (_) {} await sleep(50); } return null; };
"""

# 一页一页打开：{name, url（同一页里只换 # 也行）, ready（等到为真）, check（async 函数，返回结果）, nav（check 里点了链接、要等新页面）}
BOARD_PAGES = [
    {"name": "org", "url": "/org?conv=" + CV, "ready": "!!(window.__xayOffice && window.__xayOffice.ready())",
     "check": "async () => ({ sw: sw(), back: document.getElementById('back-link').getAttribute('href') })"},
    {"name": "watch", "url": "/#/c/" + CV, "ready": "!!document.querySelector('#conv-tabs .tab') && !!document.getElementById('files-btn')",
     "check": r"""async () => {
       const out = { sw: sw(), tools: tools(), company: companyButtons(), cli: rect('#cli-btn'), files: rect('#files-btn'),
         cliHref: document.getElementById('cli-btn').getAttribute('href'), side: rect('.side-top') };
       document.getElementById('files-btn').click(); await sleep(300);
       out.hash = location.hash;
       out.rows = $$('.frow').map(r => ({ path: r.dataset.path, who: r.querySelectorAll('.fwho .w').length, state: r.querySelector('.fst').textContent }));
       document.querySelector('.frow').click();
       await waitFor(() => $$('#dock-body .seg-body').every(b => !b.querySelector('.muted')) && $$('#dock-body .seg').length);
       out.segs = $$('#dock-body .seg').map(x => ({ head: x.querySelector('.seg-head').textContent, note: (x.querySelector('.seg-note') || {}).textContent || '', add: x.querySelectorAll('.add').length }));
       out.dockShown = document.getElementById('dock').classList.contains('show');
       return out; }"""},
    {"name": "watchEmpty", "url": "/#/c/cv_nofiles/files", "ready": "!!document.getElementById('files-btn') && !!document.querySelector('.fview')",
     "check": "async () => { await sleep(200); const e = document.querySelector('.fview .empty'); return { empty: e && !e.hidden ? e.textContent.trim() : '', rows: $$('.frow').length, filesOn: document.getElementById('files-btn').classList.contains('on') }; }"},
    {"name": "watchHome", "url": "/#/", "ready": "!!document.querySelector('.home-list')",
     "check": "async () => ({ sw: sw() })"},
    {"name": "watchDemo", "url": "/?demo=1", "ready": "!!document.querySelector('#conv-tabs .tab') && !!document.getElementById('files-btn')",
     "check": r"""async () => {
       const out = { sw: sw(), tools: tools(), company: companyButtons(), cli: rect('#cli-btn'), files: rect('#files-btn'), cliHref: document.getElementById('cli-btn').getAttribute('href') };
       // 不抖：开关"修改文件"时下面的员工列不拆、滚动位置不变；数据刷新时文件列表、右侧面板都是同一批节点
       const col = document.querySelector('#conv-cols .col.active .col-body');
       col.scrollTop = 120; await sleep(50);
       const colTop = col.scrollTop, cols = $$('#conv-cols .col, #conv-cols .thread');
       document.getElementById('files-btn').click(); await sleep(300);
       out.filesOn = { btn: document.getElementById('files-btn').classList.contains('on'), list: getComputedStyle(document.querySelector('.fview')).display, cols: getComputedStyle(document.getElementById('conv-cols')).display };
       out.rows = $$('.frow').map(r => ({ path: r.dataset.path, who: r.querySelectorAll('.fwho .w').length, state: r.querySelector('.fst').textContent }));
       const rows0 = $$('.frow');
       await window.__xayWatch.refetch(); await sleep(300);
       out.rowsSame = rows0.every(r => r.isConnected && $$('.frow').includes(r));
       // 每个文件都点开看：右侧面板里有加的行（绿）
       out.diffs = {};
       for (const r of $$('.frow')) {
         r.click();
         await waitFor(() => document.getElementById('dock').classList.contains('show') && $$('#dock-body .seg').length && !document.querySelector('#dock-body .seg-body .muted'));
         out.diffs[r.dataset.path] = $$('#dock-body .seg').map(x => ({ head: x.querySelector('.seg-head').textContent.replace(/\s+/g, ' ').trim(),
           add: x.querySelectorAll('.add').length, del: x.querySelectorAll('.del').length, hunk: x.querySelectorAll('.hunk').length }));
       }
       document.querySelector('.frow[data-path="references/plans/ui-v5-api.md"]').click();
       await waitFor(() => $$('#dock-body .seg').length === 2 && !document.querySelector('#dock-body .seg-body .muted'));
       out.sel = (document.querySelector('.frow.sel') || {}).dataset;
       out.sel = out.sel ? out.sel.path : '';
       const segs0 = $$('#dock-body .seg, #dock-body .seg-body > div');
       await window.__xayWatch.refetch(); await sleep(400);
       out.segsSame = segs0.every(x => x.isConnected);
       document.getElementById('files-btn').click(); await sleep(300);
       out.filesOff = { dock: document.getElementById('dock').classList.contains('show'), cols: getComputedStyle(document.getElementById('conv-cols')).display,
         colsSame: cols.every(x => x.isConnected), colTop: [colTop, document.querySelector('#conv-cols .col.active .col-body').scrollTop], hash: location.hash };
       return out; }"""},
    {"name": "board", "url": "/board?conv=" + CV, "ready": "!!document.querySelector('#tabs .tab')",
     "check": r"""async () => ({ sw: sw(), tools: tools(), company: companyButtons(), cli: rect('#cli-btn'), files: rect('#files-btn'),
       cliOn: document.getElementById('cli-btn').classList.contains('on'), cliPressed: document.getElementById('cli-btn').getAttribute('aria-pressed'),
       cliHref: document.getElementById('cli-btn').getAttribute('href'), filesHref: document.getElementById('files-btn').getAttribute('href'),
       old: !!document.getElementById('org-btn') || !!document.getElementById('side-org'), side: rect('.side-top') })"""},
    {"name": "boardDemo", "url": "/board?demo=1", "ready": "!!document.querySelector('#tabs .tab')",
     "check": r"""async () => ({ sw: sw(), cli: rect('#cli-btn'), files: rect('#files-btn'), cliHref: document.getElementById('cli-btn').getAttribute('href'),
       filesHref: document.getElementById('files-btn').getAttribute('href'), company: companyButtons() })"""},
    # 纯 CLI 页上再点"纯 CLI"：回到对话版看板（示例：那边认不得这边的示例对话，换成它自己的第一个）
    {"name": "boardBack", "click": "#cli-btn", "ready": "location.pathname === '/' && !!document.querySelector('#conv-tabs .tab')",
     "check": "async () => ({ path: location.pathname, search: location.search, hash: location.hash })"},
]

PAGES_DRIVER = r"""
import { spawn } from 'node:child_process';
import { mkdtempSync, rmSync } from 'node:fs';
import { join } from 'node:path';
import { tmpdir } from 'node:os';
const [chrome, base, pages, helpers] = [process.env.CHROME, process.env.BASE, JSON.parse(process.env.PAGES), process.env.HELPERS];
const port = 9500 + Math.floor(Math.random() * 400);
const profile = mkdtempSync(join(tmpdir(), 'xay-board-'));
const proc = spawn(chrome, ['--headless=new', `--remote-debugging-port=${port}`, `--user-data-dir=${profile}`, '--no-first-run',
  '--no-default-browser-check', '--hide-scrollbars', '--force-device-scale-factor=1', '--window-size=1440,900', 'about:blank'], { stdio: 'ignore' });
const sleep = ms => new Promise(r => setTimeout(r, ms));
const done = code => { try { proc.kill(); } catch (_) {} try { rmSync(profile, { recursive: true, force: true }); } catch (_) {} process.exit(code); };
let page = null;
for (let i = 0; i < 100 && !page; i++) {
  try { page = (await (await fetch(`http://127.0.0.1:${port}/json`)).json()).find(t => t.type === 'page'); } catch (_) {}
  if (!page) await sleep(150);
}
if (!page) { console.error('chrome 没起来'); done(2); }
const ws = new WebSocket(page.webSocketDebuggerUrl);
await new Promise(r => ws.addEventListener('open', r, { once: true }));
let id = 0; const pending = new Map(); const errors = [];
ws.addEventListener('message', ev => {
  const m = JSON.parse(ev.data);
  if (m.id && pending.has(m.id)) { pending.get(m.id)(m); pending.delete(m.id); }
  if (m.method === 'Runtime.exceptionThrown') errors.push(m.params.exceptionDetails.exception?.description || m.params.exceptionDetails.text);
});
const send = (method, params = {}) => new Promise((r, j) => { const i = ++id; const t = setTimeout(() => { pending.delete(i); j(new Error('超时：' + method)); }, 20000);
  pending.set(i, m => { clearTimeout(t); r(m); }); ws.send(JSON.stringify({ id: i, method, params })); });
const evaluate = async expr => {
  const r = await send('Runtime.evaluate', { expression: expr, awaitPromise: true, returnByValue: true });
  if (r.result?.exceptionDetails) throw new Error(r.result.exceptionDetails.exception?.description || r.result.exceptionDetails.text);
  return r.result?.result?.value;
};
await send('Page.enable'); await send('Runtime.enable');
await send('Emulation.setDeviceMetricsOverride', { width: 1440, height: 900, deviceScaleFactor: 1, mobile: false });
await send('Emulation.setEmulatedMedia', { features: [{ name: 'prefers-color-scheme', value: 'dark' }] });
const out = {};
try {
  for (const p of pages) {
    if (p.url) await send('Page.navigate', { url: base + p.url });
    if (p.click) await evaluate(`document.querySelector(${JSON.stringify(p.click)}).click()`);
    let ready = false;
    for (let i = 0; i < 100 && !ready; i++) { await sleep(150); try { ready = await evaluate(p.ready); } catch (_) {} }
    if (!ready) throw new Error(p.name + ' 页面没画出来 ' + errors.join(' | '));
    await sleep(400);
    out[p.name] = await evaluate(`(async () => { ${helpers}\n return await (${p.check})(); })()`);
  }
  out.errors = errors;
  console.log(JSON.stringify(out));
  done(0);
} catch (e) { console.error(String(e)); done(4); }
"""


def seed_board(conn):
    """公司页那套数据，再加一个还没改过文件的对话（修改文件里应该写"这个对话还没改过文件"）。"""
    seed(conn)
    core_events.append(conn, "conv.begun", "lead:cv_nofiles", {"title": "还没改文件的对话", "lead_kind": "claude", "aliases": [], "cwd": "/tmp/layout"},
                       ws=WS, conv="cv_nofiles", subject="cv_nofiles", ts=1793400100.0)


@unittest.skipUnless(shutil.which("node") and CHROME, "没装 node 或 Chrome，跳过真浏览器里的看板检查")
class BoardSwitchBrowserTests(unittest.TestCase):
    """office-spec.md 二¾：看板对话版（/）、纯 CLI 版（/board）、公司页（/org）左上角切换同一个位置；右上角"纯 CLI""修改文件"；修改文件看 diff。"""

    @classmethod
    def setUpClass(cls):
        cls.tmp = Path(tempfile.mkdtemp(prefix="xay-board-test-"))
        cls.env = mock.patch.dict(os.environ, {"XAY2_HOME": str(cls.tmp / "v2"), "XAY_HOME": str(cls.tmp / "old")})
        cls.env.start()
        db_path = str(cls.tmp / "v2" / "xay.db")
        conn = core_db.open_db(db_path)
        seed_board(conn)
        conn.close()
        cls.srv = web.OfficeServer(0, db_path=db_path, live_root=cls.tmp / "v2" / "live", heartbeat_seconds=0.5, poll_seconds=0.05).start_background()
        env = dict(os.environ, CHROME=CHROME, BASE="http://127.0.0.1:%d" % cls.srv.port, PAGES=json.dumps(BOARD_PAGES), HELPERS=PAGE_HELPERS)
        res = subprocess.run(["node", "--input-type=module", "-e", PAGES_DRIVER], capture_output=True, text=True, timeout=180, env=env)
        cls.err = res.stderr
        cls.out = json.loads(res.stdout) if res.returncode == 0 and res.stdout.strip() else None

    @classmethod
    def tearDownClass(cls):
        cls.srv.stop()
        cls.env.stop()
        shutil.rmtree(cls.tmp, True)

    def setUp(self):
        if self.out is None:
            self.skipTest("看板检查没跑起来（可能是沙箱环境限制）：%s" % (self.err or "")[-300:])
        self.assertEqual(self.out["errors"], [])

    def assertSameBox(self, a, b, msg=""):
        for k in ("left", "top", "width", "height"):
            self.assertLessEqual(abs(a[k] - b[k]), 1, "%s %s: %s vs %s" % (msg, k, a, b))

    def test_switch_is_in_the_same_place_on_all_three_pages(self):
        o = self.out
        org = o["org"]["sw"]["rect"]
        self.assertLess(org["left"], 60)
        for name in ("watch", "watchHome", "watchDemo", "board", "boardDemo"):
            s = o[name]["sw"]
            self.assertEqual(s["labels"], ["看板", "公司页", "任务画布"], name)
            self.assertEqual(s["active"], "看板", name)                                                          # 当前页那个按钮高亮
            self.assertSameBox(s["rect"], org, name)                                                             # 左上角坐标、宽、高都和公司页一样
        self.assertEqual(o["org"]["sw"]["active"], "公司页")
        for name in ("watch", "board"):                                                                          # 我的事 / 搜索框排在切换下面
            self.assertGreaterEqual(o[name]["side"]["top"], org["top"] + org["height"])

    def test_switch_links_follow_the_conversation(self):
        o = self.out
        self.assertEqual(o["watch"]["sw"]["hrefs"], [None, "/org?conversation=" + CV, "/org?conversation=%s&canvas=1" % CV])
        self.assertEqual(o["board"]["sw"]["hrefs"], [None, "/org?conversation=" + CV, "/org?conversation=%s&canvas=1" % CV])
        self.assertEqual(o["watchHome"]["sw"]["hrefs"], [None, "/org", "/org?canvas=1"])                          # 没选对话
        self.assertEqual(o["watchDemo"]["sw"]["hrefs"], [None, "/org?demo=1", "/org?canvas=1&demo=1"])             # 示例模式带上 demo
        self.assertTrue(all("demo=1" in h for h in o["boardDemo"]["sw"]["hrefs"][1:]))
        self.assertEqual(o["org"]["back"], "/#/c/" + CV)                                                         # 公司页"看板"回到同一个对话

    def test_top_right_tools_have_cli_and_files_but_no_company(self):
        o = self.out
        for name in ("watch", "watchDemo"):
            self.assertEqual(o[name]["tools"][-3:], ["阶段概览", "纯 CLI", "修改文件"], name)
            self.assertEqual(o[name]["company"], [], name)                                                       # 找不到"公司"按钮
        self.assertEqual(o["board"]["tools"][-2:], ["纯 CLI", "修改文件"])
        self.assertEqual(o["board"]["company"], [])
        self.assertEqual(o["boardDemo"]["company"], [])
        self.assertFalse(o["board"]["old"])                                                                      # 侧栏、顶部的"公司页"都去掉了
        self.assertEqual(o["watch"]["cliHref"], "/board?conv=" + CV)                                            # 纯 CLI：同一个对话的 /board
        self.assertEqual(o["watchDemo"]["cliHref"], "/board?demo=1")
        self.assertTrue(o["board"]["cliOn"])                                                                     # 纯 CLI 页上是按下的
        self.assertEqual(o["board"]["cliPressed"], "true")
        self.assertEqual(o["board"]["cliHref"], "/#/c/" + CV)                                                    # 再点回到对话版的同一个对话
        self.assertEqual(o["board"]["filesHref"], "/#/c/%s/files" % CV)
        self.assertTrue(o["boardDemo"]["cliHref"].startswith("/?demo=1#/c/"))
        for a, b in (("watch", "board"), ("watchDemo", "boardDemo")):                                           # 两边按钮在同一个位置
            self.assertSameBox(o[a]["cli"], o[b]["cli"], a + " 纯 CLI")
            self.assertSameBox(o[a]["files"], o[b]["files"], a + " 修改文件")
        back = o["boardBack"]                                                                                    # 纯 CLI 页上再点：回到对话版（连接先放掉，不卡住）
        self.assertEqual((back["path"], back["search"]), ("/", "?demo=1"))
        self.assertTrue(back["hash"].startswith("#/c/"))

    def test_files_view_lists_every_changed_file_and_shows_diffs(self):
        d = self.out["watchDemo"]
        self.assertEqual(d["filesOn"], {"btn": True, "list": "block", "cols": "none"})
        rows = {r["path"]: r for r in d["rows"]}
        self.assertGreaterEqual(len(rows), 3)
        self.assertEqual(len(rows), len(d["rows"]))                                                             # 按文件归并：同一个文件只出现一次
        self.assertTrue(all(r["state"] in ("已放进项目", "还在员工桌上", "等你决定", "等负责人处理", "已丢弃") for r in d["rows"]))
        self.assertEqual(rows["references/plans/ui-v5-api.md"]["who"], 2)                                      # 被两件小任务先后改过
        for path, segs in d["diffs"].items():                                                                    # 每个都能点开看到彩色 diff
            self.assertTrue(segs, path)
            self.assertTrue(all(s["add"] > 0 and s["hunk"] > 0 for s in segs), (path, segs))
        two = d["diffs"]["references/plans/ui-v5-api.md"]
        self.assertEqual(len(two), 2)                                                                            # 一段一段列，按时间先后
        self.assertIn("复核", two[0]["head"])
        self.assertIn("数据层", two[1]["head"])
        self.assertIn("第 1 轮", two[0]["head"])
        self.assertTrue(any(s["del"] > 0 for s in two))
        self.assertEqual(d["sel"], "references/plans/ui-v5-api.md")
        w = self.out["watch"]                                                                                    # 新版数据：同一个文件被三件小任务改过
        self.assertEqual(w["hash"], "#/c/%s/files" % CV)
        self.assertEqual([(r["path"], r["who"], r["state"]) for r in w["rows"]], [("a.md", 3, "已放进项目")])
        self.assertTrue(w["dockShown"])
        self.assertEqual(len(w["segs"]), 3)
        self.assertTrue(all(s["note"] and s["add"] == 0 for s in w["segs"]), w["segs"])                         # 拿不到改动：写说明，不报错
        self.assertIn("Codex 1", w["segs"][0]["head"])
        empty = self.out["watchEmpty"]                                                                           # 直接带 /files 打开；没改过文件
        self.assertTrue(empty["filesOn"])
        self.assertEqual((empty["empty"], empty["rows"]), ("这个对话还没改过文件", 0))

    def test_files_view_does_not_jitter(self):
        d = self.out["watchDemo"]
        self.assertTrue(d["rowsSame"])                                                                           # 数据刷新：文件列表还是同一批节点
        self.assertTrue(d["segsSame"])                                                                           # 右侧面板里的几段也不重建
        off = d["filesOff"]
        self.assertFalse(off["dock"])                                                                            # 关上修改文件：右侧面板一起关
        self.assertEqual(off["cols"], "flex")
        self.assertTrue(off["colsSame"])                                                                         # 员工列没拆，原样回来
        self.assertEqual(off["colTop"][0], off["colTop"][1])                                                     # 滚动位置不变
        self.assertFalse(off["hash"].endswith("/files"))


if __name__ == "__main__":
    unittest.main()
