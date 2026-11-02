// employee-panel.js - 左侧"员工详情"栏第 3 版（设计：references/plans/ui-v2-employee-panel.md）。点工位或名字打开，和右侧栏同宽同风格。
// 只摆数字和事实，不写说明句；拿不到写"—"，这家给不出来的写"这家看不到"（现在只有 Gemini 的"在想"）。从上到下：
//   名片：整身像淡背景 + 名字 + "Gemini · 文档部" + 一个状态词
//   现在：第几件 / 共几件 + 状态词 → 这件活 → 第几步 → 在想 / 在做（干活时）或 汇报 + 文件（结束后）→ 看他的屏幕
//   额度 ｜ 记忆：剩多少（额度是管家查到的真数：Codex 按他现在的线路、DeepSeek、Gemini 当前账号 + 别的账号）
//   小任务 · 共 N 件：一行一件（序号 + 状态 + 短标题），点开看做成了什么 / 汇报、要求、经过、文件、步骤、原话
//   他记住的事（折叠）
// 文字由服务端 GET /api/v2/employee 翻好；状态、记忆、第几步跟着快照实时变；在想 / 在做走直播通道（plain=1）。
// "看他的屏幕"调用 window.XayCliView.open（cli-view.js，原样的命令行）。
// 2026-10-31 补两块（方块由这里插进页面，org.html 不用改）：名片下面"大任务"（一行概括 + 按钮，点开全屏"任务画布" task-canvas.js），
// 记忆下面"token 消耗 / 速度"（token-meter.js）。
import { escapeHtml, getAgentType, AI_NAMES, formatClock, formatDuration, shortTitle, dutyText, spanText, isTechy, thoughtWords } from './utils.js';
import { fetchEmployee, connectLive, postAction } from './api.js';
import { resetText, agoText } from './quota-view.js';
import { summaryHtml, openTaskCanvas, updateTaskCanvas, closeTaskCanvas } from './task-canvas.js';
import { tokenHtml } from './token-meter.js';

const REFRESH_DELAY = 400;
const WORKING_POLL = 15000;               // 他在干活时每隔这么久重拉一次（token 速度、方格跟着动）
const CHAR_IMG = '/xay-chars/img/';

const DONE = new Set(['done', 'reported']);
const WAITING = new Set(['queued', 'planned', 'ready']);
// 每件活的状态：一个词 + 颜色 + 清单里的小图标
const TASK_WORD = {
  done: ['做完了', 'ok', '✓'], reported: ['做完了', 'ok', '✓'], working: ['正在做', 'run', '●'], needs_you: ['等你决定', 'warn', '!'],
  failed: ['没做成', 'err', '✕'], canceled: ['撤回了', 'off', '–'], queued: ['排队中', 'wait', '○'], planned: ['排队中', 'wait', '○'],
  ready: ['排队中', 'wait', '○']
};

const st = {
  key: '', data: null, error: '', loading: false, reqId: 0, sig: '', timer: 0,
  open: new Set(), showCanceled: false, notesOpen: true, notesEdit: false, notesErr: '', acctOpen: false,
  stopLive: null, liveFor: '', live: { think: '', doing: '' }, liveQueued: false, artAgent: '',
  poll: 0, cliOpenEm: ''
};
let getSnapshot = () => null;

const $ = id => document.getElementById(id);

// ---------------------------------------------------------------- 打开 / 关闭

export function initEmployeePanel({ snapshot }) {
  getSnapshot = snapshot || getSnapshot;
  addTile('emp-tree', 'emp-tile emp-tree', '大任务', 'emp-id');
  addTile('emp-tokens', 'emp-tile emp-tokens', 'token', 'emp-mem');

  document.addEventListener('click', e => {
    if (e.defaultPrevented) return;
    const link = e.target.closest('[data-person]');
    const card = e.target.closest('.station-card[data-worker-key]');
    const key = link ? link.getAttribute('data-person') : (card ? card.getAttribute('data-worker-key') : '');
    if (key) {
      if (window.getSelection && String(window.getSelection()).length > 0) return;   // 正在选字复制，不打断
      openEmployee(key);
    }
  });

  const closeBtn = $('emp-close-btn');
  if (closeBtn) closeBtn.addEventListener('click', closeEmployee);
  document.addEventListener('keydown', e => {
    if (e.key === 'Escape' && st.key) closeEmployee();
  });

  const scroll = $('emp-scroll');
  if (scroll) scroll.addEventListener('click', onClick);
  const notes = $('emp-notes');
  if (notes) notes.addEventListener('toggle', () => { st.notesOpen = notes.open; });

  if (typeof window !== 'undefined' && window.addEventListener) {
    window.addEventListener('xay:cli-open', e => {
      st.cliOpenEm = (e && e.detail && e.detail.em) || '';
      if (st.key && st.cliOpenEm === st.key) {
        stopLive();
      }
    });
    window.addEventListener('xay:cli-close', e => {
      const closedEm = (e && e.detail && e.detail.em) || '';
      if (!closedEm || st.cliOpenEm === closedEm) {
        st.cliOpenEm = '';
        if (st.key) {
          syncLive(personOf());
        }
      }
    });
  }
}

export function openEmployee(key) {
  if (!key) return;
  const panel = $('emp-panel');
  if (!panel) return;
  const same = st.key === key;
  st.key = key;
  panel.classList.remove('collapsed');
  highlight(key);
  if (!same) {
    st.data = null; st.error = ''; st.open.clear(); st.showCanceled = false; st.notesOpen = true; st.notesEdit = false; st.notesErr = '';
    st.live = { think: '', doing: '' };
    closeTaskCanvas(false);
    stopLive();
    renderAll();
    const scroll = $('emp-scroll');
    if (scroll) scroll.scrollTop = 0;
  }
  st.sig = signature(getSnapshot());
  load(false);
}

