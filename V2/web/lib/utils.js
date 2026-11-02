// utils.js - 组织架构页面通用基础工具
export function escapeHtml(str) {
  if (str === null || str === undefined) return '';
  return String(str)
    .replace(/&/g, '&amp;')
    .replace(/</g, '&lt;')
    .replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;');
}

export function formatTime(ts) {
  if (!ts) return '';
  const d = new Date(ts > 1e11 ? ts : ts * 1000);
  const pad = n => String(n).padStart(2, '0');
  return `${pad(d.getHours())}:${pad(d.getMinutes())}:${pad(d.getSeconds())}`;
}

// "10-30 16:25"：月-日 时:分（面板里每件事的开始时间）
export function formatDateTime(ts) {
  if (!ts) return '';
  const d = new Date(ts > 1e11 ? ts : ts * 1000);
  const pad = n => String(n).padStart(2, '0');
  return `${pad(d.getMonth() + 1)}-${pad(d.getDate())} ${pad(d.getHours())}:${pad(d.getMinutes())}`;
}

// 两个时间点之间用了多久："用了 12 分钟"；不到 5 秒或算不出来返回空串
export function formatDuration(start, end) {
  if (!start || !end || end < start) return '';
  const sec = Math.round((end > 1e11 ? end / 1000 : end) - (start > 1e11 ? start / 1000 : start));
  if (sec < 5) return '';
  if (sec < 90) return `用了 ${sec} 秒`;
  const min = Math.round(sec / 60);
  if (min < 90) return `用了 ${min} 分钟`;
  const hours = Math.round(sec / 360) / 10;
  return `用了 ${hours} 小时`;
}

export function getAgentType(agent) {
  const a = (agent || '').toLowerCase();
  if (a.includes('codex')) return 'codex';
  if (a.includes('dsh') || a.includes('deepseek')) return 'dsh';
  if (a.includes('agy') || a.includes('antigravity') || a.includes('gemini')) return 'agy';
  if (a.includes('claude') || a.includes('gm')) return 'gm';
  return 'other';
}

export const AI_NAMES = {
  codex: 'Codex',
  dsh: 'DeepSeek',
  agy: 'Gemini',
  gm: 'Claude',
  other: 'AI'
};

export function getStateLabel(state) {
  switch (state) {
    case 'working': return '工作中';
    case 'sleeping': return '休眠中';
    case 'blocked': return '已阻塞';
    case 'failed': return '上一轮失败';
    case 'waiting': return '等待中';
    case 'queued': return '排队中';
    case 'compacting': return '整理记忆中';
    case 'offline': return '已离线';
    case 'fired': return '已离职';
    default: return '空闲';
  }
}

// whoIs：解析发送方/接收方为展示信息
// target: 'gm' | 'user' | 'fe' (部门id) | 'em_...' (员工key) | { dept, worker }
export function whoIs(target, people = {}, depts = []) {
  if (!target || target === 'gm' || (target && target.dept === 'gm')) {
    return { key: 'gm', type: 'gm', label: '负责人' };
  }
  if (target === 'user') {
    return { key: 'user', type: 'user', label: '你' };
  }
  // 后台按规则自动写的信、下的指令（M4：失败原因发回去、文件重叠提醒、预算到了收尾…）：页面上一律是"监工"（形象用 XayChars 的 'rules'）
  if (target === 'office' || target === 'rules') {
    return { key: 'office', type: 'rules', label: '监工' };
  }
  if (typeof target === 'string') {
    // 检查是否为部门 ID
    const d = (depts || []).find(x => x.id === target);
    if (d) {
      return { key: 'dept:' + target, type: 'dept', label: d.name || target };
    }
    // 检查是否为员工 key (如 em_fe_01)
    if (people && people[target]) {
      const p = people[target];
      return {
        key: target,
        type: getAgentType(p.agent),
        label: p.label || p.key || '员工'
      };
    }
    return { key: 'x:' + target, type: 'other', label: target };
  }
  if (typeof target === 'object') {
    if (target.key && people && people[target.key]) {
      const p = people[target.key];
      return { key: target.key, type: getAgentType(p.agent), label: p.label || '员工' };
    }
    if (target.worker) {
      // 查找对应人员
      const found = Object.values(people || {}).find(p => p.label === target.worker || p.key === target.worker);
      if (found) {
        return { key: found.key, type: getAgentType(found.agent), label: found.label };
      }
      return { key: 'w:' + target.worker, type: getAgentType(target.agent), label: target.worker };
    }
  }
  return { key: 'unknown', type: 'other', label: '某人' };
}

