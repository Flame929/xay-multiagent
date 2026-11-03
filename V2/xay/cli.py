"""xay2 命令行（protocol §7）。

- 执行者（XAY_ROLE=executor）：只能 progress / done / notes / ask / tell / reply / plan propose，
  只往发件箱追加一行（§6），**不打开数据库**。
- 负责人：其余命令写账本；"当前对话"按 §1 找，找不到就报错，不猜。
"""
import argparse, json, os, re, subprocess, sys, time
from pathlib import Path

from . import providers

DEPT_RE = re.compile(r'^[a-z][a-z0-9_-]{0,15}$')
AGENTS = providers.agent_kinds()
AGENT_NAMES = {'codex': 'Codex', 'dsh': 'DeepSeek', 'agy': 'Gemini'}
STATUS_TEXT = {'planned': '计划中', 'ready': '可以开始', 'queued': '排队中', 'working': '在做', 'needs_you': '等你决定',
               'reported': '待验收', 'done': '已完成', 'failed': '没做成', 'canceled': '已取消'}
KEPT_TEXT = {'mismatch': '结果对不上', 'failed_with_changes': '没做完但改了文件', 'paused': '暂停了', 'conflict': '合并有冲突',
             'interrupted': '被打断了', 'desk_unmerged': '桌子上还有没合并的改动'}
EXECUTOR_ONLY = ('progress', 'done', 'notes')
OUTBOX_LINE_LIMIT = 4096
WAIT_MAX_LINES = 15
WAIT_MERGE_SECONDS = 600


class CliError(Exception):
    """给用户看的错误（中文），退出码 2。"""


def _err(msg):
    print(msg, file=sys.stderr)


# ================================================================ 参数

def _parser():
    parser = argparse.ArgumentParser(prog='xay2')
    parser.add_argument('--conv', help='对话编号或别名')
    sp = parser.add_subparsers(dest='cmd', required=True)
    p = sp.add_parser('begin'); p.add_argument('--title', required=True); p.add_argument('--lead', default='claude')
    p = sp.add_parser('dept'); ss = p.add_subparsers(dest='sub', required=True)
    q = ss.add_parser('open'); q.add_argument('id'); q.add_argument('--name', required=True); q.add_argument('--mission', default=''); q.add_argument('--owns', default=''); q.add_argument('--head', default='rules'); q.add_argument('--part')
    ss.add_parser('close').add_argument('id')
    p = sp.add_parser('hire'); p.add_argument('agent'); p.add_argument('--label'); p.add_argument('--line')
    p = sp.add_parser('plan'); ss = p.add_subparsers(dest='sub', required=True)
    q = ss.add_parser('add'); q.add_argument('title'); q.add_argument('--key'); q.add_argument('--to'); q.add_argument('--new', action='store_true'); q.add_argument('--agent', default='auto'); q.add_argument('--dept'); q.add_argument('--part'); q.add_argument('--after', default=''); q.add_argument('--writes', default=''); q.add_argument('--step', action='append', default=[]); q.add_argument('--readonly', action='store_true'); q.add_argument('--card'); q.add_argument('--reason', default=''); q.add_argument('--force', action='store_true')
    q = ss.add_parser('apply'); q.add_argument('file'); q.add_argument('--dry-run', action='store_true')
    q = ss.add_parser('move'); q.add_argument('task'); q.add_argument('--to', required=True); q.add_argument('--reason', default='')
    q = ss.add_parser('drop'); q.add_argument('task'); q.add_argument('--reason', default='')
    q = ss.add_parser('edit'); q.add_argument('task'); q.add_argument('--title'); q.add_argument('--writes'); q.add_argument('--dept'); q.add_argument('--part'); q.add_argument('--after'); q.add_argument('--step', action='append'); q.add_argument('--card'); q.add_argument('--mode', choices=('write', 'readonly')); q.add_argument('--reason', default=''); q.add_argument('--force', action='store_true')
    q = ss.add_parser('propose'); q.add_argument('--reason', default=''); q.add_argument('op', choices=('add', 'change', 'remove', 'assign', 'move')); q.add_argument('task'); q.add_argument('--to'); q.add_argument('--title')
    q = ss.add_parser('part'); q.add_argument('title'); q.add_argument('--why'); q.add_argument('--key'); q.add_argument('--after', default=''); q.add_argument('--check'); q.add_argument('--check-text'); q.add_argument('--reason', default='')
    q = ss.add_parser('part-edit'); q.add_argument('part'); q.add_argument('--title'); q.add_argument('--why'); q.add_argument('--after'); q.add_argument('--check'); q.add_argument('--check-text'); q.add_argument('--reason', default='')
    q = ss.add_parser('part-drop'); q.add_argument('part'); q.add_argument('--reason', default='')
    ss.add_parser('show')
    ss.add_parser('open')
    q = ss.add_parser('template'); q.add_argument('--title')
    p = sp.add_parser('answer'); p.add_argument('id'); p.add_argument('verdict'); p.add_argument('text', nargs='?')
    for name in ('ask', 'tell'):
        p = sp.add_parser(name); p.add_argument('to'); p.add_argument('body'); p.add_argument('--subject', default='')
    p = sp.add_parser('reply'); p.add_argument('letter'); p.add_argument('body'); p.add_argument('--subject', default='')
    p = sp.add_parser('progress'); p.add_argument('step'); p.add_argument('text')
    p = sp.add_parser('done'); p.add_argument('summary', nargs='?', default='完成')
    p = sp.add_parser('notes'); p.add_argument('text', nargs='?', default=None); p.add_argument('--add', default=None); p.add_argument('--remove', default=None)
    p = sp.add_parser('notes-set'); p.add_argument('em'); p.add_argument('file')
    p = sp.add_parser('accept'); p.add_argument('task'); p.add_argument('--note', default='')
    p = sp.add_parser('check'); p.add_argument('task'); p.add_argument('verdict', choices=('pass', 'fail')); p.add_argument('text', nargs='?', default='')
    p = sp.add_parser('verdict'); p.add_argument('verdict', choices=('pass', 'fail')); p.add_argument('text', nargs='?', default='')
    p = sp.add_parser('steer'); p.add_argument('em'); p.add_argument('text')
    p = sp.add_parser('compact'); p.add_argument('em'); p.add_argument('--method', choices=('native', 'handoff', 'auto'), default='auto'); p.add_argument('--reason', default=None)
    p = sp.add_parser('interrupt'); p.add_argument('em')
    p = sp.add_parser('stop'); p.add_argument('target', nargs='?'); p.add_argument('--all', action='store_true')
    p = sp.add_parser('resume'); p.add_argument('target', nargs='?')
    p = sp.add_parser('wait'); p.add_argument('--since', type=int, default=0); p.add_argument('--timeout', type=float, default=240)
    p = sp.add_parser('notify'); p.add_argument('--hook', action='store_true', help='按 Claude Code 钩子格式输出通知')
    p = sp.add_parser('hook'); ss = p.add_subparsers(dest='sub', required=True)
    ss.add_parser('install')
    ss.add_parser('uninstall')
    ss.add_parser('status')
    p = sp.add_parser('brief'); p.add_argument('--json', action='store_true'); p.add_argument('--latest', action='store_true')
    p = sp.add_parser('handoff'); p.add_argument('--to', choices=['codex', 'dsh', 'agy', 'claude'], help='移交给哪位新负责人'); p.add_argument('--conv', help='指定对话编号')
    p = sp.add_parser('snapshot'); p.add_argument('--json', action='store_true'); p.add_argument('--latest', action='store_true')
    p = sp.add_parser('events'); p.add_argument('--since', type=int, default=0); p.add_argument('--follow', action='store_true')
    p = sp.add_parser('agy-accounts'); p.add_argument('--json', action='store_true')
    p = sp.add_parser('agy-switch'); p.add_argument('target', nargs='?', default='auto')
    p = sp.add_parser('backup'); p.add_argument('--keep', type=int, default=7)
    sp.add_parser('rebuild')
    sp.add_parser('doctor')
    sp.add_parser('up')
    sp.add_parser('board')
    return parser


def main(argv=None):
    argv = sys.argv[1:] if argv is None else argv
    executor = os.environ.get('XAY_ROLE') == 'executor'
    if argv and argv[0] == 'doctor':
        return _doctor(argv[1:])
    if argv and argv[0] == 'up':
        if executor:
            _err('执行者不能使用 up 命令'); return 2
        return _up(argv[1:])
    if argv and argv[0] == 'station':
        if executor:
            _err('执行者不能开工位'); return 2
        from .station.station import main as station_main
        return station_main(argv[1:])
    if argv and argv[0] == 'office':
        if executor:
            _err('执行者不能管管家'); return 2
        try:
            from .office.daemon import main as office_main
        except ModuleNotFoundError:
            print('管家还没装好'); return 2
        return office_main(argv[1:])
    a = _parser().parse_args(argv)
    try:
        return _executor(a) if executor else _lead(a)
    except CliError as exc:
        _err(str(exc)); return 2


# ================================================================ 执行者：只写发件箱，不碰数据库（§6）

def _outbox_path():
    explicit = os.environ.get('XAY2_OUTBOX')
    if explicit:
        return Path(explicit)
    root = Path.cwd()
    try:
        out = subprocess.run(['git', 'rev-parse', '--show-toplevel'], cwd=str(root), text=True, capture_output=True, timeout=10)
        if out.returncode == 0 and out.stdout.strip():
            root = Path(out.stdout.strip())
    except (OSError, subprocess.SubprocessError):
        pass
    return root / '.xay' / 'out' / 'outbox.jsonl'


def _subject_of(body, subject):
    if subject:
        return subject
    first = (body or '').strip().splitlines()[0] if (body or '').strip() else ''
    return first[:30]


def _executor(a):
    allowed = a.cmd in ('progress', 'done', 'notes', 'ask', 'tell', 'reply', 'verdict', 'board') or (a.cmd == 'plan' and a.sub in ('propose', 'add'))
    if not allowed:
        raise CliError('执行者只能用：progress / done / notes / ask / tell / reply / verdict / board / plan propose / plan add（只写发件箱）')
    if a.cmd == 'board':
        board_file = os.environ.get('XAY2_BOARD_FILE')
        path = Path(board_file) if board_file else _outbox_path().parent / 'board.md'
        if not path.is_file():
            print('目前还没有任务板')
            return 0
        try:
            content = path.read_text(encoding='utf-8')
            print(content.rstrip())
            return 0
        except OSError as exc:
            raise CliError('无法读取任务板：%s' % exc)
    # 一个原生进程跨好几件活（Codex app-server）时，进程启动时的 XAY2_TASK 会过期：
    # 工位每一轮开工前把当前任务写进 XAY2_TASK_FILE，这里优先读它（读不到再用 XAY2_TASK）
    task = ''
    task_file = os.environ.get('XAY2_TASK_FILE')
    if task_file:
        try:
            task = Path(task_file).read_text(encoding='utf-8').strip()
        except OSError:
            task = ''
    task = task or (os.environ.get('XAY2_TASK') or '').strip()
    em = (os.environ.get('XAY2_EM') or '').strip() or None
    if a.cmd == 'notes':
        if not em:
            raise CliError('不知道你是谁：没有设置 XAY2_EM（应由工位设置），这次什么都没写')
    else:
        if not task:
            raise CliError('不知道你在做哪件事：没有设置 XAY2_TASK（应由工位设置），这次什么都没写')
    if a.cmd == 'progress':
        kind, data = 'progress', _progress_data(a)
    elif a.cmd == 'done':
        kind, data = 'report', {'claim': 'done', 'summary': a.summary}
    elif a.cmd == 'notes':
        options = [opt for opt in (('set', a.text), ('add', a.add), ('remove', a.remove)) if opt[1] is not None]
        if len(options) != 1:
            raise CliError('用法：xay2 notes "全文" / xay2 notes --add "一条" / xay2 notes --remove "关键词"')
        op, text = options[0]
        kind = 'notes'
        data = {'kind': 'notes', 'op': op, 'text': text}
        if em:
            data['em'] = em
    elif a.cmd in ('ask', 'tell'):
        kind = 'letter'
        data = {'to': a.to, 'kind': 'request' if a.cmd == 'ask' else 'tell', 'subject': _subject_of(a.body, a.subject), 'body': a.body}
        if em: data['from'] = em
    elif a.cmd == 'reply':
        kind = 'letter'
        data = {'reply_to': a.letter, 'kind': 'reply', 'subject': _subject_of(a.body, a.subject), 'body': a.body}
        if em: data['from'] = em
    elif a.cmd == 'verdict':
        kind = 'verdict'
        ok = (a.verdict.lower() == 'pass')
        data = {'ok': ok, 'text': a.text or ''}
    elif a.cmd == 'plan' and a.sub == 'add':
        kind, data = 'task', _executor_task(a)
        if em: data['from'] = em
    else:
        kind, data = 'proposal', _executor_proposal(a)
        if em: data['from'] = em
    row = {'v': 1, 'task': task, 'type': kind, 'data': data}
    if a.cmd == 'notes' and em:
        row['em'] = em
    line = json.dumps(row, ensure_ascii=False)
    if len(line.encode('utf-8')) > OUTBOX_LINE_LIMIT:
        raise CliError('这一条太长了（超过 4 KB），请写短一点，这次什么都没写')
    path = _outbox_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, 'a', encoding='utf-8') as f:
        f.write(line + '\n')
    return 0


def _progress_data(a):
    m = re.match(r'^\s*(\d+)\s*/\s*(\d+)\s*$', a.step or '')
    if not m:
        raise CliError('进度要写成"第几步/共几步"，例如：xay2 progress 2/4 "写测试"')
    step, of = int(m.group(1)), int(m.group(2))
    if of < 1 or step < 0 or step > of:
        raise CliError('进度的步数不对：第 %d 步 / 共 %d 步' % (step, of))
    return {'step': step, 'of': of, 'text': a.text}