export function closeEmployee() {
  const panel = $('emp-panel');
  if (panel) panel.classList.add('collapsed');
  st.key = '';
  st.reqId++;
  clearTimeout(st.timer);
  closeTaskCanvas(false);
  stopLive();
  syncPoll(false);
  highlight('');
}

// 名片下面的"总任务"、记忆下面的"token"：页面里没有就插进去（先藏着，有数据才露出来）
function addTile(id, cls, label, afterId) {
  if ($(id)) return;
  const after = $(afterId);
  if (!after || !after.parentNode) return;
  const el = document.createElement('section');
  el.id = id;
  el.className = cls;
  el.setAttribute('aria-label', label);
  el.hidden = true;
  after.insertAdjacentElement('afterend', el);
}

// 他在干活时隔一会儿重拉一次详情（token 速度只在接口里算，快照里没有）
function syncPoll(on) {
  if (on && !st.poll) st.poll = setInterval(() => { if (st.key && !document.hidden) load(false); }, WORKING_POLL);
  else if (!on && st.poll) { clearInterval(st.poll); st.poll = 0; }
}

export function isOpen() {
  return !!st.key;
}

function highlight(key) {
  let style = $('emp-hl-style');
  if (!style) {
    style = document.createElement('style');
    style.id = 'emp-hl-style';
    document.head.appendChild(style);
  }
  style.textContent = key && window.CSS && CSS.escape
    ? `[data-worker-key="${CSS.escape(key)}"].station-card { border-color: var(--accent); box-shadow: 0 0 0 2px color-mix(in srgb, var(--accent) 35%, transparent); }`
    : '';
}

// ---------------------------------------------------------------- 取数据 / 跟着刷新

async function load(showLoading) {
  const key = st.key;
  if (!key) return;
  const id = ++st.reqId;
  if (showLoading || !st.data) { st.loading = true; renderData(); }
  try {
    const data = await fetchEmployee(key);
    if (id !== st.reqId || key !== st.key) return;
    st.data = data; st.error = '';
  } catch (err) {
    if (id !== st.reqId || key !== st.key) return;
    st.error = err.message || String(err);
  }
  st.loading = false;
  renderAll();
}

function scheduleRefresh() {
  if (!st.key) return;
  clearTimeout(st.timer);
  st.timer = setTimeout(() => load(false), REFRESH_DELAY);
}

// 能反映"这个人的事有变化"的那部分快照；变了才重新拉详情，别人的动静不打扰他
function signature(snap) {
  if (!snap || !st.key) return '';
  const p = (snap.people || {})[st.key];
  const mine = (snap.tasks || []).filter(t => t.owner === st.key);
  return JSON.stringify([
    p ? [p.state, p.display_state, p.now, p.progress, p.context && p.context.pct, p.context && p.context.segment, p.dept, p.online, p.queue] : null,
    mine.map(t => [t.id, t.status, t.result, t.progress, t.dept]),
    snap.quota || [],
    (snap.tasks || []).map(t => [t.id, t.status, t.owner, t.dept, t.progress && t.progress.step, t.progress && t.progress.of])   // 总任务树：别人的活也算
  ]);
}

/** 每次页面拿到新快照后调用。 */
export function onSnapshot(snap) {
  if (!st.key) return;
  const sig = signature(snap);
  if (sig !== st.sig) {
    st.sig = sig;
    scheduleRefresh();
  }
  renderTop();
}

/** 事件流里每条"有大白话"的事件：和这个人有关的，马上拉一次。 */
export function onLedgerItem(item) {
  if (!st.key || !item) return;
  if ([item.em, item.from, item.to, item.owner, item.subject].includes(st.key)) scheduleRefresh();
}

// ---------------------------------------------------------------- 数据整理

// 快照里的状态最新（记忆、在不在干活、第几步、预算）；文字（这件活的短标题、那一步一句话）用接口里翻好的
function personOf() {
  const snap = getSnapshot();
  const live = snap && snap.people ? snap.people[st.key] : null;
  const api = st.data && st.data.person;
  if (!live) return api || null;
  const p = Object.assign({}, api || {}, live);
  const prog = live.progress || {};
  if (api) {
    p.now = api.now;
    p.dept_name = api.dept_name;
    const same = api.progress && api.progress.step === prog.step && api.progress.of === prog.of;
    p.progress = Object.assign({}, prog, { text: same ? (api.progress.text || '') : '' });
  } else {
    p.now = shortTitle(live.now);
    p.progress = Object.assign({}, prog, { text: '' });
  }
  if (!p.dept_name && p.dept && snap) p.dept_name = ((snap.depts || []).find(d => d.id === p.dept) || {}).name || '';
  return p;
}

function nowTs() {
  return (st.data && st.data.now) || (getSnapshot() && getSnapshot().now) || Date.now() / 1000;
}

function items() {
  return (st.data && st.data.timeline) || [];
}

function counts() {
  const c = st.data && st.data.count;
  if (c) return c;
  const out = { all: 0, done: 0, working: 0, queued: 0, failed: 0, needs_you: 0, canceled: 0 };
  items().forEach(it => {
    const s = it.task && it.task.status;
    if (s === 'canceled') { out.canceled++; return; }
    out.all++;
    if (DONE.has(s)) out.done++;
    else if (WAITING.has(s)) out.queued++;
    else if (out[s] !== undefined) out[s]++;
  });
  return out;
}

// "现在"块显示的那一件（服务端 focus 指定；快照说他在干活、接口还没跟上时先用正在做的那件）
function focusItem(p) {
  const all = items();
  const f = st.data && st.data.focus;
  let it = f ? all.find(x => x.n === f) : null;
  if (p && stateOf(p) === 'working') {
    const cur = all.find(x => x.task && x.task.status === 'working');
    if (cur) it = cur;
  }
  return it || null;
}

function stateOf(p) {
  if (!p) return 'idle';
  if (p.fired) return 'fired';
  return p.display_state || p.state || 'idle';
}

function word(status) {
  return TASK_WORD[status] || ['', 'wait', '○'];
}

