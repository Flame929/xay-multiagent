# xay 目标架构 v3：从"接 CLI + 文档约束"到"有体系的多 AI 协作"

> 日期 2026-10-29。读者：负责人（lead）与实现 v3 的执行者。约束不变：只用 bash/jq/git/Python 标准库；执行者是黑盒 CLI；不装 claude CLI；不碰账号/指纹/token；执行者不 commit/push、不读密钥。

## 1. 问题诊断：五类根因

**R1 交接带宽太窄（信息瓶颈）**
执行者几十分钟的工作被压成 15 行自由文本；中间的工具调用、读过的文件、放弃的方案、未经证实的假设全在 `events.raw` 里躺着没人看。lead 要么盲信，要么自己读原始日志（最贵）。

**R2 没有共享的"任务真相"（契约缺失）**
任务理解只存在于 lead 脑中和一张自由格式任务卡里。接口、不变量、文件归属、验收口径没有机器可读的载体，`{{result:ID}}` 传的是上游"说了什么"而不是"约定了什么"，下游各自脑补，于是互相矛盾。

**R3 状态是"运行级"而非"目标级"（没有层级）**
`registry.jsonl`/`meta.json` 只认识"一次 run"。工作线、目标、计划、返工轮次之间没有链接，多会话并行时无法回答"这个目标现在到哪一步、谁在改哪些文件、上一轮为什么失败"。

**R4 冲突只在落地时按文本检测（语义盲）**
worktree 只能发现同一处代码的文本冲突。两个任务基于相反的假设各改一处（例如一个把 API 改成异步、另一个按同步写测试）不会报错，等到 lead 验收才炸。

**R5 记忆只有一个静态文件（上下文衰减）**
`.xay/context.md` 是手写的、无版本的；决策、已知坑、否决过的方案没有归宿；agy 失忆、`-r` 续接超过几轮后漂移，都是因为没有"可重放的外部记忆"。scorecard 靠 lead 手工追加，几乎不会被执行。

## 2. 可借鉴的范式：借什么、用在哪

| 范式 | 借什么 | 用在 xay 哪里 |
|---|---|---|
| **黑板系统** | 共享黑板 + 知识源各自读写 + 控制器决定下一步 | `<项目>/.xay/` 升级为黑板：契约、账本、报告、追踪都是文件；执行者读黑板而不是读任务卡里复述的背景；lead 是控制器 |
| **全局工作空间理论（GWT）** | 注意力瓶颈：大量无意识加工，只有"胜出"的内容广播到工作空间 | 执行者的事件流是无意识加工；`xay-brief` 按"惊讶度"打分（偏离、低置信、冲突、未决）只把胜出条目广播给 lead。lead 的 token 就是工作空间容量 |
| **预测编码 / 主动推理** | 自顶向下的预测 vs 自底向上的误差，只传误差 | 契约 = 预测；报告里的 `assumptions`/`deviations` 与契约比对 = 预测误差；误差为零的任务 lead 不用看。返工 = 修正预测（改契约）或修正行动（改代码） |
| **事件溯源** | 只追加的事实日志，状态是投影 | `.xay/ledger.jsonl` 记录所有事实（派活、完成、落地、冲突、裁决）；看板、brief、scorecard 都是投影，不再各自维护状态 |
| **Actor 模型 / 监督树** | 失败隔离 + 分级重启策略 | `xay-run` 是最底层监督者（重试、超时、换强度）；`xay-dag` 是中层（跳过/替换下游）；lead 是顶层（换 AI、改契约、问用户）。失败按类型走不同策略，不是统一"返工" |
| **契约优先 / 共享世界模型** | 先定接口与不变量，再分头实现 | 已有"先定接口再分头"的口头原则，落成 `contracts/*.md` 文件并被任务卡引用、被 lint 检查、被报告回填 |
| **ADR（架构决策记录）** | 决策 + 原因 + 否决项，只追加 | `.xay/memory/decisions.md`，执行者报告里的 `decisions` 自动追加，替代手写 context |
| **Saga / 补偿** | 长事务拆步，每步可补偿 | 落地（land）就是提交点；失败丢 worktree 是补偿。v3 只补"落地前检查"，不做更复杂的事务 |

