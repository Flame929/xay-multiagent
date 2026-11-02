#!/usr/bin/env python3
"""假的 `dsh --patch <文件> --json [--session-id ID] <提示词>`（测试专用；绝不联网、绝不调用真的 dsh）。

一次运行 = 一轮，stdout 回放 fixtures_adapters/dsh_*.jsonl（来自真实录像，脱敏）。
每次运行把 argv / cwd / 补丁文件内容 / 提示词写进 $FAKE_LOG。
轮次号用 $FAKE_STATE 目录里的 <会话>.turn 文件跨进程累加（和真 dsh 续接时一样）。

提示词里的指令：
    [[capacity]]      先发 session / turn_start，然后往 stderr 写容量错误并以 1 退出（没有 turn_end）
    [[error-event]]   turn_end 带 reason.kind=error 和 message
    [[tools]]         回放带 bash 工具调用的一轮
    [[think]]         回放带 thinking 的一轮
    [[total=N]]       把 step_end 的 totalTokens 改成 N
    [[slow]]          发完 turn_start 后睡 30 秒（等测试打断）
    [[patch-warn]]    往 stderr 写一句"我们的补丁 patch: entry … not found"，然后照常回放（dsh 真的就是这样）
没有指令：回放 dsh_turn_ok.jsonl（回复 OK）。

`--dump-config`（适配器开工前的预检）：打印一份像真 dsh 的配置（YAML 片段），不记 argv 行，只记 {"dump": …}。
    FAKE_DSH_BAD_PATCH=1   我们的补丁"没找到条目"：stderr 警告，agent-default-model 标题里没有我们的补丁
    FAKE_DSH_MODEL=<名字>  生效的模型改成它（模拟用户自己的配置把模型换掉）
    FAKE_DSH_CTX=<N>       路由里写上 contextWindow: N（不写就按 pi-ai 默认 262,144）
用户自己 profile 的"条目没找到"警告每次都会出现（真 dsh 也是），适配器必须只认我们自己的补丁。
`--session-id session-gone…`：像真 dsh 一样不认这个会话号（"was recorded in <旧目录>"），出 error 事件、退出码 1。
"""
import copy
import json
import os
import re
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
LOG = os.environ.get("FAKE_LOG")


def log(record):
    if LOG:
        record["t"] = time.time()
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


def next_turn(session):
    state_dir = os.environ.get("FAKE_STATE") or (os.path.dirname(LOG) if LOG else None)
    if not state_dir:
        return 1
    path = os.path.join(state_dir, session + ".turn")
    try:
        with open(path) as fh:
            turn = int(fh.read().strip() or 0) + 1
    except (OSError, ValueError):
        turn = 1
    with open(path, "w") as fh:
        fh.write(str(turn))
    return turn


def arg_after(args, flag):
    return args[args.index(flag) + 1] if flag in args else None


USER_PATCH = "/Users/someone/.dsh/profiles/headless/cordis.patch.yml"


