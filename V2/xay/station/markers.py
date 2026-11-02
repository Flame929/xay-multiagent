"""执行者的两种回报（protocol §6）：回复里的标记、桌子里的发件箱。纯函数，不碰账本。

标记（任何 AI、任何沙箱都能用；一个标记从 【 开始，到换行或下一个 【 为止）：
    【进度 2/4】写测试                 → task.progress（source=marker）
    【结果】完成：一句话 / 没做完：… / 卡住：…   → task.reported（claim = done / partial / blocked）
    【写信 给 <部门名或员工名>】主题：正文   → letter.sent
    【回信 lt_…】正文                      → letter.sent（带 reply_to：回给原来写信的人；"要你回复"的那封就算答复了）
                                            用法只在工位交代信件时附上（M4 信件），不进任务卡末尾那 6 行

发件箱：`<桌子>/.xay/out/outbox.jsonl`，每行 {"v":1,"task":"tk_…","type":…,"data":{…}}，由 `xay2 progress` 等命令追加。
"""
import json
import re

# 任务卡末尾附上的用法（≤6 行，§6）
INSTRUCTIONS = "\n".join((
    "【怎么回报】在回复里单独写一行这些标记，系统会自动记下来：",
    "【进度 2/4】用大白话写在干什么（做到第几步 / 一共几步）",
    "【给老板看】一句大白话：做成了什么、对用户有什么用（不写文件名、函数名、命令、英文术语）",
    "【结果】完成：一句话总结　或　【结果】没做完：原因　或　【结果】卡住：原因（最后一定要写一行）",
    "【写信 给 部门名或员工名】主题：正文（需要别人帮忙时用）",
    "也可以在终端运行 xay2 progress 2/4 \"…\" / xay2 done \"…\"。不要 git commit，也不要 git push。",
))

MARK_RE = re.compile(
    r"【\s*(?:(?P<plan>计划)\s*"
    r"|(?P<prog>进度)\s*(?P<step>\d{1,3})\s*[/／]\s*(?P<of>\d{1,3})\s*"
    r"|(?P<plain>给老板看)\s*"
    r"|(?P<res>结果)\s*"
    r"|(?P<mail>写信)\s*给\s*(?P<to>[^】\n]{1,40}?)\s*"
    r"|(?P<reply>回信)\s*(?P<rid>lt_[0-9A-Za-z]{1,32})\s*)】(?P<text>[^\n【]*)")

SUMMARY_MAX = 600
SUBJECT_MAX = 40
BODY_MAX = 2000
OUTBOX_LINE_MAX = 4096
OUTBOX_TURN_MAX = 200
NOTES_MAX = 3000
OUTBOX_TYPES = ("progress", "report", "letter", "proposal", "notes")
LETTER_KINDS = ("request", "tell", "reply")
CLAIMS = ("done", "partial", "blocked")

_CLAIM_PREFIXES = (
    (("没做完", "未完成", "部分完成", "只做了一部分", "没完成"), "partial"),
    (("卡住", "受阻", "阻塞", "做不了", "被卡"), "blocked"),
    (("完成", "做完", "已完成", "全部完成", "搞定"), "done"),
)


def parse_result(text):
    """【结果】后面的文字 → (claim, summary)。认不出前缀时当作"完成"，原话整句做总结。"""
    t = (text or "").strip().lstrip("：:").strip()
    for prefixes, claim in _CLAIM_PREFIXES:
        for p in prefixes:
            if t.startswith(p):
                rest = t[len(p):].lstrip("：:，,。 ").strip()
                return claim, (rest or t)[:SUMMARY_MAX]
    return "done", t[:SUMMARY_MAX]


def parse_plain(text):
    """【给老板看】后面的文字 → 大白话一句（去掉开头冒号）。"""
    t = (text or "").strip().lstrip("：:").strip()
    return t[:SUMMARY_MAX]


def extract_plain(text):
    """从整段文字里找出【给老板看】那一句。"""
    if not text:
        return ""
    m = re.search(r"【\s*给老板看\s*】([^\n【]*)", text)
    if m:
        return parse_plain(m.group(1))
    return ""


