"""旧看板（scripts/xay-watch.html）的对话详情、改动和控制接口。

供 web.py 按如下签名调用：
- `conversation(conn, conv_id, now=None) -> dict | None`
- `diff(conn, execution, path) -> dict`
- `control(conn, body, query) -> dict`
- `decide(conn, body, query) -> dict`
"""
import json
import logging
import os
import subprocess
import time
from pathlib import Path

from xay.core import events as core_events
from xay.core.reducers import MEMORY_TURNS, dept_key, legacy_status
from xay.office import employee, paths, plain

LOG = logging.getLogger("xay.office.oldboard_conv")

MAX_DIFF_BYTES = 200 * 1024
MAX_TEXT = 4000
MAX_STEPS = 60


def _str(v):
    return v.strip() if isinstance(v, str) else ""


def _num(v):
    return v if isinstance(v, (int, float)) and not isinstance(v, bool) else None


def _legacy_task_id(run):
    run_dir = (run.get("run_dir") or "").rstrip("/")
    if run_dir:
        return "run:" + os.path.basename(run_dir)
    return "run:" + (run.get("run") or run.get("em") or "")


def conversation(conn, conv_id, now=None):
    """GET /api/conversation?id=<对话编号>。找不到返回 None。"""
    if not conv_id:
        return None
    state = core_events.load_state(conn)
    conv = state.get("convs", {}).get(conv_id)
    if conv is None:
        return None
    now = time.time() if now is None else now

    ws_id = conv.get("ws")
    ws_record = state.get("workspaces", {}).get(ws_id, {})
    ws_info = {
        "path": ws_record.get("path") or "",
        "name": ws_record.get("name") or ws_id or "",
    }
    lead_kind = conv.get("lead_kind") or "claude"

    # 查出属于该对话的全部事件，方便后续提取任务、插话等
    all_conv_events = conn.execute(
        "SELECT seq, ts, type, subject, data FROM events WHERE conv=? ORDER BY seq",
        (conv_id,),
    ).fetchall()

    threads = []
    live_cache = {}

    def get_live(em_key):
        if not em_key:
            return [], []
        if em_key not in live_cache:
            live_cache[em_key] = employee.scan_live(paths.live_root(), em_key)
        return live_cache[em_key]

    # 1. V2 任务（撤回的 task.status == 'canceled' 不列）
    v2_tasks = [
        t for t in state.get("tasks", {}).values()
        if t.get("conv") == conv_id and t.get("status") != "canceled"
    ]
    # 按创建时间或 id 稳定排序
    v2_tasks.sort(key=lambda x: (x.get("started") or 0, x.get("id") or ""))

    for t in v2_tasks:
        tk_id = t["id"]
        owner_key = t.get("owner")
        emp = state.get("employees", {}).get(owner_key, {})
        agent = emp.get("agent") or "codex"
        worker = emp.get("label") or emp.get("key") or ""
        title = t.get("title") or "未命名任务"
        dept_id = t.get("dept")
        phase_title = ""
        if dept_id:
            d_rec = state.get("depts", {}).get(dept_key(conv_id, dept_id))
            phase_title = (d_rec or {}).get("name") or dept_id

        # Codex live steer；其它 kill
        if agent == "codex":
            capabilities = {"steer": "live", "interrupt": "soft"}
        else:
            capabilities = {"steer": "none", "interrupt": "kill"}

        # 属于本任务的轮次（过滤记忆整理轮次）
        task_turns = [
            tr for tr in state.get("turns", {}).values()
            if tr.get("task") == tk_id and tr.get("kind") not in MEMORY_TURNS and tr.get("reason") != "memory"
        ]
        task_turns.sort(key=lambda x: x.get("started") or 0)

        # 本任务相关事件
        this_task_events = [r for r in all_conv_events if r["subject"] == tk_id]
        added_ts = next((r["ts"] for r in this_task_events if r["type"] == "task.added"), None)
        started = (task_turns[0].get("started") if task_turns else None) or added_ts or now
        updated = (task_turns[-1].get("ended") if task_turns and task_turns[-1].get("ended") else None) or (task_turns[-1].get("started") if task_turns else None) or started

        # 状态映射
        raw_status = t.get("status")
        if raw_status == "working":
            page_status = "running"
        elif raw_status in ("reported", "done"):
            page_status = "done"
        elif raw_status == "failed":
            page_status = "failed"
        elif raw_status == "needs_you":
            page_status = "needs_you"
        elif raw_status == "paused":
            page_status = "paused"
        elif raw_status in ("queued", "planned", "ready"):
            page_status = "queued"
        else:
            page_status = raw_status or "idle"

        # active_run, active_since
        active_run = None
        active_since = None
        for tr in task_turns:
            if tr.get("ended") is None or tr.get("status") == "running":
                active_run = tr.get("id")
                active_since = tr.get("started")
                break

        # work_s
        work_s = 0.0
        for tr in task_turns:
            s_ts = tr.get("started") or 0
            e_ts = tr.get("ended")
            if e_ts:
                work_s += max(0.0, e_ts - s_ts)
            else:
                work_s += max(0.0, now - s_ts)

        # lead_todo
        lead_todo = None
        if raw_status == "needs_you" or t.get("kept_kind"):
            lead_todo = {"kind": t.get("kept_kind") or "conflict", "title": t.get("kept_detail") or "改动先保留下来了"}

        # 构造 messages
        messages = []
        # 1. 任务卡全文 lead 消息
        card_text = t.get("card") or t.get("title") or ""
        messages.append({
            "kind": "lead",
            "id": f"{tk_id}#task",
            "ts": started,
            "text": card_text,
            "summary": title,
            "source": "task",
        })

        # 2. 插话（steer 消息）
        # control.requested: action == "steer", subject == tk_id 或 subject == owner_key
        task_ended = task_turns[-1].get("ended") if task_turns else None
        for r in all_conv_events:
            if r["type"] != "control.requested":
                continue
            if r["subject"] not in (tk_id, owner_key):
                continue
            try:
                c_data = json.loads(r["data"]) if isinstance(r["data"], str) else (r["data"] or {})
            except ValueError:
                continue
            if c_data.get("action") != "steer":
                continue
            # 判断时间范围：在任务开始之后
            c_ts = r["ts"]
            if c_ts < (started - 2):
                continue
            if task_ended and c_ts > (task_ended + 2):
                continue
            s_text = c_data.get("text", "")
            if not s_text:
                continue
            s_summary = s_text.splitlines()[0][:40] if s_text else ""
            messages.append({
                "kind": "lead",
                "id": f"steer-{r['seq']}",
                "ts": c_ts,
                "text": s_text,
                "summary": s_summary,
                "source": "steer",
            })

        # 3. 每一轮一条 AI 消息
        live_rows, live_usage = get_live(owner_key)
        usage_by_turn = {u["turn"]: u["tokens"] for u in live_usage if "turn" in u and "tokens" in u}

        for idx, tr in enumerate(task_turns):
            turn_id = tr["id"]
            tr_started = tr.get("started") or started
            tr_ended = tr.get("ended")
            is_running = (tr_ended is None) and (page_status == "running" or tr.get("status") == "running")
            streaming = is_running

            if is_running:
                ai_status = "running"
            elif tr.get("status") in ("failed", "error"):
                ai_status = "failed"
            elif tr.get("status") in ("interrupted", "canceled"):
                ai_status = "interrupted"
            else:
                ai_status = "completed"

            # 取直播行
            lo = tr_started - 1
            hi = (tr_ended or now) + 2
            tr_rows = [r for r in live_rows if lo <= r.get("ts", 0) <= hi]

            # 步骤 steps: 这一轮时间段的 tool 行，最多 60 条，做完的 done，正在做的最后一条 active
            tool_rows = [r for r in tr_rows if r.get("kind") == "tool"][:MAX_STEPS]
            steps = []
            for s_idx, trow in enumerate(tool_rows):
                raw_cmd = trow.get("text") or ""
                t_name = str(trow.get("tool") or "")
                t_kind = trow.get("tool_kind")
                info = plain.classify_loose(raw_cmd, t_kind, t_name)
                cat = info.get("cat") or "tool"
                # 页面图标类型
                mapped_kind = {"git": "cmd", "check": "cmd", "test": "cmd", "script": "cmd", "install": "cmd", "fetch": "web", "list": "search"}.get(cat, cat)
                is_last = (s_idx == len(tool_rows) - 1)
                state_str = "active" if (is_running and is_last) else "done"
                steps.append({
                    "kind": mapped_kind,
                    "text": info.get("text") or t_name or "做了一步",
                    "raw": raw_cmd,
                    "state": state_str,
                })

            # thinking
            think_parts = [r.get("text") or "" for r in tr_rows if r.get("kind") == "thinking"]
            thinking_text = "".join(think_parts)[-2000:] if think_parts else ""

            # text: 回复文字，最多 4000 字
            text_parts = [r.get("text") or "" for r in tr_rows if r.get("kind") == "text"]
            ai_text = "".join(text_parts) if text_parts else (tr.get("reply") or tr.get("report") or "")
            ai_text = ai_text[:MAX_TEXT]

            # final: task.reported 的 summary，没有就取回复里【结果】那段；没结束为 null
            if is_running:
                final_text = None
            else:
                rep = tr.get("report") or t.get("result") or ""
                _, summ = plain.summary_from_report(rep)
                final_text = summ
                if not final_text and ai_text:
                    if "【结果】" in ai_text:
                        final_text = ai_text.split("【结果】", 1)[1].strip()
                    else:
                        _, s2 = plain.summary_from_report(ai_text)
                        final_text = s2 or (ai_text.strip().splitlines()[-1] if ai_text.strip() else "")

            # files
            ev = tr.get("evidence") or {}
            ch_files = list(ev.get("changed_files") or [])
            if not ch_files and idx == len(task_turns) - 1:
                # 最后一轮也可查 task.landed
                landed_ev = next((r for r in reversed(this_task_events) if r["type"] == "task.landed"), None)
                if landed_ev:
                    try:
                        ld = json.loads(landed_ev["data"]) if isinstance(landed_ev["data"], str) else (landed_ev["data"] or {})
                        ch_files = list(ld.get("files") or [])
                    except ValueError:
                        pass

            # usage
            tokens = usage_by_turn.get(turn_id)
            if tokens is None and isinstance(ev.get("usage"), dict):
                tokens = ev["usage"].get("tokens")
            usage = {"tokens": tokens} if tokens is not None else None

            elapsed_s = max(0.0, ((tr_ended or now) - tr_started))

            messages.append({
                "kind": "ai",
                "id": turn_id,
                "ts": tr_started,
                "run": tk_id,
                "turn": idx + 1,
                "status": ai_status,
                "streaming": streaming,
                "steps": steps,
                "thinking": thinking_text,
                "text": ai_text,
                "final": final_text,
                "files": ch_files,
                "usage": usage,
                "ended": tr_ended if not streaming else None,
                "elapsed_s": elapsed_s,
            })

        # 按时间排序所有 messages
        messages.sort(key=lambda m: (m.get("ts") or 0, 0 if m["kind"] == "lead" else 1))

        threads.append({
            "id": tk_id,
            "agent": agent,
            "title": title,
            "status": page_status,
            "started": started,
            "updated": updated,
            "phase_title": phase_title,
            "capabilities": capabilities,
            "active_run": active_run,
            "messages": messages,
            "work_s": work_s,
            "active_since": active_since,
            "lead_todo": lead_todo,
            "latest_run": tk_id,
            "worker": worker,
        })

    # 2. 旧系统镜像进来的旧活（state['legacy_runs']）
    legacy_runs = [
        r for r in state.get("legacy_runs", {}).values()
        if r.get("conv") == conv_id
    ]
    legacy_runs.sort(key=lambda x: (x.get("started") or 0, x.get("ts") or 0))

    for r in legacy_runs:
        leg_id = _legacy_task_id(r)
        agent = r.get("agent") or "codex"
        title = r.get("title") or "旧任务"
        l_status = legacy_status(r)
        if l_status == "working":
            page_status = "running"
        elif l_status == "done":
            page_status = "done"
        elif l_status == "failed":
            page_status = "failed"
        elif l_status == "canceled":
            page_status = "canceled"
        else:
            page_status = l_status

        started = _num(r.get("started")) or _num(r.get("ts")) or now
        ended = _num(r.get("ended"))
        updated = ended or started
        worker = r.get("worker") or r.get("em") or ""
        phase_title = r.get("dept") or ""
        res_head = r.get("result_head") or ""

        # 一条 lead 消息（标题）+ 一条 AI 消息（final = result_head，status 按旧状态），不带步骤
        messages = [
            {
                "kind": "lead",
                "id": f"{leg_id}#task",
                "ts": started,
                "text": title,
                "summary": title,
                "source": "task",
            },
            {
                "kind": "ai",
                "id": f"{leg_id}#1",
                "ts": started,
                "run": leg_id,
                "turn": 1,
                "status": "completed" if page_status == "done" else page_status,
                "streaming": False,
                "steps": [],
                "thinking": "",
                "text": res_head,
                "final": res_head,
                "files": [],
                "usage": None,
                "ended": ended,
                "elapsed_s": max(0.0, (ended - started)) if ended else 0.0,
            },
        ]

        threads.append({
            "id": leg_id,
            "agent": agent,
            "title": title,
            "status": page_status,
            "started": started,
            "updated": updated,
            "phase_title": phase_title,
            "capabilities": {"steer": "none", "interrupt": "kill"},
            "active_run": None,
            "messages": messages,
            "work_s": max(0.0, ((ended or started) - started)),
            "active_since": None,
            "lead_todo": None,
            "latest_run": leg_id,
            "worker": worker,
        })

    # 计算 conversation 统计与状态
    running_cnt = sum(1 for t in threads if t["status"] in ("running", "starting", "streaming"))
    done_cnt = sum(1 for t in threads if t["status"] in ("done", "completed"))
    failed_cnt = sum(1 for t in threads if t["status"] in ("failed", "timeout", "error"))
    lead_todo_cnt = sum(1 for t in threads if t.get("lead_todo"))

    has_user_dec = any(
        (d.get("conv") == conv_id) and not d.get("resolved") and (d.get("for_user") is True or d.get("owner") == "user")
        for d in state.get("decisions", {}).values()
    )
    if has_user_dec or any(t["status"] == "needs_you" for t in threads):
        conv_status = "needs_you"
    elif running_cnt > 0:
        conv_status = "running"
    elif threads and all(t["status"] in ("done", "completed") for t in threads):
        conv_status = "done"
    else:
        conv_status = "idle"

    agents = []
    for t in threads:
        if t["agent"] and t["agent"] not in agents:
            agents.append(t["agent"])

    all_updated = [t["updated"] for t in threads if t.get("updated")] + [conv.get("active_ts") or 0]
    conv_updated = max(all_updated) if all_updated else now

    return {
        "conversation": {
            "id": conv_id,
            "title": conv.get("title") or "",
            "lead_kind": lead_kind,
            "workspace": ws_info,
            "status": conv_status,
            "updated": conv_updated,
            "counts": {
                "running": running_cnt,
                "done": done_cnt,
                "failed": failed_cnt,
                "lead_todo": lead_todo_cnt,
            },
            "agents": agents,
            "lead_host": "",
            "archived": False,
            "legacy": False,
        },
        "threads": threads,
    }


