"""管家的排程（arch-v10 §2、§3.1 ③、§3.5；protocol §4.3）。

每秒看一眼账本：
1. `planned` 的任务，前置都 `done` → `task.ready`。
2. `ready` 的任务：有负责人（`--to`）→ 直接 `task.queued`；没负责人但写了用哪种 AI（task.agent，或 card 是对象时的
   card.agent）→ **选人三步规则**（§3.1 ③，不靠打分，`_pick`）：
   ① 排除：只看同一种 AI；最近连续失败 2 次的不选；记忆条 >80% 而且压缩也压不下来的不选；今天预算用完 / 被叫停的不选。
   ② 优先老员工（满足任一条就交给他，他正忙就排进他的队列）：这件活依赖的前一件是他做的；要改的文件和他最近 3 件活
      有重叠；他是同一个部门的。几个人都满足时，依次比：哪条更强（依赖 > 文件 > 部门）、谁等得少。
      都不满足，但有空着的同种 AI 员工 → 交给空着的（铁规矩 2：员工长期在岗，能接着用就不招新人）。
   ③ 招新人：没人满足第 2 步也没人空着；或者那个老员工正忙、预计要等 10 分钟以上（按他最近几件活的时长估）、
      而且还有空名额（提供者表 max_concurrent、XAY2_MAX_STATIONS）——这时有空着的同种员工先给空着的，没有才招。
      名额满了就排进等得最少的那个人的队列。
   每次分配都写大白话理由（task.assigned.reason，页面会显示），比如"交给 Codex 1：他最近做的「X」改过同一批文件"。
   都没写的留给负责人分配。agy 今天的调用次数用完了 → Gemini 的活先不排（留在 ready），第二天再排。
   某种 AI 的额度线路全用完了（管家查到的真数，xay/quota_live.py：Codex 两条线路、DeepSeek、Gemini 所有账号）
   → 它的活同样先不排、不起工位，规则监工写一条提醒；额度恢复了自动接着派。
3. 有活（排队中的任务，或人决定了要合并 / 丢掉改动）的员工，确保有一个工位进程在跑：没有就脱离父进程起
   `xay2 station run --em <em>`（工位自己有锁，不会起两个；5 分钟内非正常退出 3 次就不再起，写一条提醒）。
   同时开的工位数受提供者表 max_concurrent（agy 2 个）和 XAY2_MAX_STATIONS（默认 8）限制。
4. 心跳看门狗：在线的员工 30 秒没有心跳 → `employee.offline{crash}`；他开着没收工的那一轮 →
   turn.ended{interrupted} + 按证据表 task.kept（+ 甲类决定）/ task.failed。桌子保留。
5. 总闸（§3.5）：`control.requested{stop_all}` 之后（账本现状 `halt`）不再起新工位；工位自己收到后打断这一轮、下班。
   还在跑的工位：等 20 秒还没停 → 给它的进程组发 SIGTERM（强制结束）→ 再等 10 秒还在 → SIGKILL（直接杀掉），
   然后由看门狗按"被打断"收尾。桌子全部保留。负责人拉的总闸在负责人又排了新活或"恢复"时解除；
   你在页面上拉的只有你点"恢复"才解除。`stop <员工>` 之后不再给他起工位，直到恢复或负责人又把活交给他。
6. 关小任务部门：小任务里没撤回的活全部 done 后，关掉这个小任务名下的部门（dept.closed）。不再自动开项目组或归部门。
7. 规则监工（rules.py：反应表 + 预算 + agy 每天的调用上限）：每拍跑一遍。

桥写进来的旧系统记录（legacy）不参与排程。
"""
import fnmatch
import json
import logging
import math
import os
import signal
import subprocess
import sys
import threading
import time
from pathlib import Path

from xay import providers
from xay.core import db as core_db
from xay.core import events as core_events
from xay.core.ids import new_id
from xay.core.reducers import KEPT_KINDS, MEMORY_TURNS
from xay.station import station as st

from . import budget as bud
from . import paths
from . import reactions
from . import rules as rules_mod

LOG = logging.getLogger("xay.office.scheduler")

INTERVAL = 1.0
HEARTBEAT_TIMEOUT = 30.0
SPAWN_GRACE = 15.0           # 没拿到进程句柄时：起了工位以后这么久内不重复起（它要先拿锁、报到）
CRASH_WINDOW = 300.0
CRASH_MAX = 3
REUSE_BELOW_PCT = 80.0
AGENTS = ("codex", "dsh", "agy")
AI_NAMES = {"codex": "Codex", "dsh": "DeepSeek", "agy": "Gemini"}
PLAN_BY_LEAD = ("task.added", "task.assigned", "task.changed")
PROJECT_DEPT = "proj"         # 管家替没分部门的员工开的"项目组"（每个对话一个，编号只在对话内唯一）


def _env_float(name, default):
    try:
        return float(os.environ.get(name) or default)
    except ValueError:
        return default


