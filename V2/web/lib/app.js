// app.js - xay V2 公司页主应用入口
import { escapeHtml } from './utils.js';
import { getUrlParams, fetchSnapshot, connectEventStream } from './api.js';
import { renderGmZone, renderDepartments, renderArcs, flyLetter, confetti, flashCompact, findNodeEl, involvedPeople, initDeptGrid, dispatchTask,
  mailModel, setMail, highlightMail, letterFlight, roomPlan } from './render-office.js';
import { renderLetters, focusLetter } from './render-letters.js';
import { renderPlanTasks, renderPlanHistory, bindPlanPanel } from './render-plan.js';
import { buildPlan } from './plan-model.js';
import { openTaskCanvas, updateTaskCanvas, isCanvasOpen, canvasState, canvasView } from './task-canvas.js';
import { isDetailOpen } from './plan-detail.js';
import { initOfficeView, officeDrawn, officeView, fitOffice, homeOffice } from './office-view.js';
import { renderDecisionsBar } from './decisions.js';
import { syncChars } from './station.js';
import { initEmployeePanel, onSnapshot as employeeOnSnapshot, onLedgerItem as employeeOnLedger, currentTree } from './employee-panel.js';
import { renderQuotaStrip } from './quota-view.js';
import { renderProgress, progressOf } from './progress-bar.js';

let currentSnapshot = null;
let disconnectStream = null;

// ---------- 计划模型（plan-model.js）：公司页房间、任务画布、任务拆解、计划版本四处共用同一份 ----------
// 任务树（步骤明细、活的详情、依赖）来自 GET /api/v2/plan?conv=；只在用得上时去拉（任务拆解 / 计划版本页签开着、画布或详情开着），
// 最多 2.5 秒一次。接口读不到时：员工详情开着就借它的 tree，否则只用快照（没有步骤明细）。
const planTree = { tree: null, conv: '', at: 0, busy: null, down: 0, timer: 0 };

function convOf(data) {
  return (data && data.conversation && data.conversation.id) || '';
}

function treeFor(data) {
  if (planTree.tree && planTree.conv === convOf(data)) return planTree.tree;
  return currentTree();
}

function loadPlanTree() {
  if (planTree.busy) return planTree.busy;
  if (Date.now() < planTree.down) return Promise.resolve(null);
  const conv = convOf(currentSnapshot);
  const qs = new URLSearchParams();
  if (conv) qs.set('conv', conv);
  const demo = getUrlParams().demo;
  if (demo) qs.set('demo', demo);
  planTree.busy = (async () => {
    try {
      const res = await fetch('/api/v2/plan?' + qs.toString());
      if (!res.ok) { planTree.down = Date.now() + (res.status === 404 ? 60000 : 15000); return null; }
      const j = await res.json();
      if (j && j.tree) { planTree.tree = j.tree; planTree.conv = j.conv || conv; planTree.at = Date.now(); }
      return planTree.tree;
    } catch (_) {
      planTree.down = Date.now() + 15000;
      return null;
    } finally {
      planTree.busy = null;
    }
  })();
  return planTree.busy;
}

function planNeeded() {
  const side = document.getElementById('side-panel');
  const tab = document.querySelector('.tab-btn.active');
  const t = tab && tab.getAttribute('data-tab');
  return (side && !side.classList.contains('collapsed') && (t === 'plan' || t === 'history')) || isCanvasOpen() || isDetailOpen();
}

function schedulePlanTree(now = false) {
  if (!planNeeded()) return;
  clearTimeout(planTree.timer);
  const wait = now ? 0 : Math.max(0, 2500 - (Date.now() - planTree.at));
  planTree.timer = setTimeout(async () => {
    const t = await loadPlanTree();
    if (t && currentSnapshot) renderPlanViews(currentSnapshot);
  }, wait);
}

