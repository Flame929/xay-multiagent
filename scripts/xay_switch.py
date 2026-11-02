#!/usr/bin/env python3
"""xay 命令分发（M5 切换，references/plans/arch-v10-rebuild.md §6）。

M5 起 `scripts/xay` 默认转给新版 V2（`V2/bin/xay2`），但不能打断还在用旧命令的负责人会话和旧执行者：
- 只有旧版才有的子命令（session / ctl / decide / doctor / status / quota / agy-accounts …）照旧走旧版 `xay_cli.py`；
- 新版才有的（begin / plan / accept / brief / office …）走新版；
- 两边同名的（wait / dept / answer / ask / tell / reply）看参数长得像哪边：写了 --session、编号是 m_ 开头、
  `dept create` 等 → 旧版；--since、lt_/dc_/pp_ 编号、`dept open` 等 → 新版；还分不出来就看这个会话是不是旧系统的
  （设了 XAY_LEAD_SESSION、写了 --session、或这个 Claude 会话在旧系统里开过还没结束的会话），否则默认新版；
  这种"猜"出来的走法没跑成功时，会再提示另一边的写法。
- 执行者：旧系统派的活（XAY_ROLE=executor，没有 XAY2_* 环境变量）全部走旧版；新版工位派的活走新版。
- 强制：`xay v1 <命令…>` / `xay v2 <命令…>`；全部退回旧版：`XAY_V1=1`。看会走哪边：`xay --which <命令…>`。
"""
import json
import os
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
V1_CLI = HERE / "xay_cli.py"
V2_BIN = HERE.parent / "V2" / "bin" / "xay2"

# 旧版 xay_cli.py 的全部子命令里，新版没有的
V1_ONLY = frozenset({
    "session", "ctl", "decide", "doctor", "lead", "worker", "workers", "send", "status", "quota", "route",
    "agy-accounts", "agy-switch", "escalate", "report", "inbox", "contract", "supervise",
})
# 新版 xay2 有、旧版没有的（不在两张表里的新命令也默认走新版）
V2_ONLY = frozenset({
    "begin", "hire", "plan", "accept", "steer", "interrupt", "stop", "resume", "brief", "snapshot", "events",
    "office", "rebuild", "station", "progress", "done", "notes", "gc", "compact",
})
BOTH = frozenset({"wait", "dept", "answer", "ask", "tell", "reply"})
V2_IDS = ("pp_", "lt_", "dc_")
V1_IDS = ("m_",)
V1_VERDICTS = ("accept", "modify", "reject")
V2_EXECUTOR_ENV = ("XAY2_TASK_FILE", "XAY2_TASK", "XAY2_OUTBOX", "XAY2_EM")
TRUE = ("1", "true", "yes", "on")

HELP = """xay：M5 起默认用新版（V2，和 xay2 是同一套命令）。
  新版命令：begin dept plan answer ask tell reply accept steer interrupt stop resume wait brief snapshot events office rebuild
  只有旧版有的命令照旧走旧版：session ctl decide doctor status quota route agy-accounts agy-switch workers send inbox supervise …
  两边都有的（wait / dept / answer / ask / tell / reply）看参数：写了 --session、编号 m_ 开头、dept create 等 → 旧版；
    --since、编号 lt_ / dc_ / pp_ 开头、dept open 等 → 新版；分不出来时，旧系统的会话走旧版，其余走新版。
  强制走某一边：xay v1 <命令…>   xay v2 <命令…>
  全部退回旧版：XAY_V1=1 xay <命令…>
  看一条命令会走哪边：xay --which <命令…>"""


def _has(args, *flags):
    """args 里有没有这些选项（--x 或 --x=…）。"""
    for a in args:
        for f in flags:
            if a == f or a.startswith(f + "="):
                return True
    return False


def _opt(args, flag):
    """取 --flag 的值（--flag v 或 --flag=v），没有返回 None。"""
    for i, a in enumerate(args):
        if a == flag and i + 1 < len(args):
            return args[i + 1]
        if a.startswith(flag + "="):
            return a.split("=", 1)[1]
    return None


