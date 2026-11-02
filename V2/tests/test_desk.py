"""桌子（V2/xay/desk/desk.py）测试：全部用临时目录里的一次性 git 仓库，不碰真实 ~/.xay 和本仓库。"""
import os
import shutil
import stat
import subprocess
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

from xay.desk import desk as dk
from xay.desk import evidence
from xay.desk.lock import land_lock
from xay.desk._git import DeskError

_ENV_KEYS = {
    "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t", "GIT_COMMITTER_NAME": "t",
    "GIT_COMMITTER_EMAIL": "t@t", "GIT_CONFIG_GLOBAL": os.devnull, "GIT_CONFIG_NOSYSTEM": "1",
}
_saved_env = {}


def setUpModule():
    for k, v in _ENV_KEYS.items():
        _saved_env[k] = os.environ.get(k)
        os.environ[k] = v


def tearDownModule():
    for k, v in _saved_env.items():
        if v is None:
            os.environ.pop(k, None)
        else:
            os.environ[k] = v


def sh(cwd, *args, ok=True):
    p = subprocess.run(["git", "-c", "user.name=t", "-c", "user.email=t@t", "-c", "commit.gpgsign=false", *args],
                       cwd=str(cwd), capture_output=True, text=True)
    if ok and p.returncode != 0:
        raise AssertionError("git %s 失败：%s" % (" ".join(args), p.stderr))
    return p.stdout


def write(path, text, mode=None):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    data = text if isinstance(text, bytes) else text.encode()
    path.write_bytes(data)
    if mode is not None:
        os.chmod(path, mode)


def make_repo(root):
    root = Path(root)
    root.mkdir(parents=True, exist_ok=True)
    sh(root, "init", "-q", "-b", "main", ".")
    write(root / ".gitignore", "node_modules/\n*.cache\nbuild/\n")
    write(root / "a.txt", "".join("line%d\n" % i for i in range(1, 11)))
    write(root / "b.txt", "bee\n")
    write(root / "src" / "main.py", "print('hi')\n")
    write(root / "tool.sh", "#!/bin/sh\necho hi\n", 0o644)
    sh(root, "add", "-A")
    sh(root, "commit", "-q", "-m", "init")
    return root


def edit_line(path, n, text):
    lines = Path(path).read_text().splitlines(keepends=True)
    lines[n - 1] = text + "\n"
    Path(path).write_text("".join(lines))


def fingerprint(repo):
    """项目"用户看得见的一切"：index 字节、status、stash、所有引用、工作区文件内容。"""
    repo = Path(repo)
    env = dict(os.environ, GIT_OPTIONAL_LOCKS="0")
    status = subprocess.run(["git", "status", "--porcelain=v1", "-uall", "-z"], cwd=repo, env=env,
                            capture_output=True).stdout
    tree = {}
    for dirpath, dirs, files in os.walk(repo):
        if ".git" in dirs:
            dirs.remove(".git")
        for f in files:
            p = Path(dirpath) / f
            rel = str(p.relative_to(repo))
            tree[rel] = ("link", os.readlink(p)) if p.is_symlink() else (p.read_bytes(), p.stat().st_mode)
    idx = repo / ".git" / "index"
    return {
        "index": idx.read_bytes() if idx.exists() else None,
        "status": status,
        "stash": sh(repo, "stash", "list"),
        # 桌子自己的分支和 refs/xay/ 引用不算"用户的引用"
        "refs": [l for l in sh(repo, "for-each-ref", "--format=%(refname) %(objectname)").splitlines()
                 if l and not l.startswith(("refs/heads/xay/", "refs/xay/"))],
        "head": sh(repo, "rev-parse", "HEAD", ok=False),
        "tree": tree,
    }


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(os.path.realpath(tempfile.mkdtemp(prefix="xay-desk-test-")))
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.origin = make_repo(self.tmp / "proj")
        self.home = self.tmp / "home"
        self.ws, self.em = "ws_test", "em_0001"

    def new_desk(self, em=None):
        return dk.ensure_desk(self.origin, self.ws, em or self.em, self.home)

    def git_o(self, *a):
        return sh(self.origin, *a)

    def status_o(self):
        return sh(self.origin, "status", "--porcelain", "-uall")


