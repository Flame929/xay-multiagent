#!/usr/bin/env python3
import argparse
import re
import json
import os
import signal
import subprocess
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse


def _base(value):
    return os.path.basename(value or "")


class EventParser:
    def __init__(self, agent: str):
        self.agent = agent
        self.step = 0
        self.tokens = None
        self.actions = []
        self._keys = {}
        self.final = None
        self.session = None
        self._agy_response = {}
        self._codex_seen = set()
        self._dsh_stream = None

    def _action(self, key, kind, text, state="active"):
        text = str(text or "")
        if key in self._keys:
            item = self.actions[self._keys[key]]
            if text:
                item["text"] = text
            item["state"] = state
            return
        self._keys[key] = len(self.actions)
        self.actions.append({"kind": kind, "text": text, "state": state})

    def feed(self, line: str) -> None:
        if not line.strip():
            return
        try:
            event = json.loads(line)
        except (ValueError, TypeError):
            return
        if self.agent == "codex":
            self._feed_codex(event)
        elif self.agent == "dsh":
            self._feed_dsh(event)
        elif self.agent == "agy":
            self._feed_agy(event)

    def _feed_codex(self, e):
        typ = e.get("type")
        if typ == "thread.started":
            self.session = e.get("thread_id")
        item = e.get("item") or {}
        iid = item.get("id")
        itype = item.get("type")
        if typ in ("item.started", "item.completed") and iid and itype != "error":
            if iid not in self._codex_seen:
                self._codex_seen.add(iid)
                self.step += 1
            if itype == "command_execution":
                self._action(iid, "cmd", self._strip_shell(item.get("command")), "active" if typ == "item.started" else ("done" if item.get("exit_code", 0) == 0 else "failed"))
            elif itype == "file_change":
                changes = item.get("changes") or []
                self._action(iid, "edit", ", ".join(_base(c.get("path")) for c in changes), "active" if typ == "item.started" else "done")
            elif itype == "reasoning":
                self._action(iid, "think", item.get("text", ""), "active" if typ == "item.started" else "done")
            elif itype == "agent_message":
                self.final = item.get("text")
                self._action(iid, "say", (item.get("text") or "")[:80], "done")
            elif itype == "web_search":
                self._action(iid, "search", item.get("query", ""), "active" if typ == "item.started" else "done")
            elif itype == "todo_list":
                self._action(iid, "plan", f"计划 {len(item.get('items') or [])} 项", "active" if typ == "item.started" else "done")
            elif itype == "mcp_tool_call":
                self._action(iid, "tool", item.get("name") or item.get("tool", ""), "active" if typ == "item.started" else "done")
        elif typ == "turn.failed":
            self._action("turn.failed", "say", (e.get("error") or {}).get("message", ""), "failed")
        elif typ == "turn.completed":
            usage = e.get("usage") or {}
            self.tokens = (usage.get("input_tokens") or 0) + (usage.get("output_tokens") or 0)

    def _feed_dsh(self, e):
        typ = e.get("type")
        if typ == "session":
            self.session = e.get("sessionId")
        elif typ == "status":
            phase = e.get("phase")
            if phase == "step_start":
                self.step += 1
            elif phase == "step_end" and (e.get("usage") or {}).get("totalTokens") is not None:
                self.tokens = e["usage"]["totalTokens"]
        elif typ == "tool_call":
            self._close_dsh_stream()
            call = e.get("callId") or f"tool-{len(self.actions)}"
            tool = e.get("tool", "")
            inp = e.get("input") or {}
            if tool == "bash":
                kind, text = "cmd", inp.get("command", "")
            elif tool == "read":
                kind, text = "read", inp.get("file_path", "")
            elif tool in ("write", "edit", "patch"):
                kind, text = "edit", _base(inp.get("file_path") or inp.get("path", ""))
            else:
                kind, text = "tool", tool
            self._action(call, kind, text)
        elif typ == "tool_result":
            call_id = e.get("callId")
            if call_id in self._keys:
                self._action(call_id, self.actions[self._keys[call_id]]["kind"], "", "done" if e.get("status") == "completed" else "failed")
        elif typ == "thinking":
            self._dsh_stream_event("thinking", "think", e.get("text", ""))
        elif typ == "text":
            self._dsh_stream_event("text", "say", e.get("text", ""))
        elif typ == "final":
            self._close_dsh_stream()
            self.final = e.get("text")

    def _close_dsh_stream(self):
        if self._dsh_stream is not None:
            stream_type, key = self._dsh_stream
            action = self.actions[self._keys[key]]
            action["state"] = "done"
            if action["kind"] == "say":
                action["text"] = action["text"][:80]
        self._dsh_stream = None

    def _dsh_stream_event(self, stream_type, kind, text):
        if self._dsh_stream is None or self._dsh_stream[0] != stream_type:
            self._close_dsh_stream()
            key = f"{stream_type}:{len(self.actions)}"
            self._dsh_stream = (stream_type, key)
            self._action(key, kind, text, "active")
        else:
            key = self._dsh_stream[1]
            index = self._keys[key]
            self.actions[index]["text"] += text or ""
            self.actions[index]["state"] = "active"

    def _feed_agy(self, e):
        if e.get("event") == "init":
            self.session = e.get("conversation_id")
            return
        if e.get("event") == "result":
            result = e.get("result") or {}
            self.session = result.get("conversation_id") or self.session
            self.final = result.get("response")
            usage = result.get("usage") or {}
            if usage.get("total_tokens") is not None:
                self.tokens = usage["total_tokens"]
            return
        if e.get("event") != "step_update":
            return
        s = e.get("step_update") or {}
        idx = s.get("step_index")
        if s.get("step_type") == "user_input":
            return
        if idx is not None:
            self.step = max(self.step, int(idx))
        state = "done" if s.get("state") == "DONE" else "active"
        stype = s.get("step_type")
        if stype == "agent_response":
            self._agy_response.setdefault(idx, "")
            self._agy_response[idx] += s.get("text_delta", "") or ""
            if self._agy_response[idx]:
                self._action(f"step:{idx}", "say", self._agy_response[idx][:80], state)
        elif stype == "tool":
            tool = s.get("tool_name", "")
            kind = {"read_file": "read", "view_file": "read", "write_to_file": "edit", "replace_file_content": "edit", "run_command": "cmd"}.get(tool, "tool")
            params = (s.get("tool_info") or {}).get("parameters") or {}
            value = next((params.get(k) for k in ("Path", "AbsolutePath", "TargetFile", "CommandLine", "Query") if params.get(k)), tool)
            self._action(f"step:{idx}", kind, _base(value) if kind in ("read", "edit") else value, state)
            usage = s.get("usage") or {}
            if state == "done" and usage.get("total_tokens") is not None:
                self.tokens = (self.tokens or 0) + usage["total_tokens"]

    @staticmethod
    def _strip_shell(command):
        for prefix in ("/bin/zsh -lc ", "/bin/bash -lc ", "bash -lc ", "zsh -lc "):
            if command.startswith(prefix):
                command = command[len(prefix):]
                break
        else:
            return command
        if len(command) >= 2 and command[0] == command[-1] == "'":
            command = command[1:-1].replace("'\\''", "'")
        elif len(command) >= 2 and command[0] == '"' and command[-1] in "\"'":
            command = command[1:-1].replace('\\"', '"').replace("\\\\", "\\")
        return command

    def snapshot(self) -> dict:
        if self.agent == "dsh":
            self._close_dsh_stream()
        return {"step": self.step, "tokens": self.tokens, "actions": self.actions[:], "final": self.final, "session": self.session}



