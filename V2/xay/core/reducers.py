"""reducer：(现状, 事件) → 现状。只读事件字段与现状，不读时钟、不读文件、不调外部程序（protocol §0.4）。

- `apply(state, event)` 原地改 state（账本内核用它，避免每条事件都深拷贝整个现状）。
  调用方负责：出错时把这份 state 作废（events.py 里的缓存就是这么做的）。
- `reduce(state, event)` 是纯函数版本：先深拷贝再 apply，给需要"不动原现状"的调用方用。

约定：放进现状的值不能和现状里别处共用同一个可变对象（列表/字典），否则以后原地修改会串改，
而从存档点（JSON）恢复的现状又不共用——两条路算出的结果就会不一样。所以凡是一份值要放两处，都先拷贝。
"""
import copy
import os
import sys

if "unittest" in sys.argv or any("unittest" in a for a in sys.argv):
    os.environ.pop("XAY_ROLE", None)

# 改了 reducer 的语义就把它加一：旧代码写的存档点会被忽略，改为从头重放（events.py）
REDUCER_VERSION = 10  # 4：M2 工位（思考一句话、记忆段内轮数、复述待检、合并/丢弃待办、整理记忆那一轮不动任务状态）
                      # 5：M4 信件与计划（回信关掉原来的请求、送达记下送给谁/怎么送、申请先校验、处理过的申请留档）
                      # 6：记下员工离线原因（正常下班显示成休息）
                      # 7：M4 管家自动化（失败次数、连续失败、预算记录与加额度、总闸、复述重试、规则打开的需要你）
                      # 8：M3/M4 对接（一轮进行中的 token 记到那一轮上、心跳里的命令行 pid、自动发号跳过已被占用的显示名）
                      # 9：1.23 小任务（part.*，状态 parts，任务与部门归属 part）
                      # 10：1.24 监工审查流程（check.result，任务 check 状态）

PLAN_TYPES = ('task.added', 'task.changed', 'task.removed', 'task.assigned', 'part.added', 'part.changed', 'part.removed')
CHANGEABLE = ('title', 'dept', 'deps', 'writes', 'steps', 'card', 'budget', 'mode', 'part')
ADDABLE = ('key', 'title', 'dept', 'owner', 'deps', 'writes', 'steps', 'card', 'budget', 'mode', 'reason', 'part')
LETTER_KINDS = ('request', 'decision', 'tell', 'reply', 'report', 'escalation', 'notice', 'system')
PROPOSAL_OPS = ('add', 'change', 'remove', 'assign')
KEPT_KINDS = ('mismatch', 'failed_with_changes', 'interrupted', 'conflict', 'desk_unmerged')   # 甲类“需要你”
LEAD_PLAN_TYPES = ('task.added', 'task.assigned', 'task.changed', 'part.added', 'part.changed')   # 负责人又排了活：等于说"接着干"（总闸，§3.5）
MEMORY_TURNS = ('compact', 'handoff_write')   # 整理记忆的那一轮：不是在做任务
THOUGHT_MAX = 120

# 旧系统镜像的 run 状态 → 页面上的任务状态（§9）
LEGACY_STATUS = {'running': 'working', 'active': 'working', 'queued': 'queued', 'done': 'done',
                 'failed': 'failed', 'error': 'failed', 'timeout': 'failed', 'stale': 'failed', 'stopped': 'canceled',
                 # 协议表里没写、但意思相同的旧状态名
                 'completed': 'done', 'canceled': 'canceled'}


def dept_key(conv, dept):
    """部门编号只在对话内唯一（§1，v1.5）：现状里按 "<对话>|<部门>" 存，记录里的 id 仍是短名。"""
    return '%s|%s' % (conv, dept)


def legacy_status(run):
    """旧 run 的状态映射到任务状态；表里没有的：已经结束（有 ended）记成 failed，否则当作还在跑。"""
    raw = run.get('status')
    if raw in LEGACY_STATUS:
        return LEGACY_STATUS[raw]
    return 'failed' if run.get('ended') is not None else 'working'


def _legacy_from(v):
    """旧总线的 from 是 {dept, worker, run}：有具名员工就用员工名，否则部门名，都没有就是 gm。"""
    if isinstance(v, dict):
        worker, dept = v.get('worker'), v.get('dept')
        if worker and worker != 'gm': return worker
        if dept and dept != 'gm': return dept
        return 'gm'
    return v


def empty_state():
    return {'workspaces':{}, 'convs':{}, 'aliases':{}, 'depts':{}, 'employees':{}, 'tasks':{}, 'parts':{},
            'plan_history':{}, 'plan_batch':{}, 'turns':{}, 'letters':{}, 'decisions':{}, 'proposals':{}, 'notes':{},
            'legacy_runs':{}, 'legacy_letters':{},
            'seq':0, 'last_ts':0}

def _cp(v):
    return copy.deepcopy(v) if isinstance(v, (dict, list)) else v