def _is_safe_relpath(path: str) -> bool:
    if not path or not isinstance(path, str):
        return False
    if path.startswith("/") or path.startswith("\\"):
        return False
    p = Path(path)
    if p.is_absolute():
        return False
    if ".." in p.parts:
        return False
    return True


def _is_latest_task_for_owner(conn, state: dict, owner: str, execution: str) -> bool:
    emp = state.get("employees", {}).get(owner, {})
    if emp.get("current_task") == execution or emp.get("last_task") == execution:
        return True
    emp_tasks = [
        tid for tid, t in state.get("tasks", {}).items()
        if t.get("owner") == owner and t.get("status") != "canceled"
    ]
    if not emp_tasks:
        return False
    if len(emp_tasks) == 1:
        return emp_tasks[0] == execution
    try:
        placeholders = ",".join("?" for _ in emp_tasks)
        row = conn.execute(
            f"SELECT subject FROM events WHERE subject IN ({placeholders}) ORDER BY seq DESC LIMIT 1",
            emp_tasks,
        ).fetchone()
        if row and (row[0] == execution or row["subject"] == execution):
            return True
    except Exception:
        pass
    return False


def _tracked(desk_dir: Path, path: str) -> bool:
    """这个文件在桌子上是不是已经被 git 管着（管着又没改，就不该显示成新文件）。"""
    proc = subprocess.run(["git", "-C", str(desk_dir), "ls-files", "--", path],
                          capture_output=True, text=True, errors="replace", timeout=5)
    return bool((proc.stdout or "").strip())


