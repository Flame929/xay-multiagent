"""每个员工一张桌子（设计：references/plans/arch-v10-rebuild.md §3.1 ②；事件：protocol §4.3）。

一张桌子 = 一个常驻的 git worktree：`$XAY2_HOME/desks/<ws>/<em>`，分支 `xay/emp/<em>`。
同一个员工的所有任务都在这个目录里干，所以 dsh/agy 绑定在目录上的对话一直有效；
每件活做完单独"落地"回项目（项目里表现为**未提交的改动**），桌子本身不删除、不重置。

一件活的完整流程（由工位按顺序调用，执行者自己永远不 commit / push）：

    ensure_desk()  招聘时建桌子（幂等）
    align()        开工前对齐：桌子上没有没落地的东西 → 重置到项目当前状态的快照（mode=reset）；
                   有"保留下来还没落地"的改动 → 什么都不动，返回 mode=blocked + 文件清单
                   （事件里对应 desk.synced 的 mode=kept_conflict，由管家转"需要你"）
    …执行者干活…
    changes()      证据里的"实际改了什么"（changed_files / diff_stat）
    evidence.verdict()  证据对得上吗 → landed / kept / failed
    land()         landed：把"这件活开始时 → 现在"的改动搬回项目（失败则项目一个字节都不动）
    keep()         kept：只做标记，桌子原样保留
    discard()      人说"丢掉"之后才能调用（甲类决定里的 discard 选项），回到这件活开始时的样子
    remove()       解雇且用户点头（approved_by="user"）才删桌子

状态文件 `<桌子>/.xay/desk.json`：
    base         这件活开始时的版本（changes()/land() 都是和它比）
    landed_upto  最近一次确认已经落地（或对齐）的版本；桌子现在的内容和它不同 = 有没落地的改动
    kept/kept_reason  是否被标记保留（仅供显示；是否"有没落地的改动"以内容比较为准）
    （本实现里每次对齐和落地都会把 base 和 landed_upto 一起前移，所以两者始终相等）

所有"拍快照/比较"都通过临时 index 完成，不碰用户的 index、工作区、stash、分支。
"""
from __future__ import annotations

import json
import os
import re
import shutil
import tempfile
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

from . import merge3
from ._git import (DeskError, GitError, XAY_EXCLUDE, common_dir, diff_numstat, diff_raw, git,
                   git_path, gs, repo_root, rev_parse, split_z)
from .lock import land_lock

BRANCH_FMT = "xay/emp/%s"
# desk.json 丢了也能找回"落地到哪了"：同一个值再记一份在项目仓库的这个引用里（只此一处会写 refs/xay/）
LANDED_REF = "refs/xay/landed/%s"
STATE_REL = os.path.join(".xay", "desk.json")
_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]*$")
# 固定提交时间，保证"同一个项目状态 → 同一个快照 sha"（对齐可重复，也方便比较）
_SNAP_DATE = "1700000000 +0000"


# ───────────────────────── 返回值 ─────────────────────────

@dataclass
class AlignResult:
    mode: str                       # "reset" | "blocked"
    base: str                       # 这件活开始时桌子的版本（blocked 时是原来的 base）
    unmerged_files: list = field(default_factory=list)

    def to_dict(self) -> dict:
        return {"mode": self.mode, "base": self.base, "unmerged_files": list(self.unmerged_files)}


@dataclass
class LandResult:
    status: str                     # "landed" | "conflict" | "empty"
    files: list = field(default_factory=list)   # landed/empty：改动的文件；conflict：冲突的文件
    add: int = 0
    del_: int = 0                   # `del` 是 Python 关键字，字典里仍叫 "del"
    detail: str = ""
    method: Optional[str] = None    # "apply"（git apply）| "merge3"（逐文件三方合并）
    conflicts: list = field(default_factory=list)

    def to_dict(self) -> dict:
        return {"status": self.status, "files": list(self.files), "add": self.add,
                "del": self.del_, "detail": self.detail, "method": self.method,
                "conflicts": list(self.conflicts)}