**不适合的**：合同网（竞标）——路由按特长静态决定，让三家竞标白烧 token；CRDT/自动合并——代码语义冲突不能自动收敛，仍由 lead 裁决；消息总线/守护进程——违背"无服务依赖"，文件 + 锁足够；执行者之间直接对话——黑盒 CLI 无法可靠互调，而且会绕过 lead 的控制，所有协调都经黑板。

## 3. 目标架构

### 3.1 组件图

```
 Claude 会话 A (工作线 wl-a)      Claude 会话 B (工作线 wl-b)        ← lead 层：只读 brief，写契约与裁决
        │ xay-brief / xay-dag / xay-run / xay-arbitrate
        ▼
 ┌───────────────────────── 黑板  <项目>/.xay/ ─────────────────────────┐
 │ context.md      项目恒定背景（现有）                                  │
 │ worklines/<wl>.md   工作线：目标、当前契约、未决问题、下一步            │
 │ contracts/<c>.md    契约：接口/不变量/文件归属/验收命令（含 YAML 头）     │
 │ ledger.jsonl    事件账本（只追加；所有脚本写、所有视图读）               │
 │ memory/decisions.md  ADR；memory/pitfalls.md 已知坑；memory/glossary.md │
 │ runs/<run>/     events.raw  report.json  trace.md  brief.md  meta.json  │
 │ dag/<dag>/      plan.json  summary.md                                   │
 └───────────────────────────────────────────────────────────────────────┘
        ▲ 适配器：codex-json / opencode-json / agy-stream-json → 统一事件
        │
   xay-run ──► codex exec --json │ dsh-run --json │ agy stream-json   （黑盒执行者，各自 worktree）
        │
   xay-land（落地前：文件归属 + hunk 重叠 + 假设冲突检查） → 原项目未提交改动
 全局：~/.xay/registry.jsonl（加 workline/goal/contract 字段）  ~/.xay/slots（名额，现有）
```

### 3.2 核心数据模型

层级：**工作线 → 目标 → 契约 → 任务（卡） → 运行 → {事件, 产物, 决策, 假设}**。每层是一个文件或账本条目，用 ID 互相引用；没有内存状态。

- `worklines/<wl>.md` YAML 头：`id, title, session_hint, goal, contract, status(active|blocked|done), owner_files[], open_questions[], updated`；正文是 lead 写的"当前认识"（≤30 行）。
- `contracts/<c>.md` YAML 头：`id, workline, version, interfaces[]（签名/路径）, invariants[], ownership{路径glob: 任务ID}, verify[]（命令）, assumptions[]`；正文为可读说明。任何任务卡必须 `contract: <id>`；修改契约只能由 lead 做并 `version+1`，账本记 `contract.changed`。
- 任务卡（plan.json 的 task 或 `xay-run` 的输入）新增字段：`contract, writes[]（声明要改的 glob）, reads[]（可选）, verify[], inputs[]（依赖的 run/报告）, done_when（一句话）`。`xay-run` 把这些渲染进 prompt，并存 `runs/<run>/card.json`。
- `runs/<run>/report.json`（执行者输出，见 3.3）、`trace.md`（机器提取）、`brief.md`（给 lead 的摘要）。
- `ledger.jsonl` 每行：`{ts, type, workline, contract, task, run, agent, by(lead|xay-run|xay-dag|xay-land|agent), data}`。事件类型：`task.dispatched / run.started / run.finished / report.parsed / report.invalid / land.ok / land.conflict / assumption.recorded / decision.recorded / contract.changed / verdict(accept|rework|escalate) / scorecard`。
- 存储论证：全部 jsonl + jq。单项目账本每条约 300B，1 万条才 3MB，`jq -c 'select(.workline=="wl-a")'` 毫秒级；SQLite 推迟到账本 >5 万行或需要跨项目联查时再引入（标准库自带，不算新依赖，但要额外写迁移和投影脚本，现阶段不值）。

### 3.3 执行者结构化交接协议

