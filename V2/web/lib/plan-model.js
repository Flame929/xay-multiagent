// plan-model.js - 公司页上"同一批任务"的共用计划模型（2026-10-31，references/plans/arch-v10-parts.md 最上面"最新结论"）。
// 层级：大任务（对话）→ 中任务（部门 / 组，主 AI 起名，每个一个监工；账本里叫 part）→ 小任务（交给某个 AI 的一件，账本里叫 task）→ 步骤（AI 自己拆）。
// 用户："画布和任务拆解、计划版本看上去根本不是一个任务，要把所有的任务颗粒度对齐"。
// 公司页房间（render-office.js）、任务画布（task-canvas.js）、右侧"任务拆解""计划版本"（render-plan.js）全部从这里取：
//   大任务 → 中任务（编号：第几步；同一步能同时做的写 2-A / 2-B；零散的写"零散"；颜色、状态词、状态图标、验收说法）
//         → 小任务（名字、谁做、状态词、第几步 / 共几步、监工结论、依赖；旧数据里的部门只当一个小标签）→ 步骤
// 中任务里不再分部门（部门就是中任务本身）。
// 同一个中任务在四处的编号、名字、颜色、状态图标、验收说法一模一样；同一件小任务在四处的名字、状态词、谁做也一模一样。
// 纯函数（不碰页面、不发请求），node 里能直接测。输入：快照（必有）+ 任务树（有就用：/api/v2/plan 或员工详情的 tree，补步骤明细和依赖）。
import { escapeHtml, shortTitle, progressLine, checkWord, partStatus, getAgentType, formatDateTime, spanText, isTechy } from './utils.js';

// 中任务的颜色：按画布 / 房间里的先后（第 1 步、第 2 步的 A、B…）轮着取；零散的活是灰的
export const PART_COLORS = ['#4D6BFE', '#10A37F', '#E07A5F', '#9B72CB', '#E0A100', '#0EA5E9'];
export const LOOSE_COLOR = 'var(--label-3)';
const LETTERS = 'ABCDEFGHIJKLMNOPQRSTUVWXYZ';

// 一件活的状态：颜色 + 一个词（四处一样）
const ITEM_STATUS = {
  done: ['ok', '做完了'], reported: ['ok', '做完了'], working: ['run', '正在做'], needs_you: ['warn', '等你决定'],
  failed: ['err', '没做成'], queued: ['wait', '排队中'], ready: ['wait', '等人接'], planned: ['wait', '还没开始'], canceled: ['off', '撤回了']
};
export function itemStatus(s) {
  const v = ITEM_STATUS[s] || ['wait', s ? '排队中' : '还没开始'];
  return { tone: v[0], word: v[1] };
}
export function isDone(s) {
  return s === 'done' || s === 'reported';
}

// 监工结论小标
const CHECK_TAG = { waiting: ['wait', '监工在看'], passed: ['ok', '监工通过'], failed: ['err', '监工打回'] };
export function checkTag(chk) {
  const c = chk && CHECK_TAG[chk.state];
  if (!c) return null;
  const round = Number(chk.round) || 0;
  const word = c[1] + (chk.state === 'failed' && round > 1 ? ` · 第 ${round} 轮` : '');
  const by = chk.by ? `（${chk.by}）` : '';
  return { tone: c[0], word, tip: `${word}${by}${chk.text ? `：${chk.text}` : ''}` };
}

const STEP_WORD = { done: '做完了', cur: '正在做这一步', stop: '停在这一步', todo: '还没到' };
const MAX_STEPS = 10;

// ---------------------------------------------------------------- 中任务从哪来

// 没有 parts 的快照（老接口、示例数据）、或者老管家把部门当成中任务（dept:<部门>）的：一律当成"还没拆中任务"——
// 整个对话一个中任务（id = all，名字 = 对话标题），里面的活不按部门分（用户："我要的是根据任务拆解，不是根据部门拆解！"）
export function fallbackParts(data) {
  return [{ id: 'all', title: (data && data.conversation && data.conversation.title) || '', col: 0, row: 0, after: [], status: '', check: { kind: 'rules' }, depts: [] }];
}

function isOldParts(parts) {
  return (parts || []).every(p => p.id === 'all' || p.id === '' || String(p.id).startsWith('dept:'));
}

/** 一件活 → 它在哪个中任务里（中任务编号）。快照里任务的 part 一般就是中任务编号；写的是简称（key）时按 parts[].key 对，再按部门对。 */
export function partOfTask(parts) {
  const ids = new Set((parts || []).map(p => String(p.id ?? '')));
  const byKey = new Map((parts || []).filter(p => p.key).map(p => [p.key, String(p.id)]));
  const byDept = new Map();
  (parts || []).forEach(p => (p.depts || []).forEach(d => { const id = typeof d === 'object' ? d.id : d; if (!byDept.has(id)) byDept.set(id, String(p.id)); }));
  const old = isOldParts(parts);
  const loose = ids.has('') ? '' : null;
  return t => {
    if (!t) return null;
    if (old) {
      if (ids.has('all')) return 'all';
      if (t.dept && ids.has('dept:' + t.dept)) return 'dept:' + t.dept;
      return loose;
    }
    const v = t.part == null ? '' : String(t.part);
    if (v && ids.has(v)) return v;
    if (v && byKey.has(v)) return byKey.get(v);
    if (v && t.dept && byDept.has(t.dept)) return byDept.get(t.dept);
    return loose;
  };
}