def spawn_station(em, env=None, home=None):
    """脱离父进程起一个工位（管家重启、退出都不影响它）。输出进 $XAY2_HOME/logs/station-<em>.log。"""
    home = Path(home) if home else paths.v2_home()
    logs = home / "logs"
    logs.mkdir(parents=True, exist_ok=True)
    env = dict(os.environ if env is None else env)
    root = str(paths.v2_root())
    env["PYTHONPATH"] = root + (os.pathsep + env["PYTHONPATH"] if env.get("PYTHONPATH") else "")
    env.pop("XAY_ROLE", None)
    with open(logs / ("station-%s.log" % em), "ab") as log:
        return subprocess.Popen([sys.executable, "-m", "xay.station", "run", "--em", em],
                                stdin=subprocess.DEVNULL, stdout=log, stderr=log, cwd=root, env=env,
                                start_new_session=True, close_fds=True)


def _ps(pid):
    """(父进程 pid, 命令行)；进程不在了返回 None。"""
    try:
        out = subprocess.run(["ps", "-p", str(int(pid)), "-o", "ppid=", "-o", "command="], capture_output=True,
                             text=True, timeout=5).stdout.strip()
    except (OSError, ValueError, subprocess.SubprocessError, Exception):
        return None
    if not out:
        return None
    parts = out.split(None, 1)
    try:
        return int(parts[0]), (parts[1] if len(parts) > 1 else "")
    except ValueError:
        return None


def _terminate_pid(pid, sig=signal.SIGTERM, proc=None):
    """跨平台终止进程（组）：有 killpg 用 killpg，没有（如 Windows）退回 proc.terminate() / taskkill。"""
    if pid is None:
        return False
    if hasattr(os, "killpg"):
        try:
            os.killpg(pid, sig)
            return True
        except OSError:
            try:
                os.kill(pid, sig)
                return True
            except OSError:
                return False

    hit = False
    if proc is not None and getattr(proc, "pid", None) == pid and hasattr(proc, "poll") and proc.poll() is None:
        try:
            sig_kill = getattr(signal, "SIGKILL", None)
            if sig_kill is not None and sig == sig_kill and hasattr(proc, "kill"):
                proc.kill()
            elif hasattr(proc, "terminate"):
                proc.terminate()
            hit = True
        except OSError:
            pass

    try:
        res = subprocess.run(["taskkill", "/F", "/T", "/PID", str(int(pid))],
                             capture_output=True, timeout=5)
        if res.returncode == 0:
            hit = True
    except (OSError, ValueError, subprocess.SubprocessError):
        pass

    if not hit:
        try:
            os.kill(pid, sig)
            hit = True
        except OSError:
            pass

    return hit


def legacy_employee(e):
    """桥镜像的旧系统员工（备注是"旧系统"或"旧系统 · <批次名>"）：只用来展示，不排活、不归部门、不算忙。"""
    return str((e or {}).get("note") or "").startswith("旧系统")


def _task_agent(t):
    agent = t.get("agent")
    card = t.get("card")
    if not agent and isinstance(card, dict):
        agent = card.get("agent")
    return agent if agent in AGENTS else None