def _executor_task(a):
    writes = _split(a.writes)
    if not a.readonly and not writes:
        raise CliError('执行者派活要改文件必须指定 --writes，只读活请加 --readonly')
    card = _read_card(a.card)
    data = {
        'title': a.title,
        'writes': writes,
        'mode': 'readonly' if a.readonly else 'write',
        'readonly': bool(a.readonly),
    }
    if a.agent and a.agent != 'auto':
        data['agent'] = a.agent
    if a.to:
        data['to'] = a.to
    if card is not None:
        data['card'] = card
    if a.reason:
        data['reason'] = a.reason
    if a.step:
        data['steps'] = list(a.step)
    return data


def _executor_proposal(a):
    """执行者读不了账本：任务按给的原样写（工位按 §4 校验），--to 只认员工编号 em_… 或部门短名。"""
    to = (a.to or '').strip()
    if a.op == 'add':
        change = {'op': 'add', 'task': _new_id('tk_'), 'fields': {'title': a.title or a.task}}
        if to.startswith('em_'): change['fields']['owner'] = to
        elif to and DEPT_RE.match(to): change['fields']['dept'] = to
        elif to: raise CliError('--to 请写员工编号（em_…）或部门短名')
    elif a.op == 'remove':
        change = {'op': 'remove', 'task': a.task}
    elif a.op == 'change':
        if not a.title:
            raise CliError('申请改任务要说改成什么：--title "新标题"')
        change = {'op': 'change', 'task': a.task, 'fields': {'title': a.title}}
    else:  # assign / move
        if not to:
            raise CliError('要说交给谁：--to <员工编号 em_…>' + ('' if a.op == 'assign' else ' 或 <部门短名>'))
        if to.startswith('em_'):
            change = {'op': 'assign', 'task': a.task, 'fields': {'owner': to}}
        elif a.op == 'move' and DEPT_RE.match(to):
            change = {'op': 'change', 'task': a.task, 'fields': {'dept': to}}
        else:
            raise CliError('--to 请写员工编号（em_…）' + ('' if a.op == 'assign' else '或部门短名'))
    return {'changes': [change], 'reason': a.reason, 'to': 'gm'}


def _new_id(prefix):
    from .core.ids import new_id
    return new_id(prefix)


# ================================================================ 负责人

class Ctx:
    def __init__(self, conn, conv, state):
        self.conn, self.conv, self.by = conn, conv, 'lead:' + conv
        self.state = state
        c = state['convs'][conv]
        self.ws = c.get('ws') or _cwd_ws()

    def refresh(self):
        from .core import load_state
        self.state = load_state(self.conn)

    def append(self, typ, data, subject, by=None):
        from .core import append
        try:
            return append(self.conn, typ, by or self.by, data, ws=self.ws, conv=self.conv, subject=subject)
        except Exception as exc:  # 账本拒绝：权限 / 数据不对
            from .core.errors import PermissionError as LedgerPermissionError
            if isinstance(exc, (LedgerPermissionError, ValueError)):
                raise CliError('账本没收下这一条：%s' % exc)
            raise

    def say(self, seq):
        """写完后用大白话回一句（和页面上的一样）。"""
        from .core import since
        from .core.humanize import text
        ev = since(self.conn, seq - 1, limit=1)
        line = text(ev[0], self.conn) if ev and ev[0]['seq'] == seq else None
        if line:
            print(line)

    # ---- 查找
    def tasks(self):
        return {k: t for k, t in self.state.get('tasks', {}).items() if t.get('conv') == self.conv}

    def task(self, ref):
        tasks = self.tasks()
        if ref in tasks:
            return ref
        for k, t in tasks.items():
            if t.get('key') == ref:
                return k
        raise CliError('找不到任务：%s（用任务编号 tk_… 或短名）' % ref)

    def parts(self):
        return {k: p for k, p in self.state.get('parts', {}).items() if p.get('conv') == self.conv}

    def part(self, ref):
        """中任务可以用编号（pt_…）、key 或标题指定 → 返回编号 pt_..."""
        parts = self.parts()
        if ref in parts:
            return ref
        for k, p in parts.items():
            if p.get('key') == ref:
                return k
        for k, p in parts.items():
            if p.get('title') == ref:
                return k
        raise CliError('找不到中任务：%s（用中任务编号 pt_…、短名或标题）' % ref)

    def employees(self):
        return {k: e for k, e in self.state.get('employees', {}).items() if e.get('ws') == self.ws and not e.get('former')}

    def employee(self, ref):
        """员工编号或显示名（"Codex 1"、"Codex1"、大小写不限）→ em；找不到返回 None。"""
        emps = self.employees()
        if ref in emps:
            return ref
        norm = ref.replace(' ', '').lower()
        hits = [k for k, e in emps.items() if (e.get('label') or '').replace(' ', '').lower() == norm]
        return hits[0] if len(hits) == 1 else None

    def depts(self):
        """本对话里开着的部门，按短名索引（部门编号只在对话内唯一）。"""
        return {d['id']: d for d in self.state.get('depts', {}).values() if d.get('conv') == self.conv and not d.get('closed')}

    def label(self, em):
        e = self.state.get('employees', {}).get(em)
        return e.get('label', em) if e else (em or '')


def _cwd_ws():
    from .core.ids import ws_for
    return ws_for(os.getcwd())


def _split(text):
    return [x.strip() for x in (text or '').split(',') if x.strip()]


def _no_conv_message():
    return ('不知道当前是哪个对话：请先运行 xay2 begin --title "…"（或用 --conv 指定对话；'
            'snapshot / brief 可以加 --latest 看最近的对话）')


def _resolve(conn, a, state):
    from .core import resolve_conv
    conv = resolve_conv(conn, a.conv, os.environ)
    if conv:
        return conv
    given = a.conv or os.environ.get('XAY2_CONV')
    if given:
        raise CliError('找不到对话：%s（先运行 xay2 begin，或检查 --conv / XAY2_CONV）' % given)
    return None


def _latest_conv(state):
    """--latest：本工作区里最近有动静的对话（和页面默认对话同一个规则），本工作区没有就看全部。"""
    from .core.snapshot import pick_conv
    return pick_conv(state, _cwd_ws()) or pick_conv(state)


def _agy_accounts_cmd(a):
    from . import quota, quota_live
    from .core import open_db
    fresh = []
    try:
        conn = open_db()
        try:
            rows = conn.execute(
                "SELECT data, ts FROM events WHERE type='quota.updated' ORDER BY seq DESC LIMIT 50"
            ).fetchall()
            for row in rows:
                try:
                    d = json.loads(row[0])
                except (ValueError, TypeError):
                    continue
                if isinstance(d, dict) and d.get("line") == "agy":
                    src = d.get("source") or quota_live.SOURCE.get("agy", "agy 官方 /usage")
                    for item in d.get("accounts") or []:
                        m = dict(item)
                        if not m.get("source"):
                            m["source"] = src
                        fresh.append(m)
                    break
        finally:
            conn.close()
    except Exception:
        fresh = []

    data = quota.merged_agy_accounts(fresh=fresh)
    if not data:
        try:
            data = quota.agy_accounts()
        except Exception:
            data = []
    if getattr(a, 'json', False):
        print(json.dumps(data, ensure_ascii=False, indent=2))
        return 0
    if not data:
        print("没找到 Antigravity Tools 的账号清单")
        return 0
    words = {"ok": "充足", "tight": "偏紧", "exhausted": "用完", "unknown": "未知", "blocked": "停用"}
    for item in data:
        left = item.get("left") if item.get("left") is not None else item.get("remaining_pct")
        pct = "" if left is None else f" {left}%"
        checked = item.get("checked_at")
        when = time.strftime("%m-%d %H:%M", time.localtime(checked)) if checked else "没有记录"
        prio = f"（优先级 {item.get('priority')}）" if item.get("priority") is not None else ""
        cur = "👉" if item.get("current") else "  "
        st = words.get(item.get("status"), item.get("status"))
        src = item.get("source") or "—"
        print(f"{cur} {item.get('index')}. {item.get('name')}{prio}：{st}{pct}；数据来自{src}，{when}")
    return 0


def _agy_switch_cmd(a):
    from . import quota
    target = getattr(a, 'target', 'auto') or 'auto'
    res = quota.switch_agy_account(reason="负责人手动切换", target=target)
    print(res.get("text", "切换完成"))
    return 0 if res.get("ok") else 1


def _plan_template(a):
    title = getattr(a, 'title', None) or "开发用户登录与注册模块"
    tpl = {
        "_说明": "【填表说明】所有以 _ 开头的字段都是中文注释，plan apply 时会自动忽略。标题必须用大白话（说清做什么能达成什么，≤30字），不准出现文件路径（如 api/user.py）、函数括号调用（如 login()）、英文单词（如 loginUser）、反引号、命令等。",
        "title": title,
        "groups": [
            {
                "_说明": "【示例 1：按前后端划分的中任务】why 必填（向老板说明做完后能得到什么），check 选填验收方式（rules、ai:codex、ai:dsh、ai:agy、lead，默认自动检查），after 依赖前置中任务短名",
                "key": "p_backend",
                "title": "打通用户认证后端接口",
                "why": "让系统具备安全可靠的注册与登录接口支持",
                "after": [],
                "check": "rules",
                "tasks": [
                    {
                        "_说明": "【小任务 1】title 必须大白话；agent 指派 AI（codex/dsh/agy）；writes 必填改动文件清单；verify 必填验收命令；card 详细说明需求",
                        "key": "t_db_model",
                        "title": "建立用户数据表与存储",
                        "agent": "codex",
                        "writes": ["db/user.py", "db/test_user.py"],
                        "verify": ["python3 -m unittest db/test_user.py"],
                        "card": "设计并实现用户存储结构，包含密码加盐存储与查询接口"
                    },
                    {
                        "_说明": "【小任务 2】deps 可以引用前面已定义的小任务短名；writes 与 verify 严格对应改动与验证",
                        "key": "t_api_route",
                        "title": "编写注册登录处理逻辑",
                        "agent": "codex",
                        "deps": ["t_db_model"],
                        "writes": ["api/auth.py", "api/test_auth.py"],
                        "verify": ["python3 -m unittest api/test_auth.py"],
                        "card": "实现注册校验、登录生成令牌与登出注销接口"
                    }
                ]
            },
            {
                "_说明": "【示例 2：按功能划分的中任务】示意按功能模块分工；after 填 ['p_backend'] 表示等前置中任务全部完成才开工",
                "key": "p_frontend",
                "title": "实现用户登录注册界面交互",
                "why": "让用户在网页端能够直观输入账号密码并顺利登录",
                "after": ["p_backend"],
                "check": "lead",
                "tasks": [
                    {
                        "_说明": "【小任务 1】前端交互组件；支持多文件改动，验收命令使用对应检查脚本",
                        "key": "t_fe_form",
                        "title": "搭建登录注册表单组件",
                        "agent": "agy",
                        "writes": ["web/auth_form.js", "web/auth.css"],
                        "verify": ["node --check web/auth_form.js"],
                        "card": "实现输入框、表单校验提示以及提交状态切换"
                    },
                    {
                        "_说明": "【小任务 2】对接后端服务，完成全链路交互流程",
                        "key": "t_fe_connect",
                        "title": "对接登录接口与状态提示",
                        "agent": "agy",
                        "deps": ["t_fe_form"],
                        "writes": ["web/auth_api.js"],
                        "verify": ["node --check web/auth_api.js"],
                        "card": "调用后端认证接口，登录成功后跳转并展示用户信息"
                    }
                ]
            }
        ]
    }
    print(json.dumps(tpl, ensure_ascii=False, indent=2))
    return 0


def _strip_underscores(obj):
    if isinstance(obj, dict):
        return {k: _strip_underscores(v) for k, v in obj.items() if not k.startswith('_')}
    if isinstance(obj, list):
        return [_strip_underscores(x) for x in obj]
    return obj


def _check_part_deadlock(ctx, target_pt, task_id=None, force=False):
    if force:
        return
    part_info = ctx.parts().get(target_pt)
    if not part_info:
        return
    if task_id:
        t_info = ctx.tasks().get(task_id) or ctx.state.get('tasks', {}).get(task_id) or {}
        if t_info.get('status') == 'done':
            return
    target_keys = {target_pt}
    if part_info.get('key'):
        target_keys.add(part_info['key'])
    tasks = ctx.tasks()
    p_tasks = [t for t in tasks.values() if t.get('part') in target_keys and not t.get('legacy')]
    unretracted_p = [t for t in p_tasks if t.get('status') != 'canceled']
    if not unretracted_p or not all(t.get('status') == 'done' for t in unretracted_p):
        return
    for q_id, q_info in ctx.parts().items():
        if q_id == target_pt:
            continue
        after_list = _as_list(q_info.get('after'))
        if any(a in target_keys for a in after_list):
            q_keys = {q_id}
            if q_info.get('key'):
                q_keys.add(q_info['key'])
            q_tasks = [t for t in tasks.values() if t.get('part') in q_keys and not t.get('legacy')]
            q_unretracted = [t for t in q_tasks if t.get('status') != 'canceled']
            if any(t.get('status') in ('planned', 'ready', 'queued') for t in q_unretracted):
                q_title = q_info.get('title') or q_id
                raise CliError(f"这样会让『{q_title}』里的小任务一直等着，建议放进还没做完的中任务")


