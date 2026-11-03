// render-plan.js - 右侧"任务拆解""计划版本"两个页签（2026-10-31 第二版：和画布、公司页房间用同一份计划模型 plan-model.js）。
// 层级（2026-10-31 用户纠正）：大任务 → 中任务（部门 / 组，各有监工）→ 小任务（交给某个 AI 的一件）→ 步骤（AI 自己拆）。
// 任务拆解：顶上"展开任务画布"按钮；按中任务分组（组头 = 编号 + 名字 + 状态 + 做完几件 + 验收方式，颜色和画布、房间牌子一样；
//   点组头打开画布并高亮这个中任务，"详情"看中任务的详情），中任务里不再分部门（旧数据的部门只当小任务上的小标签），
//   每件小任务一行（小头像、名字、谁做、状态、第几步；有大白话再一行"做成了"），点一行右边滑出它的详情（plan-detail.js，和画布上点一件是同一个，步骤也在里面）。
// 计划版本：每一版每条改动前面挂上它所属中任务的小标签；中任务的增改翻成大白话；每一版可以"在画布上看"（高亮这一版动到的中任务 / 活）。
import { escapeHtml, formatTime } from './utils.js';
import { partChipHtml, checkChipHtml, itemStateHtml, whoText, itemTip, avatarHtml, wireAvatars, deptTagHtml } from './plan-model.js';
import { openDetail, refreshDetail } from './plan-detail.js';
import { OPEN_ICON } from './task-canvas.js';
import { miniBarHtml } from './progress-bar.js';

// 和上次一样就不动（展开着的折叠块、滚动位置都不被打断）
function setHtml(el, html) {
  if (el._html === html) return false;
  el._html = html;
  el.innerHTML = html;
  return true;
}

let lastModel = null;
let handlers = { openCanvas: null, people: {} };
const openMore = new Set();      // 计划版本里展开了"还有 N 处"的版本

/** app.js 调一次：openCanvas(focus, opener) 打开画布（focus 为空就是整张不高亮）。 */
export function bindPlanPanel(h = {}) {
  handlers = Object.assign(handlers, h);
  ['tab-plan', 'tab-history'].forEach(id => {
    const el = document.getElementById(id);
    if (!el || el.dataset.planBound) return;
    el.dataset.planBound = '1';
    el.addEventListener('click', onClick);
    el.addEventListener('toggle', e => {
      const d = e.target;
      if (d && d.dataset && d.dataset.ver) { if (d.open) openMore.add(d.dataset.ver); else openMore.delete(d.dataset.ver); }
    }, true);
  });
}

function detailCtx(opener) {
  return {
    owner: 'plan', model: () => lastModel, opener,
    onShow: what => {
      document.querySelectorAll('#plan-list .pl-row.sel, #plan-list .pl-head.sel').forEach(el => el.classList.remove('sel'));
      if (!what) return;
      const sel = what.kind === 'item' ? `.pl-row[data-plan-item="${CSS.escape(what.id)}"]` : `.pl-head[data-plan-head="${CSS.escape(what.id)}"]`;
      const el = document.querySelector('#plan-list ' + sel);
      if (el) el.classList.add('sel');
    },
    onCanvas: focus => { if (handlers.openCanvas) handlers.openCanvas(focus, null); }
  };
}

function onClick(e) {
  const t = e.target;
  const open = (focus, btn) => { if (handlers.openCanvas) handlers.openCanvas(focus, btn); };
  const all = t.closest('[data-plan-canvas]');
  if (all) { open(null, all); return; }
  const info = t.closest('[data-plan-part-detail]');
  if (info) { openDetail({ kind: 'part', id: info.getAttribute('data-plan-part-detail') }, detailCtx(info)); return; }
  const head = t.closest('[data-plan-part]');
  if (head && lastModel) {
    const p = lastModel.partById.get(head.getAttribute('data-plan-part'));
    if (p) open({ parts: [p.id], items: [], label: `中任务 ${p.code}「${p.title}」` }, head);
    return;
  }
  const row = t.closest('[data-plan-item]');
  if (row) { openDetail({ kind: 'item', id: row.getAttribute('data-plan-item') }, detailCtx(row)); return; }
  const ver = t.closest('[data-plan-version]');
  if (ver && lastModel) {
    const v = lastModel.history.find(h => String(h.version) === ver.getAttribute('data-plan-version'));
    if (v) open({ parts: v.parts, items: v.items, label: `第 ${v.version} 版动到的`, gone: v.gone }, ver);
  }
}