class Scheduler:
    def __init__(self, conn, *, home=None, spawn=None, env=None, heartbeat_timeout=None, max_stations=None, rules=True):
        self.conn = conn
        self.home = Path(home) if home else paths.v2_home()
        self.spawn = spawn or (lambda em: spawn_station(em, env=env, home=self.home))
        self.heartbeat_timeout = heartbeat_timeout or _env_float("XAY2_HEARTBEAT_TIMEOUT", HEARTBEAT_TIMEOUT)
        self.max_stations = int(max_stations or _env_float("XAY2_MAX_STATIONS", 8))
        self.procs = {}             # em -> Popen（本管家起的）
        self.crashes = {}           # em -> [工位非正常退出的时间]
        self.gave_up = set()
        self._blind = {}            # em -> 起工位的时间（没拿到进程句柄的）
        self._stopped = {}          # em -> 叫停他的 seq
        self._cursor = None
        self._halt_seen = {}        # 总闸拉着时：em -> (第一次看到他的工位还在跑的时间, 已经做到第几步)
        self._cli_seen = {}         # 强杀工位时核对过的命令行子进程：em -> {pid: 命令行}（工位没了以后靠它认）
        self.rules = rules_mod.Rules(self) if rules else None

    def cfg(self):
        return reactions.load(self.home)

    @property
    def agy_capped(self):
        return bool(self.rules is not None and self.rules.agy_capped)

    def line_out(self, agent):
        """这种 AI 的额度线路是不是全用完了（管家查到的真数，rules.update_caps 每拍算一次）→ 大白话原因 / None。"""
        return (self.rules.lines_out.get(agent) if self.rules is not None else None) or None

    # ------------------------------------------------------------ 账本

    def _w(self, typ, data, *, ws=None, conv=None, subject=None, dedup=None):
        try:
            return core_events.append(self.conn, typ, "office", data, ws=ws, conv=conv, subject=subject, dedup=dedup)
        except (core_events.PermissionError, ValueError) as exc:
            LOG.warning("账本没收下 %s：%s", typ, exc)
            return None

    def _scan_controls(self):
        """叫停某个员工 / 恢复：只看 control.requested 和负责人改计划的事件（直接查 events 表，便宜）。
        总闸本身记在账本现状里（state["halt"]，由 reducer 算），这里不重复记。"""
        if self._cursor is None:   # 刚启动：从账本里找最近的叫停状态
            self._cursor = 0
        rows = self.conn.execute(
            "SELECT seq, type, subject, by, data FROM events WHERE seq>? AND (type='control.requested' OR "
            "(type IN ('task.added','task.assigned','task.changed') AND by LIKE 'lead:%') OR "
            "(type='decision.resolved' AND by!='rules') OR (type='task.queued' AND by='office')) ORDER BY seq LIMIT 2000",
            (self._cursor,)).fetchall()
        for r in rows:
            self._cursor = r["seq"]
            d = json.loads(r["data"]) or {}
            if r["type"] == "control.requested":
                action = d.get("action")
                if action == "stop" and isinstance(r["subject"], str) and r["subject"].startswith("em_"):
                    self._stopped[r["subject"]] = r["seq"]
                elif action == "resume":
                    if not r["subject"]:
                        self._stopped.clear()
                    else:
                        self._stopped.pop(r["subject"], None)
            elif r["type"] == "decision.resolved":
                # 叫停之后，你在"需要你"里选了"让他接着改"：等于说"接着干"，放他出来、重新起工位
                if d.get("option") == "rework":
                    em = self._decision_owner(r["subject"], r["seq"])
                    if em:
                        self._stopped.pop(em, None)
            elif r["type"] == "task.queued":
                # 你在规则开的"需要你"里选了"再让他试一次 / 换个人做 / 加预算"（管家替你重排，retry=manual）：同上
                if d.get("retry") == "manual" and d.get("owner"):
                    self._stopped.pop(d["owner"], None)
            else:   # 负责人又把活交给他：等于说"接着干"
                owner = d.get("owner")
                if owner:
                    self._stopped.pop(owner, None)
        if len(rows) == 2000:
            self._scan_controls()

    def _decision_owner(self, dc, before):
        """甲类"需要你"是谁的：decision.opened 里的 em；没写就看那件活在这之前最后交给了谁。"""
        row = self.conn.execute("SELECT data FROM events WHERE type='decision.opened' AND subject=? ORDER BY seq LIMIT 1",
                                (dc,)).fetchone()
        if not row:
            return None
        d = json.loads(row["data"]) or {}
        if d.get("em"):
            return d["em"]
        if not d.get("task"):
            return None
        for r in self.conn.execute("SELECT data FROM events WHERE subject=? AND seq<? AND type IN "
                                   "('task.added','task.assigned','task.queued') ORDER BY seq DESC LIMIT 5",
                                   (d["task"], before)).fetchall():
            owner = (json.loads(r["data"]) or {}).get("owner")
            if owner:
                return owner
        return None

    # ------------------------------------------------------------ 一拍

    def tick(self, now=None):
        now = time.time() if now is None else now
        self._scan_controls()
        s = core_events.load_state(self.conn)
        if self.rules is not None:
            self.rules.update_caps(s, now)       # agy 今天的次数满了没有：排活、起工位之前先算
        changed = self._ready(s)
        if changed:
            s = core_events.load_state(self.conn)
        if self._close_parts_depts(s):
            s = core_events.load_state(self.conn)
        if self._queue(s, now):
            s = core_events.load_state(self.conn)
        if self._place(s):
            s = core_events.load_state(self.conn)
        if self._watchdog(s, now):
            s = core_events.load_state(self.conn)
        if self.rules is not None:
            try:
                if self.rules.tick(s, now):
                    s = core_events.load_state(self.conn)
            except Exception:  # noqa: BLE001
                LOG.exception("规则监工这一拍出错了")
        self._ensure_stations(s, now)
        self._enforce_halt(s, now)
        return s

    def _ready(self, s):
        tasks = s.get("tasks", {})
        parts = s.get("parts", {})
        by_conv = {}
        for tk, t in tasks.items():
            by_conv.setdefault(t.get("conv"), {})[tk] = t
        n = 0
        for tk, t in list(tasks.items()):
            if t.get("status") != "planned" or t.get("legacy"):
                continue
            if (s.get("convs", {}).get(t.get("conv")) or {}).get("ended"):
                continue
            peers = by_conv.get(t.get("conv"), {})
            ok = True
            for dep in t.get("deps") or []:
                d = peers.get(dep) or next((x for x in peers.values() if x.get("key") == dep), None)
                if not d or d.get("status") != "done":
                    ok = False
                    break
            if ok and t.get("part"):
                part_ref = t.get("part")
                conv = t.get("conv")
                my_part = next((p for p in parts.values() if p.get("conv") == conv and (p.get("id") == part_ref or (p.get("key") and p.get("key") == part_ref))), None)
                if my_part:
                    for dep_part_ref in my_part.get("after") or []:
                        dep_part = next((p for p in parts.values() if p.get("conv") == conv and (p.get("id") == dep_part_ref or (p.get("key") and p.get("key") == dep_part_ref))), None)
                        if not dep_part:
                            ok = False
                            break
                        dep_id, dep_key = dep_part.get("id"), dep_part.get("key")
                        part_tasks = [
                            x for x in peers.values()
                            if (x.get("part") == dep_id or (dep_key and x.get("part") == dep_key))
                            and not x.get("legacy")
                        ]
                        unretracted = [x for x in part_tasks if x.get("status") != "canceled"]
                        if unretracted and not all(x.get("status") == "done" for x in unretracted):
                            ok = False
                            break
            if ok and self._w("task.ready", {}, ws=t.get("ws"), conv=t.get("conv"), subject=tk, dedup="ready:" + tk):
                n += 1
        return n

    def _busy(self, s, em):
        tasks = s.get("tasks", {})
        # ready 且已经写明交给他的（这一拍马上就排进他的队列）也算忙
        if any(t.get("owner") == em and t.get("status") in ("ready", "queued", "working") and not t.get("legacy")
               for t in tasks.values()):
            return True
        e = s["employees"][em]
        if e.get("turn") or e.get("state") in ("working", "compacting"):
            return True
        return any(d.get("em") == em and d.get("kind") in KEPT_KINDS for d in s.get("decisions", {}).values())

    # ------------------------------------------------------------ 选人（§3.1 ③）

    def _candidates(self, s, ws, agent):
        out = []
        for em, e in sorted(s.get("employees", {}).items()):
            if e.get("ws") != ws or e.get("agent") != agent or e.get("former") or legacy_employee(e):
                continue
            if (e.get("label") or "").endswith("临时工"):
                continue
            out.append(e)
        return out

    def _excluded(self, s, e, cfg, now):
        """① 排除：返回不选他的大白话原因，选得了返回 None。"""
        p = cfg["pick"]
        streak = int(e.get("fail_streak") or 0)
        if streak >= int(p.get("fail_streak") or 2):
            return "最近连续失败了 %d 次" % streak
        mem = float(p.get("memory_pct") or 80)
        pct = float((e.get("context") or {}).get("pct") or 0)
        after = e.get("compact_after")
        if pct > mem and isinstance(after, (int, float)) and after > mem:
            return "记忆用了 %.0f%%，压缩也压不下来" % pct
        epct, edim = bud.employee_pct(cfg, s, e["key"], now)
        if edim and epct >= float(cfg["budget"]["stop_pct"]):
            return "今天的预算用完了"
        if e["key"] in self._stopped:
            return "被叫停了"
        return None

    def _recent_tasks(self, s, em, n, exclude):
        turns = sorted((tr for tr in s.get("turns", {}).values()
                        if tr.get("em") == em and tr.get("kind") not in MEMORY_TURNS and tr.get("task")
                        and tr.get("task") != exclude),
                       key=lambda tr: tr.get("started") or 0, reverse=True)
        out = []
        for tr in turns:
            if tr["task"] not in out:
                out.append(tr["task"])
            if len(out) >= n:
                break
        return out

    def _affinity(self, s, t, e, cfg):
        """② 老员工接着干：返回 (强弱 1~3, 大白话理由)；都不满足返回 None。"""
        tasks = s.get("tasks", {})
        em = e["key"]
        conv = t.get("conv")
        peers = {tk: x for tk, x in tasks.items() if x.get("conv") == conv}
        for dep in t.get("deps") or []:
            d = peers.get(dep) or next((x for x in peers.values() if x.get("key") == dep), None)
            if d and d.get("owner") == em:
                return 1, "这件活接着他做的「%s」" % (d.get("title") or d.get("id"))
        mine = t.get("writes") or []
        if mine:
            for tk in self._recent_tasks(s, em, int(cfg["pick"].get("recent_tasks") or 3), t.get("id")):
                x = tasks.get(tk) or {}
                files = list(x.get("writes") or [])
                for tr in s.get("turns", {}).values():
                    if tr.get("task") == tk and tr.get("em") == em:
                        files += [f for f in (tr.get("evidence") or {}).get("changed_files") or [] if f not in files]
                both = rules_mod.writes_overlap(mine, files)
                if both:
                    return 2, "他最近做的「%s」改过同一批文件（%s）" % (x.get("title") or tk, "、".join(both[:3]))
        dept = rules_mod.task_dept(s, t, PROJECT_DEPT)
        if dept and (e.get("depts") or {}).get(conv) == dept:
            d = (s.get("depts") or {}).get("%s|%s" % (conv, dept)) or {}
            return 3, "他是%s的" % (d.get("name") or dept)
        return None

    def _slots_free(self, s, agent):
        """③ 还有没有空名额：正在忙的员工数没到 XAY2_MAX_STATIONS，同种 AI 没到提供者表的 max_concurrent。"""
        emps = s.get("employees", {})
        busy = [em for em, e in emps.items() if not e.get("former") and not legacy_employee(e) and self._busy(s, em)]
        cap = providers.get(agent).max_concurrent if agent in AGENTS else None
        same = sum(1 for em in busy if emps[em].get("agent") == agent)
        return len(busy) < self.max_stations and (cap is None or same < cap)

    @staticmethod
    def _order(e):
        label = e.get("label") or ""
        try:
            return (0, int(label.split()[-1]))
        except (ValueError, IndexError):
            return (1, label)

    def _pick(self, s, t, agent, now=None, exclude=()):
        """三步规则 → ("assign", em, 理由) | ("hire", None, 理由的前半句)。"""
        now = time.time() if now is None else now
        cfg = self.cfg()
        by_task, _ = bud.index_turns(s)
        mem = float(cfg["pick"].get("memory_pct") or 80)
        ok, skipped = [], []
        for e in self._candidates(s, t.get("ws"), agent):
            if e["key"] in exclude:
                continue
            why = self._excluded(s, e, cfg, now)
            if why:
                skipped.append((e, why))
            else:
                ok.append(e)
        waits = {e["key"]: rules_mod.wait_minutes(s, e["key"], cfg, by_task, now) for e in ok}

        def wait_text(e):
            w = waits[e["key"]]
            return "还有改动等你决定" if math.isinf(w) else "预计还要等 %d 分钟" % round(w)

        idle = sorted((e for e in ok if not self._busy(s, e["key"])),
                      key=lambda e: (float((e.get("context") or {}).get("pct") or 0) >= mem, self._order(e)))
        matches = []
        for e in ok:
            aff = self._affinity(s, t, e, cfg)
            if aff:
                matches.append((aff[0], waits[e["key"]], self._order(e), e, aff[1]))
        matches.sort(key=lambda m: m[:3])
        long_wait = float(cfg["pick"].get("wait_minutes") or 10)
        name = AI_NAMES.get(agent, agent)
        if matches:
            _, wait, _, e, why = matches[0]
            label = e.get("label")
            if not self._busy(s, e["key"]):
                return "assign", e["key"], "交给 %s：%s" % (label, why)
            if wait >= long_wait and self._slots_free(s, agent):
                busy = "%s 正忙（%s）" % (label, wait_text(e))
                if idle:
                    return "assign", idle[0]["key"], "%s，交给空着的 %s" % (busy, idle[0].get("label"))
                return "hire", None, busy
            if math.isinf(wait):
                other = min((x for x in ok if not math.isinf(waits[x["key"]])),
                            key=lambda x: (waits[x["key"]], self._order(x)), default=None)
                if other is not None:
                    return "assign", other["key"], "%s 还有改动等你决定，名额也满了，排进 %s 的队列" % (label, other.get("label"))
            return "assign", e["key"], "交给 %s（他正忙，排进他的队列，%s）：%s" % (label, wait_text(e), why)
        if idle:
            e = idle[0]
            return "assign", e["key"], "交给空闲的 %s（记忆用了 %.0f%%）" % (
                e.get("label"), float((e.get("context") or {}).get("pct") or 0))
        if ok:
            e = min(ok, key=lambda x: (waits[x["key"]], self._order(x)))
            if not self._slots_free(s, agent) and not math.isinf(waits[e["key"]]):
                return "assign", e["key"], "名额满了，排进等得最少的 %s 的队列（%s）" % (e.get("label"), wait_text(e))
            return "hire", None, "%s 员工都在忙（%s %s）" % (name, e.get("label"), wait_text(e))
        if skipped:
            return "hire", None, "%s 不合适（%s）" % (skipped[0][0].get("label"), skipped[0][1])
        return "hire", None, "没有接得上的 %s 老员工" % name

    def pick_other(self, s, t, agent, exclude=()):
        """在"需要你"里选了"换个人做"：按三步规则另找一个（必要时招新人）。→ (em, 理由) | None"""
        agent = agent or _task_agent(t) or "codex"
        how, em, why = self._pick(s, t, agent, exclude=set(exclude or ()))
        if how == "assign":
            return em, why
        em = new_id("em_")
        if not self._w("employee.hired", {"agent": agent}, ws=t.get("ws"), conv=t.get("conv"), subject=em):
            return None
        label = core_events.load_state(self.conn)["employees"][em].get("label")
        return em, "%s，招了 %s 来做" % (why, label)

    def _queue(self, s, now=None):
        n = 0
        tasks = s.get("tasks", {})
        lengths = {em: len([q for q in e.get("queue", []) if (tasks.get(q) or {}).get("status") == "queued"])
                   for em, e in s.get("employees", {}).items()}
        # 停下的活恢复后接着做：working 状态、最后一轮已结束且 evidence.paused 为真、负责人员工没有工位在跑
        turns_by_task = {}
        for tr in s.get("turns", {}).values():
            tk_id = tr.get("task")
            if tk_id:
                turns_by_task.setdefault(tk_id, []).append(tr)
        for tk, t in sorted(tasks.items(), key=lambda kv: kv[0]):
            if t.get("status") != "working" or t.get("legacy"):
                continue
            owner = t.get("owner")
            if not owner or self._running(owner):
                continue
            task_turns = turns_by_task.get(tk, [])
            if not task_turns:
                continue
            task_turns.sort(key=lambda tr: tr.get("started") or 0)
            last_turn = task_turns[-1]
            if not last_turn.get("ended"):
                continue
            if not (last_turn.get("evidence") or {}).get("paused"):
                continue
            lengths[owner] = lengths.get(owner, 0) + 1
            tn = last_turn.get("id") or "last"
            if self._w("task.queued", {"owner": owner, "position": lengths[owner], "retry": "paused"},
                       ws=t.get("ws"), conv=t.get("conv"), subject=tk, dedup="paused-queued:%s:%s" % (tk, tn)):
                n += 1
                s = core_events.load_state(self.conn)
                tasks = s.get("tasks", {})
        for tk, t in sorted(tasks.items(), key=lambda kv: kv[0]):
            if t.get("status") != "ready" or t.get("legacy"):
                continue
            ws, conv, owner = t.get("ws"), t.get("conv"), t.get("owner")
            if not owner:
                agent = _task_agent(t)
                if not agent:
                    continue        # 没说交给谁、也没说用哪种 AI：等负责人分配
                if agent == "agy" and self.agy_capped:
                    continue        # Gemini 今天的调用次数用完了：明天再排
                if self.line_out(agent):
                    continue        # 这种 AI 的额度线路全用完了：先排着，恢复了再派（规则监工写提醒）
                how, owner, reason = self._pick(s, t, agent, now)
                if how == "hire":
                    owner = new_id("em_")
                    if not self._w("employee.hired", {"agent": agent}, ws=ws, conv=conv, subject=owner):
                        continue
                    s = core_events.load_state(self.conn)
                    tasks = s.get("tasks", {})
                    reason = "%s，招了 %s 来做" % (reason, s["employees"][owner].get("label"))
                if not self._w("task.assigned", {"owner": owner, "reason": reason}, ws=ws, conv=conv, subject=tk):
                    continue
                s = core_events.load_state(self.conn)
                tasks = s.get("tasks", {})
            if owner not in s.get("employees", {}) or s["employees"][owner].get("former"):
                continue
            if s["employees"][owner].get("agent") == "agy" and self.agy_capped:
                continue
            if self.line_out(s["employees"][owner].get("agent")):
                continue
            lengths[owner] = lengths.get(owner, 0) + 1
            if self._w("task.queued", {"owner": owner, "position": lengths[owner]}, ws=ws, conv=conv, subject=tk):
                n += 1
                s = core_events.load_state(self.conn)
                tasks = s.get("tasks", {})
        return n

    def _close_parts_depts(self, s):
        """一个小任务里没撤回的活全部 done 之后，这个小任务名下（部门的 part 等于它）还开着的部门由管家写 dept.closed。"""
        depts = s.get("depts", {})
        parts = s.get("parts", {})
        tasks = s.get("tasks", {})
        n = 0
        for dk, d in sorted(depts.items()):
            if d.get("closed") or not d.get("part"):
                continue
            conv = d.get("conv")
            part_ref = d.get("part")
            p = next((x for x in parts.values() if x.get("conv") == conv and (x.get("id") == part_ref or (x.get("key") and x.get("key") == part_ref))), None)
            if not p:
                continue
            pid, pkey = p.get("id"), p.get("key")
            part_tasks = [
                t for t in tasks.values()
                if t.get("conv") == conv and not t.get("legacy") and (t.get("part") == pid or (pkey and t.get("part") == pkey))
            ]
            if not part_tasks:
                continue
            unretracted = [t for t in part_tasks if t.get("status") != "canceled"]
            if all(t.get("status") == "done" for t in unretracted):
                if self._w("dept.closed", {"reason": "小任务做完了"}, ws=d.get("ws"), conv=conv, subject=d["id"],
                           dedup="office:partdone:" + d["id"]):
                    n += 1
        return n

    def _place(self, s):
        """不再自动归部门：不再开"项目组"、不再给员工写 employee.joined 硬塞进部门。"""
        return 0

    # ------------------------------------------------------------ 看门狗

    def _running(self, em):
        proc = self.procs.get(em)
        if proc is not None:
            if proc.poll() is None:
                return True
            self.procs.pop(em, None)
            if proc.returncode != 0:     # 不是正常下班（空闲 / 叫停都是 0）：记一次崩溃
                self.crashes.setdefault(em, []).append(time.time())
        return st.station_running(self.home, em)

    def _kill_ours(self, em, s=None):
        proc = self.procs.pop(em, None)
        if proc is not None and proc.poll() is None:
            clis = self._cli_targets(s or {}, em, proc.pid)      # 先认清它的命令行子进程（要靠"父进程是工位"认）
            _terminate_pid(proc.pid, signal.SIGTERM, proc=proc)
            for pid in clis:
                _terminate_pid(pid, signal.SIGTERM)

    def _watchdog(self, s, now):
        n = 0
        for em, e in s.get("employees", {}).items():
            if e.get("former"):
                continue
            tn = e.get("turn")
            open_turn = tn and (s.get("turns", {}).get(tn) or {}).get("status") == "working"
            if e.get("online"):
                beat = max(e.get("last_heartbeat") or 0, e.get("last_ts") or 0)   # 上线那一刻还没有心跳
                if now - beat <= self.heartbeat_timeout:
                    continue
                LOG.warning("%s 的工位 %.0f 秒没有心跳，标成离线（崩溃）", e.get("label"), now - beat)
                self._kill_ours(em, s)
                if open_turn:
                    self._close_turn(s, em, tn)
                self._w("employee.offline", {"reason": "crash"}, ws=e.get("ws"), subject=em)
                n += 1
            elif open_turn and not self._running(em):
                self._close_turn(s, em, tn)
                n += 1
        return n

    def _close_turn(self, s, em, tn):
        try:
            st.close_dead_turn(self.conn, s, em, tn, "office", self.home,
                               detail="工位没了心跳（可能崩溃或被关掉），这一轮没做完")
        except Exception:  # noqa: BLE001
            LOG.exception("没能收尾 %s 的这一轮", em)

    # ------------------------------------------------------------ 工位

    def _steer_due(self, s):
        """发给某员工、还没处理的插话（protocol §4.3 聊天轮；和工位用同一个判断，免得已经当场说过的又把人叫起来）。"""
        return [em for em in st.pending_chat_steers(self.conn) if not (s.get("employees", {}).get(em) or {}).get("former")]

    def _ensure_stations(self, s, now):
        need, queued, desk = [], set(), set()
        for tk, t in sorted(s.get("tasks", {}).items()):
            em = t.get("owner")
            if not em or t.get("legacy"):
                continue
            if t.get("status") == "queued" or t.get("desk_action"):
                if em not in need:
                    need.append(em)
                if t.get("status") == "queued":
                    queued.add(em)
                if t.get("desk_action"):
                    desk.add(em)
        for em in self._steer_due(s):
            if em not in need:
                need.append(em)
        running = {em for em in s.get("employees", {}) if self._running(em)}
        for em in need:
            e = s.get("employees", {}).get(em)
            if not e or e.get("former") or em in running:
                continue
            if s.get("halt"):
                continue
            if em in self._stopped and (em in queued or em not in desk):
                continue      # 叫停了：不接新活也不回插话。手上只剩你选的"合并 / 丢掉改动"那一步（没有排着的活）的，照样起工位做完这一步（做完就下班）
            if e.get("agent") == "agy" and self.agy_capped:
                continue
            if self.line_out(e.get("agent")) and em not in desk:
                continue      # 额度线路全用完了：不起工位干新活（只剩"合并 / 丢掉改动"那一步的照样起）
            crashes = [x for x in self.crashes.get(em, []) if now - x < CRASH_WINDOW]
            self.crashes[em] = crashes
            # 起工位的函数没给进程句柄时（测试 / 别的起法），看不出它活没活着：起完等一会再说，免得连起好几个
            if em in self._blind and now - self._blind[em] < SPAWN_GRACE:
                continue
            if len(crashes) >= CRASH_MAX:
                if em not in self.gave_up:
                    self.gave_up.add(em)
                    self._w("signal.raised", {"kind": "offline", "level": "warn",
                                              "text": "%s 的工位反复起不来，先停下，等你看看（日志：logs/station-%s.log）" % (e.get("label"), em)},
                            ws=e.get("ws"), subject=em)
                continue
            agent = e.get("agent")
            cap = providers.get(agent).max_concurrent if agent in AGENTS else None
            same = sum(1 for x in running if (s["employees"].get(x) or {}).get("agent") == agent)
            if (cap is not None and same >= cap) or len(running) >= self.max_stations:
                continue
            try:
                proc = self.spawn(em)
            except OSError as exc:
                LOG.warning("起不了 %s 的工位：%s", e.get("label"), exc)
                continue
            if proc is not None:
                self.procs[em] = proc
                self._blind.pop(em, None)
            else:
                self._blind[em] = now
            self.gave_up.discard(em)
            running.add(em)
            LOG.info("给 %s 起了工位", e.get("label"))


    # ------------------------------------------------------------ 总闸：请它停 → 强制结束 → 杀掉

    def _station_pid(self, em):
        proc = self.procs.get(em)
        if proc is not None and proc.poll() is None:
            return proc.pid
        try:
            pid = int(st.pid_path(self.home, em).read_text(encoding="utf-8").split()[0])
        except (OSError, ValueError, IndexError):
            return None
        try:   # 核对这个进程确实是这个员工的工位（pid 可能被别的程序复用）
            out = subprocess.run(["ps", "-p", str(pid), "-o", "command="], capture_output=True, text=True,
                                 timeout=5).stdout
        except (OSError, subprocess.SubprocessError):
            return None
        return pid if ("xay.station" in out and em in out) else None

    def _cli_targets(self, s, em, station_pid):
        """这个员工的命令行子进程组（心跳里报的 cli_pids，协议 1.12）里，确实可以连带杀掉的那几个。

        只认：还活着、自己就是进程组组长、而且父进程就是这个员工的工位（工位已经没了、子进程被系统收养的，
        只认上一步核对过、命令行一字没变的）。pid 可能被别的程序复用，所以宁可漏杀也不误杀。"""
        seen = self._cli_seen.setdefault(em, {})
        e = (s.get("employees") or {}).get(em) or {}
        mine = {os.getpid()}
        try:
            mine.add(os.getpgrp())
        except OSError:
            pass
        out = []
        for pid in sorted({p for p in (e.get("cli_pids") or []) if isinstance(p, int)} | set(seen)):
            if pid <= 1 or pid in mine or pid == station_pid:
                continue
            try:
                leader = os.getpgid(pid) == pid
            except OSError:
                leader = False
            info = _ps(pid) if leader else None
            if info is None:
                seen.pop(pid, None)
                continue
            ppid, cmd = info
            if station_pid is not None and ppid == station_pid:
                seen[pid] = cmd
                out.append(pid)
            elif seen.get(pid) == cmd:
                out.append(pid)
            else:
                seen.pop(pid, None)
        return out

    def _cli_alive(self, em):
        """总闸已经让工位停了，但上一步核对过的命令行子进程还在（它们不在工位的进程组里）。"""
        return self._cli_targets({}, em, None) if self._cli_seen.get(em) else []

    def _hit(self, em, sig, s=None):
        """给工位的进程组发信号，连同它的命令行子进程组（各自是组长，不在工位的组里）。"""
        pid = self._station_pid(em)
        clis = self._cli_targets(s or {}, em, pid)       # 先认清楚（要看父进程是不是工位），再动手
        hit = False
        proc = self.procs.get(em)
        if pid is not None:
            if _terminate_pid(pid, sig, proc=proc):
                hit = True
        for cpid in clis:
            if _terminate_pid(cpid, sig):
                hit = True
        return hit

    def _enforce_halt(self, s, now):
        halt = s.get("halt")
        if not halt:
            self._halt_seen.clear()
            self._cli_seen.clear()
            return
        c = self.cfg()["stop"]
        term, kill = float(c.get("term_after_s") or 0), float(c.get("kill_after_s") or 0)
        for em, e in sorted(s.get("employees", {}).items()):
            if not self._running(em) and not self._cli_alive(em):
                self._halt_seen.pop(em, None)
                self._cli_seen.pop(em, None)
                continue
            first, stage = self._halt_seen.get(em, (now, 0))
            if stage == 0 and now - first >= term and self._hit(em, signal.SIGTERM, s):
                stage = 1
                LOG.warning("总闸：%s 的工位 %d 秒还没停下，强制结束", e.get("label"), term)
                self._w("control.done", {"action": "stop_all", "result": "ok",
                                         "detail": "工位 %d 秒还没停下，已强制结束" % term},
                        ws=e.get("ws"), subject=em, dedup="halt:%s:%s:term" % (halt.get("seq"), em))
            elif stage == 1 and now - first >= term + kill and self._hit(em, signal.SIGKILL, s):
                stage = 2
                LOG.warning("总闸：%s 的工位强制结束后还在，直接杀掉", e.get("label"))
                self._w("control.done", {"action": "stop_all", "result": "ok",
                                         "detail": "强制结束后 %d 秒还在，已直接杀掉" % kill},
                        ws=e.get("ws"), subject=em, dedup="halt:%s:%s:kill" % (halt.get("seq"), em))
            self._halt_seen[em] = (first, stage)