class SnapshotTests(Base):
    def test_snapshot_includes_uncommitted_and_untracked_and_leaves_origin_untouched(self):
        o = self.origin
        edit_line(o / "a.txt", 2, "unstaged edit")                     # 未暂存的修改
        write(o / "b.txt", "bee staged\n"); self.git_o("add", "b.txt")   # 已暂存的修改
        write(o / "b.txt", "bee staged\n")
        (o / "src" / "main.py").unlink()                                # 删除了被跟踪的文件
        write(o / "notes" / "todo.txt", "todo\n")                       # 未跟踪的新文件
        write(o / "x.cache", "ignored")                                 # 被忽略
        write(o / "node_modules" / "p" / "i.js", "ignored")
        write(o / ".xay" / "runs" / "r1" / "meta.json", "{}")           # 工位自己的目录
        write(o / "sub" / ".xay" / "z", "nested")
        sh(o, "stash", "list")
        before = fingerprint(o)

        snap = dk.snapshot_origin(o)

        self.assertEqual(fingerprint(o), before, "快照不能改动项目的 index/status/stash/引用/文件")
        show = lambda p: subprocess.run(["git", "show", "%s:%s" % (snap, p)], cwd=o, capture_output=True, text=True)
        self.assertIn("unstaged edit", show("a.txt").stdout)
        self.assertEqual(show("b.txt").stdout, "bee staged\n")
        self.assertNotEqual(show("src/main.py").returncode, 0, "被删除的文件快照里也没有")
        self.assertEqual(show("notes/todo.txt").stdout, "todo\n")
        for gone in ("x.cache", "node_modules/p/i.js", ".xay/runs/r1/meta.json", "sub/.xay/z"):
            self.assertNotEqual(show(gone).returncode, 0, gone + " 不该进快照")
        self.assertEqual(sh(o, "rev-parse", snap + "^").strip(), sh(o, "rev-parse", "HEAD").strip())
        self.assertEqual(dk.snapshot_origin(o), snap, "同一个状态应得到同一个快照")

    def test_clean_origin_snapshot_is_head(self):
        self.assertEqual(dk.snapshot_origin(self.origin), sh(self.origin, "rev-parse", "HEAD").strip())

    def test_untracked_only_change_is_not_clean(self):
        head = sh(self.origin, "rev-parse", "HEAD").strip()
        write(self.origin / "new.txt", "n")
        self.assertNotEqual(dk.snapshot_origin(self.origin), head)

    def test_unborn_repo(self):
        fresh = Path(self.tmp / "fresh")
        fresh.mkdir()
        sh(fresh, "init", "-q", "-b", "main", ".")
        write(fresh / "f.txt", "hello\n")
        snap = dk.snapshot_origin(fresh)
        self.assertEqual(sh(fresh, "show", snap + ":f.txt"), "hello\n")
        self.assertNotEqual(subprocess.run(["git", "rev-parse", "-q", "--verify", "HEAD"], cwd=fresh,
                                           capture_output=True).returncode, 0, "不能替用户造出 HEAD")

    def test_not_a_git_repo(self):
        plain = self.tmp / "plain"
        plain.mkdir()
        with self.assertRaises(DeskError):
            dk.snapshot_origin(plain)

    def test_same_second_same_size_edit_is_not_missed(self):
        """racy git：index 刚写过、同一秒内文件被改成同样长度，过了这一秒再拍快照也得带上这处改动。
        （以前复制 index 时没保留修改时间，这种情况会被当成没改——test_idempotent_and_layout 偶发失败就是它）"""
        fresh = self.tmp / "racy"
        fresh.mkdir()
        _to_next_second()
        sh(fresh, "init", "-q", "-b", "main", ".")
        write(fresh / "a.txt", "line1\nline2\nline3\n")
        sh(fresh, "add", "-A")
        sh(fresh, "commit", "-q", "-m", "init")
        write(fresh / "a.txt", "line1\nline2\ndirty\n")       # 和原来一样长
        _to_next_second()
        snap = dk.snapshot_origin(fresh)
        self.assertIn("dirty", sh(fresh, "show", snap + ":a.txt"))

    def test_same_second_same_size_edit_on_desk_is_a_change(self):
        """桌子上也一样：桌子的 index 刚写过（对齐 / 落地都会写），执行者同一秒内改了个同样长度的文件，
        证据（changes）里不能漏掉。"""
        d = dk.ensure_desk(self.origin, self.ws, self.em, self.home)
        dk.align(d, self.origin)
        _to_next_second()
        write(d / "a.txt", (d / "a.txt").read_bytes())        # 内容不变，只是这一秒碰过
        sh(d, "update-index", "-q", "--refresh")               # 这一秒里写了一次桌子的 index
        edit_line(d / "a.txt", 3, "dirty")
        _to_next_second()
        self.assertEqual(dk.changes(d)["changed_files"], ["a.txt"])