// 任务树（/api/v2/plan 或 /api/v2/employee 的 tree）里的每件活：编号 → {item, part, dept}
function treeIndex(tree) {
  const byId = new Map();
  const parts = [];
  const put = (it, part, dept) => { if (it && it.id != null && !byId.has(String(it.id))) byId.set(String(it.id), { it, part, dept }); };
  if (tree && Array.isArray(tree.parts) && tree.parts.length) {
    tree.parts.forEach(p => {
      parts.push(p);
      (p.depts || []).forEach(d => (d.items || []).forEach(it => put(it, String(p.id ?? ''), d.id || '')));
      (p.items || []).forEach(it => put(it, String(p.id ?? ''), ''));
    });
  } else if (tree && Array.isArray(tree.groups)) {
    tree.groups.filter(g => (g.items || []).length).forEach((g, i) => {
      const id = 'dept:' + (g.id || '');
      parts.push({ id, title: g.name || '零散的小任务', col: 0, row: i, status: '', check: { kind: 'rules' }, depts: [], items: g.items });
      (g.items || []).forEach(it => put(it, id, ''));
    });
  }
  return { byId, parts };
}

// ---------------------------------------------------------------- 一件活

// 没拆中任务的对话（id = all）：中任务的状态按里面的活算
function statusOf(items) {
  const live = items.filter(it => it.status !== 'canceled');
  if (!live.length) return 'ready';
  if (live.every(it => isDone(it.status))) return 'done';
  if (live.some(it => it.status === 'needs_you')) return 'needs_you';
  if (live.some(it => it.status === 'working' || it.status === 'queued')) return 'working';
  if (live.some(it => it.status === 'failed')) return 'failed';
  return 'ready';
}

// AI 开工前用【计划】写的步骤（快照 tasks[].planned：一串文字；也认一整段按行分）
function plannedOf(task) {
  const v = task && task.planned;
  const list = Array.isArray(v) ? v : typeof v === 'string' ? v.split(/\n+/) : [];
  return list.map(x => (typeof x === 'object' && x ? x.text || '' : String(x || ''))).map(x => x.replace(/^\s*(?:\d+[.、)）]|[-*•])\s*/, '').trim()).filter(Boolean);
}

function stepsFrom(task, tr) {
  // 有任务树：用它翻好的步骤（管家优先用 AI 自己拆的计划，其次任务卡写的名字 / 员工报进度时说的话）；
  // 没有任务树：有 AI 自己拆的计划就按它列，再按快照里的进度标做到哪；都没有只写编号和正在做的那一步
  if (tr && Array.isArray(tr.steps) && tr.steps.length) {
    return { step: Number(tr.step) || 0, of: Number(tr.of) || tr.steps.length, detail: true,
      steps: tr.steps.map(s => ({ n: Number(s.n) || 0, text: s.text || '', state: STEP_WORD[s.state] ? s.state : 'todo' })) };
  }
  const prog = (task && task.progress) || {};
  const step = Number(prog.step) > 0 ? Number(prog.step) : 0;
  let of = Number(prog.of) > 0 ? Number(prog.of) : 0;
  if (tr && Number(tr.of) > 0) of = Number(tr.of);
  const status = task ? task.status : (tr && tr.status);
  const planned = plannedOf(task);
  if (planned.length > of) of = planned.length;
  const steps = [];
  for (let i = 1; i <= Math.min(12, Math.max(of, step)); i++) {
    const state = isDone(status) || (step && i < step) ? 'done' : step && i === step ? (status === 'working' ? 'cur' : 'stop') : 'todo';
    steps.push({ n: i, text: planned[i - 1] || (i === step ? progressLine(prog.text || '', 40) : ''), state });
  }
  return { step: step || (tr && Number(tr.step)) || 0, of, detail: !!(tr && tr.steps), steps };
}

function subLine(it) {
  const s = it.status;
  if (isDone(s)) return it.of ? `做完了 · ${it.of} 步` : '做完了';
  if (s === 'working') return it.of ? `正在做 · 第 ${it.step || 0}/${it.of} 步` : '正在做';
  if (s === 'failed') return it.tries > 1 ? `试了 ${it.tries} 次，没做成` : '没做成';
  return it.word;
}

/** 一件活点开后列出来的东西：以前谁试过、这是第几次、它的每一步（画布和任务拆解展开的是同一份）。 */
export function stepRows(it) {
  const out = [];
  let k = 1;
  (it.runs || []).slice(0, -1).forEach(r => {
    const range = r.n > 1 ? `第 ${k}–${k + r.n - 1} 次` : `第 ${k} 次`;
    k += r.n;
    out.push({ text: `${range} · ${r.name || '没人接'}${r.status === 'failed' ? '没做成' : '没做完，换了人'}`, state: 'fail' });
  });
  if (it.tries > 1) out.push({ text: `这是第 ${it.tries} 次做`, state: 'note' });
  const steps = it.steps || [];
  if (!steps.length) {
    const s = it.status;
    out.push({
      text: it.legacy ? '旧系统派的，没分步骤' : s === 'working' ? '正在做，还没报步骤' : isDone(s) ? '做完了，没分步骤'
        : it.tone === 'wait' ? '还没开始' : '没分步骤', state: 'note'
    });
    return out;
  }
  let rows = steps.map(s => ({ text: s.text || `第 ${s.n} 步`, unnamed: !s.text, state: s.state }));
  if (rows.length > MAX_STEPS) {
    const rest = rows.length - (MAX_STEPS - 1);
    rows = rows.slice(0, MAX_STEPS - 1).concat([{ text: `还有 ${rest} 步`, state: 'note' }]);
  }
  return out.concat(rows);
}

// ---------------------------------------------------------------- 整份计划

