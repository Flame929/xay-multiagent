"""直播通道的读取端：跟读 $XAY2_HOME/live/<em>/<段>.jsonl 里最新一段的新行（协议 §8）。

文件由工位写（已脱敏，单行 ≤2 KB）；这里只读，坏行直接跳过，不信任内容格式。
"""
import json
import re
from pathlib import Path

ALLOWED_KINDS = ("thinking", "text", "tool", "tool_output", "status")
MAX_TEXT = 2000
BACKLOG_LINES = 200          # 没给 since 时，先补发最近多少行
MAX_INITIAL_BYTES = 1 << 20  # 第一次最多从文件尾部读 1 MB
_EM_RE = re.compile(r"^[A-Za-z0-9_-]{1,64}$")


def valid_em(em) -> bool:
    return isinstance(em, str) and bool(_EM_RE.match(em))


def _segment_key(path: Path):
    stem = path.stem
    try:
        mtime = path.stat().st_mtime
    except OSError:
        mtime = 0
    return (int(stem) if stem.isdigit() else -1, mtime)


class LiveTail:
    """每次 poll() 返回自上次以来的新条目：{n, kind, text, ts, seg}。"""

    def __init__(self, live_root, em, since=None, backlog=BACKLOG_LINES, since_seg=None):
        if not valid_em(em):
            raise ValueError("员工编号不合法")
        self.dir = Path(live_root) / em
        self.since = since
        self.backlog = backlog
        self.since_seg = since_seg
        self._path = None
        self._offset = 0
        self._buf = b""
        self._first_file = True
        self._count = 0

    def _newest(self):
        try:
            files = [p for p in self.dir.iterdir() if p.suffix == ".jsonl" and p.is_file()]
        except OSError:
            return None
        if not files:
            return None
        try:
            return max(files, key=_segment_key)
        except OSError:
            return None

    def poll(self):
        newest = self._newest()
        if newest is None:
            return []
        initial = False
        if newest != self._path:
            initial = self._first_file
            self._first_file = False
            self._path, self._offset, self._buf = newest, 0, b""
        try:
            size = newest.stat().st_size
            if initial and size > MAX_INITIAL_BYTES:
                self._offset = size - MAX_INITIAL_BYTES
                skip_partial = True
            else:
                skip_partial = False
            if size < self._offset:       # 文件被截断重写，从头来
                self._offset, self._buf = 0, b""
            if size == self._offset:
                return []
            with open(newest, "rb") as fh:
                fh.seek(self._offset)
                chunk = fh.read(size - self._offset)
        except OSError:
            return []
        self._offset += len(chunk)
        data = self._buf + chunk
        cut = data.rfind(b"\n")
        if cut < 0:
            self._buf = data if len(data) < 65536 else b""
            return []
        self._buf = data[cut + 1:]
        lines = data[:cut].split(b"\n")
        if skip_partial and lines:
            lines = lines[1:]
        items = []
        seg = _segment_key(newest)[0]           # 第几段（换新对话时工位换一个文件，n 从 1 重新数）：页面按 (seg, n) 去重
        for raw in lines:
            item = self._parse(raw)
            if item is not None:
                item["seg"] = seg
                items.append(item)
        if initial:
            if self.since_seg is not None:
                if seg == self.since_seg:
                    if self.since is not None:
                        items = [x for x in items if x["n"] > self.since]
                    else:
                        items = items[-self.backlog:]
                else:
                    # seg > self.since_seg（换了新对话）或 seg < self.since_seg：按 tail 补发新一段最近几行
                    items = items[-self.backlog:]
            else:
                if self.since is not None:
                    items = [x for x in items if x["n"] > self.since]
                else:
                    items = items[-self.backlog:]
        return items

    def _parse(self, raw: bytes):
        raw = raw.strip()
        if not raw:
            return None
        try:
            row = json.loads(raw.decode("utf-8", "replace"))
        except ValueError:
            return None
        if not isinstance(row, dict):
            return None
        kind = row.get("kind")
        if kind not in ALLOWED_KINDS:
            return None
        self._count += 1
        n = row.get("n")
        if not isinstance(n, int) or isinstance(n, bool):
            n = self._count
        text = row.get("text")
        text = text if isinstance(text, str) else ("" if text is None else str(text))
        ts = row.get("ts")
        if not isinstance(ts, (int, float)) or isinstance(ts, bool):
            ts = 0
        return {"n": n, "kind": kind, "text": text[:MAX_TEXT], "ts": ts}
