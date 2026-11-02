"""管家排程（V2/xay/office/scheduler.py）测试：临时 XAY2_HOME + 临时 git 仓库；真起工位进程时用假适配器（XAY2_FAKE_ADAPTER）。"""
import json
import os
import shutil
import signal
import subprocess
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

from xay.core import append, load_state, open_db, since
from xay.core.ids import new_id, ws_for
from xay.office.scheduler import Scheduler
from xay.station import station as st

from tests.test_station import CLEAR_ENV, GIT_ENV, make_repo

for _k in CLEAR_ENV:
    os.environ.pop(_k, None)


class SchedulerCase(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="xay-sched-test-"))
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.home = self.tmp / "v2"
        # XAY_HOME 也指到临时目录：agy 每天的次数要合计旧系统当天的登记（xay.quota），不能读到真的 ~/.xay
        patcher = mock.patch.dict(os.environ, dict(GIT_ENV, XAY2_HOME=str(self.home), XAY_HOME=str(self.tmp / "oldxay")))
        patcher.start()
        self.addCleanup(patcher.stop)
        for k in CLEAR_ENV:
            os.environ.pop(k, None)
        self.repo = make_repo(self.tmp / "proj")
        self.conn = open_db()
        self.addCleanup(self.conn.close)
        self.ws, self.conv = ws_for(self.repo), "cv_sched001"
        self.lead = "lead:" + self.conv
        append(self.conn, "conv.begun", self.lead, {"title": "排程", "lead_kind": "claude", "aliases": [], "cwd": str(self.repo)},
               ws=self.ws, conv=self.conv, subject=self.conv)
        self.spawned = []

    def hire(self, agent="codex"):
        em = new_id("em_")
        append(self.conn, "employee.hired", self.lead, {"agent": agent}, ws=self.ws, conv=self.conv, subject=em)
        return em

    def add(self, title, owner=None, deps=(), agent=None, key=None):
        tk = new_id("tk_")
        data = {"title": title, "deps": list(deps), "writes": [], "mode": "write", "reason": "测试", "card": title}
        if owner:
            data["owner"] = owner
        if agent:
            data["agent"] = agent
        if key:
            data["key"] = key
        append(self.conn, "task.added", self.lead, data, ws=self.ws, conv=self.conv, subject=tk)
        return tk

    def sched(self, **kw):
        kw.setdefault("spawn", lambda em: self.spawned.append(em))
        return Scheduler(self.conn, home=self.home, **kw)

    def events(self, typ):
        return [e for e in since(self.conn, 0, limit=100000) if e["type"] == typ]

    def status(self, tk):
        return load_state(self.conn)["tasks"][tk]["status"]


