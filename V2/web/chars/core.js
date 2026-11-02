/*
 * 怎么写一个角色：只调用 XayChars.register('agent', def)。使用 120×140 坐标系，
 * 头部中心约 (60, 52)。body(ctx) 画固定身体/头发/脸底，eyes、mouth、可选 brows
 * 按表情名放 SVG 片段，hands 放 rest/typing/wave/cheer 四组手势；片段可以是字符串
 * 或 (ctx) => 字符串。typing 的前两个子组分别是左右手，wave 是举到头旁的手。
 * ctx = { agent, color, idPrefix, id(name) }，所有渐变、clipPath 等都用
 * ctx.id('hair') 生成 id，并用 url(#同一个id) 引用，不能写固定 id。
 * extras 可按状态名或表情名放专属特效；各片段与 body 共用 ctx。
 * 通用灰色角色在下面完整示范所有必需片段；描边由根 SVG 统一提供。
 * 状态→手势：working=typing，idle=rest（偶尔 wave + happy），waiting=rest，
 * blocked=wave（挠头），failed=rest，sleeping=rest，talking=wave，celebrate=cheer。
 * celebrate 的跳跃只播 2 秒；宿主负责结束瞬时状态。setState 仅改属性，不换节点。
 * 角色专属动画写在 def.css（字符串），每个角色只注入一次；选择器必须以
 * .xc-<agent> 开头，例如 .xc-dsh[data-state="working"] .xc-dsh-tail {…}。
 * 不要在片段里写 <style>，也不要用 SMIL 动画（减少动画模式关不掉它）。
 *
 * 图片角色（现在四个 AI 都用这种）：def 不写 body/eyes/mouth/hands，改写
 *   images: { calm, smile, serious, surprised, pout, eyesclosed, aggrieved, shy } → 头像图地址
 * core 按 IMAGE_EXPR 把 9 种表情映射到这 8 张图；头像画在 (0,4) 起 120×120 的方框里，
 * 动作（点头、跳、晃）和共享特效照旧。图片角色不眨眼（换整张脸会闪）。
 */
