// office-view.js - 公司页能像任务画布一样拖动、缩放（references/plans/office-spec.md 二.8、二.11）。
// 按住空白处拖动平移；滚轮 / 触控板双指上下滑缩放（双指左右滑平移，Ctrl / ⌘ + 滚轮或捏合也是缩放）；
// 右下角"−  100%  +  |  适应屏幕  100%"。整片办公室（#office-canvas）用 transform 平移缩放，房间、工位、连线一起动；
// 只改 transform，数据刷新时不动视图（不抖）。页面左右留边距：打开时内容离左右至少 MARGIN。
const MIN_S = 0.25, MAX_S = 2;
const MARGIN = 40;                 // 打开 / 适应屏幕时，内容离视口四边至少这么远（不贴边）

const ov = { vp: null, cv: null, zoomOut: null, s: 1, x: 0, y: 0, drag: null, dragEnd: 0, homed: false };

function clamp(v, a, b) {
  return Math.min(b, Math.max(a, v));
}

function size() {
  return { w: ov.cv.offsetWidth, h: ov.cv.offsetHeight };
}

// 拖、缩放都别把办公室整个丢到屏幕外：至少留四成（最多 360 像素）在屏幕里
function keepInView() {
  const r = ov.vp.getBoundingClientRect();
  const { w, h } = size();
  const lim = (v, len, view) => {
    const keep = Math.min(view * 0.4, 360, len * ov.s);
    const lo = keep - len * ov.s, hi = view - keep;
    return lo <= hi ? clamp(v, lo, hi) : clamp(v, hi, lo);
  };
  ov.x = lim(ov.x, w, r.width);
  ov.y = lim(ov.y, h, r.height);
}

function apply() {
  if (!ov.cv) return;
  keepInView();
  const t = `translate(${Math.round(ov.x * 10) / 10}px, ${Math.round(ov.y * 10) / 10}px) scale(${Math.round(ov.s * 1000) / 1000})`;
  if (ov.cv.style.transform !== t) ov.cv.style.transform = t;
  if (ov.zoomOut) ov.zoomOut.textContent = Math.round(ov.s * 100) + '%';
}

function zoomAt(s, px, py) {
  s = clamp(s, MIN_S, MAX_S);
  ov.x = px - (px - ov.x) * s / ov.s;
  ov.y = py - (py - ov.y) * s / ov.s;
  ov.s = s;
  apply();
}

function center() {
  const r = ov.vp.getBoundingClientRect();
  return [r.width / 2, r.height / 2];
}

/** 打开时：100%，左右居中（放不下就从左边留 MARGIN 开始）；正在做的那一步看得见（在屏幕下半截以下时，挪到离顶上 80 处）。 */
export function homeOffice() {
  if (!ov.cv) return;
  const r = ov.vp.getBoundingClientRect();
  const { w } = size();
  ov.s = 1;
  ov.x = w + MARGIN * 2 <= r.width ? (r.width - w) / 2 : MARGIN;
  ov.y = 24;
  const now = ov.cv.querySelector('#dept-grid .step-band.s-now');
  if (now) {
    let top = 0;
    for (let n = now; n && n !== ov.cv; n = n.offsetParent) top += n.offsetTop;
    if (top + ov.y > r.height * 0.6) ov.y = 80 - top;
  }
  apply();
}

/** 适应屏幕：整片办公室放进来（最大 100%），四周留 MARGIN。 */
export function fitOffice() {
  if (!ov.cv) return;
  const r = ov.vp.getBoundingClientRect();
  const { w, h } = size();
  ov.s = clamp(Math.min((r.width - MARGIN * 2) / w, (r.height - MARGIN * 2) / h), MIN_S, 1);
  ov.x = (r.width - w * ov.s) / 2;
  ov.y = Math.max(MARGIN / 2, (r.height - h * ov.s) / 2);
  apply();
}

/** 点开一步后：这一步的房间要是有一截在屏幕下面，就把视图往上挪，让这一步从离顶上 80 处开始（只在点开时挪，刷新不挪）。 */
export function revealInOffice(el) {
  if (!ov.vp || !el) return;
  const r = ov.vp.getBoundingClientRect(), b = el.getBoundingClientRect();
  if (b.bottom <= r.bottom - 40) return;
  ov.y -= Math.min(b.top - r.top - 80, b.bottom - r.bottom + 60);
  apply();
}

/** 现在的视图（测试用）：{s, x, y}。 */
export function officeView() {
  return { s: ov.s, x: ov.x, y: ov.y };
}

