# 旧版 xay（V1）用法：退回旧版时看这里

> M5（2026-10-31）起负责人按 `SKILL.md` 用新版 `xay2`（V2：账本 + 管家 + 长期在岗的员工）。本文是切换前 `SKILL.md` 里旧版的用法，原样留着，
> 只在**新版出问题、要退回旧版**时用。旧版代码（`scripts/xay_cli.py`、`xay-run`、`xay-dag`、`xay_watch.py` 等）一行没删，照常能用。
> 2026-10-31 用户确认，旧版已标为 legacy：代码冻结、只作出问题时的退路，不再改。

## 怎么退回

| 想要 | 怎么做 |
| --- | --- |
| 只这一条命令走旧版 | `scripts/xay v1 <命令…>`（例：`scripts/xay v1 dept close fe`） |
| 这个会话里的 `xay` 命令全部走旧版 | 每条命令前加 `XAY_V1=1`（例：`XAY_V1=1 $X/xay wait --session "$LS_ID"`） |
| 不加也走旧版的命令 | 只有旧版才有的：`session / ctl / decide / doctor / status / quota / route / agy-accounts / agy-switch / workers / send / inbox / contract / supervise / escalate / report`；以及写了 `--session`、编号是 `m_` 开头、`dept create` 这类旧写法的 |
| 看一条命令会走哪边 | `scripts/xay --which <命令…>` |
| 7878 换回旧看板 | `XAY_V1=1 scripts/xay-deploy --skip-tests --restart-board`（会建 `~/.xay/v2/board.off`，管家以后不再占 7878；V2 公司页仍在 7879）。手动做法：`touch ~/.xay/v2/board.off`，`xay2 office stop && xay2 office start`，再 `python3 ~/.agents/skills/xay-multiagent-workflow/scripts/xay_watch.py --port 7878` |
| 再切回新版看板 | `scripts/xay-deploy --skip-tests --restart-board`（会删掉 `board.off`） |

`xay-run`、`xay-dag` 本来就是旧版的命令，不受分发影响。临时桥已删除：退回旧版后派的活只在旧看板上看得到，新版页面看不到。

---

## 3. 执行（旧版）：会话生命周期、派活与实时控制

负责人的完整执行循环为：**开启会话 → 派活（DAG / 单任务） → `xay wait` 事件等待循环 → 运行时控制 / 决策裁决 → 会话结束**。

### 3.1 开启会话与查看名额

任何 AI 担任负责人，在编排任务前先开启会话；查看名额请先运行 `xay doctor`：

```bash
X=scripts  # 或 ~/.agents/skills/xay-multiagent-workflow/scripts

# 1. 开启负责人会话（Claude 会话会自动识别，其余 AI 需显式声明 --lead）
LS_ID=$($X/xay session begin --lead <claude|codex|dsh|agy|unknown> --title "大白话会话标题")
export XAY_LEAD_SESSION="$LS_ID"

# 2. 查看当前可用并发名额与默认超时（以 doctor 输出为准，不假设固定数字）
$X/xay doctor
```

- **并发名额**：同一个 AI 在所有会话中同时运行的数量有上限。当前配置以 `$X/xay doctor` 输出的 `slots` 字段为准（如需调整可通过环境变量 `XAY_SLOTS_CODEX`、`XAY_SLOTS_DSH`、`XAY_SLOTS_AGY` 覆盖）。名额满时新任务自动排队等待名额。
- **防撞名**：同一秒、同名的运行目录和 DAG 目录会自动加后缀。

### 3.2 派活：批量 DAG 与单任务

**优先用 `xay-dag`**：把计划写成 JSON，一次调用就会自动并行、按依赖排序、把上游结果填进下游任务、最后输出汇总。

```bash
$X/xay-dag plan.json --dry-run   # 先看批次划分
$X/xay-dag plan.json --watch     # 执行并打开实时看板；汇总写在 .xay/dag/<时间>/summary.md
```

单个任务或者返工时用 `xay-run`：

```bash
$X/xay-run codex -n api -t "写接口" -e high "任务卡"
$X/xay-run dsh   -n tests -e low "任务卡"
$X/xay-run agy   -n review --readonly "任务卡"
$X/xay-run codex -r <会话ID> "返工意见"     # 在原会话里继续
```

