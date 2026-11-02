#!/usr/bin/env python3
"""xay 公司组织与部门协作：文件存储、需求单和接口投影。"""
import argparse
import fcntl
import hashlib
import json
import os
import secrets
import subprocess
import sys
import time
from pathlib import Path



def _subject(text, limit=40):
    """消息标题：取正文第一句，最多 limit 个字。"""
    line = next((x.strip() for x in str(text or "").splitlines() if x.strip()), "")
    for mark in "。！？；;":
        if mark in line:
            line = line.split(mark, 1)[0]
    return line if len(line) <= limit else line[: limit - 1] + "…"

def home():
    return Path(os.environ.get("XAY_HOME", str(Path.home() / ".xay"))).expanduser()


def lead_session(value=None):
    return str(value or os.environ.get("XAY_LEAD_SESSION") or os.environ.get("XAY_SESSION") or os.environ.get("CLAUDE_CODE_SESSION_ID") or os.environ.get("CODEX_THREAD_ID") or "default")


def root(session=None):
    return home() / "org" / lead_session(session)


def _read(path, default):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return default


def _locked_json(path, update):
    path.parent.mkdir(parents=True, exist_ok=True)
    with (path.with_suffix(path.suffix + ".lock")).open("a+", encoding="utf-8") as fh:
        fcntl.flock(fh.fileno(), fcntl.LOCK_EX)
        data = _read(path, {"departments": []})
        data = update(data) or data
        with path.open("w", encoding="utf-8") as out:
            json.dump(data, out, ensure_ascii=False, indent=2)
            out.write("\n")
            out.flush()
        fcntl.flock(fh.fileno(), fcntl.LOCK_UN)
    return data


def org(session=None):
    data = _read(root(session) / "org.json", {"departments": []})
    if not isinstance(data, dict):
        data = {"departments": []}
    data.setdefault("departments", [])
    return data


def create_dept(dept_id, name, mission, head="rules", owns=None, session=None):
    now = int(time.time())
    def change(data):
        departments = data.setdefault("departments", [])
        if any(d.get("id") == dept_id for d in departments):
            raise ValueError("部门已存在：" + dept_id)
        departments.append({"id": dept_id, "name": name, "mission": mission, "head": head,
                            "members": [], "owns": owns or [], "status": "active", "created": now})
        return data
    result = _locked_json(root(session) / "org.json", change)
    notice(dept_id, "开工：" + _subject(mission or name, 24), "本部门交付：%s；接口约定：%s" % (mission, ", ".join(owns or []) or "无"), session=session)
    return next(d for d in result["departments"] if d["id"] == dept_id)


def close_dept(dept_id, session=None):
    def change(data):
        for dept in data.setdefault("departments", []):
            if dept.get("id") == dept_id:
                dept["status"] = "done"
                return data
        raise ValueError("找不到部门：" + dept_id)
    return _locked_json(root(session) / "org.json", change)


def add_member(dept_id, worker, session=None):
    def change(data):
        for dept in data.setdefault("departments", []):
            if dept.get("id") == dept_id:
                if worker not in dept.setdefault("members", []):
                    dept["members"].append(worker)
                return data
        raise ValueError("找不到部门：" + dept_id)
    return _locked_json(root(session) / "org.json", change)


def _append(message, session=None):
    directory = root(session)
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / "bus.jsonl"
    with path.open("a", encoding="utf-8") as fh:
        fcntl.flock(fh.fileno(), fcntl.LOCK_EX)
        fh.write(json.dumps(message, ensure_ascii=False) + "\n")
        fh.flush()
        fcntl.flock(fh.fileno(), fcntl.LOCK_UN)
    return message


def _sender():
    return {"dept": os.environ.get("XAY_DEPT") or "gm", "worker": os.environ.get("XAY_WORKER") or "gm",
            "run": os.environ.get("XAY_RUN_DIR") or ""}


def send_message(to, subject, body, kind="notice", reply_to=None, verdict=None, contract=None, session=None,
                 recipient_worker=None, thread_id=None, sender=None):
    stamp = int(time.time())
    msg = {"id": "m_%d_%s" % (stamp, secrets.token_hex(2)), "ts": stamp, "from": sender or _sender(), "to": to,
           "type": kind, "subject": subject, "body": str(body)[:600], "contract": contract,
           "reply_to": reply_to, "verdict": verdict, "status": "open"}
    if recipient_worker:
        msg["to_worker"] = recipient_worker
    if kind == "tell" and not thread_id:
        thread_id = msg["id"]
    if thread_id:
        msg["thread_id"] = thread_id
    return _append(msg, session)