/**
 * 快照（+ 任务树）→ 计划模型：
 * {title, version, old, counts:{all, done, working, failed, needs, mine}, steps:[{n, col, parts:[中任务编号], multi}],
 *  parts:[P], partById, items:[I], itemOf(任务编号或任意一次的编号) → I, history:[V], hasTree}
 * P = {id, key, n, letter, code, stepWord, title, col, row, status, tone, word, color, loose, check:{kind, word, tip}, after:[中任务编号],
 *      depts:[{id, name, items:[I…]}], loose_items:[I…], items:[I…], all, done, mine, raw}
 * I = {id, ids, title, full, status, tone, word, sub, owner, name, type, none, left, part, dept, deptName, step, of, steps, detail,
 *      tries, runs, legacy, check, deps:[活编号], mine}
 * opts.em：从员工详情打开时是他的编号（他负责的活 mine = true）。
 */
export function buildPlan(snap, tree, opts = {}) {
  const s = snap || {};
  const people = s.people || {};
  const former = s.former || [];
  const formerOf = k => former.find(x => x.key === k);
  const nameOf = k => (people[k] && (people[k].label || k)) || ((formerOf(k) || {}).label) || '';
  const agentOf = k => ((people[k] || formerOf(k) || {}).agent) || '';
  const tix = treeIndex(tree);
  const snapTasks = Array.isArray(s.tasks) ? s.tasks.filter(t => t && t.id != null) : [];
  const fromSnap = snapTasks.length > 0 || !tix.byId.size;

  // 中任务原始清单
  let raw;
  if (Array.isArray(s.parts) && s.parts.length) raw = s.parts;
  else if (fromSnap) raw = fallbackParts(s);
  else {
    // 只有任务树（没有快照）：树里的中任务没有 after，按 detail.after_titles 对名字；对不上就当它接在前一列的中任务后面
    raw = tix.parts.map(p => ({ id: String(p.id ?? ''), title: p.title, col: p.col, row: p.row, after: p.after || null, status: p.status,
      check: p.check, detail: p.detail, depts: (p.depts || []).map(d => d.id) }));
    raw.forEach(p => {
      if (p.after) return;
      const names = (p.detail && p.detail.after_titles) || [];
      const byName = raw.filter(x => x !== p && names.includes(x.title)).map(x => x.id);
      const prevCol = Math.max(-1, ...raw.filter(x => x.id !== '' && (Number(x.col) || 0) < (Number(p.col) || 0)).map(x => Number(x.col) || 0));
      p.after = byName.length ? byName : p.id === '' ? [] : raw.filter(x => x.id !== '' && (Number(x.col) || 0) === prevCol).map(x => x.id);
    });
  }
  raw = raw.map(p => Object.assign({}, p, { id: String(p.id ?? '') }));
  const old = isOldParts(raw);
  if (old && !(raw.length === 1 && raw[0].id === 'all')) raw = fallbackParts(s.conversation ? s : { conversation: { title: tree && tree.title } });
  const where = partOfTask(raw);
  const treePart = new Map(tix.parts.map(p => [String(p.id ?? ''), p]));
  const deptName = new Map();
  (s.depts || []).forEach(d => deptName.set(d.id, d.name || d.id));
  tix.parts.forEach(p => (p.depts || []).forEach(d => { if (!deptName.has(d.id)) deptName.set(d.id, d.name || d.id); }));

  // 一件件活：同一个中任务里标题相同、前一次没做成或撤回的，后一次算"再试一次"（和 utils.mergeAttempts、管家的 group_attempts 同一条规则）
  const allTasks = fromSnap ? snapTasks : [...tix.byId.values()].map(({ it, part, dept }) => Object.assign({}, it, { part, dept: dept || null, _tree: true }));
  const keyToId = new Map(allTasks.filter(t => t.key).map(t => [t.key, String(t.id)]));
  const partOfId = new Map();         // 任何一次的编号（含撤回的）→ 中任务编号
  const titleOfId = new Map();
  const groups = [];
  const open = new Map();
  allTasks.forEach(t => {
    const pid = old ? 'all' : t._tree ? (raw.some(p => p.id === t.part) ? t.part : where(t)) : where(t);
    const id = String(t.id);
    partOfId.set(id, pid);
    const short = (t._tree ? (t.title || '') : shortTitle(t.title)) || '任务';
    titleOfId.set(id, short);
    const key = (pid ?? '-') + '|' + (t.legacy ? 'L:' : 'V:') + short.toLowerCase();
    const g = open.get(key);
    if (g && (g.last.status === 'failed' || g.last.status === 'canceled')) {
      g.members.push(t);
      g.last = t;
    } else {
      const fresh = { members: [t], last: t, short, pid };
      groups.push(fresh);
      open.set(key, fresh);
    }
  });

  const items = [];
  const itemOfId = new Map();
  groups.forEach(g => {
    const last = g.last;
    const lastId = String(last.id);
    let tr = null;
    for (let i = g.members.length - 1; i >= 0 && !tr; i--) {
      const hit = tix.byId.get(String(g.members[i].id));
      if (hit) tr = hit.it;
    }
    const { tone, word } = itemStatus(last.status);
    const owner = last.owner || (tr && tr.owner) || '';
    const runs = [];
    g.members.forEach(m => {
      const o = m.owner || '';
      const n = (m._tree && Number(m.tries)) || 1;           // 任务树里同一个人试了几次已经合成一件（tries）
      const r = runs[runs.length - 1];
      if (r && r.owner === o) { r.n += n; r.status = m.status; } else runs.push({ owner: o, name: nameOf(o) || m.owner_name || '', n, status: m.status });
    });
    const st = stepsFrom(fromSnap ? last : null, tr || (fromSnap ? null : last));
    const pid = g.pid;
    // 部门不再是中任务里的一层（部门就是中任务本身）：旧数据里写了部门的，只当小任务卡上的一个小标签
    const hit = tix.byId.get(lastId);
    const dept = (last.dept && deptName.has(last.dept) && last.dept) || (hit && hit.dept && deptName.has(hit.dept) && hit.dept) || '';
    const it = {
      id: lastId, ids: g.members.map(m => String(m.id)), title: g.short, full: last.title || g.short, status: last.status || '', tone, word,
      owner, name: nameOf(owner) || (tr && tr.owner_name) || '', type: owner ? getAgentType(agentOf(owner)) : 'none', none: !owner,
      left: !!(owner && (formerOf(owner) || (tr && tr.former))), part: pid, dept, deptName: dept ? (deptName.get(dept) || dept) : '',
      step: st.step, of: st.of, steps: st.steps, hasSteps: st.detail, selfSplit: plannedOf(fromSnap ? last : null).length > 0 || !!(tr && tr.planned && tr.planned.length),
      tries: runs.reduce((t, r) => t + r.n, 0), runs, legacy: !!last.legacy,
      check: checkTag(last.check) || checkTag(tr && tr.check), deps: [], _rawDeps: [], mine: false
    };
    if (!it.name && it.owner) it.name = '某位员工';
    // 详情（管家给每件活加的 detail：任务卡正文、验收命令、最近一次汇报、合并回项目的文件、开工 / 做完、干了多久、为什么交给他、监工结论）；
    // 没有的就用快照里有的（为什么交给他、汇报开头）
    const det = (tr && tr.detail) || {};
    const list = v => (Array.isArray(v) ? v.filter(x => x != null && x !== '').map(String) : v ? [String(v)] : []);
    it.detail = {
      card: String(det.card || ''), verify: list(det.verify), summary: String(det.summary || (fromSnap ? last.result || '' : '') || '').trim(),
      files: list(det.files).length ? list(det.files) : list(fromSnap ? last.writes : null), filesLanded: list(det.files).length > 0,
      started: Number(det.started) || 0, ended: Number(det.ended) || 0,
      plain: String(det.plain || (fromSnap ? last.plain : '') || (tr && tr.plain) || '').trim(),
      work_s: Number(det.work_s) || 0, tries: Number(det.tries) || it.tries,
      assigned_reason: String(det.assigned_reason || (fromSnap ? last.assigned_reason : '') || ''), check: det.check || null
    };
    it.plain = it.detail.plain;                 // 员工写的"给老板看的一句话"（监工把过关）：讲"做成了什么"的地方先放它
    it.sub = subLine(it);
    it.mine = !!(opts.em && g.members.some(m => m.owner === opts.em));
    g.members.forEach(m => {
      (m.deps || []).forEach(d => it._rawDeps.push(typeof d === 'object' && d ? d.id : d));
      const h = tix.byId.get(String(m.id));
      if (h && Array.isArray(h.it.deps)) h.it.deps.forEach(d => it._rawDeps.push(d));
    });
    g.members.forEach(m => itemOfId.set(String(m.id), it));
    if (last.status !== 'canceled') items.push(it);
  });
  const alive = new Set(items);
  items.forEach(it => {
    const deps = [];
    it._rawDeps.forEach(d => {
      if (d == null) return;
      const id = keyToId.get(String(d)) || String(d);
      const dep = itemOfId.get(id);
      if (dep && dep !== it && alive.has(dep) && !deps.includes(dep.id)) deps.push(dep.id);
    });
    it.deps = deps;
    delete it._rawDeps;
  });

  // 中任务：编号、颜色、名字、状态、验收方式
  const sorted = raw.slice().sort((a, b) => (Number(a.col) || 0) - (Number(b.col) || 0) || (Number(a.row) || 0) - (Number(b.row) || 0));
  const cols = [...new Set(sorted.filter(p => p.id !== '').map(p => Number(p.col) || 0))].sort((a, b) => a - b);
  const sameCol = c => sorted.filter(p => p.id !== '' && (Number(p.col) || 0) === c);
  let ci = 0;
  const parts = sorted.map(p => {
    const loose = p.id === '';
    const col = Number(p.col) || 0;
    const sibs = loose ? [] : sameCol(col);
    const n = loose ? 0 : cols.indexOf(col) + 1;
    const letter = sibs.length > 1 ? LETTERS[sibs.indexOf(p) % 26] : '';
    const code = loose ? '零散' : letter ? `${n}-${letter}` : String(n);
    const color = loose ? LOOSE_COLOR : PART_COLORS[(ci++) % PART_COLORS.length];
    const [tone, word] = partStatus(p.status);
    const mine = items.filter(it => it.part === p.id);
    // 中任务里不再分部门（用户 2026-10-31 纠正：部门就是中任务本身）；deptIds 只留着给信件连线找监工席
    const deptIds = p.id === 'all' ? [] : (p.depts || []).map(d => (typeof d === 'object' ? d.id : d)).filter(Boolean);
    const depts = [];
    const looseItems = mine;
    const ordered = mine;
    const title = p.id === 'all' ? '全部的小任务' : (p.title || (loose ? '零散的小任务' : '中任务'));
    const status = p.status || (p.id === 'all' ? statusOf(mine) : '');
    const [tone2, word2] = p.status ? [tone, word] : partStatus(status);
    const tp = treePart.get(p.id) || {};
    const det = tp.detail || p.detail || {};
    return {
      id: p.id, key: p.key || '', raw: p, n, letter, code, loose, col, row: Number(p.row) || 0, title, color,
      status, tone: tone2, word: word2, check: checkWord(det.check || p.check, nameOf),
      detail: { note: String(det.note || p.note || ''), after_titles: Array.isArray(det.after_titles) ? det.after_titles.map(String) : [],
        started: Number(det.started) || 0, ended: Number(det.ended) || 0 },
      after: (p.after || []).map(String), depts, deptIds, looseItems, items: ordered,
      all: ordered.length, done: ordered.filter(it => isDone(it.status)).length, mine: ordered.some(it => it.mine),
      stepWord: p.id === 'all' ? '还没拆中任务：这次的小任务都在这一个里' : loose ? '零散的小任务（不排先后）' : letter ? `第 ${n} 步的 ${letter} 线（和 ${sibs.filter(x => x !== p).map(x => `${n}-${LETTERS[sibs.indexOf(x) % 26]}`).join('、')} 同时做）` : `第 ${n} 步`
    };
  });
  const partById = new Map(parts.map(p => [p.id, p]));
  parts.forEach(p => { p.after = p.after.filter(a => partById.has(a) && a !== p.id); });
  // 部门小标签：和中任务同名的（旧对话、"前端组"这个中任务里的活）不再重复写
  items.forEach(it => { const p = partById.get(it.part); it.deptTag = it.deptName && !(p && p.title === it.deptName) ? it.deptName : ''; });
  // 中任务简称 → 编号（计划版本里 part 写的是简称时）
  const partRef = id => partById.get(String(id ?? '')) || partById.get((raw.find(p => p.key && p.key === id) || {}).id);

  const steps = [];
  parts.filter(p => !p.loose).forEach(p => {
    let st = steps.find(x => x.n === p.n);
    if (!st) steps.push(st = { n: p.n, col: p.col, parts: [] });
    st.parts.push(p.id);
  });
  steps.forEach(st => { st.multi = st.parts.length > 1; });

  const counts = {
    all: items.length, done: items.filter(it => isDone(it.status)).length, working: items.filter(it => it.status === 'working').length,
    failed: items.filter(it => it.status === 'failed').length, needs: items.filter(it => it.status === 'needs_you').length,
    waiting: items.filter(it => it.tone === 'wait').length, mine: items.filter(it => it.mine).length
  };
  const model = {
    title: shortTitle((s.conversation && s.conversation.title) || (tree && tree.title) || '') || '这次的大任务', convId: (s.conversation && s.conversation.id) || '',
    version: (s.plan && s.plan.version) || 0, old, counts, steps, parts, partById, items, hasTree: tix.byId.size > 0,
    itemOf: id => itemOfId.get(String(id)) || null, partRef, nameOf
  };
  model.history = buildHistory(model, (s.plan && s.plan.history) || [], { partOfId, titleOfId, itemOfId, nameOf, keyToId });
  return model;
}

