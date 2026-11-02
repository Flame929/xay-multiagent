#!/usr/bin/env python3
"""dsh（DeepSeek Harness）能力探针：M0 探针日 Q1-Q5，由负责人手动运行。
每轮一个进程，照抄 scripts/xay_drivers/dsh_headless.py：临时补丁固定模型与
reasoningEffort=low，--json，--session-id 续接，XAY_ROLE=executor，在临时 git
仓库（hello.py）里跑。原始事件写 ~/.xay/v2/probes/dsh-<时间>.jsonl；Q5 的
--dump-config 只在内存里解析目标模型窗口数字，绝不打印或保存整份输出。
用法：python3 V2/probes/dsh_probe.py [--dry-run] [--out-json 路径] [--timeout 180]
"""
from __future__ import annotations
import argparse, json, os, re, shlex, shutil, signal, subprocess, sys, tempfile, time
from collections import Counter
from datetime import datetime
from pathlib import Path
MODEL = "deepseek-v4.1-flash"
EFFORT, ROUND_TIMEOUT = "low", 180
PROBE_HOME = Path.home() / ".xay" / "v2" / "probes"
ROUNDS = (
    ("r1_remember", "请记住暗号 PINEAPPLE-42。只回复 OK，不要调用工具，不要做别的事。", False),
    ("r2_recall", "暗号是什么？只回复暗号本身，不要解释。", True),
    ("r3_compact", "/compact", True),
    ("r4_recall", "暗号是什么？只回复暗号本身，不要解释。", True),
)
_SECRET_RE = re.compile(
    r"(?i)(sk-[A-Za-z0-9_-]{8,}|bearer\s+[A-Za-z0-9._-]{8,}"
    r"|api[_-]?key\s*[:=]\s*\S+|[A-Fa-f0-9]{40,})")
_WINDOW_KEY_RE = re.compile(
    r"(?i)(context|window|max[a-z_]*token|token[a-z_]*(max|limit)"
    r"|max[a-z_]*(input|output)|(input|output)[a-z_]*token"
    r"|context[a-z_]*limit|limit[a-z_]*context)")
def redact(text, limit=240):
    return _SECRET_RE.sub("***", str(text or "")).replace("\n", " ")[:limit]
def default_command():
    """XAY_DSH_BIN > dsh-run（带凭据的包装）> dsh --profile headless。"""
    if os.environ.get("XAY_DSH_BIN"):
        return [os.environ["XAY_DSH_BIN"]]
    wrapper = shutil.which("dsh-run")
    return [wrapper] if wrapper else [shutil.which("dsh") or "dsh", "--profile", "headless"]
def build_argv(patch_path, prompt, session_id=None):
    argv = default_command() + ["--patch", str(patch_path), "--json"]
    if session_id:
        argv += ["--session-id", str(session_id)]
    return argv + [str(prompt)]
def _kill(proc):
    for action in (lambda: os.killpg(proc.pid, signal.SIGKILL), proc.kill):
        try:
            action()
        except OSError:
            pass
def _run(argv, cwd, timeout=None, env=None):
    """返回 (stdout, stderr, code, timed_out)；超时杀整个进程组。"""
    started = time.monotonic()
    proc = subprocess.Popen(argv, cwd=str(cwd), stdin=subprocess.DEVNULL,
                            stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                            text=True, bufsize=1, env=env, start_new_session=True)
    try:
        out, err = proc.communicate(timeout=timeout)
        return out or "", err or "", proc.returncode, False
    except subprocess.TimeoutExpired:
        _kill(proc)
        left = max(0.1, (timeout or 0) - (time.monotonic() - started))
        try:
            out, err = proc.communicate(timeout=left)
        except subprocess.TimeoutExpired:
            _kill(proc)
            out, err = "", ""
        return out or "", err or "", proc.returncode, True
def _checked(argv, cwd):
    _, err, code, _ = _run(argv, cwd, 60, dict(os.environ))
    if code:
        raise SystemExit("命令失败：%s\n%s" % (shlex.join(argv), redact(err, 300)))
