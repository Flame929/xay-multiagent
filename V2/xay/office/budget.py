"""预算（arch-v10 §3.5）：每件活、每个员工都有 tokens / 分钟 / 轮数三条线。这里只做计算（只读现状；
agy 每天的调用次数另读 xay.quota 那本小账）；到线以后写什么事件由 rules.py 决定。

用量从账本里算：
- 轮数：这件活开工过的轮数（整理记忆那种轮不算这件活的，算员工的）。**按做完的轮数算百分比**——上限 10 轮，
  就是第 10 轮做完时到 100%；之后再开工的第 11 轮会被打断。
- 分钟：每一轮从开工到收工的时长相加（正在做的这一轮算到现在）。
- tokens：每一轮的用量相加。收工的轮用 `turn.ended.usage.total`；还在做的那一轮用工位中途报的
  `context.updated.turn_tokens`（M3 起：这一轮到现在累计的 token，每涨 2 万报一次，reducer 记在 turns[tn].turn_tokens），
  所以 90% / 100% 在一轮中途就能触发收尾 / 打断（最多晚 2 万 token）。两个数同口径，取大的。
- 员工的三条线按自然日（本机时区的 0 点起）算，第二天重新算。
- 上限：任务卡 `budget` 写了的那几条用任务卡的，没写的用 reactions.json 里的默认；你在"需要你"里点过几次
  "再给一些预算"，就加几次原额度的一半（boost）。
"""
import os
import time

from xay.core.reducers import MEMORY_TURNS

DIMS = ("tokens", "minutes", "turns")
# agy 报的 token 是每一步把整段对话重新算一遍再相加（一件活常到几百万，2026-10-31 实测一件小活 500 万），
# 不是真实花费；它的额度按每天调用次数算（agy_calls_today）。所以 agy 不按 token 刹车，只看分钟和轮数。
NO_TOKEN_LINE = ("agy",)
DIM_TEXT = {"tokens": "token", "minutes": "时长", "turns": "轮数"}


def day_start(now):
    lt = time.localtime(now)
    return time.mktime((lt.tm_year, lt.tm_mon, lt.tm_mday, 0, 0, 0, 0, 0, -1))


def day_key(now):
    return time.strftime("%Y%m%d", time.localtime(now))


def index_turns(state):
    """{task: [turn…]}、{em: [turn…]}，按开工时间排好。"""
    by_task, by_em = {}, {}
    for tr in sorted((state.get("turns") or {}).values(), key=lambda x: x.get("started") or 0):
        if tr.get("task"):
            by_task.setdefault(tr["task"], []).append(tr)
        if tr.get("em"):
            by_em.setdefault(tr["em"], []).append(tr)
    return by_task, by_em


def _minutes(tr, now):
    start = tr.get("started") or 0
    end = now if tr.get("status") == "working" else (tr.get("ended") or start)
    return max(0.0, float(end) - float(start)) / 60.0


def _tokens(tr):
    """这一轮用了多少 token：收工时的 usage.total 和一轮中途报的 turn_tokens（同口径）取大的——
    还在做的那一轮只有中途数；被打断 / 崩溃、没给 usage 的那一轮也不至于记成 0。"""
    u = tr.get("usage") or {}
    best = 0
    for v in (u.get("total"), tr.get("turn_tokens")):
        if isinstance(v, (int, float)) and not isinstance(v, bool) and v > best:
            best = int(v)
    return best


def _usage(turns, now):
    done = [tr for tr in turns if tr.get("status") != "working"]
    return {"tokens": sum(_tokens(tr) for tr in turns),
            "minutes": round(sum(_minutes(tr, now) for tr in turns), 2),
            "turns": len(done),
            "started": len(turns),
            "open": len(turns) - len(done)}


def task_usage(state, tk, now, by_task=None):
    if by_task is None:
        by_task, _ = index_turns(state)
    turns = [tr for tr in by_task.get(tk, []) if tr.get("kind") not in MEMORY_TURNS]
    return _usage(turns, now)


def employee_usage(state, em, now, by_em=None):
    if by_em is None:
        _, by_em = index_turns(state)
    since = day_start(now)
    return _usage([tr for tr in by_em.get(em, []) if (tr.get("started") or 0) >= since], now)


def _positive(v):
    return isinstance(v, (int, float)) and not isinstance(v, bool) and v > 0


def _task_agent(state, t):
    owner = ((state or {}).get("employees") or {}).get(t.get("owner")) or {}
    return owner.get("agent") or t.get("agent")


def task_limits(cfg, t, state=None):
    b = cfg["budget"]
    base = {k: v for k, v in (b.get("task") or {}).items() if k in DIMS}
    for k, v in (t.get("budget") or {}).items():   # 任务卡写了的优先
        if k in DIMS and _positive(v):
            base[k] = v
    factor = 1.0 + float(b.get("boost") or 0) * int(t.get("budget_boost") or 0)
    if _task_agent(state, t) in NO_TOKEN_LINE:
        base.pop("tokens", None)
    return {k: v * factor for k, v in base.items() if _positive(v)}


