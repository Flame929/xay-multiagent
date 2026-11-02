// render-office.js - 办公室场景渲染：负责人、休息区、小任务房间（2026-10-31 起一个小任务一间房）、监工席、组织树、写信飞行、彩带、记忆整理动画
import { escapeHtml, hasChars, reducedMotion, formatClock, shortTitle, avatarHtml, getAgentType,
  getStateLabel, dutyText, spanText, thoughtWords } from './utils.js';
import { buildPlan, partChipHtml, partTip, checkChipHtml, isDone, statusIconHtml, whoText } from './plan-model.js';
import { revealInOffice } from './office-view.js';
import { renderDeskSvg, createStationCard, patchStationCard, htmlToEl, patchSlots, setAttrs, flashChar, baseStates, effectiveState } from './station.js';


const ROBOT_SVG = `
  <svg class="robot" viewBox="0 0 60 64" width="52" height="56" aria-hidden="true">
    <line x1="30" y1="5" x2="30" y2="12" stroke="#2b2b2b" stroke-width="2"/>
    <circle class="robot-bulb" cx="30" cy="5" r="3.5" fill="#F59E0B" stroke="#2b2b2b" stroke-width="1.5"/>
    <rect x="10" y="12" width="40" height="28" rx="9" fill="#cfd8e3" stroke="#2b2b2b" stroke-width="2"/>
    <rect x="15" y="17" width="30" height="17" rx="6" fill="#1f2937"/>
    <g class="robot-eyes"><circle cx="24" cy="25" r="3" fill="#5eead4"/><circle cx="36" cy="25" r="3" fill="#5eead4"/></g>
    <path d="M25 30q5 3 10 0" stroke="#5eead4" stroke-width="1.8" fill="none" stroke-linecap="round"/>
    <path d="M12 47l-6 7M48 47l6 7" stroke="#2b2b2b" stroke-width="3" stroke-linecap="round"/>
    <rect x="16" y="42" width="28" height="19" rx="6" fill="#aab6c5" stroke="#2b2b2b" stroke-width="2"/>
    <rect x="23" y="46" width="14" height="10" rx="2" fill="#fff" stroke="#2b2b2b" stroke-width="1.2"/>
    <path d="M26 49h8M26 52h6" stroke="#9aa3ad" stroke-width="1.2"/>
  </svg>`;

// ---------- 负责人 ----------
// 卡片上：状态（在派活 / 在验收 / 在等员工交活 / 空闲）+ 他最近一两个动作（快照 gm.recent：本对话 by=lead:* 的事件）。
// 拿不到的不编：没有动作就写"暂无动作"。第一次画出空壳，之后只改变了的地方；桌子只有状态变了才换。
const GM_VERBS = {
  'task.added': '拆任务', 'task.changed': '改计划', 'task.removed': '改计划', 'proposal.decided': '改计划',
  'task.assigned': '派活', 'task.accepted': '验收', 'task.canceled': '撤活', 'letter.sent': '写信',
  'letter.answered': '回信', 'letter.delivered': '看信', 'decision.resolved': '拿主意', 'conv.begun': '开工',
  'dept.opened': '安排人手', 'employee.hired': '安排人手', 'employee.joined': '安排人手', 'employee.fired': '安排人手',
  'control.requested': '管员工'
};
const GM_BUSY_SECONDS = 180;     // 最近一个动作在 3 分钟内：算他"正在"做这件事

export function gmVerb(act) {
  if (!act) return '';
  if (act.type === 'letter.sent' && act.reply) return '回信';
  return GM_VERBS[act.type] || '处理事务';
}

// "文档部 · 负责人验收通过了「X」" → "通过了「X」"：卡片上已经写着是负责人、前面的小标签已经写着"验收"，不再重复
function gmActText(text, verb) {
  const t = String(text || '').replace(/^[^·「」]{1,16} · /, '').replace(/^(?:负责人|总经理)\s*/, '').replace(/^给 (.{1,24}?) 写信：/, '给 $1：');
  return verb && t.startsWith(verb) && t.length > verb.length + 2 ? t.slice(verb.length) : t;
}

function agoText(ts, now) {
  if (!ts) return '';
  const sec = Math.max(0, (now || Date.now() / 1000) - ts);
  if (sec < 60) return '刚刚';
  if (sec < 3600) return `${Math.floor(sec / 60)} 分钟前`;
  return formatClock(ts, now);
}

export function renderGmZone(gm = {}, convId = '', opts = {}) {
  const gmZone = document.getElementById('gm-zone');
  if (!gmZone) return;

  const now = opts.now || Date.now() / 1000;
  const acts = Array.isArray(gm.recent) ? gm.recent : [];
  const last = acts[0];
  const fresh = last && now - last.ts < GM_BUSY_SECONDS;
  const working = Object.values(opts.people || {}).filter(p => p.state === 'working' || p.display_state === 'working').length;
  const gmState = fresh ? 'working' : (gm.state || 'idle');
  baseStates.gm = gmState;

  let box = document.getElementById('node-gm');
  if (!box || box.parentNode !== gmZone) {
    gmZone.replaceChildren(htmlToEl(`
      <div class="gm-station-box" id="node-gm" data-node="gm">
        <div class="gm-desk-wrap"></div>
        <div class="gm-info"></div>
      </div>`));
    box = document.getElementById('node-gm');
  }
  setAttrs(box, { title: '负责人（' + String(gm.agent || 'claude') + '）：拆任务、派活、看信回信、验收' });

  // 状态：刚做了什么就写"在 X"；员工在干活就是"在等 N 人交活"；都没有就"空闲"
  const chip = fresh
    ? `<span class="gm-chip on"><i></i>在${escapeHtml(gmVerb(last))}</span>`
    : working
      ? `<span class="gm-chip wait"><i></i>在等 ${working} 人交活</span>`
      : '<span class="gm-chip"><i></i>空闲</span>';
  const unread = opts.unread || 0;
  const mail = unread > 0
    ? `<button type="button" class="gm-mail" data-letter="${escapeHtml(opts.unreadLetter || '')}" title="有 ${unread} 封写给负责人的信还没看">✉ ${unread} 封没看</button>`
    : '';
  const list = acts.slice(0, 2);
  const actsHtml = list.length
    ? `<ul class="gm-acts">${list.map((a, i) => `
        <li class="${i === 0 ? 'latest' : ''}">
          <b>${escapeHtml(gmVerb(a))}</b>
          <span title="${escapeHtml(a.text)}">${escapeHtml(gmActText(a.text, gmVerb(a)))}</span>
          <time>${escapeHtml(agoText(a.ts, now))}</time>
        </li>`).join('')}</ul>`
    : '<ul class="gm-acts"><li class="none">暂无动作</li></ul>';
  patchSlots(box.querySelector('.gm-info'), [['head', `<div class="gm-head"><span class="gm-name">负责人</span>${chip}${mail}</div>`], ['acts', actsHtml]]);

  const wrap = box.querySelector('.gm-desk-wrap');
  const deskKey = gmState + '|' + (hasChars() ? 'c' : 's');
  if (wrap._desk !== deskKey) {
    wrap._desk = deskKey;
    wrap.replaceChildren(htmlToEl(renderDeskSvg('gm', gmState, 'gm')));
  }

  // 后台按规则自动给员工下指令、写信的（账本里的 office / rules：没做成让他接着修、文件重叠提醒、预算到了让他收尾、叫停 / 恢复…），
  // 页面上一律是"监工"：对方坐在哪间房，虚线就连那间房的监工席（node-head-<房间>，见 mailModel）。
  // 只有对方不在任何有监工席的房间里（休息区的人、负责人、设了不要监工的旧部门）时，才在负责人右边放一位监工，虚线连到她。
  // 节点编号仍叫 node-office / 'office'（和账本里的身份对上），给人看的名字和形象都是监工（XayChars 的 'rules'）。
  let office = document.getElementById('node-office');
  if (opts.office) {
    const faceKey = hasChars() ? 'c' : 's';
    if (!office || office._face !== faceKey) {
      const face = hasChars() ? window.XayChars.render('rules', { state: 'idle', size: 52 }) : ROBOT_SVG;
      const fresh = htmlToEl(`
        <div class="office-node" id="node-office" data-node="office" title="监工：按规则自动给员工下指令（没做成让他接着修、文件重叠提醒、预算到了让他收尾、叫停 / 恢复）。这几封信的收信人不在哪间房里，所以从这里发">
          ${face}
          <div class="office-name">监工</div>
        </div>`);
      fresh._face = faceKey;
      if (office) office.replaceWith(fresh); else gmZone.appendChild(fresh);
      office = fresh;
    }
  } else if (office) {
    office.remove();
  }
  gmZone.classList.toggle('has-office', !!opts.office);
}

function headDeskHtml(d, isClaudeHead, headText, check) {
  const key = 'head:' + (d.id || '');
  let face = ROBOT_SVG;
  if (hasChars()) {
    face = window.XayChars.render(isClaudeHead ? 'claude' : 'rules', {
      state: effectiveState(key),
      size: 64
    }).replace('<svg ', `<svg data-char-key="${escapeHtml(key)}" `);
  }
  return `
    <div class="head-desk" id="node-head-${escapeHtml(d.id || '')}" title="${escapeHtml(headText)}">
      ${face}
      <div class="head-title">${isClaudeHead ? 'Claude 监工' : '监工'}</div>
      ${check ? `<div class="head-check k-${escapeHtml(check.kind)}">${escapeHtml(check.word)}</div>` : ''}
    </div>`;
}

// ---------- 房间 = 小任务（2026-10-31，references/plans/arch-v10-parts.md）----------
// 一个小任务一间房：房间牌子写小任务名、状态、做完几件；房间里有部门就按部门分小区，没部门的人坐一起；每间房一个监工席，旁边写它怎么验收。
// 员工坐在他手上那件活（正在做 → 排队 → 最近做的）所在的小任务房间里；这个对话里一件活都没有的人坐负责人旁边的休息区。
// 有先后的小任务一排一排往下摆（左边一条"第 1 步 / 第 2 步…"的竖轴），能同时做的并排在同一排。
// 编号（第 1 步、2-A / 2-B）、名字、颜色、状态、验收说法都从共用的计划模型（plan-model.js）取，和画布、任务拆解、计划版本一模一样。
// 没拆小任务的对话（以前按部门当小任务的旧对话也一样）：整个对话一间房（全部的活），不再一个部门一间房。
const PART_ROOM_STATE = { planned: 'wait', ready: 'ready', working: 'run', needs_you: 'warn', done: 'ok', failed: 'err' };

