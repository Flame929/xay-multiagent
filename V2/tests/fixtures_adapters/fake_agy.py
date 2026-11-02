#!/usr/bin/env python3
"""假的 `agy --input-format stream-json --output-format stream-json ... -p=`（测试专用；绝不联网）。

常驻进程：每读到 stdin 的一行 {"event":"user",…} 就演一轮（回放 fixtures_adapters/agy_*.jsonl，
来自真实录像，脱敏）。一直读到 stdin 关闭才退出。所有动静写进 $FAKE_LOG：
    {"argv":…, "cwd":…, "pid":…, "settings": <--settings 文件内容>}   启动时
    {"stdin": {...}, "pid": …}                                         每收到一行
    {"event": "stdin_eof", "pid": …}                                   输入被关闭时

提示词里的指令：
    [[tools]]    回放带 run_command 工具的一轮
    [[silent]]   收到后什么都不回（测试 30 秒无动作的续接失败）
    [[idle]]     马上回一个空的 result（没有任何步骤；旧系统的"空转就结束"）
    [[quota]]    发 {"event":"error"} 报额度用完，再发失败的 result
    [[overload]] 发 {"event":"error"} 报容量满载，再发失败的 result
    [[total=N]]  把步骤 usage.input_tokens 改成 N
    [[cache=N]]  步骤 usage 里带 cache_read_tokens: N（agy 的 input_tokens 只算新内容）
    /compact     （整行就是它）像真 agy 一样：user_input + checkpoint 步骤 + SUCCESS 的空 result
环境变量：FAKE_AGY_INIT=lazy 表示收到第一行输入后才发 init（默认启动时就发）；
FAKE_AGY_COMPACT=fail 表示 /compact 没有 checkpoint、result 是 ERROR；
FAKE_AGY_NEW_CONV=1 表示 --conversation 给的对话找不到，init 里换成一个新对话号。
没有指令：回放 agy_turn.jsonl（回复 OK）。
"""
import copy
import json
import os
import re
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
LOG = os.environ.get("FAKE_LOG")
PID = os.getpid()


def log(record):
    if LOG:
        record["t"] = time.time()
        record.setdefault("pid", PID)
        with open(LOG, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(record, ensure_ascii=False) + "\n")


def load(name):
    with open(os.path.join(HERE, name), encoding="utf-8") as fh:
        return [json.loads(line) for line in fh if line.strip()]


def directive(text, name):
    match = re.search(r"\[\[%s(?:=([^\]]*))?\]\]" % re.escape(name), text or "")
    if not match:
        return None
    return match.group(1) if match.group(1) is not None else True


def emit(event):
    sys.stdout.write(json.dumps(event, ensure_ascii=False) + "\n")
    sys.stdout.flush()


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


def arg_after(args, flag):
    return args[args.index(flag) + 1] if flag in args else None


def main():
    args = sys.argv[1:]
    conversation = arg_after(args, "--conversation") or "conv-fake-%d" % int(time.time() * 1000)
    if os.environ.get("FAKE_AGY_NEW_CONV"):      # 要接的对话找不到，悄悄开了个新的
        conversation = "conv-fake-new-%d" % int(time.time() * 1000)
    settings_path = arg_after(args, "--settings")
    settings = None
    if settings_path and os.path.exists(settings_path):
        with open(settings_path, encoding="utf-8") as fh:
            settings = json.load(fh)
    log({"argv": args, "cwd": os.getcwd(), "settings": settings, "conversation": conversation})
    mapping = {"{CONV}": conversation, "{CWD}": os.getcwd()}
    index_base = 0
    sent_init = False

    def send_init():
        nonlocal sent_init
        if not sent_init:
            emit(subst(copy.deepcopy(load("agy_turn.jsonl")[0]), mapping))
            sent_init = True

    if os.environ.get("FAKE_AGY_INIT") != "lazy":
        send_init()
    for line in sys.stdin:
        if os.getppid() == 1:
            break
        line = line.strip()
        if not line:
            continue
        message = json.loads(line)
        log({"stdin": message})
        text = (message.get("message") or {}).get("content", "")
        send_init()
        if text.strip() == "/compact":
            ok = os.environ.get("FAKE_AGY_COMPACT") != "fail"
            emit({"event": "step_update", "step_update": {"conversation_id": conversation, "step_index": index_base,
                                                          "state": "DONE", "step_type": "user_input"}})
            if ok:
                emit({"event": "step_update", "step_update": {"conversation_id": conversation,
                                                              "step_index": index_base + 1, "state": "DONE",
                                                              "step_type": "checkpoint", "duration_seconds": 0.01}})
            emit({"event": "result", "result": {"conversation_id": conversation,
                                                 "status": "SUCCESS" if ok else "ERROR", "response": ""}})
            index_base += 10
            continue
        if directive(text, "silent"):
            continue
        if directive(text, "idle"):
            emit({"event": "result", "result": {"conversation_id": conversation, "status": "SUCCESS",
                                                 "response": "", "num_turns": 0}})
            continue
        if directive(text, "quota") or directive(text, "overload"):
            reason = ("RESOURCE_EXHAUSTED: You have exhausted your capacity on this model. Your quota will reset after 1h."
                      if directive(text, "quota") else "503 Service Unavailable: the model is overloaded, try again later")
            emit({"event": "error", "error": reason})
            emit({"event": "result", "result": {"conversation_id": conversation, "status": "ERROR", "response": ""}})
            continue
        name = "agy_tool_turn.jsonl" if directive(text, "tools") else "agy_turn.jsonl"
        total = directive(text, "total")
        cache = directive(text, "cache")
        for event in load(name)[1:]:     # 第 0 条是 init，已经发过
            event = subst(copy.deepcopy(event), mapping)
            step = event.get("step_update")
            if step is not None:
                step["step_index"] = step.get("step_index", 0) + index_base
                if total and isinstance(step.get("usage"), dict):
                    step["usage"]["input_tokens"] = int(total)
                if cache and isinstance(step.get("usage"), dict):
                    step["usage"]["cache_read_tokens"] = int(cache)
            emit(event)
            time.sleep(0.005)
        index_base += 10
    log({"event": "stdin_eof"})


if __name__ == "__main__":
    main()
