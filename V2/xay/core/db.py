import os, secrets, sqlite3
from pathlib import Path
from .migrations import migrate

BUSY_TIMEOUT_MS = 15000


class Ledger(sqlite3.Connection):
    """open_db 返回的连接。和 sqlite3.Connection 完全一样，只是能挂上进程内缓存要用的标记（_xay_key）。"""


def open_db(path=None):
    if path is None:
        home = Path(os.environ.get('XAY2_HOME', Path.home()/'.xay'/'v2'))
        home.mkdir(parents=True, exist_ok=True)
        path = str(home/'xay.db')
    elif path != ':memory:':
        Path(path).parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path, isolation_level=None, timeout=BUSY_TIMEOUT_MS / 1000, factory=Ledger)
    conn.row_factory = sqlite3.Row
    conn.execute('PRAGMA journal_mode=WAL')
    conn.execute(f'PRAGMA busy_timeout={BUSY_TIMEOUT_MS}')
    conn.execute('PRAGMA foreign_keys=ON')
    migrate(conn)
    conn._xay_key = cache_key(conn)
    return conn


def cache_key(conn):
    """进程内现状缓存的键：文件账本 = (真实路径, 账本编号)；内存账本每个连接各一份。"""
    files = [r[2] for r in conn.execute('PRAGMA database_list').fetchall() if r[1] == 'main']
    path = files[0] if files else ''
    if not path:
        return ('mem', secrets.token_hex(8))
    row = conn.execute("SELECT v FROM meta WHERE k='instance'").fetchone()
    return ('file', os.path.realpath(path), row[0] if row else '')