function roomKeyOf(partId) {
  const id = String(partId ?? '');
  if (id === '') return '_loose';
  if (id === 'all') return '_all';
  return id.startsWith('dept:') ? id.slice(5) : id;
}

export { partOfTask } from './plan-model.js';

const PICK_ORDER = { working: 0, needs_you: 1, queued: 2 };

// 他该坐哪件活那儿：正在做的 → 等你决定的 → 排队的（按队列先后）→ 最近做的（做完 / 没做成）→ 还没开始的
function seatTask(p, own, activity) {
  if (!own.length) return null;
  const stamp = t => (activity && activity.tasks && activity.tasks[t.id]) || 0;
  const queue = (p && p.queue) || [];
  const rank = t => {
    if (t.status in PICK_ORDER) return [PICK_ORDER[t.status], t.status === 'queued' && queue.includes(t.id) ? queue.indexOf(t.id) : 0];
    if (['done', 'reported', 'failed'].includes(t.status)) return [3, -stamp(t)];
    return [4, 0];
  };
  return own.map((t, i) => ({ t, r: rank(t), i }))
    .sort((a, b) => a.r[0] - b.r[0] || a.r[1] - b.r[1] || (a.r[0] === 3 ? b.i - a.i : a.i - b.i))[0].t;
}

/**
 * 快照 → 公司页怎么摆：{mode: 'parts', rooms:[…], rest:[员工编号], seat: Map(员工 → 房间编号), model（计划模型）}。
 * 一间房 = 一个中任务（部门 / 组，各有监工）：{key, part, code, color, title, col, row, n（第几步）, status, check（原样）, checkInfo（验收说法）,
 *   depts:[部门]（只给信件连线找监工席用）, zones: null（2026-10-31 起房间里不再按部门分小区，大家坐一起）,
 *   keys:[坐这儿的人], tasks:[这个中任务里的小任务], all, done, head（有没有监工席）, loose（零散的）}。纯函数，node 里也能测。
 * pm 是 buildPlan(快照)（app.js 已经算好的就传进来，不给就现算）。
 */
export function roomPlan(data, pm = null) {
  const people = involvedPeople(data);
  const model = pm || buildPlan(data || {}, null);
  const tasks = ((data && data.tasks) || []).filter(t => t && t.status !== 'canceled');
  const allDepts = (data && data.depts) || [];
  const deptById = new Map(allDepts.map(d => [d.id, d]));
  const whereTask = t => { const it = model.itemOf(t.id); return it ? it.part : null; };
  // 没有小任务的"零散"组不画（它只是个兜底的格子）
  const rooms = model.parts.filter(p => !(p.loose && !p.all)).map(p => {
    const depts = (p.deptIds || []).map(id => deptById.get(id) || { id, name: id, counts: {} });
    return {
      key: roomKeyOf(p.id), part: p.id, code: p.code, color: p.color, title: p.title, model: p,
      col: p.col, row: p.row, n: p.n || (Math.max(0, ...model.parts.map(x => x.n)) + 1), after: p.after,
      status: p.status, check: (p.raw && p.raw.check) || { kind: 'rules' }, checkInfo: p.check, count: null, depts, loose: p.loose,
      head: true, claude: false, all: p.all, done: p.done,
      tasks: tasks.filter(t => whereTask(t) === p.id), keys: [], zones: null
    };
  });
  const byPart = new Map(rooms.map(r => [r.part, r]));
  const seat = new Map();
  const sleepers = new Set();
  const rest = [];
  // 谁坐哪（office-spec.md 二.6）：正在做 → 排队 → 最近做完的那件小任务所在的中任务房间。做完了就留在原房间睡觉；
  // 只有又被派到别的中任务里干活（正在做 / 排队）时才搬过去。同一个人同时只坐一个房间
  Object.keys(people).forEach(k => {
    const p = people[k];
    const t = seatTask(p, tasks.filter(x => x.owner === k), data && data.activity);
    const r = t ? byPart.get(whereTask(t)) : null;
    if (!r) { rest.push(k); return; }
    seat.set(k, r.key);
    if (isDone(t.status)) sleepers.add(k);
  });
  const order = Object.keys(people);
  // 部门不再是中任务里的一层（用户 2026-10-31 纠正：部门就是中任务本身）：房间里大家坐一起，不分小区。
  // 一个人都不剩的房间收起成一行简单记录（编号 + 名字 + 做完几件 + 验收方式），监工也一起下班（不画监工席）
  rooms.forEach(r => {
    r.keys = order.filter(k => seat.get(k) === r.key);
    r.folded = r.keys.length === 0;
    if (r.folded) r.head = false;
  });
  return { mode: 'parts', rooms, rest: rest.filter(k => people[k]), seat, sleepers, people, model };
}

// 房间牌子：小任务名、状态、几个人、待答、做完几件
function roomHeadHtml(r, { mission, busy, nPeople, openReqs, jobs, doneCount, showState }) {
  const tone = PART_ROOM_STATE[r.status] || 'wait';
  const word = r.model ? r.model.word : '';
  return `
          <div class="room-line1"><span class="room-name room-sign has-chip" title="${escapeHtml(mission)}">${r.model ? partChipHtml(r.model, { cls: 'big', tip: false }) : escapeHtml(r.title)}</span></div>
          <div class="room-line2">
            ${showState && r.status ? `<span class="room-state s-${tone}">${escapeHtml(word)}</span>` : ''}
            ${r.checkInfo ? checkChipHtml(r.checkInfo) : ''}
            <span class="room-pill">${busy ? '<span class="live"></span>' : ''}${nPeople} 人</span>
            ${openReqs > 0 ? `<span class="room-pill warn">${openReqs} 待答</span>` : ''}
            ${jobs ? `<span class="room-progress-text" title="做完 ${doneCount} / ${jobs} 件">${showState ? `做完 ${doneCount}/${jobs} 件` : `${doneCount}/${jobs}`}</span>` : ''}
          </div>
          ${jobs ? `<span class="room-progress" aria-hidden="true"><i style="width:${Math.round(doneCount * 100 / jobs)}%"></i></span>` : ''}`;
}

// 一间办公室：先算出这一间要显示什么（编号、名字、颜色、做完几件、验收说法都是计划模型里的）
function roomModel(r, idx, ctx) {
  const { people } = ctx;
  const busy = r.keys.some(k => people[k] && people[k].state === 'working');
  const openReqs = r.depts.reduce((s, d) => s + ((d.counts && d.counts.open_requests) || 0), 0);
  const chk = r.checkInfo;
  const mission = r.model ? partTip(r.model) : r.title;
  const headText = `监工：${chk.tip}`;
  return {
    nodeId: r.key, deptAttr: '', color: r.color, busy,
    memberKeys: r.keys, zones: r.zones, extraClass: r.loose ? 'room-loose' : '',
    emptyText: r.status === 'done' ? '小任务都做完了，人去别处了' : r.status === 'planned' ? '前面的中任务做完才开工' : '还没人接这里的小任务',
    head: r.head ? { d: { id: r.key }, isClaude: false, text: headText, check: chk, state: openReqs > 0 ? 'blocked' : busy ? 'working' : 'idle' } : null,
    lead: !!(r.head && chk && chk.kind === 'lead'),
    headHtml: roomHeadHtml(r, { mission, busy, nPeople: r.keys.length, openReqs, jobs: r.all, doneCount: r.done, showState: true }),
    stamp: 0, current: true
  };
}

// 在岗时间（快照 people[员工].duty，2026-10-31）：工位卡片名字下面一小行"在岗 2 小时 10 分 · 今天干活 48 分"；没有 duty 就不显示。
// 工位卡片的其它几块由 station.js 管（按 data-slot 就地更新），这一行不带 data-slot，它们不会动它；位置每次对一下，总在名字下面
function patchDuty(card, p, now) {
  const meta = card && card.querySelector(':scope > .station-meta');
  if (!meta) return;
  const text = dutyText(p && p.duty, now);
  let el = meta.querySelector(':scope > .station-duty');
  if (!text) { if (el) el.remove(); return; }
  if (!el) { el = document.createElement('div'); el.className = 'station-duty'; }
  const on = !!(p.duty && p.duty.online_since);
  el.classList.toggle('off', !on);
  if (el.textContent !== text) el.textContent = text;
  const total = Number(p.duty.total_work_s) > 0 ? `一共干活 ${spanText(p.duty.total_work_s)}` : '';
  const tip = ['在岗时间', text, total].filter(Boolean).join('\n');
  if (el.title !== tip) el.title = tip;
  const name = meta.querySelector(':scope > [data-slot="name"]');
  const after = name || null;
  if (after ? after.nextSibling !== el : meta.firstChild !== el) meta.insertBefore(el, after ? after.nextSibling : meta.firstChild);
}

// 工位上"在想什么"的气泡（station.js 画的）：英文或像代码的，换成"正在琢磨怎么做"，原文放到鼠标悬停里。
// 气泡只在想法变了时由 station.js 换新的，这里每次就地把字改掉（已经改过的不再动）
function patchThought(card) {
  const t = card && card.querySelector('.station-thought-bubble .thought-text');
  if (!t || t.dataset.plain) return;
  const w = thoughtWords(t.textContent);
  t.dataset.plain = '1';
  if (!w.swapped) return;
  t.textContent = w.text;
  const bubble = t.closest('.station-thought-bubble');
  if (bubble) bubble.setAttribute('title', `他在想（原文）：${w.raw}`);
}

// 一个人都不剩的中任务：收起成一行简单记录（编号 + 名字 + 做完几件 + 验收方式；监工下班），点一下展开它的小任务清单
const foldOpen = new Set();      // 展开着的（刷新之间记住）

