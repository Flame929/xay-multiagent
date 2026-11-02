"""xay2 doctor 命令与 Python 版本预检测试。"""
import io
import os
import shutil
import socket
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch, MagicMock

V2_ROOT = Path(__file__).resolve().parents[1]
if str(V2_ROOT) not in sys.path:
    sys.path.insert(0, str(V2_ROOT))

from xay.cli import _doctor, main  # noqa: E402


class TestDoctor(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.tmp_home = Path(self.tmp.name) / "xay2-home"
        self.tmp_home.mkdir(parents=True, exist_ok=True)

    @patch("xay.office.daemon.running_info")
    @patch("xay.office.paths.v2_home")
    @patch("socket.create_connection")
    @patch("socket.socket")
    @patch("shutil.which")
    def test_doctor_all_passed(self, mock_which, mock_socket_cls, mock_connect, mock_home, mock_keeper):
        mock_home.return_value = self.tmp_home
        mock_keeper.return_value = None
        mock_connect.side_effect = OSError("Connection refused")
        mock_sock_inst = MagicMock()
        mock_socket_cls.return_value.__enter__.return_value = mock_sock_inst
        mock_which.side_effect = lambda cmd: f"/usr/local/bin/{cmd}"

        stdout_buf = io.StringIO()
        with patch("sys.stdout", stdout_buf):
            code = _doctor([])

        output = stdout_buf.getvalue()
        self.assertEqual(code, 0)
        self.assertIn("[✓] Python 版本", output)
        self.assertIn("[✓] SQLite3", output)
        self.assertIn("[✓] git", output)
        self.assertIn("[✓] 端口 7879（公司页）：空闲可用", output)
        self.assertIn("[✓] 端口 7878（看板）：空闲可用", output)
        self.assertIn("[✓] 数据目录", output)
        self.assertIn("体检通过", output)

    @patch("xay.office.paths.v2_home")
    def test_doctor_python_version_fail(self, mock_home):
        mock_home.return_value = self.tmp_home
        fake_ver = (3, 10, 8, "final", 0)
        stdout_buf = io.StringIO()
        with patch.object(sys, "version_info", fake_ver), patch("sys.stdout", stdout_buf):
            code = _doctor([])

        output = stdout_buf.getvalue()
        self.assertEqual(code, 1)
        self.assertIn("[✗] Python 版本：3.10.8", output)
        self.assertIn("xay 需要 Python 3.11 或更新的版本", output)
        self.assertIn("体检未通过", output)

    @patch("xay.office.daemon.running_info")
    @patch("xay.office.paths.v2_home")
    @patch("socket.create_connection")
    @patch("socket.socket")
    @patch("shutil.which")
    def test_doctor_git_missing(self, mock_which, mock_socket_cls, mock_connect, mock_home, mock_keeper):
        mock_home.return_value = self.tmp_home
        mock_keeper.return_value = None
        mock_connect.side_effect = OSError("Connection refused")
        mock_socket_cls.return_value.__enter__.return_value = MagicMock()
        mock_which.side_effect = lambda cmd: None if cmd == "git" else f"/usr/local/bin/{cmd}"

        stdout_buf = io.StringIO()
        with patch("sys.stdout", stdout_buf):
            code = _doctor([])

        output = stdout_buf.getvalue()
        self.assertEqual(code, 1)
        self.assertIn("[✗] git：未找到", output)

    @patch("xay.office.daemon.running_info")
    @patch("xay.office.paths.v2_home")
    @patch("socket.create_connection")
    @patch("shutil.which")
    def test_doctor_port_occupied_by_other(self, mock_which, mock_connect, mock_home, mock_keeper):
        mock_home.return_value = self.tmp_home
        mock_keeper.return_value = None
        # 连接成功代表端口被占
        mock_connect.return_value = MagicMock()
        mock_which.return_value = "/usr/bin/git"

        stdout_buf = io.StringIO()
        with patch("sys.stdout", stdout_buf):
            code = _doctor([])

        output = stdout_buf.getvalue()
        self.assertEqual(code, 1)
        self.assertIn("[✗] 端口 7879（公司页）：被别的程序占用了", output)
        self.assertIn("[✗] 端口 7878（看板）：被别的程序占用了", output)

    @patch("xay.office.daemon.running_info")
    @patch("xay.office.paths.v2_home")
    @patch("socket.create_connection")
    @patch("shutil.which")
    def test_doctor_port_occupied_by_keeper(self, mock_which, mock_connect, mock_home, mock_keeper):
        mock_home.return_value = self.tmp_home
        # 管家在跑，端口被占视为正常
        mock_keeper.return_value = (54321, 7879, 100.0)
        mock_connect.return_value = MagicMock()
        mock_which.return_value = "/usr/bin/cmd"

        stdout_buf = io.StringIO()
        with patch("sys.stdout", stdout_buf):
            code = _doctor([])

        output = stdout_buf.getvalue()
        self.assertEqual(code, 0)
        self.assertIn("[✓] 端口 7879（公司页）：正常（管家正在使用）", output)
        self.assertIn("[✓] 端口 7878（看板）：正常（管家正在使用）", output)

    @patch("xay.office.daemon.running_info")
    @patch("xay.office.paths.v2_home")
    @patch("socket.create_connection")
    @patch("socket.socket")
    @patch("shutil.which")
    def test_doctor_ai_tools_missing_not_error(self, mock_which, mock_socket_cls, mock_connect, mock_home, mock_keeper):
        mock_home.return_value = self.tmp_home
        mock_keeper.return_value = None
        mock_connect.side_effect = OSError("Connection refused")
        mock_socket_cls.return_value.__enter__.return_value = MagicMock()
        # 只有 git 存在，其他 AI 工具都不存在
        mock_which.side_effect = lambda cmd: "/usr/bin/git" if cmd == "git" else None

        stdout_buf = io.StringIO()
        with patch("sys.stdout", stdout_buf):
            code = _doctor([])

        output = stdout_buf.getvalue()
        self.assertEqual(code, 0)
        self.assertIn("codex：没装：这家 AI 的活派不出去，装好后再来", output)
        self.assertIn("dsh：没装：这家 AI 的活派不出去，装好后再来", output)
        self.assertIn("agy：没装：这家 AI 的活派不出去，装好后再来", output)
        self.assertIn("antigravity-tools：没装：这家 AI 的活派不出去，装好后再来", output)
        self.assertIn("体检通过", output)

    @patch("xay.office.daemon.running_info")
    @patch("xay.office.paths.v2_home")
    @patch("socket.create_connection")
    @patch("socket.socket")
    @patch("shutil.which")
    @patch("os.access")
    def test_doctor_home_unwritable(self, mock_access, mock_which, mock_socket_cls, mock_connect, mock_home, mock_keeper):
        mock_home.return_value = self.tmp_home
        mock_keeper.return_value = None
        mock_connect.side_effect = OSError("Connection refused")
        mock_socket_cls.return_value.__enter__.return_value = MagicMock()
        mock_which.return_value = "/usr/bin/tool"
        mock_access.return_value = False

        stdout_buf = io.StringIO()
        with patch("sys.stdout", stdout_buf):
            code = _doctor([])

        output = stdout_buf.getvalue()
        self.assertEqual(code, 1)
        self.assertIn("[✗] 数据目录", output)
        self.assertIn("无写权限", output)

    def test_cli_dispatch_doctor(self):
        with patch("xay.cli._doctor") as mock_doc:
            mock_doc.return_value = 0
            rc = main(["doctor"])
            self.assertEqual(rc, 0)
            mock_doc.assert_called_once_with([])

    def test_entrypoint_python_version_guard(self):
        root_dir = V2_ROOT.parent
        for rel in ("V2/bin/xay2", "scripts/xay"):
            path = root_dir / rel
            self.assertTrue(path.exists(), f"{rel} should exist")
            content = path.read_text(encoding="utf-8")
            self.assertIn("sys.version_info < (3, 11)", content)
            self.assertIn("xay 需要 Python 3.11 或更新的版本", content)
            self.assertIn("exit 2", content)


if __name__ == "__main__":
    unittest.main()