def notice(dept, subject, body, session=None):
    return send_message(dept, subject, body, kind="notice", session=session)


def messages(session=None):
    path = root(session) / "bus.jsonl"
    try:
        rows = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]
    except (OSError, ValueError):
        return []
    decisions = {m.get("reply_to"): m for m in rows if m.get("type") == "decision" and m.get("reply_to")}
    delivered = {m.get("message_id") or m.get("reply_to"): m for m in rows if m.get("type") == "delivered"}
    for row in rows:
        delivery = delivered.get(row.get("id"))
        if delivery:
            row["delivered"] = True
            row["delivered_at"] = delivery.get("ts")
        elif row.get("type") in ("tell", "reply", "decision") and row.get("to_worker"):
            row["delivered"] = False
        decision = decisions.get(row.get("id"))
        if decision:
            row["status"] = "closed"
            row["verdict"] = decision.get("verdict")
            row["answer"] = decision.get("body")
    return rows


def ask(to, body, contract=None, session=None):
    return send_message(to, _subject(body), body, kind="request", contract=contract, session=session)


def answer(message_id, verdict, body, session=None):
    original = next((m for m in messages(session) if m.get("id") == message_id), None)
    if not original:
        raise ValueError("找不到消息：" + message_id)
    return send_message(original.get("from", {}).get("dept") or "gm", "结论：" + _subject(original.get("subject") or "", 24), body, kind="decision",
                        reply_to=message_id, verdict=verdict, contract=original.get("contract"), session=session,
                        recipient_worker=original.get("from", {}).get("worker"),
                        sender=_answer_sender(original))


def _answer_sender(original):
    """拍板的是收到需求单的那个部门（或总经理），不是运行命令时环境里的身份。"""
    me = _sender()
    target = original.get("to")
    if isinstance(target, str) and target and target != "gm" and me.get("dept") in (None, "", "gm"):
        return {"dept": target, "worker": "部门负责人"}
    return me


def _dept_for_message(dept_id, session=None):
    return next((d for d in org(session).get("departments", []) if d.get("id") == dept_id), None)


def tell(worker, body, session=None):
    sender = _sender()
    dept = _dept_for_message(sender["dept"], session)
    if not dept or sender["worker"] not in dept.get("members", []) or worker not in dept.get("members", []):
        raise PermissionError("留言只能发给本部门员工")
    if worker == sender["worker"]:
        raise ValueError("不能给自己留言")
    message = send_message(sender["dept"], _subject(body), body, kind="tell", session=session,
                           recipient_worker=worker)
    return message


def reply(message_id, body, session=None):
    rows = messages(session)
    original = next((m for m in rows if m.get("id") == message_id and m.get("type") in ("tell", "reply")), None)
    if not original:
        raise ValueError("找不到留言：" + message_id)
    sender = _sender()
    dept = _dept_for_message(sender["dept"], session)
    if not dept or sender["dept"] != original.get("from", {}).get("dept") or sender["worker"] not in dept.get("members", []):
        raise PermissionError("只能回复本部门的话题")
    thread_id = original.get("thread_id") or original["id"]
    thread = [m for m in rows if m.get("type") in ("tell", "reply") and
              (m.get("thread_id") or m.get("id")) == thread_id]
    recipient = original.get("from", {}).get("worker")
    if len(thread) >= 2:
        return send_message(sender["dept"], "转部门负责人：" + _subject(body),
                            "一问一答后仍需讨论，已转交部门负责人。\n" + str(body),
                            kind="escalation", reply_to=thread_id, session=session,
                            thread_id=thread_id)
    if recipient == sender["worker"]:
        raise ValueError("请回复对方发来的留言")
    return send_message(sender["dept"], _subject(body), body, kind="reply", reply_to=message_id,
                        session=session, recipient_worker=recipient, thread_id=thread_id)


def escalate(message_id, body, session=None):
    if message_id == "-":
        return send_message("gm", "升级事项", body, kind="escalation", session=session)
    original = next((m for m in messages(session) if m.get("id") == message_id), None)
    if not original:
        raise ValueError("找不到消息：" + message_id)
    return send_message("gm", "升级事项：" + (original.get("subject") or ""), body, kind="escalation",
                        reply_to=message_id, session=session)


def inbox(dept=None, gm=False, session=None):
    target = "gm" if gm else (dept or os.environ.get("XAY_DEPT") or "gm")
    rows = messages(session)
    answered = {m.get("reply_to") for m in rows if m.get("type") == "decision"}
    worker = os.environ.get("XAY_WORKER") if os.environ.get("XAY_ROLE") == "executor" else None
    return [m for m in rows if m.get("status") == "open" and m.get("id") not in answered and
            (m.get("to") == target or m.get("to") == "gm" and target == "gm") and
            (not worker or not m.get("to_worker") or m.get("to_worker") == worker)]


