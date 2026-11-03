"""四条额度线路的真实上限与剩余（2026-10-31）。查询部分从旧系统 scripts/xay_quota.py 移植（旧文件不动）。

每条线路用它自己的官方查法（和旧系统 `xay quota` 一样）：
- Codex · Plus（codex-sub）：起一个 `codex app-server`，问 account/rateLimits/read（5 小时 / 本周用了百分之几）；
- Codex · 中转站（codex-relay）：GET <中转站地址>/usage（今天 / 本周 / 本月的美元上限，还能用多少钱）；
- DeepSeek（dsh，OpenCode Go）：GET https://opencode.ai/zen/go/v1/usage（5 小时 / 本周 / 本月）；
- Gemini（agy）：`agy -p "/usage"`（只读当前登录的账号，只看 Gemini 那组）。各账号的概况另读用户的
  Antigravity Tools 清单（`~/.antigravity_tools/accounts.json` 和 `accounts/<id>.json`），按 arch-v6 §1.5
  只取名字、优先级、停用标记和额度字段，读出来马上丢掉其余字段，不碰登录信息。

**安全**：查询要用的 key 由本进程在运行时从已有配置里读出、直接拿去调官方接口，只在函数里用；不写进账本、
日志、页面、直播、汇报。出错信息是写好的大白话，不带 key。账本里只有百分比、重置时间、余额、账号显示名。

管家（office/daemon.py）起一个 QuotaThread：每隔几分钟（reactions.json 的 quota.refresh_s，默认 300 秒；
agy 单独看 quota.agy_refresh_s，默认 600 秒）在后台各开一个线程查一条线路，各有超时（quota.timeout_s），
agy 慢也不挡别的线路、不挡排程。查到的写成 quota.updated（by=office，协议待定条目 2026-10-31）。
变了才写：任一窗口差 ≥1 个百分点、重置时间 / 余额 / 账号变了、查询失败的原因变了，或距上次 ≥ quota.keepalive_s。
测试用的临时 XAY2_HOME 默认不查（XAY2_QUOTA_LIVE=1 / 0 可以强制打开 / 关掉）。

快照（core/snapshot.py 的 quota_lines）把每条线路最新的读数合起来（工位从 Codex 通知里读到的和这里查到的，
谁新用谁）；这里另有几个给管家 / 工位做决定用的小函数：line_gone_text（"中转站今天也用完了"）、
agent_out（这种 AI 是不是所有线路 / 账号都用完了）、used_up_accounts（哪几个 Gemini 账号已经用完）。
"""
from __future__ import annotations

import json
import logging
import os
import queue
import re
import signal
import subprocess
import threading
import time
import urllib.error
import urllib.request
from datetime import datetime, timedelta
from pathlib import Path

LOG = logging.getLogger("xay.quota_live")

LINES = ("codex-sub", "codex-relay", "dsh", "agy")
LINE_AGENT = {"codex-sub": "codex", "codex-relay": "codex", "dsh": "dsh", "agy": "agy"}
SHORT = {"codex-sub": "Plus", "codex-relay": "中转站", "dsh": "DeepSeek", "agy": "Gemini"}
SOURCE = {"codex-sub": "Codex 官方额度查询", "codex-relay": "中转站用量接口", "dsh": "OpenCode 官方用量接口",
          "agy": "agy 官方 /usage"}
WINDOW_MINS = {"5h": 300, "day": 1440, "week": 10080, "month": 43200}
TIGHT_PCT = 20          # 剩余低于这个算偏紧（和旧系统、快照一致）
DEFAULTS = {"refresh_s": 300, "agy_refresh_s": 600, "timeout_s": 90, "keepalive_s": 900}
TICK_S = 5.0
STEP_PCT = 1.0          # 任一窗口变化 ≥1 个百分点才算变了
RESET_STEP_S = 600      # 重置时间变了 ≥10 分钟才算变了（滚动窗口的重置时间会一点点往后挪）
ACCOUNTS_FILE = "agy-accounts.json"


class LiveError(Exception):
    """实时查询失败；消息是给人看的大白话，绝不包含 key。"""


def _v2_home(home=None) -> Path:
    if home:
        return Path(home)
    env = os.environ.get("XAY2_HOME")
    return Path(env).expanduser() if env else Path.home() / ".xay" / "v2"


