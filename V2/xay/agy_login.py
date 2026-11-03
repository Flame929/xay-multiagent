"""agy 换号（xay 自己的一套，不再非得经过 Antigravity Tools 命令行）。2026-11-02 起。

agy 命令行认的登录在哪（实测，agy 1.4.12）：
1. 先看 macOS 钥匙串里 服务名 ``gemini``、账户名 ``antigravity`` 那一项（go-keyring 格式：``go-keyring-base64:`` + base64 的 JSON
   ``{"token": {access_token, token_type, refresh_token, expiry, expires_in}, "auth_method": "consumer", "id_token_email": 邮箱}``）；
2. 钥匙串里没有才看文件 ``~/.gemini/antigravity-cli/antigravity-oauth-token``（同样的 JSON）。
agy 自己会用 refresh_token 续期，并把续好的登录写回钥匙串。Antigravity 桌面 App 和一直开着的 agy 进程也会往这一项写
（2026-10-31 实测：Tools 说已切到 账号 A，钥匙串里却还是桌面 App 登录的 账号 B，agy 实际一直在用 账号 B）。
所以：**换号 = 把目标账号的登录写进钥匙串（和文件）**；**谁是当前号 = 钥匙串里那份登录是谁**，不信 Tools 自己的记录；
每次派活前再核对一次，被改回去就重新写（ensure_desired）。

各账号的登录从哪来：
- xay 自己的存档 ``$XAY2_HOME/agy-logins/``（每次换号前把钥匙串里当前那份存下来，所以 agy 续过期的最新登录也在）；
- 用户的 Antigravity Tools 账号文件 ``~/.antigravity_tools/accounts/<id>.json``（只当来源读，不调用它的程序）。
规矩：登录信息只在本模块里由程序读写，不打印、不进日志 / 账本 / 返回值；对外只说账号名。存档文件权限 600。
"""
from __future__ import annotations

import base64
import hashlib
import json
import os
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, Optional, Tuple

SERVICE = "gemini"
ACCOUNT = "antigravity"
PREFIX = "go-keyring-base64:"


def _home(home=None) -> Path:
    if home:
        return Path(home)
    env = os.environ.get("XAY2_HOME")
    return Path(env).expanduser() if env else Path.home() / ".xay" / "v2"


def _store(home=None, create=True) -> Path:
    path = _home(home) / "agy-logins"
    if create:
        path.mkdir(parents=True, exist_ok=True)
        try:
            os.chmod(path, 0o700)
        except OSError:
            pass
    return path


def tools_home() -> Path:
    custom = os.environ.get("XAY_AGY_TOOLS_HOME")
    return Path(custom).expanduser() if custom else Path.home() / ".antigravity_tools"


def login_file() -> Path:
    custom = os.environ.get("XAY_AGY_LOGIN_FILE")
    if custom:
        return Path(custom).expanduser()
    if os.environ.get("XAY_AGY_TOOLS_HOME"):
        return Path(os.environ["XAY_AGY_TOOLS_HOME"]).expanduser() / ".no-real-login-file"   # 同上：测试不碰真实文件
    return Path.home() / ".gemini" / "antigravity-cli" / "antigravity-oauth-token"


def _read_json(path, default=None):
    try:
        with open(path, "r", encoding="utf-8") as fh:
            return json.load(fh)
    except (OSError, ValueError):
        return default


