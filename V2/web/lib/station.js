// station.js - 员工工位卡片：记忆条、思考气泡、步骤、队列、书桌与 AI 娘角色
import { escapeHtml, getAgentType, getStateLabel, hasChars, mergeAttempts, progressLine } from './utils.js';

let _uid = 0;
function nextId() { return 'st_' + (++_uid); }

const CHAR_BOX = { x: 72, y: -2, w: 76 };
const TRANSIENT_MS = 2000;
export const baseStates = {};
export const transients = {};

export function effectiveState(key) {
  const t = transients[key];
  return t && t.until > Date.now() ? t.state : (baseStates[key] || 'idle');
}

export function flashChar(key, state, duration = TRANSIENT_MS) {
  if (!key) return;
  transients[key] = { state, until: Date.now() + duration };
  syncChars();
  setTimeout(syncChars, duration + 30);
}

export function syncChars() {
  if (!hasChars()) return;
  document.querySelectorAll('svg.xc[data-char-key]').forEach(el => {
    const key = el.getAttribute('data-char-key');
    const want = effectiveState(key);
    if (el.getAttribute('data-state') !== want) {
      window.XayChars.setState(el, want);
    }
  });
}

function charAgent(agentType) {
  return agentType === 'gm' ? 'claude' : agentType;
}

// 角色库只认 working/idle/waiting/blocked/failed/sleeping 等几种动作；员工的其他状态换成最接近的一种，免得人不见了
const CHAR_STATE = { offline: 'sleeping', queued: 'waiting', compacting: 'working' };
function charState(state) { return CHAR_STATE[state] || state; }

function renderCharInDesk(agentType, key) {
  const state = effectiveState(key);
  const svg = window.XayChars.render(charAgent(agentType), { state, size: CHAR_BOX.w });
  return svg.replace('<svg ', `<svg x="${CHAR_BOX.x}" y="${CHAR_BOX.y}" data-char-key="${escapeHtml(key)}" `);
}

function renderCharacterSvg(agentType, state) {
  const typingCls = state === 'working' ? ' anim-typing' : '';
  if (agentType === 'codex') {
    return `
      <g class="${typingCls}" transform-origin="108 42">
        <line x1="108" y1="23" x2="108" y2="16" stroke="#10A37F" stroke-width="2"/>
        <circle cx="108" cy="15" r="2.5" fill="#10A37F"/>
        <rect x="94" y="23" width="28" height="24" rx="4" fill="#18231E" stroke="#10A37F" stroke-width="2"/>
        <rect x="98" y="32" width="20" height="6" rx="2" fill="#10A37F"/>
        <circle cx="104" cy="35" r="1.5" fill="#E6FAED"/>
        <path d="M96 47 L120 47 L124 66 L92 66 Z" fill="#202F28"/>
        <circle cx="93" cy="67" r="3" fill="#10A37F"/>
        <circle cx="123" cy="67" r="3" fill="#10A37F"/>
      </g>`;
  } else if (agentType === 'dsh') {
    return `
      <g class="${typingCls}" transform-origin="108 42">
        <polygon points="94,34 88,28 92,38" fill="#4D6BFE"/>
        <polygon points="122,34 128,28 124,38" fill="#4D6BFE"/>
        <ellipse cx="108" cy="34" rx="14" ry="12" fill="#171F36" stroke="#4D6BFE" stroke-width="2"/>
        <rect x="99" y="32" width="6" height="4" rx="1.5" fill="#627EFE"/>
        <rect x="111" y="32" width="6" height="4" rx="1.5" fill="#627EFE"/>
        <path d="M96 46 Q108 49 120 46 L123 66 L93 66 Z" fill="#1D2644"/>
        <circle cx="94" cy="67" r="3" fill="#627EFE"/>
        <circle cx="122" cy="67" r="3" fill="#627EFE"/>
      </g>`;
  } else if (agentType === 'agy') {
    return `
      <g class="${typingCls}" transform-origin="108 42">
        <ellipse cx="108" cy="33" rx="18" ry="6" fill="none" stroke="#AB86D8" stroke-width="1.5" stroke-dasharray="4 2"/>
        <polygon points="108,18 122,33 108,48 94,33" fill="#241B33" stroke="#9B72CB" stroke-width="2"/>
        <circle cx="108" cy="33" r="3.5" fill="#E9D5FF"/>
        <path d="M98 48 Q108 51 118 48 L121 66 L95 66 Z" fill="#291E3B"/>
        <circle cx="95" cy="67" r="3" fill="#AB86D8"/>
        <circle cx="121" cy="67" r="3" fill="#AB86D8"/>
      </g>`;
  } else {
    return `
      <g class="${typingCls}" transform-origin="108 42">
        <circle cx="108" cy="33" r="13" fill="#25282E" stroke="#81858C" stroke-width="2"/>
        <circle cx="104" cy="33" r="2" fill="#ADB2B8"/>
        <circle cx="112" cy="33" r="2" fill="#ADB2B8"/>
        <path d="M97 46 L119 46 L122 66 L94 66 Z" fill="#2D3037"/>
        <circle cx="95" cy="67" r="3" fill="#81858C"/>
        <circle cx="121" cy="67" r="3" fill="#81858C"/>
      </g>`;
  }
}

