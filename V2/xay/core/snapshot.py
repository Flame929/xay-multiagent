import copy, json, posixpath, sqlite3, threading, time
from collections import OrderedDict
from . import mail
from .events import _reading, _row_event, _COLS
from .reducers import legacy_status, _legacy_from
from xay.office import duty as office_duty

RECENT = 20
RECENT_SCAN = 60          # 每次往回取多少条本对话的事件来挑有大白话的
RECENT_SCAN_MAX = 600     # 最多往回看这么多条（全是心跳之类时不至于扫整本账）


def pick_conv(state, ws=None):
    """没指定对话时的默认对话（§8 v1.5）：该 ws（不给 ws 就是全部）里最近一条事件所属的对话；没有对话返回 None。

    这是页面接口的约定；命令行不这么猜（§1），只有显式 --latest 时才用它。
    """
    cs = [c for c in state.get('convs', {}).values() if ws is None or c.get('ws') == ws]
    if not cs:
        return None
    return max(cs, key=lambda c: (c.get('active_ts', 0), c.get('active_seq', 0)))['id']


def build(conn, *, ws=None, conv=None, now=None):
    """§8 快照。conv=None 时用 pick_conv 选默认对话。"""
    with _reading(conn) as s:
        # 现状是缓存里的原件：这里只读，最后整份拷贝，调用方拿到的东西和缓存不共用任何对象
        return copy.deepcopy(_build(conn, s, ws=ws, conv=conv, now=now))


def _legacy_task(r):
    run_dir = (r.get('run_dir') or '').rstrip('/')
    rid = 'run:' + (posixpath.basename(run_dir) if run_dir else (r.get('run') or r.get('em') or ''))
    return {'id': rid, 'key': None, 'title': r.get('title') or '', 'owner': r.get('em'), 'dept': r.get('dept'),
            'status': legacy_status(r), 'deps': [], 'writes': [], 'progress': {}, 'result': r.get('result_head') or '',
            'assigned_reason': '', 'legacy': True, 'part': None,
            'check': {'state': 'none', 'round': 0, 'text': '', 'by': ''}, 'planned': []}


def _recent(conn, s, conv):
    """本对话最近 20 条有大白话的事件（按 seq 从旧到新）。"""
    if conv is None:
        return []
    from .humanize import text
    top = s.get('seq', 0); before = top + 1; out = []; scanned = 0
    while len(out) < RECENT and scanned < RECENT_SCAN_MAX:
        rows = conn.execute(f'SELECT {_COLS} FROM events WHERE conv=? AND seq<? ORDER BY seq DESC LIMIT ?',
                            (conv, before, RECENT_SCAN)).fetchall()
        if not rows:
            break
        for r in rows:
            e = _row_event(r); before = e['seq']; scanned += 1
            tx = text(e, conn, state=s)
            if tx is None:
                continue
            d = e['data']
            frm, to = d.get('from'), d.get('to')
            if e['type'] == 'bridge.letter':
                frm, to = _legacy_from(frm), d.get('to_worker') or to
            out.append({'seq': e['seq'], 'ts': e['ts'], 'type': e['type'], 'text': tx, 'from': frm, 'to': to})
            if len(out) >= RECENT:
                break
        if len(rows) < RECENT_SCAN:
            break
    out.reverse()
    return out


GM_RECENT = 5             # 总经理卡片上最多列几条他自己的动作
GM_RECENT_SCAN = 5000     # 最多往回看这么多条事件（按 seq 范围，不会扫整本账）