class WatchData:
    AGENTS = ("codex", "dsh", "agy")
    ROLES = {"codex": "写核心代码", "dsh": "测试与批量活", "agy": "文案、界面与调研"}
    DEFAULT_SLOTS = {"codex": 3, "dsh": 4, "agy": 3}

    def __init__(self, xay_home):
        self.home = os.path.abspath(os.path.expanduser(xay_home))
        self.cache = {}
        self.lock = threading.Lock()

    def _read_json(self, path, default=None):
        try:
            with open(path, encoding="utf-8") as f:
                return json.load(f)
        except (OSError, ValueError, TypeError):
            return default

    def _read_text(self, path, default=""):
        try:
            with open(path, encoding="utf-8", errors="replace") as f:
                return f.read()
        except OSError:
            return default

    @staticmethod
    def _number(value, default=0):
        try:
            return int(float(value))
        except (TypeError, ValueError):
            return default

    @staticmethod
    def _time(value):
        if isinstance(value, (int, float)):
            return int(value)
        if isinstance(value, str):
            try:
                return int(float(value))
            except ValueError:
                pass
            for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%dT%H:%M:%S%z"):
                try:
                    return int(__import__("datetime").datetime.strptime(value, fmt).timestamp())
                except (ValueError, TypeError):
                    pass
        return 0

    def _registry(self):
        text = self._read_text(os.path.join(self.home, "registry.jsonl"), None)
        if text is None:
            return []
        rows, dec, pos = [], json.JSONDecoder(), 0
        cutoff = __import__("time").time() - 30 * 86400
        while True:
            while pos < len(text) and text[pos].isspace():
                pos += 1
            if pos >= len(text):
                break
            try:
                row, pos = dec.raw_decode(text, pos)
            except (ValueError, TypeError):
                nl = text.find("\n", pos)
                if nl < 0:
                    break
                pos = nl + 1
                continue
            if not isinstance(row, dict):
                continue
            stamp = self._time(row.get("time"))
            if stamp and stamp < cutoff:
                continue
            kind = row.get("kind")
            value = row.get("run_dir") if kind == "run" else row.get("dag_dir") if kind == "dag" else None
            if not value:
                continue
            value = os.path.abspath(os.path.expanduser(str(value)))
            if not os.path.exists(value):
                continue
            row = dict(row)
            row["run_dir" if kind == "run" else "dag_dir"] = value
            rows.append(row)
        return rows

    @staticmethod
    def _same_path(left, right):
        if not left or not right:
            return False
        return os.path.abspath(os.path.expanduser(str(left))) == os.path.abspath(os.path.expanduser(str(right)))

    def _alive(self, pid):
        try:
            pid = int(pid)
            if pid <= 0:
                return False
            os.kill(pid, 0)
            return True
        except (ValueError, OSError, ProcessLookupError):
            return False

    def _xay_alive(self, pid, marker="xay-"):
        # 进程号会被系统复用：除了存活，还要核对确实是 xay 的进程
        if not self._alive(pid):
            return False
        try:
            command = subprocess.run(["ps", "-o", "command=", "-p", str(int(pid))], capture_output=True, text=True, check=False).stdout
        except (OSError, ValueError):
            return False
        return marker in command

    def _events(self, run_dir, agent):
        path = os.path.join(run_dir, "events.raw")
        if not os.path.exists(path):
            return EventParser(agent).snapshot()
        with self.lock:
            size = os.path.getsize(path)
            old = self.cache.get(path)
            if old is None or size < old[0]:
                old = (0, EventParser(agent))
            offset, parser = old
            with open(path, encoding="utf-8", errors="replace") as f:
                f.seek(offset)
                for line in f:
                    parser.feed(line)
                offset = f.tell()
            self.cache[path] = (offset, parser)
            return parser.snapshot()

    def _task_text(self, run_dir):
        text = self._read_text(os.path.join(run_dir, "prompt.md"))
        if "# 任务" in text:
            text = text.split("# 任务", 1)[1]
        if "\n---" in text:
            text = text.split("\n---", 1)[0]
        return text.strip()

    def _title(self, run_dir, *sources):
        for source in sources:
            if isinstance(source, dict) and source.get("title"):
                return str(source["title"]).replace("\ufffd", "").strip()  # 旧版按字节截断留下的半个汉字
        line = next((x.strip() for x in self._task_text(run_dir).splitlines() if x.strip()), "")
        return line[:30] + ("…" if len(line) > 30 else "")  # 没有大白话任务名时，取任务首行并截短

    @staticmethod
    def _model(agent, model, effort):
        if agent == "codex":
            return "GPT（默认）" if not model or model == "默认" else str(model)
        if agent == "dsh":
            return "deepseek-v4.1-flash"
        effort = str(effort or "medium")
        tier = "low" if effort in ("minimal", "low") else "high" if effort in ("high", "xhigh", "max") else "medium"
        return f"gemini-3.8-flash-{tier}"

    @staticmethod
    def _now_action(actions):
        for action in reversed(actions):
            if action.get("state") == "active":
                return action
        return actions[-1] if actions else None

    def _run_info(self, row, now):
        rd = row["run_dir"]
        start = self._read_json(os.path.join(rd, "start.json"), {}) or {}
        meta = self._read_json(os.path.join(rd, "meta.json"), {}) or {}
        agent = str(row.get("agent") or meta.get("agent") or start.get("agent") or "codex")
        snap = self._events(rd, agent)
        started = self._time(meta.get("started") or start.get("started") or row.get("started") or row.get("time"))
        status = meta.get("status") or start.get("status")
        if not status:
            status = "queued" if os.path.exists(os.path.join(rd, "queued")) else "running" if self._xay_alive(start.get("pid"), "xay-run") else "stale"
        ended = self._time(meta.get("ended") or start.get("ended"))
        duration = self._number(meta.get("duration_s"))
        if not ended and status in ("done", "failed", "timeout", "stopped", "stale") and duration:
            ended = started + duration
        if not ended and status in ("done", "failed", "timeout", "stopped", "stale"):
            for filename in ("meta.json", "result.md"):
                try:
                    ended = int(os.path.getmtime(os.path.join(rd, filename)))
                    break
                except OSError:
                    pass
        effort = meta.get("effort", start.get("effort", row.get("effort", "medium")))
        actions = [humanize(a) for a in snap.get("actions", [])]
        changed = meta.get("changed_files") or start.get("changed_files") or row.get("changed_files") or []
        if isinstance(changed, str):
            changed = [x for x in changed.splitlines() if x]
        session = snap.get("session") or meta.get("session_id") or start.get("session_id") or row.get("session") or ""
        resume = meta.get("resume") or start.get("resume") or row.get("resume") or ""
        result = self._read_text(os.path.join(rd, "result.md"), None)
        return {
            "id": rd, "agent": agent, "title": self._title(rd, row, meta, start) or str(row.get("name") or meta.get("name") or start.get("name") or ""),
            "status": status, "effort": effort, "model": self._model(agent, meta.get("model", start.get("model", row.get("model", ""))), effort),
            "started": started, "ended": ended or None, "elapsed_s": max(0, (ended or now) - started) if started else 0,
            "step": self._number(snap.get("step"), self._number(meta.get("step"))), "tokens": snap.get("tokens") if snap.get("tokens") is not None else self._number(meta.get("tokens")),
            "now": self._now_action(actions), "changed_files": list(changed),
            "isolated": bool(meta.get("isolated", start.get("isolated", row.get("isolated", False)))),
            "land": meta.get("land", start.get("land", row.get("land", ""))) or "", "notice": str(meta.get("notice") or start.get("notice") or row.get("notice") or ""),
            "session": str(session or ""), "resume": str(resume or ""), "workdir": _base(meta.get("origin") or start.get("origin") or row.get("origin") or meta.get("workdir") or start.get("workdir") or row.get("workdir") or ""),  # 给人看原项目名，不看 worktree 名
            "origin": meta.get("origin", start.get("origin", row.get("origin", ""))) or "",
            "dag": meta.get("dag") or start.get("dag") or row.get("dag") or row.get("dag_dir") or "",
            "task_id": meta.get("task") or start.get("task") or row.get("task") or "", "actions": actions, "task": self._task_text(rd), "result": result,
            "start_data": start, "meta": meta, "mission": "",
        }

    def _dag_info(self, row, runs):
        dd = row["dag_dir"]
        state = self._read_json(os.path.join(dd, "state.json"), {}) or {}
        dag_runs = [r for r in runs if self._same_path(r.get("dag"), dd)]
        task_map = {}
        for task in state.get("tasks") or []:
            tid = str(task.get("id", ""))
            task_map[tid] = [r for r in dag_runs if str(r.get("task_id", "")) == tid]
            if not task_map[tid] and task.get("run_dir"):
                target = os.path.abspath(str(task["run_dir"]))
                task_map[tid] = [r for r in dag_runs if r["id"] == target]
        return state, dag_runs, task_map

    def _mission_groups(self, runs, dags):
        by_session = {r["session"]: r for r in runs if r["session"]}
        groups = {}
        for r in runs:
            dag = next((d["dag_dir"] for d in dags if self._same_path(r.get("dag"), d["dag_dir"])), None)
            if dag:
                key = dag
            else:
                key, seen, resume = r["id"], set(), r.get("resume")
                while resume and resume not in seen:
                    seen.add(resume)
                    previous = by_session.get(resume)
                    if previous is None:
                        break
                    previous_dag = next((d["dag_dir"] for d in dags if self._same_path(previous.get("dag"), d["dag_dir"])), None)
                    key = previous_dag or previous["id"]
                    if previous_dag:
                        break
                    resume = previous.get("resume")
            groups.setdefault(key, []).append(r)
            r["mission"] = key
        return groups

    def _run_public(self, r):
        fields = ("id", "agent", "title", "status", "mission", "effort", "model", "started", "ended", "elapsed_s", "step", "tokens", "now", "changed_files", "isolated", "land", "notice", "session", "workdir")
        return {key: r[key] for key in fields}

    @staticmethod
    def _layers(tasks):
        left = {str(t.get("id")): set(t.get("deps") or []) for t in tasks if t.get("id") is not None}
        result = []
        while left:
            ready = [key for key, deps in left.items() if not deps or not (deps & set(left))]
            if not ready:
                break
            result.append(ready)
            for key in ready:
                left.pop(key, None)
        return result

    def _build_mission(self, key, group, dag_by_id, now):
        dag = dag_by_id.get(key)
        if dag:
            state, dag_runs, task_map = self._dag_info(dag, group)
            state_tasks = state.get("tasks") or []
            layers = state.get("layers") or self._layers(state_tasks)
            layer_map = {str(tid): index for index, layer in enumerate(layers) for tid in layer}
            subtasks = []
            for task in state_tasks:
                tid = str(task.get("id", ""))
                rounds = sorted(task_map.get(tid, []), key=lambda r: (r.get("started", 0), r["id"]))
                latest = rounds[-1] if rounds else None
                status = str(task.get("state") or "pending")
                if latest and status in ("running", "queued") and latest["status"] == "queued":
                    status = "queued"
                started = self._time(task.get("started")) or (latest or {}).get("started", 0)
                ended = self._time(task.get("ended")) or (latest or {}).get("ended", 0)
                subtasks.append({
                    "id": tid, "agent": task.get("agent") or (latest or {}).get("agent", "codex"),
                    "title": task.get("title") or task.get("name") or (latest or {}).get("title", ""), "status": status,
                    "batch": self._number(task.get("batch"), layer_map.get(tid, 0)), "deps": list(task.get("deps") or []),
                    "elapsed_s": max(0, (ended or now) - started) if started else 0, "step": (latest or {}).get("step", 0),
                    "now": (latest or {}).get("now"), "run": latest["id"] if latest else (task.get("run_dir") or None),
                    "rounds": len(rounds), "notice": next((r.get("notice", "") for r in rounds if r.get("notice")), ""),
                })
            known = {s["id"] for s in subtasks}
            for r in sorted(dag_runs, key=lambda x: (x.get("started", 0), x["id"])):
                tid = r.get("task_id") or r["id"]
                if tid not in known:
                    subtasks.append({"id": tid, "agent": r["agent"], "title": r["title"], "status": r["status"], "batch": 0, "deps": [], "elapsed_s": r["elapsed_s"], "step": r["step"], "now": r["now"], "run": r["id"], "rounds": 1, "notice": r["notice"]})
            title = state.get("title") or dag.get("title") or next((s["title"] for s in subtasks if s["title"]), "")
            started = self._time(state.get("started")) or min((r["started"] for r in group if r["started"]), default=0)
            ended = self._time(state.get("ended")) or None
            statuses = [s["status"] for s in subtasks]
            pending_running = any(s in ("pending", "running", "queued") for s in statuses)
            notice = next((s["notice"] for s in subtasks if s["notice"]), "")
            pid = state.get("pid")
            if pid and not self._xay_alive(pid, "xay-dag") and any(s in ("pending", "running") for s in statuses):
                mission_status, notice = "failed", "任务进程意外退出"
            elif any(s == "running" for s in statuses):
                mission_status = "running"
            elif any(s == "queued" for s in statuses):
                mission_status = "queued"
            elif any(s == "stopped" for s in statuses):
                mission_status = "stopped"
            elif any(s in ("failed", "timeout", "stale") for s in statuses):
                mission_status = "failed"
            else:
                mission_status = "done"
            if not ended and not pending_running and subtasks:
                ended = max((self._time(t.get("ended")) for t in state_tasks), default=0) or max((r.get("ended", 0) for r in group), default=0) or None
        else:
            rounds = sorted(group, key=lambda r: (r.get("started", 0), r["id"]))
            root, latest = rounds[0], rounds[-1]
            title, started = root["title"], root["started"]
            ended = max((r.get("ended", 0) for r in rounds), default=0) or None
            subtasks = [{"id": root["id"], "agent": root["agent"], "title": title, "status": latest["status"], "batch": 0, "deps": [], "elapsed_s": latest["elapsed_s"], "step": latest["step"], "now": latest["now"], "run": latest["id"], "rounds": len(rounds), "notice": next((r["notice"] for r in rounds if r["notice"]), "")}]
            # 返工时以最新一轮代表这个任务当前的状态。
            status = latest["status"]
            if status == "running":
                mission_status = "running"
            elif status == "queued":
                mission_status = "queued"
            elif status == "stopped":
                mission_status = "stopped"
            elif status in ("failed", "timeout", "stale"):
                mission_status = "failed"
            else:
                mission_status = "done"
            notice = subtasks[0]["notice"]
        workdir = _base((state.get("workdir") or state.get("base")) if dag else "") if dag else ""
        if not workdir:
            workdir = next((r["workdir"] for r in group if r["workdir"]), "")
        elapsed = max(0, (ended or now) - started) if started else 0
        return {"id": key, "kind": "dag" if dag else "single", "title": title, "status": mission_status, "started": started, "ended": ended, "elapsed_s": elapsed, "workdir": workdir, "total": len(subtasks), "done": sum(s["status"] == "done" for s in subtasks), "failed": sum(s["status"] in ("failed", "timeout") for s in subtasks), "running": sum(s["status"] == "running" for s in subtasks), "notice": notice, "subtasks": subtasks, "layers": layers if dag else [[subtasks[0]["id"]]]}

    def _all(self):
        rows = self._registry()
        now = int(__import__("time").time())
        runs = [self._run_info(r, now) for r in rows if r.get("kind") == "run"]
        dags = [r for r in rows if r.get("kind") == "dag"]
        groups = self._mission_groups(runs, dags)
        for dag in dags:
            groups.setdefault(dag["dag_dir"], [])
        dag_by_id = {d["dag_dir"]: d for d in dags}
        missions = [self._build_mission(key, group, dag_by_id, now) for key, group in groups.items()]
        runs.sort(key=lambda r: (r.get("started", 0), r["id"]), reverse=True)
        missions.sort(key=lambda m: (m["status"] not in ("running", "queued"), -(m.get("started") or 0)))
        return now, runs, missions[:40]

    def _agents(self, runs, missions, now):
        output = {}
        local = __import__("datetime").datetime.now().astimezone()
        today = int(local.replace(hour=0, minute=0, second=0, microsecond=0).timestamp())
        week = today - 6 * 86400
        for agent in self.AGENTS:
            selected = [r for r in runs if r["agent"] == agent]
            current = []
            for r in selected:
                if r["status"] not in ("running", "queued"):
                    continue
                mission = next((m for m in missions if m["id"] == r["mission"]), None)
                current.append({"mission": r["mission"], "mission_title": mission["title"] if mission else r["title"], "run": r["id"], "title": r["title"], "status": r["status"], "now": r["now"], "elapsed_s": r["elapsed_s"]})
            def period(start):
                rows = [r for r in selected if r.get("started", 0) >= start]
                return {"runs": len(rows), "tokens": sum(self._number(r.get("tokens")) for r in rows)}
            finished = [r for r in selected if r["status"] in ("done", "failed", "timeout", "stopped", "stale") and r.get("started", 0) >= now - 30 * 86400]
            good = sum(r["status"] == "done" for r in finished)
            bad = sum(r["status"] in ("failed", "timeout") for r in finished)
            done = [r for r in finished if r["status"] == "done"]
            slots = 0
            try:
                for name in os.listdir(os.path.join(self.home, "slots")):
                    if name.startswith(agent + "-") and self._xay_alive(self._read_text(os.path.join(self.home, "slots", name, "pid")).strip(), "xay-run"):
                        slots += 1
            except OSError:
                pass
            max_slots = self._number(os.environ.get("XAY_SLOTS_" + agent.upper()), self.DEFAULT_SLOTS[agent])
            recent = sorted(finished, key=lambda r: (r.get("ended") or r.get("started", 0), r["id"]), reverse=True)[:10]
            output[agent] = {"name": "Codex" if agent == "codex" else agent, "role": self.ROLES[agent], "model": self._model(agent, "", "medium"), "slots": {"used": slots, "max": max_slots, "queued": sum(r["status"] == "queued" for r in selected)}, "current": current, "today": period(today), "week": period(week), "stats": {"success_rate": good / (good + bad) if good + bad else None, "avg_s": sum(r["elapsed_s"] for r in done) / len(done) if done else None, "reworks": sum(bool(r.get("resume")) for r in selected if r.get("started", 0) >= now - 30 * 86400), "finished": len(finished)}, "recent": [self._run_public(r) for r in recent]}
        return output

    def state(self):
        now, runs, missions = self._all()
        return {"now": now, "missions": missions, "agents": self._agents(runs, missions, now), "runs": [self._run_public(r) for r in runs[:100]]}

    def detail(self, item_id):
        now, runs, missions = self._all()
        mission = next((m for m in missions if m["id"] == item_id), None)
        if mission:
            return {"mission": mission}
        target = next((r for r in runs if r["id"] == item_id), None)
        if not target:
            return None
        rounds = [r for r in runs if r["mission"] == target["mission"] and (not target.get("task_id") or r.get("task_id") == target.get("task_id"))]
        rounds.sort(key=lambda r: (r.get("started", 0), r["id"]))
        stderr = self._read_text(os.path.join(target["id"], "stderr.log"), "").splitlines()[-20:]
        return {"run": self._run_public(target), "mission_title": next((m["title"] for m in missions if m["id"] == target["mission"]), target["title"]), "task": target["task"], "actions": target["actions"][-300:], "rounds": [{"run": r["id"], "started": r["started"], "status": r["status"], "title": f"第 {i} 轮"} for i, r in enumerate(rounds, 1)], "result": target["result"], "changed_files": target["changed_files"], "notice": target["notice"], "tech": {"session": target["session"], "run_dir": target["id"], "workdir": target["meta"].get("workdir") or target["start_data"].get("workdir") or "", "origin": target["origin"], "land": target["land"], "stderr_tail": "\n".join(stderr)}}