#### 接着干还是开新的（和负责人自己的子代理一样）

每次派活都开新会话，AI 就要从头读一遍项目、忘掉上一轮试过什么。**有连续性的活交给同一个 AI 接着做**：

```bash
$X/xay-run dsh --as 表归属 -t "表归属清理第 1 批" "任务卡"   # 第一次：起名并开新会话
$X/xay send 表归属 "第 2 批：客户、时间线、工单"                # 之后：在它原来的会话里接着做；它还在忙就排队
$X/xay send 表归属 --now "先别改 core 表"                       # 正在做时实时插话（仅 codex 支持，其余自动改为排队）
$X/xay workers                                                    # 看这个对话里有哪些具名 AI、各做了几轮
$X/xay-run dsh --as 表归属 --fresh "任务卡"                      # 换新会话从头开始
```

DAG 里用 `"continue": "<任务 id>"` 在那个任务的会话里接着做（自动加进 deps），或用 `"worker": "<名字>"` 交给具名 AI。

| 接着干（`xay send` / `continue`） | 开新的（新名字或 `--fresh`） |
| --- | --- |
| 分批串行的同一件事（第 1、2、3 批） | 和前面无关的独立子任务 |
| 返工、按审查意见改、补测试、修它自己留下的失败 | 需要并行：同一个 AI 一次只做一件事，并行就起多个名字 |
| 重试失败的任务（它记得试过什么、卡在哪） | 换另一个 AI 做（改派），把上一轮汇报摘要写进任务卡 |
| 追问、让它解释或补充上一轮的结果 | 已经续了很多轮（约 8 轮以上）、上下文很长或方向走偏 |

规则：**同名同 AI**（不能把 dsh 的名字派给 codex）；审查者不能是被审查代码的作者；名字用大白话，看板上会显示成“固定搭档”。

**任务名必须写成大白话**：用户通过看板了解进度。
- plan.json 顶层写 `"title"`，表示整件事（如 `"给看板加多页面"`）。
- 每个子任务写 `"title"`（如 `"写服务端"`）。单任务用 `xay-run -t "大白话任务名"`。
- **任务卡要短**：只写背景一两句、任务、允许改的范围、验收命令。执行准则、汇报格式、只读约束由 `xay-run` 自动附加。
- **共享背景只写一次**：项目背景与测试命令写入 `<项目>/.xay/context.md`，执行者会自动读取。

### 3.3 事件等待循环（wait 循环）

**让用户随时看到进度**：派活后立刻在后台挂一个进度播报（Claude 用 Bash 的后台运行，其他 AI 用各自的后台/终端标签），它会每分钟列出“谁在干活、干了多久、正在做哪一步”，全部做完自动结束。**不要把派活命令的输出重定向到文件**，否则用户在后台任务面板里只能看到“无输出”。

```bash
$X/xay status --follow 60 --until-idle     # 后台运行；只看一眼就去掉 --follow
```

派活后，负责人切勿循环轮询日志文件，统一通过 `xay wait` 等待需要处理的事件：

```bash
# 阻塞等待事件，默认超时 240 秒
$X/xay wait --session "$LS_ID" --timeout 240
```

`xay wait` 会输出三类信息：
1. **`需要你：<标题或说明>`（Decision）**：出现阻塞性决策（如代码搬回冲突、最终失败需要裁决等）。负责人调用 `xay decide` 裁决：
   ```bash
   $X/xay decide <run_id> <option> --id <dec_id> --version <version> [--text "补充说明"]
   ```
2. **`提醒：<标题或说明>`（Signal）**：非阻塞告警（如长时间静默、可能死循环、代码越界等），负责人知悉并在必要时干预。
3. **`阶段结果：<status>（<title/run>）`**：任务结束通知（如 `done`、`failed`、`needs_you`、`orphaned` 等）。
4. **`无新信号，继续等`**：本次 wait 超时且无新事件。若任务仍在进行中，负责人应再次执行 `xay wait` 继续等待。

任务全部结束且完成验收后，关闭会话：
```bash
$X/xay session end --session "$LS_ID"
```

### 3.4 运行时控制（xay ctl）

任务执行过程中，负责人可通过 `xay ctl` 进行实时干预与控制：

