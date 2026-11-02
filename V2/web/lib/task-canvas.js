// task-canvas.js - "任务画布"（2026-10-31 第三版：多条线，references/plans/arch-v10-parts.md）。
// 层级（用户 2026-10-31 纠正）：最左是大任务（对话）→ 中任务（部门 / 组，每个一个监工）→ 小任务（交给某个 AI 的一件）→ 步骤（AI 自己拆，在详情里）。
// 用户："画布可以多线，而不是一根横线到底"；"把当前所有的任务都要进行颗粒度对齐"。
// 数据全部来自共用的计划模型（plan-model.js，和公司页房间、右侧"任务拆解""计划版本"同一份）：编号、名字、颜色、状态、验收说法四处一样。
// 画法像地铁线路图 / git 分支图：
//   最左一个大标题框 = 这次的总任务
//   每个中任务是线上的一站（中任务牌子：编号 2-A、名字、做完几件、验收方式），按"先后"一列一列往右（列头写"第 n 步"）；
//   能同时做的中任务各占一条横向轨道：从前一个中任务分叉出好几条线、各走各的、在后面汇合；线是中任务的颜色，还不能开工的是虚线
//   中任务牌子右边是它的 AI 的活（卡片）：有先后的连成一串，互不相关的是并行的几条短线；有部门就在底下垫一块写着部门名的底色
//   跨中任务的依赖（这件活要等别的中任务里的某件活）用细虚线连
//   点一件 AI 的活 / 一个中任务牌子：右边滑出详情抽屉（plan-detail.js，和"任务拆解"里点一行是同一个；步骤也在里面）
// 高亮：从员工详情打开时他负责的活高亮；从"任务拆解"点中任务、"计划版本"点"在画布上看"打开时，高亮那个中任务 / 那一版动到的东西，别的稍淡。
// 能拖动、滚轮 / 触控板缩放，右下角缩放比例 +"适应屏幕""100%"，Esc 关。只画，不发请求、不写东西。连线用 SVG，样式在 /web/task-canvas.css。
import { escapeHtml } from './utils.js';
import { buildPlan, partChipHtml, checkChipHtml, whoText, itemTip, partTip, isDone, avatarHtml, wireAvatars, deptTagHtml } from './plan-model.js';
import { openDetail, closeDetail, refreshDetail, setDetailContext, isDetailOpen, detailShowing } from './plan-detail.js';


// 版面（画布上的像素，100% 时）
export const G = {
  ROOT_GAP: 80,          // 标题框到第一列
  COL_GAP: 104,          // 两列之间（分叉 / 汇合的弯在这里拐）
  HEAD_W: 212,           // 中任务牌子宽
  ITEM_W: 236,           // 一件活的卡片宽
  FORK: 22,              // 牌子右边到分叉竖线、最后一件活到汇合竖线
  LVL_GAP: 44,           // 一串活里前后两件之间
  ROW_GAP: 10,           // 并行的两件活之间
  GROUP_GAP: 14,         // 两个部门底色之间
  GROUP_HEAD: 26,        // 部门底色上面写部门名那一条
  GROUP_PAD: 8,
  LANE_GAP: 52,          // 两条轨道之间
  STEP_HEAD: 50,         // 列头"第 n 步"离最上面那条轨道多远
  PAD: 64,               // 画布四周留白
  R: 8                   // 折线拐角的圆角
};
const MIN_S = 0.15, MAX_S = 2.5;

// ---------------------------------------------------------------- 详情栏里的一行概括 + 按钮（只看员工接口的 tree）

function treeItems(tree) {
  if (tree && Array.isArray(tree.parts) && tree.parts.length) {
    return tree.parts.flatMap(p => ((p.depts || []).flatMap(d => d.items || [])).concat(p.items || []));
  }
  return ((tree && tree.groups) || []).flatMap(g => g.items || []);
}

/** 一行概括要用的数：共几件、做完几件、等你几件、他负责几件。 */
export function canvasCounts(tree) {
  const items = treeItems(tree);
  const c = (tree && tree.count) || {};
  return {
    all: Number(c.all) || items.length,
    done: Number(c.done) || items.filter(it => isDone(it.status)).length,
    needs: Number(c.needs_you) || 0,
    mine: items.filter(it => it.mine).length
  };
}

export const OPEN_ICON = '<svg viewBox="0 0 20 20" width="18" height="18" aria-hidden="true"><circle cx="3.5" cy="10" r="2.2"/><path d="M5.5 10h3.2M8.7 10c2.2 0 2.2-5 4.6-5h2.4M8.7 10c2.2 0 2.2 5 4.6 5h2.4M8.7 10h7" fill="none" stroke="currentColor" stroke-width="1.6" stroke-linecap="round"/><circle cx="17" cy="5" r="1.8"/><circle cx="17" cy="10" r="1.8"/><circle cx="17" cy="15" r="1.8"/></svg>';

/** 详情栏"大任务"块的内容：一行概括 + "展开任务画布"按钮（没有活时按钮不能点，写"还没有任务"）。 */
export function summaryHtml(tree) {
  const empty = !treeItems(tree).length;
  const c = canvasCounts(tree);
  const nParts = empty ? 0 : ((tree && tree.parts) || []).filter(p => ((p.depts || []).flatMap(d => d.items || []).concat(p.items || [])).length).length;
  const line = empty ? '' : [`小任务 ${c.all} 件`, `做完 ${c.done}`, c.needs ? `等你 ${c.needs}` : '', c.mine ? `他负责 ${c.mine} 件` : '']
    .filter(Boolean).join(' · ');
  const title = !empty && tree && tree.title ? tree.title : '';
  const small = title ? (nParts > 1 ? `${nParts} 个中任务 · ${title}` : title) : '';
  return `
    <div class="tile-head"><span class="tile-label">大任务</span>${line ? `<span class="tile-aside tc-sum">${escapeHtml(line)}</span>` : ''}</div>
    <button type="button" class="tc-open" data-act="canvas"${empty ? ' disabled' : ''}${title ? ` title="${escapeHtml(title)}"` : ''}>
      <span class="tc-open-ic">${OPEN_ICON}</span>
      <span class="tc-open-txt"><b>${empty ? '还没有任务' : '展开任务画布'}</b>${small ? `<small>${escapeHtml(small)}</small>` : ''}</span>
      ${empty ? '' : '<span class="tc-open-go" aria-hidden="true">↗</span>'}
    </button>`;
}

// ---------------------------------------------------------------- 画布要画什么（只看先后，不看尺寸；node 里能测）

/**
 * 计划模型 → 画布模型：{title, counts, mode: ''|'mine'|'focus', label, steps:[{c, n, multi, k}], parts:[CP], lanes}
 * CP = {p（中任务）, k, ccol（第几列）, lane（第几条轨道）, preds:[CP], succs:[CP], hot, dim,
 *       items:[{it, k, level, row, hot, dim}], groups:[{id, name, k, rows:[行号]}], rows:[[件…]], edges:[[前, 后]], cross:[[前件编号, 后件编号]]}
 * opts：{focus: {parts:[中任务编号], items:[活编号], label}}
 */
