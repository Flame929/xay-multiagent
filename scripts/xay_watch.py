#!/usr/bin/env python3
import argparse
import hashlib
import re
import json
import os
import signal
import socket
import subprocess
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

_SCRIPTS_DIR = os.path.dirname(os.path.realpath(__file__))
if _SCRIPTS_DIR not in sys.path:
    sys.path.insert(0, _SCRIPTS_DIR)
from xay_ctl import CtlClient
from xay_quota import collect as collect_quota
import xay_org


def _base(value):
    return os.path.basename(value or "")


def _clean_paths(paths):
    """git 会给含空格/非 ASCII 的路径加引号并转义（如 "\" a b.txt\""），旧记录里存的就是这种；去掉引号并还原。"""
    out = []
    for p in paths or []:
        p = str(p)
        if len(p) >= 2 and p[0] == p[-1] == '"':
            try:
                p = p[1:-1].encode("latin-1", "backslashreplace").decode("unicode_escape").encode("latin-1").decode("utf-8")
            except (UnicodeError, ValueError):
                p = p[1:-1]
        if p and p not in out:
            out.append(p)
    return out


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
        self._registry_offset = 0
        self._registry_inode = None
        self._registry_rows = []
        self._event_history = []
        self._event_seq = 0
        self._run_signatures = {}
        self._norm_offsets = {}
        self._session_seen = {}
        self._session_overrides = {}
        self._session_flags = {}
        self._decision_overrides = {}
        self.v5 = ConversationModel(self)

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
        path = os.path.join(self.home, "registry.jsonl")
        try:
            stat = os.stat(path)
        except OSError:
            return list(self._registry_rows)
        if self._registry_inode != (stat.st_dev, stat.st_ino) or stat.st_size < self._registry_offset:
            self._registry_offset = 0
            self._registry_inode = (stat.st_dev, stat.st_ino)
            self._registry_rows = []
        try:
            with open(path, encoding="utf-8", errors="replace") as fh:
                fh.seek(self._registry_offset)
                chunk = fh.read()
                self._registry_offset = fh.tell()
        except OSError:
            return list(self._registry_rows)
        # 运行中的 xay 可能正在写最后一行；只消费完整行，下一次继续读半行。
        if chunk and not chunk.endswith("\n"):
            complete, _, tail = chunk.rpartition("\n")
            self._registry_offset -= len(tail.encode("utf-8"))
            chunk = complete + ("\n" if complete else "")
        cutoff = time.time() - 30 * 86400
        for line in chunk.splitlines():
            try:
                row = json.loads(line)
            except (ValueError, TypeError):
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
            self._registry_rows.append(row)
        # 旧版测试和现有页面依赖原始登记顺序；同一实体重复登记时保留最后一行。
        latest = {}
        for row in self._registry_rows:
            key = (row.get("kind"), row.get("run_dir") or row.get("dag_dir"))
            latest[key] = {**latest.get(key, {}), **row}
        return list(latest.values())

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
        except (TypeError, ValueError, OSError, ProcessLookupError):  # 运行目录被删时 pid 为 None
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
        changed = _clean_paths(meta.get("changed_files") or start.get("changed_files") or row.get("changed_files") or [])
        if isinstance(changed, str):
            changed = [x for x in changed.splitlines() if x]
        session = row.get("session") or snap.get("session") or meta.get("session_id") or start.get("session_id") or ""
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
            "start_data": {**start, "ws": row.get("ws", start.get("ws", "")), "phase": row.get("phase", start.get("phase", "")), "xs": row.get("xs", start.get("xs", "")), "key": row.get("key", start.get("key", "")), "ctl_sock": row.get("ctl_sock", start.get("ctl_sock", ""))}, "meta": meta, "mission": "",
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
                rounds = sorted(task_map.get(tid, []), key=lambda r: ((r.get("started") or 0), r["id"]))
                latest = rounds[-1] if rounds else None
                status = str(task.get("state") or "pending")
                if latest and status in ("running", "queued") and latest["status"] == "queued":
                    status = "queued"
                started = self._time(task.get("started")) or ((latest or {}).get("started") or 0)
                ended = self._time(task.get("ended")) or ((latest or {}).get("ended") or 0)
                subtasks.append({
                    "id": tid, "agent": task.get("agent") or (latest or {}).get("agent", "codex"),
                    "title": task.get("title") or task.get("name") or (latest or {}).get("title", ""), "status": status,
                    "batch": self._number(task.get("batch"), layer_map.get(tid, 0)), "deps": list(task.get("deps") or []),
                    "elapsed_s": max(0, (ended or now) - started) if started else 0, "step": (latest or {}).get("step", 0),
                    "now": (latest or {}).get("now"), "run": latest["id"] if latest else (task.get("run_dir") or None),
                    "rounds": len(rounds), "notice": next((r.get("notice", "") for r in rounds if r.get("notice")), ""),
                })
            known = {s["id"] for s in subtasks}
            for r in sorted(dag_runs, key=lambda x: ((x.get("started") or 0), x["id"])):
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
                ended = max((self._time(t.get("ended")) for t in state_tasks), default=0) or max(((r.get("ended") or 0) for r in group), default=0) or None
        else:
            rounds = sorted(group, key=lambda r: ((r.get("started") or 0), r["id"]))
            root, latest = rounds[0], rounds[-1]
            title, started = root["title"], root["started"]
            ended = max(((r.get("ended") or 0) for r in rounds), default=0) or None
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
        runs.sort(key=lambda r: ((r.get("started") or 0), r["id"]), reverse=True)
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
                rows = [r for r in selected if (r.get("started") or 0) >= start]
                return {"runs": len(rows), "tokens": sum(self._number(r.get("tokens")) for r in rows)}
            finished = [r for r in selected if r["status"] in ("done", "failed", "timeout", "stopped", "stale") and (r.get("started") or 0) >= now - 30 * 86400]
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
            recent = sorted(finished, key=lambda r: (r.get("ended") or (r.get("started") or 0), r["id"]), reverse=True)[:10]
            output[agent] = {"name": "Codex" if agent == "codex" else agent, "role": self.ROLES[agent], "model": self._model(agent, "", "medium"), "slots": {"used": slots, "max": max_slots, "queued": sum(r["status"] == "queued" for r in selected)}, "current": current, "today": period(today), "week": period(week), "stats": {"success_rate": good / (good + bad) if good + bad else None, "avg_s": sum(r["elapsed_s"] for r in done) / len(done) if done else None, "reworks": sum(bool(r.get("resume")) for r in selected if (r.get("started") or 0) >= now - 30 * 86400), "finished": len(finished)}, "recent": [self._run_public(r) for r in recent]}
        return output

    @staticmethod
    def _v4_status(status):
        return {
            "queued": "waiting_slot", "pending": "waiting_deps", "stale": "orphaned",
            "timeout": "failed", "stopped": "canceled", "running": "running",
            "done": "done", "failed": "failed",
        }.get(str(status or ""), str(status or "idle"))

    def _workspace_info(self, root, row=None):
        root = os.path.abspath(os.path.expanduser(str(root or ""))) if root else ""
        config = self._read_json(os.path.join(root, ".xay", "workspace.json"), {}) or {}
        ws_id = (row or {}).get("ws") or ("ws_" + hashlib.sha1(root.encode("utf-8")).hexdigest()[:8])
        return {
            "id": ws_id, "root": root, "name": _base(root),
            "safety_tier": config.get("safety_tier", "standard"),
            "confidential": bool(config.get("confidential", False)),
            "deny_globs": list(config.get("deny_globs") or []),
        }

    def _session_info(self, session_id, rows, root):
        item = {}
        sessions_dir = os.path.join(self.home, "sessions")
        for name in (str(session_id), str(session_id) + ".json"):
            candidate = os.path.join(sessions_dir, name)
            if not candidate.endswith(".json"):
                candidate += ".json"
            item = self._read_json(candidate, {}) or {}
            if item:
                break
        row = next((r for r in rows if r.get("session") == session_id), {})
        title = self._session_overrides.get(session_id, item.get("title") or row.get("session_title"))
        title_source = "lead" if title else "fallback"
        title = title or ("未命名会话 · " + _base(root))
        started = self._time(item.get("started")) or min((r.get("started", 0) for r in rows), default=0)
        ended = self._time(item.get("ended")) or None
        return {
            "id": str(session_id), "ws": row.get("ws") or "", "lead_kind": item.get("lead_kind") or row.get("lead_kind", "unknown"),
            "lead_key": item.get("lead_key") or row.get("lead_key", ""), "lead_host_id": item.get("lead_host_id") or row.get("lead_host_id"),
            "title": title, "title_source": title_source, "started": started, "ended": ended,
            "pinned": bool(self._session_flags.get(session_id, {}).get("pinned", item.get("pinned", False))), "archived": bool(self._session_flags.get(session_id, {}).get("archived", item.get("archived", False))),
        }

    def _decision_rows(self, runs):
        decisions = []
        seen = set()
        for run in runs:
            rd = run["id"]
            paths = [os.path.join(rd, "decisions.jsonl"), os.path.join(rd, "events.norm.jsonl")]
            for path in paths:
                try:
                    with open(path, encoding="utf-8", errors="replace") as fh:
                        lines = fh.read().splitlines()
                except OSError:
                    continue
                for line in lines:
                    try:
                        obj = json.loads(line)
                    except (ValueError, TypeError):
                        continue
                    if not isinstance(obj, dict):
                        continue
                    if path.endswith("events.norm.jsonl"):
                        if obj.get("type") not in ("decision.created", "decision.upsert"):
                            continue
                        obj = dict(obj.get("data") or obj)
                    kind = obj.get("kind")
                    if kind not in ("conflict", "failed_final", "orphaned", "question", "approval"):
                        continue
                    did = str(obj.get("id") or ("dec_" + hashlib.sha1((rd + kind).encode()).hexdigest()[:12]))
                    if did in seen:
                        continue
                    seen.add(did)
                    item = {
                        "id": did, "kind": kind, "owner": obj.get("owner", "lead"), "version": self._number(obj.get("version", 1), 1),
                        "ws": obj.get("ws") or run.get("start_data", {}).get("ws", ""), "session": obj.get("session") or run.get("session", ""),
                        "phase": obj.get("phase") or run.get("dag", ""), "task": obj.get("task") or run.get("task_id", ""),
                        "execution": obj.get("execution") or run.get("id", ""), "title": obj.get("title") or "需要处理",
                        "detail": obj.get("detail", ""), "options": list(obj.get("options") or []),
                        "payload": obj.get("payload") or {}, "created": self._time(obj.get("created")) or int(os.path.getmtime(path)),
                        "resolved": obj.get("resolved"),
                    }
                    decisions.append(item)
        for run in runs:
            if run.get("land") == "conflict" and not _conflict_settled(run):
                did = "dec_" + hashlib.sha1((run["id"] + "conflict").encode()).hexdigest()[:12]
                if did not in seen:
                    decisions.append({"id": did, "kind": "conflict", "owner": "lead", "version": 1, "ws": "", "session": run.get("session", ""), "phase": run.get("dag", ""), "task": run.get("task_id", ""), "execution": run["id"], "title": "改动发生冲突，需要有人合并", "detail": run.get("notice", ""), "options": [{"id": "retry_land", "label": "重试搬回", "danger": False}, {"id": "discard", "label": "丢弃这份改动", "danger": True}, {"id": "handoff_lead", "label": "交给负责人处理", "danger": False}], "payload": {"worktree": run.get("origin", "")}, "created": run.get("ended") or run.get("started", 0), "resolved": None})
            # 失败本身不再自动变成"等你决定"：失败原因直接显示在线程上；需要人拍板时由 watchdog / 负责人写 decisions.jsonl
            if run.get("status") in ("stale", "orphaned"):
                did = "dec_" + hashlib.sha1((run["id"] + "orphaned").encode()).hexdigest()[:12]
                if did not in seen:
                    decisions.append({"id": did, "kind": "orphaned", "owner": "lead", "version": 1, "ws": run.get("start_data", {}).get("ws", ""), "session": run.get("session", ""), "phase": run.get("dag", ""), "task": run.get("task_id", ""), "execution": run["id"], "title": "执行者失联了，需要决定是否恢复", "detail": run.get("notice", ""), "options": [{"id": "resume", "label": "继续", "danger": False}, {"id": "discard", "label": "丢弃", "danger": True}], "payload": {}, "created": run.get("ended") or run.get("started", 0), "resolved": None})
        return [self._decision_overrides.get(d["id"], d) for d in decisions if not d.get("resolved")]

    def _touch_events(self, runs):
        for run in runs:
            rd = run["id"]
            norm_path = os.path.join(rd, "events.norm.jsonl")
            try:
                norm_size = os.path.getsize(norm_path)
                norm_offset = self._norm_offsets.get(norm_path, 0)
                if norm_size < norm_offset:
                    norm_offset = 0
                with open(norm_path, encoding="utf-8", errors="replace") as fh:
                    fh.seek(norm_offset)
                    norm_text = fh.read()
                    self._norm_offsets[norm_path] = fh.tell()
                for line in norm_text.splitlines():
                    try:
                        event = json.loads(line)
                    except (ValueError, TypeError):
                        continue
                    typ = event.get("type", "")
                    data = event.get("data") or {}
                    if typ in ("tool.call", "tool.result", "reasoning", "message"):
                        action = humanize({"kind": data.get("kind") or event.get("kind", "tool"), "text": data.get("text") or event.get("text", ""), "state": data.get("state", "done")})
                        action.update({"id": data.get("id") or event.get("id"), "execution": rd, "turn": event.get("turn"), "by": event.get("by")})
                        self._event_seq += 1
                        self._event_history.append({"seq": self._event_seq, "type": "action.append", "data": action, "ts": event.get("ts", time.time())})
                    elif typ.startswith("decision."):
                        self._event_seq += 1
                        self._event_history.append({"seq": self._event_seq, "type": "decision.upsert", "data": data or event, "ts": event.get("ts", time.time())})
                    else:
                        self._event_seq += 1
                        self._event_history.append({"seq": self._event_seq, "type": "execution.upsert", "data": {"execution": rd, **data}, "ts": event.get("ts", time.time())})
            except OSError:
                pass
            signature = (run.get("status"), run.get("step"), run.get("ended"), run.get("now", {}).get("raw") if isinstance(run.get("now"), dict) else "")
            for name in ("events.norm.jsonl", "events.raw", "meta.json", "result.md"):
                try:
                    stat = os.stat(os.path.join(rd, name))
                    signature += (stat.st_mtime_ns, stat.st_size)
                except OSError:
                    pass
            if self._run_signatures.get(rd) == signature:
                continue
            self._run_signatures[rd] = signature
            self._event_seq += 1
            self._event_history.append({"seq": self._event_seq, "type": "execution.upsert", "data": {"id": rd, "execution": rd, "status": self._v4_status(run.get("status")), "task": run.get("task_id", ""), "agent": run.get("agent", "")}, "ts": time.time()})
            for event_type, data in (("task.upsert", {"id": run.get("task_id", ""), "execution": rd, "status": self._v4_status(run.get("status"))}), ("session.upsert", {"id": run.get("session", ""), "execution": rd, "last_activity": run.get("ended") or run.get("started", 0)})):
                self._event_seq += 1
                self._event_history.append({"seq": self._event_seq, "type": event_type, "data": data, "ts": time.time()})
        if len(self._event_history) > 5000:
            self._event_history = self._event_history[-5000:]

    def _v4_state(self, now, runs, missions):
        self._touch_events(runs)
        decisions = self._decision_rows(runs)
        workspaces = {}
        for run in runs:
            root = run.get("origin") or run.get("start_data", {}).get("workdir") or run.get("workdir")
            key = run.get("start_data", {}).get("ws") or "ws_" + hashlib.sha1(str(root).encode()).hexdigest()[:8]
            workspaces.setdefault(key, {**self._workspace_info(root, run.get("start_data")), "sessions": []})
        for ws in workspaces.values():
            ws_runs = [r for r in runs if (r.get("start_data", {}).get("ws") or "ws_" + hashlib.sha1(str(r.get("origin") or r.get("workdir")).encode()).hexdigest()[:8]) == ws["id"]]
            session_groups = {}
            for r in ws_runs:
                sid = r.get("session") or ("ls_" + hashlib.sha1(r["id"].encode()).hexdigest()[:12])
                session_groups.setdefault(sid, []).append(r)
            for sid, group in session_groups.items():
                session = self._session_info(sid, group, ws["root"])
                session["ws"] = ws["id"]
                session["status"] = "needs_you" if any(d.get("session") == sid for d in decisions) else ("running" if any(r.get("status") == "running" for r in group) else "done" if all(r.get("status") in ("done", "stopped", "failed", "timeout", "stale") for r in group) else "waiting")
                session["last_activity"] = max((r.get("ended") or r.get("started", 0) for r in group), default=0)
                session["unread_done"] = bool(session["status"] == "done" and not self._session_seen.get(sid))
                if self._session_seen.get(sid):
                    session["unread_done"] = False
                session["summary"] = ("等你决定" if session["status"] == "needs_you" else f"{sum(r.get('status') == 'running' for r in group)} 个 AI 在做" if session["status"] == "running" else "已完成")
                phases = {}
                for r in group:
                    pid = r.get("start_data", {}).get("phase") or r.get("dag") or ("ph_" + hashlib.sha1(r["id"].encode()).hexdigest()[:12])
                    phases.setdefault(pid, []).append(r)
                session["phases"] = [self._v4_phase(pid, group2, decisions) for pid, group2 in phases.items()]
                ws["sessions"].append(session)
            ws["status"] = "needs_you" if any(s["status"] == "needs_you" for s in ws["sessions"]) else "running" if any(s["status"] == "running" for s in ws["sessions"]) else "done"
            ws["counts"] = {"sessions": len(ws["sessions"]), "needs_you": sum(s["status"] == "needs_you" for s in ws["sessions"]), "running": sum(s["status"] == "running" for s in ws["sessions"])}
        agents = self._agents(runs, missions, now)
        for agent in agents:
            agents[agent]["memory_mb"] = 0
            if agent == "agy":
                agents[agent]["daily_cap"] = {"used": agents[agent]["today"]["runs"], "max": 40}
        disk_bytes = 0
        for root, _dirs, files in os.walk(self.home):
            for name in files:
                try:
                    disk_bytes += os.path.getsize(os.path.join(root, name))
                except OSError:
                    pass
        disk_mb = int(disk_bytes / (1024 * 1024))
        summary = {"needs_you": sum(s["status"] == "needs_you" for w in workspaces.values() for s in w["sessions"]), "running": sum(s["status"] == "running" for w in workspaces.values() for s in w["sessions"]), "waiting": sum(s["status"] == "waiting" for w in workspaces.values() for s in w["sessions"]), "done_unread": sum(bool(s.get("unread_done")) for w in workspaces.values() for s in w["sessions"])}
        return {"seq": self._event_seq, "now": now, "summary": summary, "thresholds": {"silence_s": {"codex": 180, "dsh": 120, "agy": 240}, "wait_default_s": 240}, "workspaces": list(workspaces.values()), "decisions": decisions, "agents": agents, "host": {"processes": 0, "memory_mb": 0, "xay_disk_mb": disk_mb}}

    def _v4_phase(self, phase_id, runs, decisions):
        tasks = []
        for run in runs:
            execution_id = run.get("start_data", {}).get("xs") or run.get("meta", {}).get("xs") or "xs_" + hashlib.sha1(run["id"].encode()).hexdigest()[:12]
            status = self._v4_status(run.get("status"))
            task_id = run.get("task_id") or run["id"]
            if any(d.get("execution") == run["id"] or d.get("task") == task_id for d in decisions):
                status = "needs_you"
            execution = {"id": execution_id, "task": task_id, "agent": run.get("agent"), "driver": run.get("meta", {}).get("driver", ""), "native_session_id": run.get("session"), "state": status, "flags": [], "started": run.get("started", 0), "ended": run.get("ended"), "last_event_ts": run.get("ended") or run.get("started", 0), "now": run.get("now"), "capabilities": run.get("meta", {}).get("capabilities", {})}
            card = run.get("start_data", {}).get("card") or run.get("meta", {}).get("card") or {"prompt": run.get("task", ""), "effort": run.get("effort", "medium"), "model": run.get("model", ""), "mode": "write", "writes": [], "reads": [], "verify": [], "done_when": "", "contract": None, "budget": {"minutes": None, "tokens": None}, "net": False, "isolate": "auto", "source": "sub"}
            execution.update({"safety_tier": run.get("meta", {}).get("safety_tier", "standard"), "sandbox": run.get("meta", {}).get("sandbox", ""), "net": bool(run.get("meta", {}).get("net", False)), "source": run.get("meta", {}).get("source", "sub"), "model": run.get("model", ""), "effort": run.get("effort", "medium"), "isolated": run.get("isolated", False), "turns": [], "usage": {"in": 0, "out": 0, "cached": 0, "granularity": "turn"}})
            tasks.append({"id": task_id, "key": run.get("start_data", {}).get("key", task_id), "phase": phase_id, "batch": 0, "deps": [], "agent": run.get("agent"), "title": run.get("title"), "card": card, "status": status, "fail_reason": run.get("meta", {}).get("fail_reason", ""), "executions": [execution_id], "land": run.get("land", ""), "notice": run.get("notice", ""), "changed_files": run.get("changed_files", []), "execution": execution})
        counts = {name: sum(t["status"] == name for t in tasks) for name in ("total", "done", "running", "waiting_deps", "waiting_slot", "needs_you")}
        counts["total"] = len(tasks)
        return {"id": phase_id, "session": runs[0].get("session", "") if runs else "", "seq": 1, "kind": "single", "title": runs[0].get("title", "第 1 步") if runs else "第 1 步", "status": "needs_you" if any(d.get("phase") == phase_id for d in decisions) else ("running" if any(t["status"] == "running" for t in tasks) else "done"), "started": min((r.get("started", 0) for r in runs), default=0), "ended": max((r.get("ended", 0) for r in runs), default=0) or None, "layers": [[t["id"] for t in tasks]], "counts": counts, "tasks": tasks}

    def state(self):
        now, runs, missions = self._all()
        result = {"now": now, "missions": missions, "agents": self._agents(runs, missions, now), "runs": [self._run_public(r) for r in runs[:100]]}
        result.update(self._v4_state(now, runs, missions))
        # v5（ui-v5-api §3）：workspaces 改为 工作区 → 负责人对话；v4 字段并在同一工作区上，旧调用方不受影响。
        v5 = self.v5.state()
        result["workspaces"] = self._merge_workspaces(v5["workspaces"], result.get("workspaces") or [])
        result["seq"] = v5["seq"]
        # 决定以 v5 为准：已带所属对话、是否给用户（for_user），并去掉了被返工取代/已解决的冲突
        result["decisions"] = v5["decisions"]
        result["quota"] = collect_quota(self.home)
        return result

    @staticmethod
    def _merge_workspaces(v5_workspaces, v4_workspaces):
        merged = [dict(ws) for ws in v5_workspaces]
        by_path = {ws["path"]: ws for ws in merged}
        for old in v4_workspaces:
            root = old.get("root") or ""
            target = (by_path.get(root) or by_path.get(_git_root(root))
                      or by_path.get(_workspace_for(root)["path"])) if root else None
            if target is None:
                continue
            for key, value in old.items():
                if key == "sessions":
                    target.setdefault("sessions", []).extend(value)
                elif key not in ("name", "path", "conversations"):
                    target.setdefault(key, value)
        for ws in merged:
            ws.setdefault("id", "ws_" + hashlib.sha1(ws["path"].encode("utf-8")).hexdigest()[:8])
            ws.setdefault("root", ws["path"])
            ws.setdefault("sessions", [])
        return merged

    def events(self, since=0):
        self.state()
        since = int(since or 0)
        if self._event_history and since and since < self._event_history[0]["seq"] - 1:
            raise ValueError("事件续传位置过旧")
        return [event for event in self._event_history if event["seq"] > since]

    def detail(self, item_id):
        now, runs, missions = self._all()
        mission = next((m for m in missions if m["id"] == item_id), None)
        if mission:
            return {"mission": mission}
        target = next((r for r in runs if r["id"] == item_id), None)
        if not target:
            return None
        rounds = [r for r in runs if r["mission"] == target["mission"] and (not target.get("task_id") or r.get("task_id") == target.get("task_id"))]
        rounds.sort(key=lambda r: ((r.get("started") or 0), r["id"]))
        stderr = self._read_text(os.path.join(target["id"], "stderr.log"), "").splitlines()[-20:]
        return {"run": self._run_public(target), "mission_title": next((m["title"] for m in missions if m["id"] == target["mission"]), target["title"]), "task": target["task"], "actions": target["actions"][-300:], "rounds": [{"run": r["id"], "started": r["started"], "status": r["status"], "title": f"第 {i} 轮"} for i, r in enumerate(rounds, 1)], "result": target["result"], "changed_files": target["changed_files"], "notice": target["notice"], "tech": {"session": target["session"], "run_dir": target["id"], "workdir": target["meta"].get("workdir") or target["start_data"].get("workdir") or "", "origin": target["origin"], "land": target["land"], "stderr_tail": "\n".join(stderr)}}

    def execution_path(self, execution_id):
        if os.path.isdir(str(execution_id)):
            return str(execution_id)
        for run in self._all()[1]:
            xs = run.get("start_data", {}).get("xs") or run.get("meta", {}).get("xs")
            if xs == execution_id:
                return run["id"]
        return ""


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


