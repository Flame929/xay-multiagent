// kill-switch.js - 顶栏的总闸（arch-v10 §3.5）：红色"全部停下"按钮；拉下以后显示"已全部停下"和"恢复"。
// 只挂在 org.html 顶栏的 #kill-switch-mount 上，自带样式，不改别的模块。
// 状态读 GET /api/v2/halt（账本现状 halt）；写操作走 api.js 的 postAction（带 X-Xay-Action: control 头，服务端还会查来源）。
// 公司页每次重画完会发 xay:rendered 事件（带 seq），这里跟着重读一次总闸状态。
import { getUrlParams, postAction } from './api.js';

const STYLE_ID = 'kill-switch-style';
const CSS = `
#kill-switch-mount { display: inline-flex; align-items: center; gap: 6px; }
.ks-btn {
  padding: 6px 10px; border-radius: 6px; font-size: 13px; font-weight: 600; line-height: 1.2;
  display: inline-flex; align-items: center; gap: 5px; white-space: nowrap;
}
.ks-btn[disabled] { opacity: .6; cursor: default; }
.ks-stop { background: var(--err); color: #fff; border: 1px solid var(--err); }
.ks-stop:hover:not([disabled]) { filter: brightness(1.08); }
.ks-resume { background: var(--bg-layer-1); color: var(--label-1); border: 1px solid var(--border-3); }
.ks-resume:hover:not([disabled]) { background: var(--bg-hover); }
.ks-badge {
  display: inline-flex; align-items: center; gap: 6px; padding: 4px 9px; border-radius: 12px; white-space: nowrap;
  background: var(--err-bg); color: var(--err); font-size: 12px; font-weight: 600;
  border: 1px solid color-mix(in srgb, var(--err) 30%, transparent);
}
.ks-badge .ks-dot { width: 7px; height: 7px; border-radius: 50%; background: var(--err); }
.ks-icon { width: 10px; height: 10px; border-radius: 2px; background: currentColor; display: inline-block; }
.ks-badge .ks-short { display: none; }
@media (max-width: 768px) { .ks-badge .ks-long { display: none; } .ks-badge .ks-short { display: inline; } }
`;

let current = { halt: null, loaded: false };
let busy = false;
let lastSeq = null;

function mountEl() {
  return document.getElementById('kill-switch-mount');
}

function injectStyle() {
  if (document.getElementById(STYLE_ID)) return;
  const el = document.createElement('style');
  el.id = STYLE_ID;
  el.textContent = CSS;
  document.head.appendChild(el);
}

function sinceText(ts) {
  if (!ts) return '';
  const d = new Date(ts * 1000);
  const hh = String(d.getHours()).padStart(2, '0');
  const mm = String(d.getMinutes()).padStart(2, '0');
  return `${hh}:${mm}`;
}

function whoText(by) {
  if (by === 'user') return '你';
  if (by === 'gm') return '负责人';
  return '监工';          // office / rules：后台按规则自动拉的总闸
}

function render() {
  const root = mountEl();
  if (!root) return;
  root.innerHTML = '';
  if (!current.loaded) return;
  const halt = current.halt;
  if (halt) {
    const badge = document.createElement('span');
    badge.className = 'ks-badge';
    badge.title = `${whoText(halt.by)}在 ${sinceText(halt.ts)} 拉了总闸：不再起新的活，正在做的那一轮已经叫停，桌子上的改动都保留着`;
    badge.innerHTML = '<span class="ks-dot"></span><span class="ks-long">已全部停下</span><span class="ks-short">已停下</span>';
    const btn = document.createElement('button');
    btn.type = 'button';
    btn.className = 'ks-btn ks-resume';
    btn.textContent = '恢复';
    btn.title = '松开总闸：排着队的活接着做（被打断的那几件要你在"需要你"里决定怎么处理）';
    btn.disabled = busy;
    btn.addEventListener('click', () => act('resume'));
    root.append(badge, btn);
  } else {
    const btn = document.createElement('button');
    btn.type = 'button';
    btn.className = 'ks-btn ks-stop';
    btn.innerHTML = '<span class="ks-icon"></span><span>全部停下</span>';
    btn.title = '总闸：让所有员工都停下（改动都保留）';
    btn.disabled = busy;
    btn.addEventListener('click', () => act('stop_all'));
    root.append(btn);
  }
}

async function refresh() {
  const p = getUrlParams();
  if (p.snapshot) {            // 静态快照预览：没有服务端，不显示总闸
    current = { halt: null, loaded: false };
    render();
    return;
  }
  try {
    const qs = p.demo ? '?demo=' + encodeURIComponent(p.demo) : '';
    const res = await fetch('/api/v2/halt' + qs);
    if (!res.ok) throw new Error('HTTP ' + res.status);
    const j = await res.json();
    current = { halt: j.halt || null, loaded: true };
  } catch (_) {
    // 读不到就先不显示按钮（页面另有"读不到数据"的提示），下次重画再试
    current = { halt: current.halt, loaded: current.loaded };
  }
  render();
}

async function act(action) {
  if (busy) return;
  if (action === 'stop_all') {
    const ok = window.confirm('确定要让所有员工都停下吗？\n\n正在做的那一轮会被打断，改动都保留在各自的桌子上；'
      + '排着队的活原样留着，点"恢复"以后接着做。');
    if (!ok) return;
  }
  busy = true;
  render();
  try {
    await postAction('control', 'control', { action }, action === 'stop_all' ? '没能拉下总闸' : '没能恢复');
  } catch (err) {
    window.alert((err && err.message) || String(err));
  } finally {
    busy = false;
    await refresh();
  }
}

function init() {
  if (!mountEl()) return;
  injectStyle();
  refresh();
  document.addEventListener('xay:rendered', (e) => {
    const seq = e && e.detail ? e.detail.seq : null;
    if (seq !== null && seq === lastSeq) return;
    lastSeq = seq;
    refresh();
  });
}

if (document.readyState === 'loading') {
  document.addEventListener('DOMContentLoaded', init);
} else {
  init();
}