// 任务拆解、计划版本、开着的画布按同一份模型重画
function renderPlanViews(data, pm = null) {
  const tree = treeFor(data);
  const model = pm || buildPlan(data, tree);
  renderPlanTasks(model);
  renderPlanHistory(model, data.people);
  if (isCanvasOpen()) {
    const cs = canvasState();
    updateTaskCanvas(cs && cs.em ? { snap: data } : { snap: data, tree }, {});
  }
  return model;
}

// 不点员工也能打开整个对话的任务画布（顶栏"任务画布"按钮、任务拆解页签、计划版本"在画布上看"）。focus = 要高亮的 {parts, items, label}
async function openPlanCanvas(focus, opener) {
  if (!currentSnapshot) return;
  const fresh = planTree.tree && planTree.conv === convOf(currentSnapshot) && Date.now() - planTree.at < 3000;
  if (!fresh) await Promise.race([loadPlanTree(), new Promise(r => setTimeout(r, 1500))]);
  openTaskCanvas({ snap: currentSnapshot, tree: treeFor(currentSnapshot) }, { em: '', focus: focus || null, opener: opener || null });
  schedulePlanTree();
}

function showError(message) {
  const overlay = document.getElementById('error-overlay');
  if (overlay) {
    overlay.style.display = 'flex';
    overlay.innerHTML = `
      <div class="error-banner">
        <div class="error-icon">⚠️</div>
        <div class="error-title">读不到数据</div>
        <div class="error-reason">${escapeHtml(message)}</div>
        <div class="error-actions">
          <button type="button" class="btn-action" id="btn-err-retry">重试连接</button>
        </div>
      </div>`;
    const btn = document.getElementById('btn-err-retry');
    if (btn) btn.onclick = () => initData();
  }
}

function clearError() {
  const overlay = document.getElementById('error-overlay');
  if (overlay) {
    overlay.style.display = 'none';
    overlay.innerHTML = '';
  }
}

function updateTopBar(data, pm = null) {
  const conv = data.conversation || {};
  const convTitle = conv.title || 'AI 公司组织架构';
  const depts = data.depts || [];
  const people = involvedPeople(data);        // 只数这个对话里有关的人

  // 顶栏数中任务（2026-10-31 层级纠正：部门就是中任务）：有小任务的、不算"零散"那一组
  const totalDepts = pm ? pm.parts.filter(p => !p.loose && (p.items.length || p.id !== 'all')).length : depts.length;
  const activeWorkers = Object.values(people);
  const workingWorkers = activeWorkers.filter(p => p.state === 'working' || p.display_state === 'working');

  // 待答需求 = 顶部"需要你"那几条 + 写给部门、还没人回的请求信。"需要你"都处理完（你选了、或管家自动关掉）就跟着变 0
  let totalOpenReqs = (data.decisions || []).filter(d => d && !d.delegated).length;       // 交出去的不算
  depts.forEach(d => {
    totalOpenReqs += (d.counts && d.counts.open_requests) || 0;
  });

  const barTitle = document.getElementById('bar-title');
  if (barTitle) barTitle.textContent = convTitle + ' · 公司架构';

  const statDepts = document.getElementById('stat-depts');
  if (statDepts) statDepts.textContent = String(totalDepts);

  const statWorkers = document.getElementById('stat-workers');
  if (statWorkers) statWorkers.textContent = `${workingWorkers.length}/${activeWorkers.length}`;

  const statRequests = document.getElementById('stat-requests');
  if (statRequests) statRequests.textContent = String(totalOpenReqs);

  // 额度条：四条线路的真实剩余（管家定时查；用完的标红，鼠标停留看重置时间）
  renderQuotaStrip(document.getElementById('quota-strip'), data.quota, data.now);
  // 总进度条：数字全由快照算好；没有 progress 字段时整条不显示
  renderProgress(document.getElementById('progress-strip'), progressOf(data));
  setBoardLink(conv.id);
}

// 左上角"看板"：回到看板上同一个对话（/#/c/<对话>）；示例模式带着 demo（要放在 # 前面，看板才认得出是示例）
function setBoardLink(convId) {
  const backLink = document.getElementById('back-link');
  if (!backLink) return;
  const demo = getUrlParams().demo;
  backLink.href = '/' + (demo ? '?demo=' + encodeURIComponent(demo) : '') + (convId ? '#/c/' + encodeURIComponent(convId) : '');
}