def employee_limits(cfg, e, now):
    b = cfg["budget"]
    since = day_start(now)
    boosts = [x for x in e.get("budget_boosts") or [] if x >= since]
    factor = 1.0 + float(b.get("boost") or 0) * len(boosts)
    skip = ("tokens",) if e.get("agent") in NO_TOKEN_LINE else ()
    return {k: v * factor for k, v in (b.get("employee") or {}).items() if k in DIMS and k not in skip and _positive(v)}


def measure(usage, limits):
    """→ (百分比, 是哪条线)：三条线里用得最多的那条。没有任何上限 → (0, None)。"""
    best, dim = 0.0, None
    for k in DIMS:
        lim = limits.get(k)
        if not lim:
            continue
        pct = 100.0 * float(usage.get(k) or 0) / float(lim)
        if dim is None or pct > best:
            best, dim = pct, k
    return round(best, 1), dim


def task_pct(cfg, state, tk, now, by_task=None):
    t = (state.get("tasks") or {}).get(tk) or {}
    return measure(task_usage(state, tk, now, by_task), task_limits(cfg, t, state))


def employee_pct(cfg, state, em, now, by_em=None):
    e = (state.get("employees") or {}).get(em) or {}
    return measure(employee_usage(state, em, now, by_em), employee_limits(cfg, e, now))


def person_budget(state, em, now, cfg=None, index=None):
    """快照 people[em].budget：{pct, dim, scope}——手上这件活和他今天的额度，取用得多的那个。"""
    if cfg is None:
        from . import reactions
        cfg = reactions.load()
    by_task, by_em = index or index_turns(state)
    e = (state.get("employees") or {}).get(em) or {}
    pct, dim, scope = 0.0, None, None
    tk = e.get("current_task") or next((q for q in e.get("queue") or []
                                        if ((state.get("tasks") or {}).get(q) or {}).get("status") == "queued"), None)
    if tk:
        pct, dim = task_pct(cfg, state, tk, now, by_task)
        scope = "task" if dim else None
    epct, edim = employee_pct(cfg, state, em, now, by_em)
    if edim and epct > pct:
        pct, dim, scope = epct, edim, "employee"
    out = {"pct": round(pct)}
    if dim and pct > 0:
        out.update(dim=dim, scope=scope)
    return out


_AGY_CACHE = {"sig": None, "val": None}


def _file_sig(path):
    try:
        st = os.stat(path)
    except OSError:
        return None
    return st.st_size, st.st_mtime_ns


def agy_calls_today(home, now):
    """今天 Gemini（agy）一共调用了多少次，和工位用同一本账（xay.quota：V2 自己记的 + 旧系统当天记的）。
    → (已用, 上限)。上限也取工位那个数（XAY2_AGY_DAILY_CAP / 提供者表，默认 0 = 不限），两边不会各说各的。
    管家每秒都问：两本账的文件没变就用上次算的数，不每次都把旧系统的登记从头读一遍。"""
    from xay import quota
    day = time.strftime("%Y-%m-%d", time.localtime(now))
    try:
        files = (quota._quota_dir(home) / "agy-calls.jsonl", quota._legacy_registry())
        sig = (str(home), day, os.environ.get("XAY2_AGY_DAILY_CAP")) + tuple(_file_sig(p) for p in files)
    except Exception:  # noqa: BLE001
        sig = None
    if sig is not None and _AGY_CACHE["sig"] == sig:
        return _AGY_CACHE["val"]
    try:
        usage = quota.agy_calls_today(home, day)
    except Exception:  # noqa: BLE001  账读不出来：当作 0，不拦活（工位开工前自己还会再查一次）
        return 0, quota.agy_daily_cap()
    val = (int(usage.get("total") or 0), int(usage.get("cap") or 0))
    _AGY_CACHE.update(sig=sig, val=val)
    return val


def describe(dim, usage, limits):
    """一句大白话：'轮数 9/10'、'时长 54/60 分钟'、'token 4.5M/5M'。"""
    if dim == "turns":
        return "轮数 %d/%d" % (usage.get("turns") or 0, round(limits.get("turns") or 0))
    if dim == "minutes":
        return "时长 %d/%d 分钟" % (round(usage.get("minutes") or 0), round(limits.get("minutes") or 0))
    if dim == "tokens":
        def short(n):
            n = float(n or 0)
            return ("%.1fM" % (n / 1e6)) if n >= 1e6 else ("%dk" % round(n / 1e3))
        return "token %s/%s" % (short(usage.get("tokens")), short(limits.get("tokens")))
    return ""
