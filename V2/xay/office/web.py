"""管家的网页服务：只绑定 127.0.0.1 的 HTTP + SSE（协议 §8）。

安全规则（每个请求都检查）：
- Host 必须是 127.0.0.1:<端口> 或 localhost:<端口>，否则 403；
- POST 还必须带 X-Xay-Action 头（与接口对应：control / letter / decide）并且 Origin 与本机页面同源，否则 403；
- 不回任何 CORS 头；静态文件挡掉路径穿越。
事件流：有大白话的事件发 ledger，没有的合并成一帧 changed（页面只重拉快照），每 15 秒一帧 heartbeat。
真实数据和示例数据（?demo=1）走同一套快照 / 事件流代码，只是用的账本连接不同；
真实数据读不到时就如实报错，绝不回落到示例。
"""
import builtins
import json
import logging
import re
import secrets
import select
import socket
import sqlite3
import threading
import time
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from xay.core import db as core_db
from xay.core import errors as core_errors
from xay.core import events as core_events
from xay.core import humanize as core_humanize
from xay.core import snapshot as core_snapshot
from xay.core.reducers import legacy_status

from . import board as office_board
from . import employee as office_employee
from . import paths
from . import plain as office_plain
from .demo import DemoLedger
from .live import BACKLOG_LINES, LiveTail, valid_em

LOG = logging.getLogger("xay.office.web")

DEFAULT_PORT = 7879
MAX_BODY = 64 * 1024
HEARTBEAT_SECONDS = 15.0
POLL_SECONDS = 0.25
CONTROL_ACTIONS = ("steer", "interrupt", "stop", "stop_all", "resume")
MAX_TEXT = 2000
MAX_LETTER_BODY = 4000
MAX_LETTER_SUBJECT = 100
# 看文件接口（/api/v2/file）一律不给的文件名：常见的登录凭据 / 私钥文件
_SECRET_NAMES = frozenset((".netrc", ".npmrc", ".pypirc", ".git-credentials", "credentials", "auth.json", ".htpasswd"))
_EMS_ITEM_RE = re.compile(r"^([A-Za-z0-9_-]{1,64})(?:@(\d+)\.(\d+))?$")
POST_ROUTES = {                      # 路径 → 必须带的 X-Xay-Action
    "/api/v2/control": "control",
    "/api/v2/letter": "letter",
    "/api/v2/decision": "decide",
    "/api/v2/notes": "notes",
    "/api/control": "control",
    "/api/decision": "decide",
}
CONTENT_TYPES = {
    ".html": "text/html; charset=utf-8",
    ".js": "text/javascript; charset=utf-8",
    ".mjs": "text/javascript; charset=utf-8",
    ".css": "text/css; charset=utf-8",
    ".json": "application/json; charset=utf-8",
    ".webp": "image/webp",
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".gif": "image/gif",
    ".svg": "image/svg+xml",
    ".ico": "image/x-icon",
    ".txt": "text/plain; charset=utf-8",
    ".md": "text/plain; charset=utf-8",
    ".woff2": "font/woff2",
}
ERROR_TEXT = {
    400: "请求格式不对", 404: "没有这个页面", 405: "这个地址不支持这种操作", 411: "请求缺少内容长度",
    413: "请求内容太大", 414: "请求地址太长", 431: "请求头太大", 501: "不支持这种请求",
}


class HttpError(Exception):
    def __init__(self, status, message):
        super().__init__(message)
        self.status, self.message = status, message


# ---------- 纯函数：Host / Origin / 静态路径 ----------
def allowed_hosts(port):
    return {"127.0.0.1:%d" % port, "localhost:%d" % port}


def host_ok(host_header, port):
    return bool(host_header) and host_header.strip().lower() in allowed_hosts(port)


def origin_ok(origin, host_header, port):
    """同源：Origin 必须正好是 http://<本次请求的 Host>，且 Host 本身合法。"""
    if not origin or not host_ok(host_header, port):
        return False
    return origin.strip().lower() == "http://" + host_header.strip().lower()


def content_type_for(path):
    return CONTENT_TYPES.get(Path(path).suffix.lower(), "application/octet-stream")


def resolve_static(url_path, root):
    """把请求路径映射到磁盘文件。返回 ("ok", Path) / ("forbidden", None) / ("notfound", None)。"""
    root = Path(root)
    if url_path == "/org":
        base, rel = root, "org.html"
    elif url_path == "/board":                 # 看板（M5：7878 换成 V2；"/" 也是它）
        base, rel = root, "board.html"
    elif url_path == "/chars":
        base, rel = root, "chars/gallery.html"
    elif url_path.startswith("/web/"):
        base, rel = root, url_path[len("/web/"):]
    elif url_path.startswith("/xay-chars/"):
        base, rel = root / "chars", url_path[len("/xay-chars/"):]
    else:
        return "notfound", None
    decoded = urllib.parse.unquote(rel)
    if "\x00" in decoded or "\\" in decoded:
        return "forbidden", None
    segments = decoded.split("/")
    if any(seg.startswith(".") for seg in segments):       # .. . 以及 .git 之类的隐藏项一律拒绝
        return "forbidden", None
    segments = [seg for seg in segments if seg]
    if not segments or decoded.endswith("/"):
        return "notfound", None
    try:
        real = base.joinpath(*segments).resolve(strict=True)
        real_base = base.resolve(strict=True)
    except (OSError, RuntimeError):
        return "notfound", None
    try:
        real.relative_to(real_base)
    except ValueError:
        return "forbidden", None                           # 比如目录里放了指向外面的软链接
    if not real.is_file():
        return "notfound", None
    return "ok", real


def _q1(query, key):
    values = query.get(key)
    if not values:
        return None
    value = values[0].strip()
    return value or None


def _int(value):
    try:
        number = int(str(value).strip())
    except (TypeError, ValueError):
        return None
    return number if number >= 0 else None


def is_demo(query):
    return _q1(query, "demo") in ("1", "true")


def _str(value):
    return value.strip() if isinstance(value, str) else ""


