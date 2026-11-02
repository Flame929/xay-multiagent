import sqlite3
import os
import tempfile
import unittest
from datetime import date
from pathlib import Path

from xay.core import append, open_db
from xay.core.backup import backup_daily


class BackupTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.home = self.root / 'home'
        self.old_home = os.environ.get('XAY2_HOME')
        os.environ['XAY2_HOME'] = str(self.home)
        self.addCleanup(self._restore_home)
        self.db_path = self.home / 'xay.db'
        self.conn = open_db()
        append(self.conn, 'conv.begun', 'lead:cv_backup',
               {'title': '备份测试', 'lead_kind': 'claude', 'aliases': [], 'cwd': str(self.root)},
               ws='ws_backup', conv='cv_backup', subject='cv_backup')

    def tearDown(self):
        self.conn.close()

    def _restore_home(self):
        if self.old_home is None:
            os.environ.pop('XAY2_HOME', None)
        else:
            os.environ['XAY2_HOME'] = self.old_home

    def test_first_backup_and_same_day_skip(self):
        first = backup_daily(self.db_path, today='20261031')
        self.assertEqual(first, self.home / 'backups' / 'xay-20261031.db')
        self.assertTrue(first.exists())
        self.assertIsNone(backup_daily(self.db_path, today='20261031'))

    def test_keep_removes_oldest_matching_backup_only(self):
        for day in ('20261028', '20261029', '20261030'):
            self.assertIsNotNone(backup_daily(self.db_path, today=day, keep=2))
        backups = sorted(p.name for p in (self.home / 'backups').iterdir())
        self.assertEqual(backups, ['xay-20261029.db', 'xay-20261030.db'])
        other = self.home / 'backups' / 'xay-not-a-date.db'
        other.write_text('保留', encoding='utf-8')
        backup_daily(self.db_path, today='20261031', keep=2)
        self.assertTrue(other.exists())
        self.assertEqual(sorted(p.name for p in (self.home / 'backups').iterdir()),
                         ['xay-20261030.db', 'xay-20261031.db', 'xay-not-a-date.db'])

    def test_backup_opens_and_has_same_event_count(self):
        target = backup_daily(self.db_path, today=date(2026, 10, 31))
        original_count = self.conn.execute('SELECT COUNT(*) FROM events').fetchone()[0]
        copy = sqlite3.connect(str(target))
        try:
            copied_count = copy.execute('SELECT COUNT(*) FROM events').fetchone()[0]
        finally:
            copy.close()
        self.assertEqual(copied_count, original_count)
