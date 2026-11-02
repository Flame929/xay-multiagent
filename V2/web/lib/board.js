// board.js - V2 看板（M5：7878 换成 V2）。左侧对话列表，右边这个对话里每名员工一列：
//   列头 = 名字、在做什么、第几步、记忆剩多少；列身 = 他的命令行（cli-view.js 的 XayCliView.pane，不另写直播解析）。
// 数据：GET /api/v2/convs（对话列表）、/api/v2/snapshot?conv=（这个对话）、/api/v2/stream（有动静就重拉）。
// 读不到就如实显示"读不到数据"，不回落到示例；示例只在 ?demo=1 时。只读，不发任何写请求。
import { escapeHtml, getAgentType, AI_NAMES, shortTitle, progressLine, formatClock } from './utils.js';
import { involvedPeople } from './render-office.js';

const $ = id => document.getElementById(id);
const AI_LETTER = { codex: 'C', dsh: 'D', agy: 'G', other: 'A' };
const STATE_WORDS = {
  working: '在做', queued: '排队中', blocked: '等你答复', compacting: '在整理记忆', failed: '上一轮没做成',
  offline: '离线了', sleeping: '在休息', idle: '空闲', fired: '已离职'
};
const STATE_ORDER = { working: 0, compacting: 1, blocked: 2, queued: 3, failed: 4, idle: 5, sleeping: 6, offline: 7 };
const RECENT_DAYS = 3;
const LAYOUT_KEY = 'xay-board-layout';

const params = new URLSearchParams(location.search);
const demo = params.get('demo') || '';
const state = {
  conv: params.get('conv') || '',
  convs: [], total: 0, snapshot: null, selected: '', layout: '', search: '', showOld: false,
  cols: new Map(),            // em -> { el, pane, sig }
  seq: 0, es: null, timer: null, pollTimer: null, hiddenTimer: null, visibilityBound: false
};

// ------------------------------------------------------------ 小工具

function q(extra = {}) {
  const sp = new URLSearchParams();
  Object.entries(extra).forEach(([k, v]) => { if (v) sp.set(k, v); });
  if (demo) sp.set('demo', demo);
  const s = sp.toString();
  return s ? '?' + s : '';
}

async function getJson(path) {
  const res = await fetch(path, { cache: 'no-store' });
  let body = null;
  try { body = await res.json(); } catch (_) { /* 不是 JSON */ }
  if (!res.ok) throw new Error((body && (body.error || body.detail)) || `HTTP ${res.status}`);
  return body;
}

function ago(ts, now) {
  if (!ts) return '';
  const sec = Math.max(0, Math.round((now || Date.now() / 1000) - ts));
  if (sec < 60) return '刚刚';
  if (sec < 3600) return `${Math.round(sec / 60)} 分钟前`;
  if (sec < 86400) return `${Math.round(sec / 3600)} 小时前`;
  if (sec < 86400 * 2) return '昨天';
  return formatClock(ts, now).split(' ')[0];
}

function readLayout() {
  try { return localStorage.getItem(LAYOUT_KEY) || ''; } catch (_) { return ''; }
}

function saveLayout(v) {
  try { localStorage.setItem(LAYOUT_KEY, v); } catch (_) { /* 无痕模式等：不记也没关系 */ }
}

// 左上角"公司页 / 任务画布"、右上角"纯 CLI""修改文件"去哪（office-spec.md 二¾.1~3）：选了对话就带上这个对话；没选就去 /org、/org?canvas=1。
// "纯 CLI"在这一页是按下的：再点回到对话版看板（/）的同一个对话；"修改文件"回到对话版并直接打开修改文件（#/c/<对话>/files）
function setViewLinks(convId) {
  const set = (id, href) => { const el = $(id); if (el) el.href = href; };
  set('vs-org', '/org' + q({ conversation: convId }));
  set('vs-canvas', '/org' + q({ conversation: convId, canvas: '1' }));
  const root = '/' + (demo ? '?demo=' + encodeURIComponent(demo) : '');
  const hash = convId ? '#/c/' + encodeURIComponent(convId) : '';
  set('cli-btn', root + hash);
  set('files-btn', root + (hash ? hash + '/files' : ''));
}

