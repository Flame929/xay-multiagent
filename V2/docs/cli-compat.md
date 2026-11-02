# 三家 CLI 的兼容性与优化空间（2026-10-30 调研）

范围：Codex CLI 0.165.0（`codex app-server`）、DeepSeek Harness `dsh` 0.2.1-rc.2（headless）、Antigravity `agy` 1.4.11（stream-json）。
方法：只跑了 `--help`、`--version`、`generate-json-schema`、`dsh --dump-config`（只在内存里解析、没打印）、`dsh --dump-config-schema`、`agy models`、`agy changelog`，读了本机安装包里的文档，查了官方网页。**没有任何模型调用、没有花额度。**
标记：【schema】本机生成的协议文件；【help】本机帮助文字；【包内文档】dsh 安装包自带 README；【网页】官方网页；【推算】按文档公式算出、没实测；【未验证】没做过实验，别当成事实用。

## M3 更新（2026-10-30 晚，实测 / 本机日志核对后定下的）

| 原来写的 | 现在查清的 | 代码里怎么做的 |
|---|---|---|
| dsh 自动压缩点约 55%（按 1,000,000 窗口推算） | **都不对**：用户的 dsh 路由是手写的 `opencode-xay`，没写窗口，dsh 按 pi-ai 默认 **262,144** 算；自动压缩点 = floor(min(W×0.8, W−O−65,536))，本机 O=0 → **196,608（75%）**。本机 dsh 会话日志里 70 次总结压缩 + 55 次只裁剪工具输出，触发前的用量都在约 19.6 万【本机日志】 | 窗口改成开工前从 `dsh --dump-config` 里用户路由的配置读（适配器 `_preflight`，0.1 秒、不调模型）；V2 门槛保持 55%（≈14.4 万）或 30 万 token 谁先到，正好在 dsh 自己的 75% 之前，不会压两次 |
| dsh 记忆条突降 30% 判为它自己压缩 | 按"上一步"算，真实压缩里有 42/70 次只掉了 12%~29%（触发看的是加上新工具输出后的压力），裁剪工具输出掉 16%~27%；没压缩却掉 10% 以上只有 1 次（2.9 万处）【本机日志】 | 改成"掉 10% 以上、且掉之前 ≥40%" |
| `--patch` 条目 id 写错只警告（§3 无头可靠性） | 实测：警告格式 `dsh: [<补丁路径>] patch: entry "…" not found`；**用户自己 profile 每次都有 6 条同类警告**，所以只能认我们自己补丁路径的那几条 | 预检里出现就 `AdapterConfigError` 拒绝开工；同时核对生效的模型是 deepseek-v4.1-flash、`agent-default-model` 的标题注释里有我们的补丁；每轮 stderr 也盯着 |
| agy `/compact` 小上下文看不出效果 | **大上下文好用**：65,211 → `/compact`（checkpoint 步骤，7 秒，SUCCESS 空回复）→ 下一轮 16,889，暗号还记得【实测 3 次调用】 | `native_compact=True`，先原生、不行再交接；同一段记忆原生压满 2 次改走交接 |
| agy 窗口估 1,048,576、已用 = input_tokens | token-optimizer 调研：三档真实窗口 256,000；input_tokens 不含缓存读取 | 窗口 256,000（仍标估算）；已用 = input + cache_read |
| agy deny 规则【未验证】 | 没实测 deny 对带参数命令是否匹配 | 临时 settings 总带 `deny: [command(git commit), command(git push)]`（有放行命令或 full 模式时才传 --settings）；事后照样用桌子 HEAD 核对 |
| agy 额度报错识别不到 | 适配器已按 `event` 键读；分类正则补了 429 / RESOURCE_EXHAUSTED / rate limit / billing 等写法 | 额度用完 → Antigravity Tools 命令行换号（`list` / `switch <序号> --target agy`），同一个对话重做一次 |

## 1. 一页结论

**三家共同的三件事（先做，因为它们决定"CLI 一升级 V2 会不会悄悄坏"）**
1. `providers.probe_versions` / `version_drift` 已经写好，但**启动流程里没有任何地方调用它**（只有测试在用），`Capabilities.cli_version` 也没填。今天的现实：Codex 官方已发 0.166.0（10-29），本机 0.165.0 且带自动更新；dsh 官方 `latest` 已是 rc.5，本机 rc.2；agy 的 `changelog` 只到 1.4.10。
2. 版本号精确匹配会天天告警（dsh 两周内发了 5 个 rc，`latest` 标签就指向最新的 rc）。应改成"版本只提示、**契约检查**才决定能不能用"（第 5 节）。
3. 三家都有"钩子"（hooks），但只有 agy 真正需要它来做"中途送信"；Codex 有原生 `turn/steer`，dsh 没有钩子。

