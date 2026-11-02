"""落地锁：同一个项目同一时刻只允许一个人往里"落地"。

锁文件：$XAY2_HOME/locks/land-<项目根目录真实路径的 sha1 前 12 位>。
用 flock：进程崩了系统自动放锁，不需要清理陈旧锁（旧系统用目录 + pid 文件，要自己判断死活）。
"""
from __future__ import annotations

import contextlib
import fcntl
import hashlib
import os
import time
from pathlib import Path

from ._git import DeskError


def lock_path(home, repo_root: str) -> Path:
    key = hashlib.sha1(os.path.realpath(repo_root).encode("utf-8")).hexdigest()[:12]
    return Path(home) / "locks" / ("land-" + key)


@contextlib.contextmanager
def land_lock(home, repo_root: str, timeout: float = 300.0, poll: float = 0.05):
    path = lock_path(home, repo_root)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(str(path), os.O_RDWR | os.O_CREAT, 0o644)
    try:
        deadline = time.monotonic() + timeout
        while True:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                if time.monotonic() >= deadline:
                    raise DeskError("别的任务正在往这个项目里合并，等了 %d 秒还没轮到" % int(timeout))
                time.sleep(poll)
        os.ftruncate(fd, 0)
        os.write(fd, ("%d\n" % os.getpid()).encode())
        yield path
    finally:
        os.close(fd)  # 关掉文件就放锁
