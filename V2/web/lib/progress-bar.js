// progress-bar.js - 总进度条（设计 references/plans/arch-v11-router-parity.md 第 8 节）
// 数字全由快照算好（conversation.progress），页面不自己算；没有 progress 字段时整条不显示。
// 一条横条按 by_agent 分颜色段（每种 AI 用它头像的主色，没见过的种类用中性色），右边大字百分比 + 一句话；
// 数字变了平滑涨上去，到 100% 时轻轻闪一下；鼠标停在某一段上显示"Gemini 攒了 22%"。

const AI_COLOR = { codex: 'var(--ai-codex)', dsh: 'var(--ai-dsh)', agy: 'var(--ai-agy)', claude: 'var(--ai-claude)' };

export function agentColor(agent) {
  return AI_COLOR[agent] || 'var(--ai-other)';
}

/** 快照里这个对话的进度；没有就返回 null。 */
export function progressOf(data) {
  const p = (data && data.conversation && data.conversation.progress) || (data && data.progress);
  return p && typeof p === 'object' && Number.isFinite(Number(p.pct)) ? p : null;
}

/** 某个中任务（by_part 里同 id）的百分比；没有就返回 null。 */
export function partPct(prog, id) {
  if (!prog || !Array.isArray(prog.by_part)) return null;
  const hit = prog.by_part.find(x => x && x.id === id);
  return hit && Number.isFinite(Number(hit.pct)) ? clamp(Number(hit.pct)) : null;
}

function clamp(n) { return Math.max(0, Math.min(100, Math.round(n))); }

function el(tag, cls) { const e = document.createElement(tag); if (cls) e.className = cls; return e; }

function build(mount) {
  mount.innerHTML = '';
  const label = el('span', 'pg-label'); label.textContent = '总进度';
  const track = el('div', 'pg-track');
  const tip = el('div', 'pg-tip'); tip.hidden = true;
  const wrap = el('div', 'pg-track-wrap'); wrap.append(track, tip);
  const num = el('b', 'pg-num');
  const text = el('span', 'pg-text');
  const right = el('div', 'pg-right'); right.append(num, text);
  mount.append(label, wrap, right);
  const st = { track, tip, num, text, segs: new Map(), shown: null, raf: 0, last: null };
  track.addEventListener('mousemove', e => {
    const seg = e.target.closest('.pg-seg');
    if (!seg || !seg.dataset.tip) { tip.hidden = true; return; }
    tip.textContent = seg.dataset.tip;
    tip.hidden = false;
    const box = wrap.getBoundingClientRect();
    const s = seg.getBoundingClientRect();
    tip.style.left = Math.max(0, Math.min(box.width, s.left - box.left + s.width / 2)) + 'px';
  });
  track.addEventListener('mouseleave', () => { tip.hidden = true; });
  mount._pg = st;
  return st;
}

function countTo(st, target) {
  cancelAnimationFrame(st.raf);
  const from = st.shown == null ? target : st.shown;
  if (from === target || matchMedia('(prefers-reduced-motion: reduce)').matches) {
    st.shown = target; st.num.textContent = target + '%'; return;
  }
  const t0 = performance.now(), dur = 700;
  const step = now => {
    const k = Math.min(1, (now - t0) / dur);
    const e = 1 - Math.pow(1 - k, 3);
    st.shown = Math.round(from + (target - from) * e);
    st.num.textContent = st.shown + '%';
    if (k < 1) st.raf = requestAnimationFrame(step);
  };
  st.raf = requestAnimationFrame(step);
}

/**
 * 把进度画进 mount（同一个 mount 反复调用会就地更新，好让宽度和数字平滑变化）。
 * prog 为空 → mount 隐藏。
 */
export function renderProgress(mount, prog) {
  if (!mount) return;
  if (!prog) { mount.hidden = true; return; }
  mount.hidden = false;
  const st = mount._pg || build(mount);
  st.tip.hidden = true;                        // 数据变了，段的位置也会变：先收起悬停说明，鼠标再动一下就按新位置显示
  const pct = clamp(Number(prog.pct) || 0);
  const parts = (Array.isArray(prog.by_agent) ? prog.by_agent : []).filter(s => s && Number(s.pct) > 0);
  const seen = new Set();
  parts.forEach(s => {
    const key = String(s.agent || 'other');
    seen.add(key);
    let seg = st.segs.get(key);
    if (!seg) {
      seg = el('i', 'pg-seg');
      seg.style.width = '0%';
      st.segs.set(key, seg);
    }
    st.track.append(seg);                       // 按 by_agent 的顺序排
    seg.style.background = agentColor(s.agent);
    const tipText = `${s.label || s.agent || '其他'} 攒了 ${clamp(Number(s.pct))}%`;
    seg.dataset.tip = tipText;
    seg.setAttribute('aria-label', tipText);
    requestAnimationFrame(() => { seg.style.width = clamp(Number(s.pct)) + '%'; });
  });
  st.segs.forEach((seg, key) => {
    if (seen.has(key)) return;
    seg.style.width = '0%';
    setTimeout(() => { if (!seen.has(key) && seg.style.width === '0%') { seg.remove(); st.segs.delete(key); } }, 600);
  });
  countTo(st, pct);
  st.text.textContent = prog.text || '';
  mount.setAttribute('aria-label', `总进度 ${pct}%` + (prog.text ? ' · ' + prog.text : ''));
  mount.setAttribute('aria-valuenow', String(pct));
  if (pct >= 100 && st.last != null && st.last < 100) {
    mount.classList.remove('pg-flash');
    void mount.offsetWidth;
    mount.classList.add('pg-flash');
    setTimeout(() => mount.classList.remove('pg-flash'), 1400);
  }
  mount.classList.toggle('pg-full', pct >= 100);
  st.last = pct;
}

/** 小任务卡片上的小进度条 HTML（pct 为 null 时给空串）。 */
export function miniBarHtml(pct) {
  if (pct == null) return '';
  return `<span class="pg-mini" title="这一组做到 ${pct}%" aria-label="这一组做到 ${pct}%"><i style="width:${pct}%"></i></span><span class="pg-mini-n">${pct}%</span>`;
}