def _limit(text, length=40):
    text = str(text or "")
    return text if len(text) <= length else text[:length - 1] + "…"


def _command_parts(command):
    parts = re.split(r"&&|\|\||[;|]", str(command or ""))
    cleaned = []
    for part in parts:
        part = part.strip()
        while True:
            newer = re.sub(r"^cd\s+(?:'[^']*'|\"[^\"]*\"|\S+)\s*", "", part)
            newer = re.sub(r"^(?:[A-Za-z_][A-Za-z0-9_]*=\S+\s+)+", "", newer)
            newer = re.sub(r"^sudo\s+", "", newer)
            if newer == part:
                break
            part = newer.strip()
        if part:
            cleaned.append(part)
    return cleaned


def _shell_words(command):
    try:
        return __import__("shlex").split(command)
    except ValueError:
        return str(command or "").split()


def _cmd_human(command):
    parts = _command_parts(command)
    if not parts:
        return "运行命令"
    tests = ("unittest", "pytest", "npm test", "npm run test", "pnpm test", "yarn test", "vitest", "jest", "go test", "cargo test")
    if any(any(pattern in part.lower() for pattern in tests) for part in parts):
        return "运行测试"
    first, low = parts[0], parts[0].lower()
    if any(pattern in low for pattern in ("npm run build", "pnpm build", "tsc", "make", "cargo build", "go build", "py_compile", "bash -n", "node --check")):
        return "检查代码能否运行"
    if any(pattern in low for pattern in ("npm install", "npm i ", "pnpm add", "pnpm install", "pip install", "pip3 install", "brew install", "uv add")):
        return "安装依赖"
    if any(pattern in low for pattern in ("git diff", "git status", "git log", "git show")):
        return "查看改动记录"
    words = _shell_words(first)
    command_name = words[0] if words else ""
    if command_name in ("cat", "sed", "head", "tail", "nl", "less", "wc"):
        candidate = next((word for word in reversed(words[1:]) if not word.startswith("-")), "")
        return "查看 " + _base(candidate) if candidate else "查看文件"
    if command_name in ("rg", "grep", "find", "ls", "fd", "tree"):
        return "查找文件或内容"
    if command_name in ("curl", "wget"):
        return "访问网址"
    if command_name in ("mkdir", "cp", "mv", "rm", "touch", "chmod", "ln"):
        return "整理文件"
    if command_name in ("python", "python3", "node", "bash", "sh", "zsh", "ruby"):
        script = next((word for word in words[1:] if not word.startswith("-") and (word.endswith((".py", ".js", ".sh", ".rb")) or "/" in word)), "")
        if script:
            return "运行脚本 " + _base(script)
    return "运行命令"