// ---------------------------------------------------------------- 任务拆解

function rowHtml(it, model) {
  const deps = it.deps.length ? ` · 等 ${it.deps.length} 件` : '';
  const chk = it.check ? `<span class="pm-st t-${it.check.tone}">${escapeHtml(it.check.word)}</span>` : '';
  return `
    <button type="button" class="pl-row s-${escapeHtml(it.tone)}" data-plan-item="${escapeHtml(it.id)}" title="${escapeHtml(itemTip(it, model) + '\n点一下看详情和步骤')}">
      ${avatarHtml(it)}
      <span class="pl-row-main">
        <span class="pl-row-t">${it.legacy ? '<span class="tc-legacy">旧</span>' : ''}${escapeHtml(it.title)}</span>
        <span class="pl-row-m"><b class="who-${escapeHtml(it.type)}">${escapeHtml(whoText(it))}</b><span>${escapeHtml(it.sub + deps)}</span>${deptTagHtml(it)}</span>
        ${it.plain ? `<span class="pl-row-p">${escapeHtml(it.plain)}</span>` : ''}
      </span>
      <span class="pl-row-r">${chk}${itemStateHtml(it)}</span>
    </button>`;
}

function groupHtml(p, model) {
  const after = p.after.map(id => model.partById.get(id)).filter(Boolean);
  const pct = p.all ? Math.round(p.done * 100 / p.all) : 0;
  const body = p.items.map(it => rowHtml(it, model)).join('');       // 中任务里不再分部门
  return `
    <section class="pl-group s-${escapeHtml(p.tone)}" style="--pc:${p.color}">
      <div class="pl-head" data-plan-head="${escapeHtml(p.id)}">
        <button type="button" class="pl-head-main" data-plan-part="${escapeHtml(p.id)}" title="${escapeHtml(`在画布上看中任务 ${p.code}「${p.title}」`)}">
          ${partChipHtml(p, { cls: 'big' })}
          <span class="pl-go" aria-hidden="true">${OPEN_ICON}</span>
        </button>
        <div class="pl-head-meta">
          <span class="pm-st t-${escapeHtml(p.tone === 'ready' ? 'wait' : p.tone)}">${escapeHtml(p.word)}</span>
          <span class="pl-count">做完 ${p.done}/${p.all} 件</span>${miniBarHtml(p.pct)}
          ${checkChipHtml(p.check)}
          <button type="button" class="pl-info" data-plan-part-detail="${escapeHtml(p.id)}" title="看这个中任务的说明、监工怎么验收、先后">详情</button>
        </div>
        ${after.length ? `<div class="pl-after">接在 ${after.map(a => partChipHtml(a, { title: false })).join('')} 后面</div>` : ''}
        ${p.pct == null ? `<div class="pl-bar" aria-hidden="true"><i style="width:${pct}%"></i></div>` : ''}
      </div>
      ${body || '<div class="pl-none">还没有派小任务</div>'}
    </section>`;
}

/** 任务拆解页签。model = buildPlan(快照, 任务树)。 */
export function renderPlanTasks(model) {
  const listEl = document.getElementById('plan-list');
  const countEl = document.getElementById('plan-count');
  if (!listEl || !model) return;
  lastModel = model;
  if (countEl) countEl.textContent = String(model.counts.all);
  const parts = model.parts.filter(p => p.items.length || !(p.loose || p.id === 'all'));
  if (!model.items.length) {
    if (setHtml(listEl, '<div class="dept-empty">还没有派出任务</div>')) refreshDetail('plan');
    return;
  }
  const c = model.counts;
  const nParts = parts.filter(p => !p.loose).length;
  const pills = [
    ['ok', '做完了', c.done], ['run', '正在做', c.working], ['warn', '等你决定', c.needs], ['err', '没做成', c.failed],
    ['wait', '还没开始 / 排队', c.waiting]
  ].filter(x => x[2]).map(([t, w, n]) => `<span class="pm-st t-${t}">${w} ${n}</span>`).join('');
  const top = `
    <button type="button" class="tc-open pl-canvas" data-plan-canvas="1">
      <span class="tc-open-ic">${OPEN_ICON}</span>
      <span class="tc-open-txt"><b>展开任务画布</b><small>${escapeHtml([nParts > 1 ? `${nParts} 个中任务` : model.old ? '还没拆中任务' : '', `小任务 ${c.all} 件`, model.title].filter(Boolean).join(' · '))}</small></span>
      <span class="tc-open-go" aria-hidden="true">↗</span>
    </button>
    <div class="plan-summary">${pills}</div>`;
  if (setHtml(listEl, top + parts.map(p => groupHtml(p, model)).join(''))) wireAvatars(listEl);
  refreshDetail('plan');
}

