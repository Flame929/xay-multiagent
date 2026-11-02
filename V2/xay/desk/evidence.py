"""证据判定。唯一实现在 `xay.core.evidence`（纯函数，protocol §4.3 v1.3 的表）；这里只是转手 + 桌子专用的小便利。"""
from __future__ import annotations

from ..core.evidence import CLAIMS, CLI_STATUSES, MODES, explain, verdict

__all__ = ["CLAIMS", "CLI_STATUSES", "MODES", "explain", "verdict", "verdict_from_evidence"]


def verdict_from_evidence(evidence: dict, mode: str):
    """直接吃 turn.ended 的 evidence 字典：{cli_status, claim?, changed_files:[…], …}。"""
    return verdict(evidence.get("cli_status"), evidence.get("claim"),
                   bool(evidence.get("changed_files")), mode)
