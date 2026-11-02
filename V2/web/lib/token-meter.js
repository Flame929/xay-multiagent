// token-meter.js - 员工详情栏的"token 消耗 / 速度"方块（2026-10-31；设计：references/plans/ui-v2-employee-panel.md「总任务拆解树、token」）。
// 数据是 GET /api/v2/employee 的 person.tokens（服务端 office/employee.py person_tokens 算好，协议 1.28 真实口径）：
//   task  {used, limit, pct, partial, spent, out, fresh, cached}  这件小任务（"现在"块那件，重试的几次加起来）
//   today {同上}                                                    他今天一共
//     spent = 真正花的（新读 fresh + 写出 out）；cached = 重复读的缓存（便宜，单独说）；used / pct 照旧是对着预算的（圆环）
//   speed {out_per_s, fresh_per_s, window_s}  正在干活时最近 60 秒平均每秒写出 / 新读多少（来自工位写进直播文件的用量行）
//   minutes [30 个数] + minutes_from           最近 30 分钟每分钟花了多少（按 spent，方格，一分钟一格）
// 大字："这件小任务花了 X"（spent）；小字："写出 a、新读 b，另有 c 是重复读的缓存（便宜）"；速度："每秒写出约 N 个"。
// 圆环：外圈 = 这件小任务 / 预算，内圈 = 今天 / 每天预算。拿不到的那项不画、不写，不编数字；有哪一轮没报数的前面加"约"。
import { escapeHtml } from './utils.js';

const RINGS = [['task', 34, '这件小任务'], ['today', 23, '今天']];
const WIDTH = 7.5;

/** 12345 → "1.2万"，5518762 → "552万"，9600 → "9600"。 */
export function tokenText(n) {
  const v = Math.max(0, Number(n) || 0);
  const trim = s => s.replace(/\.0$/, '');
  if (v < 1e4) return String(Math.round(v));
  if (v < 1e8) return trim((v / 1e4).toFixed(v < 1e6 ? 1 : 0)) + '万';
  return trim((v / 1e8).toFixed(1)) + '亿';
}

function tone(pct) {
  return pct >= 90 ? 'err' : pct >= 70 ? 'warn' : '';
}

function ringSvg(tok) {
  const shown = RINGS.filter(([k]) => tok[k] && tok[k].pct !== null && tok[k].pct !== undefined);
  if (!shown.length) return '';
  const arcs = shown.map(([k, r0]) => {
    const r = shown.length === 1 ? RINGS[0][1] : r0;       // 只有一圈时画在外圈
    const c = 2 * Math.PI * r;
    const p = Math.max(0, Math.min(100, Number(tok[k].pct) || 0));
    const len = p > 0 ? Math.max(c * p / 100, 1.5) : 0;   // 用了一点点也看得见
    return `
      <circle class="tk-track" cx="40" cy="40" r="${r}" stroke-width="${WIDTH}"></circle>
      ${len ? `<circle class="tk-arc a-${k} ${tone(p)}" cx="40" cy="40" r="${r}" stroke-width="${WIDTH}" stroke-dasharray="${len.toFixed(2)} ${c.toFixed(2)}"></circle>` : ''}`;
  }).join('');
  return `<svg class="tk-ring" viewBox="0 0 80 80" aria-hidden="true"><g transform="rotate(-90 40 40)">${arcs}</g></svg>`;
}

const num = v => (v === null || v === undefined || v === '' ? null : Number(v));

// 旧记录：只有总数、没分开记新读和写出（out、fresh 都是 0 / 没有，used 却有数）
function unsplit(m) {
  return !!m && !num(m.out) && !num(m.fresh) && (num(m.used) || 0) > 0;
}

// 真正花了多少：有 spent 用 spent；旧记录分不出新读和缓存，就按全部读写算（前面加"约"）
function spentOf(m) {
  if (!m) return null;
  if (unsplit(m)) return num(m.used);
  const sp = num(m.spent);
  return sp !== null && !Number.isNaN(sp) ? sp : num(m.used);
}

// "写出 a、新读 b，另有 c 是重复读的缓存（便宜）"：哪项没有就不写
function partsLine(m) {
  if (!m) return '';
  if (unsplit(m)) return '这段是旧记录，没分开记新读和写出，按全部读写算（含便宜的缓存）';
  const out = num(m.out), fresh = num(m.fresh), cached = num(m.cached);
  const bits = [out !== null ? `写出 ${tokenText(out)}` : '', fresh !== null ? `新读 ${tokenText(fresh)}` : ''].filter(Boolean);
  const head = bits.join('、');
  const tail = cached ? `${head ? '，' : ''}另有 ${tokenText(cached)} 是重复读的缓存（便宜）` : '';
  return head + tail;
}