def _diagnose():
    """执行核心体检，返回 (all_passed, checks, available_ai)。
    checks: list of (tag, text)，tag 包含 'pass', 'fail', 'info'。
    available_ai: list of str，当前环境中可用且已安装的 AI 种类。
    """
    import shutil
    import socket
    from .office import paths

    all_passed = True
    checks = []

    # 1. Python 版本（必需项：>= 3.11）
    py_ver = sys.version_info
    py_str = f"{py_ver[0]}.{py_ver[1]}.{py_ver[2]}"
    if py_ver >= (3, 11):
        checks.append(('pass', f"[✓] Python 版本：{py_str}（满足 3.11+ 要求）"))
    else:
        all_passed = False
        checks.append(('fail', f"[✗] Python 版本：{py_str}（xay 需要 Python 3.11 或更新的版本，请先升级）"))

    # 2. SQLite 支持（必需项）
    sqlite_ok = False
    sqlite_err = ""
    try:
        import sqlite3
        conn = sqlite3.connect(":memory:")
        conn.execute("CREATE TABLE _doctor_test (id INT)")
        conn.close()
        sqlite_ok = True
    except Exception as exc:
        sqlite_err = str(exc)
    if sqlite_ok:
        checks.append(('pass', "[✓] SQLite3：支持正常"))
    else:
        all_passed = False
        checks.append(('fail', f"[✗] SQLite3：不可用（{sqlite_err}）"))

    # 3. git 能用（必需项）
    git_path = shutil.which("git")
    if git_path:
        checks.append(('pass', f"[✓] git：已安装（{git_path}）"))
    else:
        all_passed = False
        checks.append(('fail', "[✗] git：未找到（请先安装 git 并加入 PATH）"))

    # 4. 管家在不在跑（非错误项）
    keeper_info = None
    try:
        from .office.daemon import running_info
        keeper_info = running_info()
    except Exception:
        keeper_info = None

    if keeper_info:
        pid, port, uptime = keeper_info
        checks.append(('pass', f"[✓] 管家状态：正在运行（进程 {pid}，端口 {port}）"))
    else:
        checks.append(('info', "[-] 管家状态：未在运行（需要时可使用 xay2 office start 启动）"))

    # 5. 端口 7878 / 7879 占用检查（必需项）
    def _is_port_occupied(port):
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=0.3):
                return True
        except OSError:
            pass
        try:
            with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
                s.bind(("127.0.0.1", port))
                return False
        except OSError:
            return True

    occ_7879 = _is_port_occupied(7879)
    if not occ_7879:
        checks.append(('pass', "[✓] 端口 7879（公司页）：空闲可用"))
    elif keeper_info is not None and keeper_info[1] == 7879:
        checks.append(('pass', "[✓] 端口 7879（公司页）：正常（管家正在使用）"))
    else:
        all_passed = False
        checks.append(('fail', "[✗] 端口 7879（公司页）：被别的程序占用了（启动管家前请先释放）"))

    occ_7878 = _is_port_occupied(7878)
    if not occ_7878:
        checks.append(('pass', "[✓] 端口 7878（看板）：空闲可用"))
    elif keeper_info is not None:
        checks.append(('pass', "[✓] 端口 7878（看板）：正常（管家正在使用）"))
    else:
        all_passed = False
        checks.append(('fail', "[✗] 端口 7878（看板）：被别的程序占用了（启动看板前请先释放）"))

    # 6. $XAY2_HOME 能不能写（必需项）
    home_dir = paths.v2_home()
    target = home_dir
    while not target.exists() and target.parent != target:
        target = target.parent
    writable = target.exists() and os.access(target, os.W_OK | os.X_OK)
    if writable:
        checks.append(('pass', f"[✓] 数据目录（$XAY2_HOME）：{home_dir}（可读写）"))
    else:
        all_passed = False
        checks.append(('fail', f"[✗] 数据目录（$XAY2_HOME）：{home_dir}（无写权限，请检查目录权限）"))

    # 7. 基础 AI 命令行工具检查（非必需项，未装不视为错误）
    ai_tools = ("codex", "dsh", "agy", "antigravity-tools")
    for tool in ai_tools:
        found = shutil.which(tool)
        if found:
            checks.append(('pass', f"[✓] {tool}：已安装（{found}）"))
        else:
            checks.append(('info', f"[-] {tool}：没装：这家 AI 的活派不出去，装好后再来"))

    # 8. 通用员工检查（providers.agent_kinds()）
    try:
        from . import providers
        kinds = providers.agent_kinds()
    except Exception:
        kinds = ("codex", "dsh", "agy")

    for kind in kinds:
        if kind in ("codex", "dsh", "agy"):
            continue
        try:
            p = providers.get(kind)
            exe = p.binary
        except Exception:
            exe = kind
        found = shutil.which(exe)
        if found:
            checks.append(('pass', f"[✓] {kind}：已安装（{found}）"))
        else:
            checks.append(('info', f"[-] {kind}：没装：这家 AI 的活派不出去，装好后再来"))

    # 统计能用的 AI 种类
    available_ai = []
    for tool in ("codex", "dsh", "agy"):
        if shutil.which(tool):
            available_ai.append(tool)
    for kind in kinds:
        if kind in ("codex", "dsh", "agy"):
            continue
        try:
            p = providers.get(kind)
            exe = p.binary
            avail = p.available
        except Exception:
            exe = kind
            avail = True
        if shutil.which(exe) and avail:
            available_ai.append(kind)

    return all_passed, checks, available_ai


def _doctor(argv=None):
    """一键环境体检：只读检查核心依赖、端口、AI CLI 及数据目录。"""
    all_passed, checks, _ = _diagnose()
    for tag, text in checks:
        print(text)
    if all_passed:
        print("体检通过！所有必需项均已满足。")
        return 0
    else:
        print("体检未通过：存在未满足的必需项，请先修复后再试。")
        return 1


def _start_keeper():
    """启动管家后台守护进程（提供给 up 命令使用，测试可 mock）。"""
    from .office import daemon
    return daemon.cmd_start(daemon.DEFAULT_PORT, foreground=False, board_port=daemon.BOARD_PORT)


def _up(argv=None):
    """一键开工：体检未通过项 + 可用 AI + 启动管家 + 打印看板和公司页地址与下一步。"""
    from .office import daemon

    all_passed, checks, available_ai = _diagnose()

    # 1. 体检只打印没通过的项
    for tag, text in checks:
        if tag == 'fail':
            print(text)

    # 2. 打印一行 '能用的 AI：…'
    ai_str = '、'.join(available_ai) if available_ai else '无'
    print(f"能用的 AI：{ai_str}")

    # 3. 如果必需项没通过，报错退出
    if not all_passed:
        print("体检未通过：存在未满足的必需项，请先修复后再试。")
        return 1

    # 4. 管家没开就开
    info = daemon.running_info()
    if not info:
        rc = _start_keeper()
        if rc != 0:
            return rc
        info = daemon.running_info()

    # 5. 打印看板和公司页地址、下一步命令
    port = info[1] if info else daemon.DEFAULT_PORT
    board_port = daemon.BOARD_PORT
    print(f"看板：http://127.0.0.1:{board_port}/")
    print(f"公司页：http://127.0.0.1:{port}/org")
    print('下一步命令：xay2 begin --title "你的大任务标题"')
    return 0


def _lead(a):
    if a.cmd == 'doctor':
        return _doctor([])
    if a.cmd == 'up':
        return _up([])
    from .core import open_db, load_state, rebuild
    if a.cmd == 'plan' and a.sub == 'template':
        return _plan_template(a)
    if a.cmd == 'agy-accounts':
        return _agy_accounts_cmd(a)
    if a.cmd == 'agy-switch':
        return _agy_switch_cmd(a)
    if a.cmd == 'hook':
        from .lead_notify import hook_cmd
        return hook_cmd(a.sub)
    if a.cmd == 'backup':
        from .core.backup import backup_daily
        from .office.paths import v2_home
        path = backup_daily(v2_home() / 'xay.db', keep=a.keep)
        print('今天已经备份过了' if path is None else '已备份到 %s' % path)
        return 0
    conn = open_db()
    try:
        if a.cmd == 'begin':
            return _begin(conn, a, load_state(conn))
        if a.cmd == 'rebuild':
            rebuild(conn); print('已重建'); return 0
        if a.cmd == 'notify':
            if not getattr(a, 'hook', False):
                raise CliError('用法：xay2 notify --hook')
            from .lead_notify import notify_hook
            return notify_hook(conn)
        if a.cmd in EXECUTOR_ONLY:
            raise CliError('%s 是执行者在自己桌子里用的命令（由工位设置 XAY_ROLE=executor）；负责人请看 xay2 brief / wait' % a.cmd)
        state = load_state(conn)
        conv = _resolve(conn, a, state)
        if conv is None and getattr(a, 'latest', False):
            conv = _latest_conv(state)
        if conv is None:
            raise CliError(_no_conv_message())
        ctx = Ctx(conn, conv, state)
        rc = COMMANDS[a.cmd](ctx, a)
        return rc if isinstance(rc, int) else 0
    finally:
        conn.close()


def _begin(conn, a, state):
    from .core import append, ALIAS_ENV
    from .core.ids import new_conv
    conv = new_conv(); aliases = []
    for key, prefix in ALIAS_ENV:
        val = (os.environ.get(key) or '').strip()
        if not val:
            continue
        alias = prefix + val
        owner = state.get('aliases', {}).get(alias)
        if owner:
            _err('提示：别名 %s 已经属于对话 %s，这次没登记到新对话上（用 --conv %s 指定新对话）' % (alias, owner, conv))
        else:
            aliases.append(alias)
    cwd = os.getcwd()
    append(conn, 'conv.begun', 'lead:' + conv, {'title': a.title, 'lead_kind': a.lead, 'aliases': aliases, 'cwd': cwd},
           ws=_cwd_ws(), conv=conv, subject=conv)
    print(conv)
    return 0


# ---------------------------------------------------------------- 部门、员工

def _dept(ctx, a):
    if a.sub == 'open':
        if not DEPT_RE.match(a.id):
            raise CliError('部门短名只能用小写字母开头的字母、数字、-、_，最多 16 个字符：%s' % a.id)
        if a.id in ctx.depts():
            raise CliError('这个对话里已经有部门 %s 了' % a.id)
        data = {'name': a.name, 'mission': a.mission, 'owns': _split(a.owns), 'head': a.head}
        if getattr(a, 'part', None):
            data['part'] = ctx.part(a.part)
            _err('建议直接用 xay2 plan part 开中任务')
        seq = ctx.append('dept.opened', data, a.id)
    else:
        if a.id not in ctx.depts():
            raise CliError('找不到部门：%s' % a.id)
        seq = ctx.append('dept.closed', {}, a.id)
    ctx.say(seq)


def _hire(ctx, a, agent=None, quiet=False):
    agent = agent or a.agent
    valid_kinds = providers.agent_kinds()
    if agent not in valid_kinds:
        raise CliError('只能招 %s：%s' % (' / '.join(valid_kinds), agent))
    if not providers.is_available(agent):
        try:
            spec = providers.load_generic_agents().get(agent) or {}
            label = spec.get('label') or agent.capitalize()
        except Exception:
            label = agent.capitalize()
        raise CliError(f'{label} 员工还没配置 API 登录，先在本机配好再派')
    em = _new_id('em_')
    data = {'agent': agent}
    if getattr(a, 'label', None): data['label'] = a.label
    if getattr(a, 'line', None): data['line'] = a.line
    ctx.append('employee.hired', data, em)
    if not quiet:
        print(em)
    return em


# ---------------------------------------------------------------- 计划

def _read_card(spec):
    if spec is None:
        return None
    if spec == '-':
        return sys.stdin.read()
    try:
        return Path(spec).expanduser().read_text(encoding='utf-8')
    except (OSError, UnicodeDecodeError) as exc:
        raise CliError('读不到任务卡文件 %s：%s' % (spec, exc))


def _check_deps(ctx, deps, extra=()):
    tasks = ctx.tasks(); known = set(tasks) | {t.get('key') for t in tasks.values() if t.get('key')} | set(extra)
    missing = [d for d in deps if d not in known]
    if missing:
        raise CliError('找不到前置任务：%s' % '、'.join(missing))


def _plan_add(ctx, a):
    from .office.plain import explain_title_code_like
    issue = explain_title_code_like(a.title)
    if issue:
        raise CliError(f"小任务「{a.title}」的标题像代码：{issue}，建议改成说它要达成什么，比如『让下单时价格算对』")
    keys = {t.get('key') for t in ctx.tasks().values() if t.get('key')}
    if a.key and a.key in keys:
        raise CliError('短名 %s 在这个对话里已经用过了' % a.key)
    deps = _split(a.after); _check_deps(ctx, deps)
    if a.dept and a.dept not in ctx.depts():
        raise CliError('找不到部门：%s' % a.dept)
    agent = a.agent if a.agent != 'auto' else None
    valid_kinds = providers.agent_kinds()
    if agent and agent not in valid_kinds:
        raise CliError('--agent 只能是 %s / auto：%s' % (' / '.join(valid_kinds), a.agent))
    if agent and not providers.is_available(agent):
        try:
            spec = providers.load_generic_agents().get(agent) or {}
            label = spec.get('label') or agent.capitalize()
        except Exception:
            label = agent.capitalize()
        raise CliError(f'{label} 员工还没配置 API 登录，先在本机配好再派')
    if a.new and a.to:
        raise CliError('--to 和 --new 只能选一个')
    if a.new and not agent:
        raise CliError('招新人要说明用哪个 AI：--new --agent %s' % '|'.join(valid_kinds))
    if agent and a.to:
        raise CliError('--to 已经指定了人，不用再写 --agent')
    owner = None
    if a.to:
        owner = ctx.employee(a.to)
        if not owner:
            raise CliError('找不到员工：%s（交给部门请用 --dept）' % a.to)
    card = _read_card(a.card)
    data = {'title': a.title, 'deps': deps, 'writes': _split(a.writes), 'steps': list(a.step), 'mode': 'readonly' if a.readonly else 'write', 'reason': a.reason}
    if a.key: data['key'] = a.key
    if a.dept: data['dept'] = a.dept
    if getattr(a, 'part', None):
        target_pt = ctx.part(a.part)
        _check_part_deadlock(ctx, target_pt, task_id=None, force=getattr(a, 'force', False))
        data['part'] = target_pt
    else:
        _err("这件小任务不属于任何中任务，页面上会放进'零散的活'。建议先 xay2 plan part 开一个中任务，再用 --part 放进去（或用 xay2 plan template / plan apply 一次建好整份计划）")
    if card is not None: data['card'] = card
    if a.new:
        owner = _hire(ctx, a, agent=agent, quiet=True)
        ctx.refresh()
        _err('招了新员工 %s（%s）来接这件活' % (ctx.label(owner), owner))
    if owner: data['owner'] = owner
    elif agent: data['agent'] = agent      # 只说用哪种 AI：管家按选人规则挑人（老员工优先，必要时招新人）
    tk = _new_id('tk_')
    ctx.append('task.added', data, tk)
    print(tk)