def _to_next_second():
    t = time.time()
    time.sleep(1.0 - (t - int(t)) + 0.01)


class EnsureDeskTests(Base):
    def test_idempotent_and_layout(self):
        edit_line(self.origin / "a.txt", 3, "dirty")
        before = fingerprint(self.origin)
        d1 = self.new_desk()
        d2 = self.new_desk()
        self.assertEqual(d1, d2)
        self.assertEqual(d1, self.home / "desks" / self.ws / self.em)
        self.assertEqual(sh(d1, "branch", "--show-current").strip(), "xay/emp/" + self.em)
        self.assertEqual(len(sh(self.origin, "worktree", "list").strip().splitlines()), 2)
        self.assertEqual(sh(self.origin, "branch", "--list", "xay/emp/*").count("xay/emp/"), 1)
        self.assertIn("dirty", (d1 / "a.txt").read_text(), "新桌子应带着项目里未提交的改动")
        st = dk.read_state(d1)
        self.assertEqual(st["base"], st["landed_upto"])
        self.assertEqual(st["base"], sh(d1, "rev-parse", "HEAD").strip())
        self.assertFalse(st["kept"])
        self.assertEqual(fingerprint(self.origin), before)

    def test_bad_names_rejected(self):
        for bad in ("../x", "a/b", "", ".hidden"):
            with self.assertRaises(DeskError):
                dk.ensure_desk(self.origin, self.ws, bad, self.home)

    def test_recreate_after_directory_lost_keeps_branch_commits(self):
        d = self.new_desk()
        write(d / "ai.txt", "ai work\n")
        sh(d, "add", "-A"); sh(d, "commit", "-q", "-m", "ai commit")
        shutil.rmtree(d)
        d2 = self.new_desk()
        self.assertEqual(d2, d)
        self.assertEqual((d2 / "ai.txt").read_text(), "ai work\n")
        r = dk.align(d2, self.origin)          # 执行者的提交还没落地，不能被对齐冲掉
        self.assertEqual(r.mode, "blocked")
        self.assertEqual(r.unmerged_files, ["ai.txt"])

    def test_state_file_lost_is_recovered_conservatively(self):
        d = self.new_desk()
        write(d / "wip.txt", "wip\n")
        (d / ".xay" / "desk.json").unlink()
        r = dk.align(d, self.origin)
        self.assertEqual(r.mode, "blocked")
        self.assertEqual(r.unmerged_files, ["wip.txt"])
        self.assertTrue(dk.read_state(d)["kept"])


