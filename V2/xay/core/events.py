"""账本写入与现状读取（protocol §0、§2、§3）。

现状怎么来（v1.4 起）：
- 现状是 events 的投影，由 reducer 按 seq 顺序算出。每个进程在内存里为每本账（路径 + 账本编号）缓存一份
  {现状, 已算到的 seq}。读和写之前都先"追上"：把 seq 大于缓存的事件（包括别的进程写的）依次 apply 进去。
- 写：BEGIN IMMEDIATE 拿写锁 → 追上 → 在最新现状上查权限 → 写事件 → apply 进缓存 → COMMIT。
  不再每条事件都整份写回现状。出错时回滚事务，并把被改过的缓存作废（下次从存档点重建），保证缓存 = 账本。
- 存档点：seq 是 200 的倍数时，在同一个事务里把整份现状写进 checkpoint 表；rebuild 也写一份。
  冷启动 = 最近的存档点 + 重放其后的事件。
"""
import json, os, random, sqlite3, threading, time
from collections import OrderedDict
from contextlib import contextmanager

from .errors import PermissionError
from .reducers import apply as _apply, empty_state, reduce, REDUCER_VERSION  # noqa: F401  (reduce 供旧调用方使用)

CHECKPOINT_EVERY = 200
LOCK_RETRIES = 5
_MAX_CACHES = 32

PERMS={
 'conv.begun':('lead','bridge'),'conv.aliased':('lead','bridge'),'conv.titled':('lead','bridge'),'conv.ended':('lead',),
 'dept.opened':('lead','bridge'),'dept.closed':('lead','bridge'),'employee.hired':('lead','office','bridge'),'employee.joined':('lead','office','bridge'),
 'employee.session':('station','office'),'employee.online':('station','office'),'employee.offline':('station','office'),'employee.fired':('lead','user'),
 'station.heartbeat':('station','office'),'desk.synced':('station','office'),'turn.started':('station','office'),'turn.ended':('station','office'),
 'context.updated':('station',),'context.compacted':('station',),'recall.checked':('station',),'task.added':('lead',),'task.changed':('lead',),'task.removed':('lead',),
 'task.assigned':('lead','office'),'task.ready':('office',),'task.queued':('office',),'task.progress':('station','employee'),'task.reported':('station','employee'),
 'task.landed':('station','office'),'task.kept':('station','office'),'task.failed':('office','lead'),'task.accepted':('lead','user'),'task.canceled':('lead','user','office'),
 'desk.removed':('user',),'proposal.made':('lead','employee'),'proposal.decided':('lead','user'),'context.compact_requested':('office','rules','lead'),
 'notes.updated':('station','employee'),'letter.sent':('lead','user','employee','office','rules'),'letter.delivered':('office','station'),
 'letter.answered':('lead','user','employee'),'decision.opened':('office','rules','station'),'decision.resolved':('lead','user'),
 'signal.raised':('office','rules','station'),'budget.warned':('office','rules'),'budget.stopped':('office','rules'),'control.requested':('lead','user','rules'),
 # 桥已于 2026-10-31 删除；保留 bridge.* 权限只为重放旧账，之后不再有新的 bridge 事件。
 'control.done':('office','station'),'bridge.run':('bridge',),'bridge.letter':('bridge',)
}

# M2 工位（V2/xay/station）补的权限，只追加、不改上面的表（协议 §3 由负责人同步）：
# 工位写自己员工的「最近一句思考」、自己任务的 task.failed、自己员工的 context.compact_requested（身份范围照旧检查）
PERMS['employee.thought'] = ('station',)
PERMS['task.failed'] = PERMS['task.failed'] + ('station',)
PERMS['context.compact_requested'] = PERMS['context.compact_requested'] + ('station',)
# 协议 1.8：管家替领了活却没分部门的员工开本对话的"项目组"
PERMS['dept.opened'] = PERMS['dept.opened'] + ('office',)
# M4 管家自动化（协议待定条目，2026-10-30）：
# - 规则监工请员工下一轮重新复述（压缩后复述不出来，反应表"再给一次笔记"）
# - 规则监工可以关掉**它自己打开的**"需要你"（比如沉默的那一轮已经结束了），以及把合并冲突自动"再合并一次"（只限 conflict + merge），见 _permission
PERMS['recall.requested'] = ('office', 'rules')
PERMS['decision.resolved'] = PERMS['decision.resolved'] + ('rules',)