# ---------------------------------------------------------------------------
# 看板 v5：工作区 → 负责人对话 → 按 AI 分区的消息（references/plans/ui-v5-api.md）
# ---------------------------------------------------------------------------

CLAUDE_FIELDS = ("title", "cwd", "originCwd", "createdAt", "lastActivityAt", "isArchived")
AGENT_ORDER = ("codex", "dsh", "agy")
ACTIVE_TASK = ("waiting_deps", "waiting_slot", "starting", "running", "pausing", "finishing", "queued")
ENDED_RAW = ("done", "failed", "timeout", "stopped", "stale", "canceled", "orphaned")
DEFAULT_CAPS = {
    "codex-app-server": {"steer": "live", "interrupt": "soft"},
    "codex-exec": {"steer": "none", "interrupt": "kill"},
}
V5_HISTORY = 5000


class ClaudeIndex:
    """Claude 桌面版对话元数据：只读 local_*.json 的白名单字段，按文件 mtime 缓存。"""

    def __init__(self, root=None):
        self.root = os.path.expanduser(root or os.environ.get(
            "XAY_CLAUDE_SESSIONS", "~/Library/Application Support/Claude/claude-code-sessions"))
        self._files = {}
        self._by_cli = {}
        self._by_host = {}
        self._checked = 0.0

    def refresh(self, force=False):
        if not force and time.time() - self._checked < 5:
            return
        self._checked = time.time()
        seen = set()
        changed = False
        for path in __import__("glob").glob(os.path.join(self.root, "*", "*", "local_*.json")):
            seen.add(path)
            try:
                mtime = os.stat(path).st_mtime_ns
            except OSError:
                continue
            old = self._files.get(path)
            if old and old[0] == mtime:
                continue
            try:
                with open(path, encoding="utf-8") as fh:
                    raw = json.load(fh)
            except (OSError, ValueError):
                continue
            if not isinstance(raw, dict):
                continue
            fields = {key: raw.get(key) for key in CLAUDE_FIELDS}  # 其他字段一律丢弃
            self._files[path] = (mtime, str(raw.get("cliSessionId") or ""), fields)
            changed = True
        for path in list(self._files):
            if path not in seen:
                self._files.pop(path)
                changed = True
        if changed or not self._by_cli:
            by_cli, by_host = {}, {}
            for path, (_mtime, cli, fields) in self._files.items():
                host = os.path.splitext(os.path.basename(path))[0]
                by_host[host] = (cli, fields)
                if cli:
                    old = by_cli.get(cli)
                    if old is None or (fields.get("lastActivityAt") or 0) >= (old.get("lastActivityAt") or 0):
                        by_cli[cli] = fields
            self._by_cli, self._by_host = by_cli, by_host

    def lookup(self, session_id, host=None):
        self.refresh()
        if session_id and session_id in self._by_cli:
            return self._by_cli[session_id]
        if host and host in self._by_host:
            return self._by_host[host][1]
        return None


