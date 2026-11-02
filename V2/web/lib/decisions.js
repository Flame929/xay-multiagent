// decisions.js - 顶部"需要你"弹条（references/plans/office-spec.md 二½）。
// 每条：发生了什么（标题、说明照后端给的大白话原样显示）→ 相关文件（点一下在页面里打开：看全文 / 看改了哪几行）→
// 你可以选的几个选项 → "交给监工"（监工按默认做法处理）、"交给负责人"（从这里拿走，负责人在 xay2 wait 里处理）；
// 右上角"关掉" = 交给负责人。只显示还没交出去的（decisions[] 里 delegated 为空的）。
// 提交：POST /api/v2/decision {id, option} 选一个选项；{id, delegate: "rules" | "lead"} 交出去（请求头、同源检查同选项）。
// 文件：GET /api/v2/file?conv=&path=[&task=] → {path, text, truncated, from}；GET /api/v2/diff?task=&path= → 改动（diff 文本）。
// 管家还没有这两个接口 / 这条没有 files 时，照旧只显示文字和选项。
import { escapeHtml } from './utils.js';
import { postDecision, postAction, currentContext, getUrlParams } from './api.js';

const FILE_KIND = { change: '改动', diff: '改动', review: '审查', conflict: '冲突', report: '汇报', card: '任务卡' };
const DELEGATE_TIP = {
  rules: '交给监工：监工按默认做法处理（改动没问题就放进项目、网络断了就让他接着做、没动静就再等等、要审查就让审查的 AI 看）',
  lead: '交给负责人：这条从你这里拿走，由负责人（主 AI）来处理'
};

/** 还要你拿主意的（没交出去的）几条。 */
export function openDecisions(decisions) {
  return (decisions || []).filter(dc => dc && !dc.delegated);
}

function filesHtml(dc) {
  const files = (Array.isArray(dc.files) ? dc.files : []).filter(f => f && (f.path || typeof f === 'string'));
  if (!files.length) return '';
  const shown = files.slice(0, 8);
  return `<div class="decision-files"><span class="decision-files-lab">相关文件</span>${shown.map(f => {
    const path = typeof f === 'string' ? f : f.path;
    const kind = typeof f === 'string' ? '' : (f.kind || '');
    const name = String(path).split('/').pop() || path;
    return `<button type="button" class="decision-file" data-file="${escapeHtml(path)}" data-kind="${escapeHtml(kind)}" title="${escapeHtml(`点一下在这里打开：${path}`)}">`
      + `<svg viewBox="0 0 16 16" width="12" height="12" aria-hidden="true"><path d="M4 1.5h5.5L13 5v9.5H4z" fill="none" stroke="currentColor" stroke-width="1.4" stroke-linejoin="round"/><path d="M9.5 1.5V5H13" fill="none" stroke="currentColor" stroke-width="1.4" stroke-linejoin="round"/></svg>`
      + `<span>${escapeHtml(name)}</span>${FILE_KIND[kind] ? `<em>${FILE_KIND[kind]}</em>` : ''}</button>`;
  }).join('')}${files.length > shown.length ? `<span class="decision-files-more">还有 ${files.length - shown.length} 个</span>` : ''}</div>`;
}

function cardHtml(dc) {
  const id = escapeHtml(dc.id);
  const options = dc.options || [];
  return `
    <div class="decision-card" data-decision-id="${id}" data-task="${escapeHtml(dc.task || '')}">
      <button type="button" class="decision-close" data-delegate="lead" title="关掉：交给负责人处理" aria-label="关掉，交给负责人处理">×</button>
      <div class="decision-main">
        <div class="decision-badge"><span class="decision-pulse"></span><span>需要你</span></div>
        <div class="decision-content">
          <div class="decision-title">${escapeHtml(dc.title || '有件事要你拿主意')}</div>
          ${dc.detail ? `<div class="decision-detail">${escapeHtml(dc.detail)}</div>` : ''}
          ${filesHtml(dc)}
        </div>
      </div>
      <div class="decision-actions">
        ${options.length ? `<span class="decision-lab">你可以</span>` : ''}
        ${options.map(opt => `
          <button type="button" class="btn-decision ${opt.danger ? 'btn-danger' : 'btn-normal'}" data-opt-id="${escapeHtml(opt.id)}" data-danger="${!!opt.danger}">
            ${escapeHtml(opt.label || opt.id)}</button>`).join('')}
        <span class="decision-sep" aria-hidden="true"></span>
        <button type="button" class="btn-delegate" data-delegate="rules" title="${escapeHtml(DELEGATE_TIP.rules)}">交给监工</button>
        <button type="button" class="btn-delegate" data-delegate="lead" title="${escapeHtml(DELEGATE_TIP.lead)}">交给负责人</button>
      </div>
    </div>`;
}

