from .db import open_db
from .events import append, since, max_seq, load_state, rebuild, _reading
from .reducers import empty_state, reduce

ALIAS_ENV = (('CLAUDE_CODE_SESSION_ID', 'claude:'), ('CODEX_THREAD_ID', 'codex:'))


def resolve_conv(conn, explicit=None, env=None):
    """谁是"当前对话"（protocol §1）：--conv → XAY2_CONV → 别名 claude:$CLAUDE_CODE_SESSION_ID → codex:$CODEX_THREAD_ID。

    --conv / XAY2_CONV 可以是对话编号，也可以是别名。找不到就返回 None——**不猜**，不会自动挑最近的对话。
    显式给了（--conv 或 XAY2_CONV）却认不出时也返回 None，不往下找。
    """
    import os
    env = os.environ if env is None else env
    with _reading(conn) as s:
        convs, aliases = s.get('convs', {}), s.get('aliases', {})
        for cand in (explicit, env.get('XAY2_CONV')):
            if cand:
                if cand in convs: return cand
                return aliases.get(cand)
        for key, prefix in ALIAS_ENV:
            val = env.get(key)
            if val and prefix + val in aliases:
                return aliases[prefix + val]
    return None


def load_aliases(conn):
    with _reading(conn) as s:
        return dict(s.get('aliases', {}))


__all__=['open_db','append','since','max_seq','rebuild','resolve_conv','load_state']
