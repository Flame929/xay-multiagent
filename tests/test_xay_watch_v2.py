"""xay 看板第二版接口测试。

只依据 .xay/context.md 约定 D 编写，不阅读实现。测试只使用约定里写明的入口：
`humanize()`、`WatchData(xay_home).state()`、`WatchData.detail(id)`、`Handler`。

所有数据都造在临时目录里，不会读写真实的 ~/.xay。
"""

import importlib.util
import json
import os
import subprocess
import sys
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request
from datetime import datetime
from pathlib import Path
from http.server import ThreadingHTTPServer

TESTS_DIR = Path(__file__).resolve().parent
PROJECT_ROOT = TESTS_DIR.parent
MODULE_PATH = PROJECT_ROOT / "scripts" / "xay_watch.py"

humanize = None
WatchData = None
Handler = None

_UNSET = object()

_AGENT_DEFAULT_MODEL = {
    "codex": "默认",
    "dsh": "deepseek-v4.1-flash",
    "agy": "gemini-3.8-flash-medium",
}


def setUpModule():
    global humanize, WatchData, Handler
    if not MODULE_PATH.is_file():
        raise AssertionError("缺少待测实现文件: %s" % MODULE_PATH)
    spec = importlib.util.spec_from_file_location("xay_watch_v2_under_test", MODULE_PATH)
    if spec is None or spec.loader is None:
        raise AssertionError("无法为 %s 创建 import spec" % MODULE_PATH)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    missing = [n for n in ("humanize", "WatchData", "Handler") if not hasattr(module, n)]
    if missing:
        raise AssertionError("实现缺少约定 D 的接口: %s" % ", ".join(missing))
    humanize = module.humanize
    # 测试用本进程的 pid 冒充运行中的 xay 进程，跳过"命令行是 xay"这一层核对（该核对另有专门测试）
    module.WatchData._xay_alive = lambda self, pid, marker="xay-": self._alive(pid)
    WatchData = module.WatchData
    Handler = module.Handler


# ---------------------------------------------------------------------------
# 通用小工具
# ---------------------------------------------------------------------------

def _json(path, obj):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(obj, ensure_ascii=False), encoding="utf-8")


def _clock(ts):
    return time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(ts))


def _real(path):
    return os.path.realpath(str(path))


def _dead_pid():
    """启动一个进程并等它退出，返回一个几乎不可能被复用的 pid。"""
    proc = subprocess.Popen([sys.executable, "-c", "pass"])
    proc.wait(timeout=30)
    return proc.pid


def _run_events(agent, session, tokens):
    if tokens is None:
        return ""
    if agent == "codex":
        return "\n".join([
            json.dumps({"type": "thread.started", "thread_id": session}),
            json.dumps({
                "type": "turn.completed",
                "usage": {"input_tokens": max(0, tokens - 10), "output_tokens": 10},
            }),
        ]) + "\n"
    if agent == "dsh":
        return "\n".join([
            json.dumps({"type": "session", "sessionId": session}),
            json.dumps({"type": "status", "phase": "step_start", "turn": 1, "step": 1}),
            json.dumps({
                "type": "status", "phase": "step_end", "turn": 1, "step": 1,
                "usage": {"totalTokens": tokens},
            }),
        ]) + "\n"
    return "\n".join([
        json.dumps({"type": "init", "conversation_id": session}),
        json.dumps({
            "type": "result",
            "result": {
                "conversation_id": session, "status": "success", "response": "",
                "usage": {"total_tokens": tokens},
            },
        }),
    ]) + "\n"