function titleOf(it) {
  const t = (it && it.task) || {};
  return t.short_title || shortTitle(t.title) || '任务';
}

function minutesText(sec) {
  const m = Math.round(sec / 60);
  if (m < 1) return '';
  return m < 90 ? `${m} 分钟` : `${Math.round(m / 6) / 10} 小时`;
}

// 名片上的状态词（一个词，干活时带上干了多久）
function stateWord(p) {
  const state = stateOf(p);
  switch (state) {
    case 'fired': return ['已离职', 'off'];
    case 'working': {
      const cur = items().find(it => it.task && it.task.status === 'working');
      const since = cur && cur.task.started ? minutesText(nowTs() - cur.task.started) : '';
      return [since ? `干活中 · ${since}` : '干活中', 'run'];
    }
    case 'compacting': return ['整理记忆', 'mem'];
    case 'queued': return ['排队中', 'warn'];
    case 'blocked': return [items().some(it => it.task && it.task.status === 'needs_you') ? '等你决定' : '卡住了', 'err'];
    case 'waiting': return ['等别人', 'warn'];
    case 'failed': return ['没做成', 'err'];
    case 'offline': return ['下班了', 'off'];
    default: return ['休息中', 'off'];
  }
}

function pct(n) {
  return Math.min(100, Math.max(0, Math.round(Number(n) || 0)));
}

// ---------------------------------------------------------------- 画面：上半（快照一变就重画）

function renderAll() {
  renderTop();
  renderData();
}

function renderTop() {
  const title = $('emp-title');
  const p = personOf();
  if (title) title.textContent = p && p.label ? p.label : '员工详情';
  if (!st.key || !p) {
    ['emp-id-body', 'emp-now', 'emp-mem', 'emp-quota'].forEach(id => { const el = $(id); if (el) el.innerHTML = ''; });
    ['emp-tree', 'emp-tokens'].forEach(id => { const el = $(id); if (el) { el.hidden = true; el.innerHTML = ''; } });
    return;
  }
  const type = getAgentType(p.agent);
  setArt(type);
  renderIdCard(p, type);
  renderNow(p);
  renderQuota(p);
  renderMemory(p);
  renderTokens(p);
  syncLive(p);
  syncPoll(stateOf(p) === 'working');
}

// ---------------------------------------------------------------- 总任务 / token（接口数据：tree、person.tokens）

function personType(em) {
  const snap = getSnapshot();
  const p = snap && snap.people ? snap.people[em] : null;
  const f = !p && snap ? (snap.former || []).find(x => x.key === em) : null;
  return getAgentType((p || f || {}).agent);
}

// "总任务"块：一行概括（共几件 · 做完几件 · 他负责几件）+ "展开任务画布"按钮；画布开着时跟着新数据重画
function renderTree() {
  const el = $('emp-tree');
  if (!el) return;
  const html = st.key && st.data ? summaryHtml(st.data.tree) : '';
  el.hidden = !html;
  if (el.innerHTML !== html) el.innerHTML = html;
  if (st.key && st.data) updateTaskCanvas({ tree: st.data.tree }, canvasOpts());
}

/** 现在打开着的员工详情拿到的任务树（"任务拆解"打开画布时 /api/v2/plan 读不到就借它用；没打开谁返回 null）。 */
export function currentTree() {
  return st.key && st.data && st.data.tree ? st.data.tree : null;
}

// 员工编号 → 名字（画布上"谁审查"写成名字）
function personName(em) {
  const snap = getSnapshot();
  const p = snap && snap.people ? snap.people[em] : null;
  const f = !p && snap ? (snap.former || []).find(x => x.key === em) : null;
  return (p || f || {}).label || '';
}

function canvasOpts(opener) {
  const p = personOf();
  return Object.assign({ em: st.key, name: (p && p.label) || '他', typeOf: personType, nameOf: personName }, opener ? { opener } : {});
}

function openCanvas(btn) {
  if (st.key && st.data) openTaskCanvas({ snap: getSnapshot(), tree: st.data.tree }, canvasOpts(btn));
}

function renderTokens(p) {
  const el = $('emp-tokens');
  if (!el) return;
  const tok = st.data && st.data.person ? st.data.person.tokens : null;
  const html = st.key && p ? tokenHtml(tok, stateOf(p) === 'working') : '';
  el.hidden = !html;
  if (el.innerHTML !== html) el.innerHTML = html;
}

function setArt(type) {
  const card = $('emp-id');
  const box = card && card.querySelector('.emp-art');
  if (!card || !box) return;
  const a = type === 'gm' ? 'claude' : (['codex', 'dsh', 'agy', 'claude', 'rules'].includes(type) ? type : 'codex');
  card.className = `emp-tile emp-id who-${type}`;
  if (st.artAgent === a && box.firstChild) return;
  st.artAgent = a;
  box.innerHTML = '';
  const img = new Image();
  img.alt = '';
  img.decoding = 'async';
  img.addEventListener('load', () => img.classList.add('ready'));
  img.addEventListener('error', () => {
    if (!img.dataset.fallback) {          // 整身图还没有：退回头像
      img.dataset.fallback = '1';
      img.classList.add('fallback');
      img.src = `${CHAR_IMG}${a}-calm.webp`;
    } else {
      img.remove();                       // 头像也没有：什么都不画，不留破图标
    }
  });
  img.src = `${CHAR_IMG}${a}-full.webp`;
  box.appendChild(img);
}

function renderIdCard(p, type) {
  const el = $('emp-id-body');
  if (!el) return;
  const [w, tone] = stateWord(p);
  el.innerHTML = `
    <div class="emp-id-name name-${type}">${escapeHtml(p.label || p.key)}</div>
    <div class="emp-id-meta"><span>${escapeHtml(AI_NAMES[type] || 'AI')}</span><span aria-hidden="true">·</span><span>${escapeHtml(p.dept_name || '未分部门')}</span></div>
    <div class="emp-chip c-${tone}"><span class="emp-sdot" aria-hidden="true"></span>${escapeHtml(w)}</div>
    ${dutyHtml(p)}`;
}

