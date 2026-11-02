"""工位主循环（arch-v10 §2、§3.1~§3.5；protocol §4.3、§4.4、§6、§8）。

一个员工一个工位进程：`xay2 station run --em <em> [--once]`。管家用 start_new_session 起它，管家重启不影响它。

循环：
  上线（employee.online）→ 每 10 秒 station.heartbeat → 取排给自己的下一件活（task.queued，按队列顺序；
  "让他接着改"的活优先）→ 桌子 ensure_desk + align（有没合并的改动：已有甲类决定就等，没有就 task.kept{desk_unmerged}
  + 甲类决定）→ desk.synced → 打开 / 续接**同一个**原生对话（native_id 存在账本里，换了才写 employee.session）
  → 发一轮（turn.started）→ 读适配器事件：思考 / 回复 / 动作写直播通道，里程碑写账本（进度、记忆条、汇报、信件、
  最近一句思考）→ turn.ended（证据 = CLI 状态 + 汇报 + 桌子实际改动）→ evidence.verdict()：
  landed → desk.land → task.landed（冲突 → task.kept{conflict} + 甲类决定）；kept → task.kept + 甲类决定；
  failed → task.failed（M2：工位可以写自己任务的 task.failed）→ 追加工作笔记 → 记忆策略（够门槛就整理记忆）。
  空闲超过提供者表的分钟数 → 关掉原生进程，employee.offline{idle}，退出；有新活时管家再起它，按账本里的
  native_id 续接原来的对话。

记忆（§3.2）：两件活之间达到门槛 → context.compact_requested → Codex 用自带压缩（等压缩那一轮结束）
  → context.compacted{native}；不支持 / 失败 / 2 小时内 CLI 自己压了 ≥2 次 → 交接压缩：先让员工写七节交接摘要
  （kind=handoff_write 的一轮），写不出就拼机器版 → notes.updated → 关掉旧对话、开新对话（段号 +1，
  employee.session{handoff}）→ context.compacted{handoff|machine}。之后第一件活先复述三行，机械检查 → recall.checked。
  别人（负责人 / 管家 / 规则监工）写给这个员工的 context.compact_requested{method: native|handoff|auto}：工位在两件活之间
  看到就按它整理一次（auto / 没写 = 工位自己挑），结果照常写 context.compacted；请求之后已经整理过（要交接的得是交接）
  就算处理过，全看账本，所以同一个请求只处理一次、重启也不重复；上次整理之后还没干过活的请求不用整理。

换线路（probes.md：Plus 满载是 M2 必做项；2026-10-30 工位收尾改成双向）：Codex 一轮因为 capacity / quota / rate_limit
  失败、这一轮没改文件 → 换到另一条线路（Plus ⇄ 中转站；机密项目不换去中转站，只能换回 Plus），这件活只换一次：
  employee.session{同一个 thread, 同一段, reason=line_switch, line=新线路}，用新线路续接同一个 thread（模型取新线路的默认）
  再做一轮（kind=continue），第一次不记失败，写一条 signal{quota}；新线路接不上原 thread → 在新线路开新 thread
  （段号 +1，带工作笔记）。新线路也失败 → 照常 task.failed。之后的活留在新线路上（真机：中转站报
  403 DAILY_LIMIT_EXCEEDED"每日订阅额度不足"→ error_kind=quota → 换回 Plus）。

M3（dsh、agy 员工）：
- 对话号：dsh 的会话号第一轮才知道、agy 的 init 可能晚到——适配器一报上来（session 事件 / 一轮结束）就补写
  employee.session（不写 native_id 为空的 session），下次下班再上班才接得回同一个对话。
- 续接失败（agy 冷续接 30 秒没动静 / 没动作就结束 / dsh 不认会话号）：当场停下这一轮，交接压缩（员工接不上
  旧对话，直接拼机器版摘要）、开新对话、账本记 employee.session{reason: resume_failed}，同一件活重做一次。
- 死循环：连续 3 次报错、中间没用任何工具 → 停下这一轮（记成失败）、signal.raised{error_loop, warn}、交接。
- agy 额度用完（且这一轮没改文件）：通过用户自己的 Antigravity Tools 换号（quota.py），同一个对话重做一次；
  每天最多 40 次调用（quota.agy_take_call，新旧系统合计），到了就不开工、提醒一次。
- agy 每件活的临时 --settings 放行任务卡里的验收命令（卡片 verify 字段，或卡片里写着"验收"那行的 `命令`）。
- dsh 补丁没生效 / 模型不对（适配器抛 AdapterConfigError）：拒绝开工，task.failed + signal.raised{config, warn}。
- 交接摘要后面附机器事实块（memo.facts_block）；旧记录放进新对话前加"只作参考"围栏；同一段记忆里原生 / 自动
  压缩满 2 次，下一次改走交接开新对话。

控制：盯账本里指向自己 / 自己任务的 control.requested（steer / interrupt / stop / stop_all）→ control.done。

**最近一句思考**：思考片段的最后一行（≤80 字），最多每 3 秒写一条 `employee.thought{text}`（新事件，工位写，
没有大白话、页面只刷新），reducer 放进 employees[em].thought，快照 people[em].thought 直接用；一轮结束清空。
"""
from __future__ import annotations

import argparse
import collections
import fcntl
import json
import logging
import os
import re
import signal
import sys
import threading
import time
from pathlib import Path

from .. import providers, quota, quota_live
from ..adapters import make_adapter
from ..adapters.base import QUOTA_LIKE, AdapterBusy, AdapterConfigError, AdapterError, redact
from ..core import append, load_state, max_seq, open_db
from ..core import mail as mailbox
from ..core.errors import PermissionError as LedgerPermissionError
from ..core.evidence import CLI_STATUSES, explain, verdict
from ..core.ids import new_id
from ..core.reducers import KEPT_KINDS, MEMORY_TURNS
from ..desk import desk as deskmod
from ..desk._git import DeskError, git, repo_root
from ..office.plain import is_plain
from . import fakes, markers, memo
from .live import LiveWriter

LOG = logging.getLogger("xay.station")

AI_NAMES = {"codex": "Codex", "dsh": "DeepSeek", "agy": "Gemini"}
KEPT_OPTIONS = [{"id": "merge", "label": "放进项目"}, {"id": "rework", "label": "让他接着做"},
                {"id": "discard", "label": "不要了", "danger": True},
                {"id": "manual", "label": "我已经自己放好了"}]
STATE_TEXT = {"idle": "空闲", "working": "干活中", "compacting": "整理记忆中", "offline": "下线了"}
TOOL_TEXT = {"read": "读文件", "write": "改文件", "cmd": "运行命令", "search": "搜索", "web": "上网",
             "mcp": "工具", "other": "工具"}
OFFLINE_REASONS = ("idle", "crash", "stopped", "fired")
LINE_NAMES = {"codex-sub": "Plus", "codex-relay": "中转站"}
EMPTY_STAT = {"files": 0, "add": 0, "del": 0}

THOUGHT_EVERY_S = 3.0      # 最近一句思考最多这么久写一次账本
THOUGHT_MAX = 80
CONTEXT_STEP = 2.0         # 记忆条：变化 ≥2 个百分点
CONTEXT_EVERY_S = 30.0     # 或距上次 ≥30 秒（§4.4）
AUTO_WINDOW_S = 7200       # 2 小时内 CLI 自己压了 ≥2 次 → 下一件活前交接压缩
DEAD_GRACE_S = 3.0         # 适配器进程没了，再等这么久收尾事件
CONTEXT_MD_MAX = 8000
ERROR_LOOP_N = 3           # 连续这么多次报错、中间没用任何工具 → 死循环（arch-v10 附录 B）
TURN_TOKENS_STEP = 5_000   # 一轮进行中累计 token 每涨这么多写一次 context.updated{turn_tokens}；原为 2 万，实测用量跳得快时会越过 90% 直接到 100%，改为 5000 以便 90% 及时提醒收尾
QUOTA_STEP = 1.0           # 额度（quota.updated）：任一窗口变化 ≥1 个百分点才写
QUOTA_EVERY_S = 600.0      # 或距上次 ≥10 分钟
NATIVE_PER_SEGMENT = 2     # 同一段记忆里原生 / 自动压缩满这么多次，下一次改走交接（token-optimizer 调研）
NO_MEMO_REASONS = ("resume_failed", "error_loop")   # 这两种情况员工写不出像样的摘要：直接拼机器版
COMPACT_REASONS = ("threshold", "rounds", "auto_twice", "resume_failed", "error_loop", "manual")   # §4.4
RETRY_PREFIX = {
    "line": "（上一次因为额度线路满载或额度用完没做成，这次换了一条线路，请重新做这件活）",
    "account": "（上一次因为 Gemini 额度用完没做成，已经换了账号，请在原来的基础上重新做这件活）",
    "resume": "（上一次没能接上原来的对话，已经换了一个新对话，请重新做这件活）",
}
# agy 默认放行的只读命令。2026-10-31 实测：agy 的 command(X) 按命令开头匹配（command(git grep) 放行 `git grep -n …`），
# 用 && / ; 连起来的命令要每一段都放行（放行 echo 不会连带放行 `echo hi && touch x`）。模型常先 ls、git grep 看一眼，
# 被拒这一轮就空手结束了（真机撞上多次）。只放只读的：不放 find（能 -delete / -exec）；sed 只放 sed -n（按行号打印，他常这么看代码）
# xay2：员工身份（XAY_ROLE=executor）下只能写自己的发件箱（进度、汇报、笔记、写信、申请改计划），碰不到账本；
# 聊天轮里说"记住 …"他要用 xay2 notes 改笔记，不放行就被拒（真机撞上）
# cd 和跑测试（python3 -m unittest / pytest）：他常只跑其中一条测试，和任务卡里完整的验收命令开头对不上就被拒（真机撞上）
# ./bin/xay2、V2/bin/xay2：在仓库里他常这样写，按开头匹配对不上 xay2（真机 2026-10-31 被拒后这一轮直接结束）
AGY_SAFE_COMMANDS = ("ls", "pwd", "cd", "git status", "git diff", "git log", "git show", "git grep",
                     "grep", "rg", "cat", "head", "tail", "wc", "sed -n", "xay2", "./bin/xay2", "bin/xay2", "V2/bin/xay2",
                     "./V2/bin/xay2", "python3 -m unittest", "python3 -m pytest")
_SPLIT_COMMAND_RE = re.compile(r"\s*(?:&&|\|\||;)\s*")
_VERIFY_LINE_RE = re.compile(r"验收")
_BACKTICK_RE = re.compile(r"`([^`\n]{1,200})`")


def xay2_home() -> Path:
    env = os.environ.get("XAY2_HOME")
    return Path(env).expanduser() if env else Path.home() / ".xay" / "v2"


def v2_root() -> Path:
    return Path(__file__).resolve().parents[2]


def _manual_text() -> str:
    try:
        p = v2_root() / "docs" / "员工手册.md"
        return p.read_text(encoding="utf-8").strip()
    except OSError:
        return ""


def _env_float(name, default):
    try:
        return float(os.environ.get(name) or default)
    except ValueError:
        return default


def pending_chat_steers(conn, em=None, limit=300):
    """没活时要在原对话里拿出来说的话（protocol §4.3 聊天轮）：负责人 / 用户发给这名员工的插话（不含管家规则发的提醒），
    在他最近一次开工（任何一轮的 turn.started）之后，而且没被当场说过（control.done{request, delivered}）。
    em=None → {em: [(conv, text), …]}（管家每拍用，一次查完）；给了 em → (texts, conv)。只看最近 limit 条插话，开销固定。"""
    steers = conn.execute(
        "SELECT seq, conv, subject, data FROM events WHERE type='control.requested' AND json_extract(data, '$.action')='steer'"
        + (" AND subject=?" if em else "") + " ORDER BY seq DESC LIMIT ?", ((em, limit) if em else (limit,))).fetchall()
    if not steers:
        return ([], None) if em else {}
    low = min(r["seq"] for r in steers)
    started = {}
    for r in conn.execute("SELECT seq, json_extract(data, '$.em') AS em FROM events WHERE type='turn.started' AND seq>?", (low,)):
        started[r["em"]] = max(started.get(r["em"], 0), r["seq"])
    delivered = {r["req"] for r in conn.execute(
        "SELECT json_extract(data, '$.request') AS req FROM events WHERE type='control.done' AND seq>? "
        "AND json_extract(data, '$.delivered')=1", (low,))}
    out = {}
    for r in sorted(steers, key=lambda x: x["seq"]):
        d = json.loads(r["data"]) or {}
        text = str(d.get("text") or "").strip()
        who = r["subject"]
        if not text or d.get("by_source") == "rules" or r["seq"] in delivered or r["seq"] < started.get(who, 0):
            continue
        out.setdefault(who, []).append((r["conv"], text))
    if em:
        rows = out.get(em, [])
        return [t for _, t in rows], next((c for c, _ in rows if c), None)
    return out


def lock_path(home, em) -> Path:
    return Path(home) / "stations" / ("%s.lock" % em)


def pid_path(home, em) -> Path:
    return Path(home) / "stations" / ("%s.pid" % em)