// ---------------------------------------------------------------- 计划版本：每一版改了什么，挂在哪个中任务上

function q(t) {
  return `「${t}」`;
}

function partChange(ch, title, model) {
  const b = ch.before || {}, a = ch.after || {};
  const out = [];
  if ('title' in a && a.title !== b.title) out.push(`${q(shortTitle(b.title) || title)}改名为${q(shortTitle(a.title) || '')}`);
  if ('after' in a) {
    const names = (a.after || []).map(x => { const p = model.partRef(x); return p ? `${q(p.title)}（${p.code}）` : null; }).filter(Boolean);
    out.push(names.length ? `${q(title)}改了先后：排在${names.join('、')}后面` : `${q(title)}改了先后：不用再等别的中任务`);
  }
  if ('check' in a) out.push(`${q(title)}的验收方式改成${checkWord(a.check, model.nameOf).word}`);
  if ('note' in a) out.push(`${q(title)}的说明改了`);
  return out.length ? out.join('；') : `${q(title)}改了`;
}

/** 计划历史 → [{version, by, ts, reason, summary, proposal, approved_by, changes:[{op, kind, part, partGone, partTitle, item, text}], parts, items, gone}] */
export function buildHistory(model, history, ctx) {
  const { partOfId, titleOfId, itemOfId, nameOf, keyToId } = ctx;
  return (history || []).map(h => {
    const texts = Array.isArray(h.change_texts) ? h.change_texts : [];
    const parts = new Set(), items = new Set(), gone = [];
    const changes = (h.changes || []).map((ch, i) => {
      const op = ch.op || 'change';
      if (op.startsWith('part_')) {
        const p = model.partRef(ch.part);
        const title = (p && p.title) || shortTitle(ch.title) || '中任务';
        let text;
        if (op === 'part_add') text = `新开中任务${q(shortTitle(ch.title) || title)}` + (p ? `，${p.loose ? '零散' : p.letter ? `第 ${p.n} 步（${p.code}）` : `第 ${p.n} 步`}，${p.check.word}` : '');
        else if (op === 'part_remove') text = `撤掉中任务${q(shortTitle(ch.title) || title)}`;
        else text = partChange(ch, title, model);
        if (p) parts.add(p.id); else gone.push(title);
        return { op, kind: 'part', part: p ? p.id : null, partTitle: title, item: null, text };
      }
      const tid = ch.task != null ? (keyToId.get(String(ch.task)) || String(ch.task)) : '';
      const it = itemOfId.get(tid) || null;
      const a = ch.after || {};
      let pid = partOfId.has(tid) ? partOfId.get(tid) : null;
      if ((pid == null) && a.part != null) { const p = model.partRef(a.part); pid = p ? p.id : null; }
      const p = pid != null ? model.partById.get(pid) : null;
      const name = (it && it.title) || titleOfId.get(tid) || shortTitle(ch.title || a.title) || '任务';
      const inPart = p ? `在${q(p.title)}里` : '';
      let text;
      if (op === 'add') {
        const who = a.owner ? (nameOf(a.owner) || '某位员工') : '';
        text = `${inPart}加了${q(name)}` + (who ? `，交给 ${who}` : '，还没定谁做');
      } else if (op === 'remove') {
        text = p ? `从${q(p.title)}里撤回了${q(name)}` : `撤回了${q(name)}`;
      } else if (op === 'assign') {
        const nw = a.owner ? (nameOf(a.owner) || '某位员工') : '';
        const ol = ch.before && ch.before.owner ? (nameOf(ch.before.owner) || '某位员工') : '';
        text = texts[i] || (nw && ol && nw !== ol ? `把${q(name)}从 ${ol} 改给 ${nw}` : nw ? `把${q(name)}交给 ${nw}` : `${q(name)}先不指定人`);
      } else {
        text = texts[i] || `${q(name)}的计划改了`;
      }
      if (p) parts.add(p.id);
      if (it && it.status !== 'canceled' && model.items.includes(it)) items.add(it.id); else if (op === 'remove' || !it) gone.push(name);
      return { op, kind: 'task', part: p ? p.id : null, partTitle: p ? p.title : '', item: it && model.items.includes(it) ? it.id : null, text };
    });
    return {
      version: h.version || 0, by: h.by || '', ts: h.ts || 0, reason: h.reason || '', summary: h.summary || '', proposal: h.proposal || null,
      approved_by: h.approved_by || '', changes, parts: [...parts], items: [...items], gone
    };
  });
}

