from .events import _reading
from .reducers import CHANGEABLE, dept_key, legacy_status, _legacy_from

AI_NAMES = {'codex': 'Codex', 'dsh': 'DeepSeek', 'agy': 'Gemini', 'claude': 'Claude'}
VERDICT_TEXT = {'accept': '同意', 'modify': '按修改办', 'reject': '不同意'}
PLAN_SUMMARY_MAX = 3      # 一版计划的一句话总结里最多列几处改动

def _who(s, conv, x):
    """信件里的 from/to → 给人看的名字：员工显示名、负责人、用户、本对话的部门名；认不出就原样。"""
    if not x: return ''
    if x == 'gm': return '负责人'
    if x == 'user': return '用户'
    if x in ('office','rules'): return '监工'      # 后台按规则自动写信、下指令的：页面上叫监工（负责人命令行里仍叫管家）
    e=(s or {}).get('employees',{}).get(x)
    if e: return e.get('label',x)
    dept=(s or {}).get('depts',{}).get(dept_key(conv,x))
    if dept: return dept.get('name') or x
    return x

def _label(s, em):
    if not em: return ''
    e=(s or {}).get('employees',{}).get(em)
    if e: return e.get('label',em)
    return em

def _dept_name(s, conv, *dept_ids):
    """第一个能在本对话里认出来的部门编号的名字；认不出返回空。"""
    for did in dept_ids:
        if did:
            rec=((s or {}).get('depts') or {}).get(dept_key(conv,did))
            if rec and rec.get('name'): return rec['name']
    return ''

def _dept_prefix(s, conv, task_dept, em):
    """"页面部 · "：这件活所属的部门，没有就用这位员工在本对话里所属的部门；都没有返回空串（不显示）。"""
    emp=((s or {}).get('employees') or {}).get(em) if em else None
    name=_dept_name(s, conv, task_dept, (emp.get('depts') or {}).get(conv) if emp else None)
    return name+' · ' if name else ''

def _task_title(s, conv, ref):
    """任务编号或短名 → 标题；认不出原样返回。"""
    tasks=(s or {}).get('tasks',{})
    t=tasks.get(ref)
    if t is None:
        t=next((x for x in tasks.values() if x.get('key')==ref and x.get('conv')==conv), None)
    return (t or {}).get('title') or ref or '任务'

def _short(title):
    """任务标题 → 给人看的短标题（去掉写给 AI 的要求、括号补充）；规则和详情栏同一份（office/plain.py）。"""
    from xay.office.plain import short_title   # 纯文字处理、只用标准库；放函数里免得 core 一加载就拉上 office
    return short_title(title) or title


def plan_change_text(s, conv, ch):
    """计划历史里的一处改动 → 大白话（页面"计划版本"栏、xay2 wait / brief 用）：
    "新增「X」（交给 Codex 1）" / "把「X」改给 Codex 2" / "把「X」改给前端部" / "把「X」改名为「Y」" / "取消「X」"。"""
    op=ch.get('op'); before=ch.get('before') or {}; after=ch.get('after') or {}
    name=_short(before.get('title') or ch.get('title') or _task_title(s,conv,ch.get('task')))
    if op=='add':
        by_em = after.get('by_em') or ch.get('by_em')
        if by_em:
            sender = _label(s, by_em) or by_em
            target = ''
            if after.get('owner'):
                target = _who(s, conv, after['owner'])
            elif after.get('agent'):
                target = AI_NAMES.get(after['agent'], after['agent'].capitalize())
            elif after.get('dept'):
                target = _who(s, conv, after['dept'])
            title_str = _short(after.get('title')) if after.get('title') else name
            to_str = f"给 {target}" if target else ""
            res = f"{sender} 派了「{title_str}」{to_str}".strip()
            reason = ch.get('reason') or after.get('reason')
            if reason:
                res += f"（{reason}）"
            return res
        extra=[]
        if after.get('owner'): extra.append('交给 '+_who(s,conv,after['owner']))
        elif after.get('dept'): extra.append('归'+_who(s,conv,after['dept']))
        deps=[_short(_task_title(s,conv,x)) for x in (after.get('deps') or [])]
        if deps: extra.append('等「%s」做完再开始' % '」「'.join(deps))
        return f"新增「{_short(after.get('title')) if after.get('title') else name}」"+(f"（{'，'.join(extra)}）" if extra else '')
    if op=='remove': return f"取消「{name}」"
    if op=='assign':
        new,old=after.get('owner'),before.get('owner')
        if new and old and new!=old: return f"把「{name}」从 {_who(s,conv,old)} 改给 {_who(s,conv,new)}"
        if new: return f"把「{name}」交给 {_who(s,conv,new)}"
        return f"「{name}」先不指定人"
    parts=[]
    for k,v in after.items():
        if k=='title': parts.append(f"改名为「{_short(v)}」")
        elif k=='dept': parts.append(f"改给{_who(s,conv,v)}" if v else '不再归哪个部门')
        elif k=='deps': parts.append(('改成等「%s」做完再开始' % '」「'.join(_task_title(s,conv,x) for x in v)) if v else '不用再等别的任务')
        elif k=='writes': parts.append(('要改的文件改成 '+'、'.join(v)) if v else '不再限定要改哪些文件')
        elif k=='steps': parts.append(f"步骤改成 {len(v)} 步" if v else '去掉了步骤')
        elif k=='card': parts.append('任务说明改了')
        elif k=='budget': parts.append('预算改了')
        elif k=='mode': parts.append('改成只读任务（只看不改）' if v=='readonly' else '改成要动手改文件的任务')
    return (f"把「{name}」"+'，'.join(parts)) if parts else f"「{name}」的计划改了"