function foldEl(m, el) {
  const r = m.r;
  const p = r.model;
  const key = r.key;
  if (!el) {
    el = htmlToEl(`<div class="room-fold" data-dept-id="fold:${escapeHtml(key)}" id="node-fold-${escapeHtml(key)}">
      <button type="button" class="room-fold-head" data-fold="${escapeHtml(key)}"></button><div class="room-fold-list"></div></div>`);
  }
  if (el.style.getPropertyValue('--room-c') !== r.color) el.style.setProperty('--room-c', r.color);
  const open = foldOpen.has(key);
  const why = r.status === 'done' ? '小任务都做完了，大家在别的组干活，监工下班了'
    : r.status === 'planned' ? '前面的中任务做完才开工，现在还没人' : '这会儿没人在这组，监工下班了';
  const head = `${p ? partChipHtml(p, { tip: false }) : escapeHtml(r.title)}
    <span class="fold-count">做完 ${r.done}/${r.all} 件</span>${r.checkInfo ? checkChipHtml(r.checkInfo) : ''}
    <span class="fold-caret" aria-hidden="true">${open ? '▾' : '▸'}</span>`;
  const btn = el.querySelector(':scope > .room-fold-head');
  if (btn._html !== head) { btn._html = head; btn.innerHTML = head; }
  setAttrs(btn, { 'aria-expanded': String(open), title: `${why}。点一下${open ? '收起' : '看它的小任务'}` });
  el.classList.toggle('open', open);
  const items = p ? p.items : [];
  const list = open ? `<ul>${items.map(it => `<li class="t-${escapeHtml(it.tone)}">${statusIconHtml(it.tone, it.word)}<span class="fold-t">${escapeHtml(it.title)}</span><small>${escapeHtml(whoText(it))} · ${escapeHtml(it.word)}</small></li>`).join('') || '<li class="none">还没有小任务</li>'}</ul>` : '';
  const listEl = el.querySelector(':scope > .room-fold-list');
  if (listEl._html !== list) { listEl._html = list; listEl.innerHTML = list; }
  listEl.hidden = !open;
  return el;
}

function createRoom(m) {
  return htmlToEl(`
      <div class="dept-card room" id="node-dept-${escapeHtml(m.nodeId)}" data-node="dept" data-dept-id="${escapeHtml(m.nodeId)}">
        <div class="room-head"></div>
        <div class="room-floor">
          <div class="room-staff dept-members-grid"></div>
        </div>
      </div>`);
}

// 让 parent 的子节点按 list 的顺序排好：已经在对的位置上的不挪（挪动会让动画重放），多余的拿掉
function placeChildren(parent, list) {
  const keep = new Set(list);
  [...parent.children].forEach(c => { if (!keep.has(c)) c.remove(); });
  list.forEach((el, i) => {
    if (parent.children[i] !== el) parent.insertBefore(el, parent.children[i] || null);
  });
}

// 就地更新一间办公室：房间牌子、监工席、（小区）、工位一个个对上号（新来的加上、走了的拿掉、还在的只改变了的地方）
const BUSY_STATES = new Set(['working', 'queued', 'compacting', 'blocked', 'failed', 'waiting']);

function patchRoom(el, m, ctx) {
  el.classList.toggle('room-loose', m.extraClass === 'room-loose');
  el.classList.toggle('busy', !!m.busy);
  el.classList.toggle('has-zones', !!m.zones);
  if (el.style.getPropertyValue('--room-c') !== m.color) el.style.setProperty('--room-c', m.color);

  const head = el.querySelector(':scope > .room-head');
  if (head._html !== m.headHtml) {
    head._html = m.headHtml;
    head.innerHTML = m.headHtml;
  }

  // 监工席一列：上面监工，验收方式是"负责人亲自审"时正下方再放一个一模一样的格子（头像换成负责人，office-spec.md 二.10）
  const floor = el.querySelector(':scope > .room-floor');
  let col = floor.querySelector(':scope > .head-col');
  if (!col && (m.head || m.lead)) { col = htmlToEl('<div class="head-col"></div>'); floor.insertBefore(col, floor.firstChild); }
  const desk = col && col.querySelector(':scope > .head-desk:not(.lead-desk)');
  if (m.head) {
    baseStates['head:' + (m.head.d.id || '')] = m.head.state;          // 监工的表情由 syncChars 跟着改，不重建
    const kind = (m.head.isClaude ? 'claude' : 'rules') + '|' + (m.head.check ? m.head.check.word : '');
    if (!desk || desk._kind !== kind) {
      const fresh = htmlToEl(headDeskHtml(m.head.d, m.head.isClaude, m.head.text, m.head.check));
      fresh._kind = kind;
      if (desk) desk.replaceWith(fresh); else col.insertBefore(fresh, col.firstChild);
    } else if (desk.getAttribute('title') !== m.head.text) {
      desk.setAttribute('title', m.head.text);
    }
  } else if (desk) {
    desk.remove();
  }
  let lead = col && col.querySelector(':scope > .lead-desk');
  if (m.lead) {
    const key = 'lead:' + m.nodeId;
    baseStates[key] = 'idle';
    const want = 'h' + (hasChars() ? 1 : 0);
    if (!lead || lead._kind !== want) {
      const face = hasChars()
        ? window.XayChars.render('claude', { state: effectiveState(key), size: 64 }).replace('<svg ', `<svg data-char-key="${escapeHtml(key)}" `)
        : avatarHtml('gm', 56, 'idle');
      const fresh = htmlToEl(`
        <div class="head-desk lead-desk" id="node-lead-${escapeHtml(m.nodeId)}" title="这一组做完的小任务由负责人亲自审">
          ${face}
          <div class="head-title">负责人</div>
          <div class="head-check k-lead">亲自审这一组</div>
        </div>`);
      fresh._kind = want;
      if (lead) lead.replaceWith(fresh); else col.appendChild(fresh);
      lead = fresh;
    }
  } else if (lead) {
    lead.remove();
  }
  if (col && !col.children.length) col.remove();

  const staff = floor.querySelector(':scope > .room-staff');
  const have = new Map([...staff.querySelectorAll('.station-card')].map(c => [c.getAttribute('data-worker-key'), c]));
  const cardFor = (k, deptAttr) => {
    let p = ctx.people[k];
    // 做完了就留在原房间睡觉（office-spec.md 二.6）：他坐的那件做完了、这会儿也没在别处忙 → 休眠表情
    if (ctx.sleepers && ctx.sleepers.has(k) && !BUSY_STATES.has(p.display_state || p.state)) p = Object.assign({}, p, { display_state: 'sleeping' });
    const extra = ctx.mailFor ? ctx.mailFor(k) : {};
    let card = have.get(k);
    if (card) {
      have.delete(k);
      patchStationCard(card, p, ctx.convId, deptAttr, ctx.tasks, ctx.activity, extra);
    } else {
      card = createStationCard(p, ctx.convId, deptAttr, ctx.tasks, ctx.activity, extra);
      if (ctx.animate && !reducedMotion()) card.classList.add('fx-enter');     // 新来 / 搬进来的人：淡入（第一次画页面时不放）
    }
    patchDuty(card, p, ctx.now);
    patchThought(card);
    return card;
  };
  const empty = () => staff.querySelector(':scope > .dept-empty') || htmlToEl(`<div class="dept-empty">${escapeHtml(m.emptyText)}</div>`);
  if (!m.zones) {
    const want = m.memberKeys.map(k => cardFor(k, m.deptAttr));
    if (!want.length && m.emptyText) want.push(empty());
    placeChildren(staff, want);
    return;
  }
  // 按部门分小区：小区牌子 = 部门名；没进部门的人那块不写牌子
  const zoneEls = new Map([...staff.querySelectorAll(':scope > .room-zone')].map(z => [z.getAttribute('data-zone'), z]));
  const want = m.zones.map(z => {
    let zel = zoneEls.get(z.id);
    if (!zel) {
      zel = htmlToEl(`<div class="room-zone${z.id ? '' : ' zone-loose'}" data-zone="${escapeHtml(z.id)}"${z.id ? ` id="node-zone-${escapeHtml(z.id)}"` : ''}>
        <div class="zone-head"></div><div class="zone-staff"></div></div>`);
    }
    const zh = z.id ? `<b>${escapeHtml(z.name)}</b><span>${z.keys.length} 人</span>` : '<b>没进部门的</b>';
    const zhead = zel.querySelector(':scope > .zone-head');
    if (zhead._html !== zh) { zhead._html = zh; zhead.innerHTML = zh; }
    const cards = z.keys.map(k => cardFor(k, z.id));
    if (!cards.length) cards.push(zel.querySelector('.zone-empty') || htmlToEl('<div class="zone-empty">这会儿没人</div>'));
    placeChildren(zel.querySelector(':scope > .zone-staff'), cards);
    return zel;
  });
  placeChildren(staff, want);
}

let pastExpanded = false;      // "以往的部门"是否展开（页面刷新之间记住）
let onPastToggle = null;
let currentRooms = null;       // 最近一次摆出来的房间（信件栏点一封信时，用它找监工席）
let currentSeat = new Map();   // 员工 → 他坐的房间（房间收在一行里时，信件虚线、派活动画落到那一组的芯片上）
const stepOpen = new Set();    // 做完 / 还没开工的步，被点开了的（刷新之间记住）

// 这个对话里真正有关的人：某个部门的成员、本对话里任何一件活的负责人、队列里有本对话的活的人。
// 同一个项目里别的对话的员工不画（画出来也是闲着、没有任务）。
export function involvedPeople(data) {
  const people = (data && data.people) || {};
  const former = new Set(((data && data.former) || []).map(f => f.key));
  const tasks = (data && data.tasks) || [];
  const taskIds = new Set(tasks.map(t => t.id));
  const keys = new Set();
  ((data && data.depts) || []).forEach(d => (d.members || []).forEach(k => keys.add(k)));
  tasks.forEach(t => { if (t.owner) keys.add(t.owner); });
  Object.values(people).forEach(p => {
    if (p && (p.queue || []).some(id => taskIds.has(id))) keys.add(p.key);
  });
  const out = {};
  keys.forEach(k => { if (people[k] && !former.has(k)) out[k] = people[k]; });
  return out;
}

