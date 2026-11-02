# 各 AI 画像与调用方法

> 依据：用户的长期使用体验（最重要）、公开评测，以及 2026-10-28 在本机的实测。
> 模型和额度会变，表现以 `scorecard.md` 的最新记录为准；发现与本文不符时更新本文。

## 目录
1. Claude（Claude Code 桌面版）
2. Codex
3. DeepSeek Harness（dsh）
4. Google Antigravity（agy）
5. 写任务卡的要点

---

## 1. Claude（Claude Code 桌面版，Claude 订阅）

- **强项**：编排和计划能力最强，拆解、权衡方案、发现隐含依赖、验收判断都很好。
- **弱点**：订阅额度少，而且有封号风险。
- **用法**：只当负责人。不做大量阅读、批量修改这类体力活；侦察、实现、测试都外包；并行用 `xay-dag`，不用 Claude 自己的子 Agent。
- 本机**没有 claude 命令行**。其他 AI 当负责人时不能调用 Claude；需要 Claude 参与时，建议用户在 Claude 桌面版里做。

## 2. Codex（OpenAI，ChatGPT Plus 订阅 / 中转站）

- **强项**：实现精准，能啃算法、后端、疑难 bug，会自己验证（实测：写完代码会主动写临时脚本验证）。
- **弱点**：
  - **过度防御**：多余的 try/except、兜底分支、参数校验、警告和确认流程，测试也写得过多。
  - **需要清晰、以结果为导向的提示词**才能发挥全力。
  - `xay-run` 会自动附加 `prompts/codex.md`（参考 OpenCode 内置 GPT 提示词提炼）：直接行动、不加没要求的防御代码、不加免责声明、测试适度。
- **任务卡写法**：写清“做完的标准”和“范围”，不写步骤；写“直接实现，不要先给计划”。
- **思考强度**：`-e` 对应 `model_reasoning_effort`（minimal/low/medium/high/xhigh）。xhigh 很慢。
- **额度来源**：
  - 默认先走订阅（Plus）。实测 Plus 经常返回 “Selected model is at capacity”：额度明明只用了 0–1%，是 OpenAI 算力紧张时 Plus 的优先级低，2026 年 10 月社区里大量出现同样的报告。这时脚本会自动改走中转站（`model_providers.custom`）。
  - 中转站能看到全部代码和提示词，**机密代码不要走中转站**（用 `--source sub`，失败就等或者换 AI）。
- **调用细节**：
  - 新任务：`codex exec --json -C <目录> -c model_provider=… -c model_reasoning_effort=… "<任务>"`
  - 续接：`codex exec resume --json … <会话ID> "<追问>"`（resume 不支持 `--profile`）
  - 只读：`-c sandbox_mode="read-only"`
  - 事件格式：`thread.started.thread_id` 是会话 ID；`item.completed` 里 `agent_message` 是汇报；`turn.failed` 表示失败。

## 3. DeepSeek Harness（dsh，DeepSeek V4，OpenCode Go 额度）

- **公开评价（2026 年 8 月开源，至今仍是预览版）**：
  - 架构灵活，“一切皆插件”，可以把 Claude Code / Codex 当作它的子 Agent。
  - 速度和成本：XDA 的测试里 15 秒完成，缓存命中 82%；但 DataCamp 在 Windows 上测出来比 Claude Code 慢。不同环境结果不一。
  - 多篇评测认为它更像“搭 Agent 平台的底座”，还不是打磨好的日常编码工具；README 明确写着会有不兼容的改动。
- **本机实测**：写测试、找边界用例很细（主动找出 6 个问题，31 个用例全部通过），响应快（10 到 20 秒）。
- **用法**：便宜的主力劳动力，负责测试、批量修改、脚本、侦察摘要、map-reduce 式大面积改动，可以同时开 2 到 3 个实例。**不要让它独自负责关键的核心逻辑**，要么交给 codex，要么必须安排交叉审查。
- **思考强度**：`-e` 通过临时配置文件设置 `reasoningEffort`（off/minimal/low/medium/high/xhigh/max），已实测生效。
- **模型**：**只能用 `deepseek-v4.1-flash`**（支持看图），`xay-run` 会拒绝其他模型。需要更强时调高 `-e`，不要换模型。
- **额度**：OpenCode Go 按月美元额度计算，**每 5 小时最多用月额度的 20%，每周最多 50%**。并行实例多的时候要留意。
- **只读**：没有命令行级别的只读开关，`--readonly` 只是文字约束，事后要检查改动文件。
- **调用细节**：
  - `dsh-run --patch <强度配置> --json [--session-id <ID>] "<任务>"`（`dsh-run` 会读取 OpenCode 密钥后启动 headless profile）
  - 事件格式：`type=session` 的 `sessionId` 是会话 ID；`type=final` 的 `text` 是汇报。

## 4. Google Antigravity（agy，只用 Gemini 3.8）

- **强项**：
  - **最懂人话、最会说人话**（用户评价：比 Claude 还好）。
  - 效率高。
  - 多模态（截图、设计稿），内置浏览器做调研和 UI 检查。
  - `/boost` 多 Agent 深度推理。
- **弱点**：**长上下文不行，经常失忆，压缩一次会丢掉大部分记忆。**
- **用法**：
  - 派**短小、自成一体**的任务，所有必要背景都写在任务卡里或 `.xay/context.md` 里。
  - 不要让它做长链条任务；续接最多 2 轮，之后开新会话。
  - 较长的任务加 `--long`，让它把进度写进 `.xay/notes/`。
  - 最适合：把用户模糊的需求翻译成清晰的 spec、写给用户看的说明/文案/最终汇报草稿、命名、UI 截图对照、网页调研、`/boost` 攻坚。
- **模型限制**：**只能用 `gemini-3.8-flash-low / -medium / -high`**，`xay-run` 会拒绝其他模型。`-e` 会自动选档位，并同时传 `--effort`。
- **斜杠命令**：`/boost`（加 `--full`，放在 worktree 里）、`/goal`、`/browser` 能在非交互模式下用；`/teamwork-preview`、`/plan` 需要交互，只能请用户亲自运行。详见 `antigravity-guide/agy-cheatsheet.md`。
- **工作区**：agy 只能读写自己工作目录里的文件，要读别的目录用 `--add-dir <目录>`（`xay-dag` 会自动给引用了 `{{dir:ID}}` 的 agy 任务加上）。
- **权限**：非交互模式下，需要授权的操作会被自动拒绝，`denied_actions` 里会列出来（`xay-run` 会提示）。只读任务就写“不要运行命令”；需要跑命令时加 `--full`，或者在 settings.json 里放行具体命令。
- **合规**：个人套餐被第三方调用有封号风险（用户已接受）；不得改设备指纹、提取 token。多账号用 `xay agy-switch` 经 Antigravity Tools 切换（用户自担风险）。
- **输出 JSON**：`conversation_id`、`status`（`response` 为空时也会是 SUCCESS）、`response`、`denied_actions`、`usage`。

## 5. 写任务卡的要点（各 AI 通用）

- 写清**目标和做完的标准**，不写步骤；给文件路径，不贴大段代码。
- 写清**范围**：允许改哪些文件、禁止动哪些。这是并行时不互相冲突的关键。
- 验收标准写成**命令**，例如“`python3 -m unittest` 全部通过”。
- 返工时附上**复现命令和实际输出**，不要只说“有 bug”。
- 一张卡只放一个目标；要做三件事就拆成三张卡，放进 DAG 里并行。
