"""页面实时推送防占满连接与去重测试（tk_9990c4ee）。

验证：
1. 标签藏到后台（document.visibilityState === 'hidden'）超过 10 秒，关掉 EventSource；回到前台立刻重新连上。
   覆盖：
   - api.js 的 connectEventStream（总推送，重连带 since）
   - api.js 的 connectLive（详情栏直播，重连带 tail）
   - cli-view.js 的 Feed
   - board.js 的 connect
2. 直播重连后不能重复显示：按 n 去重（n <= maxN 丢弃）。
   - api.js 的 connectLive
   - cli-view.js 的 Feed
3. "看他的屏幕"放大窗口打开时，如果左侧详情栏正看着同一个员工，详情栏直播（connectLive）先断开；
   窗口关上再连回来。
"""
import json
import os
import shutil
import subprocess
import unittest
from pathlib import Path

WEB = Path(__file__).resolve().parents[1] / "web"


@unittest.skipUnless(shutil.which("node"), "没装 node，跳过页面模块 SSE 测试")
class SseConnectionTests(unittest.TestCase):
    def run_node(self, script):
        res = subprocess.run(
            ["node", "--input-type=module", "-e", script],
            capture_output=True,
            text=True,
            timeout=10,
            cwd=str(WEB / "lib"),
        )
        self.assertEqual(res.returncode, 0, f"Node 执行失败：\nstdout: {res.stdout}\nstderr: {res.stderr}")
        return json.loads(res.stdout)

    def test_connect_event_stream_visibility(self):
        """connectEventStream 藏到后台 10 秒关掉，回前台重连带 since。"""
        out = self.run_node("""
let timers = [];
let nextId = 1;
const realSetTimeout = globalThis.setTimeout;
const realClearTimeout = globalThis.clearTimeout;

globalThis.setTimeout = (fn, delay, ...args) => {
  if (delay >= 10000) {
    const id = nextId++;
    timers.push({ id, fn, delay, args });
    return id;
  }
  return realSetTimeout(fn, delay, ...args);
};
globalThis.clearTimeout = (id) => {
  timers = timers.filter(t => t.id !== id);
  realClearTimeout(id);
};

function advanceTime(ms) {
  const due = timers.filter(t => t.delay <= ms);
  timers = timers.filter(t => t.delay > ms);
  due.forEach(t => t.fn(...t.args));
}

class FakeEventSource {
  static instances = [];
  constructor(url) {
    this.url = url;
    this.closed = false;
    this.listeners = {};
    FakeEventSource.instances.push(this);
  }
  addEventListener(ev, fn) {
    (this.listeners[ev] = this.listeners[ev] || []).push(fn);
  }
  removeEventListener() {}
  emit(ev, data, lastEventId) {
    const fns = this.listeners[ev] || [];
    fns.forEach(fn => fn({ data: JSON.stringify(data), lastEventId: lastEventId || '' }));
  }
  close() {
    this.closed = true;
  }
}
globalThis.EventSource = FakeEventSource;

const docListeners = {};
globalThis.document = {
  visibilityState: 'visible',
  addEventListener(ev, fn) {
    (docListeners[ev] = docListeners[ev] || []).push(fn);
  },
  removeEventListener(ev, fn) {
    const list = docListeners[ev] || [];
    const idx = list.indexOf(fn);
    if (idx >= 0) list.splice(idx, 1);
  },
  triggerVisibility(state) {
    this.visibilityState = state;
    (docListeners['visibilitychange'] || []).forEach(fn => fn());
  }
};

globalThis.location = { search: '', pathname: '/' };
globalThis.window = {
  location: globalThis.location
};

const { connectEventStream } = await import('./api.js');

let reloaded = 0;
const stop = connectEventStream({
  onReload: () => { reloaded++; },
  getSinceSeq: () => 10
});

const es1 = FakeEventSource.instances[0];
const url1 = es1.url;

// 模拟收到 ledger 事件，更新 seq 为 25
es1.emit('ledger', { seq: 25, text: 'hello' }, '25');

// 切到后台 5 秒（不到 10 秒），不关
document.triggerVisibility('hidden');
advanceTime(5000);
const closedAt5s = es1.closed;

// 切回前台，取消关闭
document.triggerVisibility('visible');
advanceTime(10000);
const closedAfterVisible = es1.closed;

// 再次切后台并满 10 秒，ES1 关闭
document.triggerVisibility('hidden');
advanceTime(10000);
const closedAt10s = es1.closed;

// 回到前台，应该立刻创建 ES2，且 since 应为 25
document.triggerVisibility('visible');
const es2 = FakeEventSource.instances[1];
const url2 = es2 ? es2.url : '';

stop();
const closedAfterStop = es2 ? es2.closed : false;

console.log(JSON.stringify({
  url1,
  closedAt5s,
  closedAfterVisible,
  closedAt10s,
  hasEs2: !!es2,
  url2,
  closedAfterStop
}));
process.exit(0);
""")
        self.assertIn("since=10", out["url1"])
        self.assertFalse(out["closedAt5s"])
        self.assertFalse(out["closedAfterVisible"])
        self.assertTrue(out["closedAt10s"])
        self.assertTrue(out["hasEs2"])
        self.assertIn("since=25", out["url2"])
        self.assertTrue(out["closedAfterStop"])

    def test_connect_live_visibility_and_dedup(self):
        """connectLive 藏到后台 10 秒关掉，回前台重连带 tail，按递增 n 去重。"""
        out = self.run_node("""
let timers = [];
let nextId = 1;
const realSetTimeout = globalThis.setTimeout;
const realClearTimeout = globalThis.clearTimeout;

globalThis.setTimeout = (fn, delay, ...args) => {
  if (delay >= 10000) {
    const id = nextId++;
    timers.push({ id, fn, delay, args });
    return id;
  }
  return realSetTimeout(fn, delay, ...args);
};
globalThis.clearTimeout = (id) => {
  timers = timers.filter(t => t.id !== id);
  realClearTimeout(id);
};

function advanceTime(ms) {
  const due = timers.filter(t => t.delay <= ms);
  timers = timers.filter(t => t.delay > ms);
  due.forEach(t => t.fn(...t.args));
}

class FakeEventSource {
  static instances = [];
  constructor(url) {
    this.url = url;
    this.closed = false;
    this.listeners = {};
    FakeEventSource.instances.push(this);
  }
  addEventListener(ev, fn) {
    (this.listeners[ev] = this.listeners[ev] || []).push(fn);
  }
  removeEventListener() {}
  emit(ev, data) {
    const fns = this.listeners[ev] || [];
    fns.forEach(fn => fn({ data: JSON.stringify(data) }));
  }
  close() {
    this.closed = true;
  }
}
globalThis.EventSource = FakeEventSource;

const docListeners = {};
globalThis.document = {
  visibilityState: 'visible',
  addEventListener(ev, fn) {
    (docListeners[ev] = docListeners[ev] || []).push(fn);
  },
  removeEventListener(ev, fn) {
    const list = docListeners[ev] || [];
    const idx = list.indexOf(fn);
    if (idx >= 0) list.splice(idx, 1);
  },
  triggerVisibility(state) {
    this.visibilityState = state;
    (docListeners['visibilitychange'] || []).forEach(fn => fn());
  }
};
globalThis.location = { search: '', pathname: '/' };
globalThis.window = { location: globalThis.location };

const { connectLive } = await import('./api.js');

const received = [];
const stop = connectLive('em_001', (item) => {
  received.push(item);
});

const es1 = FakeEventSource.instances[0];
const url1 = es1.url;

// 喂数据 n=1, n=2
es1.emit('live', { n: 1, kind: 'thinking', text: 'think 1' });
es1.emit('live', { n: 2, kind: 'thinking', text: 'think 2' });

// 喂重复数据 n=2, n=1（小于等于已收到的最大 n），应被丢弃
es1.emit('live', { n: 2, kind: 'thinking', text: 'think 2 dup' });
es1.emit('live', { n: 1, kind: 'thinking', text: 'think 1 dup' });

// 后台 10 秒断开
document.triggerVisibility('hidden');
advanceTime(10000);
const es1Closed = es1.closed;

// 回到前台，重新 new 出来带 tail=200
document.triggerVisibility('visible');
const es2 = FakeEventSource.instances[1];
const url2 = es2 ? es2.url : '';

// 模拟重连后重发了 n=2 和新行 n=3
es2.emit('live', { n: 2, kind: 'thinking', text: 'think 2 again' });
es2.emit('live', { n: 3, kind: 'thinking', text: 'think 3' });

stop();
const es2Closed = es2 ? es2.closed : false;

console.log(JSON.stringify({
  url1,
  es1Closed,
  hasEs2: !!es2,
  url2,
  es2Closed,
  receivedNs: received.map(r => r.n)
}));
process.exit(0);
""")
        self.assertNotIn("tail=", out["url1"])
        self.assertTrue(out["es1Closed"])
        self.assertTrue(out["hasEs2"])
        self.assertIn("tail=200", out["url2"])
        self.assertTrue(out["es2Closed"])
        self.assertEqual(out["receivedNs"], [1, 2, 3])

    def test_cli_view_feed_and_events(self):
        """cli-view.js 的 Feed 支持后台 10s 断开、重连、n 去重，且放大窗口派发事件。"""
        out = self.run_node("""
import fs from 'fs';
import path from 'path';

let timers = [];
let nextId = 1;
const realSetTimeout = globalThis.setTimeout;
const realClearTimeout = globalThis.clearTimeout;

globalThis.setTimeout = (fn, delay, ...args) => {
  if (delay >= 10000) {
    const id = nextId++;
    timers.push({ id, fn, delay, args });
    return id;
  }
  return realSetTimeout(fn, delay, ...args);
};
globalThis.clearTimeout = (id) => {
  timers = timers.filter(t => t.id !== id);
  realClearTimeout(id);
};

function advanceTime(ms) {
  const due = timers.filter(t => t.delay <= ms);
  timers = timers.filter(t => t.delay > ms);
  due.forEach(t => t.fn(...t.args));
}

class FakeEventSource {
  static instances = [];
  constructor(url) {
    this.url = url;
    this.closed = false;
    this.listeners = {};
    FakeEventSource.instances.push(this);
  }
  addEventListener(ev, fn) {
    (this.listeners[ev] = this.listeners[ev] || []).push(fn);
  }
  removeEventListener() {}
  emit(ev, data) {
    const fns = this.listeners[ev] || [];
    fns.forEach(fn => fn({ data: JSON.stringify(data) }));
  }
  close() {
    this.closed = true;
  }
}
globalThis.EventSource = FakeEventSource;

function mockElement(tag = 'div') {
  const children = [];
  return {
    tagName: tag.toUpperCase(),
    innerHTML: '',
    textContent: '',
    className: '',
    style: {},
    dataset: {},
    classList: { toggle() {}, add() {}, remove() {}, contains() { return false; } },
    setAttribute() {},
    getAttribute() { return null; },
    appendChild(c) { children.push(c); return c; },
    removeChild(c) {
      const idx = children.indexOf(c);
      if (idx >= 0) children.splice(idx, 1);
      return c;
    },
    remove() {},
    insertBefore(c) { children.push(c); return c; },
    addEventListener() {},
    removeEventListener() {},
    querySelector(sel) { return mockElement('div'); },
    querySelectorAll() { return []; },
    focus() {}
  };
}

const docListeners = {};
globalThis.document = {
  visibilityState: 'visible',
  head: mockElement('head'),
  body: mockElement('body'),
  getElementById() { return null; },
  createElement(tag) { return mockElement(tag); },
  addEventListener(ev, fn) {
    (docListeners[ev] = docListeners[ev] || []).push(fn);
  },
  removeEventListener(ev, fn) {
    const list = docListeners[ev] || [];
    const idx = list.indexOf(fn);
    if (idx >= 0) list.splice(idx, 1);
  },
  triggerVisibility(state) {
    this.visibilityState = state;
    (docListeners['visibilitychange'] || []).forEach(fn => fn());
  }
};

const winListeners = {};
globalThis.location = { search: '', pathname: '/' };
globalThis.window = {
  location: globalThis.location,
  addEventListener(ev, fn) {
    (winListeners[ev] = winListeners[ev] || []).push(fn);
  },
  removeEventListener(ev, fn) {
    const list = winListeners[ev] || [];
    const idx = list.indexOf(fn);
    if (idx >= 0) list.splice(idx, 1);
  },
  dispatchEvent(ev) {
    (winListeners[ev.type] || []).forEach(fn => fn(ev));
  },
  requestAnimationFrame(fn) { fn(); return 1; }
};

globalThis.CustomEvent = class CustomEvent {
  constructor(type, init) {
    this.type = type;
    this.detail = (init && init.detail) || null;
  }
};

// 载入普通脚本 cli-view.js
const code = fs.readFileSync(path.resolve('./cli-view.js'), 'utf-8');
eval(code);

const firedEvents = [];
window.addEventListener('xay:cli-open', e => firedEvents.push({ type: 'open', em: e.detail.em }));
window.addEventListener('xay:cli-close', e => firedEvents.push({ type: 'close', em: e.detail.em }));

// 测试 Feed 与 mini
const container = document.createElement('div');
const m = window.XayCliView.mini('em_worker1', container);
const es1 = FakeEventSource.instances[0];

// 喂数据 n=10, 11, 以及重复的 11
es1.emit('live', { n: 10, kind: 'text', text: 'line 10' });
es1.emit('live', { n: 11, kind: 'text', text: 'line 11' });
es1.emit('live', { n: 11, kind: 'text', text: 'line 11 duplicate' });
es1.emit('live', { n: 12, kind: 'text', text: 'line 12' });

// 后台 10 秒关闭
document.triggerVisibility('hidden');
advanceTime(10000);
const es1Closed = es1.closed;

// 回前台重新连接
document.triggerVisibility('visible');
const es2 = FakeEventSource.instances[1];
const hasEs2 = !!es2;

m.stop();
const es2Closed = es2 ? es2.closed : false;

// 测试打开 / 关闭放大窗口事件
window.XayCliView.open('em_worker1');
window.XayCliView.close();

console.log(JSON.stringify({
  es1Closed,
  hasEs2,
  es2Closed,
  firedEvents
}));
process.exit(0);
""")
        self.assertTrue(out["es1Closed"])
        self.assertTrue(out["hasEs2"])
        self.assertTrue(out["es2Closed"])
        self.assertEqual(out["firedEvents"], [
            {"type": "open", "em": "em_worker1"},
            {"type": "close", "em": "em_worker1"},
        ])

    def test_board_connect_visibility(self):
        """board.js 的 connect 在后台 10 秒关闭，回到前台重新连接。"""
        out = self.run_node("""
import fs from 'fs';
import path from 'path';

let timers = [];
let nextId = 1;
const realSetTimeout = globalThis.setTimeout;
const realClearTimeout = globalThis.clearTimeout;

globalThis.setTimeout = (fn, delay, ...args) => {
  if (delay >= 10000) {
    const id = nextId++;
    timers.push({ id, fn, delay, args });
    return id;
  }
  return realSetTimeout(fn, delay, ...args);
};
globalThis.clearTimeout = (id) => {
  timers = timers.filter(t => t.id !== id);
  realClearTimeout(id);
};

function advanceTime(ms) {
  const due = timers.filter(t => t.delay <= ms);
  timers = timers.filter(t => t.delay > ms);
  due.forEach(t => t.fn(...t.args));
}

class FakeEventSource {
  static instances = [];
  constructor(url) {
    this.url = url;
    this.closed = false;
    this.listeners = {};
    FakeEventSource.instances.push(this);
  }
  addEventListener(ev, fn) {
    (this.listeners[ev] = this.listeners[ev] || []).push(fn);
  }
  removeEventListener() {}
  emit(ev, data, lastEventId) {
    const fns = this.listeners[ev] || [];
    fns.forEach(fn => fn({ data: JSON.stringify(data), lastEventId: lastEventId || '' }));
  }
  close() {
    this.closed = true;
  }
}
globalThis.EventSource = FakeEventSource;

function mockElement(tag = 'div') {
  const children = [];
  return {
    tagName: tag.toUpperCase(),
    id: '',
    innerHTML: '',
    textContent: '',
    className: '',
    dataset: {},
    classList: { toggle() {}, add() {}, remove() {}, contains() { return false; } },
    setAttribute() {},
    getAttribute() { return 'dark'; },
    appendChild(c) { children.push(c); return c; },
    insertBefore(c) { children.push(c); return c; },
    removeChild() {},
    addEventListener() {},
    removeEventListener() {},
    querySelector() { return null; },
    querySelectorAll() { return []; }
  };
}

const docListeners = {};
globalThis.document = {
  visibilityState: 'visible',
  head: mockElement('head'),
  body: mockElement('body'),
  documentElement: mockElement('html'),
  title: '',
  getElementById(id) {
    const el = mockElement('div');
    el.id = id;
    return el;
  },
  createElement(tag) { return mockElement(tag); },
  addEventListener(ev, fn) {
    (docListeners[ev] = docListeners[ev] || []).push(fn);
  },
  removeEventListener(ev, fn) {
    const list = docListeners[ev] || [];
    const idx = list.indexOf(fn);
    if (idx >= 0) list.splice(idx, 1);
  },
  triggerVisibility(state) {
    this.visibilityState = state;
    (docListeners['visibilitychange'] || []).forEach(fn => fn());
  }
};

globalThis.location = { search: '', pathname: '/' };
globalThis.window = {
  location: globalThis.location,
  innerWidth: 1200,
  addEventListener() {},
  removeEventListener() {}
};
globalThis.fetch = async () => ({
  ok: true,
  json: async () => ({ convs: [], tasks: [], people: {} })
});

// 动态载入 board.js
await import('./board.js');

// board.js 在 init 时会异步 loadSnapshot().then(connect)
await new Promise(r => realSetTimeout(r, 20));

const es1 = FakeEventSource.instances[0];
const hasEs1 = !!es1;

// 模拟更新 seq 为 42
if (es1) {
  es1.emit('ledger', {}, '42');
}

// 切到后台 10 秒
document.triggerVisibility('hidden');
advanceTime(10000);
const es1Closed = es1 ? es1.closed : false;

// 切回前台
document.triggerVisibility('visible');
const es2 = FakeEventSource.instances[1];
const hasEs2 = !!es2;
const url2 = es2 ? es2.url : '';

console.log(JSON.stringify({
  hasEs1,
  es1Closed,
  hasEs2,
  url2
}));
process.exit(0);
""")
        self.assertTrue(out["hasEs1"])
        self.assertTrue(out["es1Closed"])
        self.assertTrue(out["hasEs2"])
        self.assertIn("since=42", out["url2"])

    def test_employee_panel_cli_window_mutual_exclusion(self):
        """放大窗口打开时，如果左侧详情栏正看着同一个员工，详情栏自己的直播断开；窗口关上再连回来。"""
        out = self.run_node("""
let timers = [];
let nextId = 1;
const realSetTimeout = globalThis.setTimeout;
const realClearTimeout = globalThis.clearTimeout;

globalThis.setTimeout = (fn, delay, ...args) => {
  if (delay >= 10000) {
    const id = nextId++;
    timers.push({ id, fn, delay, args });
    return id;
  }
  return realSetTimeout(fn, delay, ...args);
};
globalThis.clearTimeout = (id) => {
  timers = timers.filter(t => t.id !== id);
  realClearTimeout(id);
};

globalThis.Image = class FakeImage {
  constructor() {
    this.classList = { add() {}, remove() {} };
    this.dataset = {};
  }
  addEventListener() {}
  remove() {}
  set src(v) {}
};

class FakeEventSource {
  static instances = [];
  constructor(url) {
    this.url = url;
    this.closed = false;
    this.listeners = {};
    FakeEventSource.instances.push(this);
  }
  addEventListener(ev, fn) {
    (this.listeners[ev] = this.listeners[ev] || []).push(fn);
  }
  removeEventListener() {}
  close() {
    this.closed = true;
  }
}
globalThis.EventSource = FakeEventSource;

function mockElement(tag = 'div') {
  const children = [];
  return {
    tagName: tag.toUpperCase(),
    id: '',
    innerHTML: '',
    textContent: '',
    className: '',
    style: {},
    dataset: {},
    classList: { toggle() {}, add() {}, remove() {}, contains() { return false; } },
    setAttribute() {},
    getAttribute() { return null; },
    appendChild(c) { children.push(c); return c; },
    removeChild(c) {
      const idx = children.indexOf(c);
      if (idx >= 0) children.splice(idx, 1);
      return c;
    },
    remove() {},
    insertBefore(c) { children.push(c); return c; },
    insertAdjacentElement(pos, el) { children.push(el); return el; },
    addEventListener() {},
    removeEventListener() {},
    querySelector(sel) { return mockElement('div'); },
    querySelectorAll() { return []; },
    focus() {}
  };
}

const elMap = new Map();
function getMockEl(id) {
  if (elMap.has(id)) return elMap.get(id);
  const el = mockElement('div');
  el.id = id;
  elMap.set(id, el);
  return el;
}

globalThis.document = {
  visibilityState: 'visible',
  head: mockElement('head'),
  body: mockElement('body'),
  getElementById: getMockEl,
  createElement(tag) { return mockElement(tag); },
  addEventListener() {},
  removeEventListener() {}
};

const winListeners = {};
globalThis.location = { search: '', pathname: '/' };
globalThis.window = {
  location: globalThis.location,
  addEventListener(ev, fn) {
    (winListeners[ev] = winListeners[ev] || []).push(fn);
  },
  removeEventListener(ev, fn) {
    const list = winListeners[ev] || [];
    const idx = list.indexOf(fn);
    if (idx >= 0) list.splice(idx, 1);
  },
  dispatchEvent(ev) {
    (winListeners[ev.type] || []).forEach(fn => fn(ev));
  },
  requestAnimationFrame(fn) { fn(); return 1; }
};

let currentSnapshot = {
  people: {
    em_same: { agent: 'codex', state: 'working', label: 'Codex 1' },
    em_other: { agent: 'dsh', state: 'working', label: 'DeepSeek 1' }
  }
};

globalThis.fetch = async (url) => {
  return {
    ok: true,
    json: async () => ({
      person: { agent: 'codex', state: 'working', label: 'Codex 1' },
      timeline: []
    })
  };
};

const { initEmployeePanel, openEmployee } = await import('./employee-panel.js');

initEmployeePanel({ snapshot: () => currentSnapshot });

// 打开详情栏 em_same
openEmployee('em_same');
await new Promise(r => realSetTimeout(r, 20));

const es1 = FakeEventSource.instances[0];
const es1Url = es1 ? es1.url : '';
const es1ClosedInitially = es1 ? es1.closed : false;

// 派发 xay:cli-open，打开另一个人 em_other 的放大窗口：em_same 不应受影响
window.dispatchEvent({ type: 'xay:cli-open', detail: { em: 'em_other' } });
const es1ClosedForOther = es1 ? es1.closed : false;

// 派发 xay:cli-open，打开 em_same 的放大窗口：详情栏直播应立即断开！
window.dispatchEvent({ type: 'xay:cli-open', detail: { em: 'em_same' } });
const es1ClosedForSame = es1 ? es1.closed : false;

// 派发 xay:cli-close，关上放大窗口：详情栏直播应重新连接！
window.dispatchEvent({ type: 'xay:cli-close', detail: { em: 'em_same' } });
const es2 = FakeEventSource.instances[1];
const hasEs2 = !!es2;
const es2Closed = es2 ? es2.closed : false;

console.log(JSON.stringify({
  hasEs1: !!es1,
  es1Url,
  es1ClosedInitially,
  es1ClosedForOther,
  es1ClosedForSame,
  hasEs2,
  es2Closed
}));
process.exit(0);
""")
        self.assertTrue(out["hasEs1"])
        self.assertIn("em=em_same", out["es1Url"])
        self.assertFalse(out["es1ClosedInitially"])
        self.assertFalse(out["es1ClosedForOther"])
        self.assertTrue(out["es1ClosedForSame"])
        self.assertTrue(out["hasEs2"])
        self.assertFalse(out["es2Closed"])


if __name__ == "__main__":
    unittest.main()
