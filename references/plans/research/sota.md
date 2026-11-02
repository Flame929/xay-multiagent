# xay 多 agent 编排：前沿调研（SOTA）

> 调研日期 2026-10-29。优先一手资料；标"二手"的为第三方整理，"本机"为在本机 `--help`/目录结构上核实（未读取任何会话内容或 token）。
> 本机版本：codex-cli 0.165.0、dsh 0.2.1-rc.2、agy 1.4.9；本机无 `claude` 可执行文件在 PATH 中（当前 Claude 跑在 Desktop 宿主内）。

## 1. Claude Code 自身的多 agent / 编排机制

**本机观察**：环境变量有 `CLAUDE_CODE_SESSION_ID`、`CLAUDE_CODE_HOST_SESSION_ID`、`CLAUDE_CODE_CHILD_SESSION`、`CLAUDE_CODE_MESSAGING_SOCKET`/`_TOKEN`、`CLAUDE_CODE_ENABLE_SDK_FILE_CHECKPOINTING`。`~/.claude/projects` 98M（18 个项目、216 个 `.jsonl`），`file-history` 2M，`sessions` 48K；`~/.codex/sessions` 已 1.5G，说明会话日志是第一大磁盘消耗。

| 机制 | 核心要点 | 映射到跨厂商编排 |
|---|---|---|
| Subagents | 独立上下文，只回传摘要；frontmatter 可配 `tools`/`disallowedTools`/`model`/`permissionMode`/`isolation: worktree`/`background`/`maxTurns`/`effort`/`hooks`；默认最多嵌套 3 层，并发默认 20；转录写在 `~/.claude/projects/{proj}/{sid}/subagents/agent-*.jsonl`；可用 `SendMessage` 按名字续接 [sub-agents](https://code.claude.com/docs/en/sub-agents) | xay 的"角色卡"就照这套字段设计：工具白/黑名单、模型、强度、轮数上限、隔离方式、按 ID 续接 |
| Agent teams（实验） | lead + teammates + 共享任务列表 + 邮箱；邮箱是 `~/.claude/teams/{team}/inboxes/{agent}.json`，任务在 `~/.claude/tasks/{team}/`，**认领任务靠文件锁**，依赖完成后自动解锁；队友空闲时自动通知 lead 并附带最终答复；限制：不能嵌套团队、lead 固定、resume 不恢复队友、任务状态会滞后 [agent-teams](https://code.claude.com/docs/en/agent-teams) | **文件邮箱 + 文件锁任务板**这一纯文件方案可以直接拿来做跨 CLI 协作：零常驻进程，任何 CLI 都能读写；"任务状态滞后"要靠编排器从产物/退出码推断，不能只信 agent 自报 |
| 质量门 hooks | `TeammateIdle`/`TaskCreated`/`TaskCompleted` 以 exit 2 退回并附反馈 [agent-teams](https://code.claude.com/docs/en/agent-teams#enforce-quality-gates-with-hooks) | xay 的"完成判定"做成可插拔的 gate 脚本（测试、diff 范围、报告格式），不通过就自动退回 |
| Hooks 全集 | `PreToolUse`（allow/deny/ask/defer，可用 `updatedInput` 改参数）、`PostToolUse`（`updatedToolOutput`）、`PostToolBatch`（可中止循环）、`Stop`/`SubagentStop`（block 后继续干活）、`PreCompact`/`PostCompact`、`SessionStart/End`、`WorktreeCreate`；hook 类型有 command/http/mcp_tool/prompt/agent；`async`/`asyncRewake` 可在后台运行，exit 2 时唤醒 [hooks](https://code.claude.com/docs/en/hooks) | **Claude 执行者的实时监控与拦截**：`http` hook 把每个工具事件 POST 给 xay 守护进程（实时 trace），用 `PreToolUse` deny 越界写入；用 `asyncRewake` 做看门狗。其它 CLI 没有这么丰富的 hook，只能靠事件流事后判断（见 §2） |
| 跨会话消息 | 每个会话绑定一个 Unix socket（`CLAUDE_CODE_MESSAGING_SOCKET`），脚本或 hook 可先发 `{"type":"auth","token":…}` 再投递纯文本；**工具调用之间投递，不打断正在跑的工具**；会话空闲时会开新一轮；收到的消息被标为"来自其他会话"，不能批准权限；有限流、去重、队列最多 50 条；`-p` 会话也绑定 socket，`--bare` 不绑定 [cross-session-messaging](https://code.claude.com/docs/en/cross-session-messaging) | **对 Claude 执行者做 steering 的现成通道**：xay 往 worker 的 socket 写纠偏指令，无需杀进程。语义与 Codex `turn/steer` 一致，可统一抽象成 `steer(text)` |
| 后台 agent / agent view | `claude --bg`，由 supervisor 守护进程托管；状态有 Working/Needs input/Idle/Completed/Failed/Stopped；`claude agents --json --all` 可读 `state/waitingFor`；未挂载的空闲会话约 1h 后停进程、内存紧张时回收、崩溃自动重启；默认放进 `.claude/worktrees/` [agent-view](https://code.claude.com/docs/en/agent-view) | xay 的状态机与回收策略直接对齐这套："Needs input"作为一等状态；空闲进程回收、会话落盘后可续接 |
| Headless / SDK | `-p --output-format stream-json`（加 `--input-format stream-json` 可从 stdin 多轮输入）、`--json-schema`→`structured_output`、`--max-budget-usd`、`--permission-prompts none`、`--bare`（跳过 hooks/MCP/CLAUDE.md，以后会成为默认）；子 agent 消息用 `parent_tool_use_id` 关联；`system/init.capabilities` 可做特性探测（如 `interrupt_receipt_v1`）；**SIGTERM 不收尾当前轮，要先 SIGINT 或 SDK `interrupt()`** [headless](https://code.claude.com/docs/en/headless) · [Agent SDK](https://code.claude.com/docs/en/agent-sdk/overview) | 适配器靠 capability 而不是版本号判断能力；停止流程统一为"先 interrupt、等待几秒、再 TERM"；`--bare` 能保证执行者可复现（不受用户 `~/.claude` 影响） |
| Checkpoint / rewind | 每个用户 prompt 打一个检查点，保留最近 100 个，约 30 天清理；**不追踪 Bash 改动、子 agent 改动和外部改动** [checkpointing](https://code.claude.com/docs/en/checkpointing) | 跨 CLI 回滚不能依赖各家 checkpoint，统一用 git（每轮派发前在 worktree 上打 commit/stash 快照） |
| Compaction | 摘要时保留决策/未解 bug，丢弃冗余工具输出；配合"结构化笔记"和"子 agent 只回传压缩摘要" [context engineering](https://www.anthropic.com/engineering/effective-context-engineering-for-ai-agents) | lead 只读 brief，worker 读文件；续接前注入"工作线文件 + 报告摘要"来抵消压缩后的漂移 |

## 2. 跨 agent 协议与各 CLI 支持现状

| 协议 | 会话 / 流式 / 取消 / 权限 | 定位 |
|---|---|---|
| **ACP**（Zed） | JSON-RPC over stdio；`initialize`→`session/new`/`session/load`→`session/prompt`（流式 `session/update`：消息块、thought、tool_call、plan、模式切换、compaction）；`session/cancel` 是通知，agent 应结束挂起的权限请求并以 `cancelled` 返回；权限 `session/request_permission`（allow/reject × once/always）；可选由 client 提供 `fs/*`、`terminal/*` [overview](https://agentclientprotocol.com/protocol/overview) · [cancellation](https://agentclientprotocol.com/protocol/v1/cancellation.md) | 编辑器 ↔ 本地编码 agent，**语义与 xay 需求最贴近** |
| **Codex app-server** | JSON-RPC（不带 `jsonrpc` 字段）走 stdio、Unix socket 或实验性 WS；thread/turn/item 三层；`thread/start|resume|fork`、`turn/start`（可按轮覆盖 model/effort/cwd/sandbox/approvalPolicy/outputSchema）、**`turn/steer`（向进行中的轮追加输入）**、`turn/interrupt`（状态变为 `interrupted`）；审批走 `item/commandExecution/requestApproval`、`item/fileChange/requestApproval`（accept/acceptForSession/decline/cancel）；一个进程可加载多个 thread，最后一个订阅者退订后保留 30 分钟；`generate-json-schema` 可导出与版本匹配的 schema [app-server](https://learn.chatgpt.com/docs/app-server)；本机 0.165 还有共享守护进程 `codex app-server daemon` 与 `codex agents` | Codex 的一等接口，比 `exec --json` 多了 steer、审批和多线程复用 |
| **A2A**（LF，v1.0） | AgentCard 发现；Task 状态有 submitted/working/input-required/auth-required/completed/failed/canceled/rejected；SendStreamingMessage（SSE）、SubscribeToTask、CancelTask、推送 webhook；传输支持 JSON-RPC/gRPC/REST [spec](https://a2a-protocol.org/latest/specification/) | 面向**远程不透明 agent**，HTTP 服务形态；本机编排太重，但它的**任务状态机值得照抄** |
| **MCP**（2025-11-25） | 新增实验性 Tasks："先调用、后取结果"，有状态和 TTL；有 elicitation [tasks](https://modelcontextprotocol.io/specification/2025-11-25/basic/utilities/tasks) · [WorkOS 解读（二手）](https://workos.com/blog/mcp-2025-11-25-spec-update) | 适合把 xay 自己暴露成工具（让任意 CLI 当负责人时调用 `xay.dispatch/status/steer`），不适合当执行者通道 |

**各 CLI 现状**：
- Claude：ACP 走 Zed 的 `claude-agent-acp` 适配器（基于 Agent SDK）[ACP agents](https://agentclientprotocol.com/get-started/agents)；原生通道是 stream-json + hooks + 消息 socket。
- Codex：有 `codex-acp` 适配器 [同上](https://agentclientprotocol.com/get-started/agents)；原生是 app-server；本机 0.165 已看不到 `mcp-server` 子命令。
- dsh：本机 headless profile 支持 `--json`（NDJSON 运行事件）、`--session-id` 续接、从 stdin 读任务；二手资料称另有 SDK（JSON-RPC）和 ACP profile，但本机只有 desktop/headless/web 三个 profile，未核实 [二手](https://www.eesel.ai/de/blog/deepseek-harness-erklaert)。
- agy：本机 1.4.9 支持 `-p --output-format stream-json --input-format stream-json`（每行 stdin 跑一轮）、`--json-schema`、`--conversation` 续接，stream-json 里带 `subagent_info`；**没有 ACP**（帮助里查不到，社区也在抱怨）[forum](https://discuss.ai.google.dev/t/ux-issues-and-lack-of-acp-support-in-antigravity-cli/169445) · [1.1.8 release](https://newreleases.io/project/github/google-antigravity/antigravity-cli/release/1.1.8)；旧版在非 TTY 下 `-p` 会挂起 [issue #318](https://github.com/google-antigravity/antigravity-cli/issues/318)。
- Gemini CLI：`--acp` 已稳定，`--experimental-acp` 已弃用（二手）[geminicli docs](https://geminicli.com/docs/ide-integration.md)。

**结论**：xay 的"统一执行者适配层"**内部模型按 ACP 语义设计**（session / prompt / update / cancel / permission + 扩展的 `steer`），**外部按各家最强的原生通道实现**：Codex→app-server（有 steer），Claude→stream-json + 消息 socket + hooks，agy→stream-json 双向，dsh→headless `--json`。有 ACP 时可选走 ACP（`codex-acp`、`claude-agent-acp`），但 ACP 没有标准的 mid-turn steer，只能 cancel 后重新 prompt。A2A 只借鉴状态机；MCP 用来把 xay 暴露给"负责人"。

## 3. 前沿多 agent 编排架构

- **Anthropic 多 agent research**：机制是 lead 并行派 3–5 个 subagent，每个委派写清目标、输出格式、工具和边界；按复杂度给出明确的投入规则；子 agent 把产物写进文件系统，只回传引用，避免"传话游戏"；计划存外部记忆；出错从检查点续跑；生产全量 trace；LLM-judge 用单次调用 + rubric。可借鉴：委派卡四要素、产物落盘只传路径、按难度给预算。坑：token 约为聊天的 15 倍，token 用量能解释 80% 的效果差异；**同步等待整批完成是瓶颈**，无法中途纠偏；强耦合的编码任务收益较小。[来源](https://www.anthropic.com/engineering/multi-agent-research-system)
- **OpenHands SDK V1**：机制是事件溯源状态（不可变配置 + 可确定性重放）、pause/resume、安全分析器 [arXiv 2511.03690](https://arxiv.org/abs/2511.03690)；卡死检测器识别 5 种模式：同一动作得到同一观察 ≥4 次、同一动作报错 ≥3 次、连续 ≥3 条自言自语、两组动作交替 ≥6 轮、反复超上下文 [stuck detector](https://docs.openhands.dev/sdk/guides/agent-stuck-detector)。可借鉴：看门狗规则直接照抄，作用在 xay 的统一事件流上。坑：需要对动作/观察做规范化后才能比较"相同"。
- **LangGraph**：机制是 checkpointer + `thread_id`；`interrupt()` 暂停，用 `Command(resume=…)` 恢复。可借鉴：人在回路的中断点做成持久化的"待决事件"，恢复时带决定值。坑：**恢复时节点从头重跑**，中断前的副作用必须幂等；多个 interrupt 按序号匹配，顺序不能变。[interrupts](https://docs.langchain.com/oss/python/langgraph/interrupts)
- **Magentic-One**：机制是外层 Task Ledger（事实、猜测、计划）+ 内层 Progress Ledger（是否完成、是否有进展、下一个谁做、给什么指令）；停滞计数 >2 时回到外层重新规划。可借鉴：lead 每轮写一份结构化的 progress ledger，停滞计数触发重规划或换执行者。坑：每轮都调用 LLM 自评，成本高，自评也会自欺。[MSR](https://www.microsoft.com/en-us/research/articles/magentic-one-a-generalist-multi-agent-system-for-solving-complex-tasks/) · [arXiv 2411.04468](https://arxiv.org/abs/2411.04468)
- **AutoGen 0.4 Core**：机制是 Actor 模型，异步消息，单进程或分布式运行时。可借鉴：每个 worker 是一个 actor，只通过消息改变状态。坑：框架已转为维护模式（二手），不要绑定框架。[docs](https://microsoft.github.io/autogen/stable/user-guide/core-user-guide/index.html)
- **CrewAI hierarchical**：机制是 manager 把 worker 当作工具来委派。坑：实测常退化成顺序执行、委派循环、manager 找不到 coworker。对 xay 的教训：负责人不要靠 LLM 自由委派，派发和依赖要由确定性代码执行。[TDS](https://towardsdatascience.com/why-crewais-manager-worker-architecture-fails-and-how-to-fix-it/)
- **MetaGPT**：机制是 SOP + 共享消息池 + 按角色订阅；产物是结构化文档，依赖齐了才激活。可借鉴：报告和接口契约结构化（xay-report），用订阅代替点对点广播。坑：SOP 太死板，不适合探索型任务。[arXiv 2308.00352](https://arxiv.org/abs/2308.00352)
- **黑板架构**：机制是中心发布请求，子 agent 按能力自愿认领（无需知道谁擅长什么），相对提升 13–57%（[arXiv 2510.01285](https://arxiv.org/abs/2510.01285)）；共识型黑板更省 token（[arXiv 2507.01701](https://arxiv.org/abs/2507.01701)）。可借鉴：任务板 + 能力标签，让空闲 worker 认领。坑：并发认领需要锁（Claude teams 用文件锁），而且需要裁决者。
- **Actor / 监督树**：机制是 Erlang supervisor 的 one_for_one 重启策略，加重启强度上限 [OTP](https://www.erlang.org/doc/system/sup_princ.html)。可借鉴：worker 崩溃时按策略重启或续接；N 分钟内重启超过 K 次就熔断并上报 lead。坑：LLM 失败常常不是瞬时故障，盲目重启只会烧钱，要和失败归因结合。
- **事件溯源**：机制是只追加事件，状态由事件投影出来 [Fowler](https://martinfowler.com/eaaDev/EventSourcing.html)。可借鉴：xay 账本（NDJSON）作为唯一事实源，UI、scorecard、brief 都是投影。坑：schema 演进要带版本号；需要快照，否则重放会越来越慢。
- **失败学（MAST，NeurIPS 2025）**：14 种失败模式分为系统设计、agent 间失配、验证不足三类，大多源于系统设计而非模型能力 [arXiv 2503.13657](https://arxiv.org/abs/2503.13657)。可借鉴：把"规格不清、忽视他人输入、过早终止、验证缺失"做成派发前和收尾时的检查项。

**实时监控 + 纠偏的做法汇总**：①看门狗：心跳/事件超时（无事件 N 秒判为 stalled）+ OpenHands 五类循环检测；②偏航检测：写入路径越出 `allowed_paths`（Claude 用 PreToolUse 拦截，其它 CLI 用事件流告警并在事后用 diff 校验）、改动文件数或 token 超出预算、报告与 diff 不符；③预算：`--max-budget-usd`、`maxTurns`、`--print-timeout`、按 run 累计 token，超限先 steer 收尾，再 interrupt；④人类中断点：`input-required`/"Needs input" 作为一等状态，持久化待决事件（LangGraph 式），审批请求统一路由到 xay UI；⑤纠偏通道：Codex `turn/steer`、Claude 消息 socket、agy stream-json 的 stdin 下一轮、dsh 只能 interrupt 后用 `--session-id` 续接。

## 4. 资源优化

- **长驻 server vs 每任务新进程**：Codex app-server 一个进程承载多个 thread，空闲 30 分钟后卸载 [app-server](https://learn.chatgpt.com/docs/app-server)；Claude supervisor 托管后台会话，空闲约 1h 停进程、内存紧张时回收、崩溃重启，对话落盘可续接 [agent-view](https://code.claude.com/docs/en/agent-view)。建议：**Codex 用 1 个长驻 app-server 承载 N 个 thread**（省掉 N 次冷启动和 N 份 MCP 连接）；Claude/agy/dsh 每任务一个进程，但空闲即退、靠会话 ID 续接；全局并发信号量按 CLI 分别限流。
- **事件日志**：本机 `~/.codex/sessions` 已 1.5G。原始 NDJSON 按 run 分文件，run 结束后 gzip；只把归一化的小事件（kind/路径/摘要）写入账本；超过 N 天的 delta/token 级事件丢弃，只保留 item 级事件；定期快照 + 截断（类似 Kafka 按 key 压缩只保留最新值）[Kafka compaction](https://kafka.apache.org/documentation/#compaction)。Claude 的保留期由 `cleanupPeriodDays` 控制 [checkpointing](https://code.claude.com/docs/en/checkpointing)。
- **工作区隔离的轻量替代**：`git worktree` 共享对象库，只多一份工作树 [git-worktree](https://git-scm.com/docs/git-worktree)；配合 `--no-checkout` + `sparse-checkout set --cone <dirs>`，只检出任务涉及的目录 [sparse-checkout](https://git-scm.com/docs/git-sparse-checkout)；`node_modules`/`.venv`/构建缓存用 APFS `cp -c`（clonefile，写时复制，跨卷时会退化为普通复制）[eclecticlight](https://eclecticlight.co/2020/04/14/copy-move-and-clone-files-in-apfs-a-primer/)；注意 Python 的 `shutil.copyfile` 不会 clone [cpython#140489](https://github.com/python/cpython/issues/140489)。macOS 没有原生 overlayfs，不建议使用；只读任务（review/调研）直接在主 checkout 上跑，不建 worktree。注意：Claude checkpoint 不恢复硬链接/符号链接（pnpm 的硬链接会被跳过）[checkpointing](https://code.claude.com/docs/en/checkpointing)。
- **产物保留**：分三级。① 账本 + report.json 永久保留（几 KB）；② diff/patch 与 trace.md 保留 30 天；③ 原始事件流和 worktree 在合并或放弃后立即清理（worktree 先 `git worktree remove`，失败的 run 保留 7 天以便复盘）。按大小上限做 LRU 清理，并提供 `xay gc --dry-run`。

## 对 xay 最重要的 8 条启示

1. **适配层采用"ACP 语义 + 原生通道实现"**：统一接口 `start/prompt/steer/interrupt/approve/resume/status`，Codex 走 app-server（有 `turn/steer`），Claude 走 stream-json + 消息 socket + hooks，agy 走双向 stream-json，dsh 走 headless `--json`；用 capability 探测代替版本判断。
2. **任务状态机照抄 A2A/Claude**：`submitted→working→(input-required|auth-required)→completed|failed|canceled|rejected` + `stalled`；"需要人"是一等状态，UI 只需推送这一类。
3. **纠偏分三档**：先 steer（不打断工具），再 interrupt（SIGINT 或 `turn/interrupt`，不要直接 TERM），最后用 `resume`/新会话 + 工作线文件续接；每次纠偏都写进账本。
4. **看门狗用确定性规则，不靠 LLM 自评**：事件超时、OpenHands 五类循环模式、路径越界、预算超限、Magentic 式停滞计数 >2 触发重规划或换执行者。
5. **派发和依赖由代码执行，LLM 只负责拆解和评审**（CrewAI/MAST 的教训）；委派卡必须包含目标、输出格式、工具/路径边界、完成判定。
6. **文件即总线**：任务板 + 文件锁认领（Claude teams）、产物落盘只传路径（Anthropic）、NDJSON 账本作为唯一事实源（事件溯源），所有 UI 都是投影；零常驻依赖，可移植。
7. **回滚统一用 git，不依赖各家 checkpoint**（Bash 改动、子 agent 改动、硬链接都不在其覆盖范围内）；每轮派发前打快照，worktree + sparse + `cp -c` 降低成本。
8. **资源上，长驻进程只给能复用的一方**（Codex app-server 多 thread），其余执行者空闲即退、按会话续接；原始流 gzip + 分级保留 + `gc`；多 agent 约耗 15 倍 token，lead 只读 brief，worker 回传 ≤2k token 摘要。
