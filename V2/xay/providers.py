"""提供者表：每家 AI 一行，模型限制、窗口、压缩门槛、能力、空闲下线时间都写在这里。

设计依据：references/plans/arch-v10-rebuild.md §3.1 / §3.2 / §3.3、V2/docs/probes.md（本机实测）。
适配器、工位只读这张表，不自己写死数字。

约定：
- ``pct`` 一律是"百分数"（0~100），和账本里的 ``context.updated.pct`` 一致。
- 表里的压缩门槛 ``compact_at`` 是"窗口的比例"（0.60 = 60%），用 ``compaction_reason`` 比较时会自动换算。
- 硬性约束（AGENTS.md §4）在这里用代码强制：agy 只许 ``gemini-3.8-*``，dsh 只许 ``deepseek-v4.1-flash``，
  不符合就抛 ``ModelNotAllowed``。
"""

from __future__ import annotations

import functools
import json
import os
import re
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, List, Mapping, Optional, Sequence, Tuple

CODEX = "codex"
DSH = "dsh"
AGY = "agy"
AGENTS: Tuple[str, ...] = (CODEX, DSH, AGY)

# ---------------------------------------------------------------------------
# 模型限制
# ---------------------------------------------------------------------------

DSH_MODEL = "deepseek-v4.1-flash"
DSH_EFFORTS = ("off", "minimal", "low", "medium", "high", "xhigh", "max")

AGY_MODEL_PREFIX = "gemini-3.8-"
_AGY_MODEL_RE = re.compile(r"gemini-3\.8-[a-z0-9][a-z0-9.\-]*")
AGY_MODELS_BY_EFFORT: Mapping[str, str] = {
    "low": "gemini-3.8-flash-low",
    "medium": "gemini-3.8-flash-medium",
    "high": "gemini-3.8-flash-high",
}
# 旧 xay-run 的强度映射：off/minimal/low -> low；medium -> medium；其余（high/xhigh/max）-> high
_AGY_EFFORT_TIER = {"off": "low", "minimal": "low", "low": "low", "medium": "medium",
                    "high": "high", "xhigh": "high", "max": "high"}

# pi-ai（dsh 的模型层）对"没写窗口的模型"的默认窗口（dsh-llm-pi-ai 包内文档 defaultContextWindow）
PI_AI_DEFAULT_WINDOW = 262_144
# dsh-compaction-basic 的默认值（包内文档）：触发点 = floor(min(W × thresholdRatio, W − O − headroomTokens))
DSH_COMPACT_RATIO = 0.8
DSH_COMPACT_HEADROOM = 65_536

# 卡片里表示"用默认"的几种写法
_DEFAULT_WORDS = ("", "默认", "default")

# 会被拼进 `-c key="value"` 的字符串必须是干净的，防止注入额外配置
_SAFE_TOKEN = re.compile(r"[A-Za-z0-9_.:/@+\-]+")

# Codex 额度线路 -> app-server 的 model_provider（见 arch-v10 §3.1、旧 xay_quota.DEFAULT_LINES）
CODEX_LINES: Mapping[str, str] = {"codex-sub": "openai", "codex-relay": "custom"}


def is_safe_token(text) -> bool:
    """能不能安全地放进命令行 / TOML / YAML 的值里（不含引号、空格、换行等）。"""
    return bool(_SAFE_TOKEN.fullmatch(str(text or "")))


class ModelNotAllowed(ValueError):
    """卡片要求了这家 AI 不允许使用的模型。"""


class UnknownAgent(ValueError):
    pass


class UnknownLine(ValueError):
    pass