**Codex（排序）**
1. 钉住版本：用 `~/.codex/packages/standalone/releases/0.165.0-aarch64-apple-darwin/bin/codex` 这个绝对路径（`XAY_CODEX_BIN`），启动时生成 schema 比对契约，再决定是否接受 0.166.0。
2. 把原生压缩"调成我们要的样子"：配置键 `model_auto_compact_token_limit`（钉住 Codex 自己的自动压缩点，避免和 V2 的 60% 撞车）和 `compact_prompt`（让原生压缩直接写出七节交接摘要）。键在二进制里都存在，效果【未验证】。
3. `turn/start.outputSchema`：让员工的汇报变成结构化 JSON（claim / summary / 改过的文件），不再靠解析 `【进度 x/y】` 文字。
4. 把已规划但还没接的接上：`thread/queue/add`（忙时排队）、`thread/fork`（可带 `lastTurnId`/`beforeTurnId`，能"退回某一轮重做"）、`turn/settings/update`（干到一半改强度/模型）。
5. 预算：先用 `thread/tokenUsage/updated` 自己累计 + `turn/interrupt`；`thread/goal/set{tokenBudget}` 会让 Codex 自己"接着干到完成"，可能多开几轮，先探针再决定。

**dsh（排序）**
1. **更正一个假设**：按包内文档的公式，deepseek-v4.1-flash 的自带自动压缩点是 `min(窗口×0.8, 窗口−输出预留−64k)` = min(800,000, 1,000,000−384,000−65,536) = **约 550,464 token（≈55%）**，不是 80%【推算】。这和 V2 的 55% 门槛几乎重合，会"压两次"。用我们已经在传的 `--patch` 加一条 `compaction-basic` 配置就能钉住。
2. 因此 dsh 其实**有可调的原生压缩**（LLM 摘要，保留最近 16%），headless 里它自己就会跑；V2 只需要管门槛并靠"记忆条突降"识别，交接压缩退为兜底。
3. 权限：`DSH_PERMISSION_MODE`（read-only / workspace-write / danger-full-access）按任务卡传给每个员工；现在没传，靠用户全局配置的默认值。
4. 补上 dsh 的计划步骤：它自带 `todo_write` 工具，每次整表替换，可直接翻译成 `plan` 事件（现在 `plan=False`）。
5. 长期：`dsh --profile acp`（标准 ACP v1，一进程多会话、`session/cancel`、思考/用量更新）能省掉每轮约 10 秒冷启动和约 340MB 内存；但没有 steer / 计划 / 压缩，且模型要由客户端选，需要 V2 自己卡死 deepseek-v4.1-flash。后置评估。

**agy（排序）**
1. 钩子送信：工作区级 `<桌子>/.agents/hooks.json`（不碰用户全局配置）里，`PreInvocation`/`PostInvocation` 可以返回 `injectSteps:[{"userMessage":"…"}]`，`Stop` 可以返回 `{"decision":"continue","reason":"…"}`。这是 agy 唯一可能的"中途送信"通道，另一个是设置项 `queuedMessages:"send-immediately"`。都【未验证】在 stream-json 下是否生效，要一次 flash-low 小探针。
2. 用权限 deny 规则把"不许 git commit / push"落成硬规则：临时 settings 里加 `permissions.deny:["command(git commit)","command(git push)"]`（deny 优先于 allow；对带参数命令是否匹配【未验证】）。现在只写了 allow。
3. 原生 `/compact`：官方在 1.4.6–1.4.10 一直在修它（保留附件、子代理任务、压缩后大小口径），探针里出现过 `checkpoint` 步骤。M3 在大上下文里真测，好用就把 `native_compact` 改为 True。
4. 零成本体检：`agy -p "/usage" --output-format json`（额度）、`agy models`（应仍列出 gemini-3.8-flash-low/medium/high 三个），可以放进启动自检。
5. `--json-schema`（根必须是 object，stream-json 下只约束最终 result）是**启动参数**，对"一个进程多轮"的热续接等于整个进程固定一份格式，只适合一次性的审查类任务。

## 2. Codex 能力表（app-server，【schema】来自 `generate-json-schema --experimental`：176 个请求方法、85 个通知、11 种服务端请求）