```bash
# 1. 轮中插话（仅适用于支持 live steer 的执行者如 codex app-server；不打断执行，下一次采样可见）
$X/xay ctl <run_id> steer --text "纠偏提示或补充约束"

# 2. 打断重跑（打断当前执行，并带上新指示继续）
$X/xay ctl <run_id> interrupt --text "发现严重方向偏差，立即改用方案 B"

# 3. 恢复运行（继续被暂停或打断的任务）
$X/xay ctl <run_id> resume [--text "继续执行说明"]

# 4. 停下任务（立即终止并丢弃副本改动）
$X/xay ctl <run_id> stop
```

### 3.5 实时看板与独立工作区

**实时看板**：`xay-dag --watch` 会在后台启动看板服务，访问地址：`http://127.0.0.1:7878`。
- 负责人在桌面环境（如 Claude Code 桌面版）可提示用户或在内置浏览器中查看；普通终端环境直接把 URL 告知用户。
- 负责人不要靠看板来读结果，结果以 `summary.md` 和各任务汇报为准。
- 数据模型与接口契约详见 `references/watch-api.md`（投影自 `references/plans/arch-v4.md §3`）。

**自动独立工作区（默认开启）**：
- 会改文件的任务（非 `--readonly`）自动在独立 git worktree (`~/.xay/worktrees/`) 中运行。
- 成功后改动以未提交改动形式自动搬回原项目；失败或停止时 worktree 直接丢弃。
- **搬回冲突**：冲突时标记 `land=conflict`，保留 worktree。负责人可通过 `xay wait` 收到 `需要你` 提示，并在核对后手动合并或处理。
- 想关闭隔离：`xay-run --isolate off` 或 plan.json 中指定 `"isolate": false`。
- **系统通知**：任务或 DAG 完成时发送 macOS 桌面通知（测试时设置 `XAY_NOTIFY=0` 禁用）。

### 3.6 公司模式：总经理 / 部门负责人 / 员工（任务多、要长时间监工时用）

你是**总经理**：拆任务、组部门、定方向，只处理上报给 `gm` 的事。设计见 `references/plans/arch-v6-org.md`。

1. **看额度再分工**：`$X/xay quota` 看每条线路的真实剩余、重置时间和消耗速度。默认优先级：agy → Codex Plus → 中转站（最多用一半）→ dsh（省着用）。不确定派谁就 `$X/xay route` 或在 DAG 里写 `"agent": "auto"`。
   - **agy 有多个账号**：`$X/xay agy-accounts` 看每个账号的额度和优先级，`$X/xay agy-switch [序号|名字]`（不写就按优先级自动挑）随时换到额度充足的账号。派给 agy 时如果当前账号用完了，`xay-run` 会自动换号；你也可以主动调度。
2. **组部门**（不固定，按任务自由组合，做完就关）：
   ```bash
   $X/xay dept create fe --name 前端部 --mission "把订单页改成新设计" --owns 'web/**' --head claude
   $X/xay-run agy --as 小A --dept fe -t "订单页布局" "任务卡"
   $X/xay contract set order-api --owner be --file docs/order-api.md   # 部门之间的接口约定
   ```
   `--mission` 写清这个部门交付什么，xay 会把各部门的交付清单附进员工任务卡，用来对齐颗粒度；**并行的员工不要改同一批文件**（用 `--owns` 划清范围）。
3. **派部门负责人**：`--head claude` 的部门，用负责人自己的子代理当监工（Claude 用 Agent 工具，模型选 Sonnet，后台运行），提示词用 `prompts/dept-head.md` 填好部门信息。这是唯一允许用负责人子代理的场景；`--head rules` 的部门只靠规则监工。
4. **开规则监工**：`$X/xay supervise` 常驻（或每次等待前 `--once`）。失败重试、额度换线路、冲突重试或丢弃、沉默催进度都由它自动做，处理不了才交部门负责人。
5. **总经理只看自己的收件箱**：`$X/xay inbox --gm --wait 600`。部门之间的需求单由对方部门负责人一次拍板（不辩论），拍不了才上报你；你用 `xay answer <id> ...` 裁决。
6. **找用户只有两种情况**：方向或需求要变；同一件事反复失败（规则监工会自动写给用户）。其余都在公司内部消化。
7. 公司页：看板地址后加 `/org`，能看到各部门、每名员工在干什么、部门之间的沟通。