def dump_config(args, patch_path, patch):
    sys.stderr.write('dsh: [%s] patch: entry "ui-chat" not found\n' % USER_PATCH)      # 用户自己的警告，不算
    bad = bool(os.environ.get("FAKE_DSH_BAD_PATCH"))
    if bad:
        sys.stderr.write('dsh: [%s] patch: entry "agent-default-modle" not found\n' % patch_path)
    m = re.search(r"model: (\S+)", patch or "")
    model = os.environ.get("FAKE_DSH_MODEL") or (m.group(1) if (m and not bad) else "deepseek-v4.1-flash")
    e = re.search(r"reasoningEffort: (\S+)", patch or "")
    patched = USER_PATCH + ("" if bad else ", " + patch_path)
    ctx = os.environ.get("FAKE_DSH_CTX")
    lines = [
        "# == @deepseek-ai/dsh-base",
        "- id: agent",
        "  name: '@deepseek-ai/dsh-agent'",
        "# == @deepseek-ai/dsh-base, patched by %s" % patched,
        "- id: agent-default-model",
        "  name: '@deepseek-ai/dsh-agent-default-model'",
        "  config:",
        "    provider: opencode-xay",
        "    model: %s" % model,
        "    reasoningEffort: %s" % ((e.group(1) if (e and not bad) else "xhigh")),
        "# == @deepseek-ai/dsh-base, patched by %s" % USER_PATCH,
        "- id: llm-pi-ai",
        "  name: '@deepseek-ai/dsh-llm-pi-ai'",
        "  config:",
        "    providers:",
        "      opencode-xay:",
        "        displayName: opencode-xay",
        "        api: openai-completions",
        "        models:",
        "          - id: deepseek-v4-pro",
        "            name: DeepSeek V4 Pro (opencode-xay)",
        "            contextWindow: 999",
        "          - id: deepseek-v4.1-flash",
        "            name: DeepSeek V4.1 Flash (opencode-xay)",
    ] + (["            contextWindow: %s" % ctx] if ctx else []) + [
        "            compat:",
        "              supportsReasoningEffort: true",
        "# == @deepseek-ai/dsh-base",
        "- id: compaction-basic",
        "  name: '@deepseek-ai/dsh-compaction-basic'",
        "- id: subagent",
        "  config:",
        "    - id: compaction-basic",
        "      config:",
        "        thresholdRatio: 0.55",
    ]
    log({"dump": args, "cwd": os.getcwd()})
    sys.stdout.write("\n".join(lines) + "\n")


def main():
    args = sys.argv[1:]
    prompt = args[-1] if args else ""
    patch_path = arg_after(args, "--patch")
    patch = ""
    if patch_path and os.path.exists(patch_path):
        with open(patch_path, encoding="utf-8") as fh:
            patch = fh.read()
    if "--dump-config" in args:
        dump_config(args, patch_path, patch)
        return
    sys.stderr.write('dsh: [%s] patch: entry "ui-chat" not found\n' % USER_PATCH)
    if directive(prompt, "patch-warn"):
        sys.stderr.write('dsh: [%s] patch: entry "agent-default-model" not found\n' % patch_path)
        sys.stderr.flush()
        time.sleep(3)      # 真 dsh 的警告在启动时；适配器应该在这期间就把它停下
    sid_arg = arg_after(args, "--session-id")
    if sid_arg and sid_arg.startswith("session-gone"):
        log({"argv": args[:-1], "prompt": prompt, "cwd": os.getcwd(), "patch": patch,
             "session_id_arg": sid_arg, "role": os.environ.get("XAY_ROLE")})
        message = "session %s was recorded in /old/desk, not %s" % (sid_arg, os.getcwd())
        sys.stderr.write("dsh: %s\n" % message)
        emit({"type": "error", "message": message})
        sys.exit(1)
    session = sid_arg or "session-fake-%d" % int(time.time() * 1000)
    log({"argv": args[:-1], "prompt": prompt, "cwd": os.getcwd(), "patch": patch,
         "session_id_arg": arg_after(args, "--session-id"), "role": os.environ.get("XAY_ROLE")})
    turn = next_turn(session)

    if directive(prompt, "tools"):
        name = "dsh_turn_tools.jsonl"
    elif directive(prompt, "think"):
        name = "dsh_turn_thinking.jsonl"
    else:
        name = "dsh_turn_ok.jsonl"
    total = directive(prompt, "total")
    for event in load(name):
        event = copy.deepcopy(event)
        for key, value in list(event.items()):
            if value == "{TURN}":
                event[key] = turn
            elif isinstance(value, str):
                event[key] = value.replace("{SESSION}", session).replace("{CWD}", os.getcwd())
        if total and isinstance(event.get("usage"), dict):
            event["usage"]["totalTokens"] = int(total)
        if directive(prompt, "error-event") and event.get("phase") == "turn_end":
            event["reason"] = {"kind": "error", "message": "provider returned an error"}
        if directive(prompt, "capacity") and event.get("phase") == "step_start":
            sys.stderr.write("Error: provider overloaded (503): the selected model is at capacity\n")
            sys.stderr.flush()
            sys.exit(1)
        emit(event)
        if directive(prompt, "slow") and event.get("phase") == "turn_start":
            time.sleep(30)


if __name__ == "__main__":
    main()