| 能力 | CLI 怎么支持 | V2 现在用了吗 | 建议 |
|---|---|---|---|
| 开始 / 续接 / 分叉 | `thread/start`、`thread/resume`（`excludeTurns`）、`thread/fork`（`lastTurnId`/`beforeTurnId`/`ephemeral`）、`thread/revert`、`thread/turns/list` | 用了 start / resume；没用 fork | M3 接 fork：同一个人"同时干两件"或退回重做 |
| 排队 | `thread/queue/add/list/update/delete/reorder/start` + 通知 `thread/queue/changed`（实验性；临时线程会报错）；命令行 `codex queue --thread X --message …`（0.166 加了 `--resume`） | 没用（适配器忙时抛 `AdapterBusy`） | M2 接：员工忙时来活直接排进 Codex 自己的队列 |
| 中途插话 | `turn/steer {expectedTurnId}`，下一次模型采样生效，不打断正在跑的命令【实测】 | 用了（`steer()`） | 保持；`expectedTurnId` 过期要回退成 `turn/start`；压缩那一轮会被拒（`ActiveTurnNotSteerable`） |
| 打断 | `turn/interrupt`，0.8 秒收到 `interrupted`【实测】 | 用了 | 保持 |
| 原生压缩 | `thread/compact/start`（独立一轮，`contextCompaction` 条目）；自动压缩点用配置键 `model_auto_compact_token_limit`，摘要提示词用 `compact_prompt` / `experimental_compact_prompt_file`；钩子 `PreCompact`/`PostCompact`（`trigger: manual\|auto`，`continue:false` 可拦截） | 用了手动压缩；**没钉自动压缩点**，`providers.auto_compact_at=None` | M2 加 `-c model_auto_compact_token_limit=…` 与 `-c compact_prompt=…` 并探针；0.166 说"恢复/分叉后保留思考 token 记账，避免过早压缩" |
| 记忆条数字 | `thread/tokenUsage/updated`：`last.totalTokens` ÷ `modelContextWindow`（`total` 是累计，不能用） | 用了 | 保持 |
| 思考直播 | `item/reasoning/summaryTextDelta`（要求 `summary` 非 `none`，取值 auto/concise/detailed）；`show_raw_agent_reasoning` 打开后走 `item/reasoning/textDelta`；`hide_agent_reasoning` 相反 | 用了：`-c model_reasoning_summary="detailed"` + 每轮 `summary`，**是否真出摘要【未验证】**（探针日默认配置下是 0 条） | M2 探针三个值：detailed / auto / `show_raw_agent_reasoning=true`；`model/list` 里查不到"是否支持摘要"（已看过 Model 字段），只能实测 |
| 计划 / 待办 | `turn/plan/updated`、`item/plan/delta`、`turn/diff/updated` | 用了 plan；小任务没触发 | 保持，进度标记仍保留兜底 |
| 结构化最终输出 | `turn/start.outputSchema`（JSON Schema，约束最终回复）；`codex exec --output-schema` | **没用** | M2/M3 用来做汇报格式 |
| 权限 / 沙箱 | `thread/start.sandbox` = read-only / workspace-write / danger-full-access；每轮 `sandboxPolicy`（含 `networkAccess`、`writableRoots`）；新的命名权限档 `permissions` + `permissionProfile/list`；`approvalPolicy`；`approvalsReviewer: auto_review` | 用了前三项，approval 固定 `never` | 命名权限档【未验证】，暂不动；"不许 commit/push"用 `PreToolUse` 钩子或 `codex execpolicy` 规则（`prefix_rule`，`forbidden`）【未验证能否按会话加载】，另在桌子上用 `git rev-parse HEAD` 事后核对 |
| token / 时间预算 | `thread/goal/set {objective, tokenBudget, status}`；状态 active / paused / blocked / usageLimited / budgetLimited / complete；通知 `thread/goal/updated`；配置 `[goals] max_goal_token_budget`；`features.token_budget`、`features.rollout_budget`（0.166 "advisory rollout budgets"）仍是"开发中" | 没用 | M4：先自己累计 `last.totalTokens` + `turn/interrupt`；goal 做探针（会不会自动续跑多轮）；开发中的两个别依赖 |
| 记忆功能 | `thread/memory/read`（返回摘要）、`thread/memoryMode/set {enabled\|disabled}`、`memory/status`；`features.memories` 默认开 | 没用 | M2 只读 `thread/memory/read` 并入工作笔记；是否把 Codex 记忆关掉（防跨项目串味）要用户拍板 |
| 钩子（中途送信） | `hooks`（默认开）：`~/.codex/hooks.json` 或 `<repo>/.codex/hooks.json`；事件 SessionStart / UserPromptSubmit / PreToolUse / PermissionRequest / PostToolUse / PreCompact / PostCompact / Stop / Interrupt …；`Stop` 返回 `decision:"block"`+`reason` 会变成新一轮指令；`additionalContext` 默认限约 2,500 token；**非托管钩子要先被信任（按哈希）**，`--dangerously-bypass-hook-trust` 仅限单次；协议里有 `hooks/list`、`hook/started`、`hook/completed` | 没用 | 送信用不着（有 `turn/steer`）。可选：`Stop` 钩子自动跑验收命令。信任流程在 app-server 下怎么走【未验证】 |
| MCP 开关 | `-c mcp_servers.<名>.enabled=false`；`mcpServerStatus/list`、`config/mcpServer/reload` | 用了，但名单写死 3 个（lingxing / computer-use / node_repl） | 启动后用 `mcpServerStatus/list` 发现名单外的 MCP 并提示（每个线程多约 90MB） |
| 无头可靠性 | `serverOverloaded`（"Selected model is at capacity"）→ 线程状态 `systemError`；MCP 401 与无效配置只告警；`thread/resume` 水合整段历史有弃用提示；`thread/compacted` 通知已弃用，改看 `contextCompaction` 条目 | 容量错误已归类；用 `excludeTurns` | 保持；M2 做线路切换 |
| 版本漂移 | 稳定版 106 个请求 + 实验性多出 70 个；0.144→0.165 已删过 `codex mcp-server`；`codex features list` 列出每个开关的阶段（stable / experimental / under development / removed） | `tested_version` 只写了字符串 | 第 5 节 |

## 3. dsh 能力表（headless，`dsh --profile headless --json`）