export function canvasModel(pm, opts = {}) {
  const fParts = new Set((opts.focus && opts.focus.parts) || []);
  const fItems = new Set((opts.focus && opts.focus.items) || []);
  const mode = fParts.size || fItems.size ? 'focus' : pm.counts.mine ? 'mine' : '';
  const shown = pm.parts.filter(p => p.items.length || !(p.loose || p.id === 'all'));     // 空的零散的、空的"全部的小任务"不画
  const ccols = [...new Set(shown.filter(p => !p.loose).map(p => p.col))].sort((a, b) => a - b);
  // 节点编号按中任务 / 小任务自己的编号（不按第几个）：刷新时同一个东西还是同一个节点（office-spec.md 三：不抖）
  const parts = shown.map(p => ({ p, k: 'p:' + (p.id === '' ? '_loose' : p.id), ccol: p.loose ? 0 : ccols.indexOf(p.col), lane: null, preds: [], succs: [] }));
  const byId = new Map(parts.map(cp => [cp.p.id, cp]));
  parts.forEach(cp => {
    cp.preds = cp.p.after.map(id => byId.get(id)).filter(a => a && !a.p.loose && a.ccol < cp.ccol);
    cp.preds.forEach(a => a.succs.push(cp));
  });

  // 轨道：一列一列往右排。接着前一个中任务走的，能直走就占它那条轨道（中间几列这条轨道上没别的）；
  // 从一处分出好几条时，第一条直走，其余往下找空着的轨道。零散的活单独一条，放在最下面
  const occ = [];
  const free = (l, a, b) => { for (let c = a; c <= b; c++) if (occ[l] && occ[l].has(c)) return false; return true; };
  const main = parts.filter(cp => !cp.p.loose).sort((a, b) => a.ccol - b.ccol || a.p.row - b.p.row);
  main.forEach(cp => {
    let lane = null;
    for (const a of cp.preds) if (free(a.lane, a.ccol + 1, cp.ccol)) { lane = a.lane; break; }
    if (lane == null) {
      const lo = cp.preds.length ? Math.min(...cp.preds.map(a => a.ccol)) + 1 : cp.ccol;
      for (let l = 0; lane == null; l++) if (free(l, lo, cp.ccol)) lane = l;
    }
    cp.lane = lane;
    (occ[lane] = occ[lane] || new Set()).add(cp.ccol);
  });
  const nLanes = occ.length;
  parts.filter(cp => cp.p.loose).forEach((cp, i) => { cp.lane = nLanes + i; (occ[cp.lane] = new Set()).add(0); });

  // 中任务里的活：按依赖分出第几层（一串里第几件）、第几行（有先后的同一行连成一串，互不相关的各占一行）
  parts.forEach(cp => {
    const items = cp.p.items;
    const inPart = new Set(items.map(it => it.id));
    const inDeps = it => it.deps.filter(d => inPart.has(d));
    const level = new Map();
    const lvl = (it, seen) => {
      if (level.has(it.id)) return level.get(it.id);
      if (seen.has(it.id)) return 0;
      seen.add(it.id);
      const ds = inDeps(it).map(d => items.find(x => x.id === d));
      const v = ds.length ? Math.max(...ds.map(d => lvl(d, seen))) + 1 : 0;
      seen.delete(it.id);
      level.set(it.id, v);
      return v;
    };
    items.forEach(it => lvl(it, new Set()));
    const groups = cp.p.depts.filter(d => d.items.length).map(d => ({ id: d.id, name: d.name, items: d.items, all: d.items.length, done: d.items.filter(x => isDone(x.status)).length }));
    if (cp.p.looseItems.length) groups.push({ id: '', name: '', items: cp.p.looseItems });
    const labeled = groups.some(g => g.id);
    const rows = [], rowMax = [], rowOf = new Map();
    groups.forEach((g, gi) => {
      g.k = `${cp.k}|g${gi}`;
      g.label = labeled && !!g.id;
      g.loose = !g.id;
      g.rows = [];
      const order = g.items.map((it, i) => ({ it, i })).sort((a, b) => level.get(a.it.id) - level.get(b.it.id) || a.i - b.i).map(x => x.it);
      order.forEach(it => {
        const L = level.get(it.id);
        const cands = inDeps(it).filter(d => rowOf.has(d)).map(d => ({ r: rowOf.get(d), l: level.get(d) }))
          .filter(c => g.rows.includes(c.r) && rowMax[c.r] < L).sort((a, b) => b.l - a.l);
        let r;
        if (cands.length) r = cands[0].r;
        else { r = rows.length; rows.push([]); g.rows.push(r); }
        rows[r].push(it);
        rowMax[r] = L;
        rowOf.set(it.id, r);
      });
    });
    cp.groups = groups;
    cp.rows = rows;
    cp.levels = items.length ? Math.max(...items.map(it => level.get(it.id))) + 1 : 0;
    cp.items = items.map(it => ({ it, k: 'i:' + it.id, level: level.get(it.id), row: rowOf.get(it.id) }));
    const used = new Set(items.flatMap(inDeps));
    cp.edges = items.flatMap(it => inDeps(it).map(d => [d, it.id]));
    cp.sources = items.filter(it => !inDeps(it).length).map(it => it.id);
    cp.sinks = items.filter(it => !used.has(it.id)).map(it => it.id);
    cp.cross = items.flatMap(it => it.deps.filter(d => !inPart.has(d) && pm.itemOf(d)).map(d => [d, it.id]));
  });

  // 高亮
  const partHasFocusItem = new Set(pm.items.filter(it => fItems.has(it.id)).map(it => it.part));
  parts.forEach(cp => {
    cp.items.forEach(x => {
      x.hot = mode === 'focus' ? fItems.has(x.it.id) || (fParts.has(cp.p.id) && !partHasFocusItem.has(cp.p.id)) : mode === 'mine' ? x.it.mine : false;
      x.dim = !!mode && !x.hot;
    });
    cp.hot = mode === 'focus' ? fParts.has(cp.p.id) || cp.items.some(x => x.hot) : mode === 'mine' ? cp.p.mine : false;
    cp.dim = !!mode && !cp.hot;
  });

  const steps = ccols.map((col, c) => {
    const ps = parts.filter(cp => !cp.p.loose && cp.ccol === c);
    const n = ps.length ? ps[0].p.n : c + 1;
    return { c, n, multi: ps.length > 1, count: ps.length, k: 's:' + n };
  });
  return {
    title: pm.title, counts: pm.counts, mode, label: (opts.focus && opts.focus.label) || '', steps, parts, lanes: occ.length,
    hasMine: mode === 'mine', model: pm
  };
}

// ---------------------------------------------------------------- 摆位置 + 连线（尺寸由 size(节点编号) 给，node 里能测）

function elbow(x1, y1, x2, x3, y3) {
  // 从 (x1,y1) 横着走到 x2，竖着走到 y3，再横着走到 x3；两个拐角带小圆角
  const dy = y3 - y1;
  if (Math.abs(dy) < 1) return `M${x1} ${y1}H${x3}`;
  const s = Math.sign(dy);
  const r = Math.max(0, Math.min(G.R, Math.abs(dy) / 2, Math.abs(x2 - x1), Math.abs(x3 - x2)));
  const sx1 = Math.sign(x2 - x1) || 1, sx2 = Math.sign(x3 - x2) || 1;
  return `M${x1} ${y1}H${x2 - sx1 * r}Q${x2} ${y1} ${x2} ${y1 + s * r}V${y3 - s * r}Q${x2} ${y3} ${x2 + sx2 * r} ${y3}H${x3}`;
}

