#!/usr/bin/env python3
"""agy 续接 / 用量 / 压缩 / 权限探针（手动运行；只读 agy 输出，不读 token/凭据）。
用法：python3 V2/probes/agy_probe.py --dry-run
Q3 与 Q1 共用事件；默认 8 个 agy 进程（2*reps+2），硬上限 10。
"""
import argparse, datetime as dt, json, os, queue, shutil, signal
import subprocess, sys, tempfile, threading, time
from pathlib import Path
from statistics import median

MODEL = "gemini-3.8-flash-low"
MAX_PROCS = 10
IDLE_LIMIT = 30.0
WARM_PRIME = "记住这个暗号：PINEAPPLE-42。只回复 OK，不要运行命令，不要做别的事。"
WARM_ASK = "刚才的暗号是什么？只回复暗号本身，不要做别的事。"
COMPACT = "/compact"
PERM = "请运行 python3 hello.py，并把它打印出来的那一行原样告诉我。只运行这一条命令，不要读源码。"
HELLO_PY = '''import pathlib
import secrets
token = "HELLO-" + secrets.token_hex(4)
pathlib.Path("runs.log").write_text(token + "\\n", encoding="utf-8")
print(token)
'''
def now_iso(): return dt.datetime.now().astimezone().isoformat(timespec="seconds")
def message_line(text):
    # 与 scripts/xay_drivers/agy_stream.py 一致：event + message 是已验证的最小载荷。
    return json.dumps({"event": "user", "message": {"role": "user", "content": text}}, ensure_ascii=False) + "\n"
def codeword_ok(text): return "PINEAPPLE42" in "".join(ch for ch in (text or "").upper() if ch.isalnum())
def log_write(fh, **row):
    row["ts"] = time.time()
    fh.write(json.dumps(row, ensure_ascii=False) + "\n")
    fh.flush()

class Stats:
    """Q3：收集每个 step_update 的 usage 与思考迹象（不额外起进程）。"""
    def __init__(self):
        self.steps = []
    def feed(self, probe, rep, ev):
        if ev.get("event") != "step_update": return
        step = ev.get("step_update") or {}
        usage = step.get("usage") if isinstance(step.get("usage"), dict) else None
        thought = any(step.get(k) for k in ("thinking_delta", "thought_delta", "reasoning_delta")) \
            or step.get("step_type") in ("thinking", "thought", "reasoning")
        self.steps.append({"probe": probe, "rep": rep, "type": step.get("step_type"),
                           "usage": usage, "thinking": bool(thought)})
    def last_input(self, probe):
        for step in reversed(self.steps):
            if step["probe"] == probe and step["usage"] and isinstance(step["usage"].get("input_tokens"), (int, float)):
                return step["usage"]["input_tokens"]
    def summary(self):
        used = [step for step in self.steps if step["usage"]]
        return {"steps": len(self.steps), "with_usage": len(used),
                "keys": sorted({key for step in used for key in step["usage"]}),
                "samples": used[:5], "thinking": any(step["thinking"] for step in self.steps),
                "usage_steps": used}
def build_argv(env, conversation=None, settings=None):
    args = [env["binary"], "--model", env["model"], "--input-format", "stream-json",
            "--output-format", "stream-json", "--sandbox"]
    if conversation:
        args += ["--conversation", str(conversation)]
    if settings:
        args += ["--settings", str(settings)]
    # -p= 必须放最后，否则后面的 flag 会被当成 prompt。
    return args + ["-p="]