class XayHome:
    """一个临时 XAY_HOME + 项目目录，按约定 1/2/3 造登记表和运行目录。"""

    def __init__(self, root):
        self.root = Path(root)
        self.home = self.root / "xayhome"
        self.base = self.root / "proj"
        self.home.mkdir(parents=True, exist_ok=True)
        (self.home / "slots").mkdir(exist_ok=True)
        self.base.mkdir(parents=True, exist_ok=True)
        self.registry = self.home / "registry.jsonl"
        self.registry.write_text("", encoding="utf-8")

    def _append(self, obj):
        with self.registry.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(obj, ensure_ascii=False) + "\n")

    def state(self):
        return WatchData(str(self.home)).state()

    def add_run(self, run_id, *, agent="codex", name=None, started=None,
                status="done", duration=60, dag="", task="", session=None,
                resume="", title=None, tokens=None, changed_files=None,
                pid=None, notice="", land="", isolated=True, result=None,
                prompt=None, events=None, stderr=None, queued=False,
                write_meta=True, model=None):
        now = int(time.time())
        if started is None:
            started = now - max(duration, 1)
        finished = status in ("done", "failed", "timeout", "stopped")
        ended = started + duration if finished else None
        if name is None:
            name = run_id
        if title is None:
            title = "任务 " + run_id
        if session is None:
            session = "sess-" + run_id
        if model is None:
            model = _AGENT_DEFAULT_MODEL.get(agent, "默认")
        if changed_files is None:
            changed_files = []
        if pid is None:
            pid = os.getpid()
        if prompt is None:
            prompt = "# 任务\n%s\n\n---\n规则：不要提交\n" % title
        if result is None:
            result = "结果：完成\n" if status == "done" else "结果：未完成\n"
        if stderr is None:
            stderr = ""

        run_dir = self.base / ".xay" / "runs" / run_id
        run_dir.mkdir(parents=True, exist_ok=True)

        start = {
            "run": run_id, "agent": agent, "name": name, "effort": "medium",
            "model": model, "readonly": False, "started": started,
            "timeout": 1800, "workdir": str(self.base), "run_dir": str(run_dir),
            "dag": dag, "task": task, "pid": pid, "title": title,
            "origin": str(self.base), "isolated": isolated,
            "base_commit": "", "resume": resume,
        }
        _json(run_dir / "start.json", start)
        (run_dir / "prompt.md").write_text(prompt, encoding="utf-8")
        (run_dir / "stderr.log").write_text(stderr, encoding="utf-8")
        (run_dir / "result.md").write_text(result, encoding="utf-8")
        if events is None:
            events = _run_events(agent, session, tokens)
        if isinstance(events, (list, tuple)):
            events = "\n".join(events) + "\n"
        (run_dir / "events.raw").write_text(events, encoding="utf-8")
        if queued:
            (run_dir / "queued").touch()

        if write_meta:
            meta = {
                "run": run_id, "agent": agent, "name": name, "status": status,
                "session_id": session, "model": model, "effort": "medium",
                "exit": 0 if status == "done" else 1, "duration_s": duration,
                "workdir": str(self.base), "run_dir": str(run_dir),
                "dag": dag, "task": task, "started": started, "ended": ended,
                "changed_files": list(changed_files), "deleted_files": [],
                "title": title, "origin": str(self.base), "isolated": isolated,
                "base_commit": "", "resume": resume, "land": land,
                "notice": notice,
            }
            _json(run_dir / "meta.json", meta)

        self._append({
            "kind": "run", "run_dir": str(run_dir), "workdir": str(self.base),
            "agent": agent, "name": name, "dag": dag, "task": task,
            "time": _clock(started), "title": title, "origin": str(self.base),
            "isolated": isolated, "base_commit": "", "resume": resume,
        })
        return run_dir

    def add_dag(self, stamp, tasks, *, title="", started=None, ended=None,
                pid=_UNSET, layers=None):
        now = int(time.time())
        if started is None:
            started = now - 60
        dag_dir = self.base / ".xay" / "dag" / stamp
        dag_dir.mkdir(parents=True, exist_ok=True)
        if pid is _UNSET:
            pid = os.getpid()
        full_tasks = []
        for item in tasks:
            task = {
                "id": item["id"],
                "agent": item.get("agent", "codex"),
                "name": item.get("name", item["id"]),
                "deps": list(item.get("deps", [])),
                "effort": item.get("effort", "medium"),
                "state": item.get("state", "pending"),
                "started": item.get("started"),
                "ended": item.get("ended"),
                "workdir": item.get("workdir", str(self.base)),
                "run_dir": item.get("run_dir"),
                "prompt": item.get("prompt", ""),
                "title": item.get("title", item["id"]),
            }
            if "notice" in item:
                task["notice"] = item["notice"]
            full_tasks.append(task)
        state = {
            "dag": stamp, "dag_dir": str(dag_dir), "base": str(self.base),
            "started": started, "ended": ended, "pid": pid, "title": title,
            "tasks": full_tasks,
        }
        if layers is not None:
            state["layers"] = [list(layer) for layer in layers]
        _json(dag_dir / "state.json", state)
        self._append({
            "kind": "dag", "dag_dir": str(dag_dir), "base": str(self.base),
            "time": _clock(started), "title": title,
        })
        return dag_dir


def find_mission(state, mission_id):
    target = _real(mission_id)
    for mission in state["missions"]:
        if _real(mission["id"]) == target:
            return mission
    raise AssertionError("state 里没有 mission: %s" % mission_id)


def find_sub(mission, sub_id):
    for sub in mission["subtasks"]:
        if sub["id"] == sub_id:
            return sub
    raise AssertionError("mission 里没有子任务: %s" % sub_id)


def find_run(state, run_dir):
    target = _real(run_dir)
    for run in state["runs"]:
        if _real(run["id"]) == target:
            return run
    raise AssertionError("state 里没有 run: %s" % run_dir)


# ---------------------------------------------------------------------------
# D1 humanize
# ---------------------------------------------------------------------------