export function initDeptGrid(onChange) {
  onPastToggle = onChange;
  const grid = document.getElementById('dept-grid');
  if (!grid || grid.dataset.pastBound) return;
  grid.dataset.pastBound = '1';
  grid.addEventListener('click', e => {
    const step = e.target.closest('[data-step-toggle]');
    if (step) {
      const key = step.getAttribute('data-step-toggle');
      const opening = !stepOpen.has(key);
      if (opening) stepOpen.add(key); else stepOpen.delete(key);
      if (onPastToggle) onPastToggle();
      if (opening) requestAnimationFrame(() => revealInOffice(grid.querySelector(`:scope > .step-band[data-col="${CSS.escape(key)}"]`)));
      return;
    }
    const fold = e.target.closest('.room-fold-head[data-fold]');
    if (fold) {
      const key = fold.getAttribute('data-fold');
      if (foldOpen.has(key)) foldOpen.delete(key); else foldOpen.add(key);
      if (onPastToggle) onPastToggle();
      return;
    }
    if (!e.target.closest('#past-depts-toggle')) return;
    pastExpanded = !pastExpanded;
    if (onPastToggle) onPastToggle();
  });
}

// 公司页的房间：每间房按编号对上号，就地更新；顺序变了才挪。opts.plan 是 roomPlan(快照)（不给就按这几个参数现算），
// opts.mailFor(em) → 这个工位要显示的未读信件 {unread, letter}
export function renderDepartments(depts = [], people = {}, former = [], tasks = [], convId = '', activity = null, now = 0, opts = {}) {
  const deptGrid = document.getElementById('dept-grid');
  if (!deptGrid) return;
  now = now || Date.now() / 1000;
  const formerKeys = new Set((former || []).map(f => f.key));
  const plan = opts.plan || roomPlan({ depts: depts || [], people: people || {}, former: former || [], tasks: tasks || [], parts: opts.parts,
    activity, conversation: { id: convId } });
  plan.rooms.forEach(r => { r.keys = r.keys.filter(k => !formerKeys.has(k)); });
  currentRooms = plan.rooms;
  const titles = new Map(plan.rooms.map(r => [r.part, r.title]));
  const ctxM = { people: plan.people, mode: plan.mode, activity, now, partTitle: id => titles.get(id) || '',
    titleOf: k => ((plan.people[k] || (people || {})[k] || {}).label || '') };
  const models = plan.rooms.map((r, i) => Object.assign(roomModel(r, i, ctxM), { r }));
  const ctx = { people: plan.people, convId, tasks: tasks || [], activity, mailFor: opts.mailFor, now, sleepers: plan.sleepers,
    animate: deptGrid.dataset.drawn === '1' };
  // 房间、收起的房间、每一步那一行都按编号复用（不整块重建，office-spec.md 二.7）：只就地改变了的地方
  const have = new Map([...deptGrid.querySelectorAll('.dept-card.room, .room-fold')].map(el => [el.getAttribute('data-dept-id'), el]));
  const roomEl = m => {
    if (m.r.folded) return foldEl(m, have.get('fold:' + m.nodeId));
    const el = have.get(m.nodeId) || createRoom(m);
    patchRoom(el, m, ctx);
    return el;
  };
  deptGrid.classList.toggle('by-steps', plan.mode === 'parts');
  const list = [];

  if (plan.mode === 'parts') {
    // 从上往下一步一步排（office-spec.md 二.1~3，用户选定"当前步展开、做完的折叠"）：
    //   左边一条细细的步骤时间线（✓ 做完、● 正在做、○ 还没开工）；
    //   做完的步、还没开工的步收成一行（"✓ 第 1 步 · 4 组都做完" + 每组一个小芯片 + ▸，点开展开房间，再点收起）；
    //   正在做的步展开：房间排成网格（一行最多 2 个，屏幕够宽 3 个，放不下换行），这一步里做完的组用小芯片放在最上面
    const bands = [];
    models.forEach(m => {
      let b = bands.find(x => x.col === m.r.col);
      if (!b) bands.push(b = { col: m.r.col, n: m.r.n, ms: [] });
      b.ms.push(m);
    });
    const vpw = (document.getElementById('office-viewport') || {}).clientWidth || 1400;
    deptGrid.style.setProperty('--room-cols', vpw >= 1560 ? '3' : '2');
    const haveBands = new Map([...deptGrid.querySelectorAll(':scope > .step-band')].map(el => [el.getAttribute('data-col'), el]));
    const chipsHtml = ms => ms.map(m => `<span class="step-chip" id="node-chip-${escapeHtml(m.r.key)}" data-chip-room="${escapeHtml(m.r.key)}">${m.r.model ? partChipHtml(m.r.model) : escapeHtml(m.r.title)}</span>`).join('');
    currentSeat = plan.seat;
    bands.forEach((b, i) => {
      const el = haveBands.get(String(b.col)) || htmlToEl(`<div class="step-band" data-col="${b.col}"><div class="step-mark" aria-hidden="true"><i></i></div>
        <div class="step-main"><div class="step-head"></div><div class="step-rooms"></div></div></div>`);
      const loose = b.ms.every(m => m.r.loose);
      const sts = b.ms.map(m => m.r.status);
      const state = sts.every(s => s === 'done') ? 'done' : sts.every(s => !s || s === 'planned') ? 'wait' : 'now';
      const key = String(b.col);
      const open = state === 'now' || stepOpen.has(key);
      const name = loose ? '零散' : `第 ${b.n} 步`;
      const prevN = bands[i - 1] && !bands[i - 1].ms.every(m => m.r.loose) ? bands[i - 1].n : 0;
      const doneMs = state === 'now' ? b.ms.filter(m => m.r.status === 'done') : [];
      const roomMs = state === 'now' ? b.ms.filter(m => m.r.status !== 'done') : b.ms;
      let headHtml;
      if (state === 'now') {
        headHtml = `<div class="step-line s-now"><b>${name}</b>${b.ms.length > 1 ? `<span class="step-sum"> · ${b.ms.length} 组同时做</span>` : loose ? '<span class="step-sum"> · 不排先后</span>' : ''}`
          + (doneMs.length ? `<span class="step-donechips"><small>做完了</small>${chipsHtml(doneMs)}</span>` : '') + '</div>';
      } else {
        const sum = state === 'done' ? (b.ms.length > 1 ? `${b.ms.length} 组都做完` : '做完了') : `还没开工${prevN ? ` · 等第 ${prevN} 步` : ''}`;
        headHtml = `<button type="button" class="step-line s-${state}" data-step-toggle="${escapeHtml(key)}" aria-expanded="${open}" title="${open ? '点一下收起' : '点一下展开这一步的房间'}">`
          + `<b>${name}</b><span class="step-sum"> · ${sum}</span><span class="step-chips">${chipsHtml(b.ms)}</span><i class="step-caret" aria-hidden="true">${open ? '▾' : '▸'}</i></button>`;
      }
      const head = el.querySelector('.step-head');
      if (head._html !== headHtml) { head._html = headHtml; head.innerHTML = headHtml; }
      setAttrs(el, { class: `step-band s-${state}${open ? ' open' : ''}${i === 0 ? ' first' : ''}${i === bands.length - 1 ? ' last' : ''}`, 'data-col': key });
      const rooms = el.querySelector('.step-rooms');
      rooms.hidden = !open;
      placeChildren(rooms, open ? roomMs.map(roomEl) : []);
      list.push(el);
    });
  }
  if (!list.length) list.push(deptGrid.querySelector(':scope > .dept-empty') || htmlToEl('<div class="dept-empty" style="padding: 40px;">还没有中任务</div>'));
  placeChildren(deptGrid, list);
  deptGrid.dataset.drawn = '1';
  renderRestArea(plan.rest.filter(k => !formerKeys.has(k)), plan.people, Object.assign({}, opts, { now }));

  // 旧版页面的"未分配员工区"不再使用
  const unassignedSec = document.getElementById('unassigned-section');
  if (unassignedSec && unassignedSec.style.display !== 'none') {
    unassignedSec.style.display = 'none';
    unassignedSec.innerHTML = '';
  }
}

// 休息区：这个对话里手上一件活都没有的人，坐在负责人左边；点一下照样打开他的详情
function renderRestArea(keys, people, opts = {}) {
  const zone = document.getElementById('gm-zone');
  if (!zone) return;
  let el = document.getElementById('rest-area');
  if (!keys.length) {
    if (el) el.remove();
    zone.classList.remove('has-rest');
    return;
  }
  if (!el) {
    el = htmlToEl(`
      <section class="rest-area" id="rest-area" aria-label="休息区" title="这个对话里手上没活的人">
        <div class="rest-head"><b>休息区</b><span>手上没活</span></div>
        <div class="rest-seats"></div>
      </section>`);
  }
  if (el.parentNode !== zone) zone.insertBefore(el, zone.firstChild);
  zone.classList.add('has-rest');
  const seats = el.querySelector('.rest-seats');
  const have = new Map([...seats.children].map(c => [c.getAttribute('data-worker-key'), c]));
  const want = keys.map(k => {
    const p = people[k] || {};
    const type = getAgentType(p.agent);
    const state = p.display_state || p.state || 'idle';
    const label = p.label || k;
    const mail = opts.mailFor ? opts.mailFor(k) : {};
    const duty = dutyText(p.duty, opts.now);
    const sig = [type, state, label, hasChars() ? 'c' : 's', mail.unread || 0, mail.letter || '', duty].join('|');
    let s = have.get(k);
    if (!s || s._sig !== sig) {
      const fresh = htmlToEl(`
        <button type="button" class="rest-seat" data-worker-key="${escapeHtml(k)}" data-person="${escapeHtml(k)}" title="${escapeHtml(label)}：这个对话里手上没活${duty ? `（${duty}）` : ''}。点开看他做过的事">
          <span class="rest-ava">${avatarHtml(type, 46, state === 'sleeping' ? 'sleeping' : 'idle')}</span>
          <b class="name-${type}">${escapeHtml(label)}</b><small>${escapeHtml(getStateLabel(state))}</small>${duty ? `<small class="rest-duty">${escapeHtml(duty.split(' · ')[0])}</small>` : ''}
          ${mail.unread > 0 ? `<span class="station-mail" data-letter="${escapeHtml(mail.letter || '')}" title="有 ${mail.unread} 封信还没送到他手上，点开看">✉ ${mail.unread}</span>` : ''}
        </button>`);
      fresh._sig = sig;
      if (s) s.replaceWith(fresh);
      s = fresh;
    }
    return s;
  });
  placeChildren(seats, want);
}

