"""额度（M3）：agy 每天的调用上限、agy 换号。工位用；不碰账本。

1. agy 每天最多 40 次调用（AGENTS.md、arch-v10 §7，旧系统 scripts/xay_drivers/agy_stream.py 同一个数）：
   V2 自己记一本小账 ``$XAY2_HOME/quota/agy-calls.jsonl``（一行一次，加锁追加）。当天用了多少 = V2 记的
   + 旧系统 ``$XAY_HOME/registry.jsonl`` 里当天的 ``agy_call`` 行（只读；新旧系统用的是同一批账号）。
   一次调用 = 往 agy 里发一轮（干活、复述、写交接摘要、/compact 都算）。
2. agy 换号：**允许且支持自动切换 agy CLI 账号**（用户 2026-10-30 明确同意，"我自己承担风险"）。
   2026-11-02 起用 xay 自己的一套（``xay/agy_login.py``：直接把目标账号的登录写进 agy 认的钥匙串那一项，写完读回来核对），
   不再非得经过 Antigravity Tools 命令行；它只在自己那套换不了时兜底。账号清单直接读 Tools 的账号文件，
   **谁是当前号以 agy 实际的登录为准**（Tools 自己记的当前号会和真实情况对不上，见 agy_login.py 开头）。
   邮箱、登录信息只在 agy_login.py 里由程序使用，对外只留序号、别名、状态。几个 agy 员工同时撞上额度时只换一次（加锁 + 2 分钟冷却）。
3. 四条线路的真实上限与剩余（含各 Gemini 账号还剩多少）在 ``xay/quota_live.py``（2026-10-31，管家定时查、写账本）；
   优先按 Antigravity Tools 的优先级（数字小的先用）挑额度充足（≥20%）的账号；换号时跳过 skip（已经用完的账号名）。
"""
from __future__ import annotations

import datetime as _dt
import fcntl
import json
import os
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path
from typing import Dict, List, Optional, Tuple

from . import providers

SWITCH_COOLDOWN_S = 120.0
TOOLS_TIMEOUT_S = 90.0
_ANSI_RE = re.compile(r"\x1b\[[0-9;]*[A-Za-z]")
_OK_STATUS = ("正常", "normal", "ok", "active", "可用")


def xay2_home() -> Path:
    env = os.environ.get("XAY2_HOME")
    return Path(env).expanduser() if env else Path.home() / ".xay" / "v2"


def _quota_dir(home=None) -> Path:
    d = Path(home) if home else xay2_home()
    path = d / "quota"
    path.mkdir(parents=True, exist_ok=True)
    return path


def _today() -> str:
    return _dt.date.today().isoformat()


# ---------------------------------------------------------------------------
# agy 每天的调用上限
# ---------------------------------------------------------------------------

def agy_daily_cap() -> int:
    try:
        env = int(os.environ.get("XAY2_AGY_DAILY_CAP") or 0)
    except ValueError:
        env = 0
    return env if env > 0 else int(providers.get("agy").daily_call_cap or 0)   # 0 = 不限


def _legacy_registry() -> Path:
    base = os.environ.get("XAY_HOME")
    return (Path(base).expanduser() if base else Path.home() / ".xay") / "registry.jsonl"


def _count_rows(path: Path, day: str, kind: Optional[str]) -> int:
    used = 0
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as fh:
            for line in fh:
                if '"%s"' % day not in line:
                    continue
                try:
                    row = json.loads(line)
                except ValueError:
                    continue
                if not isinstance(row, dict) or row.get("date") != day:
                    continue
                if kind and row.get("kind") != kind:
                    continue
                used += int(row.get("calls", 1) or 1)
    except OSError:
        return 0
    return used


def agy_calls_today(home=None, day: Optional[str] = None) -> Dict[str, int]:
    """{"v2": V2 记的, "legacy": 旧系统记的, "total": 合计, "cap": 上限}。"""
    day = day or _today()
    v2 = _count_rows(_quota_dir(home) / "agy-calls.jsonl", day, None)
    legacy = _count_rows(_legacy_registry(), day, "agy_call")
    return {"v2": v2, "legacy": legacy, "total": v2 + legacy, "cap": agy_daily_cap()}