function handleSelectLetter(msgId, fromKey, toKey) {
  highlightMail(fromKey, toKey);
  [fromKey, toKey].forEach(k => {
    let el = findNodeEl(k);
    if (!el) return;
    el.classList.remove('flash-ping');
    void el.offsetWidth;
    el.classList.add('flash-ping');
    setTimeout(() => el.classList.remove('flash-ping'), 1300);
  });
}

// 快照里除了 seq / now（每次都变）以外的内容；时间只按分钟算（"3 分钟前"这类字样一分钟最多改一次）
let lastSig = '';
function snapshotSig(data) {
  const { seq, now, ...rest } = data;
  return Math.floor((now || 0) / 60) + '|' + JSON.stringify(rest);
}

// 数据没变就什么都不动；变了也只就地改变了的那部分（工位、角色、连线的动画不从第一帧重放）
function renderAll(data, force = false) {
  if (!data) return;
  currentSnapshot = data;
  clearError();

  const sig = snapshotSig(data);
  if (force || sig !== lastSig) {
    lastSig = sig;
    const pm = buildPlan(data, treeFor(data));       // 四处共用的计划模型：编号、名字、颜色、状态、验收说法都从这里取
    updateTopBar(data, pm);

    // 顶部需要你横条
    const decisionsBar = document.getElementById('decisions-bar');
    renderDecisionsBar(decisionsBar, data.decisions, () => {
      initData();
    });

    // 房间 = 中任务：谁坐哪间房、谁在休息区（信件虚线的监工那一头也按这个找监工席）
    const plan = roomPlan(data, pm);

    // 信件：谁和谁通过信（画虚线）、谁有还没送到的信（工位上挂"✉ 1"）
    const mail = mailModel(data.letters, data.depts, data.now, plan.rooms);
    setMail(mail, mailNames(data));
    const unreadOf = k => mail.unread.get(k) || {};

    // 负责人：状态 + 最近一两个动作；有监工（后台自动写信、下指令的）和不在哪间房里的人来往的信时，旁边放一位监工
    renderGmZone(data.gm, data.conversation?.id, {
      now: data.now, people: involvedPeople(data),
      unread: unreadOf('gm').unread, unreadLetter: unreadOf('gm').letter,
      office: mail.pairs.some(p => p.a === 'office' || p.b === 'office')
    });

    // 小任务房间与工位：只画这个对话里有关的人；手上没活的坐负责人旁边的休息区（旧对话照旧一个部门一间房，最近 24 小时没动静的折起来）
    renderDepartments(data.depts, involvedPeople(data), data.former, data.tasks, data.conversation?.id, data.activity, data.now, { mailFor: unreadOf, plan });

    // 组织树连线：位置真的变了才重画
    window.requestAnimationFrame(() => {
      renderArcs();
      officeDrawn();          // 第一次画好：把办公室摆到起始位置（之后刷新不动视图）
    });

    // 右侧信件
    renderLetters(data.letters, data.people, data.depts, handleSelectLetter);

    // 右侧任务拆解、计划版本（开着的画布也跟着新数据重画）
    renderPlanViews(data, pm);
    schedulePlanTree();

    // 同步角色表情（只改属性，不重建）
    syncChars();
  }

  // 刚推送来的派活、信件：快照里知道了交给谁 / 送到谁，再放动画（线已经画好）
  runPendingDispatch(data);
  runPendingLetters(data);

  // 左侧员工详情面板跟着刷新（它自己判断要不要重画）；附加效果（desk-fx 等）收到这个事件就检查要不要重挂
  employeeOnSnapshot(data);
  document.dispatchEvent(new CustomEvent('xay:rendered', { detail: { seq: data.seq } }));
}