class Agy:
    def __init__(self, env, probe, rep, conversation=None, settings=None):
        if env["used"] >= MAX_PROCS:
            raise RuntimeError("agy 进程数已达硬上限 %d" % MAX_PROCS)
        env["used"] += 1
        self.env, self.probe, self.rep, self.cid = env, probe, rep, ""
        self.q = queue.Queue()
        self.proc = subprocess.Popen(
            build_argv(env, conversation, settings), cwd=str(env["workdir"]),
            stdin=subprocess.PIPE, stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT, text=True, bufsize=1, start_new_session=True)
        threading.Thread(target=self._read, daemon=True).start()
    def _read(self):
        for line in self.proc.stdout: self.q.put(line)
        self.q.put(None)
    def send(self, text):
        self.proc.stdin.write(message_line(text)); self.proc.stdin.flush()
    def round(self, text):
        t0 = time.monotonic()
        self.send(text)
        r = dict(elapsed=0.0, timed_out=False, eof=False, text_s=None, tool_s=None,
                 errors=[], status="", response="", denied=[], cid=self.cid,
                 types={}, steps={}, usage={}, turns=None, took=None)
        while True:
            left = 180.0 - (time.monotonic() - t0)
            if left <= 0: r["timed_out"] = True; break
            try: raw = self.q.get(timeout=left)
            except queue.Empty: r["timed_out"] = True; break
            if raw is None: r["eof"] = True; break
            raw = raw.strip()
            if not raw: continue
            try: ev = json.loads(raw)
            except json.JSONDecodeError:
                log_write(self.env["log"], type="raw_line", probe=self.probe, rep=self.rep, text=raw[:2000]); continue
            log_write(self.env["log"], type="event", probe=self.probe, rep=self.rep, raw=ev)
            self.env["stats"].feed(self.probe, self.rep, ev)
            kind = ev.get("event") or "?"
            r["types"][kind] = r["types"].get(kind, 0) + 1
            now = round(time.monotonic() - t0, 2)
            if kind == "init":
                self.cid = ev.get("conversation_id") or (ev.get("init") or {}).get("conversation_id") or ""; r["cid"] = self.cid
            elif kind == "result":
                res = ev.get("result") or {}
                r.update(status=res.get("status") or "", response=res.get("response") or "", denied=res.get("denied_actions") or [], cid=res.get("conversation_id") or self.cid, usage=res.get("usage") or {}, turns=res.get("num_turns"), took=res.get("duration_seconds")); break
            elif kind == "error": r["errors"].append(str(ev)[:240])
            elif kind == "step_update":
                st = ev.get("step_update") or {}
                name = st.get("step_type") or "?"
                r["steps"][name] = r["steps"].get(name, 0) + 1
                if name == "tool" and r["tool_s"] is None: r["tool_s"] = now
                if st.get("text_delta") and r["text_s"] is None: r["text_s"] = now
        if r["timed_out"]: self.stop(True)
        r["elapsed"] = round(time.monotonic() - t0, 2); r["response"] = (r["response"] or "")[:600]; r["denied"] = [str(item)[:240] for item in r["denied"]]
        return r
    def _signal(self, sig):
        if self.proc.poll() is None:
            try: os.killpg(os.getpgid(self.proc.pid), sig)
            except OSError:
                try: self.proc.send_signal(sig)
                except OSError: pass
    def stop(self, force=False):
        try:
            if self.proc.stdin and not self.proc.stdin.closed: self.proc.stdin.close()
        except (OSError, ValueError): pass
        if force: self._signal(signal.SIGTERM)
        try: self.proc.wait(timeout=3 if force else 8)
        except subprocess.TimeoutExpired:
            self._signal(signal.SIGKILL)
            try: self.proc.wait(timeout=3)
            except subprocess.TimeoutExpired: pass
def session(env, probe, rep, prompts, conversation=None, settings=None):
    agy = Agy(env, probe, rep, conversation, settings)
    rounds = []
    try:
        for prompt in prompts:
            rounds.append(agy.round(prompt))
            last = rounds[-1]
            if last["timed_out"] or last["eof"] or agy.proc.poll() is not None:
                break
    finally:
        agy.stop()
    return rounds
