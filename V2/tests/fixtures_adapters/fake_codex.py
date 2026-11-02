#!/usr/bin/env python3
"""假的 `codex app-server --listen stdio://`（测试专用；绝不联网、绝不调用真的 Codex）。

按行读 stdin 里的 JSON-RPC（省略 jsonrpc 字段），按行写 stdout。每条收到的消息都记到 $FAKE_LOG
（一行一条 JSON），方便测试断言参数和顺序。通知内容回放 fixtures_adapters/codex_*.jsonl
（从 ~/.xay/v2/probes 的真实录像里截出来并脱敏）。

提示词里的指令（[[...]]）决定这一轮演什么：
    [[capacity]]  回放"Selected model is at capacity"失败的一轮
    [[tools]]     回放带命令 / 文件改动 / 多次 tokenUsage 的一轮
    [[reasoning]] 发思考摘要增量 + 计划更新
    [[autocompact]] 这一轮中间 Codex 自己做了一次压缩（回放压缩录像里的条目）
    [[slow]]      先发一点内容，然后等 steer / interrupt 再收尾
    [[approval]]  向客户端发一个命令审批请求，等回复后收尾
    [[used=N]]    把 tokenUsage 的 last.totalTokens 改成 N
    [[window=W]]  把 modelContextWindow 改成 W
没有指令：回放 codex_turn_ok.jsonl（回复 OK）。
环境变量：FAKE_LOG 日志文件；FAKE_COMPACT_DELAY 压缩那一轮的耗时（秒，默认 0.3）。
"""
import copy
import json
import os
import re
import sys
import threading
import time

HERE = os.path.dirname(os.path.abspath(__file__))
LOG = os.environ.get("FAKE_LOG")
OUT_LOCK = threading.Lock()
LOG_LOCK = threading.Lock()

STATE = {"thread": "thr-fake-1", "turn_n": 0, "active": None, "compacting": False,
         "steer": threading.Event(), "interrupt": threading.Event(), "approval": threading.Event()}


def log(record):
    if not LOG:
        return
    record["t"] = time.time()
    with LOG_LOCK:
        with open(LOG, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(record, ensure_ascii=False) + "\n")


def out(message):
    if message.get("method") == "turn/completed":
        STATE["active"] = None   # 先放开，再发 turn/completed：客户端收到后马上发下一轮不会撞上"还在忙"
    with OUT_LOCK:
        sys.stdout.write(json.dumps(message, ensure_ascii=False) + "\n")
        sys.stdout.flush()


def load(name):
    with open(os.path.join(HERE, name), encoding="utf-8") as fh:
        return [json.loads(line) for line in fh if line.strip()]


def subst(value, mapping):
    if isinstance(value, str):
        for key, replacement in mapping.items():
            value = value.replace(key, replacement)
        return value
    if isinstance(value, list):
        return [subst(v, mapping) for v in value]
    if isinstance(value, dict):
        return {k: subst(v, mapping) for k, v in value.items()}
    return value


def directive(text, name, default=None):
    match = re.search(r"\[\[%s(?:=([^\]]*))?\]\]" % re.escape(name), text or "")
    if not match:
        return default
    return match.group(1) if match.group(1) is not None else True


def replay(name, turn_id, text="", pause=0.01):
    mapping = {"{THREAD}": STATE["thread"], "{TURN}": turn_id}
    used, window = directive(text, "used"), directive(text, "window")
    for message in load(name):
        message = subst(copy.deepcopy(message), mapping)
        params = message.get("params", {})
        usage = params.get("tokenUsage")
        if usage:
            if used:
                usage["last"]["totalTokens"] = int(used)
            if window:
                usage["modelContextWindow"] = int(window)
        out(message)
        time.sleep(pause)


