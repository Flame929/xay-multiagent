// api.js - V2 数据层：快照获取、SSE 事件流、防抖刷新、3次断线轮询降级、决策提交
export function getUrlParams() {
  const sp = new URLSearchParams(window.location.search);
  return {
    ws: sp.get('ws') || '',
    conv: sp.get('conv') || sp.get('conversation') || '',
    demo: sp.get('demo') || '',
    snapshot: sp.get('snapshot') || ''
  };
}

// 最近一次拿到的快照所属的工作区和对话；POST 没有在网址里指定时用它，免得服务端默认落到"最近开的那个对话"
let snapshotContext = { ws: '', conv: '' };

function rememberContext(snap) {
  if (!snap || typeof snap !== 'object') return snap;
  snapshotContext = {
    ws: (snap.ws && snap.ws.id) || '',
    conv: (snap.conversation && snap.conversation.id) || ''
  };
  return snap;
}

/** 当前正在看的 ws / conv：网址里写了就用网址的，否则用最近一次快照里的。 */
export function currentContext() {
  const p = getUrlParams();
  return { ws: p.ws || snapshotContext.ws, conv: p.conv || snapshotContext.conv };
}

export async function fetchSnapshot(params = getUrlParams()) {
  // 1. 本地/指定静态快照调试模式
  if (params.snapshot) {
    const res = await fetch(params.snapshot);
    if (!res.ok) {
      throw new Error(`无法读取快照文件 ${params.snapshot}：HTTP ${res.status} ${res.statusText}`);
    }
    return rememberContext(await res.json());
  }

  // 2. 真实 V2 服务端接口
  const qs = new URLSearchParams();
  if (params.ws) qs.set('ws', params.ws);
  if (params.conv) qs.set('conv', params.conv);
  if (params.demo) qs.set('demo', params.demo);

  const queryStr = qs.toString();
  const url = '/api/v2/snapshot' + (queryStr ? '?' + queryStr : '');

  const res = await fetch(url);
  if (!res.ok) {
    let errDetail = '';
    try {
      const errJson = await res.json();
      if (errJson && errJson.detail) errDetail = `: ${errJson.detail}`;
    } catch (_) {}
    throw new Error(`服务端响应异常 HTTP ${res.status} (${res.statusText})${errDetail}`);
  }
  return rememberContext(await res.json());
}

/** 某位员工的详情（左侧面板）：做过的每件事 + 大白话步骤 + 结果。失败时抛出带中文原因的错误。 */
export async function fetchEmployee(em) {
  const ctx = currentContext();
  const demo = getUrlParams().demo;
  const qs = new URLSearchParams({ em });
  if (ctx.conv) qs.set('conv', ctx.conv);
  if (demo) qs.set('demo', demo);
  const res = await fetch('/api/v2/employee?' + qs.toString());
  if (!res.ok) {
    let detail = '';
    try {
      const j = await res.json();
      detail = j.detail || j.error || '';
    } catch (_) {}
    throw new Error(detail || `服务端响应异常 HTTP ${res.status}`);
  }
  return await res.json();
}

/**
 * 直播通道：GET /api/v2/live?em=&plain=1（SSE，事件名 live / heartbeat）。
 * plain=1 让服务端给每一行多带一句大白话 plain；页面只显示这一句，不显示原始命令。
 * 返回关闭函数。浏览器自己会断线重连（带 Last-Event-ID，不会重复）。
 */