def _by_name(by, d=None):
    if by.startswith('lead:'): return 'gm'
    if by.startswith('employee:') or by.startswith('station:'): return by.split(':',1)[1]
    if by == 'station' and d and d.get('by_em'): return d['by_em']
    if by in ('office','rules','user'): return 'office' if by in ('office','rules') else by
    return by

def _conv(state, conv):
    return state['convs'].setdefault(conv, {'id':conv,'title':'','lead_kind':'unknown','plan_version':0,'ended':False,'ws':None})

def _emp(state, em):
    return state['employees'].setdefault(em, {'key':em,'label':em,'agent':'codex','ws':None,'state':'idle','online':False,'depts':{},'queue':[], 'progress':{}, 'context':{'pct':0,'segment':1,'compactions':0,'calibrating':False,'estimated':False}, 'amnesia':False, 'last_ts':0, 'turn':None, 'current_task':None, 'former':False})

def _task(state, tk):
    return state['tasks'].setdefault(tk, {'id':tk,'key':None,'title':'','dept':None,'owner':None,'deps':[],'writes':[],'steps':[],'planned_steps':[],'card':'','budget':{},'mode':'write','reason':'','status':'planned','progress':{},'result':'','plain':'','assigned_reason':'','part':None,'check':{'state':'none','round':0,'text':'','by':''}})

def _history(state, conv, by, ts, reason, changes, batch=None):
    """写一条计划历史并让计划版本 +1；同一对话里连续的、batch 相同的计划事件共用一个版本和一条历史（§4.2）。"""
    if not isinstance(changes, list): changes = [changes]
    open_batches = state.setdefault('plan_batch', {})
    hist = state['plan_history'].setdefault(conv, [])
    cur = open_batches.get(conv)
    if batch is not None and cur and cur.get('batch') == batch and hist and hist[-1]['version'] == cur.get('version'):
        hist[-1]['changes'].extend(changes)
        return
    c = _conv(state, conv); c['plan_version'] += 1
    hist.append({'version':c['plan_version'],'by':by,'ts':ts,'reason':reason,'changes':changes})
    if batch is not None: open_batches[conv] = {'batch':batch, 'version':c['plan_version']}
    else: open_batches.pop(conv, None)

def _close_batch(state, conv):
    if conv is not None: state.setdefault('plan_batch', {}).pop(conv, None)

def _add_change(t):
    after={k:_cp(t.get(k)) for k in ('title','owner','dept','deps')}
    if t.get('part'): after['part']=t['part']          # 1.23 小任务：写了才记
    if t.get('by_em'):
        after['by_em']=t['by_em']
        if t.get('by_task'): after['by_task']=t['by_task']
        if t.get('agent'): after['agent']=t['agent']
    ch = {'op':'add','task':t['id'],'title':t.get('title'),'after':after}
    if t.get('by_em'):
        ch['by_em']=t['by_em']
        if t.get('by_task'): ch['by_task']=t['by_task']
        if t.get('reason'): ch['reason']=t['reason']
    return ch

def _change_fields(t, fields):
    before = {}; after = {}
    for k, v in fields.items():
        if k in CHANGEABLE:
            before[k] = _cp(t.get(k)); t[k] = _cp(v); after[k] = _cp(v)
    return {'op':'change','task':t['id'],'title':t.get('title'),'before':before,'after':after}


def _check_changes(s, conv, changes):
    """申请里的改动对不对（§4.2）：add 的任务编号是新的、要有标题；change/remove/assign 的任务已经存在、属于本对话；
    assign 要写清交给谁。提申请时查一次，通过时再查一次（中间任务可能变了）。不对就 ValueError，账本不收。"""
    if not isinstance(changes, list) or not changes: raise ValueError('申请里没有要改的地方')
    tasks=s['tasks']; added=set()
    for c in changes:
        if not isinstance(c, dict) or c.get('op') not in PROPOSAL_OPS: raise ValueError('申请里的改动格式不对（只能 add / change / remove / assign）')
        tk=c.get('task'); fields=c.get('fields') or {}
        if not isinstance(tk, str) or not tk: raise ValueError('申请里的改动没写是哪件任务')
        if not isinstance(fields, dict): raise ValueError('申请里的 fields 要是对象')
        if c['op']=='add':
            if tk in tasks or tk in added: raise ValueError(f'申请要新增的任务编号 {tk} 已经有了')
            if not str(fields.get('title') or '').strip(): raise ValueError('申请新增的任务要有标题')
            added.add(tk)
            continue
        t=tasks.get(tk)
        if t is None and tk not in added: raise ValueError(f'申请里的任务 {tk} 不存在')
        if t is not None and t.get('conv') not in (None, conv): raise ValueError('只能申请改本对话里的任务')
        if c['op']=='assign' and not fields.get('owner'): raise ValueError('申请换人要写清交给谁')