def make_repo():
    root = Path(tempfile.mkdtemp(prefix="xay-dsh-probe-"))
    (root / "hello.py").write_text('print("hello from xay dsh probe")\n', encoding="utf-8")
    _checked(["git", "init", "-q"], root)
    _checked(["git", "add", "hello.py"], root)
    _checked(["git", "-c", "user.name=xay-probe", "-c", "user.email=probe@example.invalid",
              "commit", "-qm", "probe: init"], root)
    return root
def make_patch(root):
    provider = os.environ.get("XAY_DSH_PROVIDER") or "opencode-xay"
    path = Path(root) / "dsh-model.patch.yml"
    path.write_text("- id: agent-default-model\n"
                    "  name: '@deepseek-ai/dsh-agent-default-model'\n"
                    "  config:\n"
                    "    provider: %s\n    model: %s\n    reasoningEffort: %s\n"
                    % (provider, MODEL, EFFORT), encoding="utf-8")
    return path
def run_round(argv, cwd, raw_path, name, timeout):
    """跑一轮；stdout 的原始事件追加进 raw_path，返回事件与退出信息。"""
    env = dict(os.environ)
    env.setdefault("XAY_ROLE", "executor")
    started, events = time.monotonic(), []
    out, err, code, timed_out = _run(argv, cwd, timeout, env)
    elapsed = round(time.monotonic() - started, 1)
    with open(raw_path, "a", encoding="utf-8") as fh:
        fh.write(json.dumps({"_probe_round": name}) + "\n")
        for line in out.splitlines():
            try:
                event = json.loads(line)
            except ValueError:
                continue
            if isinstance(event, dict):
                events.append(event)
                fh.write(line + "\n")
    return {"events": events, "exit_code": code, "timed_out": timed_out,
            "elapsed_s": elapsed, "stderr": bool(err.strip())}
def _final_text(events):
    for event in reversed(events):
        text = event.get("text") or event.get("message")
        if event.get("type") == "final" and isinstance(text, str) and text.strip():
            return text.strip()
    return "".join(e.get("text") or e.get("delta") or ""
                   for e in events if e.get("type") == "text").strip()
def summarize(raw, name, prompt, session_in):
    events = raw["events"]
    counts = Counter(str(e.get("type")) for e in events)
    session_out = session_in
    for event in events:
        if event.get("type") == "session" and event.get("sessionId"):
            session_out = str(event["sessionId"])
    steps = [{"turn": e.get("turn"), "usage": dict(e.get("usage") or {})}
             for e in events
             if e.get("type") == "status" and e.get("phase") == "step_end"]
    errors = []
    for event in events:
        reason = event.get("reason")
        if event.get("type") == "error":
            errors.append(redact(event.get("message") or event.get("error") or "error"))
        elif event.get("type") == "status" and event.get("phase") == "turn_end" \
                and isinstance(reason, dict) and reason.get("kind") == "error":
            errors.append(redact(reason.get("message") or "turn_end error"))
    return {"name": name, "prompt": prompt, "session_id_in": session_in,
            "session_id_out": session_out, "exit_code": raw["exit_code"],
            "timed_out": raw["timed_out"], "elapsed_s": raw["elapsed_s"],
            "final_text": _final_text(events)[:2000],
            "event_counts": dict(counts), "thinking_count": int(counts.get("thinking", 0)),
            "step_usage": steps, "errors": errors, "stderr": raw["stderr"]}
def _last_total(steps):
    values = (step["usage"].get("totalTokens") for step in reversed(steps))
    return next((int(v) for v in values if isinstance(v, (int, float)) and not isinstance(v, bool)), None)
def _numbers(node, path, out):
    """收集紧邻模型名（同级键值或路径含模型名）的窗口数字。"""
    if isinstance(node, dict):
        near = any(MODEL in str(k) for k in node) or any(
            isinstance(v, str) and MODEL in v for v in node.values())
        for key, value in node.items():
            child = path + [str(key)]
            if isinstance(value, (int, float)) and not isinstance(value, bool) \
                    and _WINDOW_KEY_RE.search(str(key)) and (near or MODEL in str(key)):
                out.append((str(key), ".".join(child), value))
            _numbers(value, child, out)
    elif isinstance(node, list):
        for index, value in enumerate(node):
            _numbers(value, path + [str(index)], out)