export function connectLive(em, onItem) {
  if (typeof EventSource === 'undefined') return () => {};
  let es = null;
  let hiddenTimer = null;
  let isClosed = false;
  let maxN = 0;
  let maxSeg = 0;

  const connect = () => {
    if (isClosed) return;
    const qs = new URLSearchParams({ em, plain: '1' });
    const demo = getUrlParams().demo;
    if (demo) qs.set('demo', demo);
    if (maxN > 0) {
      qs.set('tail', '200');
    }
    try {
      es = new EventSource('/api/v2/live?' + qs.toString());
      es.addEventListener('live', (e) => {
        try {
          const row = JSON.parse(e.data);
          if (row && typeof row === 'object') {
            // 按 (段号, 编号) 去重：重连补发的旧行不重复；换了新的一段（交接后新对话）编号从 1 重新数
            const seg = typeof row.seg === 'number' ? row.seg : 0;
            const n = typeof row.n === 'number' ? row.n : 0;
            if (seg < maxSeg) return;
            if (seg > maxSeg) { maxSeg = seg; maxN = 0; }
            if (n && n <= maxN) return;
            if (n > maxN) maxN = n;
            onItem(row);
          }
        } catch (_) {}
      });
    } catch (_) {
      es = null;
    }
  };

  const onVisibilityChange = () => {
    if (isClosed) return;
    if (typeof document === 'undefined') return;
    if (document.visibilityState === 'hidden') {
      clearTimeout(hiddenTimer);
      hiddenTimer = setTimeout(() => {
        if (isClosed) return;
        if (es) {
          es.close();
          es = null;
        }
      }, 10000);
    } else {
      clearTimeout(hiddenTimer);
      if (!isClosed && !es) {
        connect();
      }
    }
  };

  if (typeof document !== 'undefined' && document.addEventListener) {
    document.addEventListener('visibilitychange', onVisibilityChange);
  }

  connect();

  return () => {
    isClosed = true;
    clearTimeout(hiddenTimer);
    if (typeof document !== 'undefined' && document.removeEventListener) {
      document.removeEventListener('visibilitychange', onVisibilityChange);
    }
    if (es) {
      es.close();
      es = null;
    }
  };
}

/**
 * 所有写操作的统一出口：POST /api/v2/<path>，带上对应的 X-Xay-Action 头，
 * 并把当前的 ws / conv 一起发给服务端（示例模式下网址带 demo，服务端就写到示例账本）。
 * 失败时抛出带中文原因的错误。
 */
export async function postAction(path, action, payload = {}, failLabel = '操作失败') {
  const ctx = currentContext();
  const body = { ...payload };
  if (ctx.ws && body.ws === undefined) body.ws = ctx.ws;
  if (ctx.conv && body.conv === undefined) body.conv = ctx.conv;
  const demo = getUrlParams().demo;
  const url = '/api/v2/' + path + (demo ? '?demo=' + encodeURIComponent(demo) : '');

  const res = await fetch(url, {
    method: 'POST',
    headers: {
      'Content-Type': 'application/json',
      'X-Xay-Action': action
    },
    body: JSON.stringify(body)
  });

  if (!res.ok) {
    let detail = '';
    try {
      const j = await res.json();
      detail = j.detail || j.error || '';
    } catch (_) {}
    throw new Error(`${failLabel} HTTP ${res.status}${detail ? ': ' + detail : ''}`);
  }
  return await res.json();
}

export async function postDecision(id, option, text = '') {
  return postAction('decision', 'decide', { id, option, text }, '提交决议失败');
}

/**
 * 建立与服务端的实时流通讯
 * - 收到 ledger 事件：300ms 防抖触发 reloadSnapshot，并回调 onLedgerEvent（放动画）
 * - 收到 changed 事件：同一条 300ms 防抖重拉快照，但不回调 onLedgerEvent（不放动画）
 * - SSE 断线连续 3 次后自动断开 EventSource，降级为每 3 秒轮询
 */
