"""反应表的默认值（arch-v10 附录 B、§3.1 ③、§3.5）：写在代码里，可以用 `$XAY2_HOME/reactions.json` 覆盖。

覆盖文件只需要写想改的那几项，其余照默认；数字类的项写错（不是数字、负数）就忽略那一项并记一行日志。
例子（把非额度失败的自动重试改成 2 次、每件活默认最多 20 轮）：

    {"failure": {"retry": 2}, "budget": {"task": {"turns": 20}}}

文件改了会自动重新读（按修改时间），不用重启管家。
"""
import copy
import json
import logging
from pathlib import Path

from . import paths

LOG = logging.getLogger("xay.office.reactions")

FILE_NAME = "reactions.json"

DEFAULTS = {
    # ① 选人三步规则（§3.1 ③）
    "pick": {
        "fail_streak": 2,             # 最近连续失败这么多次的不选
        "memory_pct": 80,             # 记忆条超过这个、而且压缩也压不下来的不选
        "recent_tasks": 3,            # "和他最近 N 件活改的文件有重叠"
        "wait_minutes": 10,           # 老员工正忙、预计要等这么久以上，而且还有空名额 → 招新人
        "default_task_minutes": 10,   # 没有历史可参考时，一件活按这么久估
    },
    # ② 反应表（附录 B）
    "failure": {"retry": 1, "retry_delay_s": 5,       # 失败（不是额度原因）：写信给原员工、让他接着修
                "rate_limit_wait_s": 120},            # 被限流（等一会儿就好）、工位又没换线路 / 账号：等这么久直接再排一次（算 env 那 1 次）
    "env": {"retry": 1},                              # 开工前没对齐桌子 / 没上线 等：直接重排一次（不写信）
    "repeated_failure_at": 3,                         # 同一件事累计失败这么多次 → 写给用户
    "conflict": {"retry": 1, "retry_after_s": 30},    # 合并回项目时冲突：过一会自动再合并一次
    "silent": {"minutes": {"codex": 10, "dsh": 15, "agy": 15}},   # 沉默超时；翻倍后变成"需要你"
    "recall": {"retry": 1},                           # 压缩后复述不出来：再给一次笔记重新复述
    "overlap": {"enabled": True},                     # 两件活要改的文件有重叠：开工前提醒
    # ③ 预算（§3.5）：每件活、每个员工（按自然日）各三条线；任务卡 budget 字段优先于这里的默认
    "budget": {
        "task": {"tokens": 5000000, "minutes": 120, "turns": 10},
        "employee": {"tokens": 50000000, "minutes": 600, "turns": 100},
        "warn_pct": 70, "wrap_pct": 90, "stop_pct": 100,
        "boost": 0.5,                 # 你在"需要你"里点"再给一些预算"：每次加原额度的一半
    },
    # agy 每天最多调用次数：0 = 和工位用同一个数（xay.quota：XAY2_AGY_DAILY_CAP / 提供者表，默认不限；
    # 次数也是同一本账，V2 + 旧系统当天合计）。这里写一个更小的数可以让管家提前收手，写大了不起作用
    "agy_daily_calls": 0,
    # 失败 / 一直报错 / 合并冲突发生在这么久以前的，不再自动处理（管家停了一阵再起来，或者第一次接上旧账本时，
    # 不会把以前的失败翻出来重试）
    "stale_minutes": 60,
    # ④ 总闸（§3.5）：请工位停 → 等这么久还在 → 强制结束 → 再等这么久还在 → 直接杀掉
    "stop": {"term_after_s": 20, "kill_after_s": 10},
    # ⑤ 四条线路的真实额度（xay/quota_live.py）：管家每隔 refresh_s 秒用各家官方的查法查一次（0 = 不查）；
    # Gemini 的 `agy -p /usage` 要起一个 agy 进程、慢，单独 agy_refresh_s；每次查询最多等 timeout_s 秒；
    # 读数没变就不写账本，但至少每 keepalive_s 秒写一次（页面上"几分钟前查的"）
    "quota": {"refresh_s": 300, "agy_refresh_s": 600, "timeout_s": 90, "keepalive_s": 900},
}

_cache = {"path": None, "mtime": None, "cfg": None}


def _merge(base, over, where=""):
    for k, v in over.items():
        if k not in base:
            LOG.warning("reactions.json 里有不认识的项：%s%s（忽略）", where, k)
            continue
        cur = base[k]
        if isinstance(cur, dict):
            if isinstance(v, dict):
                _merge(cur, v, where + k + ".")
            else:
                LOG.warning("reactions.json 的 %s%s 应该是一组设置（忽略）", where, k)
        elif isinstance(cur, bool):
            if isinstance(v, bool):
                base[k] = v
        elif isinstance(cur, (int, float)):
            if isinstance(v, (int, float)) and not isinstance(v, bool) and v >= 0:
                base[k] = v
            else:
                LOG.warning("reactions.json 的 %s%s 应该是不小于 0 的数字（忽略）", where, k)
    return base


def load(home=None):
    """当前生效的反应表（默认值 + 覆盖文件）。返回一份拷贝，调用方随便改。"""
    path = Path(home) / FILE_NAME if home else paths.v2_home() / FILE_NAME
    try:
        mtime = path.stat().st_mtime
    except OSError:
        mtime = None
    if _cache["cfg"] is not None and _cache["path"] == str(path) and _cache["mtime"] == mtime:
        return copy.deepcopy(_cache["cfg"])
    cfg = copy.deepcopy(DEFAULTS)
    if mtime is not None:
        try:
            over = json.loads(path.read_text(encoding="utf-8"))
            if isinstance(over, dict):
                _merge(cfg, over)
            else:
                LOG.warning("reactions.json 应该是一个 JSON 对象，先用默认值")
        except (OSError, ValueError) as exc:
            LOG.warning("reactions.json 读不出来（%s），先用默认值", exc)
    _cache.update(path=str(path), mtime=mtime, cfg=cfg)
    return copy.deepcopy(cfg)