def _extract_text_config(text):
    """YAML/JSON 兜底：只扫描模型名之后、下一个模型名之前的数字。"""
    text, low, fields = text or "", (text or "").lower(), {}
    for match in re.finditer(re.escape(MODEL), text, re.I):
        end = low.find("deepseek-", match.end())
        segment = text[match.end():end if end != -1 else len(text)]
        for key, value in re.findall(r"([A-Za-z0-9_.-]+)\s*[\"']?\s*[:=]\s*(\d+)", segment):
            if _WINDOW_KEY_RE.search(key):
                fields[key] = int(value)
    return fields
def probe_window(dsh_bin):
    """只在内存里解析 --dump-config，只返回目标模型的窗口数字。"""
    try:
        out, err, code, _ = _run([dsh_bin, "--profile", "headless", "--dump-config"],
                                 Path.cwd(), 45, dict(os.environ))
    except OSError as exc:
        return {"status": "error", "detail": type(exc).__name__, "fields": {}}
    found = []
    try:
        _numbers(json.loads(out), [], found)
    except ValueError:
        pass
    fields = {}
    for key, path, value in found:
        fields[path if key in fields and fields[key] != value else key] = value
    if not fields:
        fields = _extract_text_config(out) or _extract_text_config(err)
    status = "ok" if fields else ("model_not_found" if code == 0 else "error")
    return {"status": status, "exit_code": code, "fields": fields}
def collect(rounds, window):
    r1, r2, r3, r4 = rounds
    fields = sorted({k for item in rounds for s in item["step_usage"] for k in s["usage"]})
    before, after = _last_total(r2["step_usage"]), _last_total(r4["step_usage"])
    drop = round((1 - after / before) * 100, 1) if before and after else None
    steps = [{"round": i["name"], "turn": s["turn"], "usage": s["usage"]}
             for i in rounds for s in i["step_usage"]]
    return {
        "q1_usage": {"fields": fields, "steps": steps},
        "q2_recall": {"round1_reply": r1["final_text"],
                      "round1_only_ok": bool(re.search(r"(?<![A-Z])OK(?![A-Z])", r1["final_text"] or "", re.I)),
                      "round2_reply": r2["final_text"],
                      "correct": "PINEAPPLE-42" in (r2["final_text"] or "").upper()},
        "q3_thinking": {"present": any(i["thinking_count"] for i in rounds),
                        "count": sum(i["thinking_count"] for i in rounds),
                        "per_round": {i["name"]: i["thinking_count"] for i in rounds}},
        "q4_compact": {"event_types": r3["event_counts"], "errors": r3["errors"],
                       "error": bool(r3["errors"]) or r3["exit_code"] not in (0, None)
                                or r3["timed_out"],
                       "round4_reply": r4["final_text"],
                       "correct": "PINEAPPLE-42" in (r4["final_text"] or "").upper(),
                       "total_before": before, "total_after": after, "drop_pct": drop,
                       "clear_drop": bool(before and after and after < before * 0.7)},
        "q5_window": window,
    }
def render(result):
    a = result["answers"]
    q1, q2, q3, q4, q5 = (a[k] for k in ("q1_usage", "q2_recall", "q3_thinking",
                                         "q4_compact", "q5_window"))
    lines = ["== dsh 探针 %s ==" % result["generated_at"],
             "模型 %s / effort %s / 每轮超时 %ss" % (MODEL, EFFORT, result["timeout_s"]),
             "原始事件：%s" % result["raw_jsonl"],
             "轮次：" + " | ".join("%s %s %.1fs" % (i["name"], "ok" if not i["errors"] else "err",
                                                 i["elapsed_s"]) for i in result["rounds"]),
             "Q1 step_end usage 字段：%s" % (", ".join(q1["fields"]) or "无")]
    for step in q1["steps"][:10]:
        values = " ".join("%s=%s" % kv for kv in step["usage"].items())
        lines.append("  %s#%s %s" % (step["round"], step["turn"], values or "(空)"))
    if len(q1["steps"]) > 10:
        lines.append("  …另有 %d 步，明细见结构化结果" % (len(q1["steps"]) - 10))
    lines.append("Q2 第1轮只回 OK=%s；第2轮续接答对=%s（%s）"
                 % (q2["round1_only_ok"], q2["correct"], redact(q2["round2_reply"], 80)))
    lines.append("Q3 thinking 事件：%s，共 %d 条 %s"
                 % ("有" if q3["present"] else "没有", q3["count"], q3["per_round"]))
    lines.append("Q4 /compact 事件类型：%s" % json.dumps(q4["event_types"], ensure_ascii=False))
    drop = ("下降 %.1f%%，明显" % q4["drop_pct"] if q4["clear_drop"] else
            "下降 %.1f%%，不明显" % q4["drop_pct"] if q4["drop_pct"] is not None else "无数据")
    lines.append("  报错=%s；第4轮答对=%s；totalTokens %s→%s（%s）"
                 % (q4["error"], q4["correct"], q4["total_before"], q4["total_after"], drop))
    lines.append("Q5 窗口（%s）：%s" % (q5["status"], " ".join(
        "%s=%s" % kv for kv in q5["fields"].items()) or "未提取到"))
    if result.get("out_json"):
        lines.append("结构化结果：%s" % result["out_json"])
    return "\n".join(lines[:24] + (["… 其余见 --out-json"] if len(lines) > 25 else []))
