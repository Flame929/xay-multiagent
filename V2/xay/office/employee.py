"""员工详情（页面左侧面板）：GET /api/v2/employee?em=&conv= 的数据。

一个人做过的每件事按时间倒序排成一行（点开才有：交代的事 → 结果 → 改了哪些文件 → 关键几步 → 原话和技术细节）。
同一件事试了几次（标题相同、前一次没做成或被撤回）合成一行，经过写在 attempts 里（group_attempts）。
标题只留给人看的那部分（plain.short_title：去掉"汇报时……"这类写给 AI 的要求）。
- V2 自己的活：账本里的任务卡、task.progress / task.reported / task.landed 事件、每一轮的证据，
  再加 $XAY2_HOME/live/<em>/*.jsonl（工位写的直播行，按每一轮的时间窗口截取）；
- 旧系统的活（桥镜像过来的，legacy）：只读旧 run 目录里的 prompt.md / events.norm.jsonl / result.md / meta.json。
所有文字都过 plain.py：去 Markdown、路径只留文件名、技术词翻成大白话、不出现原始命令和 JSON。
这里只读：不写账本，不写旧系统目录。
"""
import copy
import json
import posixpath
import time
from collections import OrderedDict
from pathlib import Path

from xay.core import events as core_events
from xay.core import snapshot as core_snapshot
from xay.core.reducers import MEMORY_TURNS, dept_key, legacy_status

from . import budget as bud
from . import duty as office_duty
from . import plain
from .live import ALLOWED_KINDS, MAX_TEXT, valid_em

MAX_TIMELINE = 25                 # 最多返回最近这么多件事
MAX_FILES = 30
DETAIL_MAX = 8000                 # "看技术细节"里原文最多这么多字
READ_MAX = {"prompt.md": 200_000, "result.md": 400_000, "meta.json": 1_000_000, "events.norm.jsonl": 8_000_000}
LIVE_MAX_BYTES = 2_000_000
LIVE_MAX_FILES = 4
TASK_EVENT_TYPES = ("task.added", "task.progress", "task.reported", "task.landed", "task.kept", "task.failed",
                    "task.accepted", "task.canceled", "task.removed")
TERMINAL = ("done", "reported", "failed", "canceled", "needs_you")
RETRYABLE = ("failed", "canceled")        # 前一次是这两种状态、标题又一样：后面那件算"再试一次"，合成一行
DONE = ("done", "reported")


class EmployeeNotFound(LookupError):
    pass


class ConvNotFound(LookupError):
    pass


# ---------------------------------------------------------------- 小工具

def _num(value):
    return value if isinstance(value, (int, float)) and not isinstance(value, bool) else None


def _names(paths):
    seen = []
    for p in paths or []:
        if isinstance(p, str):
            name = plain.bare_name(p)
            if name and name not in seen:
                seen.append(name)
    return seen[:MAX_FILES]


def _dept_name(state, conv, dept_id):
    if not dept_id or not conv:
        return ""
    rec = state.get("depts", {}).get(dept_key(conv, dept_id))
    return (rec or {}).get("name") or ""


def _detail(text):
    text = plain.redact(text or "").strip()
    return text if len(text) <= DETAIL_MAX else text[:DETAIL_MAX] + "\n……（后面的内容省略了）"


# ---------------------------------------------------------------- 旧系统 run 目录（只读）

_RUN_CACHE = OrderedDict()
_RUN_CACHE_MAX = 48


def _safe_run_dir(run_dir):
    """只认旧系统的 <项目>/.xay/runs/<run> 目录；相对路径、带 .. 的、不在这个结构里的一律不读。"""
    if not isinstance(run_dir, str) or not run_dir or "\x00" in run_dir:
        return None
    path = Path(run_dir)
    if not path.is_absolute() or any(part == ".." for part in path.parts):
        return None
    try:
        real = path.resolve(strict=True)
    except (OSError, RuntimeError):
        return None
    if not real.is_dir() or real.parent.name != "runs" or real.parent.parent.name != ".xay":
        return None
    return real


def _read_run_file(real, name):
    """只读 run 目录里这几个固定名字的文件；软链接指到目录外的不读。返回 (文本或 None, 修改时间, 大小)。"""
    try:
        target = (real / name).resolve(strict=True)
        if target.parent != real or not target.is_file():
            return None, 0, 0
        st = target.stat()
        with open(target, "rb") as fh:
            raw = fh.read(READ_MAX[name])
    except (OSError, RuntimeError):
        return None, 0, 0
    return raw.decode("utf-8", "replace"), st.st_mtime_ns, st.st_size


def read_legacy_run(run_dir):
    """旧 run 目录 → {ask, steps, report, files, title, fail_reason, started, ended}；读不到返回 None。"""
    real = _safe_run_dir(run_dir)
    if real is None:
        return None
    parts = {name: _read_run_file(real, name) for name in READ_MAX}
    key = (str(real),) + tuple((parts[n][1], parts[n][2]) for n in READ_MAX)
    hit = _RUN_CACHE.get(key)
    if hit is not None:
        _RUN_CACHE.move_to_end(key)
        return hit
    prompt, meta_raw, report, events_raw = (parts[n][0] for n in ("prompt.md", "meta.json", "result.md", "events.norm.jsonl"))
    if prompt is None and meta_raw is None and report is None and events_raw is None:
        return None
    try:
        meta = json.loads(meta_raw) if meta_raw else {}
    except ValueError:
        meta = {}
    if not isinstance(meta, dict):
        meta = {}
    events = []
    for line in (events_raw or "").splitlines():
        if not line.strip():
            continue
        try:
            row = json.loads(line)
        except ValueError:
            continue
        if isinstance(row, dict):
            events.append(row)
    files = _names(meta.get("changed_files") if isinstance(meta.get("changed_files"), list) else [])
    if not files:
        files = _names([e.get("path") for e in events if e.get("type") == "file.changed" and isinstance(e.get("path"), str)])
    started, duration = _num(meta.get("started")), _num(meta.get("duration_s"))
    out = {
        "ask": plain.ask_from_prompt(prompt) if prompt else "",
        "steps": plain.steps_from_legacy_events(events),
        "report": (report or "").strip(),
        "files": files,
        "title": plain.plain(str(meta.get("title") or "")),
        "fail_reason": str(meta.get("fail_reason") or ""),
        "started": started,
        "ended": started + duration if started is not None and duration is not None else None,
    }
    _RUN_CACHE[key] = out
    while len(_RUN_CACHE) > _RUN_CACHE_MAX:
        _RUN_CACHE.popitem(last=False)
    return out


# ---------------------------------------------------------------- 直播文件

def _segment_key(path):
    try:
        mtime = path.stat().st_mtime
    except OSError:
        mtime = 0
    return (int(path.stem) if path.stem.isdigit() else -1, mtime)


def read_live_rows(live_root, em):
    """$XAY2_HOME/live/<em>/ 里最近几段的行，按时间排好；坏行跳过，每个文件最多读尾部 2 MB。"""
    return scan_live(live_root, em)[0]


