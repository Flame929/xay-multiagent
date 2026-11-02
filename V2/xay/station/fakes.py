"""测试用：环境变量 XAY2_FAKE_ADAPTER=<spec.json> 时，工位用假适配器（不起任何 CLI）。

管家起的工位是独立进程，测试没法把 Python 对象传进去，所以用一个 JSON 文件描述"假员工每一轮做什么"：

    {"agent": "codex",                 # 可省略，默认用员工自己的 AI
     "turns": [[步骤…], [步骤…]],       # 第 N 次 send 用第 N 段（跨进程累计，计数存在 <spec>.count）
     "default": [步骤…],               # turns 用完以后每一轮都用它（默认回复 OK）
     "native_compact": false,          # 可选：覆盖"支不支持自带压缩"
     "lazy_session": true,             # 可选：像 dsh 一样第一轮才有对话号
     "open_log": "/tmp/x/opens.jsonl"} # 可选：每次 open 记一行 {native_id_given, native_id}

步骤和 FakeAdapter 一样，另加一种 {"write": {"path": "x.txt", "text": "…"}}：send 时在桌子里写文件
（路径只能在桌子里面；path / text 里的 {task} 换成提示词里出现的第一个任务编号）。
"""
import fcntl
import json
import os
import re
from pathlib import Path

from ..adapters.fake import FakeAdapter

ENV = "XAY2_FAKE_ADAPTER"
_TASK_RE = re.compile(r"tk_[0-9a-f]{8}")


def enabled():
    return bool(os.environ.get(ENV))


def _bump(counter_path):
    """跨进程的轮数计数（加锁读改写），返回这次的序号（从 0 开始）。"""
    with open(counter_path, "a+", encoding="utf-8") as fh:
        fcntl.flock(fh, fcntl.LOCK_EX)
        fh.seek(0)
        raw = fh.read().strip()
        n = int(raw) if raw.isdigit() else 0
        fh.seek(0)
        fh.truncate()
        fh.write(str(n + 1))
        return n


class SpecFake(FakeAdapter):
    def __init__(self, agent, spec_path):
        self.spec_path = str(spec_path)
        self.spec = json.loads(Path(spec_path).read_text(encoding="utf-8"))
        super().__init__(self.spec.get("agent") or agent)
        if "native_compact" in self.spec:
            self.capabilities.native_compact = bool(self.spec["native_compact"])
        self.lazy_session = bool(self.spec.get("lazy_session"))

    def open(self, cwd, native_id=None, card=None):
        super().open(cwd, native_id=native_id, card=card)
        log = self.spec.get("open_log")
        if log:
            with open(log, "a", encoding="utf-8") as fh:
                fh.write(json.dumps({"native_id_given": native_id, "native_id": self.native_id}) + "\n")

    def _next_steps(self, text, extra_context):
        n = _bump(self.spec_path + ".count")
        turns = self.spec.get("turns") or []
        steps = turns[n] if n < len(turns) else (self.spec.get("default") or [{"text": "OK"}])
        m = _TASK_RE.search(text or "")
        task = m.group(0) if m else "task"
        out = []
        for step in steps:
            if "write" in step:
                self._write(step["write"], task)
            else:
                out.append(json.loads(json.dumps(step).replace("{task}", task)))
        self.script = [out]
        return super()._next_steps(text, extra_context)

    def _write(self, spec, task):
        root = Path(self.cwd).resolve()
        rel = str(spec.get("path") or "").replace("{task}", task)
        target = (root / rel).resolve()
        if not rel or os.path.isabs(rel) or root not in target.parents:
            raise ValueError("假员工只能写桌子里的文件：%r" % rel)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(str(spec.get("text") or "").replace("{task}", task), encoding="utf-8")


def make(agent):
    return SpecFake(agent, os.environ[ENV])