| 能力 | CLI 怎么支持 | V2 现在用了吗 | 建议 |
|---|---|---|---|
| 开始 / 续接 / 分叉 | 默认每次新开 `session-<uuid>`；`--session-id <id>` 续接，**必须同一工作目录**，未知 id 直接报错；分叉只在 web 接口（`fork {atSeq}`） | 用了 `--session-id`；一人一张桌子 | 保持 |
| 排队 | headless 一进程一件事；sdk 的 `session/prompt` 入队；web 的 `prompt mode:'queue'` 【包内文档】 | 没用（排队在管家里） | 保持，管家排队 |
| 中途插话 | 仅 web 接口 `mode:'steer'`；acp 与 sdk 都没有 | 否 | 不支持，保持"一轮结束后才收到" |
| 打断 | headless 只能发信号；acp 有 `session/cancel`；sdk 没有 cancel | SIGINT → 5 秒 → 杀进程 | 保持 |
| 原生压缩 | 插件 `@deepseek-ai/dsh-compaction-basic`，默认 `thresholdRatio 0.8`、`headroomTokens 65536`、`retainRatio 0.16`、`auto true`、`modelPolicies`；先跑 `tool-result-pruner`（超 8192 字符的工具输出只留头 4096 + 尾 1024）；`/compact` 在 headless 里无效【实测】 | 没配；`providers.DSH.auto_compact_at=0.80` | 见下面"更正"；M3 在 `patch_text()` 里加这条配置，并用一次 ≥55% 的真会话验证 |
| 记忆条数字 | `step_end.usage.totalTokens`（`inputTokens` 不含缓存）；窗口从自带模型目录读：窗口 1,000,000、最大输出 384,000【实测】 | 用了 | 保持 |
| 思考直播 | `--json` 里的 `thinking` / `text` **在一步提交后才出现，不是逐字**；默认模式 stderr 才有实时推理（含敏感内容） | 用了 `thinking`（步级） | 页面上标明"每步更新"，不承诺逐字 |
| 计划 / 待办 | `tool-todo`：`todo_write` 整表替换，状态 pending / in_progress / completed | 没用（`plan=False`） | M3 把 `tool_call` 里的 `todo_write` 翻成 `plan` 事件；具体 JSON 形状【未验证】 |
| 结构化最终输出 | 没有；`final` 事件是整段文字 | 否 | 靠任务卡里要求固定格式 + 工位检查 |
| 权限 / 沙箱 | 环境变量 `DSH_PERMISSION_MODE`（默认 workspace-write；`approval` 在非 danger 时是 ask）；用户全局 patch 把 `permission.defaultPreset` 设成了 danger-full-access，**哪个优先【未验证】** | 没传 | M3 按任务卡传；审查员 read-only；先实测优先级 |
| token / 时间预算 | 没有硬预算；sdk 初始化可设 `maxTokens`（每次输出上限）；goal 插件需要命令行入口，headless 用不上 | 否 | 工位自己累计 `step_end` 并打断 |
| 记忆功能 | 无专门功能；有 `AGENTS.md` 指令文件与"引用其他会话的有界快照" | 否 | 用 V2 的工作笔记 |
| 钩子 | **没有**（包列表里没有 hook 类插件）；有 OpenTelemetry 遥测插件 | — | 不适用；中途信息只能下一轮发 |
| MCP 开关 | 用户的 headless 配置里自带 4 个业务 MCP（id 以 `mcp-` 开头），每个 dsh 员工都会拉起；`--patch` 可按 id 设 `disabled: true` | 没用 | M3 对非业务任务关掉【未验证效果与启动时间收益】 |
| 无头可靠性 | 失败的一轮仍以 `final` 收尾且**没有** `error` 事件，要看退出码 1 + `turn_end.reason`；单行 ≤32KiB、字符串 ≤8KiB 会被截断；首个 token 前没有心跳；`--patch` 里写错条目 id 只会警告 `patch: entry "…" not found` 然后**继续跑** | 已按退出码处理；**没检查补丁警告** | 补丁警告意味着模型限制可能失效（违反"只许 deepseek-v4.1-flash"），必须当错误处理 |
| 版本漂移 | 发版很勤：10-16 到 10-29 共 rc.1…rc.5 + alpha；`latest` = 0.2.1-rc.5；会话格式有 v0→v4 自动迁移链 | `tested_version="0.2.1-rc.2"` 精确匹配 | 第 5 节；V2 不直接读 dsh 的会话文件，格式风险低 |

**更正（需要改文档和代码的地方）**：`arch-v10-rebuild.md` §3.2 与 `providers.py` 写的"dsh 在 80% 时自动压缩"，按公式应为约 55%【推算】。验证办法：做一个上下文推到 55% 以上的真会话，看 `usage_drop` 是否在约 550k 出现。在验证前，**不要把 dsh 的 V2 门槛设成 55%**，建议暂时把 V2 的 dsh 交接门槛设为 65%，原生压缩先行。

## 4. agy 能力表（stream-json，`--input-format stream-json --output-format stream-json -p=`）

