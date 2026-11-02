"""看板 v5 数据层：负责人对话识别、历史回填、对话分组、/api/conversation、SSE 增量与续传。

全部在临时目录里完成：XAY_HOME、Claude 对话记录目录、Claude 桌面版会话目录都是假的，
不会读写真实的 ~/.xay 或 ~/.claude，也不会调用任何 AI。
"""

import http.client
import importlib.util
import json
import os
import socket
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from http.server import ThreadingHTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"
sys.path.insert(0, str(SCRIPTS))
sys.path.insert(0, str(ROOT))

SPEC = importlib.util.spec_from_file_location("xay_watch_v5", SCRIPTS / "xay_watch.py")
WATCH = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(WATCH)

import xay_cli  # noqa: E402
from scripts import xay_agent  # noqa: E402

LEAD_ENV_KEYS = ("XAY_SESSION", "XAY_LEAD_SESSION", "XAY_LEAD_KIND", "CLAUDE_CODE_SESSION_ID",
                 "CLAUDE_CODE_HOST_SESSION_ID", "CODEX_THREAD_ID", "CODEX_SESSION_ID")


def write_json(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False), encoding="utf-8")


def append_jsonl(path, *items):
    with open(path, "a", encoding="utf-8") as fh:
        for item in items:
            fh.write(json.dumps(item, ensure_ascii=False) + "\n")


def read_registry(home):
    text = (Path(home) / "registry.jsonl").read_text(encoding="utf-8")
    decoder, index, items = json.JSONDecoder(), 0, []
    while True:
        while index < len(text) and text[index].isspace():
            index += 1
        if index >= len(text):
            return items
        obj, index = decoder.raw_decode(text, index)
        items.append(obj)


def clean_env(**extra):
    env = {k: v for k, v in os.environ.items() if k not in LEAD_ENV_KEYS and not k.startswith("XAY_SLOTS_")}
    for key in ("XAY_DAG_ID", "XAY_TASK_ID", "XAY_ISOLATE"):
        env.pop(key, None)
    env.update(extra)
    return env


PROMPT = "# 执行准则（codex）\n不要碰别的文件\n\n# 任务\n把按钮改成蓝色\n第二行说明\n\n---\n汇报格式：……\n"


# ---------------------------------------------------------------------------
# 1. 负责人对话识别
# ---------------------------------------------------------------------------
class LeadDetectionTest(unittest.TestCase):
    def test_detect_order_and_explicit_wins(self):
        env = {"CLAUDE_CODE_SESSION_ID": "cl-1", "CLAUDE_CODE_HOST_SESSION_ID": "local_h", "CODEX_THREAD_ID": "cx-1"}
        self.assertEqual(xay_cli.detect_lead(env=env), {"lead_session": "cl-1", "lead_kind": "claude", "lead_host": "local_h"})
        self.assertEqual(xay_cli.detect_lead(env={**env, "XAY_SESSION": "x-1"})["lead_session"], "x-1")
        self.assertEqual(xay_cli.detect_lead(env={**env, "XAY_SESSION": "x-1"})["lead_kind"], "unknown")
        self.assertEqual(xay_cli.detect_lead(env={"CODEX_THREAD_ID": "cx-1"})["lead_kind"], "codex")
        self.assertEqual(xay_cli.detect_lead(env={"CODEX_SESSION_ID": "cs-1"})["lead_session"], "cs-1")
        self.assertEqual(xay_cli.detect_lead("explicit-1", env=env)["lead_session"], "explicit-1")
        self.assertEqual(xay_cli.detect_lead(env={})["lead_session"], "")

    def test_explicit_registered_session_keeps_kind(self):
        with tempfile.TemporaryDirectory() as tmp:
            write_json(Path(tmp) / "sessions" / "ls_x.json", {"id": "ls_x", "lead_kind": "codex"})
            lead = xay_cli.detect_lead("ls_x", env={"XAY_HOME": tmp, "CLAUDE_CODE_SESSION_ID": "cl"})
            self.assertEqual(lead["lead_kind"], "codex")

    def test_cli_lead_command(self):
        out = subprocess.run([sys.executable, str(SCRIPTS / "xay_cli.py"), "lead"], text=True, capture_output=True,
                             env=clean_env(CODEX_THREAD_ID="th-9"), timeout=10)
        self.assertEqual(json.loads(out.stdout)["lead_session"], "th-9")


