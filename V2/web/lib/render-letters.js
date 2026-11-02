// render-letters.js - 渲染右侧信件栏（聊天流、回复嵌套、气泡状态、高亮联动）
import { escapeHtml, formatTime, whoIs, avatarHtml } from './utils.js';
import { letterNodes } from './render-office.js';

const LETTER_KINDS = {
  request: ['需求', 'k-request'],
  decision: ['结论', 'k-decision'],
  escalation: ['上报', 'k-escalation'],
  tell: ['留言', 'k-tell'],
  reply: ['回信', 'k-tell'],
  report: ['汇报', 'k-report'],
  auto: ['系统', 'k-report'],
  system: ['监工来信', 'k-report'],     // 监工按规则自动写的（没做成请接着修…），和画布上飞信落地的字一样
  notice: ['通知', 'k-tell']
};

function letterFoot(m, answered) {
  const parts = [];
  if (m.kind === 'request') {
    parts.push(m.status === 'open' && !answered
      ? '<span class="letter-chip chip-wait">待答复</span>'
      : '<span class="letter-chip chip-ok">已答复</span>');
  }
  if (m.verdict) {
    const v = String(m.verdict).toLowerCase();
    const vText = v === 'reject' ? '驳回' : v === 'modify' ? '按修改执行' : '同意';
    const vClass = v === 'reject' ? 'chip-err' : v === 'modify' ? 'chip-wait' : 'chip-ok';
    parts.push(`<span class="letter-chip ${vClass}">${vText}</span>`);
  }
  // 送达情况：快照里的 delivery 已经写好大白话（"已当场告诉 Codex 1" / "还没送到：等 Codex 1 下次开工"），照着显示
  const dv = m.delivery;
  if (dv && typeof dv === 'object') {
    if (dv.state === 'delivered') {
      parts.push(`<span class="letter-chip chip-sent">✓ ${escapeHtml(dv.text || '已送到')}</span>`);
    } else if (dv.state === 'waiting') {
      parts.push(`<span class="letter-chip chip-going"><span class="hourglass">⏳</span> ${escapeHtml(dv.text || '还没送到')}</span>`);
    } else if (dv.state === 'stuck') {
      parts.push(`<span class="letter-chip chip-err">${escapeHtml(dv.text || '送不到')}</span>`);
    }
  } else if (m.delivered === true) {
    parts.push('<span class="letter-chip chip-sent">✓ 已送达</span>');
  } else if (m.delivered === false) {
    parts.push('<span class="letter-chip chip-going"><span class="hourglass">⏳</span> 还没送到</span>');
  }
  if (m.contract) {
    parts.push(`<span class="letter-chip">约定 ${escapeHtml(m.contract)}</span>`);
  }
  return parts.join('');
}

// 员工的名字可以点：点开左边的详情面板（data-person 由 app.js 统一处理）
function personAttr(who) {
  return who && String(who.key || '').startsWith('em_')
    ? ` person-link" data-person="${escapeHtml(who.key)}" title="点开看 ${escapeHtml(who.label)} 做过的事"`
    : '"';
}

function letterHtml(m, answered, people, depts) {
  const from = whoIs(m.from, people, depts);
  const to = whoIs(m.to, people, depts);
  // 点这封信时亮哪两个节点：和画布上的虚线同一套（监工那头是对方部门的监工席，写给部门、已送到的是那个人）
  const [fromNode, toNode] = letterNodes(m, depts);
  const [kindText, kindCls] = LETTER_KINDS[m.kind] || [m.kind || '信件', 'k-report'];
  const id = escapeHtml(m.id || '');
  const body = String(m.body || '');
  const subject = String(m.subject || '');

  return `
    <div class="letter ${kindCls}" id="msg-card-${id}" data-id="${id}" data-from-key="${escapeHtml(fromNode || from.key)}" data-to-key="${escapeHtml(toNode || to.key)}">
      <div class="letter-avatar">${avatarHtml(from.type, 44, 'idle')}</div>
      <div class="letter-bubble">
        <div class="letter-head">
          <b class="name-${from.type}${personAttr(from)}>${escapeHtml(from.label)}</b>
          <span class="letter-arrow">→</span>
          <b class="name-${to.type}${personAttr(to)}>${escapeHtml(to.label)}</b>
          <span class="letter-kind ${kindCls}">${kindText}</span>
          <span class="letter-time mono">${formatTime(m.ts)}</span>
        </div>
        ${subject ? `<div class="letter-subject">${escapeHtml(subject)}</div>` : ''}
        ${body && body !== subject ? `
          <div class="letter-body clamp">${escapeHtml(body)}</div>
          ${body.length > 70 ? '<button class="letter-more" type="button">展开</button>' : ''}
        ` : ''}
        ${m.answer && m.answer !== body ? `<div class="letter-answer"><b>答复：</b>${escapeHtml(m.answer)}</div>` : ''}
        <div class="letter-foot">${letterFoot(m, answered)}</div>
      </div>
    </div>`;
}