// ---------------------------------------------------------------- 四处共用的小块 HTML（样式在 org.css 的 .pm-*）

const SHIELD = '<svg viewBox="0 0 16 16" width="12" height="12" aria-hidden="true"><path d="M8 1.5 13.5 3.6v4c0 3.2-2.3 5.6-5.5 6.9C4.8 13.2 2.5 10.8 2.5 7.6v-4z" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linejoin="round"/><path d="M5.6 8l1.7 1.7 3.1-3.3" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round"/></svg>';

/** 状态图标：做完绿勾、正在做蓝点、等你决定橙叹号、没做成红叉、可以开工小三角、还不能开工空圈。 */
export function statusIconHtml(tone, word = '') {
  return `<i class="pm-ic t-${escapeHtml(tone || 'wait')}"${word ? ` title="${escapeHtml(word)}"` : ''} aria-hidden="true"></i>`;
}

export function partTip(p) {
  return [`${p.code} · ${p.title}`, p.stepWord, `${p.word} · 做完 ${p.done}/${p.all} 件`, `监工：${p.check.tip}`].join('\n');
}

/** 中任务的小标签：状态图标 + 编号（中任务的颜色）+ 名字。opts.title=false 只写编号；opts.gone 撤掉了的中任务（灰、划线）。 */
export function partChipHtml(p, opts = {}) {
  if (!p) return '';
  const showTitle = opts.title !== false;
  const tip = opts.tip === false ? '' : ` title="${escapeHtml(p.gone ? `撤掉了的中任务「${p.title}」` : partTip(p))}"`;
  return `<span class="pm-chip${p.loose ? ' loose' : ''}${p.gone ? ' gone' : ''}${opts.cls ? ' ' + opts.cls : ''}" style="--pc:${p.gone ? 'var(--label-4)' : p.color}"${tip}>`
    + (p.gone ? '' : statusIconHtml(p.tone, p.word))
    + `<b>${escapeHtml(p.gone ? '撤' : p.code)}</b>${showTitle ? `<span>${escapeHtml(p.title)}</span>` : ''}</span>`;
}