# ---------- 账本来源：真实 / 示例，接口相同 ----------
class _RealSession:
    generation = 0

    def __init__(self, db_path):
        self._db_path = db_path
        self._conn = None

    def run(self, fn):
        if self._conn is None:
            self._conn = core_db.open_db(self._db_path)
        return fn(self._conn)

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        if self._conn is not None:
            try:
                self._conn.close()
            except Exception:  # noqa: BLE001
                pass
            self._conn = None
        return False


class RealSource:
    def __init__(self, db_path=None):
        self.db_path = db_path

    def session(self):
        return _RealSession(self.db_path)


# ---------- 事件文案 ----------
def stream_frames(conn, batch):
    """把一批账本事件变成要推给页面的帧：[(seq, 事件名, 数据)]，按 seq 从小到大。

    - 有大白话的事件：每条一帧 `ledger`，data = recent 同款（带 from / to / kind / em / owner，页面据此放动画）；
    - 没有大白话的事件（技术事件、文案出错的事件等）：这一批里合并成**一帧** `changed`，data = {seq}，
      seq 取这些事件里最后一条；页面收到只重拉快照、不放动画。
    """
    frames, state, silent_seq = [], None, None
    for ev in batch:
        try:
            text = core_humanize.text(ev, conn)
        except Exception:  # noqa: BLE001 - 一条事件的文案出错，不能让整条事件流断掉；当作"没有文案"，页面照样刷新
            LOG.warning("事件 #%s 的大白话文案生成失败，按无文案处理", ev.get("seq"), exc_info=True)
            text = None
        if text is None:
            silent_seq = ev["seq"]
            continue
        data = ev.get("data") if isinstance(ev.get("data"), dict) else {}
        item = {"seq": ev["seq"], "ts": ev["ts"], "type": ev["type"], "text": text}
        if ev.get("by") in ("office", "rules"):
            item["auto"] = True                            # 后台自动做的（页面上算监工发出的：派活从监工席飞出去）
        subject = ev.get("subject")
        if subject:
            item["subject"] = subject
        for key in ("from", "to"):
            if isinstance(data.get(key), str) and data[key]:
                item[key] = data[key]
        if ev["type"].startswith("letter.") and isinstance(data.get("kind"), str):
            item["kind"] = data["kind"]
        em = data.get("em") if isinstance(data.get("em"), str) else None
        if em is None and isinstance(subject, str) and subject.startswith("em_"):
            em = subject
        if em:
            item["em"] = em
        if ev["type"] in ("task.landed", "task.accepted") and isinstance(subject, str):
            if state is None:
                state = core_events.load_state(conn)
            owner = (state.get("tasks", {}).get(subject) or {}).get("owner")
            if owner:
                item["owner"] = owner                      # 页面用它给员工放庆祝动画
        frames.append((ev["seq"], "ledger", item))
    if silent_seq is not None:
        frames.append((silent_seq, "changed", {"seq": silent_seq}))
    frames.sort(key=lambda f: f[0])
    return frames


# ---------- 快照附加：最近动静（页面折叠"以往的部门"、排工位待办用） ----------
ACTIVITY_TYPES = ("task.added", "task.changed", "task.removed", "task.assigned", "task.ready", "task.queued", "task.progress",
                  "task.reported", "task.landed", "task.kept", "task.accepted", "task.failed", "task.canceled")


def _epoch(value):
    if isinstance(value, bool) or not isinstance(value, (int, float)) or value <= 0:
        return None
    return value / 1000.0 if value > 1e11 else float(value)


def snapshot_activity(conn, snap):
    """{"tasks": {任务编号: 最近动静时间}, "depts": {部门编号: 最近动静时间}}。

    单独放在快照的 activity 字段里，不改快照原有的任何字段（快照本身由核心生成）。
    - V2 的活：账本里这件活最近一条事件的时间；旧系统镜像的活：旧 run 自己的结束 / 开始时间（不用桥入账的时间，
      否则一次性导入的几十个旧部门都会显得"刚刚有动静"），还在跑的算现在；
    - 部门：它名下所有活里最近的一个；加上部门是 V2 自己成立的（不是桥建的）时成立的时间。
    """
    conv = (snap.get("conversation") or {}).get("id")
    tasks, depts = {}, {}
    if not conv:
        return {"tasks": tasks, "depts": depts}
    marks = ",".join("?" * len(ACTIVITY_TYPES))
    rows = conn.execute("SELECT type, subject, by, MAX(ts) AS ts FROM events WHERE conv=? AND (type IN (%s) OR type='dept.opened') "
                        "GROUP BY type, subject, by" % marks, (conv,) + ACTIVITY_TYPES).fetchall()
    for r in rows:
        ts = _epoch(r["ts"])
        if ts is None or not r["subject"]:
            continue
        if r["type"] == "dept.opened":
            if r["by"] != "bridge":
                depts[r["subject"]] = max(depts.get(r["subject"], 0), ts)
        else:
            tasks[r["subject"]] = max(tasks.get(r["subject"], 0), ts)
    now = _epoch(snap.get("now")) or time.time()
    for run in core_events.load_state(conn).get("legacy_runs", {}).values():
        if run.get("conv") != conv:
            continue
        ts = now if legacy_status(run) == "working" else _epoch(run.get("ended")) or _epoch(run.get("started"))
        if ts:
            tid = office_employee.legacy_task_id(run)
            tasks[tid] = max(tasks.get(tid, 0), ts)
    for t in snap.get("tasks") or []:
        ts = tasks.get(t.get("id"))
        if ts and t.get("dept"):
            depts[t["dept"]] = max(depts.get(t["dept"], 0), ts)
    return {"tasks": tasks, "depts": depts}


def build_snapshot(conn, ws=None, conv=None):
    snap = core_snapshot.build(conn, ws=ws, conv=conv)
    snap["activity"] = snapshot_activity(conn, snap)
    return snap


# ---------- 写入类接口（在账本连接上执行） ----------
def _default_context(conn, state, ws=None, conv=None):
    """要写进哪个工作区 / 对话。页面会把当前的 ws / conv 一起发来；没发时用和快照相同的默认（跟着核心的规则走）。"""
    if conv:
        record = state.get("convs", {}).get(conv)
        if record is None:
            raise ValueError("找不到这个对话")
        return record.get("ws") or ws, conv
    snap = core_snapshot.build(conn, ws=ws)
    conv_id = (snap.get("conversation") or {}).get("id")
    if not conv_id:
        return ws, None
    return (snap.get("ws") or {}).get("id") or ws, conv_id