**输入（任务卡 prompt 的固定骨架）**，由 `prompts/card.md` 模板渲染：
```
[契约] 读 .xay/contracts/<c>.md（版本 n），你只能改 <writes>，其他文件只读。
[背景] .xay/context.md、.xay/worklines/<wl>.md 自己读；上游结果：<inputs 的 report.json 摘要字段>
[任务] <正文>    [做完的标准] <done_when>    [验收命令] <verify>
[汇报] 结束时在最后输出一个 ```xay-report 代码块（JSON，schema 见下）；之前可以写 ≤15 行人话。
```
**输出 `report.json` schema**（必填 * ）：
```
status*        done | partial | blocked | failed
summary*       ≤3 句
changed_files* [path]                       —— 与 git diff 对照，不一致记 report.invalid
verification*  [{cmd, passed:bool, tail}]   —— tail ≤5 行
assumptions    [{text, basis: card|contract|code|guess, confirmed:bool}]
decisions      [{text, alternatives:[..], why}]
deviations     [{from: card|contract, what, why}]   —— 与契约不一致之处，必须显式写
open_questions [text]                        —— 需要 lead 决定的
confidence*    0-1，并一句理由
needs_lead*    bool                          —— true 则 brief 置顶
risk_touched   [path 或 主题]                —— 碰到了归属之外的代码/数据
```
解析规则：`xay-run` 取最后一个 `xay-report` 围栏块 → `python3 -c json.loads` 校验必填 → 写 `report.json`；缺失或非法时 `status=parse_failed`，自动用 `-r` 续接一次"只补 JSON"（agy 例外：新会话 + 把原汇报贴进去），仍失败才降级为旧式 15 行。`changed_files` 与 `git diff --name-only` 的差集进 `deviations`。

### 3.4 从事件流提取"思考摘要"（不花 lead token）

`xay-trace <run>`：纯 jq/Python，把三种事件流归一为 `{t, kind, detail}`，kind ∈ `read, write, cmd, reasoning, message, denied, error`：
- codex `--json`：`item.completed` 中 `reasoning` 条目 → reasoning（截 200 字）；`command_execution` → cmd(+exit code)；`file_change` → write；`agent_message` → message。
- dsh（opencode）：`type=tool` 的 `name/args` → read/write/cmd；`type=text`/`reasoning` 段 → reasoning；`type=final` → message。
- agy stream-json：`thought`/`tool_call`/`tool_result`/`denied_actions` → 对应 kind。
输出 `trace.md`（≤80 行）：时间线按"阶段"折叠（连续 read 合并为"读了 N 个文件：…"），突出：**首次写文件前的最后一段 reasoning**（= 方案决定点）、每次 cmd 失败后的 reasoning（= 调试转折）、denied/error。
`xay-brief <run|dag|wl>`（≤40 行）合并 `report.json + trace 高亮 + git diff --stat + 契约比对`，按惊讶度排序：`needs_lead > deviations > land.conflict > verification 失败 > confidence<0.6 > open_questions > 其他`。零惊讶的任务只占一行。
可选的第二层（阶段 3）：`--digest` 用 `dsh -e low --readonly` 把 trace.md 压成 10 行"决策/假设/弃用方案"，花的是 dsh 的钱。

### 3.5 一致性机制

1. **计划期**（`xay-dag --lint`，默认开启）：同批次任务的 `writes` glob 相交 → 拒绝；任务未声明 `contract` 而 DAG 内 >1 个写任务 → 警告；`{{result:ID}}` 改为默认注入上游 `report.json` 的 `summary/assumptions/changed_files`，而非 15 行原文（`{{result_raw:ID}}` 保留旧行为）。
2. **运行期**：prompt 中写明归属；`xay-run` 结束时 `git diff --name-only` 超出 `writes` → `deviations` + 账本 `scope.violation`，不自动落地，等 lead。
3. **落地期**（`xay-land`）：除现有文本冲突外，比对同一工作线/契约下已落地 run 的 `assumptions` 与本 run 的 `assumptions`/`decisions`：关键词级（接口名、文件名、`sync/async`、字段名）粗检，命中则 `land=suspect` 并在 brief 置顶，由 lead 裁决。不做 LLM 自动判断。
4. **仲裁**（`xay-arbitrate`）：lead 用一条命令记录裁决：`accept | rework <run> | contract-change <c> | escalate-user`，写账本 + 更新契约版本 + 自动生成返工卡（见 3.7）。契约版本变更后，引用旧版本的进行中任务在 brief 标"契约已变"。

### 3.6 记忆与上下文传递

- **三级记忆**：恒定（`context.md`，手写）→ 累积（`memory/decisions.md`、`pitfalls.md`，由报告的 `decisions`/失败归因自动追加，lead 周期性让 dsh 去重压缩）→ 工作记忆（`worklines/<wl>.md`，lead 每轮用 `xay-brief --update-wl` 半自动更新"当前认识 + 下一步"）。
- **传递规则**：执行者读文件（花它们的 token）；lead 只读 brief。续接 `-r` 时自动在追问前附上该 run 的 `report.json` 摘要和契约版本号，防漂移；agy 一律新会话 + 工作线文件 + 契约，不靠它自己的记忆。
- **跨会话（多工作线）**：`registry.jsonl` 加 `workline/contract` 字段；`xay-brief --all` 按工作线分组列出"别的会话正在改哪些文件、持有哪些契约"，新会话开工前先看这一屏，就能避开归属冲突。

### 3.7 失败与返工策略（分级监督）

| 失败类型（由 report/trace 判定） | 第一响应（xay-run 自动） | 第二响应（xay-dag） | 第三响应（lead） |
|---|---|---|---|
| `parse_failed` 汇报格式错 | `-r` 补 JSON 一次 | — | 看旧式汇报 |
| `verification` 失败 | 原会话 `-r` 带失败 tail 返工 1 次 | 下游标 `blocked` | 升强度或换 AI |
| `scope.violation` 越界改文件 | 不落地 | 阻塞依赖它的任务 | 收窄或改契约 |
| `deviation` 偏离契约但自测通过 | 落地标 `suspect` | 正常 | 裁决：接受并改契约 / 返工 |
| `blocked` 需要决策 | 不重试 | 冻结该分支 | 回答 open_questions，重派 |
| 超时 / at capacity / 配额 | 按现有降级（中转、换 AI） | 换 agent 重排 | — |
| agy 失忆（续接 >2 轮或输出与卡无关） | 新会话 + 工作线文件 | — | 拆小 |
返工卡由 `xay-rework <run> "<lead 意见>"` 自动生成：原卡 + 失败 `verification.tail` + lead 意见 + "契约版本 n 未变/已变"。同一任务 3 轮未过 → 账本 `escalate`，brief 置顶，lead 换 AI 或交用户。所有裁决写账本，scorecard 从账本投影（`xay-scorecard`），不再手写。

## 4. 分阶段路线

### 阶段 1（1–2 天，执行者可完成，收益最大）：把交接做成结构化
改/新增：
- `prompts/report-format.md`（新）：xay-report JSON schema 与示例；`xay-run` 在现有执行准则后附加。
- `scripts/xay-run`：解析围栏块 → `report.json`；校验必填；parse_failed 自动补一次；`changed_files` 与 diff 对照；`meta.json` 加 `report_status, confidence, needs_lead`；输出末尾改为打印 brief 路径。
- `scripts/xay-trace`（新，Python 标准库）：三种事件流 → `trace.md`，含三类高亮。
- `scripts/xay-brief`（新）：`<run|dag 目录>` → `brief.md`，惊讶度排序，≤40 行；`xay-dag` 的 `summary.md` 改为由各 run 的 brief 拼成。
- `scripts/xay-dag`：task 支持 `writes/verify/contract/done_when` 字段（可选）；`--lint` 检查 writes 相交；`{{result:ID}}` 注入 report 摘要，`{{result_raw:ID}}` 保旧。
- `SKILL.md`：§5/§6 改为"先读 brief.md，再按需看 report.json/trace.md"；任务卡要点加 `writes/done_when`。
验收：
- 对 codex/dsh/agy 各派一个真实小任务，三者 `report.json` 均通过校验；故意让一个不输出 JSON，观察自动补齐。
- `xay-trace` 对三种 `events.raw` 样本运行无异常，`trace.md` ≤80 行且包含"首次写文件前的 reasoning"条目。
- 一个两任务 DAG 故意让 `writes` 相交，`--lint` 拒绝；去掉相交后正常运行，`summary.md` 由 brief 拼成且"零惊讶任务只占一行"。
- 单元测试：`python3 -m unittest discover scripts/tests`（新建，用三份固定事件样本）。

### 阶段 2（约 1 周）：黑板、契约、账本、工作线
- `scripts/xay-ledger`（新）：`append` / `query --workline --type --since`；`xay-run/xay-dag/xay-land` 全部改为写账本；`registry.jsonl` 加 `workline/contract`。
- `scripts/xay-wl`（新）：`new/show/update`，维护 `worklines/<wl>.md`；`xay-run/xay-dag` 加 `--wl` 并记入账本；`xay-brief --all` 跨工作线视图。
- `scripts/xay-contract`（新）：`new/bump/show`，YAML 头校验；`xay-dag --lint` 校验 `contract` 存在、`writes` ⊆ 契约 `ownership`。
- `scripts/xay-land`：归属越界不落地；假设关键词冲突 → `suspect`。
- `scripts/xay-arbitrate`、`scripts/xay-rework`（新）；`memory/decisions.md` 自动追加。
- `xay_watch.py`：改读账本投影；任务页展示 brief 与契约版本。
验收：两个 Claude 会话同时在同一项目各开一条工作线，故意归属重叠 → 第二条工作线派活前 `xay-brief --all` 可见占用，`--lint` 拒绝；一次假设冲突（同步/异步）被标 `suspect`；`xay-arbitrate rework` 自动生成返工卡且 `-r` 续接成功；账本可重放出看板所有状态（删除 meta.json 后看板仍正确）。

### 阶段 3（按需）：便宜的二级摘要与自我校准
- `xay-brief --digest`：dsh 低强度压缩 trace → 10 行决策/假设；`xay-scorecard`：从账本投影每个 AI 的通过率/返工率/越界率，按任务类型分组，自动写回 `scorecard.md`。
- 契约驱动的交叉审查卡：审查者拿到契约 + report 的 assumptions，专查"假设是否成立"。
- 账本超过 5 万行或需要跨项目查询时，再评估 SQLite 索引（只做投影，不做真相源）。
- 回放测试：用历史 `events.raw` 回归 `xay-trace/xay-brief`。
验收：连续 10 个真实任务中 lead 读取的字符数（brief 总长）比 v2 的 `summary.md + result.md` 下降 ≥50%，且验收漏判不增加（抽查）。

## 5. 不做什么、风险与权衡

**不做**：守护进程/消息队列/数据库服务；执行者互相直接对话或竞标；LLM 自动合并冲突或自动裁决；lead 的子 Agent 并行；自动 commit/push；绕过任何额度或检测机制；读取或输出密钥（trace 提取时对 `cmd` 参数做 `KEY|TOKEN|SECRET|PASSWORD` 脱敏）。

**风险与权衡**：
- 结构化汇报让执行者多花输出 token，且弱模型可能把 JSON 写错 → 用围栏块而非整段 JSON，解析失败自动补一次，仍失败降级，不阻塞主流程。
- 事件流格式随 CLI 升级变化 → 适配器分文件（`scripts/adapters/{codex,dsh,agy}.py`），用固定样本做回归测试，解析失败只降级 trace，不影响 run。
- 契约增加 lead 的前期写作成本 → 契约只在"≥2 个写任务并行"或"跨工作线"时强制；单任务可省略，`--lint` 只警告。
- 假设冲突的关键词粗检会误报 → 只标 `suspect`，不阻塞落地；误报由 lead 一句 `accept` 清除并进账本，供日后调阈值。
- 账本只追加会膨胀 → 阶段 2 提供 `xay-ledger compact`（按工作线归档到 `ledger-<月>.jsonl`）。
- 多工作线共享 `ledger.jsonl` 的并发写 → 用 `mkdir` 锁 + 单行 `>>` 追加（与现有名额锁同一套路）。
- 中转站可见代码的问题不因本架构改变 → 契约/工作线文件同样会被执行者读到，机密项目仍需 `--source sub`。
- GWT/预测编码只是设计隐喻，不要为了类比过度工程化；判定标准始终是"lead 读的字符数下降、漏判不增加"。