def reduce(state, event):
    """纯函数版本：不改传入的 state。"""
    s = copy.deepcopy(state)
    apply(s, event)
    return s


def apply(s, event):
    """把一条事件原地作用到现状 s 上，返回 s。"""
    typ, d, by, conv, ws, sub, ts = event['type'], event.get('data') or {}, event.get('by',''), event.get('conv'), event.get('ws'), event.get('subject'), event.get('ts',0)
    s['seq'] = event.get('seq', s.get('seq',0)); s['last_ts'] = ts
    if event.get('v',1) > 1: return s
    batch = d.get('batch') if isinstance(d.get('batch'), str) else None
    # 计划批次（§4.2 data.batch）："连续"按同一对话里的计划事件算——下一条会写计划历史的事件
    # （batch 不同、没有 batch，或申请通过）就结束上一批；中间夹着的心跳、进度等别的事件不打断。
    if conv is not None and conv in s.setdefault('plan_batch', {}):
        writes_history = (typ in PLAN_TYPES and not (typ == 'task.assigned' and by == 'office'))
        accepted = typ == 'proposal.decided' and d.get('verdict') == 'accept'
        if accepted or (writes_history and s['plan_batch'][conv].get('batch') != batch):
            _close_batch(s, conv)
    if typ in LEAD_PLAN_TYPES and by.startswith('lead:') and (s.get('halt') or {}).get('by') == 'gm':
        s['halt']=None     # 负责人自己拉的总闸，负责人又排了活就算恢复；你在页面上拉的只有你点"恢复"才解除
    if typ == 'conv.begun':
        c=_conv(s,conv); c.update({'title':d.get('title',''),'lead_kind':d.get('lead_kind','unknown'),'ws':ws,'plan_version':0})
        s['workspaces'].setdefault(ws, {'id':ws,'name':ws,'path':d.get('cwd','')})
        for a in d.get('aliases',[]):
            if a not in s['aliases']: s['aliases'][a]=conv
    elif typ == 'conv.aliased':
        if d.get('alias') not in s['aliases']: s['aliases'][d['alias']]=conv
    elif typ == 'conv.titled': _conv(s,conv)['title']=d.get('title','')
    elif typ == 'conv.ended': _conv(s,conv)['ended']=True
    elif typ == 'conversation.lead_changed':
        target_conv = conv or sub
        if target_conv:
            c = _conv(s, target_conv)
            if d.get('to'):
                c['lead_kind'] = d.get('to')
    elif typ == 'dept.opened':
        s['depts'][dept_key(conv,sub)]={'id':sub,'conv':conv,'ws':ws,'name':d.get('name',sub),'mission':d.get('mission',''),'owns':_cp(d.get('owns',[])),'head':d.get('head','rules'),'closed':False,'members':[],'part':d.get('part')}
    elif typ == 'dept.closed':
        dk=dept_key(conv,sub)
        if dk in s['depts']:
            s['depts'][dk]['closed']=True
            for e in s['employees'].values():
                if e.setdefault('depts',{}).get(conv)==sub: e['depts'].pop(conv)
    elif typ == 'employee.hired':
        agent=d.get('agent','codex'); label=d.get('label')
        if label and any(x.get('label')==label and x.get('key')!=sub and x.get('ws')==ws for x in s['employees'].values()): raise ValueError('员工显示名在这个项目里已被使用')
        e=_emp(s,sub)
        e.update({'agent':agent,'ws':ws,'label':label or e.get('label'),'state':'idle','online':False,'former':False,'last_ts':ts})
        if not label:
            # 自动发号（§1）：按"项目（ws）+ AI 种类"各自编，离职的号不回收（离职的人还在现状里）。
            # 先取本项目同种 AI 现有的最大号 +1（和以前一样，已有员工重放后名字不变），再跳过本项目里已经被占用的
            # 显示名——手动起的（xay2 hire --label）、别的 AI 用了这个名字的、已离职的——自动发号永远不撞名。
            names={'codex':'Codex','dsh':'DeepSeek','agy':'Gemini'}; prefix=names.get(agent,agent)
            nums=[]
            for x in s['employees'].values():
                if x.get('ws')==ws and x.get('agent')==agent and x.get('label','').startswith(prefix+' '):
                    try: nums.append(int(x['label'].split()[-1]))
                    except (ValueError, IndexError): pass
            taken={x.get('label') for x in s['employees'].values() if x.get('ws')==ws and x.get('key')!=sub}
            n=max(nums or [0])+1
            while f'{prefix} {n}' in taken: n+=1
            e['label']=f'{prefix} {n}'
        if d.get('note') is not None: e['note']=d['note']
        if d.get('line'): e['line']=d['line']
    elif typ == 'employee.joined':
        e=_emp(s,sub); dept=d.get('dept')
        if conv is not None:   # 部门属于对话：同一员工在不同对话里可以属于不同部门，在一个对话里只属于一个
            old=e.setdefault('depts',{}).get(conv)
            if old is not None and old!=dept and dept_key(conv,old) in s['depts']:
                s['depts'][dept_key(conv,old)]['members']=[m for m in s['depts'][dept_key(conv,old)]['members'] if m!=sub]
            if dept: e['depts'][conv]=dept
            else: e['depts'].pop(conv,None)
            dk=dept_key(conv,dept)
            if dept and dk in s['depts'] and sub not in s['depts'][dk]['members']: s['depts'][dk]['members'].append(sub)
    elif typ == 'employee.session':
        e=_emp(s,sub); old=e['context'].get('segment',1); seg=d.get('segment',e.get('segment',old))
        e['segment']=seg; e['native_id']=d.get('native_id'); e['context']['segment']=seg
        if d.get('line'): e['line']=d['line']   # 换了额度线路（line_switch）：之后的活都走这条
        if seg!=old: e['seg_turns']=0; e['recall_due']=True; e['recall_retries']=0   # 换了一段记忆：下一轮先复述
    elif typ == 'employee.thought':
        e=_emp(s,sub); e['thought']=str(d.get('text') or '')[:THOUGHT_MAX]; e['activity_ts']=ts
    elif typ == 'employee.online': _emp(s,sub).update({'online':True,'state':'idle','last_ts':ts})
    elif typ == 'employee.offline':
        e=_emp(s,sub); e.update({'online':False,'state':'offline','last_ts':ts,'offline_reason':d.get('reason')}); e.pop('cli_pids',None)
    elif typ == 'employee.fired': _emp(s,sub).update({'former':True,'state':'fired','online':False,'fired_ts':ts})
    elif typ == 'station.heartbeat':
        e=_emp(s,sub); e['last_heartbeat']=ts
        # M3（1.12）：工位起的命令行子进程（各自是进程组组长）。管家强杀工位时连它们的进程组一起杀；下线就清掉
        if isinstance(d.get('cli_pids'), list):
            e['cli_pids']=[int(p) for p in d['cli_pids'] if isinstance(p,(int,float)) and not isinstance(p,bool) and p>1][:16]
    elif typ == 'task.added':
        t=_task(s,sub); t.update({'id':sub,'key':d.get('key'),'title':d.get('title',''),'dept':d.get('dept'),'owner':d.get('owner'),'deps':_cp(d.get('deps',[])),'writes':_cp(d.get('writes',[])),'steps':_cp(d.get('steps',[])),'planned_steps':_cp(d.get('planned_steps',[])),'card':d.get('card',''),'budget':_cp(d.get('budget',{})),'mode':d.get('mode','write'),'reason':d.get('reason',''),'status':'planned','conv':conv,'ws':ws,'progress':{},'result':'','part':d.get('part'),'check':{'state':'none','round':0,'text':'','by':''}})
        if d.get('agent'): t['agent']=d['agent']
        if d.get('review_of'): t['review_of']=d['review_of']
        if d.get('check'): t['task_check']=_cp(d['check'])
        if d.get('by_task'): t['by_task']=d['by_task']
        if d.get('by_em'): t['by_em']=d['by_em']
        _history(s,conv,_by_name(by, d),ts,d.get('reason',''),_add_change(t),batch)
    elif typ == 'task.changed':
        t=_task(s,sub); ch=_change_fields(t, d.get('fields',{}) or {})
        _history(s,conv,_by_name(by),ts,d.get('reason',''),ch,batch)
    elif typ == 'task.removed':
        t=_task(s,sub); old=t.get('status'); t['status']='canceled'
        _history(s,conv,_by_name(by),ts,d.get('reason',''),{'op':'remove','task':sub,'title':t.get('title'),'before':{'status':old}},batch)
    elif typ == 'part.added':
        parts = s.setdefault('parts', {})
        order = len(parts) + 1
        p = {
            'id': sub,
            'conv': conv,
            'title': d.get('title', ''),
            'after': _cp(d.get('after', [])),
            'check': _cp(d.get('check', {'kind': 'rules'})),
            'note': d.get('note', ''),
            'key': d.get('key'),
            'order': order,
        }
        parts[sub] = p
        _history(s, conv, _by_name(by), ts, d.get('reason', ''),
                 {'op': 'part_add', 'part': sub, 'title': p.get('title')}, batch)
    elif typ == 'part.changed':
        parts = s.setdefault('parts', {})
        if sub not in parts:
            raise ValueError('小任务不存在')
        p = parts[sub]
        before = {}; after = {}
        fields = d.get('fields', {}) if isinstance(d.get('fields'), dict) else d
        for k in ('title', 'after', 'check', 'note', 'key'):
            if k in fields and fields[k] is not None:
                before[k] = _cp(p.get(k))
                p[k] = _cp(fields[k])
                after[k] = _cp(fields[k])
        _history(s, conv, _by_name(by), ts, d.get('reason', ''),
                 {'op': 'part_change', 'part': sub, 'title': p.get('title'), 'before': before, 'after': after}, batch)
    elif typ == 'part.removed':
        parts = s.setdefault('parts', {})
        if sub not in parts:
            raise ValueError('小任务不存在')
        p = parts[sub]
        p_key = p.get('key')
        active = [
            tk for tk, t in s.get('tasks', {}).items()
            if (t.get('part') == sub or (p_key and t.get('part') == p_key)) and t.get('status') != 'canceled'
        ]
        if active:
            raise ValueError(f'小任务「{p.get("title") or sub}」里还有未撤回的任务，不能删除')
        old_title = p.get('title', '')
        parts.pop(sub, None)
        _history(s, conv, _by_name(by), ts, d.get('reason', ''),
                 {'op': 'part_remove', 'part': sub, 'title': old_title}, batch)
    elif typ == 'task.assigned':
        t=_task(s,sub); old=t.get('owner'); t.update({'owner':d.get('owner'),'assigned_reason':d.get('reason','')})
        if by != 'office': _history(s,conv,_by_name(by),ts,d.get('reason',''),{'op':'assign','task':sub,'title':t.get('title'),'before':{'owner':old},'after':{'owner':d.get('owner')}},batch)
    elif typ == 'task.ready': _task(s,sub)['status']='ready'
    elif typ == 'task.queued':
        t=_task(s,sub); em=d.get('owner') or t.get('owner'); t['status']='queued'; t['owner']=em; e=_emp(s,em); e['queue']=[x for x in e.get('queue',[]) if x!=sub]; e['queue'].append(sub); e['state']='queued'
        t['queued_seq']=s['seq']; retry=d.get('retry')
        if isinstance(retry,str) and retry:   # 规则监工重排（auto / env / manual），各自计数（反应表的"上限"）
            r=t.setdefault('retries',{}); r[retry]=r.get(retry,0)+1
            if retry=='manual': t.pop('error_loop',None)   # 你决定再试一次：不再当成"一直报错"的活
    elif typ == 'desk.synced':
        _emp(s,sub)['desk_base']=d.get('base')
        if d.get('task') in s['tasks']: s['tasks'][d['task']]['desk_action']=None   # 丢弃改动之后的重新对齐
    elif typ == 'turn.started':
        kind=d.get('kind'); tr={'id':sub,'task':d.get('task'),'em':d.get('em'),'status':'working','started':ts}
        if kind: tr['kind']=kind
        s['turns'][sub]=tr; e=_emp(s,d.get('em'))
        if kind in MEMORY_TURNS:   # 整理记忆（写交接摘要）那一轮：任务状态不动，员工显示"整理中"
            e.update({'state':'compacting','turn':sub,'online':True,'last_ts':ts})
        elif d.get('task'):
            t=_task(s,d.get('task')); t['status']='working'; e.update({'state':'working','current_task':d.get('task'),'turn':sub,'online':True,'last_ts':ts}); e['queue']=[x for x in e.get('queue',[]) if x!=d.get('task')]
            e['seg_turns']=e.get('seg_turns',0)+1; e['last_task']=d.get('task')
        else:
            e.update({'state':'working','current_task':None,'turn':sub,'online':True,'last_ts':ts})
        e['activity_ts']=ts
    elif typ == 'task.progress':
        t=_task(s,sub); t['progress']={k:_cp(d[k]) for k in ('step','of','text') if k in d}
        if t.get('planned_steps'): t['progress']['of'] = len(t['planned_steps'])
        if t.get('owner'): o=_emp(s,t['owner']); o['progress']=copy.deepcopy(t['progress']); o['activity_ts']=ts
    elif typ == 'task.planned':
        t = _task(s, sub)
        steps = d.get('steps', [])
        t['planned_steps'] = _cp(steps)
        if 'round' in d and d['round'] is not None:
            t['planned_round'] = int(d['round'])
        if t.get('progress') and steps:
            t['progress']['of'] = len(steps)
            if t.get('owner') and t['owner'] in s.get('employees', {}):
                s['employees'][t['owner']]['progress']['of'] = len(steps)
    elif typ == 'turn.ended':
        em=d.get('em') or s['turns'].get(sub,{}).get('em'); tr=s['turns'].setdefault(sub,{}); tr.update({'status':d.get('status'),'ended':ts,'evidence':_cp(d.get('evidence',{})),'usage':_cp(d.get('usage',{}))}); e=_emp(s,em); e['turn']=None; e['last_ts']=ts; e['thought']=''
        if tr.get('kind') in MEMORY_TURNS: e['state']='compacting'   # 等 context.compacted 收尾
        else: e['current_task']=None; e['state']='queued' if e.get('queue') else 'idle'
    elif typ == 'task.reported':
        t=_task(s,sub)
        if 'summary' in d: t['result']=d.get('summary','')
        if 'claim' in d: t['claim']=d.get('claim')
        if 'plain' in d: t['plain']=d.get('plain','')
    elif typ == 'task.landed':
        t=_task(s,sub); t.update({'status':'reported','desk_action':None})
        if t.get('owner') in s['employees']: s['employees'][t['owner']]['fail_streak']=0
        if not t.get('review_of'):
            chk_def = t.get('task_check')
            if not chk_def and t.get('part'):
                parts = s.get('parts', {})
                p = parts.get(t['part']) or next((x for x in parts.values() if x.get('key') == t['part']), None)
                if p: chk_def = p.get('check')
            chk_kind = (chk_def or {}).get('kind')
            if chk_kind in ('ai', 'lead'):
                chk = t.setdefault('check', {'state': 'none', 'round': 0, 'text': '', 'by': ''})
                chk['state'] = 'waiting'
                chk['round'] = chk.get('round', 0) + 1
    elif typ == 'check.result':
        t = _task(s, sub)
        chk = t.setdefault('check', {'state': 'none', 'round': 0, 'text': '', 'by': ''})
        ok = bool(d.get('ok'))
        chk['state'] = 'passed' if ok else 'failed'
        chk['text'] = str(d.get('text') or '')
        chk['by'] = by
        if 'round' in d and d['round'] is not None:
            chk['round'] = int(d['round'])
    elif typ == 'task.kept': _task(s,sub).update({'status':'needs_you','kept_reason':d.get('reason'),'kept_detail':d.get('detail',''),'desk_action':None})
    elif typ == 'task.failed':
        t=_task(s,sub); t.update({'status':'failed','failure_reason':d.get('reason'),'failure_detail':d.get('detail',''),'fails':t.get('fails',0)+1,'failed_seq':s['seq'],'failed_ts':ts})
        if t.get('owner') in s['employees']: o=s['employees'][t['owner']]; o['fail_streak']=o.get('fail_streak',0)+1
    elif typ == 'task.accepted':
        t=_task(s,sub); t['status']='done' if t.get('status')=='reported' else t.get('status')
    elif typ == 'task.canceled': _task(s,sub)['status']='canceled'
    elif typ == 'context.updated':
        e=_emp(s,sub); e['context'].update({k:_cp(d[k]) for k in ('pct','estimated','used','window','source') if k in d}); e['context']['calibrating']=False; e['activity_ts']=ts
        # M3（1.12）：一轮进行中带 turn + turn_tokens（这一轮到现在累计的 token）→ 记到那一轮上，预算的 token 线中途就管得住。
        # 收工后工位还会再写一次最终数（在 turn.ended 之后），照样记；只增不减。
        tk_n=d.get('turn_tokens'); tr=s['turns'].get(d.get('turn')) if isinstance(d.get('turn'),str) else None
        if tr is not None and tr.get('em')==sub and isinstance(tk_n,(int,float)) and not isinstance(tk_n,bool) and tk_n>=0:
            tr['turn_tokens']=max(int(tk_n), int(tr.get('turn_tokens') or 0))
    elif typ == 'context.compact_requested': _emp(s,sub)['state']='compacting'
    elif typ == 'context.compacted':
        e=_emp(s,sub); c=e['context']; c['compactions']=c.get('compactions',0)+1; c['segment']=d.get('segment',c.get('segment',1)); c['calibrating']=True
        if 'after_pct' in d: c['pct']=d['after_pct']
        e['compact_after']=d.get('after_pct')   # 压缩后还剩多少（选人时看"压缩也压不下来"）
        # 干活中途 CLI 自己压缩（native_auto）时这一轮还没完：仍是 working
        e['state']='working' if e.get('turn') else ('queued' if e.get('queue') else 'idle')
        e['seg_turns']=0; e['recall_due']=True; e['recall_retries']=0
    elif typ == 'notes.updated':
        old=(s['notes'].get(sub) or {}).get('archived') or []
        s['notes'][sub]={'active':d.get('active',''),'archived':list(old)+_cp(d.get('archived_add',[]))}   # 归档只追加（§4.4）
    elif typ == 'recall.checked':
        e=_emp(s,sub); e['amnesia']=not d.get('ok',False); e['recall_due']=False; e['recall_seq']=s['seq']
        if d.get('ok',False): e['recall_retries']=0
    elif typ == 'recall.requested':   # 规则监工：复述不出来，下一轮再给一次笔记重新复述（M4）
        e=_emp(s,sub); e['recall_due']=True; e['recall_retries']=e.get('recall_retries',0)+1
    elif typ == 'letter.sent':
        x={'id':sub,'ts':ts,'conv':conv,'ws':ws,'from':d.get('from'),'to':d.get('to'),'kind':d.get('kind','tell'),'subject':d.get('subject',''),'body':d.get('body',''),'reply_to':d.get('reply_to'),'contract':_cp(d.get('contract')),'delivered':False,'status':'open' if d.get('kind')=='request' else 'closed','verdict':None}; s['letters'][sub]=x
        # 回信（带 reply_to，不管是 xay2 reply、【回信】标记还是 answer 附带的话）：原来那封"要回复"的信就算答复了，
        # 写信的人不再"等人答复"。只有别人回才算，自己追加一句不算。
        orig=s['letters'].get(d.get('reply_to')) if d.get('reply_to') else None
        if orig and orig.get('kind')=='request' and orig.get('status')=='open' and d.get('from')!=orig.get('from'):
            orig['status']='closed'; orig['answered_by']=sub
    elif typ == 'letter.delivered':
        # 送到了：记下怎么送的、送给了谁（工位写 data.em；负责人在 xay2 wait 里看到 = 总经理）、这一轮是哪件活。只记第一次。
        x=s['letters'].get(sub)
        if x and not x.get('delivered'):
            x.update({'delivered':True,'delivered_how':d.get('how'),'delivered_ts':ts,
                      'delivered_to':d.get('em') or ('gm' if by.startswith('lead:') else None)})
            if d.get('task'): x['delivered_task']=d['task']
    elif typ == 'letter.answered':
        if sub in s['letters']: s['letters'][sub].update({'status':'closed','verdict':d.get('verdict')})
    elif typ == 'decision.opened': s['decisions'][sub]=dict(_cp(d), id=sub, ts=ts, conv=conv, ws=ws, opened_by=by.split(':',1)[0])
    elif typ == 'decision.delegated':
        if sub in s['decisions']:
            s['decisions'][sub]['delegated'] = d.get('to')
    elif typ == 'decision.resolved':
        dec=s['decisions'].pop(sub,None)
        if dec:
            opt=d.get('option'); tk=dec.get('task')
            if dec.get('kind')=='budget' and opt=='extend':   # 再给一些预算（§3.5）：每点一次加一档
                scope=(dec.get('budget') or {}).get('scope')
                if scope=='employee' and dec.get('em') in s['employees']:
                    be=s['employees'][dec['em']]; be.setdefault('budget_boosts',[]).append(ts); be.pop('budget_stopped_ts',None)
                elif tk in s['tasks']: bt=s['tasks'][tk]; bt['budget_boost']=bt.get('budget_boost',0)+1; bt.pop('budget_stopped',None)
            if by=='rules' and dec.get('kind')=='conflict' and opt=='merge' and tk in s['tasks']:   # 规则监工自动再合并一次
                s['tasks'][tk]['auto_merges']=s['tasks'][tk].get('auto_merges',0)+1
            if dec.get('kind') in ('mismatch','failed_with_changes','interrupted','conflict','desk_unmerged') and tk:
                t=_task(s,tk)
                # desk_action：工位接着要在桌子上做的事（合并 / 丢弃），做完写 task.landed / task.kept / desk.synced 清掉
                if opt=='merge': t['status']='reported'; t['desk_action']='merge'
                elif opt=='manual': t['status']='reported'; t['desk_action']='settle'
                elif opt=='rework':
                    t['status']='queued'; t['rework_note']=d.get('text') or ''; e=_emp(s,dec.get('em') or t.get('owner'))
                    e['queue']=[x for x in e['queue'] if x!=tk]; e['queue'].append(tk); e['state']='queued'
                elif opt=='discard': t['status']='canceled'; t['desk_action']='discard'
            if dec.get('em') in s['employees'] and opt in ('merge','discard','manual'): s['employees'][dec['em']]['state']='idle'
    elif typ == 'proposal.made':
        if sub in s['proposals']: raise ValueError(f'申请编号 {sub} 已经用过了')
        _check_changes(s, conv, d.get('changes'))
        s['proposals'][sub]=dict(_cp(d),id=sub,ts=ts,conv=conv,ws=ws,status='open')
    elif typ == 'proposal.decided':
        # 处理过的申请留档（status=accepted/rejected，快照只列 open 的），同一个申请不能处理两次
        p=s['proposals'].get(sub)
        if p is None: raise ValueError(f'找不到申请 {sub}')
        if p.get('status')!='open': raise ValueError(f'申请 {sub} 已经处理过了')
        if d.get('verdict')=='accept': _check_changes(s, p.get('conv'), p.get('changes'))
        p.update({'status':'accepted' if d.get('verdict')=='accept' else 'rejected','decided_ts':ts,'decided_by':_by_name(by)})
        if d.get('note'): p['note']=d['note']
        if d.get('verdict')=='accept':
            # 按 changes 原子地应用：等价于对应的 task.* 事件，但只加一次版本、写一条历史（by = 提出申请的人）
            reason=p.get('reason','')+f'（申请 {sub} 通过）'; hist_changes=[]
            for chg in p.get('changes',[]):
                op=chg.get('op'); tk=chg.get('task'); fields=chg.get('fields') or {}
                if op=='add':
                    t=_task(s,tk); t.update({'id':tk,'conv':conv,'ws':ws,'status':'planned','progress':{},'result':''})
                    t.update({k:_cp(fields[k]) for k in ADDABLE if k in fields})
                    hist_changes.append(_add_change(t))
                elif op=='change':
                    hist_changes.append(_change_fields(_task(s,tk), fields))
                elif op=='remove':
                    t=_task(s,tk); hist_changes.append({'op':'remove','task':tk,'title':t.get('title'),'before':{'status':t.get('status')}}); t['status']='canceled'
                elif op=='assign':
                    t=_task(s,tk); old=t.get('owner'); t['owner']=fields.get('owner'); t['assigned_reason']=p.get('reason','')
                    hist_changes.append({'op':'assign','task':tk,'title':t.get('title'),'before':{'owner':old},'after':{'owner':t.get('owner')}})
            _history(s,conv,_by_name(p.get('from','')),ts,reason,hist_changes)
            _close_batch(s, conv)
            # 页面要能写"Codex 1 提的申请，总经理通过了"：这一版来自哪个申请、谁批的
            h=s['plan_history'][conv][-1]; h['proposal']=sub; h['approved_by']=_by_name(by)
            p['version']=h['version']
    elif typ == 'signal.raised':
        s.setdefault('signals',[]).append(dict(_cp(d),id=sub,ts=ts))
        if d.get('kind')=='error_loop' and 'git commit' not in str(d.get('text') or ''):
            # 一直报错、不用工具（M3 工位写）：这件活管家不再自动重派。哪件活：data.task → subject 是任务 → 员工手上那件 / 最近那件
            # （工位"执行者自己 git commit"那条提醒暂时也用 error_loop，按文字排除，见 M4 汇报）
            e=s['employees'].get(sub) or {}
            tk=d.get('task') if d.get('task') in s['tasks'] else (sub if sub in s['tasks'] else (e.get('current_task') or e.get('last_task')))
            if tk in s['tasks']: s['tasks'][tk]['error_loop']={'seq':s['seq'],'ts':ts}
    elif typ == 'bridge.run':
        # 旧系统镜像（§9，只用于展示）：同一个 run 目录每换一次状态来一条，按 run 目录合并
        key=d.get('run_dir') or sub
        if key:
            r=s.setdefault('legacy_runs',{}).setdefault(key,{'run_dir':d.get('run_dir'),'em':None,'run':None})
            if isinstance(sub,str) and sub.startswith('em_'): r['em']=sub
            elif sub: r['run']=sub
            for k in ('agent','worker','title','status','started','ended','lead_session','dept','result_head'):
                if k in d: r[k]=_cp(d[k])
            r.update({'conv':conv,'ws':ws,'ts':ts})
    elif typ == 'bridge.letter':
        lid=sub or d.get('id')
        if lid:
            letters=s.setdefault('legacy_letters',{})
            kind=d.get('type') if d.get('type') in LETTER_KINDS else 'notice'
            x={'id':lid,'ts':ts,'conv':conv,'ws':ws,'from':_cp(_legacy_from(d.get('from'))),'to':d.get('to_worker') or d.get('to'),'kind':kind,
               'subject':d.get('subject') or '','body':d.get('body') or '','reply_to':d.get('reply_to'),'contract':None,'delivered':True,
               'status':'open' if kind=='request' and not d.get('verdict') else 'closed','verdict':d.get('verdict'),'legacy':True}
            letters[lid]=x
            orig=letters.get(d.get('reply_to')) if d.get('reply_to') else None
            if orig and orig.get('kind')=='request':
                orig['status']='closed'
                if d.get('verdict') and not orig.get('verdict'): orig['verdict']=d['verdict']
    elif typ == 'budget.stopped':   # 预算用完（§3.5）：记下来，规则监工不再自动重排这件活；你加了预算才解除
        if sub in s['tasks']: s['tasks'][sub]['budget_stopped']={'dim':d.get('dim'),'pct':d.get('pct'),'ts':ts}
        elif sub in s['employees']: s['employees'][sub]['budget_stopped_ts']=ts
    elif typ == 'control.requested':   # 总闸（§3.5）：页面要显示"已全部停下"
        action=d.get('action')
        if action=='stop_all': s['halt']={'seq':s['seq'],'ts':ts,'by':_by_name(by),'source':d.get('by_source')}
        elif action=='resume' and not sub: s['halt']=None
        elif action=='resume' and sub in s['employees']: s['employees'][sub].pop('budget_stopped_ts',None)
    elif typ in ('desk.removed', 'budget.warned', 'control.done'):
        pass       # 只记录（§4.3、§4.5）
    else:
        return s   # 认不出的事件类型：跳过（§0.5）
    # 对话最近的动静（页面默认对话 = 最近一条事件所属的对话，§8 v1.5）
    if conv is not None and conv in s['convs']:
        c=s['convs'][conv]
        if ts >= c.get('active_ts', ts):
            c['active_ts']=ts; c['active_seq']=s['seq']
    return s
