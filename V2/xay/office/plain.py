"""大白话规则：把 AI 干活的原始记录（命令、路径、英文思考、报告原文）变成不懂编程的老板看得懂的话。

全部是确定的规则，不调用任何模型，同样的输入永远得到同样的输出：
- 去掉 Markdown 记号和代码块；路径只留文件名；密钥一律抹掉；
- 技术词按小词表翻译（commit→保存版本、worktree / 工作区→桌子、docstring→函数说明、测试通过→检查通过……）；
- 员工写给系统看的【进度 3/4】【结果】标记去掉；
- 命令不原样显示：分成"读了几个文件""改了哪个文件""跑自动测试：通过 ✓"等；连续的读 / 查 / 改合并成一步；
- 原始 JSON、shell 命令不会出现在结果里（技术细节由页面单独折叠展示原文）。

对外：redact / plain / strip_markdown / bare_paths / translate / first_sentences /
ask_from_prompt / summary_from_report / StepBuilder / steps_from_legacy_events / steps_from_live_rows / LiveNarrator / is_plain / is_plain_title / explain_title_code_like；
详情栏用的短句：short_title / progress_text / explain_error / why_failed / one_line / key_steps / notes_text。
"""
import json
import re
import shlex
from urllib.parse import urlsplit

MAX_STEPS = 30
HEAD_STEPS = 12                  # 步骤太多时：开头留这么多、结尾留 MAX_STEPS-HEAD_STEPS-1，中间折成一句话
SENTENCE_MAX = 80                # 每句最多这么多字，超了加省略号
NAMES_SHOWN = 3                  # "读了 5 个文件：a、b、c…" 最多列几个名字

# ---------------------------------------------------------------- 脱敏

