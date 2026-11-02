#!/usr/bin/env python3
"""公开副本导出工具（scripts/xay-export-public）测试。

验证：
1. --help 用法输出；
2. 排除清单（.public-exclude）生效，敏感素材、文档及本地临时文件不被导出；
3. 脏数据深度拦截与格式化输出：
   - 本机路径（/Users/mac）；
   - 真实项目名（Flame、EWEADN、shopify、lingxing、真实邮箱）；
   - 真实格式密钥（sk- 紧跟至少 16 位字母数字/下划线）；
   - CSS 类名如 sk-card、sk-group、sk-card-action 不被误报；
4. 白名单（.public-allow）机制：文件路径:要放过的字符串 精准跳过测试样本；
5. 输出末尾给出 密钥 / 本机路径 / 真实项目名 三类统计；
6. 干净导出时在目标目录生成唯一一个提交且无 remote；
7. 目标目录非空时拒绝覆盖；
8. 中文文件名的文件（如《员工手册.md》）不会被漏导出。
"""

import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPT_PATH = ROOT / "scripts" / "xay-export-public"


class ExportPublicTests(unittest.TestCase):
    def setUp(self):
        if SCRIPT_PATH.exists():
            os.chmod(SCRIPT_PATH, 0o755)

        self.tmp_dir = tempfile.TemporaryDirectory()
        self.base_dir = Path(self.tmp_dir.name)
        self.repo_dir = self.base_dir / "test_repo"
        self.repo_dir.mkdir()

        self._run_git(["init"], cwd=self.repo_dir)
        self._run_git(["config", "user.name", "Test User"], cwd=self.repo_dir)
        self._run_git(["config", "user.email", "test@example.com"], cwd=self.repo_dir)

    def tearDown(self):
        self.tmp_dir.cleanup()

    def _run_git(self, args: list[str], cwd: Path) -> subprocess.CompletedProcess:
        env = os.environ.copy()
        env["GIT_AUTHOR_NAME"] = "Test User"
        env["GIT_AUTHOR_EMAIL"] = "test@example.com"
        env["GIT_COMMITTER_NAME"] = "Test User"
        env["GIT_COMMITTER_EMAIL"] = "test@example.com"
        return subprocess.run(
            ["git"] + args,
            cwd=cwd,
            capture_output=True,
            text=True,
            check=True,
            env=env,
        )

    def _run_exporter(self, args: list[str], cwd: Path | None = None) -> subprocess.CompletedProcess:
        cwd = cwd or self.repo_dir
        cmd = [sys.executable, str(SCRIPT_PATH)] + args
        return subprocess.run(
            cmd,
            cwd=cwd,
            capture_output=True,
            text=True,
        )

    def test_help_option(self):
        """测试 -h 和 --help 参数正常输出说明。"""
        for opt in ("-h", "--help"):
            res = self._run_exporter([opt])
            self.assertEqual(res.returncode, 0)
            self.assertIn("xay-export-public", res.stdout)
            self.assertIn("<目标目录>", res.stdout)

    def test_exclude_list_effective(self):
        """测试 .public-exclude 生效，受排除文件不进入公开副本。"""
        exclude_content = (
            "# 版权切片\n"
            "V2/web/fx/screens/genshin.gif\n"
            "V2/web/fx/screens/railgun.webp\n"
            "# 业务文档\n"
            "references/plans/arch-v4.md\n"
            "# 本地目录与数据库\n"
            ".claude/\n"
            "*.db\n"
            "scratchpad\n"
        )
        (self.repo_dir / ".public-exclude").write_text(exclude_content, encoding="utf-8")

        (self.repo_dir / "README.md").write_text("# Test Project\n", encoding="utf-8")
        src_dir = self.repo_dir / "src"
        src_dir.mkdir(parents=True)
        (src_dir / "app.py").write_text("print('hello')\n", encoding="utf-8")

        screens_dir = self.repo_dir / "V2" / "web" / "fx" / "screens"
        screens_dir.mkdir(parents=True)
        (screens_dir / "genshin.gif").write_bytes(b"GIF89a...")
        (screens_dir / "railgun.webp").write_bytes(b"RIFF...")

        ref_dir = self.repo_dir / "references" / "plans"
        ref_dir.mkdir(parents=True)
        (ref_dir / "arch-v4.md").write_text("# Arch v4\n", encoding="utf-8")

        claude_dir = self.repo_dir / ".claude"
        claude_dir.mkdir(parents=True)
        (claude_dir / "settings.json").write_text("{}", encoding="utf-8")

        (self.repo_dir / "local.db").write_text("db binary data", encoding="utf-8")
        (self.repo_dir / "scratchpad").write_text("my notes", encoding="utf-8")

        self._run_git(["add", "."], cwd=self.repo_dir)
        self._run_git(["commit", "-m", "init commit"], cwd=self.repo_dir)

        target = self.base_dir / "exported_clean"
        res = self._run_exporter([str(target)])

        self.assertEqual(res.returncode, 0, f"导出失败: {res.stderr}\n{res.stdout}")
        self.assertTrue((target / "README.md").is_file())
        self.assertTrue((target / "src" / "app.py").is_file())
        self.assertFalse((target / "V2" / "web" / "fx" / "screens" / "genshin.gif").exists())
        self.assertFalse((target / "V2" / "web" / "fx" / "screens" / "railgun.webp").exists())
        self.assertFalse((target / "references" / "plans" / "arch-v4.md").exists())
        self.assertFalse((target / ".claude").exists())
        self.assertFalse((target / "local.db").exists())
        self.assertFalse((target / "scratchpad").exists())

    def test_css_class_names_not_treated_as_keys(self):
        """测试前端 CSS 类名（sk-card, sk-card-action, sk-group）不被作为密钥误报。"""
        web_file = self.repo_dir / "page.html"
        web_file.write_text(
            '<div class="sk-card sk-card-action sk-group sk-header">content</div>\n',
            encoding="utf-8",
        )
        self._run_git(["add", "."], cwd=self.repo_dir)
        self._run_git(["commit", "-m", "add css classes"], cwd=self.repo_dir)

        target = self.base_dir / "exported_css"
        res = self._run_exporter([str(target)])
        self.assertEqual(res.returncode, 0, f"样式名不应被误报为密钥: {res.stderr}")
        self.assertTrue((target / "page.html").is_file())

    def test_public_allow_whitelist_skips_test_mock_keys(self):
        """测试 .public-allow 白名单跳过指定测试文件中的假密钥样本。"""
        allow_content = (
            "# 豁免测试样本\n"
            "tests/test_mock.py:sk-abcdefghijklmnop1234\n"
        )
        (self.repo_dir / ".public-allow").write_text(allow_content, encoding="utf-8")

        tests_dir = self.repo_dir / "tests"
        tests_dir.mkdir(parents=True)
        (tests_dir / "test_mock.py").write_text(
            "key = 'sk-abcdefghijklmnop1234'\n",
            encoding="utf-8",
        )
        self._run_git(["add", "."], cwd=self.repo_dir)
        self._run_git(["commit", "-m", "add allowed key in test"], cwd=self.repo_dir)

        target = self.base_dir / "exported_allow"
        res = self._run_exporter([str(target)])
        self.assertEqual(res.returncode, 0, f"白名单内的假密钥应被放行: {res.stderr}")
        self.assertTrue((target / "tests" / "test_mock.py").is_file())

    def test_dirty_content_blocked_and_summary_reported(self):
        """测试脏数据被拦截，输出 文件:行号 以及 密钥/本机路径/真实项目名 三类统计。"""
        sub_repo = self.base_dir / "repo_dirty"
        sub_repo.mkdir()
        self._run_git(["init"], cwd=sub_repo)

        (sub_repo / "paths.py").write_text("p = '/Users/mac/dir'\n", encoding="utf-8")
        (sub_repo / "proj.txt").write_text("Project: Flame, vendor: EWEADN, shop: shopify\n", encoding="utf-8")
        (sub_repo / "secret.py").write_text("token = 'sk-real1234567890abcdefsecret'\n", encoding="utf-8")
        (sub_repo / "contact.txt").write_text("Email: dev@company.org\n", encoding="utf-8")

        self._run_git(["add", "."], cwd=sub_repo)
        self._run_git(["commit", "-m", "dirty commit"], cwd=sub_repo)

        target = self.base_dir / "exported_dirty"
        res = self._run_exporter([str(target)], cwd=sub_repo)

        self.assertEqual(res.returncode, 1)
        # 验证包含 文件:行号 格式
        self.assertIn("paths.py:1", res.stderr)
        self.assertIn("secret.py:1", res.stderr)
        self.assertIn("proj.txt:1", res.stderr)
        # 验证提示语
        self.assertIn("改源文件或加进 .public-exclude 再导一次", res.stderr)
        # 验证末尾三类统计输出
        self.assertIn("统计：密钥", res.stderr)
        self.assertIn("本机路径", res.stderr)
        self.assertIn("真实项目名", res.stderr)

    def test_clean_export_creates_single_commit_no_remote(self):
        """测试干净导出时，目标目录初始化为一个 Git 提交，且无 remote。"""
        (self.repo_dir / "file.txt").write_text("Clean file content\n", encoding="utf-8")
        self._run_git(["add", "."], cwd=self.repo_dir)
        self._run_git(["commit", "-m", "commit 1"], cwd=self.repo_dir)

        target = self.base_dir / "exported_git"
        res = self._run_exporter([str(target)])
        self.assertEqual(res.returncode, 0)

        log_res = self._run_git(["log", "--oneline"], cwd=target)
        lines = log_res.stdout.strip().splitlines()
        self.assertEqual(len(lines), 1, "导出的副本应该只有 1 个初始提交（没有历史）")
        self.assertIn("xay 首次公开版本", lines[0])

        remote_res = self._run_git(["remote"], cwd=target)
        self.assertEqual(remote_res.stdout.strip(), "", "导出的副本不应关联任何 remote")

    def test_chinese_filenames_are_exported(self):
        """git 默认把中文文件名转义成带引号的八进制串，导出时不能因此漏掉这些文件。"""
        (self.repo_dir / "docs").mkdir()
        (self.repo_dir / "docs" / "员工手册.md").write_text("手册内容\n", encoding="utf-8")
        (self.repo_dir / "ascii.txt").write_text("plain\n", encoding="utf-8")
        self._run_git(["add", "."], cwd=self.repo_dir)
        self._run_git(["commit", "-m", "commit 1"], cwd=self.repo_dir)

        target = self.base_dir / "exported_cn"
        res = self._run_exporter([str(target)])
        self.assertEqual(res.returncode, 0, res.stdout + res.stderr)
        self.assertEqual((target / "docs" / "员工手册.md").read_text(encoding="utf-8"), "手册内容\n")
        self.assertTrue((target / "ascii.txt").exists())

    def test_chinese_text_cut_mid_character_is_still_scanned(self):
        """前 1024 字节正好把一个中文字截成两半时，不能把整份文档当二进制漏扫。"""
        body = "中" * 400 + "\n本机路径 /Users/mac/x\n"   # 第 1024 字节落在第 342 个字中间
        self.assertEqual(len(("中" * 400).encode("utf-8")) % 3, 0)
        self.assertNotEqual(1024 % 3, 0)
        (self.repo_dir / "doc.md").write_text(body, encoding="utf-8")
        self._run_git(["add", "."], cwd=self.repo_dir)
        self._run_git(["commit", "-m", "commit 1"], cwd=self.repo_dir)

        res = self._run_exporter([str(self.base_dir / "exported_cut")])
        self.assertEqual(res.returncode, 1, res.stdout + res.stderr)
        self.assertIn("doc.md:2", res.stdout + res.stderr)

    def test_target_dir_not_empty_rejected(self):
        """测试目标目录若已存在且非空，拒绝导出以避免覆盖已有数据。"""
        (self.repo_dir / "file.txt").write_text("content", encoding="utf-8")
        self._run_git(["add", "."], cwd=self.repo_dir)
        self._run_git(["commit", "-m", "init"], cwd=self.repo_dir)

        target = self.base_dir / "existing_dir"
        target.mkdir()
        (target / "important_data.txt").write_text("do not overwrite", encoding="utf-8")

        res = self._run_exporter([str(target)])
        self.assertEqual(res.returncode, 1)
        self.assertIn("目标目录已存在且非空", res.stderr)
        self.assertTrue((target / "important_data.txt").is_file())

    def test_exporter_and_rules_files_are_skipped(self):
        """测试导出脚本、测试脚本与规则文件自身写有检查词时不会被误报。"""
        sub_repo = self.base_dir / "repo_self_check"
        sub_repo.mkdir()
        self._run_git(["init"], cwd=sub_repo)
        scripts_dir = sub_repo / "scripts"
        scripts_dir.mkdir(parents=True)
        tests_dir = sub_repo / "tests"
        tests_dir.mkdir(parents=True)

        shutil.copy(SCRIPT_PATH, scripts_dir / "xay-export-public")
        shutil.copy(ROOT / "tests" / "test_export_public.py", tests_dir / "test_export_public.py")
        (sub_repo / ".public-exclude").write_text("# exclude\n", encoding="utf-8")
        (sub_repo / ".public-allow").write_text("# allow\n", encoding="utf-8")

        self._run_git(["add", "."], cwd=sub_repo)
        self._run_git(["commit", "-m", "add tools"], cwd=sub_repo)

        target = self.base_dir / "exported_self"
        res = self._run_exporter([str(target)], cwd=sub_repo)
        self.assertEqual(res.returncode, 0, f"工具文件自身不应触发违规: {res.stderr}")

    def test_real_repo_export_scan(self):
        """对当前真实仓库执行一次导出，捕获当前的命中输出供汇报使用。"""
        real_target = self.base_dir / "real_exported_repo"
        res = self._run_exporter([str(real_target)], cwd=ROOT)
        sys.stderr.write(f"\n--- REAL EXPORT RESULT (CODE: {res.returncode}) ---\n{res.stderr}\n{res.stdout}\n--- END ---\n")



if __name__ == "__main__":
    unittest.main()