def parse_plan(text):
    """【计划】后面的文字 → [{n: 1, text: '...'}, ...]。
    支持 '1. ...'、'1、...'、'1: ...'、'1：...'。
    """
    steps = []
    for line in (text or "").splitlines():
        line = line.strip()
        if not line:
            continue
        m = re.match(r"^(\d{1,3})[\.、:：\s]\s*(.+)$", line)
        if m:
            n = int(m.group(1))
            stext = m.group(2).strip()
            if stext:
                steps.append({"n": n, "text": stext[:200]})
    return steps


def extract_plan(text):
    """从整段文字里找出【计划】列出的步骤列表。"""
    if not text:
        return []
    m = re.search(r"【\s*计划\s*】", text)
    if not m:
        return []
    next_b = text.find("【", m.end())
    raw = text[m.end():next_b] if next_b != -1 else text[m.end():]
    return parse_plan(raw)


def split_letter(text):
    """"主题：正文" → (subject, body)；没有冒号就用前 30 个字做主题。"""
    t = (text or "").strip()
    m = re.search(r"[：:]", t)
    if m and 0 < m.start() <= SUBJECT_MAX:
        subject, body = t[:m.start()].strip(), t[m.end():].strip()
        return subject, (body or subject)[:BODY_MAX]
    return t[:30], t[:BODY_MAX]


def scan(buf, final=False):
    """找出 buf 里的全部标记，按出现顺序编号（从 0 开始）。

    final=False 时只返回"已经写完"的标记（后面已经有换行或下一个 【）；最后一个还在流式输出的不返回，
    这样同一个标记不会因为文字还没写完而被记成半句话。buf 只会在末尾变长，所以编号是稳定的。
    返回 [(序号, 种类, data)]，种类 ∈ progress / result / letter（【回信】也是 letter，data 带 kind=reply 和 reply_to）；
    格式不对的标记跳过但仍占一个序号。
    """
    out = []
    for idx, m in enumerate(MARK_RE.finditer(buf or "")):
        if not final and m.end() >= len(buf):
            break
        text = m.group("text").strip()
        if m.group("plan"):
            pos = m.start() + m.group(0).find("】") + 1
            next_bracket = (buf or "").find("【", pos)
            if next_bracket != -1:
                plan_raw = buf[pos:next_bracket]
            else:
                if not final:
                    break
                plan_raw = buf[pos:]
            steps = parse_plan(plan_raw)
            if not steps:
                continue
            out.append((idx, "plan", {"steps": steps}))
        elif m.group("prog"):
            step, of = int(m.group("step")), int(m.group("of"))
            if of < 1 or step < 0 or step > of:
                continue
            out.append((idx, "progress", {"step": step, "of": of, "text": text[:200]}))
        elif m.group("plain"):
            p = parse_plain(text)
            out.append((idx, "plain", {"plain": p}))
        elif m.group("res"):
            claim, summary = parse_result(text)
            out.append((idx, "result", {"claim": claim, "summary": summary}))
        elif m.group("reply"):
            body = text.lstrip("：:").strip()[:BODY_MAX]
            if not body:
                continue
            out.append((idx, "letter", {"kind": "reply", "reply_to": m.group("rid"), "subject": body[:30], "body": body}))
        else:
            subject, body = split_letter(text)
            if not body:
                continue
            out.append((idx, "letter", {"to": m.group("to").strip(), "subject": subject, "body": body}))
    return out


class Rejected(ValueError):
    """发件箱里这一行不合格（给人看的原因）。"""


def _int(v):
    return isinstance(v, int) and not isinstance(v, bool)


def _str(v, name, limit, required=True):
    if v is None and not required:
        return None
    if not isinstance(v, str) or (required and not v.strip()):
        raise Rejected("%s 要是文字" % name)
    if len(v) > limit:
        raise Rejected("%s 太长了（最多 %d 个字）" % (name, limit))
    return v