def scan_live(live_root, em):
    """→ (直播行, 用量行)。用量行 {turn, tokens, ts}（工位 LiveWriter.usage 写的，2026-10-31），按时间排好。"""
    if live_root is None or not valid_em(em):
        return [], []
    d = Path(live_root) / em
    try:
        files = sorted((p for p in d.iterdir() if p.suffix == ".jsonl" and p.is_file()), key=_segment_key)[-LIVE_MAX_FILES:]
    except OSError:
        return [], []
    rows, usage = [], []
    for path in files:
        try:
            size = path.stat().st_size
            with open(path, "rb") as fh:
                if size > LIVE_MAX_BYTES:
                    fh.seek(size - LIVE_MAX_BYTES)
                    fh.readline()                      # 丢掉被截断的第一行
                raw = fh.read(LIVE_MAX_BYTES)
        except OSError:
            continue
        for i, line in enumerate(raw.splitlines()):
            try:
                row = json.loads(line.decode("utf-8", "replace"))
            except ValueError:
                continue
            if not isinstance(row, dict):
                continue
            ts = _num(row.get("ts"))
            if ts is None:
                continue
            if row.get("kind") == "usage":
                tokens = _num(row.get("tokens"))
                if isinstance(row.get("turn"), str) and tokens is not None and tokens >= 0:
                    u_row = {"turn": row["turn"], "tokens": int(tokens), "ts": ts}
                    out_tok = _num(row.get("out"))
                    fresh_tok = _num(row.get("fresh"))
                    if out_tok is not None and out_tok >= 0:
                        u_row["out"] = int(out_tok)
                    if fresh_tok is not None and fresh_tok >= 0:
                        u_row["fresh"] = int(fresh_tok)
                    usage.append(u_row)
                continue
            if row.get("kind") not in ALLOWED_KINDS:
                continue
            text = row.get("text")
            rows.append({"n": _num(row.get("n")) or i, "kind": row["kind"], "ts": ts,
                         "text": text[:MAX_TEXT] if isinstance(text, str) else "",
                         "tool_kind": row.get("tool_kind") if isinstance(row.get("tool_kind"), str) else None,
                         "tool": row.get("tool") if isinstance(row.get("tool"), str) else ""})
    rows.sort(key=lambda r: (r["ts"], r["n"]))
    usage.sort(key=lambda r: r["ts"])
    return rows, usage


# ---------------------------------------------------------------- 账本里的任务

def _task_events(conn, conv, ids):
    """本对话里这些任务的相关事件，按任务分好（按 seq 从小到大）。"""
    out = {i: [] for i in ids}
    if not ids or not conv:
        return out
    marks = ",".join("?" * len(TASK_EVENT_TYPES))
    rows = conn.execute("SELECT seq, ts, type, subject, data FROM events WHERE conv=? AND type IN (%s) ORDER BY seq" % marks,
                        (conv,) + TASK_EVENT_TYPES).fetchall()
    for r in rows:
        if r["subject"] in out:
            try:
                data = json.loads(r["data"])
            except ValueError:
                data = {}
            out[r["subject"]].append({"seq": r["seq"], "ts": r["ts"], "type": r["type"], "data": data if isinstance(data, dict) else {}})
    return out


def _verify_steps(evidence, ts):
    steps = []
    for item in (evidence.get("verify") or []):
        if not isinstance(item, dict):
            continue
        cmd, ok = item.get("cmd"), item.get("ok")
        if not isinstance(cmd, str) or not cmd.strip():
            continue
        info = plain.classify_cmd(cmd) if plain.looks_like_command(cmd) else None
        if info and info["cat"] in ("test", "check", "script"):
            label, cat = info["text"], info["cat"]
        else:
            label, cat = plain.cut(plain.plain(cmd), 60), "check"
        mark = "" if ok is None else ("：通过 ✓" if ok else "：没通过 ✗")
        steps.append({"icon": plain.ICONS.get(cat, "🔍"), "text": label + mark, "ts": int(ts or 0)})
    return steps


def _event_data(evs, typ):
    hit = next((e for e in reversed(evs) if e["type"] == typ), None)
    return (hit or {}).get("data") or {}


def _strip_label(text, *labels):
    text = (text or "").strip()
    for label in labels:
        for sep in ("：", ":"):
            if text.startswith(label + sep):
                return text[len(label) + 1:].strip()
    return text


def _status_result(t, files, evs=()):
    """没有汇报原文时，按任务状态说一句话。返回 (claim, summary, detail)。"""
    status = t.get("status")
    if status in ("reported", "done"):
        landed = any(e["type"] == "task.landed" for e in evs)
        extra = ("，改了 %d 个文件" % len(files) if files else "") + ("，已经放进项目" if landed and files else "")
        note = plain.plain(_strip_label(_event_data(evs, "task.accepted").get("note") or "", "验收"), 60)
        return "done", "做完了%s。" % extra + ("负责人看过了：%s。" % note.rstrip("。") if note else ""), ""
    if status == "needs_you":
        why = plain.first_sentences(plain.plain(t.get("kept_detail") or ""), 1)
        return "kept", "改动先保留下来了，等你决定。" + why, t.get("kept_detail") or ""
    if status == "failed":
        raw = t.get("failure_detail") or t.get("failure_reason") or ""
        why = plain.why_failed(raw)
        return "failed", "没做成" + ("：" + why if why else "") + "。", raw
    if status == "canceled":
        reason = plain.plain(_event_data(evs, "task.removed").get("reason") or "", 60).rstrip("。")
        return "canceled", "这件事撤回了" + ("：" + reason if reason else "") + "。", ""
    if status == "working":
        return "working", "", ""
    return "waiting", "还没开始做。", ""


def _attempt_note(status, ran, summary):
    """经过里每一次的一句话。"""
    if status == "canceled":
        return summary.replace("这件事撤回了", "派了又撤回了", 1) if not ran else "做到一半停掉了。"
    if status in DONE:
        return "做完了。"
    if status == "failed":
        return summary or "没做成。"
    if status == "needs_you":
        return "改动先保留下来了，等你决定。"
    if status == "working":
        return "正在做。"
    return "排着队，还没开始。"