# ---------------------------------------------------------------------------
# 提供者表
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class Provider:
    agent: str
    binary: str                              # 默认可执行文件名
    env_bin: Tuple[str, ...]                 # 可以覆盖可执行文件的环境变量（先到先得）
    default_effort: str
    window_source: str                       # notification（通知里给）/ catalog（模型目录）/ constant（常量）
    window_fallback: int                     # 读不到时用的窗口
    window_estimated: bool                   # 用 window_fallback 时是否算"估算"
    compact_at: float                        # 两件活之间：记忆条达到这个比例就压缩
    compact_after_turns: Optional[int]       # 或者已经做了这么多轮（agy 3 轮）
    midtask_at: Optional[float]              # 干活中途：达到这个比例，这一轮结束后马上交接（只有 agy）
    native_compaction: bool                  # 有没有"自带压缩"可以调
    auto_compact_at: Optional[float]         # CLI 自己的自动压缩点（只作参考，用来错开门槛）
    steer: str                               # live（当场插话）/ none
    interrupt: str                           # soft / kill
    idle_offline_minutes: int                # 空闲多少分钟下线（0 = 不需要）
    max_concurrent: Optional[int]            # 最多同时开几个（None = 不限）
    daily_call_cap: Optional[int]            # 每天最多调用次数（None = 不限；由工位计数，不在适配器里）
    tested_version: str                      # 本机实测过的版本（probes.md）
    # 钉住 CLI 自己的自动压缩点（窗口的比例）。Codex：-c model_auto_compact_token_limit=<int>，
    # 让它干活中途的自动压缩只在 ~90% 才发生，和我们两件活之间 60% 的原生压缩错开（不压两次）。
    # 键名已在 0.165.0 二进制的配置字段表里核对过；实际效果【未验证】。None = 不传（关掉这个开关）。
    pin_auto_compact_at: Optional[float] = None
    # 两件活之间的绝对上限（token 数）：和 compact_at 的百分比谁先到算谁（token-optimizer 调研：AI 变笨跟着
    # "装了多少"走，不跟着百分比走）。None = 只看百分比。
    compact_at_tokens: Optional[int] = None


PROVIDERS: Mapping[str, Provider] = {
    CODEX: Provider(
        agent=CODEX, binary="codex", env_bin=("XAY_CODEX_BIN", "XAY_APP_SERVER_BIN"),
        default_effort="medium",
        window_source="notification",
        # 475000 是本机实测的 modelContextWindow；只在通知里没带窗口时才用，用了就标"估算"
        window_fallback=475_000, window_estimated=True,
        compact_at=0.60, compact_after_turns=None, midtask_at=None,
        native_compaction=True, auto_compact_at=None,
        steer="live", interrupt="soft",
        idle_offline_minutes=15, max_concurrent=None, daily_call_cap=None,
        tested_version="0.165.0", pin_auto_compact_at=0.90,
    ),
    DSH: Provider(
        agent=DSH, binary="dsh", env_bin=("XAY_DSH_BIN",),
        default_effort="medium",
        # M3 查清（probes.md "M3：dsh 真实门槛"）：dsh 记忆条的分母要用 **dsh 自己认为的窗口**，它写在
        # `dsh --dump-config` 里用户自己的路由配置下（llm-pi-ai → providers → <路由> → models）。本机的
        # opencode-xay 是手写路由、没写 contextWindow，dsh 就按 pi-ai 的默认 262,144 算——不是 M0 在
        # opencode-go 模型目录里查到的 1,000,000（那个目录只管 opencode-go 路由）。适配器开工前读一次；
        # 读不到时用下面这个值并标"估算"。
        window_source="dump_config",
        window_fallback=PI_AI_DEFAULT_WINDOW, window_estimated=True,
        # 两件活之间 55% 或 30 万 token（谁先到算谁）。按 262,144 算 55% ≈ 14.4 万，在 dsh 自己约 75%
        # （≈19.7 万）的自动压缩之前，错开了；30 万是给"路由将来写了 1M 窗口"留的保险，待大海捞针实测再定。
        compact_at=0.55, compact_after_turns=None, midtask_at=None, compact_at_tokens=300_000,
        # dsh `/compact` 在 headless 下被当成普通文字（probes.md）-> 不支持自带压缩。
        # 它自己的自动压缩点 = floor(min(窗口×0.8, 窗口−输出预留−65,536))，本机 = 196,608（窗口的 75%），
        # 真实会话里 70 次总结 + 55 次只裁剪工具输出都发生在约 19.6 万处（probes.md）。
        native_compaction=False, auto_compact_at=0.75,
        steer="none", interrupt="kill",
        idle_offline_minutes=0, max_concurrent=None, daily_call_cap=None,
        tested_version="0.2.1-rc.2",
    ),
    AGY: Provider(
        agent=AGY, binary="agy", env_bin=("XAY_AGY_BIN",),
        default_effort="medium",
        window_source="constant",
        # token-optimizer 调研（V2/docs/token-optimizer.md §2，本机 agy 对话库里 285 次生成的 max_context_tokens）：
        # gemini-3.8-flash 三档的真实窗口都是 256,000，不是公开的 1,048,576。stream-json 本身不带窗口，
        # 运行时读不到，所以仍标 estimated=True。
        window_fallback=256_000, window_estimated=True,
        compact_at=0.50, compact_after_turns=3, midtask_at=0.70,
        # M3 大上下文实测（probes.md）：65,211 → /compact（checkpoint 步骤，7 秒）→ 下一轮 16,889，暗号还记得
        # -> 先用自带压缩，不行再交接。
        native_compaction=True, auto_compact_at=None,
        steer="none", interrupt="kill",
        idle_offline_minutes=10, max_concurrent=2, daily_call_cap=None,   # 2026-10-31 用户：不限次数，按真实额度 + 换号管
        tested_version="1.4.11",
    ),
}