// 在岗时间（快照 people[员工].duty 或员工接口 person.duty）："在岗 2 小时 10 分 · 今天干活 48 分"；没有就不显示
function dutyHtml(p) {
  const duty = (p && p.duty) || (st.data && st.data.person && st.data.person.duty) || null;
  const text = dutyText(duty, nowTs());
  if (!text) return '';
  const total = duty && Number(duty.total_work_s) > 0 ? `一共干活 ${spanText(duty.total_work_s)}` : '';
  return `<div class="emp-duty${duty.online_since ? '' : ' off'}" title="${escapeHtml(['在岗时间', text, total].filter(Boolean).join('\n'))}"><span class="emp-duty-ic" aria-hidden="true"></span>${escapeHtml(text)}${total ? `<small>${escapeHtml(total)}</small>` : ''}</div>`;
}

function filesHtml(files, max = 8) {
  if (!files || !files.length) return '';
  const shown = files.slice(0, max);
  const more = files.length - shown.length;
  return `<div class="emp-files">${shown.map(f => `<span class="emp-file">${escapeHtml(f)}</span>`).join('')}${more > 0 ? `<span class="emp-file more">+${more}</span>` : ''}</div>`;
}

function segmentsHtml(step, of, finished) {
  const n = Math.max(1, Math.min(12, Number(of) || 0));
  const s = Math.max(0, Math.min(n, Number(step) || 0));
  let out = '';
  for (let i = 1; i <= n; i++) out += `<i class="${i < s || (finished && i <= s) ? 'on' : i === s ? 'cur' : ''}"></i>`;
  return `<div class="emp-segs" aria-hidden="true">${out}</div>`;
}

// 这件活报到第几步：正在做的用快照里这个人的进度（实时），别的用快照里这件活最后报的进度
function stepOf(it, p, working) {
  const snap = getSnapshot();
  const prog = working ? (p.progress || {}) : ((((snap && snap.tasks) || []).find(t => it && t.id === it.task.id) || {}).progress || {});
  if (prog.step === undefined || prog.of === undefined || !(Number(prog.of) > 0)) return null;
  return { step: Number(prog.step), of: Number(prog.of), text: working ? (p.progress.text || '') : '' };
}

function screenBtn() {
  const ok = window.XayCliView && st.key && st.key !== 'gm';
  return ok ? '<button type="button" class="emp-screen" data-act="screen"><span class="emp-screen-ic" aria-hidden="true">&gt;_</span>看他的屏幕</button>' : '';
}

function renderNow(p) {
  const el = $('emp-now');
  if (!el) return;
  if (!st.data && !st.error) {
    el.innerHTML = '<div class="emp-muted">读取中…</div>';
    return;
  }
  const state = stateOf(p);
  const c = counts();
  const it = focusItem(p);
  const working = state === 'working';
  if (!it && !working) {
    el.innerHTML = `
      <div class="now-head"><span class="now-n">共 0 件</span></div>
      <div class="emp-now-title">还没派小任务</div>
      ${screenBtn()}`;
    return;
  }
  const status = it ? it.task.status : 'working';
  const [w, tone] = working && status !== 'working' ? word('working') : word(status);
  const t = it ? (it.task || {}) : {};
  const clock = !working && (DONE.has(status) || status === 'failed' || status === 'needs_you') ? formatClock(t.ended || t.started, nowTs()) : '';
  const title = it && (status === 'working' || !working) ? titleOf(it) : (p.now || '正在干活');
  const step = stepOf(it && (status === 'working' || !working) ? it : null, p, working);
  const finished = !working && (DONE.has(status) || status === 'failed' || status === 'needs_you');
  const bits = finished ? reportBits(it) : null;
  const report = bits && (bits.plain || bits.raw);
  const sees = !(st.data && st.data.person && st.data.person.sees_thinking === false);
  el.innerHTML = `
    <div class="now-head">
      <span class="now-n">${it && it.n ? `第 ${it.n} 件 <small>/ 共 ${c.all} 件</small>` : `共 ${c.all} 件`}</span>
      <span class="emp-pill p-${tone}">${escapeHtml(w)}${clock ? ` · ${escapeHtml(clock)}` : ''}</span>
    </div>
    <div class="emp-now-title">${escapeHtml(title)}</div>
    ${step ? `
    <div class="now-step">${segmentsHtml(step.step, step.of, DONE.has(status))}<span>第 ${escapeHtml(step.step)} 步 / 共 ${escapeHtml(step.of)} 步</span></div>
    ${step.text ? `<div class="now-step-text">${escapeHtml(step.text)}</div>` : ''}` : ''}
    ${working ? `
    <div class="emp-live" id="emp-live" aria-live="polite" data-sees="${sees ? '1' : '0'}"></div>` : ''}
    ${report ? `
    <div class="now-report"><div class="now-lab">${bits.plain ? '做成了什么' : '汇报'}</div>${reportHtml(bits, 'now-report-text')}${filesHtml(it.files, 6)}</div>` : ''}
    ${screenBtn()}`;
  if (working) paintLive();
}

// 快照里这件小任务（大白话 plain、AI 自己拆的步骤 planned 都在快照的 tasks[] 上）
function snapTask(it) {
  const id = it && it.task && it.task.id;
  const snap = getSnapshot();
  return id && snap ? (snap.tasks || []).find(t => t.id === id) || null : null;
}

function plannedOf(it) {
  const v = (snapTask(it) || {}).planned;
  return Array.isArray(v) ? v.filter(Boolean) : typeof v === 'string' && v.trim() ? v.split(/\n+/).filter(Boolean) : [];
}

// "做成了什么"：员工写的大白话（plain，监工把过关）优先；原文放进"技术细节"。没有大白话、原文里路径 / 函数名 / 命令多的也折起来
function reportBits(it) {
  const r = (it && it.result) || {};
  const plain = String((it && it.plain) || r.plain || (snapTask(it) || {}).plain || '').trim();
  return { plain, raw: String(r.summary || r.short || '').trim() };
}

