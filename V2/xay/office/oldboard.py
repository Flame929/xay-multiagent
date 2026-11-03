"""旧看板（scripts/xay-watch.html）的全局数据接口 GET /api/state。

从 V2 账本中提炼出旧看板页面所需要的全局数据：
- now: 当前时间戳
- seq: 账本最新序号
- summary: {needs_you, running, waiting, done_unread}
- workspaces: 按真实项目文件夹分组，每组 {id, path, name, legacy: false, conversations: [...], counts: {...}, status}
  - 临时文件夹：路径在 /tmp, /private/tmp, /var/folders, /private/var/folders, ~/.xay 下，或不存在的文件夹 -> 合并在一个 {name: "临时文件夹", path: "temp", legacy: true}
  - 对话：{id, title, lead_kind: "claude", workspace: {path, name}, status, updated, counts: {running, done, failed, lead_todo}, agents: [...], lead_host: "", archived: false, legacy}
- quota: 四条线路（codex-sub, codex-relay, dsh, agy）真实额度，取 xay.quota_live.lines_now(conn, now)
  - label 格式："Codex · Plus", "Codex · 中转", "DeepSeek", "Antigravity · <当前账号名>"
- decisions: 待用户决定的事项列表（未解决的 owner=user 或 for_user=True），id 为 V2 的 dc_...，带 conversation 和 version
- agents, runs, missions, thresholds, host: 提供必要结构
"""
from __future__ import annotations

import glob
import hashlib
import json
import os
import posixpath
import time
from typing import Any

from xay.core import events as core_events
from xay.core import snapshot as core_snapshot
from xay.core.reducers import legacy_status
from xay import quota_live
from xay import providers


TEMP_PREFIXES = ("/tmp", "/private/tmp", "/var/folders", "/private/var/folders")
ORDER = {"needs_you": 0, "running": 1, "idle": 2, "done": 3}
KNOWN_AGENTS = providers.agent_kinds()


# 工作区归类和旧看板（scripts/xay_watch.py 的 _workspace_for）一致：Claude 桌面版的对话按它打开的文件夹分组；
# 桌面版"没选文件夹"的对话都在 scratch-workspaces 下，合成一个"这台电脑"组；其余取 git 仓库根的文件夹名。
CLAUDE_SESSIONS = os.environ.get("XAY_CLAUDE_SESSIONS") or "~/Library/Application Support/Claude/claude-code-sessions"
CLAUDE_SCRATCH = os.environ.get("XAY_CLAUDE_SCRATCH") or "~/Library/Application Support/Claude/scratch-workspaces"
THIS_COMPUTER = {"path": "this-computer", "name": "这台电脑"}
_CLAUDE_CACHE: dict[str, Any] = {"files": {}, "by_cli": {}}


def claude_sessions() -> dict[str, dict[str, Any]]:
    """Claude 桌面版会话索引：cliSessionId → {cwd, archived}。AGENTS.md：只取目录、是否归档这几个字段，其余读完立刻丢，
    不读对话内容。按文件修改时间缓存。"""
    root = os.path.expanduser(CLAUDE_SESSIONS)
    files = _CLAUDE_CACHE["files"]
    seen, changed = set(), False
    for path in glob.glob(os.path.join(root, "*", "*", "local_*.json")):
        seen.add(path)
        try:
            mtime = os.stat(path).st_mtime_ns
        except OSError:
            continue
        if path in files and files[path][0] == mtime:
            continue
        try:
            with open(path, encoding="utf-8") as fh:
                raw = json.load(fh)
        except (OSError, ValueError):
            continue
        if not isinstance(raw, dict):
            continue
        keep = {"cli": str(raw.get("cliSessionId") or ""), "cwd": str(raw.get("cwd") or raw.get("originCwd") or ""),
                "archived": bool(raw.get("isArchived")), "last": raw.get("lastActivityAt") or 0}
        del raw
        files[path] = (mtime, keep)
        changed = True
    for path in list(files):
        if path not in seen:
            files.pop(path)
            changed = True
    if changed:
        by_cli: dict[str, dict[str, Any]] = {}
        for _mtime, keep in files.values():
            old = by_cli.get(keep["cli"])
            if keep["cli"] and (old is None or (keep["last"] or 0) >= (old["last"] or 0)):
                by_cli[keep["cli"]] = keep
        _CLAUDE_CACHE["by_cli"] = by_cli
    return _CLAUDE_CACHE["by_cli"]