# ───────────────────────── 快照 ─────────────────────────

def worktree_tree(root) -> str:
    """工作区现在的完整内容（含未提交的改动、没被 git 管的新文件；不含被忽略的和 .xay/）对应的 tree。

    做法：复制一份 index 当临时 index，在上面 `git add -A`，再 `write-tree`。
    真正的 index、工作区、stash、分支一个字节都不动（只会往对象库里加对象）。
    复制 index（而不是从 HEAD 重建）是为了沿用文件状态缓存，大项目不用把每个文件重读一遍，
    也保留了 sparse-checkout / assume-unchanged 的语义。复制失败再退回"从 HEAD 重建"。

    复制时**必须连修改时间一起复制**（copy2）：git 判断"文件改没改"先看文件状态缓存，只有文件修改时间
    不早于 index 文件本身的修改时间时（"同一秒内改的"，racy git）才去比内容。复制出来的 index 如果是
    "现在"的时间，同一秒内 index 刚写过、文件又被改成同样长度（比如 line3 → dirty）的情况，跨过一秒再拍
    快照就会被当成没改，快照里漏掉这处改动（2026-10-30 查 test_desk 偶发失败时实测复现）。
    """
    root = str(root)
    last: Optional[Exception] = None
    for use_copy in (True, False):
        with tempfile.TemporaryDirectory(prefix="xay-idx-") as tmp:
            idx = os.path.join(tmp, "index")
            env = {"GIT_INDEX_FILE": idx}
            try:
                if use_copy:
                    shutil.copy2(git_path(root, "index"), idx)   # 保留修改时间：racy git 的判断才和原 index 一样
                elif rev_parse(root, "HEAD^{tree}"):
                    git(root, "read-tree", "HEAD", env=env)
                git(root, "add", "-A", "--", ".", XAY_EXCLUDE, env=env)
                return gs(root, "write-tree", env=env)
            except (OSError, GitError) as exc:
                last = exc
    raise DeskError("没法读出目录当前的内容：%s" % last)


def snapshot_origin(origin) -> str:
    """项目**此刻**的状态对应的提交 sha（含未提交改动和未跟踪的新文件）。

    项目干净 → 直接返回 HEAD；否则造一个以 HEAD 为父的提交对象（不挂任何分支）。
    同一个状态得到同一个 sha。还没有任何提交的新仓库 → 无父的根提交。
    """
    root = repo_root(origin)
    tree = worktree_tree(root)
    head = rev_parse(root, "HEAD^{commit}")
    if head and rev_parse(root, head + "^{tree}") == tree:
        return head
    env = {"GIT_AUTHOR_DATE": _SNAP_DATE, "GIT_COMMITTER_DATE": _SNAP_DATE}
    parent = ["-p", head] if head else []
    return gs(root, "commit-tree", tree, *parent, "-m", "xay: 项目快照", env=env)


# ───────────────────────── 状态文件 ─────────────────────────

def _state_path(desk) -> Path:
    return Path(desk) / STATE_REL


