"""001：账本基础表（events + meta）。在 migrate() 开的写事务里执行，不能用 executescript（它会先提交）。"""

STATEMENTS = (
    '''CREATE TABLE IF NOT EXISTS events(
      seq INTEGER PRIMARY KEY AUTOINCREMENT, ts REAL NOT NULL, v INTEGER NOT NULL DEFAULT 1,
      type TEXT NOT NULL, ws TEXT, conv TEXT, subject TEXT, by TEXT NOT NULL,
      data TEXT NOT NULL, dedup TEXT UNIQUE)''',
    'CREATE TABLE IF NOT EXISTS meta(k TEXT PRIMARY KEY, v TEXT)',
)


def apply(conn):
    for sql in STATEMENTS:
        conn.execute(sql)