export function renderDeskSvg(agentType, state, charKey) {
  const uid = nextId();
  const useChars = hasChars() && charKey;
  const charSvg = useChars ? renderCharInDesk(agentType, charKey) : renderCharacterSvg(agentType, state);

  let monitorContent = '';
  let extraEffects = '';

  if (state === 'working') {
    monitorContent = `
      <clipPath id="clip-${uid}"><rect x="22" y="26" width="54" height="36" rx="2"/></clipPath>
      <g clip-path="url(#clip-${uid})">
        <g class="anim-scroll-lines">
          <line x1="26" y1="31" x2="65" y2="31" stroke="#22C55E" stroke-width="2" stroke-linecap="round"/>
          <line x1="26" y1="37" x2="55" y2="37" stroke="#4D6BFE" stroke-width="2" stroke-linecap="round"/>
          <line x1="26" y1="43" x2="70" y2="43" stroke="#F59E0B" stroke-width="2" stroke-linecap="round"/>
          <line x1="26" y1="49" x2="48" y2="49" stroke="#9B72CB" stroke-width="2" stroke-linecap="round"/>
          <line x1="26" y1="55" x2="62" y2="55" stroke="#22C55E" stroke-width="2" stroke-linecap="round"/>
          <line x1="26" y1="63" x2="65" y2="63" stroke="#22C55E" stroke-width="2" stroke-linecap="round"/>
          <line x1="26" y1="69" x2="55" y2="69" stroke="#4D6BFE" stroke-width="2" stroke-linecap="round"/>
        </g>
      </g>`;
  } else if (state === 'sleeping') {
    monitorContent = `<rect x="20" y="24" width="58" height="40" rx="3" fill="#0B0D11" opacity="0.45"/>`;
    extraEffects = `
      <g class="anim-zzz"><text x="115" y="21" font-size="11" font-weight="bold" fill="var(--label-3)" font-family="var(--mono)">z</text></g>
      <g class="anim-zzz-2"><text x="123" y="15" font-size="9" font-weight="bold" fill="var(--label-3)" font-family="var(--mono)">z</text></g>`;
  } else if (state === 'blocked') {
    monitorContent = `
      <line x1="26" y1="36" x2="60" y2="36" stroke="#EF4444" stroke-width="2" stroke-dasharray="3 3"/>
      <line x1="26" y1="44" x2="45" y2="44" stroke="#EF4444" stroke-width="2" stroke-dasharray="3 3"/>`;
    extraEffects = `
      <g transform="translate(108, 12)"><g class="anim-question">
        <circle cx="0" cy="0" r="8.5" fill="#EC1313"/>
        <text x="0" y="4" text-anchor="middle" font-size="11" font-weight="900" fill="#FFFFFF" font-family="var(--sans)">?</text>
      </g></g>`;
  } else if (state === 'failed') {
    monitorContent = `
      <rect x="20" y="24" width="58" height="40" rx="3" class="anim-red-monitor"/>
      <text x="49" y="49" text-anchor="middle" font-size="18" font-weight="900" fill="#FFFFFF" font-family="var(--sans)">!</text>`;
  } else if (state === 'waiting' || state === 'queued') {
    monitorContent = `
      <g transform="translate(49, 44)"><g class="anim-hourglass">
        <path d="M-6 -8 L6 -8 L1 -1 L6 6 L-6 6 L-1 -1 Z" fill="#F59E0B" stroke="#B45309" stroke-width="0.8"/>
      </g></g>`;
  } else if (state === 'compacting') {
    monitorContent = `
      <rect x="20" y="24" width="58" height="40" rx="3" fill="#2C2F36"/>
      <text x="49" y="48" text-anchor="middle" font-size="11" font-weight="700" fill="#9B72CB">整理记忆</text>`;
  } else {
    monitorContent = `
      <text x="26" y="42" font-size="11" fill="#10A37F" font-family="var(--mono)">&gt;_</text>
      <line x1="26" y1="48" x2="50" y2="48" stroke="var(--border-3)" stroke-width="1.5"/>`;
  }

  return `
    <svg class="station-svg" viewBox="0 0 150 102" width="100%" height="100%" preserveAspectRatio="xMidYMid meet">
      <defs>
        <filter id="shadow-${uid}" x="-10%" y="-10%" width="120%" height="130%">
          <feDropShadow dx="0" dy="2" stdDeviation="2" flood-opacity="0.12"/>
        </filter>
      </defs>
      ${charSvg}
      <g filter="url(#shadow-${uid})">
        <rect x="10" y="66" width="130" height="9" rx="3" fill="var(--bg-layer-2)" stroke="var(--border-3)" stroke-width="1"/>
        <rect x="18" y="75" width="5" height="24" rx="1.5" fill="var(--border-3)"/>
        <rect x="127" y="75" width="5" height="24" rx="1.5" fill="var(--border-3)"/>
        <line x1="23" y1="84" x2="127" y2="84" stroke="var(--border-2)" stroke-width="1.5"/>
      </g>
      <rect x="86" y="68" width="38" height="4" rx="1" fill="var(--bg-module)" stroke="var(--border-3)" stroke-width="0.5"/>
      <rect x="46" y="58" width="6" height="9" fill="var(--border-3)"/>
      <rect x="38" y="66" width="22" height="2" rx="1" fill="var(--border-4)"/>
      <rect x="18" y="22" width="62" height="44" rx="4" fill="#16181D" stroke="var(--border-4)" stroke-width="1.5" filter="url(#shadow-${uid})"/>
      <rect x="20" y="24" width="58" height="40" rx="2.5" fill="#0D0F12"/>
      ${monitorContent}
      ${useChars ? '' : extraEffects}
    </svg>`;
}