def _write_state(desk, st: dict) -> dict:
    path = _state_path(desk)
    path.parent.mkdir(parents=True, exist_ok=True)
    # .xay/ 里放一个"忽略一切（包括自己）"的 .gitignore：执行者即使 `git add -A` 也扫不进去，
    # 又不用去改项目仓库里的 info/exclude
    gi = path.parent / ".gitignore"
    if not gi.exists():
        gi.write_text("*\n", encoding="utf-8")
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(st, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    os.replace(tmp, path)
    git(desk, "update-ref", LANDED_REF % st["em"], st["landed_upto"])
    return st


def _new_state(desk, head: str, *, home, ws=None, em=None, branch: str, **extra) -> dict:
    st = {"v": 1, "ws": ws, "em": em or Path(desk).name, "branch": branch, "home": str(home),
          "base": head, "landed_upto": head, "kept": False, "kept_reason": None}
    st.update(extra)
    return st


def read_state(desk) -> dict:
    """读 desk.json；文件丢了/坏了（比如执行者 `git clean -fdx` 误删）就保守地重建：
    以引用 refs/xay/landed/<em> 记的"最近落地版本"为基准（没有就用当前 HEAD）；
    工作区和它不同则当作"保留下来的改动"（之后 align 会 blocked，等人决定）。"""
    desk = Path(desk)
    try:
        st = json.loads(_state_path(desk).read_text(encoding="utf-8"))
        if isinstance(st, dict) and st.get("base") and st.get("landed_upto") and st.get("branch"):
            return st
    except (OSError, ValueError):
        pass
    if not (desk / ".git").exists():
        raise DeskError("找不到这张桌子：%s" % desk)
    head = rev_parse(desk, LANDED_REF % desk.name) or rev_parse(desk, "HEAD^{commit}")
    branch = gs(desk, "symbolic-ref", "--short", "-q", "HEAD", ok=(0, 1)) or BRANCH_FMT % desk.name
    home = desk.parents[2] if len(desk.parents) > 2 else desk.parent
    st = _new_state(desk, head, home=home, ws=desk.parent.name, em=desk.name, branch=branch, recovered=True)
    if worktree_tree(desk) != rev_parse(desk, head + "^{tree}"):
        st.update(kept=True, kept_reason="state_lost")
    return _write_state(desk, st)


# ───────────────────────── 建桌子 ─────────────────────────

def _check_name(kind: str, value: str) -> str:
    if not isinstance(value, str) or not _NAME_RE.match(value):
        raise DeskError("%s 不合法：%r" % (kind, value))
    return value


def _is_desk_of(desk: Path, root: str) -> bool:
    if not (desk / ".git").is_file():
        return False
    try:
        return common_dir(desk) == common_dir(root)
    except GitError:
        return False


def ensure_desk(origin, ws: str, em: str, home) -> Path:
    """建员工的桌子（已存在就原样返回，幂等）。新桌子的分支 `xay/emp/<em>` 从项目当前快照长出来。"""
    _check_name("ws", ws)
    _check_name("em", em)
    root = repo_root(origin)
    home = Path(os.path.abspath(str(home)))
    desk = home / "desks" / ws / em
    branch = BRANCH_FMT % em
    if _is_desk_of(desk, root):
        if not _state_path(desk).exists():
            read_state(desk)  # 补一份状态文件
        return desk
    if desk.exists() and any(desk.iterdir()):
        raise DeskError("桌子的位置已经被占用，而且不是这个项目的工作区：%s" % desk)
    desk.parent.mkdir(parents=True, exist_ok=True)
    git(root, "worktree", "prune")
    branch_tip = rev_parse(root, "refs/heads/" + branch)
    if branch_tip:  # 桌子目录丢了但分支还在：接回原来的分支，里面的提交都保留
        git(root, "worktree", "add", "-q", str(desk), branch)
        head = rev_parse(root, LANDED_REF % em) or branch_tip  # 比最近落地版本新的提交 = 没落地的，align 会 blocked
    else:
        with land_lock(home, root):  # 别人正在往项目里落地时，不拍到落了一半的状态
            head = snapshot_origin(root)
        git(root, "worktree", "add", "-q", "-b", branch, str(desk), head)
    _write_state(desk, _new_state(desk, head, home=home, ws=ws, em=em, branch=branch))
    return desk


def workdir(desk, origin) -> Path:
    """执行者实际的工作目录：项目目录相对 git 根目录的位置，搬到桌子里（项目目录就是仓库根时就是桌子本身）。"""
    rel = os.path.relpath(os.path.realpath(str(origin)), repo_root(origin))
    return Path(desk) if rel == "." else Path(desk) / rel


# ───────────────────────── 开工前对齐 ─────────────────────────

def _pending_files(desk, st: dict) -> list:
    """桌子现在的内容和"最近确认落地的版本"相比，哪些文件不同（含没被 git 管的新文件）。"""
    now = worktree_tree(desk)
    landed = rev_parse(desk, st["landed_upto"] + "^{tree}")
    if now == landed:
        return []
    return sorted(e.path for e in diff_raw(desk, landed, now))


def _clean_untracked(desk: Path) -> None:
    """删掉没被 git 管、也没被忽略的文件（编译缓存、node_modules 这类被忽略的不动；.xay/ 不动）。
    只在确认"没有没落地的改动"之后调用，所以这些文件要么已落地要么是对齐前的残留。"""
    for rel in split_z(git(desk, "ls-files", "-o", "--exclude-standard", "-z")):
        parts = rel.split("/")
        if rel.endswith("/") or ".xay" in parts:
            continue
        try:
            os.remove(desk / rel)
        except FileNotFoundError:
            pass
        d = (desk / rel).parent
        while d != desk:
            try:
                d.rmdir()
            except OSError:
                break
            d = d.parent


def _home_of(desk: Path, st: dict) -> str:
    return st.get("home") or str(desk.parents[2] if len(desk.parents) > 2 else desk.parent)


def align(desk, origin, *, lock_timeout: float = 300.0) -> AlignResult:
    """开工前对齐。桌子上没有没落地的改动 → 硬对齐到项目当前快照（目录不变，被忽略的缓存还在）；
    有 → 什么都不动，返回 blocked 和文件清单，由管家标"需要你"。
    拍项目快照时会短暂拿落地锁，避免拍到别人"落地到一半"的状态。"""
    desk = Path(os.path.abspath(str(desk)))
    st = read_state(desk)
    pending = _pending_files(desk, st)
    if pending:
        return AlignResult("blocked", st["base"], pending)
    with land_lock(_home_of(desk, st), repo_root(origin), timeout=lock_timeout):
        snap = snapshot_origin(origin)
    git(desk, "checkout", "-q", "-f", "-B", st["branch"], snap)
    _clean_untracked(desk)
    st.update(base=snap, landed_upto=snap, kept=False, kept_reason=None)
    _write_state(desk, st)
    return AlignResult("reset", snap, [])


# ───────────────────────── 证据：实际改了什么 ─────────────────────────

def changes(desk) -> dict:
    """"这件活开始时 → 现在"桌子上实际改了什么（含没被 git 管的新文件，不含 .xay/ 和被忽略的）。
    返回可直接放进 turn.ended.evidence 的片段；执行者自己提交过（HEAD 移动了）就多一个 executor_committed: true。"""
    desk = Path(desk)
    st = read_state(desk)
    now = worktree_tree(desk)
    files, add, dele = diff_numstat(desk, st["base"], now)
    names = sorted(e.path for e in diff_raw(desk, st["base"], now))
    out = {"changed_files": names, "diff_stat": {"files": files, "add": add, "del": dele}}
    if rev_parse(desk, "HEAD") != st["landed_upto"]:
        out["executor_committed"] = True
    return out


# ───────────────────────── 落地 ─────────────────────────

def _try_git_apply(root: str, patch: bytes) -> bool:
    try:
        git(root, "apply", "--whitespace=nowarn", "-", input=patch)  # 默认就是"要么全套上，要么一个字节不动"
        return True
    except GitError:
        return False


def _mark_landed(desk: Path, st: dict, tree: str, task: Optional[str], files: list) -> None:
    """在桌子的分支上提交一个标记（内容 = 落地的那一刻桌子的完整内容），并把状态前移。"""
    msg = "xay: 任务 %s 已落地" % task if task else "xay: 任务已落地"
    parent = rev_parse(desk, "HEAD^{commit}")
    marker = gs(desk, "commit-tree", tree, *(["-p", parent] if parent else []), "-m", msg)
    git(desk, "update-ref", "-m", msg, "refs/heads/" + st["branch"], marker)
    git(desk, "symbolic-ref", "HEAD", "refs/heads/" + st["branch"])  # 执行者若切走了分支，接回来
    git(desk, "reset", "-q", "--mixed", "HEAD")                      # index 跟上，工作区不动
    st.update(base=marker, landed_upto=marker, kept=False, kept_reason=None,
              last_landed={"task": task, "sha": marker, "ts": time.time(), "files": files})
    _write_state(desk, st)


def land(desk, origin, *, task: Optional[str] = None, home=None, lock_timeout: float = 300.0) -> LandResult:
    """把桌子上"这件活开始时 → 现在"的改动搬回项目，作为**未提交**改动。

    要么全搬、要么一个字节都不动（冲突时项目原样，桌子也原样保留）。
    先 `git apply`（不用 --3way：它会往用户的 index 里暂存东西），套不上再逐文件三方合并。
    成功后在桌子分支上提交标记"xay: 任务 <task> 已落地"，并记进 desk.json。全程持有落地锁。"""
    desk = Path(desk)
    st = read_state(desk)
    root = repo_root(origin)
    if common_dir(desk) != common_dir(root):
        raise DeskError("这张桌子不属于这个项目：%s" % desk)
    with land_lock(home or _home_of(desk, st), root, timeout=lock_timeout):
        tree = worktree_tree(desk)
        entries = diff_raw(desk, st["base"], tree)
        if not entries:
            return LandResult("empty", [], 0, 0, "桌子上没有改动，没有东西要放回项目。")
        files = sorted(e.path for e in entries)
        _n, add, dele = diff_numstat(desk, st["base"], tree)
        patch = git(desk, "diff", "--binary", "--full-index", "--no-renames", st["base"], tree)
        method = "apply" if _try_git_apply(root, patch) else None
        if method is None:
            plan = merge3.make_plan(root, entries)
            if plan.conflicts:
                bad = sorted(plan.conflicts)
                return LandResult("conflict", bad, add, dele,
                                  "这些文件和项目里现在的内容冲突，项目没有被改动，桌子里的改动已保留：" + "、".join(bad),
                                  conflicts=bad)
            merge3.apply_plan(root, plan)
            method = "merge3"
        _mark_landed(desk, st, tree, task, files)
        return LandResult("landed", files, add, dele,
                          "改动已放进项目（还没提交）：%d 个文件，+%d -%d。" % (len(files), add, dele), method=method)


# ───────────────────────── 保留 / 丢弃 / 删除 ─────────────────────────

def keep(desk, reason: str) -> dict:
    """标记"保留下来"（reason 取 task.kept 的枚举：mismatch / failed_with_changes / paused / conflict / interrupted）。
    只改 desk.json，桌子上的文件原样不动。"""
    st = read_state(desk)
    st.update(kept=True, kept_reason=reason, kept_at=time.time())
    return _write_state(desk, st)


def discard(desk) -> dict:
    """把桌子退回这件活开始时的样子，丢掉所有没落地的改动（包括执行者自己提交的）。

    **只能在人做出决定之后调用**（甲类"需要你"里选了 discard）。工位自己不能因为"失败了"就调用它。
    被忽略的文件（缓存、node_modules）和 .xay/ 不受影响。"""
    desk = Path(desk)
    st = read_state(desk)
    git(desk, "checkout", "-q", "-f", "-B", st["branch"], st["base"])
    _clean_untracked(desk)
    st.update(landed_upto=st["base"], kept=False, kept_reason=None)
    return _write_state(desk, st)


def remove(desk, approved_by: Optional[str]) -> None:
    """删桌子（worktree + 分支）。只有 approved_by == "user"（用户点了头）才执行，否则抛 PermissionError。"""
    if approved_by != "user":
        raise PermissionError("删桌子必须经用户同意（approved_by='user'），收到：%r" % (approved_by,))
    desk = Path(os.path.abspath(str(desk)))
    st = read_state(desk)
    main = common_dir(desk)
    git(main, "worktree", "remove", "--force", "--force", str(desk))
    git(main, "branch", "-D", st["branch"], ok=(0, 1))
    git(main, "update-ref", "-d", LANDED_REF % st["em"], ok=(0, 1))
    git(main, "worktree", "prune")