def _git_root(path: str) -> str:
    cur = path
    while True:
        if os.path.exists(os.path.join(cur, ".git")):
            return cur
        parent = os.path.dirname(cur)
        if parent == cur:
            return path
        cur = parent


def workspace_for(path: str) -> dict[str, str]:
    """路径 → {path, name}：这台电脑 / 临时文件夹 / git 仓库根（或目录本身）。"""
    full = os.path.abspath(os.path.expanduser(path)) if path else ""
    scratch = os.path.abspath(os.path.expanduser(CLAUDE_SCRATCH))
    if full and (full == scratch or full.startswith(scratch + "/")):
        return dict(THIS_COMPUTER)
    if is_temp_folder(full):
        return {"path": "temp", "name": "临时文件夹"}
    root = _git_root(full)
    return {"path": root, "name": posixpath.basename(root.rstrip("/")) or root}


def is_temp_folder(path: str) -> bool:
    if not path or path == "temp":
        return True
    p = os.path.abspath(os.path.expanduser(path))
    if any(p == pre or p.startswith(pre + "/") for pre in TEMP_PREFIXES):
        return True
    xay_home = os.path.abspath(os.path.expanduser("~/.xay"))
    if p == xay_home or p.startswith(xay_home + "/"):
        return True
    if not os.path.isdir(p):
        return True
    return False


def _conv_status(needs_you: int, running: int, done: int, tasks_count: int, ended: bool) -> str:
    if needs_you > 0:
        return "needs_you"
    if running > 0:
        return "running"
    if done > 0 and running == 0:
        return "done"
    if ended:
        return "done"
    return "idle"


def _format_quota(raw_lines: list[dict[str, Any]]) -> list[dict[str, Any]]:
    out = []
    lines_by_name = {q.get("line"): q for q in raw_lines}
    order = ("codex-sub", "codex-relay", "dsh", "agy")
    for line_key in order:
        q = lines_by_name.get(line_key) or {}
        agent = q.get("agent") or quota_live.LINE_AGENT.get(line_key, "codex")
        if line_key == "codex-sub":
            label = "Codex · Plus"
        elif line_key == "codex-relay":
            label = "Codex · 中转"
        elif line_key == "dsh":
            label = "DeepSeek"
        elif line_key == "agy":
            acc = q.get("account")
            label = f"Antigravity · {acc}" if acc else "Antigravity"
        else:
            label = q.get("label") or line_key

        windows = []
        for w in q.get("windows") or []:
            if isinstance(w, dict):
                windows.append({
                    "name": w.get("name") or "",
                    "key": w.get("key") or "",
                    "remaining_pct": w.get("remaining_pct"),
                    "resets_at": w.get("resets_at"),
                })

        tightest = min((w for w in windows if w.get("remaining_pct") is not None),
                       key=lambda w: w["remaining_pct"], default=None)
        resets_at = tightest.get("resets_at") if tightest else None

        source_label = q.get("source_label")
        if not source_label:
            source_label = "官方实时查询" if q.get("source") == "live" else "本机估算"

        formatted = {
            "line": line_key,
            "agent": agent,
            "label": label,
            "status": q.get("status") or "unknown",
            "remaining_pct": q.get("remaining_pct"),
            "windows": windows,
            "resets_at": resets_at,
            "balance": q.get("balance"),
            "accounts": q.get("accounts") or [],
            "source_label": source_label,
            "fetched_at": q.get("fetched_at"),
            "live_error": q.get("error"),
            "used_5h": 0,
            "burn_per_h": None,
        }
        out.append(formatted)
    return out