def wait_inbox(dept=None, gm=False, timeout=0, session=None):
    deadline = time.time() + max(0, timeout)
    while True:
        rows = inbox(dept, gm, session)
        if rows or time.time() >= deadline:
            return rows
        time.sleep(0.1)


def wait_for(message_id, timeout, session=None):
    deadline = time.time() + max(0, timeout)
    while True:
        row = next((m for m in messages(session) if m.get("id") == message_id), None)
        decision = next((m for m in messages(session) if m.get("type") == "decision" and m.get("reply_to") == message_id), None)
        if decision: return decision
        if time.time() >= deadline:
            return {"status": "timeout", "message": "未答复，先按现有约定继续，并在汇报里写明"}
        time.sleep(0.1)


def contract_set(cid, owner, source, session=None):
    directory = root(session) / "contracts"; directory.mkdir(parents=True, exist_ok=True)
    target = directory / (cid + ".md")
    if os.environ.get("XAY_DEPT") and os.environ.get("XAY_DEPT") != owner:
        raise PermissionError("只有 owner 部门能改接口约定")
    text = Path(source).read_text(encoding="utf-8")
    with (directory / (cid + ".lock")).open("a+", encoding="utf-8") as lock:
        fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
        target.write_text(text, encoding="utf-8")
        fcntl.flock(lock.fileno(), fcntl.LOCK_UN)
    version = 1
    first = text.splitlines()[0] if text.splitlines() else ""
    if "version:" in first:
        try: version = int(first.split("version:", 1)[1].strip().split()[0])
        except ValueError: pass
    for dept in org(session).get("departments", []):
        notice(dept.get("id"), "接口约定已更新", "%s version %s" % (cid, version), session=session)
    return {"id": cid, "owner": owner, "version": version}


def contract_show(cid, session=None):
    path = root(session) / "contracts" / (cid + ".md")
    if not path.is_file():
        raise ValueError("找不到接口约定：" + cid)
    return path.read_text(encoding="utf-8")


def card_summary(session=None):
    rows = []
    for dept in org(session).get("departments", []):
        owns = ", ".join(dept.get("owns", [])[:3]) or "无"
        rows.append("%s：交付%s；负责文件%s" % (dept.get("name") or dept.get("id"), dept.get("mission") or "未填写", owns))
    return "\n".join(rows)


def _json_lines(path):
    """逐行读 jsonl；某一行写坏只跳过那一行，不连累整张表。"""
    rows = []
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return rows
    for line in text.splitlines():
        if line.strip():
            try:
                rows.append(json.loads(line))
            except ValueError:
                continue
    return rows


def related_sessions(session=None):
    """看板按对话（如 Claude 会话 id）查，派活和建部门却记在 xay 会话（ls_…）下。
    返回 [对话本身, 属于这个对话的 xay 会话（最新的在前）…]。"""
    sid = lead_session(session)
    found = []
    for path in (home() / "sessions").glob("*.json"):
        info = _read(path, {})
        if isinstance(info, dict) and info.get("id") and str(info.get("lead_key") or "").split(":", 1)[-1] == sid:
            found.append((info.get("started") or 0, info["id"]))
    return [sid] + [x for _, x in sorted(found, reverse=True) if x != sid]


def _run_status(row):
    run_dir = Path(row.get("run_dir") or "")
    start = _read(run_dir / "start.json", {})
    meta = _read(run_dir / "meta.json", {})
    raw = meta.get("status") or start.get("status") or row.get("status")
    if (run_dir / "queued").exists() or raw == "queued": return "queued"
    if raw in ("running", "active"): return "running"
    if raw in ("failed", "error", "stopped", "orphaned"): return "failed"
    return "done" if raw else "unknown"