function roundedPath(pts, r) {
  const p = pts.filter((q, i) => i === 0 || Math.abs(q[0] - pts[i - 1][0]) > .5 || Math.abs(q[1] - pts[i - 1][1]) > .5);
  const f = n => n.toFixed(1);
  let d = `M ${f(p[0][0])} ${f(p[0][1])}`;
  for (let i = 1; i < p.length - 1; i++) {
    const [x0, y0] = p[i - 1], [x1, y1] = p[i], [x2, y2] = p[i + 1];
    const k1 = Math.min(r, Math.hypot(x1 - x0, y1 - y0) / 2), k2 = Math.min(r, Math.hypot(x2 - x1, y2 - y1) / 2);
    const ax = x1 - Math.sign(x1 - x0) * k1, ay = y1 - Math.sign(y1 - y0) * k1;
    const bx = x1 + Math.sign(x2 - x1) * k2, by = y1 + Math.sign(y2 - y1) * k2;
    d += ` L ${f(ax)} ${f(ay)} Q ${f(x1)} ${f(y1)} ${f(bx)} ${f(by)}`;
  }
  const last = p[p.length - 1];
  return d + ` L ${f(last[0])} ${f(last[1])}`;
}

// ---------- 组织树连线 ----------
// 每个部门一条完整的线：从负责人下沿出发，一路都是这个部门自己的颜色，绕开别的房间，彼此尽量不重叠。
// routeArcs 是纯函数（node 里也能测）：给负责人框和各房间框（相对画布的 l/r/t/b，target:false 的只当障碍），
// 返回每个部门的折线点 [{key, pts:[[x,y],…]}]。
//   · 第一排的房间：从负责人下沿出发 → 在负责人和第一排之间的空当里横着走 → 下到房间正上方。
//   · 后面几排：先横到一条能竖着穿过前面各排的缝（房间之间的空当、或两边）→ 竖着下去 → 在那一排上面的空当里横到房间正上方。
//   · 同一个空当里的横线分车道：往左去的走得越远越靠上，往右同理；出发点按去向从左到右排开 —— 这样同一侧的线不会交叉、也不会叠在一起。
const ARC_MARGIN = 14;   // 线离房间边至少多远
const ARC_LANE = 9;      // 并排的线之间隔多远

function spreadAlong(list, gap) {
  // list: [{x, lo, hi}]（已按 x 排好）：彼此至少隔 gap，尽量不出各自的 [lo, hi]
  for (let i = 1; i < list.length; i++) {
    if (list[i].x < list[i - 1].x + gap) list[i].x = Math.min(list[i].hi, list[i - 1].x + gap);
  }
  for (let i = list.length - 2; i >= 0; i--) {
    if (list[i].x > list[i + 1].x - gap) list[i].x = Math.max(list[i].lo, list[i + 1].x - gap);
  }
}

export function routeArcs(gm, boxes) {
  const targets = (boxes || []).filter(b => b.target !== false);
  if (!gm || !targets.length) return [];
  const mid = b => (b.l + b.r) / 2;
  const clamp = (v, a, b) => Math.max(a, Math.min(b, v));
  const gx = mid(gm);

  // 1. 按上沿分排（同一排的房间上沿对齐）
  const rows = [];
  boxes.slice().sort((a, b) => a.t - b.t).forEach(b => {
    const row = rows.find(r => Math.abs(r.t - b.t) < 6);
    if (row) { row.items.push(b); row.b = Math.max(row.b, b.b); }
    else rows.push({ t: b.t, b: b.b, items: [b] });
  });
  const rowOf = new Map();
  rows.forEach((r, k) => r.items.forEach(b => rowOf.set(b, k)));

  // 2. 每一排里竖线能穿过去的缝：房间之间的空当和两边
  const minX = Math.min(gm.l, ...boxes.map(b => b.l)) - 40;
  const maxX = Math.max(gm.r, ...boxes.map(b => b.r)) + 40;
  const free = rows.map(row => {
    let iv = [[minX, maxX]];
    row.items.forEach(b => {
      const a0 = b.l - ARC_MARGIN, b0 = b.r + ARC_MARGIN;
      iv = iv.flatMap(([a, c]) => (b0 <= a || a0 >= c) ? [[a, c]] : [[a, a0], [b0, c]].filter(([p, q]) => q - p > 2));
    });
    return iv;
  });
  const intersect = (A, B) => A.flatMap(([a, b]) => B.map(([c, d]) => [Math.max(a, c), Math.min(b, d)]).filter(([p, q]) => q - p > 2));

  // 3. 每条线第一段横着要到哪（x0）：第一排就是房间正上方；后面几排是穿过前面各排的那条缝
  const routes = targets.map(b => ({ box: b, key: b.key, row: rowOf.get(b), tx: mid(b) }));
  const corridor = [];
  routes.forEach(rt => {
    if (rt.row === 0) { rt.x0 = rt.tx; return; }
    let ivs = free[0];
    for (let k = 1; k < rt.row; k++) ivs = intersect(ivs, free[k]);
    if (!ivs.length) ivs = [[minX - 24, minX]];              // 实在没有缝：从最左边绕下去
    const cost = ([a, b]) => Math.abs(clamp(rt.tx, a, b) - rt.tx) + Math.abs(clamp(gx, a, b) - gx);
    const iv = ivs.reduce((best, x) => (cost(x) < cost(best) ? x : best));
    corridor.push({ rt, x: clamp(rt.tx, iv[0], iv[1]), lo: iv[0], hi: iv[1] });
  });
  // 走同一条缝的几条线彼此错开（按去向从左到右）
  const byGap = new Map();
  corridor.forEach(c => {
    const host = free[0].findIndex(([a, b]) => c.x >= a - 1 && c.x <= b + 1);
    if (!byGap.has(host)) byGap.set(host, []);
    byGap.get(host).push(c);
  });
  byGap.forEach(list => {
    list.sort((a, b) => a.x - b.x || a.rt.tx - b.rt.tx);
    spreadAlong(list, ARC_LANE);
    list.forEach(c => { c.rt.x0 = c.x; });
  });

  // 4. 负责人下沿的出发点：按第一段要去的方向从左到右排开；正好在下面的直接竖着下去
  const order = routes.slice().sort((a, b) => a.x0 - b.x0 || a.tx - b.tx);
  const n = order.length;
  const sp = n > 1 ? Math.min(12, Math.max(0, gm.r - gm.l - 60) / (n - 1)) : 0;
  order.forEach((rt, i) => { rt.sx = gx + (i - (n - 1) / 2) * sp; });
  order.forEach((rt, i) => {
    const lo = i ? order[i - 1].sx : -Infinity, hi = i < n - 1 ? order[i + 1].sx : Infinity;
    if (rt.x0 > lo + 3 && rt.x0 < hi - 3 && rt.x0 > gm.l + 16 && rt.x0 < gm.r - 16) rt.sx = rt.x0;
  });

  // 5. 横着走的那段在空当里分车道
  const lanes = (top, bot, list) => {
    const h = Math.max(0, bot - top);
    const left = list.filter(s => s.to < s.from - 1).sort((a, b) => a.to - b.to);
    const right = list.filter(s => s.to > s.from + 1).sort((a, b) => b.to - a.to);
    list.filter(s => Math.abs(s.to - s.from) <= 1).forEach(s => s.set(null));
    [left, right].forEach(side => {
      const m = side.length;
      if (!m) return;
      const step = m > 1 ? Math.min(ARC_LANE, Math.max(2, h - 20) / (m - 1)) : 0;
      const base = top + (h - (m - 1) * step) / 2;
      side.forEach((s, i) => s.set(base + i * step));
    });
  };
  lanes(gm.b, rows[0].t, routes.map(rt => ({ from: rt.sx, to: rt.x0, set: y => { rt.y0 = y; } })));
  for (let k = 1; k < rows.length; k++) {
    lanes(rows[k - 1].b, rows[k].t, routes.filter(rt => rt.row === k).map(rt => ({ from: rt.x0, to: rt.tx, set: y => { rt.yk = y; } })));
  }

  // 6. 拼成折线
  return routes.map(rt => {
    const pts = [[rt.sx, gm.b]];
    if (rt.y0 != null) pts.push([rt.sx, rt.y0], [rt.x0, rt.y0]);
    if (rt.row > 0 && rt.yk != null) pts.push([rt.x0, rt.yk], [rt.tx, rt.yk]);
    pts.push([rt.tx, rt.box.t]);
    return { key: rt.key, pts };
  });
}

// 相对画布的位置：按 offsetLeft/offsetTop 一层层加上去（不受鼠标悬停时的上浮动画影响）
function relBox(el, canvas) {
  let x = 0, y = 0, node = el;
  while (node && node !== canvas) {
    x += node.offsetLeft;
    y += node.offsetTop;
    node = node.offsetParent;
  }
  if (node !== canvas) {
    const base = canvas.getBoundingClientRect(), r = el.getBoundingClientRect(), k = scaleOf(canvas);
    return { l: (r.left - base.left) / k, r: (r.right - base.left) / k, t: (r.top - base.top) / k, b: (r.bottom - base.top) / k };
  }
  return { l: x, t: y, r: x + el.offsetWidth, b: y + el.offsetHeight };
}

const SVG_NS = 'http://www.w3.org/2000/svg';
let arcSig = '';

function svgGroup(svg, cls) {
  let g = svg.querySelector(':scope > g.' + cls);
  if (!g) {
    g = document.createElementNS(SVG_NS, 'g');
    g.setAttribute('class', cls);
    svg.appendChild(g);
  }
  return g;
}