// 平滑换道（S 形）：从 (x1,y1) 到 (x2,y2)，两头都是横着的
function sbend(x1, y1, x2, y2) {
  if (Math.abs(y2 - y1) < 1) return `M${x1} ${y1}H${x2}`;
  const m = (x2 - x1) / 2;
  return `M${x1} ${y1}C${x1 + m} ${y1} ${x2 - m} ${y2} ${x2} ${y2}`;
}

/**
 * 画布模型 + 每个节点的尺寸 → {pos: Map(节点编号 → {x, y, w, h}), wires:[{d, cls, c（颜色）}], ends:[{x, y, c}], w, h, ox, oy}。
 * 节点编号：root、s<列>（列头）、p<n>（中任务牌子）、i<n>（活）、p<n>g<n>（部门底色，尺寸由这里算）。
 */
export function canvasLayout(cm, size) {
  const pos = new Map();
  const wires = [];
  const ends = [];
  const W = k => (size(k) || { w: 0 }).w, H = k => (size(k) || { h: 0 }).h;
  const wire = (d, cls, c, extra = {}) => wires.push(Object.assign({ d, cls, c }, extra));

  // 每个中任务这一格多宽多高
  const cell = new Map();
  cm.parts.forEach(cp => {
    const rowH = cp.rows.map(row => Math.max(...row.map(it => H(cp.items.find(x => x.it === it).k))));
    let h = 0;
    cp.groups.forEach((g, gi) => {
      const inner = g.rows.reduce((t, r) => t + rowH[r], 0) + G.ROW_GAP * Math.max(0, g.rows.length - 1);
      g.h = inner + (g.label ? G.GROUP_HEAD + G.GROUP_PAD : 0);
      h += g.h + (gi ? G.GROUP_GAP : 0);
    });
    const itemsW = cp.levels ? cp.levels * G.ITEM_W + (cp.levels - 1) * G.LVL_GAP : 0;
    const w = cp.items.length ? G.HEAD_W + G.FORK * 2 + itemsW + G.FORK * 2 : G.HEAD_W;
    cell.set(cp, { w, h: Math.max(H(cp.k), h), rowsH: h, rowH, itemsW });
  });

  // 轨道高度、列宽
  const laneH = [];
  cm.parts.forEach(cp => { laneH[cp.lane] = Math.max(laneH[cp.lane] || 0, cell.get(cp).h); });
  const laneY = [];
  let top = 0;
  for (let l = 0; l < laneH.length; l++) {
    const h = laneH[l] || 0;
    laneY[l] = top + h / 2;
    top += h + G.LANE_GAP;
  }
  const colW = cm.steps.map(st => Math.max(G.HEAD_W, ...cm.parts.filter(cp => !cp.p.loose && cp.ccol === st.c).map(cp => cell.get(cp).w)));
  const rootW = W('root'), rootH = H('root');
  const colX = [];
  let x = rootW + G.ROOT_GAP;
  colW.forEach((w, c) => { colX[c] = x; x += w + G.COL_GAP; });
  if (!colX.length) colX[0] = rootW + G.ROOT_GAP;

  // 标题框：第一列只有一个中任务就和它在一条线上，否则在第一列几条轨道中间
  const first = cm.parts.filter(cp => !cp.p.loose && cp.ccol === 0);
  const rootY = first.length === 1 ? laneY[first[0].lane] : first.length ? first.reduce((t, cp) => t + laneY[cp.lane], 0) / first.length
    : (cm.parts.length ? laneY[cm.parts[0].lane] : 0);
  pos.set('root', { x: 0, y: rootY - rootH / 2, w: rootW, h: rootH });

  // 每一格：牌子在左，活在右（以轨道为中线上下排开）
  const exitX = cp => (cp.p.loose ? colX[0] + cell.get(cp).w : colX[cp.ccol] + colW[cp.ccol]);
  const itemPos = new Map();
  cm.parts.forEach(cp => {
    const c = cell.get(cp);
    const cx = colX[cp.ccol];
    const y = laneY[cp.lane];
    const hh = H(cp.k);
    pos.set(cp.k, { x: cx, y: y - hh / 2, w: G.HEAD_W, h: hh });
    const color = cp.p.color;
    const planned = cp.p.status === 'planned';
    const dimCls = cp.dim ? ' dim' : '';
    if (!cp.items.length) {
      wire(`M${cx + G.HEAD_W} ${y}H${cp.succs.length ? exitX(cp) : cx + G.HEAD_W + 14}`, `w-lane${planned ? ' w-plan' : ''}${dimCls}`, color);
      if (!cp.succs.length) ends.push({ x: cx + G.HEAD_W + 19, y, c: color, dim: cp.dim });
      return;
    }
    const x0 = cx + G.HEAD_W + G.FORK * 2;
    const busX = cx + G.HEAD_W + G.FORK;
    const mergeX = x0 + c.itemsW + G.FORK;
    let ty = y - c.rowsH / 2;
    const rowTop = [];
    cp.groups.forEach((g, gi) => {
      if (gi) ty += G.GROUP_GAP;
      const gTop = ty;
      let ry = ty + (g.label ? G.GROUP_HEAD : 0);
      g.rows.forEach((r, j) => { if (j) ry += G.ROW_GAP; rowTop[r] = ry; ry += c.rowH[r]; });
      if (g.label || (cp.groups.some(x => x.label) && g.loose)) {
        pos.set(g.k, { x: x0 - 12, y: gTop - (g.label ? 0 : 6), w: c.itemsW + 24, h: g.h + (g.label ? 0 : 12), band: true });
      }
      ty += g.h;
    });
    cp.items.forEach(x => {
      const h = H(x.k);
      const ix = x0 + x.level * (G.ITEM_W + G.LVL_GAP);
      const iy = rowTop[x.row] + (c.rowH[x.row] - h) / 2;
      pos.set(x.k, { x: ix, y: iy, w: G.ITEM_W, h });
      itemPos.set(x.it.id, { x: ix, y: iy, w: G.ITEM_W, h, cy: iy + Math.min(h, 52) / 2, cp, hot: x.hot, dim: x.dim });
    });
    // 牌子 → 分叉 → 每串的第一件；每串的最后一件 → 汇合 → 出去
    cp.sources.forEach(id => {
      const p = itemPos.get(id);
      wire(elbow(cx + G.HEAD_W, y, busX, p.x, p.cy), `w-lane${planned ? ' w-plan' : ''}${p.dim ? ' dim' : ''}`, color);
    });
    const ex = exitX(cp);
    if (cp.succs.length) {
      cp.sinks.forEach(id => {
        const p = itemPos.get(id);
        wire(elbow(p.x + p.w, p.cy, mergeX, ex, y), `w-lane${planned ? ' w-plan' : ''}${p.dim ? ' dim' : ''}`, color);
      });
    } else {
      // 后面没有别的中任务了：每一串做到最后一件就到头（各画一个终点），不再拉长线汇到一处
      cp.sinks.forEach(id => {
        const p = itemPos.get(id);
        wire(`M${p.x + p.w} ${p.cy}H${p.x + p.w + 14}`, `w-lane${planned ? ' w-plan' : ''}${p.dim ? ' dim' : ''}`, color);
        ends.push({ x: p.x + p.w + 19, y: p.cy, c: color, dim: p.dim });
      });
    }
    // 一串里前后两件
    cp.edges.forEach(([a, b]) => {
      const p = itemPos.get(a), q = itemPos.get(b);
      const d = Math.abs(p.cy - q.cy) < 1 ? `M${p.x + p.w} ${p.cy}H${q.x}` : sbend(p.x + p.w, p.cy, q.x, q.cy);
      wire(d, `w-chain${planned ? ' w-plan' : ''}${p.dim && q.dim ? ' dim' : ''}`, color);
    });

  });

  // 中任务之间的线：同一条轨道直走；分叉在前一个后面拐、汇合在后一个前面拐；都走不通就找一条空轨道绕过去
  const laneFree = (l, a, b, skip) => {
    for (const cp of cm.parts) if (cp.lane === l && cp.ccol >= a && cp.ccol <= b && !skip.includes(cp)) return false;
    return true;
  };
  const route = (A, B) => {
    const ax = A ? exitX(A) : rootW, ay = A ? laneY[A.lane] : rootY;
    const bx = colX[B.ccol], by = laneY[B.lane];
    const a = A ? A.ccol + 1 : 0, b = B.ccol - 1;
    const skip = [A, B].filter(Boolean);
    const gapA = A ? G.COL_GAP : G.ROOT_GAP;
    // 颜色：分叉出去的线是后一个中任务的颜色；几条线汇合到一处时，每条保留它自己（前一个中任务）的颜色
    const merge = A && B.preds.length > 1;
    const cc = merge ? A.p.color : B.p.color;
    if (A && A.lane === B.lane && laneFree(A.lane, a, b, skip)) return { d: `M${ax} ${ay}H${bx}`, c: cc };
    if (laneFree(B.lane, a, b, skip)) return { d: sbend(ax, ay, ax + gapA, by) + (ax + gapA < bx ? `H${bx}` : ''), c: cc };
    if (A && laneFree(A.lane, a, b, skip)) return { d: `M${ax} ${ay}H${bx - G.COL_GAP}` + sbend(bx - G.COL_GAP, ay, bx, by).replace(/^M[^C H]+ [^C H]+/, ''), c: A.p.color };
    for (let l = 0; l <= cm.lanes; l++) {
      if (l === B.lane || (A && l === A.lane) || !laneFree(l, a, b, skip)) continue;
      const ly = l < laneY.length ? laneY[l] : top + 10;
      return { d: sbend(ax, ay, ax + gapA, ly) + `H${bx - G.COL_GAP}` + sbend(bx - G.COL_GAP, ly, bx, by).replace(/^M[^C H]+ [^C H]+/, ''), c: B.p.color };
    }
    return { d: sbend(ax, ay, bx, by), c: B.p.color };
  };
  cm.parts.forEach(cp => {
    const planned = cp.p.status === 'planned';
    const froms = cp.preds.length ? cp.preds : [null];
    froms.forEach(A => {
      const r = route(A, cp);
      const dim = cp.dim && (!A || A.dim);
      wire(r.d, `w-lane${cp.p.loose ? ' w-loose' : ''}${planned ? ' w-plan' : ''}${dim ? ' dim' : ''}`, r.c);
    });
  });

  // 跨中任务的依赖：细虚线（前一件的右边 → 后一件的左边；上下叠着的从下沿到上沿）
  cm.parts.forEach(cp => cp.cross.forEach(([a, b]) => {
    const p = itemPos.get(a), q = itemPos.get(b);
    if (!p || !q) return;
    let d;
    if (q.x - (p.x + p.w) >= 30) d = `M${p.x + p.w} ${p.cy}C${p.x + p.w + 50} ${p.cy} ${q.x - 50} ${q.cy} ${q.x - 3} ${q.cy}`;
    else if (q.y > p.y + p.h) {
      const x1 = p.x + p.w * 0.72, x2 = q.x + q.w * 0.28;
      d = `M${x1} ${p.y + p.h}C${x1} ${p.y + p.h + 40} ${x2} ${q.y - 40} ${x2} ${q.y - 3}`;
    } else {
      const x1 = p.x + p.w * 0.72, x2 = q.x + q.w * 0.28;
      d = `M${x1} ${p.y}C${x1} ${p.y - 40} ${x2} ${q.y + q.h + 40} ${x2} ${q.y + q.h + 3}`;
    }
    wire(d, `w-cross${(p.dim && q.dim) ? ' dim' : ''}${p.hot || q.hot ? ' hot' : ''}`, 'var(--label-3)', { from: a, to: b });
  }));

  // 列头"第 n 步"；不同步之间一条竖向虚线（office-spec.md 三）：画在两列之间那条缝的正中，从列头一直到最下面那条轨道
  const minTop = Math.min(...[...pos.values()].map(n => n.y));
  cm.steps.forEach(st => pos.set(st.k, { x: colX[st.c], y: minTop - G.STEP_HEAD, w: W(st.k), h: H(st.k) }));
  const mainBottom = Math.max(minTop, ...cm.parts.filter(cp => !cp.p.loose).flatMap(cp => [cp.k, ...cp.items.map(x => x.k)])
    .map(k => pos.get(k)).filter(Boolean).map(n => n.y + n.h));
  const steplines = cm.steps.filter(st => st.c > 0).map(st => ({ x: colX[st.c] - G.COL_GAP / 2, y1: minTop - G.STEP_HEAD - 6, y2: mainBottom + 24, n: st.n }));

  let minX = Infinity, minY = Infinity, maxX = -Infinity, maxY = -Infinity;
  pos.forEach(n => {
    minX = Math.min(minX, n.x); minY = Math.min(minY, n.y);
    maxX = Math.max(maxX, n.x + n.w); maxY = Math.max(maxY, n.y + n.h);
  });
  ends.forEach(e => { maxX = Math.max(maxX, e.x + 10); });
  const ox = G.PAD - minX, oy = G.PAD - minY;
  return { pos, wires, ends, steplines, ox, oy, w: Math.ceil(maxX - minX + G.PAD * 2), h: Math.ceil(maxY - minY + G.PAD * 2), rootY: rootY + oy };
}

