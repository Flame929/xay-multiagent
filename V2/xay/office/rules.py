"""规则监工：管家每一拍调用的反应表（arch-v10 §3.3 ④、附录 B、§3.5 预算）。

只用固定条件，不靠 AI 评价。每次触发都写进账本（by=rules；排活类的事件 task.queued / task.assigned /
task.kept 仍由 office 写），并且每一条写入都带幂等键（dedup = "rules:…"），所以管家重启、同一拍重复跑都不会多写；
页面动态里看到的是信件 / 提醒 / 需要你 / 预算这些事件的大白话。

| 遇到什么 | 自动做 | 上限 | 还不行就 |
|---|---|---|---|
| 失败（不是额度原因） | 写信给原员工说清失败原因，把活重新排给他接着修 | 1 次 | 交负责人（乙类 failure） |
| 开工前没对齐桌子 / 员工没上线 / 找不到桌子 | 直接再排一次（不是员工的错，不写信） | 1 次 | 交负责人 |
| 额度原因失败（满载 / 额度用完 / 限流） | 换线路由工位自己先做一次（Codex 换中转站、Gemini 换号）；换过还失败就不再换 | — | 交负责人 |
| 被限流、工位没换（Gemini 只在额度用完时换号） | 等 2 分钟直接再排一次（不写信） | 1 次 | 交负责人 |
| Gemini 今天的调用次数到了、这一轮没发出去 | 直接排回去，明天再做（不写信） | 1 次 | 交负责人 |
| 一直报错、不用工具（工位写 signal error_loop） | 这件活不再自动重派 | — | 乙类 error_loop |
| 沉默超时 | Codex 当场问一句（control steer）；其他 AI 记一条提醒 | 每轮 1 次 | 超时翻倍 → 乙类 silent |
| 压缩后复述不出来（recall.checked ok=false） | 下一轮再给一次笔记、重新复述（recall.requested） | 1 次 | 写一条提醒（signal remind），员工接着干 |
| 两件活要改的文件有重叠 | 开工前提醒双方（信）和负责人（signal overlap，warn） | — | — |
| 合并回项目时冲突 | 过一会自动再合并一次 | 1 次 | 留着"需要你" |
| 同一件事累计失败 ≥3 次 | — | — | 写给你（乙类 repeated_failure，owner=user） |
| 预算 70% / 90% / 100% | 提醒 / 让他收尾（steer）/ 打断 + budget.stopped | — | 乙类 budget |
| agy 今天调用满 40 次（和工位同一本账：V2 + 旧系统当天合计） | 不再给 Gemini 员工起新活，空着的 Gemini 工位叫停，写提醒；第二天自动恢复 | — | — |
| 某种 AI 的额度线路全用完了（管家查到的真数：Codex 两条线路、DeepSeek、Gemini 所有账号） | 这种 AI 的活先排着（不派、不起工位），写一条提醒；额度恢复了自动接着派 | — | — |

规则打开的"需要你"（乙类）：你选了以后由这里接着办（再试一次 / 换个人做 / 加预算 / 打断……）；
事情自己过去了（那一轮结束了、活有了别的安排）就由这里自动关掉。
"""
import fnmatch
import json
import logging
import math
import time
from pathlib import Path

from xay import quota_live
from xay import providers
from xay.core import events as core_events
from xay.core.ids import new_id
from xay.core.reducers import KEPT_KINDS, MEMORY_TURNS

from . import budget as bud
from . import reactions

LOG = logging.getLogger("xay.office.rules")

AI_NAMES = {"codex": "Codex", "dsh": "DeepSeek", "agy": "Gemini"}
ENV_REASONS = ("开工前没能对齐桌子", "没法准备桌子", "员工没能上线", "找不到项目目录")
# 额度原因的失败（turn.ended.evidence.error_kind，M3 协议 1.12）：满载 / 额度用完 / 限流
QUOTA_KINDS = ("capacity", "quota", "rate_limit")
QUOTA_WORDS = {"capacity": "满载了", "quota": "额度用完了", "rate_limit": "被限流了（请求太频繁）"}
OPT_RETRY = {"id": "retry", "label": "再试一次"}
OPT_OTHER = {"id": "other", "label": "换个人做"}
OPT_DROP = {"id": "drop", "label": "先放着"}

DELEGATE_RULES_DEFAULTS = {
    "mismatch": ("merge", "按默认做法：把改动放进项目"),
    "failed_with_changes": ("merge", "按默认做法：把已有改动放进项目"),
    "conflict": ("rework", "按默认做法：让员工接着做以解决冲突"),
    "interrupted": ("rework", "按默认做法：让员工接着做"),
    "desk_unmerged": ("merge", "按默认做法：把未合并的改动放进项目"),
    "failure": ("retry", "按默认做法：再试一次"),
    "repeated_failure": ("retry", "按默认做法：再试一次"),
    "silent": ("wait", "按默认做法：再等等"),
    "budget": ("extend", "按默认做法：再给一些预算"),
    "amnesia": ("notes", "按默认做法：再给一次笔记重新复述"),
    "review": ("pass", "按默认做法：交给审查 AI 看（未配置时默认通过）"),
    "check_failed": ("retry", "按默认做法：再让他改一次"),
    "error_loop": ("retry", "按默认做法：换个新对话重做"),
}


def _norm(g):
    g = str(g or "").strip()
    while g.startswith("./"):
        g = g[2:]
    if g.endswith("/"):
        g += "**"
    return g


def writes_overlap(a, b):
    """两组"要改的文件"（glob 或文件名）有没有重叠：返回重叠的那几项（空列表 = 没重叠）。只用固定的字符串匹配。"""
    out = []
    for x in a or []:
        for y in b or []:
            x2, y2 = _norm(x), _norm(y)
            if not x2 or not y2:
                continue
            if x2 == y2 or fnmatch.fnmatch(x2, y2):
                item = x2                    # x 落在 y 的范围里：x 更具体，给人看 x
            elif fnmatch.fnmatch(y2, x2):
                item = y2
            else:
                continue
            if item not in out:
                out.append(item)
    return out


def task_dept(s, t, project_dept="proj"):
    """这件活属于哪个部门（选人时"同一个部门"用）：任务写了、而且部门开着 → 它；要改的文件归某个部门管 → 那个；
    都没有 → None（管家开的"项目组"不算，否则人人都同部门）。"""
    conv = t.get("conv")
    open_ = {d["id"]: d for d in (s.get("depts") or {}).values() if d.get("conv") == conv and not d.get("closed")}
    if t.get("dept") in open_ and t.get("dept") != project_dept:
        return t["dept"]
    for did, d in sorted(open_.items()):
        if did == project_dept:
            continue
        if writes_overlap(d.get("owns") or [], t.get("writes") or []):
            return did
    return None


def _label(s, em):
    return ((s.get("employees") or {}).get(em) or {}).get("label") or em or ""


def _title(t):
    return t.get("title") or t.get("id") or ""


def task_check_def(s, t):
    c = t.get("task_check")
    if not c and t.get("part"):
        parts = s.get("parts", {})
        p = parts.get(t["part"]) or next((x for x in parts.values() if x.get("key") == t["part"]), None)
        if p:
            c = p.get("check")
    if not c:
        c = {"kind": "rules"}
    return c