/** 验收方式小标：盾牌 + "自动检查 / 按负责人的规则 / Codex 审查 / 负责人亲自审"。 */
export function checkChipHtml(check) {
  if (!check) return '';
  return `<span class="pm-check k-${escapeHtml(check.kind)}" title="${escapeHtml(`监工：${check.tip}`)}">${SHIELD}${escapeHtml(check.word)}</span>`;
}

/** 一件活的状态小标（颜色 + 词）。 */
export function itemStateHtml(it) {
  return `<span class="pm-st t-${escapeHtml(it.tone)}">${escapeHtml(it.word)}</span>`;
}

/** 一件活点开后的步骤清单（画布叶子、任务拆解的一行展开的都是它）。 */
export function stepsHtml(it) {
  return `<ol class="pm-steps who-${escapeHtml(it.type || 'other')}">${stepRows(it).map(r => {
    const tip = r.state === 'note' || r.state === 'fail' ? r.text : `${r.text}（${STEP_WORD[r.state] || ''}）`;
    return `<li class="st-${escapeHtml(r.state)}${r.unnamed ? ' unnamed' : ''}" title="${escapeHtml(tip)}"><i aria-hidden="true"></i><span>${escapeHtml(r.text)}</span></li>`;
  }).join('')}</ol>`;
}

/** "谁做"：名字（已离职的加注）；还没人接的写"还没人接"。 */
export function whoText(it) {
  if (it.none) return it.legacy ? '旧系统派的' : '还没人接';
  return it.name + (it.left ? '（已离职）' : '');
}

/** 一件活的悬停说明（画布和任务拆解一样）。 */
export function itemTip(it, model) {
  const deps = (it.deps || []).map(d => model && model.itemOf(d)).filter(Boolean);
  const partOf = d => (model && d.part !== it.part ? model.partById.get(d.part) : null);
  return [it.full && it.full !== it.title ? it.full : it.title, `谁做：${whoText(it)}${it.deptTag ? `（${it.deptTag}）` : ''}`, it.sub,
    it.plain ? `做成了：${it.plain}` : '', it.check ? it.check.tip : '',
    ...deps.map(d => `接在${partOf(d) ? `${partOf(d).code}「${partOf(d).title}」的` : ''}「${d.title}」后面`)].filter(Boolean).join('\n');
}

// ---------------------------------------------------------------- 详情（画布上点一件活 / 一个中任务、任务拆解里点一行：同一个详情）