# M4 信件（V2/xay/core/mail.py）补的权限，同样只追加：负责人的 `xay2 wait` 把写给总经理的信打印出来时，
# 记一条 letter.delivered{how:"inbox"}（"总经理已看到"）；身份范围照旧（只能写本对话的信）
PERMS['letter.delivered'] = PERMS['letter.delivered'] + ('lead',)

# 详情栏第 3 版（协议待定条目，2026-10-30）：工位把 Codex 的额度（app-server 的 account/rateLimits/updated）
# 写成 quota.updated{line, windows, reached?}，subject 是自己的员工。reducer 不处理（不进现状表），快照 quota 直接取最新一条
PERMS['quota.updated'] = ('station',)
# 四条线路真实额度（协议待定条目，2026-10-31）：管家定时用各家官方的查法查 Plus / 中转站 / DeepSeek / Gemini，
# 写 quota.updated{line, windows, source, …}（by=office，不带 subject）；reducer 照旧不处理
PERMS['quota.updated'] = PERMS['quota.updated'] + ('office',)

# 详情栏记忆数据 + 直接改笔记接口：notes.updated 追加 user 和 lead
PERMS['notes.updated'] = PERMS['notes.updated'] + ('user', 'lead')

# 1.23：小任务（part.*，by=lead；管家做完小任务自动关部门 dept.closed 加 office）
PERMS['part.added'] = ('lead',)
PERMS['part.changed'] = ('lead',)
PERMS['part.removed'] = ('lead',)
PERMS['dept.closed'] = PERMS['dept.closed'] + ('office',)

# 1.24：监工审查流程（check.result，by 为 lead/office/station；管家开审查活 task.added 允许 office）
PERMS['check.result'] = ('lead', 'office', 'station')
PERMS['task.added'] = PERMS['task.added'] + ('office',)

# 1.31：交出决定（decision.delegated，by 为 user/lead）
PERMS['decision.delegated'] = ('user', 'lead')

# 1.32：员工自主计划（task.planned，by 为 station，也可以 employee）
PERMS['task.planned'] = ('station', 'employee')

# 负责人中途交接（conversation.lead_changed，by 为 lead）
PERMS['conversation.lead_changed'] = ('lead',)

# 员工干到一半直接派活（tk_9ed5b54f / arch-v11 §5）：工位替员工写 task.added
PERMS['task.added'] = PERMS['task.added'] + ('station',)

_COLS = 'seq,ts,v,type,ws,conv,subject,by,data'


def _role(by):
    if by == 'station' or by.startswith('station:'): return 'station'
    if by.startswith('lead:'): return 'lead'
    if by.startswith('employee:'): return 'employee'
    return by