def plan_summary(s, conv, entry):
    """一版计划的一句话："把「X」改给 Codex 2；新增「Y」"（超过 3 处只列前 3 处，再写"等 N 处改动"）。"""
    texts=[plan_change_text(s,conv,ch) for ch in (entry.get('changes') or [])]
    if len(texts)>PLAN_SUMMARY_MAX:
        return '；'.join(texts[:PLAN_SUMMARY_MAX])+f"；等 {len(texts)} 处改动"
    return '；'.join(texts)

def proposal_change_text(s, conv, c):
    """申请里的一处改动（{op, task, fields}）→ 大白话；"改之前"按现在的任务来写。"""
    op=c.get('op'); tk=c.get('task'); f=c.get('fields') or {}
    t=(s or {}).get('tasks',{}).get(tk) or {}
    if op=='add': ch={'op':'add','task':tk,'after':{k:f.get(k) for k in ('title','owner','dept','deps')}}
    elif op=='assign': ch={'op':'assign','task':tk,'title':t.get('title'),'before':{'owner':t.get('owner')},'after':{'owner':f.get('owner')}}
    elif op=='remove': ch={'op':'remove','task':tk,'title':t.get('title')}
    else: ch={'op':'change','task':tk,'title':t.get('title'),'before':{'title':t.get('title')},'after':{k:v for k,v in f.items() if k in CHANGEABLE}}
    return plan_change_text(s,conv,ch)

def text(event, conn=None, *, state=None):
    """一条事件的大白话（§8 recent[].text）。state 给了就直接用（snapshot 里），否则从 conn 读当前现状。"""
    if state is None and conn is not None:
        with _reading(conn) as s:
            return _text(event, s)
    return _text(event, state or {})

