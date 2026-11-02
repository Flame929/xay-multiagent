"""测试员工汇报大白话与监工把关（protocol §1.29）。"""
import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from xay.adapters.fake import FakeAdapter
from xay.core import append, load_state, open_db, since
from xay.core.humanize import text as humanize_text
from xay.core.ids import new_id, ws_for
from xay.core.snapshot import build as snapshot_build
from xay.office import employee, plain
from xay.office.plain import is_plain
from xay.station import markers, station as station_mod
from xay.station.station import Station

GIT_ENV = {"GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t", "GIT_COMMITTER_NAME": "t",
           "GIT_COMMITTER_EMAIL": "t@t", "GIT_CONFIG_GLOBAL": os.devnull, "GIT_CONFIG_NOSYSTEM": "1"}
CLEAR_ENV = ("XAY2_FAKE_ADAPTER", "XAY_ROLE", "XAY2_IDLE_SECONDS", "XAY2_HEARTBEAT_S", "XAY2_TASK", "XAY2_TASK_FILE",
             "XAY2_OUTBOX", "XAY2_CONV", "XAY2_EM", "XAY2_HEARTBEAT_TIMEOUT", "XAY2_AGY_DAILY_CAP", "XAY_DSH_WINDOW",
             "XAY2_COMPACT_AT_DSH", "XAY2_COMPACT_AT_AGY", "XAY2_COMPACT_AT_CODEX", "XAY2_COMPACT_TURNS_AGY",
             "XAY2_COMPACT_TOKENS_DSH", "XAY_AGY_TOOLS_HOME", "XAY2_PLAIN_CHECK")


def make_repo(root):
    root.mkdir(parents=True)
    run = lambda *a: subprocess.run(["git", *a], cwd=str(root), check=True, capture_output=True)
    run("init", "-q", "-b", "main", ".")
    (root / "README.md").write_text("hello\n")
    run("add", "-A")
    run("-c", "commit.gpgsign=false", "commit", "-q", "-m", "init")
    return root


def writes(name, content, steps):
    def item(a, text, extra):
        p = Path(a.cwd) / name
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content)
        return steps
    return item


class Factory:
    def __init__(self, scripts=None, **fake_kw):
        self.scripts = list(scripts or [])
        self.fake_kw = fake_kw
        self.instances = []

    def __call__(self, agent):
        a = FakeAdapter(agent, **self.fake_kw)
        script = self.scripts.pop(0) if self.scripts else []
        a.script = [self._bind(a, x) for x in script]
        self.instances.append(a)
        return a

    @staticmethod
    def _bind(a, item):
        if callable(item):
            return lambda text, extra: item(a, text, extra)
        return item


