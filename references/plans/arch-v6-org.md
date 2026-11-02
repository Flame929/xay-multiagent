# 第六版：公司化组织、额度调度、多窗口看板（负责人定稿，各执行者唯一依据）

用户原话要点：
- 每个 AI 的输入框说明太多，压成一行。
- 一个 AI 同时开了多个 CLI 要能看出来：单列时并排成多个窗口；并排模式时该 AI 那一列上下等分。
- 显示每个 AI 的额度，按额度和消耗速度合理分配：agy 优先，GPT Plus 可用，中转站最多用一半，dsh 省着用。
- 少找用户：总经理（负责人 AI）→ 部门负责人（监工）→ 部门员工（执行 AI）。部门之间能直接协作、对齐颗粒度；只有方向/需求变更、同一件事反复失败才找用户。
- 新增"公司"可视化页（参考腾讯 Marvis：每个员工一张工位，看得出在干什么、在睡觉；部门之间的沟通可见），现有页面保留。

~~不做：多个 agy 账号之间切换 / 轮换账号。~~ 2026-10-30 用户明确取消这条（"我自己承担风险"），改为按 Antigravity Tools 的优先级调度 agy 多账号，见 §1.5。

## 1. 额度（新文件 `scripts/xay_quota.py`）

### 1.1 线路
| line | agent | 说明 | 用户策略 |
| --- | --- | --- | --- |
| `agy` | agy | Antigravity 当前登录账号（可在多个账号间切换，§1.5） | 优先用（priority 1） |
| `codex-sub` | codex | ChatGPT Plus 订阅（`--source sub`） | 可用（priority 2） |
| `codex-relay` | codex | 中转站（`--source relay`） | 最多用一半（priority 3，`budget_share` 0.5） |
| `dsh` | dsh | DeepSeek | 省着用（priority 4，`budget_share` 0.5） |

用户可改 `~/.xay/quota.json`（不存在时用上表默认值）：
```json
{"lines": {"agy": {"priority": 1}, "codex-sub": {"priority": 2},
  "codex-relay": {"priority": 3, "budget_share": 0.5, "daily_tokens": null},
  "dsh": {"priority": 4, "budget_share": 0.5, "limit_5h_tokens": null}}}
```
`daily_tokens` / `limit_5h_tokens` 为空表示上限未知。

### 1.2 测量

**修订（2026-10-30）**：原来只读本机记录，四条线路的上限都是"未知"。用户指出每条线路都有正规的额度查询方式，于是加上"实时查询"，查不到时再退回下面的本机估算。

实时查询（结果缓存在 `~/.xay/quota-live.json`，看板在后台按线路定时刷新：dsh / 中转 2 分钟，codex-sub 3 分钟，agy 5 分钟；`xay quota` 等查完再输出）：

| 线路 | 来源 | 读到什么 |
| --- | --- | --- |
| `dsh` | `GET https://opencode.ai/zen/go/v1/usage`（和 dsh 插件 `dsh-opencode-usage` 同一个接口），key 按 dsh 的顺序取：headless 配置的 `apiKeyEnv` → `~/.dsh/.credentials.yaml` → 环境变量 → `~/.local/share/opencode/auth.json` | 5 小时 / 本周 / 本月三个窗口的已用 %、重置时间 |
| `agy` | `agy -p "/usage"`（agy 官方命令，只看当前登录账号；只取 Gemini 那组，因为 xay 的 agy 只用 gemini） | 5 小时 / 本周剩余 %、重置时间 |
| `codex-sub` | `codex app-server` 的 `account/rateLimits/read`（走订阅、关掉 MCP） | 5 小时 / 本周已用 %、重置时间 |
| `codex-relay` | 中转站 `GET <base_url>/usage`，地址和 key 取自 `~/.codex/config.toml` 的 `model_providers.custom` | 每日（及周/月，若有）美元上限与已用、余额、订阅到期 |