def validate_outbox(raw, task, em):
    """一行发件箱 → (type, data)。不合格抛 Rejected。task 必须是这一轮的任务（§6）。"""
    if isinstance(raw, bytes):
        if len(raw) > OUTBOX_LINE_MAX:
            raise Rejected("一行超过 4 KB")
        raw = raw.decode("utf-8", "replace")
    if len(raw.encode("utf-8")) > OUTBOX_LINE_MAX:
        raise Rejected("一行超过 4 KB")
    try:
        row = json.loads(raw)
    except ValueError:
        raise Rejected("不是一行 JSON")
    if not isinstance(row, dict) or row.get("v", 1) != 1:
        raise Rejected("格式版本不对")
    if row.get("task") != task:
        raise Rejected("写的任务（%s）不是这一轮在做的任务" % (row.get("task") or "空"))
    typ, d = row.get("type"), row.get("data")
    if typ not in OUTBOX_TYPES:
        raise Rejected("不认识的类型：%s" % typ)
    if not isinstance(d, dict):
        raise Rejected("data 要是对象")
    if typ == "progress":
        step, of = d.get("step"), d.get("of")
        if not (_int(step) and _int(of) and 1 <= of <= 100 and 0 <= step <= of):
            raise Rejected("进度的步数不对")
        return typ, {"step": step, "of": of, "text": _str(d.get("text", ""), "进度说明", 200, required=False) or "",
                     "source": "outbox"}
    if typ == "report":
        if d.get("claim") not in CLAIMS:
            raise Rejected("汇报的结论只能是 done / partial / blocked")
        summary = d.get("summary") or ""
        if not isinstance(summary, str):
            raise Rejected("汇报内容要是文字")
        res = {"claim": d["claim"], "summary": summary[:SUMMARY_MAX]}
        if "plain" in d and isinstance(d["plain"], str):
            res["plain"] = d["plain"][:SUMMARY_MAX]
        return typ, res
    if typ == "letter":
        if d.get("from") not in (None, em):
            raise Rejected("只能用自己的名义写信")
        kind = d.get("kind") or "tell"
        if kind not in LETTER_KINDS:
            raise Rejected("信的种类不对")
        out = {"from": em, "kind": kind, "subject": _str(d.get("subject") or "", "主题", 80, required=False) or "",
               "body": _str(d.get("body"), "正文", BODY_MAX)}
        if kind == "reply":
            reply_to = d.get("reply_to")
            if not (isinstance(reply_to, str) and reply_to.startswith("lt_")):
                raise Rejected("回信要写清回的是哪封信（lt_…）")
            out["reply_to"] = reply_to
        else:
            out["to"] = _str(d.get("to"), "收信人", 64)
        return typ, out
    if typ == "proposal":
        if d.get("from") not in (None, em):
            raise Rejected("只能用自己的名义提申请")
        changes = d.get("changes")
        if not isinstance(changes, list) or not changes or len(changes) > 20:
            raise Rejected("申请里要有 1~20 条改动")
        clean = []
        for c in changes:
            if not isinstance(c, dict) or c.get("op") not in ("add", "change", "remove", "assign"):
                raise Rejected("申请里的改动格式不对")
            tk = c.get("task")
            if not (isinstance(tk, str) and tk.startswith("tk_")):
                raise Rejected("申请里的任务编号不对")
            fields = c.get("fields") or {}
            if not isinstance(fields, dict):
                raise Rejected("申请里的 fields 要是对象")
            if c["op"] == "add" and not str(fields.get("title") or "").strip():
                raise Rejected("新增任务要有标题")
            item = {"op": c["op"], "task": tk}
            if fields:
                item["fields"] = fields
            clean.append(item)
        to = d.get("to") or "gm"
        if not isinstance(to, str):
            raise Rejected("申请交给谁写得不对")
        return typ, {"from": em, "changes": clean, "reason": _str(d.get("reason") or "", "理由", 600, required=False) or "",
                     "to": to}
    # notes
    active = d.get("active")
    if not isinstance(active, str):
        raise Rejected("笔记要是文字")
    return typ, {"active": active[:NOTES_MAX]}
