"""员工在岗时间与干活时长计算模块。"""
import time
from collections import defaultdict

from xay.core.reducers import MEMORY_TURNS
from xay.office import budget as bud


def _calc_employee_duty_from_events(evs, turns, hired_at, now):
    """根据单个员工的事件和轮次数据计算 duty 字典。"""
    t0 = bud.day_start(now)
    cur_online_ts = None
    today_s = 0.0

    for ts, typ in evs:
        if typ == "employee.online":
            if cur_online_ts is not None:
                today_s += max(0.0, min(ts, now) - max(cur_online_ts, t0))
            cur_online_ts = ts
        elif typ in ("employee.offline", "employee.fired"):
            if cur_online_ts is not None:
                today_s += max(0.0, min(ts, now) - max(cur_online_ts, t0))
                cur_online_ts = None

    if cur_online_ts is not None:
        today_s += max(0.0, min(now, now) - max(cur_online_ts, t0))
        online_since = cur_online_ts
    else:
        online_since = None

    today_work_s = 0.0
    total_work_s = 0.0

    for tr in turns:
        if tr.get("kind") in MEMORY_TURNS:
            continue
        st = tr.get("started")
        if not st:
            continue
        et = tr.get("ended")
        if not et or tr.get("status") == "working":
            et = now
        total_work_s += max(0.0, et - st)
        today_work_s += max(0.0, min(et, now) - max(st, t0))

    return {
        "online_since": online_since,
        "today_s": round(today_s, 1),
        "today_work_s": round(today_work_s, 1),
        "total_work_s": round(total_work_s, 1),
        "hired_at": hired_at,
    }


def calc_duty(conn, state, em, now=None):
    """计算单个员工的 duty。"""
    if now is None:
        now = time.time()
    emp = state.get("employees", {}).get(em, {})
    hired_at = emp.get("hired_ts")

    evs = []
    if conn is not None:
        rows = conn.execute(
            "SELECT ts, type FROM events WHERE subject=? AND type IN ('employee.online', 'employee.offline', 'employee.fired', 'employee.hired') ORDER BY seq",
            (em,),
        ).fetchall()
        for r in rows:
            if r["type"] == "employee.hired" and hired_at is None:
                hired_at = r["ts"]
            elif r["type"] != "employee.hired":
                evs.append((r["ts"], r["type"]))

    turns = [tr for tr in state.get("turns", {}).values() if tr.get("em") == em]
    return _calc_employee_duty_from_events(evs, turns, hired_at, now)


def calc_all_duties(conn, state, now=None):
    """批量计算所有员工的 duty（快照使用，一次 SQL 扫描）。"""
    if now is None:
        now = time.time()

    events_by_em = defaultdict(list)
    hired_ats = {}
    if conn is not None:
        rows = conn.execute(
            "SELECT subject, ts, type FROM events WHERE type IN ('employee.online', 'employee.offline', 'employee.fired', 'employee.hired') ORDER BY seq"
        ).fetchall()
        for r in rows:
            if r["type"] == "employee.hired":
                hired_ats.setdefault(r["subject"], r["ts"])
            else:
                events_by_em[r["subject"]].append((r["ts"], r["type"]))

    turns_by_em = defaultdict(list)
    for tr in state.get("turns", {}).values():
        em = tr.get("em")
        if em:
            turns_by_em[em].append(tr)

    duties = {}
    for em, emp in state.get("employees", {}).items():
        evs = events_by_em.get(em, [])
        turns = turns_by_em.get(em, [])
        hired_at = emp.get("hired_ts") or hired_ats.get(em)
        duties[em] = _calc_employee_duty_from_events(evs, turns, hired_at, now)

    return duties