def _git_root(path, cache={}):
    path = os.path.abspath(os.path.expanduser(str(path or ""))) if path else ""
    if not path:
        return ""
    if path in cache:
        return cache[path]
    current = path
    root = path
    while True:
        if os.path.exists(os.path.join(current, ".git")):
            root = current
            break
        parent = os.path.dirname(current)
        if parent == current:
            break
        current = parent
    cache[path] = root
    return root


# 工作区归类与 Claude 桌面版一致：桌面版"未选文件夹"的对话都在 scratch-workspaces 下，
# 合成一个"这台电脑"分组；临时目录、确定不了的与没有负责人的旧数据归"临时文件夹"。
THIS_COMPUTER = {"path": "this-computer", "name": "这台电脑"}
TEMP_WORKSPACE = {"path": "temp", "name": "临时文件夹"}
CLAUDE_SCRATCH = os.environ.get("XAY_CLAUDE_SCRATCH") or os.path.expanduser(
    "~/Library/Application Support/Claude/scratch-workspaces")
TEMP_ROOTS = tuple(x for x in (os.environ.get("XAY_TEMP_ROOTS") or
                               "/tmp:/private/tmp:/var/folders:/private/var/folders").split(":") if x)


def _under(path, root):
    root = os.path.abspath(os.path.expanduser(root)).rstrip(os.sep)
    return bool(root) and (path == root or path.startswith(root + os.sep))


def _workspace_for(path):
    """路径 → {path, name}：这台电脑 / 临时文件夹 / git 仓库根（或目录本身）。"""
    if not path:
        return dict(TEMP_WORKSPACE)
    full = os.path.abspath(os.path.expanduser(str(path)))
    if _under(full, CLAUDE_SCRATCH):
        return dict(THIS_COMPUTER)
    if any(_under(full, root) for root in TEMP_ROOTS):
        return dict(TEMP_WORKSPACE)
    root = _git_root(full)
    name = _base(root)
    if not name:
        return dict(TEMP_WORKSPACE)
    return {"path": root, "name": name}


AGENT_NAMES = {"codex": "Codex", "dsh": "dsh", "agy": "agy"}
_TITLE_LABEL = re.compile(r"^\s*(背景|任务|目标|说明|需求|要求|上下文|前提|备注)\s*[：:]\s*")
_TITLE_READ_FIRST = re.compile(r"^\s*(请|开始前)?\s*(先|再|然后)\s*(读|看|阅读|查看|打开)")
_WORD_CHAR = re.compile(r"[A-Za-z0-9_./\\\-]")
_MEANING = re.compile(r"[\w\u4e00-\u9fff]")


def _cut_title(text, length, slack=24):
    """按长度截标题，但不把英文单词/路径从中间切开：落在单词里时先把这个词写完（最多多 slack 个字），不行就退到词前。"""
    if len(text) <= length + slack:
        return text
    cut = length - 1
    if _WORD_CHAR.match(text[cut]) and _WORD_CHAR.match(text[cut - 1]):
        end = cut
        while end < len(text) and _WORD_CHAR.match(text[end]):
            end += 1
        if end <= length + slack:
            cut = end
        else:
            begin = cut
            while begin > 0 and _WORD_CHAR.match(text[begin - 1]):
                begin -= 1
            if begin >= length // 2:
                cut = begin
    return text[:cut].rstrip(" ，,、：:（(") + "…"


def _clean_title(task, agent, length=20):
    """线程标题兜底：跳过"先读 …。"一类前缀和"背景："等标签，取第一句有意义的话；太长时先取第一个分句，再按词截断。"""
    text = re.sub(r"[ \t]+", " ", str(task or ""))
    for sentence in re.split(r"(?<=[。！？；!?;])|\n+", text):
        sentence = sentence.strip().lstrip("#>-*").strip()
        while True:
            stripped = _TITLE_LABEL.sub("", sentence, count=1)
            if stripped == sentence:
                break
            sentence = stripped.strip()
        if not sentence or _TITLE_READ_FIRST.match(sentence):
            continue
        sentence = sentence.rstrip("。！？；!?;，,、 ").strip()
        if len(sentence) > length:
            head = re.split(r"[：:]", sentence, maxsplit=1)[0].strip()
            if len(head) >= 4:
                sentence = head
        if len(sentence) > length:
            head = re.split(r"[，,；;（(]", sentence, maxsplit=1)[0].strip()
            if len(_MEANING.findall(head)) >= 6:
                sentence = head
        if len(_MEANING.findall(sentence)) < 2:
            continue
        return _cut_title(sentence, length)
    return "%s 的活" % AGENT_NAMES.get(agent, agent or "AI")


_NOISE_FILE = re.compile(r"(\.(db|sqlite|sqlite3)(-wal|-shm|-journal)?|\.lock|\.log|\.pyc|\.DS_Store|\.last-success)$", re.IGNORECASE)
_NOISE_DIR = re.compile(r"(^|/)(\.xay|node_modules|__pycache__|\.git)(/|$)")


_READONLY_HINT = re.compile(r"只读|只看不改|只看，?不改|不要(修改|改动|改)任何文件|不(要)?改(任何)?文件|只做(网页)?调研")


def _looks_readonly(task):
    """任务开头就说"只读 / 只看不改 / 不要修改任何文件"的，当只读任务看待（旧记录没有 readonly 标记）。"""
    head = str(task or "").strip()[:60]
    return bool(_READONLY_HINT.search(head))


def _real_changes(paths):
    """改动文件列表去噪：去掉首尾空白、xay 自己的记录、数据库/锁/日志等后台进程产生的文件。"""
    out = []
    for path in paths or []:
        path = str(path).strip()
        if not path or _NOISE_DIR.search(path) or _NOISE_FILE.search(path):
            continue
        if path not in out:
            out.append(path)
    return out