// 流光：沿着部门那条线走。线的形状变了才重设动画，并且接着原来走到的位置，不从头放
function runFlow(path, index) {
  if (reducedMotion()) return;
  const len = path.getTotalLength();
  const dur = Math.max(1600, len * 4);
  const old = path._flow;
  let frac = null;
  if (old) {
    const t = Number(old.currentTime) || 0, d = old._dur || dur;
    frac = (t % d) / d;
    old.cancel();
  }
  path.style.strokeDasharray = `22 ${Math.ceil(len + 30)}`;
  const anim = path.animate(
    [{ strokeDashoffset: 22 }, { strokeDashoffset: -Math.ceil(len) }],
    { duration: dur, iterations: Infinity, easing: 'ease-in-out', delay: old ? 0 : index * 400 }
  );
  anim._dur = dur;
  if (frac != null) anim.currentTime = frac * dur;
  path._flow = anim;
}

// 部门线：线的位置或忙闲变了才重画；没变什么都不动（流光不重启）
// 从上往下一步一步排时的连线（office-spec.md 二.1~3）：负责人 → 时间线最上面那个点；
// 每一步展开的房间：从时间线引一条细横线到这一行房间上方，再往下落到每间房的牌子（落下那一截是房间的颜色）。收成一行的组不画线
function stepArcs(svg, canvas, gmEl) {
  const marks = [...canvas.querySelectorAll('#dept-grid .step-band .step-mark > i')];
  const lines = [];
  if (!marks.length) return lines;
  const g = relBox(gmEl, canvas);
  const m0 = relBox(marks[0], canvas);
  const tx = (m0.l + m0.r) / 2;
  const gx = (g.l + g.r) / 2, midY = g.b + Math.max(14, (m0.t - g.b) / 2);
  lines.push({ key: '_trunk', d: roundedPath([[gx, g.b], [gx, midY], [tx, midY], [tx, m0.t]], 10), color: 'var(--line)', start: [gx, g.b], end: [tx, m0.t], trunk: true });
  canvas.querySelectorAll('#dept-grid .step-band.open').forEach(band => {
    const mark = band.querySelector('.step-mark > i');
    const rooms = [...band.querySelectorAll(':scope .step-rooms > .dept-card.room')];
    if (!mark || !rooms.length) return;
    const mb = relBox(mark, canvas);
    const x0 = (mb.l + mb.r) / 2;
    // 按行分：同一行（top 一样）的房间共用一条横线
    const rows = new Map();
    rooms.forEach(el => { const b = relBox(el, canvas); const k = Math.round(b.t); if (!rows.has(k)) rows.set(k, []); rows.get(k).push({ el, b }); });
    [...rows.entries()].sort((a, b) => a[0] - b[0]).forEach(([top, list], ri) => {
      const busY = top - 11;
      const far = Math.max(...list.map(x => x.b.l + 44));
      lines.push({ key: `_bus${lines.length}`, d: `M${x0} ${mb.b}V${busY - 8}Q${x0} ${busY} ${x0 + 8} ${busY}H${far - 8}`, color: 'var(--line)', start: [x0, mb.b], end: [far, busY], bus: true });
      list.forEach(({ el, b }) => {
        const x = b.l + 44;
        lines.push({
          key: el.getAttribute('data-dept-id') || el.id, d: `M${x - 8} ${busY}Q${x} ${busY} ${x} ${busY + 8}V${b.t}`, start: [x, busY], end: [x, b.t],
          color: (el.style.getPropertyValue('--room-c') || '#4176E6').trim(), loose: el.classList.contains('room-loose'), busy: el.classList.contains('busy')
        });
      });
    });
  });
  return lines;
}

function drawArcs() {
  const svg = document.getElementById('arc-canvas');
  const canvas = document.getElementById('office-canvas');
  const gmEl = document.getElementById('node-gm');
  if (!svg || !canvas || !gmEl) return;
  if (canvas.querySelector('#dept-grid.by-steps .step-band')) {
    const lines = stepArcs(svg, canvas, gmEl);
    const sig = JSON.stringify(lines.map(l => [l.key, l.d, l.color, l.busy]));
    if (sig === arcSig && svg.querySelector('g.arc-lines')) return;
    arcSig = sig;
    const f = n => n.toFixed(1);
    svgGroup(svg, 'arc-lines').innerHTML = lines.map(l => `
      <path class="org-branch${l.trunk || l.bus ? ' org-trunk' : ''}${l.loose ? ' dashed' : ''}${l.busy ? ' busy' : ''}" data-room="${escapeHtml(l.key)}" style="stroke:${l.color}" d="${l.d}"/>
      ${l.trunk || l.bus ? '' : `<circle class="org-node" style="stroke:${l.color}" cx="${f(l.end[0])}" cy="${f(l.end[1])}" r="3.5"/>`}`).join('');
    svgGroup(svg, 'arc-flows').innerHTML = '';
    return;
  }

  const roomEls = [...document.querySelectorAll('#dept-grid .dept-card.room')];
  const boxes = roomEls.map(el => Object.assign(relBox(el, canvas), { key: el.getAttribute('data-dept-id') || el.id, el }));
  const past = document.getElementById('past-depts-toggle');
  if (past) boxes.push(Object.assign(relBox(past, canvas), { key: '_past', target: false }));
  document.querySelectorAll('#dept-grid .step-line, #dept-grid .room-fold, #dept-grid .step-mark').forEach((el, i) => boxes.push(Object.assign(relBox(el, canvas), { key: '_obs' + i, target: false })));
  const gm = relBox(gmEl, canvas);

  const lines = routeArcs(gm, boxes).map(r => {
    const el = boxes.find(b => b.key === r.key).el;
    return {
      key: r.key,
      d: roundedPath(r.pts, 10),
      start: r.pts[0],
      end: r.pts[r.pts.length - 1],
      color: (el.style.getPropertyValue('--room-c') || '#4176E6').trim(),
      loose: el.classList.contains('room-loose'),
      busy: el.classList.contains('busy')
    };
  });

  const sig = JSON.stringify(lines.map(l => [l.key, l.d, l.color, l.loose, l.busy]));
  if (sig === arcSig && svg.querySelector('g.arc-lines')) return;
  arcSig = sig;

  const f = n => n.toFixed(1);
  const lineG = svgGroup(svg, 'arc-lines');
  const flowG = svgGroup(svg, 'arc-flows');
  lineG.innerHTML = lines.map(l => `
    <path class="org-branch${l.loose ? ' dashed' : ''}${l.busy ? ' busy' : ''}" data-room="${escapeHtml(l.key)}" style="stroke:${l.color}" d="${l.d}"/>
    <circle class="org-start" style="fill:${l.color}" cx="${f(l.start[0])}" cy="${f(l.start[1])}" r="3"/>
    <circle class="org-node" style="stroke:${l.color}" cx="${f(l.end[0])}" cy="${f(l.end[1])}" r="4"/>`).join('');

  // 流光按部门对上号：还忙的接着走，不忙了的拿掉，新忙起来的加上
  const busy = new Map(lines.filter(l => l.busy).map(l => [l.key, l]));
  flowG.querySelectorAll('path.org-flow').forEach(p => {
    if (!busy.has(p.getAttribute('data-room'))) {
      if (p._flow) p._flow.cancel();
      p.remove();
    }
  });
  let added = 0;
  busy.forEach((l, key) => {
    let p = [...flowG.querySelectorAll('path.org-flow')].find(x => x.getAttribute('data-room') === key);
    if (!p) {
      p = document.createElementNS(SVG_NS, 'path');
      p.setAttribute('class', 'org-flow');
      p.setAttribute('data-room', key);
      flowG.appendChild(p);
    }
    p.style.stroke = l.color;
    p.style.color = l.color;
    if (p.getAttribute('d') !== l.d) {
      p.setAttribute('d', l.d);
      runFlow(p, added++);
    }
  });
}

// 连线和信件虚线一起重画（各自比较位置，没变就不动）
export function renderArcs() {
  drawArcs();
  drawMail();
}

// 动画：查找对应 DOM 节点（'gm' 负责人、'head:<房间>' 房间的监工席、'office' 负责人右边那位监工、'dept:<编号>' 部门牌子
// （旧对话是那间房的牌子，小任务房间里是那个小区的牌子）、em_… 员工工位（休息区的人是他的座位））
export function findNodeEl(key) {
  if (!key) return null;
  if (key === 'gm') return document.getElementById('node-gm');
  if (key.startsWith('head:')) return document.getElementById('node-head-' + key.slice(5)) || document.getElementById('node-chip-' + key.slice(5));
  if (key === 'office' || key === 'rules') return document.getElementById('node-office');
  if (key.startsWith('dept:')) {
    const room = document.getElementById('node-dept-' + key.slice(5));
    if (room) return room.querySelector('.room-sign') || room;
    const zone = document.getElementById('node-zone-' + key.slice(5));
    return zone && (zone.querySelector('.zone-head') || zone);
  }
  const card = [...document.querySelectorAll('#office-canvas .station-card[data-worker-key], #rest-area .rest-seat[data-worker-key]')]
    .find(el => el.getAttribute('data-worker-key') === key) || null;
  if (card) return card;
  // 他坐的房间收在一行里了（做完 / 还没开工的步、正在做的步里做完的组）：落到那一组的芯片上
  const room = currentSeat.get(key);
  return room ? document.getElementById('node-chip-' + room) : null;
}

// 公司页能缩放（office-view.js 给 #office-canvas 加了 scale）：屏幕上量出来的距离要除以缩放比例，才是画布里的坐标
function scaleOf(canvas) {
  const w = canvas && canvas.offsetWidth;
  return w ? (canvas.getBoundingClientRect().width / w) || 1 : 1;
}

function centerOf(el) {
  const canvas = document.getElementById('office-canvas');
  const base = canvas.getBoundingClientRect(), k = scaleOf(canvas);
  const r = el.getBoundingClientRect();
  return { x: ((r.left + r.right) / 2 - base.left) / k, y: (r.top + Math.min(r.height / 2, 46 * k) - base.top) / k };
}

