"""示例模式（?demo=1）：把 V2/fixtures/demo.events.jsonl 一条条回放进一个内存里的临时账本。

要点（协议 §8）：
- 临时账本用 core 的 open_db(":memory:")，和真实账本走同一套 append / snapshot / stream 代码，只是换了连接。
- 回放保持原来的先后顺序和疏密，但把每两条之间的间隔压到 1~4 秒；每条的时间戳取写入那一刻的当前时间。
- 放完一轮就停几秒，然后换一个全新的内存账本再放一轮，一直循环。
- 内存账本的连接只能在创建它的线程里用（sqlite 的限制），所以由一个专属线程拥有它：
  别的线程要读，就把函数交给它执行（run）。它在等下一条事件的空档里处理这些请求。
- 示例模式只在请求带 demo=1 时启用；真实数据读不到时绝不会回落到示例。
"""
import json
import logging
import queue
import threading
import time
from concurrent.futures import Future
from pathlib import Path

from xay.core import db as core_db
from xay.core import events as core_events
from xay.core import snapshot as core_snapshot

from . import paths

LOG = logging.getLogger("xay.office.demo")

DEFAULT_MIN_GAP = 1.0
DEFAULT_MAX_GAP = 4.0
DEFAULT_SCALE = 0.5
DEFAULT_LOOP_PAUSE = 6.0


def load_events(fixture):
    """读示例事件文件；空行和坏行直接跳过。"""
    rows = []
    for line in Path(fixture).read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            row = json.loads(line)
        except ValueError:
            continue
        if isinstance(row, dict) and isinstance(row.get("type"), str) and isinstance(row.get("by"), str):
            rows.append(row)
    return rows


def compress_gaps(timestamps, min_gap=DEFAULT_MIN_GAP, max_gap=DEFAULT_MAX_GAP, scale=DEFAULT_SCALE):
    """每条事件相对上一条要等多久（秒）。第一条不等；其余 = 原间隔 * scale，再夹在 [min_gap, max_gap] 里。
    原间隔越长等得越久（单调），所以疏密关系保留。"""
    delays = []
    prev = None
    for index, ts in enumerate(timestamps):
        if index == 0:
            delays.append(0.0)
        else:
            try:
                gap = max(float(ts) - float(prev), 0.0)
            except (TypeError, ValueError):
                gap = 0.0
            delays.append(min(max(gap * scale, min_gap), max_gap))
        prev = ts if ts is not None else prev
    return delays


AGENT_LABELS = {"codex": "Codex", "dsh": "DeepSeek", "agy": "Gemini", "claude": "Claude"}


def demo_progress(snap):
    """示例模式用的总进度（设计 arch-v11 §8 的口径）：只在快照还没带 progress 时补上，
    好让 ?demo=1 能看到进度条；后台快照自己算出 progress 以后这里就不再动它。"""
    people = snap.get("people") or {}
    tasks = [t for t in (snap.get("tasks") or []) if isinstance(t, dict) and t.get("status") != "canceled"]
    by_agent, by_part, total, done, working = {}, {}, 0.0, 0, 0
    for t in tasks:
        st = t.get("status")
        if st in ("done", "reported"):
            share, done = 1.0, done + 1
        elif st == "working":
            working += 1
            pr = t.get("progress") or {}
            share = min(0.9, pr["step"] / pr["of"]) if pr.get("step") and pr.get("of") else 0.1
        elif st == "kept":
            share = 0.9
        else:
            share = 0.0
        total += share
        agent = (people.get(t.get("owner")) or {}).get("agent") or "other"
        by_agent[agent] = by_agent.get(agent, 0.0) + share
        part = t.get("part") or "all"
        got, n = by_part.get(part, (0.0, 0))
        by_part[part] = (got + share, n + 1)
    n = len(tasks)
    pct = round(total * 100 / n) if n else 0
    segs = [{"agent": a, "label": AGENT_LABELS.get(a, a), "pct": round(v * 100 / n)} for a, v in by_agent.items() if v > 0]
    if segs:
        segs[-1]["pct"] += pct - sum(x["pct"] for x in segs)       # 四舍五入后最后一段补差
    titles = {p.get("id"): p.get("title") or "" for p in (snap.get("parts") or []) if isinstance(p, dict)}
    parts = [{"id": k, "title": titles.get(k, ""), "pct": round(v * 100 / c) if c else 0} for k, (v, c) in by_part.items()]
    text = "%d 件活做完 %d 件" % (n, done) + ("，%d 件在做" % working if working else "") if n else "还没有派活"
    return {"pct": pct, "done": done, "all": n, "working": working, "by_agent": segs, "by_part": parts, "text": text}


def fill_demo_progress(conn, result):
    """给示例模式的快照（/api/v2/snapshot）和旧看板状态（/api/state）里每个对话补 progress（已有就不动）。"""
    try:
        if not isinstance(result, dict):
            return result
        conv = result.get("conversation")
        if isinstance(conv, dict) and "tasks" in result and "people" in result:
            if conv.get("id") and "progress" not in conv:
                conv["progress"] = demo_progress(result)
        elif isinstance(result.get("workspaces"), list):
            for ws in result["workspaces"]:
                for c in (ws or {}).get("conversations") or []:
                    if isinstance(c, dict) and c.get("id") and "progress" not in c:
                        c["progress"] = demo_progress(core_snapshot.build(conn, conv=c["id"]))
    except Exception:  # noqa: BLE001 - 演示用的补充，出错就不补，不影响原来的数据
        LOG.debug("示例进度没补上", exc_info=True)
    return result


