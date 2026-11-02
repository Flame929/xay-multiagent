"""Codex app-server V2 探针；只用 Python 标准库，供负责人手动运行。"""
from __future__ import annotations

import argparse
import json
import queue
import subprocess
import tempfile
import threading
import time
from pathlib import Path
from typing import Any

TIMEOUT = 180.0
SECRET = "PINEAPPLE-42"


def config_args() -> list[str]:
    """与 scripts/xay_drivers/base.py 相同的本探针配置覆盖。"""
    try:
        from xay.adapters.codex import disabled_mcp_servers
    except ImportError:
        from ..xay.adapters.codex import disabled_mcp_servers
    args = [
        "-c", 'model_provider="openai"',
        "-c", 'approval_policy="never"',
        "-c", 'sandbox_mode="workspace-write"',
        "-c", 'model_reasoning_effort="low"',
    ]
    for s in disabled_mcp_servers():
        args += ["-c", f"mcp_servers.{s}.enabled=false"]
    args += [
        "-c", 'mcp_servers.xay={enabled=false,type="stdio",command="/usr/bin/true"}',
        "-c", "mcp_servers.xay.enabled=false",
    ]
    return args


class AppServer:
    def __init__(self, cwd: Path, log_path: Path, command: str = "codex", append: bool = False) -> None:
        self.cwd, self.log_path, self.command = cwd, log_path, command
        self.proc: subprocess.Popen[str] | None = None
        self.events: queue.Queue[dict[str, Any] | None] = queue.Queue()
        self.pending: dict[int, queue.Queue[dict[str, Any]]] = {}
        self.next_id = 1
        self.lock = threading.Lock()
        self.thread_id: str | None = None
        self.turn_id: str | None = None
        self.statuses: list[str] = []
        self.raw = self.log_path.open("a" if append else "w", encoding="utf-8")

    def record(self, direction: str, message: dict[str, Any]) -> None:
        self.raw.write(json.dumps({"time": time.time(), "direction": direction,
                                   "message": message}, ensure_ascii=False) + "\n")
        self.raw.flush()

    def start(self, resume_thread: str | None = None) -> None:
        argv = [self.command, "app-server", "--listen", "stdio://"] + config_args()
        self.proc = subprocess.Popen(argv, cwd=self.cwd, stdin=subprocess.PIPE,
                                     stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                                     text=True, bufsize=1)
        threading.Thread(target=self._reader, daemon=True).start()
        self.rpc("initialize", {"clientInfo": {"name": "xay-v2-probe", "version": "1"},
                                 "capabilities": {"experimentalApi": True}})
        self.notify("initialized", {})
        if resume_thread is None:
            result = self.rpc("thread/start", {"cwd": str(self.cwd), "modelProvider": "openai",
                                                 "sandbox": "workspace-write", "approvalPolicy": "never",
                                                 "ephemeral": False})
        else:
            result = self.rpc("thread/resume", {"threadId": resume_thread, "cwd": str(self.cwd),
                                                  "sandbox": "workspace-write", "approvalPolicy": "never"})
        self.thread_id = self.find_id(result, "thread")

    def _reader(self) -> None:
        assert self.proc and self.proc.stdout
        for line in self.proc.stdout:
            if not line.strip():
                continue
            try:
                message = json.loads(line)
            except json.JSONDecodeError:
                continue
            self.record("recv", message)
            message["_received_mono"] = time.monotonic()
            msg_id = message.get("id")
            if msg_id is not None and msg_id in self.pending and not message.get("method"):
                self.pending[msg_id].put(message)
            else:
                method = message.get("method", "")
                if method == "thread/status/changed":
                    value = self.find_value(message.get("params", {}), "status")
                    if value is not None:
                        self.statuses.append(str(value))
                self.events.put(message)
        self.events.put(None)

    def send(self, message: dict[str, Any]) -> None:
        assert self.proc and self.proc.stdin
        self.record("send", message)
        self.proc.stdin.write(json.dumps(message, ensure_ascii=False) + "\n")
        self.proc.stdin.flush()

    def rpc(self, method: str, params: dict[str, Any], timeout: float = 30) -> dict[str, Any]:
        with self.lock:
            msg_id = self.next_id
            self.next_id += 1
        reply: queue.Queue[dict[str, Any]] = queue.Queue(1)
        self.pending[msg_id] = reply
        try:
            self.send({"id": msg_id, "method": method, "params": params})
            response = reply.get(timeout=timeout)
        finally:
            self.pending.pop(msg_id, None)
        if "error" in response:
            raise RuntimeError(f"{method}: {response['error']}")
        return response.get("result") or {}

    def notify(self, method: str, params: dict[str, Any]) -> None:
        self.send({"method": method, "params": params})

    @staticmethod
    def find_id(value: Any, key: str = "turn") -> str | None:
        if isinstance(value, dict):
            item = value.get(key)
            if isinstance(item, dict) and item.get("id") is not None:
                return str(item["id"])
            for name in (key + "Id", "id"):
                if value.get(name) is not None:
                    return str(value[name])
        return None

    @staticmethod
    def find_value(value: Any, wanted: str) -> Any:
        if isinstance(value, dict):
            if wanted in value:
                return value[wanted]
            for child in value.values():
                found = AppServer.find_value(child, wanted)
                if found is not None:
                    return found
        elif isinstance(value, list):
            for child in value:
                found = AppServer.find_value(child, wanted)
                if found is not None:
                    return found
        return None

    def turn(self, text: str) -> tuple[list[dict[str, Any]], float]:
        started = time.monotonic()
        self.last_turn_start = started
        result = self.rpc("turn/start", {"threadId": self.thread_id,
                                          "input": [{"type": "text", "text": text}],
                                          "cwd": str(self.cwd), "approvalPolicy": "never",
                                          "sandboxPolicy": {"type": "workspaceWrite", "networkAccess": False}})
        self.turn_id = self.find_id(result)
        seen: list[dict[str, Any]] = []
        while True:
            left = TIMEOUT - (time.monotonic() - started)
            if left <= 0:
                raise TimeoutError("turn 超过 180 秒")
            event = self.events.get(timeout=left)
            if event is None:
                raise RuntimeError("app-server 提前退出")
            seen.append(event)
            if event.get("method") == "turn/completed":
                return seen, (time.monotonic() - started) * 1000

    def wait_completed(self) -> list[dict[str, Any]]:
        """等当前（如压缩）这一轮的 turn/completed；压缩在 Codex 里是一轮 kind=Compact。"""
        started = time.monotonic()
        seen: list[dict[str, Any]] = []
        while True:
            left = TIMEOUT - (time.monotonic() - started)
            if left <= 0:
                raise TimeoutError("压缩超过 180 秒")
            event = self.events.get(timeout=left)
            if event is None:
                raise RuntimeError("app-server 提前退出")
            seen.append(event)
            if event.get("method") == "turn/completed":
                return seen

    def close(self) -> None:
        if self.proc and self.proc.poll() is None:
            self.proc.terminate()
            try:
                self.proc.wait(timeout=3)
            except subprocess.TimeoutExpired:
                self.proc.kill()
                self.proc.wait()
        self.raw.close()