class TestPlainReport(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="xay-plain-test-"))
        self.addCleanup(shutil.rmtree, self.tmp, True)
        patcher = mock.patch.dict(os.environ, dict(GIT_ENV, XAY2_HOME=str(self.tmp / "v2"), XAY_HOME=str(self.tmp / "oldxay"),
                                                   XAY_AGY_TOOLS_BIN=str(self.tmp / "no-antigravity-tools")))
        patcher.start()
        self.addCleanup(patcher.stop)
        for k in CLEAR_ENV:
            os.environ.pop(k, None)
        self.repo = make_repo(self.tmp / "proj")
        self.conn = open_db()
        self.addCleanup(self.conn.close)
        self.ws, self.conv = ws_for(self.repo), "cv_testplain"
        self.lead = "lead:" + self.conv
        append(self.conn, "conv.begun", self.lead, {"title": "大白话测试", "lead_kind": "claude", "aliases": [],
                                                    "cwd": str(self.repo)}, ws=self.ws, conv=self.conv, subject=self.conv)
        self.em = "em_c01"
        append(self.conn, "employee.hired", self.lead, {"agent": "codex", "label": "Codex 1"}, ws=self.ws, conv=self.conv, subject=self.em)
        self.desk = self.tmp / "v2" / "desks" / self.ws / self.em

    def state(self):
        return load_state(self.conn)

    def events(self, typ=None, **match):
        out = []
        for e in since(self.conn, 0, limit=100000):
            if typ and e["type"] != typ:
                continue
            if all(e["data"].get(k) == v for k, v in match.items()):
                out.append(e)
        return out

    def add_task(self, title):
        tk = new_id("tk_")
        append(self.conn, "task.added", self.lead, {"title": title, "card": "", "deps": [], "writes": [], "mode": "write",
                                                     "reason": "测试", "owner": self.em}, ws=self.ws, conv=self.conv, subject=tk)
        append(self.conn, "task.ready", "office", {}, ws=self.ws, conv=self.conv, subject=tk)
        n = len(self.state()["employees"][self.em]["queue"]) + 1
        append(self.conn, "task.queued", "office", {"owner": self.em, "position": n}, ws=self.ws, conv=self.conv, subject=tk)
        return tk

    def run_station(self, factory, **kw):
        kw.setdefault("poll_s", 0.02)
        st = Station(self.em, adapter_factory=factory, **kw)
        rc = st.run(once=True)
        self.assertEqual(rc, 0)
        return st

    def test_parse_plain_marker(self):
        """解析【给老板看】标记与提取。"""
        raw = "【进度 1/2】做了一半\n【给老板看】给用户做了快速导出功能\n【结果】完成：全都搞定了"
        scanned = markers.scan(raw, final=True)
        kinds = [s[1] for s in scanned]
        self.assertIn("plain", kinds)
        plain_item = next(s[2] for s in scanned if s[1] == "plain")
        self.assertEqual(plain_item["plain"], "给用户做了快速导出功能")

        # extract_plain
        self.assertEqual(markers.extract_plain(raw), "给用户做了快速导出功能")
        self.assertEqual(markers.parse_plain("：修复了登录按钮"), "修复了登录按钮")

    def test_is_plain_criteria(self):
        """判断像不像大白话：含路径、扩展名文件名、反引号、下划线英文、驼峰、超过80字均不过。"""
        self.assertTrue(is_plain("做好了用户登录页面，现在可以直接输入账号进入系统了"))

        # 空串
        self.assertFalse(is_plain(""))
        self.assertFalse(is_plain("   "))

        # 超过 80 字
        self.assertFalse(is_plain("这是一段非常非常非常非常非常非常非常非常非常非常非常非常非常非常非常非常非常非常非常非常非常非常非常非常非常非常非常非常非常非常非常非常长的大白话，字数明显已经超过了整整八十个汉字的上限了啊"))

        # 反引号
        self.assertFalse(is_plain("跑了 `python3 test.py` 检查通过"))

        # 斜杠
        self.assertFalse(is_plain("改了 src/app/login 目录下的逻辑"))

        # . 加扩展名的文件名
        self.assertFalse(is_plain("在 login.py 里面加了校验"))
        self.assertFalse(is_plain("更新了 config.json 文件"))

        # 下划线连起来的英文
        self.assertFalse(is_plain("修复了 user_id 的匹配问题"))

        # 驼峰英文单词
        self.assertFalse(is_plain("更新了 handleLogin 函数"))
        self.assertFalse(is_plain("新增了 UserProfile 页面"))

    def test_missing_plain_triggers_plain_retry_and_lands(self):
        """活报了做完但缺【给老板看】→ 补一轮 plain_retry → 补完正常合并。"""
        f = Factory(
            [[writes("done.txt", "content\n", [{"text": "【结果】完成：搞定了"}]),
              [{"text": "【给老板看】做好了新功能，用户可以直接看到结果"}]]]
        )
        tk = self.add_task("测试缺大白话补一轮")
        self.run_station(f)
        self.assertEqual(len(f.instances), 1)
        self.assertEqual(len(f.instances[0].sent), 2)
        kinds = [e["data"]["kind"] for e in self.events("turn.started")]
        self.assertEqual(kinds, ["first", "plain_retry"])
        self.assertIn("用一句大白话告诉老板这件活做成了什么", f.instances[0].sent[1][0])
        self.assertEqual(self.state()["tasks"][tk]["status"], "reported")
        self.assertEqual(self.state()["tasks"][tk].get("plain"), "做好了新功能，用户可以直接看到结果")
        self.assertEqual([e["subject"] for e in self.events("task.landed")], [tk])

    def test_code_like_plain_triggers_plain_retry(self):
        """【给老板看】看起来像代码或含文件名 → 补一轮 plain_retry。"""
        f = Factory(
            [[writes("fix.py", "pass\n", [{"text": "【给老板看】改了 fix.py 里的 do_fix 函数\n【结果】完成：搞定"}]),
              [{"text": "【给老板看】修好了保存时可能白屏的问题"}]]]
        )
        tk = self.add_task("测试不像大白话补一轮")
        self.run_station(f)
        self.assertEqual(len(f.instances), 1)
        self.assertEqual(len(f.instances[0].sent), 2)
        kinds = [e["data"]["kind"] for e in self.events("turn.started")]
        self.assertEqual(kinds, ["first", "plain_retry"])
        self.assertEqual(self.state()["tasks"][tk].get("plain"), "修好了保存时可能白屏的问题")
        self.assertEqual([e["subject"] for e in self.events("task.landed")], [tk])

    def test_plain_retry_only_once_and_does_not_block_merge(self):
        """plain_retry 每件活最多补 1 次，即使第二轮仍不合规，也不影响合并。"""
        f = Factory(
            [[writes("res.txt", "abc\n", [{"text": "【结果】完成：好了"}]),
              [{"text": "【给老板看】还是写了 a/b/c 路径"}]]]
        )
        tk = self.add_task("测试只补一次且不影响合并")
        self.run_station(f)
        self.assertEqual(len(f.instances), 1)
        self.assertEqual(len(f.instances[0].sent), 2)  # 最多一次
        self.assertEqual(self.state()["tasks"][tk]["status"], "reported")
        self.assertEqual([e["subject"] for e in self.events("task.landed")], [tk])

    def test_plain_in_snapshot_and_employee_detail(self):
        """plain 进入快照 tasks[] 和员工任务树的 detail。"""
        tk = self.add_task("测试快照带plain")
        append(self.conn, "task.reported", "employee:" + self.em, {"claim": "done", "summary": "做完了", "plain": "帮用户算好了总账"},
               subject=tk, conv=self.conv, ws=self.ws)
        append(self.conn, "task.landed", "office", {}, subject=tk, conv=self.conv, ws=self.ws)

        # 快照
        snap = snapshot_build(self.conn, conv=self.conv)
        task_snap = next(t for t in snap["tasks"] if t["id"] == tk)
        self.assertEqual(task_snap.get("plain"), "帮用户算好了总账")

        # 任务树 detail
        data = employee.build(self.conn, self.em, conv=self.conv)
        tree = data["tree"]
        all_nodes = [item for g in tree.get("groups", []) for item in g.get("items", [])]
        node = next(n for n in all_nodes if n["id"] == tk)
        self.assertEqual(node.get("detail", {}).get("plain"), "帮用户算好了总账")

    def test_humanize_landed_broadcast_uses_plain(self):
        """播报做完优先用 plain，没有 plain 就照旧。"""
        tk = self.add_task("测试播报")
        s = self.state()
        # 没有 plain
        ev_no_plain = {"type": "task.landed", "subject": tk, "conv": self.conv, "data": {}}
        t_no_plain = humanize_text(ev_no_plain, self.conn, state=s)
        self.assertEqual(t_no_plain, "Codex 1 做完了「测试播报」，改动已合并")

        # 有 plain
        s["tasks"][tk]["plain"] = "把报表下载功能做好了"
        t_with_plain = humanize_text(ev_no_plain, self.conn, state=s)
        self.assertEqual(t_with_plain, "Codex 1 做完了「测试播报」：把报表下载功能做好了")