def projection(conversation=None, session=None):
    sessions = related_sessions(session)
    data = next((o for o in (org(x) for x in sessions) if o.get("departments")), org(session))
    org_session = next((x for x in sessions if org(x).get("departments")), session)
    msgs = sorted((m for x in sessions for m in messages(x)), key=lambda m: m.get("ts") or 0)
    members = {m for d in data.get("departments", []) for m in d.get("members", [])}
    wanted = set(sessions)
    registry = [row for row in _json_lines(home() / "registry.jsonl")
                if row.get("session") in wanted or row.get("lead_session") in wanted]
    latest = {}
    for row in registry:
        if row.get("kind") == "run" and row.get("worker"):
            latest[row["worker"]] = row
    runs = [row for row in registry if row.get("kind") == "run" and row.get("run")]
    def tasks_of(worker):
        # 这个员工做过和正在做的每件事（每次派活一条），给页面列任务清单
        return [{"title": row.get("title") or row.get("name") or "任务", "status": _run_status(row),
                 "time": row.get("time")} for row in runs if row.get("worker") == worker][-12:]
    def member(worker):
        row = latest.get(worker, {})
        run_dir = Path(row.get("run_dir") or "")
        start = _read(run_dir / "start.json", {})
        meta = _read(run_dir / "meta.json", {})
        raw = meta.get("status") or start.get("status") or row.get("status")
        status = "idle"
        outstanding = any(m.get("type") == "request" and m.get("status") == "open" and m.get("from", {}).get("worker") == worker and m.get("from", {}).get("dept") == row.get("dept") for m in msgs)
        answered = {m.get("reply_to") for m in msgs if m.get("type") == "decision"}
        outstanding = outstanding and not any(m.get("id") in answered for m in msgs if m.get("type") == "request" and m.get("from", {}).get("worker") == worker)
        if outstanding: status = "blocked"
        elif (run_dir / "queued").exists() or raw == "queued": status = "waiting"
        elif raw in ("running", "active"): status = "working"
        elif raw in ("failed", "error"): status = "failed"
        elif int(time.time()) - int(meta.get("ended") or start.get("started") or row.get("started") or row.get("time_epoch") or 0) > 1800: status = "sleeping"
        return {"worker": worker, "agent": row.get("agent", ""), "state": status, "now": row.get("title", ""),
                "thread": meta.get("session_id") or meta.get("native_session_id") or start.get("native_session_id"),
                "rounds": sum(1 for x in registry if x.get("worker") == worker), "tasks": tasks_of(worker)}
    departments = []
    answered = {m.get("reply_to") for m in msgs if m.get("type") == "decision"}
    for d in data.get("departments", []):
        ms = [member(w) for w in d.get("members", [])]
        departments.append({"id": d.get("id"), "name": d.get("name"), "mission": d.get("mission"), "head": d.get("head"),
                            "status": d.get("status"), "members": ms,
                            "counts": {"members": len(ms), "working": sum(x["state"] == "working" for x in ms),
                                       "open_requests": sum(x.get("status") == "open" and x.get("id") not in answered and x.get("to") == d.get("id") for x in msgs)}})
    unassigned = [member(w) for w in latest if w not in members]
    contracts = []
    for path in sorted((root(org_session) / "contracts").glob("*.md")):
        first = path.read_text(encoding="utf-8").splitlines()[0] if path.is_file() else ""
        contracts.append({"id": path.stem, "owner": first.split("owner:", 1)[1].split("version:", 1)[0].strip() if "owner:" in first else "", "version": int(first.split("version:", 1)[1].split()[0]) if "version:" in first and first.split("version:", 1)[1].split()[0].isdigit() else 1, "title": path.stem})
    return {"conversation": {"id": lead_session(session), "title": conversation or lead_session(session)},
            "gm": {"kind": "claude", "title": "总经理"}, "departments": departments, "unassigned": unassigned,
            "messages": msgs[-200:], "contracts": contracts,
            # 任务拆解：这个对话里派出去的每件事，交给了谁、做到哪了
            "plan": [{"id": row.get("run"), "title": row.get("title") or row.get("name") or "任务",
                      "worker": row.get("worker") or "", "agent": row.get("agent") or "", "dept": row.get("dept") or "",
                      "status": _run_status(row), "time": row.get("time")} for row in runs][-60:]}


def _registry_rows():
    rows = []
    try:
        rows = [json.loads(line) for line in (home() / "registry.jsonl").read_text(encoding="utf-8").splitlines() if line.strip()]
    except (OSError, ValueError):
        pass
    return [row for row in rows if row.get("kind") == "run" and row.get("run_dir")]


def _supervise_state(session=None):
    return root(session) / "supervise.json"


def _update_supervise_state(update, session=None):
    path = _supervise_state(session)
    path.parent.mkdir(parents=True, exist_ok=True)
    lock_path = path.with_suffix(".state.lock")
    with lock_path.open("a+", encoding="utf-8") as lock:
        fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
        state = _read(path, {"handled": {}, "retry_counts": {}, "decisions": []})
        update(state)
        with path.open("w", encoding="utf-8") as out:
            json.dump(state, out, ensure_ascii=False, indent=2)
            out.write("\n")
        fcntl.flock(lock.fileno(), fcntl.LOCK_UN)
    return state


def _worker_run(worker, session=None):
    rows = [row for row in _registry_rows() if row.get("worker") == worker and
            (not row.get("lead_session") or row.get("lead_session") == lead_session(session))]
    return rows[-1] if rows else None