// ---------- 信件：两人之间画一条虚线，信封沿虚线飞过去，回信再飞回来 ----------
// 谁和谁通过信（负责人、监工、员工、部门），就在他们之间画一条虚线（最近 24 小时、最多 6 对；10 分钟内的更亮、虚线在走），
// 线中间一个小信封写着几封信；点线或信封打开右侧信件栏、定位到最近的那封。写给某人、还没送到的信，在他工位上挂"✉ 1"。
const MAIL_KEEP = 24 * 3600;
const MAIL_FRESH = 10 * 60;
const MAIL_PAIRS = 6;
let mailState = { pairs: [], unread: new Map(), heads: new Map() };
let mailSig = '';

// 信里写的 from / to → 画布上的节点；认不出（用户、旧系统的人）的返回 null。
// office / rules（后台自动写信、下指令的监工）先记成 'office'，再由 letterEnds 换成对方部门的监工席
export function mailNodeKey(x, deptIds) {
  if (!x || x === 'user') return null;
  if (x === 'gm') return 'gm';
  if (x === 'office' || x === 'rules') return 'office';
  if (String(x).startsWith('em_')) return x;
  if (deptIds && deptIds.has(x)) return 'dept:' + x;
  return null;
}

// 员工 / 部门 → 他那间房的监工席 'head:<房间>'。rooms（roomPlan 的房间）给了就按房间：坐在这间房的人、这间房里的部门
// （休息区的人不在任何房间里，监工的信落到负责人右边那位监工）；没给就按部门（旧算法：部门设了不要监工的没有监工席，不算）
function headIndex(depts, rooms) {
  const heads = new Map();
  if (rooms) {
    rooms.filter(r => r && r.head).forEach(r => {
      (r.keys || []).forEach(m => { if (!heads.has(m)) heads.set(m, 'head:' + r.key); });
      (r.depts || []).forEach(d => {
        const id = typeof d === 'string' ? d : d && d.id;
        if (id && !heads.has('dept:' + id)) heads.set('dept:' + id, 'head:' + r.key);
      });
    });
    return heads;
  }
  (depts || []).forEach(d => {
    if (!d || !d.id || d.head === 'none') return;
    const h = 'head:' + d.id;
    heads.set('dept:' + d.id, h);
    (d.members || []).forEach(m => { if (!heads.has(m)) heads.set(m, h); });
  });
  return heads;
}

// 写给部门的信：送到了谁手上就连到谁，否则连到部门牌子。
// 监工写的信 / 写给监工的回信：监工那一头落在对方那间房的监工席上；对方不在任何有监工席的房间里（休息区、负责人…）时
// 留作 'office'，连到负责人右边那位监工（renderGmZone 只在有这种信时才放她）
function letterEnds(l, deptIds, heads) {
  let to = l.to;
  const handed = l.delivered_to || (l.delivery && l.delivery.to);
  if (deptIds.has(l.to) && typeof handed === 'string' && handed.startsWith('em_')) to = handed;
  let a = mailNodeKey(l.from, deptIds), b = mailNodeKey(to, deptIds);
  if (a === 'office' && b) a = heads.get(b) || 'office';
  if (b === 'office' && a) b = heads.get(a) || 'office';
  return [a, b];
}

// 一封信在画布上连着哪两个节点（和虚线同一套规则；信件栏点一封信时用它让那条线、那两个人亮一下）。
// rooms 不给就用最近一次摆出来的房间
export function letterNodes(l, depts = [], rooms = currentRooms) {
  const deptIds = new Set((depts || []).map(d => d.id));
  return letterEnds(l || {}, deptIds, headIndex(depts, rooms));
}

// 纯函数：从快照里的信算出要画哪几对、谁有没送到的信。rooms = roomPlan(快照).rooms（监工那一头落在哪间房的监工席）
export function mailModel(letters = [], depts = [], now = 0, rooms = null) {
  const deptIds = new Set((depts || []).map(d => d.id));
  const heads = headIndex(depts, rooms);
  const nowSec = (now > 1e11 ? now / 1000 : now) || Date.now() / 1000;
  const pairs = new Map();
  const unread = new Map();
  (letters || []).filter(l => l && l.id && l.kind !== 'delivered' && !l.legacy).forEach(l => {
    const [a, b] = letterEnds(l, deptIds, heads);
    if (b && l.delivery && l.delivery.state === 'waiting') {
      const u = unread.get(b) || { unread: 0, letter: '' };
      u.unread++;
      u.letter = l.id;
      unread.set(b, u);
    }
    if (!a || !b || a === b || nowSec - (l.ts || 0) > MAIL_KEEP) return;
    const [x, y] = [a, b].sort();
    const key = x + '~' + y;
    const p = pairs.get(key) || { key, a: x, b: y, n: 0, last: null };
    p.n++;
    if (!p.last || (l.ts || 0) >= (p.last.ts || 0)) p.last = l;
    pairs.set(key, p);
  });
  const list = [...pairs.values()].sort((p, q) => (q.last.ts || 0) - (p.last.ts || 0)).slice(0, MAIL_PAIRS);
  list.forEach(p => { p.fresh = nowSec - (p.last.ts || 0) < MAIL_FRESH; });
  return { pairs: list, unread, heads };
}

export function setMail(model, names = {}) {
  mailState = Object.assign({ names }, model);
}

// 线头落在哪：工位卡片上沿正中、负责人桌子、监工席右上角（线从工位上方的空当走，不压着她的脸和别人的桌子）、负责人右边那位监工、部门牌子
function mailAnchor(key, canvas) {
  const el = findNodeEl(key);
  if (!el) return null;
  if (el.classList.contains('station-card')) {
    const b = relBox(el, canvas);
    return { x: (b.l + b.r) / 2, y: b.t + 26 };
  }
  if (el.classList.contains('head-desk')) {
    const b = relBox(el, canvas);
    return { x: b.r - 12, y: b.t + 8 };
  }
  if (key === 'gm') {                                   // 负责人：从桌子正下方、卡片下沿出线，不压着他的字
    const d = relBox(el.querySelector('.gm-desk-wrap') || el, canvas), box = relBox(el, canvas);
    return { x: (d.l + d.r) / 2, y: box.b - 10 };
  }
  const b = relBox(el, canvas);
  return { x: (b.l + b.r) / 2, y: (b.t + b.b) / 2 };
}

// 两点之间一条弯弯的线（二次曲线）。往哪边拱：哪边压到的卡片（负责人、监工、别人的工位）少就往哪边；
// 一样多时横着的往上拱、竖着的往右拱。同一对人不管谁写给谁都是同一条线（按编号排好先后再算）。
const curveAt = (cv, t) => ({
  x: (1 - t) * (1 - t) * cv.p.x + 2 * (1 - t) * t * cv.c.x + t * t * cv.q.x,
  y: (1 - t) * (1 - t) * cv.p.y + 2 * (1 - t) * t * cv.c.y + t * t * cv.q.y
});
function mailCurve(p, q, obstacles = []) {
  const dx = q.x - p.x, dy = q.y - p.y, len = Math.hypot(dx, dy) || 1;
  let nx = -dy / len, ny = dx / len;
  if (ny > 0.2 || (Math.abs(ny) <= 0.2 && nx < 0)) { nx = -nx; ny = -ny; }
  const k = Math.min(90, len * 0.22);
  const make = s => ({ p, c: { x: (p.x + q.x) / 2 + s * nx * k, y: (p.y + q.y) / 2 + s * ny * k }, q });
  const hits = cv => {
    let n = 0;
    for (let i = 2; i <= 18; i++) {
      const pt = curveAt(cv, i / 20);
      n += obstacles.filter(b => pt.x > b.l && pt.x < b.r && pt.y > b.t && pt.y < b.b).length;
    }
    return n;
  };
  const up = make(1), down = make(-1);
  return obstacles.length && hits(down) < hits(up) ? down : up;
}
function mailCurveFor(aKey, bKey, canvas) {
  const [x, y] = [aKey, bKey].sort();
  const pa = mailAnchor(x, canvas), pb = mailAnchor(y, canvas);
  if (!pa || !pb) return null;
  const ends = new Set([findNodeEl(x), findNodeEl(y)].map(el => el && (el.closest('.station-card, #node-gm, #node-office') || el)));
  const obstacles = [...document.querySelectorAll('#node-gm, #node-office, #dept-grid .station-card')]
    .filter(el => !ends.has(el)).map(el => relBox(el, canvas));
  return mailCurve(pa, pb, obstacles);
}
const curveD = cv => `M ${cv.p.x.toFixed(1)} ${cv.p.y.toFixed(1)} Q ${cv.c.x.toFixed(1)} ${cv.c.y.toFixed(1)} ${cv.q.x.toFixed(1)} ${cv.q.y.toFixed(1)}`;

function mailSvg(canvas) {
  let svg = document.getElementById('mail-canvas');
  if (!svg) {
    svg = document.createElementNS(SVG_NS, 'svg');
    svg.setAttribute('id', 'mail-canvas');
    svg.setAttribute('aria-hidden', 'true');
    canvas.appendChild(svg);
  }
  return svg;
}

function drawMail() {
  const canvas = document.getElementById('office-canvas');
  if (!canvas) return;
  const items = mailState.pairs.map(p => {
    const cv = mailCurveFor(p.a, p.b, canvas);
    return cv && { p, d: curveD(cv), mid: curveAt(cv, 0.5) };
  }).filter(Boolean);
  const sig = JSON.stringify(items.map(i => [i.p.key, i.d, i.p.n, i.p.fresh, i.p.last.id]));
  if (sig === mailSig && (items.length === 0 || document.getElementById('mail-canvas'))) return;
  mailSig = sig;
  const svg = mailSvg(canvas);
  const names = mailState.names || {};
  const nameOf = k => names[k] || (k === 'gm' ? '负责人' : k === 'office' ? '监工'
    : k.startsWith('head:') ? (names['dept:' + k.slice(5)] || '') + '监工' : k);
  svg.innerHTML = items.map(i => {
    const tip = `${nameOf(i.p.a)} ↔ ${nameOf(i.p.b)}：${i.p.n} 封信，最近一封「${i.p.last.subject || ''}」。点一下看信`;
    return `
    <g class="mail-line${i.p.fresh ? ' fresh' : ''}${hotMail.key === i.p.key && Date.now() < hotMail.until ? ' hot' : ''}" data-pair="${escapeHtml(i.p.key)}" data-letter="${escapeHtml(i.p.last.id)}">
      <title>${escapeHtml(tip)}</title>
      <path class="mail-hit" d="${i.d}"/>
      <path class="mail-path" d="${i.d}"/>
      <g class="mail-pin" transform="translate(${i.mid.x.toFixed(1)} ${i.mid.y.toFixed(1)})">
        <rect x="-19" y="-10" width="38" height="20" rx="10"/>
        <text x="0" y="4" text-anchor="middle">✉ ${i.p.n}</text>
      </g>
    </g>`;
  }).join('');
}