function fact(k, v, raw = false) {
  return v ? `<div class="pd-fact"><dt>${escapeHtml(k)}</dt><dd>${raw ? v : escapeHtml(v)}</dd></div>` : '';
}

function block(title, html, cls = '') {
  return html ? `<section class="pd-block${cls ? ' ' + cls : ''}"><h4>${escapeHtml(title)}</h4>${html}</section>` : '';
}

function textHtml(t, cls = 'pd-text') {
  return t ? `<div class="${cls}">${escapeHtml(t)}</div>` : '';
}

// 汇报原文：有大白话（plain）时原文整段折进"技术细节"；没有大白话、原文里路径 / 函数名 / 命令多的，也折起来，只露一句提示
function reportHtml(d) {
  const raw = d.summary.length > 1200 ? d.summary.slice(0, 1199) + '…' : d.summary;
  if (!raw || raw === d.plain) return '';
  if (d.plain || isTechy(raw)) {
    return `<details class="pd-tech"><summary>${d.plain ? '技术细节（员工汇报原文）' : '汇报里技术细节比较多，点开看原文'}</summary>${textHtml(raw)}</details>`;
  }
  return textHtml(raw);
}

/** 旧数据里写了部门的小任务：卡片上一个小标签（部门不再是一层）。 */
export function deptTagHtml(it) {
  return it && it.deptTag ? `<span class="pm-dept" title="${escapeHtml(`部门：${it.deptTag}`)}">${escapeHtml(it.deptTag)}</span>` : '';
}

function itemLinkHtml(x, model, from) {
  const p = model.partById.get(x.part);
  const other = from && x.part !== from.part;
  return `<button type="button" class="pd-link" data-detail-item="${escapeHtml(x.id)}" title="${escapeHtml(itemTip(x, model))}">`
    + `${other && p ? partChipHtml(p, { title: false, tip: false }) : statusIconHtml(x.tone, x.word)}<span class="pd-link-t">${escapeHtml(x.title)}</span>`
    + `<small>${escapeHtml(whoText(x))} · ${escapeHtml(x.word)}</small></button>`;
}

// 监工结论：快照里的 {state, round, text, by}，或管家 detail 里一轮轮的 [{ok, text, by, round}]
function checkHistoryHtml(it) {
  const d = it.detail || {};
  const rows = [];
  const list = Array.isArray(d.check) ? d.check : d.check && typeof d.check === 'object' && (d.check.rounds || d.check.results) ? (d.check.rounds || d.check.results) : null;
  if (list) {
    list.forEach((r, i) => {
      const ok = r.ok === true || r.state === 'passed';
      const word = r.state === 'waiting' ? '监工在看' : ok ? '通过' : '打回';
      rows.push(`<li class="${r.state === 'waiting' ? 't-wait' : ok ? 't-ok' : 't-err'}"><b>第 ${Number(r.round) || i + 1} 轮 · ${word}</b>${r.by ? `<span>${escapeHtml(r.by)}</span>` : ''}${r.text ? `<p>${escapeHtml(r.text)}</p>` : ''}</li>`);
    });
  } else {
    const c = it.check || checkTag(d.check);
    if (c) rows.push(`<li class="t-${c.tone}"><b>${escapeHtml(c.word)}</b>${c.tip && c.tip !== c.word ? `<p>${escapeHtml(c.tip.replace(c.word, '').replace(/^[：:（(]/, '').replace(/[)）]$/, ''))}</p>` : ''}</li>`);
  }
  return rows.length ? `<ul class="pd-checks">${rows.join('')}</ul>` : '';
}

/** 一件 AI 的活的详情（右侧抽屉里的内容）。没有的字段不出现。 */
export function itemDetailHtml(it, model) {
  const p = model.partById.get(it.part);
  const d = it.detail || {};
  const deps = it.deps.map(id => model.itemOf(id)).filter(Boolean);
  const next = model.items.filter(x => x.deps.includes(it.id));
  const dur = d.work_s ? spanText(d.work_s) : d.started && d.ended && d.ended > d.started ? spanText(d.ended - d.started) : '';
  const tries = Math.max(Number(d.tries) || 0, it.tries || 1);
  const stepLine = it.of ? `第 ${it.step || (isDone(it.status) ? it.of : 0)} 步 / 共 ${it.of} 步` : '';
  // 任务卡是写给 AI 的原文（路径、命令多），默认折起来，点开才看
  const card = d.card ? `<details class="pd-tech pd-cardbox"><summary>写给 AI 的原文（${d.card.length} 字），点开看</summary>${textHtml(d.card, 'pd-text pd-card')}</details>` : '';
  const files = d.files.length ? `<div class="pd-files">${d.files.slice(0, 30).map(f => `<code>${escapeHtml(f)}</code>`).join('')}${d.files.length > 30 ? `<span>还有 ${d.files.length - 30} 个</span>` : ''}</div>` : '';
  return `
    <header class="pd-head" style="--pc:${p ? p.color : 'var(--label-3)'}">
      <div class="pd-kicker">${p ? partChipHtml(p) : ''}<span>小任务</span></div>
      <h3>${it.legacy ? '<span class="tc-legacy">旧</span>' : ''}${escapeHtml(it.title)}</h3>
      <div class="pd-sub"><span class="pd-who who-${escapeHtml(it.type)}"><i aria-hidden="true"></i>${escapeHtml(whoText(it))}</span>${itemStateHtml(it)}${it.check ? `<span class="pm-st t-${it.check.tone}">${escapeHtml(it.check.word)}</span>` : ''}${deptTagHtml(it)}</div>
    </header>
    <dl class="pd-facts">
      ${fact('做到哪', it.sub)}${fact('第几步', it.status === 'working' || !it.of ? '' : stepLine)}${fact('第几次', tries > 1 ? `第 ${tries} 次做` : '')}
      ${fact('开工', d.started ? formatDateTime(d.started) : '')}${fact('做完', d.ended ? formatDateTime(d.ended) : '')}${fact('干了多久', dur)}
      ${fact('为什么交给他', d.assigned_reason)}
    </dl>
    ${block('做成了什么', textHtml(d.plain, 'pd-text pd-plain'))}
    ${block('监工结论', checkHistoryHtml(it))}
    ${block(it.selfSplit ? '他自己拆的步骤' : '步骤', stepsHtml(it))}
    ${block('任务卡', card)}
    ${block('验收命令', d.verify.length ? `<ul class="pd-cmds">${d.verify.map(v => `<li><code>${escapeHtml(v)}</code></li>`).join('')}</ul>` : '')}
    ${block('最近一次汇报', reportHtml(d))}
    ${block(d.filesLanded ? '合并回项目的文件' : '要改的文件', files)}
    ${block('要先等这几件做完', deps.length ? `<div class="pd-links">${deps.map(x => itemLinkHtml(x, model, it)).join('')}</div>` : '')}
    ${block('做完后接着做', next.length ? `<div class="pd-links">${next.map(x => itemLinkHtml(x, model, it)).join('')}</div>` : '')}
    <div class="pd-actions">
      <button type="button" class="pd-btn" data-detail-act="canvas" data-part="${escapeHtml(it.part ?? '')}" data-item="${escapeHtml(it.id)}">在画布上看这件小任务</button>
      ${p ? `<button type="button" class="pd-btn ghost" data-detail-part="${escapeHtml(p.id)}">看它所在的中任务</button>` : ''}
    </div>`;
}