class HumanizeTest(unittest.TestCase):
    def h(self, kind, text, state="done"):
        result = humanize({"kind": kind, "text": text, "state": state})
        self.assertEqual(set(result), {"kind", "text", "raw", "state"})
        self.assertEqual(result["kind"], kind)
        self.assertEqual(result["state"], state)
        self.assertEqual(result["raw"], text)
        return result

    def test_d1_table_cases(self):
        cases = [
            ("python3 -m unittest tests/test_x.py -v", "运行测试"),
            ("cd /a/b && pytest -q", "运行测试"),
            ("sed -n 1,80p scripts/xay-run", "查看 xay-run"),
            ("cat spec.md", "查看 spec.md"),
            ('rg -n "def main" src', "查找文件或内容"),
            ("git diff --stat", "查看改动记录"),
            ("npm install", "安装依赖"),
            ("python3 tools/gen.py --out a.json", "运行脚本 gen.py"),
            ("bash -n scripts/xay-run", "检查代码能否运行"),
            ("ls -la && python3 -m pytest", "运行测试"),
            ("echo hi", "运行命令"),
        ]
        for raw, expected in cases:
            with self.subTest(raw=raw):
                self.assertEqual(self.h("cmd", raw)["text"], expected)

    def test_cmd_test_priority_beats_first_segment(self):
        for raw in (
            "echo hi && npm test",
            "echo hi || go test ./...",
            "echo hi; cargo test",
            "echo hi | vitest run",
        ):
            with self.subTest(raw=raw):
                self.assertEqual(self.h("cmd", raw)["text"], "运行测试")

    def test_cmd_strips_cd_env_and_sudo(self):
        self.assertEqual(self.h("cmd", "FOO=1 sudo npm run build")["text"],
                         "检查代码能否运行")
        self.assertEqual(self.h("cmd", "FOO=1 BAR=2 ls -la")["text"],
                         "查找文件或内容")
        self.assertEqual(self.h("cmd", "sudo git status")["text"],
                         "查看改动记录")

    def test_cmd_pipe_uses_first_segment(self):
        self.assertEqual(self.h("cmd", "cat a.txt | wc -l")["text"], "查看 a.txt")

    def test_cmd_check_code(self):
        for raw in (
            "npm run build", "pnpm build", "tsc --noEmit", "make",
            "cargo build", "go build ./...", "py_compile x.py", "node --check a.js",
        ):
            with self.subTest(raw=raw):
                self.assertEqual(self.h("cmd", raw)["text"], "检查代码能否运行")

    def test_cmd_install(self):
        for raw in ("npm i left-pad", "pnpm add x", "pnpm install",
                    "pip3 install requests", "brew install jq", "uv add rich"):
            with self.subTest(raw=raw):
                self.assertEqual(self.h("cmd", raw)["text"], "安装依赖")

    def test_cmd_git_history(self):
        for raw in ("git status", "git log --oneline", "git show HEAD"):
            with self.subTest(raw=raw):
                self.assertEqual(self.h("cmd", raw)["text"], "查看改动记录")

    def test_cmd_view_file(self):
        self.assertEqual(self.h("cmd", "tail -n 20 out.log")["text"], "查看 out.log")
        self.assertEqual(self.h("cmd", "wc -l src/a.py")["text"], "查看 a.py")
        self.assertEqual(self.h("cmd", "sed -n 1,10p foo/bar.txt")["text"], "查看 bar.txt")
        self.assertEqual(self.h("cmd", "cat")["text"], "查看文件")

    def test_cmd_find(self):
        for raw in ("grep -rn TODO .", "find . -name '*.py'", "ls -la",
                    "fd xay", "tree -L 2"):
            with self.subTest(raw=raw):
                self.assertEqual(self.h("cmd", raw)["text"], "查找文件或内容")

    def test_cmd_network_and_file_ops(self):
        self.assertEqual(self.h("cmd", "curl -s https://example.com")["text"], "访问网址")
        self.assertEqual(self.h("cmd", "wget -q url")["text"], "访问网址")
        for raw in ("mkdir -p build", "cp a b", "mv a b", "rm -rf dist",
                    "touch x", "chmod +x x", "ln -s a b"):
            with self.subTest(raw=raw):
                self.assertEqual(self.h("cmd", raw)["text"], "整理文件")

    def test_cmd_script(self):
        self.assertEqual(self.h("cmd", "python3 tools/gen.py --out a.json")["text"],
                         "运行脚本 gen.py")
        self.assertEqual(self.h("cmd", "node app.js")["text"], "运行脚本 app.js")
        self.assertEqual(self.h("cmd", "bash scripts/build.sh")["text"],
                         "运行脚本 build.sh")
        self.assertEqual(self.h("cmd", "sh ./run.sh")["text"], "运行脚本 run.sh")

    def test_edit_read_search_think(self):
        self.assertEqual(self.h("edit", "a.py")["text"], "修改 a.py")
        self.assertEqual(self.h("read", "docs/spec.md")["text"], "查看 docs/spec.md")
        self.assertEqual(self.h("search", "python slugify")["text"],
                         "上网搜索：python slugify")
        self.assertEqual(self.h("think", "想一下")["text"], "思考")

    def test_say(self):
        self.assertEqual(self.h("say", "第一行\n第二行")["text"], "回复：第一行 第二行")
        raw = "一" * 40
        self.assertEqual(self.h("say", raw)["text"], "回复：" + "一" * 30)

    def test_plan(self):
        self.assertEqual(self.h("plan", "整理待办")["text"], "制定计划")
        self.assertEqual(self.h("plan", "计划 2 项")["text"], "制定计划（2 项）")
        self.assertEqual(self.h("plan", "计划 12 项")["text"], "制定计划（12 项）")

    def test_tool(self):
        self.assertEqual(self.h("tool", "browser: https://example.com")["text"],
                         "使用工具 browser")
        self.assertEqual(self.h("tool", "read_file: a.txt")["text"],
                         "使用工具 read_file")

    def test_other_kind_kept(self):
        self.assertEqual(self.h("note", "随便一句")["text"], "随便一句")

    def test_text_is_truncated_to_40(self):
        result = self.h("edit", "很长的文件名" * 20)
        self.assertLessEqual(len(result["text"]), 40)
        self.assertTrue(result["text"].endswith("…"))
        self.assertEqual(result["raw"], "很长的文件名" * 20)

        other = self.h("note", "长" * 100)
        self.assertLessEqual(len(other["text"]), 40)
        self.assertTrue(other["text"].endswith("…"))


# ---------------------------------------------------------------------------
# Mission 聚合：DAG
# ---------------------------------------------------------------------------

class DagMissionTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="xay-watch-v2-")
        self.h = XayHome(self.tmp.name)
        now = int(time.time())
        stamp = "20261029-100000"
        self.dag_dir = self.h.base / ".xay" / "dag" / stamp

        self.run_a = self.h.add_run(
            "20261029-100000-codex-a", agent="codex", started=now - 1000,
            duration=100, dag=str(self.dag_dir), task="a", title="写服务端",
            session="sess-a", changed_files=["server.py"], tokens=120,
        )
        self.run_b = self.h.add_run(
            "20261029-100000-dsh-b", agent="dsh", started=now - 900,
            duration=80, dag=str(self.dag_dir), task="b", title="写测试",
            session="sess-b", tokens=500,
        )
        self.run_b2 = self.h.add_run(
            "20261029-101000-dsh-b", agent="dsh", started=now - 800,
            duration=50, dag=str(self.dag_dir), task="b", title="写测试",
            session="sess-b2", resume="sess-b", tokens=300,
        )
        self.run_b3 = self.h.add_run(
            "20261029-102000-dsh-b", agent="dsh", started=now - 700,
            duration=40, dag=str(self.dag_dir), task="b", title="写测试",
            session="sess-b3", resume="sess-b2", tokens=200,
        )
        active_events = "\n".join([
            json.dumps({"type": "thread.started", "thread_id": "sess-c"}),
            json.dumps({
                "type": "item.started",
                "item": {
                    "id": "item_c", "type": "command_execution",
                    "command": "/bin/zsh -lc 'echo hi'",
                    "aggregated_output": "", "exit_code": None,
                    "status": "in_progress",
                },
            }),
        ]) + "\n"
        self.run_c = self.h.add_run(
            "20261029-103000-codex-c", agent="codex", started=now - 300,
            duration=0, status="running", dag=str(self.dag_dir), task="c",
            title="合并", session="sess-c", write_meta=False, events=active_events,
            changed_files=["merged.py"],
        )

        self.dag_dir = self.h.add_dag(
            stamp,
            [
                {"id": "a", "agent": "codex", "state": "done", "deps": [],
                 "title": "写服务端", "started": now - 1000, "ended": now - 900,
                 "run_dir": str(self.run_a)},
                {"id": "b", "agent": "dsh", "state": "done", "deps": [],
                 "title": "写测试", "started": now - 900, "ended": now - 820,
                 "run_dir": str(self.run_b)},
                {"id": "c", "agent": "codex", "state": "running", "deps": ["a", "b"],
                 "title": "合并", "started": now - 300, "run_dir": None},
                {"id": "d", "agent": "agy", "state": "pending", "deps": ["c"],
                 "title": "复查", "run_dir": None},
            ],
            title="给看板加多页面",
            started=now - 1000,
            layers=[["a", "b"], ["c"], ["d"]],
        )
        self.state = self.h.state()
        self.mission = find_mission(self.state, self.dag_dir)

    def tearDown(self):
        self.tmp.cleanup()

    def test_mission_basics(self):
        m = self.mission
        self.assertEqual(m["kind"], "dag")
        self.assertEqual(_real(m["id"]), _real(self.dag_dir))
        self.assertEqual(m["title"], "给看板加多页面")
        self.assertEqual(m["status"], "running")
        self.assertEqual(m["workdir"], self.h.base.name)
        self.assertEqual(m["total"], 4)
        self.assertEqual(m["done"], 2)
        self.assertEqual(m["running"], 1)
        self.assertEqual(m["failed"], 0)
        self.assertIsNone(m["ended"])
        self.assertGreaterEqual(m["elapsed_s"], 0)
        self.assertEqual(m["layers"], [["a", "b"], ["c"], ["d"]])

    def test_subtask_status_batch_deps(self):
        m = self.mission
        self.assertEqual([sub["id"] for sub in m["subtasks"]], ["a", "b", "c", "d"])
        self.assertEqual([sub["batch"] for sub in m["subtasks"]], [0, 0, 1, 2])
        self.assertEqual(find_sub(m, "a")["status"], "done")
        self.assertEqual(find_sub(m, "b")["status"], "done")
        self.assertEqual(find_sub(m, "c")["status"], "running")
        self.assertEqual(find_sub(m, "d")["status"], "pending")
        self.assertEqual(find_sub(m, "c")["deps"], ["a", "b"])
        self.assertEqual(find_sub(m, "d")["deps"], ["c"])

    def test_resume_rework_is_grouped_and_counts_rounds(self):
        m = self.mission
        sub_b = find_sub(m, "b")
        self.assertEqual(sub_b["rounds"], 3)
        self.assertEqual(_real(sub_b["run"]), _real(self.run_b3))
        # 返工运行不能再单独成为 Mission
        ids = [_real(x["id"]) for x in self.state["missions"]]
        self.assertNotIn(_real(self.run_b2), ids)
        self.assertNotIn(_real(self.run_b3), ids)
        self.assertEqual(sub_b["status"], "done")

    def test_subtask_now_uses_active_action(self):
        sub_c = find_sub(self.mission, "c")
        self.assertIsNotNone(sub_c["now"])
        self.assertEqual(sub_c["now"]["kind"], "cmd")
        self.assertEqual(sub_c["now"]["raw"], "echo hi")
        self.assertEqual(sub_c["now"]["text"], "运行命令")
        self.assertEqual(sub_c["now"]["state"], "active")
        self.assertGreaterEqual(sub_c["elapsed_s"], 0)

    def test_run_belongs_to_mission(self):
        run = find_run(self.state, self.run_c)
        self.assertEqual(_real(run["mission"]), _real(self.dag_dir))
        self.assertEqual(run["status"], "running")
        self.assertEqual(run["workdir"], self.h.base.name)
        self.assertEqual(run["agent"], "codex")

    def test_agents_shape(self):
        self.assertEqual(set(self.state["agents"]), {"codex", "dsh", "agy"})
        self.assertEqual(self.state["agents"]["codex"]["role"], "写核心代码")
        self.assertEqual(self.state["agents"]["dsh"]["role"], "测试与批量活")
        self.assertEqual(self.state["agents"]["agy"]["role"], "文案、界面与调研")