def _run_alive(start, meta):
    if meta.get("status") in ("done", "failed", "timeout", "stopped", "canceled", "orphaned", "completed", "interrupted", "stale"):
        return False
    try:
        pid = int(start.get("pid") or 0)
        if pid > 0:
            os.kill(pid, 0)
            return True
    except (OSError, ValueError, TypeError):
        pass
    return meta.get("status") in ("running", "active")


def _native_turn(run_dir):
    try:
        events = (Path(run_dir) / "events.norm.jsonl").read_text(encoding="utf-8").splitlines()
    except OSError:
        return None
    for raw in reversed(events):
        try:
            event = json.loads(raw)
        except ValueError:
            continue
        if event.get("type") == "turn.started":
            return (event.get("data") or {}).get("native_turn_id")
    return None


def _deliver_pending(session, active, action_runner=None):
    rows = messages(session)
    delivered = {m.get("message_id") or m.get("reply_to") for m in rows if m.get("type") == "delivered"}
    pending = [m for m in rows if m.get("type") in ("tell", "reply", "decision") and
               m.get("to_worker") and m.get("id") not in delivered]
    count = 0
    for message in pending:
        worker = message["to_worker"]
        row = _worker_run(worker, session)
        if not row or row.get("dept") not in active:
            continue
        run_dir = Path(row["run_dir"])
        start = _read(run_dir / "start.json", {})
        meta = _read(run_dir / "meta.json", {})
        running = _run_alive(start, meta)
        text = "来自 %s 部门 %s 的留言：%s" % (
            message.get("from", {}).get("dept") or "总经理",
            message.get("from", {}).get("worker") or "负责人",
            message.get("body") or "")
        agent = row.get("agent") or meta.get("agent") or start.get("agent")
        capabilities = meta.get("capabilities") or start.get("capabilities") or {}
        driver = meta.get("driver") or start.get("driver") or ""
        can_steer = capabilities.get("steer") == "live" or (not capabilities and driver == "codex-app-server")
        ok = False
        if running and agent == "codex" and can_steer:
            try:
                from xay_ctl import CtlClient
                result = CtlClient(run_dir).steer(text, expected_turn=_native_turn(run_dir),
                                                  by="lead:" + lead_session(session))
                ok = bool(result.get("ok"))
            except (ImportError, OSError, RuntimeError):
                ok = False
        elif not running:
            argv = [sys.executable, str(Path(__file__).with_name("xay_cli.py")), "send", worker, text,
                    "--session", str(lead_session(session))]
            runner = action_runner or _run_action
            ok = bool(runner(argv, _xay_env(session, row.get("dept"), worker)))
        if ok:
            _append({"id": "d_%s" % message["id"], "ts": int(time.time()), "type": "delivered",
                     "message_id": message["id"], "to_worker": worker}, session)
            count += 1
    return count


def _task_key(row, start, meta):
    if meta.get("org_task_key") or start.get("org_task_key") or row.get("org_task_key"):
        return str(meta.get("org_task_key") or start.get("org_task_key") or row.get("org_task_key"))
    task = meta.get("task") or start.get("task") or row.get("task")
    if task:
        return "%s:%s" % (meta.get("dag") or start.get("dag") or row.get("dag") or "", task)
    return "%s:%s:%s" % (row.get("worker") or "", row.get("name") or "", row.get("title") or "")


def _event_time(run_dir, start, meta):
    latest = int(meta.get("last_event_ts") or meta.get("ended") or 0)
    path = Path(run_dir) / "events.norm.jsonl"
    try:
        for raw in path.read_text(encoding="utf-8").splitlines():
            event = json.loads(raw)
            ts = event.get("ts") or (event.get("data") or {}).get("ts")
            if ts:
                latest = max(latest, int(float(ts)))
    except (OSError, ValueError, TypeError):
        pass
    return latest or int(start.get("started") or meta.get("started") or 0)


def _run_action(argv, env=None, wait=False):
    try:
        if wait:
            return subprocess.run(argv, env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=False).returncode == 0
        subprocess.Popen(argv, env=env, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                         stderr=subprocess.DEVNULL, start_new_session=True)
        return True
    except OSError:
        return False


def _xay_env(session, dept, worker, task_key=None):
    env = dict(os.environ)
    env.update({"XAY_LEAD_SESSION": str(session or "default"), "XAY_DEPT": str(dept or ""),
                "XAY_WORKER": str(worker or "")})
    if task_key:
        env["XAY_ORG_TASK_KEY"] = task_key
    return env