/** 一个中任务的详情：说明、验收方式、排在哪些中任务后面、开始 / 结束、里面的活。 */
export function partDetailHtml(p, model) {
  const d = p.detail || {};
  const after = p.after.map(id => model.partById.get(id)).filter(Boolean);
  const afterHtml = after.length
    ? after.map(a => `<button type="button" class="pd-chiplink" data-detail-part="${escapeHtml(a.id)}">${partChipHtml(a)}</button>`).join('')
    : d.after_titles.length ? escapeHtml(d.after_titles.map(t => `「${t}」`).join('、')) : '';
  const nextParts = model.parts.filter(x => x.after.includes(p.id));
  const nextHtml = nextParts.map(a => `<button type="button" class="pd-chiplink" data-detail-part="${escapeHtml(a.id)}">${partChipHtml(a)}</button>`).join('');
  const groups = p.depts.filter(g => g.items.length).map(g => ({ name: g.name, items: g.items }));
  if (p.looseItems.length) groups.push({ name: groups.length ? '没进部门的' : '', items: p.looseItems });
  const itemsHtml = groups.map(g => `${g.name ? `<div class="pd-group">${escapeHtml(g.name)}</div>` : ''}<div class="pd-links">${g.items.map(x => itemLinkHtml(x, model, null)).join('')}</div>`).join('');
  return `
    <header class="pd-head" style="--pc:${p.color}">
      <div class="pd-kicker">${partChipHtml(p, { title: false })}<span>${escapeHtml(p.loose ? '零散的小任务' : '中任务')}</span></div>
      <h3>${escapeHtml(p.title)}</h3>
      <div class="pd-sub"><span class="pm-st t-${escapeHtml(p.tone === 'ready' ? 'wait' : p.tone)}">${escapeHtml(p.word)}</span><span class="pd-count">做完 ${p.done}/${p.all} 件</span>${checkChipHtml(p.check)}</div>
    </header>
    <dl class="pd-facts">
      ${fact('排在第几', p.stepWord)}${fact('验收方式', p.check.tip)}
      ${fact('排在这些后面', afterHtml, true)}${fact('后面接着', nextHtml, true)}
      ${fact('开始', d.started ? formatDateTime(d.started) : '')}${fact('结束', d.ended ? formatDateTime(d.ended) : '')}
      ${fact('用了多久', d.started && d.ended && d.ended > d.started ? spanText(d.ended - d.started) : '')}
    </dl>
    ${block('说明', textHtml(d.note))}
    ${block(`里面的小任务（${p.all} 件）`, itemsHtml)}
    <div class="pd-actions"><button type="button" class="pd-btn" data-detail-act="canvas" data-part="${escapeHtml(p.id)}">在画布上看这个中任务</button></div>`;
}

// 小头像（画布卡片、任务拆解的一行用同一个）：角色头像图，没有图时是名字的第一个字；还没人接的是问号
const AVATAR = { codex: 'codex', dsh: 'dsh', agy: 'agy', gm: 'claude' };
export function avatarHtml(it) {
  if (!it || it.none) return '<span class="tc-ava none" aria-hidden="true">?</span>';
  const a = AVATAR[it.type];
  const initial = escapeHtml(String(it.name || '?').trim().slice(0, 1).toUpperCase());
  return `<span class="tc-ava who-${escapeHtml(it.type || 'other')}" aria-hidden="true">${initial}${a ? `<img src="/xay-chars/img/${a}-calm.webp" alt="" decoding="async">` : ''}</span>`;
}

/** 刚放进页面的小头像：图读到了才显出来，读不到就留名字的第一个字。 */
export function wireAvatars(root) {
  if (!root || !root.querySelectorAll) return;
  root.querySelectorAll('.tc-ava img:not([data-w])').forEach(img => {
    img.setAttribute('data-w', '1');
    if (img.complete && img.naturalWidth) img.classList.add('ready');
    img.addEventListener('load', () => img.classList.add('ready'));
    img.addEventListener('error', () => img.remove());
  });
}