def _v2_entry(state, conv, t, evs, rows, live_on, person_dept, now):
    tk = t["id"]
    turns = sorted((tr for tr in state.get("turns", {}).values() if tr.get("task") == tk), key=lambda x: x.get("started") or 0)
    added_ts = next((e["ts"] for e in evs if e["type"] == "task.added"), None)
    started = turns[0].get("started") if turns else None
    ended = None
    if t.get("status") in TERMINAL and turns:
        ended = max((tr.get("ended") or 0) for tr in turns) or None
    landed = next((e for e in reversed(evs) if e["type"] == "task.landed"), None)
    files = _names((landed or {}).get("data", {}).get("files"))
    if not files:
        for tr in turns:
            files.extend(n for n in _names((tr.get("evidence") or {}).get("changed_files")) if n not in files)
        files = files[:MAX_FILES]

    steps = []
    if live_on:
        for tr in turns:
            lo, hi = (tr.get("started") or 0) - 1, (tr.get("ended") or now) + 2
            steps.extend(plain.steps_from_live_rows([r for r in rows if lo <= r["ts"] <= hi], limit=10 ** 6, skip_progress=True))
    has_write = any(s["icon"] == plain.ICONS["write"] for s in steps)
    last_text = None
    progress_texts = []
    for e in evs:
        if e["type"] != "task.progress":
            continue
        d = e["data"]
        if _num(d.get("step")) is None:
            continue
        text = "第 %s 步 / 共 %s 步" % (int(d["step"]), int(d["of"])) if _num(d.get("of")) else "第 %s 步" % int(d["step"])
        body = plain.progress_text(d.get("text") or "")
        if body:
            progress_texts.append(body)
        text = plain.cut(text + ("：" + body if body else ""), 80)
        if text != last_text:
            steps.append({"icon": plain.ICONS["progress"], "text": text, "ts": int(e["ts"] or 0)})
            last_text = text
    for tr in turns:
        ev = tr.get("evidence") or {}
        for v in _verify_steps(ev, tr.get("ended")):
            label = v["text"].split("：")[0]
            twin = next((s for s in reversed(steps) if s["text"] == label), None)   # 直播里已有同一步（没带结果）：补上结果
            if twin is not None:
                twin["text"] = v["text"]
            else:
                steps.append(v)
        if not has_write:
            names = _names(ev.get("changed_files"))
            text = plain._names_text(names, "改了 %s", "改了 %d 个文件：%s")
            if text:
                steps.append({"icon": plain.ICONS["write"], "text": text, "ts": int(tr.get("ended") or 0)})
    steps.sort(key=lambda s: s["ts"])
    steps = plain.cap_steps(steps)

    report = t.get("result") or ""
    claim, summary = plain.summary_from_report(report)
    claim = t.get("claim") or claim
    detail = _detail(report)
    status_line = ""
    if not summary:
        c2, summary, raw = _status_result(t, files, evs)
        claim = claim or c2
        detail = detail or _detail(raw)
        status_line = summary
    elif t.get("status") == "needs_you":
        claim = "kept"
    elif t.get("status") == "failed":
        claim = "failed"
    elif t.get("status") in ("queued", "planned", "ready") and not claim:
        claim = "waiting"
    elif t.get("status") == "working" and not claim:
        claim = "working"

    dept = t.get("dept") if _dept_name(state, conv, t.get("dept")) else (person_dept or None)
    ask = plain.ask_from_prompt(t.get("card") or "") or plain.first_sentences(plain.drop_parentheses(plain.plain(t.get("title") or "")), 1)
    ran = bool(turns)
    if t.get("status") in ("failed", "canceled", "needs_you") and not status_line:
        status_line = _status_result(t, files, evs)[1]
    return {
        "task": {"id": tk, "title": plain.plain(t.get("title") or "") or "任务", "short_title": plain.short_title(t.get("title") or "") or "任务",
                 "status": t.get("status"), "legacy": False, "dept": dept, "dept_name": _dept_name(state, conv, dept),
                 "started": started or added_ts, "ended": ended},
        "ask": ask,
        "steps": steps,
        "key_steps": plain.key_steps(progress_texts, steps),
        "result": {"claim": claim or "", "summary": summary, "detail": detail, "short": _short_result(claim, summary, status_line)},
        "files": files,
        "_ran": ran, "_note": _attempt_note(t.get("status"), ran, status_line), "_order": added_ts or started or 0,
    }, (started or added_ts or 0)


def _short_result(claim, summary, status_line):
    """结果那一句：没做成 / 撤回 / 等你决定 用状态句；做完了用汇报里第一句有内容的话。"""
    if claim in ("failed", "canceled", "kept") and status_line:
        return plain.one_line(status_line, 80)
    return plain.one_line(summary, 80)


def legacy_task_id(run):
    """旧 run 在快照里的任务编号（和 core.snapshot 一致）：run:<目录名>。"""
    run_dir = (run.get("run_dir") or "").rstrip("/")
    return "run:" + (posixpath.basename(run_dir) if run_dir else (run.get("run") or run.get("em") or ""))


def _legacy_entry(state, conv, run, person_dept):
    rid = legacy_task_id(run)
    status = legacy_status(run)
    data = read_legacy_run(run.get("run_dir")) or {}
    report = data.get("report") or ""
    claim, summary = plain.summary_from_report(report)
    if not summary and run.get("result_head"):
        claim2, summary = plain.summary_from_report(run["result_head"])
        claim = claim or claim2
    fail = data.get("fail_reason") or ""
    status_line = ""
    if status == "failed":
        why = plain.why_failed(fail)
        status_line = "没做成" + ("：" + why if why else "") + "。"
        if not summary:
            summary = "这次没有做成，原因见下面的技术细节。"
        elif claim == "done":
            summary = "系统记为没做成（中途出了错），但它自己的汇报是：" + summary
        claim = "failed"
    elif status == "canceled":
        status_line = "这次被中途停掉了。"
        claim, summary = "canceled", summary or status_line
    elif status == "working":
        claim = "working"
    elif status == "queued":
        claim, summary = "waiting", summary or "还在排队，没开始做。"
    elif not claim:
        claim = "done"
    detail = _detail(report or run.get("result_head") or "")
    if fail and status == "failed":
        detail = (detail + "\n\n" if detail else "") + _detail("失败原因：" + fail)
    dept = run.get("dept") if _dept_name(state, conv, run.get("dept")) else (person_dept or None)
    started = _num(run.get("started")) or data.get("started")
    ended = _num(run.get("ended")) or (data.get("ended") if status != "working" else None)
    title = plain.plain(run.get("title") or "") or data.get("title") or "任务"
    ask = data.get("ask") or plain.first_sentences(title, 1)
    steps = data.get("steps") or []
    ran = status not in ("queued",)
    return {
        "task": {"id": rid, "title": title, "short_title": plain.short_title(title) or "任务", "status": status, "legacy": True,
                 "dept": dept, "dept_name": _dept_name(state, conv, dept), "started": started, "ended": ended},
        "ask": ask,
        "steps": steps,
        "key_steps": plain.key_steps([], steps),
        "result": {"claim": claim, "summary": summary, "detail": detail, "short": _short_result(claim, summary, status_line)},
        "files": data.get("files") or [],
        "_ran": ran, "_note": _attempt_note(status, ran, status_line), "_order": started or _num(run.get("ts")) or 0,
    }, (started or _num(run.get("ts")) or 0)


# ---------------------------------------------------------------- 同一件事试了几次：合成一行

def _title_key(entry):
    t = entry["task"]
    return (bool(t.get("legacy")), plain.squash(t.get("short_title") or t.get("title") or "").rstrip("。.").lower())


def group_attempts(entries):
    """[(entry, 排序时间)] → 合并后的 [(entry, 排序时间)]。

    规则（页面工位上的小清单 utils.js mergeAttempts 用同一条）：按时间从早到晚看，同一个人、同一个对话里标题相同、
    而且前一次没做成或被撤回（failed / canceled），后一次就算"再试一次"，合进同一行；前一次已经做成了的，后面同名的是新活，另起一行。
    合成的一行用最后一次的内容；attempts 按先后列出每一次；tries 只数真的动手做过的次数。"""
    groups, open_by_key = [], {}
    for entry, ts in sorted(entries, key=lambda x: x[1]):
        key = _title_key(entry)
        g = open_by_key.get(key)
        if g is not None and g["members"][-1][0]["task"].get("status") in RETRYABLE:
            g["members"].append((entry, ts))
        else:
            g = {"members": [(entry, ts)]}
            groups.append(g)
        open_by_key[key] = g
    out = []
    for g in groups:
        last, _ = g["members"][-1]
        item = {k: v for k, v in last.items() if not k.startswith("_")}
        item["attempts"] = [{"id": e["task"]["id"], "status": e["task"].get("status"), "started": e["task"].get("started"),
                             "ended": e["task"].get("ended"), "ran": bool(e.get("_ran")), "note": e.get("_note") or ""}
                            for e, _ in g["members"]]
        item["tries"] = sum(1 for a in item["attempts"] if a["ran"])
        first_start = next((a["started"] for a in item["attempts"] if a["started"]), None)
        if first_start and len(item["attempts"]) > 1:
            item["task"] = dict(item["task"], first_started=first_start)
        out.append((item, max(ts for _, ts in g["members"])))
    return out