function reportHtml(bits, cls) {
  if (bits.plain) {
    return `<div class="${cls}">${escapeHtml(bits.plain)}</div>` + (bits.raw && bits.raw !== bits.plain
      ? `<details class="pd-tech"><summary>技术细节（员工汇报原文）</summary><div class="${cls}">${escapeHtml(bits.raw)}</div></details>` : '');
  }
  if (bits.raw && isTechy(bits.raw)) {
    return `<details class="pd-tech"><summary>汇报里技术细节比较多，点开看原文</summary><div class="${cls}">${escapeHtml(bits.raw)}</div></details>`;
  }
  return `<div class="${cls}">${escapeHtml(bits.raw || '—')}</div>`;
}

function levelOf(left) {
  if (left === null || left === undefined) return 'none';
  return left < 15 ? 'danger' : left < 35 ? 'warn' : 'normal';
}

// 额度格（2026-10-31 起四条线路都是真数，管家定时查）：Codex 按他现在的线路（Plus：5 小时 / 本周；中转站：今天 + 还能用多少钱），
// DeepSeek：5 小时 / 本周 / 本月，Gemini：当前账号 + 剩余（点开看别的账号）+ 今天还能调几次
function renderQuota(p) {
  const el = $('emp-quota');
  if (!el) return;
  const q = (st.data && st.data.person && st.data.person.quota) || null;
  const now = nowTs();
  const b = p.budget || {};
  const budget = b.dim && Number(b.pct) > 0
    ? `<div class="emp-sub">${b.scope === 'employee' ? '今天预算' : '这件小任务预算'}用了 ${pct(b.pct)}%</div>` : '';
  const head = aside => `<div class="tile-head"><span class="tile-label">额度</span>${aside ? `<span class="tile-aside" title="${escapeHtml(aside)}">${escapeHtml(aside)}</span>` : ''}</div>`;
  const callsLine = c => c && c.cap ? `<div class="emp-sub" title="每天最多 ${c.cap} 次">今天还能调 ${Math.max(0, Number(c.left) || 0)} 次</div>` : '';
  if (!q) {
    el.className = 'emp-tile emp-quota half';
    el.innerHTML = `${head('')}<div class="emp-big none">—</div>${budget}`;
    return;
  }
  if (q.kind === 'pct') {
    const left = pct(q.left);
    const gone = q.status === 'exhausted';
    const lv = gone ? 'danger' : q.status === 'unknown' ? 'none' : levelOf(left);
    const wins = (q.windows || []).map(w => `${w.name} ${pct(w.left)}%`).join(' · ');
    const tight = (q.windows || []).filter(w => w.resets_at).sort((a, b2) => a.left - b2.left)[0];
    const reset = !tight ? '' : gone ? `<div class="emp-sub">${escapeHtml(resetText(tight.resets_at, now))} 恢复</div>`
      : `<div class="emp-sub">${escapeHtml(tight.name)} ${escapeHtml(resetText(tight.resets_at, now))} 重置</div>`;
    const money = q.balance ? `<div class="emp-sub">${escapeHtml(q.balance)}</div>` : '';
    const when = q.stale ? `<div class="emp-sub q-old">${escapeHtml(agoText(q.fetched_at, now))}的读数</div>` : '';
    const err = q.error ? `<div class="emp-sub q-old" title="${escapeHtml(q.error)}">最近没查到</div>` : '';
    el.className = `emp-tile emp-quota half lv-${lv}`;
    el.title = q.fetched_at ? `${agoText(q.fetched_at, now)}查的${q.source ? `（${q.source}）` : ''}` : '';
    el.innerHTML = `
      ${head([q.line_name, q.account].filter(Boolean).join(' · '))}
      <div class="emp-big">${gone ? '用完了' : `<small>剩</small>${left}<small>%</small>`}</div>
      <div class="emp-track"><div class="emp-fill" style="width:${left}%"></div></div>
      ${wins ? `<div class="emp-sub">${escapeHtml(wins)}</div>` : ''}${reset}${money}${callsLine(q.calls)}${when}${err}${accountsHtml(q, now)}${budget}`;
    return;
  }
  el.title = '';
  if (q.kind === 'calls') {
    const cap = Math.max(1, Number(q.cap) || 40);
    const left = Math.max(0, Number(q.left) || 0);
    const leftPct = pct(left * 100 / cap);
    el.className = `emp-tile emp-quota half lv-${levelOf(leftPct)}`;
    el.innerHTML = `
      ${head('今天')}
      <div class="emp-big"><small>还能用</small>${left}<small>次</small></div>
      <div class="emp-track"><div class="emp-fill" style="width:${leftPct}%"></div></div>
      <div class="emp-sub">上限 ${cap} 次</div>${q.why ? `<div class="emp-sub q-old">额度${escapeHtml(q.why)}</div>` : ''}${accountsHtml(q, now)}${budget}`;
    return;
  }
  el.className = 'emp-tile emp-quota half';
  el.innerHTML = `${head(q.line_name || '')}<div class="emp-big none">—</div><div class="emp-sub">${escapeHtml(q.why || '')}</div>${budget}`;
}

// Gemini 别的账号：默认收着，点"N 个账号"展开（名字 + 剩多少，用完的标红）
function accountsHtml(q, now) {
  const list = (q && q.accounts) || [];
  if (list.length < 2) return '';
  const ok = list.filter(a => a.status === 'ok' || a.status === 'tight').length;
  const rows = st.acctOpen ? `<ul class="emp-accts">${list.map(a => {
    const val = a.status === 'blocked' ? '停用' : a.left === null || a.left === undefined ? '—' : `${pct(a.left)}%`;
    const tip = a.resets_at ? `${resetText(a.resets_at, now)} 重置` : '';
    return `<li class="acct-${escapeHtml(a.status || 'unknown')}${a.current ? ' cur' : ''}" title="${escapeHtml(tip)}">
      <span class="acct-name">${escapeHtml(a.name)}${a.current ? '<small>现在用的</small>' : ''}</span><b>${val}</b></li>`;
  }).join('')}</ul>` : '';
  return `<button type="button" class="emp-link emp-acct-btn" data-act="accts" aria-expanded="${st.acctOpen ? 'true' : 'false'}">${list.length} 个账号 · ${ok} 个还有${st.acctOpen ? ' ▴' : ' ▾'}</button>${rows}`;
}

