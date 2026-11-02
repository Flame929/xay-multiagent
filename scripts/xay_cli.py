#!/usr/bin/env python3
"""xay 负责人命令行入口。"""

import argparse
import hashlib
import json
import os
import subprocess
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
from xay_ctl import CtlClient, CtlError
from xay_quota import collect as collect_quota, choose_route, agy_accounts, live_readings, switch_agy_account
import xay_org


def xay_home():
    return Path(os.environ.get("XAY_HOME", str(Path.home() / ".xay"))).expanduser()


def _org_session():
    detected = detect_lead()
    return os.environ.get("XAY_LEAD_SESSION") or (detected.get("lead_session") if detected.get("lead_session") else None)


def executor_guard():
    if os.environ.get("XAY_ROLE") == "executor":
        print("xay：执行者不能运行负责人控制命令", file=sys.stderr)
        return True
    return False


def current_session(explicit=None):
    if explicit or os.environ.get("XAY_LEAD_SESSION"):
        return explicit or os.environ["XAY_LEAD_SESSION"]
    native_id = os.environ.get("CLAUDE_CODE_SESSION_ID")
    if native_id:
        for path in sorted((xay_home() / "sessions").glob("ls_*.json"), reverse=True):
            data = json.loads(path.read_text(encoding="utf-8"))
            if data.get("lead_key") == "claude:" + native_id and not data.get("ended"):
                return data["id"]
    return None


# ui-v5-api §1：负责人对话识别顺序。显式 --session（或 XAY_LEAD_SESSION）优先。
LEAD_ENV = (("XAY_SESSION", ""), ("CLAUDE_CODE_SESSION_ID", "claude"),
            ("CODEX_THREAD_ID", "codex"), ("CODEX_SESSION_ID", "codex"))