def agy_take_call(em: str, purpose: str = "turn", home=None) -> Tuple[bool, Dict[str, int]]:
    """记一次 agy 调用。设了上限（XAY2_AGY_DAILY_CAP）而且到了就不记、返回 (False, 当天用量)；默认不限。"""
    path = _quota_dir(home) / "agy-calls.jsonl"
    day = _today()
    with open(path, "a+", encoding="utf-8") as fh:
        fcntl.flock(fh.fileno(), fcntl.LOCK_EX)
        try:
            usage = agy_calls_today(home, day)
            if usage["cap"] and usage["total"] >= usage["cap"]:
                return False, usage
            fh.seek(0, os.SEEK_END)
            fh.write(json.dumps({"date": day, "ts": round(time.time(), 3), "em": em, "purpose": purpose,
                                 "calls": 1}, ensure_ascii=False) + "\n")
            fh.flush()
            usage = dict(usage, v2=usage["v2"] + 1, total=usage["total"] + 1)
            return True, usage
        finally:
            fcntl.flock(fh.fileno(), fcntl.LOCK_UN)


# ---------------------------------------------------------------------------
# agy 换号（只通过 Antigravity Tools 命令行）
# ---------------------------------------------------------------------------

def tools_bin() -> Optional[str]:
    custom = os.environ.get("XAY_AGY_TOOLS_BIN")
    if custom:
        return custom
    found = shutil.which("antigravity-tools")
    if found:
        return found
    if sys.platform == "darwin":
        app = "/Applications/Antigravity Tools.app/Contents/MacOS/antigravity-tools"
        return app if os.path.exists(app) else None
    return None


def _run_tools(args: List[str], timeout: float = TOOLS_TIMEOUT_S) -> Tuple[int, str]:
    binary = tools_bin()
    if not binary:
        raise FileNotFoundError("找不到 antigravity-tools 命令")
    proc = subprocess.run([binary] + args, stdin=subprocess.DEVNULL, capture_output=True, text=True,
                          encoding="utf-8", errors="replace", timeout=timeout, cwd=str(Path.home()))
    return proc.returncode, _ANSI_RE.sub("", (proc.stdout or "") + "\n" + (proc.stderr or ""))


def parse_accounts(text: str) -> List[Dict[str, object]]:
    """`antigravity-tools list` 的表格 → [{index, current, name, status, ok}]。邮箱那一列直接丢掉。"""
    out = []
    for line in _ANSI_RE.sub("", text or "").splitlines():
        parts = [p.strip() for p in line.split("|")]
        if len(parts) < 5 or not parts[0].isdigit():
            continue
        status = parts[4]
        out.append({"index": int(parts[0]), "current": "👉" in parts[1] or "*" in parts[1],
                    "name": parts[3] or "账号 %s" % parts[0], "status": status,
                    "ok": any(w in status.lower() for w in _OK_STATUS)})
    return out


def agy_accounts(timeout: float = TOOLS_TIMEOUT_S) -> List[Dict[str, object]]:
    """换号用的账号清单 [{index, id, name, current, status, ok}]：读 Tools 的账号文件，当前号以 agy 实际的登录为准；
    没有账号文件才退回 ``antigravity-tools list``（它说的当前号可能不准）。"""
    try:
        from . import quota_live
        rich = quota_live.agy_accounts()
    except Exception:  # noqa: BLE001
        rich = []
    if rich:
        return [{"index": a["index"], "id": a["id"], "name": a["name"], "current": a["current"],
                 "status": "停用" if a["status"] == "blocked" else "正常", "ok": a["status"] != "blocked"} for a in rich]
    code, text = _run_tools(["list"], timeout)
    if code != 0:
        raise RuntimeError("antigravity-tools list 退出码 %s" % code)
    return parse_accounts(text)


BLOCKED_DAYS = 7      # 记成"不能用"的 Gemini 账号多久后再试（地区 / 资格问题一般不会自己好）


def mark_agy_blocked(name, reason: str, home=None) -> None:
    """这个 Gemini 账号用不了（比如地区不支持）：记在 $XAY2_HOME/quota/agy-blocked.json，换号时跳过。只记账号别名和原因。"""
    if not name:
        return
    path = _quota_dir(home) / "agy-blocked.json"
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        data = {}
    if not isinstance(data, dict):
        data = {}
    data[str(name)] = {"reason": str(reason or "")[:120], "ts": round(time.time(), 3)}
    try:
        path.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
    except OSError:
        pass


