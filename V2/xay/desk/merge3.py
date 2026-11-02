"""三方合并兜底：`git apply` 套不上时，逐个文件和项目里"现在的内容"做三方合并。

移植自旧系统 scripts/xay_merge3.py，区别：
- 桌子上的内容不从磁盘读，而是从 git 对象里读（base 提交 → 桌子的 tree），和桌子目录当时的状态无关；
- 先在内存里算好每个文件怎么处理，**全部没有冲突才写盘**；写盘中途出错会回滚，项目恢复原样；
- 认得可执行位和符号链接；子模块一律当冲突（不碰）。
"""
from __future__ import annotations

import os
import subprocess
import tempfile
from dataclasses import dataclass, field

from ._git import RawEntry, git


@dataclass
class Plan:
    # 路径 -> (kind, 内容, 可执行位)；可执行位 None=沿用项目里现有文件的权限
    writes: dict = field(default_factory=dict)
    deletes: list = field(default_factory=list)
    conflicts: list = field(default_factory=list)


def _kind(mode: str) -> str:
    return "link" if mode == "120000" else "file"


def _blob(cwd, sha: str, mode: str):
    return (_kind(mode), git(cwd, "cat-file", "blob", sha))


def _read_ours(path: str):
    """项目里现在的样子：None=不存在；("dir", b"")；("link", 目标)；("file", 内容)。"""
    if os.path.islink(path):
        return ("link", os.readlink(path).encode("utf-8", "surrogateescape"))
    if not os.path.lexists(path):
        return None
    if os.path.isdir(path):
        return ("dir", b"")
    with open(path, "rb") as f:
        return ("file", f.read())


def merge_text(ours: bytes, base: bytes, theirs: bytes):
    with tempfile.TemporaryDirectory(prefix="xay-m3-") as tmp:
        paths = []
        for name, data in (("ours", ours), ("base", base), ("theirs", theirs)):
            p = os.path.join(tmp, name)
            with open(p, "wb") as f:
                f.write(data)
            paths.append(p)
        proc = subprocess.run(["git", "merge-file", "-p", *paths], capture_output=True)
    return proc.returncode == 0, proc.stdout


def make_plan(root: str, entries: list) -> Plan:
    plan = Plan()
    for e in entries:  # type: RawEntry
        target = os.path.join(root, e.path)
        if "160000" in (e.old_mode, e.new_mode):
            plan.conflicts.append(e.path)
            continue
        ours = _read_ours(target)
        if ours is not None and ours[0] == "dir":
            plan.conflicts.append(e.path)
            continue
        base = None if e.status == "A" else _blob(root, e.old_sha, e.old_mode)
        theirs = None if e.status == "D" else _blob(root, e.new_sha, e.new_mode)
        new_exec = e.new_mode == "100755"
        mode_changed = e.status != "A" and e.old_mode != e.new_mode

        if e.status == "A":
            if ours is None or ours == theirs:
                plan.writes[e.path] = (theirs[0], theirs[1], new_exec)
            else:
                plan.conflicts.append(e.path)
        elif e.status == "D":
            if ours is None:
                continue
            if ours == base:
                plan.deletes.append(e.path)
            else:
                plan.conflicts.append(e.path)
        else:  # M / T
            exec_flag = new_exec if mode_changed else None
            if ours is None:
                plan.conflicts.append(e.path)
            elif ours == base or ours == theirs:
                plan.writes[e.path] = (theirs[0], theirs[1], exec_flag)
            elif theirs == base:  # 桌子只改了权限位
                if mode_changed:
                    plan.writes[e.path] = (ours[0], ours[1], exec_flag)
            elif ours[0] != "file" or base[0] != "file" or theirs[0] != "file" \
                    or b"\0" in (ours[1] + base[1] + theirs[1]):
                plan.conflicts.append(e.path)
            else:
                ok, merged = merge_text(ours[1], base[1], theirs[1])
                if ok:
                    plan.writes[e.path] = ("file", merged, exec_flag)
                else:
                    plan.conflicts.append(e.path)
    return plan


def _prev_state(path: str):
    """回滚用：记下写之前的样子（内容 + 权限）。"""
    cur = _read_ours(path)
    if cur is None:
        return None
    mode = os.lstat(path).st_mode & 0o7777
    return (cur[0], cur[1], mode)


def _remove(path: str):
    if os.path.lexists(path):
        os.remove(path)


def _write(path: str, kind: str, data: bytes, exec_flag):
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    if kind == "link":
        _remove(path)
        os.symlink(data.decode("utf-8", "surrogateescape"), path)
        return
    existed = os.path.lexists(path)
    if os.path.islink(path):
        os.remove(path)
        existed = False
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o777 if exec_flag else 0o666)
    with os.fdopen(fd, "wb") as f:
        f.write(data)
    if existed and exec_flag is not None:
        cur = os.stat(path).st_mode & 0o7777
        os.chmod(path, (cur | ((cur & 0o444) >> 2)) if exec_flag else (cur & ~0o111))


def _prune_empty_dirs(root: str, rel: str):
    d = os.path.dirname(os.path.join(root, rel))
    while os.path.realpath(d) != os.path.realpath(root) and d.startswith(root):
        try:
            os.rmdir(d)
        except OSError:
            break
        d = os.path.dirname(d)


def apply_plan(root: str, plan: Plan) -> None:
    done = []
    try:
        for rel, (kind, data, exec_flag) in plan.writes.items():
            path = os.path.join(root, rel)
            done.append((path, _prev_state(path)))
            _write(path, kind, data, exec_flag)
        for rel in plan.deletes:
            path = os.path.join(root, rel)
            done.append((path, _prev_state(path)))
            _remove(path)
            _prune_empty_dirs(root, rel)
    except BaseException:
        for path, prev in reversed(done):
            if prev is None:
                _remove(path)
            else:
                _write(path, prev[0], prev[1], None)
                if prev[0] == "file":
                    os.chmod(path, prev[2])
        raise
