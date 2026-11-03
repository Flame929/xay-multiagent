"""做完的结果自动送到负责人手上（protocol §7 & arch-v11-router-parity §2）。

- notify --hook：读当前目录对应的、最近一个没结束的对话，找出上次通知之后的要紧事
  （复用 wait 的筛选），按 Claude Code 钩子 JSON 打印；通知游标存在 $XAY2_HOME/notify/<对话编号>。
- hook install|uninstall|status：只改当前 git 项目根目录的 .claude/settings.local.json，
  配置 PostToolUse 钩子，已有设置原样保留。
"""
import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Optional


HOOK_COMMAND = "xay2 notify --hook"


def find_project_root(start_dir: Optional[Path] = None) -> Path:
    """找到当前 git 项目根目录（向上查找 .git 或 git rev-parse；没有就返回自身）。"""
    cur = (start_dir or Path.cwd()).resolve()
    try:
        res = subprocess.run(
            ['git', 'rev-parse', '--show-toplevel'],
            cwd=str(cur),
            text=True,
            capture_output=True,
            timeout=5,
        )
        if res.returncode == 0 and res.stdout.strip():
            return Path(res.stdout.strip()).resolve()
    except (OSError, subprocess.SubprocessError):
        pass

    p = cur
    while True:
        if (p / '.git').exists():
            return p
        if p.parent == p:
            break
        p = p.parent
    return cur


def settings_path(root: Optional[Path] = None) -> Path:
    base = root if root is not None else find_project_root()
    return base / '.claude' / 'settings.local.json'


def _load_settings(path: Path) -> dict:
    if not path.is_file():
        return {}
    try:
        data = json.loads(path.read_text(encoding='utf-8'))
        return data if isinstance(data, dict) else {}
    except (json.JSONDecodeError, OSError):
        return {}


