"""直播通道的写入端：`$XAY2_HOME/live/<em>/<段>.jsonl`（protocol §8；读取端在 office/live.py）。

- 每行 {n, kind, text, ts}，kind ∈ thinking / text / tool / tool_output / status；已脱敏，单行 ≤2 KB。
- 逐字的思考 / 回复片段先攒一小会（≤0.3 秒或遇到换行）再写一行，免得一个字一行；其他种类立刻写。
- 一段记忆一个文件；换段后新写新文件。超过 7 天没动的旧段文件删掉（直播只看最近）。
- 不进账本。
- 另有用量行 {n, kind: usage, turn, tokens, ts}（2026-10-31，详情栏 token 速度）：这一轮到现在累计用了多少 token
  （和 turn.ended.usage.total 同口径）。变了才写，同一轮两行至少隔 1 秒（中间的只留最新的，下一次 tick 补写）。
  读取端（office/live.py 的直播推送、office/employee.py 的步骤）按 kind 过滤，看不到这种行；只有详情栏的速度 / 消耗读它。
"""
import json
import os
import time
from pathlib import Path

from ..adapters.base import redact

KINDS = ("thinking", "text", "tool", "tool_output", "status")
LINE_MAX = 2048
COALESCE_S = 0.3
COALESCE_CHARS = 400
KEEP_DAYS = 7
USAGE_EVERY_S = 1.0


class LiveWriter:
    def __init__(self, root, em):
        self.dir = Path(root) / em
        self.segment = None
        self.path = None
        self.n = 0
        self._pending_kind = None
        self._pending = ""
        self._pending_ts = 0.0
        self._usage_last = (None, None, None, None, 0.0)      # 上一条写出去的用量行：(轮, token, out, fresh, 时间)
        self._usage_pending = None                # 隔不到 1 秒的新数：(轮, token, out, fresh)，tick / flush 时补写

    def set_segment(self, segment):
        segment = int(segment or 1)
        if segment == self.segment:
            return
        self.flush()
        self.segment = segment
        self.dir.mkdir(parents=True, exist_ok=True)
        self.path = self.dir / ("%d.jsonl" % segment)
        self.n = 0
        try:
            with open(self.path, "rb") as fh:
                self.n = sum(1 for _ in fh)
        except OSError:
            pass
        self._prune()

    def _prune(self):
        cutoff = time.time() - KEEP_DAYS * 86400
        try:
            for p in self.dir.glob("*.jsonl"):
                if p != self.path and p.stat().st_mtime < cutoff:
                    p.unlink()
        except OSError:
            pass

    def add(self, kind, text):
        if kind not in KINDS or self.path is None:
            return
        text = "" if text is None else str(text)
        if kind in ("thinking", "text"):
            if self._pending_kind not in (None, kind):
                self.flush()
            if not self._pending:
                self._pending_ts = time.time()
            self._pending_kind = kind
            self._pending += text
            if len(self._pending) >= COALESCE_CHARS or ("\n" in text and len(self._pending) >= 40):
                self.flush()
            return
        self.flush()
        self._write(kind, text)

    def usage(self, turn, tokens, out=None, fresh=None):
        """这一轮到现在累计用了多少 token（详情栏的速度 / 消耗用）。没变不写；同一轮隔不到 1 秒的先攒着。"""
        if self.path is None or not isinstance(turn, str) or not isinstance(tokens, (int, float)) or isinstance(tokens, bool):
            return
        tokens = int(tokens)
        out = int(out) if isinstance(out, (int, float)) and not isinstance(out, bool) else None
        fresh = int(fresh) if isinstance(fresh, (int, float)) and not isinstance(fresh, bool) else None
        if len(self._usage_last) == 5:
            last_turn, last_tokens, last_out, last_fresh, last_ts = self._usage_last
        else:
            last_turn, last_tokens, last_ts = self._usage_last[:3]
            last_out, last_fresh = None, None
        if turn == last_turn and tokens == last_tokens and out == last_out and fresh == last_fresh:
            self._usage_pending = None
            return
        if turn == last_turn and time.time() - last_ts < USAGE_EVERY_S:
            self._usage_pending = (turn, tokens, out, fresh)
            return
        self._write_usage(turn, tokens, out=out, fresh=fresh)

    def _write_usage(self, turn, tokens, out=None, fresh=None):
        self._usage_pending = None
        now = time.time()
        self.n += 1
        row = {"n": self.n, "kind": "usage", "turn": turn[:64], "tokens": tokens}
        if out is not None:
            row["out"] = out
        if fresh is not None:
            row["fresh"] = fresh
        row["ts"] = round(now, 3)
        try:
            with open(self.path, "a", encoding="utf-8") as fh:
                fh.write(json.dumps(row, ensure_ascii=False) + "\n")
        except OSError:
            self.n -= 1
            return
        self._usage_last = (turn, tokens, out, fresh, now)

    def tick(self):
        if self._pending and time.time() - self._pending_ts >= COALESCE_S:
            self.flush()
        last_ts = self._usage_last[-1] if self._usage_last else 0.0
        if self._usage_pending and time.time() - last_ts >= USAGE_EVERY_S:
            self._write_usage(*self._usage_pending)

    def flush(self):
        if self._pending_kind and self._pending:
            self._write(self._pending_kind, self._pending)
        self._pending_kind, self._pending = None, ""
        if self._usage_pending and self.path is not None:
            self._write_usage(*self._usage_pending)

    def _write(self, kind, text):
        text = redact(text)
        if not text.strip():
            return
        self.n += 1
        row = {"n": self.n, "kind": kind, "text": text, "ts": round(time.time(), 3)}
        line = json.dumps(row, ensure_ascii=False)
        while len(line.encode("utf-8")) > LINE_MAX and len(row["text"]) > 1:
            row["text"] = row["text"][:max(1, int(len(row["text"]) * 0.8))] + "…"
            line = json.dumps(row, ensure_ascii=False)
        try:
            with open(self.path, "a", encoding="utf-8") as fh:
                fh.write(line + "\n")
        except OSError:
            self.n -= 1