def diff(conn, execution, path):
    """GET /api/diff?execution=<线程的 run>&path=<文件>。

    能从员工桌子（git）拿到改动就给出（限 200 KB），拿不到返回页面能显示的空改动加 note，不抛 500。
    """
    if not _is_safe_relpath(path):
        return {"diff": "", "note": "拿不到这件小任务的改动"}
    if not execution or not isinstance(execution, str):
        return {"diff": "", "note": "拿不到这件小任务的改动"}

    try:
        state = core_events.load_state(conn)
        task = state.get("tasks", {}).get(execution)
        if not task:
            return {"diff": "", "note": "拿不到这件小任务的改动"}
        owner = task.get("owner")
        ws = task.get("ws")
        if not owner or not ws:
            return {"diff": "", "note": "拿不到这件小任务的改动"}
        desk_dir = paths.v2_home() / "desks" / ws / owner
        if not desk_dir.is_dir():
            return {"diff": "", "note": "拿不到这件小任务的改动"}

        # 1. 寻找落地标记：xay: 任务 <小任务编号> 已落地
        msg_target = f"xay: 任务 {execution} 已落地"
        log_proc = subprocess.run(
            ["git", "-C", str(desk_dir), "log", "--all", "--format=%H", "-F", f"--grep={msg_target}", "-1"],
            capture_output=True,
            text=True,
            errors="replace",
            timeout=5,
        )
        marker = (log_proc.stdout or "").strip()
        if marker:
            # 找到了落地标记：尝试 git diff <标记>^ <标记> -- <path>
            # 标记没有父提交时用 git diff-tree -p --root <标记> -- <path>
            p_diff = subprocess.run(
                ["git", "-C", str(desk_dir), "diff", f"{marker}^", marker, "--", path],
                capture_output=True,
                text=True,
                errors="replace",
                timeout=5,
            )
            if p_diff.returncode == 0:
                out = p_diff.stdout or ""
            else:
                p_tree = subprocess.run(
                    ["git", "-C", str(desk_dir), "diff-tree", "-p", "--root", marker, "--", path],
                    capture_output=True,
                    text=True,
                    errors="replace",
                    timeout=5,
                )
                out = p_tree.stdout or ""
            return {"diff": out[:MAX_DIFF_BYTES]}

        # 2. 找不到标记（还没落地：在做、保留待定、失败但有改动）
        # 只有当这件小任务是这个员工最近一件活时，给出桌子上"开工时版本 → 现在工作区"的改动
        if not _is_latest_task_for_owner(conn, state, owner, execution):
            return {"diff": "", "note": "拿不到这件小任务的改动"}

        from xay.desk.desk import read_state
        st = read_state(desk_dir)
        base = st.get("base")
        if not base:
            return {"diff": "", "note": "拿不到这件小任务的改动"}

        proc = subprocess.run(
            ["git", "-C", str(desk_dir), "diff", base, "--", path],
            capture_output=True,
            text=True,
            errors="replace",
            timeout=5,
        )
        out = proc.stdout or ""
        if not out and (desk_dir / path).is_file() and not _tracked(desk_dir, path):
            # 新建、还没被 git 管的文件：整份当作新加的（已被 git 管又没改的文件不算）
            proc_new = subprocess.run(
                ["git", "-C", str(desk_dir), "diff", "--no-index", "--", "/dev/null", path],
                capture_output=True,
                text=True,
                errors="replace",
                timeout=5,
            )
            out = proc_new.stdout or ""

        return {"diff": out[:MAX_DIFF_BYTES]}
    except Exception as e:
        LOG.debug("diff query failed for %s %s: %s", execution, path, e)
        return {"diff": "", "note": "拿不到这件小任务的改动"}