def humanize(action):
    action = action or {}
    kind, raw = action.get("kind", ""), str(action.get("text", "") or "")
    if kind == "cmd":
        text = _cmd_human(raw)
    elif kind == "edit":
        text = "修改 " + raw
    elif kind == "read":
        text = "查看 " + raw
    elif kind == "search":
        text = "上网搜索：" + raw
    elif kind == "think":
        text = "思考"
    elif kind == "say":
        text = "回复：" + raw.replace("\n", " ")[:30]
    elif kind == "plan":
        match = re.search(r"\d+", raw)
        text = f"制定计划（{match.group(0)} 项）" if match else "制定计划"
    elif kind == "tool":
        text = "使用工具 " + raw.split(":", 1)[0]
    else:
        text = raw
    return {"kind": kind, "text": _limit(text), "raw": raw, "state": action.get("state", "active")}


class Handler(BaseHTTPRequestHandler):
    data = None
    html_path = None

    def _send(self, body, status=200, content_type="application/json; charset=utf-8"):
        raw = body.encode("utf-8") if isinstance(body, str) else body
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(raw)))
        self.send_header("Cache-Control", "no-store")  # 升级页面后刷新即生效
        self.end_headers()
        self.wfile.write(raw)

    def _json(self, value, status=200):
        self._send(json.dumps(value, ensure_ascii=False), status)

    def do_GET(self):
        parsed = urlparse(self.path)
        if parsed.path == "/":
            try:
                self._send(open(self.html_path, encoding="utf-8").read(), content_type="text/html; charset=utf-8")
            except OSError:
                self._send("看板页面不存在", 404, "text/plain; charset=utf-8")
        elif parsed.path == "/api/state":
            self._json(self.data.state())
        elif parsed.path == "/api/run":
            item_id = parse_qs(parsed.query).get("id", [""])[0]
            detail = self.data.detail(item_id)
            self._json(detail if detail else {"error": "Not Found"}, 200 if detail else 404)
        else:
            self._send("Not Found", 404, "text/plain; charset=utf-8")

    def do_POST(self):
        if self.path != "/api/stop":
            self._send("Not Found", 404, "text/plain; charset=utf-8")
            return
        if self.headers.get("X-Xay-Action") != "stop":
            self._json({"ok": False, "error": "需要 X-Xay-Action: stop"}, 403)
            return
        try:
            length = int(self.headers.get("Content-Length", "0"))
            payload = json.loads(self.rfile.read(length) or b"{}")
            item_id = payload.get("id", "")
        except (ValueError, TypeError, json.JSONDecodeError):
            self._json({"ok": False, "error": "请求体不是有效 JSON"}, 400)
            return
        rows = self.data._registry()
        row = next((r for r in rows if (r.get("run_dir") if r.get("kind") == "run" else r.get("dag_dir")) == item_id), None)
        if not row:
            self._json({"ok": False, "error": "Not Found"}, 404)
            return
        if row.get("kind") == "run":
            start = self.data._read_json(os.path.join(item_id, "start.json"), {}) or {}
            pid, marker = start.get("pid"), "xay-run"
        else:
            state = self.data._read_json(os.path.join(item_id, "state.json"), {}) or {}
            pid, marker = state.get("pid"), "xay-dag"
        command = ""
        if self.data._alive(pid):
            try:
                command = subprocess.run(["ps", "-o", "command=", "-p", str(int(pid))], capture_output=True, text=True, check=False).stdout
            except (OSError, ValueError):
                pass
        if not self.data._alive(pid) or marker not in command:
            self._json({"ok": False, "error": "任务已经结束"}, 409)
            return
        os.kill(int(pid), signal.SIGTERM)
        self._json({"ok": True})

    def log_message(self, *_):
        pass


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=7878)
    ap.add_argument("--open", action="store_true")
    args = ap.parse_args()
    home = os.environ.get("XAY_HOME", os.path.expanduser("~/.xay"))
    Handler.data = WatchData(home)
    Handler.html_path = os.path.join(os.path.dirname(os.path.realpath(__file__)), "xay-watch.html")
    try:
        server = ThreadingHTTPServer(("127.0.0.1", args.port), Handler)
    except OSError as exc:
        if exc.errno in (48, 98):
            print(f"看板：http://127.0.0.1:{args.port}")
            return 0
        raise
    url = f"http://127.0.0.1:{args.port}"
    print(f"看板：{url}", flush=True)
    if args.open:
        subprocess.Popen(["open", url], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