def _registered_kind(session_id, env):
    """显式会话若是 xay session begin 登记的 ls_，沿用登记的负责人种类。"""
    home = Path(env.get("XAY_HOME", str(Path.home() / ".xay"))).expanduser()
    try:
        data = json.loads((home / "sessions" / (str(session_id) + ".json")).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return ""
    return str(data.get("lead_kind") or "") if isinstance(data, dict) else ""


def detect_lead(explicit=None, env=None):
    """返回 {lead_session, lead_kind, lead_host}；识别不到时 lead_session 为空串。"""
    env = os.environ if env is None else env
    host = env.get("CLAUDE_CODE_HOST_SESSION_ID") or ""
    claude_id = env.get("CLAUDE_CODE_SESSION_ID") or ""

    def guess(value):
        return env.get("XAY_LEAD_KIND") or ("claude" if claude_id and value == claude_id else "unknown")

    explicit = explicit or env.get("XAY_LEAD_SESSION") or ""
    if explicit:
        return {"lead_session": explicit, "lead_kind": _registered_kind(explicit, env) or guess(explicit), "lead_host": host}
    for name, kind in LEAD_ENV:
        value = env.get(name) or ""
        if value:
            return {"lead_session": value, "lead_kind": kind or guess(value), "lead_host": host}
    return {"lead_session": "", "lead_kind": env.get("XAY_LEAD_KIND") or "unknown", "lead_host": host}


def latest_native_turn(run):
    run_id = Path(run).name
    registry = xay_home() / "registry.jsonl"
    if not registry.is_file():
        return None
    for line in reversed(registry.read_text(encoding="utf-8").splitlines()):
        try:
            row = json.loads(line)
        except json.JSONDecodeError:
            continue
        if row.get("run") != run_id or not row.get("run_dir"):
            continue
        events = Path(row["run_dir"]) / "events.norm.jsonl"
        if not events.is_file():
            return None
        for raw in reversed(events.read_text(encoding="utf-8").splitlines()):
            try:
                event = json.loads(raw)
            except json.JSONDecodeError:
                continue
            data = event.get("data") or {}
            if event.get("type") == "turn.started" and data.get("native_turn_id"):
                return data["native_turn_id"]
        return None
    return None


def session_command(args):
    home = xay_home()
    home.mkdir(parents=True, exist_ok=True)
    root = Path.cwd().resolve()
    ws = "ws_" + hashlib.sha1(str(root).encode("utf-8")).hexdigest()[:8]
    args.session_id = current_session(args.session_id)
    if args.session_id:
        path = home / "sessions" / (args.session_id + ".json")
        session = json.loads(path.read_text(encoding="utf-8"))
        if args.action == "end":
            session["ended"] = int(time.time())
            path.write_text(json.dumps(session, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        print(args.session_id)
        return 0
    if args.action == "end":
        print("xay session end：需要 --session ls_id", file=sys.stderr)
        return 2
    lead_kind = args.lead or os.environ.get("XAY_LEAD_KIND")
    lead_key = None
    lead_host_id = None
    if lead_kind == "claude" or (not lead_kind and os.environ.get("CLAUDE_CODE_SESSION_ID")):
        lead_kind = "claude"
        native_id = os.environ.get("CLAUDE_CODE_SESSION_ID")
        lead_key = "claude:" + native_id
        lead_host_id = os.environ.get("CLAUDE_CODE_HOST_SESSION_ID") or None
    else:
        lead_kind = lead_kind or "unknown"
        # 除 Claude 外必须明确声明负责人种类，避免猜测会话身份。
        if not args.lead and not os.environ.get("XAY_LEAD_KIND"):
            print("请用 --lead codex|dsh|agy|unknown 显式声明负责人", file=sys.stderr)
            return 2
    stamp = time.strftime("%Y%m%d-%H%M%S")
    session_id = "ls_" + stamp + "_" + os.urandom(2).hex()
    lead_key = lead_key or "explicit:" + session_id
    title = args.title or "未命名会话 · " + root.name
    sessions_dir = home / "sessions"
    sessions_dir.mkdir(parents=True, exist_ok=True)
    data = {"id": session_id, "ws": ws, "lead_kind": lead_kind, "lead_key": lead_key,
            "lead_host_id": lead_host_id, "title": title, "started": int(time.time()), "ended": None, "seq": 0}
    (sessions_dir / (session_id + ".json")).write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(session_id)
    return 0


def wait_command(args):
    deadline = time.monotonic() + args.timeout
    registry = xay_home() / "registry.jsonl"
    while time.monotonic() < deadline:
        if registry.is_file():
            rows = []
            for line in registry.read_text(encoding="utf-8").splitlines():
                try:
                    row = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if row.get("session") == args.session and row.get("run_dir"):
                    rows.append(row)
            output = []
            for row in rows:
                run_dir = Path(row["run_dir"])
                if args.phase and row.get("phase") != args.phase:
                    continue
                for filename, label, active_key in (("decisions.jsonl", "需要你", "resolved"),
                                                    ("signals.jsonl", "提醒", "acked")):
                    path = run_dir / filename
                    if not path.is_file():
                        continue
                    for raw in path.read_text(encoding="utf-8").splitlines():
                        try:
                            item = json.loads(raw)
                        except json.JSONDecodeError:
                            continue
                        if label == "需要你" and item.get("owner", "lead") != "lead":
                            continue
                        if item.get(active_key):
                            continue
                        output.append(f"{label}：{item.get('title') or item.get('text') or item.get('kind')}")
                meta_path = run_dir / "meta.json"
                if meta_path.is_file():
                    meta = json.loads(meta_path.read_text(encoding="utf-8"))
                    if meta.get("status") in ("needs_you", "failed", "orphaned", "done", "stopped", "timeout"):
                        output.append(f"阶段结果：{meta.get('status')}（{row.get('title') or row.get('run')}）")
            if output:
                print("\n".join(output[:40]))
                return 0
        time.sleep(min(1, max(0, deadline - time.monotonic())))
    print("无新信号，继续等")
    return 0


# ---- 具名 AI（worker）：像负责人的子代理一样，按名字找到同一个会话接着干 ----

def _pid_alive(pid):
    try:
        pid = int(pid or 0)
        if pid <= 0:
            return False
        os.kill(pid, 0)
        return True
    except (OSError, ValueError, TypeError):
        return False


def _read_json(path):
    try:
        return json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def _native_from_events(run_dir):
    path = Path(run_dir) / "events.norm.jsonl"
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return ""
    for raw in reversed(lines):
        try:
            event = json.loads(raw)
        except ValueError:
            continue
        if event.get("type") == "session.started":
            return str((event.get("data") or {}).get("native_session_id") or "")
    return ""


def _worker_rows(lead, origin=None):
    """当前负责人对话下所有带 worker 名的运行（按登记顺序）。没有负责人时按原项目目录区分。"""
    registry = xay_home() / "registry.jsonl"
    rows = []
    try:
        lines = registry.read_text(encoding="utf-8").splitlines()
    except OSError:
        return rows
    for line in lines:
        try:
            row = json.loads(line)
        except ValueError:
            continue
        if row.get("kind") != "run" or not row.get("worker"):
            continue
        if lead:
            if row.get("lead_session") != lead:
                continue
        elif origin and os.path.realpath(row.get("origin") or "") != os.path.realpath(origin):
            continue
        rows.append(row)
    return rows


def worker_info(name, lead, origin=None):
    rows = [r for r in _worker_rows(lead, origin) if r.get("worker") == name]
    if not rows:
        return {"found": False, "name": name}
    last = rows[-1]
    run_dir = last.get("run_dir") or ""
    start = _read_json(Path(run_dir) / "start.json")
    meta = _read_json(Path(run_dir) / "meta.json")
    finished = meta.get("status") in ("done", "failed", "timeout", "stopped", "canceled", "orphaned", "completed", "interrupted", "stale")
    running = not finished and _pid_alive(start.get("pid"))
    native = str(meta.get("session_id") or meta.get("native_session_id") or "") or _native_from_events(run_dir)
    if not native:
        # 这一轮没留下会话（例如刚开始就失败），往前找最近一轮有会话的
        for row in reversed(rows[:-1]):
            old = _read_json(Path(row.get("run_dir") or "") / "meta.json")
            native = str(old.get("session_id") or "") or _native_from_events(row.get("run_dir") or "")
            if native:
                break
    return {
        "found": True, "name": name, "agent": last.get("agent"), "run_dir": run_dir,
        "status": "running" if running else (meta.get("status") or ("orphaned" if not finished else "")),
        "running": running, "native": native, "origin": last.get("origin") or start.get("origin") or "",
        "readonly": bool(start.get("readonly")), "rounds": len(rows), "title": last.get("title") or "",
        "updated": meta.get("ended") or start.get("started"),
    }


def _lead_for(args):
    return detect_lead(getattr(args, "session", None)).get("lead_session") or ""


def worker_command(args):
    lead = _lead_for(args)
    info = worker_info(args.name, lead, args.cd or os.getcwd())
    if args.wait and info.get("running"):
        deadline = time.time() + args.wait
        while info.get("running") and time.time() < deadline:
            time.sleep(3)
            info = worker_info(args.name, lead, args.cd or os.getcwd())
    print(json.dumps(info, ensure_ascii=False))
    return 0


def workers_command(args):
    lead = _lead_for(args)
    names = []
    for row in _worker_rows(lead, os.getcwd()):
        if row["worker"] not in names:
            names.append(row["worker"])
    items = [worker_info(n, lead, os.getcwd()) for n in names]
    if args.json:
        print(json.dumps(items, ensure_ascii=False, indent=2))
        return 0
    if not items:
        print("这个负责人对话里还没有具名的 AI。用 xay-run <agent> --as <名字> \"任务\" 派第一件活。")
        return 0
    for item in items:
        state = {"running": "正在做", "done": "空闲", "failed": "上一轮失败", "timeout": "上一轮超时", "stopped": "已停止"}.get(item["status"], item["status"] or "未知")
        print("%-20s %-6s %-10s %2d 轮  最近：%s" % (item["name"], item["agent"], state, item["rounds"], item["title"]))
    return 0


def send_command(args):
    """把一句话发给已有的具名 AI：默认等它做完上一件事再在同一会话里接着做；--now 且支持实时插话时直接插话。"""
    lead = _lead_for(args)
    info = worker_info(args.name, lead, args.cd or os.getcwd())
    if not info.get("found"):
        print("xay send：这个负责人对话里没有叫「%s」的 AI。先用 xay-run <agent> --as %s \"任务\" 派第一件活。" % (args.name, args.name), file=sys.stderr)
        return 2
    text = " ".join(args.text).strip()
    if not text:
        print("xay send：缺少要说的话", file=sys.stderr)
        return 2
    if args.now and info.get("running"):
        try:
            result = CtlClient(info["run_dir"]).request("steer", text=text, expected_turn=latest_native_turn(info["run_dir"]),
                                                        by=("lead:" + lead) if lead else "lead")
        except CtlError as exc:
            result = {"ok": False, "error": str(exc)}
        if result.get("ok"):
            print(json.dumps(result, ensure_ascii=False))
            return 0
        print("xay send：实时插话没被接受（%s），改为排队，等它做完再接着做。" % (result.get("error") or "不支持"), file=sys.stderr)
    argv = [str(HERE / "xay-run"), info["agent"], "--as", args.name, "-C", info["origin"] or os.getcwd()]
    if info.get("readonly"):
        argv.append("--readonly")
    argv.append(text)
    os.execv(argv[0], argv)


# ---- 实时进度：谁在干活、干了多久、正在做哪一步（给负责人和桌面版"后台任务"面板看） ----

_ACTION = {"cmd": "运行", "read": "查看", "edit": "修改", "write": "写", "search": "搜索", "web": "查网页"}


def _tail_lines(path, limit=200, max_bytes=65536):
    try:
        with open(path, "rb") as f:
            f.seek(0, 2)
            size = f.tell()
            f.seek(max(0, size - max_bytes))
            data = f.read().decode("utf-8", "replace")
    except OSError:
        return []
    return data.splitlines()[-limit:]


def _short(text, n=46):
    text = " ".join(str(text or "").split())
    return text if len(text) <= n else text[: n - 1] + "…"


def _run_progress(run_dir):
    steps, now, tokens = 0, "准备中", 0
    events = Path(run_dir) / "events.norm.jsonl"
    try:
        lines = events.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        lines = []
    for raw in lines:
        try:
            event = json.loads(raw)
        except ValueError:
            continue
        kind = event.get("type")
        data = event.get("data") or {}
        if kind == "tool.call":
            steps += 1
            verb = _ACTION.get(event.get("kind") or data.get("kind"), "调用")
            now = verb + " " + _short(event.get("path") or event.get("text") or data.get("text") or data.get("command") or "", 40)
        elif kind == "message":
            now = "写回复：" + _short(event.get("text") or data.get("text"), 30)
        elif kind == "usage":
            if data.get("in") is not None or data.get("out") is not None:
                fresh = max(0, (data.get("in") or 0) - (data.get("cached") or 0)) + (data.get("out") or 0)
            else:
                fresh = data.get("total") or 0
            tokens = tokens + int(fresh or 0) if data.get("granularity") == "step" else int(fresh or 0)
        elif kind == "turn.started" and steps == 0:
            now = "思考中"
    return steps, now, tokens


def _fmt_secs(sec):
    sec = int(max(0, sec))
    return "%d:%02d" % (sec // 60, sec % 60) if sec < 3600 else "%d时%02d分" % (sec // 3600, sec % 3600 // 60)


def _fmt_tokens(n):
    return "%.1f万" % (n / 10000.0) if n >= 10000 else str(n)


def _status_rows(lead, everyone, since):
    rows = []
    registry = xay_home() / "registry.jsonl"
    for line in _tail_lines(registry, limit=600, max_bytes=1 << 20):
        try:
            row = json.loads(line)
        except ValueError:
            continue
        if row.get("kind") != "run" or not row.get("run_dir"):
            continue
        if not everyone and lead and row.get("lead_session") != lead:
            continue
        start = _read_json(Path(row["run_dir"]) / "start.json")
        meta = _read_json(Path(row["run_dir"]) / "meta.json")
        terminal = meta.get("status") in ("done", "failed", "timeout", "stopped", "canceled", "orphaned", "stale")
        running = not terminal and _pid_alive(start.get("pid"))
        started = float(start.get("started") or 0)
        ended = started + float(meta.get("duration_s") or 0) if terminal else None
        if not running and not (terminal and ended and ended >= since):
            continue
        rows.append({"row": row, "start": start, "meta": meta, "running": running, "started": started,
                     "queued": (Path(row["run_dir"]) / "queued").exists()})
    return rows


def status_command(args):
    lead = _lead_for(args)
    reported = set()
    since = time.time() - (args.follow or 600)
    while True:
        now = time.time()
        rows = _status_rows(lead, args.all, since)
        if args.json:
            print(json.dumps([{"run_dir": r["row"]["run_dir"], "running": r["running"]} for r in rows], ensure_ascii=False))
            return 0
        running = [r for r in rows if r["running"]]
        print("[%s] 在干活 %d 个" % (time.strftime("%H:%M:%S"), len(running)), flush=True)
        for r in running:
            row = r["row"]
            who = row.get("worker") or row.get("name") or "?"
            steps, act, tokens = _run_progress(row["run_dir"])
            state = "排队等名额" if r["queued"] else act
            print("  %-6s %-10s %-16s %6s · %3d 步 · %6s token · %s" % (
                row.get("agent", ""), _short(who, 10), _short(row.get("title") or "", 16), _fmt_secs(now - r["started"]),
                steps, _fmt_tokens(tokens), state), flush=True)
        for r in rows:
            rd = r["row"]["run_dir"]
            if r["running"] or rd in reported:
                continue
            reported.add(rd)
            meta = r["meta"]
            st = {"done": "完成", "failed": "失败", "timeout": "超时", "stopped": "已停止"}.get(meta.get("status"), meta.get("status"))
            reason = (" · " + _short(meta.get("fail_reason"), 30)) if meta.get("fail_reason") else ""
            files = len(meta.get("changed_files") or [])
            print("  ✓ %s %s「%s」%s · 用时 %s · 改了 %d 个文件%s" % (
                r["row"].get("agent", ""), r["row"].get("worker") or r["row"].get("name") or "",
                _short(r["row"].get("title") or "", 20), st, _fmt_secs(meta.get("duration_s") or 0), files, reason), flush=True)
        if not args.follow:
            return 0
        if args.until_idle and not running:
            print("全部做完了。", flush=True)
            return 0
        time.sleep(args.follow)


def main(argv=None):
    parser = argparse.ArgumentParser(prog="xay")
    sub = parser.add_subparsers(dest="command", required=True)
    session = sub.add_parser("session"); session.add_argument("action", choices=("begin", "end")); session.add_argument("--title"); session.add_argument("--lead", choices=("claude", "codex", "dsh", "agy", "unknown")); session.add_argument("--session", dest="session_id")
    ctl = sub.add_parser("ctl"); ctl.add_argument("run"); ctl.add_argument("action", choices=("steer", "interrupt", "resume", "stop")); ctl.add_argument("--text"); ctl.add_argument("--expected-turn"); ctl.add_argument("--session")
    wait = sub.add_parser("wait"); wait.add_argument("--session", required=True); wait.add_argument("--phase"); wait.add_argument("--timeout", type=int, default=240)
    decide = sub.add_parser("decide"); decide.add_argument("run"); decide.add_argument("option"); decide.add_argument("--id", required=True); decide.add_argument("--version", type=int, required=True); decide.add_argument("--text"); decide.add_argument("--session")
    doctor = sub.add_parser("doctor")
    lead = sub.add_parser("lead"); lead.add_argument("--session")
    worker = sub.add_parser("worker"); worker.add_argument("action", choices=("resolve",)); worker.add_argument("--name", required=True); worker.add_argument("--session"); worker.add_argument("-C", "--cd"); worker.add_argument("--wait", type=int, default=0)
    workers = sub.add_parser("workers"); workers.add_argument("--session"); workers.add_argument("--json", action="store_true")
    send = sub.add_parser("send"); send.add_argument("name"); send.add_argument("text", nargs="+"); send.add_argument("--now", action="store_true"); send.add_argument("--session"); send.add_argument("-C", "--cd")
    status = sub.add_parser("status"); status.add_argument("--session"); status.add_argument("--all", action="store_true"); status.add_argument("--follow", type=int, default=0); status.add_argument("--until-idle", action="store_true"); status.add_argument("--json", action="store_true")
    quota = sub.add_parser("quota"); quota.add_argument("--json", action="store_true")
    route = sub.add_parser("route"); route.add_argument("--prefer", default=""); route.add_argument("--json", action="store_true")
    agy_list = sub.add_parser("agy-accounts"); agy_list.add_argument("--json", action="store_true")
    agy_switch = sub.add_parser("agy-switch"); agy_switch.add_argument("target", nargs="?", default="auto")
    dept = sub.add_parser("dept"); dept.add_argument("action", choices=("create", "list", "close")); dept.add_argument("id", nargs="?"); dept.add_argument("--name"); dept.add_argument("--mission", default=""); dept.add_argument("--head", choices=("rules", "claude"), default="rules"); dept.add_argument("--owns", default=""); dept.add_argument("--json", action="store_true")
    ask = sub.add_parser("ask"); ask.add_argument("to"); ask.add_argument("body"); ask.add_argument("--contract"); ask.add_argument("--wait", type=int)
    tell = sub.add_parser("tell"); tell.add_argument("worker"); tell.add_argument("body")
    reply = sub.add_parser("reply"); reply.add_argument("id"); reply.add_argument("body")
    answer = sub.add_parser("answer"); answer.add_argument("id"); answer.add_argument("verdict", choices=("accept", "modify", "reject")); answer.add_argument("body")
    escalate = sub.add_parser("escalate"); escalate.add_argument("id"); escalate.add_argument("body")
    report = sub.add_parser("report"); report.add_argument("body")
    inbox = sub.add_parser("inbox"); inbox.add_argument("--dept"); inbox.add_argument("--gm", action="store_true"); inbox.add_argument("--wait", type=int); inbox.add_argument("--json", action="store_true")
    contract = sub.add_parser("contract"); contract.add_argument("action", choices=("set", "show")); contract.add_argument("id"); contract.add_argument("--owner"); contract.add_argument("--file")
    supervise = sub.add_parser("supervise"); supervise.add_argument("--dept"); supervise.add_argument("--once", action="store_true")
    args = parser.parse_args(argv)
    if os.environ.get("XAY_ROLE") == "executor" and (args.command not in ("ask", "tell", "reply", "inbox", "contract") or args.command == "contract" and args.action != "show"):
        executor_guard()
        return 2
    if args.command == "lead":
        print(json.dumps(detect_lead(args.session), ensure_ascii=False)); return 0
    if args.command == "quota":
        data = collect_quota(wait=True)
        if args.json:
            print(json.dumps(data, ensure_ascii=False, indent=2))
        else:
            words = {"ok": "充足", "tight": "偏紧", "exhausted": "用完", "unknown": "上限未知"}
            for item in data:
                remaining = "" if item["remaining_pct"] is None else f" {item['remaining_pct']}%"
                source = item.get("source_label") or "本机估算"
                print(f"{item['label']}：{words.get(item['status'], item['status'])}{remaining}（{source}）；{item['note']}；xay 近 5 小时用了 {item['used_5h']:,} tokens")
        return 0
    if args.command == "agy-accounts":
        live_readings(wait=True)  # 先把当前账号的额度查新
        data = agy_accounts()
        if args.json:
            print(json.dumps(data, ensure_ascii=False, indent=2))
        elif not data:
            print("没找到 Antigravity Tools 的账号清单")
        else:
            words = {"ok": "充足", "tight": "偏紧", "exhausted": "用完", "unknown": "未知", "blocked": "停用"}
            for a in data:
                pct = "" if a["remaining_pct"] is None else f" {a['remaining_pct']}%"
                when = time.strftime("%m-%d %H:%M", time.localtime(a["checked_at"])) if a.get("checked_at") else "没有记录"
                print(f"{'👉' if a['current'] else '  '} {a['index']}. {a['name']}（优先级 {a['priority']}）：{words.get(a['status'], a['status'])}{pct}；数据来自{a.get('source') or '—'}，{when}")
        return 0
    if args.command == "agy-switch":
        ok, message = switch_agy_account(args.target, reason="负责人手动")
        print(message)
        return 0 if ok else 1
    if args.command == "route":
        data = collect_quota()
        prefer = [x.strip() for x in args.prefer.split(",") if x.strip()]
        item = choose_route(data, prefer=prefer or None)
        result = item or {"line": None, "status": "exhausted", "note": "没有可用线路"}
        print(json.dumps(result, ensure_ascii=False, indent=2 if args.json else None) if args.json else (result.get("line") or "无可用线路"))
        return 0 if item else 1
    if args.command == "session":
        if args.action == "end" and executor_guard(): return 2
        return session_command(args)
    if args.command == "worker": return worker_command(args)
    if args.command == "status": return status_command(args)
    # 执行者（员工）只能用协作类命令：ask、tell、reply、inbox、contract show
    if args.command not in ("ask", "tell", "reply", "inbox", "contract") and executor_guard(): return 2
    if args.command == "workers": return workers_command(args)
    if args.command == "send": return send_command(args)
    if args.command == "contract" and args.action != "show" and os.environ.get("XAY_ROLE") == "executor":
        return 2
    try:
        if args.command == "dept":
            if args.action == "create": out = xay_org.create_dept(args.id, args.name or args.id, args.mission, args.head, [x for x in args.owns.split(",") if x], _org_session())
            elif args.action == "close": out = xay_org.close_dept(args.id, _org_session())
            else: out = xay_org.org(_org_session())
        elif args.command == "ask":
            out = xay_org.ask(args.to, args.body, args.contract, _org_session()); out = xay_org.wait_for(out["id"], args.wait, _org_session()) if args.wait is not None else out
        elif args.command == "tell": out = xay_org.tell(args.worker, args.body, _org_session())
        elif args.command == "reply": out = xay_org.reply(args.id, args.body, _org_session())
        elif args.command == "answer": out = xay_org.answer(args.id, args.verdict, args.body, _org_session())
        elif args.command == "escalate": out = xay_org.escalate(args.id, args.body, _org_session())
        elif args.command == "report": out = xay_org.send_message("gm", "部门报告", args.body, kind="report", session=_org_session())
        elif args.command == "inbox":
            if os.environ.get("XAY_ROLE") == "executor":
                out = xay_org.wait_inbox(os.environ.get("XAY_DEPT"), False, args.wait or 0, _org_session()) if args.wait is not None else xay_org.inbox(os.environ.get("XAY_DEPT"), False, _org_session())
            else:
                out = xay_org.wait_inbox(args.dept, args.gm, args.wait or 0, _org_session()) if args.wait is not None else xay_org.inbox(args.dept, args.gm, _org_session())
            kinds = {"request": "需求单", "decision": "结论", "escalation": "上报", "notice": "通知", "report": "汇报", "tell": "留言", "reply": "回复", "auto": "自动处理"}
            if not args.json: out = "\n".join("%s [%s] %s" % (x["id"], kinds.get(x.get("type"), x.get("type")), x["subject"]) for x in out)
        elif args.command == "contract": out = xay_org.contract_set(args.id, args.owner, args.file, _org_session()) if args.action == "set" else xay_org.contract_show(args.id, _org_session())
        elif args.command == "supervise": out = xay_org.supervise(args.dept, args.once, _org_session())
        else: out = None
        if out is not None:
            print(json.dumps(out, ensure_ascii=False, indent=2) if not isinstance(out, str) else out); return 0
    except (ValueError, OSError, PermissionError) as exc:
        print("xay：%s" % exc, file=sys.stderr); return 2
    session_id = current_session(getattr(args, "session", None))
    if args.command == "ctl":
        expected_turn = args.expected_turn or (latest_native_turn(args.run) if args.action == "steer" else None)
        try:
            result = CtlClient(args.run).request(args.action, text=args.text,
                expected_turn=expected_turn, by=("lead:" + session_id) if session_id else "lead")
        except CtlError as exc:
            print(json.dumps({"ok": False, "error": str(exc)}, ensure_ascii=False)); return 1
        print(json.dumps(result, ensure_ascii=False)); return 0 if result.get("ok") else 1
    if args.command == "wait": return wait_command(args)
    if args.command == "decide":
        try: result = CtlClient(args.run).decide(args.id, args.option, args.version, args.text, by=("lead:" + session_id) if session_id else "lead")
        except CtlError as exc: print(json.dumps({"ok": False, "error": str(exc)}, ensure_ascii=False)); return 1
        print(json.dumps(result, ensure_ascii=False)); return 0 if result.get("ok") else 1
    if args.command == "doctor":
        values = {agent: int(os.environ.get("XAY_SLOTS_" + agent.upper(), default)) for agent, default in (("codex", 3), ("dsh", 4), ("agy", 2))}
        print(json.dumps({"slots": values, "wait_default_s": 240}, ensure_ascii=False, indent=2)); return 0
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
