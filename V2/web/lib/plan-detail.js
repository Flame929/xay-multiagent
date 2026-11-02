// plan-detail.js - 详情抽屉（2026-10-31，用户："点击具体的可以展示详情！"）。
// 画布上点一件小任务 / 一个中任务、"任务拆解"里点一行，都从右边滑出这个抽屉（四处同一个组件，内容由 plan-model.js 的
// itemDetailHtml / partDetailHtml 拼）：小任务的做成了什么（大白话）、任务卡、验收命令、最近一次汇报、合并回项目的文件、开工 / 做完、干了多久、第几次、
// 为什么交给他、监工结论、他自己拆的步骤；中任务的说明、监工怎么验收、排在哪些后面、开始 / 结束、里面的小任务。
// 抽屉里点别的小任务 / 中任务就换成它的详情（左上角"‹ 返回"回到上一个）；"在画布上看"交给打开它的地方处理。只画，不发请求、不写东西。
import { itemDetailHtml, partDetailHtml } from './plan-model.js';

const dd = { el: null, body: null, what: null, ctx: null, back: [], sig: '' };

export function isDetailOpen() {
  return !!dd.el;
}

/** 现在抽屉里是哪件：{kind: 'item' | 'part', id} 或 null。 */
export function detailShowing() {
  return dd.el ? dd.what : null;
}

function model() {
  try { return dd.ctx && dd.ctx.model ? dd.ctx.model() : null; } catch (_) { return null; }
}

function render(keepScroll) {
  if (!dd.el) return;
  const m = model();
  let html = '';
  if (m && dd.what) {
    if (dd.what.kind === 'item') {
      const it = m.itemOf(dd.what.id);
      html = it && m.items.includes(it) ? itemDetailHtml(it, m) : '';
      if (it && it.id !== dd.what.id) dd.what = { kind: 'item', id: it.id };
    } else {
      const p = m.partById.get(dd.what.id);
      html = p ? partDetailHtml(p, m) : '';
    }
  }
  if (!html) html = '<div class="pd-gone">这一项已经不在计划里了（撤回了或者撤掉了）。</div>';
  if (html === dd.sig) return;
  const top = dd.body.scrollTop;
  dd.sig = html;
  dd.body.innerHTML = html;
  dd.body.scrollTop = keepScroll ? top : 0;
  dd.el.querySelector('.pd-back').hidden = !dd.back.length;
  if (dd.ctx && dd.ctx.onShow) dd.ctx.onShow(dd.what);
}

function show(what, push) {
  if (push && dd.what && !(dd.what.kind === what.kind && dd.what.id === what.id)) dd.back.push(dd.what);
  dd.what = what;
  dd.sig = '';
  render(false);
}

function onClick(e) {
  const t = e.target;
  if (t.closest('.pd-close')) { closeDetail(); return; }
  if (t.closest('.pd-back')) { const prev = dd.back.pop(); if (prev) show(prev, false); return; }
  const it = t.closest('[data-detail-item]');
  if (it) { show({ kind: 'item', id: it.getAttribute('data-detail-item') }, true); return; }
  const p = t.closest('[data-detail-part]');
  if (p) { show({ kind: 'part', id: p.getAttribute('data-detail-part') }, true); return; }
  const act = t.closest('[data-detail-act="canvas"]');
  if (act && dd.ctx && dd.ctx.onCanvas) {
    const part = act.getAttribute('data-part');
    const item = act.getAttribute('data-item');
    const m = model();
    const pp = m && m.partById.get(part);
    const ii = item && m ? m.itemOf(item) : null;
    dd.ctx.onCanvas({ parts: part != null ? [part] : [], items: item ? [item] : [],
      label: ii ? `这件小任务「${ii.title}」` : pp ? `中任务 ${pp.code}「${pp.title}」` : '选中的' });
  }
}

function onKey(e) {
  if (!dd.el || e.key !== 'Escape') return;
  e.preventDefault();
  e.stopImmediatePropagation();          // 只关抽屉，不连带关画布、详情栏
  closeDetail();
}

/**
 * 打开（或换成）一件活 / 一个小任务的详情。what = {kind: 'item' | 'part', id}；
 * ctx = {model() → 计划模型（每次重画都现取，数据变了跟着变）, onCanvas(focus)（"在画布上看"）, onShow(what), onClose(), opener}。
 */
export function openDetail(what, ctx = {}) {
  if (typeof document === 'undefined' || !what) return;
  if (!dd.el) {
    const el = document.createElement('aside');
    el.className = 'pd-drawer';
    el.setAttribute('role', 'dialog');
    el.setAttribute('aria-label', '详情');
    el.innerHTML = `<div class="pd-bar"><button type="button" class="pd-back" hidden>‹ 返回</button><span class="pd-bar-t">详情</span>
      <button type="button" class="pd-close" title="关闭（Esc）" aria-label="关闭详情">×</button></div><div class="pd-body"></div>`;
    document.body.appendChild(el);
    dd.el = el;
    dd.body = el.querySelector('.pd-body');
    el.addEventListener('click', onClick);
    window.addEventListener('keydown', onKey, true);
    dd.back = [];
  } else if (dd.ctx && dd.ctx !== ctx && dd.ctx.onShow) {
    dd.ctx.onShow(null);
  }
  dd.ctx = ctx;
  show(what, !!dd.what);
  const close = dd.el.querySelector('.pd-close');
  if (close && !dd.el.contains(document.activeElement)) close.focus({ preventScroll: true });
}

/** 换一个"主人"（比如从任务拆解打开的抽屉，点"在画布上看"后交给画布）：内容不变，数据从新主人那儿取。 */
export function setDetailContext(ctx) {
  if (!dd.el) return;
  dd.ctx = ctx;
  render(true);
}

/** 数据变了：抽屉开着就按新数据重画（滚动位置不动）。owner 给了且不是现在的主人，不动。 */
export function refreshDetail(owner) {
  if (!dd.el || (owner && dd.ctx && dd.ctx.owner && dd.ctx.owner !== owner)) return;
  render(true);
}

export function closeDetail(owner) {
  if (!dd.el || (owner && dd.ctx && dd.ctx.owner && dd.ctx.owner !== owner)) return;
  window.removeEventListener('keydown', onKey, true);
  const ctx = dd.ctx;
  dd.el.remove();
  dd.el = dd.body = null;
  dd.what = null;
  dd.ctx = null;
  dd.back = [];
  dd.sig = '';
  if (ctx && ctx.onShow) ctx.onShow(null);
  if (ctx && ctx.onClose) ctx.onClose();
  if (ctx && ctx.opener && ctx.opener.isConnected && ctx.opener.focus) ctx.opener.focus({ preventScroll: true });
}
