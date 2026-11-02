"""信件送给谁、怎么交代给员工、页面上怎么说（protocol §4.5，M4 信件）。纯函数：只读现状，不碰账本、不读时钟。

送给谁（`recipient`）：
- 写给员工（em_…）：就是他（已离职的送不到）。
- 写给 gm：总经理，负责人用 `xay2 wait` 收。写给 user：页面上直接显示，不用送。
- 写给部门：部门负责人。head 不是 rules（Claude 负责人）→ 现在就是本对话的负责人 gm；
  head=rules（规则监工）→ 部门里跟这件事最相关的成员，按顺序挑：
    1) 正在做（或这一轮就要开工做）本部门任务的；2) 正在做别的活的；3) 最近干过活的；4) 名单里靠前的。
  写信的人自己不算；部门里没有别人、部门已关 → 交给总经理。
  选人是按"送的那一刻"的现状算的：信一直没送到时，谁先成了最相关的人就送给谁；送到以后记在
  `delivered_to` 上不再变。

谁来送：员工的信由他的工位送（开工时放进这一轮输入的开头 = next_turn；Codex 干活中途当场插话 = steer）；
总经理的信在 `xay2 wait` 打印出来时记成已送到（inbox）。每封信只记一次（幂等键 `delivered:<lt>`）。
"""
from .reducers import MEMORY_TURNS, dept_key

MAX_PER_DELIVERY = 10      # 一次最多交代这么多封，其余的下一次
BODY_CLIP = 1200           # 每封信正文最多放这么多字进对话
AI_NAMES = {'codex': 'Codex', 'dsh': 'DeepSeek', 'agy': 'Gemini'}
KIND_TEXT = {'request': '要你回复', 'decision': '定下来的结论', 'escalation': '上报', 'report': '汇报',
             'notice': '通知', 'system': '系统通知', 'tell': '', 'reply': ''}
DELIVERED_HOW = {'steer': '已当场告诉 %s', 'next_turn': '已交给 %s（开工时一起交代）', 'inbox': '已送到 %s'}
REPLY_HINT = '回信：在回复里单独写一行【回信 lt_…】你的回复（lt_… 换成上面那封信的编号），或者运行 xay2 reply lt_… "你的回复"。'


def _emps(state):
    return (state or {}).get('employees', {})


def _dept(state, conv, dept_id):
    if not dept_id:
        return None
    return (state or {}).get('depts', {}).get(dept_key(conv, dept_id))


def who(state, conv, x):
    """from/to → 给人看的名字：员工显示名、负责人、用户、部门名；认不出原样返回。"""
    if not x:
        return ''
    if x == 'gm':
        return '负责人'
    if x == 'user':
        return '用户'
    if x in ('office', 'rules'):
        return '监工'          # 后台按规则自动写信、下指令的：页面上叫监工
    e = _emps(state).get(x)
    if e:
        return e.get('label') or x
    d = _dept(state, conv, x)
    if d:
        return d.get('name') or x
    return x


def _last_work(state, members):
    """每个成员最近一次干活（不算整理记忆）的时间：这一轮开始或结束，取晚的。"""
    out = {m: 0.0 for m in members}
    for tr in (state or {}).get('turns', {}).values():
        em = tr.get('em')
        if em in out and tr.get('kind') not in MEMORY_TURNS:
            ts = max(tr.get('started') or 0, tr.get('ended') or 0)
            if ts > out[em]:
                out[em] = ts
    return out


def recipient(state, letter, starting=None):
    """这封信现在该送给谁：员工编号 / 'gm' / 'user' / 'office' / None（找不到收信人）。
    写给管家的（员工回管家自动写的信）和写给用户的一样不用投递，页面上直接显示。

    starting=(em, tk)：这个员工这一轮马上就要开工做 tk（工位开工前问"有没有我的信"时用），算他正在做 tk。
    """
    to = letter.get('to')
    if to in ('gm', 'user'):
        return to
    if to in ('office', 'rules'):
        return 'office'
    emps = _emps(state)
    if isinstance(to, str) and to in emps:
        return to
    d = _dept(state, letter.get('conv'), to)
    if d is None:
        return None
    if d.get('closed') or d.get('head') != 'rules':
        return 'gm'
    sender = letter.get('from')
    members = [m for m in d.get('members', []) if m != sender and m in emps and not emps[m].get('former')]
    if not members:
        return 'gm'
    tasks = (state or {}).get('tasks', {})
    last = _last_work(state, members)

    def rank(i_m):
        i, m = i_m
        e = emps[m]
        cur = starting[1] if (starting and starting[0] == m) else (e.get('current_task') if e.get('turn') else None)
        if cur:
            tier = 0 if (tasks.get(cur) or {}).get('dept') == to else 1
        else:
            tier = 2 if last[m] else 3
        return (tier, -last[m], i)

    return min(enumerate(members), key=rank)[1]