class FlowTests(Base):
    def test_two_tasks_land_and_align(self):
        d = self.new_desk()
        r = dk.align(d, self.origin)
        self.assertEqual(r.mode, "reset")
        self.assertEqual(r.base, sh(self.origin, "rev-parse", "HEAD").strip())
        self.assertEqual(r.unmerged_files, [])
        self.assertEqual(dk.changes(d), {"changed_files": [], "diff_stat": {"files": 0, "add": 0, "del": 0}})

        # 任务 1：改 a.txt 第 2 行
        edit_line(d / "a.txt", 2, "task1 edit")
        write(d / ".xay" / "out" / "outbox.jsonl", "{}\n")   # 工位自己的文件不算改动
        ch = dk.changes(d)
        self.assertEqual(ch["changed_files"], ["a.txt"])
        self.assertEqual(ch["diff_stat"], {"files": 1, "add": 1, "del": 1})
        self.assertNotIn("executor_committed", ch)

        res = dk.land(d, self.origin, task="tk_0001")
        self.assertEqual((res.status, res.files, res.add, res.del_), ("landed", ["a.txt"], 1, 1))
        self.assertEqual(res.to_dict()["del"], 1)
        self.assertIn("task1 edit", (self.origin / "a.txt").read_text())
        self.assertEqual(self.status_o(), " M a.txt\n", "落到项目里是未提交、未暂存的改动")
        self.assertEqual(self.git_o("diff", "--cached"), "")
        self.assertEqual(sh(d, "log", "-1", "--format=%s").strip(), "xay: 任务 tk_0001 已落地")
        st = dk.read_state(d)
        self.assertEqual(st["landed_upto"], sh(d, "rev-parse", "HEAD").strip())
        self.assertEqual(st["last_landed"]["task"], "tk_0001")
        self.assertEqual(sh(d, "status", "--porcelain", "-uall").replace("?? .xay/out/outbox.jsonl\n", "")
                         .replace("?? .xay/desk.json\n", ""), "", "落地后桌子是干净的")
        self.assertEqual(dk.changes(d)["changed_files"], [])

        # 桌子上被忽略的缓存；项目里用户又改了别的文件
        write(d / "warm.cache", "cache")
        write(d / "node_modules" / "pkg" / "index.js", "module")
        write(self.origin / "b.txt", "bee by user\n")

        r2 = dk.align(d, self.origin)
        self.assertEqual(r2.mode, "reset")
        self.assertEqual((d / "b.txt").read_text(), "bee by user\n", "对齐后桌子带上用户新改的")
        self.assertIn("task1 edit", (d / "a.txt").read_text(), "也带着任务 1 已落地的改动")
        self.assertTrue((d / "warm.cache").exists(), "被忽略的缓存要留着")
        self.assertTrue((d / "node_modules" / "pkg" / "index.js").exists())
        self.assertTrue((d / ".xay" / "desk.json").exists())
        self.assertEqual(sh(d, "branch", "--show-current").strip(), "xay/emp/" + self.em)
        self.assertEqual(dk.changes(d)["changed_files"], [], "对齐后不应凭空出现改动")
        self.assertEqual(dk.read_state(d)["base"], r2.base)

        # 任务 2：新文件 + 改 src/main.py
        write(d / "feature" / "new.txt", "new\n")
        write(d / "src" / "main.py", "print('hi')\nprint('task2')\n")
        res2 = dk.land(d, self.origin, task="tk_0002")
        self.assertEqual((res2.status, res2.files), ("landed", ["feature/new.txt", "src/main.py"]))
        self.assertEqual((self.origin / "feature" / "new.txt").read_text(), "new\n")
        self.assertIn("task2", (self.origin / "src" / "main.py").read_text())
        self.assertIn("task1 edit", (self.origin / "a.txt").read_text())
        self.assertEqual((self.origin / "b.txt").read_text(), "bee by user\n")
        self.assertEqual(self.status_o(), " M a.txt\n M b.txt\n M src/main.py\n?? feature/new.txt\n")
        self.assertEqual(self.git_o("diff", "--cached"), "")

    def test_empty_land_changes_nothing(self):
        d = self.new_desk()
        before = fingerprint(self.origin)
        head = sh(d, "rev-parse", "HEAD").strip()
        res = dk.land(d, self.origin, task="tk_x")
        self.assertEqual((res.status, res.files), ("empty", []))
        self.assertEqual(fingerprint(self.origin), before)
        self.assertEqual(sh(d, "rev-parse", "HEAD").strip(), head, "没有改动不造标记提交")

    def test_user_commit_between_tasks(self):
        d = self.new_desk()
        edit_line(d / "a.txt", 1, "t1")
        self.assertEqual(dk.land(d, self.origin, task="t1").status, "landed")
        self.git_o("add", "-A"); self.git_o("commit", "-q", "-m", "user commits the landed change")
        r = dk.align(d, self.origin)
        self.assertEqual(r.mode, "reset")
        self.assertEqual(r.base, sh(self.origin, "rev-parse", "HEAD").strip(), "项目干净了，快照就是新 HEAD")
        self.assertEqual(dk.changes(d)["changed_files"], [])

    def test_untracked_origin_file_roundtrip(self):
        write(self.origin / "scratch.txt", "draft\n")                     # 项目里有个没跟踪的文件
        d = self.new_desk()
        r = dk.align(d, self.origin)
        self.assertEqual((d / "scratch.txt").read_text(), "draft\n")
        self.assertEqual(dk.changes(d)["changed_files"], [], "原来就有的未跟踪文件不算执行者的改动")
        write(d / "scratch.txt", "draft\nmore\n")
        res = dk.land(d, self.origin)
        self.assertEqual((res.status, res.files), ("landed", ["scratch.txt"]))
        self.assertEqual((self.origin / "scratch.txt").read_text(), "draft\nmore\n")
        self.assertEqual(self.status_o(), "?? scratch.txt\n")

    def test_executor_commit_is_still_a_change(self):
        d = self.new_desk()
        dk.align(d, self.origin)
        edit_line(d / "a.txt", 4, "ai committed this")
        sh(d, "add", "-A"); sh(d, "commit", "-q", "-m", "ai commit")
        write(d / "extra.txt", "uncommitted extra\n")
        ch = dk.changes(d)
        self.assertEqual(ch["changed_files"], ["a.txt", "extra.txt"])
        self.assertTrue(ch["executor_committed"])
        res = dk.land(d, self.origin, task="tk_c")
        self.assertEqual((res.status, res.files), ("landed", ["a.txt", "extra.txt"]))
        self.assertIn("ai committed this", (self.origin / "a.txt").read_text())
        after = dk.changes(d)
        self.assertEqual(after["changed_files"], [])
        self.assertNotIn("executor_committed", after)
        st = dk.read_state(d)
        self.assertEqual(st["landed_upto"], sh(d, "rev-parse", "HEAD").strip())

    def test_detached_head_is_reattached_on_land_and_align(self):
        d = self.new_desk()
        dk.align(d, self.origin)
        sh(d, "checkout", "-q", "--detach")
        edit_line(d / "a.txt", 5, "from detached")
        self.assertEqual(dk.land(d, self.origin).status, "landed")
        self.assertEqual(sh(d, "branch", "--show-current").strip(), "xay/emp/" + self.em)
        sh(d, "checkout", "-q", "--detach")
        dk.align(d, self.origin)
        self.assertEqual(sh(d, "branch", "--show-current").strip(), "xay/emp/" + self.em)

    def test_unicode_and_spaces_in_paths(self):
        d = self.new_desk()
        write(d / "文档 目录" / "说明 一.txt", "你好\n")
        edit_line(d / "a.txt", 1, "行一")
        ch = dk.changes(d)
        self.assertEqual(ch["changed_files"], ["a.txt", "文档 目录/说明 一.txt"])
        res = dk.land(d, self.origin)
        self.assertEqual(res.status, "landed")
        self.assertEqual((self.origin / "文档 目录" / "说明 一.txt").read_text(), "你好\n")
        self.assertEqual(dk.align(d, self.origin).mode, "reset")
        self.assertEqual(dk.changes(d)["changed_files"], [])

    def test_relative_home_path_is_made_absolute(self):
        cwd = os.getcwd()
        os.chdir(self.tmp)
        self.addCleanup(os.chdir, cwd)
        d = dk.ensure_desk(self.origin, self.ws, "em_rel", "relhome")
        self.assertTrue(d.is_absolute())
        self.assertTrue((self.tmp / "relhome" / "desks" / self.ws / "em_rel" / ".git").exists())

    def test_workdir_for_subdirectory_origin(self):
        d = self.new_desk()
        self.assertEqual(dk.workdir(d, self.origin), d)
        self.assertEqual(dk.workdir(d, self.origin / "src"), d / "src")