def control(conn, body, query):
    """POST /api/control。

    body: {run/id/execution, op/action, text...}
    op 是插话 / 续接新指示（steer、resume）→ 写一条 steer
    停下 / 打断类 → stop / interrupt
    其他 op → HttpError 400
    """
    from xay.office.web import HttpError

    if not isinstance(body, dict):
        raise HttpError(400, "请求内容必须是 JSON 对象")
    run = _str(body.get("run") or body.get("id") or body.get("execution"))
    op = _str(body.get("op") or body.get("action"))
    text = body.get("text")

    if not run:
        raise HttpError(400, "缺少任务编号")
    if not op:
        raise HttpError(400, "缺少操作类型")

    state = core_events.load_state(conn)
    task = state.get("tasks", {}).get(run)
    if not task:
        raise HttpError(404, "找不到这个任务")

    em = task.get("owner")
    if not em:
        raise HttpError(400, "该任务尚未分配员工")
    conv = task.get("conv")
    ws = task.get("ws")

    if op in ("steer", "resume"):
        if text is not None and not isinstance(text, str):
            raise HttpError(400, "附带的话必须是文字")
        if not text or not text.strip():
            raise HttpError(400, "要调整方向，请写清楚想说的话")
        data = {"action": "steer", "by_source": "page", "text": text.strip()[:2000]}
        seq = core_events.append(conn, "control.requested", "user", data, ws=ws, conv=conv, subject=em)
        return {"ok": True, "seq": seq}
    elif op in ("stop", "interrupt", "pause"):
        action = "stop" if op == "stop" else "interrupt"
        data = {"action": action, "by_source": "page"}
        seq = core_events.append(conn, "control.requested", "user", data, ws=ws, conv=conv, subject=em)
        return {"ok": True, "seq": seq}
    else:
        raise HttpError(400, f"不支持的操作: {op}")


def decide(conn, body, query):
    """POST /api/decision。

    body: {id: dc_..., option, version?, text?}
    """
    from xay.office.web import HttpError

    if not isinstance(body, dict):
        raise HttpError(400, "请求内容必须是 JSON 对象")
    dc = _str(body.get("id"))
    option = _str(body.get("option"))
    if not dc or not option:
        raise HttpError(400, "缺少事项编号或选项")

    state = core_events.load_state(conn)
    decision = state.get("decisions", {}).get(dc)
    if not decision:
        raise HttpError(404, "找不到这件需要你拿主意的事，可能已经处理过了")

    options = [o for o in (decision.get("options") or []) if isinstance(o, dict)]
    if options and option not in [o.get("id") for o in options]:
        raise HttpError(400, "没有这个选项，可选：" + "、".join(str(o.get("label") or o.get("id")) for o in options))

    data = {"option": option}
    text = body.get("text")
    if isinstance(text, str) and text.strip():
        data["text"] = text.strip()[:2000]

    seq = core_events.append(
        conn,
        "decision.resolved",
        "user",
        data,
        ws=decision.get("ws"),
        conv=decision.get("conv"),
        subject=dc,
        dedup=f"page:decision:{dc}",
    )
    return {"ok": True, "seq": seq}