class LeadRecordingTest(unittest.TestCase):
    """xay-run / xay-dag 把识别结果写进 start.json、meta.json、registry、state.json。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="xay-v5-lead-")
        self.root = Path(self.tmp.name)
        self.home = self.root / "home"
        self.proj = self.root / "proj"
        self.proj.mkdir()
        (self.proj / "a.txt").write_text("x\n", encoding="utf-8")

    def tearDown(self):
        self.tmp.cleanup()

    def _run(self, extra=(), **env):
        argv = [str(SCRIPTS / "xay-run"), "codex", "-n", "lead", "--readonly", *extra, "看一下"]
        out = subprocess.run(argv, cwd=self.proj, text=True, capture_output=True, timeout=120,
                             env=clean_env(XAY_HOME=str(self.home), XAY_NOTIFY="0", XAY_TEST_CMD="true", **env))
        self.assertEqual(out.returncode, 0, out.stderr[-2000:])
        row = [r for r in read_registry(self.home) if r.get("kind") == "run"][-1]
        rd = Path(row["run_dir"])
        return row, json.loads((rd / "start.json").read_text()), json.loads((rd / "meta.json").read_text())

    def test_xay_run_records_claude_lead(self):
        row, start, meta = self._run(CLAUDE_CODE_SESSION_ID="cl-abc", CLAUDE_CODE_HOST_SESSION_ID="local_h1")
        for item in (row, start, meta):
            self.assertEqual(item["lead_session"], "cl-abc")
            self.assertEqual(item["lead_kind"], "claude")
            self.assertEqual(item["lead_host"], "local_h1")

    def test_xay_run_explicit_session_wins(self):
        row, start, meta = self._run(extra=("--session", "ls_given"), CLAUDE_CODE_SESSION_ID="cl-abc", XAY_SESSION="xs")
        self.assertEqual([row["lead_session"], start["lead_session"], meta["lead_session"]], ["ls_given"] * 3)

    def test_xay_dag_records_lead(self):
        plan = self.root / "plan.json"
        write_json(plan, {"title": "小计划", "workdir": str(self.proj),
                          "tasks": [{"id": "a", "agent": "codex", "prompt": "看一下", "readonly": True}]})
        out = subprocess.run([str(SCRIPTS / "xay-dag"), str(plan)], cwd=self.proj, text=True, capture_output=True, timeout=180,
                             env=clean_env(XAY_HOME=str(self.home), XAY_NOTIFY="0", XAY_TEST_CMD="true", XAY_SESSION="xs-dag"))
        self.assertEqual(out.returncode, 0, out.stdout[-2000:] + out.stderr[-2000:])
        rows = read_registry(self.home)
        dag = next(r for r in rows if r.get("kind") == "dag")
        self.assertEqual(dag["lead_session"], "xs-dag")
        state = json.loads((Path(dag["dag_dir"]) / "state.json").read_text())
        self.assertEqual(state["lead_session"], "xs-dag")
        run = next(r for r in rows if r.get("kind") == "run")
        self.assertEqual(run["lead_session"], "xs-dag")


# ---------------------------------------------------------------------------
# 2. 历史回填
# ---------------------------------------------------------------------------
class BackfillTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="xay-v5-backfill-")
        self.root = Path(self.tmp.name)
        self.home = self.root / "home"
        self.projects = self.root / "projects"
        self.claude = self.root / "claude-sessions"
        self.home.mkdir()

    def tearDown(self):
        self.tmp.cleanup()

    def _run_dir(self, name, origin=None, started=1000):
        rd = self.root / "proj" / ".xay" / "runs" / name
        write_json(rd / "start.json", {"started": started, "origin": str(origin or self.root / "proj")})
        return rd

    def _backfill(self, *args):
        out = subprocess.run([sys.executable, str(SCRIPTS / "xay-backfill-leads"), "--projects", str(self.projects),
                              "--claude-sessions", str(self.claude), *args], text=True, capture_output=True, timeout=30,
                             env=clean_env(XAY_HOME=str(self.home)))
        self.assertEqual(out.returncode, 0, out.stderr)
        return out.stdout

    def test_backfill_matches_strings_without_leaking_content(self):
        r1 = self._run_dir("20261029-100000-codex-one")
        r2 = self._run_dir("20261029-110000-dsh-two")
        r3 = self._run_dir("20261029-120000-agy-has-lead")
        dag = self.root / "proj" / ".xay" / "dag" / "20261029-130000"
        dag.mkdir(parents=True)
        append_jsonl(self.home / "registry.jsonl",
                     {"kind": "run", "run": r1.name, "run_dir": str(r1)},
                     {"kind": "run", "run": r2.name, "run_dir": str(r2)},
                     {"kind": "run", "run": r3.name, "run_dir": str(r3), "lead_session": "known"},
                     {"kind": "dag", "dag_dir": str(dag)})
        secret = "绝密对话内容-不能出现在输出里"
        lead_file = self.projects / "proj-a" / "sess-lead.jsonl"
        lead_file.parent.mkdir(parents=True)
        lead_file.write_text(json.dumps({"text": secret + " 派活 " + r1.name + " 和 " + r1.name + " 以及 .xay/dag/20261029-130000"}) + "\n")
        other = self.projects / "proj-a" / "sess-other.jsonl"
        other.write_text(json.dumps({"text": secret + " 顺便提到 " + r1.name}) + "\n")
        os.utime(lead_file, (500, 500))
        os.utime(other, (500, 500))
        output = self._backfill()
        self.assertNotIn(secret, output)
        lead_map = json.loads((self.home / "lead_map.json").read_text())
        self.assertEqual(lead_map[str(r1)], "sess-lead")       # 提到次数更多的对话
        self.assertEqual(lead_map[str(dag)], "sess-lead")
        self.assertNotIn(str(r2), lead_map)                    # 没提到的保持空缺
        self.assertNotIn(str(r3), lead_map)                    # 已有 lead_session 的不回填
        summary = json.loads(output)
        self.assertEqual(summary["needs_backfill"], 3)
        self.assertEqual(summary["mapped_total"], 2)
        # 可重复运行：已有映射保留，不会被后出现的对话覆盖
        other.write_text(json.dumps({"text": (r1.name + " ") * 10}) + "\n")
        self._backfill()
        self.assertEqual(json.loads((self.home / "lead_map.json").read_text())[str(r1)], "sess-lead")

    def test_subagent_and_cwd_fallback(self):
        work = self.root / "work"
        work.mkdir()
        r1 = self._run_dir("20261029-140000-codex-sub", started=time.time() + 3600)
        r2 = self._run_dir("20261029-150000-codex-cwd", origin=work, started=2000)
        r3 = self._run_dir("20261029-160000-codex-late", started=1000)
        append_jsonl(self.home / "registry.jsonl", *[{"kind": "run", "run": r.name, "run_dir": str(r)} for r in (r1, r2, r3)])
        sub = self.projects / "p" / "parent-1" / "subagents" / "agent-a.jsonl"
        sub.parent.mkdir(parents=True)
        sub.write_text(r1.name + " " + r3.name)  # r3 开始时这个子代理还不存在，不能算
        write_json(self.claude / "acct" / "org" / "local_x.json",
                   {"cliSessionId": "cwd-lead", "cwd": str(work), "createdAt": 1000 * 1000, "lastActivityAt": 3000 * 1000,
                    "title": "无关", "messages": ["不该读"]})
        self._backfill()
        lead_map = json.loads((self.home / "lead_map.json").read_text())
        self.assertEqual(lead_map[str(r1)], "parent-1")
        self.assertEqual(lead_map[str(r2)], "cwd-lead")
        self.assertNotIn(str(r3), lead_map)


# ---------------------------------------------------------------------------
# 3. 对话分组、/api/conversation、SSE
# ---------------------------------------------------------------------------
class ConversationFixture(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="xay-v5-conv-")
        self.root = Path(self.tmp.name)
        self.home = self.root / "home"
        self.home.mkdir()
        self.repo = self.root / "repo"
        (self.repo / ".git").mkdir(parents=True)
        self.sub = self.repo / "pkg"
        self.sub.mkdir()
        self.claude_dir = self.root / "claude-sessions"
        self.chat_cwd = self.root / "chat"
        self.chat_cwd.mkdir()
        write_json(self.claude_dir / "acct" / "org" / "local_h1.json", {
            "cliSessionId": "claude-A", "title": "我的真实对话", "cwd": str(self.chat_cwd), "originCwd": str(self.chat_cwd),
            "createdAt": 1_000_000, "lastActivityAt": 2_000_000, "isArchived": False, "secretField": "不该读"})
        self.now = int(time.time())
        # 测试目录本身就在系统临时目录下：关掉"临时目录 → 临时文件夹"，桌面版 scratch 目录改到假的位置
        self._saved_ws_rules = (WATCH.TEMP_ROOTS, WATCH.CLAUDE_SCRATCH)
        WATCH.TEMP_ROOTS = ()
        WATCH.CLAUDE_SCRATCH = str(self.root / "Claude" / "scratch-workspaces")
        self.watch = WATCH.WatchData(str(self.home))
        self.watch.v5.claude = WATCH.ClaudeIndex(str(self.claude_dir))
        self.server = None

    def tearDown(self):
        WATCH.TEMP_ROOTS, WATCH.CLAUDE_SCRATCH = self._saved_ws_rules
        self.watch.v5.stop()
        if self.server:
            self.server.shutdown()
            self.server.server_close()
            WATCH.Handler.data = self._old_data
        self.tmp.cleanup()

    def make_run(self, name, agent="codex", status="done", origin=None, registry=None, start=None, meta=None,
                 prompt=PROMPT, norm=None, raw=None, result=None, started=None):
        rd = (origin or self.sub) / ".xay" / "runs" / name
        rd.mkdir(parents=True)
        started = started or self.now - 100
        write_json(rd / "start.json", {"agent": agent, "started": started, "origin": str(origin or self.sub), "pid": 0, **(start or {})})
        if status is not None:
            write_json(rd / "meta.json", {"status": status, "agent": agent, "started": started, "ended": started + 50 if status != "running" else None,
                                          "origin": str(origin or self.sub), **(meta or {})})
        (rd / "prompt.md").write_text(prompt, encoding="utf-8")
        if norm is not None:
            append_jsonl(rd / "events.norm.jsonl", *norm)
        if raw is not None:
            append_jsonl(rd / "events.raw", *raw)
        if result is not None:
            (rd / "result.md").write_text(result, encoding="utf-8")
        append_jsonl(self.home / "registry.jsonl", {"kind": "run", "run": name, "run_dir": str(rd), "agent": agent,
                                                    "title": name, "time": started, **(registry or {})})
        return rd

    def serve(self):
        self._old_data = WATCH.Handler.data
        WATCH.Handler.data = self.watch
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), WATCH.Handler)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        return self.server.server_port

    def get(self, path, headers=None):
        port = self.server.server_port
        conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
        conn.request("GET", path, headers={"Host": "127.0.0.1:%d" % port, **(headers or {})})
        response = conn.getresponse()
        body = response.read().decode("utf-8")
        conn.close()
        return response.status, body


def norm(seq, typ, **fields):
    return {"seq": seq, "ts": time.time() + seq / 1000.0, "type": typ, **fields}


class ConversationGroupingTest(ConversationFixture):
    def test_workspaces_conversations_and_titles(self):
        # claude-A：一轮直接带 lead_session，一轮靠 ls_ 会话（lead_key=claude:claude-A），一轮靠 lead_map
        self.make_run("r1", registry={"lead_session": "claude-A", "lead_kind": "claude"}, started=self.now - 300)
        write_json(self.home / "sessions" / "ls_A.json", {"id": "ls_A", "lead_kind": "claude", "lead_key": "claude:claude-A", "title": "开工"})
        self.make_run("r2", agent="agy", registry={"session": "ls_A"}, started=self.now - 200)
        r3 = self.make_run("r3", agent="dsh", started=self.now - 150)
        write_json(self.home / "lead_map.json", {str(r3): "claude-A"})
        # codex 负责人的显式会话：标题来自 session begin
        write_json(self.home / "sessions" / "ls_B.json", {"id": "ls_B", "lead_kind": "codex", "lead_key": "explicit:ls_B", "title": "codex 负责的活"})
        self.make_run("r4", registry={"session": "ls_B", "lead_session": "ls_B", "lead_kind": "codex"})
        # 没有负责人：统一归入"临时文件夹"里的一个"以前的任务"
        self.make_run("r5", agent="dsh")
        self.make_run("r6", agent="codex")
        state = self.watch.state()
        convs = {c["id"]: (ws, c) for ws in state["workspaces"] for c in ws["conversations"]}
        self.assertEqual(set(convs), {"claude-A", "ls_B", "legacy:temp"})
        ws, conv = convs["claude-A"]
        self.assertEqual(conv["title"], "我的真实对话")
        self.assertEqual(conv["lead_kind"], "claude")
        self.assertEqual(ws["path"], str(self.chat_cwd))            # Claude 对话取 originCwd
        self.assertEqual(conv["agents"], ["codex", "dsh", "agy"])    # 固定顺序
        self.assertEqual(conv["counts"], {"running": 0, "done": 3, "failed": 0, "lead_todo": 0})
        self.assertEqual(conv["status"], "done")
        ws_b, conv_b = convs["ls_B"]
        self.assertEqual(conv_b["title"], "codex 负责的活")
        self.assertEqual(conv_b["lead_kind"], "codex")
        self.assertEqual(ws_b["path"], str(self.repo))               # 非 Claude：首次派活的 origin 的仓库根
        self.assertEqual(ws_b["name"], "repo")
        legacy_ws, legacy = convs["legacy:temp"]
        self.assertEqual(legacy["title"], "以前的任务")
        self.assertEqual((legacy_ws["path"], legacy_ws["name"]), ("temp", "临时文件夹"))
        self.assertTrue(legacy_ws["legacy"])
        self.assertFalse(ws_b["legacy"])
        self.assertIs(state["workspaces"][-1], legacy_ws)              # "临时文件夹"排在侧栏最后
        self.assertTrue(legacy_ws["conversations"][-1]["legacy"])
        for key in ("now", "seq", "workspaces", "decisions", "agents", "host"):
            self.assertIn(key, state)

    def test_desktop_scratch_conversations_merge_into_this_computer(self):
        scratch = Path(WATCH.CLAUDE_SCRATCH)
        for host, cli, title, folder in (("local_s1", "claude-S1", "多 AI 编程协作工具调研", "acct/org/scratch-1"),
                                         ("local_s2", "claude-S2", "Antigravity 应用插件集成", "acct/org/scratch-2")):
            (scratch / folder).mkdir(parents=True)
            write_json(self.claude_dir / "acct" / "org" / (host + ".json"), {
                "cliSessionId": cli, "title": title, "cwd": str(scratch / folder), "originCwd": str(scratch / folder),
                "lastActivityAt": 3_000_000})
        self.watch.v5.claude = WATCH.ClaudeIndex(str(self.claude_dir))
        self.make_run("s1", registry={"lead_session": "claude-S1", "lead_kind": "claude"})
        self.make_run("s2", registry={"lead_session": "claude-S2", "lead_kind": "claude"})
        self.make_run("p1", registry={"lead_session": "claude-A", "lead_kind": "claude"})
        state = self.watch.state()
        groups = {ws["name"]: ws for ws in state["workspaces"]}
        self.assertEqual(set(groups), {"这台电脑", "chat"})
        here = groups["这台电脑"]
        self.assertEqual(here["path"], "this-computer")
        self.assertFalse(here["legacy"])
        self.assertEqual({c["title"] for c in here["conversations"]}, {"多 AI 编程协作工具调研", "Antigravity 应用插件集成"})
        self.assertEqual(groups["chat"]["conversations"][0]["id"], "claude-A")

    def test_temp_and_unknown_workspaces_go_to_temp_folder(self):
        WATCH.TEMP_ROOTS = (str(self.root / "tmpdirs"),)
        tmp_origin = self.root / "tmpdirs" / "proj"
        tmp_origin.mkdir(parents=True)
        other = self.root / "other"
        (other / ".git").mkdir(parents=True)
        self.make_run("t1", origin=tmp_origin, registry={"lead_session": "ls_T", "lead_kind": "codex"})
        self.make_run("old1", origin=other, started=self.now - 500)
        self.make_run("old2", origin=tmp_origin, started=self.now - 400)
        self.make_run("old3", started=self.now - 300)
        self.make_run("real", registry={"lead_session": "claude-A", "lead_kind": "claude"}, started=self.now - 900)
        state = self.watch.state()
        names = [ws["name"] for ws in state["workspaces"]]
        self.assertEqual(names, ["chat", "临时文件夹"])
        temp = state["workspaces"][-1]
        self.assertTrue(temp["legacy"])
        self.assertEqual([c["id"] for c in temp["conversations"]], ["ls_T", "legacy:temp"])
        legacy = self.watch.v5.conversation("legacy:temp")
        self.assertEqual(legacy["conversation"]["title"], "以前的任务")
        self.assertEqual(len(legacy["threads"]), 3)          # 三个目录的旧数据合成一个对话
        self.assertEqual(WATCH._workspace_for(""), {"path": "temp", "name": "临时文件夹"})

    def test_thread_title_cleaning(self):
        task = "先读 .xay/context.md（重点是约定 D）。背景：页面要按负责人对话组织。其余略"
        self.make_run("auto", prompt="# 任务\n" + task + "\n", registry={"lead_session": "claude-A", "title": task[:30] + "…"})
        self.make_run("named", prompt="# 任务\n" + task + "\n", registry={"lead_session": "claude-A", "title": "改看板"})
        self.make_run("empty", agent="dsh", prompt="# 任务\n先读 .xay/context.md。\n", registry={"lead_session": "claude-A", "title": ""})
        titles = {t["id"].rsplit("/", 1)[-1]: t["title"] for t in self.watch.v5.conversation("claude-A")["threads"]}
        self.assertEqual(titles, {"auto": "页面要按负责人对话组织", "named": "改看板", "empty": "dsh 的活"})
        self.assertEqual(WATCH._clean_title("先读 a.md 和 b.md。" + "很长" * 30, "codex"), "很长" * 9 + "很…")
        self.assertEqual(WATCH._clean_title("", "codex"), "Codex 的活")

    def test_thread_title_keeps_words_whole(self):
        # 太长时先取第一个分句；仍太长则按词截断，不把英文单词/路径切成 "te…" "unitt…"
        self.assertEqual(WATCH._clean_title("EventParser 保持不变，tests/test_xay_watch.py 必须继续通过。", "codex"), "EventParser 保持不变")
        self.assertEqual(WATCH._clean_title("运行 python3 -m unittest discover -s tests -v。若有失败就修。", "codex"), "运行 python3 -m unittest discover -s tests -v")
        self.assertEqual(WATCH._clean_title("运行 python3 -m unittest discover -s tests -v 并把输出贴出来给我看看。", "codex"), "运行 python3 -m unittest…")
        self.assertEqual(WATCH._clean_title("只读代码审查，找真实的正确性 bug（不要风格建议）。", "codex"), "只读代码审查")
        self.assertEqual(WATCH._clean_title("先读 a.md。按 ui-spec.md 改 antigravity-hud/hud.js（只改这一个文件）。", "codex"), "按 ui-spec.md 改 antigravity-hud/hud.js")

    def test_readonly_and_noise_files_are_dropped(self):
        noisy = ["dingtalk-agent/scheduler/.tick.lock", "eweadn-crm/data/crm.db-wal", "rag/rag.db", ".xay/board.jsonl", "logs/rag.log", " app/x.ts", "app/x.ts", "app/y.ts"]
        self.make_run("ro", registry={"lead_session": "claude-A"}, start={"readonly": True}, meta={"changed_files": noisy})
        self.make_run("rw", registry={"lead_session": "claude-A"}, meta={"changed_files": noisy}, started=self.now - 90)
        self.make_run("hint", registry={"lead_session": "claude-A"}, prompt="# 任务\nUI 审查，只看不改：不要修改任何文件。\n", meta={"changed_files": noisy}, started=self.now - 80)
        files = {t["id"].rsplit("/", 1)[-1]: t["messages"][-1]["files"] for t in self.watch.v5.conversation("claude-A")["threads"]}
        self.assertEqual(files["ro"], [])                       # 只读任务不该有改动
        self.assertEqual(files["hint"], [])                     # 旧记录没标 readonly，但任务开头写明只看不改
        self.assertEqual(files["rw"], ["app/x.ts", "app/y.ts"])  # 锁/数据库/日志/xay 记录是别的进程改的，去掉；首尾空白清理后去重

    def test_timeout_run_explains_itself(self):
        self.make_run("slow", status="timeout", registry={"lead_session": "claude-A"}, start={"timeout": 1800},
                      meta={"exit": 124, "duration_s": 1800, "notice": "任务未完成，已丢弃独立工作区"}, result="\n")
        thread = self.watch.v5.conversation("claude-A")["threads"][0]
        ai = thread["messages"][-1]
        self.assertEqual(thread["status"], "failed")
        self.assertEqual(ai["status"], "failed")
        self.assertIsNone(ai["final"])                          # 只有换行的 result.md 不算回复
        self.assertEqual(ai["steps"][-1]["kind"], "error")
        self.assertEqual(ai["steps"][-1]["raw"], "30 分钟内没做完，已停止；任务未完成，已丢弃独立工作区")

    def test_failed_run_overrides_completed_turn(self):
        # 账本里这一轮"完成"了，但随后运行以失败收场：消息状态要和线程一致，不能一边"失败"一边"已完成"
        events = [norm(1, "turn.started", data={"turn_seq": 1}), norm(2, "message", text="解释完了", data={"final": True}),
                  norm(3, "turn.completed", data={"status": "completed"})]
        self.make_run("agy-x", agent="agy", status="failed", registry={"lead_session": "claude-A"}, norm=events, meta={"exit": 1})
        ai = self.watch.v5.conversation("claude-A")["threads"][0]["messages"][-1]
        self.assertEqual(ai["status"], "failed")
        self.assertEqual(ai["final"], "解释完了")

    def test_late_events_join_last_turn(self):
        # 最终回复晚于 turn.completed 落账：并入最后一轮，不另起一条空的"第 3 回合"
        events = [norm(1, "turn.started", data={"turn_seq": 1}), norm(2, "message", text="第一轮", data={"final": False}),
                  norm(3, "turn.started", data={"turn_seq": 2}), norm(4, "turn.completed", data={"status": "completed"}),
                  norm(5, "message", text="a.txt 里有两个词", data={"final": True})]
        self.make_run("dsh-late", agent="dsh", registry={"lead_session": "claude-A"}, norm=events)
        ais = [m for m in self.watch.v5.conversation("claude-A")["threads"][0]["messages"] if m["kind"] == "ai"]
        self.assertEqual([m["turn"] for m in ais], [1, 2])
        self.assertEqual(ais[1]["final"], "a.txt 里有两个词")

    def test_thread_work_time_sums_rounds(self):
        first = self.make_run("w1", registry={"lead_session": "claude-A"}, meta={"session_id": "th-w", "ended": self.now - 3000}, started=self.now - 3600)
        self.make_run("w2", registry={"resume": "th-w"}, meta={"ended": self.now - 100}, started=self.now - 400)
        thread = self.watch.v5.conversation("claude-A")["threads"][0]
        self.assertEqual(thread["id"], str(first))
        self.assertEqual(thread["work_s"], 600 + 300)            # 两轮各自跑的时间，不含中间等负责人的 2600 秒
        self.assertIsNone(thread["active_since"])

    def test_claude_index_reads_only_whitelisted_fields(self):
        index = WATCH.ClaudeIndex(str(self.claude_dir))
        fields = index.lookup("claude-A")
        self.assertEqual(set(fields), set(WATCH.CLAUDE_FIELDS))
        self.assertNotIn("secretField", json.dumps(index._files, ensure_ascii=False))
        self.assertEqual(index.lookup("nope", "local_h1")["title"], "我的真实对话")

    def test_title_fallback_to_first_task(self):
        self.make_run("first", registry={"lead_session": "unknown-lead", "title": "第一次派的活"}, started=self.now - 50)
        self.make_run("second", registry={"lead_session": "unknown-lead", "title": "第二次"}, started=self.now - 10)
        conv = self.watch.v5.conversation("unknown-lead")["conversation"]
        self.assertEqual(conv["title"], "第一次派的活")
        self.assertEqual(conv["lead_kind"], "unknown")


class ConversationApiTest(ConversationFixture):
    def test_conversation_structure(self):
        dag = self.sub / ".xay" / "dag" / "20261029-1"
        write_json(dag / "state.json", {"title": "大阶段", "tasks": []})
        append_jsonl(self.home / "registry.jsonl", {"kind": "dag", "dag_dir": str(dag), "title": "大阶段", "lead_session": "claude-A"})
        events = [
            norm(1, "session.started", data={"native_session_id": "th-1"}),
            norm(2, "turn.started", data={"turn_seq": 1}),
            norm(3, "reasoning", text="先想一想"),
            norm(4, "tool.call", kind="cmd", text="python3 -m pytest -q", data={"id": "c1"}),
            norm(5, "tool.result", data={"id": "c1", "ok": False, "exit": 1}),
            norm(6, "tool.call", kind="read", text="/x/y/app.js", data={"id": "c2"}),
            norm(7, "message", text="我改好了", data={"final": False}),
            norm(8, "usage", data={"in": 10, "out": 5, "total": 15, "granularity": "live"}),
            norm(9, "file.changed", path="app.js", data={}),
            norm(10, "turn.completed", data={"status": "interrupted"}),
            {"seq": 11, "ts": time.time() + 1, "type": "control.requested", "data": {"action": "resume", "text": "接着改颜色", "by": "user"}},
            norm(12, "turn.started", data={"turn_seq": 2}),
            norm(13, "message", text="结果：完成", data={"final": True}),
            norm(14, "turn.completed", data={"status": "completed"}),
        ]
        first = self.make_run("dag-a1", registry={"dag": str(dag), "task": "a"}, norm=events, result="结果：完成（result.md）",
                              meta={"changed_files": ["app.js", "style.css"], "session_id": "th-1"}, started=self.now - 90)
        rework = self.make_run("dag-a2", registry={"resume": "th-1"}, prompt="# 任务\n再改一次\n", started=self.now - 40)
        self.make_run("agy-1", agent="agy", registry={"lead_session": "claude-A"}, started=self.now - 80)
        self.serve()
        status, body = self.get("/api/conversation?id=claude-A")
        self.assertEqual(status, 200)
        payload = json.loads(body)
        self.assertEqual(payload["conversation"]["id"], "claude-A")
        threads = payload["threads"]
        self.assertEqual([t["agent"] for t in threads], ["codex", "agy"])
        thread = threads[0]
        self.assertEqual(thread["id"], str(first))
        self.assertEqual(thread["phase_title"], "大阶段")
        for key in ("agent", "title", "status", "started", "updated", "phase_title", "capabilities", "active_run", "messages"):
            self.assertIn(key, thread)
        kinds = [(m["kind"], m.get("source")) for m in thread["messages"]]
        self.assertEqual(kinds, [("lead", "task"), ("ai", None), ("lead", "resume"), ("ai", None), ("lead", "resume"), ("ai", None)])
        lead = thread["messages"][0]
        self.assertEqual(lead["text"], "把按钮改成蓝色\n第二行说明")      # 只取"# 任务"段
        self.assertNotIn("执行准则", lead["text"])
        ai1 = thread["messages"][1]
        self.assertEqual(ai1["status"], "interrupted")
        self.assertFalse(ai1["streaming"])
        self.assertEqual([s["text"] for s in ai1["steps"]], ["运行测试", "查看 app.js"])
        self.assertEqual(ai1["steps"][0]["state"], "failed")
        self.assertEqual(set(ai1["steps"][0]), {"kind", "text", "raw", "state"})
        self.assertEqual(ai1["thinking"], "先想一想")
        self.assertEqual(ai1["text"], "我改好了")
        self.assertEqual(ai1["usage"], {"tokens": 15})
        self.assertEqual(ai1["files"], ["app.js"])
        self.assertEqual(thread["messages"][2]["text"], "接着改颜色")
        ai2 = thread["messages"][3]
        self.assertEqual(ai2["final"], "结果：完成")
        self.assertEqual(ai2["files"], ["app.js", "style.css"])
        self.assertIsNotNone(ai2["ended"])
        self.assertIsInstance(ai2["elapsed_s"], int)
        self.assertEqual(thread["messages"][4]["text"], "再改一次")        # 返工轮次的指示
        self.assertEqual(thread["messages"][5]["run"], str(rework))
        status, _ = self.get("/api/conversation?id=missing")
        self.assertEqual(status, 404)
        state = json.loads(self.get("/api/state")[1])
        self.assertEqual(sum(len(ws["conversations"]) for ws in state["workspaces"]), 1)

    def test_legacy_events_raw_run(self):
        raw = [
            {"type": "thread.started", "thread_id": "t-old"},
            {"type": "item.completed", "item": {"id": "i1", "type": "command_execution", "command": "/bin/zsh -lc 'cat a.txt'", "exit_code": 0}},
            {"type": "item.completed", "item": {"id": "i2", "type": "agent_message", "text": "旧版的回复"}},
            {"type": "turn.completed", "usage": {"input_tokens": 3, "output_tokens": 4}},
        ]
        self.make_run("old", registry={"lead_session": "claude-A"}, raw=raw, result="旧版汇报")
        thread = self.watch.v5.conversation("claude-A")["threads"][0]
        ai = thread["messages"][1]
        self.assertFalse(ai["streaming"])
        self.assertEqual(ai["status"], "completed")
        self.assertEqual(ai["steps"][0]["text"], "查看 a.txt")
        self.assertEqual(ai["text"], "旧版的回复")
        self.assertEqual(ai["final"], "旧版汇报")
        self.assertEqual(ai["usage"], {"tokens": 7})

    def test_managed_run_before_ledger_is_streaming(self):
        rd = self.make_run("fresh", status="running", registry={"lead_session": "claude-A"}, start={"driver": "codex-app-server"},
                           raw=[{"method": "thread/started", "params": {}}])
        thread = self.watch.v5.conversation("claude-A")["threads"][0]
        ai = thread["messages"][1]
        self.assertEqual((ai["id"], ai["status"], ai["streaming"]), (str(rd) + "#1", "running", True))
        self.assertEqual(thread["active_run"], str(rd))

    def test_control_accepts_v5_body(self):
        rd = self.make_run("ctl", status="running", registry={"lead_session": "claude-A"})
        port = self.serve()
        seen = []
        original = WATCH.Handler._ctl
        WATCH.Handler._ctl = lambda handler, execution, payload: seen.append((execution, payload)) or {"ok": True, "accepted_as": payload["op"]}
        try:
            conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
            body = json.dumps({"id": str(rd), "op": "steer", "text": "加快点"})
            conn.request("POST", "/api/control", body=body, headers={
                "Host": "127.0.0.1:%d" % port, "Origin": "http://127.0.0.1:%d" % port, "Sec-Fetch-Site": "same-origin",
                "X-Xay-Action": "control", "Content-Type": "application/json"})
            response = conn.getresponse()
            self.assertEqual(response.status, 200, response.read())
            conn.close()
        finally:
            WATCH.Handler._ctl = original
        self.assertEqual(seen, [(str(rd), {"op": "steer", "text": "加快点"})])

    def test_decisions_carry_conversation(self):
        # 合并冲突归负责人处理：带上所属对话，但不算"等你"；单纯失败不产生决定
        self.make_run("broken", status="done", registry={"lead_session": "claude-A"}, meta={"land": "conflict"})
        self.make_run("failed", status="failed", registry={"lead_session": "claude-B"}, meta={"fail_reason": "cli_error"})
        asked = self.make_run("asked", status="done", registry={"lead_session": "claude-C"})
        (asked / "decisions.jsonl").write_text(json.dumps({"kind": "question", "owner": "user", "title": "选哪个方案"}, ensure_ascii=False) + "\n", encoding="utf-8")
        state = self.watch.state()
        by_conv = {d["conversation"]: d for d in state["decisions"]}
        self.assertEqual(set(by_conv), {"claude-A", "claude-C"})
        self.assertFalse(by_conv["claude-A"]["for_user"])
        self.assertTrue(by_conv["claude-C"]["for_user"])
        convs = {c["id"]: c for w in state["workspaces"] for c in w["conversations"]}
        self.assertEqual(convs["claude-A"]["status"], "done")
        self.assertEqual(convs["claude-A"]["counts"]["lead_todo"], 1)
        self.assertEqual(convs["claude-B"]["status"], "done")
        self.assertEqual(convs["claude-C"]["status"], "needs_you")
        thread = self.watch.v5.conversation("claude-A")["threads"][0]
        self.assertEqual(thread["lead_todo"]["kind"], "conflict")

class SSEReader:
    def __init__(self, port, path, headers=None):
        self.sock = socket.create_connection(("127.0.0.1", port), timeout=5)
        lines = ["GET %s HTTP/1.1" % path, "Host: 127.0.0.1:%d" % port, "Accept: text/event-stream"]
        lines += ["%s: %s" % item for item in (headers or {}).items()]
        self.sock.sendall(("\r\n".join(lines) + "\r\n\r\n").encode("utf-8"))
        self.file = self.sock.makefile("rb")
        while self.file.readline() not in (b"\r\n", b"\n", b""):
            pass

    def next_event(self, timeout=8):
        self.sock.settimeout(timeout)
        event = {}
        while True:
            line = self.file.readline()
            if not line:
                raise EOFError
            line = line.decode("utf-8").rstrip("\n")
            if not line:
                if "event" in event:
                    return event
                event = {}
                continue
            if line.startswith(":") or line.startswith("retry:"):
                continue
            key, _, value = line.partition(": ")
            event[key] = json.loads(value) if key == "data" else value

    def collect(self, predicate, timeout=10):
        deadline = time.time() + timeout
        seen = []
        while time.time() < deadline:
            event = self.next_event(max(0.5, deadline - time.time()))
            seen.append(event)
            if predicate(event, seen):
                return seen
        raise AssertionError("没有等到期望的 SSE 事件：%r" % [e["event"] for e in seen])

    def close(self):
        try:
            self.sock.close()
        except OSError:
            pass


class SSETest(ConversationFixture):
    def test_stream_deltas_and_resume(self):
        rd = self.make_run("live", status="running", registry={"lead_session": "claude-A"},
                           norm=[norm(1, "turn.started", data={"turn_seq": 1})])
        port = self.serve()
        state = json.loads(self.get("/api/state")[1])
        reader = SSEReader(port, "/api/events?since=%d" % state["seq"])
        try:
            append_jsonl(rd / "stream.jsonl", {"ts": 1, "turn": 1, "item": "m1", "text_delta": "你好"})
            time.sleep(0.5)
            append_jsonl(rd / "stream.jsonl", {"ts": 2, "turn": 1, "item": "m1", "text_delta": "，世界"})
            append_jsonl(rd / "events.norm.jsonl", norm(2, "tool.call", kind="cmd", text="npm test", data={"id": "x"}))
            seen = reader.collect(lambda e, s: sum(x["event"] == "message.delta" for x in s) >= 3)
            deltas = [e["data"] for e in seen if e["event"] == "message.delta"]
            self.assertEqual("".join(d.get("text_delta", "") for d in deltas), "你好，世界")
            self.assertEqual([d["step"]["text"] for d in deltas if "step" in d], ["运行测试"])
            self.assertEqual(deltas[0]["conversation"], "claude-A")
            self.assertEqual(deltas[0]["thread"], str(rd))
            self.assertEqual(deltas[0]["message_id"], str(rd) + "#1")
            mid_id = int(seen[-1]["id"])
            # 新一轮：stream.jsonl 被清空，新 AI 消息以 message.append 到达
            append_jsonl(rd / "events.norm.jsonl", norm(3, "tool.result", data={"id": "x", "ok": True, "exit": 0}),
                         norm(4, "message", text="你好，世界", data={"final": True}),
                         norm(5, "turn.completed", data={"status": "completed"}),
                         {"seq": 6, "ts": time.time() + 5, "type": "control.requested", "data": {"action": "steer", "text": "顺便加个测试"}},
                         norm(7, "turn.started", data={"turn_seq": 2}))
            (rd / "stream.jsonl").write_text(json.dumps({"ts": 3, "turn": 2, "item": "m2", "text_delta": "第二轮"}) + "\n")
            seen = reader.collect(lambda e, s: any(x["event"] == "message.append" and x["data"]["message"].get("turn") == 2 for x in s))
            names = [e["event"] for e in seen]
            self.assertIn("message.update", names)
            update = next(e["data"] for e in seen if e["event"] == "message.update")
            self.assertEqual(update["patch"].get("final"), "你好，世界")
            self.assertEqual(update["patch"].get("status"), "completed")
            leads = [e["data"]["message"] for e in seen if e["event"] == "message.append" and e["data"]["message"]["kind"] == "lead"]
            self.assertEqual([(m["source"], m["text"]) for m in leads], [("steer", "顺便加个测试")])
            new_ai = next(e["data"]["message"] for e in seen if e["event"] == "message.append" and e["data"]["message"].get("turn") == 2)
            self.assertEqual(new_ai["text"], "第二轮")
            self.assertTrue(new_ai["streaming"])
            last_id = int(seen[-1]["id"])
        finally:
            reader.close()
        # Last-Event-ID 续传：只收到这个位置之后的事件
        resumed = SSEReader(port, "/api/events?since=0", headers={"Last-Event-ID": str(mid_id)})
        try:
            seen = resumed.collect(lambda e, s: int(e["id"]) >= last_id)
            self.assertGreater(int(seen[0]["id"]), mid_id)
            self.assertNotIn("message.delta", [e["event"] for e in seen if int(e["id"]) <= mid_id])
        finally:
            resumed.close()
        # 位置无效（例如服务重启后客户端带着更大的 id）：先发 state.changed 让前端重拉快照
        stale = SSEReader(port, "/api/events", headers={"Last-Event-ID": "999999"})
        try:
            self.assertEqual(stale.next_event()["event"], "state.changed")
        finally:
            stale.close()

    def test_state_changed_is_debounced(self):
        self.make_run("a", registry={"lead_session": "claude-A"})
        model = self.watch.v5
        model.tick()
        for index in range(5):
            self.make_run("b%d" % index, registry={"lead_session": "lead-%d" % index})
            model.tick()
        emitted = [e for e in model.history if e["type"] == "state.changed"]
        self.assertEqual(len(emitted), 1)
        model._state_emitted -= 2
        model.tick()
        self.assertEqual(len([e for e in model.history if e["type"] == "state.changed"]), 2)

    def test_one_shot_compat_without_accept(self):
        self.make_run("a", registry={"lead_session": "claude-A"})
        self.serve()
        status, body = self.get("/api/events?since=0&once=1", headers={"Accept": "text/event-stream"})
        self.assertEqual(status, 200)
        self.assertIn("event:", body)


# ---------------------------------------------------------------------------
# 4. xay_agent 的 stream.jsonl
# ---------------------------------------------------------------------------
class AgentStreamTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="xay-v5-stream-")
        self.rd = Path(self.tmp.name) / "run"
        write_json(self.rd / "start.json", {"run": "run", "agent": "codex"})
        self.agent = xay_agent.XayAgent(self.rd, xay_home=Path(self.tmp.name) / "home")

    def tearDown(self):
        self.tmp.cleanup()

    def lines(self):
        path = self.rd / "stream.jsonl"
        return [json.loads(x) for x in path.read_text().splitlines()] if path.exists() else []

    def test_delta_sources(self):
        self.assertEqual(xay_agent.stream_delta({"method": "item/agentMessage/delta", "params": {"itemId": "m", "delta": "字"}}), ("字", "m"))
        self.assertEqual(xay_agent.stream_delta({"event": "step_update", "step_update": {"step_type": "agent_response", "step_index": 3, "text_delta": "好"}}), ("好", "step:3"))
        self.assertEqual(xay_agent.stream_delta('{"type": "text", "text": "嗯"}'), ("嗯", None))
        self.assertIsNone(xay_agent.stream_delta({"method": "item/started"}))
        self.assertIsNone(xay_agent.stream_delta({"event": "step_update", "step_update": {"step_type": "tool"}}))

    def test_turn_reset_segments_and_cap(self):
        self.agent._record_event({"type": "turn.started", "data": {"turn_seq": 1}})
        self.agent._write_stream({"method": "item/agentMessage/delta", "params": {"itemId": "m", "delta": "一"}})
        self.agent._write_stream({"method": "item/agentMessage/delta", "params": {"itemId": "m", "delta": "二"}})
        self.assertEqual([(x["turn"], x["item"], x["text_delta"]) for x in self.lines()], [(1, "m", "一"), (1, "m", "二")])
        self.agent._record_event({"type": "turn.started", "data": {"turn_seq": 2}})
        self.assertEqual(self.lines(), [])                              # 新一轮清空
        self.agent._write_stream({"type": "text", "text": "a"})
        self.agent._write_stream({"type": "tool_call", "tool": "bash"})
        self.agent._write_stream({"type": "text", "text": "b"})
        self.assertEqual([x["item"] for x in self.lines()], ["seg:1", "seg:2"])  # dsh：工具调用隔开的两段
        old = xay_agent.STREAM_MAX_BYTES
        xay_agent.STREAM_MAX_BYTES = (self.rd / "stream.jsonl").stat().st_size + 10
        try:
            self.agent._write_stream({"type": "text", "text": "很长的一段" * 20})
        finally:
            xay_agent.STREAM_MAX_BYTES = old
        self.assertEqual(len(self.lines()), 2)                          # 超过上限不再追加

    def test_control_requested_keeps_text(self):
        self.agent.handle_control({"op": "steer", "text": "改成红色", "by": "user"})
        events = [json.loads(x) for x in (self.rd / "events.norm.jsonl").read_text().splitlines()]
        requested = next(e for e in events if e["type"] == "control.requested")
        self.assertEqual(requested["data"]["text"], "改成红色")


if __name__ == "__main__":
    unittest.main()