class PlanningTests(SchedulerCase):
    def test_ready_waits_for_deps_then_queues_to_owner(self):
        em = self.hire()
        a = self.add("先做", owner=em, key="a")
        b = self.add("后做", owner=em, deps=["a"])
        s = self.sched()
        s.tick()
        self.assertEqual((self.status(a), self.status(b)), ("queued", "planned"))
        self.assertEqual(self.spawned, [em])
        s.tick()
        self.assertEqual(self.spawned, [em])                 # 没拿到进程句柄：等一会再说，不连着起
        # 前置做完（负责人验收）→ 后一件 ready → queued
        append(self.conn, "task.landed", "office", {"files": [], "add": 0, "del": 0}, ws=self.ws, conv=self.conv, subject=a)
        s.tick()
        self.assertEqual(self.status(b), "planned")
        append(self.conn, "task.accepted", self.lead, {}, ws=self.ws, conv=self.conv, subject=a)
        s.tick()
        self.assertEqual(self.status(b), "queued")
        q = [e for e in self.events("task.queued") if e["subject"] == b][0]
        self.assertEqual((q["by"], q["data"]["owner"]), ("office", em))
        self.assertEqual(len([e for e in self.events("task.ready") if e["subject"] == b]), 1)

    def test_unowned_task_with_agent_hires_or_reuses_idle_employee(self):
        t1 = self.add("活一", agent="codex")
        s = self.sched()
        s.tick()
        state = load_state(self.conn)
        em1 = state["tasks"][t1]["owner"]
        self.assertTrue(em1)
        self.assertEqual(state["employees"][em1]["label"], "Codex 1")
        hired = self.events("employee.hired")[-1]
        self.assertEqual((hired["by"], hired["subject"]), ("office", em1))
        assigned = self.events("task.assigned")[-1]
        self.assertEqual(assigned["by"], "office")
        self.assertIn("招了 Codex 1", assigned["data"]["reason"])
        self.assertEqual(state["conv" + "s"][self.conv]["plan_version"], 1)     # 管家自动分配不加计划版本
        # Codex 1 正忙（排着活）→ 第二件招 Codex 2
        t2 = self.add("活二", agent="codex")
        s.tick()
        em2 = load_state(self.conn)["tasks"][t2]["owner"]
        self.assertNotEqual(em2, em1)
        # Codex 1 做完了、记忆 <80% → 第三件交给他
        append(self.conn, "task.landed", "office", {"files": [], "add": 0, "del": 0}, ws=self.ws, conv=self.conv, subject=t1)
        t3 = self.add("活三", agent="codex")
        s.tick()
        self.assertEqual(load_state(self.conn)["tasks"][t3]["owner"], em1)
        self.assertIn("交给空闲的 Codex 1", self.events("task.assigned")[-1]["data"]["reason"])
        # 两个人都空着：记忆 ≥80% 的排在后面（M4 起：压缩压得下来的仍然可以选，压不下来的才排除，见 test_rules）
        append(self.conn, "context.updated", "station:" + em2, {"used": 9, "window": 10, "pct": 90, "source": "codex_last",
                                                               "estimated": False}, ws=self.ws, subject=em2)
        append(self.conn, "task.landed", "office", {"files": [], "add": 0, "del": 0}, ws=self.ws, conv=self.conv, subject=t2)
        append(self.conn, "task.landed", "office", {"files": [], "add": 0, "del": 0}, ws=self.ws, conv=self.conv, subject=t3)
        t4 = self.add("活四", agent="codex")
        s.tick()
        self.assertEqual(load_state(self.conn)["tasks"][t4]["owner"], em1)
        self.assertIn("交给空闲的 Codex 1", self.events("task.assigned")[-1]["data"]["reason"])
        # 没说给谁、也没说用哪种 AI：留给负责人
        t5 = self.add("活五")
        s.tick()
        self.assertEqual(self.status(t5), "ready")

    def test_employee_without_dept_is_placed(self):
        """不再自动归部门：不再开"项目组"、不再写 employee.joined 硬塞进部门。"""
        append(self.conn, "dept.opened", self.lead, {"name": "页面部", "mission": "", "owns": ["web/**"], "head": "rules"},
               ws=self.ws, conv=self.conv, subject="fe")
        a, b = self.hire(), self.hire()
        t1 = self.add("改页面", owner=a)
        append(self.conn, "task.changed", self.lead, {"fields": {"writes": ["web/index.html"]}, "reason": "测试"},
               ws=self.ws, conv=self.conv, subject=t1)
        self.add("算运费", owner=b)
        self.add("顺手修个字", owner=a)
        s = self.sched()
        s.tick()
        st = load_state(self.conn)
        self.assertNotIn(self.conv, st["employees"][a].get("depts", {}))
        self.assertNotIn(self.conv, st["employees"][b].get("depts", {}))
        self.assertEqual([e for e in self.events("dept.opened") if e["subject"] == "proj"], [])
        self.assertEqual(self.events("employee.joined"), [])

    def test_bridged_old_system_employees_never_get_work(self):
        """桥镜像的旧系统员工（备注"旧系统 · <批次名>"）只展示：管家选人时跳过，宁可招新人。"""
        old = new_id("em_")
        append(self.conn, "employee.hired", "bridge", {"agent": "codex", "note": "旧系统 · V2 探针日：写三家 AI 的实测脚本"},
               ws=self.ws, conv=self.conv, subject=old)
        tk = self.add("新活", agent="codex")
        self.sched().tick()
        owner = load_state(self.conn)["tasks"][tk]["owner"]
        self.assertTrue(owner)
        self.assertNotEqual(owner, old)

    def test_off_duty_shows_resting_crash_shows_offline(self):
        from xay.core import snapshot
        em = self.hire()
        append(self.conn, "employee.online", "office", {}, ws=self.ws, conv=self.conv, subject=em)
        append(self.conn, "employee.offline", "office", {"reason": "idle"}, ws=self.ws, conv=self.conv, subject=em)
        p = snapshot.build(self.conn, conv=self.conv)["people"][em]
        self.assertEqual((p["state"], p["display_state"]), ("offline", "idle"))
        append(self.conn, "employee.online", "office", {}, ws=self.ws, conv=self.conv, subject=em)
        append(self.conn, "employee.offline", "office", {"reason": "crash"}, ws=self.ws, conv=self.conv, subject=em)
        p = snapshot.build(self.conn, conv=self.conv)["people"][em]
        self.assertEqual((p["state"], p["display_state"]), ("offline", "offline"))

    def test_stop_all_halts_spawning_until_lead_plans_again(self):
        em = self.hire()
        append(self.conn, "control.requested", self.lead, {"action": "stop_all", "by_source": "cli"}, ws=self.ws, conv=self.conv)
        self.add("活", owner=em)
        s = self.sched()
        s.tick()
        self.assertEqual(self.spawned, [em])    # task.added 在总闸之后 → 等于"接着干"
        # 先排活再拉总闸 → 不起
        self.spawned.clear()
        s2 = self.sched()
        em2 = self.hire()
        self.add("活二", owner=em2)
        append(self.conn, "control.requested", self.lead, {"action": "stop_all", "by_source": "cli"}, ws=self.ws, conv=self.conv)
        s2.tick()
        self.assertEqual(self.spawned, [])
        append(self.conn, "control.requested", "user", {"action": "resume", "by_source": "page"}, ws=self.ws, conv=self.conv)
        s2.tick()
        self.assertEqual(sorted(self.spawned), sorted([em, em2]))

    def test_running_station_is_not_spawned_twice(self):
        em = self.hire()
        self.add("活", owner=em)
        lock = st.lock_path(self.home, em)
        lock.parent.mkdir(parents=True)
        import fcntl
        with open(lock, "a+") as fh:
            fcntl.flock(fh, fcntl.LOCK_EX)
            self.assertTrue(st.station_running(self.home, em))
            self.sched().tick()
            self.assertEqual(self.spawned, [])
        self.assertFalse(st.station_running(self.home, em))