(function () {
  'use strict';

  const STATES = Object.freeze(['working', 'idle', 'waiting', 'blocked', 'failed', 'sleeping', 'talking', 'celebrate']);
  const EXPRESSIONS = Object.freeze(['neutral', 'happy', 'focused', 'surprised', 'worried', 'sleepy', 'sad', 'proud', 'closed']);
  const MOUTHS = ['neutral', 'happy', 'open', 'focused', 'surprised', 'worried', 'sleepy', 'sad', 'proud'];
  const HANDS = ['rest', 'typing', 'wave', 'cheer'];
  const defaults = { working: 'focused', idle: 'neutral', waiting: 'neutral', blocked: 'worried', failed: 'sad', sleeping: 'sleepy', talking: 'happy', celebrate: 'proud' };
  const gestures = { working: 'typing', idle: 'rest', waiting: 'rest', blocked: 'wave', failed: 'rest', sleeping: 'rest', talking: 'wave', celebrate: 'cheer' };
  // 表情 → 头像图（图来自设定图的 8 种表情：平静、自然微笑、认真、惊讶、鼓腮、闭眼轻笑、委屈、害羞）
  const IMAGE_EXPR = { neutral: 'calm', happy: 'smile', focused: 'serious', surprised: 'surprised', worried: 'pout',
    sleepy: 'eyesclosed', sad: 'aggrieved', proud: 'eyesclosed', closed: 'eyesclosed' };
  const registry = new Map();
  let sequence = 0;
  let scheduled = false;

  // 眨眼时只切眼睛，嘴巴保持原表情；closed 的嘴使用 neutral。
  const expressionCSS = EXPRESSIONS.map(expression => {
    const mouth = expression === 'closed' ? 'neutral' : expression;
    return `.xc[data-expr="${expression}"]:not([data-blink="1"]) .xc-eyes > [data-e="${expression}"],
      .xc[data-expr="${expression}"] .xc-mouth > [data-m="${mouth}"],
      .xc[data-expr="${expression}"] .xc-brows > [data-e="${expression}"] { display: inline; }`;
  }).join('\n');

  const css = `
    .xc { display: inline-block; vertical-align: middle; overflow: visible; }
    .xc-root { transform-origin: 60px 126px; }
    .xc-head { transform-origin: 60px 52px; }
    .xc-eyes > [data-e], .xc-mouth > [data-m], .xc-brows > [data-e],
    .xc-hands > [data-h], .xc-fx > g { display: none; }
    ${expressionCSS}
    .xc[data-blink="1"] .xc-eyes > [data-e="closed"] { display: inline; }
    .xc[data-gesture="rest"] .xc-hands > [data-h="rest"],
    .xc[data-gesture="typing"] .xc-hands > [data-h="typing"],
    .xc[data-gesture="wave"] .xc-hands > [data-h="wave"],
    .xc[data-gesture="cheer"] .xc-hands > [data-h="cheer"] { display: inline; }
    .xc[data-state="working"] .xc-root { transform: skewX(-2deg); }
    .xc[data-state="working"] [data-h="typing"] > :first-child { animation: xc-type .28s ease-in-out infinite alternate; }
    .xc[data-state="working"] [data-h="typing"] > :nth-child(2) { animation: xc-type .28s ease-in-out -.28s infinite alternate; }
    .xc[data-state="idle"] .xc-root { animation: xc-breathe 3.6s ease-in-out infinite; }
    .xc[data-state="idle"] .xc-head { animation: xc-look 9s ease-in-out infinite; }
    .xc[data-state="idle"] [data-h="wave"], .xc[data-state="talking"] [data-h="wave"] {
      transform-origin: 84px 89px; animation: xc-wave .6s ease-in-out infinite alternate;
    }
    .xc[data-state="waiting"] .xc-root { animation: xc-sway 3s ease-in-out infinite; }
    .xc[data-state="waiting"] .xc-fx-hourglass { display: inline; animation: xc-hourglass 3s ease-in-out infinite; transform-origin: 95px 24px; }
    .xc[data-state="blocked"] .xc-head { transform: rotate(-5deg); }
    .xc[data-state="blocked"] [data-h="wave"] { animation: xc-scratch .5s ease-in-out infinite alternate; }
    .xc[data-state="blocked"] .xc-fx-question { display: inline; animation: xc-float 2s ease-in-out infinite; }
    .xc[data-state="failed"] .xc-root { transform: translateY(5px); }
    .xc[data-state="failed"] .xc-head { transform: translateY(3px) rotate(6deg); }
    .xc[data-state="failed"] .xc-fx-sweat, .xc[data-expr="worried"] .xc-fx-sweat { display: inline; }
    .xc[data-state="failed"] .xc-fx-tear, .xc[data-expr="sad"] .xc-fx-tear { display: inline; animation: xc-tear 2s ease-in infinite; }
    .xc[data-state="sleeping"] .xc-head { animation: xc-nod 4s ease-in-out infinite; }
    .xc[data-state="sleeping"] .xc-fx-zzz { display: inline; animation: xc-drift 3s ease-in-out infinite; }
    .xc[data-state="talking"] .xc-fx-bubble { display: inline; animation: xc-float 2s ease-in-out infinite; }
    .xc[data-state="talking"][data-expr="happy"] .xc-mouth > [data-m="happy"] { animation: xc-speak-shut .7s steps(1, end) infinite; }
    .xc[data-state="talking"][data-expr="happy"] .xc-mouth > [data-m="open"] { display: inline; animation: xc-speak-open .7s steps(1, end) infinite; }
    .xc[data-state="celebrate"] .xc-root { animation: xc-jump 2s ease-out both; }
    .xc[data-state="celebrate"] .xc-fx-stars { display: inline; animation: xc-sparkle .65s ease-in-out 3 alternate; }
    ${STATES.map(state => `.xc[data-state="${state}"] .xc-fx > [data-s="${state}"] { display: inline; }`).join('\n')}
    ${EXPRESSIONS.map(expression => `.xc[data-expr="${expression}"] .xc-fx > [data-x="${expression}"] { display: inline; }`).join('\n')}
    .xc-img .xc-face { filter: drop-shadow(0 0 .6px rgba(0,0,0,.45)) drop-shadow(0 2px 2px rgba(0,0,0,.18)); }
    .xc-img[data-state="working"] .xc-root { transform: none; }
    .xc-img[data-state="working"] .xc-head { animation: xc-bob 1.1s ease-in-out infinite alternate; }
    .xc-img[data-state="talking"] .xc-head { animation: xc-chat .32s ease-in-out infinite alternate; }
    .xc-img[data-state="sleeping"] .xc-face, .xc-img[data-state="failed"] .xc-face { opacity: .82; }
    .xc-img[data-state="celebrate"] .xc-head { animation: xc-cheer 2s ease-out both; }
    @keyframes xc-bob { from { transform: translateY(0) rotate(0); } to { transform: translateY(2.5px) rotate(1.5deg); } }
    @keyframes xc-chat { to { transform: translateY(-1.5px) scale(1.015); } }
    @keyframes xc-cheer { 10%, 30% { transform: rotate(-4deg); } 20%, 40% { transform: rotate(4deg); } 50%, 100% { transform: rotate(0); } }
    @keyframes xc-type { to { transform: translateY(3px); } }
    @keyframes xc-breathe { 50% { transform: translateY(-1.5px) scaleY(1.015); } }
    @keyframes xc-look { 0%, 30%, 70%, 100% { transform: rotate(0); } 40% { transform: rotate(-5deg); } 60% { transform: rotate(5deg); } }
    @keyframes xc-wave { from { transform: rotate(-6deg); } to { transform: rotate(9deg); } }
    @keyframes xc-sway { 25% { transform: rotate(-3deg); } 75% { transform: rotate(3deg); } }
    @keyframes xc-scratch { to { transform: translate(-3px, -3px); } }
    @keyframes xc-float { 50% { transform: translateY(-3px); } }
    @keyframes xc-hourglass { 40% { transform: rotate(0); } 60%, 100% { transform: rotate(180deg); } }
    @keyframes xc-tear { from { transform: translateY(0); opacity: 1; } to { transform: translateY(8px); opacity: 0; } }
    @keyframes xc-nod { 50% { transform: translateY(3px) rotate(7deg); } }
    @keyframes xc-drift { from { transform: translateY(3px); opacity: .35; } to { transform: translate(3px, -7px); opacity: 1; } }
    @keyframes xc-speak-shut { 0%, 100% { visibility: visible; } 50% { visibility: hidden; } }
    @keyframes xc-speak-open { 0%, 100% { visibility: hidden; } 50% { visibility: visible; } }
    @keyframes xc-jump { 0%, 35%, 100% { transform: translateY(0); } 17% { transform: translateY(-9px); } 50% { transform: translateY(-3px); } }
    @keyframes xc-sparkle { to { opacity: .3; } }
    @media (prefers-reduced-motion: reduce) {
      .xc, .xc * { animation: none !important; }
      .xc .xc-root, .xc .xc-head { transform: none !important; }
      .xc[data-state="talking"][data-expr="happy"] .xc-mouth > [data-m="open"] { display: none; }
    }
  `;

  function defaultExpression(state) {
    return defaults[state];
  }

  function schedule() {
    window.setTimeout(() => {
      document.querySelectorAll('svg.xc').forEach(svg => {
        const state = svg.getAttribute('data-state');
        const expression = svg.getAttribute('data-expr');
        if (state !== 'sleeping' && expression !== 'happy' && expression !== 'closed' && !svg.classList.contains('xc-img')) {
          svg.setAttribute('data-blink', '1');
          window.setTimeout(() => svg.removeAttribute('data-blink'), 120);
        }
        if (state === 'idle' && svg.getAttribute('data-auto-expr') === '1' && Math.random() < .2 &&
            !window.matchMedia('(prefers-reduced-motion: reduce)').matches) {
          svg.setAttribute('data-expr', 'happy');
          svg.setAttribute('data-gesture', 'wave');
          window.setTimeout(() => {
            if (svg.getAttribute('data-state') === 'idle' && svg.getAttribute('data-auto-expr') === '1') {
              svg.setAttribute('data-expr', 'neutral');
              svg.setAttribute('data-gesture', 'rest');
            }
          }, 900);
        }
      });
      schedule();
    }, 3000 + Math.random() * 3000);
  }

  function prepare(agent, def) {
    if (def && def.css && !document.getElementById('xc-styles-' + agent)) {
      const own = document.createElement('style');
      own.id = 'xc-styles-' + agent;
      own.textContent = def.css;
      document.head.appendChild(own);
    }
    if (!document.getElementById('xc-styles')) {
      const style = document.createElement('style');
      style.id = 'xc-styles';
      style.textContent = css;
      document.head.appendChild(style);
    }
    if (!scheduled) {
      scheduled = true;
      schedule();
    }
  }

  function escape(value) {
    return String(value).replace(/[&<>"']/g, character => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' })[character]);
  }

  function fragment(value, ctx) {
    return typeof value === 'function' ? value(ctx) : (value || '');
  }

  function groups(parts, names, attribute, ctx) {
    return names.map(name => `<g ${attribute}="${name}">${fragment(parts[name], ctx)}</g>`).join('');
  }

  // 灰色通用小角色，也是给角色作者复制、替换的完整模板。
  const generic = {
    name: '通用小助手', color: '#929aa5',
    body(ctx) {
      const coat = ctx.id('coat');
      return `<defs><linearGradient id="${coat}" x2="0" y2="1"><stop stop-color="#e5e8ec"/><stop offset="1" stop-color="#929aa5"/></linearGradient></defs>
        <ellipse cx="60" cy="130" rx="27" ry="4" fill="#929aa5" opacity=".2" stroke="none"/>
        <path d="M43 113v14h13v-13m8 0v13h13v-14" fill="#727b87"/>
        <path d="M40 125h17v6H38q-2-4 2-6m24 0h15q4 2 3 6H64z" fill="#e5e8ec"/>
        <path d="M43 79Q31 90 34 114q26 12 52 0 3-24-9-35" fill="url(#${coat})"/>
        <path d="m48 80 12 12 12-12M60 92v26" fill="none"/>
        <circle cx="60" cy="102" r="4" fill="#f8fafc"/>
        <path d="M27 52Q20 15 60 15t33 37v16H27z" fill="#a6adb7"/>
        <ellipse cx="28" cy="56" rx="5" ry="7" fill="#f4e7dd"/><ellipse cx="92" cy="56" rx="5" ry="7" fill="#f4e7dd"/>
        <path d="M29 40q0-21 31-21t31 21v19q-2 22-31 23-29-1-31-23z" fill="#f4e7dd"/>
        <path d="M28 47V36Q34 14 60 17q29-1 33 29L76 30l-8 13-13-13-12 17 1-14z" fill="#a6adb7"/>
        <path d="M37 26q11-11 24-7" fill="none" stroke="#e5e8ec"/>
        <ellipse cx="39" cy="63" rx="6" ry="3" fill="#dfb5ae" stroke="none"/><ellipse cx="81" cy="63" rx="6" ry="3" fill="#dfb5ae" stroke="none"/>`;
    },
    eyes: {
      neutral: '<ellipse cx="44" cy="53" rx="3" ry="5"/><ellipse cx="76" cy="53" rx="3" ry="5"/>',
      happy: '<path d="M38 54q6-10 12 0m20 0q6-10 12 0" fill="none"/>',
      focused: '<path d="m38 48 12 3m20 0 12-3" fill="none"/><path d="M44 52v5m32-5v5"/>',
      surprised: '<ellipse cx="44" cy="53" rx="5" ry="7" fill="white"/><ellipse cx="76" cy="53" rx="5" ry="7" fill="white"/><circle cx="44" cy="54" r="2"/><circle cx="76" cy="54" r="2"/>',
      worried: '<path d="m38 48 11-4m22 0 11 4" fill="none"/><ellipse cx="44" cy="54" rx="3" ry="4"/><ellipse cx="76" cy="54" rx="3" ry="4"/>',
      sleepy: '<path d="M38 54q6 5 12 0m20 0q6 5 12 0" fill="none"/>',
      sad: '<path d="M38 49q6 5 12 0m20 0q6 5 12 0" fill="none"/><path d="M44 54v4m32-4v4"/>',
      proud: '<path d="m38 50 12-2m20 0 12 2" fill="none"/><path d="M44 52v4m32-4v4"/>',
      closed: '<path d="M38 54h12m20 0h12" fill="none"/>'
    },
    mouth: {
      neutral: '<path d="M55 67q5 3 10 0" fill="none"/>',
      happy: '<path d="M52 65q8 13 16 0z" fill="#e99a9e"/>',
      open: '<ellipse cx="60" cy="68" rx="5" ry="7" fill="#e99a9e"/>',
      focused: '<path d="M56 68h8" fill="none"/>',
      surprised: '<ellipse cx="60" cy="68" rx="4" ry="5" fill="#e99a9e"/>',
      worried: '<path d="m53 69 4-2 4 2 5-2" fill="none"/>',
      sleepy: '<ellipse cx="60" cy="68" rx="3" ry="2" fill="#e99a9e"/>',
      sad: '<path d="M54 71q6-7 12 0" fill="none"/>',
      proud: '<path d="M53 66q8 7 14-2" fill="none"/>'
    },
    hands: {
      rest: '<path d="M35 88q-7 12 3 21" fill="none" stroke="#929aa5" stroke-width="9"/><circle cx="38" cy="109" r="5" fill="#f4e7dd"/><path d="M85 88q7 12-3 21" fill="none" stroke="#929aa5" stroke-width="9"/><circle cx="82" cy="109" r="5" fill="#f4e7dd"/>',
      typing: '<g><path d="M35 88q-1 13 16 17" fill="none" stroke="#929aa5" stroke-width="9"/><ellipse cx="51" cy="105" rx="6" ry="4" fill="#f4e7dd"/></g><g><path d="M85 88q1 13-16 17" fill="none" stroke="#929aa5" stroke-width="9"/><ellipse cx="69" cy="105" rx="6" ry="4" fill="#f4e7dd"/></g><path d="M40 113h40l4 6H36z" fill="#e5e8ec"/>',
      wave: '<path d="M35 88q-7 12 3 21" fill="none" stroke="#929aa5" stroke-width="9"/><circle cx="38" cy="109" r="5" fill="#f4e7dd"/><path d="M84 88q13-4 8-21" fill="none" stroke="#929aa5" stroke-width="9"/><path d="M88 69v-8q0-4 3-1l2 4v-7q2-3 4 0v10q1 7-5 7z" fill="#f4e7dd"/>',
      cheer: '<path d="M35 88 23 72m62 16 12-16" fill="none" stroke="#929aa5" stroke-width="9"/><circle cx="23" cy="70" r="6" fill="#f4e7dd"/><circle cx="97" cy="70" r="6" fill="#f4e7dd"/>'
    }
  };

  function effects(def, ctx) {
    const extras = def.extras || {};
    return `<g class="xc-fx-zzz" fill="#727b87" stroke="none" font-family="sans-serif" font-weight="bold"><text x="91" y="40" font-size="12">z</text><text x="101" y="28" font-size="15">z</text><text x="107" y="14" font-size="18">z</text></g>
      <g class="xc-fx-sweat"><path d="M26 40q-9 11-2 13t2-13" fill="#93c5fd" stroke="#42698a" stroke-width="1.5"/></g>
      <g class="xc-fx-question"><path d="M84 10h23q5 0 5 5v18q0 5-5 5H96l-9 6 2-6h-5q-5 0-5-5V15q0-5 5-5z" fill="#fff5d6"/><text x="89" y="31" font-size="23" font-family="sans-serif" fill="#604820" stroke="none">?</text></g>
      <g class="xc-fx-stars" fill="#f7ce64" stroke="#9a712c" stroke-width="1.5"><path d="m17 28 3 7 8 2-7 4-1 8-5-6-8 1 5-6-2-8zM99 43l3 6 7 1-5 5 1 7-6-3-6 3 1-7-5-5 7-1zM87 6l2 5 6 1-4 4v5l-4-3-5 3 1-5-4-4 6-1z"/></g>
      <g class="xc-fx-tear"><path d="M77 60q-7 9-1 11t1-11" fill="#93c5fd" stroke="#42698a" stroke-width="1.5"/></g>
      <g class="xc-fx-hourglass"><path d="M86 13h18m-18 23h18M88 14q0 6 7 10-7 5-7 11h14q0-6-7-11 7-4 7-10z" fill="#fff5d6"/><path d="m90 17 5 6 5-6m-10 16 5-6 5 6" fill="#dcaa4b" stroke="none"/></g>
      <g class="xc-fx-bubble"><path d="M80 9h28q6 0 6 6v15q0 6-6 6H94l-9 7 1-7h-6q-6 0-6-6V15q0-6 6-6z" fill="#f4f8ff"/><path d="M82 20h23m-23 7h15" fill="none" stroke="#6d86ac"/></g>
      ${Object.keys(extras).map(key => `<g ${STATES.includes(key) ? 'data-s' : 'data-x'}="${escape(key)}">${fragment(extras[key], ctx)}</g>`).join('')}`;
  }

  function imageHead(def, ctx) {
    // 头像底部是直切边，渐隐掉，歪头时就不会露出斜边
    const fade = ctx.id('fade'), mask = ctx.id('mask');
    const defs = `<defs><linearGradient id="${fade}" gradientUnits="userSpaceOnUse" x1="0" y1="94" x2="0" y2="122">
      <stop offset="0" stop-color="#fff"/><stop offset="1" stop-color="#000"/></linearGradient>
      <mask id="${mask}"><rect x="-20" y="-20" width="160" height="180" fill="url(#${fade})"/></mask></defs>`;
    const faces = EXPRESSIONS.map(expression => {
      const url = def.images[IMAGE_EXPR[expression]] || def.images.calm || '';
      return `<g data-e="${expression}"><image href="${escape(url)}" x="0" y="4" width="120" height="120" preserveAspectRatio="xMidYMid meet"/></g>`;
    }).join('');
    return `${defs}<g class="xc-eyes xc-face" stroke="none" mask="url(#${mask})">${faces}</g><g class="xc-mouth"></g>`;
  }

  function render(agent, { state = 'idle', expression, size = 72 } = {}) {
    const def = registry.get(agent) || generic;
    prepare(agent, registry.get(agent));
    const idPrefix = `xc-${++sequence}-`;
    const ctx = { agent, color: def.color, idPrefix, id: name => idPrefix + name };
    const expr = expression || defaultExpression(state);
    const head = def.images ? imageHead(def, ctx) : `<g class="xc-brows">${groups(def.brows || {}, EXPRESSIONS, 'data-e', ctx)}</g>
      <g class="xc-eyes">${groups(def.eyes, EXPRESSIONS, 'data-e', ctx)}</g>
      <g class="xc-mouth">${groups(def.mouth, MOUTHS, 'data-m', ctx)}</g>`;
    return `<svg xmlns="http://www.w3.org/2000/svg" class="xc xc-${escape(agent)}${def.images ? ' xc-img' : ''}" data-agent="${escape(agent)}" data-state="${escape(state)}" data-expr="${escape(expr)}" data-auto-expr="${expression ? '0' : '1'}" data-gesture="${gestures[state]}" viewBox="0 0 120 140" width="${size}" height="${size * 140 / 120}" role="img" aria-label="${escape(def.name + ' · ' + state + ' · ' + expr)}" fill="#2b2b2b" stroke="#2b2b2b" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
      <g class="xc-root"><g class="xc-body">${fragment(def.body, ctx)}</g>
      <g class="xc-head">${head}</g>
      <g class="xc-hands">${groups(def.hands || {}, HANDS, 'data-h', ctx)}</g>
      <g class="xc-fx">${effects(def, ctx)}</g></g></svg>`;
  }

  function setState(svgEl, state, expression) {
    const expr = expression || defaultExpression(state);
    svgEl.setAttribute('data-state', state);
    svgEl.setAttribute('data-expr', expr);
    svgEl.setAttribute('data-auto-expr', expression ? '0' : '1');
    svgEl.setAttribute('data-gesture', gestures[state]);
    svgEl.setAttribute('aria-label', `${(registry.get(svgEl.getAttribute('data-agent')) || generic).name} · ${state} · ${expr}`);
    svgEl.removeAttribute('data-blink');
  }

  window.XayChars = { register(agent, def) { registry.set(agent, def); }, has(agent) { return registry.has(agent); }, info(agent) { const def = registry.get(agent) || generic; return { name: def.name, color: def.color }; }, render, setState, STATES, EXPRESSIONS, defaultExpression };
}());
