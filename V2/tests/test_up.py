"""xay2 up 一键开工命令测试。"""
import io
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch, MagicMock

V2_ROOT = Path(__file__).resolve().parents[1]
if str(V2_ROOT) not in sys.path:
    sys.path.insert(0, str(V2_ROOT))

from xay.cli import _up, main  # noqa: E402
from tests.test_cli_smoke import CliCase  # noqa: E402


class TestUp(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.tmp_home = Path(self.tmp.name) / "xay2-home"
        self.tmp_home.mkdir(parents=True, exist_ok=True)

    @patch("xay.cli._start_keeper")
    @patch("xay.office.daemon.running_info")
    @patch("xay.office.paths.v2_home")
    @patch("socket.create_connection")
    @patch("socket.socket")
    @patch("shutil.which")
    @patch("xay.providers.agent_kinds")
    @patch("xay.providers.get")
    def test_up_all_passed_and_starts_keeper(self, mock_get, mock_kinds, mock_which,
                                             mock_socket_cls, mock_connect, mock_home,
                                             mock_keeper, mock_start):
        mock_home.return_value = self.tmp_home
        mock_keeper.return_value = None  # 管家原本没开
        mock_start.return_value = 0
        mock_connect.side_effect = OSError("Connection refused")
        mock_socket_cls.return_value.__enter__.return_value = MagicMock()
        mock_kinds.return_value = ("codex", "dsh", "agy", "opencode")

        fake_opencode = MagicMock(binary="opencode", available=True)
        mock_get.side_effect = lambda k: fake_opencode

        # git, codex, agy 安装了，dsh 和 opencode 没装
        def fake_which(cmd):
            if cmd in ("git", "codex", "agy"):
                return f"/usr/local/bin/{cmd}"
            return None
        mock_which.side_effect = fake_which

        stdout_buf = io.StringIO()
        with patch("sys.stdout", stdout_buf):
            code = _up([])

        output = stdout_buf.getvalue()
        self.assertEqual(code, 0)
        # 1. 体检只打印没通过的项：全部通过时不应打印 [✓] 或 [-]
        self.assertNotIn("[✓]", output)
        self.assertNotIn("[-]", output)
        self.assertNotIn("[✗]", output)
        # 2. 打印一行 '能用的 AI：…'
        self.assertIn("能用的 AI：codex、agy", output)
        # 3. 管家没开就开
        mock_start.assert_called_once()
        # 4. 打印看板和公司页地址、下一步命令
        self.assertIn("看板：http://127.0.0.1:7878/", output)
        self.assertIn("公司页：http://127.0.0.1:7879/org", output)
        self.assertIn('下一步命令：xay2 begin --title "你的大任务标题"', output)

    @patch("xay.cli._start_keeper")
    @patch("xay.office.daemon.running_info")
    @patch("xay.office.paths.v2_home")
    @patch("socket.create_connection")
    @patch("socket.socket")
    @patch("shutil.which")
    def test_up_keeper_already_running(self, mock_which, mock_socket_cls, mock_connect,
                                       mock_home, mock_keeper, mock_start):
        mock_home.return_value = self.tmp_home
        mock_keeper.return_value = (65432, 7879, 100.0)  # 管家已在运行
        mock_connect.side_effect = OSError("Connection refused")
        mock_socket_cls.return_value.__enter__.return_value = MagicMock()
        mock_which.side_effect = lambda cmd: f"/usr/local/bin/{cmd}"

        stdout_buf = io.StringIO()
        with patch("sys.stdout", stdout_buf):
            code = _up([])

        output = stdout_buf.getvalue()
        self.assertEqual(code, 0)
        # 不重复启动管家
        mock_start.assert_not_called()
        self.assertIn("能用的 AI：", output)
        self.assertIn("看板：http://127.0.0.1:7878/", output)
        self.assertIn("公司页：http://127.0.0.1:7879/org", output)
        self.assertIn("xay2 begin --title", output)

    @patch("xay.cli._start_keeper")
    @patch("xay.office.daemon.running_info")
    @patch("xay.office.paths.v2_home")
    @patch("socket.create_connection")
    @patch("socket.socket")
    @patch("shutil.which")
    def test_up_diagnostic_failed_shows_failures_and_stops(self, mock_which, mock_socket_cls,
                                                           mock_connect, mock_home,
                                                           mock_keeper, mock_start):
        mock_home.return_value = self.tmp_home
        mock_keeper.return_value = None
        mock_connect.side_effect = OSError("Connection refused")
        mock_socket_cls.return_value.__enter__.return_value = MagicMock()
        # git 没找到
        mock_which.side_effect = lambda cmd: None if cmd == "git" else f"/usr/local/bin/{cmd}"

        stdout_buf = io.StringIO()
        with patch("sys.stdout", stdout_buf):
            code = _up([])

        output = stdout_buf.getvalue()
        self.assertEqual(code, 1)
        # 只打印没通过的项
        self.assertIn("[✗] git：未找到", output)
        self.assertNotIn("[✓] Python 版本", output)
        self.assertIn("能用的 AI：", output)
        self.assertIn("体检未通过", output)
        # 体检失败绝不起管家，也不打印下一步开工信息
        mock_start.assert_not_called()
        self.assertNotIn("下一步命令：xay2 begin", output)

    @patch("xay.cli._start_keeper")
    @patch("xay.office.daemon.running_info")
    @patch("xay.office.paths.v2_home")
    @patch("socket.create_connection")
    @patch("socket.socket")
    @patch("shutil.which")
    def test_up_no_available_ai(self, mock_which, mock_socket_cls,
                                mock_connect, mock_home, mock_keeper, mock_start):
        mock_home.return_value = self.tmp_home
        mock_keeper.return_value = None
        mock_start.return_value = 0
        mock_connect.side_effect = OSError("Connection refused")
        mock_socket_cls.return_value.__enter__.return_value = MagicMock()
        # 只有 git，没有 AI 工具
        mock_which.side_effect = lambda cmd: "/usr/bin/git" if cmd == "git" else None

        stdout_buf = io.StringIO()
        with patch("sys.stdout", stdout_buf):
            code = _up([])

        output = stdout_buf.getvalue()
        self.assertEqual(code, 0)
        self.assertIn("能用的 AI：无", output)

    def test_up_executor_rejected(self):
        with patch.dict(os.environ, {"XAY_ROLE": "executor"}):
            stderr_buf = io.StringIO()
            with patch("sys.stderr", stderr_buf):
                code = main(["up"])
            self.assertEqual(code, 2)
            self.assertIn("执行者不能", stderr_buf.getvalue())


class TestUpSmoke(CliCase):
    def test_cli_smoke_up_dispatches(self):
        # 命令行冒烟测试：执行者调用 up 被拒绝
        r = self.xay("up", XAY_ROLE="executor")
        self.assertEqual(r.returncode, 2)
        self.assertIn("执行者不能", r.stderr)


if __name__ == "__main__":
    unittest.main()