// 去别的页面前先断开所有实时连接：命令行直播一人一条连接，浏览器对同一个地址最多同时开 6 条，
// 人一多就全占满了，点"公司页""纯 CLI"这些链接会一直转圈打不开
function releaseStreams() {
  state.cols.forEach(c => { try { c.pane && c.pane.stop(); } catch (_) { /* 已经停了 */ } });
  try { if (state.es) state.es.close(); } catch (_) { /* 已经关了 */ }
  state.es = null;
  stopPolling();
}

function setConn(ok, text) {
  const el = $('conn');
  if (!el) return;
  el.className = 'conn' + (ok ? '' : ' bad');
  el.innerHTML = `<span class="dot${ok ? ' ok' : ' err'}"></span><span>${escapeHtml(text)}</span>`;
}

// ------------------------------------------------------------ 左侧：对话列表

function convIsRecent(c, now) {
  return c.working > 0 || c.needs_you > 0 || (now - (c.active_ts || 0)) < RECENT_DAYS * 86400 || c.id === state.selected;
}

function convRow(c, now) {
  const title = c.title || '未命名对话';
  let slot = '<span class="dot"></span>';
  let st = escapeHtml(ago(c.active_ts, now));
  if (c.working > 0) { slot = '<span class="ring"></span>'; st = `${c.working} 人在做`; }
  else if (c.needs_you > 0) slot = '<span class="dot warn"></span>';
  const badges = [];
  if (c.needs_you > 0) badges.push(`<span class="badge">等你 ${c.needs_you}</span>`);
  if (c.legacy) badges.push('<span class="badge grey" title="旧系统派的活，镜像过来的">旧系统</span>');
  return `<div class="row${c.id === state.selected ? ' active' : ''}${c.legacy ? ' legacy' : ''}" role="button" tabindex="0" data-conv="${escapeHtml(c.id)}" title="${escapeHtml(title)}">
    <span class="slot">${slot}</span><span class="title">${escapeHtml(title)}</span>${badges.join('')}<span class="st">${st}</span></div>`;
}

function renderConvList() {
  const box = $('conv-list');
  if (!box) return;
  const now = Date.now() / 1000;
  const needle = state.search.trim().toLowerCase();
  let list = state.convs;
  if (needle) list = list.filter(c => (c.title || '').toLowerCase().includes(needle) || (c.ws && (c.ws.name || '').toLowerCase().includes(needle)));
  if (!list.length) {
    box.innerHTML = `<div class="side-empty">${needle ? '没有对得上的对话' : '还没有对话。负责人用 xay2 begin 开一个就会出现在这里。'}</div>`;
    return;
  }
  const recent = needle ? list : list.filter(c => convIsRecent(c, now));
  const old = needle ? [] : list.filter(c => !convIsRecent(c, now));
  const groups = new Map();
  recent.forEach(c => {
    const key = (c.ws && c.ws.id) || '';
    if (!groups.has(key)) groups.set(key, { name: (c.ws && c.ws.name) || '其他', rows: [] });
    groups.get(key).rows.push(c);
  });
  let html = '';
  groups.forEach(g => {
    html += `<div class="ws-row"><span class="name">${escapeHtml(g.name)}</span></div>` + g.rows.map(c => convRow(c, now)).join('');
  });
  if (!recent.length) html += '<div class="side-empty">最近几天没有动静</div>';
  if (old.length) {
    html += `<button type="button" class="fold-toggle" id="old-toggle">${state.showOld ? '▾' : '▸'} 更早的对话（${old.length}）</button>`;
    if (state.showOld) html += old.map(c => convRow(c, now)).join('');
  }
  box.innerHTML = html;
}

async function loadConvs() {
  try {
    const data = await getJson('/api/v2/convs' + q());
    state.convs = data.convs || [];
    state.total = data.total || state.convs.length;
    renderConvList();
  } catch (err) {
    $('conv-list').innerHTML = `<div class="side-empty">读不到对话列表：${escapeHtml(err.message || String(err))}</div>`;
  }
}