def _text(event, s):
    typ=event.get('type'); d=event.get('data') or {}; sub=event.get('subject'); conv=event.get('conv')
    tasks=s.get('tasks',{}) if s else {}; turns=s.get('turns',{}) if s else {}; emps=s.get('employees',{}) if s else {}
    turn=turns.get(sub) or {}
    # 这条事件说的是哪件活、哪位员工：事件自己带的 → 一轮里记的 → 主角本身 → 活的负责人
    task=tasks.get(sub) or tasks.get(d.get('task')) or tasks.get(turn.get('task')) or {}
    title=d.get('title') or task.get('title','')
    em=d.get('em') or turn.get('em') or (sub if sub in emps else None) or task.get('owner')
    label=_label(s,em)
    # 旧系统镜像一次会建几十个部门 / 员工：桥写的这几类事件不进动态，免得刷屏
    if event.get('by')=='bridge' and typ in ('dept.opened','dept.closed','employee.joined','employee.hired'): return None
    if typ=='conv.begun': return f"开启新对话：{d.get('title','')}"
    if typ=='conv.titled': return f"对话标题改为：{d.get('title','')}"
    if typ=='conv.ended': return '本次对话已圆满结束'
    if typ=='conversation.lead_changed':
        frm = AI_NAMES.get(d.get('from'), (d.get('from') or '').capitalize())
        to = AI_NAMES.get(d.get('to'), (d.get('to') or '').capitalize())
        return f"负责人从 {frm} 换成了 {to}"
    if typ=='dept.opened': return f"成立了{d.get('name',event.get('subject',''))}"
    if typ=='dept.closed': return f"关闭了{d.get('name',event.get('subject',''))}"
    if typ=='employee.hired': return f"新员工 {_label(s,event.get('subject'))} 加入团队"
    if typ=='employee.joined':
        name=_dept_name(s,conv,d.get('dept'))
        return f"{label} 加入了{name}" if name else None       # 认不出部门名就不说，绝不把内部编号摆给老板看
    if typ=='employee.online': return f"{label} 上线了"
    if typ=='employee.offline': return f"{label} 离线了"
    if typ=='employee.fired': return f"{label} 已离职"
    if typ=='task.added':
        if d.get('by_em'):
            who = _label(s, d.get('by_em'))
            target = _label(s, d.get('owner')) if d.get('owner') else AI_NAMES.get(d.get('agent'), (d.get('agent') or '').capitalize())
            to_str = f"给 {target}" if target else ""
            reason_str = f"（{d.get('reason')}）" if d.get('reason') else ""
            title_str = d.get('title', '')
            if to_str:
                return f"{who} 派了「{title_str}」{to_str}{reason_str}"
            return f"{who} 把「{title_str}」派进计划{reason_str}"
        return f"负责人把「{d.get('title','')}」排进计划"
    if typ=='task.changed': return f"「{title}」的计划已调整"
    if typ=='task.removed': return f"「{title}」已从计划中取消：{d.get('reason','')}"
    if typ=='task.assigned': return f"「{title}」交给 {_label(s,d.get('owner'))}：{d.get('reason','')}"
    if typ=='proposal.made': return f"{_who(s,conv,d.get('from'))} 提交了修改计划的申请：{d.get('reason','')}"
    if typ=='proposal.decided': return f"修改计划的申请已{'通过' if d.get('verdict')=='accept' else '拒绝'}"
    if typ=='task.ready': return f"「{title}」的前置工作已就绪"
    if typ=='task.queued':
        who=_label(s,d.get('owner')); retry=d.get('retry')
        if retry=='auto': return f"「{title}」没做成，监工让 {who} 接着修一次"
        if retry=='env': return f"「{title}」开工前出了点状况，监工重新排给 {who}"
        if retry=='manual': return f"「{title}」按决定再做一次，排进 {who} 的队列"
        return f"「{title}」已进入 {who} 的待办队列"
    if typ=='turn.started': return f"{label} 开始做「{title}」"
    if typ=='task.progress': return f"{label}：第{d.get('step')}步/共{d.get('of')}步，{d.get('text','')}"
    pre=_dept_prefix(s,conv,task.get('dept'),em) if typ in ('task.reported','task.landed','task.accepted') else ''
    if typ=='task.reported': return f"{pre}{label} 提交了「{title}」的工作汇报" if label else f"{pre}「{title}」有了工作汇报"
    if typ=='task.landed':
        plain_text = d.get('plain') or task.get('plain')
        if plain_text:
            return f"{pre}{label} 做完了「{title}」：{plain_text}" if label else f"{pre}「{title}」做完了：{plain_text}"
        return f"{pre}{label} 做完了「{title}」，改动已合并" if label else f"{pre}「{title}」做完了，改动已合并"
    if typ=='task.kept': return f"{label} 的改动已保留，需要你做决定"
    if typ=='task.accepted': return f"{pre}负责人验收通过了「{title}」"
    if typ=='task.failed': return f"「{title}」未能完成：{d.get('reason','')}"
    if typ=='task.canceled': return f"「{title}」已取消：{d.get('reason','')}"
    if typ=='context.compact_requested': return f"{label} 准备整理记忆"
    if typ=='context.compacted': return f"{label} 整理了一下记忆（{d.get('before_pct','?')}% → {d.get('after_pct','?')}%）"
    if typ=='notes.updated': return f"{label} 更新了工作笔记"
    if typ=='letter.sent': return f"{_who(s,event.get('conv'),d.get('from'))} 给 {_who(s,event.get('conv'),d.get('to'))} 写信：{d.get('subject','')}"
    if typ=='bridge.letter':
        conv=event.get('conv')
        return f"（旧系统）{_who(s,conv,_legacy_from(d.get('from')))} 给 {_who(s,conv,d.get('to_worker') or d.get('to'))} 写信：{d.get('subject','')}"
    if typ=='bridge.run':
        if d.get('adopt'): return None      # 桥给老记录补认员工时重发的那一条，不重复播报
        sub=event.get('subject')
        if isinstance(sub,str) and sub.startswith('em_') and sub in s.get('employees',{}):
            who=_label(s,sub)
        else:
            who=(AI_NAMES.get(d.get('agent'),'')+' 临时工').strip()
        status=legacy_status(d)
        pre=_dept_prefix(s,conv,d.get('dept'),sub if sub in emps else None)
        if status=='working': return f"（旧系统）{pre}{who} 开始做「{d.get('title','')}」"
        if status=='done': return f"（旧系统）{pre}{who} 做完了「{d.get('title','')}」"
        if status=='failed': return f"（旧系统）{pre}{who} 没做成「{d.get('title','')}」"
        return None
    if typ=='letter.answered':
        # 模板（docs/humanize.md）："{回信人} 回复了信件：{简要}"；结论用中文，后面带原信主题
        by=str(event.get('by') or '')
        who='负责人' if by.startswith('lead:') else ('用户' if by=='user' else _who(s,conv,by.split(':',1)[-1]))
        orig=(s.get('letters',{}) if s else {}).get(sub) or {}
        return f"{who} 回复了信件：{VERDICT_TEXT.get(d.get('verdict'),'')}"+(f"「{orig['subject']}」" if orig.get('subject') else '')
    if typ=='decision.opened': return f"有新事项需要你拿主意：{d.get('title','')}"
    if typ=='decision.resolved':
        if event.get('by')=='rules': return None     # 规则监工自动关掉 / 自动再合并：另有提醒，不重复播报
        return '需要你的事项已处理完成'
    if typ=='recall.requested': return f"{label} 整理记忆后没复述出来，下一轮再给他看一次笔记、重新复述"
    if typ=='signal.raised': return f"提醒：{d.get('text','')}"
    dim={'tokens':'token','minutes':'时长','turns':'轮数'}.get(d.get('dim'),'')
    dim=f"（{dim}）" if dim else ''
    if typ=='budget.warned':
        if sub in emps: return f"{label} 今天的额度用了 {d.get('pct','?')}%{dim}"
        return f"「{title}」的预算用了 {d.get('pct','?')}%{dim}"
    if typ=='budget.stopped':
        if sub in emps: return f"{label} 今天的额度用完了{dim}，先让他停下"
        return f"「{title}」的预算用完了{dim}，已经让他停下"
    if typ=='control.requested':
        a=d.get('action'); by=event.get('by',''); txt=str(d.get('text') or '').strip()
        src='监工' if by in ('rules','office') else ('你' if by=='user' else '负责人')
        who=label or '员工'
        if a=='stop_all': return f"{src}拉了总闸：所有员工都停下，改动都保留"
        if a=='resume': return f"{src}让 {who} 接着干" if sub else f"{src}松开了总闸：大家接着干"
        if a=='steer': return f"{src}对 {who} 说：{txt}" if txt else None
        if a=='interrupt': return f"{src}叫 {who} 先停一下" + (f"：{txt}" if txt else '')
        if a=='stop': return (f"{src}叫停了「{title}」" if sub in tasks else f"{src}叫 {who} 停下") + (f"：{txt}" if txt else '')
        return None
    if typ=='control.done':
        if d.get('result')=='failed': return f"没能让 {label or '员工'} 照做：{d.get('detail','')}"
        if event.get('by')=='office' and d.get('detail'): return f"总闸：{label} 的{d.get('detail')}"
        return None
    return None
