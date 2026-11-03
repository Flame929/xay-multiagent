#!/usr/bin/env python3
"""假通用命令行 AI 脚本（测试专用，绝不联网）。

用于在 test_generic_adapter.py 中测试 GenericAdapter：
- 支持 text 和 jsonl 两种输出模式；
- 支持 --session 续接会话；
- 支持只读参数校验；
- 支持根据指令输出各类错误信息及非零退出码。
"""

import json
import os
import re
import sys
import time

LOG_PATH = os.environ.get("FAKE_GENERIC_LOG")


def log_invocation():
    if LOG_PATH:
        rec = {
            "argv": sys.argv,
            "cwd": os.getcwd(),
            "time": time.time(),
        }
        with open(LOG_PATH, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(rec, ensure_ascii=False) + "\n")


def main():
    log_invocation()
    raw_args = sys.argv[1:]

    # 提取 session 参数
    session_id = None
    for i, a in enumerate(raw_args):
        if a == "--session" and i + 1 < len(raw_args):
            session_id = raw_args[i + 1]
        elif a.startswith("--session="):
            session_id = a.split("=", 1)[1]

    # 读取输入 prompt
    prompt_text = ""
    for a in raw_args:
        if a.startswith("--") or a.startswith("-"):
            continue
        # 如果是文件路径且存在
        if os.path.isfile(a):
            try:
                with open(a, "r", encoding="utf-8") as fh:
                    prompt_text += fh.read() + " "
            except OSError:
                pass
        else:
            prompt_text += a + " "

    # 检查是否要求出错退出
    if "[[quota]]" in prompt_text:
        sys.stderr.write("RESOURCE_EXHAUSTED: daily quota exceeded, please check billing\n")
        sys.exit(1)
    if "[[rate_limit]]" in prompt_text:
        sys.stderr.write("429 Too Many Requests: rate limit exceeded\n")
        sys.exit(1)
    if "[[auth]]" in prompt_text:
        sys.stderr.write("401 Unauthorized: invalid api key or token expired\n")
        sys.exit(1)
    if "[[network]]" in prompt_text:
        sys.stderr.write("fetch failed: connection reset by peer (ECONNRESET)\n")
        sys.exit(1)
    if "[[other]]" in prompt_text:
        sys.stderr.write("RuntimeError: unknown crash\n")
        sys.exit(2)

    # 判断输出模式
    is_json = ("--format" in raw_args and "json" in raw_args) or ("--json" in raw_args)
    if "[[text]]" in prompt_text:
        is_json = False

    sid = session_id or "generic-sess-999"

    if is_json:
        # jsonl 模式
        event1 = {"session_id": sid, "text": "思考中...\n"}
        event2 = {"text": "已完成代码编写与测试验证。\n", "usage": {"total_tokens": 128}}
        sys.stdout.write(json.dumps(event1, ensure_ascii=False) + "\n")
        sys.stdout.flush()
        sys.stdout.write(json.dumps(event2, ensure_ascii=False) + "\n")
        sys.stdout.flush()
    else:
        # text 模式
        sys.stdout.write("【纯文本输出】任务执行完毕，所有改动已保存。\n")
        sys.stdout.flush()

    sys.exit(0)


if __name__ == "__main__":
    main()