def q1_warm(env, rep):
    """同一个进程两轮，第 1 轮 result 后不关 stdin。"""
    rounds = session(env, "Q1", rep, [WARM_PRIME, WARM_ASK])
    out = {"rep": rep, "ok": False, "error": "", "cid": ""}
    if not rounds:
        return out
    out["round1"], out["cid"] = rounds[0], rounds[0]["cid"]
    if len(rounds) < 2:
        out["error"] = "第1轮未正常结束或进程已退出"
        return out
    out["round2"], out["ok"] = rounds[1], codeword_ok(rounds[1]["response"])
    if rounds[1]["timed_out"]: out["error"] = "第2轮超时"
    elif rounds[1]["eof"]: out["error"] = "第2轮进程退出"
    elif rounds[1]["status"] not in ("SUCCESS", ""): out["error"] = "status=" + rounds[1]["status"]
    return out
def q2_cold(env, rep, cid):
    """新进程用 --conversation 续接。"""
    if not cid:
        return {"rep": rep, "ok": False, "error": "第1轮没有 conversation_id"}
    rounds = session(env, "Q2", rep, [WARM_ASK], cid)
    out = {"rep": rep, "ok": False, "first_s": None, "idle": False, "error": "", "cid": cid}
    if not rounds:
        return out
    r = rounds[0]
    out["round"], out["ok"] = r, codeword_ok(r["response"])
    marks = [value for value in (r["text_s"], r["tool_s"]) if value is not None]
    if marks:
        out["first_s"] = round(min(marks), 2)
    else:
        out["idle"] = not r["timed_out"] and r["elapsed"] < IDLE_LIMIT
    if r["timed_out"]: out["error"] = "超时"
    elif r["status"] not in ("SUCCESS", ""): out["error"] = "status=" + r["status"]
    return out
def q4_compact(env):
    """热进程里先记住暗号，再发 /compact，再问暗号。"""
    agy = Agy(env, "Q4", 1)
    out = {"ok": False, "bad": False, "error": ""}
    try:
        out["prime"] = agy.round(WARM_PRIME)
        out["before"] = env["stats"].last_input("Q4")
        compact = agy.round(COMPACT)
        out["compact"] = compact
        out["bad"] = bool(compact["errors"] or compact["timed_out"] or compact["eof"]
                          or compact["status"] not in ("SUCCESS", ""))
        after = agy.round(WARM_ASK)
        out["after"], out["ok"] = after, codeword_ok(after["response"])
        out["after_tokens"] = env["stats"].last_input("Q4")
        if compact["timed_out"]: out["error"] = "/compact 超时"
        elif compact["status"] not in ("SUCCESS", ""): out["error"] = "status=" + compact["status"]
    finally:
        agy.stop()
    return out