class LandVarietyTests(Base):
    def _mutate(self, d):
        (d / "b.txt").unlink()                                           # 删除
        write(d / "assets" / "img.bin", b"\x00\xff\x10binary\x00")       # 新目录里的二进制文件
        os.chmod(d / "tool.sh", 0o755)                                   # 只改可执行位
        os.symlink("a.txt", d / "link")                                  # 新符号链接
        write(d / "src" / "main.py", "print('hi')\nprint('more')\n")     # 普通修改

    def _check(self, o):
        self.assertFalse((o / "b.txt").exists())
        self.assertEqual((o / "assets" / "img.bin").read_bytes(), b"\x00\xff\x10binary\x00")
        self.assertTrue(os.stat(o / "tool.sh").st_mode & stat.S_IXUSR, "可执行位要带过去")
        self.assertTrue((o / "link").is_symlink())
        self.assertEqual(os.readlink(o / "link"), "a.txt")
        self.assertIn("more", (o / "src" / "main.py").read_text())

    def test_apply_path(self):
        d = self.new_desk()
        self._mutate(d)
        res = dk.land(d, self.origin)
        self.assertEqual(res.status, "landed")
        self.assertEqual(res.method, "apply")
        self._check(self.origin)

    def test_merge3_fallback_path(self):
        d = self.new_desk()
        self._mutate(d)
        with mock.patch.object(dk, "_try_git_apply", return_value=False):
            res = dk.land(d, self.origin)
        self.assertEqual(res.status, "landed")
        self.assertEqual(res.method, "merge3")
        self._check(self.origin)
        self.assertEqual(self.git_o("diff", "--cached"), "")

    def test_merge3_merges_nonoverlapping_edits_in_same_file(self):
        d = self.new_desk()
        edit_line(self.origin / "a.txt", 6, "user edit 6")              # 落在桌子那处修改的上下文里，git apply 套不上
        edit_line(d / "a.txt", 9, "desk edit 9")
        res = dk.land(d, self.origin)
        self.assertEqual((res.status, res.method), ("landed", "merge3"))
        lines = (self.origin / "a.txt").read_text().splitlines()
        self.assertEqual((lines[5], lines[8]), ("user edit 6", "desk edit 9"))
        self.assertEqual(self.status_o(), " M a.txt\n")

    def test_already_applied_change_is_not_a_conflict(self):
        d = self.new_desk()
        edit_line(d / "a.txt", 3, "same")
        write(d / "same_new.txt", "same\n")
        edit_line(self.origin / "a.txt", 3, "same")                     # 用户手动做了同样的修改
        write(self.origin / "same_new.txt", "same\n")
        res = dk.land(d, self.origin)
        self.assertEqual(res.status, "landed")
        self.assertEqual(res.method, "merge3")

    def test_merge3_rolls_back_when_write_fails(self):
        d = self.new_desk()
        write(d / "b.txt", "bee by desk\n")
        write(d / "z_new.txt", "z\n")
        before = fingerprint(self.origin)
        state_before = dk.read_state(d)
        real = dk.merge3._write
        calls = {"n": 0}

        def flaky(path, *a, **kw):
            calls["n"] += 1
            if calls["n"] == 2:
                raise OSError("disk full")
            return real(path, *a, **kw)

        with mock.patch.object(dk, "_try_git_apply", return_value=False), \
                mock.patch.object(dk.merge3, "_write", flaky):
            with self.assertRaises(OSError):
                dk.land(d, self.origin)
        self.assertGreaterEqual(calls["n"], 2)   # 第 2 次写失败；之后的是回滚时的还原
        self.assertEqual(fingerprint(self.origin), before, "写盘中途出错，项目要恢复原样")
        self.assertEqual(dk.read_state(d), state_before, "没落地成功，桌子状态不动")
        self.assertEqual(dk.changes(d)["changed_files"], ["b.txt", "z_new.txt"])


