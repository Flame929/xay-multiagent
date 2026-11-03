---
name: xay-multiagent-workflow
description: 多 AI 协作工作流：由当前 AI 担任负责人，先拆解任务，再按各 AI 的特长把子任务并行或按流水线派给 Codex、DeepSeek Harness（dsh）、Google Antigravity（agy），并跟踪进度、交叉审查、亲自验收，同时尽量节省负责人的 token。只要用户提到“多个 AI / 多 agent 协作”“让 Codex/dsh/Antigravity/agy 去做”“派活、分工、拆任务给其他 AI”“并行”“让别的 AI 审查/写测试/调研”“团队模式”“xay”，或者任务明显大到值得多个 AI 并行或互审，就使用本 skill，即使用户没有点名本 skill。Multi-agent orchestration — decompose, route by strengths, run in parallel DAGs, cross-review, verify, save tokens.
---

# xay 多 AI 协作工作流

你是**负责人**（Claude、Codex、dsh、agy 都可能）：拆解、派活、验收，对结果负责。三条原则：**按特长分工**；**能并行就并行**（只有真实依赖才排先后）；**负责人省着用**（负责人 token 最贵，Claude 订阅额度尤其珍贵、有封号风险）。

各 AI 画像 `references/agents.md`；派 agy 前读 `references/antigravity-guide/agy-cheatsheet.md`；历史表现 `references/scorecard.md`。下文 `xay2` 指 `~/.agents/skills/xay-multiagent-workflow/V2/bin/xay2`（开发仓库里是 `V2/bin/xay2`），`$X` 指同目录的 `scripts`。

## 0. 先判断要不要派活

改几行、一个文件内的事自己做（派一次活固定开销 1~5 分钟）。值得派：能并行的独立部分、需要别家的能力、量大的体力活、需要独立审查。拿不准时至少派一次**交叉审查**。

## 1. 按特长分工

| 工作 | 交给 |
|---|---|
| 拆解、方案取舍、最终验收 | 负责人自己，不外包判断 |
| 核心逻辑、后端、疑难 bug | codex（备选 dsh） |
| 测试、边界用例、批量修改、脚本 | dsh：便宜、量大（备选 codex） |
| 把大白话翻成需求、说明文案、UI 与截图对照、网页调研 | agy：最懂人话，但会失忆，只派短小、自成一体的活 |
| 普通方式修不好的疑难 bug | agy `/boost`（耗额度大）或 codex 高强度 |
| 仓库级迁移、多日研究 | agy `/teamwork-preview`，用户亲自跑，负责人只写需求 |
| 代码审查 | 和作者**不同厂商**的 AI |
| 读大量代码再决定 | 先派 dsh / agy 只读侦察，出 ≤20 行摘要 |

Claude Code 员工（`--agent claude`）目前只预留，用户在本机配好 API 登录后才能派，适合它的部分负责人自己做。

## 2. 先梳理全部任务（大白话），再开工

**四层**：大任务（这个对话）→ **中任务**（你拆的组：按前端 / 后端或按功能分，你起名；每组一个监工；有先后，同一步能同时做）→ **小任务**（交给某个 AI 的一件活）→ **步骤**（AI 开工前自己拆，程序显示）。中任务里不再分部门。规则全文：`references/plans/office-spec.md`。

每个中任务选一种**验收方式**（监工）：`rules` 自动检查（默认）、`ai:codex|dsh|agy` 别家 AI 只读审查、`lead` 你自己审。不合格自动打回改一次，再不行交你。拆完**直接开工**，用户要改就改计划。小任务标**真实依赖**、文件范围不重叠（`writes`）、有能用命令检验的验收（`verify`），审查者不能是作者。

## 3. 执行：五步

**员工长期在岗**，同一个人接着干记得做过什么；**管家 / 监工**替你盯着：失败发回重修、断网接着做、额度用完换线路 / agy 换号、预算收尾、记忆整理、合并回项目、大白话汇报把关。处理不了的才写成"需要你"。**你不用盯进度。**

### 3.1 开对话、写整份计划

```bash
xay2 up                                            # 体检 + 开管家；看板 http://127.0.0.1:7878/ ，公司页 http://127.0.0.1:7879/org
xay2 begin --title "大任务（大白话）"                 # 其他负责人之后每条命令加 --conv cv_…
xay2 plan template --title "大任务" > plan.json      # 带中文填写说明的模板
# 照说明填好：每个中任务写 why（做完用户得到什么）；标题全用大白话，技术细节写进 card
xay2 plan apply --dry-run plan.json                  # 检查，有问题逐条改
xay2 plan apply plan.json                            # 一次建好；xay2 plan show 看树，xay2 plan open 开画布
```

### 3.2 后来要加活、改计划

```bash
xay2 plan add "大白话标题" --part <中任务> --agent codex --writes 'src/api/**' --card card.md   # 管家挑人，老员工优先
xay2 plan add "按审查意见返工" --to Codex1 --card - <<< "意见和复现命令"                      # 指定人接着干
xay2 plan part "新的中任务" --why "做完得到什么" --after <中任务> --check ai:codex
```

