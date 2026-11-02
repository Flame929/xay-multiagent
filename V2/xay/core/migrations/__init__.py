"""按 meta.schema_version 依次执行 NNN_*.py（protocol §2）。已是最新版本时只读一次，不开写事务。"""
import importlib
import sqlite3

_001_init = importlib.import_module(__name__ + '.001_init')
_002_checkpoint = importlib.import_module(__name__ + '.002_checkpoint')

MIGRATIONS = ((1, _001_init), (2, _002_checkpoint))
LATEST = MIGRATIONS[-1][0]


def _version(conn):
    try:
        row = conn.execute("SELECT v FROM meta WHERE k='schema_version'").fetchone()
    except sqlite3.OperationalError:
        return 0
    try:
        return int(row[0]) if row else 0
    except (TypeError, ValueError):
        return 0


def migrate(conn):
    if _version(conn) >= LATEST:
        return
    conn.execute('BEGIN IMMEDIATE')
    try:
        current = _version(conn)  # 拿到写锁后再读一次：别的进程可能刚升级完
        for number, module in MIGRATIONS:
            if number > current:
                module.apply(conn)
        if current < LATEST:
            conn.execute("INSERT OR REPLACE INTO meta(k,v) VALUES ('schema_version', ?)", (str(LATEST),))
        conn.execute('COMMIT')
    except BaseException:
        if conn.in_transaction:
            conn.execute('ROLLBACK')
        raise