## 4. 思考强度（旧版：`-e`）

| 档位 | 用于 |
|---|---|
| `low` | 侦察摘要、格式化、简单脚本、文档草稿、按 spec 写简单测试 |
| `medium`（默认） | 常规实现、常规审查 |
| `high` | 核心逻辑、复杂重构、安全相关审查 |
| `xhigh` / `max` | 疑难 bug，并且已经失败过一次 |

模型是固定的，只能调强度：agy 只用 Gemini 3.8（`-e` 会自动在 `gemini-3.8-flash-low/medium/high` 之间切换）；dsh 只用 `deepseek-v4.1-flash`。`xay-run` 会拒绝其他模型。强度越高越慢、越贵：先用低档，不行再升档。

## 5. 旧版里省 token 的做法

- **负责人不读大文件、不读原始日志**：先看汇总（`summary.md`、`xay-run` 的输出），只在验收需要时打开对应的 `result.md` 或运行 `git diff --stat`。
- **侦察外包**：需要了解陌生代码时，派 `dsh -e low --readonly` 或 `agy --readonly` 出一份不超过 20 行的摘要。
- **不要用负责人自身的子 Agent 干活或做并行**（例如 Claude 的 Agent 工具、Codex/agy 的子任务调用等）：这会按负责人的高昂单价扣费；干活一律走 `xay-dag` / `xay-run`。唯一例外是公司模式里的部门负责人（监工，用较便宜的模型，见 3.6）。
- **事件驱动等待，切勿循环轮询**：派活后统一调用 `xay wait --session <ls_id>` 挂起等待信号，不要写 bash while-sleep 脚本轮询刷日志，避免浪费负责人的上下文与调用轮次。
- **优先用 `xay ctl` 实时干预与 `-r` 续接**：运行时发现偏差优先使用 `xay ctl` 插话（`steer`）或打断重跑（`interrupt`），避免全盘推翻重来；返工、分批、重试优先用 `xay send <名字>` 交回原来那个 AI 接着做（见 3.2“接着干还是开新的”），不要每次都开新会话让它重读项目（agy 例外：容易失忆，续接超 2 轮建议开新任务卡）。
- **汇报已限制在 15 行以内**：需要细节时再查阅具体的 `result.md`。

## 6. 旧版的验收细节

1. 看改动文件和 `git diff --stat`，确认没有超出范围。
2. **亲自运行**验收命令，只看结果的最后几行（`| tail -20`）。
3. 汇报里的关键结论，挑一两条亲手复现。
4. 让**不同厂商**的 AI 做只读审查，再核实它指出的问题。审查者也会误报。
5. 隔离运行的任务会自动把改动搬回原项目，负责人在原项目里直接验收即可。汇报里出现 `land=conflict` 时，**这是负责人自己的事，当场处理**：`xay-land <run_dir>` 重试搬回，或确认已被后续任务取代后 `xay-land <run_dir> --discard`，再一起跑一遍测试。没处理的冲突会在看板上一直显示“冲突未合并”。

不通过时，带上复现命令和实际输出，用 `xay send <名字>`（或 `-r`）发回原执行者返工。同一个子任务 3 轮还不过，就换一个 AI，或者带着分歧点交给用户决定。

## 7. 旧版的故障与额度降级

| 现象 | 处理 |
|---|---|
| Codex 订阅返回 at capacity / 限额 | 自动改走中转站。Plus 套餐在 OpenAI 算力紧张时优先级低，经常出现这种情况；如果总是满载，设置 `XAY_CODEX_SOURCE=relay` 直接走中转站 |
| dsh 额度用尽（每 5 小时最多用月额度的 20%） | 改派 codex，或者降低强度、减少并行实例 |
| agy 返回空结果或有操作被拒绝 | 只读任务就让它别跑命令；需要跑命令时加 `--full`，或者在 settings.json 里放行具体命令 |
| agy 忘了前面的内容 | 不要续接，开新会话，把要点写进任务卡；长任务加 `--long` |
| 超时 | 用 `xay send <名字>` 让它接着做或汇报进度，或者把任务拆小 |
| 任务失败要重试 | 先看失败原因：额度不足就等或换线路；其余用 `xay send <名字>` 让原来的 AI 带着记忆重试，不要开新会话从头再来 |