def _send_retry(worker, dept, session, text, now=False, task_key=None):
    argv = [sys.executable, str(Path(__file__).with_name("xay_cli.py")), "send", worker, text,
            "--session", str(session or "default")]
    if now:
        argv.append("--now")
    return _run_action(argv, _xay_env(session, dept, worker, task_key))


def _quota_reassign(row, start, meta, session, task_key=None):
    agent = row.get("agent") or meta.get("agent") or start.get("agent") or "codex"
    dept, worker = row.get("dept") or start.get("dept"), row.get("worker") or start.get("worker")
    selected_worker = worker
    if not worker:
        return False, "缺少具名员工，无法重派"
    origin = meta.get("origin") or start.get("origin") or row.get("origin") or ""
    title = row.get("title") or start.get("title") or "额度失败后重派"
    prompt = (start.get("card") or {}).get("prompt") or ""
    if not prompt:
        try:
            prompt = (Path(row["run_dir"]) / "prompt.md").read_text(encoding="utf-8")
        except OSError:
            return False, "任务卡不存在"
    argv = [str(Path(__file__).with_name("xay-run"))]
    if agent == "codex":
        # Plus / 中转由 xay-run 按额度自动选（arch-v6 §1.4）
        argv += ["codex", "--source", "auto", "--as", str(worker)]
    else:
        prefer = start.get("prefer") or meta.get("prefer") or ["agy", "codex", "dsh"]
        others = [name for name in prefer if name != agent] or ["codex"]
        next_agent = None
        try:
            from xay_quota import choose_route, collect
            picked = choose_route(collect(), others)
            next_agent = (picked or {}).get("agent")
        except Exception:
            next_agent = None
        agent = next_agent or others[0]
        selected_worker = str(worker) + "-" + agent
        argv += [agent, "--as", selected_worker]
    argv += ["-n", str(start.get("name") or row.get("name") or worker), "-t", title, "-C", origin]
    if dept:
        argv += ["--dept", str(dept)]
    argv += ["--raw", prompt]
    ok = _run_action(argv, _xay_env(session, dept, selected_worker, task_key))
    return ok, "按额度线路重派同一任务卡"


def _patch_already_applied(run_dir, meta):
    origin = meta.get("origin") or ""
    patch = Path(run_dir) / "land.patch"
    if not origin or not patch.is_file():
        return False
    return subprocess.run(["git", "-C", origin, "apply", "--check", "-R", str(patch)],
                          stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=False).returncode == 0


def _write_user_decision(row, meta, detail, state, session=None, category="repeat-failure"):
    run_dir = Path(row["run_dir"])
    key = _task_key(row, _read(run_dir / "start.json", {}), meta)
    decision_id = "dec_supervise_" + hashlib.sha1((key + ":" + category).encode()).hexdigest()[:12]
    if decision_id in state.setdefault("decisions", []):
        return False
    record = {"id": decision_id, "kind": "failed_final", "owner": "user", "version": 1,
              "ws": row.get("ws") or "", "session": row.get("session") or session or "",
              "phase": row.get("dag") or "", "task": row.get("task") or "",
              "execution": str(run_dir), "title": "同一任务反复失败，需要用户决定",
              "detail": detail, "options": [{"id": "rework", "label": "继续返工", "danger": False},
                                                   {"id": "ignore", "label": "先不管", "danger": False}],
              "payload": {"fail_reason": meta.get("fail_reason") or ""}, "created": int(time.time()), "resolved": None}
    path = run_dir / "decisions.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as out:
        fcntl.flock(out.fileno(), fcntl.LOCK_EX)
        out.write(json.dumps(record, ensure_ascii=False) + "\n")
        out.flush()
        fcntl.flock(out.fileno(), fcntl.LOCK_UN)
    state["decisions"].append(decision_id)
    return True