function renderMemory(p) {
  const el = $('emp-mem');
  if (!el) return;
  const ctx = p.context || {};
  const n = Number(ctx.compactions) || 0;
  const head = `<div class="tile-head"><span class="tile-label">记忆</span>${n ? `<span class="tile-aside">整理 ${n} 次</span>` : ''}</div>`;
  if (ctx.pct === undefined || ctx.pct === null) {
    el.className = 'emp-tile emp-mem half';
    el.innerHTML = `${head}<div class="emp-big none">—</div>`;
    return;
  }
  const left = 100 - pct(ctx.pct);
  el.className = `emp-tile emp-mem half lv-${levelOf(left)}`;
  el.innerHTML = `
    ${head}
    <div class="emp-big"><small>${ctx.estimated || ctx.calibrating ? '约剩' : '剩'}</small>${left}<small>%</small></div>
    <div class="emp-track"><div class="emp-fill" style="width:${left}%"></div></div>`;
}

// ---------------------------------------------------------------- 画面：下半（接口数据变了才重画）

function renderData() {
  renderWork();
  renderNotes();
  renderTree();
  const p = personOf();
  if (st.key && p) {                      // "现在"块和额度要用接口里的数据：到了也重画一次
    renderNow(p);
    renderQuota(p);
    renderTokens(p);
  }
}

function detailHtml(it) {
  const t = it.task || {};
  const r = it.result || {};
  const [w, tone] = word(t.status);
  const title = titleOf(it);
  const same = a => (a || '').replace(/[。.！!？?\s]/g, '') === title.replace(/[。.！!？?\s]/g, '');
  const attempts = it.attempts || [];
  const keySteps = it.key_steps || [];
  const steps = it.steps || [];
  const start = formatClock(t.started, nowTs());
  const when = [start ? `${start} 开始` : '', formatDuration(t.started, t.ended)].filter(Boolean).join('，');
  const raw = it.task && it.task.id;
  const rawOpen = st.open.has('raw:' + raw);
  const bits = reportBits(it);
  return `
    <div class="work-detail">
      <div class="d-block">
        <div class="d-label">${bits.plain ? '做成了什么' : '汇报'} <span class="emp-pill p-${tone}">${escapeHtml(w || '—')}</span></div>
        ${reportHtml(bits, 'd-text')}
      </div>
      ${it.ask && !same(it.ask) ? `
      <div class="d-block">
        <div class="d-label">要求</div>
        <div class="d-text">${escapeHtml(it.ask)}</div>
      </div>` : ''}
      ${attempts.length > 1 ? `
      <div class="d-block">
        <div class="d-label">经过</div>
        <ol class="d-attempts">
          ${(() => {
            let n = 0;                      // 只给真的动手做过的那几次编号；派了没开始就撤回的不算一次
            return attempts.map(a => {
              const [, aTone] = word(a.status);
              const c = formatClock(a.started, nowTs());
              const label = a.ran ? `第 ${++n} 次` : '撤回';
              return `<li><span class="work-dot t-${aTone}" aria-hidden="true"></span><span><b>${label}</b>${c ? `<em>${escapeHtml(c)}</em>` : ''}${escapeHtml(a.note || word(a.status)[0])}</span></li>`;
            }).join('');
          })()}
        </ol>
      </div>` : ''}
      ${(it.files || []).length ? `
      <div class="d-block">
        <div class="d-label">文件</div>
        ${filesHtml(it.files, 30)}
      </div>` : ''}
      ${keySteps.length ? `
      <div class="d-block">
        <div class="d-label">${plannedOf(it).length ? '他自己拆的步骤' : '步骤'}</div>
        <ul class="d-steps">${keySteps.map(s => `<li>${escapeHtml(s)}</li>`).join('')}</ul>
      </div>` : ''}
      <button type="button" class="d-raw-btn" data-act="raw" data-id="${escapeHtml(raw)}" aria-expanded="${rawOpen}">${rawOpen ? '收起原话' : '原话'}</button>
      ${rawOpen ? `
      <div class="d-raw">
        ${r.detail ? `<pre class="d-pre">${escapeHtml(r.detail)}</pre>` : '<div class="d-label">—</div>'}
        ${steps.length ? `<div class="d-label">全部 ${steps.length} 步</div>
        <ol class="d-allsteps">${steps.map(s => `<li>${escapeHtml(s.text)}</li>`).join('')}</ol>` : ''}
        <div class="d-meta">${t.legacy ? '旧系统 · ' : ''}${t.dept_name ? escapeHtml(t.dept_name) + ' · ' : ''}${when ? escapeHtml(when) + ' · ' : ''}${escapeHtml(raw)}</div>
      </div>` : ''}
    </div>`;
}