| 能力 | CLI 怎么支持 | V2 现在用了吗 | 建议 |
|---|---|---|---|
| 开始 / 续接 / 分叉 | `--conversation <ID>`、`-c`；热续接（stdin 一行一轮）3/3 成功【实测】；同一对话不能被两个进程同时打开；`/fork` 只在交互里 | 用了 | 保持；`--project`/`--new-project` 可忽略 |
| 排队 | 设置 `queuedMessages`：`queue`（默认，等本轮结束）或 `send-immediately`（打断 agent）；stream-json 一行一轮顺序处理 | 没用；适配器忙时抛 `AdapterBusy` | 管家排队即可 |
| 中途插话 | 官方没有；见钩子一行与 `send-immediately` | 否 | M3 探针后再定 |
| 打断 | 只能关输入 / SIGINT / 杀进程；`--print-timeout` 到点返回已有输出并以 0 退出（新版行为，changelog 记载） | 关输入 + SIGINT + 杀 | 保持；对"一轮"可加 `--print-timeout` 当保险【未验证与热续接的交互】 |
| 原生压缩 | `/compact`（`/compact <话>` 压完接着答）；自动压缩阈值设置页**没有**可调键；1.4.10 修了压缩后大小口径 | `native_compact=False`，走交接压缩（3 轮或 50%） | M3 在大上下文真测 `/compact` 并看 `usage` 是否下降 |
| 记忆条数字 | `step_update.usage.input_tokens`（50 步里 14 步带）；窗口无来源，只能估 1,048,576 | 用了（标估算） | 保持；`-p "/context"` 是否可用【未验证】 |
| 思考直播 | 交互界面有 "Thought for…"；stream-json 里 `thinking_tokens=0`、无文字【实测，flash 档】 | 否（"看不到"） | 用 flash-high 做一次小探针，看有没有 thinking 步骤；Python SDK 的 `response.thoughts` 可以流式，但 SDK 走 API key / Vertex 计费，不共用 agy 登录，**不建议** |
| 计划 / 待办 | `--mode plan` 产出计划文件（artifact）；stream-json 没有计划事件 | 否 | 靠回复里的进度标记 |
| 结构化最终输出 | `--json-schema`（schema 串或文件；根必须 object；stream-json 只约束最终 result） | 没用 | 只用于一次性审查任务 |
| 权限 / 沙箱 | `--settings <JSON或文件>`（仅本次）：`permissions.allow/ask/deny`（deny > ask > allow）、`toolPermission`、`agentMode`、`queuedMessages`、`artifactReviewPolicy`（`always-proceed` 不等产物审阅）、`trustedWorkspaces`、`enableTerminalSandbox`；另有 `--mode`、`--sandbox`、`--dangerously-skip-permissions` | 用了 allow、`--mode`、`--sandbox`；没用 deny 与其余键 | M3：加 deny（git commit/push）；加 `artifactReviewPolicy:"always-proceed"`（避免计划模式等人审阅，【未验证是否会卡】） |
| token / 时间预算 | 没有；`result.usage` 是本轮合计；每天 40 次调用由工位计数 | 工位计数 | 保持 |
| 记忆功能 | 工作区根目录放 `AGENTS.md` / `GEMINI.md` 自动读取；无跨会话记忆 | 否 | 用 V2 的工作笔记 + `.xay/context.md` |
| 钩子（中途送信） | `hooks.json`：`.agents/hooks.json`（工作区）、`~/.gemini/config/hooks.json`（全局）、插件内；设置里的 `hooks` 键**不加载**。事件 SessionStart / PreToolUse / PostToolUse / PreInvocation / PostInvocation / Stop；输入 JSON 含 `conversationId`、`transcriptPath`、`modelName`；`PreToolUse` 可返回 `decision`+`overwrite`；`PostInvocation` 可返回 `terminationBehavior:"force_continue"`；超时默认 30 秒；`PostToolUse` 钩子失败不会再把工具标成失败（changelog 里已修）【网页】 | 没用 | M3 探针：在桌子 `.agents/hooks.json` 放一个读"信箱"文件的 `PreInvocation` 钩子。注意：工作区钩子可能要先"信任文件夹"【未验证】；该文件要加进桌子的 `.git/info/exclude` |
| MCP 开关 | 用户 `~/.gemini/config/mcp_config.json` 或工作区 `.agents/mcp_config.json`；`agy mcp enable/disable` **会写用户配置** | 否 | 8 个 node MCP 子进程占约 450–515MB，是内存大头；动用户配置要先问用户；工作区文件能否覆盖【未验证】 |
| 无头可靠性 | 需要授权的操作在无头里一律被拒，结果里 `denied_actions`；模型/代理出错退出码 3 + `AGY_ERROR`；工具权限被拒不算失败；冷续接可能空转（已用 30 秒看门狗）；**绝对不要跑隐藏子命令 `remote-control serve`**（会连 Google 中继并写配置） | 全部已处理 | 保持；`stream-json` 的 `init` 事件 1.x 里曾误报工具列表，别依赖它 |
| 版本漂移 | `agy --version`；`agy changelog` 滞后（只到 1.4.10）；10 月内 changelog 里有 hooks、print 模式、压缩多项改动；模型名列表可用 `agy models` 核对 | `tested_version="1.4.11"` | 第 5 节 |

## 5. 兼容风险与探测办法

**现状**：`probe_versions()`（跑 `<bin> --version`）与 `version_drift()` 有了，但没人调用；适配器不记录自己跑的是哪个版本。账本协议里已有 `signal.raised`，可新增 `kind: cli_drift`。

