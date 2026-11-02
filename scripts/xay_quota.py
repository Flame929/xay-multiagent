#!/usr/bin/env python3
"""xay 额度读取与线路推荐。

两种来源：
1. 实时查询（真实上限）：每条线路用它自己的官方查询方式——
   - DeepSeek（dsh，OpenCode Go）：GET https://opencode.ai/zen/go/v1/usage（和 dsh 插件 dsh-opencode-usage 同一个接口）
   - Antigravity（agy）：`agy -p "/usage"`（agy 官方命令，只读当前登录账号）
   - Codex · Plus：`codex app-server` 的 account/rateLimits/read
   - Codex · 中转：中转站的 GET <base_url>/usage
   需要的 key 由程序运行时从已有配置里读出并直接使用，不写进缓存、日志或输出。
   结果缓存在 XAY_HOME/quota-live.json，按线路定时刷新。
2. 本机估算：XAY_HOME 下运行记录里的 token 用量和报错，实时查询失败时退回它。
"""

import json
import os
import re
import shutil
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path


DEFAULT_LINES = {
    "agy": {"agent": "agy", "label": "Antigravity", "priority": 1},
    "codex-sub": {"agent": "codex", "label": "Codex · Plus", "priority": 2},
    "codex-relay": {"agent": "codex", "label": "Codex · 中转", "priority": 3, "budget_share": 0.5},
    "dsh": {"agent": "dsh", "label": "DeepSeek", "priority": 4, "budget_share": 0.5},
}
ERROR_RE = re.compile(r"usage\s*limit|usageLimitExceeded|DAILY_LIMIT_EXCEEDED|quota|RESOURCE_EXHAUSTED|额度", re.I)
RESET_RE = re.compile(r"(?:reset(?:s)?(?:\s+at)?|重置(?:时间)?)[^0-9]{0,24}(\d{10})(?!\d)", re.I)
_USAGE_CACHE = {}
# 实时查询：多久重新查一次（秒）。agy 和 codex 要起进程，间隔长一些。
LIVE_TTL = {"dsh": 120, "codex-relay": 120, "codex-sub": 180, "agy": 300}
LIVE_MAX_AGE = 1800  # 超过半小时没查成功，就不再把旧读数当真
LIVE_FILE = "quota-live.json"
_LIVE_LOCK = threading.Lock()
_LIVE_BUSY = set()


class LiveError(Exception):
    """实时查询失败；消息是给用户看的大白话，绝不包含 key。"""


def xay_home(home=None):
    return Path(home or os.environ.get("XAY_HOME", str(Path.home() / ".xay"))).expanduser()


def _read_json(path, default=None):
    try:
        with Path(path).open(encoding="utf-8") as f:
            value = json.load(f)
        return value
    except (OSError, ValueError):
        return default


def _rows(path):
    try:
        lines = Path(path).read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return []
    out = []
    for line in lines:
        try:
            value = json.loads(line)
        except ValueError:
            continue
        if isinstance(value, dict):
            out.append(value)
    return out


def load_config(home=None):
    config = _read_json(xay_home(home) / "quota.json", {})
    lines = {name: dict(value) for name, value in DEFAULT_LINES.items()}
    if isinstance(config, dict) and isinstance(config.get("lines"), dict):
        for name, value in config["lines"].items():
            if name in lines and isinstance(value, dict):
                lines[name].update(value)
    return lines


def _number(value):
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return 0


def _event_ts(event, fallback):
    for key in ("ts", "time", "timestamp", "created"):
        value = event.get(key)
        if isinstance(value, (int, float)):
            return float(value)
        if isinstance(value, str):
            try:
                return float(value)
            except ValueError:
                try:
                    return datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()
                except ValueError:
                    pass
    return fallback


def _timestamp(value, fallback):
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str):
        try:
            return float(value)
        except ValueError:
            try:
                return datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()
            except ValueError:
                pass
    return float(fallback)


def _reset_from_text(text):
    match = RESET_RE.search(text)
    if match:
        return int(match.group(1))
    for value in re.findall(r"\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}:\d{2}(?:Z|[+-]\d{2}:?\d{2})?", text):
        try:
            return int(datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp())
        except ValueError:
            pass
    return None


def _line_for_run(row, start, meta):
    agent = str(row.get("agent") or start.get("agent") or meta.get("agent") or "")
    if agent == "codex":
        source = str(meta.get("codex_source") or meta.get("source") or start.get("source") or row.get("source") or "sub").lower()
        return "codex-relay" if "relay" in source else "codex-sub"
    return agent if agent in DEFAULT_LINES else None