class ConflictTests(Base):
    def test_conflict_leaves_origin_untouched_and_desk_kept(self):
        d = self.new_desk()
        dk.align(d, self.origin)
        edit_line(self.origin / "a.txt", 5, "user wins")                # 开工后用户改了同一行
        edit_line(d / "a.txt", 5, "desk version")
        write(d / "n.txt", "brand new\n")                               # 同一次落地里的另一个文件
        before = fingerprint(self.origin)
        head_before = sh(d, "rev-parse", "HEAD").strip()
        state_before = dk.read_state(d)

        res = dk.land(d, self.origin, task="tk_conf")

        self.assertEqual(res.status, "conflict")
        self.assertEqual(res.conflicts, ["a.txt"])
        self.assertEqual(res.files, ["a.txt"])
        self.assertIn("a.txt", res.detail)
        self.assertEqual(fingerprint(self.origin), before, "冲突时项目一个字节都不能动（n.txt 也不能漏进去）")
        self.assertFalse((self.origin / "n.txt").exists())
        self.assertEqual(sh(d, "rev-parse", "HEAD").strip(), head_before, "桌子分支不动")
        self.assertEqual(dk.read_state(d), state_before, "状态文件不动")
        self.assertEqual(dk.changes(d)["changed_files"], ["a.txt", "n.txt"], "桌子上的改动原样保留")
        dk.keep(d, "conflict")
        self.assertEqual(dk.align(d, self.origin).mode, "blocked")

    def test_delete_vs_modify_is_conflict(self):
        d = self.new_desk()
        (d / "b.txt").unlink()
        write(self.origin / "b.txt", "bee changed by user\n")
        before = fingerprint(self.origin)
        res = dk.land(d, self.origin)
        self.assertEqual((res.status, res.conflicts), ("conflict", ["b.txt"]))
        self.assertEqual(fingerprint(self.origin), before)

    def test_new_file_vs_different_existing_file_is_conflict(self):
        d = self.new_desk()
        write(d / "same.txt", "desk\n")
        write(self.origin / "same.txt", "user\n")
        before = fingerprint(self.origin)
        res = dk.land(d, self.origin)
        self.assertEqual((res.status, res.conflicts), ("conflict", ["same.txt"]))
        self.assertEqual(fingerprint(self.origin), before)