**每家一升级最可能坏什么**
| | 最可能坏的 | 现象 | 严重度 |
|---|---|---|---|
| Codex | 实验性方法 / 字段改名（`thread/queue/*`、`thread/settings/update`、`turn/steer` 参数）；`-c` 配置键被忽略（只告警）；`current` 软链被自动更新换掉 | 请求报错 `-32600`；设置悄悄不生效（比如 `model_reasoning_summary`） | 高（自动更新会在无人知晓时发生） |
| dsh | `--patch` 条目 id 改名（`agent-default-model`、`compaction-basic`）→ 只警告、补丁失效；`--json` 事件新增 / 改名 phase；rc 版本间配置项增删 | **模型限制失效**；记忆条突然为 0 或事件被忽略 | 高（影响硬性约束） |
| agy | stream-json 事件键（`event`、`step_update`、`result`）与输入载荷；`--settings` 键名；模型名；退出码约定；钩子行为 | 一轮永不结束；放行命令被拒；找不到模型 | 中 |

**三层探测（越往下越贵）**
1. **每次启动、免费**：`--version`；Codex 执行 `codex app-server generate-json-schema --experimental --out <临时目录>`（约一两秒），agy 解析 `--help` 的参数集合，dsh 解析 `--help` 与 `--profile headless --help`。
2. **契约清单（核心）**：写成代码里的一张表，只检查 V2 真用到的东西，缺一项就把这家标"不可用"、给出大白话原因：
   - Codex：请求方法 `initialize / thread/start / thread/resume / thread/unsubscribe / thread/compact/start / turn/start / turn/steer / turn/interrupt / model/list`（以及 M2 要接的 `thread/queue/add`）；`TurnStartParams` 有 `threadId / input / effort / summary / additionalContext / sandboxPolicy / outputSchema`；通知 `thread/tokenUsage/updated`、`turn/plan/updated`、`item/reasoning/summaryTextDelta`；`ThreadTokenUsage.last.totalTokens` 与 `modelContextWindow`。
   - dsh：`--patch --json --session-id` 都在帮助里；用 `dsh --patch <我们的补丁> --dump-config` 在内存里确认 `agent-default-model` 的 model 是 `deepseek-v4.1-flash`、`compaction-basic` 的配置生效；**运行时 stderr 出现 `patch: entry "…" not found` 就中止并报错**。
   - agy：帮助里有 `--model --input-format --output-format --sandbox --mode --settings --add-dir --conversation --dangerously-skip-permissions`；`agy models` 里三个 gemini-3.8-flash 档都在。
3. **金丝雀（花一点额度，手动或升级后触发）**：每家一轮"回复 OK"的真实小任务（dsh / agy 用最低档），对照 `V2/tests/fixtures_adapters` 里已录的样本，确认事件序列没变。命令如 `xay2 doctor --live`。

**今天的参考指纹（不是上限，只是"升级前"的快照）**
- Codex 0.165.0：实验性 schema 的请求 / 通知 / 服务端请求方法集合共 272 项，方法集合哈希前缀 `ccc7c055254f0233`；稳定版 201 项，`5a7396053a00dbc9`。
- agy 1.4.11：帮助里的参数共 22 个，参数集合哈希前缀 `4f4810c9e569`。
- dsh 0.2.1-rc.2：`--help` 与 headless `--help` 的参数集合哈希前缀 `6932bcb5ff16`；`--dump-config-schema` 约 631KB，但里面混有用户自己的配置，**不适合整体当指纹**，只取 `compaction-basic` 的配置段。

**处理策略**
| 情况 | 动作 |
|---|---|
| 版本一致 | 正常 |
| 版本变了、契约清单全过 | 记一条 `cli_drift`（info），页面角落提示；自动接受 |
| 契约清单有缺项 | 该家标"不可用"，不再开新活；正在跑的不打断；写给用户 |
| 金丝雀失败 | 同上，并保留原始收发做对比 |

**钉版本**：Codex 用 `releases/<版本>/bin/codex` 的绝对路径（环境变量 `XAY_CODEX_BIN` 已支持）；dsh 在 `npm -g` 里没法并存，只能记录 `realpath` + 版本，变了就走上面的流程；agy 的 `agy update` 是用户手动动作，V2 永不调用任何 `update` 子命令。

## 6. V2 后续清单

最后一列是本轮按仓库现有代码（`V2/xay/`、`V2/probes/`）和 `V2/docs/probes.md`、`protocol.md` 1.12~1.15 逐项核对的结果；只读了代码和文档，没有真跑 CLI。标记只有四种：已做 / 部分做了 / 没做 / 待核对。