def _usage_for_run(run_dir, row, now):
    start = _read_json(Path(run_dir) / "start.json", {}) or {}
    meta = _read_json(Path(run_dir) / "meta.json", {}) or {}
    line = _line_for_run(row, start, meta)
    if not line:
        return []
    path = Path(run_dir) / "events.norm.jsonl"
    try:
        stat = path.stat()
        signature = (stat.st_mtime_ns, stat.st_size)
    except OSError:
        return []
    cached = _USAGE_CACHE.get(str(Path(run_dir)))
    if cached and cached[0] == signature:
        return [dict(x, line=line) for x in cached[1]]
    events = _rows(path)
    result = []
    seen = set()
    for idx, event in enumerate(events):
        if event.get("type") != "usage":
            continue
        data = event.get("data") or {}
        ts = _event_ts(event, _timestamp(meta.get("ended") or start.get("started"), now))
        # step 和 turn 常常同时记录同一笔累计量；优先保留 turn，完全相同的记录去重。
        key = (round(ts, 3), _number(data.get("in")), _number(data.get("out")), _number(data.get("cached")))
        if key in seen:
            continue
        seen.add(key)
        result.append({"line": line, "ts": ts, "in": _number(data.get("in")), "out": _number(data.get("out")), "cached": _number(data.get("cached")), "idx": idx})
    _USAGE_CACHE[str(Path(run_dir))] = (signature, [dict(x) for x in result])
    return result


def _raw_rate_limits(run_dir):
    latest = None
    for idx, event in enumerate(_rows(Path(run_dir) / "events.raw")):
        method = event.get("method") or event.get("type")
        if method != "account/rateLimits/updated":
            continue
        params = event.get("params") or event.get("data") or event
        limits = params.get("rateLimits") if isinstance(params, dict) else None
        if not isinstance(limits, dict):
            limits = params
        primary = limits.get("primary") if isinstance(limits, dict) else None
        if isinstance(primary, dict):
            emitted = event.get("emittedAtMs")
            limits = dict(limits, _ts=(float(emitted) / 1000.0) if emitted else 0.0)
            latest = (idx, limits)
    return latest[1] if latest else None


def _errors_and_success(home, rows, now):
    errors = {name: [] for name in DEFAULT_LINES}
    successes = {name: [] for name in DEFAULT_LINES}
    for row in rows:
        rd = row.get("run_dir")
        if not rd:
            continue
        start = _read_json(Path(rd) / "start.json", {}) or {}
        meta = _read_json(Path(rd) / "meta.json", {}) or {}
        line = _line_for_run(row, start, meta)
        if not line:
            continue
        status = str(meta.get("status") or row.get("status") or "").lower()
        ts = _timestamp(meta.get("ended") or row.get("time") or start.get("started"), now)
        if status in ("done", "completed"):
            # 成功的运行只算"恢复"信号：它的工具输出里出现"额度/quota"等字样不代表额度用完
            successes[line].append(_timestamp(start.get("started"), now) + float(meta.get("duration_s") or 0) if start.get("started") else ts)
            continue
        text = "\n".join(str(x) for x in (meta.get("fail_reason"), meta.get("error")))
        if status in ("failed", "failure", "error", "timeout"):
            for path in (Path(rd) / "stderr.log", Path(rd) / "agent.log"):
                try:
                    text += "\n" + path.read_text(encoding="utf-8", errors="replace")
                except OSError:
                    pass
        for event in _rows(Path(rd) / "events.norm.jsonl") + _rows(Path(rd) / "events.raw"):
            kind = str(event.get("type") or event.get("method") or "").lower()
            if any(word in kind for word in ("error", "failed", "warning")):
                text += "\n" + json.dumps(event, ensure_ascii=False)
        if ERROR_RE.search(text):
            errors[line].append((ts, text, _reset_from_text(text)))
    return errors, successes


# ── 实时查询：解析（纯函数，测试直接喂样例） ─────────────────────────────

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
    """OpenCode Go 用量：percent 是已用百分比。"""
    usage = (data or {}).get("usage")
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
    """中转站 /v1/usage：订阅按美元计，有日/周/月上限（null 表示不限）。"""
    if not isinstance(data, dict):
        raise LiveError("中转站用量接口返回的格式看不懂")
    if data.get("isValid") is False:
        raise LiveError("中转站说这个 key 已失效")
    sub = data.get("subscription") if isinstance(data.get("subscription"), dict) else {}
    # 时区跟着中转站走（它的时间都带 +08:00 这样的偏移），算"每天"从哪一刻重置
    tz = None
    for key in ("weekly_window_start", "expires_at"):
        try:
            tz = datetime.fromisoformat(str(sub.get(key)).replace("Z", "+00:00")).tzinfo
            if tz:
                break
        except ValueError:
            pass
    local_now = datetime.fromtimestamp(now, tz) if tz else datetime.fromtimestamp(now)
    windows = []
    for prefix, wkey, name in (("daily", "day", "今天"), ("weekly", "week", "本周"), ("monthly", "month", "本月")):
        limit, used = sub.get(f"{prefix}_limit_usd"), sub.get(f"{prefix}_usage_usd")
        if not limit:
            continue
        reset = None
        if prefix == "daily":
            reset = (local_now.replace(hour=0, minute=0, second=0, microsecond=0) + timedelta(days=1)).timestamp()
        elif prefix == "weekly" and _iso_ts(sub.get("weekly_window_start")):
            reset = _iso_ts(sub["weekly_window_start"]) + 7 * 86400
        windows.append(_window(wkey, name, (1 - float(used or 0) / float(limit)) * 100, reset))
    out = {"windows": windows}
    unit = "$" if str(data.get("unit") or "USD").upper() == "USD" else ""
    if isinstance(data.get("remaining"), (int, float)):
        out["balance"] = f"还能用 {unit}{data['remaining']:.0f}" if unit else f"还剩 {data['remaining']:.0f}"
    expires = _iso_ts(sub.get("expires_at"))
    if expires and expires - now < 3 * 86400:
        out["warning"] = "中转订阅 " + _fmt_reset(expires, now) + " 到期"
    if not windows and "balance" not in out:
        raise LiveError("中转站没有返回额度上限")
    return out