// 选中一封信时，它那条虚线亮一下（这期间线被重画了也接着亮）。
// 传进来的还是 office / rules（监工）时，和画线一样换成对方部门的监工席
let hotMail = { key: '', until: 0 };
export function highlightMail(aKey, bKey) {
  const heads = mailState.heads || new Map();
  let [a, b] = [aKey, bKey].map(k => (k === 'rules' ? 'office' : k));
  if (a === 'office' && b) a = heads.get(b) || a;
  if (b === 'office' && a) b = heads.get(a) || b;
  const key = [a, b].sort().join('~');
  hotMail = { key, until: Date.now() + 2400 };
  const g = [...document.querySelectorAll('#mail-canvas .mail-line')].find(x => x.getAttribute('data-pair') === key);
  if (g) g.classList.add('hot');
  setTimeout(() => {
    document.querySelectorAll('#mail-canvas .mail-line.hot').forEach(x => x.classList.remove('hot'));
  }, 2450);
}

let flyingCount = 0;
const flightQueue = [];
const LETTER_WORDS = { request: '需求', decision: '结论', escalation: '上报', report: '汇报', tell: '留言', reply: '回信', notice: '通知', system: '监工来信' };
const LETTER_COLORS = { request: '#F59E0B', decision: '#22C55E', escalation: '#EC1313', report: '#81858C', tell: '#4176E6' };

// 信封沿两人之间的虚线从写信人飞到收信人（回信就是反过来飞）
export function flyLetter(fromKey, toKey, type = 'tell') {
  if (reducedMotion()) return;
  if (flyingCount >= 3) {
    flightQueue.push([fromKey, toKey, type]);
    return;
  }
  const canvas = document.getElementById('office-canvas');
  if (!canvas || !fromKey || !toKey || fromKey === toKey) return;
  const cv = mailCurveFor(fromKey, toKey, canvas);
  if (!cv) return;
  const forward = [fromKey, toKey].sort()[0] === fromKey;
  const pb = forward ? cv.q : cv.p;
  const pts = [];
  for (let i = 0; i <= 20; i++) pts.push(curveAt(cv, forward ? i / 20 : 1 - i / 20));
  const color = LETTER_COLORS[type] || '#4176E6';
  highlightMail(fromKey, toKey);

  const env = document.createElement('div');
  env.className = 'letter-fly';
  env.innerHTML = `
    <svg viewBox="0 0 30 22" width="30" height="22">
      <rect x="1" y="1" width="28" height="20" rx="3" fill="#fff" stroke="${color}" stroke-width="2"/>
      <path d="M2 3l13 10L28 3" fill="none" stroke="${color}" stroke-width="2" stroke-linejoin="round"/>
      <circle cx="15" cy="13" r="3" fill="${color}"/>
    </svg>`;
  canvas.appendChild(env);
  flyingCount++;
  travel(env, pts, { minMs: 1300, maxMs: 2400, perPx: 2.4 }).onfinish = () => {
    env.remove();
    landPing(canvas, pb, '✉ ' + (LETTER_WORDS[type] || '信件'), color);
    flash(findNodeEl(toKey));
    flyingCount--;
    if (flightQueue.length) {
      setTimeout(() => flyLetter(...flightQueue.shift()), 150);
    }
  };
}

// 推送里的一封信 → 画布上从谁飞到谁（写给部门的、已经送到某人手上的，飞到那个人）
export function letterFlight(item, data) {
  const depts = (data && data.depts) || [];
  const l = ((data && data.letters) || []).find(x => x.id === item.subject) || item;
  const [a, b] = letterNodes(Object.assign({}, item, l), depts);
  return a && b && a !== b ? [a, b, l.kind || item.kind || 'tell'] : null;
}

export function confetti(targetKey) {
  if (reducedMotion()) return;
  const el = findNodeEl(targetKey);
  const canvas = document.getElementById('office-canvas');
  if (!el || !canvas) return;

  const c = centerOf(el);
  const colors = ['#F59E0B', '#22C55E', '#4D6BFE', '#E07A5F', '#9B72CB', '#EC4899'];
  for (let i = 0; i < 16; i++) {
    const bit = document.createElement('span');
    bit.className = 'confetti';
    bit.style.background = colors[i % colors.length];
    canvas.appendChild(bit);
    const dx = (Math.random() - 0.5) * 140, up = 40 + Math.random() * 50, rot = (Math.random() - 0.5) * 720;
    bit.animate([
      { transform: `translate(${c.x}px, ${c.y}px) rotate(0)`, opacity: 1 },
      { transform: `translate(${c.x + dx * 0.6}px, ${c.y - up}px) rotate(${rot / 2}deg)`, opacity: 1, offset: 0.4 },
      { transform: `translate(${c.x + dx}px, ${c.y + 40}px) rotate(${rot}deg)`, opacity: 0 }
    ], { duration: 1300 + Math.random() * 400, easing: 'cubic-bezier(.2,.7,.4,1)' }).onfinish = () => bit.remove();
  }
}

// context.compacted → 角色闪一下"整理记忆"
export function flashCompact(key) {
  const el = findNodeEl(key);
  if (el) {
    el.classList.add('flash-compacting');
    setTimeout(() => el.classList.remove('flash-compacting'), 2000);
  }
  flashChar(key, 'compacting', 2000);
}

function flash(el, cls = 'flash-ping', ms = 1300) {
  if (!el) return;
  el.classList.remove(cls);
  void el.offsetWidth;
  el.classList.add(cls);
  setTimeout(() => el.classList.remove(cls), ms);
}

// 一个东西沿着折线匀速走（按每段长短分时间）
function travel(el, pts, { minMs = 1200, maxMs = 2600, perPx = 2.2 } = {}) {
  const seg = pts.slice(1).map((p, i) => Math.hypot(p.x - pts[i].x, p.y - pts[i].y));
  const total = seg.reduce((a, b) => a + b, 0) || 1;
  let acc = 0;
  const frames = pts.map((p, i) => {
    if (i) acc += seg[i - 1];
    return { transform: `translate(${p.x.toFixed(1)}px, ${p.y.toFixed(1)}px)`, offset: acc / total, opacity: i === 0 ? 0 : 1 };
  });
  return el.animate(frames, { duration: Math.min(maxMs, Math.max(minMs, total * perPx)), easing: 'ease-in-out' });
}

function landPing(canvas, at, text, color) {
  const ping = document.createElement('div');
  ping.className = 'land-ping';
  ping.style.cssText = `left:${at.x}px;top:${at.y - 30}px;background:${color}`;
  ping.textContent = text;
  canvas.appendChild(ping);
  setTimeout(() => ping.remove(), 1500);
}

// ---------- 派活：一张任务单从负责人桌上沿着那个部门的线飞到员工工位 ----------
// 新的 task.assigned / task.queued（或带负责人的 task.added）推送来了、页面重拉快照知道交给谁之后调用。
export function dispatchTask(emKey, title = '', opts = {}) {
  if (reducedMotion()) return;
  const canvas = document.getElementById('office-canvas');
  const station = findNodeEl(emKey);
  const gmEl = document.getElementById('node-gm');
  if (!canvas || !station || !gmEl || !station.classList.contains('station-card')) return;

  const room = station.closest('.dept-card.room');
  const roomKey = room ? room.getAttribute('data-dept-id') : '';
  // 后台自动派的活算监工派的：从这间房的监工席直接飞到工位（没有监工席的房间照旧从负责人桌出发）
  const head = opts.fromHead && room ? room.querySelector('.head-desk') : null;
  const from = head || gmEl;
  const branch = head ? null : [...document.querySelectorAll('#arc-canvas path.org-branch')].find(p => p.getAttribute('data-room') === roomKey);
  // 路线：负责人桌子 → 部门那条线（从负责人下沿到房间上沿）→ 员工工位；没有线时直接飞过去
  const pts = [centerOf(head || gmEl.querySelector('.gm-desk-wrap') || gmEl)];
  if (branch) {
    const len = branch.getTotalLength();
    for (let i = 0; i <= 24; i++) {
      const p = branch.getPointAtLength(len * i / 24);
      pts.push({ x: p.x, y: p.y });
    }
  }
  const dest = centerOf(station.querySelector('.station-svg-wrap') || station);
  pts.push(dest);

  const chip = document.createElement('div');
  chip.className = 'task-fly';
  chip.innerHTML = `
    <svg viewBox="0 0 24 28" width="20" height="24" aria-hidden="true">
      <rect x="2" y="3" width="20" height="23" rx="3" fill="#fff" stroke="var(--ai-gm)" stroke-width="2"/>
      <rect x="8" y="1" width="8" height="5" rx="1.5" fill="var(--ai-gm)"/>
      <path d="M6 12h12M6 16h12M6 20h8" stroke="#9aa3ad" stroke-width="1.6" stroke-linecap="round"/>
    </svg>
    <span>${escapeHtml(shortTitle(title, 14) || '新活')}</span>`;
  canvas.appendChild(chip);
  if (branch) {
    branch.classList.add('dispatching');
    setTimeout(() => branch.classList.remove('dispatching'), 2400);
  }
  flash(from);
  travel(chip, pts).onfinish = () => {
    chip.remove();
    landPing(canvas, dest, head ? '📋 监工派来新活' : '📋 接到新活', 'var(--ai-gm)');
    flash(station);
  };
}