| # | 做什么 | 改哪里 | 工作量 | 里程碑 | 现在到哪了（本轮核对） |
|---|---|---|---|---|---|
| 1 | 启动自检 `doctor`：调用 `probe_versions` + 契约清单，结果写账本信号 `cli_drift`，页面显示 | `V2/xay/providers.py`、新增 `V2/xay/adapters/contracts.py`、`V2/xay/office/daemon.py` | M | M2（Codex 员工上线前） | 部分做了：每次工位开工已经跑 `probe_versions`（`V2/xay/station/station.py:387`，测试 `V2/tests/test_station.py:218`；提交 6405a87），版本不对写 `signal.raised{kind: cli_version_changed}`，`employee.online` 带 `cli_version`。还差：`adapters/contracts.py` 契约清单、`xay2 doctor` 子命令（`xay/cli.py` 里没有）、`cli_drift` 信号（现在叫 `cli_version_changed`）、页面提示；`office/daemon.py` 启动时也没跑 |
| 2 | 钉住 Codex 绝对路径；适配器把 `realpath` 与版本填进 `Capabilities.cli_version` 和 `employee.session` | `V2/xay/adapters/codex.py`、`providers.resolve_command` | S | M2 | 部分做了：`XAY_CODEX_BIN` / `XAY_APP_SERVER_BIN` 已支持（`V2/xay/providers.py:116`、`resolve_command` 第 559 行起），版本已填进 `Capabilities.cli_version` 和 `employee.session`（`station.py:771`、`station.py:946`，`test_station.py:218`）。还差：没有记录命令行的 `realpath`（适配器里没有），也没有自动钉到 `releases/<版本>/bin/codex` 的逻辑，只能靠人手设环境变量 |
| 3 | 更正 dsh 压缩假设（80%→约 55%【推算】），`patch_text()` 增加 `compaction-basic` 配置；实测一次 | `V2/xay/providers.py`、`V2/xay/adapters/dsh.py`、`V2/docs/probes.md`、`references/plans/arch-v10-rebuild.md` §3.2 | S | M3 | 已做：dsh 窗口改成开工前读 `--dump-config`（`V2/xay/adapters/dsh.py:168`，读不到用 262,144），两件活之间门槛 55% 或 30 万 token（`providers.py:139`），"它自己压缩 = 掉 10% 且之前 ≥40%"（`dsh.py:376`）；结论写进 `probes.md` 的 M3 小节和 `arch-v10` §3.2（0f65c6f）。`compaction-basic` 是有意没加：dsh 自己约 75%（196,608）才压，我们的门槛在它前面 |
| 4 | dsh 补丁生效检查：内存解析 `--dump-config` + 运行时识别 `patch: entry … not found` | `V2/xay/adapters/dsh.py` | S | M3 | 已做：`dsh.py:168 _preflight()` 在内存里解析 `--dump-config` 核对补丁与模型，`providers.dsh_patch_warnings()` 只认我们补丁路径的警告；每轮 stderr 也查（`dsh.py:269`），不通过抛 `AdapterConfigError` 拒绝开工（9fe2239；测试 `tests/test_adapters.py:630`） |
| 5 | Codex 加 `-c model_auto_compact_token_limit=…`、`-c compact_prompt=…`，探针确认摘要格式和压缩点 | `V2/xay/adapters/codex.py`（`_config_args`）、`V2/probes/codex_probe.py` | S | M2 | 部分做了：`codex.py:158` 已按 `pin_auto_compact_at=0.90` 传 `-c model_auto_compact_token_limit`（`providers.py:105`、`tests/test_station.py:234`）。还差 `-c compact_prompt=…`（全仓没有）；`probes/codex_probe.py` 和 `probes.md` 里也没有压缩点或摘要格式的探针结果（M2 的小活到不了门槛，原生压缩只在假 AI 上测过） |
| 6 | Codex 思考摘要探针：`detailed` / `auto` / `show_raw_agent_reasoning=true` 三选一 | `V2/probes/codex_probe.py`、`V2/docs/probes.md` | S | M2 | 已做：`codex.py:152` 传 `model_reasoning_summary`，默认 detailed（`providers.py` 的 `DEFAULT_REASONING_SUMMARY`，`tests/test_adapters.py:342`）；真机 M2 冒烟收到 8 段思考摘要（`probes.md:39`，8ebdcff）。`auto` 和 `show_raw_agent_reasoning=true` 没试，现在不需要 |
| 7 | Codex 结构化汇报：`send()` 支持 `outputSchema`，协议里定汇报格式 | `V2/xay/adapters/codex.py`、`V2/docs/protocol.md` | M | M2→M3 | 没做：`send()` 不支持 `outputSchema`（全仓只有本文提到它），汇报仍是回复里的【结果】文字 → `task.reported{claim,summary}`（`protocol.md:203`），不是结构化 JSON |
| 8 | Codex 排队 / 分叉 / 中途改设置：`thread/queue/add`、`thread/fork`、`turn/settings/update` | `V2/xay/adapters/codex.py` | M | M2（排队）、M3（其余） | 没做：`codex.py` 里没有 `thread/queue/*`、`thread/fork`、`turn/settings/update`；忙时照旧抛 `AdapterBusy` |
| 9 | dsh `todo_write` → `plan` 事件；`DSH_PERMISSION_MODE` 按任务卡传入（先实测与用户全局默认谁优先） | `V2/xay/adapters/dsh.py` | S | M3 | 没做：`dsh.py:85` 仍是 `plan=False`，没有把 `todo_write` 翻成 `plan` 事件；`DSH_PERMISSION_MODE` 全仓没有 |
| 10 | agy 临时 settings 增加 `permissions.deny`（git commit / push）、`artifactReviewPolicy`；探针确认 deny 对带参数命令生效 | `V2/xay/adapters/agy.py`（`settings_for`）、`V2/probes/agy_probe.py` | S | M3 | 部分做了：`agy.py:83 settings_for()` 已写 `permissions.deny`（`DENY_COMMANDS` = git commit / git push），但只在有放行命令或 full 模式时才写 `--settings`（`_write_settings` 第 209 行起），其余情况 deny 不生效。还差 `artifactReviewPolicy`（全仓没有）；"deny 对带参数命令是否匹配"也没有探针（`probes/agy_probe.py` 里没有 deny 用例） |
| 11 | agy 钩子 / `queuedMessages` 探针（PreInvocation `injectSteps`、Stop `continue`）；通过后实现 `steer()` | `V2/xay/adapters/agy.py`、`V2/probes/agy_probe.py` | M | M3 探针，M4 启用 | 没做：`agy.py` / `agy_probe.py` 里没有 hooks（`hooks.json`、`PreInvocation`、`injectSteps`）、`queuedMessages` / `send-immediately`；`steer` 仍是 `"none"` |
| 12 | agy 原生 `/compact` 在大上下文里实测，决定 `native_compact` | `V2/xay/providers.py`、`V2/xay/adapters/agy.py` | S | M3 | 已做：`providers.py:159` 的 `native_compaction=True` → `agy.py:111` 的 `native_compact`；`compact()` 已实现，"同一段压满 2 次改交接"在 `station.py:1683`；大上下文 65,211 → 16,889 写进 `probes.md`（9fe2239、2fa41e1） |
| 13 | 预算：按 `thread/tokenUsage/updated` / `step_end` / `result.usage` 累计 + 70/90/100% 规则；Codex goal 探针 | `V2/xay/adapters/*.py`、工位 | M | M4 | 部分做了：`V2/xay/office/budget.py` 已按 `turn.ended.usage.total` 和一轮中途的 `context.updated.turn_tokens` 累计（`_tokens`，同口径取大的）；70/90/100 三条线在 `office/rules.py:475`，默认值在 `office/reactions.py:43`（协议 1.14，d85602f、01aeecc）。还差：Codex `thread/goal/set` 的探针没做（全仓没有 goal） |
| 14 | 零成本体检：agy `/usage` json、`agy models`；Codex `account/rateLimits/read` | `V2/xay/providers.py`、额度模块 | S | M4 | 没做：启动自检里没有 `agy -p "/usage"`、`agy models`，也没有 Codex 的 `account/rateLimits/read`（`quota.py` 只记 agy 每天调用次数）。相关但不同：Codex 额度已改成收 `account/rateLimits/updated` 通知（`codex.py:590`，协议 1.15） |
| 15 | dsh `acp` profile 适配器评估（一进程多会话 + `session/cancel`），需锁死模型选项 | 新适配器 | L | M4 以后 | 没做：没有 acp 适配器（全仓 grep 无 `acp`），也没有评估记录 |
| 16 | 把本文所有【未验证】项逐条变成探针结果，写回 `V2/docs/probes.md` | `V2/docs/probes.md` | S | 随 M2–M4 | 部分做了：`probes.md` 已写回 dsh 真实窗口与自压点（M3 小节）、agy `/compact` 大上下文（M3 小节）、Codex 思考摘要（M2 冒烟）、dsh headless `/compact` 复查（工位收尾小节）。还没写回：Codex `compact_prompt`、`thread/goal/set`、hooks 信任、命名权限档、`execpolicy`；dsh `DSH_PERMISSION_MODE` 优先级、关 MCP 收益、`todo_write` 形状、acp/sdk；agy hooks / `send-immediately`、flash-high 思考、deny 带参数、`/context` |