export function hasChars() {
  return !!(window.XayChars && window.XayChars.render);
}

export function avatarHtml(type, size = 28, state = 'idle') {
  if (hasChars() && type !== 'dept' && type !== 'other' && type !== 'user') {
    return window.XayChars.render(type === 'gm' ? 'claude' : type, {
      state: state || 'idle',
      size: size || 28
    });
  }
  return `<span class="avatar-dot tag-${type}" style="width:${size}px;height:${size}px;"></span>`;
}

export function reducedMotion() {
  return !!(window.matchMedia && window.matchMedia('(prefers-reduced-motion: reduce)').matches);
}

// 时间点：今天的写 "19:05"，别的日子写 "10-29 19:05"（详情栏"做过的事"每一行右边）
export function formatClock(ts, now) {
  if (!ts) return '';
  const d = new Date(ts > 1e11 ? ts : ts * 1000);
  const n = now ? new Date(now > 1e11 ? now : now * 1000) : new Date();
  const pad = x => String(x).padStart(2, '0');
  const hm = `${pad(d.getHours())}:${pad(d.getMinutes())}`;
  const sameDay = d.getFullYear() === n.getFullYear() && d.getMonth() === n.getMonth() && d.getDate() === n.getDate();
  return sameDay ? hm : `${pad(d.getMonth() + 1)}-${pad(d.getDate())} ${hm}`;
}

// 给人看的短标题（office/plain.py 的 short_title 的简化版，工位小清单用：快照里只有原标题）：
// 只留第一句正事，去掉"汇报时……/不要……"这类写给 AI 的要求、括号里的补充、【进度】标记、反引号、路径前面的目录。
const TITLE_INSTRUCTION = /^\s*(?:汇报|回报|回复|报告|完成后|做完后|做完以后|最后|注意|要求|请(?:你|在|务必)|记得|另外|别忘|输出|结尾|开头|第一句|顺便|不要|不许|禁止|只改|只许|允许|务必|必须)/;
function dropParens(text) {
  let t = text;
  for (let i = 0; i < 4; i++) {
    const next = t.replace(/（[^（）]*）|\([^()]*\)/g, '');
    if (next === t) break;
    t = next;
  }
  return t;
}
export function shortTitle(title, limit = 60) {
  const t = String(title || '')
    .replace(/【[^】]{0,12}】/g, '')
    .replace(/`/g, '')
    .replace(/(?:[\w.@+~-]+\/)+([\w@+~-]+\.[A-Za-z]\w{0,7})\b/g, '$1');
  const cut = s => (s.length <= limit ? s : s.slice(0, limit - 1).trimEnd() + '…');
  const sents = t.split(/(?<=[。！？!?])|\n+/).map(s => s.trim()).filter(Boolean);
  for (const s of sents) {
    if (TITLE_INSTRUCTION.test(s)) continue;
    const kept = [];
    for (const clause of s.split(/(?<=[，,；;])/)) {
      if (TITLE_INSTRUCTION.test(clause)) break;
      kept.push(clause);
    }
    const core = dropParens(kept.join('')).replace(/\s+/g, ' ').replace(/[。.！!？?；;，,：:\s]+$/, '').trim();
    if (core) return cut(core);
  }
  return cut(dropParens(t).replace(/\s+/g, ' ').trim());
}

// 进度那句话（office/plain.py 的 progress_text 的简化版）：去掉【进度 3/4】、反引号、开头重复的"第 2 步："，只留第一句。
// 快照里 progress.text 是员工的原话，工位卡片上的"第几步"那一行可以用它（详情栏用接口里翻好的）。
export function progressLine(text, limit = 50) {
  const t = String(text || '')
    .replace(/【[^】]{0,12}】/g, '')
    .replace(/`/g, '')
    .replace(/(?:[\w.@+~-]+\/)+([\w@+~-]+\.[A-Za-z]\w{0,7})\b/g, '$1')
    .replace(/^\s*第\s*\d+\s*步(?:\s*\/\s*共\s*\d+\s*步)?\s*(?:已完成|完成了|做完了|已做完)?\s*[：:，,、]?\s*/, '')
    .trim();
  const first = (t.split(/(?<=[。！？!?])|\n+/).map(s => s.trim()).find(Boolean) || '').replace(/[。.；;，,\s]+$/, '');
  return first.length <= limit ? first : first.slice(0, limit - 1).trimEnd() + '…';
}