def run_turn(text, turn_id):
    try:
        if directive(text, "capacity"):
            replay("codex_turn_capacity.jsonl", turn_id, text)
        elif directive(text, "tools"):
            replay("codex_turn_tools.jsonl", turn_id, text)
        elif directive(text, "reasoning"):
            thread = STATE["thread"]
            out({"method": "turn/started", "params": {"threadId": thread, "turn": {"id": turn_id, "status": "inProgress"}}})
            for delta in ("Thinking about ", "the plan."):
                out({"method": "item/reasoning/summaryTextDelta", "params": {
                    "threadId": thread, "turnId": turn_id, "itemId": "rs-1", "summaryIndex": 0, "delta": delta}})
            out({"method": "turn/plan/updated", "params": {"threadId": thread, "turnId": turn_id, "plan": [
                {"step": "write test", "status": "inProgress"}, {"step": "run test", "status": "pending"}]}})
            out({"method": "item/completed", "params": {"threadId": thread, "turnId": turn_id, "item": {
                "type": "agentMessage", "id": "msg-1", "text": "done", "phase": "final_answer"}}})
            out({"method": "turn/completed", "params": {"threadId": thread, "turn": {"id": turn_id, "status": "completed", "error": None}}})
        elif directive(text, "autocompact"):
            thread = STATE["thread"]
            mapping = {"{THREAD}": thread, "{TURN}": turn_id}
            out({"method": "turn/started", "params": {"threadId": thread, "turn": {"id": turn_id, "status": "inProgress"}}})
            for message in load("codex_compaction.jsonl"):
                if message["method"] in ("item/started", "thread/tokenUsage/updated", "item/completed"):
                    out(subst(copy.deepcopy(message), mapping))
                    time.sleep(0.01)
            out({"method": "item/completed", "params": {"threadId": thread, "turnId": turn_id, "item": {
                "type": "agentMessage", "id": "msg-1", "text": "done", "phase": "final_answer"}}})
            out({"method": "turn/completed", "params": {"threadId": thread, "turn": {"id": turn_id, "status": "completed", "error": None}}})
        elif directive(text, "slow"):
            thread = STATE["thread"]
            out({"method": "turn/started", "params": {"threadId": thread, "turn": {"id": turn_id, "status": "inProgress"}}})
            out({"method": "item/agentMessage/delta", "params": {"threadId": thread, "turnId": turn_id, "itemId": "msg-1", "delta": "working"}})
            deadline = time.time() + 10
            status = "completed"
            while time.time() < deadline:
                if STATE["interrupt"].is_set():
                    status = "interrupted"
                    break
                if STATE["steer"].is_set():
                    break
                time.sleep(0.01)
            out({"method": "item/completed", "params": {"threadId": thread, "turnId": turn_id, "item": {
                "type": "agentMessage", "id": "msg-1", "text": "working", "phase": "final_answer"}}})
            out({"method": "turn/completed", "params": {"threadId": thread, "turn": {
                "id": turn_id, "status": status, "error": None}}})
        elif directive(text, "approval"):
            thread = STATE["thread"]
            out({"method": "turn/started", "params": {"threadId": thread, "turn": {"id": turn_id, "status": "inProgress"}}})
            out({"id": "srv-1", "method": "item/commandExecution/requestApproval", "params": {
                "threadId": thread, "turnId": turn_id, "itemId": "exec-1", "command": "rm -rf /tmp/never"}})
            STATE["approval"].wait(3)
            out({"method": "turn/completed", "params": {"threadId": thread, "turn": {"id": turn_id, "status": "completed", "error": None}}})
        else:
            replay("codex_turn_ok.jsonl", turn_id, text)
    finally:
        STATE["active"] = None


def run_compaction(turn_id):
    delay = float(os.environ.get("FAKE_COMPACT_DELAY", "0.3"))
    messages = load("codex_compaction.jsonl")
    mapping = {"{THREAD}": STATE["thread"], "{TURN}": turn_id}
    for index, message in enumerate(messages):
        message = subst(copy.deepcopy(message), mapping)
        if message["method"] == "turn/completed":
            time.sleep(delay)
            STATE["compacting"] = False
            log({"event": "compact_completing"})   # 先记账，再发 turn/completed：测试靠它判断先后
        out(message)
        time.sleep(0.01 if index else 0.05)