def get(agent: str) -> Provider:
    try:
        return PROVIDERS[agent]
    except KeyError:
        raise UnknownAgent("不认识的 AI：%r（只有 %s）" % (agent, ", ".join(AGENTS)))


# ---------------------------------------------------------------------------
# 强度 / 模型
# ---------------------------------------------------------------------------

def _card_get(card, key, default=None):
    if isinstance(card, Mapping):
        return card.get(key, default)
    return getattr(card, key, default)


def resolve_effort(agent: str, card=None) -> str:
    """卡片里的强度；没有就用默认。dsh 的未知值回落到 medium（和旧驱动一致）。"""
    p = get(agent)
    effort = str(_card_get(card, "effort") or p.default_effort).strip().lower()
    if agent == DSH:
        return effort if effort in DSH_EFFORTS else "medium"
    if not _SAFE_TOKEN.fullmatch(effort):
        raise ValueError("强度写法不合法：%r" % effort)
    return effort


def default_model(agent: str, effort: Optional[str] = None) -> Optional[str]:
    """每个强度的默认模型。codex 返回 None（让 app-server 自己选）。"""
    if agent == CODEX:
        return None
    if agent == DSH:
        return DSH_MODEL
    if agent == AGY:
        tier = _AGY_EFFORT_TIER.get(str(effort or get(AGY).default_effort).lower(), "medium")
        return AGY_MODELS_BY_EFFORT[tier]
    raise UnknownAgent(agent)


def validate_model(agent: str, model: Optional[str]) -> Optional[str]:
    """校验模型名；不允许就抛 ModelNotAllowed。返回规范化后的模型（None/默认写法 -> None）。"""
    get(agent)
    text = None if model is None else str(model).strip()
    if text is None or text.lower() in _DEFAULT_WORDS:
        return None
    if agent == DSH:
        if text != DSH_MODEL:
            raise ModelNotAllowed("dsh 只允许使用 %s（收到：%s）" % (DSH_MODEL, text))
        return text
    if agent == AGY:
        if not _AGY_MODEL_RE.fullmatch(text):
            raise ModelNotAllowed("agy 只允许使用 %s* 模型（收到：%s）" % (AGY_MODEL_PREFIX, text))
        return text
    # codex：不限模型名，但要能安全地放进 `-c model="..."`
    if not _SAFE_TOKEN.fullmatch(text):
        raise ModelNotAllowed("模型名写法不合法：%r" % text)
    return text


def resolve_model(agent: str, card=None) -> Optional[str]:
    """卡片里的模型（校验过）；没写就按强度取默认。codex 没写返回 None。"""
    model = validate_model(agent, _card_get(card, "model"))
    if model:
        return model
    return default_model(agent, resolve_effort(agent, card))