- key 只在查询那一刻由程序读出并使用，不写进缓存、日志、汇报或测试输出。
- 剩余按**最紧的窗口**算（例如 5 小时剩 100% 但本周只剩 5%，就算 5%），`resets_at` 是那个窗口的重置时间。
- 查询失败时保留上一次成功的读数（30 分钟内仍可用，备注写明"最新一次查询失败"）；没有可用读数就用本机估算，备注写明失败原因。读数之后窗口已到重置时间的，按满额算。
- 正式 `~/.xay` 才实时查询；测试用的临时目录不联网、不起进程（`XAY_QUOTA_LIVE=0/1` 可强制关/开）。

本机估算（只读本机记录）：
- 用量：遍历 `~/.xay/registry.jsonl` 里各运行的 `events.norm.jsonl` 的 `usage` 事件，按"新消耗"（输入扣缓存 + 输出，与看板一致）统计 5 小时、24 小时、7 天窗口内的用量，以及最近 1 小时的消耗速度（tokens/小时）。codex 按 meta/start 的 `source`（含自动降级记录 `codex_source`）分到 sub / relay。按 run_dir + 文件 mtime 缓存。
- 权威信号：codex-sub 取最近一次 `events.raw` 中 `account/rateLimits/updated` 且 `primary` 非空的记录：`primary.usedPercent`（5 小时窗）、`secondary.usedPercent`（7 天窗）、`resetsAt`。
- 用完信号：最近一次额度类错误（`usage limit`、`usageLimitExceeded`、`DAILY_LIMIT_EXCEEDED`、`quota`、`RESOURCE_EXHAUSTED`、`额度`）的时间。错误之后该线路有成功运行则视为已恢复。错误里能解析出重置时间就用，否则 codex-sub 用 `resetsAt`，codex-relay 按次日 0 点，dsh 按 5 小时后，agy 按 5 小时后估计。

### 1.3 输出
`xay quota [--json]` 与 `/api/state` 新字段 `quota`：
```json
[{"line": "codex-sub", "agent": "codex", "label": "Codex · Plus", "priority": 2,
  "status": "ok|tight|exhausted|unknown", "remaining_pct": 74, "window": "5h",
  "resets_at": 1793310356, "used_5h": 120000, "used_24h": 900000, "burn_per_h": 30000,
  "eta_exhausted": 1793300000, "note": "一句人话，如“5 小时窗已用 26%，本周 35%”"}]
```
- `remaining_pct`：有实时读数、权威信号或已知上限时给出，否则为 null。没有实时读数时 `budget_share` 按"份额内剩余"计算。
- `status`：exhausted = 有未恢复的用完信号（实时读数之后发生的才算）或剩余 ≤ 0；tight = 剩余 < 20%（有实时读数且设了 `budget_share` 时，剩余低于 `(1 - budget_share) × 100%` 就算偏紧，即 xay 该用的那份已经用掉了）或按当前速度 1 小时内耗尽；unknown = 没有任何数据；否则 ok。
- 修订新增字段：`source`（`live` / `estimate`）、`source_label`（如"agy 官方 /usage"）、`windows`（各窗口 `{key, name, remaining_pct, resets_at}`）、`balance`（中转站余额）、`fetched_at`、`live_error`（实时查询失败原因）。

### 1.4 调度
- `xay-run codex --source auto`（auto 成为默认值，`XAY_CODEX_SOURCE` 仍可覆盖）：codex-sub 不是 exhausted 且剩余 ≥ 10% 用 sub，否则 codex-relay 不是 exhausted 用 relay，都不行就失败并写 `fail_reason=usage_limit_exceeded`。
- `xay-run` 派给 exhausted 的线路时，在 stderr 打一行提醒（不阻止），并写进 start.json 的 `quota_warning`。
- DAG 任务 `"agent": "auto"` + 可选 `"prefer": ["agy", "codex", "dsh"]`：按 prefer 顺序（缺省按 priority）选第一条不是 exhausted/tight 的线路，选定后写回 state.json。
- `xay route [--prefer agy,codex,dsh] [--json]`：只打印推荐，不派活，供负责人参考。agy 当前账号不够但有别的充足账号时，仍推荐 agy（派活时自动换号）。

### 1.5 agy 多账号调度（2026-10-30 新增）
用户原话要点：agy 还有其他账号，可以调度；风险自己承担；agy（命令行）和 Antigravity 桌面 App 不是一个东西，agy 用的是另外的额度；参考 Antigravity Tools 里的优先级排序；允许负责人自由调度额度充足的账号。

