"""小任务排程测试（V2/xay/office/scheduler.py）：
1. p2 after p1 → p2 的活在 p1 做完前不变 ready、做完后变 ready
2. after 用 key 也认
3. p1 做完后它的部门被关（dept.closed，reason="小任务做完了"）
4. 员工不再被塞进"项目组"
"""
import os
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from xay.core import append, load_state, open_db, since
from xay.core.ids import new_id, ws_for
from xay.office.scheduler import Scheduler

from tests.test_station import CLEAR_ENV, GIT_ENV, make_repo

for _k in CLEAR_ENV:
    os.environ.pop(_k, None)


class PartsSchedulerTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="xay-part-sched-test-"))
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.home = self.tmp / "v2"
        patcher = mock.patch.dict(os.environ, dict(GIT_ENV, XAY2_HOME=str(self.home), XAY_HOME=str(self.tmp / "oldxay")))
        patcher.start()
        self.addCleanup(patcher.stop)
        for k in CLEAR_ENV:
            os.environ.pop(k, None)
        self.repo = make_repo(self.tmp / "proj")
        self.conn = open_db()
        self.addCleanup(self.conn.close)
        self.ws, self.conv = ws_for(self.repo), "cv_part001"
        self.lead = "lead:" + self.conv
        append(self.conn, "conv.begun", self.lead, {"title": "小任务排程测试", "lead_kind": "claude", "aliases": [], "cwd": str(self.repo)},
               ws=self.ws, conv=self.conv, subject=self.conv)
        self.spawned = []

    def hire(self, agent="codex"):
        em = new_id("em_")
        append(self.conn, "employee.hired", self.lead, {"agent": agent}, ws=self.ws, conv=self.conv, subject=em)
        return em

    def add_part(self, title, after=(), key=None, pt_id=None):
        pt = pt_id or new_id("pt_")
        data = {"title": title, "after": list(after)}
        if key:
            data["key"] = key
        append(self.conn, "part.added", self.lead, data, ws=self.ws, conv=self.conv, subject=pt)
        return pt

    def add_task(self, title, part=None, owner=None, deps=(), agent=None, key=None):
        tk = new_id("tk_")
        data = {"title": title, "deps": list(deps), "writes": [], "mode": "write", "reason": "测试", "card": title}
        if part:
            data["part"] = part
        if owner:
            data["owner"] = owner
        if agent:
            data["agent"] = agent
        if key:
            data["key"] = key
        append(self.conn, "task.added", self.lead, data, ws=self.ws, conv=self.conv, subject=tk)
        return tk

    def finish_task(self, tk):
        append(self.conn, "task.landed", "office", {"files": [], "add": 0, "del": 0}, ws=self.ws, conv=self.conv, subject=tk)
        append(self.conn, "task.accepted", self.lead, {}, ws=self.ws, conv=self.conv, subject=tk)

    def sched(self, **kw):
        kw.setdefault("spawn", lambda em: self.spawned.append(em))
        return Scheduler(self.conn, home=self.home, **kw)

    def events(self, typ):
        return [e for e in since(self.conn, 0, limit=100000) if e["type"] == typ]

    def status(self, tk):
        return load_state(self.conn)["tasks"][tk]["status"]

    def test_p2_after_p1_by_id_blocks_and_readies(self):
        """p2 after p1（编号）→ p2 的活在 p1 做完前不变 ready、做完后变 ready。"""
        p1 = self.add_part("阶段一")
        p2 = self.add_part("阶段二", after=[p1])
        em = self.hire()
        t1 = self.add_task("活一", part=p1, owner=em)
        t2 = self.add_task("活二", part=p2, owner=em)
        s = self.sched()
        s.tick()
        self.assertEqual(self.status(t1), "queued")
        self.assertEqual(self.status(t2), "planned")

        # p1 的活做完后，p2 的活变 ready → queued
        self.finish_task(t1)
        s.tick()
        self.assertEqual(self.status(t2), "queued")

    def test_after_recognized_by_key(self):
        """after 里写 key 也认：小任务编号和 key 两种都能对上。"""
        p1 = self.add_part("阶段一", key="p1_key")
        p2 = self.add_part("阶段二", after=["p1_key"], key="p2_key")
        em = self.hire()
        t1 = self.add_task("活一", part="p1_key", owner=em)
        t2 = self.add_task("活二", part="p2_key", owner=em)
        s = self.sched()
        s.tick()
        self.assertEqual(self.status(t1), "queued")
        self.assertEqual(self.status(t2), "planned")

        self.finish_task(t1)
        s.tick()
        self.assertEqual(self.status(t2), "queued")

    def test_empty_part_counts_as_done(self):
        """没有活的小任务算做完：p2 after p1，但 p1 没有活，p2 的活直接开工。"""
        p1 = self.add_part("空阶段")
        p2 = self.add_part("阶段二", after=[p1])
        em = self.hire()
        t2 = self.add_task("活二", part=p2, owner=em)
        s = self.sched()
        s.tick()
        self.assertEqual(self.status(t2), "queued")

    def test_part_department_closed_after_part_done(self):
        """一个小任务做完后，挂在该小任务下的部门被关掉（dept.closed，幂等）。"""
        p1 = self.add_part("阶段一", key="part_fe")
        dept_id = "dept_fe"
        append(self.conn, "dept.opened", self.lead, {"name": "前端部", "mission": "页面", "part": p1},
               ws=self.ws, conv=self.conv, subject=dept_id)
        em = self.hire()
        t1 = self.add_task("前端活", part=p1, owner=em)

        s = self.sched()
        s.tick()
        st = load_state(self.conn)
        dk = "%s|%s" % (self.conv, dept_id)
        self.assertFalse(st["depts"][dk].get("closed"))

        # 完成活
        self.finish_task(t1)
        s.tick()
        st = load_state(self.conn)
        self.assertTrue(st["depts"][dk].get("closed"))

        closed_events = [e for e in self.events("dept.closed") if e["subject"] == dept_id]
        self.assertEqual(len(closed_events), 1)
        ev = closed_events[0]
        self.assertEqual(ev["by"], "office")
        self.assertEqual(ev["data"].get("reason"), "小任务做完了")

        # 再跑一拍，幂等性保证不重复关
        s.tick()
        self.assertEqual(len([e for e in self.events("dept.closed") if e["subject"] == dept_id]), 1)

    def test_part_department_with_part_key_closed(self):
        """部门的 part 字段指定为 key 时，小任务完成也能正确关掉。"""
        p1 = self.add_part("阶段一", key="p1_key")
        dept_id = "dept_be"
        append(self.conn, "dept.opened", self.lead, {"name": "后端部", "mission": "接口", "part": "p1_key"},
               ws=self.ws, conv=self.conv, subject=dept_id)
        em = self.hire()
        t1 = self.add_task("后端活", part="p1_key", owner=em)

        s = self.sched()
        s.tick()
        dk = "%s|%s" % (self.conv, dept_id)
        self.assertFalse(load_state(self.conn)["depts"][dk].get("closed"))

        self.finish_task(t1)
        s.tick()
        self.assertTrue(load_state(self.conn)["depts"][dk].get("closed"))

    def test_employee_never_placed_into_project_dept(self):
        """员工不再被自动塞进项目组（不再开项目组，不再写 employee.joined）。"""
        em = self.hire()
        self.add_task("自由活", owner=em)
        s = self.sched()
        s.tick()
        st = load_state(self.conn)
        self.assertNotIn(self.conv, st["employees"][em].get("depts", {}))
        self.assertEqual([e for e in self.events("dept.opened") if e["subject"] == "proj"], [])
        self.assertEqual(self.events("employee.joined"), [])

    def test_part_with_canceled_tasks_considered_done(self):
        """小任务里撤回的活不算未完成：撤回后没撤回的活全部 done，后置小任务开工且部门关闭。"""
        p1 = self.add_part("阶段一")
        p2 = self.add_part("阶段二", after=[p1])
        dept_id = "dept_cancel"
        append(self.conn, "dept.opened", self.lead, {"name": "撤回组", "mission": "测试", "part": p1},
               ws=self.ws, conv=self.conv, subject=dept_id)
        em = self.hire()
        t1 = self.add_task("撤回任务", part=p1, owner=em)
        t2 = self.add_task("后续任务", part=p2, owner=em)

        s = self.sched()
        s.tick()
        self.assertEqual(self.status(t2), "planned")
        dk = "%s|%s" % (self.conv, dept_id)
        self.assertFalse(load_state(self.conn)["depts"][dk].get("closed"))

        # 撤回 t1
        append(self.conn, "task.removed", self.lead, {"reason": "不要了"}, ws=self.ws, conv=self.conv, subject=t1)
        s.tick()
        self.assertEqual(self.status(t2), "queued")
        self.assertTrue(load_state(self.conn)["depts"][dk].get("closed"))