def _as_list(v):
    if v is None:
        return []
    if isinstance(v, list):
        return [str(x).strip() for x in v if str(x).strip()]
    return [x.strip() for x in str(v).split(',') if x.strip()]


def _find_cycle(graph):
    visited = {}
    cycle = []

    def dfs(node, path):
        visited[node] = 0
        path.append(node)
        for dep in graph.get(node, []):
            if dep in visited:
                if visited[dep] == 0:
                    idx = path.index(dep)
                    cycle.extend(path[idx:] + [dep])
                    return True
            else:
                if dfs(dep, path):
                    return True
        path.pop()
        visited[node] = 1
        return False

    for n in sorted(graph.keys()):
        if n not in visited:
            if dfs(n, []):
                return cycle
    return None


def _transitive_deps(task_key, task_dep_map):
    res = set()
    stack = list(task_dep_map.get(task_key, []))
    while stack:
        d = stack.pop()
        if d not in res:
            res.add(d)
            stack.extend(task_dep_map.get(d, []))
    return res


def _check_label(chk):
    c = chk or {}
    if isinstance(c, str):
        c = _parse_check(c)
    kind = c.get('kind', 'rules')
    if kind == 'ai':
        reviewer = c.get('reviewer', '')
        r_name = AGENT_NAMES.get(reviewer, reviewer) or 'AI'
        return f"{r_name} 审查"
    elif kind == 'lead':
        return '负责人亲自审'
    elif c.get('commands') or c.get('must_change') or c.get('must_not_change') or c.get('text'):
        return '按负责人的规则'
    return '自动检查'


def _format_task_item(t, emp_lookup=None, current_task=None):
    emp_lookup = emp_lookup or {}
    tk_id = t.get('id') or t.get('key') or 'tk_'
    key = t.get('key')
    ref = f"{tk_id}（{key}）" if (key and key != tk_id) else tk_id
    title = t.get('title') or ''
    if t.get('owner'):
        who = emp_lookup.get(t['owner'], t['owner'])
    elif t.get('agent'):
        who = AGENT_NAMES.get(t['agent'], t['agent'])
    elif t.get('to'):
        who = emp_lookup.get(t['to'], t['to'])
    else:
        who = '未分配'
    status = t.get('status', 'planned')
    st_text = STATUS_TEXT.get(status, status)
    prog = t.get('progress') or {}
    steps = t.get('steps') or []
    if prog.get('of'):
        prog_text = f"（第 {prog.get('step', 1)}/{prog['of']} 步）"
    elif steps:
        prog_text = f"（共 {len(steps)} 步）"
    else:
        prog_text = ""
    is_curr = current_task and (tk_id == current_task or key == current_task or t.get('id') == current_task)
    curr_text = " · （你在做的是这件）" if is_curr else ""
    return f"{ref}：{title} · {who} · {st_text}{prog_text}{curr_text}"


def _render_plan_tree(parts, depts, tasks, emp_lookup=None, title=None, current_task=None, max_lines=60):
    emp_lookup = emp_lookup or {}
    lines = []
    if title:
        lines.append(f"大任务：{title}")
    real_parts = [dict(p) for p in parts if p.get('id') != '']
    key_to_id = {p['key']: (p.get('id') or p.get('key')) for p in real_parts if p.get('key')}
    id_to_p = {(p.get('id') or p.get('key')): p for p in real_parts}

    for p in real_parts:
        pid = p.get('id') or p.get('key')
        norm_after = []
        for a in _as_list(p.get('after')):
            target = key_to_id.get(a, a)
            if target != pid and target not in norm_after:
                norm_after.append(target)
        p['_norm_after'] = norm_after
        p['_pid'] = pid

    col_map = {}
    def get_col(pid, visiting):
        if pid in col_map:
            return col_map[pid]
        if pid in visiting:
            return 0
        visiting.add(pid)
        afters = id_to_p.get(pid, {}).get('_norm_after', [])
        valid = [a for a in afters if a in id_to_p and a != pid]
        c = 0 if not valid else max(get_col(a, visiting) for a in valid) + 1
        visiting.remove(pid)
        col_map[pid] = c
        return c

    for p in real_parts:
        pid = p['_pid']
        col_map[pid] = get_col(pid, set())
        p['col'] = col_map[pid]

    by_col = {}
    for p in real_parts:
        by_col.setdefault(p['col'], []).append(p)

    for c in sorted(by_col.keys()):
        plist = by_col[c]
        plist.sort(key=lambda x: (x.get('order') or 0, x.get('key') or '', x['_pid']))
        for r, p in enumerate(plist):
            p['row'] = r
            p['_col_total'] = len(plist)

    tasks_by_part = {p['_pid']: [] for p in real_parts}
    loose_tasks = []
    for t in tasks:
        t_part = t.get('part')
        matched_pid = None
        if t_part in id_to_p:
            matched_pid = t_part
        elif t_part in key_to_id:
            matched_pid = key_to_id[t_part]
        if matched_pid and matched_pid in tasks_by_part:
            tasks_by_part[matched_pid].append(t)
        else:
            loose_tasks.append(t)

    sorted_parts = sorted(real_parts, key=lambda p: (p['col'], p['row']))
    for p in sorted_parts:
        c = p['col']
        r = p['row']
        tot = p['_col_total']
        step_code = f"{c + 1}-{chr(ord('A') + (r % 26))}" if tot > 1 else str(c + 1)
        chk_label = _check_label(p.get('check'))
        lines.append(f"第 {step_code} 步：{p.get('title', '')}（{chk_label}）")
        why = (p.get('why') or p.get('note') or '').strip()
        if why:
            lines.append(f"  {why}")

        p_dept_ids = []
        dept_name_map = {}
        for d in p.get('depts') or []:
            if isinstance(d, dict) and d.get('key'):
                did = d['key']
                if did not in p_dept_ids:
                    p_dept_ids.append(did)
                dept_name_map[did] = d.get('name') or did
            elif isinstance(d, str):
                if d not in p_dept_ids:
                    p_dept_ids.append(d)
                dept_name_map[d] = d
        for d in depts:
            if d.get('part') in (p.get('id'), p.get('key')):
                did = d.get('id')
                if did and did not in p_dept_ids:
                    p_dept_ids.append(did)
                dept_name_map[did] = d.get('name') or did

        p_tasks = tasks_by_part[p['_pid']]
        if p_dept_ids:
            tasks_in_depts = set()
            for did in p_dept_ids:
                dname = dept_name_map.get(did, did)
                dtasks = [t for t in p_tasks if t.get('dept') == did]
                lines.append(f"  部门：{dname}")
                for t in dtasks:
                    tasks_in_depts.add(t.get('id') or t.get('key') or id(t))
                    lines.append(f"    - {_format_task_item(t, emp_lookup, current_task=current_task)}")
            loose_in_p = [t for t in p_tasks if (t.get('id') or t.get('key') or id(t)) not in tasks_in_depts]
            for t in loose_in_p:
                lines.append(f"  - {_format_task_item(t, emp_lookup, current_task=current_task)}")
        else:
            for t in p_tasks:
                lines.append(f"  - {_format_task_item(t, emp_lookup, current_task=current_task)}")

    if loose_tasks:
        lines.append("零散的小任务（自动检查）")
        for t in loose_tasks:
            lines.append(f"  - {_format_task_item(t, emp_lookup, current_task=current_task)}")

    if len(lines) > max_lines:
        total_tasks = len(tasks)
        shown_lines = lines[:max_lines - 1]
        shown_tasks = sum(1 for ln in shown_lines if ln.strip().startswith('- '))
        rem = max(1, total_tasks - shown_tasks)
        shown_lines.append(f"…还有 {rem} 件")
        return shown_lines
    return lines


def render_board(state, conv, current_task=None, max_lines=40):
    """渲染当前对话的计划树（≤max_lines 行），标出你在做的是哪件。"""
    tasks = [t for t in (state.get('tasks') or {}).values() if t.get('conv') == conv and not t.get('legacy')]
    parts = [p for p in (state.get('parts') or {}).values() if p.get('conv') == conv]
    depts = [d for d in (state.get('depts') or {}).values() if d.get('conv') == conv and not d.get('closed')]
    emp_lookup = {k: e.get('label', k) for k, e in (state.get('employees') or {}).items()}
    conv_title = (state.get('convs') or {}).get(conv, {}).get('title') or conv or ''
    lines = _render_plan_tree(parts, depts, tasks, emp_lookup=emp_lookup, title=conv_title,
                              current_task=current_task, max_lines=max_lines)
    if not lines or (len(lines) == 1 and lines[0].startswith('大任务：')):
        return f"大任务：{conv_title}\n还没有计划" if conv_title else "还没有计划"
    return '\n'.join(lines)


