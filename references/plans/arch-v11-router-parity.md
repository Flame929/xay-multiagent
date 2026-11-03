# 第十一版：补上同类项目的长处 + 总进度条

> 2026-11-03 · 负责人 Claude · 用户批准。对照的同类项目：dazhi666666/agent-router（主项目）、dazhi666666/agent-router-mcp（任务板独立版）。
> **借鉴说明**：xay 是用户自己的设计；这一版有几处小功能借鉴了 agent-router / agent-router-mcp / Jinlong75/AgentRouter 的思路（没有复制代码），公开说明见 README「致谢」。
> 本文只写新增和变化的部分；没提到的照 `arch-v10-rebuild.md` 和 `V2/docs/protocol.md`。

## 用户原话要点

- "他比我强的都加上进行优化"，"然后总进度条也要加上"。
- 进度条："多线 AI 一起攒一个进度条，进度条越满就说明任务即将干完"。
- 自动领活："子 agent 是不是会自动干活？我的是不是不会？必须要等指令？可以加入一个自动领活的机制吗？"

对方比 xay 强的五点（上一轮对比的结论）：① 子 AI 的结果直接送进主 AI 的对话；② 主 AI 可以换，聊到一半也能换；③ 接的 AI 更多；④ 子 AI 能直接给别的子 AI 派活；⑤ 上手简单。另外顺带补两件：⑥ 员工也能看到整块任务板；⑦ 没写用哪种 AI 的活也能自动派出去。

## 不做什么

- **OpenCode、ZCode、Devin 只预留、不安装**：预设写好、用假命令行测通，用户以后装了就能用（用户 2026-11-03："opencode 和 zcode 都预留就行了，不用真安装"）。
- **不让 AI 自己去抢活**（对方 MCP 版的 claim_task + 轮询信箱）：要每个 AI 一直开着、反复查信箱，查漏了活就一直挂着。xay 改成由管家替空着的员工"领"（推，不是拉），效果一样，更可靠。
- **不做"只会聊天的模型套壳"**：dsh 本身就是给 DeepSeek 套的干活外壳，已经覆盖。
- **不自动改用户的 Claude 设置**：推送用的钩子只有用户点头后才由 `xay2 hook install` 写进项目的 `.claude/settings.local.json`。
- 不改旧系统（V1）、不改旧看板的样子（进度条只是加一条，不动其他布局）。

## 1. 自动领活（管家替空着的人领）

现状：`ready` 的活既没写 `--to`、也没写 `agent`（`plan add` 的 `--agent` 默认 `auto` 会被当成"没写"），就一直留着等负责人分配。

改成：这种活由管家**先挑一种 AI，再走原来的选人三步**。挑 AI 的规则（取第一条成立的）：

1. 同一个小任务（part）里别的活用过的 AI（不算撤回的），用得最多的那种。
2. 有**空着**的员工（idle，没被排除）的 AI 种类；多种都有空人时按 `dsh → agy → codex` 的顺序（便宜的先用；Codex Plus 每周额度紧）。只读活（`readonly`）不选 codex。
3. 还有空名额（能招新人）的种类，同样按上面的顺序。
4. 都满了：排队最短的那种。

被排除的种类：额度线路全用完、今天被停、提供者表里不可用（命令行没装）。挑出来后写进 `task.assigned` 的大白话理由，例如"没指定用哪种 AI，交给空着的 Gemini 2"。

"做完马上接下一件"不用新做：员工做完回到空闲，管家下一拍就按选人三步（依赖 > 文件 > 部门）把同组的下一件交给他。

## 2. 结果主动送到负责人那里

负责人在 Claude 桌面版里，外面的程序没法往它的对话里塞话，所以做两条路：

- **后台等**（不改配置，默认）：`xay2 wait --timeout` 上限放宽到 3600 秒。SKILL.md 3.3 写明：Claude 负责人派完活后把 `xay2 wait --since <游标> --timeout 3000` 放后台跑（Bash `run_in_background`），自己接着干别的；有要紧事它就退出，桌面版自动把结果交回给负责人。
- **钩子插话**（可选，要用户同意才装）：新命令 `xay2 notify --hook`：读当前目录对应的、最近一个没结束的对话，找出上次通知之后的要紧事（同 `wait` 的筛选：需要你、申请、给总经理的信、做完 / 没做成 / 改动先保留、警告），每条一行大白话，按 Claude Code 钩子要的 JSON 打印（`hookSpecificOutput.additionalContext`）；没有新事就什么也不打印、退出码 0。通知游标存在 `$XAY2_HOME/notify/<对话编号>`，和 `wait` 的游标互不影响。`xay2 hook install|uninstall|status` 只往**当前项目**的 `.claude/settings.local.json` 加 / 删一条 `PostToolUse` 钩子（命令是 `xay2 notify --hook`），不碰别的设置。

## 3. 负责人可以换、可以中途交接

SKILL.md 本来就允许 Codex / dsh / agy 当负责人。新增：

- `xay2 handoff [--conv cv_…] [--to codex|dsh|agy|claude]`：打印一份 ≤60 行的交接单——大任务标题、总进度、每个小任务和每件活的状态与负责的人、没处理的"需要你" / 申请 / 信、最近 10 件事、当前游标、新负责人接手要跑的第一条命令（`xay2 wait --conv … --since …`）。写了 `--to` 就记一条新事件 `conversation.lead_changed {from, to}`（by=lead），对话的 `lead_kind` 改成新的，页面上的总经理头像跟着换。
- `xay2 brief` 第一行后面加"总进度 63%"。

## 4. 接更多 AI：通用命令行员工