def _permission(event, state):
    typ, by, conv, sub, d = event['type'], event['by'], event.get('conv'), event.get('subject'), event.get('data') or {}
    role=_role(by); allowed=PERMS.get(typ)
    if allowed is None:
        if role not in ('lead','office','rules','station'): raise PermissionError(f'没有权限写入事件：{typ}')
    elif role not in allowed: raise PermissionError(f'没有权限写入事件：{typ}')
    if role=='lead' and conv and by not in ('lead', f'lead:{conv}'): raise PermissionError('负责人只能写当前对话')
    if typ == 'task.added' and role == 'station':
        if not (d.get('by_task') and d.get('by_em')):
            raise PermissionError('工位只能替员工派活（必须包含 by_task 与 by_em）')
    if role in ('station','employee'):
        em = by.split(':', 1)[1] if ':' in by else (d.get('by_em') or d.get('em'))
        owner_ok=False
        if sub==em: owner_ok=True
        if d.get('em')==em: owner_ok=True
        if sub in state.get('tasks',{}) and state['tasks'][sub].get('owner')==em: owner_ok=True
        if typ == 'check.result' and sub in state.get('tasks', {}) and state['tasks'][sub].get('owner') != em:
            owner_ok = True
        if typ == 'task.added' and role == 'station' and d.get('by_task') and d.get('by_em') and (not em or d.get('by_em') == em):
            owner_ok = True
        if typ in ('letter.sent','proposal.made') and d.get('from')==em: owner_ok=True
        if typ in ('employee.session','employee.online','employee.offline','station.heartbeat','context.updated','context.compacted','context.compact_requested','recall.checked','notes.updated'): owner_ok=(sub==em)
        if not owner_ok: raise PermissionError('员工只能写自己的事件')
        if role=='employee' and typ not in ('letter.sent','proposal.made','task.progress','task.reported','notes.updated','task.planned'): raise PermissionError('执行者只能汇报自己的工作')
    if typ=='conv.begun' and role=='lead' and conv != sub: raise ValueError('conv.begun 的 subject 必须等于 conv')
    if role=='bridge' and not (typ.startswith('bridge.') or typ=='conv.begun' or typ=='employee.hired' or typ=='employee.joined' or typ in ('dept.opened','dept.closed') or typ=='conv.aliased' or typ=='conv.titled'):
        raise PermissionError('临时桥只能写桥接事件')
    if role=='lead' and typ not in ('employee.hired','employee.joined','employee.fired') and conv is None:
        raise PermissionError('负责人写入事件必须指定对话')
    if typ in ('task.added','task.changed','task.removed','task.assigned','task.ready','task.queued','task.progress','task.reported','task.landed','task.kept','task.failed','task.accepted','task.canceled','check.result','task.planned') and sub not in state.get('tasks',{}) and typ not in ('task.added',):
        raise ValueError('任务不存在')
    if typ in ('employee.session','employee.online','employee.offline','employee.fired','station.heartbeat','context.updated','context.compact_requested','context.compacted','notes.updated','recall.checked','recall.requested') and sub not in state.get('employees',{}) and typ not in ('employee.fired',):
        raise ValueError('员工不存在')
    if typ in ('part.changed', 'part.removed') and sub not in state.get('parts', {}):
        raise ValueError('小任务不存在')
    if typ == 'part.removed':
        p = state.get('parts', {}).get(sub)
        p_key = p.get('key') if p else None
        active = [
            tk for tk, t in state.get('tasks', {}).items()
            if (t.get('part') == sub or (p_key and t.get('part') == p_key)) and t.get('status') != 'canceled'
        ]
        if active:
            raise ValueError(f'小任务「{p.get("title") or sub}」里还有未撤回的任务，不能删除')
    if typ in ('decision.resolved', 'decision.delegated'):
        dec = state.get('decisions', {}).get(sub)
        if not dec: raise ValueError('这件需要你的事已经处理过了')
    if typ == 'decision.delegated':
        if d.get('to') not in ('rules', 'lead'):
            raise ValueError('to 只能是 rules 或 lead')
    if typ == 'decision.resolved' and role == 'rules':
        dec = state.get('decisions', {}).get(sub)
        if not (dec.get('opened_by') == 'rules' or (dec.get('kind') == 'conflict' and d.get('option') == 'merge') or dec.get('delegated') == 'rules'):
            raise PermissionError('规则监工只能关掉自己打开的需要你，或者把合并冲突自动再合并一次')
    if typ == 'conversation.lead_changed':
        if not d.get('to'):
            raise ValueError('新负责人不能为空')
        if sub and sub not in state.get('convs', {}):
            raise ValueError('对话不存在')


# ---------------------------------------------------------------- 进程内现状缓存

class _Cache:
    __slots__ = ('state', 'lock')

    def __init__(self):
        self.state = None          # None = 还没加载 / 已作废，下次从存档点重建
        self.lock = threading.RLock()


_REGISTRY = OrderedDict()
_REGISTRY_LOCK = threading.Lock()