/** 画好第一份数据后调一次：回到起始位置（之后数据刷新都不再动视图）。 */
export function officeDrawn() {
  if (ov.homed || !ov.cv || !ov.cv.offsetWidth) return;
  ov.homed = true;
  homeOffice();
}

// 这些地方按下去是点东西（工位、按钮、链接、信、收起的房间…），不当拖动
const HIT = 'button, a, input, textarea, select, label, summary, [data-person], [data-letter], .station-card, .rest-seat, .gm-station-box, .head-desk, .lead-desk, .mail-line, .tc-zoom';

function onDown(e) {
  if (e.button !== 0 || e.target.closest(HIT)) return;
  ov.drag = { id: e.pointerId, x: e.clientX, y: e.clientY, ox: ov.x, oy: ov.y, on: false };
}

function onMove(e) {
  const d = ov.drag;
  if (!d || d.id !== e.pointerId) return;
  if (!d.on) {
    if (Math.hypot(e.clientX - d.x, e.clientY - d.y) < 4) return;
    d.on = true;
    try { ov.vp.setPointerCapture(e.pointerId); } catch (_) {}
    ov.vp.classList.add('dragging');
  }
  ov.x = d.ox + e.clientX - d.x;
  ov.y = d.oy + e.clientY - d.y;
  apply();
}

function onUp(e) {
  if (!ov.drag || ov.drag.id !== e.pointerId) return;
  if (ov.drag.on) ov.dragEnd = Date.now();
  ov.drag = null;
  ov.vp.classList.remove('dragging');
}

function onWheel(e) {
  if (e.target.closest('.room-fold-list')) return;
  e.preventDefault();
  const r = ov.vp.getBoundingClientRect();
  const px = e.clientX - r.left, py = e.clientY - r.top;
  const k = e.deltaMode === 1 ? 33 : e.deltaMode === 2 ? r.height : 1;
  const dx = e.deltaX * k, dy = e.deltaY * k;
  if (e.ctrlKey || e.metaKey) zoomAt(ov.s * clamp(Math.exp(-dy * 0.01), 0.7, 1.4), px, py);      // 触控板捏合 / Ctrl + 滚轮
  else if (Math.abs(dx) > Math.abs(dy)) { ov.x -= dx; ov.y -= dy; apply(); }                      // 双指左右滑：平移
  else zoomAt(ov.s * clamp(Math.exp(-dy * 0.0018), 0.8, 1.25), px, py);                           // 滚轮 / 双指上下滑：缩放
}

function controlsHtml() {
  return `
    <button type="button" data-ov="out" title="缩小" aria-label="缩小">−</button>
    <output class="tc-zoom-n" aria-live="polite">100%</output>
    <button type="button" data-ov="in" title="放大" aria-label="放大">+</button>
    <span class="tc-zoom-sep" aria-hidden="true"></span>
    <button type="button" data-ov="fit" title="整个公司放进屏幕">适应屏幕</button>
    <button type="button" data-ov="one" title="原始大小，回到最上面">100%</button>`;
}

/** 页面启动时调一次。 */
export function initOfficeView() {
  const vp = document.getElementById('office-viewport');
  const cv = document.getElementById('office-canvas');
  if (!vp || !cv || ov.vp) return;
  ov.vp = vp;
  ov.cv = cv;
  vp.classList.add('pannable');
  const ctl = document.createElement('div');
  ctl.className = 'tc-zoom of-zoom';
  ctl.setAttribute('role', 'group');
  ctl.setAttribute('aria-label', '公司页缩放');
  ctl.innerHTML = controlsHtml();
  vp.appendChild(ctl);
  ov.zoomOut = ctl.querySelector('.tc-zoom-n');
  ctl.addEventListener('click', e => {
    const b = e.target.closest('[data-ov]');
    if (!b) return;
    const [cx, cy] = center();
    const act = b.getAttribute('data-ov');
    if (act === 'in') zoomAt(ov.s * 1.25, cx, cy);
    else if (act === 'out') zoomAt(ov.s / 1.25, cx, cy);
    else if (act === 'fit') fitOffice();
    else if (act === 'one') homeOffice();
  });
  vp.addEventListener('pointerdown', onDown);
  vp.addEventListener('pointermove', onMove);
  vp.addEventListener('pointerup', onUp);
  vp.addEventListener('pointercancel', onUp);
  vp.addEventListener('wheel', onWheel, { passive: false });
  // 刚拖完松手不算点（不打开工位、不开信）
  vp.addEventListener('click', e => { if (Date.now() - ov.dragEnd < 80 && !e.target.closest('.of-zoom')) { e.stopPropagation(); e.preventDefault(); } }, true);
  window.addEventListener('resize', () => apply());
  apply();
}