def _gm_recent(conn, s, conv):
    """总经理最近做的事（画布改版，2026-10-31）：本对话里 by=lead:* 的、有大白话的事件，最多 5 条，从新到旧。

    每条 {seq, ts, type, text, to?}；总经理看信（letter.delivered how=inbox，`xay2 wait` 写的）没有现成文案，
    这里补一句"看了 X 的信：主题"。拿不到的不编：没有动作就是空数组。
    """
    if conv is None:
        return []
    from .humanize import text, _who
    top = s.get('seq', 0)
    rows = conn.execute(f"SELECT {_COLS} FROM events WHERE conv=? AND by LIKE 'lead:%' AND seq>? ORDER BY seq DESC LIMIT 40",
                        (conv, top - GM_RECENT_SCAN)).fetchall()
    out = []
    for r in rows:
        e = _row_event(r)
        d = e['data'] if isinstance(e['data'], dict) else {}
        if e['type'] == 'letter.delivered':
            letter = s.get('letters', {}).get(e['subject']) or {}
            tx = f"看了 {_who(s, conv, letter.get('from'))} 的信：{letter.get('subject', '')}" if letter else None
        else:
            tx = text(e, conn, state=s)
        if not tx:
            continue
        item = {'seq': e['seq'], 'ts': e['ts'], 'type': e['type'], 'text': tx}
        to = d.get('owner') if e['type'] == 'task.assigned' else d.get('to') if e['type'] == 'letter.sent' else None
        if isinstance(to, str) and to:
            item['to'] = to
        if e['type'] == 'letter.sent' and (d.get('reply_to') or d.get('kind') == 'reply'):
            item['reply'] = True
        out.append(item)
        if len(out) >= GM_RECENT:
            break
    return out


def _history(s, conv):
    """计划历史（§4.2），每版另附大白话：summary = 这一版改了什么（"把「X」改给 Codex 2"），
    change_texts = 每处改动一句（和 changes 一一对应）。新建字典，不改缓存里的原件。"""
    from .humanize import plan_change_text, plan_summary
    out = []
    for h in s['plan_history'].get(conv, []):
        out.append(dict(h, summary=plan_summary(s, conv, h),
                        change_texts=[plan_change_text(s, conv, ch) for ch in h.get('changes') or []]))
    return out


def _budget_fn(s, now):
    """people[em].budget：{pct, dim?, scope?}（手上这件活和他今天的额度取用得多的，§3.5）。算法在管家的 budget 模块里，
    上限来自 reactions.json；那边出了任何问题都只回 {}，快照照常给。"""
    try:
        from xay.office import budget as bud
        from xay.office import reactions
        cfg = reactions.load(); index = bud.index_turns(s)
    except Exception:  # noqa: BLE001
        return lambda em: {}
    def fn(em):
        try:
            return bud.person_budget(s, em, now, cfg=cfg, index=index)
        except Exception:  # noqa: BLE001
            return {}
    return fn


# 四条额度线路（2026-10-31，真实额度）：线路 → (哪种 AI, 全名, 顶栏上的短名)；页面按这个顺序摆
QUOTA_LINES = {'codex-sub': ('codex', 'Codex · Plus', 'Plus'), 'codex-relay': ('codex', 'Codex · 中转站', '中转站'),
               'dsh': ('dsh', 'DeepSeek', 'DeepSeek'), 'agy': ('agy', 'Gemini', 'Gemini')}
QUOTA_ORDER = ('codex-sub', 'codex-relay', 'dsh', 'agy')
QUOTA_WINDOW_NAMES = {'5h': '5 小时', 'day': '今天', 'week': '本周', 'month': '本月'}
QUOTA_MAX_AGE = 1800      # 超过半小时没查到新读数：读数还给页面看（标 stale），但不再当真（status=unknown，不拦活）
QUOTA_TIGHT = 20          # 剩余低于这个算偏紧
STATION_SOURCE = 'Codex 运行时上报'
_QUOTA_CACHE = OrderedDict()   # 哪本账 → (扫到的 seq, {线路: {reading, error, account, accounts}})
_QUOTA_CACHE_MAX = 16
_QUOTA_LOCK = threading.Lock()  # 管家的网页服务、排程、额度线程是不同的线程


def _db_key(conn):
    try:
        path = next((r[2] for r in conn.execute('PRAGMA database_list').fetchall() if r[1] == 'main'), '')
    except sqlite3.Error:
        path = ''
    return path or 'mem:%d' % id(conn)