// 同一件事试了几次合成一件（和 office/employee.py 的 group_attempts 同一条规则）：
// 按派活先后看，标题相同、而且前一次没做成或被撤回（failed / canceled）的，后一次算"再试一次"；前一次做成了的，后面同名的是新活。
// 返回 [{id, title, short, status, ids, tries}]，status / id 取最后一次。
const RETRY_AFTER = new Set(['failed', 'canceled']);
export function mergeAttempts(tasks) {
  const groups = [];
  const open = new Map();
  (tasks || []).forEach(t => {
    const short = shortTitle(t.title) || '任务';
    const key = (t.legacy ? 'L:' : 'V:') + short.toLowerCase();
    const g = open.get(key);
    if (g && RETRY_AFTER.has(g.last.status)) {
      g.members.push(t);
      g.last = t;
    } else {
      const fresh = { members: [t], last: t, short };
      groups.push(fresh);
      open.set(key, fresh);
    }
  });
  return groups.map(g => ({
    id: g.last.id, title: g.last.title || '', short: g.short, status: g.last.status,
    ids: g.members.map(m => m.id), tries: g.members.length
  }));
}

// ---------- 中任务（2026-10-31，references/plans/arch-v10-parts.md：大任务 → 中任务 → 小任务 → 步骤）：任务画布和公司页共用 ----------
const REVIEWER_WORD = { codex: 'Codex', dsh: 'DeepSeek', agy: 'Gemini', claude: 'Claude', gm: '负责人', lead: '负责人' };

/**
 * 中任务用什么验收（监工怎么验）→ {kind: rules|ai|lead, word, tip}。word 是给人看的四种说法：
 * 自动检查 / 按负责人的规则 / Codex 审查（审查员是谁就写谁）/ 负责人亲自审。nameOf(员工编号) → 名字（审查员是某个员工时用）。
 */
export function checkWord(check, nameOf) {
  const c = check || {};
  const kind = c.kind === 'ai' || c.kind === 'lead' ? c.kind : 'rules';
  const text = String(c.text || '').trim();
  const list = v => (Array.isArray(v) ? v.filter(Boolean) : []);
  let word, tip;
  if (kind === 'ai') {
    const r = String(c.reviewer || '');
    const who = c.reviewer_name || REVIEWER_WORD[r.toLowerCase()] || (r && nameOf ? nameOf(r) : '') || '别的 AI';
    word = `${who} 审查`;
    tip = `小任务做完后交给 ${who} 审查`;
  } else if (kind === 'lead') {
    word = '负责人亲自审';
    tip = '小任务做完后由负责人亲自审';
  } else if (list(c.commands).length || list(c.must_change).length || list(c.must_not_change).length || text) {
    word = '按负责人的规则';
    tip = '按负责人写的验收规则逐条检查' + (list(c.commands).length ? `（要跑：${list(c.commands).join('；')}）` : '');
  } else {
    word = '自动检查';
    tip = '自动检查：任务卡里的验收命令跑通、交上来的东西对得上';
  }
  if (text) tip += `。看什么：${text}`;
  return { kind, word, tip };
}

// 中任务的状态 → [颜色, 一个词]
const PART_STATUS = {
  planned: ['wait', '还不能开工'], ready: ['ready', '可以开工'], working: ['run', '正在做'],
  needs_you: ['warn', '等你决定'], done: ['ok', '做完了'], failed: ['err', '有小任务没做成']
};
export function partStatus(s) {
  return PART_STATUS[s] || ['wait', '还没开始'];
}