def _save_settings(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')


def _has_hook(post_list: list) -> bool:
    for item in post_list:
        if not isinstance(item, dict):
            continue
        if item.get('command') == HOOK_COMMAND:
            return True
        sub_hooks = item.get('hooks')
        if isinstance(sub_hooks, list):
            for sub in sub_hooks:
                if isinstance(sub, dict) and sub.get('command') == HOOK_COMMAND:
                    return True
    return False


def hook_install(root: Optional[Path] = None) -> int:
    path = settings_path(root)
    settings = _load_settings(path)
    hooks = settings.setdefault('hooks', {})
    if not isinstance(hooks, dict):
        hooks = {}
        settings['hooks'] = hooks
    post = hooks.setdefault('PostToolUse', [])
    if not isinstance(post, list):
        post = []
        hooks['PostToolUse'] = post

    if _has_hook(post):
        print("已安装过钩子，无需重复安装")
        return 0

    post.append({
        "matcher": "*",
        "hooks": [
            {
                "type": "command",
                "command": HOOK_COMMAND,
            }
        ]
    })
    _save_settings(path, settings)
    print("已成功安装 PostToolUse 钩子到 .claude/settings.local.json")
    return 0


def hook_uninstall(root: Optional[Path] = None) -> int:
    path = settings_path(root)
    if not path.is_file():
        print("未安装过钩子（配置文件不存在）")
        return 0

    settings = _load_settings(path)
    hooks = settings.get('hooks')
    if not isinstance(hooks, dict):
        print("未安装过钩子")
        return 0
    post = hooks.get('PostToolUse')
    if not isinstance(post, list):
        print("未安装过钩子")
        return 0

    new_post = []
    removed = False
    for item in post:
        if not isinstance(item, dict):
            new_post.append(item)
            continue
        if item.get('command') == HOOK_COMMAND:
            removed = True
            continue
        sub_hooks = item.get('hooks')
        if isinstance(sub_hooks, list):
            new_subs = [s for s in sub_hooks if not (isinstance(s, dict) and s.get('command') == HOOK_COMMAND)]
            if len(new_subs) != len(sub_hooks):
                removed = True
            if new_subs:
                item['hooks'] = new_subs
                new_post.append(item)
            else:
                removed = True
        else:
            new_post.append(item)

    if not removed:
        print("未在 .claude/settings.local.json 中发现 xay 钩子，无需卸载")
        return 0

    hooks['PostToolUse'] = new_post
    _save_settings(path, settings)
    print("已成功从 .claude/settings.local.json 卸载 PostToolUse 钩子")
    return 0


def hook_status(root: Optional[Path] = None) -> int:
    path = settings_path(root)
    if not path.is_file():
        print("未安装（.claude/settings.local.json 不存在）")
        return 0

    settings = _load_settings(path)
    hooks = settings.get('hooks')
    post = hooks.get('PostToolUse') if isinstance(hooks, dict) else None
    if isinstance(post, list) and _has_hook(post):
        print("已安装：PostToolUse 钩子生效中（xay2 notify --hook）")
    else:
        print("未安装（.claude/settings.local.json 中未配置 xay2 notify --hook）")
    return 0


def hook_cmd(sub: str, root: Optional[Path] = None) -> int:
    if sub == 'install':
        return hook_install(root)
    if sub == 'uninstall':
        return hook_uninstall(root)
    if sub == 'status':
        return hook_status(root)
    print(f"未知 hook 操作：{sub}", file=sys.stderr)
    return 2


def notify_hook(conn, cwd: Optional[Path] = None) -> int:
    """处理 xay2 notify --hook 命令。"""
    from .core import since, load_state
    from .core.ids import ws_for
    from .office.paths import v2_home
    from .cli import _wakes, _wait_groups, _wait_line, _mark_seen, Ctx

    work_dir = (cwd or Path.cwd()).resolve()
    ws = ws_for(str(work_dir))
    state = load_state(conn)

    # 找当前工作区对应的、最近一个没结束的对话
    convs = state.get('convs', {}).values()
    candidates = [c for c in convs if c.get('ws') == ws and not c.get('ended')]
    if not candidates:
        return 0

    target = max(candidates, key=lambda c: (c.get('active_ts', 0), c.get('active_seq', 0)))
    conv_id = target['id']

    # 读通知游标（存在 $XAY2_HOME/notify/<对话编号>）
    notify_dir = v2_home() / 'notify'
    cursor_file = notify_dir / conv_id
    since_seq = 0
    if cursor_file.is_file():
        try:
            since_seq = int(cursor_file.read_text(encoding='utf-8').strip())
        except (ValueError, OSError):
            since_seq = 0

    scan = since_seq
    hits = []
    while True:
        rows = since(conn, scan, conv=conv_id, limit=500)
        if any(e.get('type') == 'letter.sent' for e in rows):
            state = load_state(conn)
        hits.extend([e for e in rows if _wakes(e, state)])
        if rows:
            scan = rows[-1]['seq']
            if len(rows) == 500:
                continue
        break

    if not hits:
        # 没有要紧事：不输出任何内容，退出码 0
        if scan > since_seq:
            try:
                notify_dir.mkdir(parents=True, exist_ok=True)
                cursor_file.write_text(str(scan), encoding='utf-8')
            except OSError:
                pass
        return 0

    groups, cursor = _wait_groups(hits, since_seq)
    if not groups:
        if cursor > since_seq:
            try:
                notify_dir.mkdir(parents=True, exist_ok=True)
                cursor_file.write_text(str(cursor), encoding='utf-8')
            except OSError:
                pass
        return 0

    ctx = Ctx(conn, conv_id, state)
    lines = [_wait_line(ctx, state, g) for g in groups]
    payload = {
        "hookSpecificOutput": {
            "hookEventName": "PostToolUse",
            "additionalContext": "\n".join(lines),
        }
    }
    print(json.dumps(payload, ensure_ascii=False))

    _mark_seen(ctx, groups)
    try:
        notify_dir.mkdir(parents=True, exist_ok=True)
        cursor_file.write_text(str(cursor), encoding='utf-8')
    except OSError:
        pass

    return 0
