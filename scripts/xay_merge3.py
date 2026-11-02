#!/usr/bin/env python3
"""把独立工作区的改动三方合并回原项目：先在内存里算好每个文件的结果，全部无冲突才写盘。

用法：xay_merge3.py <原项目仓库根> <独立工作区根> <基准提交>
输出：{"ok": true, "files": [...]} 或 {"ok": false, "conflicts": [...]}；退出码 0 = 已合并，1 = 有冲突（原项目一个字节都没改）。

独立工作区里的改动需要先 `git add -A`（xay-run 搬回时已经做了）。
"""
import json
import os
import subprocess
import sys
import tempfile


def git(cwd, *args, check=True):
    return subprocess.run(["git", "-C", cwd, *args], capture_output=True, check=check).stdout


def read(path):
    try:
        with open(path, "rb") as f:
            return f.read()
    except FileNotFoundError:
        return None


def merge_text(ours, base, theirs):
    with tempfile.TemporaryDirectory() as tmp:
        paths = []
        for name, data in (("ours", ours), ("base", base), ("theirs", theirs)):
            path = os.path.join(tmp, name)
            with open(path, "wb") as f:
                f.write(data)
            paths.append(path)
        proc = subprocess.run(["git", "merge-file", "-p", *paths], capture_output=True)
    return proc.returncode == 0, proc.stdout


def plan(repo, worktree, base):
    raw = git(worktree, "diff", "--cached", "--no-renames", "--name-status", "-z", base)
    parts = [p.decode("utf-8", "surrogateescape") for p in raw.split(b"\0") if p]
    writes, deletes, conflicts = {}, [], []
    for status, rel in zip(parts[0::2], parts[1::2]):
        ours = read(os.path.join(repo, rel))
        theirs = read(os.path.join(worktree, rel))
        base_blob = git(worktree, "show", "%s:%s" % (base, rel), check=False) if status != "A" else None
        if status == "A":
            if ours is None or ours == theirs:
                writes[rel] = theirs
            else:
                conflicts.append(rel)
        elif status == "D":
            if ours is None:
                continue
            if ours == base_blob:
                deletes.append(rel)
            else:
                conflicts.append(rel)
        else:
            if ours is None:
                conflicts.append(rel)
            elif ours == base_blob or ours == theirs:
                writes[rel] = theirs
            elif theirs == base_blob:
                continue
            elif b"\0" in (ours + theirs + (base_blob or b"")):
                conflicts.append(rel)
            else:
                ok, merged = merge_text(ours, base_blob or b"", theirs)
                if ok:
                    writes[rel] = merged
                else:
                    conflicts.append(rel)
    return writes, deletes, conflicts


def main(argv):
    if len(argv) != 4:
        sys.exit(__doc__)
    repo, worktree, base = argv[1:]
    writes, deletes, conflicts = plan(repo, worktree, base)
    if conflicts:
        print(json.dumps({"ok": False, "conflicts": conflicts}, ensure_ascii=False))
        return 1
    for rel, data in writes.items():
        target = os.path.join(repo, rel)
        os.makedirs(os.path.dirname(target) or ".", exist_ok=True)
        with open(target, "wb") as f:
            f.write(data)
        mode = os.stat(os.path.join(worktree, rel)).st_mode & 0o777
        os.chmod(target, mode)
    for rel in deletes:
        os.remove(os.path.join(repo, rel))
    print(json.dumps({"ok": True, "files": sorted(list(writes) + deletes)}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