# ---------------------------------------------------------------------------
# Mission 聚合：单独运行与返工链
# ---------------------------------------------------------------------------

class SingleMissionTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="xay-watch-v2-")
        self.h = XayHome(self.tmp.name)
        now = int(time.time())
        self.run_1 = self.h.add_run(
            "20261029-110000-codex-doc", started=now - 600, duration=100,
            title="写文档", session="sess-doc", tokens=100,
        )
        self.run_2 = self.h.add_run(
            "20261029-111000-codex-doc", started=now - 400, duration=60,
            title="写文档", session="sess-doc-2", resume="sess-doc", tokens=50,
        )
        self.run_3 = self.h.add_run(
            "20261029-112000-codex-doc", started=now - 200, duration=30,
            title="写文档", session="sess-doc-3", resume="sess-doc-2", tokens=30,
        )
        self.state = self.h.state()

    def tearDown(self):
        self.tmp.cleanup()

    def test_single_run_is_one_mission(self):
        self.assertEqual(len(self.state["missions"]), 1)
        m = self.state["missions"][0]
        self.assertEqual(m["kind"], "single")
        self.assertEqual(_real(m["id"]), _real(self.run_1))
        self.assertEqual(m["title"], "写文档")
        self.assertEqual(m["status"], "done")
        self.assertEqual(m["total"], 1)
        self.assertEqual(m["done"], 1)
        self.assertEqual(len(m["subtasks"]), 1)
        self.assertEqual(m["subtasks"][0]["rounds"], 3)
        self.assertEqual(_real(m["subtasks"][0]["run"]), _real(self.run_3))
        self.assertIsNotNone(m["ended"])

    def test_resume_chain_is_one_mission(self):
        ids = {_real(m["id"]) for m in self.state["missions"]}
        self.assertEqual(ids, {_real(self.run_1)})
        for run_dir in (self.run_2, self.run_3):
            run = find_run(self.state, run_dir)
            self.assertEqual(_real(run["mission"]), _real(self.run_1))


# ---------------------------------------------------------------------------
# Mission status 规则与排序
# ---------------------------------------------------------------------------

class MissionStatusTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="xay-watch-v2-")
        self.h = XayHome(self.tmp.name)
        now = int(time.time())

        # done
        done_dag_dir = self.h.base / ".xay" / "dag" / "20261029-120000"
        done_a = self.h.add_run("run-done-a", started=now - 900, duration=50,
                                dag=str(done_dag_dir), task="a", tokens=10)
        done_b = self.h.add_run("run-done-b", started=now - 850, duration=50,
                                dag=str(done_dag_dir), task="b", tokens=10)
        self.dag_done = self.h.add_dag(
            "20261029-120000",
            [
                {"id": "a", "state": "done", "deps": [], "started": now - 900,
                 "ended": now - 850, "run_dir": str(done_a)},
                {"id": "b", "state": "done", "deps": ["a"], "started": now - 850,
                 "ended": now - 800, "run_dir": str(done_b)},
            ],
            title="都做完了", started=now - 900, ended=now - 800,
            layers=[["a"], ["b"]],
        )

        # failed
        failed_dag_dir = self.h.base / ".xay" / "dag" / "20261029-121000"
        failed_a = self.h.add_run("run-failed-a", started=now - 800, duration=40,
                                  status="failed", dag=str(failed_dag_dir),
                                  task="a", notice="命令失败", tokens=10)
        self.dag_failed = self.h.add_dag(
            "20261029-121000",
            [
                {"id": "a", "state": "failed", "deps": [], "started": now - 800,
                 "ended": now - 760, "run_dir": str(failed_a), "notice": "命令失败"},
            ],
            title="失败的任务", started=now - 800, ended=now - 760,
            layers=[["a"]],
        )

        # stopped（没有 failed 时优先 stopped）
        stopped_dag_dir = self.h.base / ".xay" / "dag" / "20261029-122000"
        stopped_a = self.h.add_run("run-stopped-a", started=now - 700,
                                   duration=30, status="stopped",
                                   dag=str(stopped_dag_dir), task="a", tokens=10)
        self.dag_stopped = self.h.add_dag(
            "20261029-122000",
            [
                {"id": "a", "state": "stopped", "deps": [], "started": now - 700,
                 "ended": now - 670, "run_dir": str(stopped_a)},
                {"id": "b", "state": "skipped", "deps": ["a"], "run_dir": None},
            ],
            title="被停掉的任务", started=now - 700, ended=now - 670,
            layers=[["a"], ["b"]],
        )

        # queued
        queued_dag_dir = self.h.base / ".xay" / "dag" / "20261029-123000"
        queued_run = self.h.add_run("run-queued-q", started=now - 100,
                                    status="queued", dag=str(queued_dag_dir),
                                    task="q", queued=True, tokens=None)
        self.dag_queued = self.h.add_dag(
            "20261029-123000",
            [
                {"id": "q", "state": "running", "deps": [], "started": now - 100,
                 "run_dir": str(queued_run)},
            ],
            title="排队中的任务", started=now - 100, layers=[["q"]],
        )
        self.queued_run = queued_run

        # 进程意外退出：state.json 有 pid，但 pid 已死，仍有 running/pending
        self.dag_dead = self.h.add_dag(
            "20261029-124000",
            [
                {"id": "r", "state": "running", "deps": [], "started": now - 90,
                 "run_dir": None},
                {"id": "p", "state": "pending", "deps": ["r"], "run_dir": None},
            ],
            title="意外退出的任务", started=now - 90, pid=_dead_pid(),
            layers=[["r"], ["p"]],
        )
        self.state = self.h.state()

    def tearDown(self):
        self.tmp.cleanup()

    def test_status_rules(self):
        done = find_mission(self.state, self.dag_done)
        self.assertEqual(done["status"], "done")
        self.assertEqual(done["failed"], 0)

        failed = find_mission(self.state, self.dag_failed)
        self.assertEqual(failed["status"], "failed")
        self.assertEqual(failed["failed"], 1)
        self.assertEqual(failed["notice"], "命令失败")

        stopped = find_mission(self.state, self.dag_stopped)
        self.assertEqual(stopped["status"], "stopped")
        self.assertEqual(find_sub(stopped, "b")["status"], "skipped")

    def test_queued_run_makes_mission_queued(self):
        queued = find_mission(self.state, self.dag_queued)
        self.assertEqual(queued["status"], "queued")
        self.assertEqual(find_sub(queued, "q")["status"], "queued")
        run = find_run(self.state, self.queued_run)
        self.assertEqual(run["status"], "queued")

    def test_dead_dag_pid_marks_failed_with_notice(self):
        dead = find_mission(self.state, self.dag_dead)
        self.assertEqual(dead["status"], "failed")
        self.assertEqual(dead["notice"], "任务进程意外退出")

    def test_ordering_running_queued_then_finished(self):
        # 单独建一份数据，避免 ended 为 null 的失败任务干扰排序断言。
        with tempfile.TemporaryDirectory(prefix="xay-watch-order-") as tmp:
            h = XayHome(tmp)
            now = int(time.time())
            dag_running_path = h.base / ".xay" / "dag" / "20261029-125000"
            dag_queued_path = h.base / ".xay" / "dag" / "20261029-126000"
            dag_failed_path = h.base / ".xay" / "dag" / "20261029-127000"
            dag_done_path = h.base / ".xay" / "dag" / "20261029-128000"
            done_run = h.add_run("run-o-done", started=now - 5000, duration=50,
                                 dag=str(dag_done_path), task="d", tokens=10)
            failed_run = h.add_run("run-o-fail", started=now - 4000, duration=40,
                                   status="failed", dag=str(dag_failed_path),
                                   task="f", tokens=10)
            running_run = h.add_run("run-o-run", started=now - 1000,
                                    status="running", dag=str(dag_running_path),
                                    task="x", write_meta=False, tokens=0)
            queued_run = h.add_run("run-o-queue", started=now - 2000,
                                   status="queued", dag=str(dag_queued_path),
                                   task="y", queued=True, tokens=0)
            dag_running = h.add_dag(
                "20261029-125000",
                [{"id": "x", "state": "running", "deps": [],
                  "started": now - 1000, "run_dir": str(running_run)}],
                title="正在跑", started=now - 1000, layers=[["x"]],
            )
            dag_queued = h.add_dag(
                "20261029-126000",
                [{"id": "y", "state": "running", "deps": [],
                  "started": now - 2000, "run_dir": str(queued_run)}],
                title="排队中", started=now - 2000, layers=[["y"]],
            )
            dag_failed = h.add_dag(
                "20261029-127000",
                [{"id": "f", "state": "failed", "deps": [],
                  "started": now - 4000, "ended": now - 2000,
                  "run_dir": str(failed_run)}],
                title="失败", started=now - 4000, ended=now - 2000,
                layers=[["f"]],
            )
            dag_done = h.add_dag(
                "20261029-128000",
                [{"id": "d", "state": "done", "deps": [],
                  "started": now - 5000, "ended": now - 3000,
                  "run_dir": str(done_run)}],
                title="完成", started=now - 5000, ended=now - 3000,
                layers=[["d"]],
            )
            state = h.state()
            order = [_real(m["id"]) for m in state["missions"]]
            expected = [_real(dag_running), _real(dag_queued),
                        _real(dag_failed), _real(dag_done)]
            self.assertEqual(order, expected)


# ---------------------------------------------------------------------------
# Agent：slots / today / week / stats
# ---------------------------------------------------------------------------

class AgentSlotsTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="xay-watch-v2-")
        self.h = XayHome(self.tmp.name)
        slots = self.h.home / "slots"
        (slots / "dsh-0").mkdir()
        (slots / "dsh-0" / "pid").write_text(str(os.getpid()), encoding="utf-8")
        (slots / "dsh-1").mkdir()
        (slots / "dsh-1" / "pid").write_text(str(_dead_pid()), encoding="utf-8")
        (slots / "codex-0").mkdir()
        (slots / "codex-0" / "pid").write_text(str(os.getpid()), encoding="utf-8")
        self.h.add_run("run-queued", agent="dsh", started=int(time.time()) - 20,
                       status="queued", queued=True)
        for key in list(os.environ):
            if key.startswith("XAY_SLOTS_"):
                os.environ.pop(key, None)
        self.state = self.h.state()

    def tearDown(self):
        self.tmp.cleanup()

    def test_slots(self):
        codex = self.state["agents"]["codex"]
        dsh = self.state["agents"]["dsh"]
        agy = self.state["agents"]["agy"]
        self.assertEqual(codex["slots"]["used"], 1)
        self.assertEqual(codex["slots"]["max"], 3)
        self.assertEqual(dsh["slots"]["used"], 1)
        self.assertEqual(dsh["slots"]["max"], 4)
        self.assertEqual(dsh["slots"]["queued"], 1)
        self.assertEqual(agy["slots"]["used"], 0)
        self.assertEqual(agy["slots"]["max"], 3)

    def test_model_and_defaults(self):
        self.assertEqual(self.state["agents"]["dsh"]["model"],
                         "deepseek-v4.1-flash")
        self.assertEqual(self.state["agents"]["agy"]["model"],
                         "gemini-3.8-flash-medium")


class AgentUsageTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="xay-watch-v2-")
        self.h = XayHome(self.tmp.name)
        now = int(time.time())
        today_start = int(datetime.now().replace(
            hour=0, minute=0, second=0, microsecond=0).timestamp())
        if now - today_start < 120:
            today_1 = today_start + 1
        else:
            today_1 = now - 300
        today_2 = min(now - 1, today_1 + 60)

        self.h.add_run("run-today-1", agent="dsh", started=today_1,
                       duration=100, tokens=500)
        self.h.add_run("run-today-2", agent="dsh", started=today_2,
                       duration=50, tokens=50, session="sess-rework",
                       resume="sess-run-today-1")
        self.h.add_run("run-week-1", agent="dsh", started=now - 3 * 86400,
                       duration=200, tokens=700)
        self.h.add_run("run-month-1", agent="dsh", started=now - 10 * 86400,
                       duration=300, status="failed", tokens=300)
        self.state = self.h.state()

    def tearDown(self):
        self.tmp.cleanup()

    def test_today_and_week(self):
        dsh = self.state["agents"]["dsh"]
        self.assertEqual(dsh["today"]["runs"], 2)
        self.assertEqual(dsh["today"]["tokens"], 550)
        self.assertEqual(dsh["week"]["runs"], 3)
        self.assertEqual(dsh["week"]["tokens"], 1250)

    def test_stats(self):
        stats = self.state["agents"]["dsh"]["stats"]
        self.assertEqual(stats["finished"], 4)
        self.assertAlmostEqual(stats["success_rate"], 0.75, places=4)
        self.assertAlmostEqual(stats["avg_s"], 350 / 3.0, delta=1.0)
        self.assertEqual(stats["reworks"], 1)


# ---------------------------------------------------------------------------
# detail()
# ---------------------------------------------------------------------------

class DetailTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="xay-watch-v2-")
        self.h = XayHome(self.tmp.name)
        now = int(time.time())
        stamp = "20261029-130000"
        self.dag_dir = self.h.base / ".xay" / "dag" / stamp
        prompt = "# 任务\n实现功能\n\n---\n规则：不要提交\n"
        events = "\n".join([
            json.dumps({"type": "thread.started", "thread_id": "sess-d1"}),
            json.dumps({
                "type": "item.started",
                "item": {"id": "i1", "type": "command_execution",
                         "command": "/bin/zsh -lc 'echo hi'", "status": "in_progress"},
            }),
            json.dumps({
                "type": "item.completed",
                "item": {"id": "i1", "type": "command_execution",
                         "command": "/bin/zsh -lc 'echo hi'", "exit_code": 0,
                         "status": "completed"},
            }),
            json.dumps({
                "type": "item.completed",
                "item": {"id": "i2", "type": "file_change",
                         "changes": [{"path": "a.py", "kind": "add"}],
                         "status": "completed"},
            }),
            json.dumps({
                "type": "item.completed",
                "item": {"id": "i3", "type": "agent_message", "text": "完成了"},
            }),
            json.dumps({"type": "turn.completed",
                        "usage": {"input_tokens": 10, "output_tokens": 5}}),
        ]) + "\n"
        stderr = "\n".join("line-%d" % i for i in range(1, 26)) + "\n"
        self.run_dir = self.h.add_run(
            "20261029-130000-codex-impl", agent="codex", started=now - 200,
            duration=100, dag=str(self.dag_dir), task="impl", title="实现功能",
            session="sess-d1", changed_files=["a.py"], prompt=prompt,
            events=events, stderr=stderr, result="结果：完成\n改动文件：a.py\n",
            tokens=15,
        )
        self.h.add_dag(
            stamp,
            [{"id": "impl", "agent": "codex", "state": "done", "deps": [],
              "title": "实现功能", "started": now - 200, "ended": now - 100,
              "run_dir": str(self.run_dir)}],
            title="实现功能", started=now - 200, ended=now - 100,
            layers=[["impl"]],
        )
        self.state = self.h.state()

    def tearDown(self):
        self.tmp.cleanup()

    def test_run_detail(self):
        detail = WatchData(str(self.h.home)).detail(str(self.run_dir))
        self.assertIn("run", detail)
        self.assertEqual(_real(detail["run"]["id"]), _real(self.run_dir))
        self.assertEqual(detail["mission_title"], "实现功能")
        self.assertEqual(detail["task"], "实现功能")

        self.assertEqual(len(detail["actions"]), 3)
        self.assertEqual(detail["actions"][0]["kind"], "cmd")
        self.assertEqual(detail["actions"][0]["text"], "运行命令")
        self.assertEqual(detail["actions"][0]["raw"], "echo hi")
        self.assertEqual(detail["actions"][1]["kind"], "edit")
        self.assertEqual(detail["actions"][1]["raw"], "a.py")
        self.assertEqual(detail["actions"][2]["kind"], "say")

        self.assertEqual(len(detail["rounds"]), 1)
        self.assertEqual(_real(detail["rounds"][0]["run"]), _real(self.run_dir))
        self.assertIn("完成", detail["result"])
        self.assertEqual(detail["changed_files"], ["a.py"])
        self.assertEqual(detail["notice"], "")

        tech = detail["tech"]
        self.assertEqual(_real(tech["run_dir"]), _real(self.run_dir))
        self.assertEqual(_real(tech["workdir"]), _real(self.h.base))
        tail = tech["stderr_tail"]
        self.assertIsInstance(tail, str)
        self.assertEqual(tail.splitlines()[0], "line-6")
        self.assertEqual(len(tail.splitlines()), 20)

    def test_mission_detail(self):
        detail = WatchData(str(self.h.home)).detail(str(self.dag_dir))
        self.assertIn("mission", detail)
        self.assertEqual(_real(detail["mission"]["id"]), _real(self.dag_dir))
        self.assertEqual(detail["mission"]["title"], "实现功能")

    def test_run_fields(self):
        run = find_run(self.state, self.run_dir)
        for key in ("id", "agent", "title", "status", "mission", "effort",
                    "model", "started", "ended", "elapsed_s", "step",
                    "tokens", "now", "changed_files", "isolated", "land",
                    "notice", "session", "workdir"):
            self.assertIn(key, run)
        self.assertEqual(run["model"], "GPT（默认）")
        self.assertEqual(run["step"], 3)
        self.assertEqual(run["tokens"], 15)