def _plan_apply_parts(ctx, a, raw):
    parts = raw.get('groups') if 'groups' in raw else raw.get('parts')
    if not isinstance(parts, list) or not parts:
        raise CliError('计划文件里要有中任务列表（groups 或 parts）')

    errors = []
    warnings = []
    part_keys = set()
    task_keys = set()
    from .office.plain import explain_title_code_like

    # 1. 基础校验
    for p_idx, part in enumerate(parts, 1):
        if not isinstance(part, dict):
            errors.append(f"第 {p_idx} 个中任务格式不对（必须是对象）")
            continue
        p_title = str(part.get('title') or '').strip()
        if not p_title:
            errors.append(f"第 {p_idx} 个中任务没有标题（title）")
        else:
            issue = explain_title_code_like(p_title)
            if issue:
                errors.append(f"第 {p_idx} 个中任务「{p_title}」的标题像代码：{issue}，建议改成说它要达成什么，比如『让下单时价格算对』")

        why = str(part.get('why') or part.get('note') or '').strip()
        if not why:
            errors.append(f"第 {p_idx} 个中任务「{p_title}」缺少 why（说明这一步做完，用户能得到什么）")

        if part.get('depts'):
            errors.append(f"第 {p_idx} 个中任务「{p_title}」写了部门（depts）：部门就是中任务本身，请把它拆成单独的中任务")

        p_key = part.get('key')
        if p_key:
            if p_key in part_keys:
                errors.append(f"第 {p_idx} 个中任务的短名「{p_key}」重复了")
            part_keys.add(p_key)

        if not part.get('check'):
            warnings.append(f"提示：第 {p_idx} 个中任务「{p_title}」未指定验收方式，默认自动检查")

        p_tasks = part.get('tasks') or []
        if not isinstance(p_tasks, list):
            errors.append(f"第 {p_idx} 个中任务「{p_title}」的小任务列表格式不对")
            continue

        for t_idx, t in enumerate(p_tasks, 1):
            if not isinstance(t, dict):
                errors.append(f"第 {p_idx} 个中任务「{p_title}」的第 {t_idx} 件小任务格式不对（必须是对象）")
                continue
            t_title = str(t.get('title') or '').strip()
            t_key = t.get('key')
            t_label = f"第 {p_idx} 个中任务「{p_title}」的第 {t_idx} 件小任务" + (f"（{t_key}）" if t_key else (f"「{t_title}」" if t_title else ""))

            if not t_title:
                errors.append(f"{t_label}缺少标题（title）")
            else:
                issue = explain_title_code_like(t_title)
                if issue:
                    errors.append(f"{t_label}的标题像代码：{issue}，建议改成说它要达成什么，比如『让下单时价格算对』")

            if not t.get('agent') and not t.get('to'):
                errors.append(f"{t_label}缺少指派对象（agent 或 to）")
            if not t.get('card'):
                errors.append(f"{t_label}缺少任务卡（card）")

            is_ro = bool(t.get('readonly')) or t.get('mode') == 'readonly'
            if not is_ro:
                writes = _as_list(t.get('writes'))
                if not writes:
                    errors.append(f"{t_label}是写代码的小任务，但没有写要改的文件（writes）")
                verify = _as_list(t.get('verify'))
                if not verify:
                    errors.append(f"{t_label}是写代码的小任务，但没有验收命令（verify）")

            if t_key:
                if t_key in task_keys:
                    errors.append(f"{t_label}的短名「{t_key}」重复了")
                task_keys.add(t_key)

    # 2. 依赖引用必须存在
    known_part_refs = part_keys | set(ctx.parts()) | {p.get('key') for p in ctx.parts().values() if p.get('key')}
    for p_idx, part in enumerate(parts, 1):
        p_title = str(part.get('title') or '').strip()
        for after_ref in _as_list(part.get('after')):
            if after_ref not in known_part_refs:
                errors.append(f"第 {p_idx} 个中任务「{p_title}」引用的前置中任务「{after_ref}」不存在")

    known_task_refs = task_keys | set(ctx.tasks()) | {t.get('key') for t in ctx.tasks().values() if t.get('key')}
    for p_idx, part in enumerate(parts, 1):
        p_title = str(part.get('title') or '').strip()
        for t_idx, t in enumerate(part.get('tasks') or [], 1):
            t_title = str(t.get('title') or '').strip()
            for dep in _as_list(t.get('deps')):
                if dep not in known_task_refs:
                    errors.append(f"第 {p_idx} 个中任务「{p_title}」的小任务「{t_title}」引用的前置小任务「{dep}」不存在")

    # 3. 循环依赖检查
    part_graph = {}
    for part in parts:
        ref = part.get('key') or part.get('id')
        if ref:
            part_graph[ref] = [a for a in _as_list(part.get('after')) if a in part_keys]
    part_cycle = _find_cycle(part_graph)
    if part_cycle:
        errors.append(f"中任务之间存在循环依赖：{' -> '.join(part_cycle)}")

    task_graph = {}
    for part in parts:
        for t in part.get('tasks') or []:
            ref = t.get('key') or t.get('id')
            if ref:
                task_graph[ref] = [d for d in _as_list(t.get('deps')) if d in task_keys]
    task_cycle = _find_cycle(task_graph)
    if task_cycle:
        errors.append(f"小任务之间存在循环依赖：{' -> '.join(task_cycle)}")

    # 4. 部门引用检查
    for p_idx, part in enumerate(parts, 1):
        part_depts = {d['key'] for d in part.get('depts') or [] if isinstance(d, dict) and d.get('key')}
        known_depts = part_depts | set(ctx.depts())
        for t in part.get('tasks') or []:
            if t.get('dept') and t['dept'] not in known_depts:
                errors.append(f"小任务「{t.get('title')}」指定的部门「{t['dept']}」不存在")

    # 5. 并行改动范围重叠检查（提醒，不拦）
    for p_idx, part in enumerate(parts, 1):
        p_title = str(part.get('title') or '').strip()
        p_tasks = [t for t in part.get('tasks') or [] if isinstance(t, dict)]
        tdep_map = {}
        for t in p_tasks:
            k = t.get('key') or t.get('id') or t.get('title')
            tdep_map[k] = _as_list(t.get('deps'))
        all_tdeps = {k: _transitive_deps(k, tdep_map) for k in tdep_map}
        for i in range(len(p_tasks)):
            t_a = p_tasks[i]
            k_a = t_a.get('key') or t_a.get('id') or t_a.get('title')
            for j in range(i + 1, len(p_tasks)):
                t_b = p_tasks[j]
                k_b = t_b.get('key') or t_b.get('id') or t_b.get('title')
                if k_a not in all_tdeps[k_b] and k_b not in all_tdeps[k_a]:
                    w_a = set(_as_list(t_a.get('writes')))
                    w_b = set(_as_list(t_b.get('writes')))
                    overlap = w_a & w_b
                    if overlap:
                        warnings.append(
                            f"提示：第 {p_idx} 个中任务「{p_title}」里并行的两件小任务「{t_a.get('title')}」和「{t_b.get('title')}」改动范围重叠（{'、'.join(sorted(overlap))}）"
                        )

    if errors:
        msg = "计划检查未通过，请修改后重试：\n" + "\n".join("- " + e for e in errors)
        raise CliError(msg)

    for w in warnings:
        _err(w)

    if getattr(a, 'dry_run', False):
        dry_parts = []
        dry_depts = []
        dry_tasks = []
        for p in parts:
            p_copy = dict(p)
            p_id = p.get('key') or _new_id('pt_')
            p_copy['id'] = p_id
            dry_parts.append(p_copy)
            for d in p.get('depts') or []:
                d_copy = dict(d)
                d_copy['id'] = d.get('key') or d.get('id')
                d_copy['part'] = p_id
                dry_depts.append(d_copy)
            for t in p.get('tasks') or []:
                t_copy = dict(t)
                t_copy['part'] = p_id
                dry_tasks.append(t_copy)
        conv_title = raw.get('title') or ctx.state.get('convs', {}).get(ctx.conv, {}).get('title') or ctx.conv
        lines = _render_plan_tree(dry_parts, dry_depts, dry_tasks, title=conv_title)
        print('\n'.join(lines))
        return 0

    new_title = raw.get('title')
    if new_title and new_title != ctx.state.get('convs', {}).get(ctx.conv, {}).get('title'):
        ctx.append('conv.titled', {'title': new_title}, ctx.conv)

    batch = 'apply:' + _new_id('')
    existing_parts = ctx.parts()
    part_key_to_existing = {p.get('key'): k for k, p in existing_parts.items() if p.get('key')}
    existing_tasks = ctx.tasks()
    task_key_to_existing = {t.get('key'): k for k, t in existing_tasks.items() if t.get('key')}

    batch_part_map = {}
    for part in parts:
        pkey = part.get('key')
        if pkey and pkey in part_key_to_existing:
            batch_part_map[pkey] = part_key_to_existing[pkey]
        else:
            batch_part_map[pkey] = part.get('id') or _new_id('pt_')

    batch_task_map = {}
    for part in parts:
        for t in part.get('tasks') or []:
            tkey = t.get('key')
            if tkey and tkey in task_key_to_existing:
                batch_task_map[tkey] = task_key_to_existing[tkey]
            else:
                batch_task_map[tkey] = t.get('id') or _new_id('tk_')

    # 1. part.added / part.changed
    for part in parts:
        pkey = part.get('key')
        pt_id = batch_part_map[pkey] if pkey else (part.get('id') or _new_id('pt_'))
        parsed_check = _parse_check(part.get('check'), part.get('check_text'))
        mapped_after = [batch_part_map.get(x, x) for x in _as_list(part.get('after'))]
        why = str(part.get('why') or part.get('note') or '').strip()
        if pkey and pkey in part_key_to_existing:
            fields = {'title': part['title'], 'after': mapped_after, 'check': parsed_check}
            if why:
                fields['note'] = why
            if pkey:
                fields['key'] = pkey
            ctx.append('part.changed', {'fields': fields, 'batch': batch}, pt_id)
        else:
            data = {'title': part['title'], 'after': mapped_after, 'check': parsed_check, 'batch': batch}
            if why:
                data['note'] = why
            if pkey:
                data['key'] = pkey
            ctx.append('part.added', data, pt_id)
            print(pt_id)

    # 2. dept.opened
    for part in parts:
        pkey = part.get('key')
        pt_id = batch_part_map[pkey] if pkey else None
        for dept in part.get('depts') or []:
            d_id = dept.get('key')
            if d_id and d_id not in ctx.depts():
                dept_data = {
                    'name': dept['name'],
                    'mission': dept.get('mission', ''),
                    'owns': _as_list(dept.get('owns')),
                    'head': dept.get('head', 'rules'),
                    'part': pt_id,
                }
                ctx.append('dept.opened', dept_data, d_id)

    # 3. task.added / task.changed
    for part in parts:
        pkey = part.get('key')
        pt_id = batch_part_map[pkey] if pkey else None
        for t in part.get('tasks') or []:
            tkey = t.get('key')
            tk_id = batch_task_map[tkey] if tkey else (t.get('id') or _new_id('tk_'))
            mapped_deps = [batch_task_map.get(d, task_key_to_existing.get(d, d)) for d in _as_list(t.get('deps'))]

            card_raw = t.get('card')
            if isinstance(card_raw, dict):
                card_obj = dict(card_raw)
                card_obj.setdefault('text', '')
                card_obj['verify'] = _as_list(t.get('verify') or card_obj.get('verify'))
            else:
                card_obj = {'text': str(card_raw or ''), 'verify': _as_list(t.get('verify'))}
            if t.get('effort'):
                card_obj['effort'] = t['effort']

            writes = _as_list(t.get('writes'))
            steps = list(t.get('steps') or [])
            mode = 'readonly' if (t.get('readonly') or t.get('mode') == 'readonly') else 'write'

            if tkey and tkey in task_key_to_existing:
                fields = {
                    'title': t['title'],
                    'part': pt_id,
                    'deps': mapped_deps,
                    'writes': writes,
                    'steps': steps,
                    'card': card_obj,
                    'mode': mode,
                }
                if t.get('dept'):
                    fields['dept'] = t['dept']
                ctx.append('task.changed', {'fields': fields, 'batch': batch}, tk_id)
            else:
                data = {
                    'title': t['title'],
                    'part': pt_id,
                    'deps': mapped_deps,
                    'writes': writes,
                    'steps': steps,
                    'card': card_obj,
                    'mode': mode,
                    'batch': batch,
                }
                if tkey:
                    data['key'] = tkey
                if t.get('dept'):
                    data['dept'] = t['dept']
                if t.get('to'):
                    owner = ctx.employee(t['to']) or t['to']
                    data['owner'] = owner
                elif t.get('agent'):
                    data['agent'] = t['agent']
                ctx.append('task.added', data, tk_id)
                print(tk_id)
    return 0


def _plan_apply(ctx, a):
    try:
        raw = json.loads(Path(a.file).read_text(encoding='utf-8'))
    except (OSError, ValueError) as exc:
        raise CliError('读不了计划文件 %s：%s' % (a.file, exc))
    raw = _strip_underscores(raw)
    if isinstance(raw, dict) and ('groups' in raw or 'parts' in raw):
        return _plan_apply_parts(ctx, a, raw)
    items = raw if isinstance(raw, list) else (raw.get('tasks') if isinstance(raw, dict) else None)
    if not isinstance(items, list) or not items:
        raise CliError('计划文件里要有任务列表（[...] 或 {"tasks": [...]}）')
    keys = {t.get('key') for t in ctx.tasks().values() if t.get('key')}
    batch_keys, batch_ids, prepared = set(), set(), []
    from .office.plain import explain_title_code_like
    for n, item in enumerate(items, 1):
        if not isinstance(item, dict) or not str(item.get('title') or '').strip():
            raise CliError('第 %d 件小任务没有标题（title）' % n)
        t_title = str(item.get('title') or '').strip()
        issue = explain_title_code_like(t_title)
        if issue:
            raise CliError(f"第 {n} 件小任务「{t_title}」的标题像代码：{issue}，建议改成说它要达成什么，比如『让下单时价格算对』")
        key = item.get('key')
        if key and (key in keys or key in batch_keys):
            raise CliError('第 %d 件小任务的短名 %s 重复了' % (n, key))
        tk = item.get('id') or _new_id('tk_')
        if tk in ctx.state.get('tasks', {}) or tk in batch_ids:
            raise CliError('第 %d 件小任务的编号 %s 已经有了' % (n, tk))
        if key: batch_keys.add(key)
        batch_ids.add(tk)
        prepared.append((tk, {k: v for k, v in item.items() if k not in ('id', 'batch')}))
    for tk, data in prepared:
        _check_deps(ctx, data.get('deps') or [], extra=batch_keys | batch_ids)
    if getattr(a, 'dry_run', False):
        dry_tasks = [{'id': tk, **data} for tk, data in prepared]
        conv_title = (raw.get('title') if isinstance(raw, dict) else None) or ctx.state.get('convs', {}).get(ctx.conv, {}).get('title') or ctx.conv
        lines = _render_plan_tree([], [], dry_tasks, title=conv_title)
        print('\n'.join(lines))
        return 0
    batch = 'apply:' + _new_id('')
    for tk, data in prepared:
        data['batch'] = batch
        ctx.append('task.added', data, tk)
        print(tk)


def _target(ctx, ref):
    """--to：员工（编号或显示名）或本对话的部门 → ('em', em) | ('dept', id)。"""
    em = ctx.employee(ref)
    if em:
        return 'em', em
    if ref in ctx.depts():
        return 'dept', ref
    return None, None


def _plan_move(ctx, a):
    tk = ctx.task(a.task)
    kind, target = _target(ctx, a.to)
    if kind == 'em':
        seq = ctx.append('task.assigned', {'owner': target, 'reason': a.reason}, tk)
    elif kind == 'dept':
        seq = ctx.append('task.changed', {'fields': {'dept': target}, 'reason': a.reason}, tk)
    else:
        raise CliError('找不到员工或部门：%s' % a.to)
    ctx.say(seq)


def _plan_drop(ctx, a):
    ctx.say(ctx.append('task.removed', {'reason': a.reason}, ctx.task(a.task)))


def _plan_edit(ctx, a):
    from .office.plain import explain_title_code_like
    tk = ctx.task(a.task); fields = {}
    if a.title is not None:
        issue = explain_title_code_like(a.title)
        if issue:
            raise CliError(f"小任务「{a.title}」的标题像代码：{issue}，建议改成说它要达成什么，比如『让下单时价格算对』")
        fields['title'] = a.title
    if a.writes is not None: fields['writes'] = _split(a.writes)
    if a.dept is not None:
        if a.dept and a.dept not in ctx.depts():
            raise CliError('找不到部门：%s' % a.dept)
        fields['dept'] = a.dept or None
    if getattr(a, 'part', None) is not None:
        target_pt = None if a.part == '' else ctx.part(a.part)
        if target_pt is not None:
            _check_part_deadlock(ctx, target_pt, task_id=tk, force=getattr(a, 'force', False))
        fields['part'] = target_pt
    if a.after is not None:
        deps = _split(a.after); _check_deps(ctx, deps); fields['deps'] = deps
    if a.step is not None: fields['steps'] = list(a.step)
    if a.card is not None: fields['card'] = _read_card(a.card)
    if a.mode is not None: fields['mode'] = a.mode
    if not fields:
        raise CliError('没有要改的内容（--title / --writes / --dept / --part / --after / --step / --card / --mode）')
    ctx.say(ctx.append('task.changed', {'fields': fields, 'reason': a.reason}, tk))


