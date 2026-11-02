// desk-fx.js - 工位小彩蛋：电脑屏幕内容、桌面小物、说话气泡（进度汇报 / 吐槽）、偶尔路过的猫
// 只读页面上已经画好的工位（状态徽章、步骤、任务清单），自己往上叠一层；不改 station.js / render-office.js，
// 工位被重画时用 MutationObserver 重新挂上。点电脑屏幕弹出小屏（干活时是真实命令行），再点小屏放大。
(function () {
  'use strict';

  const COLORS = { codex: '#10A37F', dsh: '#4D6BFE', agy: '#9B72CB', gm: '#E07A5F', rules: '#14B8A6', other: '#81858C' };
  const reduced = () => window.matchMedia && matchMedia('(prefers-reduced-motion: reduce)').matches;
  const pick = arr => arr[Math.floor(Math.random() * arr.length)];
  const esc = s => String(s == null ? '' : s).replace(/[&<>"]/g, c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;' })[c]);
  let uidSeq = 0;
  const uid = () => 'fx' + (++uidSeq);

  // ---------- 屏幕场景（坐标 116×80，挂在显示器 58×40 的屏幕区里） ----------
  const SCENES = {
    code(c, info) {
      const lines = [[10, 34, '#c792ea'], [16, 46, '#82aaff'], [16, 30, '#c3e88d'], [22, 40, '#ffcb6b'], [16, 24, '#89ddff'], [10, 18, '#c792ea'], [10, 44, '#82aaff']];
      const step = info.step ? `第${info.step}步` : '干活中';
      const pct = info.pct != null ? info.pct : 45;
      return `<rect width="116" height="80" fill="#0f1320"/>
        <rect width="116" height="10" fill="#1b2133"/>
        <circle cx="6" cy="5" r="2" fill="#ff5f57"/><circle cx="12" cy="5" r="2" fill="#febc2e"/><circle cx="18" cy="5" r="2" fill="#28c840"/>
        <rect x="26" y="2" width="30" height="6" rx="2" fill="#0f1320"/><rect x="29" y="4.2" width="20" height="1.6" rx=".8" fill="#5b6478"/>
        ${lines.map((l, i) => `<text x="2" y="${18 + i * 7.4}" font-size="4.6" fill="#3f4759" font-family="var(--mono)">${i + 1}</text>
          <rect class="fx-ln" style="animation-delay:${(i * .55).toFixed(2)}s" x="${l[0]}" y="${14.6 + i * 7.4}" width="${l[1]}" height="3.6" rx="1.8" fill="${l[2]}"/>`).join('')}
        <rect class="fx-caret" x="62" y="58.6" width="2" height="5" fill="#e6edf3"/>
        <rect y="70" width="116" height="10" fill="${c}"/>
        <text x="4" y="77.2" font-size="6.4" font-weight="700" fill="#fff" font-family="var(--sans)">${esc(step)}</text>
        <rect x="58" y="73" width="54" height="4" rx="2" fill="rgba(255,255,255,.35)"/>
        <rect x="58" y="73" width="${(54 * pct / 100).toFixed(1)}" height="4" rx="2" fill="#fff"/>`;
    },
    terminal(c) {
      const rows = ['$ 跑一遍检查', '✓ 页面能打开', '✓ 手机上不乱', '✓ 数据对得上', '全部通过 ✨'];
      return `<rect width="116" height="80" fill="#0b0d10"/>
        ${rows.map((r, i) => `<text class="fx-row" style="animation-delay:${(i * .7).toFixed(1)}s" x="6" y="${14 + i * 13}" font-size="8.4" font-weight="${i === 4 ? 800 : 500}"
          fill="${i === 0 ? '#9aa4b2' : i === 4 ? c : '#4ade80'}" font-family="var(--sans)">${r}</text>`).join('')}
        <rect class="fx-caret" x="6" y="70" width="5" height="2" fill="#e6edf3"/>`;
    },
    dashboard(c) {
      const bars = [22, 34, 18, 40, 28, 46];
      return `<rect width="116" height="80" fill="#14161c"/>
        <text x="6" y="13" font-size="8" font-weight="800" fill="#fff" font-family="var(--sans)">全员进度</text>
        <circle class="fx-blink" cx="104" cy="10" r="3" fill="#ef4444"/>
        ${bars.map((h, i) => `<rect class="fx-bar" style="animation-delay:${(i * .25).toFixed(2)}s" x="${8 + i * 17}" y="${72 - h}" width="11" height="${h}" rx="2" fill="${i % 2 ? c : '#4D6BFE'}"/>`).join('')}
        <line x1="6" y1="72.5" x2="110" y2="72.5" stroke="#3a3f4b" stroke-width="1"/>`;
    },
    genshin() {   // 梗图原样：白屏正中一个深灰"原神"标志（画的备用版）
      return `<rect width="116" height="80" fill="#fff"/>
        <g class="fx-gs-in"><text x="58" y="43" text-anchor="middle" font-size="19" font-weight="900" fill="#3c3c3c" font-family="'Songti SC','STSong',serif" letter-spacing="1">原神</text>
        <text x="58" y="53" text-anchor="middle" font-size="4.6" fill="#555" font-family="Georgia,serif" letter-spacing=".8">Genshin Impact</text></g>`;
    },
    anime() {
      const dm = [['awsl', 14, '#fff', 0], ['前方高能！', 26, '#ffd166', 1.2], ['哈哈哈哈', 38, '#7dd3fc', 2.1], ['名场面', 20, '#f9a8d4', 3.0], ['这集神了', 32, '#fff', 3.8]];
      return `<rect width="116" height="80" fill="#1d1730"/>
        <circle cx="58" cy="48" r="26" fill="#2c2350"/>
        <path d="M44 34 q-10 14 -4 30 l6 -2 q-4 -14 2 -24 Z" fill="#ff8fc7"/><path d="M72 34 q10 14 4 30 l-6 -2 q4 -14 -2 -24 Z" fill="#ff8fc7"/>
        <circle cx="58" cy="40" r="13" fill="#ffe1d0"/><path d="M45 38 q13 -16 26 0 q-6 -6 -13 -5 q-7 -1 -13 5 Z" fill="#ff8fc7"/>
        <circle cx="53" cy="42" r="1.8" fill="#3b2f63"/><circle cx="63" cy="42" r="1.8" fill="#3b2f63"/><path d="M55.5 47 q2.5 2 5 0" stroke="#d9668f" stroke-width="1" fill="none"/>
        ${dm.map(d => `<text class="fx-dm" style="animation-delay:${d[3]}s" x="118" y="${d[1]}" font-size="7" font-weight="700" fill="${d[2]}" stroke="#000" stroke-width=".5" paint-order="stroke" font-family="var(--sans)">${d[0]}</text>`).join('')}
        <rect y="72" width="116" height="8" fill="rgba(0,0,0,.55)"/>
        <path d="M4 74 l4 2 l-4 2 Z" fill="#fff"/>
        <rect x="12" y="75.2" width="78" height="1.6" rx=".8" fill="rgba(255,255,255,.3)"/>
        <rect class="fx-seek" x="12" y="75.2" width="78" height="1.6" rx=".8" fill="#fb7299"/>
        <text x="94" y="77.6" font-size="4.6" fill="#ddd" font-family="var(--sans)">第12集</text>`;
    },
    fish() {
      const g = uid();
      const fish = (cls, y, col, d) => `<g class="${cls}" style="animation-delay:${d}s"><g transform="translate(0 ${y})">
          <ellipse cx="0" cy="0" rx="7" ry="4" fill="${col}"/><path d="M-6 0 l-5 -4 l0 8 Z" fill="${col}"/><circle cx="3.5" cy="-1" r="1" fill="#1f2937"/></g></g>`;
      return `<defs><linearGradient id="${g}" x1="0" y1="0" x2="0" y2="1"><stop offset="0" stop-color="#38bdf8"/><stop offset="1" stop-color="#0c4a6e"/></linearGradient></defs>
        <rect width="116" height="80" fill="url(#${g})"/>
        <path d="M8 80 q4 -12 0 -22 M14 80 q-5 -10 0 -18 M100 80 q4 -14 -1 -26 M106 80 q-4 -9 0 -16" stroke="#22c55e" stroke-width="2" fill="none" stroke-linecap="round"/>
        ${fish('fx-fish', 30, '#fb923c', 0)}${fish('fx-fish fx-fish-slow', 52, '#facc15', 1.5)}
        <circle class="fx-bub" cx="40" cy="70" r="2" fill="none" stroke="#e0f2fe"/><circle class="fx-bub" style="animation-delay:1s" cx="74" cy="70" r="1.5" fill="none" stroke="#e0f2fe"/>
        <text x="6" y="12" font-size="8" font-weight="800" fill="#fff" font-family="var(--sans)">摸鱼中…</text>`;
    },
    gacha() {
      return `<rect width="116" height="80" fill="#26164a"/>
        <g class="fx-rays" style="transform-origin:58px 40px"><path d="M58 40 L40 0 L52 0 Z M58 40 L76 0 L88 0 Z M58 40 L116 22 L116 34 Z M58 40 L0 22 L0 34 Z M58 40 L30 80 L42 80 Z M58 40 L74 80 L86 80 Z" fill="#fde68a" opacity=".22"/></g>
        <g class="fx-flip" style="transform-origin:58px 40px"><rect x="44" y="18" width="28" height="40" rx="3" fill="#fbbf24" stroke="#fff7d6" stroke-width="1.6"/>
          <path d="M58 28 l3 6 l6.5 .8 l-4.8 4.4 l1.3 6.4 l-6 -3.2 l-6 3.2 l1.3 -6.4 l-4.8 -4.4 l6.5 -.8 Z" fill="#fff"/></g>
        <text class="fx-pop" x="58" y="72" text-anchor="middle" font-size="9" font-weight="900" fill="#fde68a" font-family="var(--sans)">十连出金！</text>
        <text x="6" y="11" font-size="6" fill="#c4b5fd" font-family="var(--sans)">抽卡模拟器</text>`;
    },
    blocks() {
      const cols = ['#f472b6', '#60a5fa', '#34d399', '#fbbf24', '#a78bfa'];
      let base = '';
      [[0, 4], [1, 3], [2, 4], [3, 2], [4, 4], [5, 3], [6, 1], [7, 4]].forEach(([x, h]) => {
        for (let y = 0; y < h; y++) base += `<rect x="${30 + x * 7}" y="${72 - (y + 1) * 7}" width="6.4" height="6.4" rx="1" fill="${cols[(x + y) % 5]}"/>`;
      });
      return `<rect width="116" height="80" fill="#0b1020"/>
        <rect x="29" y="4" width="57" height="69" fill="none" stroke="#334155"/>
        ${base}
        <g class="fx-drop"><rect x="51" y="0" width="6.4" height="6.4" rx="1" fill="#22d3ee"/><rect x="58" y="0" width="6.4" height="6.4" rx="1" fill="#22d3ee"/><rect x="58" y="-7" width="6.4" height="6.4" rx="1" fill="#22d3ee"/></g>
        <text x="90" y="16" font-size="6" fill="#94a3b8" font-family="var(--sans)">分数</text><text x="90" y="26" font-size="8" font-weight="800" fill="#fff" font-family="var(--mono)">9527</text>`;
    },
    bsod() {
      return `<rect width="116" height="80" fill="#0a5bc4"/>
        <text x="8" y="30" font-size="24" fill="#fff" font-family="var(--sans)">:(</text>
        <text x="8" y="46" font-size="7.4" font-weight="700" fill="#fff" font-family="var(--sans)">出了点问题，正在重来</text>
        <text class="fx-blink" x="8" y="58" font-size="5.6" fill="#dbeafe" font-family="var(--sans)">已收集错误信息 37%</text>
        <rect x="8" y="64" width="12" height="12" fill="#fff"/><rect x="10" y="66" width="3" height="3" fill="#0a5bc4"/><rect x="15" y="70" width="3" height="3" fill="#0a5bc4"/>`;
    },
    chat(c) {
      return `<rect width="116" height="80" fill="#eef1f5"/>
        <rect width="116" height="12" fill="#fff"/><text x="58" y="8.6" text-anchor="middle" font-size="6.4" font-weight="700" fill="#334155" font-family="var(--sans)">等回信…</text>
        <rect x="56" y="18" width="54" height="12" rx="6" fill="${c}"/><text x="83" y="26" text-anchor="middle" font-size="6" fill="#fff" font-family="var(--sans)">这个怎么定？</text>
        <rect x="6" y="36" width="34" height="14" rx="7" fill="#fff"/>
        <circle class="fx-dot" cx="15" cy="43" r="2" fill="#94a3b8"/><circle class="fx-dot" style="animation-delay:.18s" cx="23" cy="43" r="2" fill="#94a3b8"/><circle class="fx-dot" style="animation-delay:.36s" cx="31" cy="43" r="2" fill="#94a3b8"/>
        <text x="6" y="64" font-size="5.6" fill="#94a3b8" font-family="var(--sans)">对方正在输入…（已经输入了很久）</text>`;
    },
    queue(c) {
      return `<rect width="116" height="80" fill="#15171c"/>
        <circle cx="58" cy="34" r="13" fill="none" stroke="#2c313b" stroke-width="4"/>
        <circle class="fx-spin" cx="58" cy="34" r="13" fill="none" stroke="${c}" stroke-width="4" stroke-linecap="round" stroke-dasharray="22 60" style="transform-origin:58px 34px"/>
        <text x="58" y="64" text-anchor="middle" font-size="8.4" font-weight="800" fill="#e5e7eb" font-family="var(--sans)">排队中…</text>`;
    },
    sleep() {
      return `<rect width="116" height="80" fill="#050608"/>
        <g class="fx-dvd-x"><g class="fx-dvd-y"><g class="fx-hue">
          <rect x="2" y="2" width="40" height="18" rx="9" fill="#a78bfa"/><text x="22" y="15" text-anchor="middle" font-size="9" font-weight="900" fill="#050608" font-family="var(--sans)">Zzz</text>
        </g></g></g>`;
    },
    compact() {
      return `<rect width="116" height="80" fill="#1d1f26"/>
        <rect class="fx-sq-l" x="14" y="24" width="20" height="24" rx="3" fill="#9B72CB"/>
        <rect x="48" y="24" width="20" height="24" rx="3" fill="#b9a2e0"/>
        <rect class="fx-sq-r" x="82" y="24" width="20" height="24" rx="3" fill="#9B72CB"/>
        <text x="58" y="66" text-anchor="middle" font-size="8.4" font-weight="800" fill="#e9d5ff" font-family="var(--sans)">整理记忆…</text>`;
    }
  };

  // 素材：Google Flow 生成的屏幕小视频（转成会动的 webp）、做图 skill 生成的桌面小物；有就用素材，没有就退回上面画的
  const FX = '/web/fx/';
  const MEDIA = { code: 'code', terminal: 'tests', anime: 'anime', fish: 'fish', gacha: 'gacha', sleep: 'sleep', bsod: 'error', queue: 'waiting', chat: 'chat', compact: 'compact', woodfish: 'woodfish', overtime: 'overtime', possum: 'possum', zendisco: 'zendisco', chicken: 'chicken' };
  // 刷番：经典番剧致敬片段（Flow 生成），屏幕上配番名和弹幕
  const SHOWS = {
    railgun: ['某科学的超电磁炮', ['炮姐！', 'Only my railgun', '硬币呢？']],
    sao: ['刀剑神域', ['星爆气流斩！', '桐人好帅', '十六连击']],
    pokemon: ['宝可梦', ['就决定是你了！', '十万伏特', '童年回来了']],
    naruto: ['火影忍者', ['多重影分身之术', '燃起来了！', '青春']],
    onepiece: ['海贼王', ['我是要成为海贼王的男人', '更新了吗', '橡胶橡胶']],
    conan: ['名侦探柯南', ['真相只有一个！', '凶手就是你', '又有人倒下了']]
  };
  // 2026 热梗屏幕的角标
  const MEME_CAP = { possum: '我将辞职研究这只负鼠', zendisco: '禅者行脚 · 蹦迪版', chicken: '做完你的，做你的' };
  const PROPS = ['mug', 'plant', 'figure', 'cat', 'catwalk'];
  const have = new Set();
  // 原神启动：直接引用官网首页的标志图片（不拷进仓库），加载失败就露出底下画的那张
  const GENSHIN = { logo: 'https://ys.mihoyo.com/main/_nuxt/img/logo-header-full.a71d70a.png' };   // 偷看小屏放 fx/screens/genshin.gif

  function screenMarkup(scene, color, info) {
    const still = reduced();   // 系统开了"减少动画"：不放会动的素材，用画的（CSS 已把它们冻住）
    if (scene === 'woodfish' && !still) {
      return `<rect width="116" height="80" fill="#1a1426"/><image href="${FX}screens/woodfish.webp" width="116" height="80" preserveAspectRatio="xMidYMid slice"/>
        <text class="fx-merit" x="80" y="26" font-size="8" font-weight="900" fill="#fde68a" stroke="#000" stroke-width=".5" paint-order="stroke" font-family="var(--sans)">功德+1</text>`;
    }
    if (scene === 'genshin') {
      // 官网的白色发光标志染成深灰放在白屏上（去掉外发光），跟"原神启动"梗图一样；图片加载失败就露出底下画的那张
      const f = uid();
      return SCENES.genshin() + `<filter id="${f}"><feColorMatrix values="0 0 0 0 .22  0 0 0 0 .22  0 0 0 0 .22  0 0 0 2.4 -1.2"/></filter>
        <rect width="116" height="80" fill="#fff"/>
        <g class="fx-gs-in"><image href="${GENSHIN.logo}" x="24" y="4" width="68" height="51" filter="url(#${f})"/>
        <text x="60" y="58" text-anchor="middle" font-size="4.6" fill="#555" font-family="Georgia,serif" letter-spacing=".8">Genshin Impact</text></g>`;
    }
    if (scene.startsWith('show:')) {
      const k = scene.slice(5), show = SHOWS[k];
      if (!show || !have.has(k) || still) return SCENES.anime(color, info);
      const ep = 1 + (k.length * 7 + info.name.length) % 24;
      return `<rect width="116" height="80" fill="#111"/><image href="${FX}screens/${k}.webp" width="116" height="72" preserveAspectRatio="xMidYMid slice"/>
        ${show[1].map((t, i) => `<text class="fx-dm" style="animation-delay:${i * 1.5}s" x="118" y="${12 + i * 11}" font-size="6.6" font-weight="700" fill="${['#fff', '#ffd166', '#7dd3fc'][i]}" stroke="#000" stroke-width=".5" paint-order="stroke" font-family="var(--sans)">${t}</text>`).join('')}
        <rect y="72" width="116" height="8" fill="rgba(0,0,0,.75)"/><path d="M4 74 l4 2 l-4 2 Z" fill="#fff"/>
        <rect class="fx-seek" x="11" y="75.2" width="44" height="1.6" rx=".8" fill="#fb7299"/>
        <text x="112" y="77.8" text-anchor="end" font-size="4.8" fill="#ddd" font-family="var(--sans)">${show[0]} 第${ep}集</text>`;
    }
    const m = MEDIA[scene];
    if (!m || !have.has(m) || still) return (SCENES[scene] || SCENES.anime)(color, info);
    if (MEME_CAP[scene]) {
      return `<rect width="116" height="80" fill="#0f1320"/><image href="${FX}screens/${m}.webp" width="116" height="80" preserveAspectRatio="xMidYMid slice"/>
        <rect y="68" width="116" height="12" fill="rgba(0,0,0,.6)"/>
        <text x="58" y="76.4" text-anchor="middle" font-size="6.4" font-weight="800" fill="#fde68a" font-family="var(--sans)">${MEME_CAP[scene]}</text>`;
    }
    let html = `<rect width="116" height="80" fill="#0f1320"/><image href="${FX}screens/${m}.webp" width="116" height="80" preserveAspectRatio="xMidYMid slice"/>`;
    if (scene === 'code' && info.step) {
      html += `<rect y="70" width="116" height="10" fill="${color}" opacity=".92"/>
        <text x="4" y="77.2" font-size="6.4" font-weight="700" fill="#fff" font-family="var(--sans)">第${info.step}/${info.of}步</text>
        <rect x="58" y="73" width="54" height="4" rx="2" fill="rgba(255,255,255,.35)"/>
        <rect x="58" y="73" width="${(54 * (info.pct || 0) / 100).toFixed(1)}" height="4" rx="2" fill="#fff"/>`;
    }
    return html;
  }

  const DRAWN_IDLE = ['genshin', 'genshin', 'anime', 'anime', 'fish', 'gacha', 'blocks'];
  const MEDIA_IDLE = ['genshin', 'genshin', 'show', 'show', 'show', 'anime', 'fish', 'gacha', 'woodfish', 'overtime', 'possum', 'possum', 'zendisco'];
  const WORK_SCENES = ['code', 'code', 'code', 'terminal', 'terminal', 'chicken'];
  const STATE_SCENE = { failed: 'bsod', blocked: 'chat', waiting: 'queue', queued: 'queue', sleeping: 'sleep', compacting: 'compact' };

  // 每个员工记住当前在看哪个场景，重画时不跳台；过一阵子换台
  const memo = new Map();
  function sceneFor(key, state) {
    const now = Date.now();
    const m = memo.get(key);
    const shows = Object.keys(SHOWS).filter(k => have.has(k));
    let pool = state === 'working' ? (key === 'gm' ? [have.has('code') ? 'code' : 'dashboard'] : WORK_SCENES.filter(n => n !== 'chicken' || have.has('chicken')))
      : state === 'idle' ? (!reduced() && Object.values(MEDIA).some(m => have.has(m)) ? MEDIA_IDLE.filter(n => n === 'genshin' || (n === 'show' ? shows.length : have.has(MEDIA[n]))) : DRAWN_IDLE) : null;
    if (!pool) return STATE_SCENE[state] || null;
    if (m && m.state === state && now < m.until) return m.scene;
    // 别的桌正在放的台先不选，免得满屏都是原神
    const inUse = new Set([...memo].filter(([k, v]) => k !== key && now < v.until).map(([, v]) => v.scene));
    const fresh = pool.filter(n => n === 'show' ? shows.some(k => !inUse.has('show:' + k)) : !inUse.has(n));
    if (fresh.length && new Set(fresh).size > 1) pool = fresh;
    let scene = pick(pool);
    if (m && new Set(pool).size > 1) while (scene === m.scene || (scene === 'show' && shows.length === 1 && m.scene === 'show:' + shows[0])) scene = pick(pool);   // 只有一种可选时不换台，免得死循环
    if (scene === 'show') {
      const free = shows.filter(k => (!m || m.scene !== 'show:' + k) && !inUse.has('show:' + k));
      scene = 'show:' + pick(free.length ? free : shows.filter(k => !m || m.scene !== 'show:' + k).concat(shows.length === 1 ? shows : []));
    }
    memo.set(key, { state, scene, until: now + 9000 + Math.random() * 9000 });
    return scene;
  }

  // ---------- 读页面上已画好的工位 ----------
  function stationInfo(svg) {
    const card = svg.closest('[data-worker-key]');
    if (card) {
      const badge = card.querySelector('.station-state-badge');
      const state = badge ? ([...badge.classList].find(c => c.startsWith('state-')) || 'state-idle').slice(6) : 'idle';
      const nameEl = card.querySelector('.station-worker-name');
      const agent = nameEl ? ([...nameEl.classList].find(c => c.startsWith('name-')) || 'name-other').slice(5) : 'other';
      const stepBadge = card.querySelector('.step-badge');
      const m = stepBadge && /(\d+)\s*\/\s*(\d+)/.exec(stepBadge.textContent);
      const running = card.querySelector('.todo-running span') || card.querySelector('.todo-queued span');
      return {
        key: card.getAttribute('data-worker-key'), card, anchor: card.querySelector('.station-svg-wrap'), state, agent,
        name: nameEl ? nameEl.textContent.trim() : '',
        step: m ? +m[1] : null, of: m ? +m[2] : null, pct: m ? Math.round(+m[1] * 100 / Math.max(1, +m[2])) : null,
        stepText: (card.querySelector('.step-text') || {}).textContent || '',
        task: running ? (running.getAttribute('title') || running.textContent) : ''
      };
    }
    const gm = svg.closest('#node-gm');
    if (gm) {
      const anyWorking = document.querySelector('#dept-grid .station-state-badge.state-working');
      return { key: 'gm', card: gm, anchor: gm.querySelector('.gm-desk-wrap'), state: anyWorking ? 'working' : 'idle', agent: 'gm', name: '负责人' };
    }
    return null;
  }

  // ---------- 往显示器和桌面上叠东西 ----------
  const NS = 'http://www.w3.org/2000/svg';
  const hash = str => [...String(str)].reduce((a, c) => (a * 31 + c.charCodeAt(0)) >>> 0, 7);
  function mugSvg(color, steaming) {
    return `<g class="fx-mug">
      ${steaming ? '<path class="fx-steam" d="M131 54 q-2 -3 0 -6 q2 -3 0 -6" stroke="var(--label-4)" stroke-width="1.1" fill="none" stroke-linecap="round"/><path class="fx-steam" style="animation-delay:.7s" d="M135 54 q-2 -3 0 -6 q2 -3 0 -6" stroke="var(--label-4)" stroke-width="1.1" fill="none" stroke-linecap="round"/>' : ''}
      <path d="M127 57 h11 v7 a2 2 0 0 1 -2 2 h-7 a2 2 0 0 1 -2 -2 Z" fill="#fff" stroke="#2b2b2b" stroke-width="1"/>
      <path d="M138 59 h1.6 a2 2 0 0 1 0 4 h-1.6" fill="none" stroke="#2b2b2b" stroke-width="1"/>
      <path d="M130 61 l1.5 -1.5 l1.5 1.5 l1.5 -1.5" stroke="${color}" stroke-width="1" fill="none"/></g>`;
  }
  const PLANT = `<g class="fx-plant"><path d="M11 60 h7 l-1 6 h-5 Z" fill="#e8a87c" stroke="#2b2b2b" stroke-width=".8"/>
      <path class="fx-leaf" d="M14.5 60 q-5 -5 -3 -10 q3 3 3 10 Z M14.5 60 q4 -6 2 -11 q-3 4 -2 11 Z" fill="#4ade80" stroke="#166534" stroke-width=".6"/></g>`;
  const NOTE = `<g transform="rotate(-8 75 60)"><rect x="70" y="54" width="8" height="8" fill="#fde047" stroke="#ca8a04" stroke-width=".5"/><path d="M71.5 57h5 M71.5 59h3.5" stroke="#a16207" stroke-width=".6"/></g>`;

  function decorate(svg) {
    const info = stationInfo(svg);
    if (!info) return;
    svg.setAttribute('data-fx', info.state);   // 记下挂的时候是什么状态，状态变了就重挂
    const color = COLORS[info.agent] || COLORS.other;
    const scene = sceneFor(info.key, info.state);
    const layer = document.createElementNS(NS, 'g');
    layer.setAttribute('class', 'fx-layer');
    layer.setAttribute('aria-hidden', 'true');
    layer.setAttribute('data-sig', sigOf(info));
    layers.set(info.key, layer);
    let html = '';
    if (scene) html += `<svg class="fx-screen" x="20" y="24" width="58" height="40" viewBox="0 0 116 80" data-scene="${scene}">${screenMarkup(scene, color, info)}</svg>`;
    const h = hash(info.key);
    if (info.key === 'gm' || h % 3) html += have.has('plant') ? `<image class="fx-prop" href="${FX}props/plant.webp" x="0" y="47" width="20" height="20"${h % 2 ? ' transform="translate(20 0) scale(-1 1)"' : ''}/>` : PLANT;
    if (info.state === 'working') html += NOTE;
    if (have.has('figure') && (info.key === 'gm' || h % 4 === 1)) html += `<image class="fx-prop" href="${FX}props/figure.webp" x="104" y="49" width="17" height="17"/>`;
    html += have.has('mug')
      ? `<image class="fx-prop" href="${FX}props/mug.webp" x="124" y="51" width="16" height="16" style="filter:hue-rotate(${(h * 47) % 360}deg) drop-shadow(0 1px 1px rgba(0,0,0,.25))"/>${info.state === 'working' ? '<path class="fx-steam" d="M131 51 q-2 -3 0 -6 q2 -3 0 -6" stroke="var(--label-4)" stroke-width="1.1" fill="none" stroke-linecap="round"/><path class="fx-steam" style="animation-delay:.7s" d="M135 51 q-2 -3 0 -6 q2 -3 0 -6" stroke="var(--label-4)" stroke-width="1.1" fill="none" stroke-linecap="round"/>' : ''}`
      : mugSvg(color, info.state === 'working');
    layer.innerHTML = html;
    svg.appendChild(layer);
    if (bubbles.has(info.key) && bubbles.get(info.key).dataset.hush && info.card.classList.contains('station-card')) info.card.classList.add('fx-hush');
    // 记一下上一次状态：干活 → 空闲 时喊一声"做完啦"
    if (info.key !== 'gm') {
      const prev = lastState.get(info.key);
      lastState.set(info.key, info.state);
      if (prev === 'working' && info.state === 'idle') setTimeout(() => say(info.key, pick(LINES.done), 'happy'), 300);
      if (prev && prev !== 'failed' && info.state === 'failed') setTimeout(() => say(info.key, pick(LINES.failed), 'sad'), 300);
    }
  }
  const lastState = new Map();
  const layers = new Map();   // 员工 → 他的叠层节点
  const sigOf = i => [i.state, i.step, i.of].join('|');   // 这些变了屏幕内容才要重画

  function decorateAll() {
    // 页面重画可能整块换掉工位，也可能就地改（morph）：没有叠层、或状态变了，都重挂
    document.querySelectorAll('#office-canvas svg.station-svg').forEach(svg => {
      const info = stationInfo(svg);
      const layer = svg.querySelector(':scope > .fx-layer');
      if (layer && (!info || layer.getAttribute('data-sig') === sigOf(info))) return;   // 状态或第几步变了才重挂（工位卡现在就地更新，svg 不一定会换）
      if (layer) layer.remove();
      // 同一个员工、状态和步数都没变：把原来的叠层整块搬到新画的工位上，动画不从头放
      const kept = info && layers.get(info.key);
      if (kept && !kept.isConnected && kept.getAttribute('data-sig') === sigOf(info)) {
        svg.appendChild(kept);
        svg.setAttribute('data-fx', info.state);
        if (bubbles.has(info.key) && bubbles.get(info.key).dataset.hush && info.card.classList.contains('station-card')) info.card.classList.add('fx-hush');
        return;
      }
      decorate(svg);
    });
    if (peekSrc && !peekSrc.isConnected) {   // 工位被重画或换了台：小屏跟到新屏幕上（画面没变就不重建），工位没了就收起
      const s = screenOf(peekKey);
      if (s) { showPeek(s); placePeek(); } else hidePeek();
    }
  }
  function screenOf(key) {
    const a = key && anchorOf(key);
    return a && a.querySelector('svg.fx-screen');
  }

  // ---------- 说话气泡 ----------
  const LINES = {
    working: [
      i => i.step ? `第 ${i.step} 步 / 共 ${i.of} 步${i.stepText ? '：' + i.stepText : ''}` : null,
      i => i.task ? `正在做「${i.task}」` : null,
      i => i.pct != null ? `进度 ${i.pct}%，别催别催～` : null,
      () => '做完你的，做你的', () => '助我破鼎！', () => '从从容容，游刃有余（假的）', () => '匆匆忙忙，连滚带爬', () => '这一步快好了！', () => '代码在跑了，先别叫我', () => '写完这段就去倒杯水', () => '思路来了思路来了！'
    ],
    idle: ['我将辞职，认真研究这只负鼠', '我又贪了，再摸五分钟', '如何呢？又能怎', '眼睛瞎在手机上了', '爱你老己，先歇会儿', '耳机里在放拼好歌', '活呢？活呢？', '原神，启动！', '老板没派活，那我先刷会儿番', '等派活等到花儿都谢了', '抽卡又歪了……', '我没在摸鱼，我在思考人生', '今天也是带薪发呆的一天', '人在工位，心在提瓦特', '敲一下电子木鱼，功德+1', '班味太重了，透透气', '牛马也要喝水的嘛'],
    blocked: ['真没空陪你闹了，快回信', '如何呢？又能怎，继续等', '隔壁部门怎么还不回信 😤', '信寄出去了，望穿秋水', '在等答复，先喝口茶', '已读不回是吧……', '家人们谁懂啊，等了半天'],
    failed: ['当时确实不舒服', '伦敦未必有我忧郁', '如何呢？又能怎，再来一次', '呜呜又挂了，我看看哪错了', '这……不是我的锅吧？', '没关系，重来就好！', '破防了，但还能再试一次'],
    waiting: ['排队中，前面还有人', '名额满了，等等我～'],
    queued: ['排队中，前面还有人', '名额满了，等等我～'],
    sleeping: ['Zzz……', '五分钟……再睡五分钟……'],
    compacting: ['脑子装满了，整理一下', '记性有点不够用了……'],
    done: ['敬自己一杯！', '爱你老己 ✨', '这波很有活人感', '做完啦！求夸奖 ✨', '搞定收工！', '交差！下一件是什么？', '泰裤辣！', '这波遥遥领先！'],
    agent: {
      codex: ['（推眼镜）这段写得真优雅', '白龙娘出马，一个顶俩'],
      dsh: ['鲸鱼娘全速前进！', '让我再深度思考一下～'],
      agy: ['我来看看页面好不好看～', '配色要再可爱一点！']
    },
    gm: ['给大家提供点情绪价值', '别慌，做完你的，做你的', '大家辛苦啦，进度我盯着呢', '今天的活能按时交吗？', '有问题随时写信给我'],
    gmIdle: ['喝口茶，看看报表', '大家都在摸鱼吗……'],
    boss: ['上班时间不许摸鱼！', '我看见你开原神了！', '进度汇报一下？', '又在研究负鼠？', '班味呢？班味去哪了！'],
    caught: ['我没摸，我在研究负鼠', '我又贪了……', '没有没有，我在思考！', '马上干！马上干！', '这是在找灵感……'],
    cat: ['别踩我键盘啦！', '喵？谁家的猫？', '它在帮我写代码吗……']
  };

  function anchorOf(key) {
    if (key === 'gm') return document.querySelector('#node-gm .gm-desk-wrap');
    if (key.startsWith('head:')) return document.getElementById('node-head-' + key.slice(5));
    const card = document.querySelector(`[data-worker-key="${CSS.escape(key)}"]`);
    return card && card.querySelector('.station-svg-wrap');
  }

  // 气泡放在 #office-canvas 的浮层里（不放进工位节点），工位整块重画时气泡不会被抹掉
  const talking = new Map();
  const bubbles = new Map();
  function say(key, text, tone = '') {
    if (!text) return;
    const anchor = anchorOf(key);
    const canvas = document.getElementById('office-canvas');
    if (!anchor || !canvas) return;
    if (bubbles.has(key)) bubbles.get(key).remove();
    const card = anchor.closest('.station-card');
    const nameEl = card && card.querySelector('.station-worker-name');
    const agent = nameEl ? ([...nameEl.classList].find(c => c.startsWith('name-')) || 'name-other').slice(5) : 'other';
    const left = key.startsWith('head:');
    const el = document.createElement('div');
    el.className = 'fx-say' + (tone ? ' fx-say-' + tone : '') + (left ? ' fx-say-left fx-say-below' : '');
    el.setAttribute('aria-hidden', 'true');
    el.style.setProperty('--c', key === 'gm' ? COLORS.gm : left ? COLORS.rules : COLORS[agent] || COLORS.other);
    el.textContent = text;
    canvas.appendChild(el);
    // 公司页能缩放：屏幕上量出来的距离除以缩放比例，才是画布里的坐标
    const base = canvas.getBoundingClientRect(), r0 = (card || anchor).getBoundingClientRect();
    const k = canvas.offsetWidth ? (base.width / canvas.offsetWidth) || 1 : 1;
    const r = { left: (r0.left - base.left) / k + base.left, right: (r0.right - base.left) / k + base.left, top: (r0.top - base.top) / k + base.top, bottom: (r0.bottom - base.top) / k + base.top };
    const x = left ? r.left - base.left + 2 : r.right - base.left + (card ? 6 : 8) - el.offsetWidth;
    let y = left ? r.bottom - base.top + 8 : r.top - base.top + (card ? 4 : 16) - el.offsetHeight;
    const lx = Math.max(4, x), w = el.offsetWidth, h = el.offsetHeight;
    // 和别的气泡叠在一起时往上挪到它头顶（监工喊话和员工接话常挨着）
    const others = [...canvas.querySelectorAll(':scope > .fx-say')].filter(o => o !== el && !o.classList.contains('fx-say-out'));
    for (let n = 0; n < 4; n++) {
      const hit = others.find(o => { const ox = o.offsetLeft, oy = o.offsetTop; return lx < ox + o.offsetWidth && ox < lx + w && Math.max(4, y) < oy + o.offsetHeight && oy < Math.max(4, y) + h; });
      if (!hit) break;
      y = hit.offsetTop - h - 6;
    }
    el.style.left = lx + 'px';
    el.style.top = Math.max(4, y) + 'px';
    bubbles.set(key, el);
    talking.set(key, Date.now());
    if (tone === 'happy' || tone === 'sad') el.dataset.hush = '1';
    if (card && el.dataset.hush) card.classList.add('fx-hush');   // 只有"做完/出错"这种真消息才暂时盖住思考气泡
    setTimeout(() => {
      el.classList.add('fx-say-out');
      setTimeout(() => {
        el.remove();
        if (bubbles.get(key) === el) bubbles.delete(key);
        const c = anchorOf(key);
        const cc = c && c.closest('.station-card');
        if (cc && !bubbles.has(key)) cc.classList.remove('fx-hush');
      }, 260);
    }, 3600);
  }

  function lineFor(info) {
    if (info.state === 'working') {
      if (Math.random() < .25 && LINES.agent[info.agent]) return pick(LINES.agent[info.agent]);
      const opts = LINES.working.map(f => f(info)).filter(Boolean);
      return opts.length ? pick(opts.slice(0, 3).concat(opts)) : null;
    }
    return pick(LINES[info.state] || LINES.idle);
  }

  function visible(el) {
    const r = el.getBoundingClientRect();
    return r.width > 0 && r.bottom > 0 && r.top < innerHeight && r.right > 0 && r.left < innerWidth;
  }

  function tick() {
    if (document.hidden) return;
    const now = Date.now();
    if (document.querySelectorAll('.fx-say').length >= 2) return;
    const svgs = [...document.querySelectorAll('#dept-grid svg.station-svg[data-fx]')].filter(visible);
    // 员工头顶有真实的思考气泡时不插玩笑话，别把真信息挡掉
    const infos = svgs.map(stationInfo).filter(i => i && now - (talking.get(i.key) || 0) > 12000 && !(i.card.querySelector && i.card.querySelector('.station-thought-bubble')));
    const roll = Math.random();

    // 监工抓摸鱼：同一房间里有空闲员工时，监工喊一声，员工接一句
    if (roll < .18) {
      const idle = infos.filter(i => i.state === 'idle');
      for (const i of idle.sort(() => Math.random() - .5)) {
        const room = i.card.closest('.room');
        const head = room && room.querySelector('.head-desk');
        if (head && visible(head)) {
          say('head:' + head.id.replace('node-head-', ''), pick(LINES.boss), 'boss');
          setTimeout(() => say(i.key, pick(LINES.caught), 'sweat'), 1500);
          return;
        }
      }
    }
    if (roll < .28) {
      const gm = document.querySelector('#node-gm');
      if (gm && visible(gm) && now - (talking.get('gm') || 0) > 15000) {
        say('gm', pick(document.querySelector('#dept-grid .state-working') ? LINES.gm : LINES.gmIdle));
        return;
      }
    }
    if (!infos.length) return;
    const working = infos.filter(i => i.state === 'working');
    const who = working.length && Math.random() < .6 ? pick(working) : pick(infos);
    say(who.key, lineFor(who), who.state === 'working' ? 'report' : '');
  }

  // ---------- 彩蛋：一只猫偶尔走过桌面，在键盘上坐一会儿 ----------
  const CAT = `<g class="fx-cat-body">
      <path d="M-9 0 q0 -9 9 -9 q8 0 9 6 l0 3 Z" fill="#f4a259" stroke="#2b2b2b" stroke-width=".9"/>
      <circle cx="9" cy="-9" r="5" fill="#f4a259" stroke="#2b2b2b" stroke-width=".9"/>
      <path d="M5.5 -12.5 l1 -4 l2.5 3 Z M10 -13.5 l2.5 -3 l.8 4 Z" fill="#f4a259" stroke="#2b2b2b" stroke-width=".8" stroke-linejoin="round"/>
      <circle cx="8" cy="-9.4" r=".8" fill="#2b2b2b"/><circle cx="11.2" cy="-9.4" r=".8" fill="#2b2b2b"/>
      <path class="fx-tail" d="M-9 -3 q-6 -2 -6 -9" stroke="#2b2b2b" stroke-width="2.6" fill="none" stroke-linecap="round"/>
      <path class="fx-tail" d="M-9 -3 q-6 -2 -6 -9" stroke="#f4a259" stroke-width="1.4" fill="none" stroke-linecap="round"/></g>`;

  function catWalk() {
    if (document.hidden || reduced()) return;
    const svgs = [...document.querySelectorAll('#office-canvas svg.station-svg[data-fx]')].filter(visible);
    if (!svgs.length) return;
    const svg = pick(svgs);
    const info = stationInfo(svg);
    const layer = svg.querySelector(':scope > .fx-layer');
    if (!info || !layer) return;
    const cat = document.createElementNS(NS, 'g');
    cat.setAttribute('class', 'fx-cat');
    cat.innerHTML = have.has('catwalk') ? `<g class="fx-cat-bob"><image href="${FX}props/catwalk.webp" x="-12" y="-22" width="24" height="24"/></g>` : CAT;
    layer.appendChild(cat);
    const at = x => `translate(${x}px, 66px)`;
    const anim = cat.animate([
      { transform: at(-24) }, { transform: at(104), offset: .4 }, { transform: at(104), offset: .62 }, { transform: at(176) }
    ], { duration: 7600, easing: 'linear' });
    setTimeout(() => cat.isConnected && say(info.key, pick(LINES.cat), 'sweat'), 3300);
    const img = cat.querySelector('image');
    if (img && have.has('cat')) {   // 走到键盘上坐下，再起身走掉
      setTimeout(() => { img.setAttribute('href', FX + 'props/cat.webp'); cat.firstChild.classList.remove('fx-cat-bob'); }, 3040);
      setTimeout(() => { img.setAttribute('href', FX + 'props/catwalk.webp'); cat.firstChild.classList.add('fx-cat-bob'); }, 4710);
    }
    anim.onfinish = () => cat.remove();
  }

  // ---------- 偷看屏幕：点电脑弹出小屏；员工在干活时小屏里是他真实的命令行，再点小屏放大 ----------
  // 真实命令行由 V2 会话的 cli-view.js 提供（window.XayCliView），没加载时小屏照旧放彩蛋，点小屏改为打开左侧详情
  let peek = null, peekSrc = null, peekKey = null, peekKind = null, peekStop = null;
  const cliView = () => window.XayCliView && typeof XayCliView.mini === 'function' ? XayCliView : null;
  function showPeek(screen) {
    const scene = screen.getAttribute('data-scene');
    const svg = screen.closest('svg.station-svg');
    const info = svg && stationInfo(svg);
    if (!scene || !info) return hidePeek();
    const live = info.key !== 'gm' && info.state !== 'idle' && cliView();
    const kind = live ? 'cli' : scene;
    if (peek && info.key === peekKey && kind === peekKind) { peekSrc = screen; return; }   // 同一个画面：不重建，视频、命令行接着放
    hidePeek();
    peekSrc = screen; peekKey = info.key; peekKind = kind;
    peek = document.createElement('div');
    peek.className = 'fx-peek';
    const cli = info.key !== 'gm' && cliView() && typeof XayCliView.open === 'function' ? XayCliView : null;
    const canOpen = !!cli || info.card.classList.contains('station-card');
    const body = live ? '<div class="fx-peek-cli"></div>'
      : scene === 'genshin' && !reduced()
        ? `<img class="fx-peek-gif" width="232" height="160" src="${FX}screens/genshin.gif" alt="原神启动">`   // 本地 GIF（原来嵌的 YouTube 要登录、放不出来）
        : `<svg viewBox="0 0 116 80" width="232" height="160" aria-hidden="true">${screenMarkup(scene, COLORS[info.agent] || COLORS.other, info)}</svg>`;
    peek.innerHTML = `<div class="fx-peek-title">${live ? `${esc(info.name)} 的命令行` : `偷看 ${esc(info.name)} 的屏幕`}${canOpen ? `<span>${live ? '点开放大' : cli ? '点开看命令行' : '点开看详情'}</span>` : ''}</div>${body}`;
    if (canOpen) {
      peek.classList.add('fx-peek-open');
      peek.addEventListener('click', e => {
        e.stopPropagation();
        const key = info.key;
        hidePeek();
        if (cli && cli.open(key) !== false) return;   // 空闲的员工也能看他上一次的命令行；打不开再去左侧详情
        const card = document.querySelector(`.station-card[data-worker-key="${CSS.escape(key)}"]`);   // 页面会重画，按员工重新找当前的工位卡
        if (card) card.click();   // 交给员工详情面板自己的点击处理
      });
    }
    document.body.appendChild(peek);
    if (live) {
      try { peekStop = live.mini(info.key, peek.querySelector('.fx-peek-cli')); } catch (err) { peekStop = null; }
    }
    placePeek();
  }
  function placePeek() {
    if (!peek || !peekSrc) return;
    const r = peekSrc.getBoundingClientRect();
    const w = peek.offsetWidth || 248, h = peek.offsetHeight || 196;
    let top = r.top - h - 8;
    if (top < 56) top = r.bottom + 8;
    peek.style.left = Math.max(8, Math.min(innerWidth - w - 8, r.left + r.width / 2 - w / 2)) + 'px';
    peek.style.top = top + 'px';
  }
  function hidePeek() {
    if (peekStop) { try { typeof peekStop === 'function' ? peekStop() : peekStop.stop && peekStop.stop(); } catch (err) { /* 停不了也不影响收起 */ } }
    if (peek) peek.remove();
    peek = peekSrc = peekKey = peekKind = peekStop = null;
  }

  // ---------- 启动 ----------
  function start() {
    const canvas = document.getElementById('office-canvas');
    if (!canvas) return;
    const names = Object.values(MEDIA).concat(Object.keys(SHOWS)).map(m => 'screens/' + m).concat(PROPS.map(p => 'props/' + p));
    // 一个一个查（本机小服务器同时来十几个请求会丢），失败重试一次
    const probe = (n, retry = 1) => fetch(FX + n + '.webp')   // 用 GET：本机服务器对 HEAD 会中途断开；顺便把素材预先载入缓存
      .then(r => { if (r.ok) have.add(n.split('/')[1]); })
      .catch(() => retry && new Promise(ok => setTimeout(ok, 200)).then(() => probe(n, retry - 1)));
    names.reduce((p, n) => p.then(() => probe(n)), Promise.resolve())
      .then(() => {
        if (!have.size) return;
        memo.clear();
        document.querySelectorAll('#office-canvas svg.station-svg[data-fx]').forEach(svg => {
          const layer = svg.querySelector('.fx-layer');
          if (layer) layer.remove();
          svg.removeAttribute('data-fx');
        });
        decorateAll();
      });
    let queued = false;
    new MutationObserver(() => {
      if (queued) return;
      queued = true;
      setTimeout(() => { queued = false; decorateAll(); }, 30);   // 不用 rAF：页面在后台时 rAF 不跑
    }).observe(canvas, { childList: true, subtree: true });
    document.addEventListener('xay:rendered', () => decorateAll());   // 公司页每次重画完会发这个事件
    decorateAll();

    // 换台：屏幕场景到点后重画这一台
    setInterval(() => {
      const now = Date.now();
      document.querySelectorAll('#office-canvas svg.station-svg[data-fx]').forEach(svg => {
        const info = stationInfo(svg);
        const m = info && memo.get(info.key);
        const layer = svg.querySelector('.fx-layer');
        if (m && now > m.until && !(layer && layer.querySelector('.fx-cat'))) {   // 猫在桌上时先不换台
          if (layer) layer.remove();
          svg.removeAttribute('data-fx');
          decorate(svg);
        }
      });
    }, 2000);

    setTimeout(function loop() { tick(); setTimeout(loop, 3800 + Math.random() * 2600); }, 1800);
    setTimeout(function loop() { catWalk(); setTimeout(loop, 45000 + Math.random() * 50000); }, 14000 + Math.random() * 10000);

    // 点电脑屏幕开关小屏；这一下不再冒泡去打开左侧详情（在捕获阶段拦下）
    canvas.addEventListener('click', e => {
      const s = e.target.closest && e.target.closest('svg.fx-screen');
      if (!s) return;
      e.stopPropagation();
      if (peek && peekKey === (stationInfo(s.closest('svg.station-svg')) || {}).key) hidePeek(); else showPeek(s);
    }, true);
    document.addEventListener('click', e => { if (peek && !peek.contains(e.target)) hidePeek(); });
    document.addEventListener('keydown', e => { if (e.key === 'Escape') hidePeek(); });
    document.addEventListener('scroll', e => { if (peek && !peek.contains(e.target)) hidePeek(); }, true);   // 真正在滚的是外面的 #office-viewport
    addEventListener('resize', hidePeek);
  }

  window.XayDeskFx = { say, catWalk, refresh: decorateAll, have, scenes: Object.keys(SCENES), sceneSvg: (name, color, info = {}) => SCENES[name](color, info) };
  if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', start);
  else start();
})();
