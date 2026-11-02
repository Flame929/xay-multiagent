"""git 命令行的薄封装（只用标准库 + git CLI）。

约定：
- 一律不读用户的 git 身份、不触发钩子、不签名（我们自己写 commit 时用固定身份 xay）。
- 查询类命令设置 GIT_OPTIONAL_LOCKS=0，避免 `git status` 之类顺手去改用户的 index。
"""
from __future__ import annotations

import os
import subprocess
from dataclasses import dataclass
from typing import Optional


class DeskError(RuntimeError):
    """桌子相关操作失败（给人看的中文信息）。"""


class GitError(DeskError):
    def __init__(self, args, rc: int, stderr: bytes):
        self.args_list = list(args)
        self.rc = rc
        self.stderr = stderr.decode("utf-8", "replace").strip()
        super().__init__("git %s 失败（%s）：%s" % (" ".join(map(str, args)), rc, self.stderr))


# 这些变量会让 git 去操作别的仓库/别的 index（比如工位本身在某个 git 钩子里被调用时），必须清掉
_STRIP = {"GIT_DIR", "GIT_WORK_TREE", "GIT_INDEX_FILE", "GIT_COMMON_DIR", "GIT_PREFIX"}
_CONF = ("-c", "core.hooksPath=/dev/null", "-c", "commit.gpgsign=false",
         "-c", "gc.auto=0", "-c", "core.quotepath=off")
IDENT = {"GIT_AUTHOR_NAME": "xay", "GIT_AUTHOR_EMAIL": "xay@localhost",
         "GIT_COMMITTER_NAME": "xay", "GIT_COMMITTER_EMAIL": "xay@localhost"}

# 工位自己的目录，永远不进快照、不进补丁、不被清理
XAY_EXCLUDE = ":(exclude,glob)**/.xay/**"


def git(cwd, *args, env: Optional[dict] = None, input: Optional[bytes] = None, ok=(0,)) -> bytes:
    e = {k: v for k, v in os.environ.items() if k not in _STRIP}
    e.update(IDENT)
    e["GIT_OPTIONAL_LOCKS"] = "0"
    e["GIT_TERMINAL_PROMPT"] = "0"
    if env:
        e.update(env)
    p = subprocess.run(["git", *_CONF, *map(str, args)], cwd=str(cwd), env=e, input=input,
                       capture_output=True)
    if p.returncode not in ok:
        raise GitError(args, p.returncode, p.stderr)
    return p.stdout


def gs(cwd, *args, env: Optional[dict] = None, ok=(0,)) -> str:
    return git(cwd, *args, env=env, ok=ok).decode("utf-8", "surrogateescape").strip()


def rev_parse(cwd, rev: str) -> Optional[str]:
    """解析成完整 sha；不存在（比如还没有任何提交的 HEAD）返回 None。"""
    out = git(cwd, "rev-parse", "--verify", "-q", rev, ok=(0, 1)).decode().strip()
    return out or None


def repo_root(path) -> str:
    try:
        top = gs(path, "rev-parse", "--show-toplevel")
    except (GitError, FileNotFoundError, NotADirectoryError) as exc:
        raise DeskError("这个目录不在 git 仓库里，没法给员工建桌子：%s" % path) from exc
    return os.path.realpath(top)


def git_path(cwd, name: str) -> str:
    out = gs(cwd, "rev-parse", "--path-format=absolute", "--git-path", name)
    return out


def common_dir(cwd) -> str:
    return os.path.realpath(gs(cwd, "rev-parse", "--path-format=absolute", "--git-common-dir"))


def split_z(raw: bytes) -> list:
    return [p.decode("utf-8", "surrogateescape") for p in raw.split(b"\0") if p]


@dataclass
class RawEntry:
    path: str
    old_mode: str
    new_mode: str
    old_sha: str
    new_sha: str
    status: str  # A / D / M / T


def diff_raw(cwd, a: str, b: str) -> list:
    """两个 tree/commit 之间的逐文件差异（不做重命名检测，路径原样）。"""
    out = git(cwd, "diff", "--raw", "-z", "--no-renames", "--no-abbrev", a, b)
    parts = out.split(b"\0")
    entries, i = [], 0
    while i + 1 < len(parts) and parts[i]:
        meta = parts[i].decode().lstrip(":").split(" ")
        entries.append(RawEntry(path=parts[i + 1].decode("utf-8", "surrogateescape"),
                                old_mode=meta[0], new_mode=meta[1], old_sha=meta[2],
                                new_sha=meta[3], status=meta[4][:1]))
        i += 2
    return entries


def diff_numstat(cwd, a: str, b: str) -> tuple:
    """返回 (files, add, del)。二进制文件只算文件数，行数记 0。"""
    out = git(cwd, "diff", "--numstat", "-z", "--no-renames", a, b)
    files = add = dele = 0
    for rec in out.split(b"\0"):
        if not rec:
            continue
        x, y, _path = rec.decode("utf-8", "surrogateescape").split("\t", 2)
        files += 1
        add += int(x) if x.isdigit() else 0
        dele += int(y) if y.isdigit() else 0
    return files, add, dele