## 7. 本文没有验证的事（汇总）
- Codex：`model_auto_compact_token_limit` / `compact_prompt` 的实际效果；三种思考配置谁能出内容；hooks 在 app-server 下的信任流程；命名权限档；`thread/goal/set` 会不会自动续跑；`codex execpolicy` 规则能否按会话加载。
- dsh：约 55% 的自动压缩点；`DSH_PERMISSION_MODE` 与用户全局 `permission` 谁优先；用 `--patch` 关业务 MCP 的收益；`todo_write` 的 JSON 形状；acp / sdk 的实际表现（只读了包内文档）。
- agy：钩子与 `send-immediately` 在 stream-json 下是否生效、工作区钩子是否需要信任；`/compact` 的实际压缩量；flash-high 是否有思考步骤；deny 规则对带参数命令是否匹配；`/context` 在打印模式是否可用。
- hcom 在 Codex / agy 上具体用哪种钩子送信：官网只说"automatic（自动）"，没公开事件名；第三方文档说 Codex 靠回合结束钩子加终端注入。上面的 agy 方案是根据官方 hooks 文档自己推出来的，不是照抄 hcom。

来源：本机 `codex app-server generate-json-schema`、各 CLI `--help`、dsh 安装包 README（`dsh-compaction-basic`、`dsh-headless`、`dsh-acp`、`dsh-sdk-protocol`、`dsh-tool-todo`）；[Codex hooks](https://learn.chatgpt.com/docs/hooks)、[Codex releases](https://github.com/openai/codex/releases)、[Antigravity hooks](https://antigravity.google/docs/hooks)、[Antigravity settings](https://antigravity.google/docs/settings)、[Antigravity SDK](https://github.com/google-antigravity/antigravity-sdk-python)、[hcom wiki](https://github.com/aannoo/hcom/wiki)。