function bigHtml(m) {
  const v = spentOf(m);
  if (v === null || Number.isNaN(v)) return '';
  const pct = m.pct !== null && m.pct !== undefined ? Math.round(Number(m.pct) || 0) : null;
  const parts = partsLine(m);
  return `
    <div class="tk-big"><span class="tk-big-lab"><i class="tk-dot d-task" aria-hidden="true"></i>这件小任务花了</span>
      <b>${m.partial || unsplit(m) ? '<small>约</small>' : ''}${escapeHtml(tokenText(v))}</b></div>
    ${parts ? `<div class="tk-parts">${escapeHtml(parts)}</div>` : ''}
    ${m.limit ? `<div class="tk-budget">预算 ${escapeHtml(tokenText(m.limit))}${pct !== null ? `，用了 <span class="tk-pct ${tone(pct)}">${pct}%</span>` : ''}</div>` : ''}`;
}

function todayHtml(m) {
  const v = spentOf(m);
  if (v === null || Number.isNaN(v)) return '';
  const pct = m.pct !== null && m.pct !== undefined ? Math.round(Number(m.pct) || 0) : null;
  const parts = partsLine(m);
  return `
    <div class="tk-row tk-today" title="${escapeHtml(parts ? `今天：${parts}` : '今天')}"><i class="tk-dot d-today" aria-hidden="true"></i><span class="tk-lab">今天一共花了</span>
      <b>${m.partial || unsplit(m) ? '<small>约</small>' : ''}${escapeHtml(tokenText(v))}</b>
      ${m.limit ? `<span class="tk-of">/ ${escapeHtml(tokenText(m.limit))}</span>` : ''}
      ${pct !== null ? `<span class="tk-pct ${tone(pct)}">${pct}%</span>` : ''}
    </div>`;
}

function clock(ts) {
  const d = new Date(ts * 1000);
  return `${String(d.getHours()).padStart(2, '0')}:${String(d.getMinutes()).padStart(2, '0')}`;
}

function cellsHtml(tok) {
  const cells = tok.minutes;
  if (!Array.isArray(cells) || !cells.length) return '';
  const max = Math.max(...cells.map(v => Number(v) || 0));
  const from = Number(tok.minutes_from) || 0;
  return `
    <div class="tk-cells" role="img" aria-label="最近 ${cells.length} 分钟每分钟的用量">${cells.map((v, i) => {
      const n = Number(v) || 0;
      const lv = n > 0 && max > 0 ? Math.max(1, Math.ceil(4 * n / max)) : 0;
      return `<i class="lv${lv}" title="${from ? clock(from + i * 60) + ' · ' : ''}${escapeHtml(tokenText(n))}"></i>`;
    }).join('')}</div>
    <div class="tk-axis"><span>${cells.length} 分钟前</span><span>现在</span></div>`;
}

/**
 * 方块内容的 HTML；什么数都没有 → ''（调用方把方块藏起来）。
 * working：快照说他正在干活（不在干活就不写速度，方格还在）。
 */
export function tokenHtml(tok, working) {
  if (!tok || (!tok.task && !tok.today && !tok.speed && !tok.minutes)) return '';
  const sp = working && tok.speed ? tok.speed : null;
  const outPerS = sp ? num(sp.out_per_s) : null;
  const speed = outPerS !== null && !Number.isNaN(outPerS) ? sp : null;
  const cells = cellsHtml(tok);
  const ring = ringSvg(tok);
  const main = bigHtml(tok.task) + todayHtml(tok.today);
  const freshS = speed ? num(speed.fresh_per_s) : null;
  return `
    <div class="tile-head"><span class="tile-label">token 消耗</span></div>
    ${main ? `<div class="tk-use${ring ? '' : ' no-ring'}">${ring}<div class="tk-legend">${main}</div></div>` : ''}
    ${speed || cells ? `
    <div class="tk-speed">
      <div class="tk-speed-head"><span class="tile-label">速度${speed ? `<small>最近 ${Math.max(1, Math.round((Number(speed.window_s) || 60) / 60))} 分钟</small>` : ''}</span>${speed ? `<span class="tk-speed-num" title="${escapeHtml(freshS ? `另外每秒新读约 ${tokenText(freshS)} 个` : '')}"><small>每秒写出约</small><b>${escapeHtml(tokenText(outPerS))}</b><small>个</small></span>` : ''}</div>
      ${cells}
    </div>` : ''}`;
}