def _human_secs(seconds):
    seconds = int(seconds or 0)
    if seconds >= 3600 and seconds % 3600 == 0:
        return "%d 小时" % (seconds // 3600)
    if seconds >= 60:
        return "%d 分钟" % (seconds // 60)
    return "%d 秒" % seconds


_SETTLED_CACHE = {}


def _conflict_settled(run):
    """冲突的独立工作区已经不需要处理：工作区被删掉了（xay-land 搬回或丢弃过），或这份补丁已经在项目里了。"""
    meta = run.get("meta") or {}
    worktree = meta.get("worktree") or ""
    if worktree and not os.path.isdir(worktree):
        return True
    patch = os.path.join(run.get("id") or "", "land.patch")
    origin = meta.get("origin") or run.get("origin") or ""
    if not origin or not os.path.isfile(patch):
        return False
    try:
        top = subprocess.run(["git", "-C", origin, "rev-parse", "--show-toplevel"], capture_output=True, text=True, timeout=10).stdout.strip()
        index = os.path.join(top, ".git", "index")
        key = (patch, os.path.getmtime(patch), os.path.getmtime(index) if os.path.exists(index) else 0)
        if key not in _SETTLED_CACHE:
            check = subprocess.run(["git", "-C", top, "apply", "--check", "-R", patch], capture_output=True, timeout=20)
            _SETTLED_CACHE[key] = check.returncode == 0
        return _SETTLED_CACHE[key]
    except Exception:
        return False


_FAIL_CODES = (
    ("usage_limit_exceeded", "额度用完了，等额度恢复或换一条线路再试"),
    ("cli_error", "AI 程序异常退出"),
    ("empty_result", "AI 一个字都没回就结束了（agy 常见原因：想运行命令被拒绝，可加 --full）"),
    ("orphaned", ""),
    ("timeout", ""),
)


def _human_fail_code(text, raw=None):
    """把 xay-run / xay_agent 写的失败代码换成一句人话；不认识的原样返回。"""
    text = str(text or "").strip()
    if not text:
        return ""
    for code, human in _FAIL_CODES:
        if text.startswith(code):
            return human
    if text.startswith("exit "):
        return "AI 程序异常退出（退出码 %s）" % text[5:].strip()
    return text


def _fail_reason(run):
    """失败/超时/失联的运行 → 一句给人看的原因（超时时限、驱动写的 fail_reason、收尾说明）。"""
    raw = run.get("raw_status")
    parts = []
    if raw == "timeout":
        parts.append("%s内没做完，已停止" % _human_secs(run.get("timeout_s")) if run.get("timeout_s") else "超过时限没做完，已停止")
    elif raw in ("stale", "orphaned"):
        parts.append("执行进程失联了")
    for text in (_human_fail_code(run.get("meta", {}).get("fail_reason"), raw), run.get("notice")):
        text = str(text or "").strip()
        if text and text not in parts:
            parts.append(text)
    return "；".join(parts)


def _title_from_task(title, task):
    """xay-run 没收到 -t 时用任务首行（截 30 字）当标题；这种标题视为"没有标题"。"""
    title = str(title or "").strip()
    if not title:
        return True
    line = next((x.strip() for x in str(task or "").splitlines() if x.strip()), "")
    stem = title.rstrip("…").strip()
    return bool(line) and bool(stem) and line.startswith(stem)


def _first_line(text, length=60):
    line = next((x.strip() for x in str(text or "").splitlines() if x.strip()), "")
    return _limit(line.lstrip("#").strip(), length)


def _step(kind, text, state="active"):
    """归一事件的 tool.call → humanize 后的一行动作。"""
    raw = str(text or "")
    if kind == "cmd":
        item = humanize({"kind": "cmd", "text": raw, "state": state})
    elif kind == "read":
        item = humanize({"kind": "read", "text": _base(raw) or raw, "state": state})
    elif kind in ("write", "edit"):
        item = humanize({"kind": "edit", "text": _base(raw) or raw, "state": state})
    elif kind == "search":
        item = {"kind": "search", "text": _limit("查找：" + raw), "state": state}
    elif kind == "web":
        item = humanize({"kind": "search", "text": raw, "state": state})
    else:
        item = humanize({"kind": "tool", "text": raw or str(kind or "工具"), "state": state})
    item["raw"] = raw
    return {"kind": item["kind"], "text": item["text"], "raw": item["raw"], "state": item["state"]}


class RunTail:
    """一个 run_dir 的增量读取器：按 offset tail events.norm.jsonl 与 stream.jsonl。"""

    def __init__(self, run_dir):
        self.run_dir = run_dir
        self.norm_offset = 0
        self.stream_offset = 0
        self.entries = []        # 时间线：AI 消息（每轮一条）与插话/续接的负责人消息
        self.turns = {}          # turn_seq -> AI 消息内部状态
        self.current = None
        self.native = ""
        self.stream_turn = None
        self.stream_parts = {}
        self._lead_seq = 0

    @property
    def has_norm(self):
        return os.path.exists(os.path.join(self.run_dir, "events.norm.jsonl"))

    def _read_new(self, name, offset):
        path = os.path.join(self.run_dir, name)
        try:
            size = os.path.getsize(path)
        except OSError:
            return None, 0
        if size < offset:
            return "", -1  # 文件被截断（stream.jsonl 每轮清空）
        if size == offset:
            return "", offset
        with open(path, "rb") as fh:
            fh.seek(offset)
            chunk = fh.read(size - offset)
        complete = chunk.rfind(b"\n") + 1  # 只消费完整行
        return chunk[:complete].decode("utf-8", "replace"), offset + complete

    def update(self):
        text, offset = self._read_new("events.norm.jsonl", self.norm_offset)
        if offset == -1:
            self.__init__(self.run_dir)
            text, offset = self._read_new("events.norm.jsonl", 0)
        if text:
            for line in text.splitlines():
                try:
                    event = json.loads(line)
                except (ValueError, TypeError):
                    continue
                if isinstance(event, dict):
                    self._feed(event)
        if text is not None:
            self.norm_offset = offset
        text, offset = self._read_new("stream.jsonl", self.stream_offset)
        if offset == -1:
            self.stream_parts, self.stream_turn = {}, None
            text, offset = self._read_new("stream.jsonl", 0)
        if text:
            for line in text.splitlines():
                try:
                    item = json.loads(line)
                except (ValueError, TypeError):
                    continue
                if not isinstance(item, dict) or not item.get("text_delta"):
                    continue
                if item.get("turn") is not None and item.get("turn") != self.stream_turn:
                    self.stream_parts, self.stream_turn = {}, item.get("turn")
                key = str(item.get("item") or "")
                self.stream_parts[key] = self.stream_parts.get(key, "") + str(item["text_delta"])
        if text is not None:
            self.stream_offset = offset

    def _turn(self, seq=None, ts=None):
        seq = int(seq) if str(seq or "").isdigit() else (max(self.turns) + 1 if self.turns else 1)
        if seq in self.turns:
            self.current = self.turns[seq]
            return self.current
        turn = {"seq": seq, "ts": ts or time.time(), "status": "running", "steps": [], "step_ids": {},
                "thinking": [], "messages": [], "final": None, "files": [], "tokens": None, "ended": None}
        self.turns[seq] = turn
        self.entries.append(("ai", seq))
        self.current = turn
        return turn

    def _feed(self, event):
        typ = str(event.get("type") or "")
        data = event.get("data") if isinstance(event.get("data"), dict) else {}
        ts = event.get("ts") or time.time()
        if typ == "session.started":
            self.native = str(data.get("native_session_id") or self.native)
            return
        if typ == "turn.started":
            self._turn(data.get("turn_seq"), ts)
            return
        if typ == "control.requested":
            action = str(data.get("action") or "")
            text = str(data.get("text") or "").strip()
            if text and action in ("steer", "resume", "interrupt", "pause", "reassign"):
                self._lead_seq += 1
                source = {"steer": "steer", "resume": "resume"}.get(action, "interrupt_text")
                self.entries.append(("lead", {"kind": "lead", "id": "%s#c%d" % (self.run_dir, self._lead_seq), "ts": ts,
                                              "text": text, "summary": _first_line(text), "source": source,
                                              "by": event.get("by") or data.get("by")}))
            return
        if typ in ("control.acked", "control.failed", "warning", "signal", "decision.created", "decision.resolved", "note.updated"):
            return
        # 轮次结束后还有零星事件（例如最终回复晚于 turn.completed 落账）：并入最后一轮，不另起一条空消息
        turn = self.current or (self.turns[max(self.turns)] if self.turns else self._turn(None, ts))
        if typ == "tool.call":
            ident = str(data.get("id") or "step:%d" % len(turn["steps"]))
            kind = event.get("kind") or data.get("kind") or "other"
            step = _step(kind, event.get("text") or data.get("raw") or "", "active")
            if ident in turn["step_ids"]:
                turn["steps"][turn["step_ids"][ident]].update(step)
            else:
                turn["step_ids"][ident] = len(turn["steps"])
                turn["steps"].append(step)
        elif typ == "tool.result":
            index = turn["step_ids"].get(str(data.get("id") or ""))
            if index is not None:
                ok = data.get("ok")
                turn["steps"][index]["state"] = "failed" if ok is False or (data.get("exit") not in (None, 0)) else "done"
        elif typ == "reasoning":
            text = str(event.get("text") or data.get("text") or "").strip()
            if text:
                turn["thinking"].append(text)
        elif typ == "message":
            text = str(event.get("text") or data.get("text") or "")
            if text:
                turn["messages"].append(text)
                if data.get("final"):
                    turn["final"] = text
        elif typ == "file.changed":
            path = event.get("path") or data.get("path")
            if path and path not in turn["files"]:
                turn["files"].append(path)
        elif typ == "usage":
            # 显示"新消耗"：缓存命中的输入几乎不花钱，不算进去（输入里已含缓存部分时扣掉）
            if data.get("in") is not None or data.get("out") is not None:
                total = max(0, (data.get("in") or 0) - (data.get("cached") or 0)) + (data.get("out") or 0)
            else:
                total = data.get("total")
            if data.get("granularity") == "step":
                turn["tokens"] = (turn["tokens"] or 0) + int(total or 0)
            else:
                turn["tokens"] = int(total or 0)
        elif typ == "turn.completed":
            status = str(data.get("status") or event.get("status") or "completed")
            turn["status"] = {"complete": "completed", "success": "completed", "succeeded": "completed", "done": "completed",
                              "paused": "interrupted", "error": "failed"}.get(status, status)
            turn["ended"] = ts
            self.current = None
        elif typ == "error":
            text = str(event.get("text") or data.get("error") or "出错")
            turn["steps"].append({"kind": "error", "text": _limit("出错：" + text), "raw": text, "state": "failed"})

    def stream_text(self, seq):
        if not self.stream_parts or (self.stream_turn is not None and self.stream_turn != seq):
            return ""
        return "\n\n".join(part for part in self.stream_parts.values() if part)


class ConversationModel:
    """v5 投影：负责人对话、线程、消息，以及按快照差分生成的 SSE 事件。"""

    def __init__(self, data):
        self.data = data
        self.claude = ClaudeIndex()
        self.lock = threading.RLock()
        self.cond = threading.Condition()
        self.history = []
        self.seq = 0
        self.tails = {}
        self._json_cache = {}
        self._text_cache = {}
        self._alive_cache = {}
        self._decision_cache = {}
        self._legacy_cache = {}
        self.snapshot = None
        self._state_sig = None
        self._state_pending = False
        self._state_emitted = 0.0
        self._pump = None
        self._stop = threading.Event()

    # ---- 读取工具 -----------------------------------------------------
    def _cj(self, path):
        try:
            stat = os.stat(path)
        except OSError:
            return {}
        key = (stat.st_mtime_ns, stat.st_size)
        old = self._json_cache.get(path)
        if old and old[0] == key:
            return old[1]
        value = self.data._read_json(path, {}) or {}
        if not isinstance(value, dict):
            value = {}
        self._json_cache[path] = (key, value)
        return value

    def _ct(self, path):
        try:
            stat = os.stat(path)
        except OSError:
            return None
        key = (stat.st_mtime_ns, stat.st_size)
        old = self._text_cache.get(path)
        if old and old[0] == key:
            return old[1]
        value = self.data._read_text(path, None)
        self._text_cache[path] = (key, value)
        return value

    def _alive(self, pid, marker):
        key = (pid, marker)
        old = self._alive_cache.get(key)
        if old and time.time() - old[0] < 5:
            return old[1]
        value = self.data._xay_alive(pid, marker)
        self._alive_cache[key] = (time.time(), value)
        return value

    def _task_text(self, run_dir):
        text = self._ct(os.path.join(run_dir, "prompt.md")) or ""
        if "# 任务" in text:
            text = text.split("# 任务", 1)[1]
        if "\n---" in text:
            text = text.split("\n---", 1)[0]
        return text.strip()

    def _lead_map(self):
        value = self._cj(os.path.join(self.data.home, "lead_map.json"))
        return {os.path.abspath(str(k)): str(v) for k, v in value.items() if v}

    def _session_file(self, session_id):
        if not session_id or "/" in str(session_id):
            return {}
        return self._cj(os.path.join(self.data.home, "sessions", str(session_id) + ".json"))

    def _canonical(self, lead):
        """ls_ 会话若登记的是 Claude 对话（lead_key=claude:<id>），归到那个 Claude 对话。"""
        lead = str(lead or "")
        info = self._session_file(lead)
        key = str(info.get("lead_key") or "")
        if key.startswith("claude:") and len(key) > 7:
            return key[7:]
        return lead

    # ---- 运行 -----------------------------------------------------------
    def _run(self, row, dag_rows, lead_map):
        rd = row["run_dir"]
        start = self._cj(os.path.join(rd, "start.json"))
        meta = self._cj(os.path.join(rd, "meta.json"))
        agent = str(row.get("agent") or meta.get("agent") or start.get("agent") or "codex")
        started = self.data._time(meta.get("started") or start.get("started") or row.get("started") or row.get("time"))
        raw = meta.get("status") or start.get("status")
        if not raw:
            raw = "queued" if os.path.exists(os.path.join(rd, "queued")) else "running" if self._alive(start.get("pid"), "xay-run") else "stale"
        ended = self.data._time(meta.get("ended") or start.get("ended"))
        if not ended and raw in ENDED_RAW:
            ended = started + self.data._number(meta.get("duration_s")) if meta.get("duration_s") else 0
            if not ended:
                for name in ("meta.json", "result.md"):
                    try:
                        ended = int(os.path.getmtime(os.path.join(rd, name)))
                        break
                    except OSError:
                        pass
        dag = os.path.abspath(str(meta.get("dag") or start.get("dag") or row.get("dag"))) if (meta.get("dag") or start.get("dag") or row.get("dag")) else ""
        tail = self.tails.get(rd)
        if tail is None:
            tail = self.tails[rd] = RunTail(rd)
        tail.update()
        # 负责人对话：registry/start/meta 的 lead_session → ls_ 会话 → lead_map → 所属 DAG
        lead = next((str(s.get("lead_session")) for s in (row, start, meta) if s.get("lead_session")), "")
        lead_kind = next((str(s.get("lead_kind")) for s in (row, start, meta) if s.get("lead_kind") and s.get("lead_kind") != "unknown"), "")
        host = next((str(s.get("lead_host") or s.get("lead_host_id")) for s in (row, start, meta) if s.get("lead_host") or s.get("lead_host_id")), "")
        session = next((str(s.get("session")) for s in (row, start, meta) if s.get("session")), "")
        if not lead and session:
            lead = session
        if not lead:
            lead = lead_map.get(rd, "")
            if lead:
                lead_kind = "claude"
        if not lead and dag:
            drow = dag_rows.get(dag) or {}
            state = self._cj(os.path.join(dag, "state.json"))
            lead = drow.get("lead_session") or state.get("lead_session") or lead_map.get(dag, "")
            lead_kind = lead_kind or drow.get("lead_kind") or state.get("lead_kind") or ""
        title = next((str(s["title"]).replace("�", "").strip() for s in (row, meta, start) if isinstance(s, dict) and s.get("title")), "")
        task = self._task_text(rd)
        changed = meta.get("changed_files") or start.get("changed_files") or row.get("changed_files") or []
        if isinstance(changed, str):
            changed = [x for x in changed.splitlines() if x]
        # 只读任务不该有改动：git 看到的多半是别的进程动了数据库、日志；写任务也要去掉这类噪音
        readonly = bool(start.get("readonly") or meta.get("readonly") or row.get("readonly")) or _looks_readonly(task)
        changed = [] if readonly else _real_changes(_clean_paths(changed))
        driver = str(meta.get("driver") or start.get("driver") or row.get("driver") or "")
        caps = meta.get("capabilities") if isinstance(meta.get("capabilities"), dict) else {}
        default_caps = DEFAULT_CAPS.get(driver) or ({"steer": "live", "interrupt": "soft"} if agent == "codex" and tail.has_norm else {"steer": "none", "interrupt": "kill"})
        return {
            "id": rd, "agent": agent, "title": _clean_title(task, agent) if _title_from_task(title, task) else title, "raw_status": raw,
            "status": self.data._v4_status(raw), "started": started, "ended": ended or None,
            "origin": str(meta.get("origin") or start.get("origin") or row.get("origin") or meta.get("workdir") or start.get("workdir") or row.get("workdir") or ""),
            "dag": dag, "task_id": str(meta.get("task") or start.get("task") or row.get("task") or ""),
            "resume": str(meta.get("resume") or start.get("resume") or row.get("resume") or ""),
            "worker": str(meta.get("worker") or start.get("worker") or row.get("worker") or ""),
            "native": str(meta.get("session_id") or meta.get("native_session_id") or tail.native or ""),
            "lead": self._canonical(lead) if lead else "", "lead_raw": lead, "lead_kind": lead_kind, "lead_host": host,
            "session": session, "session_title": str(row.get("session_title") or start.get("session_title") or ""),
            "task": task, "changed": list(changed), "readonly": readonly, "tail": tail, "meta": meta, "start": start,
            "timeout_s": self.data._number(start.get("timeout") or meta.get("timeout") or 0),
            "capabilities": {"steer": caps.get("steer") or default_caps["steer"], "interrupt": caps.get("interrupt") or default_caps["interrupt"]},
            "land": meta.get("land", start.get("land", row.get("land", ""))) or "", "notice": str(meta.get("notice") or ""),
        }

    def _run_messages(self, run, first_round):
        """一轮执行 → [负责人消息, AI 消息…]。"""
        rd = run["id"]
        messages = [{"kind": "lead", "id": rd + "#task", "ts": run["started"], "text": run["task"],
                     "summary": run["title"] if first_round else _first_line(run["task"]) or run["title"],
                     "source": "task" if first_round else "resume"}]
        tail = run["tail"]
        active = run["status"] in ACTIVE_TASK or run["status"] == "paused"
        result = self._ct(os.path.join(rd, "result.md"))
        # 由 xay_agent 托管的运行（codex/agy）走 events.norm.jsonl；账本还没出现时先给一条空的流式消息，
        # 不能退回 EventParser，否则同一条消息会先被标成"不流式"。
        managed = str(run["start"].get("driver") or "") in ("codex-app-server", "agy-stream", "codex-exec", "agy-oneshot", "dsh-acp", "dsh-headless")
        if tail.has_norm or (managed and active):
            ai_messages = []
            for kind, value in tail.entries:
                if kind == "lead":
                    messages.append(dict(value))
                    continue
                turn = tail.turns[value]
                status = turn["status"]
                if status == "running" and not active:
                    status = {"done": "completed", "canceled": "interrupted", "paused": "interrupted"}.get(run["status"], "failed")
                if status == "running" and run["status"] == "paused":
                    status = "interrupted"
                text = "\n\n".join(turn["messages"])
                streaming = status == "running"
                if streaming:
                    live = tail.stream_text(value)
                    if len(live) > len(text):
                        text = live
                message = {"kind": "ai", "id": "%s#%d" % (rd, value), "ts": turn["ts"], "run": rd, "turn": value,
                           "status": status, "streaming": streaming, "steps": [dict(s) for s in turn["steps"]],
                           "thinking": "\n".join(turn["thinking"])[-2000:], "text": text, "final": turn["final"],
                           "files": list(turn["files"]), "usage": {"tokens": turn["tokens"]} if turn["tokens"] is not None else None,
                           "ended": turn["ended"] if not streaming else None}
                messages.append(message)
                ai_messages.append(message)
            if not ai_messages and active:
                live = tail.stream_text(None)
                message = {"kind": "ai", "id": rd + "#1", "ts": run["started"], "run": rd, "turn": 1, "status": "running",
                           "streaming": True, "steps": [], "thinking": "", "text": live, "final": None, "files": [], "usage": None, "ended": None}
                messages.append(message)
                ai_messages.append(message)
        else:
            # 旧版运行（第三版 events.raw）：用 EventParser 解析，标记为不流式
            try:
                stat = os.stat(os.path.join(rd, "events.raw"))
                key = (stat.st_mtime_ns, stat.st_size, run["status"])
            except OSError:
                key = (None, None, run["status"])
            cached = self._legacy_cache.get(rd)
            if not cached or cached[0] != key:
                snap = self.data._events(rd, run["agent"])
                steps = [humanize(a) for a in snap.get("actions", []) if a.get("kind") != "say"]
                status = "running" if active else "completed" if run["status"] == "done" else "interrupted" if run["status"] == "canceled" else "failed"
                cached = self._legacy_cache[rd] = (key, {
                    "kind": "ai", "id": rd + "#1", "ts": run["started"], "run": rd, "turn": 1, "status": status,
                    "streaming": False, "steps": [{"kind": s["kind"], "text": s["text"], "raw": s["raw"], "state": s["state"]} for s in steps],
                    "thinking": "", "text": str(snap.get("final") or ""), "final": None, "files": [],
                    "usage": {"tokens": snap.get("tokens")} if snap.get("tokens") is not None else None,
                    "ended": None if active else run["ended"]})
            message = dict(cached[1], steps=[dict(x) for x in cached[1]["steps"]], files=[])
            messages.append(message)
            ai_messages = [message]
        for message in ai_messages:
            # 已结束但账本里没有 turn.completed 的（被杀、旧数据）用 run 的结束时间
            if message["status"] != "running" and not message.get("ended"):
                message["ended"] = run["ended"]
            message["elapsed_s"] = max(0, int(message["ended"] - (message["ts"] or 0))) if message.get("ended") and message.get("ts") else None
        if ai_messages and not active:
            last = ai_messages[-1]
            if last["final"] is None and result is not None and result.strip():
                last["final"] = result
            elif last["final"] is not None and not str(last["final"]).strip():
                last["final"] = None
            # 运行以失败/超时/失联收场：消息状态跟运行一致（账本里的"完成"可能早于后续失败），并把原因写成一行
            if run["raw_status"] in ("failed", "timeout", "stale", "orphaned"):
                if last["status"] == "completed":
                    last["status"] = "failed"
                reason = _fail_reason(run)
                if reason and not any(s.get("kind") == "error" and s.get("raw") == reason for s in last["steps"]):
                    last["steps"].append({"kind": "error", "text": _limit("出错：" + reason, 60), "raw": reason, "state": "failed"})
            for path in run["changed"]:
                if path not in last["files"] and "/.xay/" not in "/" + path:
                    last["files"].append(path)
        return messages

    def _threads(self, runs, dags, decisions):
        runs = sorted(runs, key=lambda r: (r["started"] or 0, r["id"]))
        groups, order, by_native = {}, [], {}
        for run in runs:
            # 续接（同一个 AI 接着干）优先归到原线程，哪怕它是另一个 DAG 里的任务
            if run["resume"] and run["resume"] in by_native:
                key = by_native[run["resume"]]
            elif run["dag"] and run["task_id"]:
                key = ("dag", run["dag"], run["task_id"])
            else:
                key = ("run", run["id"])
            if key not in groups:
                groups[key] = []
                order.append(key)
            groups[key].append(run)
            if run["native"]:
                by_native[run["native"]] = key
        threads = []
        for key in order:
            rounds = groups[key]
            first, latest = rounds[0], rounds[-1]
            status = latest["status"]
            # 只看最新一轮：早先轮次的冲突/失败已被后面的返工取代
            own = [d for d in decisions if d.get("execution") == latest["id"]]
            if any(d.get("owner") == "user" for d in own):
                status = "needs_you"
            lead_own = [d for d in own if d.get("owner") != "user"]
            lead_todo = {"kind": lead_own[-1].get("kind"), "title": lead_own[-1].get("title"), "detail": lead_own[-1].get("detail", "")} if lead_own else None
            messages = []
            for index, run in enumerate(rounds):
                messages.extend(self._run_messages(run, index == 0))
            # 不按 ts 重排：轮次按开始时间，轮内按账本顺序（各通道的时间戳精度不同）
            dag = dags.get(first["dag"]) if first["dag"] else None
            dag_state = self._cj(os.path.join(first["dag"], "state.json")) if first["dag"] else {}
            active = latest["id"] if latest["status"] in ACTIVE_TASK or latest["status"] == "paused" else None
            updated = max([r["ended"] or r["started"] or 0 for r in rounds] + [int(m.get("ts") or 0) for m in messages])
            # 用时 = 各轮实际跑的时间之和（不含轮次之间等负责人的时间）；进行中那一轮由前端按 active_since 累加
            work = sum(max(0, (r["ended"] or 0) - (r["started"] or 0)) for r in rounds if r["ended"] and r["started"] and r["id"] != active)
            threads.append({
                "id": first["id"], "agent": first["agent"], "title": first["title"], "status": status,
                "started": first["started"], "updated": updated,
                "phase_title": (dag_state.get("title") or (dag or {}).get("title") or "") if first["dag"] else "",
                "capabilities": dict(latest["capabilities"]), "active_run": active, "messages": messages,
                "work_s": int(work), "active_since": latest["started"] if active else None,
                "lead_todo": lead_todo, "latest_run": latest["id"],
                "worker": next((r["worker"] for r in reversed(rounds) if r["worker"]), None),
                "_runs": rounds,
            })
        return threads

    def build(self):
        data = self.data
        rows = data._registry()
        lead_map = self._lead_map()
        dags = {os.path.abspath(r["dag_dir"]): r for r in rows if r.get("kind") == "dag"}
        runs = [self._run(r, dags, lead_map) for r in rows if r.get("kind") == "run"]
        live = {r["id"] for r in runs}
        for rd in list(self.tails):
            if rd not in live:
                self.tails.pop(rd, None)
        decisions = []
        overrides = json.dumps(data._decision_overrides, sort_keys=True, ensure_ascii=False, default=str)
        for r in runs:
            key = (overrides, r["raw_status"], r["land"], r["notice"], r["ended"], r["session"], r["dag"], r["task_id"], str(r["meta"].get("fail_reason") or ""))
            for name in ("decisions.jsonl", "events.norm.jsonl"):
                try:
                    stat = os.stat(os.path.join(r["id"], name))
                    key += (stat.st_mtime_ns, stat.st_size)
                except OSError:
                    key += (None,)
            cached = self._decision_cache.get(r["id"])
            if not cached or cached[0] != key:
                rows_for_run = data._decision_rows([{
                    "id": r["id"], "start_data": r["start"], "session": r["session"], "dag": r["dag"], "task_id": r["task_id"],
                    "land": r["land"], "notice": r["notice"], "origin": r["origin"], "ended": r["ended"], "started": r["started"],
                    "status": r["raw_status"], "meta": r["meta"]}])
                cached = self._decision_cache[r["id"]] = (key, rows_for_run)
            decisions.extend(dict(d) for d in cached[1])
        threads = self._threads(runs, dags, decisions)
        # 线程 → 负责人对话：任意一轮带了负责人就用它；否则统一归入"临时文件夹 / 以前的任务"
        convs = {}
        legacy_id = "legacy:" + TEMP_WORKSPACE["path"]
        for thread in threads:
            rounds = thread.pop("_runs")
            lead_run = next((r for r in rounds if r["lead"]), None)
            if lead_run:
                cid = lead_run["lead"]
            else:
                cid = legacy_id
            conv = convs.setdefault(cid, {"id": cid, "threads": [], "runs": [], "lead_run": lead_run})
            conv["threads"].append(thread)
            conv["runs"].extend(rounds)
            if conv["lead_run"] is None:
                conv["lead_run"] = lead_run
        conversations, run_index = {}, {}
        for cid, item in convs.items():
            runs_sorted = sorted(item["runs"], key=lambda r: (r["started"] or 0, r["id"]))
            first = runs_sorted[0]
            lead_run = item["lead_run"]
            claude = None
            if not cid.startswith("legacy:"):
                claude = self.claude.lookup(cid, lead_run["lead_host"] if lead_run else None)
            session = self._session_file(lead_run["lead_raw"]) if lead_run else {}
            begin_title = ""
            for candidate in [session.get("title")] + [r["session_title"] for r in runs_sorted]:
                if candidate and not str(candidate).startswith("未命名会话"):
                    begin_title = str(candidate)
                    break
            first_thread = min(item["threads"], key=lambda t: (t["started"] or 0, t["id"]))
            if cid.startswith("legacy:"):
                title, kind = "以前的任务", "unknown"
                workspace = dict(TEMP_WORKSPACE)
            else:
                title = (claude or {}).get("title") or begin_title or first_thread["title"]
                kind = "claude" if claude else (lead_run["lead_kind"] if lead_run and lead_run["lead_kind"] else session.get("lead_kind") or "unknown")
                origin = (claude or {}).get("originCwd") or (claude or {}).get("cwd")
                workspace = _workspace_for(origin or first["origin"] or "")
            item["threads"].sort(key=lambda t: (AGENT_ORDER.index(t["agent"]) if t["agent"] in AGENT_ORDER else 9, t["started"] or 0, t["id"]))
            statuses = [t["status"] for t in item["threads"]]
            if "needs_you" in statuses:
                status = "needs_you"
            elif any(s in ACTIVE_TASK for s in statuses):
                status = "running"
            elif "paused" in statuses:
                status = "idle"
            else:
                status = "done"
            updated = max(t["updated"] or 0 for t in item["threads"])
            if claude and claude.get("lastActivityAt"):
                updated = max(updated, int(self.data._number(claude.get("lastActivityAt")) / 1000))
            conv = {
                "id": cid, "title": title, "lead_kind": kind,
                "workspace": workspace,
                "status": status, "updated": updated,
                "counts": {"running": sum(s in ACTIVE_TASK for s in statuses), "done": statuses.count("done"),
                           "failed": sum(s in ("failed", "orphaned") for s in statuses),
                           "lead_todo": sum(1 for t in item["threads"] if t.get("lead_todo"))},
                "agents": [a for a in AGENT_ORDER if any(t["agent"] == a for t in item["threads"])],
                "lead_host": next((r["lead_host"] for r in runs_sorted if r["lead_host"]), None),
                "archived": bool((claude or {}).get("isArchived")) if claude else False,
                "legacy": cid.startswith("legacy:"),
            }
            conversations[cid] = {"conversation": conv, "threads": item["threads"]}
            for thread in item["threads"]:
                for message in thread["messages"]:
                    if message.get("run"):
                        run_index[message["run"]] = (cid, thread["id"])
                run_index[thread["id"]] = (cid, thread["id"])
        # 每轮 run_dir → 所在线程（含返工轮次）
        for cid, value in conversations.items():
            for thread in value["threads"]:
                for message in thread["messages"]:
                    if message.get("kind") == "lead" and "#" in message["id"]:
                        run_index[message["id"].rsplit("#", 1)[0]] = (cid, thread["id"])
        workspaces = {}
        for cid, value in conversations.items():
            conv = value["conversation"]
            path = conv["workspace"]["path"]
            ws = workspaces.setdefault(path, {"path": path, "name": conv["workspace"]["name"], "conversations": [],
                                              "legacy": path == TEMP_WORKSPACE["path"]})
            ws["conversations"].append(conv)
        for ws in workspaces.values():
            ws["conversations"].sort(key=lambda c: (c["legacy"], -(c["updated"] or 0)))
        # "临时文件夹"排在侧栏最后（前端默认折叠）
        ordered = sorted(workspaces.values(), key=lambda w: (w["legacy"], -max((c["updated"] or 0) for c in w["conversations"])))
        latest_runs = {t["latest_run"] for v in conversations.values() for t in v["threads"]}
        # 被后续返工取代的旧轮次决定不再出现
        decisions[:] = [d for d in decisions if d.get("execution") in latest_runs or d.get("execution") not in run_index]
        for decision in decisions:
            decision["conversation"] = run_index.get(decision.get("execution"), (None,))[0]
            decision["for_user"] = decision.get("owner") == "user"
        return {"conversations": conversations, "workspaces": ordered, "run_index": run_index, "decisions": decisions}

    # ---- 差分与事件 ---------------------------------------------------
    def _emit(self, event_type, data):
        self.seq += 1
        self.history.append({"seq": self.seq, "type": event_type, "data": data, "ts": time.time()})
        if len(self.history) > V5_HISTORY:
            del self.history[:len(self.history) - V5_HISTORY]

    @staticmethod
    def _thread_public(thread):
        return {k: v for k, v in thread.items() if k != "messages"}

    def _diff(self, old, new):
        old_messages = {}
        old_threads = {}
        for cid, value in old["conversations"].items():
            for thread in value["threads"]:
                old_threads[(cid, thread["id"])] = self._thread_public(thread)
                for message in thread["messages"]:
                    old_messages[(cid, thread["id"], message["id"])] = message
        for cid, value in new["conversations"].items():
            for thread in value["threads"]:
                tid = thread["id"]
                public = self._thread_public(thread)
                before = old_threads.get((cid, tid))
                if before is None:
                    self._emit("thread.update", {"conversation": cid, "thread": tid, "patch": public})
                else:
                    patch = {k: v for k, v in public.items() if before.get(k) != v}
                    if patch:
                        self._emit("thread.update", {"conversation": cid, "thread": tid, "patch": patch})
                for message in thread["messages"]:
                    previous = old_messages.get((cid, tid, message["id"]))
                    if previous is None:
                        self._emit("message.append", {"conversation": cid, "thread": tid, "message": message})
                        continue
                    base = {"conversation": cid, "thread": tid, "message_id": message["id"]}
                    patch = {}
                    old_text, new_text = previous.get("text") or "", message.get("text") or ""
                    if new_text != old_text:
                        if new_text.startswith(old_text):
                            self._emit("message.delta", {**base, "text_delta": new_text[len(old_text):]})
                        else:
                            patch["text"] = new_text
                    old_steps, new_steps = previous.get("steps") or [], message.get("steps") or []
                    if new_steps != old_steps:
                        prefix = len(old_steps)
                        same = [{k: s.get(k) for k in ("kind", "raw")} for s in old_steps] == [{k: s.get(k) for k in ("kind", "raw")} for s in new_steps[:prefix]]
                        if same and new_steps[:prefix] == old_steps:
                            for step in new_steps[prefix:]:
                                self._emit("message.delta", {**base, "step": step})
                        elif same:
                            for step in new_steps[prefix:]:
                                self._emit("message.delta", {**base, "step": step})
                            patch["steps"] = new_steps
                        else:
                            patch["steps"] = new_steps
                    for key, value in message.items():
                        if key in ("text", "steps", "id", "kind"):
                            continue
                        if previous.get(key) != value:
                            patch[key] = value
                    if patch:
                        self._emit("message.update", {**base, "patch": patch})

    def _state_signature(self, snap):
        convs = []
        for value in snap["conversations"].values():
            conv = value["conversation"]
            convs.append((conv["id"], conv["title"], conv["status"], json.dumps(conv["counts"], sort_keys=True),
                          tuple(conv["agents"]), conv["workspace"]["path"], len(value["threads"])))
        decisions = tuple(sorted((d.get("id"), d.get("version"), d.get("owner")) for d in snap["decisions"]))
        return (tuple(sorted(convs)), decisions)

    def tick(self):
        with self.lock:
            new = self.build()
            old = self.snapshot
            before = self.seq
            if old is not None:
                self._diff(old, new)
            self.snapshot = new
            sig = self._state_signature(new)
            if old is not None and sig != self._state_sig:
                self._state_pending = True
            self._state_sig = sig
            if self._state_pending and time.time() - self._state_emitted >= 1.0:
                self._emit("state.changed", {})
                self._state_pending = False
                self._state_emitted = time.time()
            changed = self.seq != before
        if changed:
            with self.cond:
                self.cond.notify_all()
        return new

    def current(self):
        with self.lock:
            if self.snapshot is None:
                return self.tick()
            return self.snapshot

    def ensure_pump(self, interval=0.3):
        with self.lock:
            if self._pump is not None and self._pump.is_alive():
                return
            self._stop.clear()

            def loop():
                while not self._stop.is_set():
                    try:
                        self.tick()
                    except Exception as exc:  # 看板投影出错不能让推送线程退出
                        print("看板 v5 刷新出错：%s" % exc, file=sys.stderr, flush=True)
                    self._stop.wait(interval)
            self._pump = threading.Thread(target=loop, name="xay-v5-pump", daemon=True)
            self._pump.start()

    def stop(self):
        self._stop.set()
        with self.cond:
            self.cond.notify_all()

    def events_since(self, since):
        """返回 (events, reset)。reset=True 表示续传位置无效，客户端应重新拉 /api/state。"""
        with self.lock:
            history = list(self.history)
            seq = self.seq
        if since > seq or (history and since < history[0]["seq"] - 1) or (not history and 0 < since < seq):
            return [item for item in history], True
        return [item for item in history if item["seq"] > since], False

    def wait(self, since, timeout):
        with self.cond:
            if self.seq > since:
                return
            self.cond.wait(timeout)

    # ---- 对外 ---------------------------------------------------------
    def state(self):
        snap = self.tick()
        return {"seq": self.seq, "workspaces": snap["workspaces"], "decisions": snap["decisions"]}

    def conversation(self, cid):
        snap = self.tick()
        value = snap["conversations"].get(cid)
        if value is None:
            return None
        threads = sorted(value["threads"], key=lambda t: (AGENT_ORDER.index(t["agent"]) if t["agent"] in AGENT_ORDER else 9, t["started"] or 0, t["id"]))
        return {"conversation": value["conversation"], "threads": threads}


class Handler(BaseHTTPRequestHandler):
    data = None
    html_path = None

    def _send(self, body, status=200, content_type="application/json; charset=utf-8", cache="no-store"):
        raw = body.encode("utf-8") if isinstance(body, str) else body
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(raw)))
        self.send_header("Cache-Control", cache)  # 默认 no-store：升级页面后刷新即生效
        self.end_headers()
        self.wfile.write(raw)

    def _json(self, value, status=200):
        self._send(json.dumps(value, ensure_ascii=False), status)

    def _host_ok(self):
        host = (self.headers.get("Host") or "").split(",", 1)[0].strip().lower()
        if host in ("127.0.0.1:7878", "localhost:7878"):
            return True
        # 测试服务器会随机分配端口；仍只允许本机回环地址。
        try:
            port = int(self.server.server_address[1])
        except (AttributeError, TypeError, ValueError):
            port = 7878
        return port != 7878 and host in ("127.0.0.1:" + str(port), "localhost:" + str(port))

    def _post_auth(self, action, legacy=False):
        if not self._host_ok():
            self._json({"ok": False, "error": "非法 Host"}, 403)
            return False
        if self.headers.get("X-Xay-Action") != action:
            self._json({"ok": False, "error": "需要 X-Xay-Action: " + action}, 403)
            return False
        # v2 的 /api/stop 没有浏览器来源头，继续保留该旧入口；v4 写操作严格校验。
        if legacy and not self.headers.get("Origin") and not self.headers.get("Sec-Fetch-Site"):
            return True
        origin = (self.headers.get("Origin") or "").lower().rstrip("/")
        host = (self.headers.get("Host") or "").lower().split(",", 1)[0].strip()
        if origin not in ("http://127.0.0.1:7878", "http://localhost:7878", "http://" + host):
            self._json({"ok": False, "error": "Origin 必须与看板同源"}, 403)
            return False
        if self.headers.get("Sec-Fetch-Site") != "same-origin":
            self._json({"ok": False, "error": "需要 Sec-Fetch-Site: same-origin"}, 403)
            return False
        return True

    def _payload(self):
        length = int(self.headers.get("Content-Length", "0") or 0)
        value = json.loads(self.rfile.read(length) or b"{}")
        if not isinstance(value, dict):
            raise ValueError("请求体必须是 JSON 对象")
        return value

    def _spawn_rework(self, execution, text):
        """已结束的任务收到新指示：用 xay-run -r <原 AI 会话> 在原工作区续接一轮。"""
        run_dir = str(execution or "")
        start = self.data._read_json(os.path.join(run_dir, "start.json"), {}) or {}
        meta = self.data._read_json(os.path.join(run_dir, "meta.json"), {}) or {}
        if not meta.get("status") or meta.get("status") in ("running", "queued"):
            return {"ok": False, "error": "任务还在运行，无法续接"}
        agent = meta.get("agent") or start.get("agent")
        native = meta.get("session_id") or meta.get("native_session_id") or start.get("native_session_id")
        origin = meta.get("origin") or start.get("origin") or meta.get("workdir") or start.get("workdir")
        if agent not in ("codex", "dsh", "agy") or not native or not origin or not os.path.isdir(origin):
            return {"ok": False, "error": "缺少续接所需的会话或工作目录"}
        cmd = [os.path.join(_SCRIPTS_DIR, "xay-run"), agent, "-r", str(native), "-C", origin,
               "-n", str(meta.get("name") or start.get("name") or "rework"),
               "-t", str(meta.get("title") or start.get("title") or "返工")]
        lead = meta.get("lead_session") or start.get("lead_session")
        if lead:
            cmd += ["--session", str(lead)]
        if meta.get("readonly") or start.get("readonly"):
            cmd.append("--readonly")
        if meta.get("worker") or start.get("worker"):
            cmd += ["--as", str(meta.get("worker") or start.get("worker"))]
        if meta.get("effort") or start.get("effort"):
            cmd += ["-e", str(meta.get("effort") or start.get("effort"))]
        cmd.append(text)
        log = os.path.join(self.data.home, "rework.log")
        try:
            with open(log, "ab") as fh:
                subprocess.Popen(cmd, cwd=origin, stdin=subprocess.DEVNULL, stdout=fh, stderr=subprocess.STDOUT, start_new_session=True)
        except OSError as exc:
            return {"ok": False, "error": f"续接启动失败：{exc}"}
        self._emit("state.changed", {})
        return {"ok": True, "accepted_as": "rework", "effective": "new_round"}

    def _ctl(self, execution, payload):
        run_dir = str(execution or "")
        start = self.data._read_json(os.path.join(run_dir, "start.json"), {}) or {}
        meta = self.data._read_json(os.path.join(run_dir, "meta.json"), {}) or {}
        sock_path = meta.get("ctl_sock") or start.get("ctl_sock")
        if not sock_path:
            for item in self.data._all()[1]:
                if item.get("id") == run_dir:
                    sock_path = item.get("start_data", {}).get("ctl_sock")
                    break
        if not sock_path:
            name = _base(run_dir)
            sock_path = os.path.join(self.data.home, "ctl", name + ".sock")
        try:
            fields = {key: value for key, value in payload.items() if key != "op"}
            return CtlClient(run_dir, sock=sock_path, timeout=2).request(
                payload.get("op", ""), **{**fields, "by": "user"}
            )
        except (OSError, ValueError, RuntimeError) as exc:
            return {"ok": False, "error": str(exc)}

    def _decision_audit(self, decision, event_type, option):
        execution = self.data.execution_path(decision.get("execution", ""))
        if not execution:
            return
        path = os.path.join(execution, "decisions.jsonl")
        record = {"type": event_type, "id": decision.get("id"), "kind": decision.get("kind"), "option": option, "by": "user", "ts": int(time.time()), "version": decision.get("version")}
        try:
            with open(path, "a", encoding="utf-8") as fh:
                fh.write(json.dumps(record, ensure_ascii=False) + "\n")
        except OSError:
            pass

    def _emit(self, event_type, data):
        self.data._event_seq += 1
        self.data._event_history.append({"seq": self.data._event_seq, "type": event_type, "data": data, "ts": time.time()})
        if len(self.data._event_history) > 5000:
            self.data._event_history = self.data._event_history[-5000:]

    def _wants_stream(self, query):
        # 浏览器 EventSource 与 curl 都会带 Accept；不带 Accept 或 once=1 的调用方沿用 v4 的一次性快照。
        if query.get("once", [""])[0] in ("1", "true"):
            return False
        accept = self.headers.get("Accept") or ""
        return "text/event-stream" in accept or "*/*" in accept

    def _sse_write(self, item=None, comment=None):
        if comment is not None:
            raw = ": %s\n\n" % comment
        else:
            raw = "id: %d\nevent: %s\ndata: %s\n\n" % (item["seq"], item["type"], json.dumps(item.get("data") or {}, ensure_ascii=False))
        self.wfile.write(raw.encode("utf-8"))
        self.wfile.flush()

    def _sse_stream(self, query):
        """ui-v5-api §3：长连接 SSE；Last-Event-ID 优先于 since，续传位置无效时先发 state.changed。"""
        model = self.data.v5
        raw_since = self.headers.get("Last-Event-ID") or query.get("since", ["0"])[0]
        try:
            since = int(raw_since or 0)
        except ValueError:
            self._json({"ok": False, "error": "since 必须是数字"}, 400)
            return
        model.ensure_pump()
        model.current()
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream; charset=utf-8")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("X-Accel-Buffering", "no")
        self.end_headers()
        self.close_connection = True
        org_path = os.path.join(xay_org.root().as_posix(), "bus.jsonl")
        org_stamp = os.path.getmtime(org_path) if os.path.exists(org_path) else 0
        try:
            self.wfile.write(b"retry: 2000\n\n")
            last_beat = time.time()
            while not model._stop.is_set():
                current_org_stamp = os.path.getmtime(org_path) if os.path.exists(org_path) else 0
                if current_org_stamp != org_stamp:
                    org_stamp = current_org_stamp
                    self._sse_write({"seq": model.seq, "type": "org.changed", "data": {}})
                events, reset = model.events_since(since)
                if reset:
                    since = model.seq
                    self._sse_write({"seq": since, "type": "state.changed", "data": {}})
                    events = []
                for item in events:
                    self._sse_write(item)
                    since = item["seq"]
                if time.time() - last_beat >= 15:
                    self._sse_write(comment="heartbeat %d" % int(time.time()))
                    last_beat = time.time()
                model.wait(since, 15)
        except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError, OSError):
            return

    def do_GET(self):
        if not self._host_ok():
            self._send("Forbidden", 403, "text/plain; charset=utf-8")
            return
        parsed = urlparse(self.path)
        if parsed.path == "/":
            try:
                self._send(open(self.html_path, encoding="utf-8").read(), content_type="text/html; charset=utf-8")
            except OSError:
                self._send("看板页面不存在", 404, "text/plain; charset=utf-8")
        elif parsed.path == "/api/state":
            self._json(self.data.state())
        elif parsed.path == "/api/org":
            conversation = parse_qs(parsed.query).get("conversation", [""])[0] or None
            snap = self.data.v5.tick() or {}
            convs = snap.get("conversations") or {}
            if not conversation:
                # 没指定就看最近活跃的那个负责人对话
                recent = [c for w in snap.get("workspaces") or [] for c in w.get("conversations") or [] if not c.get("legacy")]
                recent.sort(key=lambda c: -(c.get("updated") or 0))
                conversation = recent[0]["id"] if recent else None
            payload = xay_org.projection(conversation=conversation, session=conversation)
            title = ((convs.get(conversation) or {}).get("conversation") or {}).get("title")
            if title:
                payload["conversation"]["title"] = title
            self._json(payload)
        elif parsed.path == "/org":
            org_path = os.path.join(os.path.dirname(os.path.realpath(__file__)), "xay-org.html")
            try:
                self._send(open(org_path, encoding="utf-8").read(), content_type="text/html; charset=utf-8")
            except OSError:
                self._send("公司页面不存在", 404, "text/plain; charset=utf-8")
        elif parsed.path == "/chars":
            chars_path = os.path.join(os.path.dirname(os.path.realpath(__file__)), "xay-chars", "gallery.html")
            try:
                with open(chars_path, encoding="utf-8") as chars_file:
                    self._send(chars_file.read(), content_type="text/html; charset=utf-8")
            except OSError:
                self._send("角色图鉴不存在", 404, "text/plain; charset=utf-8")
        elif parsed.path.startswith("/xay-chars/img/"):
            # 角色头像图（来自 ai-chan-sheets 设定图，见 scripts/xay-chars/img/LICENSE.md）
            name = parsed.path[len("/xay-chars/img/"):]
            if not re.fullmatch(r"[a-z0-9-]+\.webp", name):
                self._send("Not Found", 404, "text/plain; charset=utf-8")
                return
            img_path = os.path.join(os.path.dirname(os.path.realpath(__file__)), "xay-chars", "img", name)
            try:
                with open(img_path, "rb") as img_file:
                    self._send(img_file.read(), content_type="image/webp", cache="max-age=3600")
            except OSError:
                self._send("Not Found", 404, "text/plain; charset=utf-8")
        elif parsed.path.startswith("/xay-chars/"):
            name = parsed.path[len("/xay-chars/"):]
            if not re.fullmatch(r"[a-z0-9-]+\.js", name):
                self._send("Not Found", 404, "text/plain; charset=utf-8")
                return
            chars_path = os.path.join(os.path.dirname(os.path.realpath(__file__)), "xay-chars", name)
            try:
                with open(chars_path, encoding="utf-8") as chars_file:
                    self._send(chars_file.read(), content_type="application/javascript; charset=utf-8")
            except OSError:
                self._send("Not Found", 404, "text/plain; charset=utf-8")
        elif parsed.path == "/api/conversation":
            item_id = parse_qs(parsed.query).get("id", [""])[0]
            found = self.data.v5.conversation(item_id)
            self._json(found if found else {"error": "Not Found"}, 200 if found else 404)
        elif parsed.path == "/api/events" and self._wants_stream(parse_qs(parsed.query)):
            self._sse_stream(parse_qs(parsed.query))
        elif parsed.path == "/api/events":
            self.data.state()
            query = parse_qs(parsed.query)
            raw_since = query.get("since", [self.headers.get("Last-Event-ID", "0")])[0]
            try:
                since = int(raw_since or 0)
            except ValueError:
                self._json({"ok": False, "error": "since 必须是数字"}, 400)
                return
            history = list(self.data._event_history)
            if history and since and since < history[0]["seq"] - 1:
                self._json({"ok": False, "error": "事件续传位置过旧，请重新获取快照"}, 410)
                return
            events = [item for item in history if item["seq"] > since]
            if not events:
                events = [{"seq": self.data._event_seq, "type": "heartbeat", "data": {"now": int(time.time())}, "ts": time.time()}]
            raw = b"".join((f"id: {item['seq']}\nevent: {item['type']}\ndata: {json.dumps(item.get('data') or {}, ensure_ascii=False)}\n\n").encode("utf-8") for item in events)
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream; charset=utf-8")
            self.send_header("Cache-Control", "no-cache")
            self.send_header("Connection", "close")
            self.send_header("Content-Length", str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)
        elif parsed.path in ("/api/execution", "/api/actions", "/api/diff", "/api/phase", "/api/session", "/api/history"):
            query = parse_qs(parsed.query)
            state = self.data.state()
            item_id = query.get("id", [""])[0]
            if parsed.path == "/api/execution":
                detail = self.data.detail(item_id)
                if detail is None:
                    detail = self.data.detail(self.data.execution_path(item_id)) if self.data.execution_path(item_id) else None
                if detail:
                    run_id = detail["run"]["id"]
                    task = None
                    execution = None
                    for workspace in state.get("workspaces", []):
                        for session in workspace.get("sessions", []):
                            for phase in session.get("phases", []):
                                for candidate in phase.get("tasks", []):
                                    if candidate.get("execution", {}).get("id") == item_id or candidate.get("execution", {}).get("id") == run_id:
                                        task, execution = candidate, candidate.get("execution")
                    payload = {"execution": execution or {"id": item_id, "state": detail["run"].get("status")}, "task": task or {"id": detail["run"].get("mission", ""), "title": detail["run"].get("title", "")}, "turns": detail.get("rounds", []), "actions": detail.get("actions", []), "final_text": detail.get("result"), "changed_files": [{"path": path, "add": 0, "del": 0} for path in detail.get("changed_files", [])], "tech": detail.get("tech", {})}
                    self._json(payload)
                    return
                self._json(detail or {"error": "Not Found"}, 200 if detail else 404)
            elif parsed.path == "/api/actions":
                execution_id = query.get("execution", [item_id])[0]
                detail = self.data.detail(execution_id)
                if detail is None and self.data.execution_path(execution_id):
                    detail = self.data.detail(self.data.execution_path(execution_id))
                actions = (detail or {}).get("actions", [])
                limit = max(1, min(300, int(query.get("limit", [300])[0] or 300)))
                self._json({"actions": actions[-limit:]})
            elif parsed.path == "/api/diff":
                execution_id = query.get("execution", [""])[0]
                run_path = self.data.execution_path(execution_id)
                diff = ""
                if run_path:
                    start = self.data._read_json(os.path.join(run_path, "start.json"), {}) or {}
                    meta = self.data._read_json(os.path.join(run_path, "meta.json"), {}) or {}
                    origin = meta.get("origin") or start.get("origin") or meta.get("workdir") or start.get("workdir")
                    base = meta.get("base_commit") or start.get("base_commit")
                    path = query.get("path", [""])[0]
                    if origin and base:
                        try:
                            diff = subprocess.run(["git", "-C", origin, "diff", str(base), "--", path], capture_output=True, text=True, check=False).stdout
                        except OSError:
                            diff = ""
                self._json({"path": query.get("path", [""])[0], "diff": diff})
            elif parsed.path == "/api/history":
                items = state.get("runs", [])
                if query.get("agent", [""])[0]:
                    items = [x for x in items if x.get("agent") == query["agent"][0]]
                if query.get("session", [""])[0]:
                    items = [x for x in items if x.get("session") == query["session"][0]]
                if query.get("ws", [""])[0]:
                    items = [x for x in items if x.get("origin") == query["ws"][0] or x.get("workdir") == query["ws"][0]]
                if query.get("status", [""])[0]:
                    items = [x for x in items if x.get("status") == query["status"][0]]
                if query.get("q", [""])[0]:
                    q = query["q"][0].lower(); items = [x for x in items if q in (x.get("title", "") + x.get("workdir", "")).lower()]
                self._json({"items": items[:max(1, min(200, int(query.get("limit", [100])[0] or 100)))]})
            else:
                found = None
                for workspace in state.get("workspaces", []):
                    for session in workspace.get("sessions", []):
                        if parsed.path == "/api/session" and session.get("id") == item_id:
                            found = session
                        for phase in session.get("phases", []):
                            if parsed.path == "/api/phase" and phase.get("id") == item_id:
                                found = phase
                self._json(found or {"error": "Not Found"}, 200 if found else 404)
        elif parsed.path == "/api/run":
            item_id = parse_qs(parsed.query).get("id", [""])[0]
            detail = self.data.detail(item_id)
            self._json(detail if detail else {"error": "Not Found"}, 200 if detail else 404)
        else:
            self._send("Not Found", 404, "text/plain; charset=utf-8")

    def do_POST(self):
        parsed = urlparse(self.path)
        action_by_path = {"/api/control": "control", "/api/decision": "decide", "/api/session": "session", "/api/stop": "stop"}
        action = action_by_path.get(parsed.path)
        if not action:
            if not self._host_ok():
                self._send("Forbidden", 403, "text/plain; charset=utf-8")
            else:
                self._send("Not Found", 404, "text/plain; charset=utf-8")
            return
        if not self._post_auth(action, legacy=action == "stop"):
            return
        try:
            payload = self._payload()
        except (ValueError, TypeError, json.JSONDecodeError):
            self._json({"ok": False, "error": "请求体不是有效 JSON"}, 400)
            return
        if action == "control":
            # v5 页面发 {id, op, text}，v4 发 {execution, action}；任选其一
            op = payload.get("action") or payload.get("op")
            if op not in ("steer", "interrupt", "pause", "resume", "stop", "reassign"):
                self._json({"ok": False, "error": "不支持的控制动作"}, 422)
                return
            execution = self.data.execution_path(payload.get("execution") or payload.get("id") or "")
            if not execution:
                self._json({"ok": False, "error": "任务不存在"}, 404)
                return
            response = self._ctl(execution, {"op": op, **{k: payload[k] for k in ("text", "to_agent", "expected_turn") if k in payload}})
            if not response.get("ok") and op in ("resume", "steer", "interrupt") and str(payload.get("text") or "").strip():
                # 任务已结束、托管进程不在了：按新指示在同一 AI 会话里续接一轮（返工），归入同一线程
                rework = self._spawn_rework(execution, str(payload["text"]).strip())
                if rework.get("ok"):
                    response = rework
            self._json(response, 200 if response.get("ok") else 409 if response.get("error") else 503)
            return
        if action == "decide":
            did, option = str(payload.get("id", "")), str(payload.get("option", ""))
            decisions = self.data._decision_rows(self.data._all()[1])
            current = self.data._decision_overrides.get(did) or next((d for d in decisions if d["id"] == did), None)
            if current is None:
                self._json({"ok": False, "error": "决定不存在"}, 404)
                return
            if int(payload.get("version", -1)) != int(current.get("version", 1)):
                self._json({"ok": False, "error": "决定版本已变化", "version": current.get("version")}, 409)
                return
            if option == "takeover":
                current = dict(current); current["owner"] = "user"; current["version"] += 1
                self.data._decision_overrides[did] = current
                self._decision_audit(current, "decision.takeover", option)
                self._emit("decision.upsert", current)
                self._json({"ok": True, "accepted_as": "takeover", "version": current["version"]})
                return
            if option == "handoff_lead":
                current = dict(current); current["owner"] = "lead"; current["version"] += 1
                self.data._decision_overrides[did] = current
                self._decision_audit(current, "decision.handoff", option)
                self._emit("decision.upsert", current)
                self._json({"ok": True, "accepted_as": "handoff", "version": current["version"]})
                return
            if current.get("kind") == "approval" and payload.get("confirm") is not True:
                self._json({"ok": False, "error": "approval 决定需要 confirm=true"}, 422)
                return
            response = self._ctl(current.get("execution", ""), {"op": "decide", "id": did, "option": option, "text": payload.get("text", ""), "version": current.get("version")})
            if response.get("ok"):
                current = dict(current); current["resolved"] = {"ts": int(time.time()), "by": "user", "option": option, "text": payload.get("text", "")}
                self.data._decision_overrides[did] = current
                self._emit("decision.upsert", current)
            self._json(response, 200 if response.get("ok") else 409)
            return
        if action == "session":
            sid = str(payload.get("id", ""))
            if payload.get("seen"):
                self.data._session_seen[sid] = int(time.time())
            if "title" in payload:
                self.data._session_overrides[sid] = str(payload["title"])
            flags = self.data._session_flags.setdefault(sid, {})
            for key in ("pin", "archive"):
                if key in payload:
                    flags["pinned" if key == "pin" else "archived"] = bool(payload[key])
            self._json({"ok": True, "id": sid})
            return
        item_id = payload.get("id", "")
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