def state(conn, now: float | None = None) -> dict[str, Any]:
    now = time.time() if now is None else float(now)
    with core_events._reading(conn) as s:
        workspaces_raw = s.get("workspaces", {})
        convs_raw = s.get("convs", {})
        tasks_raw = s.get("tasks", {})
        legacy_runs_raw = s.get("legacy_runs", {})
        decisions_raw = s.get("decisions", {})
        employees_raw = s.get("employees", {})
        top_seq = s.get("seq", 0)

        # 整理未决的决策
        open_decisions = []
        for d in decisions_raw.values():
            if d.get("resolved"):
                continue
            owner = d.get("owner", "user")
            for_user = (owner == "user")
            open_decisions.append({
                "id": d.get("id"),
                "conversation": d.get("conv"),
                "version": d.get("version", 1),
                "owner": owner,
                "for_user": for_user,
                "title": d.get("title") or "需要你决定",
                "detail": d.get("detail") or "",
                "kind": d.get("kind") or "question",
                "created": d.get("ts") or now,
                "options": d.get("options") or [],
                "resolved": False,
            })

        user_decisions = [d for d in open_decisions if d["for_user"]]

        # 按对话统计任务和员工
        conv_stats: dict[str, dict[str, Any]] = {}
        for cid, c in convs_raw.items():
            conv_stats[cid] = {
                "tasks_count": 0,
                "running": 0,
                "done": 0,
                "failed": 0,
                "lead_todo": 0,
                "needs_you": 0,
                "agents": set(),
                "_v2": 0,
                "_legacy": 0,
                "updated": c.get("active_ts") or c.get("created") or now,
            }

        # 加上未决决策中针对该对话的计数
        for d in open_decisions:
            cid = d.get("conversation")
            if cid in conv_stats:
                if d.get("for_user"):
                    conv_stats[cid]["needs_you"] += 1
                else:
                    conv_stats[cid]["lead_todo"] += 1

        for t in tasks_raw.values():
            cid = t.get("conv")
            if cid not in conv_stats:
                continue
            st = conv_stats[cid]
            st["tasks_count"] += 1
            st["_v2"] += 1
            status = t.get("status")
            if status == "working":
                st["running"] += 1
            elif status in ("done", "reported", "landed", "accepted"):
                st["done"] += 1
            elif status in ("failed", "canceled"):
                st["failed"] += 1

            owner = t.get("owner")
            if owner:
                emp = employees_raw.get(owner) or {}
                agent = emp.get("agent")
                if agent in providers.agent_kinds():
                    st["agents"].add(agent)

        for run in legacy_runs_raw.values():
            cid = run.get("conv")
            if cid not in conv_stats:
                continue
            st = conv_stats[cid]
            st["tasks_count"] += 1
            st["_legacy"] += 1
            status = legacy_status(run)
            if status == "working":
                st["running"] += 1
            elif status == "done":
                st["done"] += 1
            elif status in ("failed", "orphaned"):
                st["failed"] += 1

            agent = run.get("agent")
            if not agent and run.get("em"):
                emp = employees_raw.get(run["em"]) or {}
                agent = emp.get("agent")
            if agent in providers.agent_kinds():
                st["agents"].add(agent)

        # 构建工作区和对话分组
        temp_group: dict[str, Any] = {
            "id": "ws_temp",
            "path": "temp",
            "name": "临时文件夹",
            "legacy": True,
            "conversations": [],
            "counts": {"running": 0, "done": 0, "failed": 0, "lead_todo": 0},
            "status": "idle",
        }
        real_groups: dict[str, dict[str, Any]] = {}
        claude_of = {conv: alias[len("claude:"):] for alias, conv in (s.get("aliases") or {}).items()
                     if isinstance(alias, str) and alias.startswith("claude:")}
        try:
            sessions = claude_sessions()
        except Exception:  # noqa: BLE001  读不到桌面版的索引：按 xay 自己记的目录分
            sessions = {}

        for cid, c in convs_raw.items():
            st = conv_stats[cid]
            ws_id = c.get("ws")
            ws_record = workspaces_raw.get(ws_id) or {}
            sess = sessions.get(claude_of.get(cid) or "") or {}
            ws_info = workspace_for(sess.get("cwd") or ws_record.get("path") or "")
            orig_path, name = ws_info["path"], ws_info["name"]
            is_temp = orig_path == "temp"
            # 旧系统做的对话也是用户真实的历史，照常列在它的文件夹下（旧看板也是这样）；只有临时文件夹里的折叠成"以前的任务"
            conv_legacy = is_temp

            c_status = _conv_status(st["needs_you"], st["running"], st["done"], st["tasks_count"], bool(c.get("ended")))
            sorted_agents = [a for a in providers.agent_kinds() if a in st["agents"]]

            conv_item = {
                "id": cid,
                "title": c.get("title") or "",
                "lead_kind": c.get("lead_kind") or "claude",
                "workspace": ws_info,
                "status": c_status,
                "updated": st["updated"],
                "counts": {
                    "running": st["running"],
                    "done": st["done"],
                    "failed": st["failed"],
                    "lead_todo": st["lead_todo"],
                },
                "agents": sorted_agents,
                "lead_host": c.get("lead_host") or "",
                "archived": bool(c.get("archived") or sess.get("archived")),
                "legacy": conv_legacy,
                "progress": core_snapshot.calc_progress(s, cid),
            }

            if is_temp:
                temp_group["conversations"].append(conv_item)
                for k in ("running", "done", "failed", "lead_todo"):
                    temp_group["counts"][k] += conv_item["counts"][k]
            else:
                if orig_path not in real_groups:
                    group_id = ws_id or f"ws_{hashlib.sha1(orig_path.encode()).hexdigest()[:8]}"
                    group_name = name
                    real_groups[orig_path] = {
                        "id": group_id,
                        "path": orig_path,
                        "name": group_name,
                        "legacy": False,
                        "conversations": [],
                        "counts": {"running": 0, "done": 0, "failed": 0, "lead_todo": 0},
                        "status": "idle",
                    }
                g = real_groups[orig_path]
                g["conversations"].append(conv_item)
                for k in ("running", "done", "failed", "lead_todo"):
                    g["counts"][k] += conv_item["counts"][k]

        def _calc_group_status(convs: list[dict[str, Any]]) -> str:
            if any(c["status"] == "needs_you" for c in convs):
                return "needs_you"
            if any(c["status"] == "running" for c in convs):
                return "running"
            if any(c["status"] == "done" for c in convs):
                return "done"
            return "idle"

        def _sort_convs(convs: list[dict[str, Any]], legacy: bool) -> list[dict[str, Any]]:
            if legacy:
                return sorted(convs, key=lambda c: c["updated"] or 0, reverse=True)
            return sorted(convs, key=lambda c: (ORDER.get(c["status"], 2), -(c["updated"] or 0)))

        # 排序与状态汇总
        workspaces_out = []
        for g in real_groups.values():
            g["status"] = _calc_group_status(g["conversations"])
            g["conversations"] = _sort_convs(g["conversations"], False)

        ordered_real = sorted(
            real_groups.values(),
            key=lambda g: max((c["updated"] or 0 for c in g["conversations"]), default=0),
            reverse=True,
        )
        workspaces_out.extend(ordered_real)

        if temp_group["conversations"]:
            temp_group["status"] = _calc_group_status(temp_group["conversations"])
            temp_group["conversations"] = _sort_convs(temp_group["conversations"], True)
            workspaces_out.append(temp_group)

        # 额度
        raw_quota = quota_live.lines_now(conn, now)
        quota_out = _format_quota(raw_quota)

        # 概要统计
        summary = {
            "needs_you": len(user_decisions),
            "running": sum(c["counts"]["running"] for ws in workspaces_out for c in ws["conversations"] if not ws["legacy"]),
            "waiting": sum(1 for t in tasks_raw.values() if t.get("status") in ("queued", "waiting", "waiting_deps", "waiting_slot")),
            "done_unread": 0,
        }

    return {
        "now": now,
        "seq": top_seq,
        "summary": summary,
        "workspaces": workspaces_out,
        "quota": quota_out,
        "decisions": open_decisions,
        "agents": {},
        "runs": [],
        "missions": [],
        "thresholds": {},
        "host": "",
    }