// 网址带 canvas=1（xay2 plan open 打印的就是这种）：第一次拿到数据后直接打开整个对话的任务画布
let canvasFromUrl = typeof window !== 'undefined' && new URLSearchParams(window.location.search).get('canvas') === '1';

async function initData() {
  try {
    const data = await fetchSnapshot();
    renderAll(data);
    if (canvasFromUrl) {
      canvasFromUrl = false;
      openPlanCanvas(null, document.getElementById('canvas-btn'));
    }
  } catch (err) {
    console.error('加载组织架构数据失败:', err);
    showError(err.message || String(err));
  }
}

// 派活动画：推送里只有任务编号，交给谁要等快照重拉回来才知道；同一件活几秒内只飞一次
const pendingDispatch = new Map();   // 任务编号 → {at: 推送到达的时间, auto: 后台自动派的（从监工席飞）}
const dispatched = new Map();        // 任务编号 → 上次飞的时间
const DISPATCH_TYPES = new Set(['task.assigned', 'task.queued', 'task.added']);

// 虚线两头的人叫什么（悬停提示用）
function mailNames(data) {
  const names = { gm: '负责人', office: '监工' };
  Object.values(data.people || {}).forEach(p => { names[p.key] = p.label || p.key; });
  (data.depts || []).forEach(d => { names['dept:' + d.id] = d.name || d.id; });
  return names;
}

// 信件动画：推送来一封信 → 等快照重拉、虚线画好 → 信封沿虚线从写信人飞到收信人（回信就飞回去）
const pendingLetters = [];
function runPendingLetters(data) {
  if (!pendingLetters.length) return;
  const items = pendingLetters.splice(0);
  requestAnimationFrame(() => requestAnimationFrame(() => {
    renderArcs();
    items.forEach((item, i) => {
      const f = letterFlight(item, data);
      if (f) setTimeout(() => flyLetter(...f), i * 500);
    });
  }));
}

function runPendingDispatch(data) {
  if (!pendingDispatch.size) return;
  const now = Date.now();
  const tasks = new Map((data.tasks || []).map(t => [t.id, t]));
  [...pendingDispatch].forEach(([id, { at, auto }], i) => {
    const t = tasks.get(id);
    if (t && t.owner && !(now - (dispatched.get(id) || 0) < 8000)) {
      pendingDispatch.delete(id);
      dispatched.set(id, now);
      requestAnimationFrame(() => setTimeout(() => dispatchTask(t.owner, t.title, { fromHead: auto }), 80 + i * 450));
    } else if (now - at > 15000 || (t && t.owner)) {
      pendingDispatch.delete(id);
    }
  });
}

function handleLedgerAnimation(item) {
  if (!item || !item.type) return;
  employeeOnLedger(item);

  const type = item.type;
  if (DISPATCH_TYPES.has(type) && item.subject) pendingDispatch.set(item.subject, { at: Date.now(), auto: !!item.auto });
  if (type === 'letter.sent') {
    pendingLetters.push(item);
  } else if (type === 'task.accepted' || type === 'task.landed') {
    const owner = item.owner || item.subject;
    if (owner) {
      setTimeout(() => confetti(owner), 50);
    }
  } else if (type === 'context.compacted') {
    const em = item.subject || item.em;
    if (em) {
      flashCompact(em);
    }
  }
}