// ---------------------------------------------------------------- 计划版本

function humanizeBy(by, people = {}) {
  if (!by) return '负责人';
  if (by === 'gm' || by.startsWith('lead:')) return '负责人';
  if (by === 'user') return '用户';
  if (by === 'office' || by === 'rules') return '监工';      // 后台按规则自动改的计划：页面上叫监工
  if (people[by]) return people[by].label || by;
  return by;
}

const OP_TEXT = { add: '加小任务', change: '改小任务', assign: '换人', remove: '撤小任务', part_add: '新开中任务', part_change: '改中任务', part_remove: '撤中任务' };
const SHOW_CHANGES = 6;

function changeHtml(ch, model) {
  const p = ch.part != null ? model.partById.get(ch.part) : null;
  const tag = p ? partChipHtml(p) : ch.partTitle || ch.kind === 'part' ? partChipHtml({ gone: true, title: ch.partTitle || '中任务' }) : '<span class="pm-chip none"><b>—</b><span>没归中任务</span></span>';
  return `
    <div class="pl-change op-${escapeHtml(ch.op)}">
      <span class="pl-change-tag">${tag}</span>
      <span class="pl-change-op">${escapeHtml(OP_TEXT[ch.op] || '改')}</span>
      <span class="pl-change-t">${escapeHtml(ch.text)}</span>
    </div>`;
}

/** 计划版本页签。model = buildPlan(快照, …)（history 在里面）；people 用来把"谁改的"写成名字。 */
export function renderPlanHistory(model, people = {}) {
  const listEl = document.getElementById('history-list');
  const countEl = document.getElementById('history-count');
  if (!listEl || !model) return;
  lastModel = model;
  const history = model.history.slice().sort((a, b) => (b.version || 0) - (a.version || 0));
  if (countEl) countEl.textContent = String(model.version || history.length || 0);
  if (!history.length) {
    setHtml(listEl, '<div class="dept-empty">暂无计划版本记录</div>');
    return;
  }
  setHtml(listEl, history.map(v => {
    const author = humanizeBy(v.by, people);
    const who = v.proposal
      ? `<b>${escapeHtml(author)}</b> 提的申请，${escapeHtml(humanizeBy(v.approved_by, people))}通过了`
      : `<b>${escapeHtml(author)}</b> 改的`;
    const timeStr = formatTime(v.ts);
    const n = v.changes.length;
    const shown = v.changes.slice(0, SHOW_CHANGES).map(ch => changeHtml(ch, model)).join('');
    const rest = n > SHOW_CHANGES
      ? `<details class="pl-more" data-ver="${v.version}"${openMore.has(String(v.version)) ? ' open' : ''}><summary>还有 ${n - SHOW_CHANGES} 处</summary>${v.changes.slice(SHOW_CHANGES).map(ch => changeHtml(ch, model)).join('')}</details>`
      : '';
    const touched = v.parts.length || v.items.length;
    const what = [v.parts.length ? `${v.parts.length} 个中任务` : '', v.items.length ? `${v.items.length} 件小任务` : ''].filter(Boolean).join('、');
    return `
      <div class="plan-version-card">
        <div class="version-header">
          <div class="version-badge">第 ${v.version} 版</div>
          <div class="version-author">${who}</div>
          ${timeStr ? `<div class="version-time mono">${timeStr}</div>` : ''}
        </div>
        <div class="version-reason">原因：${escapeHtml(v.reason || '没写原因')}</div>
        <div class="version-changes-title">这一版改了 ${n} 处${what ? `，动到 ${what}` : ''}：</div>
        <div class="version-changes-list">${shown}${rest}</div>
        ${touched ? `<button type="button" class="pl-vcanvas" data-plan-version="${v.version}">${OPEN_ICON}<span>在画布上看这一版动到的</span></button>` : ''}
      </div>`;
  }).join(''));
}