class KeepDiscardRemoveTests(Base):
    def test_failed_task_keep_blocks_next_align_then_discard_unblocks(self):
        d = self.new_desk()
        dk.align(d, self.origin)
        base = dk.read_state(d)["base"]
        edit_line(d / "a.txt", 2, "half done")
        write(d / "new.txt", "partial\n")
        write(d / "build" / "out.bin", "ignored artifact")
        write(d / "warm.cache", "cache")
        ch = dk.changes(d)
        self.assertEqual(ch["changed_files"], ["a.txt", "new.txt"])
        kind, reason = evidence.verdict("failed", None, bool(ch["changed_files"]), "write")
        self.assertEqual((kind, reason), ("kept", "failed_with_changes"))
        st = dk.keep(d, reason)
        self.assertTrue(st["kept"])
        self.assertEqual(st["kept_reason"], "failed_with_changes")

        r = dk.align(d, self.origin)
        self.assertEqual(r.mode, "blocked")
        self.assertEqual(r.base, base)
        self.assertEqual(r.unmerged_files, ["a.txt", "new.txt"])
        self.assertIn("half done", (d / "a.txt").read_text(), "blocked 时桌子一个字都不能动")
        self.assertTrue((d / "new.txt").exists())
        self.assertEqual(dk.read_state(d)["base"], base)

        dk.discard(d)                                                    # 人选了"丢掉"
        self.assertNotIn("half done", (d / "a.txt").read_text())
        self.assertFalse((d / "new.txt").exists())
        self.assertTrue((d / "warm.cache").exists(), "被忽略的文件不受 discard 影响")
        self.assertTrue((d / "build" / "out.bin").exists())
        self.assertTrue((d / ".xay" / "desk.json").exists())
        self.assertFalse(dk.read_state(d)["kept"])
        self.assertEqual(dk.changes(d)["changed_files"], [])
        self.assertEqual(dk.align(d, self.origin).mode, "reset")

    def test_discard_also_drops_executor_commits(self):
        d = self.new_desk()
        dk.align(d, self.origin)
        base = dk.read_state(d)["base"]
        write(d / "c.txt", "committed by ai\n")
        sh(d, "add", "-A"); sh(d, "commit", "-q", "-m", "ai")
        dk.discard(d)
        self.assertEqual(sh(d, "rev-parse", "HEAD").strip(), base)
        self.assertFalse((d / "c.txt").exists())

    def test_keep_without_changes_does_not_block(self):
        d = self.new_desk()
        dk.keep(d, "mismatch")                                           # 说做完了但没有改动
        r = dk.align(d, self.origin)
        self.assertEqual(r.mode, "reset")
        self.assertFalse(dk.read_state(d)["kept"])

    def test_kept_changes_can_be_landed_later(self):
        d = self.new_desk()
        edit_line(d / "a.txt", 8, "kept then merged")
        dk.keep(d, "mismatch")
        self.assertEqual(dk.align(d, self.origin).mode, "blocked")
        res = dk.land(d, self.origin, task="tk_late")                    # 人选了 merge
        self.assertEqual(res.status, "landed")
        st = dk.read_state(d)
        self.assertFalse(st["kept"])
        self.assertEqual(dk.align(d, self.origin).mode, "reset")

    def test_remove_requires_user_approval(self):
        d = self.new_desk()
        for who in (None, "", "gm", "lead", "office", "employee:em_0001", "USER"):
            with self.assertRaises(PermissionError, msg=repr(who)):
                dk.remove(d, who)
            self.assertTrue((d / ".git").exists(), "没批准就不能动桌子")
        before = fingerprint(self.origin)
        dk.remove(d, approved_by="user")
        self.assertFalse(d.exists())
        self.assertEqual(sh(self.origin, "branch", "--list", "xay/emp/*").strip(), "")
        self.assertEqual(len(sh(self.origin, "worktree", "list").strip().splitlines()), 1)
        self.assertEqual(sh(self.origin, "for-each-ref", "refs/xay/").strip(), "")
        self.assertEqual(fingerprint(self.origin), before)
        again = self.new_desk()                                          # 删了之后可以重新招
        self.assertTrue((again / ".git").exists())