function selectConv(id) {
  if (!id || id === state.selected) return;
  state.conv = id;
  const sp = new URLSearchParams(location.search);
  sp.set('conv', id);
  history.pushState({ conv: id }, '', location.pathname + '?' + sp.toString());
  setViewLinks(id);
  clearCols();
  state.snapshot = null;
  state.selected = id;
  renderConvList();
  loadSnapshot();
}

// ------------------------------------------------------------ 右边：这个对话

function peopleOf(snap) {
  const people = Object.values(involvedPeople(snap));
  const legacyOwners = new Set(), v2Owners = new Set();
  (snap.tasks || []).forEach(t => { if (t.owner) (t.legacy ? legacyOwners : v2Owners).add(t.owner); });
  // 只在旧系统里干过活的人（桥镜像过来的"临时工"等）：新版没有他的记忆读数，也没有命令行直播。
  // 员工归项目，同一个人在新版别的对话里干过活的，记忆和命令行照常显示。
  const noV2Reading = p => { const c = p.context || {}; return !c.pct && !c.calibrating && !c.estimated && !p.online; };
  people.forEach(p => { p.legacyOnly = legacyOwners.has(p.key) && !v2Owners.has(p.key) && noV2Reading(p); });
  const st = p => p.display_state || p.state || 'idle';
  people.sort((a, b) => (STATE_ORDER[st(a)] ?? 9) - (STATE_ORDER[st(b)] ?? 9)
    || String(a.label || '').localeCompare(String(b.label || ''), 'zh-CN', { numeric: true }));
  return people;
}

function memText(p) {
  if (p.legacyOnly) return { text: '记忆：旧系统不报', pct: null };
  const c = p.context || {};
  if (c.calibrating) return { text: '记忆：刚开始算', pct: null };
  if (typeof c.pct !== 'number') return { text: '记忆：还不知道', pct: null };
  const left = Math.max(0, Math.min(100, Math.round(100 - c.pct)));
  return { text: `记忆剩 ${c.estimated ? '约 ' : ''}${left}%`, pct: left };
}

function wsName(ws) {
  if (!ws) return '';
  const name = ws.name || ws.id || '';
  if (ws.path && name === ws.id) return ws.path.replace(/\/+$/, '').split('/').pop() || name;
  return name;
}

function stepText(p, s) {
  const pr = p.progress || {};
  if (p.legacyOnly || !['working', 'compacting', 'blocked'].includes(s)) return '';
  if (pr.step && pr.of) {
    const line = progressLine(pr.text || '', 40);
    return `第 ${pr.step}/${pr.of} 步${line ? '：' + line : ''}`;
  }
  if (s === 'working') return '刚开工，还没报第几步';
  return '';
}

function headHtml(p) {
  const type = getAgentType(p.agent);
  const s = p.display_state || p.state || 'idle';
  const mem = memText(p);
  const memCls = mem.pct == null ? '' : mem.pct < 15 ? 'low' : mem.pct < 35 ? 'tight' : '';
  const bar = mem.pct == null ? '' : `<span class="mem-bar" aria-hidden="true"><i class="${memCls}" style="width:${mem.pct}%"></i></span>`;
  const now = p.now ? shortTitle(p.now, 60) : '';
  const doing = now ? (s === 'working' ? '在做：' : '最近：') + now : '手上没有活';
  const step = stepText(p, s);
  const note = p.legacyOnly ? '<div class="note">旧系统派的活：命令行过程这里看不到，只看得到在做哪件、做没做完</div>' : '';
  return `<div class="r1"><span class="avatar" style="--ai-c:var(--ai-${type === 'other' ? 'other' : type})">${AI_LETTER[type] || 'A'}</span>
      <span class="nm">${escapeHtml(p.label || p.key)}</span>
      <span class="stt ${escapeHtml(s)}">${s === 'working' ? '<span class="ring"></span>' : ''}${escapeHtml(STATE_WORDS[s] || s)}</span>
      <span class="mem" title="${escapeHtml(AI_NAMES[type] || 'AI')} 的记忆还剩多少">${bar}${escapeHtml(mem.text)}</span></div>
    <div class="r2"><span class="now" title="${escapeHtml(p.now || '')}">${escapeHtml(doing)}</span>${step ? `<span class="step">${escapeHtml(step)}</span>` : ''}</div>${note}`;
}