_SECRET_PATTERNS = (
    (re.compile(r"(?i)\b(API[_-]?KEY|TOKEN|SECRET|PASSWORD)\s*=\s*\S+"), r"\1=<已隐藏>"),
    (re.compile(r"(?i)\bAuthorization:[ \t]*[^\r\n]+"), "Authorization: <已隐藏>"),
    (re.compile(r"(?i)\bBearer\s+\S+"), "Bearer <已隐藏>"),
    (re.compile(r'(?i)"(api_key|token|secret|password)"\s*:\s*"[^"]+"'), r'"\1":"<已隐藏>"'),
    (re.compile(r"\bsk-[A-Za-z0-9_\-]{8,}"), "<已隐藏>"),
    (re.compile(r"\bgh[pousr]_[A-Za-z0-9]{20,}"), "<已隐藏>"),
    (re.compile(r"\bAKIA[0-9A-Z]{16}\b"), "<已隐藏>"),
    (re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----.*?-----END [A-Z ]*PRIVATE KEY-----", re.S), "<已隐藏>"),
)


def redact(text):
    """抹掉常见密钥形态（页面上任何来自 AI 的文字都先过这一道）。"""
    if not isinstance(text, str):
        return ""
    for pattern, repl in _SECRET_PATTERNS:
        text = pattern.sub(repl, text)
    return text


# ---------------------------------------------------------------- 小工具

_CJK = re.compile(r"[\u4e00-\u9fff]")
_LETTER = re.compile(r"[A-Za-z]")


def mostly_english(text):
    """主要是英文（机器没法确定地翻译，页面上就不原样展示）。"""
    letters, cjk = len(_LETTER.findall(text)), len(_CJK.findall(text))
    return letters >= 8 and cjk * 2 < letters


_BOLD_HEAD = re.compile(r"\*\*([^*\n]{1,160})\*\*")
_BOLD_TAIL = re.compile(r"\*\*[^*\n]*$")


def drop_english_headers(text):
    """Codex 的思考摘要每段开头有一行英文粗体小标题（**Planning …**）：没有中文的小标题去掉，换成断句，
    免得和后面的中文粘成"Planning to add mul 函数先看看……"。还没收尾的半截小标题也去掉。"""
    t = _BOLD_HEAD.sub(lambda m: m.group(0) if _CJK.search(m.group(1)) else "\n", text or "")
    return _BOLD_TAIL.sub(lambda m: m.group(0) if _CJK.search(m.group(0)) else "", t)


def cut(text, limit):
    text = (text or "").strip()
    return text if len(text) <= limit else text[:max(limit - 1, 1)].rstrip() + "…"


def squash(text):
    return re.sub(r"\s+", " ", text or "").strip()


_EXT = re.compile(r"\.[A-Za-z][A-Za-z0-9]{0,7}$")
_URL = re.compile(r"https?://[^\s)）」』>\"']+")


def bare_name(path):
    """路径只留文件名；以斜杠结尾的是文件夹，加"文件夹"三个字。"""
    path = (path or "").strip().strip("'\"")
    if not path:
        return ""
    is_dir = path.endswith("/") and not _EXT.search(path.rstrip("/"))
    segs = [s for s in path.split("/") if s and s not in (".", "..", "~")]
    if not segs:
        return "文件夹" if is_dir else ""
    name = segs[-1]
    return name + " 文件夹" if is_dir else name


def _is_pathish(run):
    segs = [s for s in run.split("/") if s]
    if not segs or "/" not in run:
        return False
    if run.startswith(("/", "~/", "./", "../")):
        return True
    if len(segs) >= 3:
        return True
    last = segs[-1]
    if run.endswith("/"):
        return len(segs) >= 2
    return bool(_EXT.search(last))


_RUN = re.compile(r"[A-Za-z0-9_.@+\-~/]+")


def bare_paths(text):
    """正文里的路径换成文件名，网址换成网站名。"""
    def url(m):
        try:
            host = urlsplit(m.group(0)).hostname or ""
        except ValueError:
            host = ""
        return host or "网址"

    text = _URL.sub(url, text)

    def run(m):
        token = m.group(0)
        lead = len(token) - len(token.lstrip(".,;:"))
        core = token[lead:].rstrip(".,;:")
        tail = token[lead + len(core):]
        if not core or not _is_pathish(core):
            return token
        return token[:lead] + (bare_name(core) or core) + tail

    return _RUN.sub(run, text)


# ---------------------------------------------------------------- 小词表

# 前后不能紧挨着字母数字、斜杠、横线，后面也不能是 ".扩展名"（免得把 merge.py 这样的文件名翻坏）
_B0 = r"(?<![A-Za-z0-9_./\-])"
_B1 = r"(?![A-Za-z0-9_/\-=]|\.[A-Za-z0-9])"   # 后面紧跟 = 的是变量名（TOKEN=…），不翻

GLOSSARY = (
    (r"git\s+commit(?:s|ted|ting)?", "保存版本"),
    (r"commit(?:s|ted|ting)?", "保存版本"),
    (r"git\s+push(?:es|ed|ing)?", "上传"),
    (r"worktrees?|desks?", "桌子"),
    (r"pytest|unittests?", "自动测试"),
    (r"docstrings?", "函数说明"),
    (r"diffs?|patch(?:es)?", "改动"),
    (r"merge[sd]?|merging", "合并"),
    (r"pull\s+requests?|PRs?", "合并申请"),
    (r"tokens?", "记忆用量"),
    (r"__pycache__|pycache|caches?", "临时缓存"),
    (r"stdout|stderr", "输出"),
    (r"prompts?", "任务说明"),
    (r"LLMs?", "AI"),
    (r"functions?", "函数"),
    (r"tests?|testing", "测试"),
    (r"debug(?:s|ged|ging)?", "排查问题"),
    (r"configs?|configuration", "配置"),
    (r"APIs?", "接口"),
    (r"endpoints?", "接口地址"),
    (r"schemas?", "数据格式"),
    (r"refactor(?:s|ed|ing)?", "整理代码"),
    (r"lint(?:s|ed|ing|er)?", "代码检查"),
    (r"CLI", "命令行"),
    (r"branch(?:es)?", "分支"),
    (r"repos?|repositor(?:y|ies)", "代码仓库"),
    (r"deploy(?:s|ed|ing|ment)?", "上线"),
    (r"bugs?", "问题"),
    (r"fixtures?", "样例数据"),
    (r"mock(?:s|ed|ing)?", "模拟"),
    (r"dependenc(?:y|ies)", "依赖"),
    (r"regex(?:es)?", "匹配规则"),
    (r"snapshots?", "快照"),
    (r"frontend|front-end", "前端"),
    (r"backend|back-end", "后端"),
    (r"timeouts?", "超时"),
)
_CASE_SENSITIVE = ("CLI", "APIs?", r"pull\s+requests?|PRs?", "LLMs?")
_GLOSSARY = [(re.compile(_B0 + "(?:" + pat + ")" + _B1, 0 if pat in _CASE_SENSITIVE else re.I), repl)
             for pat, repl in GLOSSARY]
# 中文里的行话（不用管词边界）
PHRASES = (
    ("测试没通过", "检查没通过"), ("测试未通过", "检查没通过"), ("测试失败", "检查没通过"),
    ("测试都通过", "检查都通过"), ("测试全部通过", "检查全部通过"), ("测试通过", "检查通过"), ("通过测试", "通过了检查"),
    ("单元测试", "自动测试"), ("回归测试", "复查"), ("上下文窗口", "记忆"), ("上下文", "记忆"),
    ("独立工作区", "桌子"), ("工作区", "桌子"),
)
_SECTION_SIGN = re.compile(r"§\s*(\d+(?:\.\d+)?)")
_CJK_GAP = re.compile(r"(?<=[一-鿿]) (?=[一-鿿])")


def translate(text):
    """技术词翻成大白话；翻完后两个汉字中间留下的空格去掉（"中文 docstring" → "中文函数说明"）。"""
    for pattern, repl in _GLOSSARY:
        text = pattern.sub(repl, text)
    for old, new in PHRASES:
        text = text.replace(old, new)
    text = _CJK_GAP.sub("", text)
    return _SECTION_SIGN.sub(r"第\1节", text)


# ---------------------------------------------------------------- Markdown

_FENCE = re.compile(r"```[^\n]*\n.*?(?:```|\Z)", re.S)
_INLINE = re.compile(r"`([^`\n]+)`")


def strip_markdown(text, keep_ticks=False):
    """去掉代码块、标题、粗体、链接、列表符号、表格线；行与行的分隔保留。keep_ticks=True 时保留行内代码的反引号（让后面按"命令/路径"处理）。"""
    t = _FENCE.sub("\n", text or "")
    t = re.sub(r"!\[[^\]]*\]\([^)]*\)", "", t)
    t = re.sub(r"\[([^\]]+)\]\([^)]*\)", r"\1", t)
    t = re.sub(r"^\s{0,3}#{1,6}\s*", "", t, flags=re.M)
    t = re.sub(r"^\s*>\s?", "", t, flags=re.M)
    t = re.sub(r"\*\*|__|~~", "", t)
    t = re.sub(r"(?<![\w*])\*(?![\s*])([^*\n]+?)\*(?![\w*])", r"\1", t)
    t = re.sub(r"^\s*(?:[-*+•]|\d+[.)])\s+", "", t, flags=re.M)
    t = re.sub(r"^\s*\|?[\s:\-|]{3,}\|?\s*$", "", t, flags=re.M)
    t = re.sub(r"^\s*\|(.*)\|\s*$", lambda m: " ".join(c.strip() for c in m.group(1).split("|")), t, flags=re.M)
    return t if keep_ticks else t.replace("`", "")


def _inline_code(text):
    """行内代码：命令换成人话，路径只留文件名，JSON 不显示，其余原样去掉反引号。"""
    def one(m):
        s = m.group(1).strip()
        if not s:
            return ""
        if s[0] in "{[" and ('"' in s or ":" in s):
            return "一段数据"
        if looks_like_command(s):
            return classify_cmd(s)["text"]
        if " " not in s and _is_pathish_loose(s):
            return bare_name(s) or s
        return s
    return _INLINE.sub(one, text)


def _is_pathish_loose(s):
    return "/" in s and (_is_pathish(s) or bool(_EXT.search(s)) or s.endswith("/"))


# 员工写给系统看的标记：【进度 3/4】【结果】……人不需要看见
_MARKER = re.compile(r"【\s*(?:进度\s*\d+\s*[/／]\s*\d+|结果|进度|汇报|说明|问题|备注|完成|总结)\s*】\s*")


def strip_markers(text):
    return _MARKER.sub("", text or "")


def plain(text, limit=None):
    """整段文字的大白话：脱敏 → 行内代码 → 去 Markdown → 去【进度】这类标记 → 路径变文件名 → 小词表 → 合并空白（保留换行）。"""
    t = redact(text)
    t = _FENCE.sub("\n", t)
    t = _inline_code(t)
    t = strip_markdown(t)
    t = strip_markers(t)
    t = translate(bare_paths(t))
    lines = [squash(line) for line in t.splitlines()]
    out = "\n".join(line for line in lines if line)
    return cut(out, limit) if limit else out


_SENT_SPLIT = re.compile(r"(?<=[。！？!?])|\n+|(?<=[.])\s+(?=\S)")


def sentences(text):
    parts = [squash(p) for p in _SENT_SPLIT.split(text or "")]
    return [p for p in parts if p and not re.fullmatch(r"[\s\-–—·•.。,，:：;；]+", p)]


def cut_clause(text, limit):
    """太长的一句在小句边界（，、；：）处截断再加省略号，比从词中间硬切好读。"""
    text = (text or "").strip()
    if len(text) <= limit:
        return text
    head = text[:limit]
    at = max(head.rfind(ch) for ch in "，、；：,;:")
    return (head[:at] if at >= limit * 0.5 else head[:limit - 1]).rstrip() + "……"


def first_sentences(text, n=3, each=SENTENCE_MAX):
    """前 n 句，每句限长（在小句边界截断），句与句之间补句号。以冒号结尾的句子是后面列表的引子，单独成句没有意义，跳过（只剩它时才留）。"""
    sents = sentences(text)
    kept = [s for s in sents if not s.rstrip().endswith(("：", ":"))] or sents
    out = [cut_clause(s.rstrip("。.；;，,：: "), each) for s in kept[:n]]
    return "。".join(out) + ("。" if out else "")


_PAREN = re.compile(r"（[^（）]*）|\([^()]*\)")


def drop_parentheses(text):
    """去掉括号里的补充说明（可嵌套）：交代的事只要主干，括号里多是技术细节。"""
    for _ in range(4):
        new = _PAREN.sub("", text)
        if new == text:
            break
        text = new
    return text


# ---------------------------------------------------------------- 命令 → 人话

KNOWN_BINS = frozenset("""cat sed head tail nl less more bat wc file stat ls tree find fd rg grep ag ack pwd which echo printf
true false test curl wget mkdir cp mv rm touch chmod ln rsync git python python3 python3.11 python3.12 python3.13 node npm npx pnpm yarn
pip pip3 uv brew make cargo go bash sh zsh ruby deno cd env sudo time nohup timeout open tee xay xay2 sleep diff patch tar unzip zip jq awk
sort uniq cut tr xargs""".split())

_WRAPPERS = frozenset(("sudo", "time", "nohup", "env", "exec", "command", "stdbuf", "nice"))
_READ_BINS = frozenset(("cat", "sed", "head", "tail", "nl", "less", "more", "bat", "wc", "file", "stat"))
_SEARCH_BINS = frozenset(("rg", "grep", "ag", "ack"))
_LIST_BINS = frozenset(("ls", "tree", "find", "fd", "locate"))
_NOISE_BINS = frozenset(("pwd", "echo", "printf", "true", "false", "test", "cd", "sleep", "which", "export", "set", "[", "type"))
_FILES_BINS = frozenset(("mkdir", "cp", "mv", "rm", "touch", "chmod", "ln", "rsync", "tar", "unzip", "zip"))
_TEST_PAT = re.compile(r"unittest|pytest|py\.test|npm (?:run )?test|pnpm (?:run )?test|yarn test|vitest|jest|go test|cargo test|\bmocha\b")
_CHECK_PAT = re.compile(r"py_compile|compileall|node --check|bash -n|\bsh -n\b|\btsc\b|npm run build|pnpm (?:run )?build|cargo build|go build|"
                        r"\bmake\b|\bruff\b|flake8|eslint|\bmypy\b|pyflakes|--dry-run|--check\b")
_INSTALL_PAT = re.compile(r"(?:npm|pnpm|yarn) (?:install|add|i)\b|pip3? install|brew install|uv (?:add|pip install)|cargo install")
_HEREDOC = re.compile(r"<<-?\s*['\"]?[A-Za-z_]\w*['\"]?")
_ENV_ASSIGN = re.compile(r"^[A-Za-z_]\w*=")
_RANGE = re.compile(r"^\d+(?:,\d+)?[pdq]?$|^\d+,\$p$|^\$p$|^/.*/$")

# 同一条命令里有好几件事时，按这个优先级挑一件代表
_PRIORITY = {"test": 0, "check": 1, "install": 2, "commit": 3, "merge": 4, "write": 5, "script": 6, "web": 7, "fetch": 7,
             "files": 8, "read": 9, "list": 10, "search": 11, "git": 12, "tool": 13, "other": 14, "noise": 99}


def looks_like_command(s):
    s = s.strip()
    tokens = s.split()
    if not tokens or len(tokens) > 40:
        return False
    first = tokens[0]
    if _ENV_ASSIGN.match(first):
        return True
    if first in ("pytest", "unittest"):
        return True
    return len(tokens) >= 2 and first in KNOWN_BINS and not first.endswith((".py", ".js", ".md"))


def _split_parts(cmd):
    """按没被引号包住的 && || ; | 和换行拆成几段。heredoc（<<'EOF'）后面的正文整个丢掉。"""
    cmd = _HEREDOC.split(cmd, maxsplit=1)[0]
    parts, buf, quote, i = [], [], None, 0
    while i < len(cmd):
        ch = cmd[i]
        if quote:
            buf.append(ch)
            if ch == quote and cmd[i - 1] != "\\":
                quote = None
        elif ch in "'\"":
            quote = ch
            buf.append(ch)
        elif ch == "\n" or ch == ";" or ch == "|" or (ch == "&" and cmd[i:i + 2] == "&&"):
            if ch in "|&" and cmd[i:i + 2] in ("||", "&&"):
                i += 1
            parts.append("".join(buf))
            buf = []
        else:
            buf.append(ch)
        i += 1
    parts.append("".join(buf))
    return [p.strip() for p in parts if p.strip()]


def _tokens(part):
    try:
        toks = shlex.split(part)
    except ValueError:
        toks = part.split()
    while toks:
        head = toks[0]
        if _ENV_ASSIGN.match(head) or head in _WRAPPERS:
            toks = toks[1:]
        elif head == "timeout" and len(toks) > 2:
            toks = toks[2:]
        else:
            break
    return toks


def _file_args(toks):
    """命令参数里像文件名的那些（跳过选项、行号范围、变量和通配符）。"""
    out = []
    for t in toks:
        if t.startswith("-") or t in (">", ">>", "<", "&&") or _RANGE.match(t) or t.isdigit():
            continue
        if re.match(r"^\d?>", t) or any(ch in t for ch in "$*{}()`<>"):
            continue
        name = bare_name(t)
        if name:
            out.append(name)
    return out


def _redirect_target(toks):
    for i, t in enumerate(toks):
        target = ""
        if t in (">", ">>") and i + 1 < len(toks):
            target = toks[i + 1]
        elif re.match(r"^>>?[^>&]", t):
            target = t.lstrip(">")
        if target and target != "/dev/null" and not target.startswith("&"):
            return target
    return ""


def _classify_part(part):
    toks = _tokens(part)
    if not toks:
        return {"cat": "noise", "text": "", "files": []}
    while toks and toks[0] in ("do", "then", "else", "elif"):
        toks = toks[1:]
    if not toks or toks[0] in ("for", "while", "until", "if", "done", "fi", "esac", "{", "}"):
        return {"cat": "noise", "text": "", "files": []}
    name = toks[0].rsplit("/", 1)[-1]
    low = " ".join(toks).lower()
    if name in ("bash", "sh", "zsh") and len(toks) >= 3 and toks[1] in ("-lc", "-c", "-ic", "-l"):
        return classify_cmd(toks[2])
    if name == "cd":
        return {"cat": "noise", "text": "", "files": []}
    if _TEST_PAT.search(low):
        return {"cat": "test", "text": "跑自动测试", "files": []}
    if _INSTALL_PAT.search(low):
        return {"cat": "install", "text": "安装依赖", "files": []}
    if _CHECK_PAT.search(low) and name not in ("git", "echo", "printf"):
        text = "试运行" if "--dry-run" in low else "检查代码能否运行"
        return {"cat": "check", "text": text, "files": []}
    target = _redirect_target(toks)
    if name in ("tee",):
        files = [bare_name(t) for t in toks[1:] if not t.startswith("-")]
        return {"cat": "write", "text": "改了文件", "files": [f for f in files if f]}
    if name in ("sed", "perl") and any(t.startswith("-i") or t == "-pi" for t in toks[1:]):
        files = _file_args(toks[1:])
        return {"cat": "write", "text": "改了文件", "files": files[-1:]}
    if target:
        return {"cat": "write", "text": "改了文件", "files": [bare_name(target)]}
    if name == "git":
        sub = next((t for t in toks[1:] if not t.startswith("-")), "")
        if sub == "commit":
            return {"cat": "commit", "text": "保存版本", "files": []}
        if sub == "merge":
            return {"cat": "merge", "text": "合并改动", "files": []}
        if sub in ("diff", "status", "log", "show", "blame", "ls-files"):
            return {"cat": "git", "text": "查看改动记录", "files": []}
        if sub == "worktree":
            return {"cat": "git", "text": "整理桌子", "files": []}
        if sub in ("checkout", "switch", "branch"):
            return {"cat": "git", "text": "切换分支", "files": []}
        if sub in ("add", "stash", "reset", "restore", "rebase", "cherry-pick", "apply"):
            return {"cat": "git", "text": "整理改动", "files": []}
        return {"cat": "git", "text": "操作代码仓库", "files": []}
    if name in _READ_BINS:
        return {"cat": "read", "text": "读了文件", "files": _file_args(toks[1:])}
    if name in _SEARCH_BINS:
        return {"cat": "search", "text": "查找内容", "files": []}
    if name in _LIST_BINS:
        return {"cat": "list", "text": "看了看有哪些文件", "files": []}
    if name in ("curl", "wget"):
        url = next((t for t in toks[1:] if t.startswith("http")), "")
        host = ""
        if url:
            try:
                host = urlsplit(url).hostname or ""
            except ValueError:
                host = ""
        return {"cat": "fetch", "text": "访问网址" + (" " + host if host else ""), "files": []}
    if name in _FILES_BINS:
        return {"cat": "files", "text": "整理文件", "files": []}
    if name in ("python", "python3", "node", "deno", "ruby", "bash", "sh", "zsh") or re.match(r"python3?\.\d+$", name):
        if "-m" in toks:
            mod = toks[toks.index("-m") + 1] if toks.index("-m") + 1 < len(toks) else ""
            return {"cat": "script", "text": "运行 %s" % (mod.split(".")[0] or "脚本"), "files": []} if mod else {"cat": "script", "text": "运行脚本", "files": []}
        script = next((t for t in toks[1:] if not t.startswith("-") and re.search(r"\.(?:py|js|mjs|sh|rb|ts)$", t)), "")
        if script:
            return {"cat": "script", "text": "运行脚本 " + bare_name(script), "files": []}
        return {"cat": "script", "text": "运行了一小段脚本", "files": []}
    if name in ("xay", "xay2"):
        sub = next((t for t in toks[1:] if not t.startswith("-")), "")
        return {"cat": "tool", "text": {"progress": "向系统汇报进度", "done": "向系统提交汇报"}.get(sub, "使用公司的内部工具"), "files": []}
    if name in _NOISE_BINS:
        return {"cat": "noise", "text": "", "files": []}
    return {"cat": "other", "text": "运行了一个命令", "files": []}


def classify_cmd(cmd):
    """一条 shell 命令 → {cat, text, files}。text 是人话，永远不含原始命令。"""
    cmd = redact(cmd or "")
    best, files = None, []
    for part in _split_parts(cmd):
        item = _classify_part(part)
        if item["cat"] in ("read", "write"):
            files.extend(item["files"])
        if best is None or _PRIORITY[item["cat"]] < _PRIORITY[best["cat"]]:
            best = item
    if best is None:
        return {"cat": "noise", "text": "", "files": []}
    out = dict(best)
    if best["cat"] in ("read", "write"):
        seen = []
        for f in files:
            if f and f not in seen:
                seen.append(f)
        out["files"] = seen
    return out


_TOOL_WORDS = (
    (("replace", "write", "edit", "patch", "create_file", "apply", "filechange", "multiedit"), "write"),
    (("read", "view", "open_file", "cat"), "read"),
    (("grep", "glob", "search_code", "find", "list_dir", "ls", "codebase"), "search"),
    (("web", "fetch", "browse", "google", "internet"), "web"),
    (("todo", "plan"), "plan"),
    (("bash", "shell", "run_command", "command", "exec"), "cmd"),
)


def classify_tool(kind, text, tool=""):
    """工具调用 → {cat, text, files}。kind 是适配器给的种类（read/write/cmd/search/web/mcp/other），可以为空。"""
    kind, text, tool = (kind or "").lower(), (text or "").strip(), (tool or "").lower()
    if kind in ("", "other", "mcp"):
        hay = (tool or text).lower().replace("-", "_")
        for words, mapped in _TOOL_WORDS:
            if any(w in hay for w in words):
                kind = mapped
                break
        else:
            kind = "other"
    if kind == "cmd":
        return classify_cmd(text)
    if kind == "read":
        name = bare_name(text) if text and " " not in text.strip() else ""
        return {"cat": "read", "text": "读了文件", "files": [name] if name and name.lower() not in ("read_file", "view_file") else []}
    if kind == "write":
        name = bare_name(text) if text and " " not in text.strip() else ""
        bad = name.lower() in ("write_file", "edit", "replace_file_content", "multi_replace_file_content", "filechange")
        return {"cat": "write", "text": "改了文件", "files": [name] if name and not bad else []}
    if kind in ("search", "web"):
        q = plain(text, 40) if text else ""
        if kind == "web" or any(w in tool for w in ("web", "fetch", "google")):
            return {"cat": "web", "text": "查资料", "files": [q] if q and not _looks_toolname(q) else []}
        return {"cat": "search", "text": "查找内容", "files": []}
    if kind == "plan":
        return {"cat": "plan", "text": "列了计划", "files": []}
    return {"cat": "tool", "text": "使用了一个工具", "files": []}


def _looks_toolname(s):
    return bool(re.fullmatch(r"[a-z_]+", s or ""))


# 工位写直播行时给工具加的前缀（station.TOOL_TEXT）："改文件：calc.py"、"运行命令：python3 …"
_LIVE_PREFIX = {"读文件": "read", "改文件": "write", "运行命令": "cmd", "搜索": "search", "上网": "web", "工具": "other"}
_LIVE_PREFIX_RE = re.compile(r"^(%s)[：:]\s*" % "|".join(_LIVE_PREFIX))


def classify_loose(text, tool_kind=None, tool=""):
    """直播里的一行"工具"：不一定带种类。有种类按种类；带工位前缀的按前缀；都没有就从文字猜——像命令按命令，像文件名按"处理了文件"。"""
    text = (text or "").strip()
    if not tool_kind:
        m = _LIVE_PREFIX_RE.match(text)
        if m:
            tool_kind, text = _LIVE_PREFIX[m.group(1)], text[m.end():].strip()
    if tool_kind:
        return classify_tool(tool_kind, text, tool)
    if not text:
        return {"cat": "tool", "text": "使用了一个工具", "files": []}
    if looks_like_command(text) or any(sym in text for sym in ("&&", "||", "<<", " | ")):
        return classify_cmd(text)
    if " " not in text and (_is_pathish_loose(text) or _EXT.search(text)):
        return {"cat": "file", "text": "处理了文件", "files": [bare_name(text)]}
    return classify_tool("", text, tool)


# ---------------------------------------------------------------- 步骤

ICONS = {"read": "📖", "write": "✏️", "test": "🧪", "check": "🔍", "search": "🔎", "list": "🔎", "web": "🌐", "fetch": "🌐", "think": "💭",
         "say": "💬", "plan": "📋", "git": "🗂️", "commit": "🗂️", "merge": "🗂️", "install": "📦", "files": "🗂️",
         "script": "⚙️", "tool": "⚙️", "other": "⚙️", "file": "📄", "warn": "⚠️", "progress": "📍", "note": "📝"}
_MERGE = frozenset(("read", "write", "search", "list", "web", "file"))
_CHECKED = frozenset(("test", "check"))


def _names_text(names, one, many):
    names = [n for n in names if n]
    if not names:
        return None
    if len(names) == 1:
        return one % names[0]
    shown = "、".join(names[:NAMES_SHOWN]) + ("…" if len(names) > NAMES_SHOWN else "")
    return many % (len(names), shown)


class StepBuilder:
    """把一串"做了什么"攒成人话步骤；连续的读 / 查 / 改合并成一步。"""

    def __init__(self, limit=MAX_STEPS):
        self.limit = limit
        self.items = []
        self._calls = {}

    # ---- 添加 ----
    def _last(self):
        return self.items[-1] if self.items else None

    def think(self, text, ts=0):
        text = plain(drop_english_headers(text))
        if not text:
            return
        last = self._last()
        if last and last["cat"] == "think":
            return                                   # 连续的思考只留第一句
        if mostly_english(text):
            return                                   # 英文思考机器没法确定地翻成人话，不显示（显示一堆"想了想"也没信息）
        self.items.append({"cat": "think", "text": "想：" + cut(first_sentences(text, 1).rstrip("。"), 40), "ts": ts})

    def say(self, text, ts=0):
        text = plain(text)
        if not text or mostly_english(text):
            return
        last = self._last()
        if last and last["cat"] == "say":
            return
        self.items.append({"cat": "say", "text": "说：" + cut(first_sentences(text, 1).rstrip("。"), 40), "ts": ts})

    def note(self, icon_cat, text, ts=0):
        text = plain(text)
        if text:
            self.items.append({"cat": icon_cat, "text": cut(text, 80), "ts": ts})

    def action(self, info, ts=0, call_id=None):
        """info = classify_* 的结果。"""
        cat = info["cat"]
        if cat == "noise":
            return
        last = self._last()
        if cat in _MERGE and last and last["cat"] == cat:
            last["n"] = last.get("n", 1) + 1
            for f in info["files"]:
                if f not in last["files"]:
                    last["files"].append(f)
            last["ts"] = last["ts"] or ts
            if call_id:
                self._calls[call_id] = last
            return
        item = {"cat": cat, "ts": ts, "n": 1, "files": list(info["files"]), "base": info["text"], "ok": None}
        self.items.append(item)
        if call_id:
            self._calls[call_id] = item

    def result(self, call_id, ok=None, exit_code=None):
        item = self._calls.get(call_id)
        if item is None:
            return
        if ok is None and exit_code is None:
            return
        failed = (ok is False) or (isinstance(exit_code, int) and not isinstance(exit_code, bool) and exit_code != 0)
        item["ok"] = not failed

    # ---- 输出 ----
    def _render(self, it):
        cat = it["cat"]
        if "base" not in it:
            return it["text"]
        base, files, n = it["base"], it["files"], it.get("n", 1)
        if cat == "read":
            return _names_text(files, "读了 %s", "读了 %d 个文件：%s") or ("读了文件" if n == 1 else "读了 %d 个文件" % n)
        if cat == "file":
            return _names_text(files, "处理了 %s", "处理了 %d 个文件：%s") or "处理了文件"
        if cat == "write":
            return _names_text(files, "改了 %s", "改了 %d 个文件：%s") or ("改了文件" if n == 1 else "改了文件（共 %d 次）" % n)
        if cat in ("search", "list"):
            return base if n == 1 else "查找了 %d 次文件或内容" % n
        if cat == "web":
            qs = [q for q in files if q]
            if qs:
                return "查资料：" + "；".join(qs[:2]) + ("…" if len(qs) > 2 else "")
            return "查了资料" if n == 1 else "查了 %d 次资料" % n
        if cat in _CHECKED:
            suffix = "" if it["ok"] is None else ("：通过 ✓" if it["ok"] else "：没通过 ✗")
            return base + suffix
        if it["ok"] is False:
            return base + "（没成功 ✗）"
        return base

    def steps(self):
        out = []
        for it in self.items:
            text = self._render(it)
            if text:
                out.append({"icon": ICONS.get(it["cat"], "⚙️"), "text": cut(text, 90), "ts": int(it.get("ts") or 0)})
        return cap_steps(out, self.limit)


def cap_steps(steps, limit=MAX_STEPS):
    if len(steps) <= limit:
        return steps
    head = HEAD_STEPS
    tail = limit - head - 1
    hidden = len(steps) - head - tail
    mid_ts = steps[head]["ts"] if head < len(steps) else 0
    return steps[:head] + [{"icon": "…", "text": "中间还有 %d 步，没有逐条列出" % hidden, "ts": mid_ts}] + steps[-tail:]


def _num_ts(value):
    return value if isinstance(value, (int, float)) and not isinstance(value, bool) else 0


def _error_line(raw):
    raw = (raw or "").strip()
    if raw.startswith("{"):
        try:
            data = json.loads(raw)
            raw = str(data.get("message") or "")
        except ValueError:
            raw = ""
    known = explain_error(raw)
    if known:
        return "遇到问题：" + known
    line = plain(raw)
    if not line or mostly_english(line):
        return "遇到了问题，已记录"
    return "遇到问题：" + cut(first_sentences(line, 1).rstrip("。"), 50)


def steps_from_legacy_events(events, limit=MAX_STEPS):
    """旧系统归一化日志（events.norm.jsonl 的行）→ 人话步骤。"""
    sb = StepBuilder(limit)
    for ev in events:
        if not isinstance(ev, dict):
            continue
        typ, data, ts = ev.get("type"), ev.get("data") or {}, _num_ts(ev.get("ts"))
        if not isinstance(data, dict):
            data = {}
        text = ev.get("text") if isinstance(ev.get("text"), str) else ""
        if typ == "tool.call":
            info = classify_tool(ev.get("kind"), text, str(data.get("tool") or ""))
            if (data.get("tool") or "") in ("todo_list", "plan") or data.get("display_kind") == "plan":
                info = {"cat": "plan", "text": "列了计划", "files": []}
            if info["cat"] == "write" and not info["files"]:
                paths = data.get("paths")
                if isinstance(paths, list):
                    info["files"] = [bare_name(p) for p in paths if isinstance(p, str) and bare_name(p)]
            sb.action(info, ts, call_id=str(data.get("id") or "") or None)
        elif typ == "tool.result":
            sb.result(str(data.get("id") or ""), data.get("ok"), data.get("exit"))
        elif typ == "reasoning":
            sb.think(text, ts)
        elif typ == "message":
            if not data.get("final"):
                sb.say(text, ts)
        elif typ == "error":
            sb.note("warn", _error_line(text or str(data.get("message") or "")), ts)
        elif typ == "warning":
            if data.get("source") == "quota" and text:
                sb.note("warn", text, ts)
    return sb.steps()


_PROGRESS_MARK = re.compile(r"【\s*进度\s*\d+\s*[/／]\s*\d+\s*】")


def steps_from_live_rows(rows, limit=MAX_STEPS, skip_progress=False):
    """直播通道的行（{kind, text, ts, 可选 tool_kind}）→ 人话步骤。思考和回复是片段，连续的拼起来再取第一句。
    skip_progress=True：回复里从【进度 x/y】开始的那部分不再记成"说：…"（账本里的进度事件已经是一步了，免得重复）。"""
    sb = StepBuilder(limit)
    pending_kind, pending_text, pending_ts = None, "", 0

    def flush():
        nonlocal pending_kind, pending_text, pending_ts
        if pending_kind == "thinking":
            sb.think(pending_text, pending_ts)
        elif pending_kind == "text":
            said = _PROGRESS_MARK.split(pending_text, maxsplit=1)[0] if skip_progress else pending_text
            sb.say(said, pending_ts)
        pending_kind, pending_text, pending_ts = None, "", 0

    for row in rows:
        kind = row.get("kind")
        if kind in ("thinking", "text"):
            if pending_kind != kind:
                flush()
                pending_kind, pending_ts = kind, _num_ts(row.get("ts"))
            pending_text += row.get("text") or ""
            continue
        flush()
        if kind == "tool":
            sb.action(classify_loose(row.get("text"), row.get("tool_kind"), str(row.get("tool") or "")), _num_ts(row.get("ts")))
    flush()
    return sb.steps()


# ---------------------------------------------------------------- 直播条

class LiveNarrator:
    """直播行 → 一句大白话（给页面"正在想 / 正在做"条）。思考和回复的片段在这里拼起来；给的是**最新一句**。"""

    def __init__(self):
        self._kind, self._buf = None, ""

    @staticmethod
    def _present(info):
        cat, files = info["cat"], [f for f in info["files"] if f]
        if cat == "read":
            return "读 " + "、".join(files[:2]) if files else "读文件"
        if cat in ("write", "file"):
            return ("改 " if cat == "write" else "处理 ") + "、".join(files[:2]) if files else "改文件"
        if cat == "web":
            return "查资料" + ("：" + files[0] if files else "")
        if cat == "plan":
            return "列计划"
        return info["text"]

    def feed(self, item):
        kind = item.get("kind")
        text = item.get("text") or ""
        if kind in ("thinking", "text"):
            self._buf = (self._buf + text) if self._kind == kind else text
            self._kind = kind
            self._buf = self._buf[-2000:]
            t = plain(drop_english_headers(self._buf))
            if not t or mostly_english(t):
                return "正在思考……" if kind == "thinking" else "正在写回复……"
            last = sentences(t)
            return cut(last[-1].rstrip("。") if last else t, 50)
        self._kind, self._buf = None, ""
        if kind == "tool":
            return self._present(classify_loose(text, item.get("tool_kind"), str(item.get("tool") or "")))
        return None


# ---------------------------------------------------------------- 交代的事 / 结果

_LABEL = re.compile(r"^\s*(背景|任务|目标|说明|需求|要求|上下文|前提|备注|允许改的范围|验收|约束|规则|汇报|注意)\s*[：:]\s*")
_REPORT_LABELS = ("改动文件", "验证", "问题与需要负责人决定的事", "问题", "汇报", "说明", "备注")


def _task_section(prompt):
    prompt = redact(prompt)
    m = re.search(r"^#\s*任务\s*$", prompt, flags=re.M)
    body = prompt[m.end():] if m else prompt
    cut_at = re.search(r"^\s*(?:---+|#\s+\S.*)$", body, flags=re.M)
    if cut_at:
        body = body[:cut_at.start()]
    return body.strip()


def ask_from_prompt(prompt, limit=3):
    """任务卡 → 负责人交代的事：取"# 任务"那一节里"任务："开头的那段，大白话，最多 limit 句。"""
    body = _task_section(prompt or "")
    if not body:
        return ""
    paras = [p for p in re.split(r"\n\s*\n", body) if p.strip()]
    chosen = None
    for i, p in enumerate(paras):
        m = _LABEL.match(p)
        if m and m.group(1) == "任务":
            chosen = _LABEL.sub("", p, count=1)
            # 任务段后面紧跟的、没有标签的段落一起带上，遇到下一个标签就停
            for q in paras[i + 1:]:
                if _LABEL.match(q):
                    break
                chosen += "\n" + q
            break
    if chosen is None:
        rest = [p for p in paras if not (_LABEL.match(p) and _LABEL.match(p).group(1) in ("背景", "允许改的范围", "验收", "约束", "规则"))]
        chosen = _LABEL.sub("", (rest or paras)[0], count=1)
    text = drop_parentheses(plain(chosen))
    text = "\n".join(line for line in text.splitlines() if not _LABEL.match(line))
    return first_sentences(re.sub(r"[ \t]{2,}", " ", text), limit, each=70)


_RESULT_LINE = re.compile(r"^\s*(?:【结果】|结果\s*[：:])\s*(.*)$")
_ONLY_NAME = re.compile(r"^[\w.\-]+\.\w{1,8}$")


def claim_from_text(text):
    head = (text or "").strip()[:30]
    if re.match(r"(部分完成|没做完|未完成|没有完成|做了一部分)", head):
        return "partial"
    if re.match(r"(卡住|失败|没做成|无法完成|做不了)", head):
        return "blocked"
    if re.match(r"(完成|已完成|做完|成功)", head):
        return "done"
    return ""


def summary_from_report(text, limit=3):
    """汇报原文 → (claim, 大白话摘要)。摘要 = "结果"那一行 + 后面最多 limit-1 句有意义的话。"""
    raw = redact(text or "")
    if not raw.strip():
        return "", ""
    lines = strip_markdown(_FENCE.sub("\n", raw), keep_ticks=True).splitlines()
    result_at, claim_text = None, ""
    for i, line in enumerate(lines):
        m = _RESULT_LINE.match(line)
        if m:
            result_at, claim_text = i, m.group(1).strip()
            break
    picked, section = [], ""
    if result_at is not None and claim_text:
        picked.extend(sentences(plain(_inline_code(claim_text))))
    start = (result_at + 1) if result_at is not None else 0
    for line in lines[start:]:
        if len(picked) >= limit:
            break
        s = line.strip()
        if not s:
            continue
        lm = re.match(r"^(%s)\s*(?:[（(][^）)]*[）)])?\s*[：:]\s*(.*)$" % "|".join(map(re.escape, _REPORT_LABELS)), s)
        if lm:
            section, s = lm.group(1), lm.group(2).strip()
            if section == "改动文件" or not s:
                continue
        elif section == "改动文件":
            continue
        s_plain = plain(_inline_code(s))
        if not s_plain or s_plain in ("无", "无。", "没有") or _ONLY_NAME.match(s_plain) or len(s_plain) < 6:
            continue
        if section.startswith("问题") and s_plain.startswith("无"):
            continue
        picked.extend(sentences(s_plain))
    claim = claim_from_text(claim_text) if claim_text else ""
    out = [cut(p.rstrip("。.；;，, "), SENTENCE_MAX) for p in picked[:limit] if p]
    if result_at is None and not out:
        return claim, first_sentences(plain(raw), limit)
    return claim, ("。".join(out) + "。") if out else ""


# ---------------------------------------------------------------- 详情栏用的短句（标题 / 进度 / 出错原因 / 关键几步 / 工作笔记）

TITLE_MAX = 60
# 写给 AI 的要求（"汇报时第一句先说……"、"不要改别的文件"），人不需要看
_INSTRUCTION = re.compile(r"^\s*(?:汇报|回报|回复|报告|完成后|做完后|做完以后|最后|注意|要求|请(?:你|在|务必)|记得|另外|别忘|输出|结尾|"
                          r"开头|第一句|顺便|不要|不许|禁止|只改|只许|允许|务必|必须)")
_CLAUSE_SPLIT = re.compile(r"(?<=[，,；;])")


def _drop_instructions(sentence):
    """一句话里从"汇报时……/不要……"这种小句开始，后面都是写给 AI 的要求，截掉。"""
    out = []
    for clause in _CLAUSE_SPLIT.split(sentence):
        if _INSTRUCTION.match(clause):
            break
        out.append(clause)
    return "".join(out).rstrip("，,；; ")


def short_title(title, limit=TITLE_MAX):
    """任务标题 → 给人看的短标题：只留第一句正事，去掉写给 AI 的要求、括号里的补充、【进度】标记和反引号。"""
    text = plain(title or "")
    for sent in sentences(text):
        if _INSTRUCTION.match(sent):
            continue
        core = squash(drop_parentheses(_drop_instructions(sent))).rstrip("。.！!？?；;，,：: ")
        if core:
            return cut(core, limit)                 # 和 utils.js 的 shortTitle 截法一样（超长时留 limit-1 个字加"…"）
    return cut(squash(drop_parentheses(text)), limit) if text else ""


_STEP_PREFIX = re.compile(r"^\s*第\s*\d+\s*步(?:\s*/\s*共\s*\d+\s*步)?\s*(?:已完成|完成了|做完了|已做完)?\s*[：:，,、]?\s*")


def progress_text(text, limit=50):
    """员工报的进度 → 一句话：去掉【进度 3/4】和开头重复的"第 2 步："，只留第一句。"""
    body = _STEP_PREFIX.sub("", plain(text or ""))
    return first_sentences(body, 1, each=limit).rstrip("。") if body else ""


_ERRORS = (
    (r"at capacity|overloaded|server (?:is )?busy|\b529\b|服务器繁忙|满载", "AI 那边太忙（满载了）"),
    (r"rate.?limit|too many requests|\b429\b|限流", "用得太快，被暂时限流了"),
    (r"quota|usage limit|insufficient|out of credits?|额度", "额度用完了"),
    (r"context (?:length|window)|maximum context|too many tokens|prompt is too long|记忆满", "记忆装不下了"),
    (r"timed? ?out|timeout|deadline exceeded|超时", "等太久，超时了"),
    (r"unauthori[sz]ed|\b401\b|not logged in|log ?in required|登录", "登录过期了"),
    (r"permission denied|operation not permitted|\b403\b|forbidden|没有权限", "没有权限"),
    (r"network|connection (?:refused|reset|error)|ECONN|ENOTFOUND|网络", "网络连不上"),
)
_ERRORS_RE = [(re.compile(p, re.I), t) for p, t in _ERRORS]


def explain_error(raw):
    """常见的英文报错 → 一句大白话；认不出来返回空串。"""
    raw = redact(raw or "")
    for pattern, text in _ERRORS_RE:
        if pattern.search(raw):
            return text
    return ""


def why_failed(raw):
    """没做成的原因：认得出的报错翻成人话；中文原因取第一句；英文看不懂的不硬翻。"""
    known = explain_error(raw)
    if known:
        return known
    line = first_sentences(plain(raw or ""), 1, each=50).rstrip("。")
    return "" if not line or mostly_english(line) else line


_CLAIM_ONLY = re.compile(r"^(?:完成|已完成|做完了?|已做完|成功|部分完成|没做完|未完成|卡住了?|失败了?)$")


def one_line(summary, limit=70):
    """结果摘要 → 一句话：跳过只有"完成。"两个字的那句。"""
    sents = [s.rstrip("。.；;，, ") for s in sentences(summary or "")]
    for s in sents:
        if s and not _CLAIM_ONLY.match(s):
            return cut_clause(s, limit) + "。"
    return (sents[0] + "。") if sents and sents[0] else ""


_KEY_ICONS = frozenset(ICONS[c] for c in ("write", "test", "check", "commit", "install"))
_SKIP_ICONS = frozenset(ICONS[c] for c in ("think", "say", "warn", "progress", "note")) | {"…"}


def key_steps(progress_texts, steps, limit=3):
    """关键几步（最多 limit 条）：优先用员工自己报的进度（已经是人话）；没有就从步骤里挑改文件、跑检查这些。"""
    picked = []
    for t in progress_texts or []:
        if t and t not in picked:
            picked.append(t)
    if picked:
        return picked if len(picked) <= limit else [picked[0]] + picked[-(limit - 1):]
    main = [s["text"] for s in steps or [] if s.get("icon") in _KEY_ICONS]
    rest = [s["text"] for s in steps or [] if s.get("icon") not in _KEY_ICONS and s.get("icon") not in _SKIP_ICONS]
    out = []
    for t in main + rest:
        if t not in out:
            out.append(t)
        if len(out) >= limit:
            break
    order = {s["text"]: i for i, s in enumerate(steps or [])}
    return sorted(out, key=lambda t: order.get(t, 0))


_TASK_ID = re.compile(r"\s*[（(]\s*(?:tk|tn|run|em|dc)_[0-9A-Za-z_\-:]+\s*[）)]")
_QUOTED = re.compile(r"「([^」]+)」")


def notes_text(text, limit=600):
    """员工的工作笔记 → 人话：去掉任务编号，引号里的任务名换成短标题，每行过一遍大白话。"""
    raw = _TASK_ID.sub("", redact(text or ""))
    raw = _QUOTED.sub(lambda m: "「%s」" % (short_title(m.group(1)) or m.group(1)), raw)
    lines = [plain(line) for line in raw.splitlines()]
    return cut("\n".join(line for line in lines if line), limit)


_PATH_TOKEN = re.compile(r"[^\s]*[/\\][^\s]*")
_CODE_FILE_EXT = re.compile(r"\b[A-Za-z0-9_\-]+\.[A-Za-z][A-Za-z0-9]{0,7}\b")
_FUNC_CALL = re.compile(r"\b[A-Za-z0-9_]+\s*\([^)]*\)")
_SNAKE_CASE = re.compile(r"[A-Za-z]+_[A-Za-z0-9_]*|_[A-Za-z0-9]+")
_CAMEL_CASE = re.compile(r"[a-z]+[A-Z][A-Za-z0-9]*|\b[A-Z][a-z]+[A-Z][A-Za-z0-9]*")
_COMMAND_WORD = re.compile(
    r"\b(git|python|python3|pytest|npm|node|unittest|pip|cargo|make|bash|sh|curl|wget|docker|xay2|grep|cat|cd|ls|rm|mkdir)\b",
    re.IGNORECASE,
)


def explain_title_code_like(title):
    """判断标题像不像代码。合规返回 None；不合格返回像代码的片段或原因。"""
    if not isinstance(title, str):
        return "标题不是字符串"
    t = title.strip()
    if not t:
        return "标题为空"
    if len(t) > 30:
        return f"超过 30 个字（共 {len(t)} 字）"
    if "`" in t:
        return "`"
    m_path = _PATH_TOKEN.search(t)
    if m_path and ("/" in m_path.group(0) or "\\" in m_path.group(0)):
        return m_path.group(0)
    m = _CODE_FILE_EXT.search(t)
    if m:
        return m.group(0)
    m = _FUNC_CALL.search(t)
    if m:
        return m.group(0)
    m = _SNAKE_CASE.search(t)
    if m:
        return m.group(0)
    m = _CAMEL_CASE.search(t)
    if m:
        return m.group(0)
    m = _COMMAND_WORD.search(t)
    if m:
        return m.group(0)
    return None


def is_plain_title(title):
    return explain_title_code_like(title) is None


def is_plain(text):
    """判断像不像大白话：
    不能含 / 或 . 加扩展名的文件名、反引号、下划线连起来的英文、驼峰英文单词、超过 80 个字。
    """
    if not isinstance(text, str):
        return False
    t = text.strip()
    if not t or len(t) > 80:
        return False
    if "/" in t or "\\" in t or "`" in t:
        return False
    if _CODE_FILE_EXT.search(t):
        return False
    if _FUNC_CALL.search(t):
        return False
    if _SNAKE_CASE.search(t):
        return False
    if _CAMEL_CASE.search(t):
        return False
    if _COMMAND_WORD.search(t):
        return False
    return True


looks_like_plain = is_plain