class _DemoSession:
    """和真实账本的会话同样的接口：run(fn) 与 generation。"""

    def __init__(self, ledger):
        self._ledger = ledger

    @property
    def generation(self):
        return self._ledger.generation

    def run(self, fn):
        return self._ledger.run(fn)

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class DemoLedger:
    def __init__(self, fixture=None, *, min_gap=DEFAULT_MIN_GAP, max_gap=DEFAULT_MAX_GAP,
                 scale=DEFAULT_SCALE, loop_pause=DEFAULT_LOOP_PAUSE, db_factory=None):
        self.fixture = Path(fixture) if fixture else paths.fixtures_dir() / "demo.events.jsonl"
        self.min_gap, self.max_gap, self.scale, self.loop_pause = min_gap, max_gap, scale, loop_pause
        self._db_factory = db_factory or (lambda: core_db.open_db(":memory:"))
        self.generation = 0      # 每换一个新内存账本加 1
        self.loops_done = 0      # 已完整放完几轮
        self.error = None
        self.rejected = []       # 本轮被账本拒绝的示例事件：[(第几条, 原因)]，示例文件过期时会出现
        self._conn = None
        self._q = queue.Queue()
        self._stop = threading.Event()
        self._ready = threading.Event()
        self._thread = None

    # ---- 生命周期 ----
    def start(self, timeout=15.0):
        if self._thread is not None:
            return self
        self._thread = threading.Thread(target=self._main, name="xay-demo", daemon=True)
        self._thread.start()
        if not self._ready.wait(timeout):
            raise RuntimeError("示例账本没能在规定时间内启动")
        if self.error is not None:
            raise RuntimeError("示例账本启动失败：%s" % self.error)
        return self

    def stop(self):
        self._stop.set()
        self._q.put(None)
        if self._thread is not None and self._thread is not threading.current_thread():
            self._thread.join(3.0)

    def session(self):
        return _DemoSession(self)

    def wait_loops(self, n=1, timeout=30.0):
        """测试用：等到至少完整放完 n 轮。"""
        end = time.monotonic() + timeout
        while time.monotonic() < end:
            if self.loops_done >= n:
                return True
            time.sleep(0.01)
        return self.loops_done >= n

    # ---- 其他线程来读：把函数交给专属线程执行 ----
    def run(self, fn, timeout=20.0):
        if threading.current_thread() is self._thread:
            return fill_demo_progress(self._conn, fn(self._conn))
        if self._thread is None or not self._thread.is_alive():
            raise RuntimeError("示例账本没在运行")
        fut = Future()
        self._q.put((fn, fut))
        return fut.result(timeout)

    # ---- 专属线程 ----
    def _main(self):
        try:
            while not self._stop.is_set():
                self._one_round()
        except Exception as exc:  # noqa: BLE001 - 线程顶层，记下原因即可
            LOG.exception("示例回放线程出错")
            self.error = exc
        finally:
            self._ready.set()
            self._fail_pending()

    def _one_round(self):
        events = load_events(self.fixture)
        if not events:
            raise RuntimeError("示例事件文件是空的：%s" % self.fixture)
        delays = compress_gaps([e.get("ts") for e in events], self.min_gap, self.max_gap, self.scale)
        conn = self._db_factory()
        self._conn = conn
        self.rejected = []
        self.generation += 1
        try:
            for index, (event, delay) in enumerate(zip(events, delays)):
                if not self._wait(delay):
                    return
                self._append(conn, event, index)
                if index == 0:
                    self._ready.set()
            self.loops_done += 1
            self._wait(self.loop_pause)
        finally:
            self._conn = None
            try:
                conn.close()
            except Exception:  # noqa: BLE001
                pass

    def _append(self, conn, event, index):
        try:
            core_events.append(
                conn, event["type"], event["by"], event.get("data") or {},
                ws=event.get("ws"), conv=event.get("conv"), subject=event.get("subject"),
                dedup=event.get("dedup"), ts=time.time())
        except Exception as exc:  # noqa: BLE001 - 一条被拒不能让整个回放停下来
            self.rejected.append((index + 1, str(exc)))
            LOG.warning("示例事件第 %d 条被账本拒绝，已跳过：%s", index + 1, exc)

    def _wait(self, delay):
        """睡 delay 秒，期间处理别的线程交来的读请求。返回 False 表示该停了。"""
        end = time.monotonic() + delay
        while not self._stop.is_set():
            remaining = end - time.monotonic()
            if remaining <= 0:
                for _ in range(self._q.qsize()):   # 到点了也先把已排队的请求处理掉，避免一直饿着
                    try:
                        self._serve(self._q.get_nowait())
                    except queue.Empty:
                        break
                return True
            try:
                self._serve(self._q.get(timeout=remaining))
            except queue.Empty:
                pass
        return False

    def _serve(self, item):
        if item is None:
            return
        fn, fut = item
        if fut.cancelled():
            return
        try:
            fut.set_result(fill_demo_progress(self._conn, fn(self._conn)))
        except BaseException as exc:  # noqa: BLE001 - 异常要原样交还给等结果的线程
            fut.set_exception(exc)

    def _fail_pending(self):
        while True:
            try:
                item = self._q.get_nowait()
            except queue.Empty:
                return
            if item is not None and not item[1].done():
                item[1].set_exception(RuntimeError("示例账本已停止"))