function rowHtml(it, focusN) {
  const t = it.task || {};
  const id = String(t.id || '');
  const open = st.open.has(id);
  const [w, tone, icon] = word(t.status);
  const clock = formatClock(t.ended || t.started, nowTs());
  const sub = [t.status === 'needs_you' || t.status === 'failed' ? w : '', (it.tries || 0) > 1 ? `试了 ${it.tries} 次` : ''].filter(Boolean).join(' · ');
  const cur = focusN && it.n === focusN;
  return `
    <li class="work-item${open ? ' open' : ''}${cur ? ' cur' : ''}${t.status === 'canceled' ? ' muted' : ''}">
      <button type="button" class="work-row" data-act="row" data-id="${escapeHtml(id)}" aria-expanded="${open}" title="${escapeHtml(w)}">
        <span class="work-ic t-${tone}" aria-label="${escapeHtml(w)}">${icon}</span>
        <span class="work-n">${it.n || ''}</span>
        <span class="work-title">${t.legacy ? '<span class="legacy-tag" title="旧系统">旧</span>' : ''}${escapeHtml(titleOf(it))}</span>
        <span class="work-time">${escapeHtml(clock)}</span>
        ${sub ? `<span class="work-sub t-${tone}">${escapeHtml(sub)}</span>` : ''}
      </button>
      ${open ? detailHtml(it) : ''}
    </li>`;
}

function renderWork() {
  const el = $('emp-work');
  const scroll = $('emp-scroll');
  if (!el) return;
  if (!st.key) { el.innerHTML = ''; return; }
  const top = scroll ? scroll.scrollTop : 0;
  const head = (label, aside) => `<div class="tile-head"><span class="tile-label">${label}</span>${aside ? `<span class="tile-aside">${escapeHtml(aside)}</span>` : ''}</div>`;
  if (st.error && !st.data) {
    el.innerHTML = `${head('小任务', '')}<div class="emp-error"><div>读不到：${escapeHtml(st.error)}</div><button type="button" class="emp-link" data-act="retry">重试</button></div>`;
    return;
  }
  if (!st.data) {
    el.innerHTML = `${head('小任务', '')}<div class="emp-muted emp-pad">读取中…</div>`;
    return;
  }
  const c = counts();
  const all = items();
  const rows = all.filter(it => it.task && it.task.status !== 'canceled').sort((a, b) => (a.n || 0) - (b.n || 0));
  const canceled = all.filter(it => it.task && it.task.status === 'canceled');
  const aside = [c.done ? `做完 ${c.done}` : '', c.working ? `在做 ${c.working}` : '', c.queued ? `排队 ${c.queued}` : '',
                 c.needs_you ? `等你 ${c.needs_you}` : '', c.failed ? `没做成 ${c.failed}` : ''].filter(Boolean).join(' · ');
  const hidden = c.all - rows.length;
  const focusN = st.data.focus || 0;
  el.innerHTML = `
    ${head(`小任务 · 共 ${c.all} 件`, aside)}
    ${st.error ? `<div class="emp-error small">刷新失败：${escapeHtml(st.error)}</div>` : ''}
    ${hidden > 0 ? `<div class="work-more static">前面还有 ${hidden} 件</div>` : ''}
    ${rows.length ? `<ul class="work-list">${rows.map(it => rowHtml(it, focusN)).join('')}</ul>` : (canceled.length ? '' : '<div class="emp-muted emp-pad">还没派小任务</div>')}
    ${canceled.length ? `
      <button type="button" class="work-more" data-act="canceled" aria-expanded="${st.showCanceled}">${st.showCanceled ? '收起' : `撤回 ${canceled.length} 件`}</button>
      ${st.showCanceled ? `<ul class="work-list">${canceled.map(it => rowHtml(it, 0)).join('')}</ul>` : ''}` : ''}`;
  if (scroll) scroll.scrollTop = top;
}

// 他记住的事：完整笔记（默认展开）+ 整理记忆的历史 + 最近一次交接摘要；可以直接改（写账本，by=用户）
const METHOD_TEXT = { native: '自带压缩', native_auto: '它自己压的', handoff: '交接', machine: '机器交接' };
const BY_TEXT = { 用户: '你', 负责人: '负责人', 工位: '工位', 员工: '他自己' };

function memPct(v) {
  return typeof v === 'number' ? Math.round(v) + '%' : '—';
}