def _cache_for(conn):
    cache = getattr(conn, '_xay_cache', None)
    if cache is not None:
        return cache
    key = getattr(conn, '_xay_key', None)
    if key is None:
        from .db import cache_key
        key = cache_key(conn)
        try: conn._xay_key = key
        except AttributeError: pass
    if key[0] == 'mem':
        # 内存账本只属于这一个连接：缓存挂在连接上，连接关了就跟着没了
        cache = _Cache()
        try: conn._xay_cache = cache
        except AttributeError: pass   # 不是 open_db 开的连接：每次都从头算（慢，但对）
        return cache
    with _REGISTRY_LOCK:
        cache = _REGISTRY.get(key)
        if cache is None:
            cache = _REGISTRY[key] = _Cache()
            while len(_REGISTRY) > _MAX_CACHES:
                _REGISTRY.popitem(last=False)   # 丢掉最久没用的；需要时从存档点重建
        else:
            _REGISTRY.move_to_end(key)
        return cache


def _row_event(r):
    e = dict(r); e['data'] = json.loads(e['data']); return e


def _apply_committed(state, e):
    """重放一条已经入账的事件。入账时 reducer 已经成功过；万一新代码在这里出错，记日志后继续（结果仍是确定的）。"""
    try:
        _apply(state, e)
    except Exception as exc:  # noqa: BLE001
        _log(f"重放事件 #{e.get('seq')}（{e.get('type')}）时 reducer 出错，已跳过：{exc}")
    state['seq'] = e['seq']


def _cold_load(conn):
    try:
        row = conn.execute('SELECT seq, reducer, data FROM checkpoint ORDER BY seq DESC LIMIT 1').fetchone()
    except sqlite3.OperationalError:
        row = None
    if row is not None and row['reducer'] == REDUCER_VERSION:
        state = json.loads(row['data'])
        if state.get('seq') == row['seq']:
            return state
    return empty_state()


def _catch_up(conn, cache):
    """调用方已持有 cache.lock。把缓存追到账本当前最新的 seq。"""
    if cache.state is not None:
        top = conn.execute('SELECT COALESCE(MAX(seq),0) FROM events').fetchone()[0]
        if top < cache.state.get('seq', 0):   # 账本文件被换掉了（比如从备份恢复）：缓存作废
            cache.state = None
    if cache.state is None:
        cache.state = _cold_load(conn)
    state = cache.state
    while True:
        rows = conn.execute(f'SELECT {_COLS} FROM events WHERE seq>? ORDER BY seq LIMIT 5000', (state['seq'],)).fetchall()
        for r in rows:
            _apply_committed(state, _row_event(r))
        if len(rows) < 5000:
            return state


@contextmanager
def _reading(conn):
    """内部用：追上后把缓存里的现状（不拷贝）借给调用方读，期间持锁。调用方不能改它。"""
    cache = _cache_for(conn)
    with cache.lock:
        yield _catch_up(conn, cache)


def _write_checkpoint(conn, state):
    conn.execute('INSERT OR REPLACE INTO checkpoint(seq, reducer, data) VALUES(?,?,?)',
                 (state['seq'], REDUCER_VERSION, json.dumps(state, ensure_ascii=False, separators=(',', ':'))))
    conn.execute('DELETE FROM checkpoint WHERE seq<?', (state['seq'],))


def _is_lock_error(exc):
    msg = str(exc).lower()
    return isinstance(exc, sqlite3.OperationalError) and ('locked' in msg or 'busy' in msg)


def _rollback(conn):
    try:
        if conn.in_transaction:
            conn.execute('ROLLBACK')
    except sqlite3.Error:
        pass


def _with_write_lock(conn, fn):
    """BEGIN IMMEDIATE 后执行 fn()；遇到"数据库被锁"（busy_timeout 到期）整笔回滚后退避重试，最多 5 次。"""
    for attempt in range(LOCK_RETRIES):
        try:
            conn.execute('BEGIN IMMEDIATE')
            try:
                return fn()
            except BaseException:
                _rollback(conn)
                raise
        except sqlite3.OperationalError as exc:
            if not _is_lock_error(exc) or attempt == LOCK_RETRIES - 1:
                raise
            time.sleep(random.uniform(0.5, 1.5) * 0.05 * (2 ** attempt))