def _quota_latest(conn, top):
    """每条线路最新的读数（工位写的 Codex 额度 + 管家查到的四条线路，谁新用谁）。按账本增量扫，只看新事件。

    - 有窗口的记录 = 一次读数（最新的那条说了算）；
    - 没窗口、带 error 的 = 管家这次没查到（不清掉之前的读数，只记下原因）；
    - 没窗口、也没 error 的（工位在中转站上时 Codex 通知里两个窗口都是 null）= 什么都没说，跳过；
    - Gemini 另记最新的当前账号名（account）和各账号概况（accounts）。"""
    key = _db_key(conn)
    with _QUOTA_LOCK:
        scanned, latest = _QUOTA_CACHE.get(key, (0, {}))
    if scanned > top:
        # 别的线程已经扫到更后面了（多扫的只是更新的读数，照用）；账本真的换了一本（测试 / 示例）才从头扫
        row = conn.execute('SELECT MAX(seq) FROM events').fetchone()
        if (row[0] or 0) < scanned:
            scanned, latest = 0, {}
        else:
            return latest
    if top > scanned:
        latest = dict(latest)
        for seq, ts, by, sub, raw in conn.execute("SELECT seq, ts, by, subject, data FROM events WHERE seq>? AND seq<=? "
                                                  "AND type='quota.updated' ORDER BY seq", (scanned, top)).fetchall():
            try:
                d = json.loads(raw)
            except ValueError:
                continue
            if not isinstance(d, dict) or not isinstance(d.get('line'), str) or not isinstance(d.get('windows'), list):
                continue
            cur = dict(latest.get(d['line']) or {})
            if isinstance(d.get('accounts'), list):
                cur['accounts'] = d['accounts']
            if isinstance(d.get('account'), str):
                cur['account'] = d['account']
            if d['windows']:
                cur['reading'] = {'ts': ts, 'em': sub if str(by).startswith('station:') else None, 'data': d}
                cur['error'] = None
            elif d.get('error'):
                cur['error'] = {'ts': ts, 'text': str(d['error'])[:80]}
            else:
                continue
            latest[d['line']] = cur
        with _QUOTA_LOCK:
            _QUOTA_CACHE[key] = (top, latest)                  # 两个线程同时扫：谁后写都对，下次接着往后扫
            _QUOTA_CACHE.move_to_end(key)
            while len(_QUOTA_CACHE) > _QUOTA_CACHE_MAX:
                _QUOTA_CACHE.popitem(last=False)
    return latest


def _quota_status(left, stale):
    if left is None or stale:
        return 'unknown'
    return 'exhausted' if left <= 0 else 'tight' if left < QUOTA_TIGHT else 'ok'