export function renderDecisionsBar(container, decisions = [], onResolved) {
  if (!container) return;
  container._onResolved = onResolved;
  container._decisions = decisions || [];
  const list = openDecisions(decisions);
  if (!list.length) {
    if (container.style.display !== 'none') container.style.display = 'none';
    if (container._html) { container._html = ''; container.innerHTML = ''; }
    return;
  }
  if (container.style.display !== 'block') container.style.display = 'block';

  // 和上次一样就不动（"需要你"的闪点不从头闪；正在提交的按钮不被打断）
  const html = `<div class="decisions-container">${list.map(cardHtml).join('')}</div>`;
  if (container._html === html) return;
  container._html = html;
  container.innerHTML = html;

  // 绑定事件：只绑一次，挂在横条上（里面的按钮换了也照样认）
  if (container.dataset.bound) return;
  container.dataset.bound = '1';
  container.addEventListener('click', async e => {
    const card = e.target.closest('.decision-card');
    if (!card || !container.contains(card)) return;
    const dcId = card.getAttribute('data-decision-id');
    const file = e.target.closest('.decision-file');
    if (file) {
      e.stopPropagation();
      const dc = (container._decisions || []).find(d => d.id === dcId) || {};
      openFileViewer({ path: file.getAttribute('data-file'), kind: file.getAttribute('data-kind'), task: dc.task || card.getAttribute('data-task') || '' }, file);
      return;
    }
    const del = e.target.closest('[data-delegate]');
    const btn = e.target.closest('.btn-decision');
    if (!del && !btn) return;
    e.stopPropagation();
    if (btn) {
      const label = btn.textContent.trim();
      if (btn.getAttribute('data-danger') === 'true' && !window.confirm(`确定要「${label}」吗？这一步做了就退不回来。`)) return;
    }
    const target = del || btn;
    target.disabled = true;
    const original = target.textContent;
    if (target.classList.contains('decision-close')) target.textContent = '…'; else target.textContent = '提交中…';
    try {
      if (del) await delegateDecision(dcId, del.getAttribute('data-delegate'));
      else await postDecision(dcId, btn.getAttribute('data-opt-id'));
      card.classList.add('leaving');
      if (container._onResolved) container._onResolved(dcId, del ? 'delegate:' + del.getAttribute('data-delegate') : btn.getAttribute('data-opt-id'));
    } catch (err) {
      alert((del ? '没交出去：' : '没提交上：') + (err.message || err));
      target.disabled = false;
      target.textContent = original;
    }
  });
}

/** 交出去：rules = 交给监工（按默认做法），lead = 交给负责人。 */
export async function delegateDecision(id, to) {
  return postAction('decision', 'decide', { id, delegate: to === 'rules' ? 'rules' : 'lead' }, '交出去失败');
}

// ---------------------------------------------------------------- 文件查看框（在页面里看全文 / 看改了哪几行）

const fv = { el: null, opener: null, file: null, tab: 'text', cache: new Map() };

function qs(params) {
  const q = new URLSearchParams();
  Object.entries(params).forEach(([k, v]) => { if (v) q.set(k, v); });
  const demo = getUrlParams().demo;
  if (demo) q.set('demo', demo);
  return q.toString();
}

async function load(kind, file) {
  const key = kind + '|' + file.task + '|' + file.path;
  if (fv.cache.has(key)) return fv.cache.get(key);
  const conv = currentContext().conv;
  const url = kind === 'diff' ? '/api/v2/diff?' + qs({ task: file.task, path: file.path, conv })
    : '/api/v2/file?' + qs({ conv, path: file.path, task: file.task });
  let out;
  try {
    const res = await fetch(url);
    const type = res.headers.get('content-type') || '';
    if (!res.ok) {
      let detail = '';
      try { const j = await res.json(); detail = j.detail || j.error || ''; } catch (_) {}
      // 管家还没有这个接口（老版本）：路由本身 404，说明白；文件真的不在了：照后端说的原因写
      const noRoute = res.status === 404 && (!detail || /页面|not found/i.test(detail));
      out = { error: noRoute ? '现在还看不了：管家还不支持在页面里打开文件（更新后就能看）' : (detail || `读不到（${res.status}）`) };
    } else if (type.includes('json')) {
      const j = await res.json();
      out = kind === 'diff' ? { text: j.diff || j.text || '', from: j.from || '' } : { text: j.text || '', truncated: !!j.truncated, from: j.from || '', path: j.path };
    } else {
      out = { text: await res.text() };
    }
  } catch (err) {
    out = { error: '读不到：' + (err.message || err) };
  }
  if (!out.error) fv.cache.set(key, out);
  return out;
}