// ---------- 时长（2026-10-31：在岗时间、干了多久）----------
/** 秒数 → "48 分" / "2 小时 10 分" / "不到 1 分"；算不出来返回空串。 */
export function spanText(sec) {
  const s = Number(sec);
  if (!isFinite(s) || s < 0) return '';
  if (s < 60) return '不到 1 分';
  const min = Math.floor(s / 60);
  if (min < 60) return `${min} 分`;
  const h = Math.floor(min / 60), m = min % 60;
  if (h < 48) return m ? `${h} 小时 ${m} 分` : `${h} 小时`;
  const d = Math.floor(h / 24), hh = h % 24;
  return hh ? `${d} 天 ${hh} 小时` : `${d} 天`;
}

/**
 * 员工在岗时间（快照 people[员工].duty / 员工接口 person.duty：{online_since, today_s, today_work_s, total_work_s, hired_at}）
 * → "在岗 2 小时 10 分 · 今天干活 48 分"；下班了（online_since 为空）写"已下班 · 今天干活 48 分"。没有 duty 返回空串。
 */
export function dutyText(duty, now) {
  if (!duty || typeof duty !== 'object') return '';
  const n = Number(now) > 0 ? (now > 1e11 ? now / 1000 : Number(now)) : Date.now() / 1000;
  const since = Number(duty.online_since) || 0;
  const on = since ? `在岗 ${spanText(Math.max(0, n - (since > 1e11 ? since / 1000 : since)))}` : '已下班';
  const work = Number(duty.today_work_s);
  const today = isFinite(work) && work > 0 ? `今天干活 ${spanText(work)}` : (duty.today_work_s != null ? '今天还没干活' : '');
  return [on, today].filter(Boolean).join(' · ');
}

// ---------- 大白话（2026-10-31，用户："页面上的内容都要通俗易懂"）----------
const TECH = [
  /(?:^|[\s(（"'`])(?:\.{0,2}\/)?[\w.-]+\/[\w.\/-]+/,        // 路径 a/b/c.js
  /\b[\w-]+\.(?:py|js|ts|tsx|jsx|css|html|md|json|ya?ml|sh|toml|sql|go|rs|java)\b/i,   // 文件名
  /\b[A-Za-z_]\w*\(\)/,                                  // 函数名 foo()
  /`[^`]+`/,                                              // 反引号里的代码
  /(?:^|\s)--?[a-z][\w-]+/,                              // 命令行参数 --flag
  /\b[a-z]+_[a-z_]+\b/,                                  // snake_case
  /\b[a-z]+[A-Z]\w+\b/,                                  // camelCase
  /\b(?:npm|npx|pytest|python3?|node|git|pip|curl|cd|xay2?)\s/,  // 命令
  /[{}<>]|=>|::|&&/                                       // 代码符号
];

/** 一段话里像路径、函数名、命令、代码的地方有几处（0 = 大白话）。 */
export function techScore(text) {
  const t = String(text || '');
  return TECH.reduce((n, re) => n + (re.test(t) ? 1 : 0), 0);
}

/** 这句话主要是英文（中文字很少、英文字母很多）。 */
export function mostlyEnglish(text) {
  const t = String(text || '');
  const cjk = (t.match(/[\u4e00-\u9fff]/g) || []).length;
  const latin = (t.match(/[A-Za-z]/g) || []).length;
  return latin >= 12 && cjk * 3 < latin;
}

/** 汇报这类话：技术细节多（≥ 2 处像路径 / 函数 / 命令）就算"技术原文"，页面上折进"技术细节"。 */
export function isTechy(text) {
  return techScore(text) >= 2 || mostlyEnglish(text);
}

/** 员工"在想什么"：英文或像代码 → "正在琢磨怎么做"（原文放悬停）；否则原样。返回 {text, raw, swapped}。 */
export function thoughtWords(text) {
  const raw = String(text || '').trim();
  if (!raw) return { text: '', raw: '', swapped: false };
  const swap = mostlyEnglish(raw) || techScore(raw) >= 2;
  return { text: swap ? '正在琢磨怎么做' : raw, raw, swapped: swap };
}