def station_running(home, em) -> bool:
    """这个员工现在有没有工位在跑（看锁：工位活着时一直拿着它）。"""
    path = lock_path(home, em)
    if not path.exists():
        return False
    try:
        with open(path, "a+") as fh:
            try:
                fcntl.flock(fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError:
                return True
            fcntl.flock(fh, fcntl.LOCK_UN)
            return False
    except OSError:
        return False


def _decision(conn, by, em, ws, conv, tk, title, kind, detail, dedup=None, files=None):
    """甲类"需要你"：保留下来的改动怎么办（选项四个，§4.5）。"""
    data = {
        "kind": kind,
        "title": "「%s」的改动还没放进项目，要怎么办？" % (title or tk),
        "detail": detail,
        "task": tk,
        "em": em,
        "options": [dict(o) for o in KEPT_OPTIONS],
        "owner": "lead",
    }
    if files is not None:
        data["files"] = files[:30]
    return append(conn, "decision.opened", by, data,
                  ws=ws, conv=conv, subject=new_id("dc_"), dedup=dedup)


def close_dead_turn(conn, state, em, tn, by, home, detail="工位意外退出，这一轮没做完"):
    """一轮开了工却没人收工（工位崩溃 / 被杀）：写 turn.ended{interrupted}，再按证据表写 kept / failed（+ 甲类决定）。

    工位重启时（by=station:<em>）和管家心跳看门狗（by=office）都用它；幂等键相同，两边谁先写都只记一次。
    桌子原样保留。"""
    tr = (state.get("turns") or {}).get(tn) or {}
    if tr.get("status") != "working":
        return None
    e = (state.get("employees") or {}).get(em) or {}
    ws = e.get("ws")
    tk = tr.get("task")
    t = (state.get("tasks") or {}).get(tk) or {}
    conv = t.get("conv")
    memory = tr.get("kind") in MEMORY_TURNS
    ch = {"changed_files": [], "diff_stat": dict(EMPTY_STAT)}
    desk = Path(home) / "desks" / str(ws) / em
    has_desk = (desk / ".git").exists()
    if not memory and has_desk:
        try:
            ch = deskmod.changes(desk)
        except (DeskError, OSError):
            pass
    evidence = {"cli_status": "interrupted", "changed_files": list(ch["changed_files"]),
                "diff_stat": dict(ch["diff_stat"]), "error": detail}
    append(conn, "turn.ended", by, {"em": em, "status": "interrupted", "evidence": evidence},
           ws=ws, conv=conv, subject=tn, dedup="turn-end:" + tn)
    if memory or not tk or t.get("status") != "working":
        return "closed"
    mode = t.get("mode") if t.get("mode") in ("write", "readonly") else "write"
    has = bool(ch["changed_files"])
    kind, reason = verdict("interrupted", None, has, mode)
    if kind == "kept":
        if has_desk:
            try:
                deskmod.keep(desk, reason)
            except (DeskError, OSError):
                pass
        why = explain("interrupted", None, has, mode)
        files_data = [{"path": f, "kind": "changed"} for f in (ch.get("changed_files") or [])[:30]]
        append(conn, "task.kept", by, {"reason": reason, "detail": why}, ws=ws, conv=conv, subject=tk, dedup="kept:" + tn)
        _decision(conn, by, em, ws, conv, tk, t.get("title"), reason, why, dedup="decision:" + tn, files=files_data)
    else:
        append(conn, "task.failed", by, {"reason": reason, "detail": detail}, ws=ws, conv=conv, subject=tk,
               dedup="failed:" + tn)
    return kind


def _plain_check_on():
    """监工的"大白话把关"（plain_retry）开不开：默认开；XAY2_PLAIN_CHECK=0 关掉（写在这条规则之前的老测试用）。"""
    return os.environ.get("XAY2_PLAIN_CHECK", "1").strip().lower() not in ("0", "off", "false", "no")


class Station:
    def __init__(self, em, *, adapter_factory=None, home=None, heartbeat_s=None, idle_s=None, poll_s=0.2,
                 version_probe=None, turn_max_s=None):
        self.em = em
        self.by = "station:" + em
        self.home = Path(home) if home else xay2_home()
        self.factory = adapter_factory
        self.heartbeat_s = heartbeat_s or _env_float("XAY2_HEARTBEAT_S", 10.0)
        self._idle_s = idle_s
        self.poll_s = poll_s
        self.version_probe = version_probe
        self.turn_max_s = turn_max_s or _env_float("XAY2_TURN_MAX_S", 4 * 3600.0)
        self.conn = None
        self.state = None
        self._seen = -1
        self.ws = None
        self.agent = None
        self.adapter = None
        self.turn = None
        self.cli_version = None
        self.pct = None
        self.calibrating = False
        self._usage = None
        self._ctx_last = (None, 0.0)
        self._think_buf = ""
        self._thought_pending = ""
        self._thought_written = ""
        self._thought_ts = 0.0
        self.pending_steers = []
        self.stop_reason = None
        self._sig = False
        self._force_handoff = None
        self._auto_ts = []
        self._native_done = None
        self._ctl_cursor = 0
        self._wait_seq = None
        self._desk_tried = set()
        self._last_conv = None
        self._outbox_pos = None
        self._session_pending = None     # (reason, segment)：对话号还没报上来，报上来再写 employee.session
        self._config_error = None        # 上一次打开原生对话是因为"设置不对"失败的（拒绝开工）
        self._err_streak = 0             # 连续报错次数（中间用过工具就清零）
        self._cmds = collections.deque(maxlen=20)   # 最近的命令和结果（交接事实块用）
        self._pending_cmd = None
        self._lead_words = []            # 这一段记忆里负责人的原话（任务标题、插话）
        # 别人要求的整理记忆（_scan_compact_requests）：工位重启从头扫，"处理过没有"全看账本，不记在别处
        self._creq_cursor = 0            # 账本扫到哪条了
        self._creq_pending = {}          # {seq: {conv, by, reason, method}}：还没处理的请求
        self._creq_turns = {"any": 0, "handoff": 0}   # 上次整理（任何一种 / 交接或机器版）之后干过几轮活
        self._creq_tried = set()         # 这次运行里试过、没整理成的（打不开对话等）：这次运行里不反复试
        self.live = LiveWriter(self.home / "live", em)
        self._hb_stop = threading.Event()
        self._hb = None
        self._lock_fh = None
        self.rejected_writes = []   # 账本没收下的写入（调试 / 测试看）
        self._task_turn_counts = collections.defaultdict(int)

    # ------------------------------------------------------------ 账本

    def _refresh(self, force=False):
        top = max_seq(self.conn)
        if force or top != self._seen or self.state is None:
            self.state = load_state(self.conn)
            self._seen = top
        return self.state

    def _emp(self):
        return (self.state or {}).get("employees", {}).get(self.em)

    def _w(self, typ, data, *, subject=None, conv=None, by=None, dedup=None):
        try:
            return append(self.conn, typ, by or self.by, data, ws=self.ws, conv=conv,
                          subject=self.em if subject is None else subject, dedup=dedup)
        except (LedgerPermissionError, ValueError) as exc:
            LOG.warning("账本没收下 %s：%s", typ, exc)
            self.rejected_writes.append((typ, str(exc)))
            return None

    def _conv(self):
        return (self.turn or {}).get("conv") or self._last_conv

    def _label(self):
        e = self._emp() or {}
        return e.get("label") or self.em

    def _signal(self, kind, text, level="remind", conv=None, dedup=None, task=None):
        data = {"kind": kind, "level": level, "text": text}
        if task:
            data["task"] = task       # 哪件活（反应表按它找，例如 error_loop 的活不再自动重派）
        return self._w("signal.raised", data, conv=conv or self._conv(), dedup=dedup)

    # ------------------------------------------------------------ 进程外壳

    @property
    def idle_s(self):
        if self._idle_s is not None:
            return self._idle_s
        env = os.environ.get("XAY2_IDLE_SECONDS")
        if env:
            return _env_float("XAY2_IDLE_SECONDS", 60.0)
        minutes = providers.get(self.agent).idle_offline_minutes
        return minutes * 60.0 if minutes else 60.0

    def _take_lock(self, wait=3.0):
        path = lock_path(self.home, self.em)
        path.parent.mkdir(parents=True, exist_ok=True)
        fh = open(path, "a+")
        deadline = time.monotonic() + wait
        while True:
            try:
                fcntl.flock(fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
                self._lock_fh = fh
                return True
            except OSError:
                if time.monotonic() >= deadline:
                    fh.close()
                    return False
                time.sleep(0.1)

    def _release_lock(self):
        try:
            pid_path(self.home, self.em).unlink()
        except OSError:
            pass
        if self._lock_fh is not None:
            try:
                fcntl.flock(self._lock_fh, fcntl.LOCK_UN)
                self._lock_fh.close()
            except OSError:
                pass
            self._lock_fh = None

    def _on_signal(self, signum, _frame):
        self._sig = True

    def _cli_pids(self):
        a = self.adapter
        try:
            return sorted(a.child_pids()) if a is not None and hasattr(a, "child_pids") else []
        except Exception:  # noqa: BLE001
            return []

    def _beat(self, conn=None):
        """心跳带上命令行子进程的 pid（每个都是自己进程组的组长）：总闸 / 管家杀工位时要连它们的进程组一起杀。"""
        pids = self._cli_pids()
        self._beat_pids = pids
        append(conn or self.conn, "station.heartbeat", self.by, {"pid": os.getpid(), "cli_pids": pids},
               ws=self.ws, subject=self.em)

    def _beat_if_pids_changed(self):
        """命令行进程换了（dsh 每轮一个、agy 重启、换线路）就马上补一条心跳，不等 10 秒。"""
        if self._cli_pids() != getattr(self, "_beat_pids", None):
            try:
                self._beat()
            except Exception as exc:  # noqa: BLE001
                LOG.warning("心跳没写上：%s", exc)

    def _start_heartbeat(self):
        def loop():
            conn = None
            while not self._hb_stop.wait(self.heartbeat_s):
                try:
                    if conn is None:
                        conn = open_db()
                    self._beat(conn)
                except Exception as exc:  # noqa: BLE001
                    LOG.warning("心跳没写上：%s", exc)
                    try:
                        if conn is not None:
                            conn.close()
                    except Exception:  # noqa: BLE001
                        pass
                    conn = None
            if conn is not None:
                conn.close()
        self._hb = threading.Thread(target=loop, name="xay-heartbeat-" + self.em, daemon=True)
        self._hb.start()

    def _probe_version(self):
        """记下 CLI 版本（cli-compat.md §1）：和实测过的不一样也照常干活，只写一条提醒。假适配器不探测。"""
        if self.version_probe is not None:
            version = self.version_probe(self.agent)
        elif self.factory is not None or fakes.enabled():
            return
        else:
            version = providers.probe_versions([self.agent]).get(self.agent)
        self.cli_version = version
        tested = providers.get(self.agent).tested_version
        name = AI_NAMES.get(self.agent, self.agent)
        if version is None:
            self._signal("cli_version_changed", "读不到 %s 命令行的版本号，先照常用，留意有没有异常" % name,
                         dedup="cli-version:%s:none" % self.em)
        elif version != tested:
            self._signal("cli_version_changed", "%s 命令行变成了 %s（实测过的是 %s），先照常用，留意有没有异常"
                         % (name, version, tested), dedup="cli-version:%s:%s" % (self.em, version))

    def run(self, once=False):
        self.conn = open_db()
        self._refresh(force=True)
        e = self._emp()
        if not e:
            print("找不到员工：%s" % self.em, file=sys.stderr)
            return 2
        if e.get("former"):
            print("%s 已经离职，不开工位" % e.get("label"), file=sys.stderr)
            return 2
        self.ws, self.agent = e.get("ws"), e.get("agent") or "codex"
        if not self._take_lock():
            print("%s 的工位已经在运行了" % e.get("label"), file=sys.stderr)
            return 0
        pid_path(self.home, self.em).write_text("%d\n" % os.getpid(), encoding="utf-8")
        if threading.current_thread() is threading.main_thread():
            signal.signal(signal.SIGTERM, self._on_signal)
        self._ctl_cursor = max_seq(self.conn)
        self._init_memory(e)
        self._probe_version()
        online = {"pid": os.getpid()}
        if self.cli_version:
            online["cli_version"] = self.cli_version
        self._w("employee.online", online)
        self._beat()
        self._start_heartbeat()
        reason = "crash"
        try:
            self._recover()
            reason = self._loop(once)
        except Exception:  # noqa: BLE001
            LOG.exception("工位出错退出")
            reason = "crash"
        finally:
            self._shutdown(reason)
        return 1 if reason == "crash" else 0

    def _init_memory(self, e):
        ctx = e.get("context") or {}
        self.pct = float(ctx["pct"]) if isinstance(ctx.get("pct"), (int, float)) else None
        self.calibrating = bool(ctx.get("calibrating"))
        self._ctx_last = (self.pct, 0.0)
        self.live.set_segment(ctx.get("segment") or 1)
        now = time.time()
        rows = self.conn.execute("SELECT ts, data FROM events WHERE type='context.compacted' AND subject=? "
                                 "ORDER BY seq DESC LIMIT 20", (self.em,)).fetchall()
        for r in rows:
            if (json.loads(r["data"]) or {}).get("method") != "native_auto":
                break
            if now - r["ts"] < AUTO_WINDOW_S:
                self._auto_ts.append(r["ts"])

    def _recover(self):
        """上一次工位没正常收工（崩溃 / 被杀）留下的那一轮：标"被打断"，桌子保留。"""
        e = self._emp() or {}
        tn = e.get("turn")
        if tn and (self.state.get("turns", {}).get(tn) or {}).get("status") == "working":
            close_dead_turn(self.conn, self.state, self.em, tn, self.by, self.home,
                            detail="上一次工位没正常收工，这一轮算被打断")
            self._refresh(force=True)

    def _shutdown(self, reason):
        self._hb_stop.set()
        try:
            if self.turn is not None:
                tn = self.turn["id"]
                self.turn = None
                self._refresh(force=True)
                close_dead_turn(self.conn, self.state, self.em, tn, self.by, self.home,
                                detail="工位出错退出，这一轮没做完")
        except Exception:  # noqa: BLE001
            LOG.exception("收尾时没能关掉这一轮")
        self._close_adapter()
        self.live.flush()
        try:
            self._w("employee.offline", {"reason": reason if reason in OFFLINE_REASONS else "crash"})
        except Exception:  # noqa: BLE001
            LOG.exception("没写上下线")
        self._release_lock()
        if self._hb is not None:
            self._hb.join(timeout=2.0)
        try:
            self.conn.close()
        except Exception:  # noqa: BLE001
            pass

    # ------------------------------------------------------------ 主循环

    def _loop(self, once):
        idle_since = time.monotonic()
        while True:
            self._poll_controls()
            if self._sig and not self.stop_reason:
                self.stop_reason = "stopped"
            if self.stop_reason:
                return self.stop_reason
            self._refresh()
            e = self._emp()
            if e is None or e.get("former"):
                return "fired"
            if not e.get("online"):     # 管家把我们标成了离线（比如心跳一度没跟上）：重新报到
                self._w("employee.online", {"pid": os.getpid()})
                self._refresh(force=True)
            did = self._desk_actions()
            if not did:
                did = self._requested_compact()     # 别人要求的整理记忆：两件活之间做，不打断正在干的
            if not did:
                tk = self._next_task()
                if tk:
                    did = self._work(tk)
            if not did:
                did = self._chat()
            if did:
                idle_since = time.monotonic()
                continue
            if once:
                return "idle"
            if time.monotonic() - idle_since >= self.idle_s:
                return "idle"
            time.sleep(self.poll_s)

    def _next_task(self):
        if self._wait_seq is not None and self._wait_seq == self._seen:
            return None
        self._wait_seq = None
        e = self._emp() or {}
        tasks = self.state.get("tasks", {})

        def mine(tk):
            t = tasks.get(tk) or {}
            return t.get("status") == "queued" and t.get("owner") == self.em

        order = [tk for tk in e.get("queue", []) if mine(tk)]
        order += sorted(tk for tk in tasks if mine(tk) and tk not in order)
        order.sort(key=lambda tk: 0 if tasks[tk].get("kept_reason") else 1)   # "让他接着改"的先做（改动在桌子上）
        return order[0] if order else None

    def _open_kept_decision(self):
        return any(d.get("em") == self.em and d.get("kind") in KEPT_KINDS
                   for d in self.state.get("decisions", {}).values())

    def _origin(self, t):
        w = self.state.get("workspaces", {}).get(t.get("ws") or self.ws) or {}
        path = w.get("path")
        return path if path and Path(path).is_dir() else None

    def _default_origin(self):
        w = (self.state or {}).get("workspaces", {}).get(self.ws) or {}
        path = w.get("path")
        if path and Path(path).is_dir():
            return path
        for ws in (self.state or {}).get("workspaces", {}).values():
            p = ws.get("path")
            if p and Path(p).is_dir():
                return p
        return None

    def _desk_path(self):
        return self.home / "desks" / str(self.ws) / self.em

    def _desk_actions(self):
        """人做了甲类决定之后桌子上要做的事：合并 → land；丢掉 → discard（之后写 desk.synced）。"""
        tasks = self.state.get("tasks", {})
        # 做过的（tk, 动作）在账本清掉 desk_action 后忘掉：同一件活以后再被决定"合并"还能再做
        self._desk_tried = {x for x in self._desk_tried if (tasks.get(x[0]) or {}).get("desk_action") == x[1]}
        for tk, t in tasks.items():
            action = t.get("desk_action")
            if t.get("owner") != self.em or not action or (tk, action) in self._desk_tried:
                continue
            self._desk_tried.add((tk, action))
            desk, origin = self._desk_path(), self._origin(t)
            self._last_conv = t.get("conv")
            if not (desk / ".git").exists() or not origin:
                act_name = "合并" if action == "merge" else ("确认手动合好" if action == "settle" else "丢掉改动")
                self._signal("offline", "「%s」要%s，但找不到桌子或项目目录" % (t.get("title"), act_name),
                             conv=t.get("conv"))
                continue
            # 笔记里那句"改动先保留，等负责人决定"跟着改成最后的结果（员工下次读笔记才知道后来怎么样了）
            unmerged = "桌子上之前没合并的改动，" if t.get("kept_reason") == "desk_unmerged" else ""
            if action == "merge":
                if self._land(tk, t, desk, origin) == "landed":
                    self._settle_note(tk, t, unmerged + "后来决定合并，已合并回项目")
                else:
                    self._settle_note(tk, t, unmerged + "后来决定合并，但合并时又冲突了，" + memo.KEPT_NOTE)
            elif action == "settle":
                try:
                    ch = deskmod.changes(desk)
                    files = list(ch.get("changed_files") or [])
                    stat = ch.get("diff_stat") or {}
                    tree = deskmod.worktree_tree(desk)
                    st = deskmod.read_state(desk)
                    deskmod._mark_landed(desk, st, tree, tk, files)
                    data = {"files": files, "manual": True}
                    if "add" in stat:
                        data["add"] = stat["add"]
                    if "del" in stat:
                        data["del"] = stat["del"]
                    self._w("task.landed", data, subject=tk, conv=t.get("conv"))
                    self.live.add("status", "按决定确认「%s」的改动已手动合好" % t.get("title"))
                    self._settle_note(tk, t, unmerged + "后来手动合好了，已合并回项目")
                except (DeskError, OSError) as exc:
                    self._signal("offline", "标记「%s」改动已落地时出错：%s" % (t.get("title"), exc), conv=t.get("conv"))
            else:
                try:
                    st = deskmod.discard(desk)
                    self.live.add("status", "按决定丢掉了「%s」留在桌子上的改动" % t.get("title"))
                    self._w("desk.synced", {"task": tk, "base": st["base"], "mode": "reset"}, conv=t.get("conv"))
                    self._settle_note(tk, t, unmerged + "后来决定丢掉，改动已经丢掉了")
                except (DeskError, OSError) as exc:
                    self._signal("offline", "没能丢掉「%s」的改动：%s" % (t.get("title"), exc), conv=t.get("conv"))
            self._refresh(force=True)
            return True
        return False

    # ------------------------------------------------------------ 一件活

    def _work(self, tk):
        t = self.state["tasks"][tk]
        conv = t.get("conv")
        self._last_conv = conv
        if self.agent == "agy" and not self._agy_cap_ok(t, conv):
            self._wait_seq = self._seen         # 今天的 agy 调用次数到上限了：这件活先排着，不开工
            return False
        if not self._quota_ok(t, conv):
            self._wait_seq = self._seen         # 额度都用完了：等账本里有新动静（比如新的额度读数）再看
            return False
        if self.agent == "agy":
            self._agy_precheck(tk, conv)
        origin = self._origin(t)
        if not origin:
            self._fail(tk, conv, "找不到项目目录", "账本里没有这个项目的路径，或者那个目录已经不在了")
            return True
        try:
            desk = deskmod.ensure_desk(origin, self.ws, self.em, self.home)
            cwd = deskmod.workdir(desk, origin)
        except (DeskError, OSError) as exc:
            self._fail(tk, conv, "没法准备桌子", str(exc))
            return True
        self._outbox_init(desk)
        self._maybe_compact(tk, conv, cwd)       # 上一件活后没整理成的 / 一上线就已经很满的
        if self.stop_reason or self._sig:
            return True
        rework = bool(t.get("kept_reason"))
        try:
            res = deskmod.align(desk, origin)
        except (DeskError, OSError) as exc:
            self._fail(tk, conv, "开工前没能对齐桌子", str(exc))
            return True
        if res.mode == "blocked":
            if not rework:
                if self._open_kept_decision():   # 上一件活的改动还等人决定：先等着，不另开工
                    self._wait_seq = self._seen
                    return False
                detail = "还有上次没放进项目的改动：%s。请决定放进项目、让他接着改，还是不要了。" % "、".join(res.unmerged_files[:8])
                self._keep(tk, t, desk, "desk_unmerged", detail)
                return True
            mode = "kept_conflict"   # 接着改：上次保留的改动留在桌子上
        else:
            mode = "reset"
        self._w("desk.synced", {"task": tk, "base": res.base, "mode": mode}, conv=conv)
        if rework:      # 负责人选了"让他接着改"：笔记里那句"等负责人决定"改掉（这一轮的结果收工时另记一行）
            note = (t.get("rework_note") or "").strip()
            self._settle_note(tk, t, "后来让接着改" + ("（负责人的意见：%s）" % note[:80] if note else ""), append=False)
        self._err_streak = 0
        if not self._ensure_adapter(cwd, tk, conv):
            self._open_failed(tk, conv)
            return True
        if self._force_handoff == "resume_failed":
            # 一打开就知道没接上（比如 agy 冷续接时直接换了对话号）：先交接、开新对话，再开工
            if not self._resume_redo(tk, conv, cwd, {}):
                self._open_failed(tk, conv)
                return True
        self._configure_adapter(t)
        self._refresh(force=True)
        e = self._emp()
        kind = "recall" if e.get("recall_due") else ("first" if not e.get("seg_turns") else "continue")
        self._lead_words.append("任务「%s」" % (t.get("title") or tk))
        text, extra = self._turn_input(t, tk, kind, rework, origin)
        ended = self._turn(tk, conv, kind, text, extra, title=t.get("title") or "")
        info = self._close_turn(tk, t, conv, desk, kind, ended)
        tried = set()
        handlers = {"resume": self._resume_redo, "line": self._switch_line, "account": self._switch_account}
        first_done_info = None
        while True:
            # 第一次没做成、也没改文件，而且原因能补救：补救一次（每种只一次），同一件活再做一轮；第一次不记失败
            retry = self._retry_wanted(t, ended, info, tried)
            if not retry:
                break
            tried.add(retry)
            if retry == "plain_retry":
                first_done_info = dict(info)
                kind2 = "plain_retry"
                text = "用一句大白话告诉老板这件活做成了什么、对他有什么用，不要写文件名、函数名、命令和英文术语，只回这一句，格式：【给老板看】……"
                extra2 = None
            elif retry == "network_confirm":
                kind2 = "network_confirm"
                text = "刚才网络断了一下，这一轮没正常收尾。请跑一遍任务卡里的验收命令，确认这件活已经做完，然后再按格式汇报一次结果。"
                extra2 = None
            elif retry in ("network_continue", "network_continue2"):
                kind2 = "network_continue"
                text = "刚才网络断了一下，这一轮没做完。你改过的文件都还在，接着把任务卡里剩下的做完，跑过验收命令后按格式汇报【结果】。"
                extra2 = None
            elif retry in ("denied", "denied2"):
                kind2 = "denied_retry"
                denied_cmds = "；".join(str(x) for x in (info.get("denied") or []))
                text = ("刚才这些操作被系统拒绝了：%s。这里不能运行这些命令；"
                        "找代码用你自带的搜索工具（grep_search / find_by_name），"
                        "看文件用读文件工具（view_file）。请接着把这件活做完。" % denied_cmds)
                if info.get("files"):
                    text += ("你已经改过的文件都还在，接着做任务卡里剩下的部分（不要从头再来）；"
                             "全部做完、验收命令跑过之后，再按格式汇报【结果】。")
                extra2 = None
            else:
                kind2 = handlers[retry](tk, conv, cwd, ended)
                if not kind2:
                    break
                self._configure_adapter(t)
                text, extra2 = self._turn_input(t, tk, kind2, rework, origin)
                text = RETRY_PREFIX[retry] + "\n\n" + text
            ended = self._turn(tk, conv, kind2, text, extra2 if kind2 != "continue" else extra,
                               title=t.get("title") or "")
            info = self._close_turn(tk, t, conv, desk, kind2, ended)
            if first_done_info is not None:
                # 补的这一轮只更新 plain，不影响合并
                new_plain = info.get("plain") or (self.state.get("tasks", {}).get(tk) or {}).get("plain")
                info = dict(first_done_info)
                if new_plain:
                    info["plain"] = new_plain
        outcome = self._conclude(tk, t, desk, origin, info)
        self._after_task(tk, t, conv, cwd, outcome)
        return True

    def _pending_steers_for_chat(self):
        return pending_chat_steers(self.conn, self.em)

    def _chat(self):
        texts, conv = self._pending_steers_for_chat()
        if not texts:
            return False
        origin = self._default_origin()
        desk = self._desk_path()
        try:
            if origin:
                desk = deskmod.ensure_desk(origin, self.ws, self.em, self.home)
                cwd = deskmod.workdir(desk, origin)
            else:
                desk.mkdir(parents=True, exist_ok=True)
                cwd = desk
        except (DeskError, OSError):
            desk.mkdir(parents=True, exist_ok=True)
            cwd = desk
        self._outbox_init(desk)
        conv = conv or self._conv()
        if not self._ensure_adapter(cwd, None, conv):
            return False
        prefix = ("这是负责人在你没活的时候跟你说的话。只回话，不要改项目文件。\n"
                  "如果他让你记住、忘掉、改掉某件事：必须运行 xay2 notes 改你的工作笔记（每条用一句大白话写；xay2 notes --add \"要记的一句\" 加一条；"
                  "xay2 notes --remove \"关键词\" 删掉含这个词的那条；xay2 notes \"全文\" 整份替换），运行成功后再回话。"
                  "只在嘴上答应不算记住：以后换新对话时，带过去的只有工作笔记。")
        input_text = prefix + "\n\n" + "\n\n".join(texts)
        self.pending_steers = []
        ended = self._turn(None, conv, "chat", input_text, extra=None, title="聊天")
        info = self._close_turn(None, None, conv, desk, "chat", ended)
        if info.get("files"):
            # 不丢弃：桌子上可能还留着等你决定的改动；不合并，下一件活开工前按"桌子上有没合并的改动"交负责人决定
            self._signal("chat_changed_files", "%s 在聊天时改了项目文件，改动没有合并，留在他桌子上" % self._label(),
                         level="warn", conv=conv)
        return True

    def _retry_wanted(self, t, ended, info, tried):
        if self.stop_reason or self._sig or info.get("error_loop") or info.get("paused"):
            return None
        if "network_confirm" not in tried and info["status"] != "completed" \
                and info.get("error_kind") == "network" and info.get("claim") == "done":
            return "network_confirm"
        # agy 有命令被拒，这一轮就直接结束了，活常常只做了一半（真机 2026-10-31 两次：改了两个文件就停、没报做完，
        # 却按"有改动"合并了）。没报"做完"就在原对话里接着做，不管改没改文件，最多补 2 轮
        if self.agent == "agy" and info.get("denied") and info.get("claim") != "done":
            n = len({"denied", "denied2"} & tried)
            if n < 2:
                return "denied2" if n else "denied"
        # 网络断开但活没做完时自动接着做（照 network_confirm / denied_retry 的写法，不管改没改文件都补，每件活最多补 2 次）
        if "network_confirm" not in tried and info["status"] != "completed" and info.get("error_kind") == "network" and info.get("claim") != "done":
            n = len({"network_continue", "network_continue2"} & tried)
            if n < 2:
                return "network_continue2" if n else "network_continue"
        # 监工把关（kind = "plain_retry"，每件活最多 1 次）：活报了做完，但没有【给老板看】，或者那句看起来不是大白话
        if _plain_check_on() and "plain_retry" not in tried and info.get("claim") == "done":
            tk = t.get("id") if t else None
            plain_val = info.get("plain") or (self.state.get("tasks", {}).get(tk) or {}).get("plain") or ""
            if not plain_val or not is_plain(plain_val):
                return "plain_retry"
        if info["files"]:
            return None
        if info.get("resume_failed") and "resume" not in tried:
            return "resume"
        if "line" not in tried and self._line_switch_wanted(t, ended, info):
            return "line"
        if self.agent == "agy" and "account" not in tried and info["status"] == "failed" \
                and info.get("error_kind") in ("quota", "ineligible"):
            return "account"
        if self.agent == "agy" and not ({"denied", "denied2"} & tried) and info.get("denied"):
            return "denied"
        return None

    def _open_failed(self, tk, conv):
        err = getattr(self, "_open_error", "") or "原生对话打不开"
        if self._config_error:
            # 硬性约束可能失效（dsh 补丁没生效 / 模型不对）：拒绝开工，提醒负责人
            self._signal("config", "%s 拒绝开工：%s" % (self._label(), err), level="warn", conv=conv,
                         dedup="config:%s:%s" % (self.em, tk))
            self._fail(tk, conv, "设置不对，拒绝开工", err)
        else:
            self._fail(tk, conv, "员工没能上线", err)

    # ---- agy：每天调用上限 / 放行验收命令 / 换号

    def _agy_cap_ok(self, t, conv):
        usage = quota.agy_calls_today(self.home)
        if not usage["cap"] or usage["total"] < usage["cap"]:     # 0 = 不限（默认）
            return True
        self._signal("quota", "今天 Gemini 已经调用了 %d 次（上限 %d，新旧系统合计），「%s」先排着，明天再做"
                     % (usage["total"], usage["cap"], t.get("title")), level="warn", conv=conv,
                     dedup="agy-cap:%s:%s" % (self.em, time.strftime("%Y-%m-%d")))
        self.live.add("status", "今天的 Gemini 调用次数到上限了，先不开工")
        return False

    def _take_call(self, purpose):
        """agy 的每一次调用（一轮 / /compact）先记账；到上限返回 False。其他家不限。"""
        if self.agent != "agy":
            return True
        ok, usage = quota.agy_take_call(self.em, purpose, home=self.home)
        if not ok:
            self._signal("quota", "今天 Gemini 已经调用了 %d 次（上限 %d），这一步不做了" % (usage["total"], usage["cap"]),
                         level="warn", dedup="agy-cap:%s:%s" % (self.em, time.strftime("%Y-%m-%d")))
        return ok

    def _verify_commands(self, t):
        """任务卡里的验收命令：卡片对象的 verify（或 allow_commands）字段；卡片文字 / 步骤里写着"验收"那行的 `命令`。"""
        card = (t or {}).get("card")
        cmds = []
        if isinstance(card, dict):
            v = card.get("verify") or card.get("allow_commands") or []
            cmds += [str(x).strip() for x in ([v] if isinstance(v, str) else v) if str(x).strip()]
            text = str(card.get("text") or "")
        else:
            text = str(card or "")
        for line in text.splitlines() + [str(s) for s in ((t or {}).get("steps") or [])]:
            if _VERIFY_LINE_RE.search(line):
                cmds += [c.strip() for c in _BACKTICK_RE.findall(line) if c.strip()]
        out = []
        for c in cmds:
            if c not in out:
                out.append(c)
        return out[:10]

    def _agy_allow(self, t):
        """agy 这件活放行的命令：验收命令 + 几个只读的常用命令（按命令开头匹配）。验收命令里用 && / ; 连起来的
        （如 `cd V2 && python3 -m unittest …`）拆成一段一段放行，agy 要求每一段都在名单里。"""
        out = []
        for c in self._verify_commands(t):
            for part in [c] + _SPLIT_COMMAND_RE.split(c):
                part = part.strip()
                if part and part not in out:
                    out.append(part)
        for c in AGY_SAFE_COMMANDS:
            if c not in out:
                out.append(c)
        return out

    def _configure_adapter(self, t):
        """agy：这件活的验收命令 / 只读与否和进程启动时不一样，就在下一轮前用同一个对话号重启进程。"""
        if self.agent != "agy" or self.adapter is None or not hasattr(self.adapter, "set_allow_commands"):
            return
        mode = "readonly" if t.get("mode") == "readonly" else "write"
        if self.adapter.set_allow_commands(self._agy_allow(t), mode=mode):
            self.live.add("status", "这件活的验收命令不一样，重启 Gemini 进程（还是同一个对话）")

    def _agy_precheck(self, tk, conv):
        """开工前事前预检：当前账号额度已用完（或偏紧且有更充裕的高优先级账号）时，自动提前切号。
        避免发给 agy 报错 429 后再慢吞吞事后重试。"""
        if os.environ.get("XAY_AGY_AUTO_SWITCH") == "0":
            return
        try:
            q = quota_live.entry(quota_live.lines_now(self.conn), "agy")
            fresh = (q or {}).get("accounts") or []
            accounts = quota.merged_agy_accounts(fresh=fresh, home=self.home)
        except Exception:
            return
        cur = next((a for a in accounts if a.get("current")), None)
        if not cur:
            return
        cur_status = cur.get("status")
        cur_left = cur.get("left")
        is_exhausted = cur_status == "exhausted" or (cur_left is not None and float(cur_left) <= 0)
        is_tight = cur_status == "tight" or (cur_left is not None and float(cur_left) < 20)

        if not is_exhausted and not is_tight:
            return

        skip = quota_live.used_up_accounts(q)
        if cur.get("name") and cur["name"] not in skip and is_exhausted:
            skip.append(cur["name"])

        candidate = quota.choose_agy_account(accounts, skip=skip)
        if not candidate or candidate.get("index") == cur.get("index"):
            return

        if not is_exhausted:
            cand_left = candidate.get("left")
            if candidate.get("status") == "tight" and (cand_left is not None and cur_left is not None and cand_left <= cur_left):
                return

        reason = "开工前检测到当前账号「%s」%s" % (cur.get("name", "当前账号"), "额度已用完" if is_exhausted else "额度偏紧")
        res = quota.switch_agy_account(reason, home=self.home, skip=skip, accounts=accounts)
        if res.get("switched"):
            msg = "Gemini 开工前预检切号：%s" % res.get("text")
            self._signal("quota", msg, conv=conv)
            self.live.add("status", msg)
            if self.adapter is not None:
                self._close_adapter()

    def _switch_account(self, tk, conv, cwd, ended):
        """agy 额度用完：通过用户自己的 Antigravity Tools 换号，然后用同一个对话号重启 agy（换号要重启才生效）。"""
        e = self._emp() or {}
        old = (self.adapter.native_id if self.adapter is not None else None) or e.get("native_id")
        title = (self.state.get("tasks", {}).get(tk) or {}).get("title") or tk
        q = quota_live.entry(quota_live.lines_now(self.conn), "agy")
        fresh = (q or {}).get("accounts") or []
        ineligible = (ended or {}).get("error_kind") == "ineligible"
        if ineligible:
            # 这个号本身用不了（地区不支持等）：记下来，以后预检、换号都跳过它
            cur0 = next((a for a in quota.merged_agy_accounts(fresh=fresh, home=self.home) if a.get("current")), None)
            if cur0:
                quota.mark_agy_blocked(cur0.get("name"), str((ended or {}).get("error") or "账号用不了")[:120], home=self.home)
        accounts = quota.merged_agy_accounts(fresh=fresh, home=self.home)
        skip = quota_live.used_up_accounts(q)   # 已经用完的号不换过去
        cur = next((a for a in accounts if a.get("current")), None)
        if cur and cur.get("name") and cur["name"] not in skip:
            skip.append(cur["name"])  # 刚报了额度错误的当前号加进 skip，避免再换回自己
        why = "当前账号用不了" if ineligible else "额度用完"
        res = quota.switch_agy_account("%s 做「%s」时%s" % (self._label(), title, why), home=self.home, skip=skip, accounts=accounts)
        if not res.get("ok"):
            self._signal("quota", "Gemini 额度用完，自动换账号没成功：%s" % res.get("text"), level="warn", conv=conv)
            self.live.add("status", "额度用完，换账号没成功：%s" % res.get("text"))
            return None
        text = "%s，同一个对话接着做「%s」" % (res.get("text"), title)
        self._signal("quota", text, conv=conv)
        self.live.add("status", text)
        self._close_adapter()
        a = self._make_adapter()
        try:
            a.open(str(cwd), native_id=old, card=self._card(cwd, tk, conv))
        except (AdapterError, ValueError, OSError) as exc:
            LOG.warning("换号后没能重新打开 Gemini：%s", exc)
            self._open_error = str(exc)
            return None
        self.adapter = a
        if self.cli_version:
            a.capabilities.cli_version = self.cli_version
        self._session_pending = ("resume", None)
        self._drain()
        self._sync_session()
        self._refresh(force=True)
        return "recall" if (self._emp() or {}).get("recall_due") else "continue"

    def _resume_redo(self, tk, conv, cwd, ended):
        """续接失败：交接压缩（员工接不上旧对话，直接拼机器版摘要）、开新对话（employee.session{resume_failed}），
        同一件活带着笔记在新对话里重做一次。"""
        e = self._emp() or {}
        ctx = e.get("context") or {}
        seg = int(ctx.get("segment") or 1)
        before = round(self.pct if self.pct is not None else float(ctx.get("pct") or 0.0), 1)
        self._w("context.compact_requested", {"reason": "resume_failed", "method": "handoff"}, conv=conv)
        self.live.add("status", "接不上原来的对话，换一个新对话接着干")
        self._handoff(tk, conv, cwd, before, seg, "resume_failed")
        if not self._ensure_adapter(cwd, tk, conv):
            return None
        self._refresh(force=True)
        return "recall" if (self._emp() or {}).get("recall_due") else "first"

    def _card(self, cwd, tk, conv, line=None):
        out = self._desk_path() / ".xay" / "out"
        env = {"XAY_ROLE": "executor", "XAY2_EM": self.em, "XAY2_TASK": tk or "",
               "XAY2_OUTBOX": str(out / "outbox.jsonl"), "XAY2_TASK_FILE": str(out / "task"),
               "PATH": str(v2_root() / "bin") + os.pathsep + os.environ.get("PATH", "")}
        if conv:
            env["XAY2_CONV"] = conv
        card = {"env": env}
        if self.agent == "codex":
            card["line"] = line or self._current_line()
            model = providers.codex_line_model(card["line"])
            if model:
                card["model"] = model
        else:
            t = (self.state.get("tasks") or {}).get(tk) or {}
            tcard = t.get("card") if isinstance(t.get("card"), dict) else {}
            for key in ("model", "effort"):
                if tcard.get(key):
                    card[key] = str(tcard[key])
            if card.get("model"):
                providers.validate_model(self.agent, card["model"])   # 硬性约束：不合规的模型在起进程之前就拒绝
            if self.agent == "agy":
                card["allow_commands"] = self._agy_allow(t)
                card["mode"] = "readonly" if t.get("mode") == "readonly" else "write"
        return card

    def _current_line(self):
        e = self._emp() or {}
        return e.get("line") or os.environ.get("XAY2_CODEX_LINE") or "codex-sub"

    def _other_line(self):
        """换线路换到哪：Plus ⇄ 中转站。"""
        return "codex-sub" if self._current_line() == "codex-relay" else "codex-relay"

    def _confidential(self, t):
        """机密项目只走 Plus（arch-v10 附录 E）。现在任务卡 / 工作区里都还没有这个开关；有了就认。"""
        card = t.get("card")
        ws = self.state.get("workspaces", {}).get(t.get("ws") or self.ws) or {}
        return bool((isinstance(card, dict) and card.get("confidential")) or ws.get("confidential"))

    def _line_switch_wanted(self, t, ended, info):
        """额度原因（满载 / 额度用完 / 限流）没做成、这一轮没改文件 → 换另一条线路再做一次（两个方向都行）。"""
        kind = info.get("error_kind") or ended.get("error_kind")
        if self.agent != "codex" or info["status"] != "failed" or kind not in QUOTA_LIKE:
            return False
        if info["files"]:
            return False
        if self._other_line() == "codex-relay" and self._confidential(t):
            self.live.add("status", "Plus 满载，但这是机密项目，不换中转站")
            return False
        gone = quota_live.line_gone_text(self.conn, self._other_line())   # 那条线路也用完了（管家查到的真数）：不换过去
        if gone:
            self.live.add("status", "%s，不换过去" % gone)
            return False
        return True

    def _quota_ok(self, t, conv):
        """开工前看一眼真实额度：这种 AI 所有线路（Gemini：所有账号）都用完了 → 这件活先排着，不开工（管家那边写提醒）。"""
        try:
            why = quota_live.agent_out(quota_live.lines_now(self.conn), self.agent,
                                       line=self._current_line() if self.agent == "codex" else None,
                                       confidential=self.agent == "codex" and self._confidential(t))
        except Exception:  # noqa: BLE001  读不到额度就照常开工
            why = None
        if not why:
            self._quota_held = None
            return True
        text = "%s，「%s」先排着" % (why, t.get("title") or "")
        if getattr(self, "_quota_held", None) != text:     # 心跳也会让这里再看一次：同一句话不重复写
            self._quota_held = text
            self.live.add("status", text)
        return False

    @staticmethod
    def _line_switch_text(source, target, error_kind):
        if source == "codex-sub":
            why = "满载" if error_kind == "capacity" else "额度不够"
            return "Codex Plus %s，已换中转站接着做" % why
        why = {"capacity": "满载", "rate_limit": "限流了"}.get(error_kind, "额度用完了")
        return "Codex 中转站%s，已换回 Plus 接着做" % why

    def _switch_line(self, tk, conv, cwd, ended):
        """换到另一条线路（Plus ⇄ 中转站）：先用新线路续接同一个 thread；协议上接不上就在新线路开新 thread
        （第 seg+1 段，带工作笔记）。都打不开返回 None（这件活按第一次的结果记）。"""
        error_kind = ended.get("error_kind")
        e = self._emp() or {}
        old = (self.adapter.native_id if self.adapter is not None else None) or e.get("native_id")
        seg = int((e.get("context") or {}).get("segment") or 1)
        source, target = self._current_line(), self._other_line()
        self._close_adapter()
        card = self._card(cwd, tk, conv, line=target)
        card["line_switch"] = True
        a, new_seg = self._make_adapter(), seg
        try:
            if not old:
                raise AdapterError("没有可以续接的 thread")
            a.open(str(cwd), native_id=old, card=card)
            if a.native_id != old:
                new_seg = seg + 1
        except (AdapterError, ValueError, OSError) as exc:
            LOG.warning("%s接不上原来的 thread，改开新 thread：%s", LINE_NAMES.get(target, target), exc)
            a, new_seg = self._make_adapter(), seg + 1
            try:
                a.open(str(cwd), native_id=None, card=card)
            except (AdapterError, ValueError, OSError) as exc2:
                LOG.warning("%s也打不开：%s", LINE_NAMES.get(target, target), exc2)
                self._open_error = str(exc2)
                self.live.add("status", "想换%s，但打不开：%s" % (LINE_NAMES.get(target, target), exc2))
                return None
        self.adapter = a
        if self.cli_version:
            a.capabilities.cli_version = self.cli_version
        self._session_pending = ("skip", None)    # 下面自己写带 line 的 employee.session
        self._drain()
        self._session_pending = None
        data = {"native_id": a.native_id, "segment": new_seg, "reason": "line_switch", "line": target}
        if self.cli_version:
            data["cli_version"] = self.cli_version
        self._w("employee.session", data)
        self.live.set_segment(new_seg)
        text = self._line_switch_text(source, target, error_kind)
        self._signal("quota", text, conv=conv)
        self.live.add("status", text)
        self._refresh(force=True)
        if new_seg == seg:
            return "continue"
        return "recall" if (self._emp() or {}).get("recall_due") else "first"

    def _make_adapter(self):
        if self.factory is not None:
            return self.factory(self.agent)
        if fakes.enabled():
            return fakes.make(self.agent)
        return make_adapter(self.agent)

    def _ensure_adapter(self, cwd, tk, conv):
        """原生对话：已经开着就接着用；否则按账本里的 native_id 续接（没有就新开）。"""
        if self.adapter is not None and self.adapter.alive():
            return True
        if self.adapter is not None:
            self._close_adapter()
        e = self._emp() or {}
        native = e.get("native_id")
        seg = int((e.get("context") or {}).get("segment") or 1)
        self._config_error = None
        try:
            card = self._card(cwd, tk, conv)
        except providers.ModelNotAllowed as exc:
            self._open_error = self._config_error = str(exc)
            return False
        a = self._make_adapter()
        try:
            a.open(str(cwd), native_id=native, card=card)
            reason, segment = ("resume", seg) if native else (self._open_reason(seg), seg)
        except (AdapterConfigError, providers.ModelNotAllowed) as exc:
            self._open_error = self._config_error = str(exc)
            LOG.warning("设置不对，拒绝开工：%s", exc)
            return False
        except (AdapterError, ValueError, OSError) as exc:
            if not native:
                self._open_error = str(exc)
                LOG.warning("原生对话打不开：%s", exc)
                return False
            LOG.warning("续接原来的对话失败，改开新对话：%s", exc)
            self.live.add("status", "续接原来的对话失败，改开一个新对话")
            a = self._make_adapter()
            try:
                a.open(str(cwd), native_id=None, card=card)
            except (AdapterError, ValueError, OSError) as exc2:
                self._open_error = str(exc2)
                return False
            reason, segment = "resume_failed", seg + 1
        self.adapter = a
        if self.cli_version:
            a.capabilities.cli_version = self.cli_version
        self._session_pending = (reason, segment)
        self._drain()
        self._sync_session()
        if reason == "resume_failed" and self._session_pending is not None:
            self._write_session(None, segment, reason)      # 新对话号还不知道：先把接不上的旧号清掉
        if reason == "resume":
            self.live.add("status", "接着原来的对话干活")
        self._beat_if_pids_changed()
        return True

    def _open_reason(self, seg):
        """开新对话时这一段记忆的来由：上一个工位交接后没等到新对话号就下班了（账本里最后一条是同段的空号），
        就沿用那条的 reason（handoff / resume_failed），不说成 new。"""
        row = self.conn.execute("SELECT data FROM events WHERE type='employee.session' AND subject=? "
                                "ORDER BY seq DESC LIMIT 1", (self.em,)).fetchone()
        d = json.loads(row["data"]) if row else {}
        if d and d.get("native_id") is None and d.get("segment") == seg and d.get("reason") in ("handoff", "resume_failed"):
            return d["reason"]
        return "new"

    def _write_session(self, nid, segment, reason):
        data = {"native_id": nid, "segment": segment, "reason": reason}
        if self.cli_version:
            data["cli_version"] = self.cli_version
        self._w("employee.session", data)
        self.live.set_segment(segment)
        self._refresh(force=True)

    def _sync_session(self):
        """把适配器现在的原生对话号补进账本（employee.session）。dsh 的会话号第一轮才有、agy 的 init 可能晚到，
        所以打开对话、收到 session 事件、一轮结束时都调一次；对话号还不知道就先不写（_session_pending 留着）。"""
        a = self.adapter
        nid = getattr(a, "native_id", None) if a is not None else None
        pending = self._session_pending
        if not nid or (pending and pending[0] == "skip"):
            return
        self._refresh()
        e = self._emp() or {}
        if nid == e.get("native_id"):
            self._session_pending = None
            return
        seg = int((e.get("context") or {}).get("segment") or 1)
        reason, segment = pending or (("resume_failed" if e.get("native_id") else "new"), None)
        if reason == "resume":   # 说是续接，CLI 却给了别的对话号：等于原来的对话没接上
            reason, segment = "resume_failed", None
        if segment is None:
            segment = seg + 1 if reason == "resume_failed" else seg
        self._session_pending = None
        self._write_session(nid, segment, reason)

    def _close_adapter(self):
        if self.adapter is None:
            return
        try:
            self.adapter.close()
        except Exception:  # noqa: BLE001
            LOG.exception("关原生对话时出错")
        self.adapter = None

    def _context_md(self, origin):
        try:
            path = Path(repo_root(origin)) / ".xay" / "context.md"
            return path.read_text(encoding="utf-8", errors="replace")[:CONTEXT_MD_MAX].strip()
        except (OSError, DeskError):
            return ""

    def _turn_input(self, t, tk, kind, rework, origin):
        parts = []
        letters = self._mail(start=tk)          # M4：还没送到的信放在这一轮输入的最开头
        if letters:
            parts.append(letters)
        if kind == "recall":
            parts.append(memo.RECALL_PROMPT)

        is_first = False
        if tk:
            prev_calls = self._task_turn_counts[tk]
            self._task_turn_counts[tk] = prev_calls + 1
            turns_in_state = [tr for tr in (self.state.get("turns", {}) or {}).values() if tr.get("task") == tk]
            is_first = (prev_calls == 0 and len(turns_in_state) == 0)

        if is_first:
            manual = _manual_text()
            if manual:
                parts.append(manual)
        elif tk and not (self.state.get("tasks", {}) or {}).get(tk, {}).get("planned_steps"):
            parts.append("先用【计划】把你的步骤列出来")
        if self.pending_steers:
            for who in ("负责人插话", "监工提醒"):
                said = [s for w, s in self.pending_steers if w == who]
                if said:
                    parts.append("【%s】" % who + "\n".join(said))
            self._lead_words += ["插话：" + s for w, s in self.pending_steers if w == "负责人插话"]
            self.pending_steers = []
        if rework:
            note = (t.get("rework_note") or "").strip()
            parts.append("【接着改】这件活上次的改动还保留在你的桌子上，请在这个基础上接着改。"
                         + ("负责人的意见：" + note if note else ""))
        title = t.get("title") or tk
        parts.append("【任务】%s（编号 %s）" % (title, tk))
        card = t.get("card")
        if isinstance(card, dict):
            card = card.get("text") or json.dumps(card, ensure_ascii=False)
        card = str(card or "").strip()
        if card and card != title:
            parts.append(card)
        if t.get("steps"):
            parts.append("步骤：\n" + "\n".join("%d. %s" % (i, s) for i, s in enumerate(t["steps"], 1)))
        if t.get("writes"):
            parts.append("要改的文件范围：" + "、".join(t["writes"]))
        if t.get("mode") == "readonly":
            parts.append("这是只读任务：只看、只汇报，不要改任何文件。")
        if self.agent == "agy":
            # 无头模式没人能点"允许"：不在放行名单里的命令会被直接拒绝，这一轮就白跑了（M3 真机冒烟第一次就撞上）
            allowed = self._agy_allow(t)
            parts.append("【权限】这里没人能点\"允许\"。只能运行这些命令：%s；其他命令都会被系统拒绝。"
                         "看文件、列目录、找东西请用读文件 / 列目录 / 搜索工具，不要用 ls、cat、grep 之类的命令。"
                         % "、".join("`%s`" % c for c in allowed))
        parts.append(markers.INSTRUCTIONS)
        extra = None
        if kind in ("first", "recall"):
            extra = {}
            notes = (self.state.get("notes", {}).get(self.em) or {}).get("active")
            if notes:   # 旧记录加围栏：只作参考，不是新指令（token-optimizer 调研）
                extra["xay_work_notes"] = "【你的工作笔记】\n" + memo.fenced(notes)
            ctxmd = self._context_md(origin)
            if ctxmd:
                extra["xay_project_context"] = "【项目背景（.xay/context.md）】\n" + memo.fenced(ctxmd)
            extra = extra or None
        return "\n\n".join(parts), extra

    # ------------------------------------------------------------ 一轮

    def _write_task_file(self, tk):
        path = self._desk_path() / ".xay" / "out" / "task"
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(tk or "", encoding="utf-8")
        except OSError:
            pass

    def _turn(self, tk, conv, kind, text, extra, title=""):
        tn = new_id("tn_")
        memory = kind in MEMORY_TURNS
        self._err_streak = 0
        self.turn = {"id": tn, "task": tk, "conv": conv, "kind": kind, "memory": memory, "buf": "", "markers": 0,
                     "claim": None, "progress": None, "outbox_n": 0, "rejects": 0, "interrupting": False,
                     "title": title, "error": None, "resume_failed": False, "error_loop": False}
        self._write_task_file("" if (memory or not tk) else tk)
        self._w("turn.started", {"task": tk, "em": self.em, "kind": kind}, subject=tn, conv=conv)
        self.live.add("status", "整理记忆：写交接摘要" if memory else ("聊天" if kind == "chat" else "开始做「%s」" % title))
        self._think_buf, self._thought_pending = "", ""
        if not self._take_call(kind):
            ended = {"status": "failed", "error": "今天的 Gemini 调用次数到上限了，这一轮没发出去",
                     "error_kind": "daily_cap", "final_text": ""}
        else:
            try:
                self.adapter.send(text, extra)
            except (AdapterError, AdapterBusy, ValueError) as exc:
                ended = {"status": "failed", "error": str(exc), "error_kind": "other", "final_text": ""}
            else:
                ended = self._pump()
        if not self.turn["buf"] and ended.get("final_text"):
            self.turn["buf"] = str(ended["final_text"])
        self.live.flush()
        self.live.add("status", "这一轮结束：%s" % {"completed": "正常做完", "failed": "失败了",
                                                   "interrupted": "被打断了"}.get(ended.get("status"), ended.get("status")))
        return ended

    def _next_event(self, timeout):
        return next(self.adapter.events(timeout=timeout), None)

    def _pump(self):
        deadline = time.monotonic() + self.turn_max_s
        last = 0.0
        dead_since = None
        while True:
            ev = self._next_event(self.poll_s)
            if ev is not None:
                if ev.kind == "turn_ended":
                    return dict(ev.data)
                self._on_event(ev)
            now = time.monotonic()
            if now - last >= self.poll_s:
                last = now
                self.live.tick()
                self._flush_thought()
                if not self.turn["memory"]:
                    self._poll_outbox()
                self._poll_controls()
                self._mail()                    # M4：开工时交代的信记成已送到；新来的信能插话就当场送
                self._beat_if_pids_changed()    # 命令行进程一起来就报 pid（总闸要连进程组一起杀）
                if self._sig and not self.turn["interrupting"]:
                    self.turn["paused"] = True
                    self._interrupt("收到停止信号")
                if now > deadline and not self.turn["interrupting"]:
                    self._interrupt("这一轮做得太久了")
                    self._signal("silent", "%s 这一轮做得太久了，已经叫停" % self._label(), level="warn")
            if ev is None and not self.adapter.alive():
                dead_since = dead_since or now
                if now - dead_since >= DEAD_GRACE_S:
                    return {"status": "interrupted" if self.turn["interrupting"] else "failed", "final_text": "",
                            "error": self.turn.get("error") or "执行者进程意外退出了", "error_kind": "other"}
                time.sleep(0.05)
            else:
                dead_since = None

    def _drain(self):
        if self.adapter is None:
            return
        for ev in self.adapter.drain():
            if ev.kind != "turn_ended":
                self._on_event(ev)

    def _on_event(self, ev):
        k, d = ev.kind, ev.data or {}
        if k == "thinking":
            self.live.add("thinking", d.get("text"))
            self._think_feed(d.get("text"))
        elif k == "text":
            self.live.add("text", d.get("text"))
            if self.turn is not None:
                self.turn["buf"] += str(d.get("text") or "")
                if not self.turn["memory"]:
                    self._scan_markers(final=False)
        elif k == "tool":
            self._err_streak = 0                     # 用了工具：不算"连续报错"
            self._pending_cmd = str(d.get("text") or "")[:200] if d.get("kind") == "cmd" else None
            self.live.add("tool", "%s：%s" % (TOOL_TEXT.get(d.get("kind"), "工具"), d.get("text") or ""))
        elif k == "tool_output":
            if self._pending_cmd:
                rows = [ln.strip() for ln in str(d.get("text") or "").splitlines() if ln.strip()]
                ok = d.get("ok") is not False
                self._cmds.append({"cmd": self._pending_cmd, "ok": ok,
                                   "first_line": ((rows[-1] if not ok else rows[0]) if rows else "")[:160]})
                self._pending_cmd = None
            self.live.add("tool_output", d.get("text"))
        elif k == "session":
            if not d.get("previous"):    # 冷续接时换了对话号：由下面的"续接失败"来处理，这里不记
                self._sync_session()
        elif k == "status":
            self.live.add("status", STATE_TEXT.get(d.get("state"), d.get("state")))
        elif k == "usage":
            self._on_usage(d)
        elif k == "rate_limits":
            self._on_rate_limits(d)
        elif k == "plan":
            self._on_plan(d)
        elif k == "compaction_started":
            self.live.add("status", "正在整理记忆…")
        elif k == "compacted":
            if d.get("auto"):
                self._on_auto_compacted(d)
            else:
                self._native_done = dict(d)
        elif k == "error":
            text = str(d.get("text") or "")
            self.live.add("status", "出错：" + text)
            if self.turn is not None:
                self.turn["error"] = text[:500]
            if d.get("kind") == "context_overflow":
                self._force_handoff = "threshold"
            if d.get("kind") == "resume_failed":
                self._force_handoff = "resume_failed"
                if self.turn is not None and not self.turn["resume_failed"]:
                    # 冷续接没接上：不再等这一轮了，当场停下，由 _work 交接、开新对话、重做一次
                    self.turn["resume_failed"] = True
                    if not self.turn["interrupting"]:
                        self._interrupt("接不上原来的对话")
                return
            if d.get("kind") not in ("network", "quota", "rate_limit", "capacity"):
                self._err_streak += 1
            t = self.turn
            if self._err_streak >= ERROR_LOOP_N and t is not None and not t["memory"] and not t["error_loop"]:
                # 死循环：连续 3 次报错、中间没用任何工具（旧系统那次就这样烧掉了约 94 万 token）
                t["error_loop"] = True
                self._force_handoff = "error_loop"
                self._signal("error_loop", "%s 连续 %d 次报错、中间没用任何工具，已经停下这一轮，换个新对话接着干"
                             % (self._label(), self._err_streak), level="warn", conv=t["conv"],
                             dedup="error-loop:" + t["id"], task=t["task"])
                if not t["interrupting"]:
                    self._interrupt("连续报错、没在干活")

    # ---- 思考一句话

    def _think_feed(self, text):
        self._think_buf = (self._think_buf + str(text or ""))[-800:]
        lines = [ln.strip(" *#>-\t") for ln in self._think_buf.splitlines()]
        lines = [ln for ln in lines if ln]
        if lines:
            self._thought_pending = " ".join(lines[-1].split())[:THOUGHT_MAX]

    def _flush_thought(self):
        p = self._thought_pending
        if not p or p == self._thought_written:
            return
        now = time.monotonic()
        if self._thought_ts and now - self._thought_ts < THOUGHT_EVERY_S:
            return
        self._w("employee.thought", {"text": p}, conv=self._conv())
        self._thought_written, self._thought_ts = p, now

    # ---- 记忆条 / 计划

    def _on_usage(self, d):
        try:
            self.pct = float(d.get("pct") or 0.0)
        except (TypeError, ValueError):
            return
        self.calibrating = False
        self._usage = dict(d)
        if self.turn is not None and isinstance(d.get("turn_total"), (int, float)):
            self.turn["tokens"] = int(d["turn_total"])
            self.live.usage(self.turn["id"], self.turn["tokens"],
                            out=d.get("turn_out"), fresh=d.get("turn_fresh"))   # 细粒度用量只进直播文件（详情栏 token 速度），不进账本
        self._write_context(force=False)

    def _on_rate_limits(self, d):
        """Codex 的额度（Plus 5 小时 / 本周用了百分之几）进账本：quota.updated{line, windows, reached?}（详情栏"额度剩多少"）。
        变了才写：任一窗口差 ≥1 个百分点、重置时间变了、线路变了，或距上次 ≥10 分钟。"""
        windows = []
        for w in d.get("windows") or []:
            if not isinstance(w, dict) or not isinstance(w.get("used_pct"), (int, float)) or not w.get("key"):
                continue
            item = {"key": str(w["key"])[:16], "mins": int(w.get("mins") or 0), "used_pct": round(float(w["used_pct"]), 1)}
            if isinstance(w.get("resets_at"), (int, float)):
                item["resets_at"] = int(w["resets_at"])
            windows.append(item)
        data = {"line": d.get("line") or self._current_line(), "windows": windows}
        if d.get("reached"):
            data["reached"] = str(d["reached"])[:60]
        last, last_ts = getattr(self, "_quota_last", (None, 0.0))
        now = time.time()
        if last is not None and now - last_ts < QUOTA_EVERY_S and last["line"] == data["line"] \
                and last.get("reached") == data.get("reached") and len(last["windows"]) == len(windows) \
                and all(a["key"] == b["key"] and a.get("resets_at") == b.get("resets_at")
                        and abs(a["used_pct"] - b["used_pct"]) < QUOTA_STEP for a, b in zip(last["windows"], windows)):
            return
        if self._w("quota.updated", data, conv=self._conv()) is not None:
            self._quota_last = (data, now)

    def _write_context(self, force):
        """记忆条进账本：变化 ≥2 个百分点或距上次 ≥30 秒（§4.4）。一轮进行中还带上这一轮到现在累计用的
        token（turn_tokens，和 turn.ended.usage.total 同口径），涨了 ≥5000 也写一次，预算的 token 线中途就能管住。"""
        if self._usage is None:
            return
        pct = round(float(self._usage.get("pct") or 0.0), 1)
        last, last_ts = self._ctx_last
        now = time.time()
        t = self.turn
        tokens = t.get("tokens") if t is not None else None
        grew = tokens is not None and tokens - (t.get("tokens_written") or 0) >= TURN_TOKENS_STEP
        if last is not None and pct == last and not grew and not (force and tokens is not None
                                                                   and tokens != t.get("tokens_written")):
            return
        if not (force or grew or last is None or abs(pct - last) >= CONTEXT_STEP or now - last_ts >= CONTEXT_EVERY_S):
            return
        data = {"used": int(self._usage.get("used") or 0), "window": int(self._usage.get("window") or 0), "pct": pct,
                "source": self._usage.get("source") or "", "estimated": bool(self._usage.get("estimated"))}
        if tokens is not None:
            data["turn"], data["turn_tokens"] = t["id"], int(tokens)
        if self._w("context.updated", data, conv=self._conv()) is not None:
            self._ctx_last = (pct, now)
            if tokens is not None:
                t["tokens_written"] = int(tokens)

    def _on_plan(self, d):
        if self.turn is None or self.turn["memory"]:
            return
        steps = [s for s in (d.get("steps") or []) if isinstance(s, dict)]
        if not steps:
            return
        cur = next((i for i, s in enumerate(steps) if s.get("status") == "in_progress"), None)
        done = sum(1 for s in steps if s.get("status") == "completed")
        if cur is not None:
            step, text = cur + 1, steps[cur].get("text") or ""
        else:
            step = done
            text = steps[done - 1].get("text") if done else steps[0].get("text")
        prog = {"step": step, "of": len(steps), "text": str(text or "")[:200], "source": "native_plan"}
        if prog != self.turn["progress"]:
            self.turn["progress"] = prog
            self._w("task.progress", prog, subject=self.turn["task"], conv=self.turn["conv"])

    def _on_auto_compacted(self, d):
        e = self._emp() or {}
        seg = int((e.get("context") or {}).get("segment") or 1)
        before = d.get("before", self.pct)
        data = {"method": "native_auto", "before_pct": round(float(before or 0.0), 1), "segment": seg}
        if d.get("after") is not None:
            data["after_pct"] = round(float(d["after"]), 1)
        self._w("context.compacted", data, conv=self._conv())
        self._auto_ts.append(time.time())
        self._ctx_last = (None, 0.0)

    # ---- 回报：标记 + 发件箱

    def _scan_markers(self, final):
        t = self.turn
        for idx, kind, data in markers.scan(t["buf"], final=final):
            if idx < t["markers"]:
                continue
            t["markers"] = idx + 1
            self._report(kind, data, dedup="marker:%s:%d" % (t["id"], idx), marker=True)

    def _resolve(self, name):
        n = (name or "").strip()
        norm = n.replace(" ", "").lower()
        if norm in ("gm", "总经理", "负责人", "经理"):
            return "gm"
        if norm in ("user", "用户", "老板"):
            return "user"
        for key, e in self.state.get("employees", {}).items():
            if e.get("ws") == self.ws and not e.get("former") and (
                    key == n or (e.get("label") or "").replace(" ", "").lower() == norm):
                return key
        conv = self._conv()
        for d in self.state.get("depts", {}).values():
            if d.get("conv") == conv and not d.get("closed") and n in (d.get("id"), d.get("name")):
                return d.get("id")
        return None

    def _is_dept(self, ref):
        conv = self._conv()
        return any(d.get("conv") == conv and d.get("id") == ref for d in self.state.get("depts", {}).values())

    def _report(self, kind, data, dedup, marker=False):
        t = self.turn
        tk = t["task"] if t else None
        conv = t["conv"] if t else self._conv()
        emby = "employee:" + self.em
        if kind == "plan":
            steps = data.get("steps") or []
            if steps and tk:
                turns_for_task = [tr for tr in (self.state.get("turns") or {}).values() if tr.get("task") == tk]
                cur_round = max(1, len(turns_for_task))
                self._w("task.planned", {"steps": steps, "round": cur_round},
                        subject=tk, conv=conv, by=self.by, dedup=dedup)
                self._refresh(force=True)
        elif kind == "progress":
            data = dict(data)
            data.setdefault("source", "marker" if marker else "outbox")
            t_rec = (self.state.get("tasks", {}) or {}).get(tk) or {}
            planned = t_rec.get("planned_steps")
            if planned:
                plan_len = len(planned)
                if data.get("of") != plan_len:
                    data["of"] = plan_len
            self._w("task.progress", data, subject=tk, conv=conv, by=emby, dedup=dedup)
        elif kind == "plain":
            plain_str = data.get("plain", "")
            if t is not None:
                t["plain"] = plain_str
            if tk:
                self._w("task.reported", {"plain": plain_str},
                        subject=tk, conv=conv, by=emby, dedup=dedup)
        elif kind in ("result", "report"):
            rep_data = {"claim": data["claim"], "summary": data.get("summary", "")}
            plain_str = data.get("plain") or (t.get("plain") if t else None)
            if not plain_str and t and t.get("buf"):
                plain_str = markers.extract_plain(t["buf"])
            if plain_str:
                rep_data["plain"] = plain_str
            if self._w("task.reported", rep_data,
                       subject=tk, conv=conv, by=emby, dedup=dedup) is not None:
                t["claim"] = data["claim"]
        elif kind == "letter":
            self._refresh()
            if data.get("kind") == "reply":
                orig = self.state.get("letters", {}).get(data.get("reply_to")) or {}
                to = orig.get("from") or "gm"
            else:
                to = self._resolve(data.get("to"))
            if not to:
                self._reject("找不到收信人「%s」，这封信没寄出" % data.get("to"))
                return
            kind_ = data.get("kind") or ("request" if self._is_dept(to) else "tell")
            if marker:
                kind_ = "request" if self._is_dept(to) else "tell"
            letter = {"from": self.em, "to": to, "kind": kind_, "subject": data.get("subject") or "", "body": data["body"]}
            if data.get("reply_to"):
                letter["reply_to"] = data["reply_to"]
            self._w("letter.sent", letter, subject=new_id("lt_"), conv=conv, by=emby, dedup=dedup)
        elif kind == "proposal":
            self._w("proposal.made", data, subject=new_id("pp_"), conv=conv, by=emby, dedup=dedup)
        elif kind == "notes":
            op = data.get("op") or "set"
            text = data.get("text") if data.get("text") is not None else data.get("active", "")
            self._refresh()
            old = (self.state.get("notes", {}).get(self.em) or {}).get("active") or ""
            if op == "add":
                new_notes = (old.rstrip("\n") + "\n- " + text) if old else ("- " + text)
                self._w("notes.updated", {"active": new_notes}, by=emby, dedup=dedup)
            elif op == "remove":
                kw = text.lower()
                lines = old.splitlines()
                kept = [ln for ln in lines if kw not in ln.lower()]
                if len(kept) == len(lines):
                    self._signal("outbox_rejected", "笔记里没有找到要删的内容", level="remind", dedup=dedup)
                else:
                    new_notes = "\n".join(kept)
                    self._w("notes.updated", {"active": new_notes}, by=emby, dedup=dedup)
            else:
                self._w("notes.updated", {"active": text}, by=emby, dedup=dedup)

    def _reject(self, text):
        t = self.turn
        if t is not None and t["rejects"] >= 5:
            return
        if t is not None:
            t["rejects"] += 1
        self._signal("outbox_rejected", "%s 的一条回报没记上：%s" % (self._label(), text))

    def _outbox_init(self, desk):
        """发件箱读到哪了：存在 <桌子>/.xay/out/outbox.pos；第一次见到这张桌子就从文件末尾开始（之前的不认）。"""
        if self._outbox_pos is not None:
            return
        out = Path(desk) / ".xay" / "out"
        pos_file = out / "outbox.pos"
        try:
            pos = json.loads(pos_file.read_text(encoding="utf-8"))
            if isinstance(pos.get("offset"), int) and isinstance(pos.get("line"), int):
                self._outbox_pos = pos
                return
        except (OSError, ValueError, AttributeError):
            pass
        path = out / "outbox.jsonl"
        offset = line = 0
        try:
            data = path.read_bytes()
            offset = data.rfind(b"\n") + 1
            line = data[:offset].count(b"\n")
        except OSError:
            pass
        self._outbox_pos = {"offset": offset, "line": line}
        self._save_outbox_pos()

    def _save_outbox_pos(self):
        out = self._desk_path() / ".xay" / "out"
        try:
            out.mkdir(parents=True, exist_ok=True)
            tmp = out / "outbox.pos.tmp"
            tmp.write_text(json.dumps(self._outbox_pos), encoding="utf-8")
            os.replace(tmp, out / "outbox.pos")
        except OSError:
            pass

    def _poll_outbox(self):
        if self._outbox_pos is None:
            return
        path = self._desk_path() / ".xay" / "out" / "outbox.jsonl"
        try:
            size = path.stat().st_size
        except OSError:
            return
        pos = self._outbox_pos
        if size < pos["offset"]:      # 文件被截断了：从头读，行号接着往下编（幂等键不撞）
            pos["offset"] = 0
        if size == pos["offset"]:
            return
        try:
            with open(path, "rb") as fh:
                fh.seek(pos["offset"])
                chunk = fh.read(min(size - pos["offset"], 1 << 20))
        except OSError:
            return
        cut = chunk.rfind(b"\n")
        if cut < 0:
            return
        for raw in chunk[:cut].split(b"\n"):
            pos["line"] += 1
            if raw.strip():
                self._outbox_line(pos["line"], raw)
        pos["offset"] += cut + 1
        self._save_outbox_pos()

    def _outbox_line(self, lineno, raw):
        t = self.turn
        if t and t["outbox_n"] >= markers.OUTBOX_TURN_MAX:
            self._reject("这一轮发件箱超过 %d 行，后面的不记了" % markers.OUTBOX_TURN_MAX)
            return
        if t:
            t["outbox_n"] += 1
        try:
            raw_text = raw.decode("utf-8", "replace") if isinstance(raw, bytes) else str(raw)
            row = json.loads(raw_text)
        except Exception:
            row = None

        is_notes = isinstance(row, dict) and (row.get("type") == "notes" or (isinstance(row.get("data"), dict) and row["data"].get("kind") == "notes"))
        if is_notes:
            row_em = row.get("em") or (row.get("data") or {}).get("em") or (row.get("data") or {}).get("from")
            if row_em != self.em:
                self._reject("写的不是自己的笔记")
                return
            d = row.get("data") or {}
            op = d.get("op") or "set"
            text = d.get("text") if d.get("text") is not None else d.get("active", "")
            if not isinstance(text, str):
                self._reject("笔记要是文字")
                return
            self._report("notes", {"op": op, "text": text[:markers.NOTES_MAX]}, dedup="outbox:%s:%d" % (self.em, lineno))
            return

        is_verdict = isinstance(row, dict) and (row.get("type") == "verdict" or (isinstance(row.get("data"), dict) and row["data"].get("kind") == "verdict"))
        if is_verdict:
            if not t:
                self._reject("没有在干活，这次 verdict 没记上")
                return
            self._refresh()
            cur_task = (self.state.get("tasks") or {}).get(t["task"]) or {}
            review_of = cur_task.get("review_of")
            if not review_of:
                self._reject("当前任务不是审查活，不能交 verdict")
                return
            d = row.get("data") or {}
            ok = bool(d.get("ok"))
            text = str(d.get("text") or "")
            conv = t.get("conv") or self._conv()
            self._w("check.result", {"task": review_of, "ok": ok, "text": text},
                    subject=review_of, conv=conv, dedup="outbox:%s:%d" % (self.em, lineno))
            return

        if not t:
            self._reject("没有在干活，这次回报没记上")
            return
        try:
            typ, data = markers.validate_outbox(raw, t["task"], self.em)
        except markers.Rejected as exc:
            self._reject(str(exc))
            return
        self._report(typ, data, dedup="outbox:%s:%d" % (self.em, lineno))

    # ---- 收工：证据 → 结论

    def _close_turn(self, tk, t, conv, desk, kind, ended):
        """收工第一步：读完回报、写 turn.ended（证据），还不下结论。"""
        self._poll_outbox()
        self._scan_markers(final=True)
        plain_from_buf = markers.extract_plain(self.turn.get("buf", ""))
        if not plain_from_buf and kind == "plain_retry":
            candidate = (self.turn.get("buf") or "").strip()
            if candidate and is_plain(candidate):
                plain_from_buf = candidate
        if plain_from_buf and not self.turn.get("plain"):
            self.turn["plain"] = plain_from_buf
            if tk:
                emby = "employee:" + self.em
                self._w("task.reported", {"plain": plain_from_buf},
                        subject=tk, conv=conv, by=emby, dedup="plain:%s:%s" % (self.turn["id"], tk))
        self._flush_thought()
        self.live.flush()
        if not self.turn["resume_failed"]:
            self._sync_session()
        tn = self.turn["id"]
        status = ended.get("status") if ended.get("status") in CLI_STATUSES else "failed"
        err = ended.get("error") or self.turn.get("error")
        error_kind = ended.get("error_kind")
        if self.turn["error_loop"]:      # 死循环被我们叫停：记成"失败"（不是被人打断），好让反应表接着处理
            status, error_kind = "failed", "error_loop"
            err = "连续 %d 次报错、中间没用任何工具，已经停下这一轮" % ERROR_LOOP_N
        elif self.turn["resume_failed"]:
            error_kind = "resume_failed"
            err = err or "接不上原来的对话"
        try:
            ch = deskmod.changes(desk)
        except (DeskError, OSError) as exc:
            LOG.warning("读不出桌子上的改动：%s", exc)
            ch = {"changed_files": [], "diff_stat": dict(EMPTY_STAT)}
        claim = self.turn["claim"]
        evidence = {"cli_status": status, "changed_files": list(ch["changed_files"]), "diff_stat": dict(ch["diff_stat"])}
        if self.turn.get("paused"):
            evidence["paused"] = True
        if claim:
            evidence["claim"] = claim
        if err and status != "completed":
            evidence["error"] = redact(str(err))[:500]
        if error_kind and status != "completed":
            evidence["error_kind"] = str(error_kind)      # 给反应表看：额度 / 续接失败 / 死循环 / 设置不对 …
        denied = [redact(str(x))[:200] for x in (ended.get("denied") or [])][:10]
        if denied:      # agy 无头模式里被拒绝的操作（没在放行名单里的命令等）
            evidence["denied"] = denied
            self.live.add("status", "这些操作被系统拒绝了：" + "；".join(denied[:3]))
        if ch.get("executor_committed"):
            evidence["executor_committed"] = True
            self._signal("executor_commit", "%s 自己做了 git commit（规定不许），改动照样按证据处理" % self._label(),
                         level="warn", task=tk)
        data = {"em": self.em, "status": status, "evidence": evidence}
        usage = ended.get("usage")
        if isinstance(usage, dict):
            u = {k: usage[k] for k in ("in", "out", "cached", "total") if isinstance(usage.get(k), (int, float))}
            if u:
                data["usage"] = u
        self._w("turn.ended", data, subject=tn, conv=conv, dedup="turn-end:" + tn)
        self._write_context(force=True)
        resume_failed, error_loop = self.turn["resume_failed"], self.turn["error_loop"]
        paused = bool(self.turn.get("paused"))
        if kind == "recall" and not resume_failed:
            self._check_recall(tk, t, conv)
        turn_plain = self.turn.get("plain")
        self.turn = None
        if error_kind == "config":
            self._signal("config", "%s 拒绝开工：%s" % (self._label(), evidence.get("error") or "设置不对"),
                         level="warn", conv=conv, dedup="config:" + tn)
        plain_text = turn_plain or (self.state.get("tasks", {}).get(tk) or {}).get("plain") or plain_from_buf or ""
        return {"tn": tn, "status": status, "claim": claim, "files": list(ch["changed_files"]),
                "error": evidence.get("error") or "", "error_kind": error_kind if status != "completed" else None,
                "resume_failed": resume_failed, "error_loop": error_loop, "paused": paused,
                "denied": denied, "plain": plain_text}

    def _conclude(self, tk, t, desk, origin, info):
        """收工第二步：证据 → 结论（landed / kept / failed）。"""
        mode = t.get("mode") if t.get("mode") in ("write", "readonly") else "write"
        status, claim, has, tn = info["status"], info["claim"], bool(info["files"]), info["tn"]
        v, reason = verdict(status, claim, has, mode)
        if v == "landed":
            v = self._land(tk, t, desk, origin)
        elif info.get("paused") and not has:
            # 这一轮是因为 stop / stop_all 被停下的，而且桌子上没有改动：不写 task.failed，只写 turn.ended
            self._refresh(force=True)
            return {"verdict": "paused", "reason": "paused", "files": [], "status": status, "claim": claim}
        elif v == "kept" and info.get("paused") and reason in ("interrupted", "failed_with_changes"):
            # 叫停 / 总闸停下的：任务记"已停下"（paused）；"需要你"仍是甲类（合并 / 接着改 / 丢掉照常能用）
            why = "叫停了，这一轮改好的文件还没放进项目，你可以选择放进项目、让他接着做，或者不要了"
            self._keep(tk, t, desk, "paused", why, tn=tn, decision_kind=reason)
        elif v == "kept":
            self._keep(tk, t, desk, reason, explain(status, claim, has, mode), tn=tn)
        else:
            self._fail(tk, t.get("conv"), reason, info["error"], dedup="failed:" + tn)
        self._refresh(force=True)
        return {"verdict": v, "reason": reason, "files": info["files"], "status": status, "claim": claim}

    def _last_task(self, exclude):
        turns = [tr for tr in self.state.get("turns", {}).values()
                 if tr.get("em") == self.em and tr.get("kind") not in MEMORY_TURNS]
        turns.sort(key=lambda tr: tr.get("started") or 0)
        order = []
        for tr in turns:
            tk = tr.get("task")
            if not tk:
                continue
            if tk in order:
                order.remove(tk)
            order.append(tk)
        past = [x for x in order if x != exclude]
        if not past:
            return None, [], ""
        last_tk = past[-1]
        t = self.state.get("tasks", {}).get(last_tk) or {}
        files = []
        for tr in turns:
            if tr.get("task") == last_tk:
                for f in (tr.get("evidence") or {}).get("changed_files") or []:
                    if f not in files:
                        files.append(f)
        for f in t.get("files") or []:
            if f not in files:
                files.append(f)
        return last_tk, files, t.get("title") or ""

    def _previous_files(self, exclude):
        _, files, _ = self._last_task(exclude)
        return files

    def _check_recall(self, tk, t, conv):
        self._refresh(force=True)
        card = t.get("card")
        card = (card.get("text") or "") if isinstance(card, dict) else str(card or "")
        _, last_files, last_title = self._last_task(tk)
        ok, missing = memo.recall_check(self.turn["buf"], tk, t.get("title"), last_files, card, last_title=last_title)
        self._w("recall.checked", {"ok": ok, "missing": missing}, conv=conv)
        if not ok:
            self.live.add("status", "复述里没提到%s，可能失忆了" % "和".join({"task": "当前任务", "file": "改过的文件"}[m] for m in missing))

    def _land(self, tk, t, desk, origin):
        conv = t.get("conv")
        try:
            res = deskmod.land(desk, origin, task=tk, home=self.home)
        except (DeskError, OSError) as exc:
            self._keep(tk, t, desk, "conflict", "放回项目时发生冲突，改动还没放进项目：%s" % exc)
            return "kept"
        if res.status in ("landed", "empty"):
            self._w("task.landed", {"files": list(res.files), "add": res.add, "del": res.del_}, subject=tk, conv=conv)
            self.live.add("status", res.detail)
            return "landed"
        self._keep(tk, t, desk, "conflict", res.detail)
        return "kept"

    def _keep(self, tk, t, desk, reason, detail, tn=None, decision_kind=None):
        conv = t.get("conv")
        try:
            deskmod.keep(desk, reason)
        except (DeskError, OSError):
            pass
        self._w("task.kept", {"reason": reason, "detail": detail}, subject=tk, conv=conv,
                dedup=("kept:" + tn) if tn else None)
        files_data = []
        try:
            ch = deskmod.changes(desk)
            changed_files = list(ch.get("changed_files") or [])
            fk = "conflict" if (decision_kind or reason) == "conflict" else "changed"
            files_data = [{"path": f, "kind": fk} for f in changed_files[:30]]
        except Exception:
            pass
        try:
            _decision(self.conn, self.by, self.em, self.ws, conv, tk, t.get("title"), decision_kind or reason, detail,
                      dedup=("decision:" + tn) if tn else None, files=files_data)
        except (LedgerPermissionError, ValueError) as exc:
            LOG.warning("没写上需要你：%s", exc)
        self.live.add("status", "改好的文件还没放进项目，等负责人决定：%s" % detail)

    def _fail(self, tk, conv, reason, detail, dedup=None):
        self._w("task.failed", {"reason": reason, "detail": redact(str(detail or ""))[:500]}, subject=tk, conv=conv,
                dedup=dedup)
        self.live.add("status", "没做成：%s" % reason)

    def _after_task(self, tk, t, conv, cwd, outcome):
        if outcome.get("verdict") == "paused":
            return
        self._append_note(tk, t, outcome)
        if self.stop_reason or self._sig:
            return
        self._maybe_compact(tk, conv, cwd)

    def _append_note(self, tk, t, outcome):
        self._refresh(force=True)
        text = {"landed": "已合并回项目", "kept": memo.KEPT_NOTE, "failed": "没做成"}.get(outcome["verdict"], "")
        files = outcome["files"]
        summary = (self.state.get("tasks", {}).get(tk) or {}).get("result") or ""
        line = "- 「%s」（%s）：%s" % (t.get("title"), tk, text)
        line += ("；改了 %d 个文件：%s" % (len(files), "、".join(files[:6]))) if files else "；没有改文件"
        if summary:
            line += "；汇报：" + summary[:150]
        old = (self.state.get("notes", {}).get(self.em) or {}).get("active") or ""
        active, archived = memo.append_note(old, line)
        data = {"active": active}
        if archived:
            data["archived_add"] = archived
        self._w("notes.updated", data, conv=t.get("conv"))

    def _settle_note(self, tk, t, outcome, append=True):
        """保留下来的活有了结果：笔记里那句"改动先保留，等负责人决定"改成最后怎么样了（找不到那一行就补一行）。"""
        self._refresh(force=True)
        old = (self.state.get("notes", {}).get(self.em) or {}).get("active") or ""
        res = memo.settle_note(old, tk, t.get("title"), outcome, append=append)
        if res is None or res[0] == old:
            return
        data = {"active": res[0]}
        if res[1]:
            data["archived_add"] = res[1]
        self._w("notes.updated", data, conv=t.get("conv"))

    # ------------------------------------------------------------ 记忆策略

    def _maybe_compact(self, tk, conv, cwd):
        if self.calibrating and not self._force_handoff:
            return          # 刚整理过，等下一条真实的记忆条再判断
        self._refresh(force=True)
        e = self._emp() or {}
        ctx = e.get("context") or {}
        pct = self.pct if self.pct is not None else float(ctx.get("pct") or 0.0)
        now = time.time()
        self._auto_ts = [x for x in self._auto_ts if now - x < AUTO_WINDOW_S]
        method = None
        seg = int(ctx.get("segment") or 1)
        if self._force_handoff:
            reason, method = self._force_handoff, "handoff"
        elif len(self._auto_ts) >= 2:
            reason, method = "auto_twice", "handoff"
        else:
            used = (self._usage or {}).get("used") or ctx.get("used")
            reason = providers.compaction_reason(self.agent, pct, int(e.get("seg_turns") or 0),
                                                 used=int(used) if isinstance(used, (int, float)) else None)
            if reason is None:
                return
            if self._native_in_segment(seg) >= NATIVE_PER_SEGMENT:
                method = "handoff"      # 同一段记忆已经原生压过 2 次：每压一次丢一大半细节，这次换新对话
        self._compact_now(tk, conv, cwd, reason, method)

    def _compact_now(self, tk, conv, cwd, reason, method, announce=True, why=None):
        """整理一次记忆。method：native / handoff / None（= auto：能原生就原生，同一段已经原生压满 2 次或 2 小时内
        CLI 自己压了 2 次就交接）。announce=False：账本里已经有一条别人写的、员工正显示"整理记忆中"的请求，不再重复写。
        → 打不开原生对话返回 False，其余（原生成功 / 交接，都写了 context.compacted）返回 True。"""
        if not self._ensure_adapter(cwd, tk, conv):
            return False
        e = self._emp() or {}
        ctx = e.get("context") or {}
        pct = self.pct if self.pct is not None else float(ctx.get("pct") or 0.0)
        seg = int(ctx.get("segment") or 1)
        native_ok = bool(self.adapter.capabilities.native_compact)
        if method not in ("native", "handoff"):
            busy = self._native_in_segment(seg) >= NATIVE_PER_SEGMENT or len(self._auto_ts) >= 2
            method = "native" if native_ok and not busy else "handoff"
        elif method == "native" and not native_ok:
            method = "handoff"          # 这家没有能用的自带压缩（dsh）：只能交接
        if announce:
            self._w("context.compact_requested", {"reason": reason, "method": method}, conv=conv)
        self.live.add("status", why or "记忆用了 %.0f%%，开始整理记忆" % pct)
        before = round(pct, 1)
        if method == "native":
            self._native_done = None
            try:
                ok = self._take_call("compact") and bool(self.adapter.compact())
            except (AdapterError, AdapterBusy, ValueError) as exc:
                LOG.warning("自带压缩失败：%s", exc)
                ok = False
            self._drain()
            if ok:
                data = {"method": "native", "before_pct": before, "segment": seg}
                after = (self._native_done or {}).get("after")
                if after is not None:
                    data["after_pct"] = round(float(after), 1)
                self._w("context.compacted", data, conv=conv)
                self._compacted(after)
                return True
            self.live.add("status", "自带压缩没成功，改用交接压缩")
        self._handoff(tk, conv, cwd, before, seg, reason)
        return True

    # ---- 别人要求的整理记忆（负责人 / 管家 / 规则监工写的 context.compact_requested）

    def _scan_compact_requests(self):
        """扫账本：发给这个员工、不是工位自己写的 context.compact_requested，哪些还没处理。

        "处理过" = 请求之后有过一次整理（context.compacted，不算 CLI 干活中途自己压的 native_auto）；要求交接的，
        得是交接 / 机器版那种才算。请求之前（上次整理之后）一轮活都没干过的，没有要整理的东西，直接算处理过。
        全从账本算，所以同一个请求只处理一次，工位重启也不会重复。"""
        if self._creq_cursor >= max_seq(self.conn):
            return
        rows = self.conn.execute(
            "SELECT seq, type, conv, by, data FROM events WHERE seq>? AND ((subject=? AND type IN "
            "('context.compact_requested','context.compacted')) OR (type='turn.started' AND instr(data, ?) > 0)) "
            "ORDER BY seq", (self._creq_cursor, self.em, '"%s"' % self.em)).fetchall()
        turns, pending = self._creq_turns, self._creq_pending
        for r in rows:
            self._creq_cursor = r["seq"]
            d = json.loads(r["data"]) or {}
            if r["type"] == "turn.started":
                if d.get("em") == self.em and d.get("kind") not in MEMORY_TURNS:
                    turns["any"] += 1
                    turns["handoff"] += 1
            elif r["type"] == "context.compacted":
                m = d.get("method")
                if m == "native_auto":
                    continue
                full = m in ("handoff", "machine")
                turns["any"] = 0
                if full:
                    turns["handoff"] = 0
                for seq in [s for s, q in pending.items() if s < r["seq"] and (full or q["method"] != "handoff")]:
                    pending.pop(seq)
            elif not str(r["by"]).startswith("station:"):
                method = d.get("method") if d.get("method") in ("native", "handoff") else "auto"
                if turns["handoff" if method == "handoff" else "any"] == 0:
                    LOG.info("%s 上次整理记忆之后还没干过活，不用整理（请求 #%d）", self.em, r["seq"])
                    continue
                pending[r["seq"]] = {"conv": r["conv"], "by": r["by"], "method": method,
                                     "reason": d.get("reason") if d.get("reason") in COMPACT_REASONS else "manual"}

    def _requested_compact(self):
        """两件活之间：有别人要求的整理记忆就做一次（不打断正在干的活——这里只在两件活之间被调用）。
        几个请求一起来：合并成一次，有人要交接就交接，否则有人要原生就原生，否则工位自己挑。"""
        if self.stop_reason or self._sig:
            return False
        self._scan_compact_requests()
        todo = sorted(s for s in self._creq_pending if s not in self._creq_tried)
        if not todo:
            return False
        reqs = [self._creq_pending[s] for s in todo]
        self._creq_tried.update(todo)
        methods = {q["method"] for q in reqs}
        method = "handoff" if "handoff" in methods else ("native" if "native" in methods else None)
        reason = reqs[-1]["reason"]
        e = self._emp() or {}
        last = (self.state.get("tasks", {}).get(e.get("last_task")) or {})
        conv = next((q["conv"] for q in reversed(reqs) if q["conv"]), None) or last.get("conv") or self._last_conv
        who = {"office": "监工", "rules": "监工", "user": "你"}.get(reqs[-1]["by"], "负责人")   # 页面上后台自动的事都算监工
        origin = self._origin({})
        try:
            if not origin:
                raise DeskError("账本里没有这个项目的路径，或者那个目录已经不在了")
            desk = deskmod.ensure_desk(origin, self.ws, self.em, self.home)
            cwd = deskmod.workdir(desk, origin)
        except (DeskError, OSError) as exc:
            self._signal("offline", "%s要 %s 整理记忆，但找不到桌子：%s" % (who, self._label(), exc), conv=conv,
                         dedup="compact-req-fail:%s:%d" % (self.em, todo[-1]))
            return True
        self._outbox_init(desk)
        self._last_conv = conv
        self._refresh(force=True)
        announce = (self._emp() or {}).get("state") != "compacting"   # 请求是他干活时来的：状态已经回到空闲，再标一次
        ok = self._compact_now(e.get("last_task"), conv, cwd, reason, method, announce=announce,
                               why="%s要求整理记忆，开始整理" % who)
        if not ok:
            self._signal("offline", "%s要 %s 整理记忆，但没能打开他原来的对话：%s"
                         % (who, self._label(), getattr(self, "_open_error", "") or "原因不明"), conv=conv,
                         dedup="compact-req-fail:%s:%d" % (self.em, todo[-1]))
        self._scan_compact_requests()
        return True

    def _native_in_segment(self, seg):
        """这一段记忆里已经原生 / 自动压过几次（从账本数，工位重启也算数）。"""
        n = 0
        rows = self.conn.execute("SELECT data FROM events WHERE type='context.compacted' AND subject=? "
                                 "ORDER BY seq DESC LIMIT 50", (self.em,)).fetchall()
        for r in rows:
            d = json.loads(r["data"]) or {}
            if d.get("segment") != seg or d.get("method") not in ("native", "native_auto"):
                break
            n += 1
        return n

    def _facts_block(self, tk):
        """交接摘要后面附的机器事实（memo.facts_block）：从账本、桌子、这一段记忆里见过的命令原样取。"""
        self._refresh(force=True)
        turns = sorted((tr for tr in self.state.get("turns", {}).values()
                        if tr.get("em") == self.em and tr.get("kind") not in MEMORY_TURNS),
                       key=lambda tr: tr.get("started") or 0)
        order = []
        for tr in turns:
            if tr.get("task") in order:
                order.remove(tr.get("task"))
            order.append(tr.get("task"))
        tasks = []
        for x in order[-6:]:
            t = self.state.get("tasks", {}).get(x) or {}
            files = []
            for tr in turns:
                if tr.get("task") == x:
                    files += [f for f in (tr.get("evidence") or {}).get("changed_files") or [] if f not in files]
            tasks.append({"id": x, "title": t.get("title"), "status": t.get("status"), "files": files})
        diff = ""
        desk = self._desk_path()
        if (desk / ".git").exists():
            try:
                diff = git(desk, "diff", "--stat", "HEAD").decode("utf-8", "replace")
            except (DeskError, OSError):
                diff = ""
        verify_cmds = set(self._verify_commands(self.state.get("tasks", {}).get(tk) or {}))
        verify = [c for c in self._cmds if c["cmd"] in verify_cmds or any(v in c["cmd"] for v in verify_cmds)]
        last = {}
        for c in verify:
            last[c["cmd"]] = c
        return memo.facts_block(tasks, diff, (self._emp() or {}).get("desk_base") or "", list(self._cmds),
                                list(self._lead_words), list(last.values()))

    def _handoff(self, tk, conv, cwd, before, seg, reason):
        memo_text, method = None, "machine"
        session_reason = "resume_failed" if reason == "resume_failed" else "handoff"
        if self.adapter is not None and self.adapter.alive() and reason not in NO_MEMO_REASONS:
            ended = self._turn(tk, conv, "handoff_write", memo.HANDOFF_PROMPT, None, title="整理记忆")
            tn, reply = self.turn["id"], self.turn["buf"]
            status = ended.get("status") if ended.get("status") in CLI_STATUSES else "failed"
            self._w("turn.ended", {"em": self.em, "status": status,
                                   "evidence": {"cli_status": status, "changed_files": [], "diff_stat": dict(EMPTY_STAT)}},
                    subject=tn, conv=conv, dedup="turn-end:" + tn)
            self.turn = None
            if status == "completed" and memo.valid_memo(reply):
                memo_text, method = memo.trim_memo(reply), "handoff"
        self._refresh(force=True)
        old = (self.state.get("notes", {}).get(self.em) or {}).get("active") or ""
        if memo_text is None:
            memo_text = self._machine_memo()
            prev = old.split(memo.FACTS_TITLE)[0].strip()
            if prev:   # 机器版不知道员工以前的判断：把上一份笔记照抄在后面（放不下就截掉尾巴）
                memo_text += "\n## 之前的工作笔记（照抄）\n" + prev
        memo_text = memo.with_facts(memo_text, self._facts_block(tk))   # 员工的判断 + 工位记的事实
        notes = {"active": memo_text}
        if old.strip():
            notes["archived_add"] = [old]
        self._w("notes.updated", notes, conv=conv)
        # 关掉旧对话，开一个新对话（第 seg+1 段记忆）
        self._close_adapter()
        a = self._make_adapter()
        self._session_pending = (session_reason, seg + 1)
        try:
            a.open(str(cwd), native_id=None, card=self._card(cwd, tk, conv))
            self.adapter = a
            if self.cli_version:
                a.capabilities.cli_version = self.cli_version
            self._drain()
            self._sync_session()
        except (AdapterError, ValueError, OSError) as exc:
            LOG.warning("交接后没能开新对话（下一件活再开）：%s", exc)
        if self._session_pending is not None:
            # 新对话号还不知道（dsh 第一轮才有、agy 的 init 晚到、或者没开成）：先写一条清掉旧号，
            # 免得下次上班又接回旧对话；号报上来时再补一条（同一段，不重复算）
            self._write_session(None, seg + 1, session_reason)
        self.live.set_segment(seg + 1)
        self._w("context.compacted", {"method": method, "before_pct": before, "segment": seg + 1}, conv=conv)
        self._compacted(None)
        self._auto_ts = []
        self._cmds.clear()
        self._lead_words = []

    def _compacted(self, after):
        self.pct = round(float(after), 1) if after is not None else None
        self.calibrating = True
        self._usage = None
        self._ctx_last = (None, 0.0)
        self._force_handoff = None
        self._refresh(force=True)

    def _machine_memo(self):
        self._refresh(force=True)
        turns = sorted((tr for tr in self.state.get("turns", {}).values()
                        if tr.get("em") == self.em and tr.get("kind") not in MEMORY_TURNS),
                       key=lambda tr: tr.get("started") or 0)
        order = []
        for tr in turns:
            if tr.get("task") in order:
                order.remove(tr.get("task"))
            order.append(tr.get("task"))
        tasks = []
        for tk in order[-6:]:
            t = self.state.get("tasks", {}).get(tk) or {}
            files = []
            for tr in turns:
                if tr.get("task") == tk:
                    files += [f for f in (tr.get("evidence") or {}).get("changed_files") or [] if f not in files]
            tasks.append({"id": tk, "title": t.get("title"), "status": t.get("status"), "result": t.get("result"),
                          "files": files})
        diff = ""
        desk = self._desk_path()
        if (desk / ".git").exists():
            try:
                diff = git(desk, "diff", "--stat", "HEAD").decode("utf-8", "replace")
            except (DeskError, OSError):
                diff = ""
        last = next((x.get("result") for x in reversed(tasks) if x.get("result")), "")
        return memo.machine_memo(self._label(), tasks, diff, last)

    # ------------------------------------------------------------ 送信（M4；送给谁的规则在 core/mail.py）

    _mail_due = None    # (这一轮的任务, [(信, 对话)])：开工时交代了，等这一轮真的开始了再记"已送到"
    _mail_top = 0       # 账本看到哪条了：干活中途只在来了新信时才去翻现状

    def _mail(self, start=None):
        """送信（protocol §4.5）。

        start=任务编号：这一轮开工。返回放在输入最开头的"【你有 N 封信】……"（没有信就空串）。这几封先记下来，
        等这一轮真的开始了（_pump 第一次轮询）才写 letter.delivered{next_turn}——没开始就失败的，下一轮还会再交代；
        上一轮交代过、可那一轮没做成（比如满载换线路重做）的，这一轮再说一次。
        start=None：干活中途每次轮询。先把上面那几封记成已送到；再看有没有新来的信：能当场插话的（Codex）
        就插话送达，写 letter.delivered{steer}；插不进去的（dsh / agy、整理记忆那一轮）留到下一轮开工。
        每封信只记一次（幂等键 delivered:<lt>）。"""
        if start is not None:
            self._refresh()
            got = mailbox.pending_for(self.state, self.em, start)
            ids = {l["id"] for l in got}
            again = [l for l in mailbox.redeliver(self.state, self.em, start) if l["id"] not in ids]
            self._mail_due = (start, [(l["id"], l.get("conv")) for l in got])
            self._mail_top = max_seq(self.conn)
            if not (got or again):
                return ""
            self.live.add("status", "交代 %d 封信" % (len(got) + len(again)))
            return mailbox.render(self.state, again + got, self.em, again=len(again))
        t = self.turn
        if t is None or t["memory"]:
            return ""
        due, self._mail_due = self._mail_due, None
        if due and due[0] == t["task"]:
            for lt, conv in due[1]:
                self._w("letter.delivered", {"how": "next_turn", "em": self.em, "task": t["task"]},
                        subject=lt, conv=conv, dedup="delivered:" + lt)
        if self.adapter is None or self.adapter.capabilities.steer != "live":
            return ""
        top = max_seq(self.conn)
        if top <= self._mail_top:
            return ""
        new = self.conn.execute("SELECT 1 FROM events WHERE seq>? AND seq<=? AND type='letter.sent' LIMIT 1",
                                (self._mail_top, top)).fetchone()
        self._mail_top = top
        if not new:
            return ""
        self._refresh()
        got = mailbox.pending_for(self.state, self.em, t["task"])
        if got and self.adapter.steer(mailbox.render(self.state, got, self.em, steer=True)):
            for l in got:
                self._w("letter.delivered", {"how": "steer", "em": self.em, "task": t["task"]},
                        subject=l["id"], conv=l.get("conv"), dedup="delivered:" + l["id"])
            self.live.add("status", "新来 %d 封信，当场告诉他了" % len(got))
        return ""

    # ------------------------------------------------------------ 控制

    def _poll_controls(self):
        rows = self.conn.execute("SELECT seq, conv, subject, data FROM events WHERE seq>? AND type='control.requested' "
                                 "ORDER BY seq LIMIT 200", (self._ctl_cursor,)).fetchall()
        for r in rows:
            self._ctl_cursor = r["seq"]
            d = json.loads(r["data"]) or {}
            action, sub = d.get("action"), r["subject"]
            tasks = (self.state or {}).get("tasks", {})
            is_task = sub in tasks and tasks[sub].get("owner") == self.em
            if not (sub == self.em or is_task or action == "stop_all"):
                continue
            self._control(r["seq"], r["conv"], sub, action, d, is_task)

    def _control(self, seq, conv, sub, action, d, is_task):
        target = sub or self.em

        def done(result, detail=""):
            data = {"action": action, "result": result, "request": seq}
            if detail:
                data["detail"] = detail
            if action == "steer" and detail == "当场告诉他了":
                data["delivered"] = True              # 干活中途当场说过了：没活时不再拿出来重复说（pending_chat_steers）
            self._w("control.done", data, subject=target, conv=conv, dedup="control-done:%d:%s" % (seq, self.em))

        turn = self.turn
        active = turn is not None and self.adapter is not None
        if action == "steer":
            text = str(d.get("text") or "").strip()
            rules = d.get("by_source") == "rules"     # 规则监工（管家）发的，不是负责人说的
            who = "监工提醒" if rules else "负责人插话"
            if not text:
                done("noop", "没有要说的话")
            elif active and not turn["memory"] and self.adapter.steer(("【监工提醒】" + text) if rules else text):
                self.live.add("status", "%s：%s" % (who, text))
                done("ok", "当场告诉他了")
            else:
                self.pending_steers.append((who, text))
                done("ok", "这一轮插不进去，下一轮开头告诉他")
        elif action == "interrupt":
            if active:
                done("ok", self._interrupt("负责人叫他先停一下"))
            else:
                done("noop", "他现在没在干活")
        elif action in ("stop", "stop_all"):
            if action == "stop" and is_task and not (active and turn["task"] == sub):
                done("noop", "这件活现在没在做")
                return
            if active:
                turn["paused"] = True                 # 叫停 / 总闸停下的：记"已停下"（paused），不说"被打断"
                self._interrupt("叫停")
            if action == "stop_all" or not is_task:
                self.stop_reason = "stopped"
            done("ok", "已经叫停")
        else:
            done("noop")

    def _interrupt(self, why):
        if self.turn is not None:
            self.turn["interrupting"] = True
        self.live.add("status", "打断这一轮：" + why)
        try:
            return self.adapter.interrupt() if self.adapter is not None else "kill"
        except Exception:  # noqa: BLE001
            return "kill"


def main(argv=None):
    parser = argparse.ArgumentParser(prog="xay2 station")
    sub = parser.add_subparsers(dest="cmd", required=True)
    run = sub.add_parser("run")
    run.add_argument("--em", required=True)
    run.add_argument("--once", action="store_true", help="排给他的活做完就下线（不等空闲时间）")
    args = parser.parse_args(sys.argv[1:] if argv is None else argv)
    if os.environ.get("XAY_ROLE") == "executor":
        print("执行者不能开工位", file=sys.stderr)
        return 2
    logging.basicConfig(level=logging.INFO, stream=sys.stderr, force=True,
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    return Station(args.em).run(once=args.once)


if __name__ == "__main__":
    sys.exit(main())