def do_control(conn, body, query):
    action = _str(body.get("action"))
    if action not in CONTROL_ACTIONS:
        raise ValueError("不认识这个操作，可用的有：" + "、".join(CONTROL_ACTIONS))
    em, task = _str(body.get("em")), _str(body.get("task"))
    if em and task:
        raise ValueError("员工和任务只能指定一个")
    state = core_events.load_state(conn)
    ws_arg = _str(body.get("ws")) or _q1(query, "ws")
    conv_arg = _str(body.get("conv")) or _q1(query, "conv")
    if em:
        employee = state.get("employees", {}).get(em)
        if not employee:
            raise ValueError("找不到这位员工")
        subject = em
        ws, conv = _default_context(conn, state, employee.get("ws"), conv_arg)
    elif task:
        record = state.get("tasks", {}).get(task)
        if not record:
            raise ValueError("找不到这个任务")
        subject, ws, conv = task, record.get("ws"), record.get("conv")
    elif action in ("stop_all", "resume"):     # 总闸：全部停下 / 全部恢复（页面顶栏的红按钮）
        subject = None
        ws, conv = _default_context(conn, state, ws_arg, conv_arg)
    else:
        raise ValueError("请指定要操作的员工（em）或任务（task）")
    data = {"action": action, "by_source": "page"}
    text = body.get("text")
    if text is not None and not isinstance(text, str):
        raise ValueError("附带的话必须是文字")
    if isinstance(text, str) and text.strip():
        data["text"] = text.strip()[:MAX_TEXT]
    if action == "steer" and "text" not in data:
        raise ValueError("要调整方向，请写清楚想说的话")
    seq = core_events.append(conn, "control.requested", "user", data, ws=ws, conv=conv, subject=subject)
    return {"ok": True, "seq": seq}


def do_letter(conn, body, query):
    to = _str(body.get("to"))
    text = body.get("body")
    if not to:
        raise ValueError("请写清楚信寄给谁")
    if not isinstance(text, str) or not text.strip():
        raise ValueError("信的内容不能为空")
    text = text.strip()
    if len(text) > MAX_LETTER_BODY:
        raise ValueError("信太长了，请控制在 %d 个字以内" % MAX_LETTER_BODY)
    subject = _str(body.get("subject"))
    if not subject:
        subject = re.split(r"[。！？!?\n]", text, maxsplit=1)[0].strip()
        subject = subject if len(subject) <= 30 else subject[:29] + "…"
    if len(subject) > MAX_LETTER_SUBJECT:
        raise ValueError("主题太长了，请控制在 %d 个字以内" % MAX_LETTER_SUBJECT)
    state = core_events.load_state(conn)
    ws, conv = _default_context(conn, state, _str(body.get("ws")) or _q1(query, "ws"),
                                _str(body.get("conv")) or _q1(query, "conv"))
    if conv is None:
        raise ValueError("现在还没有任何对话，信没地方可寄")
    if to == "gm":
        kind = "tell"
    elif to in state.get("employees", {}):
        kind = "tell"
    else:
        depts = core_snapshot.build(conn, ws=ws, conv=conv).get("depts") or []
        if to not in [d.get("id") for d in depts]:           # 部门只在本对话里找（用公开的快照结构，不碰内部存法）
            raise ValueError("找不到收信的人或部门")
        kind = "request"
    letter_id = "lt_" + secrets.token_hex(4)
    data = {"from": "user", "to": to, "kind": kind, "subject": subject, "body": text}
    seq = core_events.append(conn, "letter.sent", "user", data, ws=ws, conv=conv, subject=letter_id)
    return {"ok": True, "seq": seq, "id": letter_id}


def do_decision(conn, body, query):
    dc = _str(body.get("id"))
    if not dc:
        raise ValueError("缺少事项编号")
    delegate = _str(body.get("delegate"))
    state = core_events.load_state(conn)
    decision = state.get("decisions", {}).get(dc)
    if not decision:
        raise ValueError("找不到这件需要你拿主意的事，可能已经处理过了")
    if delegate:
        if delegate not in ("rules", "lead"):
            raise ValueError("delegate 只能是 rules 或 lead")
        seq = core_events.append(conn, "decision.delegated", "user", {"to": delegate}, ws=decision.get("ws"),
                                 conv=decision.get("conv"), subject=dc, dedup="page:delegate:%s" % dc)
        return {"ok": True, "seq": seq}
    option = _str(body.get("option"))
    if not option:
        raise ValueError("缺少事项编号或选项")
    options = [o for o in (decision.get("options") or []) if isinstance(o, dict)]
    if options and option not in [o.get("id") for o in options]:
        raise ValueError("没有这个选项，可选：" + "、".join(str(o.get("label") or o.get("id")) for o in options))
    data = {"option": option}
    text = body.get("text")
    if isinstance(text, str) and text.strip():
        data["text"] = text.strip()[:MAX_TEXT]
    seq = core_events.append(conn, "decision.resolved", "user", data, ws=decision.get("ws"),
                             conv=decision.get("conv"), subject=dc, dedup="page:decision:%s" % dc)
    return {"ok": True, "seq": seq}


def do_notes(conn, body, query):
    em = _str(body.get("em"))
    if not em:
        raise ValueError("缺少员工编号")
    text = body.get("text")
    if not isinstance(text, str):
        raise ValueError("笔记内容必须是文字")
    if len(text) > 6000:
        raise HttpError(400, "笔记太长了，请控制在 6000 字以内")
    state = core_events.load_state(conn)
    employee = state.get("employees", {}).get(em)
    if not employee:
        raise ValueError("找不到这位员工")
    ws, conv = _default_context(conn, state, employee.get("ws"),
                                _str(body.get("conv")) or _q1(query, "conv"))
    seq = core_events.append(conn, "notes.updated", "user", {"active": text},
                             ws=ws, conv=conv, subject=em)
    return {"ok": True, "seq": seq}