def number_rows(grouped, entries):
    """给合并后的行编号（详情栏"第几件 / 共几件"）：按这件事第一次派下来的时间（不是开工时间，免得开工后序号变），
    撤回的不算，从 1 开始。原地加 n；返回件数。entries 是合并前的 [(entry, ts)]，用来查每次的派活时间。"""
    order = {e["task"]["id"]: e.get("_order") or ts or 0 for e, ts in entries}
    def key(pair):
        item = pair[0]
        return min((order.get(a["id"], 0) for a in item.get("attempts") or []), default=0), item["task"].get("id") or ""
    n = 0
    for item, _ts in sorted(grouped, key=key):
        if item["task"].get("status") == "canceled":
            item["n"] = None
            continue
        n += 1
        item["n"] = n
    return n


def count_rows(items):
    """{all, done, working, queued, failed, needs_you, canceled}：按合并后的行数（同一件事重试算一件），all 不算撤回的。"""
    out = {"all": 0, "done": 0, "working": 0, "queued": 0, "failed": 0, "needs_you": 0, "canceled": 0}
    for item in items:
        status = item["task"].get("status")
        if status == "canceled":
            out["canceled"] += 1
            continue
        out["all"] += 1
        if status in DONE:
            out["done"] += 1
        elif status in ("queued", "ready", "planned"):
            out["queued"] += 1
        elif status in out:
            out[status] += 1
    return out


def focus_row(items, state):
    """"现在"块显示哪一件（返回它的 n，没有就 None）：正在做的 → 他在排队时的下一件 → 最近结束的一件 → 排着队的第一件。
    items 是按最近动静从新到旧排好的行。"""
    rows = [it for it in items if it.get("n")]
    working = [it for it in rows if it["task"].get("status") == "working"]
    waiting = sorted((it for it in rows if it["task"].get("status") in ("queued", "ready", "planned")), key=lambda it: it["n"])
    finished = [it for it in rows if it["task"].get("status") in DONE + ("failed", "needs_you")]
    if working:
        return max(it["n"] for it in working)
    if state == "queued" and waiting:
        return waiting[0]["n"]
    if finished:
        return finished[0]["n"]
    return waiting[0]["n"] if waiting else None


# ---------------------------------------------------------------- 额度（详情栏"额度剩多少"）

SEES_THINKING = {"codex": True, "dsh": True, "agy": False}      # 和各适配器的 capabilities.thinking 一致
LINE_NAMES = {"codex-sub": "Plus", "codex-relay": "中转站", "dsh": "DeepSeek", "agy": "Gemini"}


def _own_quota_line(conn, em):
    try:
        row = conn.execute("SELECT data FROM events WHERE type='quota.updated' AND subject=? ORDER BY seq DESC LIMIT 1", (em,)).fetchone()
        d = json.loads(row[0]) if row else {}
    except Exception:  # noqa: BLE001  读不到就当没有
        return None
    return d.get("line") if isinstance(d, dict) and isinstance(d.get("line"), str) else None


def agy_calls(quota_home):
    """今天 Gemini 用了几次、上限几次（和工位的 xay.quota.agy_calls_today 同一个数，但这里只读、不建目录）。"""
    from xay import quota as q
    day = time.strftime("%Y-%m-%d")
    used = q._count_rows(Path(quota_home) / "quota" / "agy-calls.jsonl", day, None) + q._count_rows(q._legacy_registry(), day, "agy_call")
    return used, q.agy_daily_cap()


def person_quota(conn, state, snap, em, agent, quota_home=None):
    """详情栏"额度"格（2026-10-31 起四条线路都是真数，见 xay/quota_live.py）：按员工所在的线路取快照 quota 里那一条——
    Codex 看他现在的线路（现状里的 line → 他工位最近报的线路 → Plus），DeepSeek 看 dsh，Gemini 看 agy（当前账号 + 各账号）。
    → {kind: pct, line, line_name, left, status, windows:[{key, name, left, resets_at}], fetched_at, stale, source,
       balance?, warning?, error?, account?, accounts?, calls?}；
    还没读到 → {kind: none, line, line_name, why}；Gemini 没读到时退回今天的调用次数 {kind: calls, left, used, cap}。
    calls（Gemini 今天还能调几次）quota_home=None 时不读（示例模式）。"""
    if agent == "codex":
        line = ((state.get("employees") or {}).get(em) or {}).get("line") or _own_quota_line(conn, em) or "codex-sub"
    else:
        line = agent if agent in ("dsh", "agy") else None
    base = {"line": line, "line_name": LINE_NAMES.get(line, line or "")}
    rec = next((q for q in snap.get("quota") or [] if q.get("line") == line), None) if line else None
    if rec and rec.get("remaining_pct") is not None:
        out = dict(base, kind="pct", left=rec["remaining_pct"], status=rec.get("status"), fetched_at=rec.get("fetched_at"),
                   stale=bool(rec.get("stale")), reached=rec.get("reached"), source=rec.get("source_label"),
                   windows=[{"key": w["key"], "name": w["name"], "left": w["remaining_pct"], "resets_at": w.get("resets_at")}
                            for w in rec.get("windows") or []])
        for k in ("balance", "warning", "error"):
            if rec.get(k):
                out[k] = rec[k]
    elif rec and rec.get("error"):
        out = dict(base, kind="none", why="没查到：" + str(rec["error"])[:40])
    else:
        out = dict(base, kind="none", why="还没读到")
    if agent == "agy":
        if rec and rec.get("account"):
            out["account"] = rec["account"]
        if rec and rec.get("accounts"):
            out["accounts"] = rec["accounts"]
        if quota_home is not None:
            try:
                used, cap = agy_calls(quota_home)
            except Exception:  # noqa: BLE001
                used, cap = None, None
            if cap:
                calls = {"left": max(0, cap - used), "used": used, "cap": cap}
                if out["kind"] == "none":
                    out.update(kind="calls", **calls)          # 还没读到 Gemini 的额度：先看今天的调用次数
                else:
                    out["calls"] = calls
    return out


def _local_day(ts):
    return time.strftime("%Y-%m-%d", time.localtime(ts)) if ts else ""


def today_counts(items, now):
    """今天（本机时间）做完几件、没做成几件；等你决定的不管哪天都算（还没处理完）。按合并后的行数，不按尝试次数。"""
    today = _local_day(now)
    out = {"done": 0, "failed": 0, "needs_you": 0, "working": 0, "queued": 0}
    for item in items:
        t = item["task"]
        status = t.get("status")
        when = t.get("ended") or t.get("started")
        if status == "needs_you":
            out["needs_you"] += 1
        elif status == "working":
            out["working"] += 1
        elif status in ("queued", "ready", "planned"):
            out["queued"] += 1
        elif status in DONE and _local_day(when) == today:
            out["done"] += 1
        elif status == "failed" and _local_day(when) == today:
            out["failed"] += 1
    return out


# ---------------------------------------------------------------- 总任务拆解树（详情栏顶上，2026-10-31）

TREE_STEP_TEXT = 40               # 每一步最多这么多字
TREE_MAX_STEPS = 12
TREE_MAX_DEPTH = 3                # 依赖再深也只缩进这么多层
TREE_MAX_AFTER = 3
LOOSE_NAME = "未分部门"


