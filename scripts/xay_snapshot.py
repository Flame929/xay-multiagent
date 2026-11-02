#!/usr/bin/env python3
"""任务开始/结束时给工作目录拍"脏文件"快照，只把这次任务真正动过的文件算作改动。

用法：
  xay_snapshot.py snap <dir> <out.json>   记下开始时已有改动的文件及其内容哈希
  xay_snapshot.py diff <dir> <snap.json>  输出 {"changed": [...], "deleted": [...]}

规则：结束时的脏文件里，开始时不脏、或内容哈希变了的，算这次改动；
开始时脏、结束时变干净的（被改回去或被提交），也算改动。
"""
import hashlib
import json
import os
import subprocess
import sys


def _dirty(root):
    try:
        out = subprocess.run(["git", "-C", root, "status", "--porcelain", "-z", "--untracked-files=all"],
                             capture_output=True, timeout=60, check=True).stdout
    except Exception:
        return None
    parts = out.split(b"\0")
    files = set()
    i = 0
    while i < len(parts):
        entry = parts[i]
        i += 1
        if len(entry) < 4:
            continue
        if entry[:1] in (b"R", b"C"):
            i += 1  # 重命名/复制后面跟着旧路径
        path = entry[3:].decode("utf-8", "replace")
        if path.startswith(".xay/") or path.endswith("/"):
            continue
        files.add(path)
    return files


def _hash(root, rel):
    path = os.path.join(root, rel)
    try:
        if os.path.islink(path):
            return "link:" + os.readlink(path)
        h = hashlib.sha256()
        with open(path, "rb") as f:
            for chunk in iter(lambda: f.read(1 << 20), b""):
                h.update(chunk)
        return h.hexdigest()
    except FileNotFoundError:
        return None
    except OSError:
        return "unreadable"


def snap(root, out):
    files = _dirty(root)
    data = {"git": files is not None, "files": {p: _hash(root, p) for p in sorted(files or ())}}
    with open(out, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False)


def diff(root, snap_path):
    try:
        with open(snap_path, encoding="utf-8") as f:
            before = json.load(f).get("files") or {}
    except Exception:
        before = {}
    now = _dirty(root) or set()
    changed = set()
    for p in now:
        if p not in before or _hash(root, p) != before[p]:
            changed.add(p)
    for p in set(before) - now:
        changed.add(p)
    deleted = sorted(p for p in changed if not os.path.lexists(os.path.join(root, p)))
    print(json.dumps({"changed": sorted(changed), "deleted": deleted}, ensure_ascii=False))


if __name__ == "__main__":
    if len(sys.argv) != 4 or sys.argv[1] not in ("snap", "diff"):
        sys.exit(__doc__)
    (snap if sys.argv[1] == "snap" else diff)(sys.argv[2], sys.argv[3])