function diffHtml(text) {
  if (!text.trim()) return '<div class="fv-empty">这个文件没有改动</div>';
  return `<pre class="fv-code fv-diff">${text.split('\n').map(line => {
    const cls = line.startsWith('+++') || line.startsWith('---') ? 'h' : line.startsWith('@@') ? 'at' : line.startsWith('+') ? 'add' : line.startsWith('-') ? 'del' : '';
    return `<span${cls ? ` class="${cls}"` : ''}>${escapeHtml(line) || ' '}</span>`;
  }).join('')}</pre>`;                              // 每行是一个块，不再另外加换行（不然隔一行空一行）
}

async function paint() {
  if (!fv.el) return;
  const body = fv.el.querySelector('.fv-body');
  const meta = fv.el.querySelector('.fv-meta');
  fv.el.querySelectorAll('[data-fv-tab]').forEach(b => b.classList.toggle('active', b.getAttribute('data-fv-tab') === fv.tab));
  body.innerHTML = '<div class="fv-empty">正在读…</div>';
  meta.textContent = '';
  const file = fv.file;
  const out = await load(fv.tab === 'diff' ? 'diff' : 'text', file);
  if (!fv.el || fv.file !== file) return;
  if (out.error) { body.innerHTML = `<div class="fv-empty err">${escapeHtml(out.error)}</div>`; return; }
  const FROM = { desk: '他工位里的版本（改好了，还没放进项目）', project: '项目里现在的版本' };
  meta.textContent = [out.from ? `这是${FROM[out.from] || out.from}` : '', out.truncated ? '文件太长，只显示了前面一部分' : ''].filter(Boolean).join(' · ');
  body.innerHTML = fv.tab === 'diff' ? diffHtml(out.text) : `<pre class="fv-code">${escapeHtml(out.text) || ' '}</pre>`;
  body.scrollTop = 0;
}

function onKey(e) {
  if (e.key === 'Escape' && fv.el) { e.preventDefault(); e.stopImmediatePropagation(); closeFileViewer(); }
}

/** 在页面里打开一个文件：{path, kind, task}。有任务编号时能切"看改了哪几行"。 */
export function openFileViewer(file, opener) {
  closeFileViewer(false);
  fv.file = file;
  fv.opener = opener || null;
  const canDiff = !!file.task;
  fv.tab = canDiff && (file.kind === 'change' || file.kind === 'diff' || file.kind === 'conflict') ? 'diff' : 'text';
  const el = document.createElement('div');
  el.className = 'fv-overlay';
  el.innerHTML = `
    <div class="fv-box" role="dialog" aria-modal="true" aria-label="${escapeHtml('看文件：' + file.path)}">
      <header class="fv-head">
        <div class="fv-title"><b>${escapeHtml(String(file.path).split('/').pop())}</b><small>${escapeHtml(file.path)}</small></div>
        <div class="fv-tabs" role="tablist">
          <button type="button" class="fv-tab" data-fv-tab="text">看全文</button>
          ${canDiff ? '<button type="button" class="fv-tab" data-fv-tab="diff">看改了哪几行</button>' : ''}
        </div>
        <button type="button" class="fv-close" title="关闭（Esc）" aria-label="关闭">×</button>
      </header>
      <div class="fv-meta"></div>
      <div class="fv-body"></div>
    </div>`;
  el.addEventListener('click', e => {
    if (e.target === el || e.target.closest('.fv-close')) { closeFileViewer(); return; }
    const t = e.target.closest('[data-fv-tab]');
    if (t && t.getAttribute('data-fv-tab') !== fv.tab) { fv.tab = t.getAttribute('data-fv-tab'); paint(); }
  });
  document.body.appendChild(el);
  fv.el = el;
  window.addEventListener('keydown', onKey, true);
  el.querySelector('.fv-close').focus();
  paint();
}

export function closeFileViewer(restore = true) {
  if (!fv.el) return;
  window.removeEventListener('keydown', onKey, true);
  fv.el.remove();
  fv.el = null;
  const back = fv.opener;
  fv.opener = null;
  if (restore && back && back.isConnected && back.focus) back.focus();
}