// ① 记忆条（context.pct，calibrating 时显示"等校准"，estimated 时加"约"）
export function renderContextBar(ctx) {
  if (!ctx) return '';
  const pct = Math.min(100, Math.max(0, Number(ctx.pct) || 0));
  let labelText = '';
  let isCalibrating = !!ctx.calibrating;
  if (isCalibrating) {
    labelText = '等校准';
  } else if (ctx.estimated) {
    labelText = `约 ${pct}%`;
  } else {
    labelText = `${pct}%`;
  }

  const warnClass = pct >= 80 ? 'ctx-danger' : pct >= 60 ? 'ctx-warn' : 'ctx-normal';
  const calibClass = isCalibrating ? 'ctx-calibrating' : '';

  return `
    <div class="station-context-wrap ${warnClass} ${calibClass}" title="记忆用量：${labelText}${ctx.segment ? ` · 第 ${ctx.segment} 段` : ''}${ctx.compactions ? ` · 压缩 ${ctx.compactions} 次` : ''}">
      <div class="context-bar-track">
        <div class="context-bar-fill" style="width:${isCalibrating ? 100 : pct}%;"></div>
      </div>
      <span class="context-label">${escapeHtml(labelText)}</span>
    </div>`;
}

// ② 头顶思考小气泡（thought 非空时显示一句，空则不显示）
export function renderThoughtBubble(thought) {
  if (!thought || !String(thought).trim()) return '';
  const safeText = escapeHtml(thought.trim());
  return `
    <div class="station-thought-bubble" title="${safeText}">
      <span class="thought-icon">💭</span>
      <span class="thought-text">${safeText}</span>
    </div>`;
}