class LiveStationTests(SchedulerCase):
    """真起工位进程（假适配器），看管家能不能接住崩溃、空闲下班后再起。"""

    def spec(self, **spec):
        path = self.tmp / "fake.json"
        path.write_text(json.dumps(spec))
        return path

    def live_sched(self, spec, **env):
        e = dict(os.environ, XAY2_FAKE_ADAPTER=str(spec), XAY2_HEARTBEAT_S="0.3", **env)
        s = Scheduler(self.conn, home=self.home, env=e, heartbeat_timeout=2.0)
        self.addCleanup(self.kill_all, s)
        return s

    def kill_all(self, s):
        for proc in list(s.procs.values()):
            try:
                os.killpg(proc.pid, signal.SIGKILL)
            except OSError:
                pass
            proc.wait(timeout=5)

    def tick_until(self, s, fn, timeout=20.0, what=""):
        deadline = time.time() + timeout
        while time.time() < deadline:
            s.tick()
            v = fn()
            if v:
                return v
            time.sleep(0.1)
        log = self.home / "logs"
        tail = "".join(p.read_text()[-1500:] for p in log.glob("station-*.log")) if log.exists() else ""
        self.fail("等了 %.0f 秒还没等到：%s\n%s" % (timeout, what, tail))

    def test_killed_station_is_marked_offline_and_turn_interrupted(self):
        em = self.hire()
        tk = self.add("一直在干的活", owner=em)
        s = self.live_sched(self.spec(turns=[[{"sleep": 60}]]))
        self.tick_until(s, lambda: self.events("turn.started"), what="开工")
        self.assertEqual(load_state(self.conn)["employees"][em]["online"], True)
        proc = s.procs[em]
        os.killpg(proc.pid, signal.SIGKILL)
        t0 = time.time()
        self.tick_until(s, lambda: self.events("employee.offline"), timeout=35, what="标离线")
        self.assertLess(time.time() - t0, 35)
        off = self.events("employee.offline")[-1]
        self.assertEqual((off["by"], off["data"]["reason"]), ("office", "crash"))
        kept = self.events("task.kept")
        self.assertEqual([(e["by"], e["data"]["reason"]) for e in kept], [("office", "interrupted")])
        self.assertEqual(self.status(tk), "needs_you")
        dec = list(load_state(self.conn)["decisions"].values())
        self.assertEqual([(d["kind"], d["task"]) for d in dec], [("interrupted", tk)])
        self.assertEqual(self.events("turn.ended")[-1]["data"]["status"], "interrupted")
        self.assertFalse(st.station_running(self.home, em))

    def test_idle_exit_then_new_task_restarts_and_resumes_same_session(self):
        em = self.hire()
        opens = self.tmp / "opens.jsonl"
        spec = self.spec(default=[{"write": {"path": "{task}.txt", "text": "done {task}"}}, {"text": "【结果】完成：好了"}],
                         open_log=str(opens))
        s = self.live_sched(spec, XAY2_IDLE_SECONDS="0.5")
        t1 = self.add("第一件", owner=em)
        self.tick_until(s, lambda: [e for e in self.events("employee.offline") if e["data"]["reason"] == "idle"],
                        what="第一件做完、空闲下班")
        self.assertEqual(self.status(t1), "reported")
        self.assertTrue((self.repo / ("%s.txt" % t1)).exists())
        self.tick_until(s, lambda: em not in s.procs and not st.station_running(self.home, em), what="工位退出")

        t2 = self.add("第二件", owner=em)
        self.tick_until(s, lambda: self.status(t2) == "reported", what="第二件做完")
        self.assertTrue((self.repo / ("%s.txt" % t2)).exists())
        rows = [json.loads(x) for x in opens.read_text().splitlines()]
        self.assertEqual(len(rows), 2)
        self.assertIsNone(rows[0]["native_id_given"])
        self.assertEqual(rows[1]["native_id_given"], rows[0]["native_id"])     # 第二次起来续接原来的对话
        sessions = self.events("employee.session")
        self.assertEqual([(e["data"]["native_id"], e["data"]["reason"]) for e in sessions], [(rows[0]["native_id"], "new")])
        self.assertEqual(len([e for e in self.events("employee.online")]), 2)

    def test_offline_employee_with_unhandled_steer_spawns_station_and_halt_blocks(self):
        em = self.hire()
        append(self.conn, "employee.offline", "office", {"reason": "idle"}, ws=self.ws, conv=self.conv, subject=em)
        s = self.sched()
        s.tick()
        self.assertEqual(self.spawned, [], "没有未处理插话时不给起工位")

        # 来了发给该员工的插话
        append(self.conn, "control.requested", self.lead, {"action": "steer", "text": "在吗"},
               ws=self.ws, conv=self.conv, subject=em)
        s.tick()
        self.assertEqual(self.spawned, [em], "下班员工有未处理插话，起工位")

        # 如果拉了总闸（halt），即使有新插话也不起
        self.spawned.clear()
        em2 = self.hire()
        append(self.conn, "employee.offline", "office", {"reason": "idle"}, ws=self.ws, conv=self.conv, subject=em2)
        append(self.conn, "control.requested", self.lead, {"action": "stop_all"}, ws=self.ws, conv=self.conv)
        append(self.conn, "control.requested", self.lead, {"action": "steer", "text": "总闸后说话"},
               ws=self.ws, conv=self.conv, subject=em2)
        s.tick()
        self.assertEqual(self.spawned, [], "总闸拉下时不起工位")