def pending_for(state, em, task=None):
    """还没送到、现在该送给这位员工的信（按写信时间先后，最多 MAX_PER_DELIVERY 封）。task：他这一轮要开工做的活。"""
    e = _emps(state).get(em) or {}
    if e.get('former'):
        return []
    ws = e.get('ws')
    starting = (em, task) if task else None
    out = []
    for l in (state or {}).get('letters', {}).values():
        if l.get('delivered') or l.get('from') == em:
            continue
        if ws and l.get('ws') not in (None, ws):
            continue
        if recipient(state, l, starting) == em:
            out.append(l)
    out.sort(key=lambda l: (l.get('ts') or 0, l.get('id') or ''))
    return out[:MAX_PER_DELIVERY]


def redeliver(state, em, task):
    """上一轮开工时交代过、可是那一轮没做成（失败且没改文件，比如额度满载换线路重做）的信：这一轮再交代一次。"""
    turns = [tr for tr in (state or {}).get('turns', {}).values()
             if tr.get('em') == em and tr.get('kind') not in MEMORY_TURNS]
    if not turns:
        return []
    last = max(turns, key=lambda tr: (tr.get('started') or 0))
    if last.get('task') != task or last.get('status') != 'failed' or (last.get('evidence') or {}).get('changed_files'):
        return []
    since = last.get('started') or 0
    out = [l for l in (state or {}).get('letters', {}).values()
           if l.get('delivered_to') == em and l.get('delivered_how') == 'next_turn' and l.get('delivered_task') == task
           and (l.get('delivered_ts') or 0) >= since]
    out.sort(key=lambda l: (l.get('ts') or 0, l.get('id') or ''))
    return out[:MAX_PER_DELIVERY]


def _head(state, l, em):
    conv = l.get('conv')
    sender = who(state, conv, l.get('from'))
    to = l.get('to')
    kind = KIND_TEXT.get(l.get('kind'))
    tags = ([kind] if kind else []) + [l.get('id') or '']
    if l.get('reply_to'):
        orig = (state or {}).get('letters', {}).get(l['reply_to']) or {}
        what = '回复了你的信%s' % ('「%s」' % orig['subject'] if orig.get('subject') else '（%s）' % l['reply_to'])
    elif to == em:
        what = '写给你'
    else:
        what = '写给%s，转给你' % who(state, conv, to)
    return '%s %s（%s）：%s' % (sender, what, '，'.join(tags), l.get('subject') or '')


def render(state, letters, em, *, steer=False, again=0):
    """交代给员工的那段话："你有 2 封信：……"。again：其中有几封是上一轮交代过、再说一次的。"""
    n = len(letters)
    head = '【你有 %d 封信】' % n
    if steer:
        head += '不用停下手头的活，看完接着做；要回复的顺手回一下。'
    if again:
        head += '（其中 %d 封上一轮已经交代过，上一轮没做成，再说一次）' % again
    lines = [head]
    for i, l in enumerate(letters, 1):
        lines.append('%d. %s' % (i, _head(state, l, em)))
        body = str(l.get('body') or '').strip()
        if body and body != (l.get('subject') or '').strip():
            if len(body) > BODY_CLIP:
                body = body[:BODY_CLIP] + '……（后面省略）'
            lines.append('   ' + body.replace('\n', '\n   '))
    lines.append(REPLY_HINT)
    return '\n'.join(lines)


def delivery(state, letter):
    """快照里每封信的送达情况（页面直接显示 text，不自己推断）：
    {state: delivered|waiting|stuck|none, text, to}。旧系统镜像的信返回 None（沿用原来的显示）。"""
    if letter.get('legacy'):
        return None
    conv = letter.get('conv')
    if letter.get('delivered'):
        to = letter.get('delivered_to')
        if to == 'gm' or (not to and letter.get('to') == 'gm'):
            return {'state': 'delivered', 'text': '负责人已看到', 'to': 'gm'}
        if to:
            return {'state': 'delivered', 'text': DELIVERED_HOW.get(letter.get('delivered_how'), '已送到 %s') % who(state, conv, to),
                    'to': to}
        return {'state': 'delivered', 'text': '已送到', 'to': None}
    r = recipient(state, letter)
    if r in ('user', 'office'):
        return {'state': 'none', 'text': '', 'to': r}
    if r == 'gm':
        return {'state': 'waiting', 'text': '还没送到：等负责人来看', 'to': 'gm'}
    if r is None:
        return {'state': 'stuck', 'text': '送不到：找不到收信人', 'to': None}
    e = _emps(state).get(r) or {}
    name = e.get('label') or r
    if e.get('former'):
        return {'state': 'stuck', 'text': '送不到：%s 已经离职' % name, 'to': r}
    via = '' if letter.get('to') == r else '（转给 %s）' % name
    return {'state': 'waiting', 'text': ('还没送到%s：等 %s 下次开工' % (via, name)), 'to': r}
