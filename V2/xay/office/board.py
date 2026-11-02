"""看板左侧的对话列表（M5：7878 看板换成 V2，`GET /api/v2/convs`）。

每个对话一行：标题、项目、最近动静时间、几个人在做、几件事等你决定、是不是旧系统镜像过来的（`legacy`）。
只读现状缓存，不写账本；按最近动静从新到旧排，最多 limit 条。
"""
import posixpath
import time

from xay.core.events import _reading          # 和快照一样直接读缓存里的现状（只读、不拷贝整份）
from xay.core.reducers import legacy_status

LIMIT = 300


def conv_list(conn, *, now=None, limit=LIMIT):
    now = time.time() if now is None else now
    with _reading(conn) as s:
        workspaces = s.get("workspaces", {})
        rows = {}
        for c in s.get("convs", {}).values():
            cid = c.get("id")
            if not cid:
                continue
            ws = workspaces.get(c.get("ws")) or {}
            path = ws.get("path") or ""
            name = ws.get("name") or c.get("ws") or ""
            if path and name == c.get("ws"):            # 项目名默认就是编号：用项目文件夹的名字给人看
                name = posixpath.basename(path.rstrip("/")) or name
            rows[cid] = {"id": cid, "title": c.get("title") or "", "lead_kind": c.get("lead_kind") or "unknown",
                         "ws": {"id": c.get("ws"), "name": name, "path": path},
                         "active_ts": c.get("active_ts") or 0, "ended": bool(c.get("ended")),
                         "_owners": set(), "_working": set(), "_v2": 0, "_legacy": 0,
                         "tasks": 0, "done": 0, "needs_you": 0}
        for t in s.get("tasks", {}).values():
            r = rows.get(t.get("conv"))
            if r is None:
                continue
            r["tasks"] += 1
            r["_v2"] += 1
            if t.get("status") in ("done", "reported"):
                r["done"] += 1
            if t.get("owner"):
                r["_owners"].add(t["owner"])
                if t.get("status") == "working":
                    r["_working"].add(t["owner"])
        for run in s.get("legacy_runs", {}).values():
            r = rows.get(run.get("conv"))
            if r is None:
                continue
            st = legacy_status(run)
            r["tasks"] += 1
            r["_legacy"] += 1
            if st == "done":
                r["done"] += 1
            owner = run.get("em")
            if owner:
                r["_owners"].add(owner)
                if st == "working":
                    r["_working"].add(owner)
        for d in s.get("decisions", {}).values():
            r = rows.get(d.get("conv"))
            if r is not None and d.get("owner") in ("lead", "user"):
                r["needs_you"] += 1
        top = s.get("seq", 0)
    out = []
    for r in rows.values():
        r["people"] = len(r.pop("_owners"))
        r["working"] = len(r.pop("_working"))
        legacy, v2 = r.pop("_legacy"), r.pop("_v2")
        r["legacy"] = legacy > 0 and v2 == 0           # 只有旧系统镜像过来的活
        out.append(r)
    out.sort(key=lambda r: (r["active_ts"], r["id"]), reverse=True)
    return {"v": 1, "seq": top, "now": now, "convs": out[:max(1, int(limit))], "total": len(out)}