def _positionals(args):
    """粗略取位置参数（跳过 --x 以及紧跟的值；--x=v 整个跳过）。只用来看第一、二个位置参数长什么样。"""
    out, skip = [], False
    takes_value = {"--session", "--phase", "--timeout", "--since", "--name", "--mission", "--owns", "--head",
                   "--contract", "--wait", "--subject", "--text", "--id", "--version", "--conv"}
    for a in args:
        if skip:
            skip = False
            continue
        if a.startswith("--"):
            if "=" not in a and a in takes_value:
                skip = True
            continue
        out.append(a)
    return out


def _v1_session_open(env):
    """这个 Claude 会话在旧系统里开过、还没结束的负责人会话（xay session begin）。"""
    native = (env.get("CLAUDE_CODE_SESSION_ID") or "").strip()
    if not native:
        return False
    home = Path(env.get("XAY_HOME") or (Path.home() / ".xay")).expanduser()
    try:
        files = sorted((home / "sessions").glob("ls_*.json"), reverse=True)
    except OSError:
        return False
    want = "claude:" + native
    for path in files[:200]:
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if isinstance(data, dict) and data.get("lead_key") == want and not data.get("ended"):
            return True
    return False


def _context(rest, env):
    """分不出来时：旧系统的会话走旧版，其余默认新版。返回 (目标, 理由)。"""
    if _has(rest, "--session"):
        return "v1", "写了 --session（旧系统的会话编号）"
    if (env.get("XAY_LEAD_SESSION") or "").strip():
        return "v1", "设了 XAY_LEAD_SESSION（旧系统的会话）"
    if _v1_session_open(env):
        return "v1", "这个 Claude 会话在旧系统里开着会话（xay session begin）"
    return "v2", "默认用新版"


def route(argv, env=None):
    """决定走哪边。返回 (target, argv, reason, guessed)：target 是 'v1' / 'v2' / 'help'；
    argv 是要交给那一边的参数（去掉了开头的 v1 / v2）；guessed=True 表示是按会话"猜"的，没跑成功要提示另一边的写法。"""
    env = os.environ if env is None else env
    argv = list(argv)
    if not argv or argv[0] in ("-h", "--help", "help"):
        return "help", argv, "看用法", False
    if argv[0] in ("v1", "--v1"):
        return "v1", argv[1:], "写了 xay v1", False
    if argv[0] in ("v2", "--v2"):
        return "v2", argv[1:], "写了 xay v2", False
    if (env.get("XAY_V1") or "").strip().lower() in TRUE:
        return "v1", argv, "设了 XAY_V1=1（全部退回旧版）", False
    if env.get("XAY_ROLE") == "executor":
        if any((env.get(k) or "").strip() for k in V2_EXECUTOR_ENV):
            return "v2", argv, "新版工位派的活（有 XAY2_* 环境变量）", False
        return "v1", argv, "旧系统派的活（执行者，没有 XAY2_* 环境变量）", False
    if argv[0] == "--conv" or argv[0].startswith("--conv="):
        return "v2", argv, "写了 --conv（新版的对话编号）", False
    cmd, rest = argv[0], argv[1:]
    if cmd in V1_ONLY:
        return "v1", argv, "「%s」只有旧版有" % cmd, False
    if cmd in V2_ONLY or cmd not in BOTH:
        return "v2", argv, "「%s」是新版命令" % cmd, False
    pos = _positionals(rest)
    first = pos[0] if pos else ""
    if cmd == "wait":
        if _has(rest, "--session", "--phase"):
            return "v1", argv, "wait 写了 --session / --phase（旧版写法）", False
        if _has(rest, "--since"):
            return "v2", argv, "wait 写了 --since（新版写法）", False
        return "v2", argv, "wait 没写 --session（新版写法）", True
    if cmd == "dept":
        if first in ("create", "list"):
            return "v1", argv, "dept %s 是旧版写法（新版用 dept open）" % first, False
        if first == "open":
            return "v2", argv, "dept open 是新版写法", False
        if _has(rest, "--json") or _opt(rest, "--head") == "claude":
            return "v1", argv, "dept 写了 --json / --head claude（旧版写法）", False
        target, why = _context(rest, env)
        return target, argv, "dept %s：%s" % (first or "", why), True
    if cmd in ("answer", "reply"):
        if first.startswith(V2_IDS):
            return "v2", argv, "编号 %s 是新版的" % first[:3], False
        if first.startswith(V1_IDS):
            return "v1", argv, "编号 m_ 开头是旧版的", False
        if cmd == "reply" and _has(rest, "--subject"):
            return "v2", argv, "reply 写了 --subject（新版写法）", False
        if cmd == "answer" and len(pos) >= 2 and pos[1] not in V1_VERDICTS:
            return "v2", argv, "answer 的选项「%s」只有新版认" % pos[1], False
        target, why = _context(rest, env)
        return target, argv, "%s：%s" % (cmd, why), True
    if cmd == "ask":
        if _has(rest, "--contract", "--wait"):
            return "v1", argv, "ask 写了 --contract / --wait（旧版写法）", False
        if _has(rest, "--subject"):
            return "v2", argv, "ask 写了 --subject（新版写法）", False
        target, why = _context(rest, env)
        return target, argv, "ask：%s" % why, True
    # tell
    if _has(rest, "--subject") or first.startswith("em_"):
        return "v2", argv, "tell 写了 --subject 或员工编号 em_（新版写法）", False
    target, why = _context(rest, env)
    return target, argv, "tell：%s" % why, True