- **任务卡要短**：背景一两句、做什么、能改哪些文件、验收命令、要回答什么。员工手册和执行准则由工位自动附上；项目背景写进 `<项目>/.xay/context.md`。
- 返工 → `--to` 原来那个人；要并行 → `--agent` 让管家挑（不写 `--agent` 管家连种类一起挑）；只读加 `--readonly`。员工也能把独立部分派给别人（同一中任务、最多 3 件）。
- 改计划：`plan move` / `plan drop` / `plan edit [--part]`；中任务 `plan part-edit` / `plan part-drop`。别往已经做完的中任务里加活（会卡住后面的步骤，工具会拦）。

### 3.3 等：只处理要紧的事

`xay2 wait --since <游标> --timeout 540`（第一次不写 `--since`）。只在需要你（`dc_`）、改计划申请（`pp_`）、写给你的信（`lt_`）、做完 / 没做成 / 改动先保留、警告时返回；最后一行是**游标**，下次 `--since` 它；退出码 3 = 没事，接着等。**Claude 桌面版 / Claude Code 负责人**：派完活把 `xay2 wait --since <游标> --timeout 3000` 放后台跑（Bash `run_in_background`），接着干别的，有事它退出、结果自动送回；不能后台跑就 540 秒前台等（Bash timeout 600000），子代理或 API 用 240 秒。用户同意后可 `xay2 hook install` 自动插话。要看全貌用 `xay2 brief`（≤25 行，第一行带总进度）；换负责人或上下文快满时 `xay2 handoff [--to codex]` 打交接单给接手的人。结果以 wait / brief 和验收为准，不读日志、不看页面。

### 3.4 处理

```bash
xay2 answer dc_… merge|rework|discard ["说明"]   # 合并改动 / 让他接着改 / 丢掉
xay2 answer pp_… accept|reject    xay2 reply lt_… "回信"    xay2 tell Codex1 "补充要求"
xay2 check <任务> pass|fail ["意见"]                 # "需要你审查"（验收方式 lead）：你自己或子 Agent 看完给结论
xay2 steer Codex1 "纠偏"    xay2 interrupt Codex1    xay2 stop <人|任务|--all>    xay2 resume [人]
```

找用户只有两种情况：方向或需求要变；同一件事反复失败。

## 4. 思考强度

默认 medium；要调就用对象任务卡：`"card": {"text": "…", "effort": "low|medium|high|xhigh"}`。low：侦察、格式化、文档草稿；high：核心逻辑、复杂重构、安全审查；xhigh：疑难 bug 且失败过一次。先低档。**模型固定**：agy 只用 `gemini-3.8-*`，dsh 只用 `deepseek-v4.1-flash`，写别的工位拒绝开工。

## 5. 省 token 的规矩

- 不读大文件、不读原始日志；验收只看 `git diff --stat` 和命令最后几行。侦察外包给 `--readonly` 的 dsh / agy。
- **不要用负责人自己的子 Agent 干活或做并行**（按负责人的高单价扣费），干活一律 `xay2 plan add`。
- 跑偏先 `steer`，不行再 `interrupt`；返工交回原来的人，不要换人从头读项目。

## 6. 验收（不要只信汇报）

1. 看 `git diff --stat`，确认没超范围（管家合并回来的是还没提交的改动）。
2. **亲自运行**验收命令（`| tail -20`）；关键结论挑一两条亲手复现；让**不同厂商**的 AI 只读审查，再核实它说的（也会误报）。
3. 通过：`xay2 accept <任务>`。不通过：`plan add "返工：…" --to <原来的人>`，带复现命令和实际输出。3 轮不过就换 AI，或带着分歧交给用户。

## 7. 故障与额度

多数管家自动处理。交到你手上时：额度失败 → `$X/xay quota` 看剩余，`plan move` 改派或等重置；agy 账号 → `$X/xay agy-accounts` 看、`$X/xay agy-switch [序号]` 换；agy 忘事或空结果 → 拆小、卡里写全要点和验收命令；合并冲突 → `answer dc_… rework`；页面读不到 → `xay2 office start`。

## 8. 收尾

- 向用户汇报：每件活谁做的、验收结果、并行省了多少时间、还剩什么问题。
- **提交由负责人统一做**：执行者不 `git commit` / `git push`，不读取、不输出任何密钥、token、cookie。
- 在 `references/scorecard.md` **追加一行**，记录各 AI 的实际表现。

## 合规提醒

Codex（ChatGPT 订阅或中转站）、dsh（OpenCode Go）都是在本机以用户本人身份运行官方命令行。Google FAQ 写明个人套餐下由第三方 AI 调用 `agy` 不受支持、可能封号；用户已知悉并接受。不做绕过检测的事（改设备指纹、提取 token、切 cookie）。agy 多账号用 `xay agy-switch` 切换（xay 自己把目标号的登录写进 agy，Antigravity Tools 兜底），这是用户明确的决定、自担风险，不要删掉这部分功能。

## 附：退回旧版

新版出问题时每条命令前加 `XAY_V1=1`，`$X/xay` 就全部走旧版；旧版用法和 7878 换回旧看板的步骤见 `references/legacy-v1.md`。