def append(conn, type, by, data, *, ws=None, conv=None, subject=None, dedup=None, ts=None):
    if not isinstance(type,str) or not type: raise ValueError('事件类型不合法')
    if not isinstance(by,str) or not by: raise ValueError('事件写入者不合法')
    if not isinstance(data,dict): raise ValueError('事件 data 必须是对象')
    if ts is None: ts=time.time()
    if not isinstance(ts,(int,float)) or isinstance(ts,bool): raise ValueError('事件时间不合法')
    ts=float(ts)
    text=json.dumps(data,ensure_ascii=False,separators=(',',':'))
    cache=_cache_for(conn)

    def write():
        # 先拿写锁再追现状：权限检查和 reducer 都基于最新现状（含别的进程刚写的）
        with cache.lock:
            touched=False
            try:
                state=_catch_up(conn,cache)
                event={'type':type,'by':by,'data':json.loads(text),'ws':ws,'conv':conv,'subject':subject,'ts':ts,'v':1}
                _permission(event,state)
                if dedup is not None:
                    row=conn.execute('SELECT seq FROM events WHERE dedup=?',(dedup,)).fetchone()
                    if row: conn.execute('COMMIT'); return row['seq']
                cur=conn.execute('INSERT INTO events(ts,v,type,ws,conv,subject,by,data,dedup) VALUES(?,?,?,?,?,?,?,?,?)',(ts,1,type,ws,conv,subject,by,text,dedup))
                seq=cur.lastrowid; event['seq']=seq
                touched=True
                _apply(state,event)   # reducer 抛错 → 整笔回滚，缓存作废
                state['seq']=seq
                if seq % CHECKPOINT_EVERY == 0: _write_checkpoint(conn,state)
                conn.execute('COMMIT')
                return seq
            except BaseException:
                if touched: cache.state=None
                raise

    return _with_write_lock(conn, write)


def rebuild(conn):
    """清空投影，按 seq 从头重放全部事件，并写一份新的存档点（§0.3）。"""
    cache=_cache_for(conn)

    def work():
        with cache.lock:
            try:
                state=empty_state(); last=0
                while True:
                    rows=conn.execute(f'SELECT {_COLS} FROM events WHERE seq>? ORDER BY seq LIMIT 5000',(last,)).fetchall()
                    for r in rows:
                        _apply_committed(state,_row_event(r)); last=r['seq']
                    if len(rows)<5000: break
                conn.execute('DELETE FROM checkpoint')
                if state['seq']: _write_checkpoint(conn,state)
                conn.execute('COMMIT')
                cache.state=state
            except BaseException:
                cache.state=None
                raise

    _with_write_lock(conn, work)


def _rows(conn, seq, ws, conv, limit):
    q=f'SELECT {_COLS} FROM events WHERE seq>?'; args=[seq]
    if ws is not None: q+=' AND ws=?'; args.append(ws)
    if conv is not None: q+=' AND conv=?'; args.append(conv)
    q+=' ORDER BY seq LIMIT ?'; args.append(limit)
    return conn.execute(q,args).fetchall()

def since(conn, seq, *, ws=None, conv=None, limit=500):
    return [_row_event(r) for r in _rows(conn,seq,ws,conv,limit)]

def max_seq(conn):
    row=conn.execute('SELECT COALESCE(MAX(seq),0) n FROM events').fetchone(); return row['n']

def load_state(conn):
    """当前现状的一份独立拷贝（调用方随便改都不影响账本缓存）。"""
    with _reading(conn) as state:
        return json.loads(json.dumps(state, ensure_ascii=False))


def _log(line):
    try:
        home = os.environ.get('XAY2_HOME') or os.path.join(os.path.expanduser('~'), '.xay', 'v2')
        path = os.path.join(home, 'logs', 'core.log')
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, 'a', encoding='utf-8') as f:
            f.write(time.strftime('%Y-%m-%d %H:%M:%S ') + line + '\n')
    except OSError:
        pass