def agy_blocked(home=None) -> Dict[str, Dict[str, object]]:
    """记成"不能用"、还没过期的 Gemini 账号：{别名: {reason, ts}}。"""
    try:
        data = json.loads((_quota_dir(home) / "agy-blocked.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    cutoff = time.time() - BLOCKED_DAYS * 86400
    return {k: v for k, v in (data or {}).items() if isinstance(v, dict) and float(v.get("ts") or 0) >= cutoff}


def merged_agy_accounts(timeout: float = TOOLS_TIMEOUT_S,
                        fresh: Optional[List[Dict[str, object]]] = None, home=None) -> List[Dict[str, object]]:
    blocked = agy_blocked(home)
    return [dict(a, status="blocked", blocked_reason=blocked[str(a.get("name"))]["reason"])
            if str(a.get("name") or "") in blocked else a for a in _merged_agy_accounts(timeout, fresh)]


def _merged_agy_accounts(timeout: float = TOOLS_TIMEOUT_S,
                         fresh: Optional[List[Dict[str, object]]] = None) -> List[Dict[str, object]]:
    """换号用的账号清单：以 ``antigravity-tools list`` 为准（序号、谁是当前、能不能用——真正执行 switch 的就是它），
    再按序号把 quota_live 读到的优先级 / 剩余额度 / 额度状态补上（两边序号不一致时按名字对）。
    若提供了 fresh（账本中管家定时查到的新读数），某个账号 fresh 里的 checked_at 比 Tools 记录新（或 Tools 记录没有时间）
    就用 fresh 的 status / left / resets_at / checked_at / source 覆盖。
    命令行读不到才退回 quota_live 自己的清单（若有 fresh 同样按规则覆盖）。
    命令行说不能用（ok 是 False）的照旧当 blocked。"""
    try:
        base = agy_accounts(timeout)
    except (OSError, RuntimeError, subprocess.SubprocessError):
        base = []
    try:
        from . import quota_live
        rich = quota_live.agy_accounts()
    except Exception:  # noqa: BLE001  读不到额度记录：只按命令行清单换
        rich = []

    fresh_by_name = {str(f.get("name") or ""): f for f in (fresh or []) if isinstance(f, dict) and f.get("name")}

    def _apply_fresh(m: Dict[str, object]) -> Dict[str, object]:
        f = fresh_by_name.get(str(m.get("name") or ""))
        if f:
            tools_checked = m.get("checked_at")
            fresh_checked = f.get("checked_at")
            is_newer = False
            if tools_checked is None:
                is_newer = True
            elif fresh_checked is not None:
                try:
                    is_newer = float(fresh_checked) > float(tools_checked)
                except (ValueError, TypeError):
                    is_newer = False
            if is_newer:
                for key in ("status", "left", "resets_at", "checked_at", "source"):
                    if key in f:
                        m[key] = f[key]
        return m

    if not base:
        out = []
        for r in rich:
            m = _apply_fresh(dict(r))
            if m.get("ok") is False or m.get("status") == "blocked":
                m["status"] = "blocked"
            out.append(m)
        return out

    by_index = {int(r.get("index") or 0): r for r in rich}
    by_name = {str(r.get("name") or ""): r for r in rich if r.get("name")}
    out = []
    for a in base:
        r = by_index.get(int(a["index"]))
        if r is None or (r.get("name") and str(r.get("name")) != str(a.get("name"))):
            r = by_name.get(str(a.get("name") or "")) or {}
        m = dict(a)
        for key in ("priority", "left", "resets_at", "checked_at", "source"):
            if r.get(key) is not None:
                m[key] = r[key]
        if r.get("status") in ("ok", "tight", "exhausted", "unknown"):
            m["status"] = r["status"]
        elif m.get("status") not in ("ok", "tight", "exhausted", "unknown", "blocked"):
            m["status"] = "ok"
        m = _apply_fresh(m)
        if a.get("ok") is False:
            m["status"] = "blocked"                     # 命令行说这个号不能用（停用 / 失效）：不管额度记录怎么写都不换过去
            m["ok"] = False
        out.append(m)
    return out


def choose_agy_account(accounts: Optional[List[Dict[str, object]]] = None,
                       skip=(),
                       exclude: Optional[object] = None) -> Optional[Dict[str, object]]:
    """智能优选下一个 agy 账号：
    - 优先读取 quota_live 的各账号额度与优先级（或退回 agy_accounts()）；
    - 按 Antigravity Tools 设定的优先级（priority 数字越小越优）；
    - 同优先级下挑剩余额度（left / remaining_pct）最多的；
    - 优先从状态充足（status='ok' 或 left>=20）的账号池里挑；若没有再挑偏紧（status='tight' 或 left>0）的；
    - 排除当前账号、禁用/封禁账号（blocked）、额度已用完账号（exhausted / left <= 0）、以及 skip 列表中的账号。
    """
    if accounts is None:
        accounts = merged_agy_accounts()
    if not accounts:
        return None

    skip_set = set(skip or ())
    cur = next((a for a in accounts if a.get("current")), None)

    def is_excluded(a: Dict[str, object]) -> bool:
        if a.get("current"):
            return True
        a_id = str(a.get("id") or "")
        a_name = str(a.get("name") or "")
        a_idx = str(a.get("index") or "")
        if exclude and str(exclude) in (a_id, a_name, a_idx):
            return True
        if a_name in skip_set or a_id in skip_set:
            return True
        if a.get("status") in ("blocked", "exhausted"):
            return True
        left = a.get("left") if a.get("left") is not None else a.get("remaining_pct")
        if left is not None and float(left) <= 0:
            return True
        if not a.get("ok", True) and a.get("status") not in ("ok", "tight"):
            return True
        return False

    candidates = [a for a in accounts if not is_excluded(a)]
    if not candidates:
        return None

    def is_ok(a: Dict[str, object]) -> bool:
        left = a.get("left") if a.get("left") is not None else a.get("remaining_pct")
        if left is not None:
            return float(left) >= 20.0
        return a.get("status") == "ok" or a.get("ok") is True

    ok_pool = [a for a in candidates if is_ok(a)]
    tight_pool = [a for a in candidates if not is_ok(a)]

    def sort_key(a: Dict[str, object]):
        prio = a.get("priority")
        prio_val = int(prio) if isinstance(prio, (int, float)) else 999
        left = a.get("left") if a.get("left") is not None else a.get("remaining_pct")
        left_val = float(left) if left is not None else (100.0 if a.get("ok") else 0.0)
        # 优先级、剩余额度都一样时：从当前账号往下数（到头再从 1 开始），把用量摊开，也不会马上换回刚用完的号
        start = int(cur.get("index") or 0) if cur else 0
        idx_val = (int(a.get("index") or 0) - start - 1) % 10_000
        return (prio_val, -left_val, idx_val)

    if ok_pool:
        ok_pool.sort(key=sort_key)
        return ok_pool[0]
    if tight_pool:
        tight_pool.sort(key=sort_key)
        return tight_pool[0]

    # 保底：若只有基础 list 信息，按序号顺序
    start = int(cur["index"]) if cur and cur.get("index") else 0
    ordered = sorted(candidates, key=lambda a: ((int(a.get("index") or 0) - start - 1) % 10_000))
    return ordered[0] if ordered else None


def _pick_next(accounts: List[Dict[str, object]], skip=()) -> Optional[Dict[str, object]]:
    """向后兼容 _pick_next：优先按优先级和额度挑选，没有则按序号轮转。"""
    chosen = choose_agy_account(accounts, skip=skip)
    if chosen:
        return chosen
    if not accounts:
        return None
    cur = next((a for a in accounts if a.get("current")), None)
    start = int(cur["index"]) if cur and cur.get("index") else 0
    ordered = sorted(accounts, key=lambda a: ((int(a.get("index") or 0) - start - 1) % 10_000))
    skip_set = set(skip or ())
    return next((a for a in ordered if a.get("ok") and not a.get("current") and a.get("name") not in skip_set), None)


def ensure_agy_login(home=None) -> Optional[str]:
    """派活前核对 agy 的登录还是不是 xay 定好的号；被桌面 App / 还开着的 agy 改回去了就写回来。返回要提示的话。"""
    from . import agy_login
    msg = agy_login.ensure_desired(home)
    if msg:
        _log(home, msg)                         # 记下每次被改回去的情况，看看到底是谁在改
    return msg


def _log(home, text: str) -> None:
    try:
        logs = (Path(home) if home else xay2_home()) / "logs"
        logs.mkdir(parents=True, exist_ok=True)
        with open(logs / "agy-switch.log", "a", encoding="utf-8") as fh:
            fh.write(time.strftime("%Y-%m-%d %H:%M:%S") + " " + text + "\n")
    except OSError:
        pass


def switch_agy_account(reason: str, home=None, cooldown_s: float = SWITCH_COOLDOWN_S, skip=(), target: Optional[object] = None, accounts: Optional[List[Dict[str, object]]] = None) -> Dict[str, object]:
    """agy 换号：通过 Antigravity Tools 命令行切换账号/cookie/凭据。
    返回 {ok, switched, text, to?}（text 是大白话，不含邮箱）。

    - target：指定目标账号（序号 / 名称 / id；不传或传 auto 则按优先级与剩余额度自动优选）；
    - skip：真实额度已经用完的账号名（管家查到的各账号额度），自动优选时不换过去；
    - cooldown_s：冷却防抖（自动切换时 2 分钟内刚换过则不重复换；手动指定 target 时跳过冷却）；
    - accounts：可选直接传入账号列表（测试或调用方已有数据时）。
    """
    qdir = _quota_dir(home)
    with open(qdir / "agy-switch.lock", "a+") as lock:
        fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
        try:
            state_path = qdir / "agy-switch.json"
            try:
                last = json.loads(state_path.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                last = {}
            ago = time.time() - float(last.get("ts") or 0)
            is_auto = target in (None, "", "auto")

            if is_auto and 0 <= ago < cooldown_s:
                return {"ok": True, "switched": False, "to": last.get("to"),
                        "text": "%d 秒前刚换过 Gemini 账号（到「%s」），直接重做" % (int(ago), last.get("to") or "?")}

            # 以 antigravity-tools list 为准，补上优先级、剩余额度（merged_agy_accounts）
            if accounts is None:
                accounts = merged_agy_accounts(home=home)
                if not accounts:
                    return {"ok": False, "switched": False, "text": "读不到 Antigravity Tools 的账号清单"}

            cur = next((a for a in accounts if a.get("current")), None)

            if is_auto:
                chosen = choose_agy_account(accounts, skip=skip)
                if not chosen:
                    if choose_agy_account(accounts):
                        return {"ok": False, "switched": False, "text": "别的 Gemini 账号额度也都用完了"}
                    return {"ok": False, "switched": False, "text": "Antigravity Tools 里没有别的可用 Gemini 账号"}
            else:
                target_str = str(target).strip()
                chosen = next((a for a in accounts if target_str in (str(a.get("index")), str(a.get("id")), str(a.get("name")))), None) \
                    or next((a for a in accounts if str(a.get("name", "")).lower().startswith(target_str.lower())), None)
                if not chosen:
                    return {"ok": False, "switched": False, "text": "没有叫「%s」的 agy 账号（可以用 xay agy-accounts 看清单）" % target_str}

            if cur and chosen.get("index") == cur.get("index"):
                return {"ok": True, "switched": False, "to": chosen.get("name"),
                        "text": "Gemini 已经是「%s」" % chosen.get("name")}

            from . import agy_login
            own_why = None
            if chosen.get("id"):
                # 先用 xay 自己的一套：把这个号的登录写进 agy 认的钥匙串，写完读回来核对
                ok_own, own_why = agy_login.switch_to(str(chosen["id"]), home)
            if not chosen.get("id") or not ok_own:
                try:                                                                 # 兜底：Antigravity Tools 命令行
                    code, _text = _run_tools(["switch", str(chosen["index"]), "--target", "agy"])
                except subprocess.TimeoutExpired:
                    return {"ok": False, "switched": False, "text": "Antigravity Tools 切换超时" + ("（自己换也没成：%s）" % own_why if own_why else "")}
                except (OSError, subprocess.SubprocessError) as exc:
                    return {"ok": False, "switched": False, "text": "Antigravity Tools 切换失败：%s" % exc}
                try:
                    now = next((a for a in agy_accounts() if a.get("current")), None)   # 切完按 agy 实际的登录核对
                except (OSError, RuntimeError, subprocess.SubprocessError):
                    now = None
                if code != 0 or not now or now["index"] != chosen["index"]:
                    return {"ok": False, "switched": False, "text": "没能切过去" + ("：%s" % own_why if own_why else "（Antigravity Tools 也没切成）")}
                if chosen.get("id"):
                    agy_login.set_desired(str(chosen["id"]), home)

            text = "Gemini 已从「%s」换到「%s」" % (cur["name"] if cur else "未知", chosen["name"])
            try:
                state_path.write_text(json.dumps({"ts": time.time(), "to": chosen["name"],
                                                  "index": chosen["index"]}, ensure_ascii=False), encoding="utf-8")
            except OSError:
                pass
            _log(home, "%s；原因：%s" % (text, reason))
            return {"ok": True, "switched": True, "to": chosen["name"], "text": text}
        finally:
            fcntl.flock(lock.fileno(), fcntl.LOCK_UN)