def _tree_steps(t, prog_events):
    """一件活的步骤 → (step, of, [{n, text, state: done|cur|stop|todo}])。
    名字优先用 planned_steps（AI 自己拆的），没有才用任务卡写好的 steps，
    再没有才用员工报进度时那一步说的话，都没有就只有编号；做完了的活每一步都算做完，
    没在做（排队、没做成、等你决定）时停在的那一步记 stop。"""
    raw_planned = t.get("planned_steps") or []
    if raw_planned:
        planned = [plain.cut(plain.plain(s.get("text", "") if isinstance(s, dict) else str(s)), TREE_STEP_TEXT)
                   for s in raw_planned if (isinstance(s, dict) and s.get("text")) or (isinstance(s, str) and s.strip())]
    else:
        planned = [plain.cut(plain.plain(s), TREE_STEP_TEXT) for s in (t.get("steps") or []) if isinstance(s, str) and s.strip()]
    prog = t.get("progress") or {}
    step = int(prog["step"]) if _num(prog.get("step")) is not None and prog["step"] > 0 else None
    if raw_planned:
        of = len(planned) if planned else None
    else:
        of = int(prog["of"]) if _num(prog.get("of")) is not None and prog["of"] > 0 else None
    texts = {}
    for e in prog_events:
        d = e["data"]
        if e["type"] != "task.progress" or _num(d.get("step")) is None:
            continue
        body = plain.progress_text(d.get("text") or "", TREE_STEP_TEXT)
        if body:
            texts[int(d["step"])] = body
    total = min(TREE_MAX_STEPS, max(of or 0, len(planned), step or 0))
    status = t.get("status")
    steps = []
    for i in range(1, total + 1):
        if status in DONE or (step is not None and i < step):
            state = "done"
        elif step is not None and i == step:
            state = "cur" if status == "working" else "stop"
        else:
            state = "todo"
        steps.append({"n": i, "text": planned[i - 1] if i <= len(planned) else texts.get(i, ""), "state": state})
    return step, (of or total or None), steps


def _tree_dept(state, conv, dept, owner):
    """这件活挂在哪个部门下：任务写的部门（本对话开过的）→ 负责人在本对话的部门 → 未分部门（""）。和工位、清单的放法一样。"""
    if dept and dept_key(conv, dept) in (state.get("depts") or {}):
        return dept
    mine = (((state.get("employees") or {}).get(owner) or {}).get("depts") or {}).get(conv) if owner else None
    return mine if mine and dept_key(conv, mine) in (state.get("depts") or {}) else ""


def _format_item_nodes(items, by_id, alias, group_names):
    if not items:
        return []
    items_copy = [dict(n) for n in items]
    pos = {n["id"]: i for i, n in enumerate(items_copy)}
    parent, children = {}, {}
    for i, n in enumerate(items_copy):
        deps = []
        for d in n.get("_deps", []):
            d = alias.get(d, d)
            if d != n["id"] and d in by_id and d not in deps:
                deps.append(d)
        n["deps"] = deps
        inner = [d for d in deps if d in pos and pos[d] < i]
        if inner:
            parent[n["id"]] = max(inner, key=lambda d: pos[d])
            children.setdefault(parent[n["id"]], []).append(n)
        n["after"] = [{"id": d, "title": by_id[d]["title"], "group": "" if d in pos else group_names.get(by_id[d].get("_dept"), "")}
                      for d in deps if d != parent.get(n["id"])][:TREE_MAX_AFTER]
    flat = []

    def walk(n, depth):
        n["depth"] = min(depth, TREE_MAX_DEPTH)
        n["chained"] = n["id"] in parent
        flat.append(n)
        kids = children.get(n["id"], [])
        for ch in kids:
            walk(ch, depth + (1 if len(kids) > 1 else 0))
    for n in items_copy:
        if n["id"] not in parent:
            walk(n, 0)
    return [{k: v for k, v in n.items() if not k.startswith("_")} for n in flat]