- **账号清单**：读用户的 Antigravity Tools（`~/.antigravity_tools/accounts.json` 与 `accounts/<id>.json`），只取名字、优先级（`priority`，数字小的先用）、停用/封禁标记和额度字段，**不读登录信息**。只算 Gemini 那组额度。
- **每个账号的额度**：取两者中较新的——xay 自己在该账号当前时用 `agy -p "/usage"` 查到的读数（记在 `quota-live.json` 的 `agy_accounts`），或 Tools 的记录（Tools 只在它开着时刷新，可能很旧）。记录之后已过重置时间的窗口按满额算。剩余 < 20% 算偏紧，≤ 0 算用完。
- **怎么切**：只调用用户自己的 `antigravity-tools switch <邮箱> --target agy`（只切 agy 命令行，不动桌面 App 的账号），切完核对 `current_agy_account_id` 已变。邮箱只在调用时用，不输出。切换记进 `~/.xay/agy-switch.log`；之后 agy 的旧读数作废、重新查询，切换前旧账号的"用完"报错不再算到新账号头上。
- **挑哪个**：可用（不是停用/封禁）且剩余 ≥ 20% 的账号里，按优先级从小到大，同优先级挑剩得多的。
- **自动**：`xay-run agy` 派活前，若当前账号用完，或偏紧且此刻没有 agy 在跑，就自动换到挑出来的账号，提示写进 start.json 的 `quota_warning`（`XAY_AGY_AUTO_SWITCH=0` 可关）。正在跑的 agy 任务不受影响（只是偏紧时不换）。
- **负责人手动**：`xay agy-accounts [--json]` 看清单，`xay agy-switch [序号|名字|auto]` 随时切。执行者不能切（属于负责人命令）。
- **看板**：agy 卡片显示当前账号名、"N 个账号 · M 个充足"、当前号不够时"可换 X"；悬停列出全部账号。

## 2. 组织（新文件 `scripts/xay_org.py`）

### 2.1 角色
- **总经理**：负责人 AI（如 Claude）。拆任务、组部门、定方向，只处理上报到 `gm` 的事。
- **部门负责人**：每个部门一个监工。先由规则自动处理（§2.4），需要判断的交给总经理派出的 Claude 子代理（Sonnet 级），提示词在 `prompts/dept-head.md`。
- **员工**：部门里的执行 AI，就是第五版的具名 worker（`--as`）。

部门不固定：总经理按任务自由组合，可以按工作内容分（前端部、测试部），也可以为一件大事临时组部门，做完解散。

### 2.2 存储（按负责人对话隔离）
`~/.xay/org/<lead_session>/`：
- `org.json`：`{"departments": [{"id", "name", "mission", "head": "rules|claude", "members": ["worker 名"], "owns": ["glob，本部门负责的文件"], "status": "active|done", "created"}]}`。写入时加文件锁。
- `bus.jsonl`：部门间消息，一行一条，只追加：
```json
{"id": "m_<时间>_<4位>", "ts": 1793310000, "from": {"dept": "fe", "worker": "小A", "run": "<run_dir>"},
 "to": "be | gm", "type": "request|decision|notice|escalation|report|auto",
 "subject": "一句话", "body": "≤600 字", "contract": "接口约定 id 或 null",
 "reply_to": "m_… 或 null", "verdict": "accept|modify|reject 或 null", "status": "open|closed"}
```
- `contracts/<id>.md`：接口约定（例如 API 字段、文件格式），首行 `owner: <dept>  version: <n>`。只有 owner 部门能改；改动后自动发一条 `notice` 给所有部门。