def parse_codex_rate_limits(result):
    """codex app-server 的 rateLimits：usedPercent 是已用百分比，resetsAt 是秒级时间戳。"""
    limits = (result or {}).get("rateLimits") if isinstance(result, dict) and "rateLimits" in result else result
    if not isinstance(limits, dict):
        raise LiveError("Codex 没有返回额度信息")
    windows = []
    for key in ("primary", "secondary"):
        item = limits.get(key)
        if not isinstance(item, dict) or item.get("usedPercent") is None:
            continue
        mins = int(item.get("windowDurationMins") or (300 if key == "primary" else 10080))
        wkey, name = ("5h", "5 小时") if mins == 300 else ("week", "本周") if mins == 10080 else (f"{mins}m", f"{mins // 60} 小时")
        windows.append(_window(wkey, name, 100 - float(item["usedPercent"]), item.get("resetsAt")))
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
        wkey, name = next((v for k, v in AGY_WINDOW_NAMES.items() if k in label), (label, parts[1]))
        windows.append(_window(wkey, name, float(match.group(1)), _iso_ts(parts[3]) if len(parts) > 3 else None))
    if not windows:
        raise LiveError("agy /usage 的输出里没有 Gemini 额度")
    return {"windows": windows}


# ── 实时查询：取数（会联网或起进程；key 只在这里用，不外传） ─────────────────