def task_tree(state, conv, em, evs, now=None):
    """这次总任务的拆解树：{title, count, groups:[{id, name, mine, owners, count, items:[…]}]}。
    每个 item 是一件事（同一个人、标题相同、前一次没做成或撤回的合成一件，和 group_attempts 同一条规则；撤回的不列）：
    {id, title, status, legacy, owner, owner_name, former, mine, tries, step, of, steps, depth, chained, after:[{id, title, group}]}。
    同一个部门里依赖前面某件的排在它下面、chained=true（页面画"↳"）；一件后面接好几件时这几件缩一层（depth）；
    依赖别的部门的、或不是挂靠的那一件的，写在 after 里。evs 是 _task_events 的结果。"""
    if now is None:
        now = time.time()
    c = (state.get("convs") or {}).get(conv) or {}
    emps = state.get("employees") or {}
    tasks = [t for t in (state.get("tasks") or {}).values() if t.get("conv") == conv]
    by_task, _ = bud.index_turns(state)
    keymap = {t["key"]: t["id"] for t in tasks if t.get("key")}
    info, per_owner = {}, OrderedDict()
    for t in tasks:
        turns = by_task.get(t["id"], [])
        started = turns[0].get("started") if turns else None
        added = next((e["ts"] for e in evs.get(t["id"], []) if e["type"] == "task.added"), None)
        entry = {"task": {"id": t["id"], "title": t.get("title") or "", "short_title": plain.short_title(t.get("title") or "") or "任务",
                          "status": t.get("status"), "legacy": False, "started": started or added, "ended": None},
                 "_ran": bool(turns), "_note": ""}
        info[t["id"]] = {"owner": t.get("owner"), "dept": t.get("dept"), "rec": t, "order": added or started or 0,
                         "deps": [keymap.get(d, d) for d in (t.get("deps") or []) if isinstance(d, str)]}
        per_owner.setdefault(t.get("owner"), []).append((entry, started or added or 0))
    for r in (state.get("legacy_runs") or {}).values():
        if r.get("conv") != conv:
            continue
        rid, status = legacy_task_id(r), legacy_status(r)
        ts = _num(r.get("started")) or _num(r.get("ts")) or 0
        title = plain.plain(r.get("title") or "")
        entry = {"task": {"id": rid, "title": title, "short_title": plain.short_title(title) or "任务", "status": status,
                          "legacy": True, "started": ts, "ended": None}, "_ran": status != "queued", "_note": ""}
        info[rid] = {"owner": r.get("em"), "dept": r.get("dept"), "rec": {"status": status}, "order": ts, "deps": []}
        per_owner.setdefault(r.get("em"), []).append((entry, ts))

    alias, nodes = {}, []
    for owner, entries in per_owner.items():
        for item, _ts in group_attempts(entries):
            last = item["task"]["id"]
            ids = [a["id"] for a in item["attempts"]]
            for a in ids:
                alias[a] = last
            if item["task"].get("status") == "canceled":
                continue
            rec = info[last]["rec"]
            step, of, steps = _tree_steps(rec, evs.get(last, [])) if not item["task"].get("legacy") else (None, None, [])
            emp = emps.get(owner) or {}

            # detail 字段计算
            card_raw = rec.get("card")
            if isinstance(card_raw, dict):
                card_text = str(card_raw.get("text") or card_raw.get("card") or "")
            else:
                card_text = str(card_raw or "")
            card_text = card_text[:1500]

            verify_cmds = []
            if isinstance(card_raw, dict) and isinstance(card_raw.get("verify"), list):
                verify_cmds = [v for v in card_raw["verify"] if isinstance(v, str)]
            elif isinstance(rec.get("verify"), list):
                verify_cmds = [v for v in rec["verify"] if isinstance(v, str)]

            rep_ev = None
            for a in reversed(ids):
                rep_ev = next((e for e in reversed(evs.get(a, [])) if e.get("type") == "task.reported"), None)
                if rep_ev:
                    break
            summary = (rep_ev["data"].get("summary") or "") if rep_ev else str(rec.get("result") or "")

            land_ev = None
            for a in reversed(ids):
                land_ev = next((e for e in reversed(evs.get(a, [])) if e.get("type") == "task.landed"), None)
                if land_ev:
                    break
            landed_files = (land_ev["data"].get("files") or []) if land_ev else []
            landed_files = [f for f in landed_files if isinstance(f, str)][:30]

            all_turns = [tr for a in ids for tr in by_task.get(a, [])]
            turn_starts = [tr["started"] for tr in all_turns if tr.get("started")]
            first_started = min(turn_starts) if turn_starts else None

            ended_ts = None
            t_status = item["task"].get("status")
            if t_status in ("done", "reported"):
                if land_ev:
                    ended_ts = land_ev.get("ts")
                elif all_turns and all_turns[-1].get("ended"):
                    ended_ts = all_turns[-1]["ended"]
                elif rep_ev:
                    ended_ts = rep_ev.get("ts")
            elif t_status in ("failed", "canceled"):
                fail_ev = None
                for a in reversed(ids):
                    fail_ev = next((e for e in reversed(evs.get(a, [])) if e.get("type") in ("task.failed", "task.canceled")), None)
                    if fail_ev:
                        break
                if fail_ev:
                    ended_ts = fail_ev.get("ts")
                elif all_turns and all_turns[-1].get("ended"):
                    ended_ts = all_turns[-1]["ended"]

            work_s = 0.0
            for tr in all_turns:
                if tr.get("kind") in MEMORY_TURNS:
                    continue
                st = tr.get("started")
                if not st:
                    continue
                et = tr.get("ended") if (tr.get("status") != "working" and tr.get("ended")) else now
                work_s += max(0.0, et - st)
            work_s = round(work_s, 1)

            tries = item.get("tries", 1)
            assigned_reason = str(rec.get("reason") or rec.get("assigned_reason") or "")
            chk_conclusion = copy.deepcopy(rec.get("check") or {'state': 'none', 'round': 0, 'text': '', 'by': ''})

            task_detail = {
                "card": card_text,
                "verify": verify_cmds,
                "summary": summary,
                "plain": rec.get("plain") or "",
                "files": landed_files,
                "started": first_started,
                "ended": ended_ts,
                "work_s": work_s,
                "tries": tries,
                "assigned_reason": assigned_reason,
                "check": chk_conclusion,
                "planned": [s.get("text", "") if isinstance(s, dict) else str(s) for s in (rec.get("planned_steps") or [])],
            }

            nodes.append({"id": last, "title": item["task"]["short_title"], "status": item["task"].get("status"),
                          "legacy": bool(item["task"].get("legacy")), "owner": owner, "owner_name": emp.get("label") or "",
                          "former": bool(emp.get("former")), "mine": bool(owner) and owner == em, "tries": item["tries"],
                          "step": step, "of": of, "steps": steps,
                          "detail": task_detail,
                          "_dept": _tree_dept(state, conv, info[last]["dept"], owner),
                          "_part": rec.get("part"),
                          "_order": min(info[a]["order"] or 0 for a in ids), "_deps": [d for a in ids for d in info[a]["deps"]]})
    by_id = {n["id"]: n for n in nodes}

    # 小任务 parts 计算
    conv_parts = [p for p in state.get("parts", {}).values() if p.get("conv") == conv]
    is_old = len(conv_parts) == 0
    depts_active = [d for d in state.get("depts", {}).values() if d.get("conv") == conv and not d.get("closed")]
    active_dept_ids = {d["id"] for d in depts_active}
    dept_name_map = {d["id"]: d.get("name") or d["id"] for d in depts_active}

    key_to_id = {p["key"]: p["id"] for p in conv_parts if p.get("key")}
    valid_pids = {p["id"] for p in conv_parts}

    for n in nodes:
        if is_old:
            pid = "all"
        else:
            p_val = n.get("_part")
            target = key_to_id.get(p_val, p_val)
            pid = target if target in valid_pids else ""
        n["part"] = pid

    groups = OrderedDict()
    for d in (state.get("depts") or {}).values():
        if d.get("conv") == conv:
            groups[d["id"]] = {"id": d["id"], "name": d.get("name") or d["id"], "items": []}
    groups[""] = {"id": "", "name": LOOSE_NAME, "items": []}
    for n in sorted(nodes, key=lambda x: (x["_order"], x["id"])):
        groups.setdefault(n["_dept"], {"id": n["_dept"], "name": n["_dept"], "items": []})["items"].append(n)

    group_names = {gid: g["name"] for gid, g in groups.items()}
    out_groups = []
    for g in groups.values():
        items = g["items"]
        if not items:
            continue
        flat = _format_item_nodes(items, by_id, alias, group_names)
        owners = []
        for n in flat:
            if n["owner_name"] and n["owner_name"] not in owners:
                owners.append(n["owner_name"])
        out_groups.append({"id": g["id"], "name": g["name"], "mine": any(n["mine"] for n in flat), "owners": owners,
                           "count": count_rows([{"task": {"status": n["status"]}} for n in flat]),
                           "items": flat})

    # 把 nodes 分配到各 part
    nodes_by_part = OrderedDict()
    for n in nodes:
        nodes_by_part.setdefault(n["part"], []).append(n)

    raw_parts = core_snapshot.build_parts(state, conv)
    tree_parts = []
    for pm in raw_parts:
        pid = pm["id"]
        nodes_in_part = nodes_by_part.get(pid, [])
        part_dept_ids = list(pm.get("depts") or [])
        part_depts = []
        for did in part_dept_ids:
            dept_nodes = [n for n in nodes_in_part if n.get("_dept") == did]
            dept_items = _format_item_nodes(dept_nodes, by_id, alias, dept_name_map)
            part_depts.append({
                "id": did,
                "name": dept_name_map.get(did, did),
                "items": dept_items,
            })
        outer_nodes = [n for n in nodes_in_part if n.get("_dept") not in set(part_dept_ids)]
        outer_items = _format_item_nodes(outer_nodes, by_id, alias, dept_name_map)
        mine = any(n.get("mine") for n in nodes_in_part)
        tree_parts.append({
            "id": pm["id"],
            "title": pm["title"],
            "col": pm["col"],
            "row": pm["row"],
            "status": pm["status"],
            "check": copy.deepcopy(pm["check"]),
            "detail": copy.deepcopy(pm.get("detail", {})),
            "mine": mine,
            "depts": part_depts,
            "items": outer_items,
        })

    return {"title": plain.short_title(c.get("title") or "") or "这次的活",
            "count": count_rows([{"task": {"status": n["status"]}} for n in nodes]),
            "groups": out_groups,
            "parts": tree_parts}


# ---------------------------------------------------------------- token 速度 / 消耗（详情栏，2026-10-31）

SPEED_WINDOW_S = 60               # 速度 = 最近这么多秒里这一轮的累计用量涨了多少 ÷ 秒数
SPEED_MIN_S = 5                   # 一轮刚开工不到这么多秒：还算不出速度
MINUTE_CELLS = 30                 # 方格：最近 30 分钟，一分钟一格