function setupSidePanel() {
  document.querySelectorAll('.tab-btn').forEach(btn => {
    btn.addEventListener('click', () => {
      const tab = btn.getAttribute('data-tab');
      document.querySelectorAll('.tab-btn').forEach(b => b.classList.remove('active'));
      document.querySelectorAll('.tab-content').forEach(c => c.classList.remove('active'));
      btn.classList.add('active');
      const activeContent = document.getElementById('tab-' + tab);
      if (activeContent) activeContent.classList.add('active');
      if (tab === 'plan' || tab === 'history') schedulePlanTree(true);
    });
  });

  // 顶栏"任务画布"：整个对话的画布（不用先点员工）
  const canvasBtn = document.getElementById('canvas-btn');
  if (canvasBtn) canvasBtn.addEventListener('click', () => openPlanCanvas(null, canvasBtn));
  bindPlanPanel({ openCanvas: openPlanCanvas });

  const toggleBtn = document.getElementById('panel-toggle-btn');
  const closeBtn = document.getElementById('panel-close-btn');
  const sidePanel = document.getElementById('side-panel');

  if (toggleBtn && sidePanel) {
    toggleBtn.addEventListener('click', () => {
      if (window.innerWidth <= 768) {
        sidePanel.classList.toggle('mobile-open');
      } else {
        sidePanel.classList.toggle('collapsed');
      }
      schedulePlanTree(true);
    });
  }

  if (closeBtn && sidePanel) {
    closeBtn.addEventListener('click', () => {
      sidePanel.classList.remove('mobile-open');
      sidePanel.classList.add('collapsed');
    });
  }

  const themeBtn = document.getElementById('theme-btn');
  if (themeBtn) {
    themeBtn.addEventListener('click', () => {
      const root = document.documentElement;
      const cur = root.getAttribute('data-theme');
      root.setAttribute('data-theme', cur === 'dark' ? 'light' : 'dark');
    });
  }
}

// 画布上点信件虚线、线上的信封、工位或负责人身上的"✉ 1"：打开右侧信件栏，翻到那封信（不再去打开员工详情）
function setupMailClicks() {
  const canvas = document.getElementById('office-canvas');
  if (!canvas) return;
  canvas.addEventListener('click', e => {
    const hit = e.target.closest && e.target.closest('#mail-canvas .mail-line[data-letter], .station-mail[data-letter], .gm-mail[data-letter]');
    if (!hit) return;
    e.stopPropagation();
    e.preventDefault();
    const id = hit.getAttribute('data-letter');
    const pair = hit.getAttribute('data-pair');
    if (pair) highlightMail(...pair.split('~'));
    focusLetter(id);
  });
}

// 给自动测试用（office-spec.md 二.7 不抖）：用手上这份快照再画一遍（force = 当成数据变了，整套渲染都走一遍）
if (typeof window !== 'undefined') {
  window.__xayOffice = {
    rerender: (force = true) => { if (currentSnapshot) renderAll(JSON.parse(JSON.stringify(currentSnapshot)), force); return !!currentSnapshot; },
    snapshot: () => (currentSnapshot ? JSON.parse(JSON.stringify(currentSnapshot)) : null),
    render: snap => { renderAll(snap, true); return true; },
    view: () => officeView(), fit: () => fitOffice(), home: () => homeOffice(),
    openCanvas: focus => openPlanCanvas(focus || null, null), canvasView: () => canvasView(),
    ready: () => !!currentSnapshot
  };
}

function init() {
  setBoardLink(getUrlParams().conv);          // 数据还没到也先按网址里的对话填好
  initOfficeView();
  setupSidePanel();
  setupMailClicks();
  initEmployeePanel({ snapshot: () => currentSnapshot });
  initDeptGrid(() => {
    if (currentSnapshot) renderAll(currentSnapshot, true);
  });

  window.addEventListener('resize', () => {
    if (currentSnapshot) {
      renderArcs();
    }
  });

  // 左右面板开合、窗口变宽变窄时，办公室可用的宽度变了：重画组织树连线
  const viewport = document.getElementById('office-viewport');
  if (viewport && typeof ResizeObserver !== 'undefined') {
    let raf = 0;
    new ResizeObserver(() => {
      cancelAnimationFrame(raf);
      raf = requestAnimationFrame(() => { if (currentSnapshot) renderArcs(); });
    }).observe(viewport);
  }

  initData();

  // 启动实时流
  disconnectStream = connectEventStream({
    onReload: () => {
      initData();
    },
    onLedgerEvent: (item) => {
      handleLedgerAnimation(item);
    },
    getSinceSeq: () => {
      return currentSnapshot ? currentSnapshot.seq : 0;
    }
  });
}

if (document.readyState === 'loading') {
  document.addEventListener('DOMContentLoaded', init);
} else {
  init();
}