def _live_mtime(home, em):
    try:
        return max((p.stat().st_mtime for p in (Path(home) / "live" / em).glob("*.jsonl")), default=0.0)
    except OSError:
        return 0.0


def _classify(text):
    try:
        from xay.adapters.base import classify_error
    except Exception:  # noqa: BLE001
        return "other"
    return classify_error(text or "")


class Rules:
    def __init__(self, sched):
        self.sched = sched
        self.conn = sched.conn
        self.home = Path(sched.home)
        self._done = set()          # 这次运行里已经写过 / 撞过幂等键的 dedup
        self._resolved_cursor = 0   # 已经接着办过的 decision.resolved
        self.agy_capped = False
        self._agy_resume_day = None
        self.lines_out = {}         # 哪种 AI 的额度线路全用完了 → 大白话原因（管家查到的真数；排活、起工位前先看）
        self._quota_now = []        # 这一拍的四条线路额度（快照同款）
        self._out_told = set()      # 这一次"全用完了"已经提醒过的 AI

    # ------------------------------------------------------------ 账本

    def cfg(self):
        return reactions.load(self.home)

    def _w(self, typ, data, *, by="rules", ws=None, conv=None, subject=None, dedup=None):
        if dedup is not None and dedup in self._done:
            return None
        try:
            seq = core_events.append(self.conn, typ, by, data, ws=ws, conv=conv, subject=subject, dedup=dedup)
        except (core_events.PermissionError, ValueError) as exc:
            LOG.warning("账本没收下规则监工的 %s：%s", typ, exc)
            seq = None
        if dedup is not None:
            self._done.add(dedup)
        return seq

    def _seen(self, dedup):
        """这个幂等键以前写过没有（管家重启后也查得到）。"""
        if dedup in self._done:
            return True
        row = self.conn.execute("SELECT 1 FROM events WHERE dedup=?", (dedup,)).fetchone()
        if row:
            self._done.add(dedup)
        return bool(row)

    def _letter(self, t, em, subject, body, dedup, kind="system"):
        return self._w("letter.sent", {"from": "office", "to": em, "kind": kind, "subject": subject, "body": body},
                       ws=t.get("ws"), conv=t.get("conv"), subject=new_id("lt_"), dedup=dedup)

    def _signal(self, kind, text, *, level="remind", ws=None, conv=None, subject=None, dedup=None, **extra):
        data = {"kind": kind, "level": level, "text": text}
        data.update(extra)
        return self._w("signal.raised", data, ws=ws, conv=conv, subject=subject, dedup=dedup)

    def _decision(self, kind, title, detail, options, *, owner="lead", task=None, em=None, ws=None, conv=None,
                  dedup=None, files=None, **extra):
        data = {"kind": kind, "title": title, "detail": detail, "options": [dict(o) for o in options], "owner": owner}
        if task:
            data["task"] = task
        if em:
            data["em"] = em
        if files is not None:
            data["files"] = files[:30]
        elif task:
            files_list = []
            try:
                tasks = (core_events.load_state(self.conn).get("tasks") or {})
                t_obj = tasks.get(task) or {}
                fk = "review" if kind in ("review", "check_failed") else "changed"
                for p in (t_obj.get("writes") or []):
                    if p and p not in [x["path"] for x in files_list]:
                        files_list.append({"path": p, "kind": fk})
                if not files_list:
                    for p in (t_obj.get("files") or []):
                        if p and p not in [x["path"] for x in files_list]:
                            files_list.append({"path": p, "kind": "landed" if t_obj.get("status") in ("reported", "done") else "changed"})
            except Exception:
                pass
            if files_list:
                data["files"] = files_list[:30]
        data.update(extra)
        return self._w("decision.opened", data, ws=ws, conv=conv, subject=new_id("dc_"), dedup=dedup)

    def _control(self, action, subject, *, text=None, ws=None, conv=None, dedup=None):
        data = {"action": action, "by_source": "rules"}
        if text:
            data["text"] = text
        return self._w("control.requested", data, ws=ws, conv=conv, subject=subject, dedup=dedup)

    def _requeue(self, s, t, retry, dedup):
        em = t.get("owner")
        tasks = s.get("tasks") or {}
        pos = 1 + sum(1 for q in ((s.get("employees") or {}).get(em) or {}).get("queue") or []
                      if (tasks.get(q) or {}).get("status") == "queued" and q != t["id"])
        return self._w("task.queued", {"owner": em, "position": pos, "retry": retry}, by="office",
                       ws=t.get("ws"), conv=t.get("conv"), subject=t["id"], dedup=dedup)

    # ------------------------------------------------------------ 一拍

    def tick(self, s, now=None):
        now = time.time() if now is None else now
        cfg = self.cfg()
        n = 0
        steps = (self._budget, self._agy_cap, self._lines_out, self._failures, self._checks, self._error_loops, self._conflicts,
                 self._silence, self._recall, self._overlap, self._delegated, self._followups, self._moot)
        for step in steps:
            try:
                wrote = step(s, now, cfg)
            except Exception:  # noqa: BLE001
                LOG.exception("规则监工这一条没跑成功：%s", step.__name__)
                wrote = 0
            if wrote:
                n += wrote
                s = core_events.load_state(self.conn)
        return n

    # ------------------------------------------------------------ 失败

    def _last_turn(self, s, tk):
        turns = [tr for tr in (s.get("turns") or {}).values()
                 if tr.get("task") == tk and tr.get("kind") not in MEMORY_TURNS]
        return max(turns, key=lambda tr: tr.get("started") or 0) if turns else {}

    def _error_kind(self, s, t):
        tr = self._last_turn(s, t["id"])
        ev = tr.get("evidence") or {}
        return ev.get("error_kind") or _classify(ev.get("error") or t.get("failure_detail") or "")

    def _quota(self, s, t):
        """额度原因的失败吗？→ (种类 capacity / quota / rate_limit，不是就 None；工位补救过没有：None / "done" / "tried")。

        种类看这件活最后一轮的 evidence.error_kind（M3 起工位都写），没有就按错误文字分类。
        补救过 = 这件活做的时候（第一轮开工 → 记失败）工位已经先自己处理过一次：Codex 换了额度线路
        （employee.session{reason: line_switch}）、Gemini 换了账号（工位写的 signal quota，remind）→ "done"；
        Gemini 想换号没换成（同上，warn）→ "tried"。"今天 40 次到了"那条不算（幂等键以 agy-cap: 开头）。
        补救过（不管换成没有）还失败 → 不再自动重试、不再换，交负责人。"""
        kind = self._error_kind(s, t)
        em = t.get("owner")
        turns = [x for x in (s.get("turns") or {}).values() if x.get("task") == t["id"]]
        start = min((x.get("started") or 0 for x in turns), default=0)
        end = float(t.get("failed_ts") or time.time())
        switched = None
        if em and turns:                  # 一轮都没开工就失败的：谈不上工位补救过
            rows = self.conn.execute(
                "SELECT type, data, dedup FROM events WHERE subject=? AND by=? AND type IN ('employee.session','signal.raised') "
                "AND ts>=? AND ts<=?", (em, "station:" + em, float(start), end)).fetchall()
            for r in rows:
                d = json.loads(r["data"]) or {}
                if r["type"] == "employee.session" and d.get("reason") == "line_switch":
                    switched = "done"
                elif r["type"] == "signal.raised" and d.get("kind") == "quota" and not str(r["dedup"] or "").startswith("agy-cap:"):
                    if d.get("level") == "warn":
                        switched = switched or "tried"
                    else:
                        switched = "done"
        if kind in QUOTA_KINDS:
            return kind, switched
        return ("quota" if switched else None), switched

    def _has_open_budget_decision(self, s, tk, em=None):
        """这件活或其当前员工是否有未关闭的 budget 决策。"""
        for d in (s.get("decisions") or {}).values():
            if d.get("kind") == "budget":
                if d.get("task") == tk:
                    return True
                if em and d.get("em") == em:
                    return True
        return False

    def _turn_budget_interrupted(self, s, tk, t, em=None):
        """这件活最后一轮是不是因为预算被打断的。"""
        tr = self._last_turn(s, tk)
        tn = tr.get("id")
        if tn:
            if self._seen("rules:budget:%s:stop:%s" % (tk, tn)):
                return True
            if em and self._seen("rules:ebudget:%s:turn:%s" % (em, tn)):
                return True
        # 兜底：账本中是否有 rules 在该轮期间发给员工的预算打断
        start = float(tr.get("started") or 0)
        end = float(tr.get("ended") or t.get("failed_ts") or time.time())
        if em and start > 0:
            row = self.conn.execute(
                "SELECT 1 FROM events WHERE type='control.requested' AND subject=? AND by='rules' "
                "AND ts>=? AND ts<=? AND data LIKE '%预算%' LIMIT 1",
                (em, start - 5.0, end + 5.0),
            ).fetchone()
            if row:
                return True
        return False

    def _failures(self, s, now, cfg):
        n = 0
        halted = bool(s.get("halt"))
        fcfg = cfg["failure"]
        for tk, t in sorted((s.get("tasks") or {}).items()):
            if t.get("status") != "failed" or t.get("legacy") or not t.get("failed_seq"):
                continue
            if ((s.get("convs") or {}).get(t.get("conv")) or {}).get("ended"):
                continue
            key = "%s:%s" % (tk, t["failed_seq"])
            if t.get("error_loop"):
                continue                                    # 一直报错的活：不自动重派（_error_loops 开"需要你"）
            if now - float(t.get("failed_ts") or 0) < float(fcfg.get("retry_delay_s") or 0):
                continue
            if now - float(t.get("failed_ts") or 0) > float(cfg.get("stale_minutes") or 0) * 60:
                continue                                    # 很久以前的失败：不翻出来自动处理
            em = t.get("owner")
            # 这件活已经有没关的 budget "需要你"、或者这一轮是因为预算被打断的：不再另开 failure 的"需要你"
            if self._has_open_budget_decision(s, tk, em) or self._turn_budget_interrupted(s, tk, t, em):
                continue
            e = (s.get("employees") or {}).get(em) or {}
            fails = int(t.get("fails") or 0)
            retries = t.get("retries") or {}
            reason = t.get("failure_reason") or "没做成"
            detail = (t.get("failure_detail") or "").strip()
            why = reason + ("：" + detail if detail and detail != reason else "")
            if fails >= int(cfg.get("repeated_failure_at") or 3):
                if self._decision("repeated_failure", "「%s」已经失败 %d 次了，要怎么办？" % (_title(t), fails),
                                  "最近一次：%s。由于连续多次失败，监工不再自动重试。你可以让他再试一次、换个人做，或者先放着。" % why, [OPT_RETRY, OPT_OTHER, OPT_DROP],
                                  owner="user", task=tk, ws=t.get("ws"), conv=t.get("conv"),
                                  dedup="rules:repeated:" + key):
                    n += 1
                continue
            if halted:
                continue                                    # 总闸拉着：不自动重试，恢复后再说
            can_retry = em and not e.get("former")
            qkind, switched = self._quota(s, t)
            if qkind == "rate_limit" and not switched and can_retry \
                    and int(retries.get("env") or 0) < int(cfg["env"].get("retry") or 0):
                # 被限流、工位也没换线路 / 账号（Gemini 只在"额度用完"时换号）：不是员工的错，等一会儿直接再排一次
                if now - float(t.get("failed_ts") or 0) < float(fcfg.get("rate_limit_wait_s") or 0):
                    continue
                if self._requeue(s, t, "env", "rules:retry:" + key):
                    n += 1
                continue
            if qkind:
                agent = e.get("agent")
                words = "%s %s" % (_label(s, em), QUOTA_WORDS.get(qkind, "额度不够"))
                gone = None if switched else self._quota_gone(e, now)
                if gone:
                    # 要换去的线路 / 账号也用完了（管家查到的真数）：工位没往那儿换，直接交负责人
                    detail_text = "%s，%s，没有换过去（%s）。监工不再自动重试，你可以等额度恢复后让他再试一次、换个人做，或者先放着。" % (words, gone, why)
                elif switched == "tried":
                    detail_text = "%s，工位试着换 Gemini 账号没换成（%s）。监工不再自动重试，你可以让他再试一次、换个人做，或者先放着。" % (
                        words, why)
                elif switched:
                    detail_text = ("工位已经换过 Gemini 账号" if agent == "agy" else "工位已经换过额度线路") + \
                        "，还是没做成（%s）。监工不再自动重试，你可以让他再试一次、换个人做，或者先放着。" % why
                elif agent == "codex" and e.get("line") == "codex-relay":
                    detail_text = "%s，他已经在中转站上了，没有别的线路可换（%s）。你可以让他再试一次、换个人做，或者先放着。" % (words, why)
                else:
                    detail_text = "%s，工位没有别的线路或账号可换（%s）。你可以让他再试一次、换个人做，或者先放着。" % (words, why)
                if self._decision("failure", "「%s」因为额度原因没做成，要怎么办？" % _title(t), detail_text,
                                  [OPT_RETRY, OPT_OTHER, OPT_DROP], task=tk, ws=t.get("ws"), conv=t.get("conv"),
                                  dedup="rules:failure:" + key, cause="quota"):
                    n += 1
                continue
            if self._error_kind(s, t) == "daily_cap" and can_retry \
                    and int(retries.get("env") or 0) < int(cfg["env"].get("retry") or 0):
                # Gemini 今天的调用次数到了、这一轮没发出去：不是员工的错，不写信，排回去等明天
                if self._requeue(s, t, "env", "rules:retry:" + key):
                    n += 1
                continue
            bpct, bdim = bud.task_pct(cfg, s, tk, now)
            if t.get("budget_stopped") or (bdim and bpct >= cfg["budget"]["stop_pct"]):
                if self._decision("failure", "「%s」没做成，预算也用完了，要怎么办？" % _title(t),
                                  "%s。这件活的预算已经用完（%.0f%%），监工不再自动重试。你可以再给一些预算让他接着做，或者就到这里结束。" % (why, bpct),
                                  [OPT_RETRY, OPT_OTHER, OPT_DROP], task=tk, ws=t.get("ws"), conv=t.get("conv"),
                                  dedup="rules:failure:" + key, cause="budget"):
                    n += 1
                continue
            env = reason in ENV_REASONS
            if env and can_retry and int(retries.get("env") or 0) < int(cfg["env"].get("retry") or 0):
                if self._requeue(s, t, "env", "rules:retry:" + key):
                    n += 1
                continue
            if not env and can_retry and int(retries.get("auto") or 0) < int(fcfg.get("retry") or 0):
                body = ("这件活上次没做成，原因：%s\n请先弄清楚为什么失败，在原来的基础上修好，做完照常汇报。"
                        "（这是监工的自动重试，只重试 1 次）" % why)
                self._letter(t, em, "「%s」上次没做成，请接着修" % _title(t), body, "rules:retry-letter:" + key)
                if self._requeue(s, t, "auto", "rules:retry:" + key):
                    n += 1
                continue
            title = "「%s」又没做成（第 %d 次），要怎么办？" % (_title(t), fails) if fails > 1 else "「%s」没做成，要怎么办？" % _title(t)
            if self._decision("failure", title, "%s。自动重试的次数用完了，你可以让他再试一次、换个人做，或者先放着。" % why,
                              [OPT_RETRY, OPT_OTHER, OPT_DROP], task=tk, ws=t.get("ws"), conv=t.get("conv"),
                              dedup="rules:failure:" + key, cause="failed"):
                n += 1
        return n

    def _checks(self, s, now, cfg):
        n = 0
        tasks = s.get("tasks") or {}
        for tk, t in sorted(tasks.items()):
            if t.get("legacy") or t.get("review_of"):
                continue
            if ((s.get("convs") or {}).get(t.get("conv")) or {}).get("ended"):
                continue
            chk_def = task_check_def(s, t)
            kind = chk_def.get("kind")
            if kind not in ("ai", "lead"):
                continue

            chk = t.get("check") or {}
            cstate = chk.get("state") or "none"
            cround = int(chk.get("round") or 1)
            key = "%s:%s" % (tk, cround)

            if cstate == "waiting":
                if kind == "ai":
                    reviewer = chk_def.get("reviewer") or "codex"
                    orig_title = _title(t)
                    card_lines = [
                        "原任务卡：%s" % (t.get("card") or "（无）"),
                        "改动文件：%s" % (", ".join(t.get("writes") or []) or "（未指定）"),
                    ]
                    if chk_def.get("text"):
                        card_lines.append("审查重点：%s" % chk_def["text"])
                    card_lines.append('看完用 xay2 verdict pass 或 xay2 verdict fail "哪里不对、怎么改" 交结论')
                    card = "\n".join(card_lines)

                    data = {
                        "title": "审查「%s」" % orig_title,
                        "card": card,
                        "mode": "readonly",
                        "part": t.get("part"),
                        "review_of": tk,
                        "deps": [],
                        "writes": [],
                        "reason": "审查「%s」" % orig_title,
                    }
                    if reviewer in providers.agent_kinds():
                        data["agent"] = reviewer
                    else:
                        data["owner"] = reviewer
                    if self._w("task.added", data, by="office", ws=t.get("ws"), conv=t.get("conv"),
                               subject=new_id("tk_"), dedup="rules:review_task:" + key):
                        n += 1
                elif kind == "lead":
                    options = [{"id": "pass", "label": "通过"}, {"id": "fail", "label": "不合格"}]
                    detail = "审查重点：%s。通过则完成此任务，不合格则打回修改。" % (chk_def.get("text") or "请检查任务质量并交结论")
                    if self._decision("review", "需要你审查「%s」" % _title(t), detail, options,
                                      owner="lead", task=tk, ws=t.get("ws"), conv=t.get("conv"),
                                      dedup="rules:review_dec:" + key):
                        n += 1

            elif cstate == "failed":
                em = t.get("owner")
                if cround <= 1:
                    if em:
                        body = "这件活验收未通过，审查意见：%s\n请在原来的基础上修好，做完照常汇报。" % (chk.get("text") or "请按意见修改")
                        self._letter(t, em, "「%s」验收未通过，请接着改" % _title(t), body, "rules:check_failed_letter:" + key)
                    if self._requeue(s, t, "check", "rules:check_failed_requeue:" + key):
                        n += 1
                else:
                    opts = [
                        {"id": "retry", "label": "让他接着改"},
                        {"id": "other", "label": "换个人做"},
                        {"id": "lead", "label": "我来审查"},
                    ]
                    detail = "审查意见：%s。已经打回修改过一次，仍未通过验收。你可以让他接着改、换个人做，或者亲自审查。" % (chk.get("text") or "未说明原因")
                    if self._decision("check_failed", "「%s」审查两次未通过，要怎么办？" % _title(t), detail,
                                      opts, owner="lead", task=tk, ws=t.get("ws"), conv=t.get("conv"),
                                      dedup="rules:check_failed_dec:" + key):
                        n += 1
        return n

    def _error_loops(self, s, now, cfg):
        n = 0
        for tk, t in sorted((s.get("tasks") or {}).items()):
            loop = t.get("error_loop")
            if not loop or t.get("legacy") or t.get("status") not in ("failed", "needs_you"):
                continue        # 还在做（这一轮还没收尾）或者已经有了别的结果：先不动
            if now - float(loop.get("ts") or 0) > float(cfg.get("stale_minutes") or 0) * 60:
                continue
            kept = any(d.get("task") == tk and d.get("kind") in KEPT_KINDS for d in (s.get("decisions") or {}).values())
            who = _label(s, t.get("owner"))
            if kept:
                opts = [{"id": "ok", "label": "知道了"}]
                detail = "%s 连续报错且未使用工具，工位已经让他停下并整理了记忆。怎么处理请看另一条需要你。" % who
            else:
                opts = [{"id": "retry", "label": "换新对话重做"}, OPT_OTHER, OPT_DROP]
                detail = "%s 连续报错且未使用工具，工位已经让他停下并整理了记忆。监工不会再自动重派这件活，你可以让他换新对话重做、换人，或者先放着。" % who
            if self._decision("error_loop", "「%s」一直在报错，已经停下了，要怎么办？" % _title(t), detail, opts,
                              task=tk, ws=t.get("ws"), conv=t.get("conv"), dedup="rules:errloop:%s:%s" % (tk, loop.get("seq"))):
                n += 1
        return n

    def _conflicts(self, s, now, cfg):
        n = 0
        if s.get("halt"):
            return 0
        c = cfg["conflict"]
        for dc, d in sorted((s.get("decisions") or {}).items()):
            if d.get("kind") != "conflict" or not d.get("task"):
                continue
            t = (s.get("tasks") or {}).get(d["task"]) or {}
            if t.get("status") != "needs_you" or int(t.get("auto_merges") or 0) >= int(c.get("retry") or 0):
                continue
            if now - float(d.get("ts") or 0) < float(c.get("retry_after_s") or 0):
                continue
            if now - float(d.get("ts") or 0) > float(cfg.get("stale_minutes") or 0) * 60:
                continue
            self._signal("conflict", "「%s」合并回项目时有冲突，监工自动再试一次合并" % _title(t),
                         ws=t.get("ws"), conv=t.get("conv"), subject=d["task"], dedup="rules:merge-note:" + dc)
            if self._w("decision.resolved", {"option": "merge", "text": "规则监工：合并冲突，自动再试一次"},
                       ws=d.get("ws"), conv=d.get("conv"), subject=dc, dedup="rules:merge:" + dc):
                n += 1
        return n

    # ------------------------------------------------------------ 沉默 / 复述 / 重叠

    def _silence(self, s, now, cfg):
        n = 0
        mins = cfg["silent"].get("minutes") or {}
        for em, e in sorted((s.get("employees") or {}).items()):
            tn = e.get("turn")
            tr = (s.get("turns") or {}).get(tn) or {}
            if not tn or tr.get("status") != "working" or tr.get("kind") in MEMORY_TURNS or e.get("former"):
                continue
            limit = float(mins.get(e.get("agent")) or 0) * 60.0
            if limit <= 0:
                continue
            last = max(float(tr.get("started") or 0), float(e.get("activity_ts") or 0), _live_mtime(self.home, em))
            quiet = now - last
            t = (s.get("tasks") or {}).get(tr.get("task")) or {}
            ws, conv = e.get("ws"), t.get("conv")
            if quiet >= limit:
                m = int(quiet // 60)
                if e.get("agent") == "codex":
                    wrote = self._control("steer", em, ws=ws, conv=conv, dedup="rules:silent:%s:1" % tn,
                                          text="你已经 %d 分钟没有任何输出了。还在做吗？请用一两句话说说现在做到哪了、是不是卡住了。" % m)
                else:
                    wrote = self._signal("silent", "%s 做「%s」已经 %d 分钟没有任何输出了" % (_label(s, em), _title(t), m),
                                         ws=ws, conv=conv, subject=em, dedup="rules:silent:%s:1" % tn)
                n += 1 if wrote else 0
            if quiet >= 2 * limit:
                if self._decision("silent", "%s 做「%s」已经 %d 分钟没动静了，要怎么办？" % (_label(s, em), _title(t), int(quiet // 60)),
                                  "这一轮开工以后很久没有任何思考、输出或动作。你可以再等等、打断这一轮，或者叫停这件活。",
                                  [{"id": "wait", "label": "再等等"}, {"id": "interrupt", "label": "打断这一轮"},
                                   {"id": "stop", "label": "叫停这件活"}],
                                  task=tr.get("task"), ws=ws, conv=conv, dedup="rules:silent:%s:2" % tn,
                                  target={"em": em, "turn": tn}):
                    n += 1
        return n

    def _recall(self, s, now, cfg):
        n = 0
        for em, e in sorted((s.get("employees") or {}).items()):
            if not e.get("amnesia") or not e.get("recall_seq") or e.get("former"):
                continue
            key = "%s:%s" % (em, e["recall_seq"])
            if self._seen("rules:recall:" + key) or self._seen("rules:amnesia:" + key):
                continue        # 这一次没复述出来已经处理过了：等下一次复述的结果
            conv = self._em_conv(s, e)
            if int(e.get("recall_retries") or 0) < int(cfg["recall"].get("retry") or 0):
                if self._w("recall.requested", {"reason": "整理记忆后没复述出来，下一轮再给他看一次笔记、重新复述"},
                           ws=e.get("ws"), conv=conv, subject=em, dedup="rules:recall:" + key):
                    n += 1
            else:
                text = "%s 整理记忆后两次都没复述对，可能忘了些细节；已经把工作笔记和任务卡重新给他，接着干" % _label(s, em)
                if self._signal("remind", text, level="remind", ws=e.get("ws"), conv=conv, subject=em,
                                dedup="rules:amnesia:" + key):
                    n += 1
        return n

    def _overlap(self, s, now, cfg):
        if not cfg["overlap"].get("enabled", True):
            return 0
        n = 0
        active = [t for t in (s.get("tasks") or {}).values()
                  if t.get("status") in ("queued", "working") and not t.get("legacy") and t.get("owner") and t.get("writes")]
        active.sort(key=lambda t: t["id"])
        for i, a in enumerate(active):
            for b in active[i + 1:]:
                if a.get("ws") != b.get("ws") or a.get("owner") == b.get("owner"):
                    continue
                both = writes_overlap(a.get("writes"), b.get("writes"))
                if not both:
                    continue
                key = "rules:overlap:%s:%s" % (a["id"], b["id"])
                files = "、".join(both[:4])
                ea, eb = _label(s, a["owner"]), _label(s, b["owner"])
                if self._signal("overlap", "「%s」（%s）和「%s」（%s）要改的文件有重叠：%s，开工前请留意"
                                % (_title(a), ea, _title(b), eb, files), level="warn",
                                ws=b.get("ws"), conv=b.get("conv"), subject=b["id"], dedup=key):
                    n += 1
                for mine, other, who in ((a, b, eb), (b, a, ea)):
                    self._letter(mine, mine["owner"], "和 %s 要改的文件有重叠" % who,
                                 "你在做的「%s」和 %s 的「%s」都要改：%s。改之前先看看对方的改动，别互相覆盖；"
                                 "有冲突写信给对方或者负责人。" % (_title(mine), who, _title(other), files),
                                 key + ":" + mine["owner"], kind="notice")
        return n

    # ------------------------------------------------------------ 预算

    def _budget(self, s, now, cfg):
        n = 0
        b = cfg["budget"]
        warn, wrap, stop = float(b["warn_pct"]), float(b["wrap_pct"]), float(b["stop_pct"])
        by_task, by_em = bud.index_turns(s)
        emps = s.get("employees") or {}
        turns = s.get("turns") or {}
        for tk, t in sorted((s.get("tasks") or {}).items()):
            if t.get("legacy") or t.get("status") not in ("working", "queued"):
                continue
            usage = bud.task_usage(s, tk, now, by_task)
            limits = bud.task_limits(cfg, t, s)
            pct, dim = bud.measure(usage, limits)
            if not dim or pct < warn:
                continue
            em = t.get("owner")
            e = emps.get(em) or {}
            tn = e.get("turn")
            active = bool(tn and (turns.get(tn) or {}).get("status") == "working" and (turns.get(tn) or {}).get("task") == tk)
            key = "rules:budget:%s:%d" % (tk, int(t.get("budget_boost") or 0))
            ws, conv = t.get("ws"), t.get("conv")
            what = bud.describe(dim, usage, limits)
            warned_now = False
            if self._w("budget.warned", {"dim": dim, "pct": round(min(pct, 999))}, ws=ws, conv=conv, subject=tk,
                       dedup=key + ":warn"):
                n += 1
                warned_now = True
            if pct >= wrap and pct < stop and active:
                self._w("budget.warned", {"dim": dim, "pct": round(pct)}, ws=ws, conv=conv, subject=tk, dedup=key + ":wrap-note")
                if self._control("steer", em, ws=ws, conv=conv, dedup=key + ":wrap",
                                 text="这件活的预算快用完了（%s），请收尾：把手上这一步做完，然后汇报做到哪了、还剩什么。" % what):
                    n += 1
            elif pct >= stop and active and not warned_now and not self._seen(key + ":wrap-note"):
                # 一次就越过 90% 直接到了 100%（且先前 70% 报过）：打断前也补一条 budget.warned
                self._w("budget.warned", {"dim": dim, "pct": round(min(pct, 999))}, ws=ws, conv=conv, subject=tk,
                        dedup=key + ":wrap-note")
            if pct < stop or (t.get("status") == "working" and not active):
                continue        # 两轮之间（这一轮刚收工、结论还没出来）：先不动，免得和工位抢
            if self._w("budget.stopped", {"dim": dim, "pct": round(min(pct, 999))}, ws=ws, conv=conv, subject=tk,
                       dedup=key + ":stopped"):
                n += 1
            self._decision("budget", "「%s」的预算用完了（%s），要怎么办？" % (_title(t), what),
                           "已经让 %s 停下。可以再给这件活加一半预算让他接着做，也可以就到这里结束。" % _label(s, em),
                           [{"id": "extend", "label": "再给一些预算"}, {"id": "stop", "label": "就到这里"}],
                           task=tk, ws=ws, conv=conv, dedup=key + ":decision", budget={"scope": "task", "dim": dim})
            if active:
                if self._control("interrupt", em, ws=ws, conv=conv, dedup="rules:budget:%s:stop:%s" % (tk, tn),
                                 text="这件活的预算用完了"):
                    n += 1
            elif t.get("status") == "queued":
                if self._w("task.kept", {"reason": "paused", "detail": "预算用完了（%s），先停下，等你决定加不加预算" % what},
                           by="office", ws=ws, conv=conv, subject=tk,
                           dedup="rules:budget:%s:pause:%s" % (tk, t.get("queued_seq") or t.get("failed_seq") or 0)):
                    n += 1
        # 员工今天的三条线
        day = bud.day_key(now)
        start = bud.day_start(now)
        for em, e in sorted(emps.items()):
            if e.get("former") or str(e.get("note") or "").startswith("旧系统"):   # 桥镜像的旧系统员工（备注"旧系统 · <批次名>"）
                continue
            ws = e.get("ws")
            if e.get("budget_stopped_ts") and float(e["budget_stopped_ts"]) < start:
                # 昨天因为预算叫停的：新的一天额度重新算，自动恢复
                if self._control("resume", em, ws=ws, dedup="rules:ebudget:%s:resume:%s" % (em, day),
                                 text="新的一天，%s 的额度重新算了" % _label(s, em)):
                    n += 1
                continue
            tn = e.get("turn")
            working = bool(tn and (turns.get(tn) or {}).get("status") == "working")
            has_work = working or any(((s.get("tasks") or {}).get(q) or {}).get("status") == "queued" for q in e.get("queue") or [])
            if not has_work:
                continue
            usage = bud.employee_usage(s, em, now, by_em)
            limits = bud.employee_limits(cfg, e, now)
            pct, dim = bud.measure(usage, limits)
            if not dim or pct < warn:
                continue
            nb = len([x for x in e.get("budget_boosts") or [] if x >= start])
            key = "rules:ebudget:%s:%s:%d" % (em, day, nb)
            conv = self._em_conv(s, e)
            what = bud.describe(dim, usage, limits)
            ewarn_just_written = False
            if self._w("budget.warned", {"dim": dim, "pct": round(min(pct, 999))}, ws=ws, conv=conv, subject=em,
                       dedup=key + ":warn"):
                n += 1
                ewarn_just_written = True
            if pct >= wrap and pct < stop and working:
                if self._control("steer", em, ws=ws, conv=conv, dedup=key + ":wrap",
                                 text="你今天的预算快用完了（%s），请做完手上这一步就收尾，并汇报做到哪了。" % what):
                    n += 1
            elif pct >= stop and working and not ewarn_just_written and not self._seen(key + ":wrap-note"):
                self._w("budget.warned", {"dim": dim, "pct": round(min(pct, 999))}, ws=ws, conv=conv, subject=em,
                        dedup=key + ":wrap-note")
            if pct < stop:
                continue
            first = not self._seen(key + ":stop")
            if first:
                self._control("stop", em, ws=ws, conv=conv, dedup=key + ":stop", text="今天的预算用完了")
                if tn:
                    self._done.add("rules:ebudget:%s:turn:%s" % (em, tn))   # 叫停本身会打断这一轮
                self._w("budget.stopped", {"dim": dim, "pct": round(min(pct, 999))}, ws=ws, conv=conv, subject=em,
                        dedup=key + ":stopped")
                self._decision("budget", "%s 今天的预算用完了（%s），要怎么办？" % (_label(s, em), what),
                               "已经让他停下。可以再给他加一半额度接着干，也可以让他今天先休息（明天自动恢复）。",
                               [{"id": "extend", "label": "再给一些额度"}, {"id": "rest", "label": "今天先休息"}],
                               em=em, ws=ws, conv=conv, dedup=key + ":decision", budget={"scope": "employee", "dim": dim})
                n += 1
            elif working:
                # 叫停之后负责人又直接把活交给他：照样打断
                if self._control("interrupt", em, ws=ws, conv=conv, dedup="rules:ebudget:%s:turn:%s" % (em, tn),
                                 text="今天的预算用完了"):
                    n += 1
        return n

    def _agy_usage(self, s, cfg, now):
        """agy 今天的 (已用, 上限)：次数和上限都和工位用同一个（xay.quota，V2 + 旧系统当天合计）；
        reactions.json 的 agy_daily_calls 写了更小的数就按它提前收手。没有 Gemini 员工就不去读账。"""
        if not any(e.get("agent") == "agy" and not e.get("former") for e in (s.get("employees") or {}).values()):
            return 0, 0
        used, cap = bud.agy_calls_today(self.home, now)
        lower = int(cfg.get("agy_daily_calls") or 0)
        if lower > 0:
            cap = min(cap, lower) if cap else lower
        return used, cap

    @staticmethod
    def _em_conv(s, e):
        """员工这件事写进哪个对话（员工本身归项目、不归对话）：正在做的那件 → 最近做的那件 → 排着的第一件。
        "需要你"一定要带对话，不然页面上哪个对话都看不到它，人却一直显示"等人答复"。"""
        tasks = s.get("tasks") or {}
        tr = (s.get("turns") or {}).get(e.get("turn")) or {}
        for tk in [tr.get("task"), e.get("current_task"), e.get("last_task")] + list(e.get("queue") or []):
            conv = (tasks.get(tk) or {}).get("conv")
            if conv:
                return conv
        return None

    def update_caps(self, s, now=None):
        """只算、不写账：agy 今天的调用次数满了没有；哪种 AI 的额度线路全用完了（管家在排活、起工位之前先调用）。"""
        now = time.time() if now is None else now
        used, cap = self._agy_usage(s, self.cfg(), now)
        self.agy_capped = bool(cap) and used >= cap
        self._quota_now = quota_live.lines_now(self.conn, now)
        out = {}
        for agent in providers.agent_kinds():
            if not providers.is_available(agent):
                out[agent] = "未配置 API 登录"
                continue
            why = quota_live.agent_out(self._quota_now, agent, now)
            if why:
                out[agent] = why
        self.lines_out = out
        return self.agy_capped

    def _quota_gone(self, e, now):
        """额度原因没做成时：他能换去的那条线路（Codex：另一条线路；Gemini：别的账号）是不是也用完了 → 大白话
        （"中转站今天也用完了（明天 00:00 重置）"）；DeepSeek 没有别的线路，说它哪个窗口用完了。不知道 / 没用完 → None。"""
        quota = self._quota_now or quota_live.lines_now(self.conn, now)
        agent = e.get("agent")
        if agent == "codex":
            line = "codex-relay" if (e.get("line") or "codex-sub") == "codex-sub" else "codex-sub"
            return quota_live.gone_text(quota_live.entry(quota, line), now, also=True)
        if agent == "agy":
            return "别的 Gemini 账号也都用完了" if quota_live.agy_others_gone(quota_live.entry(quota, "agy")) else None
        if agent == "dsh":
            return quota_live.gone_text(quota_live.entry(quota, "dsh"), now)
        return None

    def _lines_out(self, s, now, cfg):
        """某种 AI 的额度线路全用完了、又有它的活在等：写一条提醒（同一次用完只写一次：恢复以后再用完才再写；
        管家重启时同一个小时里不重复）。活由排程先排着（scheduler._queue / _ensure_stations，工位开工前也看一眼）。"""
        n = 0
        tasks = s.get("tasks") or {}
        emps = s.get("employees") or {}
        self._out_told &= set(self.lines_out)
        for agent, why in sorted(self.lines_out.items()):
            if agent in self._out_told:
                continue
            held = [t for t in tasks.values() if not t.get("legacy") and (
                (t.get("status") == "ready" and (t.get("agent") == agent or (emps.get(t.get("owner")) or {}).get("agent") == agent))
                or (t.get("status") == "queued" and (emps.get(t.get("owner")) or {}).get("agent") == agent))]
            if not held:
                continue
            t = sorted(held, key=lambda x: x.get("id") or "")[0]
            name = AI_NAMES.get(agent, agent)
            text = "%s。%s 的活（%d 件）先排着，额度恢复了自动接着派" % (why, name, len(held))
            self._out_told.add(agent)
            if self._signal("quota", text, level="warn", ws=t.get("ws"), conv=t.get("conv"),
                            dedup="rules:lineout:%s:%s" % (agent, time.strftime("%Y%m%d%H", time.localtime(now)))):
                n += 1
        return n

    def _agy_cap(self, s, now, cfg):
        day = bud.day_key(now)
        n = 0
        if self._agy_resume_day != day:
            self._agy_resume_day = day
            # 以前哪天因为 agy 次数用完被叫停、还没恢复的 Gemini 员工：新的一天自动恢复
            rows = self.conn.execute("SELECT subject, dedup FROM events WHERE type='control.requested' AND by='rules' "
                                     "AND dedup LIKE 'rules:agycap:%'").fetchall()
            for r in rows:
                parts = (r["dedup"] or "").split(":")
                if len(parts) == 4 and parts[2] != day and r["subject"]:
                    e = (s.get("employees") or {}).get(r["subject"]) or {}
                    if self._control("resume", r["subject"], ws=e.get("ws"), dedup="rules:agycap-resume:%s:%s" % (parts[2], r["subject"]),
                                     text="新的一天，Gemini 的调用次数重新算了"):
                        n += 1
        calls, cap = self._agy_usage(s, cfg, now)
        if not cap:
            self.agy_capped = False
            return n
        self.agy_capped = calls >= cap
        if not self.agy_capped:
            return n
        if self._signal("quota", "Gemini 今天已经调用了 %d 次（每天最多 %d 次，新旧系统合计），剩下的 Gemini 活明天再做" % (calls, cap),
                        level="warn", dedup="rules:agycap:" + day):
            n += 1
        from xay.station import station as st
        for em, e in sorted((s.get("employees") or {}).items()):
            if e.get("agent") != "agy" or e.get("former") or e.get("turn"):
                continue        # 正在做的那一轮让他做完（之后不再给他起新活）
            if st.station_running(self.home, em):
                if self._control("stop", em, ws=e.get("ws"), dedup="rules:agycap:%s:%s" % (day, em),
                                 text="Gemini 今天的调用次数用完了"):
                    n += 1
        return n

    # ------------------------------------------------------------ 你选了以后接着办 / 自动关掉

    def _delegated(self, s, now, cfg):
        n = 0
        decisions = s.get("decisions") or {}
        for dc, d in sorted(decisions.items()):
            if d.get("delegated") != "rules":
                continue
            kind = d.get("kind")
            pair = DELEGATE_RULES_DEFAULTS.get(kind)
            if not pair:
                continue
            opt, reason = pair
            text = "交给监工：%s" % reason
            dedup = "rules:delegated:%s" % dc
            if self._w("decision.resolved", {"option": opt, "text": text},
                       ws=d.get("ws"), conv=d.get("conv"), subject=dc, dedup=dedup):
                n += 1
                try:
                    self._follow(s, dc, d, opt, text, {"by": "rules", "data": d, "ws": d.get("ws"), "conv": d.get("conv")})
                except Exception:
                    LOG.exception("处理委托决定的后续动作失败：%s", dc)
        return n

    def _opened(self, dc):
        row = self.conn.execute("SELECT by, data, ws, conv FROM events WHERE type='decision.opened' AND subject=? "
                                "ORDER BY seq LIMIT 1", (dc,)).fetchone()
        if not row:
            return None
        return {"by": row["by"], "data": json.loads(row["data"]) or {}, "ws": row["ws"], "conv": row["conv"]}

    def _followups(self, s, now, cfg):
        n = 0
        rows = self.conn.execute("SELECT seq, subject, by, data FROM events WHERE seq>? AND type='decision.resolved' "
                                 "ORDER BY seq LIMIT 500", (self._resolved_cursor,)).fetchall()
        for r in rows:
            self._resolved_cursor = r["seq"]
            if r["by"] == "rules":
                continue
            dc = r["subject"]
            op = self._opened(dc)
            if not op or op["by"] != "rules":
                continue
            d = op["data"]
            opt = (json.loads(r["data"]) or {}).get("option")
            text = (json.loads(r["data"]) or {}).get("text") or ""
            try:
                n += self._follow(s, dc, d, opt, text, op)
            except Exception:  # noqa: BLE001
                LOG.exception("没能接着办 %s", dc)
            s = core_events.load_state(self.conn)
        return n

    def _follow(self, s, dc, d, opt, text, op):
        kind = d.get("kind")
        tasks = s.get("tasks") or {}
        t = tasks.get(d.get("task")) or {}
        key = "rules:follow:%s" % dc
        if kind in ("failure", "repeated_failure", "error_loop") and t:
            if opt == "retry" and t.get("status") == "failed":
                why = t.get("failure_reason") or "没做成"
                body = "这件活上次没做成（%s），%s决定让你再试一次。%s" % (why, "你" if d.get("owner") == "user" else "负责人",
                                                                ("意见：" + text) if text else "")
                self._letter(t, t.get("owner"), "「%s」再试一次" % _title(t), body, key + ":letter")
                return 1 if self._requeue(s, t, "manual", key + ":queued") else 0
            if opt == "other" and t.get("status") in ("failed", "needs_you"):
                agent = ((s.get("employees") or {}).get(t.get("owner")) or {}).get("agent")
                pick = self.sched.pick_other(s, t, agent, exclude={t.get("owner")})
                if not pick:
                    return 0
                em, reason = pick
                self._w("task.assigned", {"owner": em, "reason": "%s决定换人：%s" % ("你" if d.get("owner") == "user" else "负责人", reason)},
                        by="office", ws=t.get("ws"), conv=t.get("conv"), subject=t["id"], dedup=key + ":assigned")
                s = core_events.load_state(self.conn)
                return 1 if self._requeue(s, s["tasks"][t["id"]], "manual", key + ":queued") else 0
            return 0
        if kind == "silent":
            target = d.get("target") or {}
            em, tn = target.get("em"), target.get("turn")
            e = (s.get("employees") or {}).get(em) or {}
            if e.get("turn") != tn:
                return 0          # 那一轮已经结束了
            if opt == "interrupt":
                return 1 if self._control("interrupt", em, ws=op["ws"], conv=op["conv"], dedup=key, text="很久没动静，你决定打断") else 0
            if opt == "stop" and d.get("task"):
                return 1 if self._control("stop", d["task"], ws=op["ws"], conv=op["conv"], dedup=key, text="很久没动静，你决定叫停") else 0
            return 0
        if kind == "amnesia" and opt == "notes" and d.get("em"):
            return 1 if self._w("recall.requested", {"reason": "你决定再给他看一次笔记、重新复述"}, ws=op["ws"], conv=op["conv"],
                                subject=d["em"], dedup=key) else 0
        if kind == "budget" and opt == "extend":
            scope = (d.get("budget") or {}).get("scope")
            if scope == "employee" and d.get("em"):
                return 1 if self._control("resume", d["em"], ws=op["ws"], conv=op["conv"], dedup=key, text="加了额度，接着干") else 0
            if t and t.get("status") == "needs_you" and t.get("kept_reason") == "paused":
                return 1 if self._requeue(s, t, "manual", key) else 0
            if t and t.get("status") == "failed":
                return 1 if self._requeue(s, t, "manual", key) else 0
        if kind == "review" and t and opt in ("pass", "fail") and (t.get("check") or {}).get("state") == "waiting":
            # 在页面上点"通过 / 不合格"或用 xay2 answer 回答（xay2 check 自己会写结论，这时任务已经不是 waiting）
            return 1 if self._w("check.result", {"task": t["id"], "ok": opt == "pass", "text": text},
                                by="office", ws=t.get("ws"), conv=t.get("conv"), subject=t["id"],
                                dedup=key + ":result") else 0
        if kind == "check_failed" and t:
            if opt == "retry":
                why = (t.get("check") or {}).get("text") or "未通过验收"
                body = "这件活验收未通过（%s），负责人决定让你再改一次。%s" % (why, ("意见：" + text) if text else "")
                self._letter(t, t.get("owner"), "「%s」再改一次" % _title(t), body, key + ":letter")
                return 1 if self._requeue(s, t, "manual", key + ":queued") else 0
            if opt == "other":
                agent = ((s.get("employees") or {}).get(t.get("owner")) or {}).get("agent")
                pick = self.sched.pick_other(s, t, agent, exclude={t.get("owner")})
                if not pick:
                    return 0
                em, reason = pick
                self._w("task.assigned", {"owner": em, "reason": "负责人决定换人：%s" % reason},
                        by="office", ws=t.get("ws"), conv=t.get("conv"), subject=t["id"], dedup=key + ":assigned")
                s = core_events.load_state(self.conn)
                return 1 if self._requeue(s, s["tasks"][t["id"]], "manual", key + ":queued") else 0
            if opt == "lead":
                options = [{"id": "pass", "label": "通过"}, {"id": "fail", "label": "不合格"}]
                return 1 if self._decision("review", "需要你审查「%s」" % _title(t), "由于两次未通过，你选择亲自审查",
                                           options, owner="lead", task=t["id"], ws=t.get("ws"), conv=t.get("conv"),
                                           dedup=key + ":lead_dec") else 0
        return 0

    def _moot(self, s, now, cfg):
        """规则打开的"需要你"，事情自己过去了就关掉（不然页面上一直挂着一条过时的）。"""
        n = 0
        start = bud.day_start(now)
        tasks = s.get("tasks") or {}
        for dc, d in sorted((s.get("decisions") or {}).items()):
            if d.get("opened_by") != "rules":
                continue
            kind = d.get("kind")
            t = tasks.get(d.get("task")) or {}
            why = None
            if kind == "silent":
                target = d.get("target") or {}
                if ((s.get("employees") or {}).get(target.get("em")) or {}).get("turn") != target.get("turn"):
                    why = ("wait", "那一轮已经结束了")
            elif kind in ("failure", "repeated_failure") and t and t.get("status") != "failed":
                why = ("drop", "这件活已经有了新的安排")
            elif kind == "error_loop" and t and t.get("status") not in ("failed", "needs_you"):
                why = ("drop", "这件活已经有了新的安排")
            elif kind == "review" and t and t.get("check", {}).get("state") != "waiting":
                why = ("drop", "已经给出审查结论")
            elif kind == "check_failed" and t and t.get("check", {}).get("state") != "failed":
                why = ("drop", "这件活已经有了新的安排")
            elif kind == "amnesia":
                if self._w("decision.resolved", {"option": "ok", "text": "复述检查不再拦人"},
                           ws=d.get("ws"), conv=d.get("conv"), subject=dc, dedup="rules:moot:" + dc):
                    n += 1
                continue
            elif kind == "budget":
                scope = (d.get("budget") or {}).get("scope")
                if scope == "employee" and float(d.get("ts") or 0) < start:
                    why = ("rest", "已经过了一天，额度重新算了")
                elif scope == "task" and t and t.get("status") in ("reported", "done", "canceled"):
                    why = ("stop", "这件活已经结束了")
            if why and self._w("decision.resolved", {"option": why[0], "text": "监工自动关掉：" + why[1]},
                               ws=d.get("ws"), conv=d.get("conv"), subject=dc, dedup="rules:moot:" + dc):
                n += 1
        return n


def expected_minutes(s, em, cfg, by_task=None, now=None):
    """他做一件活大概要多久（分钟）：最近 5 件做完的活每件的干活时长取平均；没有就用默认值。"""
    now = time.time() if now is None else now
    if by_task is None:
        by_task, _ = bud.index_turns(s)
    done = []
    for tk, turns in by_task.items():
        mine = [tr for tr in turns if tr.get("em") == em and tr.get("kind") not in MEMORY_TURNS]
        if not mine or any(tr.get("status") == "working" for tr in mine):
            continue
        status = ((s.get("tasks") or {}).get(tk) or {}).get("status")
        if status not in ("reported", "done", "failed", "needs_you"):
            continue
        done.append((max(tr.get("ended") or 0 for tr in mine), sum(bud._minutes(tr, now) for tr in mine)))
    done.sort()
    vals = [m for _, m in done[-5:]]
    default = float(cfg["pick"].get("default_task_minutes") or 10)
    return max(1.0, sum(vals) / len(vals)) if vals else default


def wait_minutes(s, em, cfg, by_task=None, now=None):
    """把一件新活排进他的队列，预计要等多久才轮到（分钟）。有改动等人决定 → 无限长。"""
    now = time.time() if now is None else now
    if any(d.get("em") == em and d.get("kind") in KEPT_KINDS for d in (s.get("decisions") or {}).values()):
        return math.inf
    e = (s.get("employees") or {}).get(em) or {}
    tasks = s.get("tasks") or {}
    avg = expected_minutes(s, em, cfg, by_task, now)
    queued = [q for q in e.get("queue") or [] if (tasks.get(q) or {}).get("status") == "queued"]
    queued += [tk for tk, t in tasks.items() if t.get("owner") == em and t.get("status") == "queued" and tk not in queued]
    wait = avg * len(queued)
    cur = e.get("current_task")
    if cur and (tasks.get(cur) or {}).get("status") == "working":
        if by_task is None:
            by_task, _ = bud.index_turns(s)
        spent = sum(bud._minutes(tr, now) for tr in by_task.get(cur, []) if tr.get("em") == em)
        wait += max(avg - spent, 2.0)
    elif e.get("turn") or e.get("state") == "compacting":
        wait += 2.0
    return wait
