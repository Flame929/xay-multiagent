"""002：现状改成"内存投影 + 存档点"。

旧版每写一条事件都把整个现状（一个 JSON）读出来、改完再整个写回 projection 表，现状越大越慢，
多个进程同时写时互相等锁直到超时。现在现状在每个进程的内存里按 seq 追事件，
每 200 条事件（以及 rebuild 时）在 checkpoint 表存一份；冷启动 = 最近的存档点 + 重放其后的事件。
instance：这本账的随机编号，进程内缓存用（路径 + 编号）区分不同的账本文件。
"""
import secrets


def apply(conn):
    conn.execute('''CREATE TABLE IF NOT EXISTS checkpoint(
      seq INTEGER PRIMARY KEY, reducer INTEGER NOT NULL, data TEXT NOT NULL)''')
    conn.execute('DROP TABLE IF EXISTS projection')
    conn.execute("INSERT OR IGNORE INTO meta(k,v) VALUES ('instance', ?)", (secrets.token_hex(8),))