class SchedulerThread(threading.Thread):
    """守护线程：每 INTERVAL 秒跑一次 Scheduler.tick()；出错只记日志，下一拍再来。"""

    def __init__(self, interval=INTERVAL, db_path=None, home=None):
        super().__init__(name="xay-scheduler", daemon=True)
        self.interval, self.db_path, self.home = interval, db_path, home
        self.stop_event = threading.Event()
        self.scheduler = None

    def run(self):
        conn, failures = None, 0
        while not self.stop_event.is_set():
            try:
                if conn is None:
                    conn = core_db.open_db(self.db_path)
                    self.scheduler = Scheduler(conn, home=self.home)
                self.scheduler.tick()
                failures = 0
            except Exception as exc:  # noqa: BLE001
                failures += 1
                if failures == 1 or failures % 30 == 0:
                    LOG.warning("排程这一拍没跑成功（连续第 %d 次）：%s", failures, exc)
                if conn is not None:
                    try:
                        conn.close()
                    except Exception:  # noqa: BLE001
                        pass
                    conn = None
            self.stop_event.wait(self.interval if not failures else min(30.0, self.interval * (failures + 1)))
        if conn is not None:
            try:
                conn.close()
            except Exception:  # noqa: BLE001
                pass

    def stop(self, timeout=5.0):
        self.stop_event.set()
        if self.is_alive() and self is not threading.current_thread():
            self.join(timeout)