// ③ 步骤（"第 2 步 / 共 4 步：写测试"）
// 第几步：只在干活（或整理记忆）时显示；文字去掉【进度】标记、反引号和重复的"第 x 步"，只留一句人话
export function renderProgressStep(progress, state = 'working') {
  if (!progress || progress.step === undefined || progress.of === undefined) return '';
  if (state !== 'working' && state !== 'compacting') return '';
  const text = progressLine(progress.text);
  const stepText = `第 ${progress.step} 步 / 共 ${progress.of} 步${text ? '：' + text : ''}`;
  return `
    <div class="station-progress-step" title="${escapeHtml(stepText)}">
      <span class="step-badge">${progress.step}/${progress.of}</span>
      <span class="step-text">${escapeHtml(text)}</span>
    </div>`;
}

// 任务清单：只取 snapshot.tasks 里 owner 是他的活（含旧系统的活）。
// 和左侧详情栏同一条合并规则（utils.js mergeAttempts）：同一件事试了几次算一件；撤回的不列；正在做的排最前，其余最近动静的在前。
// 最多 3 行：超过 3 件时列 2 件，第 3 行写"还有 N 件"。标题只用短标题（去掉写给 AI 的要求）。
const TODO_CLASS = {
  working: 'running', done: 'done', reported: 'done', failed: 'failed', canceled: 'canceled',
  needs_you: 'warn', queued: 'queued', ready: 'queued', planned: 'queued'
};
const TODO_SHOWN = 3;

function taskListHtml(tasks = [], activity = null) {
  const stamp = id => (activity && activity.tasks && activity.tasks[id]) || 0;
  const groups = mergeAttempts(tasks).filter(g => g.status !== 'canceled');
  if (!groups.length) {
    return '<ul class="todo"><li class="todo-idle"><i></i><span>暂无任务</span></li></ul>';
  }
  const sorted = groups
    .map((g, i) => ({ g, i, s: Math.max(0, ...g.ids.map(stamp)) }))
    .sort((a, b) => ((b.g.status === 'working') - (a.g.status === 'working')) || (b.s - a.s) || (b.i - a.i))
    .map(x => x.g);
  const shown = sorted.length > TODO_SHOWN ? sorted.slice(0, TODO_SHOWN - 1) : sorted;
  const more = sorted.length - shown.length;
  return `
    <ul class="todo">
      ${shown.map(g => {
        const st = TODO_CLASS[g.status] || 'queued';
        const tip = g.short + (g.tries > 1 ? `（试了 ${g.tries} 次）` : '');
        return `<li class="todo-${st}"><i></i><span title="${escapeHtml(tip)}">${escapeHtml(g.short)}</span></li>`;
      }).join('')}
      ${more > 0 ? `<li class="todo-more">还有 ${more} 件</li>` : ''}
    </ul>`;
}

// ---------- 就地更新（2026-10-31 画布改版） ----------
// 页面每次拿到新快照都走这里。数据没变的块一个节点都不动，角色、工位、流光的动画就不会从第一帧重放：
//   · 状态牌、排队牌、思考气泡、名字、第几步、小清单、记忆条：每块单独比 HTML，变了才换那一块；
//   · 桌子（.station-svg-wrap > svg.station-svg）：只有状态变了才整个换掉（彩蛋层会被它自己搬到新桌子上）；
//   · 角色（svg.xc）：状态变了只用 XayChars.setState 改属性（syncChars），不重建。
export function htmlToEl(html) {
  const t = document.createElement('template');
  t.innerHTML = String(html).trim();
  return t.content.firstElementChild;
}

// 按顺序摆好几块"槽"：[名字, HTML]；HTML 为空就拿掉；和上次一样就不动；只换变了的那块。不认识的子节点原样留着
export function patchSlots(parent, slots) {
  let prev = null;
  for (const [name, html] of slots) {
    const cur = [...parent.children].find(c => c.getAttribute('data-slot') === name) || null;
    if (cur && cur._html === html) { prev = cur; continue; }
    if (!html) { if (cur) cur.remove(); continue; }
    const el = htmlToEl(html);
    if (!el) continue;
    el.setAttribute('data-slot', name);
    el._html = html;
    if (cur) cur.replaceWith(el);
    else parent.insertBefore(el, prev ? prev.nextSibling : parent.firstChild);
    prev = el;
  }
}