def quota_lines(conn, now=None, top=None):
    """四条线路的额度（快照 quota；管家、工位做决定也用它）。每条：
    {line, agent, label, short, windows:[{key, name, mins, remaining_pct, resets_at}], remaining_pct（最紧的窗口）,
     status: ok|tight|exhausted|unknown, reached, source: live|none, source_label, fetched_at, stale, em,
     balance?/balance_usd?/warning?（中转站）, error?/error_at?（最近一次没查到）, account?/accounts?（Gemini）}。
    过了重置时间的窗口不知道现在用了多少，不列；读数超过半小时 stale=true、status=unknown。"""
    now = time.time() if now is None else now
    if top is None:
        top = conn.execute('SELECT COALESCE(MAX(seq), 0) FROM events').fetchone()[0]
    latest = _quota_latest(conn, top)
    out = []
    for line in list(QUOTA_ORDER) + sorted(x for x in latest if x not in QUOTA_ORDER):
        agent, label, short = QUOTA_LINES.get(line, ('codex', line, line))
        cur = latest.get(line) or {}
        q = {'line': line, 'agent': agent, 'label': label, 'short': short, 'windows': [], 'remaining_pct': None,
             'status': 'unknown', 'reached': None, 'source': 'none', 'source_label': None, 'fetched_at': None,
             'stale': False, 'em': None}
        rec = cur.get('reading')
        if rec and line == 'agy' and cur.get('account') and rec['data'].get('account') not in (None, cur['account']):
            rec = None                                   # 读数是换号之前那个账号的
            q['error'] = '刚换了账号，还没查到新账号的额度'
        if rec:
            d = rec['data']
            windows = []
            for w in d.get('windows') or []:
                if not isinstance(w, dict) or not isinstance(w.get('used_pct'), (int, float)):
                    continue
                resets = w.get('resets_at') if isinstance(w.get('resets_at'), (int, float)) else None
                if resets is not None and resets <= now:
                    continue
                k = str(w.get('key') or '')
                mins = int(w.get('mins') or 0)
                name = QUOTA_WINDOW_NAMES.get(k) or ('%d 小时' % (mins // 60) if mins >= 60 else '%d 分钟' % mins)
                windows.append({'key': k, 'name': name, 'mins': mins,
                                'remaining_pct': max(0, min(100, int(round(100 - float(w['used_pct']))))), 'resets_at': resets})
            left = min((w['remaining_pct'] for w in windows), default=None)
            stale = now - float(rec['ts']) > QUOTA_MAX_AGE
            q.update(windows=windows, remaining_pct=left, status=_quota_status(left, stale), reached=d.get('reached') or None,
                     source='live', source_label=d.get('source') or (STATION_SOURCE if rec['em'] else None),
                     fetched_at=rec['ts'], stale=stale, em=rec['em'])
            for k in ('balance', 'balance_usd', 'warning'):
                if d.get(k) is not None:
                    q[k] = d[k]
        err = cur.get('error')
        if err and (not rec or err['ts'] > rec['ts']):
            q['error'], q['error_at'] = err['text'], err['ts']
        if line == 'agy':
            q['account'] = cur.get('account') or (rec['data'].get('account') if rec else None)
            q['accounts'] = copy.deepcopy(cur.get('accounts') or [])
        out.append(q)
    return out


def _quota(conn, s, now):
    """快照 quota：四条线路（quota_lines）。出错只回 []。"""
    try:
        return quota_lines(conn, now, s.get('seq', 0))
    except sqlite3.Error:
        return []


def _count_tasks(task_list):
    non_canceled = [t for t in task_list if t.get('status') != 'canceled']
    return {
        'all': len(non_canceled),
        'done': sum(1 for t in non_canceled if t.get('status') in ('done', 'reported')),
        'working': sum(1 for t in non_canceled if t.get('status') == 'working'),
        'failed': sum(1 for t in non_canceled if t.get('status') == 'failed'),
    }


def _status_from_tasks(task_list, after_done=True):
    non_canceled = [t for t in task_list if t.get('status') != 'canceled']
    statuses = [t.get('status') for t in non_canceled]
    if any(s == 'needs_you' for s in statuses):
        return 'needs_you'
    if any(s in ('working', 'queued') for s in statuses):
        return 'working'
    if non_canceled and all(s in ('done', 'reported') for s in statuses):
        return 'done'
    if any(s == 'failed' for s in statuses):
        return 'failed'
    return 'ready' if after_done else 'planned'


def _calc_part_started_ended(p_tasks, s, status):
    turns = s.get('turns', {})
    task_ids = {t['id'] for t in p_tasks if isinstance(t, dict) and t.get('id')}
    started_list = []
    ended_list = []
    for t in p_tasks:
        if isinstance(t, dict):
            if t.get('started'):
                started_list.append(t['started'])
            if t.get('ended'):
                ended_list.append(t['ended'])
    for tr in turns.values():
        if tr.get('task') in task_ids:
            st = tr.get('started')
            if st:
                started_list.append(st)
            et = tr.get('ended')
            if et and tr.get('status') != 'working':
                ended_list.append(et)
    started = min(started_list) if started_list else None
    ended = max(ended_list) if (status == 'done' and ended_list) else None
    return started, ended


def build_parts(s, conv, tasks=None, depts=None):
    """计算当前对话的小任务列表（快照 parts 与员工接口 tree.parts 共用）：
    [{id, title, col, row, after, status, check, depts, count:{all, done, working, failed}, detail:{note, check, after_titles, started, ended}}]
    """
    if conv is None:
        return []
    c = s.get('convs', {}).get(conv, {})
    if tasks is None:
        tasks = [t for t in s.get('tasks', {}).values() if t.get('conv') == conv]
    if depts is None:
        depts = [d for d in s.get('depts', {}).values() if d.get('conv') == conv and not d.get('closed')]

    conv_parts = [p for p in s.get('parts', {}).values() if p.get('conv') == conv]

    # 旧对话：这个对话一个 part 事件都没有（整个对话一个小任务 id="all"，不按部门分）
    if not conv_parts:
        st = _status_from_tasks(tasks, after_done=True)
        cnt = _count_tasks(tasks)
        chk = {'kind': 'rules'}
        p_started, p_ended = _calc_part_started_ended(tasks, s, st)
        return [{
            'id': 'all',
            'title': c.get('title', ''),
            'col': 0,
            'row': 0,
            'after': [],
            'status': st,
            'check': chk,
            'depts': [],
            'count': cnt,
            'detail': {
                'note': '',
                'check': chk,
                'after_titles': [],
                'started': p_started,
                'ended': p_ended,
            },
            '_tasks': list(tasks),
        }]

    # 新对话
    key_to_id = {p['key']: p['id'] for p in conv_parts if p.get('key')}
    id_to_p = {p['id']: p for p in conv_parts}

    for p in conv_parts:
        norm_after = []
        for a in p.get('after') or []:
            target = key_to_id.get(a, a)
            if (target in id_to_p or target in key_to_id) and target != p['id'] and target not in norm_after:
                norm_after.append(target)
        p['_norm_after'] = norm_after

    col_map = {}
    def get_col(pid, visiting):
        if pid in col_map:
            return col_map[pid]
        if pid in visiting:
            return 0
        visiting.add(pid)
        p = id_to_p.get(pid)
        if not p:
            return 0
        afters = p.get('_norm_after', [])
        valid = [a for a in afters if a in id_to_p and a != pid]
        c = 0 if not valid else max(get_col(a, visiting) for a in valid) + 1
        visiting.remove(pid)
        col_map[pid] = c
        return c

    for pid in id_to_p:
        col_map[pid] = get_col(pid, set())

    by_col = {}
    for p in sorted(conv_parts, key=lambda x: (x.get('order', 0), x['id'])):
        c = col_map[p['id']]
        p['_col'] = c
        by_col.setdefault(c, []).append(p)

    for c, plist in by_col.items():
        plist.sort(key=lambda x: (x.get('order', 0), x['id']))
        for r, p in enumerate(plist):
            p['_row'] = r

    tasks_by_part = {p['id']: [] for p in conv_parts}
    loose_tasks = []
    for t in tasks:
        t_part = t.get('part')
        pid = key_to_id.get(t_part, t_part)
        if pid in tasks_by_part:
            tasks_by_part[pid].append(t)
        else:
            loose_tasks.append(t)

    depts_by_part = {p['id']: [] for p in conv_parts}
    loose_depts = []
    for d in depts:
        d_part = d.get('part')
        pid = key_to_id.get(d_part, d_part)
        if pid in depts_by_part:
            depts_by_part[pid].append(d['id'])
        else:
            loose_depts.append(d['id'])

    status_map = {}
    out_parts = []
    sorted_parts = sorted(conv_parts, key=lambda p: (p['_col'], p['_row']))
    for p in sorted_parts:
        pid = p['id']
        p_tasks = tasks_by_part[pid]
        after_ids = p.get('_norm_after', [])
        after_all_done = all(status_map.get(a) == 'done' for a in after_ids)
        st = _status_from_tasks(p_tasks, after_done=after_all_done)
        status_map[pid] = st
        cnt = _count_tasks(p_tasks)
        chk = copy.deepcopy(p.get('check') or {'kind': 'rules'})
        after_titles = [id_to_p[a].get('title', '') for a in after_ids if a in id_to_p]
        p_started, p_ended = _calc_part_started_ended(p_tasks, s, st)
        out_parts.append({
            'id': pid,
            'title': p.get('title', ''),
            'col': p['_col'],
            'row': p['_row'],
            'after': copy.deepcopy(after_ids),
            'status': st,
            'check': chk,
            'depts': depts_by_part[pid],
            'count': cnt,
            'detail': {
                'note': p.get('note', ''),
                'check': chk,
                'after_titles': after_titles,
                'started': p_started,
                'ended': p_ended,
            },
            '_tasks': p_tasks,
        })

    if loose_tasks:
        max_col = max((p['_col'] for p in conv_parts), default=-1)
        st = _status_from_tasks(loose_tasks, after_done=True)
        cnt = _count_tasks(loose_tasks)
        chk = {'kind': 'rules'}
        p_started, p_ended = _calc_part_started_ended(loose_tasks, s, st)
        out_parts.append({
            'id': '',
            'title': '零散的活',
            'col': max_col + 1,
            'row': 0,
            'after': [],
            'status': st,
            'check': chk,
            'depts': loose_depts,
            'count': cnt,
            'detail': {
                'note': '',
                'check': chk,
                'after_titles': [],
                'started': p_started,
                'ended': p_ended,
            },
            '_tasks': loose_tasks,
        })

    return out_parts


def _build(conn, s, *, ws=None, conv=None, now=None):
    now=time.time() if now is None else now
    budget_of=_budget_fn(s, now)
    duties=office_duty.calc_all_duties(conn, s, now=now)
    if conv is None:
        conv=pick_conv(s, ws)
    c=s['convs'].get(conv, {'id':conv,'title':'','lead_kind':'unknown','plan_version':0,'ws':ws})
    wsid=ws or c.get('ws')
    w=s['workspaces'].get(wsid, {'id':wsid,'name':wsid or '','path':''})
    legacy_runs=[r for r in s.get('legacy_runs',{}).values()]
    # 这一页的"需要你"：本对话的，加上没挂在任何对话上、但属于这个项目的（员工归项目，比如他今天的预算）。
    # 人显示成"等人答复"也只看这几条——页面上看得到的"需要你"和人的状态说的是同一件事
    raw_decisions=[d for d in s['decisions'].values() if d.get('conv')==conv or (d.get('conv') is None and d.get('ws')==wsid)]
    decisions=[dict(d, delegated=d.get('delegated'), files=copy.deepcopy(d.get('files', []))) for d in raw_decisions]
    people={}; former=[]
    for e in s['employees'].values():
        if e.get('ws')!=wsid: continue
        if e.get('former'):
            former.append({'key':e['key'],'label':e['label'],'agent':e['agent']}); continue
        state=e.get('state','idle')
        if any(d.get('em')==e['key'] and d.get('owner') in ('lead','user') for d in decisions): state='blocked'
        elif any(l.get('from')==e['key'] and l.get('kind')=='request' and l.get('status')=='open' for l in s['letters'].values()): state='blocked'
        now_title=s['tasks'].get(e.get('current_task'),{}).get('title','') if e.get('current_task') else ''
        # 旧系统镜像：他在旧系统里有正在跑的活 → 显示成在做这件活（§9）
        running=[r for r in legacy_runs if r.get('em')==e['key'] and legacy_status(r)=='working']
        if running:
            r=max(running, key=lambda x:(x.get('started') or 0, x.get('ts') or 0))
            state='working'; now_title=r.get('title') or now_title
        # 正常下班（空闲关了进程 / 被叫停）的人在页面上就是"在休息"；只有工位崩了才显示离线（§5 v1.8）
        rest=state=='idle' or (state=='offline' and e.get('offline_reason') in ('idle','stopped'))
        if rest and now-e.get('last_ts',now)>1800: display='sleeping'
        elif rest: display='idle'
        else: display=state
        people[e['key']]={'key':e['key'],'label':e['label'],'agent':e['agent'],'dept':(e.get('depts') or {}).get(conv),'state':state,'display_state':display,'now':now_title,'thought':e.get('thought') or '','queue':[q for q in e.get('queue',[]) if s['tasks'].get(q,{}).get('status')=='queued'],'context':dict(e.get('context',{})),'progress':dict(e.get('progress',{})),'budget':budget_of(e['key']),'amnesia':e.get('amnesia',False),'online':bool(e.get('online',False)),'duty':duties.get(e['key'],{'online_since':None,'today_s':0.0,'today_work_s':0.0,'total_work_s':0.0,'hired_at':e.get('hired_ts')})}
    former.sort(key=lambda x:s['employees'][x['key']].get('fired_ts',0))
    tasks=[]
    part_key_ids={p['key']:p['id'] for p in (s.get('parts') or {}).values() if p.get('conv')==conv and p.get('key') and p.get('key')!=p.get('id')}
    for t in s['tasks'].values():
        if t.get('conv')!=conv: continue
        item = {k:t.get(k) for k in ('id','key','title','owner','dept','status','deps','writes','progress','result','plain','assigned_reason','part','check')}
        item['planned'] = [s.get('text', '') if isinstance(s, dict) else str(s) for s in (t.get('planned_steps') or [])]
        if item.get('part') in part_key_ids: item['part']=part_key_ids[item['part']]   # 写的是小任务简称：换成编号，页面才对得上
        tasks.append(item)
    for r in sorted((r for r in legacy_runs if r.get('conv')==conv), key=lambda x:(x.get('started') or 0, x.get('ts') or 0)):
        tasks.append(_legacy_task(r))
    # 每封信附上送达情况（delivery：页面直接显示它的 text，M4 信件）；新建字典，不改缓存里的原件
    letters=[dict(l, delivery=mail.delivery(s, l)) for l in s['letters'].values() if l.get('conv')==conv]
    legacy_letters=sorted((l for l in s.get('legacy_letters',{}).values() if l.get('conv')==conv), key=lambda x:x.get('ts') or 0)
    depts=[]
    for d in s['depts'].values():
        if d.get('conv')!=conv or d.get('closed'): continue
        members=[m for m in d.get('members',[]) if m in people]
        depts.append({'id':d['id'],'name':d['name'],'mission':d['mission'],'head':d['head'],'members':members,'counts':{'members':len(members),'working':sum(people[m]['state']=='working' for m in members),'open_requests':sum(1 for l in letters if l.get('to')==d['id'] and l.get('kind')=='request' and l.get('status')=='open')},'part':part_key_ids.get(d.get('part'),d.get('part'))})
    raw_parts = build_parts(s, conv, tasks=tasks, depts=depts)
    parts = [{k: v for k, v in p.items() if not k.startswith('_')} for p in raw_parts]
    proposals=[p for p in s['proposals'].values() if p.get('conv')==conv and p.get('status')=='open']
    top=s.get('seq',0)
    recent=_recent(conn, s, conv)
    return {'v':1,'seq':top,'now':now,'ws':w,'conversation':{'id':c.get('id'),'title':c.get('title',''),'lead_kind':c.get('lead_kind','unknown'),'plan_version':c.get('plan_version',0)},'gm':{'agent':c.get('lead_kind','unknown'),'state':'working' if any(t.get('status')=='working' for t in tasks) else 'idle','recent':_gm_recent(conn, s, conv)},'people':people,'depts':depts,'tasks':tasks,'parts':parts,'plan':{'version':c.get('plan_version',0),'history':_history(s, conv)},'letters':letters+legacy_letters,'decisions':decisions,'proposals':proposals,'signals':s.get('signals',[])[-20:],'former':former,'halt':copy.deepcopy(s.get('halt')),'quota':_quota(conn, s, now),'recent':recent}
