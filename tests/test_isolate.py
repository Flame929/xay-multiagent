"""xay-run 自动独立工作区（worktree）测试。

按约定 B7 用 XAY_TEST_CMD 替代真实 CLI；XAY_HOME 和 XAY_NOTIFY=0 都指向临时目录，
不会碰真实的 ~/.xay，也不会调用任何 AI 命令。
"""

import json
import os
import shlex
import signal
import subprocess
import tempfile
import time
import unittest
from pathlib import Path

TESTS_DIR = Path(__file__).resolve().parent
PROJECT_ROOT = TESTS_DIR.parent
XAY_RUN = PROJECT_ROOT / "scripts" / "xay-run"



def read_registry(home):
    """registry.jsonl 按约定是 JSONL；也容忍 jq 输出的多行 JSON。"""
    path = Path(home) / "registry.jsonl"
    if not path.is_file():
        return []
    text = path.read_text(encoding="utf-8")
    decoder = json.JSONDecoder()
    items = []
    index = 0
    while True:
        while index < len(text) and text[index].isspace():
            index += 1
        if index >= len(text):
            break
        try:
            obj, index = decoder.raw_decode(text, index)
        except json.JSONDecodeError:
            # 最后一条可能正在写，忽略残缺的尾巴。
            break
        items.append(obj)
    return items


def git(cwd, *args):
    proc = subprocess.run(
        ["git", "-C", str(cwd)] + list(args),
        capture_output=True, text=True,
    )
    if proc.returncode != 0:
        raise AssertionError(
            "git %s 失败（%s）：%s" % (" ".join(args), cwd, proc.stderr.strip())
        )
    return proc


def make_repo(path):
    path.mkdir(parents=True, exist_ok=True)
    git(path, "init", "-q")
    (path / "tracked.txt").write_text("base\n", encoding="utf-8")
    git(path, "add", "-A")
    git(path, "-c", "user.name=xay", "-c", "user.email=xay@localhost",
        "commit", "-q", "-m", "init")


def wait_for(predicate, timeout=60, message="等待超时"):
    deadline = time.time() + timeout
    while time.time() < deadline:
        value = predicate()
        if value:
            return value
        time.sleep(0.05)
    raise AssertionError(message)


class IsolateTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="xay-iso-")
        self.root = Path(self.tmp.name)
        self.home = self.root / "xayhome"
        self.home.mkdir()
        self.proj = self.root / "proj"
        self.proj.mkdir()
        self._procs = []

    def tearDown(self):
        for proc in self._procs:
            if proc.poll() is None:
                proc.kill()
                try:
                    proc.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    pass
        self.tmp.cleanup()

    # -- 调用 xay-run -------------------------------------------------------

    def _env(self, test_cmd):
        env = os.environ.copy()
        for key in list(env):
            if key.startswith("XAY_SLOTS_"):
                env.pop(key)
        env["XAY_HOME"] = str(self.home)
        env["XAY_NOTIFY"] = "0"
        env["XAY_TEST_CMD"] = test_cmd
        for key in ("XAY_DAG_ID", "XAY_TASK_ID", "XAY_ISOLATE"):
            env.pop(key, None)
        return env

    def start(self, test_cmd, name, extra=(), cwd=None):
        argv = [str(XAY_RUN), "codex", "-n", name, "-e", "low"]
        argv.extend(extra)
        argv.append("测试任务")
        proc = subprocess.Popen(
            argv,
            cwd=str(cwd or self.proj),
            env=self._env(test_cmd),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        self._procs.append(proc)
        return proc

    def run_xay(self, test_cmd, name, extra=(), cwd=None, timeout=90):
        proc = self.start(test_cmd, name, extra=extra, cwd=cwd)
        out, err = proc.communicate(timeout=timeout)
        self.assertEqual(
            proc.returncode, 0,
            "xay-run 退出码 %s\nstdout:\n%s\nstderr:\n%s"
            % (proc.returncode, out[-2000:], err[-4000:]),
        )
        return out, err

    def run_dir(self, name):
        for entry in reversed(read_registry(self.home)):
            if entry.get("kind") != "run":
                continue
            path = entry.get("run_dir") or ""
            if entry.get("name") == name or Path(path).name.endswith("-" + name):
                return Path(path)
        raise AssertionError("登记表里没有运行 %r；现有记录：%r"
                             % (name, read_registry(self.home)))

    def meta(self, name):
        path = self.run_dir(name) / "meta.json"
        wait_for(lambda: path.is_file(), timeout=30,
                 message="没有等到 meta.json: %s" % path)
        return json.loads(path.read_text(encoding="utf-8"))

    # -- 断言 ---------------------------------------------------------------

    def worktree_path(self, run_id):
        return self.home / "worktrees" / ("%s-%s" % (self.proj.name, run_id))

    def run_id(self, meta):
        return meta.get("run") or Path(meta["run_dir"]).name

    def assert_worktree_gone(self, meta):
        run_id = self.run_id(meta)
        self.assertFalse(
            self.worktree_path(run_id).exists(),
            "worktree 应该已删除: %s" % self.worktree_path(run_id),
        )
        branches = git(self.proj, "branch", "--list", "xay/" + run_id).stdout.strip()
        self.assertEqual(branches, "", "分支应该已删除: xay/%s" % run_id)

    def assert_no_worktrees(self):
        worktrees = self.home / "worktrees"
        if not worktrees.is_dir():
            return
        self.assertEqual(list(worktrees.iterdir()), [])

    # -- 测试 ---------------------------------------------------------------

    def test_non_git_dir_gets_repository(self):
        (self.proj / "note.txt").write_text("hello\n", encoding="utf-8")
        self.run_xay("printf 'changed\\n' > note.txt", "initrepo")

        self.assertTrue((self.proj / ".git").is_dir(), "非 git 目录应自动建仓库")
        meta = self.meta("initrepo")
        self.assertTrue(meta["isolated"])
        self.assertEqual(meta["status"], "done")
        self.assertEqual(meta["land"], "applied")
        self.assertEqual((self.proj / "note.txt").read_text(encoding="utf-8"),
                         "changed\n")
        self.assertIn("git 仓库", meta.get("notice", ""))
        # 初始快照已经提交，基准提交可用。
        head = git(self.proj, "rev-parse", "HEAD").stdout.strip()
        self.assertTrue(head)
        self.assert_worktree_gone(meta)

    def test_changes_are_landed_and_worktree_removed(self):
        make_repo(self.proj)
        self.run_xay(
            "printf 'new\\n' > tracked.txt && printf 'added\\n' > added.txt",
            "land",
        )
        meta = self.meta("land")
        self.assertTrue(meta["isolated"])
        self.assertEqual(meta["status"], "done")
        self.assertEqual(meta["land"], "applied")
        self.assertEqual((self.proj / "tracked.txt").read_text(encoding="utf-8"),
                         "new\n")
        self.assertEqual((self.proj / "added.txt").read_text(encoding="utf-8"),
                         "added\n")
        names = {Path(name).name for name in meta["changed_files"]}
        self.assertTrue({"tracked.txt", "added.txt"} <= names,
                        "changed_files 缺文件: %r" % meta["changed_files"])
        self.assert_worktree_gone(meta)

    def test_dirty_and_untracked_files_are_visible_in_worktree(self):
        make_repo(self.proj)
        (self.proj / "tracked.txt").write_text("dirty\n", encoding="utf-8")
        (self.proj / "untracked.txt").write_text("u\n", encoding="utf-8")

        (self.proj / "keep.txt").write_text("k\n", encoding="utf-8")

        # 未跟踪文件会成为基准的一部分：不动它就不会被当成“新增”而冲突，改了它则改动能搬回。
        cmd = (
            '[ "$(cat tracked.txt)" = dirty ] '
            '&& [ -f untracked.txt ] '
            "&& printf 'u2\\n' > untracked.txt "
            "&& printf 'seen\\n' > seen.txt"
        )
        self.run_xay(cmd, "dirty")
        meta = self.meta("dirty")
        self.assertEqual(meta["status"], "done")
        self.assertEqual(meta["land"], "applied")
        self.assertTrue((self.proj / "seen.txt").is_file(),
                        "测试命令没有看到原目录的未提交改动/未跟踪文件")
        # 原目录的未提交改动保持原样；没动过的未跟踪文件不受影响；改过的未跟踪文件搬回了新内容。
        self.assertEqual((self.proj / "tracked.txt").read_text(encoding="utf-8"),
                         "dirty\n")
        self.assertEqual((self.proj / "keep.txt").read_text(encoding="utf-8"), "k\n")
        self.assertEqual((self.proj / "untracked.txt").read_text(encoding="utf-8"),
                         "u2\n")
        self.assertEqual({Path(n).name for n in meta["changed_files"]} & {"keep.txt", "tracked.txt"}, set(),
                         "changed_files 不应包含执行者没动过的文件: %r" % meta["changed_files"])

    def test_two_parallel_runs_land_different_files(self):
        make_repo(self.proj)
        barrier = self.root / "barrier"
        quoted = shlex.quote(str(barrier))
        wait_loop = "while [ ! -f %s ]; do sleep 0.05; done; " % quoted

        proc_one = self.start(wait_loop + "printf 'one\\n' > one.txt", "one",
                              extra=("--isolate", "on"))
        proc_two = self.start(wait_loop + "printf 'two\\n' > two.txt", "two",
                              extra=("--isolate", "on"))

        def both_worktrees_ready():
            run_dirs = {e["run_dir"] for e in read_registry(self.home)
                        if e.get("kind") == "run" and e.get("run_dir")}
            if len(run_dirs) < 2:
                return False
            return all(
                self.worktree_path(Path(run_dir).name).is_dir()
                for run_dir in run_dirs
            )

        wait_for(both_worktrees_ready, timeout=60,
                 message="两个并行运行没有各自建立 worktree")
        barrier.touch()

        for proc in (proc_one, proc_two):
            out, err = proc.communicate(timeout=120)
            self.assertEqual(
                proc.returncode, 0,
                "xay-run 退出码 %s\nstdout:\n%s\nstderr:\n%s"
                % (proc.returncode, out[-2000:], err[-4000:]),
            )

        meta_one = self.meta("one")
        meta_two = self.meta("two")
        self.assertEqual(meta_one["land"], "applied")
        self.assertEqual(meta_two["land"], "applied")
        self.assertEqual((self.proj / "one.txt").read_text(encoding="utf-8"), "one\n")
        self.assertEqual((self.proj / "two.txt").read_text(encoding="utf-8"), "two\n")

    def test_same_line_change_conflicts(self):
        make_repo(self.proj)
        (self.proj / "line.txt").write_text("base\n", encoding="utf-8")
        git(self.proj, "add", "line.txt")
        git(self.proj, "-c", "user.name=xay", "-c", "user.email=xay@localhost",
            "commit", "-q", "-m", "add line")

        barrier = self.root / "barrier"
        quoted = shlex.quote(str(barrier))
        wait_loop = "while [ ! -f %s ]; do sleep 0.05; done; " % quoted

        proc_a = self.start(wait_loop + "printf 'A\\n' > line.txt", "A",
                            extra=("--isolate", "on"))
        proc_b = self.start(wait_loop + "printf 'B\\n' > line.txt", "B",
                            extra=("--isolate", "on"))

        def both_worktrees_ready():
            run_dirs = {e["run_dir"] for e in read_registry(self.home)
                        if e.get("kind") == "run" and e.get("run_dir")}
            if len(run_dirs) < 2:
                return False
            return all(
                self.worktree_path(Path(run_dir).name).is_dir()
                for run_dir in run_dirs
            )

        wait_for(both_worktrees_ready, timeout=60,
                 message="两个运行没有各自建立 worktree")
        barrier.touch()

        for proc in (proc_a, proc_b):
            out, err = proc.communicate(timeout=120)
            self.assertEqual(
                proc.returncode, 0,
                "xay-run 退出码 %s\nstdout:\n%s\nstderr:\n%s"
                % (proc.returncode, out[-2000:], err[-4000:]),
            )

        meta_a = self.meta("A")
        meta_b = self.meta("B")
        metas = [meta_a, meta_b]
        self.assertEqual(sorted(m["land"] for m in metas),
                         ["applied", "conflict"])
        self.assertEqual(meta_a["status"], "done")
        self.assertEqual(meta_b["status"], "done")

        conflict = [m for m in metas if m["land"] == "conflict"][0]
        applied = [m for m in metas if m["land"] == "applied"][0]
        self.assertIn("冲突", conflict["notice"])
        self.assertTrue(self.worktree_path(self.run_id(conflict)).is_dir(),
                        "冲突时 worktree 应保留")
        self.assertFalse(self.worktree_path(self.run_id(applied)).exists(),
                         "成功搬回后 worktree 应删除")
        self.assertIn((self.proj / "line.txt").read_text(encoding="utf-8"),
                      ("A\n", "B\n"))

    def test_readonly_is_not_isolated(self):
        make_repo(self.proj)
        self.run_xay("printf 'ro\\n' > ro.txt", "readonly", extra=("--readonly",))
        meta = self.meta("readonly")
        self.assertFalse(meta["isolated"])
        self.assertEqual(meta["status"], "done")
        self.assertEqual(meta["land"], "")
        self.assertEqual(os.path.realpath(meta["workdir"]),
                         os.path.realpath(str(self.proj)))
        self.assertTrue((self.proj / "ro.txt").is_file())
        self.assert_no_worktrees()

    def test_failed_command_discards_worktree(self):
        make_repo(self.proj)
        proc = self.start("printf 'x\\n' > fail.txt; exit 3", "failed")
        proc.communicate(timeout=120)
        self.assertNotEqual(proc.returncode, 0)

        meta = self.meta("failed")
        self.assertEqual(meta["status"], "failed")
        self.assertEqual(meta["land"], "discarded")
        self.assertFalse((self.proj / "fail.txt").exists(),
                         "失败任务的改动不应该搬回")
        self.assert_worktree_gone(meta)

    def test_term_marks_stopped(self):
        make_repo(self.proj)
        proc = self.start("sleep 30", "term")
        wait_for(
            lambda: any(e.get("kind") == "run" and e.get("name") == "term"
                        for e in read_registry(self.home)),
            timeout=60, message="登记表里没有等到 term 运行",
        )
        run_dir = self.run_dir("term")
        wait_for(lambda: (run_dir / "start.json").is_file(), timeout=60,
                 message="没有等到 start.json")
        wait_for(lambda: self.worktree_path(run_dir.name).is_dir(), timeout=60,
                 message="没有等到 worktree")
        os.kill(proc.pid, signal.SIGTERM)
        proc.communicate(timeout=30)

        meta = self.meta("term")
        self.assertEqual(meta["status"], "stopped")
        self.assertEqual(meta["land"], "discarded")
        result = (run_dir / "result.md").read_text(encoding="utf-8")
        self.assertIn("已被手动停止", result)
        self.assert_worktree_gone(meta)
        slots = self.home / "slots"
        leftover = [p.name for p in slots.iterdir()
                    if p.name.startswith("codex-")] if slots.is_dir() else []
        self.assertEqual(leftover, [], "停止后应释放名额")


    def test_unisolated_counts_only_files_this_run_touched(self):
        make_repo(self.proj)
        (self.proj / "tracked.txt").write_text("别人先改的\n", encoding="utf-8")
        (self.proj / "other.log").write_text("别人的日志\n", encoding="utf-8")
        (self.proj / "same.txt").write_text("不动\n", encoding="utf-8")
        cmd = "printf 'x' >> other.log; echo new > mine.txt"
        self.run_xay(cmd, "snap", extra=("--isolate", "off"))
        meta = self.meta("snap")
        self.assertFalse(meta["isolated"])
        # tracked.txt / same.txt 开始前就脏、任务没动 → 不算；other.log 被这次任务追加 → 算
        self.assertEqual(sorted(meta["changed_files"]), ["mine.txt", "other.log"])
        self.assertEqual(meta.get("fail_reason"), "")

    def test_failed_run_records_fail_reason(self):
        make_repo(self.proj)
        proc = self.start("exit 3", "boom", extra=("--isolate", "off"))
        proc.communicate(timeout=90)
        meta = self.meta("boom")
        self.assertEqual(meta["status"], "failed")
        self.assertEqual(meta["fail_reason"], "exit 3")
        self.assertEqual(meta["changed_files"], [])

    # -- 具名 AI：同名再派活在原会话里接着做 ---------------------------------

    def _fake_session(self, name, sid):
        meta_path = self.run_dir(name) / "meta.json"
        meta = json.loads(meta_path.read_text(encoding="utf-8"))
        meta["session_id"] = sid
        meta_path.write_text(json.dumps(meta, ensure_ascii=False), encoding="utf-8")

    def test_named_worker_continues_same_session(self):
        make_repo(self.proj)
        self.run_xay("true", "w-first", extra=("--as", "表格", "--isolate", "off"))
        self._fake_session("w-first", "sess-42")
        self.run_xay("true", "w-second", extra=("--as", "表格", "--isolate", "off"))
        rows = [r for r in read_registry(self.home) if r.get("kind") == "run"]
        self.assertEqual([r.get("worker") for r in rows], ["表格", "表格"])
        self.assertEqual(rows[0].get("resume"), "")
        self.assertEqual(rows[1].get("resume"), "sess-42")
        prompt = (self.run_dir("w-second") / "prompt.md").read_text(encoding="utf-8")
        self.assertIn("接着上一轮", prompt)
        self.assertNotIn("__XAY_WORKDIR__", prompt)
        # --fresh 换新会话
        self.run_xay("true", "w-third", extra=("--as", "表格", "--fresh", "--isolate", "off"))
        self.assertEqual(read_registry(self.home)[-1].get("resume"), "")

    def test_named_worker_queues_while_busy_and_keeps_agent(self):
        make_repo(self.proj)
        first = self.start("sleep 3", "q-first", extra=("--as", "排队", "--isolate", "off"))
        wait_for(lambda: any(r.get("name") == "q-first" for r in read_registry(self.home)), message="第一轮没开始")
        wait_for(lambda: (self.run_dir("q-first") / "start.json").is_file(), message="第一轮没开始")
        t0 = time.time()
        self.run_xay("true", "q-second", extra=("--as", "排队", "--isolate", "off"))
        self.assertIsNotNone(first.poll(), "第二轮应该等第一轮做完才开始")
        self.assertGreater(time.time() - t0, 1.5)
        argv = [str(XAY_RUN), "dsh", "-n", "q-x", "--as", "排队", "--isolate", "off", "测试任务"]
        proc = subprocess.run(argv, cwd=str(self.proj), env=self._env("true"), capture_output=True, text=True, timeout=60)
        self.assertEqual(proc.returncode, 2)
        self.assertIn("不能当 dsh 用", proc.stderr)

    def _dag(self, plan, *extra, env_extra=None):
        path = self.root / "plan.json"
        path.write_text(json.dumps(plan, ensure_ascii=False), encoding="utf-8")
        env = self._env("true")
        env.update(env_extra or {})
        return subprocess.run([str(XAY_RUN.parent / "xay-dag"), str(path), *extra], cwd=str(self.proj), env=env,
                              capture_output=True, text=True, timeout=120)

    def test_dag_continue_resumes_previous_task_session(self):
        make_repo(self.proj)
        plan = {"workdir": str(self.proj), "title": "分批", "tasks": [
            {"id": "b1", "agent": "codex", "prompt": "第一批", "isolate": False},
            {"id": "b2", "agent": "codex", "prompt": "第二批", "continue": "b1", "isolate": False}]}
        proc = self._dag(plan, env_extra={"XAY_TEST_SESSION": "s-batch"})
        self.assertEqual(proc.returncode, 0, proc.stdout[-2000:] + proc.stderr[-2000:])
        rows = {r.get("task"): r for r in read_registry(self.home) if r.get("kind") == "run"}
        self.assertEqual(rows["b1"].get("resume"), "")
        self.assertEqual(rows["b2"].get("resume"), "s-batch")

    def test_dag_rejects_parallel_tasks_on_same_worker(self):
        plan = {"workdir": str(self.proj), "tasks": [
            {"id": "x", "agent": "dsh", "prompt": "甲", "worker": "同一个"},
            {"id": "y", "agent": "dsh", "prompt": "乙", "worker": "同一个"}]}
        proc = self._dag(plan, "--dry-run")
        self.assertNotEqual(proc.returncode, 0)
        self.assertIn("没有先后依赖", proc.stdout + proc.stderr)

if __name__ == "__main__":
    unittest.main()
