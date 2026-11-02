"""xay V2 适配器：把 Codex / dsh / agy 三家原生 CLI 统一成同一套动作和事件。

用法::

    from xay.adapters import make_adapter
    a = make_adapter("agy")
    a.open(cwd, native_id=None, card={"effort": "low", "allow_commands": ["python3 hello.py"]})
    a.send("做这件事")
    for ev in a.events():          # AdapterEvent(kind, data, ts)
        ...

``FakeAdapter`` 是给测试用的脚本化假适配器。
"""

from .base import (
    Adapter, AdapterBusy, AdapterConfigError, AdapterError, AdapterEvent, Capabilities, EVENT_KINDS, ERROR_KINDS,
    classify_error, redact, redact_data,
)
from .codex import CodexAdapter
from .dsh import DshAdapter
from .agy import AgyAdapter
from .fake import FakeAdapter

ADAPTERS = {"codex": CodexAdapter, "dsh": DshAdapter, "agy": AgyAdapter}


def make_adapter(agent: str, **kwargs) -> Adapter:
    """按 AI 名字造一个适配器（还没 open）。"""
    try:
        cls = ADAPTERS[agent]
    except KeyError:
        raise ValueError("不认识的 AI：%r（只有 %s）" % (agent, ", ".join(ADAPTERS)))
    return cls(**kwargs)


__all__ = [
    "Adapter", "AdapterBusy", "AdapterConfigError", "AdapterError", "AdapterEvent", "Capabilities", "EVENT_KINDS", "ERROR_KINDS",
    "classify_error", "redact", "redact_data", "CodexAdapter", "DshAdapter", "AgyAdapter", "FakeAdapter",
    "ADAPTERS", "make_adapter",
]