function clearCols() {
  state.cols.forEach(c => { try { c.pane && c.pane.stop(); } catch (_) { /* 已经停了 */ } });
  state.cols.clear();
  const cols = $('cols');
  if (cols) cols.innerHTML = '';
  const tabs = $('tabs');
  if (tabs) tabs.innerHTML = '';
}

function applyLayout(count) {
  const layout = state.layout || (count >= 2 && window.innerWidth >= 1100 ? 'side' : 'tabs');
  const cols = $('cols');
  cols.className = 'cols ' + layout;
  const btn = $('layout-btn');
  btn.textContent = layout === 'side' ? '单列' : '并排';
  btn.classList.toggle('on', layout === 'side');
  btn.hidden = count < 2;
  return layout;
}

function renderCols(snap) {
  const cols = $('cols');
  const people = peopleOf(snap);
  if (!people.length) {
    clearCols();
    cols.innerHTML = '<div class="cols-empty"><b>这个对话里还没有员工</b>负责人派了活（xay2 plan add）就会在这里出现，每人一列。</div>';
    applyLayout(0);
    return;
  }
  cols.querySelectorAll('.cols-empty, .error-box').forEach(el => el.remove());   // 上一次的空白提示 / 读不到数据
  const keep = new Set(people.map(p => p.key));
  state.cols.forEach((c, k) => {
    if (!keep.has(k)) { c.pane && c.pane.stop(); c.el.remove(); state.cols.delete(k); }
  });
  if (!state.selected_em || !keep.has(state.selected_em)) state.selected_em = people[0].key;
  people.forEach((p, i) => {
    let c = state.cols.get(p.key);
    if (!c) {
      const el = document.createElement('section');
      el.className = 'col';
      el.dataset.em = p.key;
      el.innerHTML = '<div class="col-head"></div><div class="col-body"></div>';
      const pane = window.XayCliView && window.XayCliView.pane ? window.XayCliView.pane(p.key, el.querySelector('.col-body')) : null;
      c = { el, pane, sig: '' };
      state.cols.set(p.key, c);
    }
    const html = headHtml(p);
    if (html !== c.sig) { c.el.querySelector('.col-head').innerHTML = html; c.sig = html; }
    c.el.classList.toggle('active', p.key === state.selected_em);
    if (cols.children[i] !== c.el) cols.insertBefore(c.el, cols.children[i] || null);
  });
  $('tabs').innerHTML = people.map(p => {
    const s = p.display_state || p.state || 'idle';
    const type = getAgentType(p.agent);
    const mark = s === 'working' ? '<span class="ring"></span>' : s === 'blocked' || s === 'failed' ? '<span class="dot warn"></span>' : '';
    return `<button type="button" class="tab${p.key === state.selected_em ? ' active' : ''}" role="tab" data-em="${escapeHtml(p.key)}" aria-selected="${p.key === state.selected_em}">
      <span class="avatar" style="--ai-c:var(--ai-${type === 'other' ? 'other' : type})">${AI_LETTER[type] || 'A'}</span>${escapeHtml(p.label || p.key)}${mark}</button>`;
  }).join('');
  applyLayout(people.length);
}