def _supervise_run(row, dept, session, state, now=None, action_runner=None):
    run_dir = row["run_dir"]
    start = _read(Path(run_dir) / "start.json", {})
    meta = _read(Path(run_dir) / "meta.json", {})
    worker = row.get("worker") or start.get("worker")
    if not worker:
        return 0
    actual_dept = row.get("dept") or start.get("dept") or dept
    if dept and actual_dept != dept:
        return 0
    task_key = _task_key(row, start, meta)
    run_id = str(row.get("run") or Path(run_dir).name)
    handled = state.setdefault("handled", {})
    runner = action_runner or _run_action
    actions = 0

    def run(argv, env=None, wait=False):
        return runner(argv, env) if action_runner else runner(argv, env, wait)

    def automatic(event, subject, body):
        send_message(actual_dept or "gm", subject, body, kind="auto", session=session)
        handled[event] = {"action": subject, "ts": int(time.time())}

    def escalate(event, subject, body):
        send_message(actual_dept or "gm", subject, body, kind="escalation", session=session)
        handled[event] = {"action": "escalation", "ts": int(time.time())}

    if meta.get("direction_changed") or meta.get("requirement_changed"):
        event = run_id + ":direction"
        if event not in handled:
            created = _write_user_decision(row, meta, "总经理确认方向或需求已变更，需要用户拍板。", state, session, "direction-change")
            automatic(event, "方向变更上报用户", "总经理确认方向或需求已变更，已写入 owner=user 决定。" if created else "方向变更决定已存在。")
            actions += int(created)

    status = meta.get("status") or start.get("status") or row.get("status") or ""
    fail_reason = str(meta.get("fail_reason") or "")
    failure = status in ("failed", "timeout", "orphaned")
    quota = any(word in fail_reason.lower() for word in ("usage_limit", "quota", "rate_limit", "额度", "daily_limit"))
    if failure:
        event = run_id + ":failure"
        if event not in handled:
            failures = []
            for candidate in _registry_rows():
                cstart = _read(Path(candidate["run_dir"]) / "start.json", {})
                cmeta = _read(Path(candidate["run_dir"]) / "meta.json", {})
                if _task_key(candidate, cstart, cmeta) == task_key and (cmeta.get("status") or cstart.get("status") or candidate.get("status")) in ("failed", "timeout", "orphaned"):
                    failures.append(candidate)
            if len(failures) >= 3:
                created = _write_user_decision(row, meta, "同一任务已失败 %d 次，需要用户决定是否继续。" % len(failures), state, session, "repeat-failure")
                if created:
                    automatic(event, "反复失败需要用户决定", "同一任务累计失败 %d 次，已写入 owner=user 决定。" % len(failures))
                else:
                    handled[event] = {"action": "decision_exists", "ts": int(time.time())}
                actions += int(created)
            else:
                retry_count = int(state.setdefault("retry_counts", {}).get(task_key, 0))
                if quota and retry_count < (1 if (row.get("agent") or start.get("agent")) == "codex" else max(1, len(start.get("prefer") or meta.get("prefer") or ["agy", "codex", "dsh"]) - 1)):
                    ok, description = _quota_reassign(row, start, meta, session, task_key)
                elif quota:
                    ok = False
                    description = "额度线路已经重派过，转部门负责人"
                elif retry_count < 1:
                    text = "上一轮失败：%s。排查后重试" % (fail_reason or status)
                    ok = _send_retry(worker, actual_dept, session, text, task_key=task_key)
                    description = "已通知员工排查并重试"
                else:
                    ok = False
                    description = "自动重试一次后仍失败"
                if ok:
                    state["retry_counts"][task_key] = retry_count + 1
                    automatic(event, "额度失败自动换线" if quota else "失败自动处理", "%s；run=%s" % (description, run_id))
                else:
                    escalate(event, "任务失败转部门负责人", "%s；run=%s" % (description, run_id))
                actions += 1

    if meta.get("land") == "conflict":
        event = run_id + ":land-conflict"
        if event not in handled:
            if _patch_already_applied(run_dir, meta):
                argv = [str(Path(__file__).with_name("xay-land")), run_dir, "--discard"]
                description = "改动已在项目中，丢弃冲突工作区"
            else:
                argv = [str(Path(__file__).with_name("xay-land")), run_dir]
                description = "重试一次搬回冲突补丁"
            if run(argv, _xay_env(session, actual_dept, worker), True):
                automatic(event, "冲突自动处理", "%s；run=%s" % (description, run_id))
            else:
                escalate(event, "冲突转部门负责人", "自动处理失败：%s；run=%s" % (description, run_id))
            actions += 1

    if status in ("running", "active"):
        last = _event_time(run_dir, start, meta)
        silence = {"codex": 180, "dsh": 120, "agy": 240}.get(row.get("agent") or start.get("agent"), 120)
        if last and now is not None and now - last >= silence:
            event = run_id + ":silence"
            if event not in handled:
                capabilities = meta.get("capabilities") or start.get("capabilities") or {}
                driver = meta.get("driver") or start.get("driver") or ""
                can_steer = capabilities.get("steer") == "live" or (not capabilities and driver == "codex-app-server")
                if can_steer:
                    ok = _send_retry(worker, actual_dept, session, "汇报一下进度", now=True, task_key=task_key)
                    if ok:
                        automatic(event, "沉默员工已提醒", "已请员工汇报进度；run=%s" % run_id)
                        actions += 1
                    else:
                        escalate(event, "沉默员工转部门负责人", "实时提醒未能送达；run=%s" % run_id)
                        actions += 1
                else:
                    escalate(event, "沉默员工转部门负责人", "超过 %d 秒没有新动作；run=%s" % (silence, run_id))
                    actions += 1
    return actions