export function setAttrs(el, attrs) {
  Object.entries(attrs).forEach(([k, v]) => {
    if (el.getAttribute(k) !== v) el.setAttribute(k, v);
  });
}

// 一个工位的各块内容（纯字符串）
function stationParts(worker, deptId, allTasks = [], activity = null, extra = {}) {
  const agentType = getAgentType(worker.agent);
  const state = worker.display_state || worker.state || 'idle';
  const charKey = worker.key || ((deptId || '') + '/' + (worker.label || ''));
  baseStates[charKey] = charState(state);
  const label = escapeHtml(worker.label || worker.key || '员工');
  // ④ 队列里还有几件（queue 长度）
  const queueLen = (worker.queue && Array.isArray(worker.queue)) ? worker.queue.length : 0;
  const unread = extra.unread || 0;
  return {
    agentType, state, charKey,
    // data-em / data-agent / data-state / data-task-title 给附加效果（desk-fx）用：每次更新都跟着最新状态走
    attrs: {
      'data-dept': String(deptId || ''), 'data-em': String(worker.key || ''), 'data-agent': String(worker.agent || ''),
      'data-state': String(state), 'data-task-title': String(worker.now || ''), title: `点开看 ${worker.label || worker.key || '员工'} 做过的事`
    },
    top: [
      ['badge', `<span class="station-state-badge state-${escapeHtml(state)}"><span class="dot"></span>${escapeHtml(getStateLabel(state))}</span>`],
      ['queue', queueLen > 0 ? `<span class="station-queue-badge" title="队列还有 ${queueLen} 件任务">排队 ${queueLen}</span>` : ''],
      ['mail', unread > 0 ? `<button type="button" class="station-mail" data-letter="${escapeHtml(extra.letter || '')}" title="有 ${unread} 封信还没送到他手上，点开看">✉ ${unread}</button>` : ''],
      ['thought', renderThoughtBubble(worker.thought).trim()]
    ],
    meta: [
      ['name', `<div class="station-name-row"><span class="station-worker-name name-${agentType}" title="${label}">${label}</span></div>`],
      ['progress', renderProgressStep(worker.progress, state).trim()],
      ['todo', taskListHtml(allTasks.filter(t => t.owner === worker.key), activity).trim()],
      ['context', renderContextBar(worker.context).trim()]
    ]
  };
}

// 新建一张工位卡片（空壳 + 各块），之后都用 patchStationCard 就地更新
export function createStationCard(worker, convId, deptId, allTasks = [], activity = null, extra = {}) {
  const card = htmlToEl(`
    <div class="station-card" id="station-${escapeHtml(worker.key)}" data-worker-key="${escapeHtml(worker.key)}" data-dept="${escapeHtml(deptId || '')}"
         data-em="${escapeHtml(worker.key)}" data-agent="${escapeHtml(worker.agent || '')}" data-state="" data-task-title="">
      <div class="station-svg-wrap"></div>
      <div class="station-meta"></div>
    </div>`);
  patchStationCard(card, worker, convId, deptId, allTasks, activity, extra);
  return card;
}

export function patchStationCard(card, worker, convId, deptId, allTasks = [], activity = null, extra = {}) {
  const p = stationParts(worker, deptId, allTasks, activity, extra);
  setAttrs(card, p.attrs);
  patchSlots(card, p.top);
  const wrap = card.querySelector(':scope > .station-svg-wrap');
  const deskKey = p.agentType + '|' + p.state + '|' + (hasChars() ? 'c' : 's');
  if (wrap && wrap._desk !== deskKey) {
    wrap._desk = deskKey;
    wrap.replaceChildren(htmlToEl(renderDeskSvg(p.agentType, p.state, p.charKey)));
  }
  const meta = card.querySelector(':scope > .station-meta');
  if (meta) patchSlots(meta, p.meta);
}