def _oldboard_control(conn, body, query):
    try:
        from . import oldboard_conv
    except ImportError:
        raise HttpError(404, "还没接上")
    return oldboard_conv.control(conn, body, query)


def _oldboard_decide(conn, body, query):
    try:
        from . import oldboard_conv
    except ImportError:
        raise HttpError(404, "还没接上")
    return oldboard_conv.decide(conn, body, query)


POST_HANDLERS = {
    "/api/v2/control": do_control,
    "/api/v2/letter": do_letter,
    "/api/v2/decision": do_decision,
    "/api/v2/notes": do_notes,
    "/api/control": _oldboard_control,
    "/api/decision": _oldboard_decide,
}


def halt_info(conn):
    """总闸现状（页面顶栏的红按钮用）：{halt: null | {seq, ts, by, source}, working: 正在干活的人数, running: 在岗人数}。"""
    state = core_events.load_state(conn)
    emps = [e for e in state.get("employees", {}).values() if not e.get("former")]
    return {"halt": state.get("halt"),
            "working": sum(1 for e in emps if e.get("turn")),
            "online": sum(1 for e in emps if e.get("online"))}


# ---------- 服务器 ----------
class OfficeServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True
    request_queue_size = 64

    def __init__(self, port=DEFAULT_PORT, *, db_path=None, web_root=None, live_root=None,
                 demo_factory=None, heartbeat_seconds=HEARTBEAT_SECONDS, poll_seconds=POLL_SECONDS):
        self.stop_event = threading.Event()
        self._serving = False
        self._thread = None
        super().__init__(("127.0.0.1", port), Handler)
        self.port = self.server_address[1]
        self.web_root = Path(web_root) if web_root else paths.web_root()
        self.live_root = Path(live_root) if live_root else paths.live_root()
        self.quota_home = self.live_root.parent          # $XAY2_HOME：详情栏读 quota/agy-calls.jsonl（Gemini 今天用了几次）
        self.heartbeat_seconds, self.poll_seconds = heartbeat_seconds, poll_seconds
        self.real_source = RealSource(db_path)
        self._demo = None
        self._demo_lock = threading.Lock()
        self._demo_factory = demo_factory or DemoLedger

    @property
    def url(self):
        return "http://127.0.0.1:%d" % self.port

    def source_for(self, demo):
        if not demo:
            return self.real_source
        with self._demo_lock:
            if self._demo is None:
                self._demo = self._demo_factory().start()
            return self._demo

    def serve_forever(self, poll_interval=0.25):
        self._serving = True
        try:
            super().serve_forever(poll_interval)
        finally:
            self._serving = False

    def start_background(self):
        self._serving = True
        self._thread = threading.Thread(target=self.serve_forever, name="xay-office-http", daemon=True)
        self._thread.start()
        return self

    def stop(self):
        self.stop_event.set()
        if self._serving:
            self.shutdown()
        self.server_close()
        if self._thread is not None:
            self._thread.join(3.0)
        if self._demo is not None:
            self._demo.stop()


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    timeout = 60

    # ---- 基础输出 ----
    def version_string(self):
        return "xay-office"

    def log_message(self, fmt, *args):
        LOG.debug("%s %s", self.address_string(), fmt % args)

    def log_error(self, fmt, *args):
        LOG.warning("%s %s", self.address_string(), fmt % args)

    def _common_headers(self):
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("X-Frame-Options", "SAMEORIGIN")

    def _send(self, status, body, ctype, *, head=False, close=False, extra=()):
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self._common_headers()
        for key, value in extra:
            self.send_header(key, value)
        if close or (self.command == "POST" and not self._body_read):
            self.send_header("Connection", "close")
            self.close_connection = True
        self.end_headers()
        if not head and body:
            self.wfile.write(body)

    def _json(self, status, obj, *, head=False):
        body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self._send(status, body, "application/json; charset=utf-8", head=head,
                   extra=(("Cache-Control", "no-store"),))

    def _json_error(self, status, message):
        self._json(status, {"error": message, "detail": message})   # detail 是给现有页面脚本读的

    def send_error(self, code, message=None, explain=None):          # 协议层的错误也用中文 JSON
        self._body_read = False
        self.command = self.command or ""
        text = ERROR_TEXT.get(code, "请求出错（%s）" % code)
        body = json.dumps({"error": text, "detail": text}, ensure_ascii=False).encode("utf-8")
        try:
            self.send_response(code)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Connection", "close")
            self.end_headers()
            if self.command != "HEAD":
                self.wfile.write(body)
        except OSError:
            pass
        self.close_connection = True

    # ---- 分发 ----
    def do_GET(self):
        self._dispatch()

    def do_HEAD(self):
        self._dispatch()

    def do_POST(self):
        self._dispatch()

    def do_OPTIONS(self):
        self._dispatch()

    do_PUT = do_DELETE = do_PATCH = do_OPTIONS

    def _dispatch(self):
        self._body_read = False
        self._streaming = False
        raw_path = "?"
        srv = self.server
        try:
            if self.path.startswith("/"):
                raw_path, _, raw_query = self.path.partition("?")
                raw_query = raw_query.partition("#")[0]
                raw_path = raw_path.partition("#")[0]
            else:
                parts = urllib.parse.urlsplit(self.path)
                raw_path, raw_query = parts.path, parts.query
            if not raw_path.startswith("/"):
                raise HttpError(400, ERROR_TEXT[400])
            query = urllib.parse.parse_qs(raw_query, keep_blank_values=True)
            if not host_ok(self.headers.get("Host"), srv.port):
                raise HttpError(403, "访问地址不对：请用 127.0.0.1 或 localhost 打开管家页面")
            method = self.command
            if method == "POST":
                self._check_post(raw_path)
                self._post(raw_path, query)
            elif method in ("GET", "HEAD"):
                self._get(raw_path, query, head=(method == "HEAD"))
            else:
                raise HttpError(405, ERROR_TEXT[405])
        except HttpError as exc:
            self._reply_error(exc.status, exc.message)
        except (BrokenPipeError, ConnectionResetError, socket.timeout):
            self.close_connection = True
        except (core_errors.PermissionError, builtins.PermissionError) as exc:
            self._reply_error(403, str(exc) or "没有权限做这件事")
        except ValueError as exc:
            self._reply_error(422, str(exc) or "请求内容有问题")
        except sqlite3.Error as exc:
            LOG.warning("账本读写出错：%s", exc)
            self._reply_error(500, "读不到数据：账本暂时打不开或正忙，请稍后再试")
        except OSError as exc:
            LOG.warning("账本文件打不开：%s", exc)
            self._reply_error(500, "读不到数据：账本文件打不开，请检查 XAY2_HOME 指向的目录")
        except Exception:  # noqa: BLE001 - 最后一道兜底，不能让线程带着半截响应死掉
            LOG.exception("处理请求出错：%s %s", self.command, raw_path)
            self._reply_error(500, "读不到数据：管家内部出错了，详情见日志 office.log")

    def _reply_error(self, status, message):
        if self._streaming:                    # 事件流已经开始发了，没法再换成错误页，直接断开
            self.close_connection = True
            return
        try:
            self._json_error(status, message)
        except OSError:
            self.close_connection = True

    def _check_post(self, path):
        expected = POST_ROUTES.get(path)
        if expected is None:
            raise HttpError(404, ERROR_TEXT[404])
        if self.headers.get("X-Xay-Action") != expected:
            raise HttpError(403, "缺少操作标记，请从管家页面发起操作")
        if not origin_ok(self.headers.get("Origin"), self.headers.get("Host"), self.server.port):
            raise HttpError(403, "请求来源和页面不一致，已拒绝")

    def _read_body(self):
        if "chunked" in (self.headers.get("Transfer-Encoding") or "").lower():
            raise HttpError(411, ERROR_TEXT[411])
        try:
            length = int(self.headers.get("Content-Length") or "")
        except ValueError:
            raise HttpError(411, ERROR_TEXT[411])
        if length < 0 or length > MAX_BODY:
            raise HttpError(413, ERROR_TEXT[413])
        raw = self.rfile.read(length)
        self._body_read = True
        try:
            body = json.loads(raw.decode("utf-8"))
        except ValueError:
            raise HttpError(422, "请求内容不是有效的 JSON")
        if not isinstance(body, dict):
            raise HttpError(422, "请求内容必须是一个 JSON 对象")
        return body

    def _post(self, path, query):
        body = self._read_body()
        source = self.server.source_for(is_demo(query))
        handler = POST_HANDLERS[path]
        with source.session() as session:
            result = session.run(lambda conn: handler(conn, body, query))
        self._json(200, result)

    def _get(self, path, query, *, head):
        if path == "/api/state":
            return self._oldboard_state(query, head)
        if path == "/api/events":
            if head:
                raise HttpError(405, ERROR_TEXT[405])
            return self._oldboard_events(query)
        if path == "/api/conversation":
            return self._oldboard_conversation(query, head)
        if path == "/api/diff":
            return self._oldboard_diff(query, head)
        if path == "/api/v2/snapshot":
            return self._snapshot(query, head)
        if path == "/api/v2/file":
            return self._v2_file(query, head)
        if path == "/api/v2/diff":
            return self._v2_diff(query, head)
        if path == "/api/v2/employee":
            return self._employee(query, head)
        if path == "/api/v2/plan":
            return self._plan(query, head)
        if path == "/api/v2/halt":
            return self._halt(query, head)
        if path == "/api/v2/convs":
            return self._convs(query, head)
        if path in ("/api/v2/stream", "/api/v2/live"):
            if head:
                raise HttpError(405, ERROR_TEXT[405])
            return self._stream(query) if path == "/api/v2/stream" else self._live(query)
        if path.startswith("/api/"):
            raise HttpError(404, ERROR_TEXT[404])
        return self._static(path, head)

    # ---- 旧看板接口 ----
    def _oldboard_state(self, query, head):
        from . import oldboard
        demo = is_demo(query)
        try:
            source = self.server.source_for(demo)
        except Exception as exc:  # noqa: BLE001
            LOG.exception("示例账本启动失败")
            raise HttpError(500, "读不到数据：示例数据没能启动（%s）" % exc)
        with source.session() as session:
            data = session.run(lambda conn: oldboard.state(conn))
        self._json(200, data, head=head)

    def _oldboard_conversation(self, query, head):
        cid = _q1(query, "id")
        if not cid:
            raise HttpError(400, "缺少对话编号")
        try:
            from . import oldboard_conv
        except ImportError:
            raise HttpError(404, "还没接上")
        demo = is_demo(query)
        try:
            source = self.server.source_for(demo)
        except Exception as exc:  # noqa: BLE001
            LOG.exception("示例账本启动失败")
            raise HttpError(500, "读不到数据：示例数据没能启动（%s）" % exc)
        with source.session() as session:
            data = session.run(lambda conn: oldboard_conv.conversation(conn, cid))
        if data is None:
            raise HttpError(404, "Not Found")
        self._json(200, data, head=head)

    def _oldboard_diff(self, query, head):
        execution = _q1(query, "execution") or ""
        diff_path = _q1(query, "path") or ""
        try:
            from . import oldboard_conv
        except ImportError:
            raise HttpError(404, "还没接上")
        demo = is_demo(query)
        try:
            source = self.server.source_for(demo)
        except Exception as exc:  # noqa: BLE001
            LOG.exception("示例账本启动失败")
            raise HttpError(500, "读不到数据：示例数据没能启动（%s）" % exc)
        with source.session() as session:
            data = session.run(lambda conn: oldboard_conv.diff(conn, execution, diff_path))
        self._json(200, data, head=head)

    def _oldboard_events(self, query):
        srv = self.server
        cursor = self._cursor(query)
        demo = is_demo(query)
        try:
            source = srv.source_for(demo)
        except Exception as exc:  # noqa: BLE001
            LOG.exception("示例账本启动失败")
            raise HttpError(500, "读不到数据：示例数据没能启动（%s）" % exc)
        with source.session() as session:
            if cursor is None:
                cursor = session.run(core_events.max_seq)
            generation = session.generation
            if not self._sse_start():
                return
            last_beat = time.monotonic()
            last_state_changed = 0.0
            pending_state_changed = False
            last_conv_update = {}  # conv_id -> last_emit_mono
            live_state = {}        # em -> (mtime, size)

            while not srv.stop_event.is_set():
                now_mono = time.monotonic()
                try:
                    if session.generation != generation:
                        generation, cursor = session.generation, 0
                    top = session.run(core_events.max_seq)
                    if top < cursor:
                        cursor = top
                    if top > cursor:
                        batch = session.run(lambda c, cur=cursor: core_events.since(c, cur, limit=500))
                        if batch:
                            # 账本里某个对话有新事件 → 对这个对话发一次 thread.update
                            affected_convs = {ev.get("conv") for ev in batch if ev.get("conv")}
                            for cid in affected_convs:
                                if now_mono - last_conv_update.get(cid, 0.0) >= 2.0:
                                    payload = {"conversation": cid, "thread": "", "patch": {}}
                                    if not self._sse_write(self._frame("thread.update", payload, event_id=top)):
                                        return
                                    last_conv_update[cid] = now_mono

                            pending_state_changed = True
                            cursor = batch[-1]["seq"]

                    # 状态变更事件，最多 1 秒一次
                    if pending_state_changed and (now_mono - last_state_changed >= 1.0):
                        if not self._sse_write(self._frame("state.changed", {"seq": top}, event_id=top)):
                            return
                        last_state_changed = now_mono
                        pending_state_changed = False

                    # 某个对话里有员工正在干活、他的直播文件变了 → 对该对话发 thread.update（最多 2 秒一次）
                    if not demo and srv.live_root and srv.live_root.is_dir():
                        state_data = session.run(core_events.load_state)
                        for em, edata in (state_data.get("employees") or {}).items():
                            if not edata.get("turn") and edata.get("state") != "working":
                                continue
                            cid = edata.get("conv")
                            if not cid:
                                cur_task = edata.get("current_task")
                                if cur_task:
                                    t = (state_data.get("tasks") or {}).get(cur_task)
                                    if t:
                                        cid = t.get("conv")
                            if not cid:
                                continue

                            em_dir = srv.live_root / em
                            if not em_dir.is_dir():
                                continue
                            mtime, size = 0.0, 0
                            try:
                                for f in em_dir.iterdir():
                                    st = f.stat()
                                    mtime = max(mtime, st.st_mtime)
                                    size += st.st_size
                            except OSError:
                                continue
                            sig = (mtime, size)
                            old_sig = live_state.get(em)
                            live_state[em] = sig
                            if old_sig is not None and sig != old_sig:
                                if now_mono - last_conv_update.get(cid, 0.0) >= 2.0:
                                    payload = {"conversation": cid, "thread": "", "patch": {}}
                                    if not self._sse_write(self._frame("thread.update", payload, event_id=top)):
                                        return
                                    last_conv_update[cid] = now_mono
                except sqlite3.Error as exc:
                    LOG.warning("oldboard events 读账本出错: %s", exc)

                if now_mono - last_beat >= srv.heartbeat_seconds:
                    if not self._sse_write(self._frame("heartbeat", {"ts": time.time()})):
                        return
                    last_beat = now_mono

                if self._client_gone():
                    return
                if srv.stop_event.wait(srv.poll_seconds):
                    return

    # ---- 快照 ----
    def _snapshot(self, query, head):
        ws, conv = _q1(query, "ws"), _q1(query, "conv")
        try:
            source = self.server.source_for(is_demo(query))
        except Exception as exc:  # noqa: BLE001
            LOG.exception("示例账本启动失败")
            raise HttpError(500, "读不到数据：示例数据没能启动（%s）" % exc)
        with source.session() as session:
            snap = session.run(lambda conn: build_snapshot(conn, ws=ws, conv=conv))
        self._json(200, snap, head=head)

    # ---- 读文件接口（GET /api/v2/file） ----
    def _v2_file(self, query, head):
        conv_id = _q1(query, "conv")
        rel_path = _q1(query, "path")
        task_id = _q1(query, "task")
        if not conv_id or not rel_path:
            raise HttpError(400, "缺少对话编号或文件路径")
        demo = is_demo(query)
        try:
            source = self.server.source_for(demo)
        except Exception as exc:  # noqa: BLE001
            raise HttpError(500, "读不到数据（%s）" % exc)
        with source.session() as session:
            state = session.run(core_events.load_state)
            conv = state.get("convs", {}).get(conv_id)
            if not conv:
                raise HttpError(404, "找不到该对话")
            ws_id = conv.get("ws")
            ws_rec = state.get("workspaces", {}).get(ws_id) or {}
            if task_id:
                t = state.get("tasks", {}).get(task_id)
                if not t:
                    raise HttpError(404, "找不到该任务")
                owner = t.get("owner")
                t_ws = t.get("ws") or ws_id
                if not owner or not t_ws:
                    raise HttpError(404, "该任务尚未分配工位")
                desk_path = paths.v2_home() / "desks" / t_ws / owner
                if not desk_path.is_dir():
                    raise HttpError(404, "找不到任务对应的员工桌子")
                base_dir = desk_path
                from_type = "desk"
            else:
                proj_path = ws_rec.get("path")
                if not proj_path or not Path(proj_path).is_dir():
                    raise HttpError(404, "找不到项目目录")
                base_dir = Path(proj_path)
                from_type = "project"

            clean = rel_path.strip().replace("\\", "/").lstrip("/")
            parts = clean.split("/")
            if ".." in parts:
                raise HttpError(403, "禁止使用 .. 路径穿越")
            for p in parts:
                if p == ".git":
                    raise HttpError(403, "禁止访问 .git 目录下的文件")
                if p == ".env" or p.startswith(".env.") or p.startswith(".env"):
                    raise HttpError(403, "禁止访问 .env 文件")
                if p.endswith((".key", ".pem", ".p12", ".pfx")) or p in _SECRET_NAMES or p.startswith("id_rsa") or p.startswith("id_ed25519"):
                    raise HttpError(403, "禁止访问证书密钥文件")
            if clean.endswith(".key") or clean.endswith(".pem"):
                raise HttpError(403, "禁止访问证书密钥文件")
            full = base_dir / clean
            try:
                resolved = full.resolve()
                resolved_base = base_dir.resolve()
                resolved.relative_to(resolved_base)
            except (ValueError, OSError):
                raise HttpError(403, "禁止逃出目录（软链接或路径越界）")

            if not full.is_file():
                raise HttpError(404, "文件不存在")
            max_bytes = 200 * 1024
            try:
                with open(full, "rb") as fh:
                    raw = fh.read(max_bytes + 1)
            except OSError as exc:
                raise HttpError(500, f"读取文件出错：{exc}")
            truncated = len(raw) > max_bytes
            raw = raw[:max_bytes]
            text = raw.decode("utf-8", errors="replace")
            res_data = {
                "path": clean,
                "text": text,
                "truncated": truncated,
                "from": from_type,
            }
        self._json(200, res_data, head=head)

    # ---- 查 diff 接口（GET /api/v2/diff） ----
    def _v2_diff(self, query, head):
        task_id = _q1(query, "task")
        diff_path = _q1(query, "path")
        if not task_id or not diff_path:
            raise HttpError(400, "缺少任务编号或文件路径")
        demo = is_demo(query)
        try:
            source = self.server.source_for(demo)
        except Exception as exc:  # noqa: BLE001
            raise HttpError(500, "读不到数据（%s）" % exc)
        with source.session() as session:
            try:
                from . import oldboard_conv
                data = session.run(lambda conn: oldboard_conv.diff(conn, task_id, diff_path))
            except Exception:
                data = {"diff": ""}
        self._json(200, data, head=head)

    # ---- 总闸现状（页面顶栏红按钮） ----
    def _halt(self, query, head):
        try:
            source = self.server.source_for(is_demo(query))
        except Exception as exc:  # noqa: BLE001
            raise HttpError(500, "读不到数据：示例数据没能启动（%s）" % exc)
        with source.session() as session:
            info = session.run(halt_info)
        self._json(200, info, head=head)

    # ---- 看板左侧的对话列表（M5） ----
    def _convs(self, query, head):
        try:
            source = self.server.source_for(is_demo(query))
        except Exception as exc:  # noqa: BLE001
            raise HttpError(500, "读不到数据：示例数据没能启动（%s）" % exc)
        with source.session() as session:
            data = session.run(office_board.conv_list)
        self._json(200, data, head=head)

    # ---- 员工详情（页面左侧面板） ----
    def _employee(self, query, head):
        em, conv = _q1(query, "em"), _q1(query, "conv")
        if not valid_em(em):
            raise HttpError(422, "缺少员工编号，或员工编号格式不对")
        demo = is_demo(query)
        try:
            source = self.server.source_for(demo)
        except Exception as exc:  # noqa: BLE001
            LOG.exception("示例账本启动失败")
            raise HttpError(500, "读不到数据：示例数据没能启动（%s）" % exc)
        live_root = None if demo else self.server.live_root
        quota_home = None if demo else self.server.quota_home
        try:
            with source.session() as session:
                data = session.run(lambda conn: office_employee.build(conn, em, conv=conv, live_root=live_root, quota_home=quota_home))
        except office_employee.EmployeeNotFound as exc:
            raise HttpError(404, str(exc) or "找不到这位员工")
        self._json(200, data, head=head)

    # ---- 任务拆解树（画布/顶栏/任务拆解） ----
    def _plan(self, query, head):
        conv = _q1(query, "conv")
        demo = is_demo(query)
        try:
            source = self.server.source_for(demo)
        except Exception as exc:  # noqa: BLE001
            LOG.exception("示例账本启动失败")
            raise HttpError(500, "读不到数据：示例数据没能启动（%s）" % exc)
        try:
            with source.session() as session:
                data = session.run(lambda conn: office_employee.plan(conn, conv=conv))
        except office_employee.ConvNotFound as exc:
            raise HttpError(404, str(exc) or "找不到该对话")
        self._json(200, data, head=head)

    # ---- SSE ----
    def _sse_start(self):
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Accel-Buffering", "no")
        self.send_header("Connection", "close")
        self._common_headers()
        self.end_headers()
        self.close_connection = True
        self._streaming = True
        return self._sse_write("retry: 3000\n: connected\n\n")

    def _sse_write(self, text):
        try:
            self.wfile.write(text.encode("utf-8"))
            self.wfile.flush()
            return True
        except (OSError, ValueError):
            return False

    def _client_gone(self):
        try:
            readable, _, _ = select.select([self.connection], [], [], 0)
            if readable:
                return self.connection.recv(1, socket.MSG_PEEK) == b""
        except (OSError, ValueError):
            return True
        return False

    @staticmethod
    def _frame(event, data, event_id=None):
        head = ("id: %s\n" % event_id) if event_id is not None else ""
        return "%sevent: %s\ndata: %s\n\n" % (head, event, json.dumps(data, ensure_ascii=False))

    def _cursor(self, query):
        values = [v for v in (_int(_q1(query, "since")), _int(self.headers.get("Last-Event-ID"))) if v is not None]
        return max(values) if values else None

    def _stream(self, query):
        srv = self.server
        ws, conv = _q1(query, "ws"), _q1(query, "conv")
        cursor = self._cursor(query)
        try:
            source = srv.source_for(is_demo(query))
        except Exception as exc:  # noqa: BLE001
            LOG.exception("示例账本启动失败")
            raise HttpError(500, "读不到数据：示例数据没能启动（%s）" % exc)
        with source.session() as session:
            if cursor is None:
                cursor = session.run(core_events.max_seq)       # 没给起点：只看从现在开始的新事件
            generation = session.generation
            if not self._sse_start():
                return
            last_beat, failures = time.monotonic(), 0
            while not srv.stop_event.is_set():
                try:
                    if session.generation != generation:        # 示例账本换了一本新的，从头跟
                        generation, cursor = session.generation, 0
                    top = session.run(core_events.max_seq)
                    if top < cursor:                            # 起点比账本还新（页面是旧的），从当前开始
                        cursor = top
                    if top > cursor:
                        batch = session.run(lambda c, cur=cursor: core_events.since(c, cur, ws=ws, conv=conv, limit=500))
                        frames = session.run(lambda c, b=batch: stream_frames(c, b))
                        for seq, event, payload in frames:
                            if not self._sse_write(self._frame(event, payload, seq)):
                                return
                        cursor = batch[-1]["seq"] if batch else top
                        last_beat = time.monotonic()
                        failures = 0
                        if batch and len(batch) >= 500:
                            continue
                    failures = 0
                except sqlite3.Error as exc:
                    failures += 1
                    LOG.warning("事件流读账本出错（第 %d 次）：%s", failures, exc)
                    if failures >= 20:
                        return
                if time.monotonic() - last_beat >= srv.heartbeat_seconds:
                    if not self._sse_write(self._frame("heartbeat", {"ts": time.time()})):
                        return
                    last_beat = time.monotonic()
                if self._client_gone():
                    return
                if srv.stop_event.wait(srv.poll_seconds):
                    return

    def _live(self, query):
        srv = self.server
        has_em = "em" in query
        has_ems = "ems" in query
        if has_em and has_ems:
            raise HttpError(422, "缺少员工编号，或员工编号格式不对")
        if not has_em and not has_ems:
            raise HttpError(422, "缺少员工编号，或员工编号格式不对")

        demo = is_demo(query)
        # tail=N：第一次补发最近多少行（1~500，默认 200；终端小屏只要十几行）
        backlog = _int(_q1(query, "tail"))
        backlog = BACKLOG_LINES if backlog is None else max(1, min(500, backlog))
        with_plain = _q1(query, "plain") in ("1", "true")

        if has_em:
            em = _q1(query, "em")
            if not valid_em(em):
                raise HttpError(422, "缺少员工编号，或员工编号格式不对")
            since = self._cursor(query)
            tail = None if demo else LiveTail(srv.live_root, em, since, backlog=backlog)   # 示例模式没有直播内容，只发心跳
            # plain=1：每行多带一句大白话 plain（页面"正在想 / 正在做"条用）；不带这个参数时帧内容和以前完全一样
            narrator = office_plain.LiveNarrator() if with_plain else None
            if not self._sse_start():
                return
            last_beat = time.monotonic()
            while not srv.stop_event.is_set():
                for item in (tail.poll() if tail else []):
                    if narrator is not None:
                        line = narrator.feed(item)
                        if line:
                            item = dict(item, plain=line)
                    if not self._sse_write(self._frame("live", item, item["n"])):
                        return
                    last_beat = time.monotonic()
                if time.monotonic() - last_beat >= srv.heartbeat_seconds:
                    if not self._sse_write(self._frame("heartbeat", {"ts": time.time()})):
                        return
                    last_beat = time.monotonic()
                if self._client_gone():
                    return
                if srv.stop_event.wait(srv.poll_seconds):
                    return
        else:
            raw_ems = _q1(query, "ems")
            if not raw_ems:
                raise HttpError(422, "缺少员工编号，或员工编号格式不对")
            parts = raw_ems.split(",")
            targets = {}
            for part in parts:
                part = part.strip()
                m = _EMS_ITEM_RE.match(part)
                if not m:
                    raise HttpError(422, "缺少员工编号，或员工编号格式不对")
                target_em = m.group(1)
                if target_em not in targets:
                    s_seg = int(m.group(2)) if m.group(2) is not None else None
                    s_line = int(m.group(3)) if m.group(3) is not None else None
                    targets[target_em] = (s_seg, s_line)
            if not targets or len(targets) > 64:
                raise HttpError(422, "缺少员工编号，或员工编号格式不对")

            tails = {}
            narrators = {}
            if not demo:
                for target_em, (s_seg, s_line) in targets.items():
                    tails[target_em] = LiveTail(srv.live_root, target_em, since=s_line, backlog=backlog, since_seg=s_seg)
                    if with_plain:
                        narrators[target_em] = office_plain.LiveNarrator()

            if not self._sse_start():
                return
            last_beat = time.monotonic()
            while not srv.stop_event.is_set():
                for target_em, tail in tails.items():
                    for item in tail.poll():
                        if with_plain:
                            narrator = narrators.get(target_em)
                            if narrator is not None:
                                line = narrator.feed(item)
                                if line:
                                    item = dict(item, plain=line)
                        item = dict(item, em=target_em)
                        if not self._sse_write(self._frame("live", item)):
                            return
                        last_beat = time.monotonic()
                if time.monotonic() - last_beat >= srv.heartbeat_seconds:
                    if not self._sse_write(self._frame("heartbeat", {"ts": time.time()})):
                        return
                    last_beat = time.monotonic()
                if self._client_gone():
                    return
                if srv.stop_event.wait(srv.poll_seconds):
                    return

    # ---- 静态文件 ----
    def _static(self, path, head):
        root = self.server.web_root
        if path == "/":
            if (root / "watch.html").is_file():
                path = "/web/watch.html"
            elif (root / "board.html").is_file():
                path = "/web/board.html"
            else:
                return self._send(302, b"", "text/plain; charset=utf-8", head=head, extra=(("Location", "/org"),))
        if path == "/favicon.ico":
            self.send_response(204)
            self._common_headers()
            self.end_headers()
            return
        status, target = resolve_static(path, root)
        if status == "forbidden":
            raise HttpError(403, "不允许访问这个路径")
        if status != "ok":
            raise HttpError(404, ERROR_TEXT[404])
        try:
            size = target.stat().st_size
            fh = open(target, "rb")
        except OSError:
            raise HttpError(404, ERROR_TEXT[404])
        with fh:
            self.send_response(200)
            self.send_header("Content-Type", content_type_for(target))
            self.send_header("Content-Length", str(size))
            self.send_header("Cache-Control", "no-cache")
            self._common_headers()
            self.end_headers()
            if head:
                return
            while True:
                chunk = fh.read(65536)
                if not chunk:
                    break
                self.wfile.write(chunk)