// ---------------------------------------------------------------- 画布：节点 HTML

function nodesHtml(cm) {
  const raw = [];
  const out = { push: html => raw.push({ k: (html.match(/data-k="([^"]*)"/) || [])[1], html: html.trim() }) };
  const c = cm.counts;
  const n = cm.parts.filter(cp => !cp.p.loose).length;
  out.push(`<div class="tc-node tc-root" data-k="root">
    <div class="tc-root-k">大任务</div>
    <div class="tc-root-t">${escapeHtml(cm.title)}</div>
    <div class="tc-root-c">${n > 1 ? `${n} 个中任务 · ` : ''}${c.all} 件小任务 · 做完 ${c.done}</div></div>`);
  cm.steps.forEach(st => {
    out.push(`<div class="tc-node tc-step" data-k="${st.k}"><b>第 ${st.n} 步</b>${st.multi ? `<span>${st.count} 组同时做</span>` : ''}</div>`);
  });
  cm.parts.forEach(cp => {
    const p = cp.p;
    const cls = `${cp.hot ? ' hot' : ''}${cp.dim ? ' dim' : ''}${p.loose ? ' loose' : ''}`;
    out.push(`<button type="button" class="tc-node tc-part s-${p.tone}${cls}" data-k="${cp.k}" data-part="${escapeHtml(p.id)}" style="--pc:${p.color}" title="${escapeHtml(partTip(p) + '\n点一下看这个中任务的详情')}">
      <span class="tc-part-top">${partChipHtml(p, { title: false, tip: false })}<span class="tc-part-step">${escapeHtml(p.loose ? '不排先后' : p.letter ? `第 ${p.n} 步 · ${p.letter} 线` : `第 ${p.n} 步`)}</span></span>
      <span class="tc-part-t">${escapeHtml(p.title)}</span>
      <span class="tc-part-m">${escapeHtml(p.status ? `${p.word} · 做完 ${p.done}/${p.all} 件` : `做完 ${p.done}/${p.all} 件`)}</span>
      ${checkChipHtml(p.check)}</button>`);
    cp.groups.forEach(g => {
      if (!(g.label || (cp.groups.some(x => x.label) && g.loose))) return;
      out.push(`<div class="tc-node tc-band${g.label ? '' : ' loose'}${cp.dim ? ' dim' : ''}" data-k="${g.k}" data-band="1" style="--pc:${p.color}">
        ${g.label ? `<b>${escapeHtml(g.name)}</b><span>${g.done}/${g.all}</span>` : ''}</div>`);
    });
    cp.items.forEach(x => {
      const it = x.it;
      const chk = it.check ? `<em class="tc-chk-r t-${it.check.tone}">${escapeHtml(it.check.word)}</em>` : '';
      const deps = it.deps.length ? `<span class="tc-deps" title="${escapeHtml(`要等 ${it.deps.length} 件做完`)}">等 ${it.deps.length}</span>` : '';
      out.push(`<div class="tc-node tc-item who-${it.type} s-${it.tone}${it.mine && cm.mode === 'mine' ? ' mine' : ''}${x.hot && cm.mode === 'focus' ? ' hot' : ''}${x.dim ? ' dim' : ''}" data-k="${x.k}" style="--pc:${p.color}">
        <button type="button" class="tc-item-b" data-item="${escapeHtml(it.id)}" title="${escapeHtml(itemTip(it, cm.model) + '\n点一下看详情和步骤')}">
          ${avatarHtml(it)}
          <span class="tc-item-t">${it.legacy ? '<span class="tc-legacy">旧</span>' : ''}${escapeHtml(it.title)}</span>
          <span class="tc-item-m"><b>${escapeHtml(whoText(it))}</b><i class="tc-dot t-${it.tone}" aria-hidden="true"></i><span>${escapeHtml(it.sub)}</span>${deptTagHtml(it)}${deps}${chk}</span>
          ${it.plain ? `<span class="tc-item-p">${escapeHtml(it.plain)}</span>` : ''}
          <span class="tc-item-go" aria-hidden="true">›</span></button></div>`);
    });
  });
  return raw;
}

function htmlNode(html) {
  const t = document.createElement('template');
  t.innerHTML = html;
  return t.content.firstElementChild;
}

// 节点按编号对上号：没变的原样留着（同一个节点），变了的只换那一个，没了的拿掉，新来的加上。
// 换掉的节点先站在旧位置，等摆位置时再挪（不闪）
function syncNodes(world, list) {
  const have = new Map([...world.querySelectorAll(':scope > .tc-node')].map(el => [el.dataset.k, el]));
  const keep = new Set();
  let fresh = false;
  list.forEach(({ k, html }) => {
    keep.add(k);
    const el = have.get(k);
    if (el && el._html === html) return;
    const node = htmlNode(html);
    node._html = html;
    if (el) {
      node.style.cssText = el.style.cssText;
      if (el.classList.contains('sel')) node.classList.add('sel');
      el.replaceWith(node);
    } else {
      node.classList.add('tc-new');
      world.appendChild(node);
      fresh = true;
    }
  });
  have.forEach((el, k) => { if (!keep.has(k)) el.remove(); });
  return fresh;
}

function wiresSvg(lay) {
  const paths = lay.wires.map(w => `<path class="tc-w ${w.cls}" style="--wc:${w.c}" d="${w.d}"${w.cls.includes('w-cross') ? ' marker-end="url(#tc-arrow)"' : ''}/>`);
  const caps = lay.ends.map(e => `<circle class="tc-end${e.dim ? ' dim' : ''}" style="--wc:${e.c}" cx="${e.x}" cy="${e.y}" r="5"/>`);
  const steps = (lay.steplines || []).map(l => `<line class="tc-stepline" x1="${l.x}" y1="${l.y1}" x2="${l.x}" y2="${l.y2}"><title>第 ${l.n} 步从这里开始</title></line>`);
  return `<defs><marker id="tc-arrow" viewBox="0 0 8 8" refX="6" refY="4" markerWidth="7" markerHeight="7" orient="auto-start-reverse">
    <path d="M0.5 0.8L7 4L0.5 7.2z" class="tc-arrow"/></marker></defs><g transform="translate(${lay.ox} ${lay.oy})">${steps.join('')}${paths.join('')}${caps.join('')}</g>`;
}

// 在页面上量尺寸、摆好、连线
function layoutDom(world, cm) {
  const els = new Map();
  world.querySelectorAll('.tc-node').forEach(el => els.set(el.dataset.k, el));
  const sizes = new Map();
  els.forEach((el, k) => { if (!el.dataset.band) sizes.set(k, { w: el.offsetWidth, h: el.offsetHeight }); });
  const lay = canvasLayout(cm, k => sizes.get(k));
  lay.pos.forEach((n, k) => {
    const el = els.get(k);
    if (!el) return;
    if (n.band) {
      if (el.style.width !== n.w + 'px') el.style.width = n.w + 'px';
      if (el.style.height !== n.h + 'px') el.style.height = n.h + 'px';
    }
    const t = `translate(${Math.round(n.x + lay.ox)}px, ${Math.round(n.y + lay.oy)}px)`;
    if (el.style.transform !== t) el.style.transform = t;
    el.classList.remove('tc-new');
  });
  const svg = world.querySelector('.tc-wires');
  if (svg.getAttribute('width') !== String(lay.w)) svg.setAttribute('width', lay.w);
  if (svg.getAttribute('height') !== String(lay.h)) svg.setAttribute('height', lay.h);
  const vb = `0 0 ${lay.w} ${lay.h}`;
  if (svg.getAttribute('viewBox') !== vb) svg.setAttribute('viewBox', vb);
  const wires = wiresSvg(lay);
  if (svg._html !== wires) { svg._html = wires; svg.innerHTML = wires; }
  if (world.style.width !== lay.w + 'px') world.style.width = lay.w + 'px';
  if (world.style.height !== lay.h + 'px') world.style.height = lay.h + 'px';
  const boxes = new Map([...lay.pos.entries()].map(([k, n]) => [k, { x: n.x + lay.ox, y: n.y + lay.oy, w: n.w, h: n.h }]));
  const itemBox = new Map();
  cm.parts.forEach(cp => cp.items.forEach(x => itemBox.set(x.it.id, boxes.get(x.k))));
  return { w: lay.w, h: lay.h, rootY: lay.rootY, boxes, itemBox };
}

// ---------------------------------------------------------------- 画布：打开 / 关闭 / 拖动 / 缩放 / 点开步骤

const cv = {
  el: null, stage: null, world: null, zoomOut: null, opener: null, sig: '', snap: null, tree: null, focus: null,
  s: 1, x: 0, y: 0, size: { w: 0, h: 0 }, drag: null, dragEnd: 0, opts: {}, cm: null, pm: null
};

export function isCanvasOpen() {
  return !!cv.el;
}

function clamp(v, a, b) {
  return Math.min(b, Math.max(a, v));
}

// 拖、滑、缩放都别把整张图丢到屏幕外：图的边上至少留屏幕的四成（最多 480 像素）在屏幕里
function keepInView() {
  const r = cv.stage.getBoundingClientRect();
  const { w, h } = cv.size;
  if (!w || !r.width) return;
  const lim = (v, size, view) => {
    const KEEP = Math.min(view * 0.4, 480);
    const lo = KEEP - (size - G.PAD) * cv.s;
    const hi = view - KEEP - G.PAD * cv.s;
    return lo <= hi ? clamp(v, lo, hi) : clamp(v, hi, lo);
  };
  cv.x = lim(cv.x, w, r.width);
  cv.y = lim(cv.y, h, r.height);
}

function apply() {
  if (!cv.world) return;
  keepInView();
  cv.world.style.transform = `translate(${cv.x}px, ${cv.y}px) scale(${cv.s})`;
  const g = 24 * cv.s;
  cv.stage.style.backgroundSize = `${g}px ${g}px`;
  cv.stage.style.backgroundPosition = `${cv.x}px ${cv.y}px`;
  if (cv.zoomOut) cv.zoomOut.textContent = Math.round(cv.s * 100) + '%';
}

function zoomAt(s, px, py) {
  s = clamp(s, MIN_S, MAX_S);
  cv.x = px - (px - cv.x) * s / cv.s;
  cv.y = py - (py - cv.y) * s / cv.s;
  cv.s = s;
  apply();
}

function stageCenter() {
  const r = cv.stage.getBoundingClientRect();
  return [r.width / 2, r.height / 2];
}

// 把一块区域（画布坐标）放进屏幕：留出底下图例那一条
function showBox(b, maxS = 1, minS = MIN_S) {
  const r = cv.stage.getBoundingClientRect();
  const availH = r.height - 120;
  cv.s = clamp(Math.min((r.width - 80) / b.w, availH / b.h), minS, maxS);
  cv.x = (r.width - b.w * cv.s) / 2 - b.x * cv.s;
  cv.y = (r.height - 56 - b.h * cv.s) / 2 - b.y * cv.s;
  apply();
}

function fit() {
  showBox({ x: 0, y: 0, w: cv.size.w, h: cv.size.h }, 1);
}

function unionBox(list) {
  const bs = list.filter(Boolean);
  if (!bs.length) return null;
  const x = Math.min(...bs.map(b => b.x)), y = Math.min(...bs.map(b => b.y));
  return { x, y, w: Math.max(...bs.map(b => b.x + b.w)) - x, h: Math.max(...bs.map(b => b.y + b.h)) - y };
}

// 打开时：有高亮就把高亮的那几处放进屏幕；整张图不大就整张放进来；太大就停在最左边（标题框和前几列看得见）
function home() {
  const r = cv.stage.getBoundingClientRect();
  const { w, h, boxes } = cv.size;
  if (cv.cm && cv.cm.mode === 'focus') {
    const hot = [];
    cv.cm.parts.forEach(cp => {
      if (cp.hot) hot.push(boxes.get(cp.k));
      cp.items.forEach(x => { if (x.hot) hot.push(boxes.get(x.k)); });
    });
    const b = unionBox(hot);
    if (b) { showBox({ x: b.x - 160, y: b.y - 70, w: b.w + 260, h: b.h + 120 }, 0.9, 0.3); return; }
  }
  const s = Math.min((r.width - 80) / w, (r.height - 120) / h);
  if (s >= 0.6) { fit(); return; }
  const seen = [...boxes.values()].filter(b => b.x < (r.width - 40) / 0.75);
  const b = unionBox(seen) || { x: 0, y: 0, w, h };
  cv.s = clamp(Math.min(0.85, (r.height - 130) / (b.h + 40)), 0.45, 0.85);
  cv.x = 40 - G.PAD * cv.s;
  cv.y = (r.height - 56) / 2 - (b.y + b.h / 2) * cv.s;
  apply();
}

function plan() {
  return buildPlan(cv.snap, cv.tree, { em: cv.opts.em || '' });
}

function render() {
  const pm = plan();
  cv.pm = pm;
  const cm = canvasModel(pm, { focus: cv.focus });
  cv.cm = cm;
  cv.el.classList.toggle('mode-focus', cm.mode === 'focus');
  cv.el.classList.toggle('mode-mine', cm.mode === 'mine');
  if (!cv.world.querySelector(':scope > .tc-wires')) cv.world.innerHTML = '<svg class="tc-wires" aria-hidden="true"></svg>';
  syncNodes(cv.world, nodesHtml(cm));
  wireAvatars(cv.world);
  cv.size = layoutDom(cv.world, cm);
  // 第一次摆好以后再打开"挪位置的过渡"：之后中任务 / 小任务增减、重新排版时平滑挪过去
  if (!cv.world.classList.contains('tc-ready')) requestAnimationFrame(() => { if (cv.world) cv.world.classList.add('tc-ready'); });
  markSelected(detailShowing());
  const c = cm.counts;
  const stats = cv.el.querySelector('.tc-stats');
  if (stats) {
    const who = cv.opts.name || '他';
    const nParts = cm.parts.filter(cp => !cp.p.loose).length;
    stats.innerHTML = [nParts > 1 ? `中任务 <b>${nParts}</b> 个` : '', `小任务 <b>${c.all}</b> 件`, `做完 <b>${c.done}</b>`,
      c.needs ? `等你 <b>${c.needs}</b>` : '', cm.mode === 'mine' ? `<span class="tc-me">${escapeHtml(who)} 负责 <b>${c.mine}</b> 件</span>` : '']
      .filter(Boolean).map(s => `<span class="tc-stat">${s}</span>`).join('');
  }
  const t = cv.el.querySelector('.tc-bar-title b');
  if (t) { t.textContent = cm.title; t.title = cm.title; }
  const fb = cv.el.querySelector('.tc-focus');
  if (fb) {
    let text = '';
    if (cm.mode === 'focus') {
      const hp = cm.parts.filter(cp => cp.hot).length, hi = cm.parts.reduce((t2, cp) => t2 + cp.items.filter(x => x.hot).length, 0);
      text = `${cm.label || '高亮'}：${[hp ? `${hp} 个中任务` : '', hi ? `${hi} 件小任务` : ''].filter(Boolean).join('、') || '画布上已经没有了'}`;
      if (cv.focus && cv.focus.gone && cv.focus.gone.length) text += `（${cv.focus.gone.map(g => `「${g}」`).join('')}已经撤掉，画布上没有）`;
    }
    fb.hidden = !text;
    fb.querySelector('span').textContent = text;
  }
  const legendMe = cv.el.querySelector('.tc-legend-me');
  if (legendMe) {
    legendMe.hidden = !cm.mode;
    legendMe.lastChild.textContent = cm.mode === 'mine' ? `高亮的是 ${cv.opts.name || '他'} 负责的` : '高亮的是选中的';
    const tip = cv.el.querySelector('.tc-legend-tip');
    if (tip) tip.hidden = !!cm.mode;
  }
}

// 详情抽屉开着哪件，画布上那张卡片 / 那块牌子就描一圈
function markSelected(what, reveal = false) {
  if (!cv.world) return;
  cv.world.querySelectorAll('.tc-node.sel').forEach(el => el.classList.remove('sel'));
  if (!what) return;
  const sel = what.kind === 'item' ? `.tc-item-b[data-item="${CSS.escape(what.id)}"]` : `.tc-part[data-part="${CSS.escape(what.id)}"]`;
  const hit = cv.world.querySelector(sel);
  const node = hit && hit.closest('.tc-node');
  if (!node) return;
  node.classList.add('sel');
  if (reveal) revealNode(node);
}

// 详情抽屉盖住了右边：选中的那张卡片挪到抽屉左边看得见的地方
function revealNode(node) {
  const r = cv.stage.getBoundingClientRect();
  const nb = node.getBoundingClientRect();
  const drawer = document.querySelector('.pd-drawer');
  const right = (drawer ? Math.min(r.right, drawer.getBoundingClientRect().left) : r.right) - 32;
  const left = r.left + 32, top = r.top + 32, bottom = r.bottom - 90;
  let dx = 0, dy = 0;
  if (nb.right > right) dx = right - nb.right;
  if (nb.left + dx < left) dx = left - nb.left;
  if (nb.top < top) dy = top - nb.top;
  else if (nb.bottom > bottom) dy = bottom - nb.bottom;
  if (dx || dy) { cv.x += dx; cv.y += dy; apply(); }
}

// 画布打开的详情：数据从画布这份模型取；"在画布上看"就在画布里高亮那一处
function detailCtx(opener) {
  return {
    owner: 'canvas', model: () => cv.pm, opener,
    onShow: what => markSelected(what, true),
    onCanvas: focus => { if (!cv.el) return; cv.focus = focus; cv.sig = ''; render(); home(); }
  };
}

function showDetail(what, opener) {
  openDetail(what, detailCtx(opener));
}

// 左上角切换里"看板"去哪：和公司页顶栏那个一样（app.js 按对话填好的 #back-link）
function boardHref() {
  const a = typeof document !== 'undefined' && document.getElementById('back-link');
  return (a && a.getAttribute('href')) || '/';
}

function shellHtml() {
  return `
    <header class="tc-bar">
      <div class="tc-bar-title">
        <nav class="view-switch" aria-label="切换看法">
          <a class="vs-btn" href="${escapeHtml(boardHref())}" title="看板：每名员工一列，看他的命令行">看板</a>
          <button type="button" class="vs-btn" data-tc="close" title="回到公司页（Esc）">公司页</button>
          <span class="vs-btn active" aria-current="page">任务画布</span>
        </nav><b></b></div>
      <div class="tc-focus" hidden><span></span><button type="button" data-tc="unfocus">取消高亮</button></div>
      <div class="tc-stats"></div>
      <button type="button" class="tc-close" data-tc="close" title="关闭（Esc）" aria-label="关闭任务画布">×</button>
    </header>
    <div class="tc-stage" tabindex="-1">
      <div class="tc-world"></div>
    </div>
    <div class="tc-legend" aria-label="图例">
      <span class="tc-lg-line" title="线的颜色就是中任务的颜色"><i></i>中任务的线</span><span class="tc-lg-plan"><i></i>还不能开工</span>
      <span class="tc-lg-cross"><i></i>等别的中任务里的小任务</span>
      <span class="tc-lg-sep"><i class="tc-dot t-ok"></i>做完了</span><span><i class="tc-dot t-run"></i>正在做</span><span><i class="tc-dot t-err"></i>没做成</span>
      <span><i class="tc-dot"></i>还没开始</span><span class="tc-legend-me" hidden><i></i>高亮的是他负责的</span>
      <span class="tc-legend-tip">点小任务或中任务看详情</span>
    </div>
    <div class="tc-zoom" role="group" aria-label="缩放">
      <button type="button" data-tc="out" title="缩小（-）" aria-label="缩小">−</button>
      <output class="tc-zoom-n" aria-live="polite">100%</output>
      <button type="button" data-tc="in" title="放大（+）" aria-label="放大">+</button>
      <span class="tc-zoom-sep" aria-hidden="true"></span>
      <button type="button" data-tc="fit" title="整张图放进屏幕">适应屏幕</button>
      <button type="button" data-tc="one" title="原始大小（0）">100%</button>
    </div>`;
}

function onKey(e) {
  if (!cv.el) return;
  if (isDetailOpen()) return;              // 详情抽屉开着：Esc 先关抽屉、Tab 在抽屉里走（plan-detail.js 自己管）
  if (e.key === 'Escape') {
    e.preventDefault();
    e.stopImmediatePropagation();          // 只管画布，不连带关掉详情栏
    if (cv.focus) { cv.focus = null; render(); apply(); return; }     // 有高亮：先取消高亮（office-spec.md 三）
    closeTaskCanvas();
    return;
  }
  if (e.key === 'Tab') {                   // 焦点留在画布里
    const f = [...cv.el.querySelectorAll('button')].filter(b => !b.closest('[hidden]'));
    if (!f.length) return;
    const i = f.indexOf(document.activeElement);
    if (e.shiftKey && i <= 0) { e.preventDefault(); f[f.length - 1].focus(); }
    else if (!e.shiftKey && (i === -1 || i === f.length - 1)) { e.preventDefault(); f[0].focus(); }
    return;
  }
  if (e.metaKey || e.ctrlKey || e.altKey) return;
  const [cx, cy] = stageCenter();
  const step = 80;
  const act = {
    '+': () => zoomAt(cv.s * 1.2, cx, cy), '=': () => zoomAt(cv.s * 1.2, cx, cy), '-': () => zoomAt(cv.s / 1.2, cx, cy),
    '0': () => zoomAt(1, cx, cy), ArrowLeft: () => { cv.x += step; apply(); }, ArrowRight: () => { cv.x -= step; apply(); },
    ArrowUp: () => { cv.y += step; apply(); }, ArrowDown: () => { cv.y -= step; apply(); }
  }[e.key];
  if (act) { e.preventDefault(); e.stopImmediatePropagation(); act(); }
}

function onWheel(e) {
  e.preventDefault();
  const r = cv.stage.getBoundingClientRect();
  const px = e.clientX - r.left, py = e.clientY - r.top;
  const k = e.deltaMode === 1 ? 33 : e.deltaMode === 2 ? r.height : 1;      // 按行 / 按页滚的换成像素
  const dx = e.deltaX * k, dy = e.deltaY * k;
  if (e.ctrlKey || e.metaKey) {
    zoomAt(cv.s * clamp(Math.exp(-dy * 0.01), 0.7, 1.4), px, py);         // 触控板双指捏合（浏览器报成 Ctrl + 滚轮）
  } else if (Math.abs(dx) > Math.abs(dy)) {
    cv.x -= dx; cv.y -= dy;                                               // 双指左右滑（或 Shift + 滚轮）：平移
    apply();
  } else {
    zoomAt(cv.s * clamp(Math.exp(-dy * 0.0018), 0.8, 1.25), px, py);      // 滚轮 / 双指上下滑：缩放
  }
}

// 按下不马上算拖动：挪过 4 像素才开始拖（这样点一件活能展开步骤，按住它拖也能挪画布）
function onDown(e) {
  if (e.button !== 0 || e.target.closest('[data-tc]')) return;
  cv.drag = { id: e.pointerId, x: e.clientX, y: e.clientY, ox: cv.x, oy: cv.y, on: false };
}

function onMove(e) {
  const d = cv.drag;
  if (!d || d.id !== e.pointerId) return;
  if (!d.on) {
    if (Math.hypot(e.clientX - d.x, e.clientY - d.y) < 4) return;
    d.on = true;
    cv.stage.setPointerCapture(e.pointerId);
    cv.stage.classList.add('dragging');
  }
  cv.x = d.ox + e.clientX - d.x;
  cv.y = d.oy + e.clientY - d.y;
  apply();
}

function onUp(e) {
  if (!cv.drag || cv.drag.id !== e.pointerId) return;
  if (cv.drag.on) cv.dragEnd = Date.now();
  cv.drag = null;
  cv.stage.classList.remove('dragging');
}

function onCtl(e) {
  const fresh = Date.now() - cv.dragEnd > 80;           // 刚拖完松手不算点
  const item = e.target.closest('[data-item]');
  if (item && cv.world && cv.world.contains(item)) {
    if (fresh) showDetail({ kind: 'item', id: item.dataset.item }, item);
    return;
  }
  const part = e.target.closest('[data-part]');
  if (part && cv.world && cv.world.contains(part)) {
    if (fresh) showDetail({ kind: 'part', id: part.dataset.part }, part);
    return;
  }
  const b = e.target.closest('[data-tc]');
  if (!b) return;
  const [cx, cy] = stageCenter();
  const act = b.getAttribute('data-tc');
  if (act === 'close') closeTaskCanvas();
  else if (act === 'in') zoomAt(cv.s * 1.25, cx, cy);
  else if (act === 'out') zoomAt(cv.s / 1.25, cx, cy);
  else if (act === 'one') zoomAt(1, cx, cy);
  else if (act === 'fit') fit();
  else if (act === 'unfocus') { cv.focus = null; render(); apply(); }
}

// 打开时给的东西：{snap, tree}；老写法直接给 tree（员工接口的 tree），快照从 opts.snap 拿
function sourceOf(src, opts) {
  if (src && (src.snap !== undefined || src.tree !== undefined)) return { snap: src.snap || null, tree: src.tree || null };
  return { snap: (opts && opts.snap) || null, tree: src || null };
}

function sigOf() {
  const s = cv.snap || {};
  return JSON.stringify([cv.opts.em || '', cv.focus, s.tasks, s.parts, s.depts, s.plan && s.plan.version, cv.tree]);
}

/**
 * 打开全屏的任务画布。src = {snap（快照）, tree（任务树，可以没有）}；
 * opts：{em（从员工详情打开时他的编号）, name（他的名字）, focus: {parts, items, label, gone}（要高亮的中任务 / 活）, opener（关掉后焦点回到哪）}。
 * 没有活时不打开，返回 false。
 */
export function openTaskCanvas(src, opts = {}) {
  if (typeof document === 'undefined') return false;
  const { snap, tree } = sourceOf(src, opts);
  if (!buildPlan(snap, tree).items.length) return false;
  closeTaskCanvas(false);
  cv.opts = opts;
  cv.opener = opts.opener || null;
  cv.focus = opts.focus || null;
  cv.snap = snap;
  cv.tree = tree;
  const el = document.createElement('div');
  el.className = 'tc-overlay';
  el.setAttribute('role', 'dialog');
  el.setAttribute('aria-modal', 'true');
  el.setAttribute('aria-label', '任务画布');
  el.innerHTML = shellHtml();
  document.body.appendChild(el);
  cv.el = el;
  cv.stage = el.querySelector('.tc-stage');
  cv.world = el.querySelector('.tc-world');
  cv.zoomOut = el.querySelector('.tc-zoom-n');
  cv.sig = sigOf();
  render();
  home();
  el.addEventListener('click', onCtl);
  cv.stage.addEventListener('wheel', onWheel, { passive: false });
  cv.stage.addEventListener('pointerdown', onDown);
  cv.stage.addEventListener('pointermove', onMove);
  cv.stage.addEventListener('pointerup', onUp);
  cv.stage.addEventListener('pointercancel', onUp);
  window.addEventListener('keydown', onKey, true);
  const close = el.querySelector('.tc-close');
  if (isDetailOpen()) setDetailContext(detailCtx(null));     // 从"任务拆解"的详情点"在画布上看"：抽屉交给画布接着用
  else if (close) close.focus();
  return true;
}

/**
 * 有新数据时调用：画布开着、内容变了就重画（缩放、位置和展开了哪几件不动）。src 里只给变了的那样（快照或任务树）。
 * opts.em 给了而和画布不是同一个人（比如画布是从"任务拆解"打开的），不动。
 */
export function updateTaskCanvas(src, opts = {}) {
  if (!cv.el) return;
  if (opts.em !== undefined && (opts.em || '') !== (cv.opts.em || '')) return;
  const next = src && (src.snap !== undefined || src.tree !== undefined) ? src : { tree: src };
  if (next.snap) cv.snap = next.snap;
  if (next.tree) cv.tree = next.tree;
  cv.opts = Object.assign({}, cv.opts, opts);
  const sig = sigOf();
  if (sig === cv.sig) return;
  cv.sig = sig;
  if (!plan().items.length) { closeTaskCanvas(); return; }
  render();
  apply();
  refreshDetail('canvas');
}

/** 画布现在的视图（测试用）：{s, x, y}；画布没开返回 null。 */
export function canvasView() {
  return cv.el ? { s: cv.s, x: cv.x, y: cv.y } : null;
}

/** 画布是从哪打开的：{em, focus}（app.js 用来决定要不要替它拉任务树）。 */
export function canvasState() {
  return cv.el ? { em: cv.opts.em || '', focus: cv.focus, hasTree: !!cv.tree } : null;
}

export function closeTaskCanvas(restoreFocus = true) {
  if (!cv.el) return;
  window.removeEventListener('keydown', onKey, true);
  closeDetail('canvas');
  cv.el.remove();
  cv.el = cv.stage = cv.world = cv.zoomOut = null;
  cv.drag = null;
  cv.snap = cv.tree = cv.cm = null;
  const back = cv.opener;
  cv.opener = null;
  if (restoreFocus && back && back.isConnected && back.focus) back.focus();
}