# 每条线路可以用环境变量钉一个模型（不设就让 app-server 用 model/list 里这条线路的默认模型）
CODEX_LINE_MODEL_ENV: Mapping[str, str] = {"codex-sub": "XAY2_CODEX_SUB_MODEL", "codex-relay": "XAY2_CODEX_RELAY_MODEL"}


def codex_line_model(line: Optional[str]) -> Optional[str]:
    """换线路时用的模型：环境变量里钉了就用它（校验过），否则 None。"""
    var = CODEX_LINE_MODEL_ENV.get(line or "")
    return validate_model(CODEX, os.environ.get(var)) if var and os.environ.get(var) else None


def codex_provider_for_line(line: Optional[str]) -> str:
    """额度线路 -> model_provider。线路缺失/不认识直接报错：悄悄回落到中转站正是旧驱动的 bug。"""
    if line in CODEX_LINES:
        return CODEX_LINES[line]
    raise UnknownLine("codex 员工必须带 card['line']（%s），收到：%r" % (" / ".join(CODEX_LINES), line))


# ---------------------------------------------------------------------------
# 窗口 / 记忆条 / 压缩门槛
# ---------------------------------------------------------------------------

def _is_int(value) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def _dsh_package_roots() -> List[Path]:
    exe = shutil.which(os.environ.get("XAY_DSH_BIN") or "dsh")
    roots: List[Path] = []
    if exe:
        try:
            cur = Path(os.path.realpath(exe)).parent
        except OSError:
            cur = None
        for _ in range(4):
            if cur is None or cur.parent == cur:
                break
            roots.append(cur)
            cur = cur.parent
    return roots


_CATALOG_REL = Path("node_modules/@earendil-works/pi-ai/dist/providers/data/opencode-go.json")


def dsh_catalog_path() -> Optional[Path]:
    """dsh 自带的模型目录；XAY_DSH_CATALOG 可以指向别的文件（测试用）。"""
    override = os.environ.get("XAY_DSH_CATALOG")
    if override:
        path = Path(override)
        return path if path.is_file() else None
    for root in _dsh_package_roots():
        path = root / _CATALOG_REL
        if path.is_file():
            return path
    return None


@functools.lru_cache(maxsize=8)
def _catalog_window(path_text: str, model: str) -> Optional[int]:
    try:
        with open(path_text, "r", encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, ValueError):
        return None
    if not isinstance(data, dict):
        return None
    for models in data.values():
        if not isinstance(models, dict):
            continue
        for key, entry in models.items():
            if isinstance(entry, dict) and (entry.get("id") == model or str(key).endswith(":" + model)):
                window = entry.get("contextWindow")
                if _is_int(window) and window > 0:
                    return window
    return None


def dsh_catalog_window(model: str = DSH_MODEL) -> Optional[int]:
    path = dsh_catalog_path()
    return _catalog_window(str(path), model) if path else None


def context_window(agent: str, model: Optional[str] = None, reported: Optional[int] = None) -> Tuple[int, bool]:
    """返回 (窗口大小, 是否估算)。

    - ``reported``：CLI 自己给的窗口（codex 通知里的 modelContextWindow；dsh 适配器开工前从
      ``dsh --dump-config`` 读出来的路由窗口），优先；
    - dsh：环境变量 ``XAY_DSH_WINDOW`` 可以钉一个值（测试 / 冒烟用）；
    - 其余用提供者表里的常量（agy、读不到配置的 dsh 都标"估算"）。
    """
    p = get(agent)
    if _is_int(reported) and reported > 0:
        return reported, False
    if agent == DSH:
        env = _env_int("XAY_DSH_WINDOW")
        if env:
            return env, False
    return p.window_fallback, p.window_estimated


def pct_of(used: int, window: int) -> float:
    return round(100.0 * float(used) / float(window), 1) if window else 0.0


def _env_int(name: str) -> Optional[int]:
    try:
        value = int(float(os.environ.get(name) or 0))
    except ValueError:
        return None
    return value if value > 0 else None


