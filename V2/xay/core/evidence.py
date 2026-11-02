"""结论看证据：protocol §4.3（v1.3）"证据规则"表的唯一实现。纯函数，不读时钟、不读文件，也不依赖 desk。

一轮结束后，工位拿"这一轮的状态 / 汇报里怎么说 / 实际改没改 / 任务是只读还是要写"来判。
表从上往下取**第一条匹配**的行：

    cli_status          claim              有改动        结论
    completed           done / 无          有，write     landed
    completed           任意               有，readonly  kept(mismatch)  "只读任务却改了文件"
    completed           done / 无          无，readonly  landed（只读任务，汇报即成果）
    completed           done / 无          无，write     kept(mismatch)  "说做完了但没有改动"
    completed           partial / blocked  任意          kept(mismatch)
    failed/interrupted  done               有            kept(mismatch)
    failed/interrupted  done               无            kept(mismatch)  "说做完了，但这一轮没正常结束，也没有改动"
    failed/interrupted  非 done            有            kept(failed_with_changes)
    failed              非 done            无            failed
    interrupted         非 done            无            kept(interrupted)

"非 done"包括 partial / blocked / 没有汇报；"done / 无"里的"无"就是 claim 为 None（空串也当没有）。
"""
from __future__ import annotations

from typing import Optional

CLI_STATUSES = ("completed", "failed", "interrupted")
CLAIMS = ("done", "partial", "blocked")
MODES = ("readonly", "write")


def _norm_claim(cli_status, claim, mode):
    if cli_status not in CLI_STATUSES:
        raise ValueError("cli_status 只能是 %s，收到 %r" % ("/".join(CLI_STATUSES), cli_status))
    if claim in ("", None):
        claim = None
    elif claim not in CLAIMS:
        raise ValueError("claim 只能是 %s 或空，收到 %r" % ("/".join(CLAIMS), claim))
    if mode not in MODES:
        raise ValueError("mode 只能是 %s，收到 %r" % ("/".join(MODES), mode))
    return claim


def verdict(cli_status: str, claim: Optional[str], has_changes: bool, mode: str):
    """→ ("landed", None) | ("kept", reason) | ("failed", reason)。

    kept 的 reason 是 task.kept 的枚举（mismatch / failed_with_changes / interrupted）；
    failed 的 reason 是一句大白话中文（task.failed.reason 本来就是给人看的）。"""
    claim = _norm_claim(cli_status, claim, mode)
    has_changes = bool(has_changes)
    done_like = claim in (None, "done")
    if cli_status == "completed":
        if has_changes and mode == "write" and done_like:
            return ("landed", None)
        if has_changes and mode == "readonly":
            return ("kept", "mismatch")
        if not has_changes and mode == "readonly" and done_like:
            return ("landed", None)
        return ("kept", "mismatch")  # 无改动 + write，或 partial / blocked
    # failed / interrupted
    if claim == "done":
        return ("kept", "mismatch")
    if has_changes:
        return ("kept", "failed_with_changes")
    if cli_status == "failed":
        return ("failed", "这一轮失败了，也没有留下任何改动")
    return ("kept", "interrupted")


def explain(cli_status: str, claim: Optional[str], has_changes: bool, mode: str) -> str:
    """结论对应的一句大白话（放进 task.kept.detail / 需要你的说明）；landed 返回空串。"""
    kind, reason = verdict(cli_status, claim, has_changes, mode)
    claim = None if claim == "" else claim
    if kind == "landed":
        return ""
    if kind == "failed":
        return reason
    if reason == "interrupted":
        return "这一轮被打断了，也没有留下改动"
    if reason == "failed_with_changes":
        return "这一轮%s，改好的文件还没放进项目，你可以选择放进项目、让他接着做，或者不要了" % ("失败了" if cli_status == "failed" else "被打断了")
    # mismatch
    if cli_status == "completed":
        if has_changes and mode == "readonly":
            return "只读任务却改了文件，改动还没放进项目。你可以看看改了什么，选择放进项目、让他接着做或不要了"
        if claim in ("partial", "blocked"):
            return "他说%s，不是完全做完，改动还没放进项目，你可以选择放进项目、让他接着做，或者不要了" % ("只做了一部分" if claim == "partial" else "卡住了")
        return "说做完了但没有改动"
    if has_changes:
        return "他说做完了，但这一轮其实%s，改好的文件还没放进项目，你可以选择放进项目、让他接着做，或者不要了" % (
            "失败了" if cli_status == "failed" else "被打断了")
    return "说做完了，但这一轮没正常结束，也没有改动"