def _http_json(url, key, label, timeout=15):
    req = urllib.request.Request(url, headers={"Authorization": "Bearer " + key, "Accept": "application/json", "User-Agent": "curl/7.68.0"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode("utf-8", "replace"))
    except urllib.error.HTTPError as exc:
        raise LiveError(f"{label}返回 HTTP {exc.code}" + ("（key 无效或无权限）" if exc.code in (401, 403) else "")) from None
    except (urllib.error.URLError, OSError) as exc:
        raise LiveError(f"连不上{label}") from None
    except ValueError:
        raise LiveError(f"{label}返回的不是 JSON") from None


def _dsh_key():
    """和 dsh 一致：先看 headless 配置指定的环境变量名，再按 dsh 插件的顺序找。"""
    names = []
    try:
        match = re.search(r"apiKeyEnv:\s*['\"]?([A-Z0-9_]+)", (Path.home() / ".dsh/profiles/headless/cordis.patch.yml").read_text(encoding="utf-8"))
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


def fetch_dsh():
    key = _dsh_key()
    if not key:
        raise LiveError("没找到 dsh 的 OpenCode Go key")
    out = parse_opencode_usage(_http_json("https://opencode.ai/zen/go/v1/usage", key, "OpenCode 用量接口"))
    out["source"] = "OpenCode 官方用量接口"
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


def fetch_relay(now=None):
    provider = _codex_provider("custom")
    key = provider.get("experimental_bearer_token") or (os.environ.get(provider["env_key"]) if provider.get("env_key") else None)
    if not key:
        raise LiveError("Codex 配置里没有中转站 key")
    out = parse_relay_usage(_http_json(provider["base_url"].rstrip("/") + "/usage", key, "中转站用量接口"), now or time.time())
    out["source"] = "中转站用量接口"
    return out


def _codex_args():
    try:
        from xay_drivers.base import config_args
    except ImportError:
        try:
            from scripts.xay_drivers.base import config_args
        except ImportError:
            config_args = None
    # 用和派活一样的覆盖：走订阅（openai）、关掉所有 MCP，避免查个额度还启动一堆服务
    if config_args:
        return config_args({"source": "sub", "mode": "readonly"}, "standard")
    return ["-c", 'model_provider="openai"']


def fetch_codex_sub(timeout=30):
    command = os.environ.get("XAY_APP_SERVER_BIN") or os.environ.get("XAY_CODEX_BIN") or shutil.which("codex") or "codex"
    try:
        proc = subprocess.Popen([command, "app-server", *_codex_args()], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                stderr=subprocess.DEVNULL, text=True, cwd=str(Path.home()))
    except OSError:
        raise LiveError("找不到 codex 命令") from None
    result = {}

    def talk():
        for msg in ({"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {"clientInfo": {"name": "xay-quota", "version": "1"}}},
                    {"jsonrpc": "2.0", "method": "initialized", "params": {}},
                    {"jsonrpc": "2.0", "id": 2, "method": "account/rateLimits/read", "params": {}}):
            proc.stdin.write(json.dumps(msg) + "\n")
        proc.stdin.flush()
        for line in proc.stdout:
            try:
                msg = json.loads(line)
            except ValueError:
                continue
            if msg.get("id") == 2:
                result.update(msg)
                return

    worker = threading.Thread(target=talk, daemon=True)
    worker.start()
    worker.join(timeout)
    proc.kill()
    proc.wait()
    if not result:
        raise LiveError("Codex 额度查询超时")
    if result.get("error"):
        raise LiveError("Codex 额度查询出错（可能没登录 ChatGPT 账号）")
    out = parse_codex_rate_limits(result.get("result"))
    out["source"] = "Codex 官方额度查询"
    return out


def fetch_agy(timeout=60):
    command = os.environ.get("XAY_AGY_BIN") or shutil.which("agy") or "agy"
    before = _agy_current_id()
    try:
        proc = subprocess.run([command, "-p", "/usage"], stdin=subprocess.DEVNULL, capture_output=True, text=True,
                              timeout=timeout, cwd=str(Path.home()))
    except subprocess.TimeoutExpired:
        raise LiveError("agy /usage 超时") from None
    except OSError:
        raise LiveError("找不到 agy 命令") from None
    if proc.returncode != 0:
        raise LiveError(f"agy /usage 退出码 {proc.returncode}")
    out = parse_agy_usage(proc.stdout)
    out["source"] = "agy 官方 /usage"
    account = _agy_current_id()
    if account and account == before:
        out["account_id"] = account  # 查询前后是同一个账号，才把读数记到它名下
    return out


LIVE_FETCHERS = {"agy": fetch_agy, "codex-sub": fetch_codex_sub, "codex-relay": fetch_relay, "dsh": fetch_dsh}


def _write_live(path, cache):
    tmp = Path(str(path) + f".{os.getpid()}.tmp")
    try:
        tmp.write_text(json.dumps(cache, ensure_ascii=False), encoding="utf-8")
        os.replace(tmp, path)
    except OSError:
        pass


def refresh_live(home=None, names=None, fetchers=None, timeout=70):
    """并行查询指定线路，把结果并进缓存文件。失败时保留上一次成功的读数（last）。"""
    fetchers = fetchers or LIVE_FETCHERS
    with _LIVE_LOCK:
        names = [n for n in (names or fetchers) if n in fetchers and n not in _LIVE_BUSY]
        _LIVE_BUSY.update(names)
    if not names:
        return
    results = {}

    def run(name):
        started = time.time()
        try:
            results[name] = dict(fetchers[name](), ok=True, fetched_at=started)
        except LiveError as exc:
            results[name] = {"ok": False, "error": str(exc), "fetched_at": started}
        except Exception:  # 查询出意外不能拖垮看板
            results[name] = {"ok": False, "error": "查询时出了意外错误", "fetched_at": started}

    try:
        threads = [threading.Thread(target=run, args=(n,), daemon=True) for n in names]
        for t in threads:
            t.start()
        deadline = time.time() + timeout
        for t in threads:
            t.join(max(0.1, deadline - time.time()))
        path = xay_home(home) / LIVE_FILE
        with _LIVE_LOCK:
            cache = _read_json(path, {}) or {}
            for name, reading in list(results.items()):
                if not reading["ok"]:
                    old = cache.get(name) or {}
                    last = old if old.get("ok") else old.get("last")
                    if last and name == "agy" and last.get("account_id") != _agy_current_id():
                        last = None  # 上一次读数是别的账号的
                    if last:
                        reading["last"] = last
                elif name == "agy" and reading.get("account_id"):
                    # 记下每个 agy 账号自己的最新读数，换账号调度时用
                    accounts = cache.setdefault("agy_accounts", {})
                    accounts[reading["account_id"]] = {k: reading[k] for k in ("ok", "fetched_at", "windows", "source")}
                cache[name] = reading
            _write_live(path, cache)
    finally:
        with _LIVE_LOCK:
            _LIVE_BUSY.difference_update(names)


def live_readings(home=None, wait=False, now=None):
    """读缓存；过期的线路重新查。wait=False 时在后台查（看板用），本次先用旧读数。"""
    path = xay_home(home) / LIVE_FILE
    now = time.time() if now is None else now
    cache = _read_json(path, {}) or {}
    stale = [n for n in LIVE_FETCHERS if now - float((cache.get(n) or {}).get("fetched_at") or 0) >= LIVE_TTL.get(n, 120)]
    agy = cache.get("agy") or {}
    current = _agy_current_id()
    if "agy" not in stale and current and (agy.get("account_id") or (agy.get("last") or {}).get("account_id")) not in (None, current):
        stale.append("agy")  # agy 已经换了账号，旧读数作废
        cache["agy"] = {"ok": False, "fetched_at": 0, "error": "刚换了账号，正在重新查询"}
    if stale:
        if wait:
            refresh_live(home, stale)
            cache = _read_json(path, {}) or {}
        else:
            threading.Thread(target=refresh_live, args=(home, stale), daemon=True).start()
    return cache


def _live_enabled(home):
    flag = os.environ.get("XAY_QUOTA_LIVE")
    if flag in ("0", "1"):
        return flag == "1"
    # 默认只有正式的 ~/.xay 才去查真实账号；测试用的临时目录不联网、不起进程
    try:
        return xay_home(home).resolve() == (Path.home() / ".xay").resolve()
    except OSError:
        return False


def _usable_reading(entry, now):
    """取能用的读数：本次成功的，或失败时上一次成功且不太旧的。返回 (读数, 失败原因)。"""
    if not isinstance(entry, dict):
        return None, None
    reading = entry if entry.get("ok") else entry.get("last")
    error = None if entry.get("ok") else entry.get("error")
    if not reading or now - float(reading.get("fetched_at") or 0) > LIVE_MAX_AGE:
        return None, error
    windows = []
    for w in reading.get("windows") or []:
        w = dict(w)
        if w.get("resets_at") and float(w["resets_at"]) <= now:
            w["remaining_pct"], w["resets_at"] = 100, None  # 读数之后窗口已经重置
        windows.append(w)
    return dict(reading, windows=windows), error


def _fmt_reset(ts, now):
    when, today = datetime.fromtimestamp(ts), datetime.fromtimestamp(now)
    if when.date() == today.date():
        return when.strftime("%H:%M")
    if when.date() == (today + timedelta(days=1)).date():
        return "明天 " + when.strftime("%H:%M")
    return f"{when.month}/{when.day} " + when.strftime("%H:%M")


def _windows_text(windows, now):
    parts = []
    for w in windows:
        if w.get("remaining_pct") is None:
            continue
        text = f"{w['name']}剩 {w['remaining_pct']}%"
        if w.get("resets_at"):
            text += f"（{_fmt_reset(w['resets_at'], now)} 重置）"
        parts.append(text)
    return "，".join(parts)


# ── agy 多账号（通过用户自己的 Antigravity Tools 切换；xay 不碰登录信息） ──────────

AGY_MIN_PCT = 20  # 账号剩余低于这个数就不主动切过去


def _agy_tools_home():
    custom = os.environ.get("XAY_AGY_TOOLS_HOME")
    return Path(custom).expanduser() if custom else Path.home() / ".antigravity_tools"


def _agy_tools_bin():
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


def _agy_current_id():
    index = _read_json(_agy_tools_home() / "accounts.json", {}) or {}
    return index.get("current_agy_account_id") if isinstance(index, dict) else None


def _tools_windows(quota):
    """Antigravity Tools 记录的额度：只看 Gemini 那组（xay 的 agy 只用 gemini）。"""
    windows = []
    for group in (quota or {}).get("quota_groups") or []:
        if "gemini" not in str(group.get("display_name") or "").lower():
            continue
        for bucket in group.get("buckets") or []:
            if bucket.get("remaining_fraction") is None:
                continue
            kind = str(bucket.get("window") or "")
            wkey, name = ("5h", "5 小时") if kind == "5h" else ("week", "本周") if kind == "weekly" else (kind, bucket.get("display_name") or kind)
            windows.append(_window(wkey, name, float(bucket["remaining_fraction"]) * 100, _iso_ts(bucket.get("reset_time"))))
    return windows


def agy_accounts(home=None, now=None, live=None):
    """读 Antigravity Tools 的账号清单（只取名字、优先级、状态、额度，不读登录信息）。

    额度取 xay 自己用 `agy -p /usage` 查到的读数和 Tools 记录里较新的那个；Tools 只在它开着时刷新，可能很旧。
    """
    if not os.environ.get("XAY_AGY_TOOLS_HOME") and not _live_enabled(home):
        return []
    now = time.time() if now is None else now
    tools = _agy_tools_home()
    index = _read_json(tools / "accounts.json", {}) or {}
    if not isinstance(index, dict) or not isinstance(index.get("accounts"), list):
        return []
    if live is None:
        live = _read_json(xay_home(home) / LIVE_FILE, {}) or {}
    seen = (live or {}).get("agy_accounts") or {}
    current = index.get("current_agy_account_id")
    out = []
    for number, summary in enumerate(index["accounts"], 1):
        account_id = summary.get("id")
        detail = _read_json(tools / "accounts" / f"{account_id}.json", {}) or {}
        quota = detail.get("quota") if isinstance(detail.get("quota"), dict) else {}
        candidates = []
        if _tools_windows(quota):
            candidates.append({"ok": True, "fetched_at": float(quota.get("last_updated") or 0), "windows": _tools_windows(quota), "source": "Antigravity Tools 记录"})
        if isinstance(seen.get(account_id), dict):
            candidates.append(seen[account_id])
        reading = max(candidates, key=lambda r: float(r.get("fetched_at") or 0)) if candidates else None
        windows = []
        for w in (reading or {}).get("windows") or []:
            w = dict(w)
            if w.get("resets_at") and float(w["resets_at"]) <= now:
                w["remaining_pct"], w["resets_at"] = 100, None  # 记录之后窗口已重置
            if w.get("remaining_pct") is not None:
                windows.append(w)
        bind = min(windows, key=lambda w: w["remaining_pct"]) if windows else None
        blocked = bool(summary.get("disabled") or detail.get("disabled") or detail.get("validation_blocked") or quota.get("is_forbidden"))
        remaining = bind["remaining_pct"] if bind else None
        status = "blocked" if blocked else "unknown" if remaining is None else "exhausted" if remaining <= 0 else "tight" if remaining < AGY_MIN_PCT else "ok"
        out.append({"index": number, "id": account_id, "name": summary.get("name") or detail.get("name") or f"账号 {number}",
                    "priority": detail.get("priority", 999), "current": account_id == current, "status": status,
                    "remaining_pct": remaining, "resets_at": bind.get("resets_at") if bind else None, "windows": windows,
                    "source": (reading or {}).get("source"), "checked_at": (reading or {}).get("fetched_at")})
    return out


def choose_agy_account(accounts, exclude=None):
    """按 Antigravity Tools 的优先级（数字小的先用）挑一个额度充足的账号；同优先级挑剩得多的。"""
    pool = [a for a in accounts if a.get("status") == "ok" and a.get("id") != exclude]
    pool.sort(key=lambda a: (a.get("priority", 999), -(a.get("remaining_pct") or 0), a.get("index", 0)))
    return pool[0] if pool else None


def _agy_busy(home=None):
    """有正在跑的 agy 执行者吗（占着 agy 并发名额且进程还活着）。"""
    for slot in (xay_home(home) / "slots").glob("agy-*"):
        try:
            pid = int((slot / "pid").read_text().strip())
            os.kill(pid, 0)
            return True
        except (OSError, ValueError):
            continue
    return False


def _log_switch(home, text):
    try:
        with (xay_home(home) / "agy-switch.log").open("a", encoding="utf-8") as f:
            f.write(datetime.now().strftime("%F %T") + " " + text + "\n")
    except OSError:
        pass


def switch_agy_account(target=None, home=None, reason="手动", accounts=None):
    """把 agy 切到指定账号（序号 / 名字 / id；不给就按优先级自动挑）。返回 (成功?, 大白话说明)。"""
    accounts = agy_accounts(home) if accounts is None else accounts
    if not accounts:
        return False, "没找到 Antigravity Tools 的账号清单"
    current = next((a for a in accounts if a["current"]), None)
    if target in (None, "", "auto"):
        chosen = choose_agy_account(accounts, exclude=current["id"] if current else None)
        if not chosen:
            return False, "没有额度充足的其他 agy 账号"
    else:
        text = str(target).strip()
        chosen = next((a for a in accounts if text in (str(a["index"]), a["id"], a["name"])), None) \
            or next((a for a in accounts if a["name"].lower().startswith(text.lower())), None)
        if not chosen:
            return False, f"没有叫「{text}」的 agy 账号（可以用 xay agy-accounts 看序号和名字）"
    if current and chosen["id"] == current["id"]:
        return True, f"agy 已经是「{chosen['name']}」"
    # 用户自己的 Antigravity Tools 负责切换；按邮箱指定（邮箱只在这里用，不输出）
    detail = _read_json(_agy_tools_home() / "accounts" / f"{chosen['id']}.json", {}) or {}
    email = detail.get("email") or next((x.get("email") for x in (_read_json(_agy_tools_home() / "accounts.json", {}) or {}).get("accounts", []) if x.get("id") == chosen["id"]), None)
    binary = _agy_tools_bin()
    if not binary:
        return False, "找不到 antigravity-tools 命令"
    try:
        proc = subprocess.run([binary, "switch", email or str(chosen["index"]), "--target", "agy"],
                              stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=90)
    except subprocess.TimeoutExpired:
        return False, "Antigravity Tools 切换超时"
    except OSError:
        return False, "找不到 antigravity-tools 命令"
    if proc.returncode != 0 or _agy_current_id() != chosen["id"]:
        return False, "Antigravity Tools 没能切过去（可以打开它看看）"
    # 换了账号，agy 的旧读数作废
    with _LIVE_LOCK:
        path = xay_home(home) / LIVE_FILE
        cache = _read_json(path, {}) or {}
        cache["agy"] = {"ok": False, "fetched_at": 0, "error": "刚换了账号，正在重新查询"}
        cache["agy_switched_at"] = time.time()  # 之前那个账号的"用完"报错不再算到新账号头上
        _write_live(path, cache)
    message = f"agy 已从「{current['name'] if current else '未知'}」切到「{chosen['name']}」" + (
        f"（剩 {chosen['remaining_pct']}%）" if chosen.get("remaining_pct") is not None else "")
    _log_switch(home, f"{message}；原因：{reason}")
    return True, message


def agy_precheck(home=None):
    """派活给 agy 之前：当前账号用完（或偏紧且没有 agy 在跑）就按优先级换一个充足的账号。返回要提示的话。"""
    data = collect(home, wait=True)
    agy = next((x for x in data if x.get("line") == "agy"), None)
    if not agy or agy.get("status") not in ("exhausted", "tight") or not agy.get("alt_account"):
        return ""
    if agy["status"] == "tight" and _agy_busy(home):
        return ""  # 只是偏紧，又有 agy 正在跑，先不打扰
    ok, message = switch_agy_account(agy["alt_account"]["id"], home, reason=f"当前账号{'额度用完' if agy['status'] == 'exhausted' else '额度偏紧'}")
    return message if ok else f"agy 当前账号额度不足，自动换账号失败：{message}"


def _status(line, remaining, errors, successes, now, tight_below=20):
    recent = max(errors, default=None, key=lambda x: x[0]) if errors else None
    recovered = bool(recent and any(ts > recent[0] for ts in successes))
    if recent and not recovered:
        reset = recent[2] if len(recent) > 2 else None
        # 报错里写了恢复时间且已过；或者 6 小时前的旧报错又没写恢复时间（例如中转站要手动重置）→ 不再当作用完
        if (reset and float(reset) <= now) or (not reset and now - recent[0] > 6 * 3600):
            recovered = True
    exhausted_signal = bool(recent and not recovered)
    if exhausted_signal or remaining is not None and remaining <= 0:
        return "exhausted", recent
    if remaining is None:
        return "unknown", recent
    return "tight" if remaining < tight_below else "ok", recent


def _eta(now, remaining, burn, reset=None):
    if reset and remaining is not None and remaining <= 0:
        return int(reset)
    if remaining is None or burn <= 0 or remaining <= 0:
        return None
    return int(now + (remaining / 100.0) * 0) if False else None


def collect(home=None, now=None, live=None, wait=False):
    """汇总四条线路的额度。

    live：None=自动（正式 ~/.xay 才实时查询，环境变量 XAY_QUOTA_LIVE=0/1 可强制）；
    False=只用本机估算；dict=直接给定实时读数（测试用，格式同 quota-live.json）。
    wait：True 时等实时查询查完再返回（命令行用）；看板不等，后台刷新。
    """
    home = xay_home(home)
    now = float(now if now is not None else time.time())
    if live is None:
        live = live_readings(home, wait=wait) if _live_enabled(home) else {}
    live = live or {}
    accounts = agy_accounts(home, now, live)
    # 换号时间即使不实时查询也要认（切换写在缓存文件里）
    switched_at = live.get("agy_switched_at") or (_read_json(home / LIVE_FILE, {}) or {}).get("agy_switched_at")
    current_agy = next((a for a in accounts if a["current"]), None)
    lines = load_config(home)
    rows = _rows(home / "registry.jsonl")
    usage = {name: [] for name in lines}
    rate = None
    errors, successes = _errors_and_success(home, rows, now)
    for row in rows:
        if row.get("kind") != "run" and not row.get("run_dir"):
            continue
        rd = row.get("run_dir")
        if not rd:
            continue
        for item in _usage_for_run(rd, row, now):
            usage[item["line"]].append(item)
        start = _read_json(Path(rd) / "start.json", {}) or {}
        if _line_for_run(row, start, _read_json(Path(rd) / "meta.json", {}) or {}) == "codex-sub":
            candidate = _raw_rate_limits(rd)
            if candidate and (rate is None or candidate.get("_ts", 0) >= rate.get("_ts", 0)):
                rate = candidate
    if rate and (
            (rate.get("primary") or {}).get("resetsAt") and float((rate.get("primary") or {})["resetsAt"]) <= now
            or any(ts > rate.get("_ts", 0) for ts in successes.get("codex-sub", []))
            and float((rate.get("primary") or {}).get("usedPercent") or 0) >= 100):
        rate = None  # 记录过期：窗口已重置，或之后又有成功运行（例如用户手动重置了额度）
    output = []
    for name, cfg in sorted(lines.items(), key=lambda p: (p[1].get("priority", 99), p[0])):
        items = usage.get(name, [])
        def sum_window(seconds):
            return sum(max(0, x["in"] - x["cached"]) + max(0, x["out"]) for x in items if now - seconds <= x["ts"] <= now)
        used_5h, used_24h = sum_window(5 * 3600), sum_window(24 * 3600)
        burn = sum_window(3600)
        share = cfg.get("budget_share")
        reading, live_error = _usable_reading(live.get(name), now)
        if name == "agy" and reading and current_agy and reading.get("account_id") not in (None, current_agy["id"]):
            reading, live_error = None, "刚换了账号，正在重新查询"
        # Codex 订阅：运行时收到的额度事件和实时查询，谁新用谁
        if name == "codex-sub" and rate and (not reading or rate.get("_ts", 0) > float(reading.get("fetched_at") or 0)):
            try:
                reading = dict(parse_codex_rate_limits(rate), source="Codex 运行时上报", fetched_at=rate.get("_ts", 0))
            except LiveError:
                pass
        windows = [w for w in (reading or {}).get("windows") or [] if w.get("remaining_pct") is not None]
        remaining, window, reset, cap = None, "5h", None, None
        tight_below = 20
        if windows:
            # 最紧的那个窗口说了算（例如 5 小时还剩 100%，但本周只剩 5%）
            bind = min(windows, key=lambda w: w["remaining_pct"])
            remaining, window, reset = bind["remaining_pct"], bind["key"], bind.get("resets_at")
            if share:
                tight_below = max(20, (1 - float(share)) * 100)  # 只让 xay 用一部分时，用过了这部分就算偏紧
        else:
            cap = cfg.get("daily_tokens") if name == "codex-relay" else cfg.get("limit_5h_tokens")
            if cap is not None:
                cap = float(cap) * (float(share) if share is not None else 1.0)
                cap_used = used_24h if name == "codex-relay" else used_5h
                remaining = int(round(max(0, min(100, (1 - cap_used / cap) * 100)))) if cap else 0
        errs = errors.get(name, [])
        if name == "agy" and switched_at:
            errs = [e for e in errs if e[0] > float(switched_at)]
        if windows:
            errs = [e for e in errs if e[0] > float(reading.get("fetched_at") or 0)]  # 实时读数之前的报错已经过时
        status, recent_error = _status(name, remaining, errs, successes.get(name, []), now, tight_below)
        if status == "exhausted" and recent_error and len(recent_error) > 2 and recent_error[2]:
            reset = recent_error[2]
        elif status == "exhausted" and recent_error and reset is None:
            if name == "codex-relay":
                local_reset = datetime.fromtimestamp(recent_error[0]).replace(hour=0, minute=0, second=0, microsecond=0)
                reset = int(local_reset.timestamp()) + 86400
            elif name != "codex-sub":
                reset = int(recent_error[0] + 5 * 3600)
        if windows:
            note = _windows_text(windows, now)
            if reading.get("balance"):
                note += "，" + reading["balance"]
            if reading.get("warning"):
                note += "；" + reading["warning"]
            if live_error:
                note += f"（最新一次查询失败：{live_error}，这是之前的读数）"
        else:
            note = f"近 5 小时 {used_5h:,} tokens，速度 {burn:,}/小时" if items else "没有本机用量记录"
            if live_error:
                note = f"实时查询失败（{live_error}），以下是本机估算：" + note
        if status == "exhausted":
            note = "额度已用完，等待恢复" + ("：" + note if windows else "")
        elif status == "tight":
            note = "额度偏紧，建议换线路" + ("：" + note if windows else "")
        alt = None
        if name == "agy" and accounts:
            alt = choose_agy_account(accounts, exclude=current_agy["id"] if current_agy else None)
            if status in ("exhausted", "tight") and alt:
                note += f"；可以换到「{alt['name']}」（剩 {alt['remaining_pct']}%），派活时会自动换"
        eta = None
        five = next((w for w in windows if w["key"] == "5h"), None)
        if five and five["remaining_pct"] < 100 and used_5h:
            cap = used_5h / ((100 - five["remaining_pct"]) / 100.0)  # 用真实已用比例反推 5 小时上限
            left = five["remaining_pct"]
        else:
            left = remaining
        if left is not None and left > 0 and burn > 0 and cap:
            eta = int(now + (left / 100.0) * float(cap) / burn * 3600)
            if eta <= now + 3600 and status == "ok":
                status = "tight"
        label = cfg.get("label", name)
        if name == "agy" and current_agy:
            label = f"{label} · {current_agy['name']}"
        output.append({"line": name, "agent": cfg.get("agent", name), "label": label, "priority": cfg.get("priority", 99), "status": status,
                       "remaining_pct": remaining, "window": window, "resets_at": reset, "used_5h": used_5h, "used_24h": used_24h,
                       "burn_per_h": burn, "eta_exhausted": eta, "note": note, "budget_share": share,
                       "source": "live" if windows else "estimate", "source_label": (reading or {}).get("source") if windows else "本机估算",
                       "windows": windows, "balance": (reading or {}).get("balance") if windows else None,
                       "fetched_at": (reading or {}).get("fetched_at") if windows else None, "live_error": live_error,
                       **({"accounts": accounts, "alt_account": alt} if name == "agy" and accounts else {})})
    return output


def choose_route(quota, prefer=None, agent=None):
    names = list(prefer or [])
    expanded = []
    for name in names:
        if name == "codex":
            expanded.extend(("codex-sub", "codex-relay"))
        else:
            expanded.append(name)
    names = expanded
    if agent == "codex":
        names = ["codex-sub", "codex-relay"]
    if not names:
        names = [x["line"] for x in sorted(quota, key=lambda x: x.get("priority", 99))]
    by_name = {x["line"]: x for x in quota}
    for name in names:
        item = by_name.get(name)
        if item and (item.get("status") not in ("exhausted", "tight") or item.get("alt_account")):
            return item
    if agent == "codex":
        for name in ("codex-sub", "codex-relay"):
            item = by_name.get(name)
            if item and item.get("status") != "exhausted":
                return item
    return None


def choose_codex_source(quota):
    """按 codex 的专用规则返回 sub、relay，或 None。"""
    by_name = {item.get("line"): item for item in quota}
    sub = by_name.get("codex-sub")
    if sub and sub.get("status") != "exhausted":
        remaining = sub.get("remaining_pct")
        if remaining is not None and remaining >= 10:
            return "sub"
    relay = by_name.get("codex-relay")
    if relay and relay.get("status") != "exhausted":
        return "relay"
    return None


def main(argv=None):
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)
    data = collect()
    print(json.dumps(data, ensure_ascii=False, indent=2 if args.json else None))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