def _env_ratio(name: str) -> Optional[float]:
    """门槛覆盖：写 55 或 0.55 都行（>1 当百分数）。只给测试 / 真机冒烟临时调低门槛用。"""
    try:
        value = float(os.environ.get(name) or 0)
    except ValueError:
        return None
    if value <= 0:
        return None
    return value / 100.0 if value > 1 else value


def compact_limits(agent: str) -> Tuple[float, Optional[int], Optional[int]]:
    """两件活之间的压缩门槛：(窗口比例, 绝对 token 上限或 None, 轮数上限或 None)。

    可以用环境变量临时覆盖（XAY2_COMPACT_AT_<AI>、XAY2_COMPACT_TOKENS_<AI>、XAY2_COMPACT_TURNS_<AI>，AI 大写）。"""
    p = get(agent)
    tag = agent.upper()
    ratio = _env_ratio("XAY2_COMPACT_AT_" + tag) or p.compact_at
    tokens = _env_int("XAY2_COMPACT_TOKENS_" + tag) or p.compact_at_tokens
    turns = _env_int("XAY2_COMPACT_TURNS_" + tag) or p.compact_after_turns
    return ratio, tokens, turns


def compaction_reason(agent: str, pct: float, turns_in_segment: int = 0, midtask: bool = False,
                      used: Optional[int] = None) -> Optional[str]:
    """是否该压缩。返回账本里的 reason（threshold / rounds）或 None。

    - 两件活之间（midtask=False）：达到 compact_at（百分比）或 compact_at_tokens（绝对数，谁先到算谁），
      或 agy 已做满 3 轮；
    - 干活中途（midtask=True）：只有 agy 有中途门槛（70%），其他家交给 CLI 自己的自动压缩。
    """
    p = get(agent)
    if midtask:
        if p.midtask_at is not None and pct / 100.0 >= p.midtask_at:
            return "threshold"
        return None
    ratio, tokens, turns = compact_limits(agent)
    if pct / 100.0 >= ratio:
        return "threshold"
    if tokens and _is_int(used) and used >= tokens:
        return "threshold"
    if turns is not None and turns_in_segment >= turns:
        return "rounds"
    return None


# ---------------------------------------------------------------------------
# dsh：从 `dsh --dump-config` 读出它自己的窗口、自动压缩点，并核对我们的补丁真的生效了
# ---------------------------------------------------------------------------

def _indent(line: str) -> int:
    return len(line) - len(line.lstrip(" "))


def _children(lines: List[str], start: int) -> List[str]:
    """lines[start] 这一行下面、缩进比它深的那些行（空行也算进去）。"""
    base = _indent(lines[start])
    out = []
    for line in lines[start + 1:]:
        if line.strip() and _indent(line) <= base:
            break
        out.append(line)
    return out


def _find_key(lines: List[str], key: str) -> Optional[int]:
    """最浅的一层里找 `key:`（返回行号）。"""
    depth = min((_indent(x) for x in lines if x.strip()), default=0)
    for i, line in enumerate(lines):
        if line.strip() and _indent(line) == depth and line.strip().split(":", 1)[0] == key:
            return i
    return None


def _value(lines: List[str], key: str) -> Optional[str]:
    i = _find_key(lines, key)
    if i is None:
        return None
    return lines[i].split(":", 1)[1].strip().strip("'\"") or None


def _int_value(lines: List[str], key: str) -> Optional[int]:
    text = _value(lines, key)
    try:
        return int(text) if text is not None else None
    except ValueError:
        return None


def _float_value(lines: List[str], key: str) -> Optional[float]:
    text = _value(lines, key)
    try:
        return float(text) if text is not None else None
    except ValueError:
        return None


def _sub(lines: List[str], key: str) -> List[str]:
    i = _find_key(lines, key)
    return _children(lines, i) if i is not None else []


def _top_entry(lines: List[str], entry_id: str):
    """顶层条目 `- id: <entry_id>`（不在缩进里，子代理组里的同名条目不算）→ (标题注释, 条目内容的行)。"""
    for i, line in enumerate(lines):
        if line.rstrip() == "- id: %s" % entry_id:
            header = ""
            for k in range(i - 1, -1, -1):
                if lines[k].startswith("# =="):
                    header = lines[k]
                    break
            body = []
            for x in lines[i + 1:]:
                if x.strip() and not x.startswith(" "):
                    break
                body.append(x)
            return header, body
    return None, None