def _read_json(path, default=None):
    try:
        with Path(path).open(encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return default


# ---------------------------------------------------------------- 解析（纯函数，测试直接喂旧系统的样例）

def _window(key, name, remaining, resets_at):
    remaining = None if remaining is None else max(0, min(100, int(round(float(remaining)))))
    return {"key": key, "name": name, "remaining_pct": remaining, "resets_at": int(resets_at) if resets_at else None}


def _iso_ts(value):
    if not value:
        return None
    try:
        return int(datetime.fromisoformat(str(value).replace("Z", "+00:00")).timestamp())
    except ValueError:
        return None


def parse_opencode_usage(data):
    """OpenCode Go 用量（dsh）：percent 是已用百分比。"""
    usage = (data or {}).get("usage") if isinstance(data, dict) else None
    if not isinstance(usage, dict):
        raise LiveError("OpenCode 用量接口返回的格式看不懂")
    windows = []
    for key, wkey, name in (("rolling", "5h", "5 小时"), ("weekly", "week", "本周"), ("monthly", "month", "本月")):
        item = usage.get(key)
        if isinstance(item, dict) and item.get("percent") is not None:
            windows.append(_window(wkey, name, 100 - float(item["percent"]), _iso_ts(item.get("resetsAt"))))
    if not windows:
        raise LiveError("OpenCode 用量接口没有返回额度窗口")
    return {"windows": windows}


def parse_relay_usage(data, now):
    """中转站 /usage：订阅按美元计，有日 / 周 / 月上限（null 表示不限）。"""
    if not isinstance(data, dict):
        raise LiveError("中转站用量接口返回的格式看不懂")
    if data.get("isValid") is False:
        raise LiveError("中转站说这个 key 已失效")
    sub = data.get("subscription") if isinstance(data.get("subscription"), dict) else {}
    windows = []
    for prefix, wkey, name in (("daily", "day", "今天"), ("weekly", "week", "本周"), ("monthly", "month", "本月")):
        limit, used = sub.get("%s_limit_usd" % prefix), sub.get("%s_usage_usd" % prefix)
        if not limit:
            continue
        reset = None
        # "今天"的额度什么时候重置中转站不给：不猜（2026-10-31 实测它在早上 9 点左右重置，不是 0 点）。
        # 用完了就靠每 5 分钟一次的查询发现它恢复
        if prefix == "weekly" and _iso_ts(sub.get("weekly_window_start")):
            reset = _iso_ts(sub["weekly_window_start"]) + 7 * 86400
        windows.append(_window(wkey, name, (1 - float(used or 0) / float(limit)) * 100, reset))
    out = {"windows": windows}
    usd = str(data.get("unit") or "USD").upper() == "USD"
    if isinstance(data.get("remaining"), (int, float)) and not isinstance(data.get("remaining"), bool):
        left = max(0.0, float(data["remaining"]))
        out["balance_usd"] = round(left, 2)
        out["balance"] = ("还能用 $%.0f" % left) if usd else ("还剩 %.0f" % left)
    expires = _iso_ts(sub.get("expires_at"))
    if expires and expires - now < 3 * 86400:
        out["warning"] = "中转订阅 %s 到期" % fmt_reset(expires, now)
    if not windows and "balance" not in out:
        raise LiveError("中转站没有返回额度上限")
    return out


def parse_codex_rate_limits(result):
    """codex app-server 的 rateLimits：usedPercent 是已用百分比，resetsAt 是秒级时间戳。"""
    limits = result.get("rateLimits") if isinstance(result, dict) and "rateLimits" in result else result
    if not isinstance(limits, dict):
        raise LiveError("Codex 没有返回额度信息")
    windows = []
    for key in ("primary", "secondary"):
        item = limits.get(key)
        if not isinstance(item, dict) or item.get("usedPercent") is None:
            continue
        mins = int(item.get("windowDurationMins") or (300 if key == "primary" else 10080))
        wkey, name = ("5h", "5 小时") if mins == 300 else ("week", "本周") if mins == 10080 else ("%dm" % mins, "%d 小时" % (mins // 60))
        w = _window(wkey, name, 100 - float(item["usedPercent"]), item.get("resetsAt"))
        w["mins"] = mins
        windows.append(w)
    if not windows:
        raise LiveError("Codex 没有返回额度窗口")
    return {"windows": windows}


AGY_WINDOW_NAMES = {"five hour": ("5h", "5 小时"), "weekly": ("week", "本周"), "daily": ("day", "今天")}


def parse_agy_usage(text):
    """`agy -p "/usage"` 的输出：每行「分组\\t名称\\t剩余%\\t重置时间」。xay 只用 gemini 模型，只看 Gemini 那组。"""
    windows = []
    for line in str(text or "").splitlines():
        parts = [p.strip() for p in line.split("\t")]
        if len(parts) < 3 or "gemini" not in parts[0].lower():
            continue
        match = re.match(r"(\d+(?:\.\d+)?)\s*%", parts[2])
        if not match:
            continue  # 例如 Disabled
        label = parts[1].lower()
        wkey, name = next((v for k, v in AGY_WINDOW_NAMES.items() if k in label), (label[:16], parts[1][:16]))
        windows.append(_window(wkey, name, float(match.group(1)), _iso_ts(parts[3]) if len(parts) > 3 else None))
    if not windows:
        raise LiveError("agy /usage 的输出里没有 Gemini 额度")
    return {"windows": windows}


# ---------------------------------------------------------------- 取数（会联网或起进程；key 只在这里用，不外传）

def _http_json(url, key, label, timeout=15):
    req = urllib.request.Request(url, headers={"Authorization": "Bearer " + key, "Accept": "application/json",
                                               "User-Agent": "curl/7.68.0"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode("utf-8", "replace"))
    except urllib.error.HTTPError as exc:
        raise LiveError("%s返回 HTTP %s" % (label, exc.code) + ("（key 无效或无权限）" if exc.code in (401, 403) else "")) from None
    except (urllib.error.URLError, OSError):
        raise LiveError("连不上%s" % label) from None
    except ValueError:
        raise LiveError("%s返回的不是 JSON" % label) from None


def _dsh_key():
    """和 dsh 一致：先看 headless 配置指定的环境变量名，再按 dsh 插件的顺序找。只返回给 fetch_dsh 用。"""
    names = []
    try:
        match = re.search(r"apiKeyEnv:\s*['\"]?([A-Z0-9_]+)",
                          (Path.home() / ".dsh/profiles/headless/cordis.patch.yml").read_text(encoding="utf-8"))
        if match:
            names.append(match.group(1))
    except OSError:
        pass
    names += [n for n in ("OPENCODE_XAY_GO_API_KEY", "OPENCODE_GO_API_KEY") if n not in names]
    refs = {}
    try:
        in_refs = False
        for line in (Path.home() / ".dsh/.credentials.yaml").read_text(encoding="utf-8").splitlines():
            if re.match(r"^\S", line):
                in_refs = line.startswith("refs:")
                continue
            match = re.match(r"^\s+([A-Za-z_][A-Za-z0-9_]*):\s*['\"]?([^'\"\s]+)", line)
            if in_refs and match:
                refs[match.group(1)] = match.group(2)
    except OSError:
        pass
    for name in names:
        if refs.get(name) or os.environ.get(name):
            return refs.get(name) or os.environ.get(name)
    auth = _read_json(Path(os.environ.get("OPENCODE_DATA_DIR") or Path.home() / ".local/share/opencode") / "auth.json", {}) or {}
    return ((auth.get("opencode-go") or {}).get("key")) or None


def fetch_dsh(timeout=20, now=None):
    key = _dsh_key()
    if not key:
        raise LiveError("没找到 dsh 的 OpenCode Go key")
    out = parse_opencode_usage(_http_json("https://opencode.ai/zen/go/v1/usage", key, "OpenCode 用量接口", timeout))
    out["source"] = SOURCE["dsh"]
    return out


def _codex_provider(name):
    path = Path(os.environ.get("CODEX_HOME") or Path.home() / ".codex") / "config.toml"
    try:
        import tomllib
        with path.open("rb") as f:
            config = tomllib.load(f)
    except (ImportError, OSError, ValueError):
        raise LiveError("读不了 Codex 配置") from None
    provider = ((config.get("model_providers") or {}).get(name)) or {}
    if not provider.get("base_url"):
        raise LiveError("Codex 配置里没有中转站地址")
    return provider


def fetch_relay(timeout=20, now=None):
    from . import providers
    provider = _codex_provider(providers.CODEX_LINES["codex-relay"])
    key = provider.get("experimental_bearer_token") or (os.environ.get(provider["env_key"]) if provider.get("env_key") else None)
    if not key:
        raise LiveError("Codex 配置里没有中转站 key")
    out = parse_relay_usage(_http_json(str(provider["base_url"]).rstrip("/") + "/usage", key, "中转站用量接口", timeout),
                            time.time() if now is None else now)
    out["source"] = SOURCE["codex-relay"]
    return out


def _kill_group(proc):
    try:
        os.killpg(proc.pid, signal.SIGKILL)
    except OSError:
        try:
            proc.kill()
        except OSError:
            pass
    try:
        proc.wait(timeout=5)
    except subprocess.SubprocessError:
        pass


def fetch_codex_sub(timeout=30, now=None):
    """起一个只读、关掉 MCP 的 app-server（走订阅那条线路），问一次额度就关掉。"""
    from . import providers
    from .adapters.codex import disabled_mcp_servers
    command = providers.resolve_command("codex", {})
    args = ["-c", 'model_provider="%s"' % providers.CODEX_LINES["codex-sub"], "-c", 'approval_policy="never"',
            "-c", 'sandbox_mode="read-only"']
    for server in disabled_mcp_servers():
        args += ["-c", "mcp_servers.%s.enabled=false" % server]
    try:
        proc = subprocess.Popen(list(command) + ["app-server", "--listen", "stdio://"] + args, stdin=subprocess.PIPE,
                                stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True, encoding="utf-8",
                                errors="replace", cwd=str(Path.home()), start_new_session=True)
    except OSError:
        raise LiveError("找不到 codex 命令") from None
    result = {}

    def talk():
        try:
            for msg in ({"id": 1, "method": "initialize", "params": {"clientInfo": {"name": "xay-quota", "version": "2"}}},
                        {"method": "initialized", "params": {}},
                        {"id": 2, "method": "account/rateLimits/read", "params": {}}):
                proc.stdin.write(json.dumps(msg) + "\n")
            proc.stdin.flush()
            for line in proc.stdout:
                try:
                    msg = json.loads(line)
                except ValueError:
                    continue
                if isinstance(msg, dict) and msg.get("id") == 2:
                    result.update(msg)
                    return
        except (OSError, ValueError):
            return

    worker = threading.Thread(target=talk, daemon=True)
    worker.start()
    worker.join(timeout)
    _kill_group(proc)
    if not result:
        raise LiveError("Codex 额度查询超时")
    if result.get("error"):
        raise LiveError("Codex 额度查询出错（可能没登录 ChatGPT 账号）")
    out = parse_codex_rate_limits(result.get("result"))
    out["source"] = SOURCE["codex-sub"]
    return out


def fetch_agy(timeout=60, now=None):
    """`agy -p "/usage"`。查询前后当前账号没变，才把读数记到这个账号名下（account_id 只在本进程里用）。"""
    from . import providers
    command = providers.resolve_command("agy", {})
    before = agy_current_id()
    try:
        proc = subprocess.Popen(list(command) + ["-p", "/usage"], stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                                stderr=subprocess.DEVNULL, text=True, encoding="utf-8", errors="replace",
                                cwd=str(Path.home()), start_new_session=True)
    except OSError:
        raise LiveError("找不到 agy 命令") from None
    try:
        stdout, _ = proc.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        _kill_group(proc)
        raise LiveError("agy /usage 超时") from None
    if proc.returncode != 0:
        raise LiveError("agy /usage 退出码 %s" % proc.returncode)
    out = parse_agy_usage(stdout)
    out["source"] = SOURCE["agy"]
    account = agy_current_id()
    if account and account == before:
        out["account_id"] = account
    return out


FETCHERS = {"codex-sub": fetch_codex_sub, "codex-relay": fetch_relay, "dsh": fetch_dsh, "agy": fetch_agy}


# ---------------------------------------------------------------- agy 各账号（Antigravity Tools 的清单；不碰登录信息）

def _tools_home() -> Path:
    custom = os.environ.get("XAY_AGY_TOOLS_HOME")
    return Path(custom).expanduser() if custom else Path.home() / ".antigravity_tools"


def agy_current_id():
    """agy 现在实际用的是哪个号：看 agy 的登录（钥匙串）是谁（xay/agy_login.py）。
    读不到 agy 的登录才退回 Tools 自己记的当前号——2026-10-31 实测它会和真实情况对不上（Tools 说 账号 A，agy 实际在用 账号 B）。"""
    from . import agy_login
    real = agy_login.current_account_id()
    if real:
        return real
    index = _read_json(_tools_home() / "accounts.json", {}) or {}
    return index.get("current_agy_account_id") if isinstance(index, dict) else None


def _safe_detail(raw):
    """账号文件里只留这几样：名字、优先级、停用 / 封禁标记、额度（Gemini 那组）。其余（邮箱、登录信息……）直接丢掉。"""
    if not isinstance(raw, dict):
        return {}
    quota = raw.get("quota") if isinstance(raw.get("quota"), dict) else {}
    groups = []
    for g in quota.get("quota_groups") or []:
        if not isinstance(g, dict) or "gemini" not in str(g.get("display_name") or "").lower():
            continue
        groups.append({"buckets": [{k: b.get(k) for k in ("window", "display_name", "remaining_fraction", "reset_time")}
                                   for b in g.get("buckets") or [] if isinstance(b, dict)]})
    return {"name": raw.get("name"), "priority": raw.get("priority"),
            "blocked": bool(raw.get("disabled") or raw.get("validation_blocked") or quota.get("is_forbidden")),
            "last_updated": quota.get("last_updated"), "groups": groups}


def _tools_windows(detail):
    windows = []
    for group in detail.get("groups") or []:
        for b in group.get("buckets") or []:
            if b.get("remaining_fraction") is None:
                continue
            kind = str(b.get("window") or "")
            wkey, name = ("5h", "5 小时") if kind == "5h" else ("week", "本周") if kind == "weekly" else (kind[:16], str(b.get("display_name") or kind)[:16])
            windows.append(_window(wkey, name, float(b["remaining_fraction"]) * 100, _iso_ts(b.get("reset_time"))))
    return windows


def agy_accounts(now=None, seen=None):
    """Antigravity Tools 的账号清单 → [{id, index, name, priority, current, status, left, resets_at, checked_at, source}]。
    额度取 xay 自己用 /usage 查到的读数（seen：{账号 id: {fetched_at, windows}}）和 Tools 记录里较新的那个。
    id 只给本进程用（换号、记读数），写进账本前用 public_accounts 去掉。没有 Tools 就返回 []。"""
    now = time.time() if now is None else now
    tools = _tools_home()
    index = _read_json(tools / "accounts.json", {}) or {}
    if not isinstance(index, dict) or not isinstance(index.get("accounts"), list):
        return []
    current = agy_current_id()
    seen = seen or {}
    out = []
    for number, summary in enumerate(index["accounts"], 1):
        if not isinstance(summary, dict):
            continue
        account_id = summary.get("id")
        detail = _safe_detail(_read_json(tools / "accounts" / ("%s.json" % account_id), {}) if account_id else {})
        candidates = []
        tw = _tools_windows(detail)
        if tw:
            candidates.append({"fetched_at": float(detail.get("last_updated") or 0), "windows": tw, "source": "Antigravity Tools 记录"})
        if isinstance(seen.get(account_id), dict):
            candidates.append(dict(seen[account_id], source=SOURCE["agy"]))
        reading = max(candidates, key=lambda r: float(r.get("fetched_at") or 0)) if candidates else None
        windows = []
        for w in (reading or {}).get("windows") or []:
            w = dict(w)
            if w.get("resets_at") and float(w["resets_at"]) <= now:
                w["remaining_pct"], w["resets_at"] = 100, None  # 记录之后窗口已经重置
            if w.get("remaining_pct") is not None:
                windows.append(w)
        bind = min(windows, key=lambda w: w["remaining_pct"]) if windows else None
        left = bind["remaining_pct"] if bind else None
        blocked = bool(summary.get("disabled")) or detail.get("blocked")
        status = "blocked" if blocked else "unknown" if left is None else "exhausted" if left <= 0 else "tight" if left < TIGHT_PCT else "ok"
        name = str(summary.get("name") or detail.get("name") or "账号 %d" % number)[:40]
        prio = detail.get("priority")
        out.append({"id": account_id, "index": number, "name": name, "priority": prio if isinstance(prio, int) else 999,
                    "current": account_id == current, "status": status, "left": left,
                    "resets_at": bind.get("resets_at") if bind else None,
                    "checked_at": (reading or {}).get("fetched_at"), "source": (reading or {}).get("source")})
    return out


def public_accounts(accounts):
    """写进账本 / 给页面的样子：去掉 id，只留序号、名字、优先级、是不是当前、状态、剩多少、什么时候重置、什么时候查的。"""
    keep = ("index", "name", "priority", "current", "status", "left", "resets_at", "checked_at")
    return [{k: a.get(k) for k in keep} for a in accounts]


# ---------------------------------------------------------------- 写账本的样子

def to_ledger(line, reading):
    """查到的读数 → quota.updated 的 data（和工位写的同一种窗口格式：used_pct = 100 - 剩余）。"""
    windows = []
    for w in reading.get("windows") or []:
        if w.get("remaining_pct") is None or not w.get("key"):
            continue
        item = {"key": str(w["key"])[:16], "mins": int(w.get("mins") or WINDOW_MINS.get(w["key"], 0)),
                "used_pct": round(100.0 - float(w["remaining_pct"]), 1)}
        if w.get("resets_at"):
            item["resets_at"] = int(w["resets_at"])
        windows.append(item)
    data = {"line": line, "windows": windows, "source": str(reading.get("source") or SOURCE.get(line, ""))[:40]}
    for k in ("balance", "warning"):
        if reading.get(k):
            data[k] = str(reading[k])[:60]
    if isinstance(reading.get("balance_usd"), (int, float)):
        data["balance_usd"] = round(float(reading["balance_usd"]), 2)
    return data


def _changed(old, new, now, old_ts, keepalive_s):
    """跟上一次写进账本的比，算不算变了（变了才写，免得账本里全是一样的读数）。"""
    if old is None or now - float(old_ts or 0) >= keepalive_s:
        return True
    if old.get("error") != new.get("error") or old.get("account") != new.get("account"):
        return True
    a, b = old.get("windows") or [], new.get("windows") or []
    if [w.get("key") for w in a] != [w.get("key") for w in b]:
        return True
    for x, y in zip(a, b):
        if abs(float(x.get("used_pct") or 0) - float(y.get("used_pct") or 0)) >= STEP_PCT:
            return True
        rx, ry = x.get("resets_at"), y.get("resets_at")
        if (rx is None) != (ry is None) or (rx and ry and abs(rx - ry) >= RESET_STEP_S):
            return True
    if abs(float(old.get("balance_usd") or 0) - float(new.get("balance_usd") or 0)) >= 1:
        return True

    def acc(d):
        return [(x.get("name"), x.get("current"), x.get("status"), int((x.get("left") or 0) // 5)) for x in d.get("accounts") or []]
    return acc(old) != acc(new)


# ---------------------------------------------------------------- 管家：定时刷新

def live_enabled(home=None):
    flag = os.environ.get("XAY2_QUOTA_LIVE")
    if flag in ("0", "1"):
        return flag == "1"
    # 默认只有正式的 ~/.xay/v2 才去查真实账号；测试用的临时目录不联网、不起进程
    try:
        return _v2_home(home).resolve() == (Path.home() / ".xay" / "v2").resolve()
    except OSError:
        return False


def load_cfg(home=None):
    try:
        from .office import reactions
        cfg = (reactions.load(home) or {}).get("quota") or {}
    except Exception:  # noqa: BLE001
        cfg = {}
    out = dict(DEFAULTS)
    for k, v in cfg.items():
        if k in out and isinstance(v, (int, float)) and not isinstance(v, bool) and v >= 0:
            out[k] = v
    return out


class Refresher:
    """查四条线路、写账本。start() 在后台线程里查（每条线路一个，各自超时），collect() 把查完的写进账本；
    两步都很快，管家的额度线程每 5 秒调一次。测试直接调 run_once（同步）并传假的 fetchers。"""

    def __init__(self, conn, *, home=None, fetchers=None):
        self.conn = conn
        self.home = _v2_home(home)
        self.fetchers = dict(fetchers or FETCHERS)
        self.last_try = {}             # line -> 上次开始查的时间
        self.last = {}                 # line -> (上次写进账本的 data, 时间)
        self.busy = {}                 # line -> 开始查的时间（还没查完）
        self.results = queue.Queue()
        self.agy_seen_id = None        # 上次查 agy 时的当前账号：换了号马上重查
        self._load_last()

    def _load_last(self):
        try:
            rows = self.conn.execute("SELECT ts, data FROM events WHERE type='quota.updated' AND by='office' "
                                     "ORDER BY seq DESC LIMIT 200").fetchall()
        except Exception:  # noqa: BLE001
            return
        for r in rows:
            try:
                d = json.loads(r[1])
            except ValueError:
                continue
            if isinstance(d, dict) and d.get("line") in LINES and d["line"] not in self.last:
                self.last[d["line"]] = (d, float(r[0]))

    # ---- 什么时候查

    def due(self, now, cfg):
        out = []
        if float(cfg["refresh_s"]) <= 0:
            return out                              # quota.refresh_s = 0：不查
        current = agy_current_id() if "agy" in self.fetchers else None
        for line in self.fetchers:
            if line in self.busy:
                if now - self.busy[line] > float(cfg["timeout_s"]) * 2 + 30:
                    self.busy.pop(line, None)      # 查询线程卡死了：不等它，下次重新查
                else:
                    continue
            every = float(cfg["agy_refresh_s"] if line == "agy" else cfg["refresh_s"]) or float(cfg["refresh_s"])
            if every <= 0:
                continue
            if line == "agy" and current and self.agy_seen_id and current != self.agy_seen_id:
                out.append(line)                    # 刚换了 Gemini 账号：旧读数作废，马上查新账号
                continue
            if now - self.last_try.get(line, 0) >= every:
                out.append(line)
        return out

    def start(self, lines, now, timeout):
        for line in lines:
            if line in self.busy or line not in self.fetchers:
                continue
            self.busy[line] = now
            self.last_try[line] = now
            if line == "agy":
                self.agy_seen_id = agy_current_id()
            threading.Thread(target=self._work, args=(line, now, timeout), name="xay-quota-" + line, daemon=True).start()

    def _work(self, line, started, timeout):
        reading, error = None, None
        try:
            reading = self.fetchers[line](timeout=timeout, now=started)
        except LiveError as exc:
            error = str(exc)[:80]
        except Exception:  # noqa: BLE001  查询出意外不能拖垮管家；不记异常原文（里面可能带配置）
            error = "查询时出了意外错误"
        self.results.put((line, started, reading, error))

    # ---- 写账本

    def _seen_path(self):
        return self.home / "quota" / ACCOUNTS_FILE

    def _agy_seen(self, reading=None):
        """xay 自己查到的各 Gemini 账号读数（{账号 id: {fetched_at, windows}}，只有百分比和重置时间）。"""
        path = self._seen_path()
        seen = _read_json(path, {}) or {}
        if reading and reading.get("account_id"):
            seen[reading["account_id"]] = {"fetched_at": reading["fetched_at"], "windows": reading.get("windows") or []}
            try:
                path.parent.mkdir(parents=True, exist_ok=True)
                tmp = path.with_name(path.name + ".%d.tmp" % os.getpid())
                tmp.write_text(json.dumps(seen, ensure_ascii=False), encoding="utf-8")
                os.replace(tmp, path)
            except OSError:
                pass
        return seen

    def _data(self, line, started, reading, error):
        if line == "agy" and reading is not None and not reading.get("account_id") and agy_current_id():
            reading, error = None, "查的时候刚好换了账号，等下一次"   # 读数说不清是哪个账号的：不用
        if reading is not None:
            reading = dict(reading, fetched_at=started)
            data = to_ledger(line, reading)
        else:
            data = {"line": line, "windows": [], "source": SOURCE.get(line, ""), "error": error or "没查到"}
        if line == "agy":
            accounts = agy_accounts(started, self._agy_seen(reading))
            cur = next((a for a in accounts if a["current"]), None)
            if cur:
                data["account"] = cur["name"]
            if accounts:
                data["accounts"] = public_accounts(accounts)
        return data

    def collect(self, now=None, keepalive_s=None):
        """把查完的写进账本（变了才写）。返回写了几条。"""
        keepalive_s = DEFAULTS["keepalive_s"] if keepalive_s is None else keepalive_s
        n = 0
        while True:
            try:
                line, started, reading, error = self.results.get_nowait()
            except queue.Empty:
                return n
            self.busy.pop(line, None)
            try:
                data = self._data(line, started, reading, error)
            except Exception:  # noqa: BLE001
                LOG.exception("整理 %s 的额度读数出错", line)
                continue
            if error:
                LOG.info("%s 额度没查到：%s", SHORT.get(line, line), error)
            t = time.time() if now is None else now
            old, old_ts = self.last.get(line, (None, 0))
            if not _changed(old, data, t, old_ts, keepalive_s):
                continue
            try:
                from .core import events as core_events
                core_events.append(self.conn, "quota.updated", "office", data, ts=t)
            except Exception as exc:  # noqa: BLE001
                LOG.warning("额度读数没写进账本：%s", exc)
                continue
            self.last[line] = (data, t)
            n += 1

    def run_once(self, now=None, lines=None, timeout=None, keepalive_s=None):
        """同步查一遍（测试 / 调试用）：查指定线路（默认全部），等它们查完，写账本。返回写了几条。"""
        now = time.time() if now is None else now
        cfg = load_cfg(self.home)
        lines = list(lines or self.fetchers)
        timeout = float(cfg["timeout_s"] if timeout is None else timeout)
        self.start(lines, now, timeout)
        deadline = time.time() + timeout + 5
        n = 0
        while any(line in self.busy for line in lines) and time.time() < deadline:
            n += self.collect(now, keepalive_s)          # 查完的那条会从 busy 里拿掉
            time.sleep(0.02)
        return n + self.collect(now, keepalive_s)


class QuotaThread(threading.Thread):
    """管家里的额度线程：每 5 秒看一眼哪条线路该查了，查询本身在各自的小线程里跑，不挡排程。"""

    def __init__(self, db_path=None, home=None, fetchers=None, tick_s=TICK_S):
        super().__init__(name="xay-quota", daemon=True)
        self.db_path, self.home, self.fetchers, self.tick_s = db_path, home, fetchers, tick_s
        self.stop_event = threading.Event()

    def run(self):
        from .core import db as core_db
        conn, refresher = None, None
        while not self.stop_event.is_set():
            try:
                if live_enabled(self.home):
                    cfg = load_cfg(self.home)
                    if conn is None:
                        conn = core_db.open_db(self.db_path)
                        refresher = Refresher(conn, home=self.home, fetchers=self.fetchers)
                    now = time.time()
                    refresher.start(refresher.due(now, cfg), now, float(cfg["timeout_s"]))
                    refresher.collect(None, float(cfg["keepalive_s"]))
            except Exception as exc:  # noqa: BLE001
                LOG.warning("额度刷新这一拍出错：%s", exc)
                if conn is not None:
                    try:
                        conn.close()
                    except Exception:  # noqa: BLE001
                        pass
                    conn, refresher = None, None
            self.stop_event.wait(self.tick_s)
        if conn is not None:
            try:
                conn.close()
            except Exception:  # noqa: BLE001
                pass

    def stop(self, timeout=3.0):
        self.stop_event.set()
        if self.is_alive() and self is not threading.current_thread():
            self.join(timeout)


# ---------------------------------------------------------------- 给管家 / 工位做决定用（读快照里的 quota）

def fmt_reset(ts, now):
    when, today = datetime.fromtimestamp(ts), datetime.fromtimestamp(now)
    if when.date() == today.date():
        return when.strftime("%H:%M")
    if when.date() == (today + timedelta(days=1)).date():
        return "明天 " + when.strftime("%H:%M")
    return "%d/%d %s" % (when.month, when.day, when.strftime("%H:%M"))


def lines_now(conn, now=None):
    """快照同款的四条线路（core.snapshot.quota_lines）；出错回 []（读不到就当不知道，不拦活）。"""
    try:
        from .core import snapshot as core_snapshot
        return core_snapshot.quota_lines(conn, now)
    except Exception:  # noqa: BLE001
        return []


def entry(quota, line):
    return next((q for q in quota or [] if q.get("line") == line), None)


def _tightest(q):
    wins = [w for w in q.get("windows") or [] if w.get("remaining_pct") is not None]
    return min(wins, key=lambda w: w["remaining_pct"]) if wins else None


def gone_text(q, now, also=False):
    """一条用完的线路怎么说："中转站今天也用完了"（知道什么时候重置才加"（明天 00:00 重置）"）。不是用完的返回 None。"""
    if not q or q.get("status") != "exhausted":
        return None
    w = _tightest(q) or {}
    who = q.get("short") or SHORT.get(q.get("line"), q.get("line"))
    if q.get("line") == "agy" and q.get("account"):
        who = "%s（%s）" % (who, q["account"])
    text = "%s%s%s用完了" % (who, w.get("name") or "", "也" if also else "")
    if w.get("resets_at"):
        text += "（%s 重置）" % fmt_reset(w["resets_at"], now)
    return text


def line_gone_text(conn, line, now=None, also=True):
    now = time.time() if now is None else now
    return gone_text(entry(lines_now(conn, now), line), now, also)


def used_up_accounts(q):
    """Gemini 里额度已经用完的账号名（换号时跳过它们）。"""
    return [a.get("name") for a in (q or {}).get("accounts") or [] if a.get("status") == "exhausted" and a.get("name")]


def agy_others_gone(q):
    """Gemini 当前账号以外，是不是已经没有能换的账号了（清单里别的号都用完了 / 停用了）。清单为空 = 不知道。"""
    others = [a for a in (q or {}).get("accounts") or [] if not a.get("current")]
    return bool(others) and all(a.get("status") in ("exhausted", "blocked") for a in others)


def agent_out(quota, agent, now=None, line=None, confidential=False):
    """这种 AI 现在是不是所有线路（Gemini：所有账号）都用完了 → 大白话原因；还有能用的 / 不知道 → None。
    只认新鲜的读数（快照里 status=exhausted 的；太旧的读数 status 是 unknown，不拦活）。"""
    now = time.time() if now is None else now
    if agent == "codex":
        lines = ["codex-sub"] if confidential else ["codex-sub", "codex-relay"]
        if line in lines:
            lines = [line] + [x for x in lines if x != line]
        texts = [gone_text(entry(quota, x), now) for x in lines]
        if all(texts):
            return "Codex %s都用完了：%s" % ("两条线路" if len(texts) > 1 else "Plus", "；".join(texts))
        return None
    if agent == "dsh":
        text = gone_text(entry(quota, "dsh"), now)
        return "DeepSeek 额度用完了：%s" % text if text else None
    if agent == "agy":
        q = entry(quota, "agy")
        text = gone_text(q, now)
        if text and (agy_others_gone(q) or not (q or {}).get("accounts")):
            return "Gemini 额度用完了：%s%s" % (text, "，别的账号也都用完了" if (q or {}).get("accounts") else "")
        return None
    return None