def strings(value: Any) -> list[str]:
    if isinstance(value, str):
        return [value]
    if isinstance(value, dict):
        return sum((strings(v) for v in value.values()), [])
    if isinstance(value, list):
        return sum((strings(v) for v in value), [])
    return []


def event_summary(events: list[dict[str, Any]]) -> dict[str, Any]:
    usage: list[Any] = []
    methods: list[str] = []
    reasoning_summary = reasoning_text = 0
    plan_steps = plan_state = None
    compaction_types: list[str] = []
    for event in events:
        method = event.get("method", "")
        if method:
            methods.append(method)
        if method == "thread/tokenUsage/updated":
            usage.append(event.get("params", {}))
        if method == "item/reasoning/summaryTextDelta":
            reasoning_summary += 1
        if method == "item/reasoning/textDelta":
            reasoning_text += 1
        if method == "turn/plan/updated":
            plan = AppServer.find_value(event, "plan")
            if isinstance(plan, list):
                plan_steps = len(plan)
                plan_state = [AppServer.find_value(item, "status") for item in plan]
        item = AppServer.find_value(event, "item")
        if isinstance(item, dict) and item.get("type") == "contextCompaction":
            compaction_types.append("contextCompaction")
    return {"methods": methods, "usage": usage, "reasoningSummary": reasoning_summary,
            "reasoningText": reasoning_text, "planSteps": plan_steps, "planStatus": plan_state,
            "compactionItemTypes": compaction_types, "rawText": " ".join(strings(events))}


def usage_last(summary: dict[str, Any]) -> Any:
    for value in reversed(summary.get("usage", [])):
        found = AppServer.find_value(value, "last")
        if isinstance(found, dict):
            found = found.get("totalTokens", found.get("inputTokens"))
        if found is not None:
            return found
    return None