def dsh_auto_compact_tokens(window: int, max_output: int = 0, ratio: float = DSH_COMPACT_RATIO,
                            headroom: int = DSH_COMPACT_HEADROOM) -> Optional[int]:
    """dsh-compaction-basic 的自动压缩点 floor(min(W×ratio, W−O−headroom))；算出来 ≤0 时 dsh 不会主动压缩。"""
    if not window:
        return None
    point = int(min(window * ratio, window - (max_output or 0) - headroom))
    return point if point > 0 else None


def parse_dsh_dump(text: str, route: str, model: str, patch_path: Optional[str] = None) -> Dict[str, object]:
    """解析 `dsh --profile headless [--patch 我们的补丁] --dump-config` 的输出（只在内存里，不落盘、不打印）。

    返回 {model, effort, patched_by_us, window, window_source, max_output, auto_compact_tokens, auto}：
    - model / effort / patched_by_us：顶层 agent-default-model 条目里真正生效的模型，以及它的标题注释
      "patched by …" 里有没有我们的补丁（没有 = 补丁没生效）；
    - window：路由里这个模型写的 contextWindow → 路由的 defaultContextWindow → 路由是 dsh 自带目录里的
      （比如 opencode-go）就用目录里的 → pi-ai 默认 262,144；
    - auto_compact_tokens：按 compaction-basic 的配置（没写就是默认）算出的 dsh 自己的自动压缩点。
    """
    lines = (text or "").splitlines()
    out: Dict[str, object] = {"model": None, "effort": None, "patched_by_us": False, "window": None,
                              "window_source": "", "max_output": 0, "auto_compact_tokens": None, "auto": True}
    header, body = _top_entry(lines, "agent-default-model")
    if body is not None:
        cfg = _sub(body, "config")
        out["model"] = _value(cfg, "model")
        out["effort"] = _value(cfg, "reasoningEffort")
        out["max_output"] = _int_value(cfg, "maxTokens") or 0
        if patch_path:
            names = {str(patch_path), os.path.realpath(str(patch_path))}
            out["patched_by_us"] = any(n in (header or "") for n in names)
    _h, llm = _top_entry(lines, "llm-pi-ai")
    route_lines = _sub(_sub(_sub(llm or [], "config"), "providers"), route) if llm else []
    window, source = None, ""
    if route_lines:
        models = _sub(route_lines, "models")
        for i, line in enumerate(models):
            if line.strip() == "- id: %s" % model:
                item = [(" " * (_indent(line) + 2)) + line.strip()[2:]] + _children(models, i)
                window = _int_value(item, "contextWindow")
                if window:
                    source = "dsh 配置里路由 %s 的模型窗口" % route
                if not out["max_output"]:
                    out["max_output"] = _int_value(item, "maxTokens") or 0
                break
        if not window:
            window = _int_value(route_lines, "defaultContextWindow")
            if window:
                source = "dsh 配置里路由 %s 的默认窗口" % route
    if not window and not route_lines:
        catalog = dsh_catalog_path()
        if catalog and _catalog_has_route(str(catalog), route):
            window = _catalog_window(str(catalog), model)
            if window:
                source = "dsh 自带模型目录（%s）" % route
    if not window:
        window, source = PI_AI_DEFAULT_WINDOW, "路由 %s 没写窗口，按 pi-ai 默认" % route
    out["window"], out["window_source"] = window, source
    _h, comp = _top_entry(lines, "compaction-basic")
    ccfg = _sub(comp or [], "config")
    ratio = _float_value(ccfg, "thresholdRatio") or DSH_COMPACT_RATIO
    headroom = _int_value(ccfg, "headroomTokens")
    out["auto"] = (_value(ccfg, "auto") or "true").lower() != "false"
    out["auto_compact_tokens"] = dsh_auto_compact_tokens(
        window, int(out["max_output"] or 0), ratio, DSH_COMPACT_HEADROOM if headroom is None else headroom) \
        if out["auto"] else None
    return out