新适配器 `generic`：任何有"非交互模式"的命令行 AI，写一份声明就能当员工，不写代码。

- 声明放 `$XAY2_HOME/agents.json`（`{"agents": {"opencode": {...}}}`），代码里自带四份**预设**：`claude`（Claude Code，用户 2026-11-03 决定可以当员工，但**只预留接口、不和负责人共用订阅**：用独立配置目录 `$XAY2_HOME/claude-employee/`（环境变量 `CLAUDE_CONFIG_DIR`）跑，看不到本机已登录的订阅；只认用户在本机配的 API 登录（`agents.json` 里写环境变量名，如 `ANTHROPIC_API_KEY`），没配就算"没登录"、管家不给它派活；`claude -p --output-format stream-json --verbose`，续接用 `--resume <session_id>`，只读活用 `--permission-mode plan`，干活用 `--permission-mode bypassPermissions`，限在员工自己的桌子里）、`opencode`、`devin`、`zcode`（后三个只预留：写好命令格式，装没装、登没登录由 `xay2 doctor` 检查）。
- 声明字段：`label`（页面显示名，如"OpenCode"）、`exe`、`args`（参数模板，占位符 `{prompt}`、`{prompt_file}`、`{model}`、`{session}`）、`resume_args`（续接同一个对话的参数模板，没有就每轮开新对话、把上一轮汇报附进输入）、`output`（`text` 或 `jsonl`；jsonl 时写 `session_field`、`text_field`、`usage_field` 这几个取值路径）、`model`、`env`（只许写变量名映射，不许写密钥值）、`max_concurrent`（默认 2）、`readonly_args`（只读活额外加的参数，没有就不接只读活）。
- 新种类的名字就是声明的键（`opencode` 等），和 codex / dsh / agy 并列：`plan add --agent opencode`、员工显示名"OpenCode 1"。额度不测（显示"不查额度"），出错按退出码和输出里的关键词归类（`quota` / `rate_limit` / `auth` / `network` / `other`）。
- 不支持中途插话（steer），叫停照常（结束进程组）。
- 已经写死的 `agent: codex|dsh|agy` 校验放宽为"提供者表里有的种类"。

## 5. 员工之间直接派活

执行者新增命令 `xay2 plan add "标题" [--agent X | --to 员工] [--readonly] [--writes …] --card -`：只写发件箱（`{type: "task"}`），工位检查后以 `task.added`（by=station）写进账本，然后照常排程。限制：

- 只能派进**自己这件活所属的小任务**，不加任何依赖（派出去的是能同时做的独立部分，不等派活的人做完）；`by_task` 记下是谁派的。
- 一件活最多派 3 件；员工派出来的活里不能再派（只一层）。
- 要改文件的必须写 `--writes`，而且不能和自己这件活的 `writes` 重叠（避免两人改同一批文件）；只读活不限。
- 计划版本 +1，历史里写"Codex 1 派了「X」给 Gemini（理由）"。负责人的 `wait` 把它当要紧事返回一次。

## 6. 员工也能看整块任务板

工位每轮开工时在员工的输出目录写一份 `board.md`（环境变量 `XAY2_BOARD_FILE`）：本对话的计划树（小任务 → 每件活的标题、状态、谁在做、第几步），≤40 行，标出"你在做的是这件"。执行者用 `xay2 board` 打印它。员工手册加一句：要和别人配合时先看板。

## 7. 上手简单：一条命令开工

`xay2 up`：先跑一遍体检（只打印没通过的项和"能用的 AI：codex、dsh、agy、opencode…"），管家没开就开，最后打印看板和公司页地址、下一步怎么做（`xay2 begin --title …`）。`xay2 doctor` 加查 agents.json 里各个通用员工的命令装没装。

## 8. 总进度条

**口径**（快照里算好，页面不自己算）：本对话没撤回的活每件 1 份；做完 = 1；正在做 = `min(0.9, step/of)`（没有步数时 0.1）；排队、没开始、没做成 = 0；改动先保留（kept）= 0.9。总进度 = 合计 ÷ 件数。每份按做这件活的 AI 种类记账，所以总进度条能拆成"每种 AI 攒了多少"。

快照（`/api/v2/snapshot` 与 `/api/state` 里每个对话）新增：

```json
"progress": {
  "pct": 63,                      // 0~100 的整数
  "done": 5, "all": 9, "working": 2,
  "by_agent": [{"agent": "codex", "label": "Codex", "pct": 30}, {"agent": "agy", "label": "Gemini", "pct": 22}],
  "by_part": [{"id": "pt_…", "title": "…", "pct": 80}],
  "text": "9 件活做完 5 件，2 件在做"
}
```

`by_agent[].pct` 是这种 AI 攒的份额占**全部**的百分比，加起来等于总的 `pct`（四舍五入后最后一段补差）。没有活时 `pct = 0`、`all = 0`。

**页面**（Claude 子代理做，外观按现有设计语言）：
- 公司页顶部一条总进度条：一条横条，按 `by_agent` 分颜色段（每种 AI 用它头像的主色），右边"63%"和 `text`；数字变化时平滑涨上去，做完 100% 时轻轻闪一下。
- 旧看板（7878）对话标题下方同样一条细一些的进度条。
- 小任务卡片上已有的"做完 x/y 件"旁边加一条小进度条（用 `by_part`）。

## 约束

- 改协议的活：说明写成 `V2/docs/changes/2026-11-03-<任务 key>.md`，负责人验收时合进 `protocol.md`。
- 快照、接口只加字段不删字段。
- 所有面向用户的文字用大白话中文。