// 从画布上点虚线 / 信封 / 工位上的"✉ 1"：打开右侧信件栏，翻到那封信并选中它
export function focusLetter(id) {
  const panel = document.getElementById('side-panel');
  if (panel) {
    panel.classList.remove('collapsed');
    if (window.innerWidth <= 768) panel.classList.add('mobile-open');
  }
  const tab = document.querySelector('.tab-btn[data-tab="bus"]');
  if (tab && !tab.classList.contains('active')) tab.click();
  const card = id && document.getElementById('msg-card-' + id);
  if (!card) return false;
  document.querySelectorAll('.letter.selected').forEach(c => c.classList.remove('selected'));
  card.classList.add('selected');
  const body = card.querySelector(':scope > .letter-bubble > .letter-body.clamp');
  const more = body && body.nextElementSibling;
  if (more && more.classList.contains('letter-more')) { body.classList.remove('clamp'); more.textContent = '收起'; }
  requestAnimationFrame(() => card.scrollIntoView({ block: 'center', behavior: 'smooth' }));
  const bubble = card.querySelector(':scope > .letter-bubble');
  if (bubble) {
    bubble.classList.remove('flash-ping');
    void bubble.offsetWidth;
    bubble.classList.add('flash-ping');
    setTimeout(() => bubble.classList.remove('flash-ping'), 1300);
  }
  return true;
}

export function renderLetters(letters = [], people = {}, depts = [], onSelectLetter) {
  const listEl = document.getElementById('bus-list');
  const countEl = document.getElementById('bus-count');
  if (!listEl) return;

  const msgs = (letters || []).filter(m => m && m.kind !== 'delivered').slice().sort((x, y) => (x.ts || 0) - (y.ts || 0));
  if (countEl) countEl.textContent = String(msgs.length);

  if (!msgs.length) {
    listEl.innerHTML = '<div class="dept-empty">还没有信件</div>';
    return;
  }

  const byId = new Map(msgs.map(m => [m.id, m]));
  const answered = new Set(msgs.filter(m => m.reply_to && byId.has(m.reply_to)).map(m => m.reply_to));

  const rootOf = m => {
    let cur = m, guard = 0;
    while (cur.reply_to && byId.has(cur.reply_to) && guard++ < 20) {
      cur = byId.get(cur.reply_to);
    }
    return cur.id;
  };

  const scroller = listEl.closest('.tab-content');
  const atBottom = !scroller || scroller.scrollHeight - scroller.scrollTop - scroller.clientHeight < 80;
  const known = new Set([...listEl.querySelectorAll('.letter[data-id]')].map(el => el.getAttribute('data-id')));
  const fresh = !listEl.querySelector('.letter') || [...known].some(id => !byId.has(id));

  if (fresh) listEl.innerHTML = '';
  let added = 0;

  msgs.forEach(m => {
    const id = String(m.id || '');
    const existing = !fresh && document.getElementById('msg-card-' + id);
    if (existing) {
      const foot = existing.querySelector(':scope > .letter-bubble > .letter-foot');
      const html = letterFoot(m, answered.has(m.id));
      if (foot && foot.innerHTML !== html) foot.innerHTML = html;
      return;
    }

    const holder = document.createElement('div');
    holder.innerHTML = letterHtml(m, answered.has(m.id), people, depts);
    const el = holder.firstElementChild;
    if (!fresh) el.classList.add('letter-new');

    const root = rootOf(m);
    if (root !== m.id) {
      const rootEl = document.getElementById('msg-card-' + root);
      if (rootEl) {
        let box = rootEl.querySelector(':scope > .letter-replies');
        if (!box) {
          box = document.createElement('div');
          box.className = 'letter-replies';
          rootEl.appendChild(box);
        }
        el.classList.add('is-reply');
        box.appendChild(el);
        added++;
        return;
      }
    }
    listEl.appendChild(el);
    added++;
  });

  if (scroller && (fresh || (added && atBottom))) {
    scroller.scrollTo({ top: scroller.scrollHeight, behavior: fresh ? 'auto' : 'smooth' });
  }

  if (!listEl.dataset.bound) {
    listEl.dataset.bound = '1';
    listEl.addEventListener('click', e => {
      const more = e.target.closest('.letter-more');
      if (more) {
        const bodyEl = more.previousElementSibling;
        const open = bodyEl.classList.toggle('clamp');
        more.textContent = open ? '展开' : '收起';
        e.stopPropagation();
        return;
      }
      const card = e.target.closest('.letter[data-id]');
      if (card) {
        const msgId = card.getAttribute('data-id');
        const fromKey = card.getAttribute('data-from-key');
        const toKey = card.getAttribute('data-to-key');
        document.querySelectorAll('.letter.selected').forEach(c => c.classList.remove('selected'));
        card.classList.add('selected');
        if (onSelectLetter) onSelectLetter(msgId, fromKey, toKey);
      }
    });
  }
}