def _turn_breakdown(tr, live_pts, live_max):
    """一轮的用量拆解：{used, out, fresh, cached, spent}；都没有返回 None。
    - 收工后的数字用 turn.ended.usage 的 in / out / cached 算同样四项；
    - 进行中的轮用直播用量行（out / fresh）算；
    - 预算 used 照旧（取 total、turn_tokens、直播行最大）。"""
    u = tr.get("usage")
    tid = tr.get("id")
    if isinstance(u, dict) and u:
        total_tok = int(u.get("total") or 0)
        out_tok = int(u.get("out") or 0)
        cached_tok = int(u.get("cached") or 0)
        in_tok = int(u.get("in") or 0)
        if not total_tok:
            total_tok = in_tok + out_tok + cached_tok
        fresh_tok = max(0, total_tok - out_tok - cached_tok)
        cached_tok = max(0, total_tok - out_tok - fresh_tok)
        spent_tok = fresh_tok + out_tok
        used_tok = max(total_tok, int(tr.get("turn_tokens") or 0), live_max.get(tid, 0))
        return {"used": used_tok, "out": out_tok, "fresh": fresh_tok, "cached": cached_tok, "spent": spent_tok}
    if live_pts:
        r = live_pts[-1]
        tokens = int(r.get("tokens") or 0)
        out_tok = int(r.get("out") or 0)
        fresh_tok = int(r.get("fresh") or 0)
        used_tok = max(tokens, int(tr.get("turn_tokens") or 0))
        cached_tok = max(0, used_tok - out_tok - fresh_tok)
        spent_tok = fresh_tok + out_tok
        return {"used": used_tok, "out": out_tok, "fresh": fresh_tok, "cached": cached_tok, "spent": spent_tok}
    if _num(tr.get("turn_tokens")) is not None and tr.get("turn_tokens") >= 0:
        used_tok = int(tr["turn_tokens"])
        return {"used": used_tok, "out": 0, "fresh": 0, "cached": used_tok, "spent": 0}
    return None


def _sum_tokens(turns, live_pts_by_turn, live_max):
    """→ (used, out, fresh, cached, spent, partial)。一轮的数都没有 → (None, 0, 0, 0, 0, False)。"""
    vals = [_turn_breakdown(tr, live_pts_by_turn.get(tr.get("id"), []), live_max) for tr in turns]
    known = [v for v in vals if v is not None]
    if not known:
        return None, 0, 0, 0, 0, False
    return (sum(v["used"] for v in known),
            sum(v["out"] for v in known),
            sum(v["fresh"] for v in known),
            sum(v["cached"] for v in known),
            sum(v["spent"] for v in known),
            len(known) < len(vals))


def _meter(used, limit, partial, out=0, fresh=0, cached=0, spent=0):
    limit = int(limit) if _num(limit) is not None and limit > 0 else None
    return {
        "used": int(used),
        "limit": limit,
        "pct": round(100.0 * used / limit, 1) if limit else None,
        "partial": bool(partial),
        "out": int(out),
        "fresh": int(fresh),
        "cached": int(cached),
        "spent": int(spent),
    }


def _speed(usage_rows, turn, started, now):
    """这一轮最近 60 秒平均每秒写出 / 新读了多少 token；没有 out 数据的旧用量行不算速度（返回 None），开工不到 5 秒 → None。"""
    pts = [r for r in usage_rows if r["turn"] == turn and r["ts"] <= now + 1]
    if not pts:
        return None
    if any(r.get("out") is None for r in pts):
        return None
    lo = now - SPEED_WINDOW_S
    before = [r for r in pts if r["ts"] <= lo]
    if before:
        base_ts = lo
        base_out = max(r.get("out") or 0 for r in before)
        base_fresh = max(r.get("fresh") or 0 for r in before)
    else:
        base_ts = max(lo, started or pts[0]["ts"])
        base_out = 0
        base_fresh = 0
    span = now - base_ts
    if span < SPEED_MIN_S:
        return None
    cur_out = max(r.get("out") or 0 for r in pts)
    cur_fresh = max(r.get("fresh") or 0 for r in pts)
    out_per_s = round(max(0, cur_out - base_out) / span, 1)
    fresh_per_s = round(max(0, cur_fresh - base_fresh) / span, 1)
    return {"out_per_s": out_per_s, "fresh_per_s": fresh_per_s, "window_s": int(round(span))}