def _write_private(path: Path, data) -> None:
    tmp = path.with_name(path.name + ".%d.tmp" % os.getpid())
    fd = os.open(str(tmp), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        json.dump(data, fh, ensure_ascii=False)
    os.replace(tmp, path)


# ---------------------------------------------------------------- 钥匙串（测试时用 XAY_AGY_KEYCHAIN_FILE 换成一个普通文件）

def _fake_keychain() -> Optional[Path]:
    custom = os.environ.get("XAY_AGY_KEYCHAIN_FILE")
    if custom:
        return Path(custom).expanduser()
    if os.environ.get("XAY_AGY_TOOLS_HOME"):
        # 账号清单换成了别处（测试、自定义）却没给假钥匙串：一律不碰这台机器真实的 agy 登录
        return Path(os.environ["XAY_AGY_TOOLS_HOME"]).expanduser() / ".no-real-keychain"
    return None


def _decode(raw: str) -> Optional[dict]:
    raw = (raw or "").strip()
    if raw.startswith(PREFIX):
        try:
            raw = base64.b64decode(raw[len(PREFIX):]).decode("utf-8")
        except (ValueError, UnicodeDecodeError):
            return None
    try:
        value = json.loads(raw)
    except ValueError:
        return None
    return value if isinstance(value, dict) else None


def keychain_read() -> Optional[dict]:
    fake = _fake_keychain()
    if fake is not None:
        try:
            return _decode(fake.read_text(encoding="utf-8"))
        except OSError:
            return None
    if sys.platform != "darwin":
        return None
    try:
        proc = subprocess.run(["security", "find-generic-password", "-s", SERVICE, "-a", ACCOUNT, "-w"],
                              stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=15)
    except (OSError, subprocess.SubprocessError):
        return None
    return _decode(proc.stdout) if proc.returncode == 0 else None


def keychain_write(payload: dict) -> bool:
    value = PREFIX + base64.b64encode(json.dumps(payload, ensure_ascii=False).encode("utf-8")).decode("ascii")
    fake = _fake_keychain()
    if fake is not None:
        try:
            fake.write_text(value, encoding="utf-8")
            return True
        except OSError:
            return False
    if sys.platform != "darwin":
        return False
    try:
        # 先删再加（和 Antigravity Tools 一样）。实测原地更新（-U）会把这一项写坏：agy 读不出来、直接变成没登录。
        # -A：允许 agy 读取时不弹窗。
        subprocess.run(["security", "delete-generic-password", "-s", SERVICE, "-a", ACCOUNT],
                       stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=15)
        proc = subprocess.run(["security", "add-generic-password", "-A", "-s", SERVICE, "-a", ACCOUNT, "-w", value],
                              stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=15)
    except (OSError, subprocess.SubprocessError):
        return False
    return proc.returncode == 0


# ---------------------------------------------------------------- 各账号的登录

def _payload_from_tools(token: dict, email: str) -> Optional[dict]:
    if not isinstance(token, dict) or not token.get("refresh_token"):
        return None
    expiry_ts = token.get("expiry_timestamp")
    expiry = datetime.fromtimestamp(float(expiry_ts), timezone.utc) if isinstance(expiry_ts, (int, float)) else datetime.fromtimestamp(0, timezone.utc)
    return {"token": {"access_token": token.get("access_token") or "", "token_type": token.get("token_type") or "Bearer",
                      "refresh_token": token["refresh_token"], "expiry": expiry.isoformat().replace("+00:00", "Z"),
                      "expires_in": int(token.get("expires_in") or 0)},
            "auth_method": "consumer", "id_token_email": email}


def tools_accounts() -> Dict[str, dict]:
    """Antigravity Tools 的账号：{id: {name, email, payload}}（邮箱和登录只在本模块用）。"""
    root = tools_home()
    index = _read_json(root / "accounts.json", {}) or {}
    out = {}
    for summary in index.get("accounts") or [] if isinstance(index, dict) else []:
        if not isinstance(summary, dict) or not summary.get("id"):
            continue
        detail = _read_json(root / "accounts" / ("%s.json" % summary["id"]), {}) or {}
        email = str(summary.get("email") or detail.get("email") or "").lower()
        out[summary["id"]] = {"name": summary.get("name") or detail.get("name") or "", "email": email,
                              "disabled": bool(summary.get("disabled") or detail.get("disabled")),
                              "payload": _payload_from_tools(detail.get("token") or {}, email) if email else None}
    return out


def _key(email: str) -> str:
    return hashlib.sha256(email.lower().encode("utf-8")).hexdigest()[:16]


def _saved(home=None) -> Dict[str, dict]:
    """xay 自己存下来的登录：{邮箱: payload}。"""
    out = {}
    for path in _store(home, create=False).glob("*.json"):
        if path.name == "desired.json":
            continue
        data = _read_json(path)
        if isinstance(data, dict) and data.get("id_token_email") and (data.get("token") or {}).get("refresh_token"):
            out[str(data["id_token_email"]).lower()] = data
    return out


def _email_of(payload: Optional[dict], home=None) -> Optional[str]:
    """一份登录是谁的：先看里面写的邮箱；没写就拿 refresh_token 去对已知的登录。"""
    if not isinstance(payload, dict):
        return None
    email = str(payload.get("id_token_email") or "").lower()
    if email:
        return email
    refresh = (payload.get("token") or {}).get("refresh_token")
    if not refresh:
        return None
    for mail, saved in _saved(home).items():
        if (saved.get("token") or {}).get("refresh_token") == refresh:
            return mail
    for acc in tools_accounts().values():
        if acc.get("payload") and acc["payload"]["token"]["refresh_token"] == refresh:
            return acc["email"]
    return None


def current_payload() -> Optional[dict]:
    """agy 现在用的那份登录：钥匙串优先，钥匙串里没有才看文件（和 agy 自己的顺序一样）。"""
    return keychain_read() or _read_json(login_file())


def current_email(home=None) -> Optional[str]:
    return _email_of(current_payload(), home)


def account_id_of(email: Optional[str]) -> Optional[str]:
    """邮箱 → Antigravity Tools 里的账号 id；Tools 里没有的号用 login-<摘要>。"""
    if not email:
        return None
    for account_id, acc in tools_accounts().items():
        if acc.get("email") == email.lower():
            return account_id
    return "login-" + _key(email)


def current_account_id(home=None) -> Optional[str]:
    return account_id_of(current_email(home))


def remember_current(home=None) -> Optional[str]:
    """把钥匙串里当前那份登录存进 xay 自己的存档（agy 续过期的最新登录也就留住了）。返回邮箱。"""
    payload = current_payload()
    email = _email_of(payload, home)
    if not email or not (payload.get("token") or {}).get("refresh_token"):
        return None
    payload = dict(payload, id_token_email=email)
    try:
        _write_private(_store(home) / ("%s.json" % _key(email)), payload)
    except OSError:
        return None
    return email


def _login_for(account_id: str, home=None) -> Tuple[Optional[str], Optional[dict]]:
    acc = tools_accounts().get(account_id)
    email = acc["email"] if acc else None
    if not email and account_id.startswith("login-"):
        email = next((m for m in _saved(home) if "login-" + _key(m) == account_id), None)
    if not email:
        return None, None
    return email, _saved(home).get(email) or (acc or {}).get("payload")


def _desired_path(home=None, create=False) -> Path:
    return _store(home, create=create) / "desired.json"


def _desired(home=None) -> dict:
    data = _read_json(_desired_path(home), {}) or {}
    return data if isinstance(data, dict) else {}


def desired_account_id(home=None) -> Optional[str]:
    return _desired(home).get("account_id")


def set_desired(account_id: str, home=None) -> None:
    """记下 xay 定好的号（ensure_desired 照它核对）。"""
    try:
        _write_private(_desired_path(home, create=True), {"account_id": account_id, "ts": time.time()})
    except OSError:
        pass


def tools_current_id() -> Optional[str]:
    index = _read_json(tools_home() / "accounts.json", {}) or {}
    return index.get("current_agy_account_id") if isinstance(index, dict) else None


def switch_to(account_id: str, home=None) -> Tuple[bool, str]:
    """把 agy 的登录换成这个账号（写钥匙串 + 文件，写完读回来核对）。返回 (成功?, 大白话)，不含邮箱。"""
    remember_current(home)
    before = current_payload()
    email, payload = _login_for(account_id, home)
    if not payload:
        return False, "没有这个账号的登录（Antigravity Tools 里也没有），得先用 agy 登录一次这个号"
    if not keychain_write(payload) or keychain_read() is None:
        if before:
            keychain_write(before)              # 写坏了就把原来那份放回去，别让 agy 变成没登录
        return False, "写不进 agy 的登录（钥匙串），已放回原来的号"
    try:
        path = login_file()
        path.parent.mkdir(parents=True, exist_ok=True)
        _write_private(path, payload)
    except OSError:
        pass                                    # 文件只是钥匙串不能用时的后备，写不进不算失败
    if current_email(home) != email:
        return False, "写进去了，但读回来不是这个号"
    set_desired(account_id, home)
    return True, "已换好"


def ensure_desired(home=None) -> Optional[str]:
    """派活前核对：xay 定好的号被别人（桌面 App、一直开着的 agy 进程）改回去了，就重新写回来。
    返回要提示的话（没动就返回 None）。"""
    desired = _desired(home)
    want = desired.get("account_id")
    if not want:
        return None
    now_id = current_account_id(home)
    if now_id == want or now_id is None:
        return None
    try:
        tools_changed = (tools_home() / "accounts.json").stat().st_mtime > float(desired.get("ts") or 0)
    except OSError:
        tools_changed = False
    if now_id == tools_current_id() and tools_changed:
        set_desired(now_id, home)               # 用户自己在 Antigravity Tools 里换的号：照用户的来，不改回去
        return None
    ok, _text = switch_to(want, home)
    names = {k: v.get("name") for k, v in tools_accounts().items()}
    was, will = names.get(now_id or "", "别的号"), names.get(want, "定好的号")
    return ("agy 的登录被改成了「%s」（可能是 Antigravity 桌面 App 或还开着的 agy 写回去的），已改回「%s」" % (was, will)) if ok \
        else ("agy 的登录被改成了「%s」，想改回「%s」没成功" % (was, will))
