// cli-view.js - "看他的屏幕"：员工真实的命令行过程（他跑的命令和输出、他说的话、思考）。放大窗口底部能对他说话。
// 普通脚本（不是模块），挂在 window.XayCliView；样式自己注入，不靠外部 CSS。设计见 references/plans/ui-v2-employee-panel.md。
//
//   XayCliView.open(emKey)            -> boolean    打开放大的终端窗口（z-index 1000），Esc / 点窗口外面 / × 关闭；
//                                                   emKey 是 'gm'、空或格式不对时不打开，返回 false。再调一次就换人。
//   XayCliView.mini(emKey, container) -> { stop() } 往容器里画最近 10 行（10px 等宽、深色底），实时滚动；
//                                                   没输出 / 'gm' / 不认识的 key 画一行"等待输出…"；stop() 断开并清空，可重复调用。
//   XayCliView.close()                              关掉放大窗口。
//   XayCliView.pane(emKey, container) -> { stop() } 往容器里画完整的终端（内容和放大窗口一样，铺满容器，跟着滚到最新；
//                                                   往上翻就停住，点"↓ 最新"回来）；看板每名员工一列用（M5）。
//
// 数据：整个页面所有人的直播共用一条连接 GET /api/v2/live?ems=em_a,em_b@3.12,…&tail=N（SSE，不带 plain = 原样；
// em@段.行 = 这个人已经看到第几段第几行，服务端只发之后的；没带 @ 按 tail 补发），每帧 data 带 em，按人分给他的小屏 / 列 / 放大窗口。
// 要看的人变了就换一条新连接（旧的先断开）；藏到后台 10 秒断开，回来再连（office-spec 第「二¾」节第 6 条）。
// 直播文件在工位写入时已经脱敏，这里显示前再遮一次常见的密钥样子。唯一的写操作：放大窗口底部的输入框，把话当插话发出去
// （POST /api/v2/control，action=steer，记为用户原话）；他在干活就当场 / 下一轮开头听到，没活时管家叫他起来在原对话里回。小屏、看板的列只读。
(function () {
  'use strict';
  if (window.XayCliView) return;

  var EM_RE = /^[A-Za-z0-9_-]{1,64}$/;
  var TAIL = 200;              // 第一次补发最近多少行（服务端上限 500）
  var MAX_BLOCKS = 600;        // 窗口里最多留这么多段
  var MINI_LINES = 10;
  var OUT_LINES = 40;          // 一次命令输出在大窗口里最多显示这么多行（多的折掉，只留尾部）
  var CMD_LINES = 30;
  var LIVE_SECONDS = 90;       // 最近一行在这么多秒内：标"直播中"
  var TOOL_PREFIX = /^(读文件|改文件|运行命令|搜索|上网|工具)[：:]\s*/;
  var QUIET_STATUS = { '空闲': 1, '干活中': 1 };

  function valid(em) {
    return typeof em === 'string' && em !== 'gm' && EM_RE.test(em);
  }

  function raf(fn) {
    return window.requestAnimationFrame ? window.requestAnimationFrame(fn) : setTimeout(fn, 16);
  }

  function esc(s) {
    return String(s == null ? '' : s).replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;').replace(/"/g, '&quot;');
  }

  // 页面上再遮一次（工位写直播文件时已经过 redact；这里防万一）
  var SECRETS = [
    [/-----BEGIN [A-Z ]*PRIVATE KEY-----[\s\S]*?-----END [A-Z ]*PRIVATE KEY-----/g, '<已隐藏>'],
    [/\b(API[_-]?KEY|TOKEN|SECRET|PASSWORD)(\s*[=:]\s*)\S+/gi, '$1$2<已隐藏>'],
    [/\bAuthorization:[ \t]*[^\r\n]+/gi, 'Authorization: <已隐藏>'],
    [/\bBearer\s+\S+/gi, 'Bearer <已隐藏>'],
    [/"(api_key|token|secret|password)"\s*:\s*"[^"]+"/gi, '"$1":"<已隐藏>"'],
    [/\bsk-[A-Za-z0-9_\-]{8,}/g, '<已隐藏>'],
    [/\bgh[pousr]_[A-Za-z0-9]{20,}/g, '<已隐藏>'],
    [/\bAKIA[0-9A-Z]{16}\b/g, '<已隐藏>']
  ];
  function mask(text) {
    var t = String(text == null ? '' : text);
    for (var i = 0; i < SECRETS.length; i++) t = t.replace(SECRETS[i][0], SECRETS[i][1]);
    return t;
  }

  function clock(ts) {
    if (!ts) return '';
    var d = new Date(ts * 1000);
    var p = function (n) { return (n < 10 ? '0' : '') + n; };
    return p(d.getHours()) + ':' + p(d.getMinutes());
  }

  function demoParam() {
    try { return new URLSearchParams(window.location.search).get('demo') || ''; } catch (_) { return ''; }
  }

  // ------------------------------------------------------------ 一个人的直播：几段内容（行从下面的总线来）

  var feeds = Object.create(null);   // em -> Feed；还有人在看（小屏 / 列 / 放大窗口）才留着
  var nextId = 1;

  function dispatchCliEvent(name, em) {
    if (typeof window === 'undefined' || !window.dispatchEvent) return;
    try {
      var ev;
      if (typeof CustomEvent === 'function') {
        ev = new CustomEvent(name, { detail: { em: em } });
      } else if (document && document.createEvent) {
        ev = document.createEvent('CustomEvent');
        ev.initCustomEvent(name, false, false, { em: em });
      }
      if (ev) window.dispatchEvent(ev);
    } catch (_) {}
  }

  function Feed(em) {
    this.em = em;
    this.blocks = [];
    this.subs = [];
    this.lastTs = 0;
    this.maxSeg = 0;
    this.maxN = 0;
  }

  // 换连接时告诉服务端他已经看到哪一行（em@段.行），只发之后的；还什么都没收到就只写 em，按 tail 补发
  Feed.prototype.pos = function () {
    return this.maxSeg || this.maxN ? this.em + '@' + this.maxSeg + '.' + this.maxN : this.em;
  };

  // 直播行按 (seg, n) 去重：同一段里编号不大于已收到的最大编号就是重连补发的旧行；换了新的一段（交接后新对话）编号从 1 重新数
  function seenBefore(f, row) {
    var seg = typeof row.seg === 'number' ? row.seg : 0;
    var n = typeof row.n === 'number' ? row.n : 0;
    if (seg < (f.maxSeg || 0)) return true;
    if (seg > (f.maxSeg || 0)) { f.maxSeg = seg; f.maxN = 0; }
    if (n && n <= f.maxN) return true;
    if (n > f.maxN) f.maxN = n;
    return false;
  }

  Feed.prototype.push = function (b) {
    b.id = nextId++;
    b.v = 1;
    this.blocks.push(b);
    if (this.blocks.length > MAX_BLOCKS) this.blocks.splice(0, this.blocks.length - MAX_BLOCKS);
  };

  Feed.prototype.add = function (row) {
    var kind = row.kind;
    var text = typeof row.text === 'string' ? row.text : '';
    var ts = typeof row.ts === 'number' ? row.ts : 0;
    if (ts) this.lastTs = Math.max(this.lastTs, ts);
    var last = this.blocks[this.blocks.length - 1];
    if (kind === 'thinking' || kind === 'text') {
      if (last && last.kind === kind) { last.text += text; last.v++; }
      else this.push({ kind: kind, text: text, ts: ts });
    } else if (kind === 'tool') {
      var m = text.match(TOOL_PREFIX);
      if (m && m[1] === '运行命令') this.push({ kind: 'cmd', text: text.slice(m[0].length), ts: ts });
      else this.push({ kind: 'tool', label: m ? m[1] : '工具', text: m ? text.slice(m[0].length) : text, ts: ts });
    } else if (kind === 'tool_output') {
      this.push({ kind: 'out', text: text, ts: ts });
    } else if (kind === 'status') {
      if (QUIET_STATUS[text.trim()]) return;
      this.push({ kind: /^出错/.test(text) ? 'err' : /^开始做|^整理记忆|^接着原来的对话/.test(text) ? 'start' : 'status', text: text, ts: ts });
    } else {
      return;
    }
    this.notify();
  };

  Feed.prototype.notify = function () {
    for (var i = 0; i < this.subs.length; i++) {
      try { this.subs[i](); } catch (_) { /* 某个视图出错不影响别的 */ }
    }
  };

  Feed.prototype.isLive = function () {
    return this.lastTs > 0 && Date.now() / 1000 - this.lastTs < LIVE_SECONDS;
  };

  // soon：现在没有连接就马上连（公司页的小屏、放大窗口一次只开一个，不用等）；看板一次铺好几列，等一下合并成一条再连
  function subscribe(em, fn, soon) {
    var f = feeds[em];
    var fresh = !f;
    if (fresh) f = feeds[em] = new Feed(em);
    f.subs.push(fn);
    if (fresh) busChanged(soon);
    return {
      feed: f,
      off: function () {
        var i = f.subs.indexOf(fn);
        if (i >= 0) f.subs.splice(i, 1);
        if (!f.subs.length && feeds[em] === f) {
          delete feeds[em];
          busChanged(false);
        }
      }
    };
  }

  // ------------------------------------------------------------ 总线：整个页面所有人的直播共用一条连接
  // 浏览器对同一网址最多同时开 6 条连接，按人开会占满，同一浏览器里别的看板页就卡住。
  // 要看的人变了：等 RETUNE_MS（几次变化合并成一次）再换一条新连接，先断开旧的再开新的，已经收到的人带上位置不重发；
  // 出错不靠浏览器自动重连（它会带着旧网址里过时的位置），自己关掉、过几秒按最新位置重连，连续失败间隔加长。

  var MAX_EMS = 64;            // 服务端一条连接最多带这么多人（一个页面不会同时看这么多人）
  var RETUNE_MS = 200;
  var HIDDEN_MS = 10000;       // 藏到后台这么久断开
  var RETRY_MIN = 2000;        // 出错后第一次等这么久重连，之后每次翻倍
  var RETRY_MAX = 30000;

  var bus = { es: null, key: '', retune: null, retry: null, fails: 0, paused: false, hiddenTimer: null };

  function wanted() {
    return Object.keys(feeds).sort().slice(0, MAX_EMS);
  }

  function busClose() {
    if (bus.es) {
      try { bus.es.close(); } catch (_) { /* 已经关了 */ }
      bus.es = null;
    }
    bus.key = '';
  }

  function busStop() {
    clearTimeout(bus.retune);
    clearTimeout(bus.retry);
    bus.retune = bus.retry = null;
    bus.fails = 0;
    busClose();
  }

  // 按现在要看的人连上：人没变、连接还在就不动；没人看了就断开
  function busSync() {
    var ems = wanted();
    if (!ems.length) { busStop(); return; }
    if (bus.paused || bus.retry) return;         // 藏在后台 / 正等着重连：到时候按最新的人和位置连
    var key = ems.join(',');
    if (bus.es && bus.key === key) return;
    clearTimeout(bus.retune);
    bus.retune = null;
    busClose();                                   // 先断开旧的，再开新的
    busOpen(ems, key);
  }

  function busOpen(ems, key) {
    if (typeof EventSource === 'undefined') return;
    var url = '/api/v2/live?ems=' + ems.map(function (em) { return feeds[em].pos(); }).join(',') + '&tail=' + TAIL;
    var demo = demoParam();
    if (demo) url += '&demo=' + encodeURIComponent(demo);
    var es;
    try { es = new EventSource(url); } catch (_) { busFailed(); return; }
    bus.es = es;
    bus.key = key;
    es.addEventListener('open', function () { if (bus.es === es) bus.fails = 0; });
    es.addEventListener('live', function (e) {
      if (bus.es !== es) return;                  // 已经换掉的旧连接
      var row = null;
      try { row = JSON.parse(e.data); } catch (_) { return; }
      if (!row || typeof row !== 'object') return;
      var em = typeof row.em === 'string' ? row.em : (ems.length === 1 ? ems[0] : '');
      var f = em ? feeds[em] : null;
      if (!f || seenBefore(f, row)) return;       // 没人在看他了 / 补发的旧行不重复显示（按 段号 + 编号）
      f.add(row);
    });
    es.addEventListener('error', function () {
      if (bus.es !== es) return;
      busClose();
      busFailed();
    });
  }

  function busFailed() {
    bus.fails++;
    clearTimeout(bus.retry);
    bus.retry = setTimeout(function () {
      bus.retry = null;
      busSync();
    }, Math.min(RETRY_MAX, RETRY_MIN * Math.pow(2, bus.fails - 1)));
  }

  function busChanged(soon) {
    if (!wanted().length) { busStop(); return; }  // 没人看了：马上断开
    if (bus.paused || bus.retry) return;
    if (soon && !bus.es) { busSync(); return; }
    if (!bus.retune) bus.retune = setTimeout(function () { bus.retune = null; busSync(); }, RETUNE_MS);
  }

  if (typeof document !== 'undefined' && document.addEventListener) {
    document.addEventListener('visibilitychange', function () {
      if (document.visibilityState === 'hidden') {
        clearTimeout(bus.hiddenTimer);
        bus.hiddenTimer = setTimeout(function () {
          bus.hiddenTimer = null;
          bus.paused = true;
          clearTimeout(bus.retune);
          clearTimeout(bus.retry);
          bus.retune = bus.retry = null;
          busClose();
        }, HIDDEN_MS);
      } else {
        clearTimeout(bus.hiddenTimer);
        bus.hiddenTimer = null;
        if (bus.paused) {
          bus.paused = false;
          bus.fails = 0;
          busSync();
        }
      }
    });
  }

  // ------------------------------------------------------------ 样式（只注入一次；颜色固定成终端的深色，明暗主题都一样）

  var CSS = [
    '.xcv-ov{position:fixed;inset:0;z-index:1000;background:rgba(8,10,14,.55);display:flex;align-items:center;justify-content:center;padding:24px;}',
    '.xcv-win{width:min(960px,94vw);height:min(680px,86vh);display:flex;flex-direction:column;border-radius:12px;overflow:hidden;',
    'background:#0d1117;color:#e6edf3;box-shadow:0 24px 60px rgba(0,0,0,.5),0 0 0 1px rgba(255,255,255,.08);font-family:ui-monospace,"SF Mono",Menlo,Consolas,monospace;}',
    '.xcv-bar{flex:none;height:38px;display:flex;align-items:center;gap:10px;padding:0 10px 0 14px;background:#161b22;border-bottom:1px solid #21262d;}',
    '.xcv-dots{display:flex;gap:6px;}.xcv-dots i{width:11px;height:11px;border-radius:50%;background:#30363d;}',
    '.xcv-dots i:nth-child(1){background:#ff5f57}.xcv-dots i:nth-child(2){background:#febc2e}.xcv-dots i:nth-child(3){background:#28c840}',
    '.xcv-title{flex:1;min-width:0;font:600 12.5px -apple-system,"PingFang SC",system-ui,sans-serif;color:#e6edf3;white-space:nowrap;overflow:hidden;text-overflow:ellipsis;}',
    '.xcv-chip{flex:none;font:600 11px -apple-system,"PingFang SC",system-ui,sans-serif;padding:2px 8px;border-radius:9px;background:#21262d;color:#8b949e;}',
    '.xcv-chip.on{background:rgba(46,160,67,.18);color:#3fb950;}',
    '.xcv-chip.on::before{content:"";display:inline-block;width:6px;height:6px;border-radius:50%;background:#3fb950;margin-right:5px;vertical-align:1px;animation:xcvPulse 1.4s ease-in-out infinite;}',
    '@keyframes xcvPulse{0%,100%{opacity:1}50%{opacity:.35}}',
    '.xcv-x{flex:none;width:28px;height:28px;border:0;border-radius:6px;background:none;color:#8b949e;font:18px/1 -apple-system,system-ui,sans-serif;cursor:pointer;}',
    '.xcv-x:hover{background:#21262d;color:#e6edf3;}.xcv-x:focus-visible{outline:2px solid #388bfd;outline-offset:-2px;}',
    '.xcv-body{position:relative;flex:1;min-height:0;overflow:auto;padding:12px 16px 18px;font-size:12.5px;line-height:1.6;white-space:pre-wrap;overflow-wrap:anywhere;}',
    '.xcv-b{margin:0 0 6px;}',
    '.xcv-cmd{color:#e6edf3;}.xcv-cmd .ps{color:#3fb950;font-weight:700;user-select:none;}',
    '.xcv-tool{color:#79c0ff;}.xcv-tool .lb{color:#58a6ff;opacity:.8;}',
    '.xcv-out{color:#8b949e;margin-top:-4px;}',
    '.xcv-fold{color:#6e7681;font-style:italic;}',
    '.xcv-text{color:#e6edf3;}',
    '.xcv-think{color:#8b949e;font-style:italic;}.xcv-think .lb{font-style:normal;color:#6e7681;user-select:none;}',
    '.xcv-status{color:#6e7681;}',
    '.xcv-err{color:#ff7b72;}',
    '.xcv-start{color:#d2a8ff;margin-top:10px;}',
    '.xcv-wait{color:#6e7681;}',
    '.xcv-down{position:absolute;right:16px;bottom:12px;display:none;border:0;border-radius:14px;padding:4px 12px;background:#388bfd;color:#fff;',
    'font:600 11.5px -apple-system,"PingFang SC",system-ui,sans-serif;cursor:pointer;box-shadow:0 4px 12px rgba(0,0,0,.4);}',
    '.xcv-down.show{display:block;}',
    '.xcv-pane{width:100%;height:100%;min-height:0;display:flex;flex-direction:column;background:#0d1117;color:#e6edf3;',
    'font-family:ui-monospace,"SF Mono",Menlo,Consolas,monospace;}',
    '.xcv-pane .xcv-body{padding:10px 12px 16px;font-size:12px;}',
    '.xcv-mini{width:100%;height:100%;box-sizing:border-box;overflow:hidden;padding:6px 8px;background:#0d1117;color:#c9d1d9;',
    'font:10px/14px ui-monospace,"SF Mono",Menlo,Consolas,monospace;text-align:left;display:flex;flex-direction:column;justify-content:flex-end;}',
    '.xcv-mini div{white-space:nowrap;overflow:hidden;text-overflow:ellipsis;min-height:14px;}',
    '.xcv-mini .ps{color:#3fb950;}.xcv-mini .k-out,.xcv-mini .k-status,.xcv-mini .k-wait{color:#6e7681;}',
    '.xcv-mini .k-think{color:#8b949e;font-style:italic;}.xcv-mini .k-tool{color:#79c0ff;}.xcv-mini .k-err{color:#ff7b72;}.xcv-mini .k-start{color:#d2a8ff;}',
    '.xcv-you{color:#f0b72f;margin-top:8px;}.xcv-you .ps{font-weight:700;user-select:none;}',
    '.xcv-say{flex:none;display:flex;align-items:flex-end;gap:8px;padding:10px 12px;border-top:1px solid #21262d;background:#0d1117;}',
    '.xcv-say .ps{flex:none;color:#f0b72f;font-weight:700;line-height:34px;user-select:none;}',
    '.xcv-say textarea{flex:1;min-width:0;min-height:34px;max-height:120px;box-sizing:border-box;resize:none;margin:0;padding:7px 10px;',
    'border:1px solid #30363d;border-radius:8px;background:#161b22;color:#e6edf3;font:12.5px/1.55 ui-monospace,"SF Mono",Menlo,Consolas,monospace;outline:none;}',
    '.xcv-say textarea::placeholder{color:#6e7681;}.xcv-say textarea:focus{border-color:#388bfd;box-shadow:0 0 0 3px rgba(56,139,253,.18);}',
    '.xcv-say button{flex:none;height:34px;padding:0 14px;border:0;border-radius:8px;background:#238636;color:#fff;cursor:pointer;',
    'font:600 12px -apple-system,"PingFang SC",system-ui,sans-serif;}',
    '.xcv-say button:hover{background:#2ea043;}.xcv-say button:disabled{opacity:.45;cursor:default;}',
    '.xcv-say button:focus-visible{outline:2px solid #388bfd;outline-offset:2px;}',
    '.xcv-sent{flex:none;align-self:center;font:600 11px -apple-system,"PingFang SC",system-ui,sans-serif;color:#3fb950;opacity:0;transition:opacity .2s;}',
    '.xcv-sent.show{opacity:1;}.xcv-sent.bad{color:#ff7b72;}',
    '@media (prefers-reduced-motion: reduce){.xcv-chip.on::before{animation:none;}}'
  ].join('');

  function injectStyle() {
    if (document.getElementById('xay-cli-style')) return;
    var s = document.createElement('style');
    s.id = 'xay-cli-style';
    s.textContent = CSS;
    document.head.appendChild(s);
  }

  // ------------------------------------------------------------ 一段内容 → HTML

  function lines(text) {
    return String(text || '').replace(/\s+$/, '').split('\n');
  }

  function clip(rows, max, fromEnd) {
    if (rows.length <= max) return { rows: rows, hidden: 0 };
    return fromEnd ? { rows: rows.slice(rows.length - max), hidden: rows.length - max }
                   : { rows: rows.slice(0, max), hidden: rows.length - max };
  }

  function blockHtml(b) {
    var t = mask(b.text);
    var c;
    switch (b.kind) {
      case 'cmd':
        c = clip(lines(t), CMD_LINES, false);
        return '<span class="ps">$ </span>' + esc(c.rows.join('\n')) + (c.hidden ? '\n<span class="xcv-fold">… 还有 ' + c.hidden + ' 行</span>' : '');
      case 'out':
        c = clip(lines(t), OUT_LINES, true);
        return (c.hidden ? '<span class="xcv-fold">… 前面 ' + c.hidden + ' 行</span>\n' : '') + esc(c.rows.join('\n'));
      case 'tool':
        return '<span class="lb">› ' + esc(b.label) + ' </span>' + esc(t);
      case 'think':
      case 'thinking':
        return '<span class="lb">✻ </span>' + esc(t.replace(/^\s+/, ''));
      case 'start':
        return '── ' + esc(t) + (b.ts ? '  ' + clock(b.ts) : '') + ' ──';
      case 'err':
      case 'status':
        return '# ' + esc(t);
      default:
        return esc(t.replace(/^\s+/, ''));
    }
  }

  var KIND_CLASS = { cmd: 'xcv-cmd', out: 'xcv-out', tool: 'xcv-tool', thinking: 'xcv-think', text: 'xcv-text',
                     status: 'xcv-status', err: 'xcv-err', start: 'xcv-start' };

  // 小屏：每段变成一两行，取最后 10 行
  function miniLines(blocks) {
    var out = [];
    for (var i = 0; i < blocks.length; i++) {
      var b = blocks[i];
      var rows = lines(mask(b.text)).map(function (s) { return s.trim(); }).filter(Boolean);
      if (!rows.length) continue;
      if (b.kind === 'cmd') out.push({ k: 'cmd', h: '<span class="ps">$ </span>' + esc(rows[0]) + (rows.length > 1 ? ' …' : '') });
      else if (b.kind === 'out') rows.slice(-3).forEach(function (r) { out.push({ k: 'out', h: esc(r) }); });
      else if (b.kind === 'tool') out.push({ k: 'tool', h: '› ' + esc(b.label) + ' ' + esc(rows[0]) });
      else if (b.kind === 'thinking') out.push({ k: 'think', h: '✻ ' + esc(rows[rows.length - 1]) });
      else if (b.kind === 'text') rows.slice(-2).forEach(function (r) { out.push({ k: 'text', h: esc(r) }); });
      else if (b.kind === 'start') out.push({ k: 'start', h: '── ' + esc(rows[0]) });
      else out.push({ k: b.kind, h: '# ' + esc(rows[0]) });
    }
    return out.slice(-MINI_LINES);
  }

  // ------------------------------------------------------------ 小屏

  function mini(emKey, container) {
    var stopped = false;
    var box = null;
    var sub = null;
    var queued = false;
    function paint() {
      queued = false;
      if (stopped || !box) return;
      var rows = sub ? miniLines(sub.feed.blocks) : [];
      box.innerHTML = rows.length
        ? rows.map(function (r) { return '<div class="k-' + r.k + '">' + r.h + '</div>'; }).join('')
        : '<div class="k-wait">等待输出…</div>';
    }
    function schedule() {
      if (queued || stopped) return;
      queued = true;
      raf(paint);
    }
    try {
      if (container && container.appendChild) {
        injectStyle();
        box = document.createElement('div');
        box.className = 'xcv-mini';
        box.setAttribute('aria-hidden', 'true');
        container.appendChild(box);
        if (valid(emKey)) sub = subscribe(emKey, schedule, true);
        paint();
      }
    } catch (_) { /* 画不出来就算了，不影响页面 */ }
    return {
      stop: function () {
        if (stopped) return;
        stopped = true;
        if (sub) { sub.off(); sub = null; }
        if (box && box.parentNode) box.parentNode.removeChild(box);
        box = null;
      }
    };
  }

  // ------------------------------------------------------------ 放大窗口

  var win = null;    // {em, ov, body, chip, down, sub, els, follow, timer, lastFocus}

  function labelOf(em) {
    try {
      var card = document.querySelector('[data-worker-key="' + (window.CSS && CSS.escape ? CSS.escape(em) : em) + '"]');
      var name = card && card.querySelector('.station-worker-name');
      if (name && name.textContent.trim()) return name.textContent.trim();
    } catch (_) { /* 找不到就用编号 */ }
    return em;
  }

  function onKey(e) {
    if (e.key === 'Escape' && win) {
      e.preventDefault();
      e.stopImmediatePropagation();      // 只关终端窗口，不连带关掉下面的详情栏
      close();
    }
  }

  function atBottom(el) {
    return el.scrollHeight - el.scrollTop - el.clientHeight < 24;
  }

  function syncWin() {
    if (!win) return;
    paintBlocks(win);
    paintChip();
  }

  // 把一个人的内容画进 w.body（放大窗口和看板的列共用）：w = {sub, body, down, els, waitEl, follow}
  function paintBlocks(w) {
    var blocks = w.sub.feed.blocks;
    var body = w.body;
    var stick = w.follow;
    if (!blocks.length) {
      if (!w.waitEl) {
        w.waitEl = document.createElement('div');
        w.waitEl.className = 'xcv-b xcv-wait';
        w.waitEl.textContent = '等待输出…';
        body.insertBefore(w.waitEl, w.down);
      }
    } else if (w.waitEl) {
      w.waitEl.remove();
      w.waitEl = null;
    }
    // 头上被挤掉的段
    var firstId = blocks.length ? blocks[0].id : Infinity;
    w.els.forEach(function (rec, id) {
      if (id < firstId) { rec.el.remove(); w.els.delete(id); }
    });
    for (var i = 0; i < blocks.length; i++) {
      var b = blocks[i];
      var rec = w.els.get(b.id);
      if (rec && rec.v === b.v) continue;
      if (!rec) {
        var el = document.createElement('div');
        el.className = 'xcv-b ' + (KIND_CLASS[b.kind] || 'xcv-text');
        body.insertBefore(el, w.down);
        rec = { el: el, v: 0 };
        w.els.set(b.id, rec);
      }
      rec.el.innerHTML = blockHtml(b);
      rec.v = b.v;
    }
    if (stick) body.scrollTop = body.scrollHeight;
    w.down.classList.toggle('show', !w.follow);
  }

  // ------------------------------------------------------------ 嵌在页面里的完整终端（看板的列）

  function pane(emKey, container) {
    var p = null;
    var stopped = false;
    try {
      if (container && container.appendChild) {
        injectStyle();
        var box = document.createElement('div');
        box.className = 'xcv-pane';
        box.innerHTML = '<div class="xcv-body" tabindex="0"><button type="button" class="xcv-down">↓ 最新</button></div>';
        container.appendChild(box);
        p = { box: box, body: box.querySelector('.xcv-body'), down: box.querySelector('.xcv-down'),
              els: new Map(), follow: true, waitEl: null, queued: false, sub: null };
        p.body.addEventListener('scroll', function () {
          p.follow = atBottom(p.body);
          p.down.classList.toggle('show', !p.follow);
        });
        p.down.addEventListener('click', function () {
          p.follow = true;
          p.body.scrollTop = p.body.scrollHeight;
          p.down.classList.remove('show');
        });
        if (valid(emKey)) {
          p.sub = subscribe(emKey, function () {
            if (p.queued || stopped) return;
            p.queued = true;
            raf(function () { p.queued = false; if (!stopped) paintBlocks(p); });
          }, false);
        } else {
          p.sub = { feed: { blocks: [], lastTs: 0, isLive: function () { return false; } }, off: function () {} };
        }
        paintBlocks(p);
      }
    } catch (_) { /* 画不出来就算了，不影响页面 */ }
    return {
      stop: function () {
        if (stopped) return;
        stopped = true;
        if (p && p.sub) p.sub.off();
        if (p && p.box && p.box.parentNode) p.box.parentNode.removeChild(p.box);
        p = null;
      },
      // 最近一行的时间（秒，0 = 还没有）和是不是还在直播：看板列头用
      lastTs: function () { return (p && p.sub && p.sub.feed.lastTs) || 0; },
      isLive: function () { return !!(p && p.sub && p.sub.feed.isLive()); }
    };
  }

  function cardWorking(em) {
    try {
      var card = document.querySelector('.station-card[data-worker-key="' + (window.CSS && CSS.escape ? CSS.escape(em) : em) + '"]');
      return !!card && card.getAttribute('data-state') === 'working';
    } catch (_) { return false; }
  }

  function paintChip() {
    if (!win) return;
    var live = win.sub.feed.isLive() || cardWorking(win.em);
    win.chip.className = 'xcv-chip' + (live ? ' on' : '');
    win.chip.textContent = live ? '直播中' : (win.sub.feed.lastTs ? '最近一段 · ' + clock(win.sub.feed.lastTs) : '还没有记录');
  }

  function open(emKey) {
    if (!valid(emKey)) return false;
    try {
      injectStyle();
      if (win && win.em === emKey) return true;
      var lastFocus = win ? win.lastFocus : document.activeElement;
      if (win) teardown(false);
      var ov = document.createElement('div');
      ov.className = 'xcv-ov';
      var label = labelOf(emKey);
      ov.innerHTML =
        '<div class="xcv-win" role="dialog" aria-modal="true" aria-label="' + esc(label) + ' 的屏幕">' +
          '<div class="xcv-bar"><span class="xcv-dots" aria-hidden="true"><i></i><i></i><i></i></span>' +
            '<span class="xcv-title">' + esc(label) + ' 的屏幕</span><span class="xcv-chip"></span>' +
            '<button type="button" class="xcv-x" title="关闭（Esc）" aria-label="关闭">×</button></div>' +
          '<div class="xcv-body" tabindex="0"><button type="button" class="xcv-down">↓ 最新</button></div>' +
          '<form class="xcv-say" autocomplete="off"><span class="ps" aria-hidden="true">›</span>' +
            '<textarea rows="1" maxlength="2000" aria-label="对 ' + esc(label) + ' 说" placeholder="对 ' + esc(label) +
              ' 说…（比如：记住以后提交前先跑测试）"></textarea>' +
            '<span class="xcv-sent" role="status" aria-live="polite"></span><button type="submit">发送</button></form>' +
        '</div>';
      document.body.appendChild(ov);
      var w = {
        em: emKey, ov: ov, body: ov.querySelector('.xcv-body'), chip: ov.querySelector('.xcv-chip'),
        down: ov.querySelector('.xcv-down'), els: new Map(), follow: true, waitEl: null, lastFocus: lastFocus, queued: false
      };
      win = w;
      ov.addEventListener('mousedown', function (e) { if (e.target === ov) w.downOutside = true; });
      ov.addEventListener('click', function (e) {
        if (e.target === ov && w.downOutside) close();       // 点窗口外面关闭（在窗口里按下、外面松开的拖选不算）
        w.downOutside = false;
      });
      ov.querySelector('.xcv-x').addEventListener('click', close);
      w.body.addEventListener('scroll', function () {
        w.follow = atBottom(w.body);
        w.down.classList.toggle('show', !w.follow);
      });
      w.down.addEventListener('click', function () {
        w.follow = true;
        w.body.scrollTop = w.body.scrollHeight;
        w.down.classList.remove('show');
      });
      w.sub = subscribe(emKey, function () {
        if (w.queued || win !== w) return;
        w.queued = true;
        raf(function () { w.queued = false; if (win === w) syncWin(); });
      }, true);
      w.timer = setInterval(paintChip, 5000);
      window.addEventListener('keydown', onKey, true);
      wireSay(w, label);
      syncWin();
      if (w.input && w.input.focus) w.input.focus();
      dispatchCliEvent('xay:cli-open', emKey);
      return true;
    } catch (_) {
      teardown(false);
      return false;
    }
  }

  // 页面停在哪个对话，就记在哪个对话里（他没活时会在这个对话里回你）
  function convOf(body) {
    try {
      var c = new URLSearchParams(window.location.search).get('conv');
      if (c) body.conv = c;
    } catch (_) { /* 拿不到就让服务端用他最近的对话 */ }
    return body;
  }

  function wireSay(w, label) {
    var form = w.ov.querySelector('.xcv-say');
    var input = form.querySelector('textarea');
    var btn = form.querySelector('button');
    var sent = form.querySelector('.xcv-sent');
    w.input = input;
    function fit() {
      input.style.height = 'auto';
      input.style.height = Math.min(input.scrollHeight, 120) + 'px';
    }
    function flash(text, bad) {
      sent.textContent = text;
      sent.className = 'xcv-sent show' + (bad ? ' bad' : '');
      clearTimeout(w.sentTimer);
      w.sentTimer = setTimeout(function () { sent.className = 'xcv-sent'; }, bad ? 6000 : 2500);
    }
    function echo(text) {
      var el = document.createElement('div');
      el.className = 'xcv-b xcv-you';
      el.innerHTML = '<span class="ps">你 › </span>' + esc(text);
      w.body.appendChild(el);
      w.follow = true;
      w.body.scrollTop = w.body.scrollHeight;
    }
    function send() {
      var text = input.value.trim();
      if (!text || btn.disabled) return;
      btn.disabled = true;
      var demo = demoParam();
      fetch('/api/v2/control' + (demo ? '?demo=' + encodeURIComponent(demo) : ''), {
        method: 'POST',
        headers: { 'Content-Type': 'application/json', 'X-Xay-Action': 'control' },
        body: JSON.stringify(convOf({ em: w.em, action: 'steer', text: text }))
      }).then(function (res) {
        if (!res.ok) throw new Error('HTTP ' + res.status);
        echo(text);
        input.value = '';
        fit();
        flash('已送出');
      }).catch(function () {
        flash('没送出去，再试一次', true);
      }).then(function () {
        btn.disabled = false;
        if (win === w) input.focus();
      });
    }
    input.addEventListener('input', fit);
    input.addEventListener('keydown', function (e) {
      if (e.key === 'Enter' && !e.shiftKey && !e.isComposing && e.keyCode !== 229) {   // 中文输入法选字时的回车不算
        e.preventDefault();
        send();
      }
    });
    form.addEventListener('submit', function (e) { e.preventDefault(); send(); });
  }

  function teardown(restore) {
    var w = win;
    if (!w) return;
    win = null;
    var em = w.em;
    if (w.sub) w.sub.off();
    clearInterval(w.timer);
    clearTimeout(w.sentTimer);
    if (w.ov && w.ov.parentNode) w.ov.parentNode.removeChild(w.ov);
    window.removeEventListener('keydown', onKey, true);
    if (restore && w.lastFocus && w.lastFocus.focus && document.contains(w.lastFocus)) {
      try { w.lastFocus.focus(); } catch (_) { /* 焦点还不回去也没关系 */ }
    }
    dispatchCliEvent('xay:cli-close', em);
  }

  function close() {
    teardown(true);
  }

  window.XayCliView = {
    open: open,
    mini: mini,
    close: close,
    pane: pane,
    isOpen: function () { return !!win; }
  };
})();