export function connectEventStream({
  onReload,
  onLedgerEvent,
  onStatusChange,
  getSinceSeq
}) {
  const params = getUrlParams();
  if (params.snapshot) {
    // 静态文件预览模式，不需要连接 SSE
    if (onStatusChange) onStatusChange('snapshot_mode');
    return () => {};
  }

  let es = null;
  let pollTimer = null;
  let debounceTimer = null;
  let disconnectCount = 0;
  let isPollingFallback = false;
  let hiddenTimer = null;
  let isClosed = false;
  let lastSeq = 0;

  const triggerDebouncedReload = (ledgerData) => {
    clearTimeout(debounceTimer);
    debounceTimer = setTimeout(() => {
      if (onReload) onReload();
    }, 300);

    if (onLedgerEvent && ledgerData) {
      onLedgerEvent(ledgerData);
    }
  };

  const startPolling = () => {
    if (pollTimer) return;
    isPollingFallback = true;
    if (onStatusChange) onStatusChange('polling_fallback');
    pollTimer = setInterval(() => {
      if (onReload) onReload();
    }, 3000);
  };

  const connectSSE = () => {
    if (isClosed || isPollingFallback) return;

    const qs = new URLSearchParams();
    if (params.ws) qs.set('ws', params.ws);
    if (params.conv) qs.set('conv', params.conv);
    if (params.demo) qs.set('demo', params.demo);
    const sinceFromCaller = getSinceSeq ? getSinceSeq() : 0;
    const since = Math.max(sinceFromCaller || 0, lastSeq || 0);
    if (since) qs.set('since', String(since));

    const sseUrl = '/api/v2/stream' + (qs.toString() ? '?' + qs.toString() : '');

    try {
      es = new EventSource(sseUrl);

      es.onopen = () => {
        disconnectCount = 0;
        if (onStatusChange) onStatusChange('connected');
      };

      const bumpSeq = (e) => {
        const n = Number(e.lastEventId || 0);
        if (n) lastSeq = Math.max(lastSeq, n);
      };

      es.addEventListener('ledger', (e) => {
        bumpSeq(e);
        let parsed = null;
        try {
          parsed = JSON.parse(e.data);
        } catch (_) {
          parsed = { text: e.data };
        }
        if (parsed && typeof parsed.seq === 'number') {
          lastSeq = Math.max(lastSeq, parsed.seq);
        }
        triggerDebouncedReload(parsed);
      });

      // 没有大白话的技术事件：只重拉快照，不放动画
      es.addEventListener('changed', (e) => {
        bumpSeq(e);
        disconnectCount = 0;
        let parsed = null;
        try {
          parsed = JSON.parse(e.data);
        } catch (_) {}
        if (parsed && typeof parsed.seq === 'number') {
          lastSeq = Math.max(lastSeq, parsed.seq);
        }
        triggerDebouncedReload(null);
      });

      es.addEventListener('heartbeat', () => {
        disconnectCount = 0;
      });

      es.onerror = () => {
        disconnectCount++;
        if (disconnectCount >= 3) {
          if (es) {
            es.close();
            es = null;
          }
          startPolling();
        } else {
          if (onStatusChange) onStatusChange('reconnecting');
        }
      };
    } catch (err) {
      disconnectCount++;
      if (disconnectCount >= 3) {
        startPolling();
      }
    }
  };

  const onVisibilityChange = () => {
    if (isClosed) return;
    if (typeof document === 'undefined') return;
    if (document.visibilityState === 'hidden') {
      clearTimeout(hiddenTimer);
      hiddenTimer = setTimeout(() => {
        if (isClosed || isPollingFallback) return;
        if (es) {
          es.close();
          es = null;
          if (onStatusChange) onStatusChange('paused');
        }
      }, 10000);
    } else {
      clearTimeout(hiddenTimer);
      if (!isClosed && !isPollingFallback && !es) {
        disconnectCount = 0;
        connectSSE();
      }
    }
  };

  if (typeof document !== 'undefined' && document.addEventListener) {
    document.addEventListener('visibilitychange', onVisibilityChange);
  }

  connectSSE();

  return () => {
    isClosed = true;
    clearTimeout(hiddenTimer);
    if (typeof document !== 'undefined' && document.removeEventListener) {
      document.removeEventListener('visibilitychange', onVisibilityChange);
    }
    clearTimeout(debounceTimer);
    if (pollTimer) {
      clearInterval(pollTimer);
      pollTimer = null;
    }
    if (es) {
      es.close();
      es = null;
    }
  };
}