function renderNotes(force = false) {
  const el = $('emp-notes');
  if (!el) return;
  if (st.notesEdit && !force && $('emp-notes-ta')) return;     // 正在改：定时刷新不动它，免得打的字没了
  const p = st.data && st.data.person;
  if (!st.key || !p) { el.hidden = true; el.innerHTML = ''; return; }
  const mem = st.data.memory || {};
  const notes = (mem.notes != null ? mem.notes : p.notes) || '';
  const comps = mem.compactions || [];
  const lines = notes.split('\n').map(x => x.trim()).filter(Boolean);
  const who = mem.notes_by ? (BY_TEXT[mem.notes_by] || mem.notes_by) : '';
  const when = mem.notes_ts ? formatClock(mem.notes_ts) : '';
  const aside = [lines.length ? lines.length + ' 条' : '', when ? when + (who ? ' · ' + who + '改的' : '') : ''].filter(Boolean).join(' · ');
  el.hidden = false;
  el.open = st.notesOpen;
  const canEdit = !p.fired;
  let body;
  if (st.notesEdit) {
    body = `
      <textarea class="emp-notes-edit" id="emp-notes-ta" maxlength="6000" aria-label="改他记住的事">${escapeHtml(notes)}</textarea>
      <div class="emp-notes-actions">
        ${st.notesErr ? `<span class="emp-notes-err">${escapeHtml(st.notesErr)}</span>` : ''}
        <button type="button" class="emp-btn ghost" data-act="notes-cancel">取消</button>
        <button type="button" class="emp-btn" data-act="notes-save">保存</button>
      </div>`;
  } else {
    body = (lines.length
      ? `<ul class="emp-notes-list">${lines.map(l => `<li>${escapeHtml(l.replace(/^[-*·•]\s*/, ''))}</li>`).join('')}</ul>`
      : '<div class="emp-notes-empty">—</div>')
      + (canEdit ? '<div class="emp-notes-actions"><button type="button" class="emp-btn ghost" data-act="notes-edit">编辑</button></div>' : '');
  }
  const compHtml = comps.length ? `
    <div class="emp-mem-sub">整理记忆 · ${comps.length} 次</div>
    <div class="emp-mem-comps">${comps.slice(-6).reverse().map(c => `
      <span class="mem-chip m-${escapeHtml(c.method || '')}" title="第 ${c.n} 次 · ${escapeHtml(formatClock(c.ts))}">
        <b>#${c.n}</b>${escapeHtml(METHOD_TEXT[c.method] || c.method || '')}<i>${memPct(c.before_pct)} → ${memPct(c.after_pct)}</i></span>`).join('')}
    </div>` : '';
  const handoffHtml = mem.handoff ? `
    <details class="emp-mem-handoff"><summary>最近一次交接摘要</summary><pre>${escapeHtml(mem.handoff)}</pre></details>` : '';
  el.innerHTML = `
    <summary><span class="tile-label">他记住的事</span><span class="tile-aside">${escapeHtml(aside)}</span></summary>
    ${body}${compHtml}${handoffHtml}`;
  if (st.notesEdit) {
    const ta = $('emp-notes-ta');
    if (ta && document.activeElement !== ta) { ta.focus(); ta.setSelectionRange(ta.value.length, ta.value.length); }
  }
}

async function saveNotes() {
  const ta = $('emp-notes-ta');
  if (!ta || !st.key) return;
  const key = st.key;
  st.notesErr = '';
  try {
    await postAction('notes', 'notes', { em: key, text: ta.value }, '没保存上');
    if (key !== st.key) return;
    st.notesEdit = false;
    await load(false);
  } catch (err) {
    st.notesErr = err.message || String(err);
    renderNotes(true);
  }
}

function onClick(e) {
  const btn = e.target.closest('[data-act]');
  if (!btn || !btn.closest('#emp-panel')) return;
  const act = btn.getAttribute('data-act');
  const id = btn.getAttribute('data-id') || '';
  if (act === 'screen') {
    if (window.XayCliView && st.key) window.XayCliView.open(st.key);
    return;
  }
  if (act === 'retry') { load(true); return; }
  if (act === 'notes-edit') { e.preventDefault(); st.notesEdit = true; st.notesErr = ''; st.notesOpen = true; renderNotes(true); return; }
  if (act === 'notes-cancel') { e.preventDefault(); st.notesEdit = false; st.notesErr = ''; renderNotes(true); return; }
  if (act === 'notes-save') { e.preventDefault(); saveNotes(); return; }
  if (act === 'accts') { st.acctOpen = !st.acctOpen; const p = personOf(); if (p) renderQuota(p); return; }
  if (act === 'canceled') { st.showCanceled = !st.showCanceled; renderWork(); return; }
  if (act === 'canvas') { openCanvas(btn); return; }
  const key = act === 'raw' ? 'raw:' + id : act === 'row' ? id : '';
  if (!key) return;
  if (st.open.has(key)) st.open.delete(key); else st.open.add(key);
  renderWork();
}

// ---------------------------------------------------------------- 在想 / 在做（直播通道，大白话）

function stopLive() {
  if (st.stopLive) { st.stopLive(); st.stopLive = null; }
  st.liveFor = '';
}

function syncLive(p) {
  const working = !!(st.key && p && stateOf(p) === 'working');
  if (!working || (st.cliOpenEm && st.cliOpenEm === st.key)) {
    stopLive();
    return;
  }
  if (st.liveFor !== st.key) {
    stopLive();
    st.liveFor = st.key;
    st.live = { think: '', doing: '' };
    liveBuf.kind = ''; liveBuf.text = '';
    st.stopLive = connectLive(st.key, onLive);
  }
  paintLive();
}

// 英文的思考 / 回复大白话那边只给"正在思考……"：这里改用原话的最后一句（去掉 ** 标题符号），免得"在想 正在思考……"
const PLACEHOLDER = /^正在(思考|写回复)……$/;
const liveBuf = { kind: '', text: '' };
function rawTail(item) {
  liveBuf.text = (liveBuf.kind === item.kind ? liveBuf.text : '') + (item.text || '');
  liveBuf.kind = item.kind;
  liveBuf.text = liveBuf.text.slice(-1200);
  const lines = liveBuf.text.replace(/\*\*/g, '').split('\n').map(s => s.trim()).filter(Boolean);
  const last = lines.length ? lines[lines.length - 1] : '';
  return last.length > 80 ? last.slice(0, 79) + '…' : last;
}

function onLive(item) {
  if (!item) return;
  if (item.kind !== 'thinking' && item.kind !== 'text') { liveBuf.kind = ''; liveBuf.text = ''; }
  else {
    const tail = rawTail(item);
    if (item.plain && PLACEHOLDER.test(item.plain) && tail) item = Object.assign({}, item, { plain: tail });
  }
  if (!item.plain) return;
  if (item.kind === 'thinking') st.live.think = item.plain;
  else if (item.kind === 'tool' || item.kind === 'text') st.live.doing = (item.kind === 'text' ? '写回复：' : '') + item.plain;
  if (!st.liveQueued) {
    st.liveQueued = true;
    requestAnimationFrame(() => { st.liveQueued = false; paintLive(); });
  }
}

function paintLive() {
  const el = $('emp-live');
  if (!el) return;
  const { think, doing } = st.live;
  const sees = el.getAttribute('data-sees') !== '0';
  // 在想什么：英文或像代码的，换成"正在琢磨怎么做"，原文放到鼠标悬停里
  const tw = thoughtWords(think);
  const thinkHtml = think ? `<span${tw.swapped ? ` class="emp-live-swap" title="${escapeHtml(`原文：${tw.raw}`)}"` : ''}>${escapeHtml(tw.text)}</span>`
    : `<span class="emp-live-quiet">${sees ? '—' : '这家看不到'}</span>`;
  el.innerHTML = `
    <div class="emp-live-row"><b><span class="emp-live-dot" aria-hidden="true"></span>在想</b>${thinkHtml}</div>
    <div class="emp-live-row"><b>在做</b>${doing ? `<span>${escapeHtml(doing)}</span>` : '<span class="emp-live-quiet">—</span>'}</div>`;
}