def _plan_propose(ctx, a):
    to = (a.to or '').strip()

    def owner_of(ref):
        em = ctx.employee(ref)
        if em: return em
        if ref.startswith('em_'): return ref   # 申请可以提到还没招的员工编号，由批准的人把关
        return None

    if a.op == 'add':
        fields = {'title': a.title or a.task}
        if to:
            kind, target = _target(ctx, to)
            if kind == 'em': fields['owner'] = target
            elif kind == 'dept': fields['dept'] = target
            elif owner_of(to): fields['owner'] = owner_of(to)
            else: raise CliError('找不到员工或部门：%s' % to)
        change = {'op': 'add', 'task': _new_id('tk_'), 'fields': fields}
    else:
        tk = ctx.task(a.task)
        if a.op == 'remove':
            change = {'op': 'remove', 'task': tk}
        elif a.op == 'change':
            if not a.title:
                raise CliError('申请改任务要说改成什么：--title "新标题"')
            change = {'op': 'change', 'task': tk, 'fields': {'title': a.title}}
        else:  # assign / move（§7：move 到员工 = assign，move 到部门 = change{dept}）
            if not to:
                raise CliError('要说交给谁：--to <员工>' + ('' if a.op == 'assign' else ' 或 <部门>'))
            dept = to if (a.op == 'move' and to in ctx.depts() and not ctx.employee(to)) else None
            if dept:
                change = {'op': 'change', 'task': tk, 'fields': {'dept': dept}}
            elif owner_of(to):
                change = {'op': 'assign', 'task': tk, 'fields': {'owner': owner_of(to)}}
            else:
                raise CliError('找不到员工%s：%s' % ('' if a.op == 'assign' else '或部门', to))
    pp = _new_id('pp_')
    ctx.append('proposal.made', {'from': 'gm', 'changes': [change], 'reason': a.reason, 'to': 'gm'}, pp)
    print(pp)


def _parse_check(check_arg, check_text=None):
    if not check_arg:
        c = {'kind': 'rules'}
    elif isinstance(check_arg, dict):
        c = dict(check_arg)
    elif check_arg == 'rules':
        c = {'kind': 'rules'}
    elif check_arg.startswith('ai:'):
        reviewer = check_arg[3:]
        c = {'kind': 'ai', 'reviewer': reviewer}
    elif check_arg == 'lead':
        c = {'kind': 'lead'}
    else:
        c = {'kind': check_arg}
    if check_text:
        c['text'] = check_text
    return c


def _plan_part(ctx, a):
    from .office.plain import explain_title_code_like
    issue = explain_title_code_like(a.title)
    if issue:
        raise CliError(f"中任务「{a.title}」的标题像代码：{issue}，建议改成说它要达成什么，比如『让下单时价格算对』")
    why = (getattr(a, 'why', None) or '').strip()
    if not why:
        raise CliError("中任务必须写 --why：说明这一步做完，用户能得到什么")
    pt = _new_id('pt_')
    data = {'title': a.title, 'note': why, 'after': _split(a.after), 'reason': a.reason}
    if a.key:
        data['key'] = a.key
    data['check'] = _parse_check(a.check, a.check_text)
    ctx.append('part.added', data, pt)
    print(pt)


def _plan_part_edit(ctx, a):
    from .office.plain import explain_title_code_like
    pt = ctx.part(a.part)
    fields = {}
    if a.title is not None:
        issue = explain_title_code_like(a.title)
        if issue:
            raise CliError(f"中任务「{a.title}」的标题像代码：{issue}，建议改成说它要达成什么，比如『让下单时价格算对』")
        fields['title'] = a.title
    if getattr(a, 'why', None) is not None:
        fields['note'] = a.why
    if a.after is not None:
        fields['after'] = _split(a.after)
    if a.check is not None or a.check_text is not None:
        if a.check is not None:
            fields['check'] = _parse_check(a.check, a.check_text)
        else:
            existing = ctx.parts().get(pt, {}).get('check') or {'kind': 'rules'}
            new_check = dict(existing)
            new_check['text'] = a.check_text
            fields['check'] = new_check
    if not fields:
        raise CliError('没有要改的内容（--title / --why / --after / --check / --check-text）')
    seq = ctx.append('part.changed', {'fields': fields, 'reason': a.reason}, pt)
    ctx.say(seq)


def _plan_part_drop(ctx, a):
    pt = ctx.part(a.part)
    seq = ctx.append('part.removed', {'reason': a.reason}, pt)
    ctx.say(seq)


def _plan_show(ctx, a):
    parts = list(ctx.parts().values())
    tasks = list(ctx.tasks().values())
    depts = [d for d in ctx.state.get('depts', {}).values() if d.get('conv') == ctx.conv and not d.get('closed')]
    emp_lookup = {em: ctx.label(em) for em in ctx.employees()}
    conv_title = ctx.state.get('convs', {}).get(ctx.conv, {}).get('title') or ctx.conv
    lines = _render_plan_tree(parts, depts, tasks, emp_lookup=emp_lookup, title=conv_title)
    if not lines or (len(lines) == 1 and lines[0].startswith('大任务：')):
        print('大任务：%s\n还没有计划' % conv_title)
        return 0
    print('\n'.join(lines))
    return 0


def _plan_open(ctx, a):
    from .office.daemon import running_info
    info = running_info()
    if not info:
        raise CliError('管家没在运行，请先 xay2 office start')
    _, port, _ = info
    print('http://127.0.0.1:%d/org?conv=%s&canvas=1' % (port, ctx.conv))
    return 0


def _plan(ctx, a):
    return {'add': _plan_add, 'apply': _plan_apply, 'move': _plan_move, 'drop': _plan_drop,
            'edit': _plan_edit, 'propose': _plan_propose,
            'part': _plan_part, 'part-edit': _plan_part_edit, 'part-drop': _plan_part_drop,
            'show': _plan_show, 'open': _plan_open, 'template': lambda ctx, a: _plan_template(a)}[a.sub](ctx, a)


# ---------------------------------------------------------------- 答复、信、验收、控制

def _answer(ctx, a):
    ref = a.id
    if ref.startswith('dc_'):
        dec = ctx.state.get('decisions', {}).get(ref)
        if not dec or dec.get('conv') not in (None, ctx.conv):
            raise CliError('找不到待你决定的事项 %s（可能已经处理过了）' % ref)
        options = [o.get('id') for o in dec.get('options', []) if isinstance(o, dict)]
        if options and a.verdict not in options:
            raise CliError('%s 的选项只有：%s' % (ref, ' / '.join(options)))
        data = {'option': a.verdict}
        if a.text: data['text'] = a.text
        ctx.say(ctx.append('decision.resolved', data, ref))
    elif ref.startswith('pp_'):
        p = ctx.state.get('proposals', {}).get(ref)
        if not p or p.get('conv') not in (None, ctx.conv):
            raise CliError('找不到还没处理的申请 %s' % ref)
        if p.get('status') not in (None, 'open'):
            raise CliError('申请 %s 已经处理过了（%s）' % (ref, '通过了' if p.get('status') == 'accepted' else '没通过'))
        verdict = {'accept': 'accept', '通过': 'accept', '同意': 'accept', 'reject': 'reject', '拒绝': 'reject', '不同意': 'reject'}.get(a.verdict)
        if not verdict:
            raise CliError('申请只能 accept（通过）或 reject（拒绝）')
        data = {'verdict': verdict}
        if a.text: data['note'] = a.text
        seq = ctx.append('proposal.decided', data, ref)
        ctx.refresh()
        if verdict == 'accept':
            print('计划改到第 %d 版：%s' % (ctx.state['convs'][ctx.conv]['plan_version'], _plan_line(ctx)))
        else:
            ctx.say(seq)
        _tell_proposer(ctx, ref, p, verdict, a.text)
    elif ref.startswith('lt_'):
        letter = ctx.state.get('letters', {}).get(ref)
        if not letter:
            raise CliError('找不到信 %s' % ref)
        if a.verdict not in ('accept', 'modify', 'reject'):
            raise CliError('回信只能 accept / modify / reject，后面可以再写一句说明')
        answer_letter = None
        if a.text:
            answer_letter = _new_id('lt_')
            ctx.append('letter.sent', {'from': 'gm', 'to': letter.get('from') or '', 'kind': 'reply', 'subject': _subject_of(a.text, ''),
                                       'body': a.text, 'reply_to': ref}, answer_letter)
        ctx.say(ctx.append('letter.answered', {'verdict': a.verdict, 'answer_letter': answer_letter}, ref))
    else:
        raise CliError('不认识这个编号：%s（只能答复 dc_ / pp_ / lt_）' % ref)


def _plan_line(ctx):
    """最新一版计划的一句话（和页面"计划版本"栏同一个写法）。"""
    from .core.humanize import plan_summary
    hist = ctx.state.get('plan_history', {}).get(ctx.conv) or []
    return plan_summary(ctx.state, ctx.conv, hist[-1]) if hist else ''


def _tell_proposer(ctx, pp, p, verdict, note):
    """改计划申请处理完了，写封信告诉提申请的员工（他下一轮开工时、或干活中途插话时收到）。总经理自己提的不用写。"""
    em = p.get('from')
    if not (isinstance(em, str) and em.startswith('em_')):
        return
    if verdict == 'accept':
        version = ctx.state['convs'][ctx.conv]['plan_version']
        subject, body = '你的改计划申请通过了', '你的改计划申请（%s）通过了，计划改到第 %d 版：%s。' % (pp, version, _plan_line(ctx))
    else:
        subject, body = '你的改计划申请没通过', '你的改计划申请（%s）没通过，计划先不改。' % pp
    if note:
        body += '负责人的话：' + note
    ctx.append('letter.sent', {'from': 'gm', 'to': em, 'kind': 'notice', 'subject': subject, 'body': body}, _new_id('lt_'))


def _letter_to(ctx, ref):
    if ref in ('gm', 'user'):
        return ref
    em = ctx.employee(ref)
    if em:
        return em
    if ref in ctx.depts():
        return ref
    raise CliError('找不到收信人：%s（员工、部门、gm 或 user）' % ref)


def _ask_tell(ctx, a):
    to = _letter_to(ctx, a.to)
    data = {'from': 'gm', 'to': to, 'kind': 'request' if a.cmd == 'ask' else 'tell', 'subject': _subject_of(a.body, a.subject), 'body': a.body}
    lt = _new_id('lt_')
    ctx.append('letter.sent', data, lt)
    print(lt)


def _reply(ctx, a):
    letter = ctx.state.get('letters', {}).get(a.letter)
    if not letter:
        raise CliError('找不到信 %s' % a.letter)
    lt = _new_id('lt_')
    ctx.append('letter.sent', {'from': 'gm', 'to': letter.get('from') or '', 'kind': 'reply', 'subject': _subject_of(a.body, a.subject),
                               'body': a.body, 'reply_to': a.letter}, lt)
    print(lt)


def _accept(ctx, a):
    tk = ctx.task(a.task)
    status = ctx.state['tasks'][tk].get('status')
    if status != 'reported':
        raise CliError('「%s」现在是"%s"，交上来（待验收）之后才能验收' % (ctx.state['tasks'][tk].get('title'), STATUS_TEXT.get(status, status)))
    data = {'note': a.note} if a.note else {}
    ctx.say(ctx.append('task.accepted', data, tk))


def _check(ctx, a):
    tk = ctx.task(a.task)
    ok = (a.verdict.lower() == 'pass')
    data = {'task': tk, 'ok': ok, 'text': a.text or ''}
    seq = ctx.append('check.result', data, tk)
    for dc, dec in list(ctx.state.get('decisions', {}).items()):
        if dec.get('kind') == 'review' and dec.get('task') == tk:
            dec_data = {'option': a.verdict}
            if a.text:
                dec_data['text'] = a.text
            ctx.append('decision.resolved', dec_data, dc)
    ctx.say(seq)


def _control(ctx, a):
    data = {'by_source': 'cli'}
    if a.cmd == 'stop' and a.all:
        data['action'] = 'stop_all'; subject = None
    elif a.cmd == 'resume' and not a.target:
        halt = ctx.state.get('halt') or {}
        if not halt:
            raise CliError('总闸没拉下，不用恢复')
        if halt.get('by') == 'user':
            raise CliError('总闸是用户在页面上拉的，只有用户在页面上点"恢复"才能解除')
        data['action'] = 'resume'; subject = None
    else:
        ref = a.em if a.cmd in ('steer', 'interrupt') else a.target
        if not ref:
            raise CliError('要说停谁：xay2 stop <员工|任务>，或 xay2 stop --all')
        subject = ctx.employee(ref)
        if a.cmd == 'resume' and not subject:
            raise CliError('找不到员工：%s' % ref)
        if not subject and a.cmd == 'stop':
            subject = ctx.task(ref)
        if not subject:
            raise CliError('找不到员工：%s' % ref)
        data['action'] = a.cmd
        if a.cmd == 'steer': data['text'] = a.text
    ctx.say(ctx.append('control.requested', data, subject))


def _compact(ctx, a):
    em = ctx.employee(a.em)
    if not em:
        raise CliError('找不到员工：%s' % a.em)
    data = {'reason': 'manual', 'method': a.method}
    if a.reason is not None:
        data['note'] = a.reason
    ctx.say(ctx.append('context.compact_requested', data, em))


def _notes_set(ctx, a):
    em = ctx.employee(a.em)
    if not em:
        raise CliError('找不到员工：%s' % a.em)
    if a.file == '-':
        text = sys.stdin.read()
    else:
        path = Path(a.file)
        if not path.is_file():
            raise CliError('读不到笔记文件：%s' % a.file)
        try:
            text = path.read_text(encoding='utf-8')
        except (OSError, UnicodeDecodeError) as exc:
            raise CliError('读不到笔记文件：%s（%s）' % (a.file, exc))
    seq = ctx.append('notes.updated', {'active': text}, em)
    ctx.say(seq)


# ---------------------------------------------------------------- 看现状