if __name__ == "__main__":
    unittest.main()


class PendingChatSteerTests(SchedulerCase):
    """没活时的聊天轮：当场说过的、管家规则发的提醒都不算；开过工之后的才算（不然会一遍遍把人叫起来重复说）。"""
    def test_only_undelivered_lead_steers_after_last_turn_count(self):
        em = self.hire()
        steer = lambda text, src="cli": append(self.conn, "control.requested", self.lead, {"action": "steer", "text": text, "by_source": src},
                                              ws=self.ws, conv=self.conv, subject=em)
        old = steer("开工前说的")
        append(self.conn, "turn.started", "station:" + em, {"task": None, "em": em, "kind": "chat"}, ws=self.ws, conv=self.conv, subject="tn_00000001")
        mid = steer("干活中途当场说过的")
        append(self.conn, "control.done", "station:" + em, {"action": "steer", "result": "ok", "request": mid, "delivered": True, "detail": "当场告诉他了"},
               ws=self.ws, conv=self.conv, subject=em)
        steer("预算快用完了", src="rules")
        steer("记住：提交前先跑测试")
        texts, conv = st.pending_chat_steers(self.conn, em)
        self.assertEqual(texts, ["记住：提交前先跑测试"])
        self.assertEqual(conv, self.conv)
        self.assertEqual(list(st.pending_chat_steers(self.conn)), [em])
        self.assertNotIn(old, [])   # 开工前那句已被那一轮带走
        append(self.conn, "turn.started", "station:" + em, {"task": None, "em": em, "kind": "chat"}, ws=self.ws, conv=self.conv, subject="tn_00000002")
        self.assertEqual(st.pending_chat_steers(self.conn), {})