function renderHead(snap) {
  const conv = snap.conversation || {};
  const title = conv.title || '未命名对话';
  $('conv-title').textContent = title;
  document.title = title + ' · xay 看板';
  const tasks = snap.tasks || [];
  const done = tasks.filter(t => t.status === 'done' || t.status === 'reported').length;
  const working = tasks.filter(t => t.status === 'working').length;
  const bits = [];
  if (wsName(snap.ws)) bits.push(wsName(snap.ws));
  if (conv.plan_version) bits.push(`计划第 ${conv.plan_version} 版`);
  bits.push(tasks.length ? `${tasks.length} 件活：做完 ${done}，在做 ${working}` : '还没有活');
  if (tasks.length && tasks.every(t => t.legacy)) bits.push('旧系统派的');
  if (snap.halt) bits.push('总闸已拉下');
  $('conv-line').innerHTML = bits.map(b => `<span>${escapeHtml(b)}</span>`).join('');
  setViewLinks(conv.id);
  const orgHref = '/org' + q({ conversation: conv.id });
  const decs = snap.decisions || [];
  const bar = $('dec-bar');
  if (decs.length) {
    bar.hidden = false;
    bar.innerHTML = `<span class="t">有 ${decs.length} 件事等你决定：${escapeHtml(decs[0].title || '')}${decs.length > 1 ? ' 等' : ''}</span><a href="${escapeHtml(orgHref)}" title="在公司页顶上的「需要你」里处理">去处理 →</a>`;
  } else {
    bar.hidden = true;
    bar.innerHTML = '';
  }
}

function showError(message) {
  clearCols();
  $('conv-title').textContent = '读不到数据';
  $('conv-line').innerHTML = '';
  $('dec-bar').hidden = true;
  $('cols').className = 'cols tabs';
  $('cols').innerHTML = `<div class="error-box"><b>读不到数据</b>${escapeHtml(message)}<br><button type="button" class="btn" id="retry-btn">重试</button></div>`;
}

let loading = null;
async function loadSnapshot() {
  if (loading) return loading;
  loading = (async () => {
    try {
      const snap = await getJson('/api/v2/snapshot' + q({ conv: state.conv }));
      const id = (snap.conversation && snap.conversation.id) || '';
      if (!id) {
        clearCols();
        $('conv-title').textContent = '还没有对话';
        $('conv-line').innerHTML = '';
        $('cols').innerHTML = '<div class="cols-empty"><b>还没有对话</b>负责人用 xay2 begin 开一个对话、派了活，就会在这里出现。</div>';
        return;
      }
      if (state.selected && id !== state.selected) clearCols();
      state.selected = id;
      state.snapshot = snap;
      state.seq = Math.max(state.seq, snap.seq || 0);
      renderHead(snap);
      renderCols(snap);
      renderConvList();
    } catch (err) {
      showError(err.message || String(err));
    }
  })();
  try { await loading; } finally { loading = null; }
}

// ------------------------------------------------------------ 实时：有动静就重拉

function scheduleReload() {
  clearTimeout(state.timer);
  state.timer = setTimeout(() => { loadSnapshot(); loadConvs(); }, 400);
}

function bindVisibility() {
  if (state.visibilityBound || typeof document === 'undefined' || !document.addEventListener) return;
  state.visibilityBound = true;
  document.addEventListener('visibilitychange', () => {
    if (document.visibilityState === 'hidden') {
      clearTimeout(state.hiddenTimer);
      state.hiddenTimer = setTimeout(() => {
        if (state.es) {
          state.es.close();
          state.es = null;
          setConn(false, '后台已暂停连接');
        }
      }, 10000);
    } else {
      clearTimeout(state.hiddenTimer);
      if (!state.es) {
        connect();
      }
    }
  });
}

function connect() {
  bindVisibility();
  if (typeof EventSource === 'undefined') { setConn(false, '浏览器不支持实时更新，每 5 秒刷新'); startPolling(); return; }
  try { if (state.es) state.es.close(); } catch (_) { /* 已经关了 */ }
  const es = new EventSource('/api/v2/stream' + q({ since: state.seq ? String(state.seq) : '' }));
  state.es = es;
  es.onopen = () => { setConn(true, demo ? '示例数据 · 实时' : '实时连接中'); stopPolling(); };
  const bump = e => { const n = Number(e.lastEventId || 0); if (n) state.seq = Math.max(state.seq, n); scheduleReload(); };
  es.addEventListener('ledger', bump);
  es.addEventListener('changed', bump);
  es.addEventListener('heartbeat', () => setConn(true, demo ? '示例数据 · 实时' : '实时连接中'));
  es.onerror = () => {
    setConn(false, '连接断了，正在重连…');
    if (es.readyState === EventSource.CLOSED) { startPolling(); setTimeout(connect, 5000); }
  };
}