def _snapshot_for(ctx):
    from .core.snapshot import build
    return build(ctx.conn, conv=ctx.conv)


def _snapshot(ctx, a):
    out = _snapshot_for(ctx)
    print(json.dumps(out, ensure_ascii=False, indent=2) if a.json else json.dumps(out, ensure_ascii=False))


BRIEF_MAX_LINES = 25
BRIEF_ASKS = 5            # "需要你决定的"最多列几件，其余的让 xay2 wait 一条条列
PERSON_TEXT = {'idle': '空闲', 'sleeping': '空闲', 'queued': '排队中', 'blocked': '等人答复', 'offline': '离线',
               'compacting': '整理记忆', 'failed': '上一轮失败', 'working': '在干活'}
# 列不下全部任务时先列要紧的：等你决定的、在做的、等你验收的、没做成的……做完和取消的最后
TASK_URGENCY = {'needs_you': 0, 'working': 1, 'reported': 2, 'failed': 3, 'queued': 4, 'ready': 5, 'planned': 6, 'done': 7, 'canceled': 8}


def _clip(text, n):
    """压成一行、最多 n 个字（超过用…）：wait / brief 每条都要短。"""
    text = ' '.join(str(text or '').split())
    return text if len(text) <= n else text[:n - 1] + '…'


def _task_status_text(t):
    """任务状态的大白话（brief 用）：在做的带第几步，交上来的提示怎么验收。"""
    st = t.get('status')
    if st == 'working':
        p = t.get('progress') or {}
        return '在做' + ('，第 %s/%s 步：%s' % (p['step'], p['of'], _clip(p.get('text'), 20)) if p.get('of') else '')
    if st == 'reported':
        return '做完了，等你验收（xay2 accept %s）' % (t.get('key') or t['id'])
    return STATUS_TEXT.get(st, st or '')


def _brief_asks(ctx, out):
    """需要你决定的：要你拿主意的事（甲类 / 乙类）、待批的改计划申请、等你回复的信（含写给部门、落到你这儿的）。"""
    from .core.humanize import proposal_change_text
    s = ctx.state
    items = []
    for d in out['decisions']:
        if d.get('owner') == 'lead' or d.get('delegated') == 'lead':
            opts = ' / '.join(o.get('id', '') for o in d.get('options', []))
            tag = '（你交给负责人了）' if d.get('delegated') == 'lead' else ''
            items.append('%s：%s%s（选项：%s）' % (d['id'], _clip(d.get('title'), 40), tag, opts))
    for p in out['proposals']:
        if p.get('to') == 'gm':
            who = '负责人' if p.get('from') == 'gm' else ctx.label(p.get('from'))
            what = '；'.join(proposal_change_text(s, ctx.conv, c) for c in (p.get('changes') or [])[:2])
            items.append('%s：%s 申请%s（accept / reject）' % (p['id'], who, _clip(what, 40)))
    for l in out['letters']:
        to_gm = (l.get('delivery') or {}).get('to') == 'gm' or (l.get('legacy') and l.get('to') == 'gm')
        if to_gm and l.get('kind') == 'request' and l.get('status') == 'open' and l.get('from') != 'gm':
            sender = {'user': '用户'}.get(l.get('from')) or ctx.label(l.get('from'))
            items.append('%s：%s 问「%s」（xay2 reply %s "…"）' % (l['id'], sender, _clip(l.get('subject') or l.get('body'), 24), l['id']))
    return items


def _brief_mail(ctx, out):
    """信件一行：还没送到几封、各在等谁。"""
    from .core.mail import who
    waiting = [l for l in out['letters'] if (l.get('delivery') or {}).get('state') in ('waiting', 'stuck')]
    if not waiting:
        return '信：都送到了' if out['letters'] else '信：还没有'
    count = {}
    for l in waiting:
        to = (l.get('delivery') or {}).get('to')
        name = who(ctx.state, ctx.conv, to) if to else '找不到收信人'
        count[name] = count.get(name, 0) + 1
    parts = ['给 %s 的 %d 封' % (k, v) for k, v in count.items()]
    return '还没送到的信 %d 封：%s' % (len(waiting), '、'.join(parts[:4]) + ('等' if len(parts) > 4 else ''))


def _brief(ctx, a):
    """≤25 行纯文本现状（§7）：对话、团队现状、需要你决定的、信（还没送到几封）、任务清单（大白话状态）。"""
    out = _snapshot_for(ctx)
    if a.json:
        print(json.dumps(out, ensure_ascii=False, indent=2)); return
    conv = out['conversation']
    all_people = out['people']
    task_ids = {t.get('id') for t in out.get('tasks', [])}
    involved = set()
    for dept in out.get('depts', []):
        involved.update(dept.get('members') or [])
    for task in out.get('tasks', []):
        if task.get('owner'):
            involved.add(task['owner'])
    for person in all_people.values():
        if any(task_id in task_ids for task_id in (person.get('queue') or [])):
            involved.add(person.get('key'))
    former = {person.get('key') for person in out.get('former', [])}
    people = {key: person for key, person in all_people.items()
              if key in involved and key not in former}
    prog = out.get('progress') or {}
    pct = prog.get('pct', 0)
    lines = ['对话：%s（%s，计划第 %d 版） 总进度 %d%%' % (_clip(conv['title'], 40), conv['id'], conv['plan_version'], pct)]
    halt = out.get('halt')
    if halt:
        how = '用户在页面上点"恢复"' if halt.get('by') == 'user' else 'xay2 resume 或再排一件活'
        lines.append('总闸：已拉下（%s），不会起新工位；恢复：%s' % (time.strftime('%H:%M', time.localtime(halt.get('ts') or 0)), how))
    if people:
        parts = []
        for p in people.values():
            doing = '在做「%s」' % _clip(p['now'], 20) if p.get('now') else PERSON_TEXT.get(p['display_state'], p['display_state'])
            prog = p.get('progress') or {}
            if p.get('now') and prog.get('of'):
                doing += '（第 %s/%s 步）' % (prog['step'], prog['of'])
            parts.append('%s %s' % (p['label'], doing))
        lines.append('团队：' + '；'.join(parts[:6]) + ('；等 %d 人' % len(parts) if len(parts) > 6 else ''))
    else:
        lines.append('团队：还没有员工')
    asks = _brief_asks(ctx, out)
    if asks:
        lines.append('需要你决定 %d 件：' % len(asks))
        lines += ['  ' + x for x in asks[:BRIEF_ASKS]]
        if len(asks) > BRIEF_ASKS:
            lines.append('  …还有 %d 件（xay2 wait 会一条条列出来）' % (len(asks) - BRIEF_ASKS))
    else:
        lines.append('需要你决定：没有')
    lines.append(_brief_mail(ctx, out))
    tasks = out['tasks']
    lines.append('任务（共 %d 件）：' % len(tasks))
    conv_parts = [p for p in ctx.state.get('parts', {}).values() if p.get('conv') == ctx.conv]
    if not conv_parts:
        room = BRIEF_MAX_LINES - len(lines)
        if len(tasks) <= room:
            shown, rest = tasks, []
        else:
            order = sorted(range(len(tasks)), key=lambda i: (TASK_URGENCY.get(tasks[i]['status'], 9), i))
            keep = set(order[:room - 1])
            shown = [t for i, t in enumerate(tasks) if i in keep]
            rest = [t for i, t in enumerate(tasks) if i not in keep]
        for t in shown:
            who = people.get(t.get('owner'), {}).get('label') or ('还没分配' if not t.get('owner') else t['owner'])
            lines.append('- %s：%s（%s%s）' % (_clip(t['title'], 30), _task_status_text(t), (t['key'] + '，') if t.get('key') else '', who))
        if rest:
            count = {}
            for t in rest:
                k = STATUS_TEXT.get(t['status'], t['status'])
                count[k] = count.get(k, 0) + 1
            lines.append('…另外 %d 件没列出：%s（xay2 snapshot --json 看全部）'
                         % (len(rest), '、'.join('%s %d 件' % kv for kv in count.items())))
    else:
        conv_parts.sort(key=lambda p: (p.get('order') or 0, p.get('id')))
        known_part_keys = {p['id'] for p in conv_parts} | {p.get('key') for p in conv_parts if p.get('key')}
        groups = []
        for p in conv_parts:
            p_keys = {p['id']}
            if p.get('key'):
                p_keys.add(p['key'])
            p_tasks = [
                t for t in tasks
                if (t.get('part') or ctx.state.get('tasks', {}).get(t.get('id'), {}).get('part')) in p_keys
            ]
            groups.append((p.get('title') or p['id'], p_tasks))
        loose = [
            t for t in tasks
            if (t.get('part') or ctx.state.get('tasks', {}).get(t.get('id'), {}).get('part')) not in known_part_keys
        ]
        if loose:
            groups.append(('零散的小任务', loose))

        total_needed = sum(1 + len(gt) for _, gt in groups)
        room = BRIEF_MAX_LINES - len(lines)
        if total_needed <= room:
            for g_title, g_tasks in groups:
                done = sum(1 for t in g_tasks if t.get('status') == 'done')
                total = len([t for t in g_tasks if t.get('status') != 'canceled'])
                lines.append('中任务：%s（做完 %d/%d）' % (_clip(g_title, 30), done, total))
                for t in g_tasks:
                    who = people.get(t.get('owner'), {}).get('label') or ('还没分配' if not t.get('owner') else t['owner'])
                    lines.append('- %s：%s（%s%s）' % (_clip(t['title'], 30), _task_status_text(t), (t['key'] + '，') if t.get('key') else '', who))
        else:
            all_task_ids_shown = set()
            for g_title, g_tasks in groups:
                if len(lines) >= BRIEF_MAX_LINES - 1:
                    break
                done = sum(1 for t in g_tasks if t.get('status') == 'done')
                total = len([t for t in g_tasks if t.get('status') != 'canceled'])
                lines.append('中任务：%s（做完 %d/%d）' % (_clip(g_title, 30), done, total))
                for t in g_tasks:
                    if len(lines) >= BRIEF_MAX_LINES - 1:
                        break
                    who = people.get(t.get('owner'), {}).get('label') or ('还没分配' if not t.get('owner') else t['owner'])
                    lines.append('- %s：%s（%s%s）' % (_clip(t['title'], 30), _task_status_text(t), (t['key'] + '，') if t.get('key') else '', who))
                    all_task_ids_shown.add(t.get('id'))
            rest_tasks = [t for t in tasks if t.get('id') not in all_task_ids_shown]
            if rest_tasks and len(lines) < BRIEF_MAX_LINES:
                count = {}
                for t in rest_tasks:
                    k = STATUS_TEXT.get(t['status'], t['status'])
                    count[k] = count.get(k, 0) + 1
                lines.append('…另外 %d 件没列出：%s（xay2 snapshot --json 看全部）'
                             % (len(rest_tasks), '、'.join('%s %d 件' % kv for kv in count.items())))
    print('\n'.join(lines[:BRIEF_MAX_LINES]))


def _events(ctx, a):
    from .core import since
    cur = a.since
    while True:
        rows = since(ctx.conn, cur, conv=ctx.conv)
        for e in rows:
            print(json.dumps(e, ensure_ascii=False)); cur = e['seq']
        if not a.follow and len(rows) < 500:
            break
        if not rows:
            time.sleep(1)


# ---------------------------------------------------------------- wait（§7：负责人的唯一等待方式）

def _to_gm(e, state):
    """这封信该总经理收：写给 gm 的；或写给部门、按送信规则落到总经理的（部门负责人是 Claude、部门里没有别人）。
    总经理自己写的不算。"""
    d = e.get('data') or {}
    to = d.get('to')
    if d.get('from') == 'gm' or to == 'user' or (isinstance(to, str) and to.startswith('em_')):
        return False
    if to == 'gm':
        return True
    from .core.mail import recipient
    letter = (state.get('letters') or {}).get(e.get('subject')) or dict(d, conv=e.get('conv'), id=e.get('subject'))
    return recipient(state, letter) == 'gm'


def _wakes(e, state):
    typ, d = e['type'], e.get('data') or {}
    if typ in ('task.landed', 'task.kept', 'task.failed'): return True
    if typ == 'decision.opened': return d.get('owner') == 'lead'
    if typ == 'decision.delegated': return d.get('to') == 'lead'
    if typ == 'signal.raised': return d.get('level') == 'warn'
    if typ == 'proposal.made': return d.get('to') == 'gm'
    if typ == 'letter.sent': return _to_gm(e, state)
    if typ == 'task.added' and (d.get('by_task') or d.get('by_em')): return True
    return False


def _merge_key(e):
    """"同类 10 分钟内合并"：同一个对象的同一种提醒合成一行；合并回项目的活、没做成的活各合成一行。
    要你拿主意的（需要你、申请、来信）各带着要答复的编号，不合并。"""
    typ = e['type']
    if typ == 'signal.raised':
        return ('signal', (e.get('data') or {}).get('kind'), e.get('subject'))
    if typ in ('task.landed', 'task.failed'):
        return (typ,)
    return None