def effort_from_argv():
    args = sys.argv
    for index, arg in enumerate(args):
        if arg == "-c" and index + 1 < len(args) and args[index + 1].startswith("model_reasoning_effort="):
            return args[index + 1].split("=", 1)[1].strip('"')
    return None


def handle(message):
    method, mid, params = message.get("method"), message.get("id"), message.get("params") or {}
    if method is None:
        # 客户端对我们（服务端）请求的回复，比如审批
        if mid == "srv-1":
            STATE["approval"].set()
        return
    if method == "initialized":
        return
    if method == "initialize":
        out({"id": mid, "result": {"userAgent": "fake-codex/0.165.0", "codexHome": "/tmp/fake", "platformFamily": "unix"}})
    elif method in ("thread/start", "thread/resume"):
        if method == "thread/resume":
            STATE["thread"] = params["threadId"]
        thread = {"id": STATE["thread"], "modelProvider": params.get("modelProvider"),
                  "model": params.get("model") or "gpt-fake", "reasoningEffort": effort_from_argv()}
        out({"id": mid, "result": {"thread": thread}})
        out({"method": "thread/started", "params": {"thread": thread}})
    elif method == "model/list":
        out({"id": mid, "result": {"data": [{"model": "gpt-fake", "isDefault": True}]}})
    elif method == "turn/start":
        if STATE["compacting"]:
            out({"id": mid, "error": {"code": -32000, "message": "ActiveTurnNotSteerable { turn_kind: Compact }"}})
        elif STATE["active"]:
            out({"id": mid, "error": {"code": -32000, "message": "turn already active"}})
        else:
            STATE["turn_n"] += 1
            turn_id = "turn-fake-%d" % STATE["turn_n"]
            STATE["active"] = turn_id
            STATE["steer"].clear()
            STATE["interrupt"].clear()
            out({"id": mid, "result": {"turn": {"id": turn_id, "status": "inProgress"}}})
            text = "".join(part.get("text", "") for part in params.get("input", []))
            threading.Thread(target=run_turn, args=(text, turn_id), daemon=True).start()
    elif method == "thread/compact/start":
        if STATE["active"] or STATE["compacting"]:
            out({"id": mid, "error": {"code": -32000, "message": "busy"}})
        else:
            STATE["turn_n"] += 1
            STATE["compacting"] = True
            out({"id": mid, "result": {}})
            threading.Thread(target=run_compaction, args=("turn-fake-%d" % STATE["turn_n"],), daemon=True).start()
    elif method == "turn/steer":
        if STATE["active"] and params.get("expectedTurnId") == STATE["active"]:
            STATE["steer"].set()
            out({"id": mid, "result": {"turnId": STATE["active"]}})
        else:
            out({"id": mid, "error": {"code": -32000, "message": "expectedTurnId mismatch"}})
    elif method == "turn/interrupt":
        if STATE["active"] and params.get("turnId") == STATE["active"]:
            STATE["interrupt"].set()
            out({"id": mid, "result": {}})
        else:
            out({"id": mid, "error": {"code": -32000, "message": "no such turn"}})
    elif method == "thread/unsubscribe":
        out({"id": mid, "result": {"status": "unsubscribed"}})
    else:
        out({"id": mid, "error": {"code": -32601, "message": "unknown method %s" % method}})


def main():
    log({"argv": sys.argv[1:], "cwd": os.getcwd()})
    for line in sys.stdin:
        if os.getppid() == 1:
            break
        line = line.strip()
        if not line:
            continue
        message = json.loads(line)
        log({"in": message})
        handle(message)
    log({"event": "stdin_eof"})


if __name__ == "__main__":
    main()