def dry_run() -> None:
    requests = [
        {"id": 1, "method": "initialize", "params": {"capabilities": {"experimentalApi": True}}},
        {"method": "initialized", "params": {}},
        {"id": 2, "method": "thread/start", "params": {"modelProvider": "openai", "sandbox": "workspace-write", "approvalPolicy": "never"}},
        {"id": 3, "method": "turn/start", "params": {"threadId": "<thread>", "input": [{"type": "text", "text": "Reply OK"}]}},
        {"id": 4, "method": "thread/compact/start", "params": {"threadId": "<thread>"}},
        {"id": 5, "method": "thread/unsubscribe", "params": {"threadId": "<thread>"}},
        {"id": 6, "method": "thread/resume", "params": {"threadId": "<thread>", "sandbox": "workspace-write", "approvalPolicy": "never"}},
    ]
    for request in requests:
        print(json.dumps(request, ensure_ascii=False))


def run(out_json: str | None) -> int:
    stamp = time.strftime("%Y%m%d-%H%M%S", time.gmtime()) + f"-{time.time_ns() % 1_000_000_000:09d}"
    log_path = Path.home() / ".xay" / "v2" / "probes" / f"codex-{stamp}.jsonl"
    log_path.parent.mkdir(parents=True, exist_ok=True)
    result: dict[str, Any] = {"log": str(log_path), "questions": {}, "statusValues": []}
    with tempfile.TemporaryDirectory(prefix="xay-codex-probe-") as temp:
        cwd = Path(temp)
        (cwd / "hello.py").write_text("def hello():\n    return 'hello'\n", encoding="utf-8")
        subprocess.run(["git", "init", "-q"], cwd=cwd, check=True, stdout=subprocess.DEVNULL)
        app = AppServer(cwd, log_path)
        try:
            app.start()
            e1, ms1 = app.turn("Reply with exactly OK.")
            s1 = event_summary(e1)
            result["questions"]["Q1"] = {"usage": s1["usage"], "turnMs": ms1}
            e2, _ = app.turn(f"Remember the passphrase {SECRET}. Reply with exactly OK.")
            e3, _ = app.turn("What passphrase did I ask you to remember?")
            s3 = event_summary(e3)
            result["questions"]["Q2"] = {"correct": SECRET in s3["rawText"]}
            e4, ms4 = app.turn("Briefly explain your answer, then stop.")
            s4 = event_summary(e4)
            first_reason = next((x for x in e4 if x.get("method", "").startswith("item/reasoning/")), None)
            result["questions"]["Q3"] = {"summaryTextDelta": s4["reasoningSummary"], "textDelta": s4["reasoningText"],
                                           "firstMs": None if first_reason is None else round((first_reason["_received_mono"] - app.last_turn_start) * 1000)}
            e5, _ = app.turn("Do three steps: add a function to hello.py, add a test, and run the test.")
            s5 = event_summary(e5)
            result["questions"]["Q4"] = {"planSteps": s5["planSteps"], "planStatus": s5["planStatus"]}
            compact = app.rpc("thread/compact/start", {"threadId": app.thread_id}, timeout=TIMEOUT)
            ec = app.wait_completed()
            sc = event_summary(ec)
            result["questions"]["Q5"] = {"return": compact, "compactTurnMethods": sc["methods"],
                                         "compactItemTypes": sc["compactionItemTypes"], "compactUsage": sc["usage"]}
            e6, _ = app.turn("After compaction, repeat the passphrase.")
            s6 = event_summary(e6)
            result["questions"]["Q5"].update({"notifications": s6["methods"], "itemTypes": s6["compactionItemTypes"],
                                               "correct": SECRET in s6["rawText"], "usage": s6["usage"],
                                               "lastBefore": usage_last(s5), "lastAfter": usage_last(s6),
                                               "lastDecreased": usage_last(s5) is not None and usage_last(s6) is not None and usage_last(s6) < usage_last(s5)})
            all_statuses = list(app.statuses)
            app.rpc("thread/unsubscribe", {"threadId": app.thread_id}, timeout=30)
            old_thread = app.thread_id
            app.close()
            app = AppServer(cwd, log_path, append=True)
            app.start(old_thread)
            app.thread_id = app.thread_id or old_thread
            e7, _ = app.turn("After resuming, repeat the passphrase.")
            result["questions"]["Q6"] = {"correct": SECRET in event_summary(e7)["rawText"]}
            all_statuses.extend(app.statuses)
            result["questions"]["Q7"] = {"states": all_statuses}
            result["statusValues"] = all_statuses
        finally:
            app.close()
    if out_json:
        Path(out_json).write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print("Q 编号 | 结论 | 关键数字")
    for key, value in result["questions"].items():
        print(f"{key} | {json.dumps(value, ensure_ascii=False, separators=(',', ':'))[:180]}")
    print(f"原始收发 | {log_path}")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--out-json")
    args = parser.parse_args()
    if args.dry_run:
        dry_run()
        return 0
    try:
        return run(args.out_json)
    except Exception as exc:
        print(f"失败 | {type(exc).__name__}: {exc}")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