def q5_permissions(env):
    """临时 settings.json 只放行 command(python3 hello.py)，看是否真执行。"""
    settings = env["workdir"] / "probe-settings.json"
    settings.write_text(json.dumps({"permissions": {"allow": ["command(python3 hello.py)"]}}, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    runs = env["workdir"] / "runs.log"
    if runs.exists():
        runs.unlink()
    agy = Agy(env, "Q5", 1, settings=str(settings))
    out = {"denied": [], "ran": False, "echo": False, "out": "", "error": ""}
    try:
        result = agy.round(PERM)
        out["round"], out["denied"] = result, result["denied"]
        token = runs.read_text(encoding="utf-8").strip() if runs.is_file() else ""
        out["ran"], out["out"] = bool(token), token[:80]
        out["echo"] = bool(token) and token in result["response"]
        if result["timed_out"]: out["error"] = "超时"
        elif result["status"] not in ("SUCCESS", ""): out["error"] = "status=" + result["status"]
    finally:
        agy.stop()
    return out
def make_repo():
    workdir = Path(tempfile.mkdtemp(prefix="xay-agy-probe-"))
    (workdir / "hello.py").write_text(HELLO_PY, encoding="utf-8")
    git = ["git", "-c", "user.email=probe@example.invalid", "-c", "user.name=xay-probe", "-c", "commit.gpgsign=false"]
    subprocess.run(["git", "init", "-q"], cwd=workdir, check=True)
    subprocess.run(git + ["add", "hello.py"], cwd=workdir, check=True)
    subprocess.run(git + ["commit", "-q", "-m", "probe"], cwd=workdir, check=True, capture_output=True)
    return workdir
def dry_run(args, raw, total):
    print("\n".join([
        "agy 探针 dry-run（不调用 agy，不写文件）",
        "模型：%s（只允许 gemini-3.8-*）" % MODEL,
        "agy：%s" % (shutil.which(args.agy) or "未找到（真实运行会报错，dry-run 不拦）"),
        "重复：%d；agy 进程：%d/%d（Q3 与 Q1 共用事件）" % (args.reps, total, MAX_PROCS),
        "Q1 热续接 ×%d：一个进程两轮，第 1 轮 result 后不关 stdin" % args.reps,
        "Q2 冷续接 ×%d：新进程加 --conversation <第 1 轮 ID>" % args.reps,
        "Q3 用量：汇总 step_update 的 usage 与思考文本（0 进程）",
        "Q4 /compact：热进程记暗号 -> /compact -> 再问暗号",
        "Q5 权限：临时 settings.json 放行 command(python3 hello.py)",
        "argv：%s --model %s --input-format stream-json --output-format stream-json --sandbox [-p 最后]"
        % (args.agy, MODEL),
        "原始事件：%s" % raw,
        "结构化结果：%s" % (args.out_json or "（未指定 --out-json，不另写）"),
        "工作目录：临时 git 仓库（含 hello.py）；每轮超时 180s；进程硬上限 %d" % MAX_PROCS,
    ]))
    return 0

def report(payload):
    q1, q2, q3, q4, q5 = (payload[key] for key in ("q1", "q2", "q3", "q4", "q5"))
    L = ["agy 探针结果（%s，进程 %d，用时 %.0fs）" % (payload["model"], payload["processes"], payload["duration_s"])]
    if q1:
        ts = "/".join("%.1fs" % x["round2"]["elapsed"] for x in q1 if x.get("round2")) or "-"
        L.append("Q1 热续接(%d)：第2轮答对 %d/%d；耗时 %s；超时/报错 %d"
                 % (len(q1), sum(bool(x.get("ok")) for x in q1), len(q1), ts, sum(bool(x.get("error")) for x in q1)))
    else: L.append("Q1 热续接：未运行")
    if q2:
        done = [x for x in q2 if x.get("round")]
        marks = [x["first_s"] for x in done if x["first_s"] is not None]
        L.append("Q2 冷续接(%d)：答对 %d/%d；首动作中位 %s；30s 空转结束 %d；无会话号 %d"
                 % (len(q2), sum(x["ok"] for x in done), len(done),
                    "-" if not marks else "%.1fs" % median(marks),
                    sum(x["idle"] for x in done),
                    sum("没有 conversation_id" in x["error"] for x in q2)))
    else: L.append("Q2 冷续接：未运行")
    if q3["steps"]:
        sample = q3["samples"][0]["usage"] if q3["samples"] else {}
        L.append("Q3 用量：step %d 个，带 usage %d/%d 个；字段 %s；示例 %s；思考文本 %s"
                 % (q3["steps"], q3["with_usage"], q3["steps"], ",".join(q3["keys"]) or "无",
                    json.dumps(sample, ensure_ascii=False), "有" if q3["thinking"] else "无"))
    else: L.append("Q3 用量：没有收到 step_update")
    if q4.get("compact"):
        c = q4["compact"]
        L.append("Q4 /compact：事件 %s（step: %s）；报错 %s；压缩后答对 %s；input_tokens %s→%s"
                 % (",".join(sorted(c["types"])) or "-", ",".join(sorted(c["steps"])) or "-",
                    "有" if q4["bad"] else "无", "是" if q4["ok"] else "否",
                    q4["before"], q4["after_tokens"]))
    else: L.append("Q4 /compact：没有结果")
    if q5.get("round"):
        L.append("Q5 权限：denied_actions %d；hello.py 运行证据 %s；输出回显 %s"
                 % (len(q5["denied"]), "是" if q5["ran"] else "否", "是" if q5["echo"] else "否"))
    else: L.append("Q5 权限：没有结果")
    L.append("原始事件：%s" % payload["raw_events"])
    if payload["out_json"]: L.append("结构化结果：%s" % payload["out_json"])
    L.append("临时仓库：%s" % payload["workdir"])
    if q1:
        rate = sum(bool(x.get("ok")) for x in q1) / len(q1)
        L.append("建议：%s" % ("热续接可用，agy 员工可默认热续接（仍要限制轮数）"
                              if rate == 1 and not any(x.get("error") for x in q1) else
                              "热续接不稳定：短任务热续接，长任务一件活一个对话" if rate >= 0.67 else
                              "热续接不可靠：一件活一个对话 + 工作笔记"))
    for x in q1[:1] + q2[:1]:
        if x.get("error"):
            L.append("注意：%s" % x["error"][:120])
    return L[:25]

def main(cli_args=None):
    ap = argparse.ArgumentParser(description="agy 续接/用量/压缩/权限探针（手动运行，最多 10 个 agy 进程）")
    ap.add_argument("--reps", type=int, default=3)
    ap.add_argument("--agy", default=os.environ.get("XAY_AGY_BIN", "agy"))
    ap.add_argument("--out-json", default="")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args(cli_args)
    if not 1 <= args.reps <= 4:
        print("错误：--reps 必须在 1..4（2*reps+2 个 agy 进程，上限 %d）" % MAX_PROCS)
        return 2
    total = 2 * args.reps + 2
    raw = Path.home() / ".xay" / "v2" / "probes" / ("agy-%s.jsonl" % dt.datetime.now().strftime("%Y%m%d-%H%M%S"))
    if args.dry_run:
        return dry_run(args, raw, total)
    if not shutil.which(args.agy):
        print("错误：找不到 agy 可执行文件 %s" % args.agy)
        return 2
    started = time.monotonic()
    p = {"probe": "agy", "model": MODEL, "binary": args.agy, "reps": args.reps,
         "raw_events": str(raw), "out_json": str(Path(args.out_json).expanduser()) if args.out_json else "",
         "processes_planned": total, "started_at": now_iso(), "q1": [], "q2": [], "q3": {},
         "q4": {}, "q5": {}, "workdir": ""}
    log = None
    try:
        raw.parent.mkdir(parents=True, exist_ok=True)
        log = raw.open("a", encoding="utf-8")
        try: workdir = make_repo()
        except Exception as exc: print("错误：创建临时 git 仓库失败：%s" % exc); return 2
        env = {"binary": args.agy, "model": MODEL, "timeout": 180.0, "workdir": workdir,
               "log": log, "stats": Stats(), "used": 0}
        p["workdir"] = str(workdir)
        def guarded(func, *func_args):
            try:
                return func(*func_args)
            except Exception as exc:  # 单项失败不拖垮其余探针
                return {"error": "%s: %s" % (type(exc).__name__, exc)}
        for i in range(1, args.reps + 1):
            p["q1"].append(guarded(q1_warm, env, i))
        for i, item in enumerate(p["q1"], 1):
            p["q2"].append(guarded(q2_cold, env, i, item.get("cid", "")))
        p["q4"] = guarded(q4_compact, env)
        p["q5"] = guarded(q5_permissions, env)
        p["q3"] = env["stats"].summary()
        p["processes"] = env["used"]
        p["duration_s"] = round(time.monotonic() - started, 1)
        p["finished_at"] = now_iso()
        if args.out_json:
            out = Path(args.out_json).expanduser()
            out.parent.mkdir(parents=True, exist_ok=True); out.write_text(json.dumps(p, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    finally:
        if log is not None:
            log.close()
    print("\n".join(report(p)))
    return 0
if __name__ == "__main__":
    sys.exit(main())