def _per_minute(usage_rows, now):
    """最近 30 分钟每分钟按 spent（fresh + out）用了多少 token（按整分钟对齐，最后一格是这一分钟）→ (格子, 第一格开始的时间)；这段时间没有用量行 → (None, None)。
    每一行记的是这一轮到那时的累计数，所以一格 = 这一分钟里各轮累计数涨了多少（一轮的第一行从 0 算起）。"""
    end = (int(now) // 60 + 1) * 60
    start = end - MINUTE_CELLS * 60
    cells, last, seen = [0] * MINUTE_CELLS, {}, False
    for r in usage_rows:
        out_v = r.get("out")
        fresh_v = r.get("fresh")
        if out_v is not None or fresh_v is not None:
            spent = (out_v or 0) + (fresh_v or 0)
        else:
            spent = r.get("tokens") or 0
        prev = last.get(r["turn"], 0)
        last[r["turn"]] = max(prev, spent)
        if start <= r["ts"] < end:
            cells[int((r["ts"] - start) // 60)] += max(0, spent - prev)
            seen = True
    return (cells, start) if seen else (None, None)


def person_tokens(state, em, now, attempts, usage_rows, cfg=None):
    """详情栏的 token：{task?, today?, speed?, minutes?, minutes_from?}，拿不到的那项不给。
    - task：这件活（"现在"块那件，重试的几次加起来）用了多少，对着这件活的 token 预算；
    - today：他今天（本机 0 点起开工的轮）用了多少，对着每人每天的 token 预算；今天没干过活就是 0；
      Gemini 不按 token 管预算（budget.NO_TOKEN_LINE，额度按每天调用次数），两项都只给数、limit / pct 为 None；
    - speed：正在干活时这一轮最近 60 秒的平均速度 {out_per_s, fresh_per_s, window_s}（只来自直播文件的用量行，没有 out 不给）；
    - minutes：最近 30 分钟每分钟按 spent（fresh + out）的用量（方格）。
    task / today 里带 out, fresh, cached, spent，预算 used 照旧；有哪一轮没报数时 partial=true（页面写"约"）。"""
    if cfg is None:
        try:
            from . import reactions
            cfg = reactions.load()
        except Exception:  # noqa: BLE001  预算设置读不到：只给数，不给占比
            cfg = None
    by_task, by_em = bud.index_turns(state)
    emp = (state.get("employees") or {}).get(em) or {}
    live_max = {}
    live_pts_by_turn = {}
    for r in usage_rows:
        tn = r["turn"]
        live_max[tn] = max(live_max.get(tn, 0), r["tokens"])
        live_pts_by_turn.setdefault(tn, []).append(r)
    out = {}
    tasks = state.get("tasks") or {}
    v2 = [a for a in attempts or [] if a in tasks]
    if v2:
        turns = [tr for tk in v2 for tr in by_task.get(tk, []) if tr.get("kind") not in MEMORY_TURNS]
        used, out_tok, fresh_tok, cached_tok, spent_tok, partial = _sum_tokens(turns, live_pts_by_turn, live_max)
        if used is not None:
            limit = bud.task_limits(cfg, tasks[v2[-1]], state).get("tokens") if cfg else None   # Gemini 没有 token 线：None
            out["task"] = _meter(used, limit, partial, out=out_tok, fresh=fresh_tok, cached=cached_tok, spent=spent_tok)
    today = [tr for tr in by_em.get(em, []) if (tr.get("started") or 0) >= bud.day_start(now)]
    if today:
        used, out_tok, fresh_tok, cached_tok, spent_tok, partial = _sum_tokens(today, live_pts_by_turn, live_max)
    else:
        used, out_tok, fresh_tok, cached_tok, spent_tok, partial = 0, 0, 0, 0, 0, False
    if used is not None:
        out["today"] = _meter(used, bud.employee_limits(cfg, emp, now).get("tokens") if cfg else None, partial,
                              out=out_tok, fresh=fresh_tok, cached=cached_tok, spent=spent_tok)
    cur = (state.get("turns") or {}).get(emp.get("turn")) if emp.get("turn") else None
    if cur and cur.get("status") == "working":
        speed = _speed(usage_rows, cur.get("id") or emp.get("turn"), cur.get("started"), now)
        if speed is not None:
            out["speed"] = speed
    cells, start = _per_minute(usage_rows, now)
    if cells is not None:
        out["minutes"], out["minutes_from"] = cells, start
    return out


def _notes_by_label(by):
    if not by:
        return None
    if by == "user" or by.startswith("user:"):
        return "用户"
    if by == "lead" or by.startswith("lead:"):
        return "负责人"
    if by == "station" or by.startswith("station:"):
        return "工位"
    if by == "employee" or by.startswith("employee:"):
        return "员工"
    return by


def build_memory(conn, em, state):
    """详情栏记忆数据：完整笔记文本、最近修改时间与修改者、交接摘要、压缩历史。"""
    notes = (state.get("notes", {}).get(em) or {}).get("active") or ""
    row = conn.execute(
        "SELECT seq, ts, by, data FROM events WHERE type='notes.updated' AND subject=? ORDER BY seq DESC LIMIT 1",
        (em,)
    ).fetchone()
    if row:
        notes_ts = row["ts"]
        notes_by = _notes_by_label(row["by"])
    else:
        notes_ts = None
        notes_by = None

    compact_rows = conn.execute(
        "SELECT seq, ts, data FROM events WHERE type='context.compacted' AND subject=? ORDER BY seq ASC",
        (em,)
    ).fetchall()
    compactions = []
    for idx, r in enumerate(compact_rows, 1):
        d = json.loads(r["data"]) if r["data"] else {}
        compactions.append({
            "n": idx,
            "ts": r["ts"],
            "method": d.get("method"),
            "before_pct": d.get("before_pct"),
            "after_pct": d.get("after_pct"),
        })

    handoff = None
    if compact_rows:
        last_compact = compact_rows[-1]
        last_d = json.loads(last_compact["data"]) if last_compact["data"] else {}
        if last_d.get("method") in ("handoff", "machine"):
            prev_note = conn.execute(
                "SELECT data FROM events WHERE type='notes.updated' AND subject=? AND seq < ? ORDER BY seq DESC LIMIT 1",
                (em, last_compact["seq"])
            ).fetchone()
            if prev_note:
                prev_d = json.loads(prev_note[0]) if prev_note[0] else {}
                handoff = prev_d.get("active")

    return {
        "notes": notes,
        "notes_ts": notes_ts,
        "notes_by": notes_by,
        "handoff": handoff,
        "compactions": compactions,
    }


# ---------------------------------------------------------------- 入口

def build(conn, em, *, conv=None, live_root=None, now=None, quota_home=None):
    """返回 {v, seq, now, conv, person, timeline, total, today, count, focus, memory}。员工不存在时抛 EmployeeNotFound。
    timeline 是合并过的（同一件事试了几次算一行），total 是合并后的行数。
    详情栏第 3 版另加：每行 n（第几件，撤回的是 None）、count（共几件、做完几件……）、focus（"现在"块显示第几件）、
    person.quota（额度剩多少；quota_home=None 时 Gemini 的次数不读，示例模式用）、person.sees_thinking、person.line。
    2026-10-31 又加：tree（这次总任务的拆解树，task_tree）、person.tokens（token 消耗 / 速度，person_tokens）。
    记忆：memory（完整笔记、最近谁改的、最近一次交接摘要、整理记忆的历史，build_memory）。"""
    if not valid_em(em):
        raise EmployeeNotFound("员工编号格式不对")
    state = core_events.load_state(conn)
    emp = state.get("employees", {}).get(em)
    if emp is None:
        raise EmployeeNotFound("找不到这位员工")
    ws = emp.get("ws")
    if not conv:
        conv = core_snapshot.pick_conv(state, ws)
    snap = core_snapshot.build(conn, ws=ws, conv=conv, now=now)
    now = snap["now"]
    raw = snap["people"].get(em)
    if raw is None:                                   # 已离职：快照里只剩 former
        raw = {"key": em, "label": emp.get("label") or em, "agent": emp.get("agent") or "codex", "state": "fired",
               "display_state": "fired", "now": "", "context": {}, "progress": {}, "online": False, "queue": []}
    dept = (emp.get("depts") or {}).get(conv)
    notes = plain.notes_text((state.get("notes", {}).get(em) or {}).get("active") or "", 600)
    person = {
        "key": em, "label": raw.get("label"), "agent": raw.get("agent"), "dept": dept, "dept_name": _dept_name(state, conv, dept),
        "state": raw.get("state"), "display_state": raw.get("display_state") or raw.get("state"),
        "now": plain.short_title(raw.get("now") or ""), "context": raw.get("context") or {}, "progress": raw.get("progress") or {},
        "online": bool(raw.get("online")), "queue": len(raw.get("queue") or []), "notes": notes, "fired": bool(emp.get("former")),
        "duty": raw.get("duty") or office_duty.calc_duty(conn, state, em, now=now),
    }
    if person["progress"].get("text"):
        person["progress"] = dict(person["progress"], text=plain.progress_text(person["progress"]["text"]))
    agent = person["agent"] or "codex"
    person["sees_thinking"] = SEES_THINKING.get(agent, True)
    person["quota"] = person_quota(conn, state, snap, em, agent, quota_home)
    person["line"] = person["quota"].get("line")
    person["budget"] = raw.get("budget") or {}

    v2_tasks = [t for t in state.get("tasks", {}).values() if t.get("owner") == em and t.get("conv") == conv]
    legacy = [r for r in state.get("legacy_runs", {}).values() if r.get("em") == em and r.get("conv") == conv]
    conv_ids = [t["id"] for t in state.get("tasks", {}).values() if t.get("conv") == conv]
    evs = _task_events(conn, conv, conv_ids)          # 整个对话的：拆解树也要用
    rows, usage_rows = scan_live(live_root, em)
    entries = []
    for t in v2_tasks:
        entries.append(_v2_entry(state, conv, t, evs.get(t["id"], []), rows, bool(rows), dept, now))
    for r in legacy:
        entries.append(_legacy_entry(state, conv, r, dept))
    items = group_attempts(entries)
    number_rows(items, entries)                       # 按第一次派活的先后编号
    items.sort(key=lambda x: x[1], reverse=True)
    rows_out = [e[0] for e in items]
    focus = focus_row(rows_out, person["state"])
    focus_item = next((it for it in rows_out if focus and it.get("n") == focus), None)
    person["tokens"] = person_tokens(state, em, now, [a["id"] for a in (focus_item or {}).get("attempts") or []], usage_rows)
    return {"v": 1, "seq": snap["seq"], "now": now, "conv": conv, "person": person,
            "timeline": rows_out[:MAX_TIMELINE], "total": len(rows_out), "today": today_counts(rows_out, now),
            "count": count_rows(rows_out), "focus": focus, "tree": task_tree(state, conv, em, evs, now=now) if conv else None,
            "memory": build_memory(conn, em, state)}


def plan(conn, *, conv=None):
    """GET /api/v2/plan?conv=<对话> 的数据：返回 {conv, title, tree}。"""
    state = core_events.load_state(conn)
    if not conv:
        conv = core_snapshot.pick_conv(state)
    if not conv or conv not in state.get("convs", {}):
        raise ConvNotFound("找不到该对话")
    c = state["convs"][conv]
    conv_ids = [t["id"] for t in state.get("tasks", {}).values() if t.get("conv") == conv]
    evs = _task_events(conn, conv, conv_ids)
    tree = task_tree(state, conv, None, evs, now=time.time())
    return {
        "conv": conv,
        "title": c.get("title") or "",
        "tree": tree,
    }