def _command(target, argv):
    if target == "v1":
        return [sys.executable, str(V1_CLI), *argv]
    return [str(V2_BIN), *argv]


def _legacy_notice(argv, env=None):
    env = os.environ if env is None else env
    return ((env.get("XAY_V1") or "").strip().lower() in TRUE
            or bool(argv and argv[0] in ("v1", "--v1")))


def _other_hint(target, argv):
    other = "v1" if target == "v2" else "v2"
    name = "旧版" if other == "v1" else "新版"
    shown = " ".join(argv[:4]) + (" …" if len(argv) > 4 else "")
    lines = ["xay：这条是按会话猜着交给%s的；如果你要的是%s，写成：xay %s %s" % (
        "新版" if target == "v2" else "旧版", name, other, shown)]
    if target == "v2" and argv and argv[0] == "wait":
        lines.append("      旧系统的等待要带会话编号：xay wait --session <ls_…> --timeout 240")
    return "\n".join(lines)


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    if argv and argv[0] == "--which":
        target, rest, reason, guessed = route(argv[1:])
        print("%s：%s%s" % ({"v1": "旧版", "v2": "新版", "help": "用法"}[target], reason, "（按会话猜的）" if guessed else ""))
        return 0
    target, rest, reason, guessed = route(argv)
    if target == "help":
        print(HELP)
        return 0
    if target == "v1" and _legacy_notice(argv):
        print("提示：旧版（V1）已标为 legacy，只作退路，不再更新。", file=sys.stderr)
    cmd = _command(target, rest)
    if not guessed:
        try:
            os.execv(cmd[0], cmd)
        except OSError as exc:
            print("xay：没能启动%s（%s）：%s" % ("旧版" if target == "v1" else "新版", cmd[0], exc), file=sys.stderr)
            return 2
    try:
        code = subprocess.call(cmd)
    except KeyboardInterrupt:
        return 130
    except OSError as exc:
        print("xay：没能启动%s（%s）：%s" % ("旧版" if target == "v1" else "新版", cmd[0], exc), file=sys.stderr)
        return 2
    if code == 2:
        print(_other_hint(target, rest), file=sys.stderr)
    return code


if __name__ == "__main__":
    raise SystemExit(main())