function startPolling() {
  if (state.pollTimer) return;
  state.pollTimer = setInterval(() => { loadSnapshot(); loadConvs(); }, 5000);
}

function stopPolling() {
  clearInterval(state.pollTimer);
  state.pollTimer = null;
}

// ------------------------------------------------------------ 交互

function bind() {
  $('conv-list').addEventListener('click', e => {
    if (e.target.closest('#old-toggle')) { state.showOld = !state.showOld; renderConvList(); return; }
    const row = e.target.closest('.row[data-conv]');
    if (row) selectConv(row.dataset.conv);
  });
  $('conv-list').addEventListener('keydown', e => {
    const row = e.target.closest('.row[data-conv]');
    if (row && (e.key === 'Enter' || e.key === ' ')) { e.preventDefault(); selectConv(row.dataset.conv); }
  });
  $('search').addEventListener('input', e => { state.search = e.target.value || ''; renderConvList(); });
  $('tabs').addEventListener('click', e => {
    const tab = e.target.closest('.tab[data-em]');
    if (!tab) return;
    state.selected_em = tab.dataset.em;
    if (state.snapshot) renderCols(state.snapshot);
    const c = state.cols.get(tab.dataset.em);
    if (c && $('cols').classList.contains('side')) c.el.scrollIntoView({ behavior: 'smooth', inline: 'nearest', block: 'nearest' });
  });
  $('layout-btn').addEventListener('click', () => {
    const now = $('cols').classList.contains('side') ? 'tabs' : 'side';
    state.layout = now;
    saveLayout(now);
    applyLayout(state.cols.size);
  });
  $('cols').addEventListener('click', e => {
    if (e.target.closest('#retry-btn')) { loadSnapshot(); loadConvs(); }
  });
  $('theme-btn').addEventListener('click', () => {
    const root = document.documentElement;
    const dark = root.getAttribute('data-theme') === 'dark' ||
      (!root.getAttribute('data-theme') && window.matchMedia && window.matchMedia('(prefers-color-scheme: dark)').matches);
    root.setAttribute('data-theme', dark ? 'light' : 'dark');
  });
  window.addEventListener('resize', () => { if (!state.layout) applyLayout(state.cols.size); });
  // 点链接去别的页面（同一个窗口）：先放掉实时连接，免得新页面排不上队
  document.addEventListener('click', e => {
    const a = e.target && e.target.closest && e.target.closest('a[href]');
    if (!a || e.button !== 0 || e.metaKey || e.ctrlKey || e.shiftKey || e.altKey || (a.target && a.target !== '_self')) return;
    if (a.origin === location.origin && a.pathname === location.pathname && a.search === location.search) return;   // 同一页里跳
    releaseStreams();
  }, true);
  window.addEventListener('pagehide', releaseStreams);
  window.addEventListener('pageshow', e => { if (e.persisted) { clearCols(); state.selected = ''; loadSnapshot().then(connect); } });   // 按"后退"回来：重新连上
  window.addEventListener('popstate', () => {
    const id = new URLSearchParams(location.search).get('conv') || '';
    state.conv = id;
    setViewLinks(id);
    clearCols();
    state.selected = '';
    loadSnapshot();
  });
}

function init() {
  const theme = params.get('theme');
  if (theme === 'dark' || theme === 'light') document.documentElement.setAttribute('data-theme', theme);
  if (demo) {
    $('demo-banner').hidden = false;
  }
  state.layout = params.get('layout') === 'side' || params.get('layout') === 'tabs' ? params.get('layout') : readLayout();
  setViewLinks(state.conv);
  bind();
  loadConvs();
  loadSnapshot().then(connect);
  setInterval(loadConvs, 30000);           // "几分钟前"跟着走；别的对话有动静也能看到
}

init();
