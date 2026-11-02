// quota-view.js - 四条额度线路（2026-10-31：真数由管家定时用各家官方的查法查，见 V2/xay/quota_live.py）。
// 公司页顶栏的紧凑额度条（每条线路一个小格：名字 + 剩余% + 颜色，用完的标红，鼠标停留看重置时间），
// 以及详情栏"额度"格共用的几个小函数。数据来自快照 quota：[{line, agent, short, windows, remaining_pct, status, …}]。
import { escapeHtml } from './utils.js';

const AI_VAR = { codex: 'var(--ai-codex)', dsh: 'var(--ai-dsh)', agy: 'var(--ai-agy)' };
const STATUS_WORD = { ok: '充足', tight: '偏紧', exhausted: '用完了', unknown: '不知道' };

function toDate(ts) {
  return new Date(ts > 1e11 ? ts : ts * 1000);
}

const pad = x => String(x).padStart(2, '0');

/** 重置时间："04:11"（今天）/"明天 00:00"/"11/4 15:50"。 */
export function resetText(ts, now) {
  if (!ts) return '';
  const d = toDate(ts);
  const n = now ? toDate(now) : new Date();
  const hm = `${pad(d.getHours())}:${pad(d.getMinutes())}`;
  const day = x => `${x.getFullYear()}-${x.getMonth()}-${x.getDate()}`;
  if (day(d) === day(n)) return hm;
  const tomorrow = new Date(n.getTime() + 86400000);
  if (day(d) === day(tomorrow)) return `明天 ${hm}`;
  return `${d.getMonth() + 1}/${d.getDate()} ${hm}`;
}

/** 多久以前："刚刚"/"3 分钟前"/"2 小时前"/"10/29 19:05"。 */
export function agoText(ts, now) {
  if (!ts) return '';
  const sec = Math.max(0, (now || Date.now() / 1000) - (ts > 1e11 ? ts / 1000 : ts));
  if (sec < 90) return '刚刚';
  if (sec < 3600) return `${Math.round(sec / 60)} 分钟前`;
  if (sec < 86400) return `${Math.round(sec / 3600)} 小时前`;
  const d = toDate(ts);
  return `${d.getMonth() + 1}/${d.getDate()} ${pad(d.getHours())}:${pad(d.getMinutes())}`;
}

export function pctOf(n) {
  return Math.min(100, Math.max(0, Math.round(Number(n) || 0)));
}

/** ok | tight | exhausted | unknown（服务端已经算好；太旧的读数是 unknown）。 */
export function levelOf(q) {
  return (q && STATUS_WORD[q.status]) ? q.status : 'unknown';
}

/** "5 小时剩 99%（04:11 重置），本周剩 34%（11/4 15:50 重置）"。windows 里的剩余字段叫 remaining_pct 或 left。 */
export function windowsText(windows, now) {
  return (windows || []).map(w => {
    const left = w.remaining_pct !== undefined ? w.remaining_pct : w.left;
    const reset = w.resets_at ? `（${resetText(w.resets_at, now)} 重置）` : '';
    return `${w.name}剩 ${pctOf(left)}%${reset}`;
  }).join('，');
}

/** Gemini 各账号一行一个："OLYN Xiong（现在用的）：60%，11/2 00:26 重置"。 */
export function accountLines(accounts, now) {
  return (accounts || []).map(a => {
    const st = a.status === 'blocked' ? '停用了'
      : a.left === null || a.left === undefined ? '不知道' : `${pctOf(a.left)}%`;
    const reset = a.resets_at && a.status !== 'blocked' ? `，${resetText(a.resets_at, now)} 重置` : '';
    return `${a.name}${a.current ? '（现在用的）' : ''}：${st}${reset}`;
  });
}

/** 鼠标停留时的说明（多行纯文字，放进 title）。 */
export function quotaTip(q, now) {
  if (!q) return '';
  const name = (q.label || q.short || q.line) + (q.account ? ` · ${q.account}` : '');
  const lines = [];
  if (q.windows && q.windows.length) {
    lines.push(`${name}：${windowsText(q.windows, now)}`);
  } else {
    lines.push(`${name}：还没读到额度`);
  }
  if (q.balance) lines.push(q.balance);
  if (q.warning) lines.push(q.warning);
  if (q.accounts && q.accounts.length > 1) lines.push('各账号：' + accountLines(q.accounts, now).join('；'));
  if (q.fetched_at) lines.push(`${agoText(q.fetched_at, now)}查的${q.source_label ? `（${q.source_label}）` : ''}${q.stale ? '，很久没查到新的了' : ''}`);
  if (q.error) lines.push(`最近一次没查到：${q.error}`);
  return lines.join('\n');
}

/** 顶栏额度条：四个小格。 */
export function renderQuotaStrip(el, quota, now) {
  if (!el) return;
  const list = (quota || []).filter(q => q && q.line);
  if (!list.length) { el.innerHTML = ''; el.hidden = true; return; }
  el.hidden = false;
  el.innerHTML = list.map(q => {
    const lv = levelOf(q);
    const has = q.remaining_pct !== null && q.remaining_pct !== undefined;
    const val = has ? `${pctOf(q.remaining_pct)}%` : '—';
    const tip = quotaTip(q, now);
    const word = lv === 'unknown' && has ? '（旧读数）' : (STATUS_WORD[lv] ? `，${STATUS_WORD[lv]}` : '');
    return `<span class="q-cell q-${lv}${q.stale ? ' q-stale' : ''}" tabindex="0" data-line="${escapeHtml(q.line)}"
        title="${escapeHtml(tip)}" aria-label="${escapeHtml(`${q.short || q.line} 剩 ${val}${word}`)}">
      <i class="q-dot" style="background:${AI_VAR[q.agent] || 'var(--ai-other)'}"></i>
      <span class="q-name">${escapeHtml(q.short || q.line)}</span><b class="q-pct">${val}</b>
    </span>`;
  }).join('');
}