### 2.3 协作协议：需求单，一次拍板，不辩论
1. 员工需要别的部门配合时运行 `xay ask <部门> "要什么、为什么、建议怎么改" [--contract X] [--wait 600]`，生成 `request`。
2. 对方部门负责人用 `xay answer <消息id> accept|modify|reject "说明"` 一次给结论（modify = 按负责人给的版本做）。不允许来回讨论。
3. 对方负责人拿不准，或双方结论冲突 → `xay escalate <消息id> "…"` 给总经理，总经理用 `xay answer` 裁决。
4. `--wait` 时员工阻塞等结论（超时返回"未答复，先按现有约定继续，并在汇报里写明"）；不 wait 时结论通过 `xay send`（排队）送回该员工。
5. 部门开工时发一条 `notice` 写清"本部门交付什么、接口约定是哪几个"，用于对齐颗粒度；员工任务卡里自动附上相关部门的交付清单摘要（每个部门 ≤ 3 行）。
6. 同部门员工用 `xay tell <员工名> "…"` 留言，收件人必须属于发送者所在部门；跨部门仍用 `xay ask`。收件人用 `xay reply <消息id> "…"` 回复。一个话题只允许一问一答，第三条及之后尝试回复自动变为发给本部门负责人的 `escalation`，不再送给原收件人。
7. 每轮 `xay supervise` 都投递发给员工的 `tell`、`reply` 和需求单 `decision` 结论。正在运行且驱动支持实时插话的 Codex 用 `ctl steer` 当场送达；其他员工忙碌时先排队，等本轮结束后通过 `xay send` 在原会话里送达。留言正文以"来自 X 部门 Y 的留言"开头。成功投递后向 `bus.jsonl` 追加 `type: delivered` 记录；`/api/org` 的对应消息带 `delivered: true`，尚未投递时为 `false`。

### 2.4 规则监工（`xay supervise [--dept X] [--once]`，常驻时每 15 秒一轮）
| 情况 | 自动处理 | 仍不行 |
| --- | --- | --- |
| 员工失败（非额度原因），本任务自动重试 < 1 次 | `xay send <员工> "上一轮失败：<原因>。排查后重试"` | 交部门负责人 |
| 额度原因失败 | 按 §1.4 换线路重派同一任务卡（codex 换 sub/relay；其余换 prefer 中下一个） | 交部门负责人 |
| land=conflict | 补丁已在项目里 → `xay-land --discard`；否则 `xay-land` 重试一次 | 交部门负责人 |
| 沉默超阈值（arch-v4 §8.1） | 能实时插话的发"汇报一下进度" | 交部门负责人 |
| 同一任务累计失败 ≥ 3 次 | — | 写一条 `owner: user` 的决定（反复失败，需要用户） |
| 方向/需求变更（由总经理判断） | — | 总经理写 `owner: user` 的决定 |

每次自动处理都写一条 `type: auto` 的 bus 消息（看板展示用）。交给部门负责人 = bus 里写 `escalation`，`to` 为该部门（部门负责人在自己的收件箱里看到）。

### 2.5 命令
```
xay dept create <id> --name 前端部 --mission "…" [--head rules|claude] [--owns 'web/**']
xay dept list [--json]            xay dept close <id>
xay-run <agent> --as <员工> --dept <id> "…"   # 自动加入部门；设置 XAY_DEPT / XAY_WORKER 环境变量
xay ask <dept|gm> "…" [--contract X] [--wait 秒]     # 员工也能用（执行者白名单）
xay answer <消息id> accept|modify|reject "说明"
xay escalate <消息id|-> "…"        xay report "…"     # 部门负责人 → 总经理
xay inbox [--dept X | --gm] [--wait 秒] [--json]       # 只列 open 的、发给自己的消息
xay contract set <id> --owner <dept> --file x.md   xay contract show <id>
xay supervise [--dept X] [--once]
```
`XAY_ROLE=executor` 时只允许 `ask`、`inbox`（只看自己）、`contract show`。员工任务卡末尾自动附一行：`需要别的部门配合：xay ask <部门> "…" --wait 600；不要改别的部门负责的文件（见 owns）。`