def _catalog_has_route(path_text: str, route: str) -> bool:
    """dsh 自带目录一个路由一个文件（opencode-go.json 管 opencode-go 路由）。"""
    return Path(path_text).stem == route


PATCH_WARNING_RE = re.compile(r'patch: entry "([^"]+)" not found')


def dsh_patch_warnings(stderr_text: str, patch_path: str) -> List[str]:
    """stderr 里属于**我们的补丁**的"条目没找到"警告 → 那些条目 id。用户自己 profile 里的同类警告不算。"""
    names = {str(patch_path), os.path.realpath(str(patch_path))}
    bad = []
    for line in (stderr_text or "").splitlines():
        if any("[%s]" % n in line for n in names):
            m = PATCH_WARNING_RE.search(line)
            bad.append(m.group(1) if m else line.strip()[:120])
    return bad


# ---------------------------------------------------------------------------
# 命令行 / 版本探测
# ---------------------------------------------------------------------------

def resolve_command(agent: str, card=None) -> List[str]:
    """可执行文件（可能带固定参数）。优先级：card['bin'] > 环境变量 > 默认。

    ``bin`` 可以是字符串或列表（列表就是完整的前缀命令）。dsh 自定义 bin 时整个替换
    （和旧 XAY_DSH_BIN 一致，不再自动加 --profile headless）。
    """
    p = get(agent)
    custom = _card_get(card, "bin")
    if not custom:
        for var in p.env_bin:
            if os.environ.get(var):
                custom = os.environ[var]
                break
    if custom:
        return [str(x) for x in custom] if isinstance(custom, (list, tuple)) else [str(custom)]
    if agent == DSH:
        wrapper = shutil.which("dsh-run")  # 带凭据的包装脚本（旧系统同样优先用它）
        if wrapper:
            return [wrapper]
        return [shutil.which("dsh") or "dsh", "--profile", "headless"]
    return [shutil.which(p.binary) or p.binary]


_VERSION_RE = re.compile(r"\d+\.\d+\.\d+(?:[-+][0-9A-Za-z.]+)?")


def _version_command(agent: str, bins: Optional[Mapping[str, object]]) -> List[str]:
    if bins and bins.get(agent):
        cmd = bins[agent]
        return ([str(x) for x in cmd] if isinstance(cmd, (list, tuple)) else [str(cmd)]) + ["--version"]
    if agent == DSH:
        # 版本探测用 dsh 本体，不经过带凭据的 dsh-run
        custom = [v for v in get(DSH).env_bin if os.environ.get(v)]
        if custom:
            return [os.environ[custom[0]], "--version"]
        return [shutil.which("dsh") or "dsh", "--version"]
    return resolve_command(agent) + ["--version"]


def probe_versions(agents: Sequence[str] = AGENTS, timeout: float = 10.0,
                   bins: Optional[Mapping[str, object]] = None) -> Dict[str, Optional[str]]:
    """跑 `<bin> --version`（很便宜），返回 {agent: 版本号或 None}。永远不抛异常。"""
    out: Dict[str, Optional[str]] = {}
    for agent in agents:
        try:
            cmd = _version_command(agent, bins)
            proc = subprocess.run(cmd, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                                  stderr=subprocess.STDOUT, text=True, timeout=timeout)
            match = _VERSION_RE.search(proc.stdout or "")
            out[agent] = match.group(0) if match and proc.returncode == 0 else None
        except (OSError, subprocess.SubprocessError, ValueError):
            out[agent] = None
    return out


def version_drift(versions: Mapping[str, Optional[str]]) -> Dict[str, Tuple[str, Optional[str]]]:
    """和本机实测版本不一致的 AI：{agent: (实测版本, 现在的版本)}。读不到版本也算不一致。"""
    drift: Dict[str, Tuple[str, Optional[str]]] = {}
    for agent, actual in versions.items():
        tested = get(agent).tested_version
        if actual != tested:
            drift[agent] = (tested, actual)
    return drift