def _wait_groups(hits, since_seq):
    """把叫醒 wait 的事件排成要打印的行。返回 (groups, 游标)。

    - 同类 10 分钟内合并（_merge_key）；最多 15 行，没打印到的留给下一次。
    - 工位"改动先保留"（task.kept）后面紧跟着这件活的"需要你"（decision.opened）：只打印"需要你"那一行，
      把保留的原因并进去（两行说的是同一件事）。
    - 游标 = 最后一条已打印（或已并进打印行）的事件；"需要你"没赶上打印时，它和并进去的那条下次还能等到。"""
    last_decision = {}
    for i, e in enumerate(hits):
        if e['type'] == 'decision.opened' and (e.get('data') or {}).get('task'):
            last_decision[e['data']['task']] = i
    groups, cursor, kept = [], since_seq, {}
    for i, e in enumerate(hits):
        if e['type'] == 'task.kept' and last_decision.get(e.get('subject'), -1) > i:
            kept[e.get('subject')] = e
            continue
        key = _merge_key(e)
        g = next((g for g in groups if key is not None and g['key'] == key and e['ts'] - g['ts'] <= WAIT_MERGE_SECONDS), None)
        if g:
            g['events'].append(e); cursor = e['seq']
            continue
        if len(groups) >= WAIT_MAX_LINES:
            break   # 没打印到的留给下一次：游标停在最后一条已打印（或已合并）的事件
        g = {'key': key, 'ts': e['ts'], 'event': e, 'events': [e]}
        if e['type'] == 'decision.opened':
            g['kept'] = kept.pop((e.get('data') or {}).get('task'), None)
        groups.append(g); cursor = e['seq']
    return groups, cursor


def _task_bits(ctx, s, e):
    sub = e.get('subject')
    task = s.get('tasks', {}).get(sub, {}) if sub else {}
    return _clip(task.get('title') or '任务', 40), (ctx.label(task.get('owner')) if task.get('owner') else '员工'), (e.get('data') or {})


def _join(items, n=3):
    return '、'.join(items[:n]) + (' 等' if len(items) > n else '')


def _wait_line(ctx, s, g):
    """一行（不换行）大白话；每段都截短，保证一条不超过两行。"""
    e = g['event']
    d, typ, sub = e.get('data') or {}, e['type'], e.get('subject')
    if typ == 'decision.opened':
        opts = ' / '.join(o.get('id', '') for o in d.get('options', []) if isinstance(o, dict))
        reason = ((g.get('kept') or {}).get('data') or {}).get('reason')
        extra = ('——' + KEPT_TEXT.get(reason, reason)) if reason else ''
        return '需要你：%s%s（%s，选项：%s）' % (_clip(d.get('title'), 60), extra, sub, opts)
    if typ == 'decision.delegated' and d.get('to') == 'lead':
        dec = (s.get('decisions') or {}).get(sub) or {}
        opts = ' / '.join(o.get('id', '') for o in dec.get('options', []) if isinstance(o, dict))
        return '需要你（你交给了负责人）：%s（%s，选项：%s）' % (_clip(dec.get('title') or sub, 60), sub, opts)
    if typ == 'task.landed':
        items = []
        for x in g['events']:
            title, who, dx = _task_bits(ctx, s, x)
            items.append('%s（%s，%d 个文件）' % (title, who, len(dx.get('files') or [])))
        return ('已合并：' + items[0]) if len(items) == 1 else '已合并 %d 件：%s' % (len(items), _join(items))
    if typ == 'task.kept':
        title, who, _ = _task_bits(ctx, s, e)
        return '改动先保留了，等你决定：%s（%s，%s）' % (title, who, KEPT_TEXT.get(d.get('reason'), d.get('reason') or ''))
    if typ == 'task.failed':
        items = []
        for x in g['events']:
            title, who, dx = _task_bits(ctx, s, x)
            items.append((title, who, _clip(dx.get('reason'), 30)))
        if len(items) == 1:
            title, who, reason = items[0]
            return '没做成：%s（%s）%s' % (title, who, ('：' + reason) if reason else '')
        return '没做成 %d 件：%s' % (len(items), _join(['%s（%s%s）' % (t, w, '：' + r if r else '') for t, w, r in items]))
    if typ == 'proposal.made':
        from .core.humanize import proposal_change_text
        who = '负责人' if d.get('from') == 'gm' else ctx.label(d.get('from'))
        changes = d.get('changes') or []
        what = '；'.join(proposal_change_text(s, ctx.conv, c) for c in changes[:2]) + (' 等 %d 处' % len(changes) if len(changes) > 2 else '')
        return '%s 申请改计划：%s；理由：%s（%s，选项：accept / reject）' % (who, _clip(what, 60), _clip(d.get('reason'), 40) or '没写', sub)
    if typ == 'letter.sent':
        from .core.mail import who as mail_who
        sender = mail_who(s, ctx.conv, d.get('from'))
        via = '' if d.get('to') == 'gm' else '（写给%s）' % mail_who(s, ctx.conv, d.get('to'))
        subject = (d.get('subject') or '').strip()
        body = ' '.join(str(d.get('body') or '').split())
        head = _clip(subject or body, 30)
        tail = ('——' + _clip(body, 40)) if subject and body and body != subject else ''
        ask = '，等你回复：xay2 reply %s "…"' % sub if d.get('kind') == 'request' else ''
        return '%s 来信%s：%s%s（%s%s）' % (sender, via, head, tail, sub, ask)
    if typ == 'task.added' and (d.get('by_task') or d.get('by_em')):
        from .core.humanize import AI_NAMES
        who = ctx.label(d.get('by_em')) or '员工'
        to = ctx.label(d.get('owner')) if d.get('owner') else (AI_NAMES.get(d.get('agent'), (d.get('agent') or '').capitalize()) if d.get('agent') else '')
        to_str = f"给 {to}" if to else ""
        reason_str = f"（{d.get('reason')}）" if d.get('reason') else ""
        title = _clip(d.get('title') or '任务', 40)
        return f"{who} 派了「{title}」{to_str}{reason_str}".strip()
    line = '提醒：%s' % _clip(d.get('text'), 80)
    if len(g['events']) > 1:
        line += '（10 分钟内同样的提醒共 %d 条）' % len(g['events'])
    return line


def _mark_seen(ctx, groups):
    """写给总经理的信打印出来 = 总经理看到了：记 letter.delivered{how:inbox}（页面上从"还没送到"变成"总经理已看到"）。"""
    from .core import append
    for g in groups:
        for e in g['events']:
            lt = e.get('subject')
            if e['type'] != 'letter.sent' or (ctx.state.get('letters', {}).get(lt) or {}).get('delivered'):
                continue
            try:
                append(ctx.conn, 'letter.delivered', ctx.by, {'how': 'inbox'}, ws=ctx.ws, conv=ctx.conv, subject=lt,
                       dedup='delivered:' + lt)
            except Exception as exc:  # noqa: BLE001  记不上不影响 wait 本身
                _err('提示：没能把 %s 记成已看到：%s' % (lt, exc))


def _wait(ctx, a):
    from .core import since
    if a.timeout > 3600:
        raise CliError('--timeout 最多 3600 秒')
    end = time.time() + max(0.0, a.timeout)
    scan = a.since
    while True:
        rows = since(ctx.conn, scan, conv=ctx.conv, limit=500)
        if any(e['type'] == 'letter.sent' for e in rows):
            ctx.refresh()      # 写给部门的信要按现在的部门和成员算该谁收
        hits = [e for e in rows if _wakes(e, ctx.state)]
        if hits:
            groups, cursor = _wait_groups(hits, a.since)
            ctx.refresh()
            for g in groups:
                print('#%d %s' % (g['event']['seq'], _wait_line(ctx, ctx.state, g)))
            print('游标 %d' % cursor)
            _mark_seen(ctx, groups)
            return 0
        if rows:
            scan = rows[-1]['seq']
            if len(rows) == 500:
                continue
        if time.time() >= end:
            break
        time.sleep(min(0.3, max(0.0, end - time.time())))
    print('无新信号，继续等')
    return 3


def _handoff(ctx, a):
    """≤60 行大白话交接单（arch-v11 §3）：大任务标题、总进度、每个小任务和每件活的状态与负责的人、
    没处理的"需要你" / 申请 / 信、最近 10 件事、当前游标、新负责人接手要跑的第一条命令。
    写了 --to 就记一条新事件 conversation.lead_changed {from, to}（by=lead）。"""
    from .core import events as core_events
    from .core.humanize import AI_NAMES

    # 1. 如果传了 --to，写入 conversation.lead_changed 事件
    if getattr(a, 'to', None):
        c = ctx.state.get('convs', {}).get(ctx.conv, {})
        curr_lead = c.get('lead_kind') or 'unknown'
        core_events.append(ctx.conn, 'conversation.lead_changed', ctx.by,
                           {'from': curr_lead, 'to': a.to},
                           ws=ctx.ws, conv=ctx.conv, subject=ctx.conv)
        ctx.refresh()

    snap = _snapshot_for(ctx)
    conv = snap.get('conversation') or {}
    prog = snap.get('progress') or {}
    pct = prog.get('pct', 0)
    prog_text = prog.get('text', '')

    lines = []
    lines.append('【负责人交接单】')
    lines.append('大任务：%s（%s）' % (conv.get('title') or '未命名任务', ctx.conv))
    lines.append('总进度：%d%%（%s）' % (pct, prog_text))

    lead_kind = conv.get('lead_kind') or 'unknown'
    lead_name = AI_NAMES.get(lead_kind, lead_kind.capitalize())
    if getattr(a, 'to', None):
        lines.append('负责人：已移交给 %s' % AI_NAMES.get(a.to, a.to.capitalize()))
    else:
        lines.append('当前负责人：%s' % lead_name)

    # 没处理的"需要你" / 申请 / 信
    decisions = [d for d in snap.get('decisions', []) if not d.get('resolved')]
    proposals = [p for p in snap.get('proposals', []) if p.get('status') == 'open']
    letters = [l for l in snap.get('letters', []) if l.get('kind') == 'request' and l.get('status') == 'open']

    lines.append('')
    lines.append('待处理事项：')
    pending = []
    for d in decisions:
        pending.append('需要你决定：%s' % _clip(d.get('title', ''), 30))
    for p in proposals:
        pending.append('改计划申请：%s' % _clip(p.get('reason', '') or p.get('id', ''), 30))
    for l in letters:
        pending.append('待回复信件：%s' % _clip(l.get('subject', '') or l.get('id', ''), 30))

    if pending:
        for it in pending[:5]:
            lines.append('  · ' + it)
        if len(pending) > 5:
            lines.append('  · …还有 %d 件待处理' % (len(pending) - 5))
    else:
        lines.append('  · 无待处理的“需要你”、申请或信件')

    # 每个小任务和每件活的状态与负责的人
    lines.append('')
    lines.append('任务与分工：')
    parts = snap.get('parts', [])
    tasks = snap.get('tasks', [])
    people = snap.get('people', {})

    def _task_desc(t):
        st = t.get('status')
        owner_em = t.get('owner')
        owner_label = people.get(owner_em, {}).get('label') or owner_em or '未分配'
        if st in ('done', 'reported', 'landed', 'accepted'):
            st_text = '做完了'
        elif st == 'working':
            p = t.get('progress') or {}
            if p.get('of'):
                st_text = '在做，第 %s/%s 步' % (p.get('step'), p.get('of'))
            else:
                st_text = '在做'
        elif st == 'kept' or (st == 'needs_you' and t.get('kept_reason')):
            st_text = '改动保留'
        elif st == 'failed':
            st_text = '没做成'
        elif st in ('queued', 'waiting'):
            st_text = '排队中'
        elif st == 'ready':
            st_text = '就绪'
        else:
            st_text = '已排期'
        return '「%s」：%s（%s）' % (_clip(t.get('title', ''), 25), st_text, owner_label)

    if parts:
        tasks_by_part = {}
        for t in tasks:
            tasks_by_part.setdefault(t.get('part'), []).append(t)
        task_line_count = 0
        for p in parts:
            if task_line_count >= 15:
                lines.append('  …（更多任务略）')
                break
            p_pct = 0
            for bp in prog.get('by_part', []):
                if bp.get('id') == p.get('id'):
                    p_pct = bp.get('pct', 0)
                    break
            cnt = p.get('count') or {}
            lines.append('  [%s] %s（做完 %s/%s，进度 %d%%）' % (
                p.get('id') or '零散', _clip(p.get('title', ''), 20),
                cnt.get('done', 0), cnt.get('all', 0), p_pct
            ))
            task_line_count += 1
            p_tasks = tasks_by_part.get(p.get('id')) or []
            for t in p_tasks[:3]:
                if task_line_count >= 15:
                    lines.append('    …（更多任务略）')
                    break
                lines.append('    · ' + _task_desc(t))
                task_line_count += 1
            if len(p_tasks) > 3 and task_line_count < 15:
                lines.append('    …还有 %d 件活' % (len(p_tasks) - 3))
                task_line_count += 1
    else:
        for t in tasks[:12]:
            lines.append('  · ' + _task_desc(t))
        if len(tasks) > 12:
            lines.append('  …还有 %d 件活' % (len(tasks) - 12))

    # 最近 10 件事
    lines.append('')
    lines.append('最近动向：')
    recent = snap.get('recent', [])
    last_10 = recent[-10:] if len(recent) >= 10 else recent
    if last_10:
        for item in last_10:
            lines.append('  · ' + _clip(item.get('text', ''), 50))
    else:
        lines.append('  · 暂无动态')

    # 当前游标与接手命令
    cursor = snap.get('seq', 0)
    lines.append('')
    lines.append('当前游标：%d' % cursor)
    lines.append('新负责人接手命令：')
    lines.append('  xay2 wait --conv %s --since %d' % (ctx.conv, cursor))

    if len(lines) > 60:
        lines = lines[:59] + ['…（交接单已按 60 行上限截断）']

    print('\n'.join(lines))
    return 0


COMMANDS = {
    'dept': _dept, 'hire': _hire, 'plan': _plan, 'answer': _answer, 'ask': _ask_tell, 'tell': _ask_tell, 'reply': _reply,
    'accept': _accept, 'check': _check, 'steer': _control, 'compact': _compact, 'interrupt': _control, 'stop': _control, 'resume': _control,
    'snapshot': _snapshot, 'brief': _brief, 'handoff': _handoff, 'events': _events, 'wait': _wait, 'notes-set': _notes_set,
    'board': _plan_show,
}


if __name__ == '__main__':
    raise SystemExit(main())