class LockTests(Base):
    def test_land_waits_for_lock_then_proceeds(self):
        d = self.new_desk()
        edit_line(d / "a.txt", 2, "locked land")
        out = {}
        with land_lock(self.home, str(self.origin)):
            t = threading.Thread(target=lambda: out.setdefault("r", dk.land(d, self.origin, lock_timeout=20)))
            t.start()
            time.sleep(0.4)
            self.assertTrue(t.is_alive(), "锁被占着，落地要等")
            self.assertNotIn("locked land", (self.origin / "a.txt").read_text())
        t.join(20)
        self.assertEqual(out["r"].status, "landed")
        self.assertTrue((self.home / "locks").is_dir())

    def test_lock_timeout_raises_and_changes_nothing(self):
        d = self.new_desk()
        edit_line(d / "a.txt", 2, "x")
        before = fingerprint(self.origin)
        with land_lock(self.home, str(self.origin)):
            with self.assertRaises(DeskError):
                dk.land(d, self.origin, lock_timeout=0.2)
        self.assertEqual(fingerprint(self.origin), before)
        self.assertEqual(dk.changes(d)["changed_files"], ["a.txt"])

    def test_align_waits_while_someone_is_landing(self):
        d = self.new_desk()
        out = {}
        with land_lock(self.home, str(self.origin)):
            t = threading.Thread(target=lambda: out.setdefault("r", dk.align(d, self.origin, lock_timeout=20)))
            t.start()
            time.sleep(0.4)
            self.assertTrue(t.is_alive(), "别人正在落地，对齐要等（否则会拍到落了一半的项目）")
        t.join(20)
        self.assertEqual(out["r"].mode, "reset")

    def test_two_desks_land_one_after_another(self):
        d1, d2 = self.new_desk("em_0001"), self.new_desk("em_0002")
        edit_line(d1 / "a.txt", 1, "from d1")
        write(d2 / "b.txt", "from d2\n")
        results = []
        ts = [threading.Thread(target=lambda d=d: results.append(dk.land(d, self.origin))) for d in (d1, d2)]
        [t.start() for t in ts]
        [t.join(30) for t in ts]
        self.assertEqual(sorted(r.status for r in results), ["landed", "landed"])
        self.assertIn("from d1", (self.origin / "a.txt").read_text())
        self.assertEqual((self.origin / "b.txt").read_text(), "from d2\n")


if __name__ == "__main__":
    unittest.main()