### 2.6 看板接口
- `GET /api/org?conversation=<id>` →
```json
{"conversation": {"id", "title"},
 "gm": {"kind": "claude", "title": "总经理"},
 "departments": [{"id", "name", "mission", "head": "rules|claude", "status",
   "members": [{"worker", "agent", "state": "working|idle|waiting|blocked|failed|sleeping",
                "now": "一行当前动作或空", "thread": "线程 id 或 null", "rounds": 3}],
   "counts": {"members", "working", "open_requests"}}],
 "unassigned": [同 members 结构，没进任何部门的具名员工],
 "messages": [bus 最近 200 条，按时间],
 "contracts": [{"id", "owner", "version", "title"}],
 "plan": [{"id", "title", "dept", "status", "deps": []}]}
```
  `state`：running → working；排队等名额或等别人 → waiting；有 open 的 request 等答复 → blocked；最近一轮失败 → failed；空闲超过 30 分钟 → sleeping；其余 idle。
- SSE 新事件 `org.changed`（数据为空，前端收到后重新拉 `/api/org`；防抖 1 秒）。
- `GET /org` 返回 `scripts/xay-org.html`。

## 3. 多窗口与精简输入框（`scripts/xay-watch.html`）
- **窗口** = 同一个 AI 当前活跃的线程（running / paused / 等名额 / 等你），按开始时间排。
- 单列模式（一次看一个 AI）：该 AI 有 N ≥ 2 个窗口时，主区域横向并排 N 个窗口（最多 4 个一行，再多换行）；每个窗口显示该线程的标题行、流式消息（自动滚到底）和一行输入框。只有 0~1 个窗口时保持现在的线程列表。窗口区上方一行"历史 N 件"可切回完整线程列表。
- 并排模式（每个 AI 一列）：某 AI 有 N ≥ 2 个窗口时，那一列上下等分 N 份，每份是一个窗口（标题行 + 流式消息 + 一行输入框）。
- **输入框压成一行**：去掉"发给…""回车发送…""会先打断它…"等说明文字；只留一个单行输入框（占位符最多 6 个字，如"说一句…"）+ 发送按钮。发送目标就是所在窗口/线程，用左侧一个小状态点表示。说明放进 title 悬浮提示。
- **额度显示**：侧栏底部原来的"Codex 0/3 · dsh 2/4 · agy 0/3"改成每条线路一行：名字、细进度条（剩余）、一句 note；exhausted 红、tight 黄、unknown 灰。AI 标签页上的 AI 名后显示一个小圆点表示该 AI 的最差线路状态。
- `?demo=1` 内嵌数据补上 `quota` 与一个 dsh×3 并行窗口的场景。

## 4. 公司页（新文件 `scripts/xay-org.html`，参考 Marvis 的布局与气质，形象原创）
- 顶部：对话标题 + 一行现状（几个部门、几名员工在干活、几条待答复的需求）。左上角返回现有看板。
- 总经理工位在最上方居中；下面每个部门一块"办公区"（浅色地面 + 部门名牌 + 使命一句话）。办公区里每名员工一张工位：等距视角或正面的简洁书桌 + 显示器 + 原创的小角色（每个 AI 一种颜色：Codex 黑绿、dsh 蓝、agy 紫，角色本身用简单几何形状原创绘制，不使用腾讯 Marvis 的猫形象或任何第三方吉祥物）。
- 状态动画：working = 显示器滚动代码行 + 角色轻微敲键；sleeping = 显示器变暗 + "z z"；blocked = 头顶问号气泡；failed = 显示器红色；waiting = 沙漏。显示器下方一行小字显示 `now`。
- 部门之间的消息：在办公区之间画弧线，request 未答复时弧线上有流动的点；点击弧线或右侧"沟通记录"面板查看消息（按时间、带 verdict 标签）。
- 右侧可折叠面板：任务拆解（plan 树，按部门着色）与沟通记录。
- 点击员工 → 跳到现有看板该线程（`/#/c/<conversation>` 并定位到线程）。
- 纯 HTML/CSS/SVG，单文件，无外部依赖；用 EventSource 订阅 `org.changed`；`?demo=1` 内嵌示例：3 个部门、7 名员工（含 dsh×3）、一条 open request、一条 decision、一条 escalation。
- 明暗两色、宽屏与 390px 手机宽都要可用。

## 5. 不变的约束
执行者不 git commit / push、不读或输出密钥；agy 只用 gemini-3.8-*；dsh 只用 deepseek-v4.1-flash；xay 自己不做设备指纹伪装、token 提取（agy 多账号切换经用户的 Antigravity Tools，§1.5）。