def supervise(dept=None, once=False, session=None, action_runner=None, now=None):
    session = lead_session(session)
    total = 0
    while True:
        lock_path = root(session) / "supervise.lock"
        lock_path.parent.mkdir(parents=True, exist_ok=True)
        with lock_path.open("a+", encoding="utf-8") as lock:
            fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
            data = org(session)
            active = {d["id"] for d in data.get("departments", []) if d.get("status") == "active"}
            if dept:
                active &= {dept}
            state = _read(_supervise_state(session), {"handled": {}, "retry_counts": {}, "decisions": []})
            seen_tasks = set()
            rows = _registry_rows()
            rows.sort(key=lambda item: str(item.get("run") or item.get("time") or ""), reverse=True)
            for row in rows:
                if row.get("lead_session") and row.get("lead_session") != session:
                    continue
                run_dept = row.get("dept")
                if run_dept in active:
                    start = _read(Path(row["run_dir"]) / "start.json", {})
                    meta = _read(Path(row["run_dir"]) / "meta.json", {})
                    task_key = _task_key(row, start, meta)
                    if task_key in seen_tasks:
                        continue
                    seen_tasks.add(task_key)
                    total += _supervise_run(row, run_dept, session, state, now or int(time.time()), action_runner)
            total += _deliver_pending(session, active, action_runner)
            _update_supervise_state(lambda current: current.update(state), session)
            fcntl.flock(lock.fileno(), fcntl.LOCK_UN)
        if once:
            return total
        time.sleep(15)


def main(argv=None):
    parser = argparse.ArgumentParser(prog="xay org")
    sub = parser.add_subparsers(dest="command", required=True)
    dept = sub.add_parser("dept"); dept.add_argument("action", choices=("create", "list", "close")); dept.add_argument("id", nargs="?"); dept.add_argument("--name"); dept.add_argument("--mission", default=""); dept.add_argument("--head", choices=("rules", "claude"), default="rules"); dept.add_argument("--owns", default=""); dept.add_argument("--json", action="store_true")
    run = sub.add_parser("run"); run.add_argument("action", choices=("ask", "answer", "escalate", "report", "inbox"))
    askp = sub.add_parser("ask"); askp.add_argument("to"); askp.add_argument("body"); askp.add_argument("--contract"); askp.add_argument("--wait", type=int, default=0)
    ans = sub.add_parser("answer"); ans.add_argument("id"); ans.add_argument("verdict", choices=("accept", "modify", "reject")); ans.add_argument("body")
    esc = sub.add_parser("escalate"); esc.add_argument("id"); esc.add_argument("body")
    rep = sub.add_parser("report"); rep.add_argument("body")
    inp = sub.add_parser("inbox"); inp.add_argument("--dept"); inp.add_argument("--gm", action="store_true"); inp.add_argument("--wait", type=int); inp.add_argument("--json", action="store_true")
    contract = sub.add_parser("contract"); contract.add_argument("action", choices=("set", "show")); contract.add_argument("id"); contract.add_argument("--owner"); contract.add_argument("--file")
    args = parser.parse_args(argv); session = lead_session()
    try:
        if args.command == "dept":
            if args.action == "create": out = create_dept(args.id, args.name or args.id, args.mission, args.head, [x for x in args.owns.split(",") if x])
            elif args.action == "close": out = close_dept(args.id)
            else: out = org()
        elif args.command == "ask":
            out = ask(args.to, args.body, args.contract); out = wait_for(out["id"], args.wait) if args.wait else out
        elif args.command == "answer": out = answer(args.id, args.verdict, args.body)
        elif args.command == "escalate": out = escalate(args.id, args.body)
        elif args.command == "report": out = send_message("gm", "部门报告", args.body, kind="report")
        elif args.command == "inbox": out = wait_inbox(args.dept, args.gm, args.wait or 0, session) if args.wait is not None else inbox(args.dept, args.gm, session); out = out if args.json else "\n".join("%s %s" % (x["id"], x["subject"]) for x in out)
        elif args.command == "contract": out = contract_set(args.id, args.owner, args.file) if args.action == "set" else contract_show(args.id)
        print(json.dumps(out, ensure_ascii=False, indent=2) if not isinstance(out, str) else out)
        return 0
    except (ValueError, OSError, PermissionError) as exc:
        print("xay：%s" % exc, file=sys.stderr); return 2


if __name__ == "__main__":
    raise SystemExit(main(argv=None))