def dry_run():
    repo = "<临时目录>/xay-dsh-probe-XXXX"
    patch = repo + "/dsh-model.patch.yml"
    print("== dsh 探针 --dry-run（不会调用 dsh）==")
    print("临时 git 仓库：%s（git init + hello.py + 一次提交）" % repo)
    print("临时补丁：%s（model=%s, reasoningEffort=%s）" % (patch, MODEL, EFFORT))
    for name, prompt, resume in ROUNDS:
        session = "<上一轮 sessionId>" if resume else None
        print("[%s] XAY_ROLE=executor %s"
              % (name, shlex.join(build_argv(patch, prompt, session))))
    print("[q5_config] %s" % shlex.join([shutil.which("dsh") or "dsh", "--profile",
                                          "headless", "--dump-config"]))
    print("原始事件：%s/dsh-<时间>.jsonl\n说明：r2-r4 会加 --session-id；Q5 输出只在内存解析，不打印、不保存。" % PROBE_HOME)
def main(argv=None):
    parser = argparse.ArgumentParser(description="dsh 能力探针（Q1-Q5）")
    parser.add_argument("--dry-run", action="store_true", help="只打印命令，不调用 dsh")
    parser.add_argument("--out-json", help="另写结构化结果的路径")
    parser.add_argument("--timeout", type=int, default=ROUND_TIMEOUT, help="每轮最多 180 秒")
    args = parser.parse_args(argv)
    timeout = max(5, min(args.timeout, ROUND_TIMEOUT))
    if args.dry_run:
        dry_run()
        return 0
    dsh_bin, base = shutil.which("dsh") or "dsh", default_command()
    if not (shutil.which(base[0]) or Path(base[0]).exists()):
        print("找不到 dsh：请先确认 dsh-run/dsh 已安装，或设置 XAY_DSH_BIN。")
        return 2
    PROBE_HOME.mkdir(parents=True, exist_ok=True)
    raw_path = PROBE_HOME / ("dsh-%s.jsonl" % datetime.now().strftime("%Y%m%d-%H%M%S"))
    raw_path.write_text("", encoding="utf-8")
    repo, rounds, session_id = make_repo(), [], None
    patch = make_patch(repo)
    try:
        for name, prompt, resume in ROUNDS:
            used = session_id if resume else None
            item = summarize(run_round(build_argv(patch, prompt, used), repo, raw_path,
                                       name, timeout), name, prompt, used)
            rounds.append(item)
            session_id = item["session_id_out"] or session_id
    finally:
        shutil.rmtree(repo, ignore_errors=True)
    out_json = str(Path(args.out_json).expanduser().resolve()) if args.out_json else None
    result = {"probe": "dsh", "model": MODEL, "effort": EFFORT, "timeout_s": timeout,
              "generated_at": datetime.now().astimezone().isoformat(timespec="seconds"),
              "raw_jsonl": str(raw_path), "out_json": out_json, "rounds": rounds}
    result["answers"] = collect(rounds, probe_window(dsh_bin))
    print(render(result))
    if args.out_json:
        out_path = Path(args.out_json).expanduser()
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n",
                            encoding="utf-8")
    return 0
if __name__ == "__main__":
    sys.exit(main())