# ---------------------------------------------------------------------------
# POST /api/stop
# ---------------------------------------------------------------------------

class HandlerTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="xay-watch-v2-")
        self.h = XayHome(self.tmp.name)
        now = int(time.time())
        self.finished_run = self.h.add_run(
            "20261029-140000-codex-done", started=now - 300, duration=60,
            pid=_dead_pid(), title="已经结束", tokens=10,
        )
        self.old_env = os.environ.get("XAY_HOME")
        watch = WatchData(str(self.h.home))
        os.environ["XAY_HOME"] = str(self.h.home)
        self.old_data = getattr(Handler, "data", None)
        Handler.data = watch
        # 约定没有规定 Handler 怎么拿到数据；给常见的注入方式都设一份。
        for name in ("watch", "watch_data"):
            try:
                setattr(Handler, name, watch)
            except Exception:
                pass
        for name in ("xay_home", "home"):
            try:
                setattr(Handler, name, str(self.h.home))
            except Exception:
                pass
        # Handler 可能是类，也可能是 Handler(watch) 工厂；两种都支持。
        if isinstance(Handler, type):
            handler_cls = Handler
        else:
            try:
                handler_cls = Handler(watch)
            except Exception:
                handler_cls = Handler(str(self.h.home))
        for name in ("watch", "watch_data"):
            try:
                setattr(handler_cls, name, watch)
            except Exception:
                pass
        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), handler_cls)
        for name in ("watch", "watch_data"):
            try:
                setattr(self.httpd, name, watch)
            except Exception:
                pass
        self.httpd.xay_home = str(self.h.home)
        self.port = self.httpd.server_address[1]
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)
        self.thread.start()

    def tearDown(self):
        self.httpd.shutdown()
        self.httpd.server_close()
        self.thread.join(timeout=5)
        if self.old_env is None:
            os.environ.pop("XAY_HOME", None)
        else:
            os.environ["XAY_HOME"] = self.old_env
        Handler.data = self.old_data
        self.tmp.cleanup()

    def _post(self, body, with_header=True):
        request = urllib.request.Request(
            "http://127.0.0.1:%d/api/stop" % self.port,
            data=json.dumps(body).encode("utf-8"),
            method="POST",
            headers={"Content-Type": "application/json"},
        )
        if with_header:
            request.add_header("X-Xay-Action", "stop")
        try:
            with urllib.request.urlopen(request, timeout=5) as response:
                return response.status, response.read(), response.headers
        except urllib.error.HTTPError as exc:
            return exc.code, exc.read(), exc.headers

    def test_403_without_action_header(self):
        status, _body, headers = self._post({"id": str(self.finished_run)},
                                            with_header=False)
        self.assertEqual(status, 403)
        self.assertIsNone(headers.get("Access-Control-Allow-Origin"))

    def test_404_unknown_id(self):
        status, _body, _headers = self._post(
            {"id": str(self.h.base / "not-registered")})
        self.assertEqual(status, 404)

    def test_409_finished_run(self):
        status, body, _headers = self._post({"id": str(self.finished_run)})
        self.assertEqual(status, 409)
        payload = json.loads(body.decode("utf-8"))
        self.assertFalse(payload["ok"])
        self.assertIn("结束", payload["error"])

class PidReuseTest(unittest.TestCase):
    def test_reused_pid_is_not_xay(self):
        spec = importlib.util.spec_from_file_location("xay_watch_pid_check", MODULE_PATH)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        data = module.WatchData(tempfile.mkdtemp())
        # 本进程活着，但不是 xay-run：进程号被复用的情形，不能算"运行中"
        self.assertFalse(data._xay_alive(os.getpid(), "xay-run"))
        self.assertTrue(data._xay_alive(os.getpid(), "python"))
        self.assertFalse(data._xay_alive(_dead_pid(), "python"))


if __name__ == "__main__":
    unittest.main()
