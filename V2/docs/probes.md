# 探针日结果（M0，2026-10-30 本机实测）

脚本在 `V2/probes/`（Codex 写 codex_probe，dsh 写 dsh_probe、agy_probe；负责人修了 Codex 脚本两处小 bug 后运行）。原始收发在 `~/.xay/v2/probes/`（不进仓库）。

## 结论先说
| 方案里的分叉点 | 实测结果 | 定下来的做法 |
|---|---|---|
| agy 热续接（输入不关，一行一件活）稳不稳 | 3/3 成功，第二轮 4~10 秒，无报错 | **agy 员工默认热续接**；仍限每段记忆 3 轮后交接（agy 已知长上下文失忆） |
| agy 冷续接（`--conversation`） | 在**原目录**里 3/3 成功，首个动作中位 13 秒，没有一次"30 秒空转就结束" | 旧系统 agy 续接失败的主要原因很可能是**工作区目录已被删**（见下面 dsh 的同类证据）→ 一人一张桌子是对的 |
| agy `/compact` | 能用：出现一个 `checkpoint` 步骤，不报错，之后暗号仍记得；小上下文里看不出 token 下降 | 仍默认交接压缩（方案不变）；M3 在大上下文里再测一次原生 `/compact`，好用就改成"先原生、不行再交接" |
| agy `--settings` 临时放行命令 | 放行 `command(python3 hello.py)` 后**真的执行了**，denied_actions=0；不改用户的 ~/.gemini 配置 | 工位每件活生成一个临时 settings.json，按任务卡的验收命令放行（解决"非交互模式拒跑命令、空手而归"） |
| agy 用量与思考 | 50 个步骤里 14 个带 usage（input/output/thinking/cache_read/total），thinking_tokens=0，**没有思考文本** | 记忆条按带 usage 的步骤更新；思考栏显示"这家看不到" |
| dsh 续接 | `--session-id` 在同一目录里续接成功，暗号记得 | 照方案 |
| dsh 续接换目录 | 实际踩到：会话在旧工作区里记录，旧目录被删后续接直接报错 `session … was recorded in <旧目录>` | **一人一张桌子**的直接证据 |
| dsh 用量字段 | step_end 有 inputTokens / cacheReadTokens / outputTokens / totalTokens；**inputTokens 不含缓存**（4142 vs total 20528） | 记忆条用 `totalTokens`（方案已按此写） |
| dsh `/compact`（headless） | 被当成普通文字处理（模型回了一段话），没有压缩事件，token 没降 | **dsh 用交接压缩**；它自己 80% 的自动压缩照常发生，我们靠记忆条突降识别 |
| dsh 窗口大小 | `--dump-config` 里没有；在 dsh 自带的模型目录（opencode-go）里查到 deepseek-v4.1-flash：**窗口 1,000,000，最大输出 384,000** | 写进提供者表；启动时从模型目录读，不写死 |
| Codex 记忆条 | `thread/tokenUsage/updated` 有 `last.totalTokens`、`total.totalTokens`、`modelContextWindow=475000` | 记忆条 = last.totalTokens ÷ 475000（窗口取通知里的值） |
| Codex 原生压缩 | `thread/compact/start` 立即返回 `{}`，随后**作为单独一轮（kind=Compact）**执行：turn/started → `contextCompaction` 条目 → tokenUsage 下降（37011→32014）→ turn/completed。**压缩这一轮没结束就发下一轮会被拒**（`ActiveTurnNotSteerable { turn_kind: Compact }`） | 工位压缩后必须等这一轮的 turn/completed 再派下一件；员工状态在这期间是 `compacting` |
| Codex 压缩后还记得吗 | 压缩 → 退订 → 关进程 → 新进程 `thread/resume` 后，暗号**仍然记得** | 原生压缩 + 续接可靠；照样做"复述自检" |
| Codex 思考直播 | 默认配置下 reasoning 条目的 summary 是空的，0 条思考增量 | M2 启动参数加 `model_reasoning_summary`（再实测一次哪个值能出摘要）；拿不到就如实显示"看不到" |
| Codex 计划步骤 | 低强度小任务里没有出现 `turn/plan/updated` | 进度主要靠回复里的 `【进度 x/y】` 标记，Codex 自带计划有就用 |
| Codex Plus 满载 | 一次探针里出现 2 次 "Selected model is at capacity"（`serverOverloaded`），状态变 `systemError` | 额度线路切换（Plus→中转站）在 M2 必须有；`systemError` 用来判断 |
| Codex 配置告警 | 旧驱动给 `mcp_servers.*.type` 的覆盖被忽略（告警 5 条） | V2 适配器去掉这些无效覆盖，只保留 `enabled=false` |

## 方案因此改动的地方
1. agy：默认热续接（原方案写"探针决定"）；每件活一个临时 `--settings` 放行验收命令。
2. dsh：确定用交接压缩，窗口 1,000,000 从模型目录读取。
3. Codex：压缩是独立的一轮，必须等它结束；要显式打开思考摘要配置；Plus 满载切线路是 M2 必做项。
4. 旧系统 dsh/agy "续接不可靠"的根因之一是工作区目录被删——V2 的"桌子不删"同时解决了它。

## M2 真机冒烟（2026-10-30 晚，真 Codex 0.165，临时仓库里两件相关小活）
| 要验证的 | 结果 |
|---|---|
| 同一个员工的多件活用同一个原生对话 | ✓ 全程只有 1 条 `employee.session{reason:new}`；中间经历 Plus 满载、换中转站、工位重启，三轮都在同一个 thread 里续 |
| 记得上一件活 | ✓ 第 2 件任务卡没写上一件改了什么，他汇报第一句说对了"上一件活改了这些文件：calc.py、test_calc.py" |
| 成果不丢 | ✓ 第 1 件活其实做完、测试通过，最后汇报时 Plus 满载 → 判为"失败但有改动"，桌子保留、开"需要你"；负责人选"合并"后合并回项目 |
| 进度汇报 | ✓ 回复里的【进度 x/4】被识别成第几步 |
| 思考直播 | ✓ 开了 `model_reasoning_summary` 后收到 8 段思考摘要 |
| 记忆条 | ✓ `last.totalTokens ÷ modelContextWindow`；中转站模型的窗口是 258,400（和 Plus 的 475,000 不同，按通知里的值算） |
| Plus 满载 | 一晚上撞了 3 次 → 已补"满载自动换中转站、同一个对话重做"（e7aef29，假 Codex 测过，真机下次撞上再看） |
| 记忆压缩 | 这么小的活到不了 60%，真机还没触发过；只有假 AI 测过 |

## M3：dsh / agy 实测与核对（2026-10-30 晚）

### dsh 的真实窗口和自动压缩点（`dsh --dump-config` + 本机 dsh 会话日志，没花额度）
| 查什么 | 结果 | 定下来的做法 |
|---|---|---|
| dsh 认为的窗口 | 本机的 dsh 路由是用户自己手写的 `opencode-xay`（`llm-pi-ai → providers → opencode-xay → models`），**没写 contextWindow**，dsh 按 pi-ai 默认 **262,144** 算。M0 查到的 1,000,000 是 dsh 自带 `opencode-go` 目录里的，只管那个路由 | 适配器开工前跑一次 `dsh --patch <我们的补丁> --dump-config`（0.1~0.2 秒、不调模型），从用户路由的配置里读窗口；读不到用 262,144 并标估算 |
| dsh 自己什么时候压缩 | 包内公式 floor(min(W×0.8, W−输出预留−65,536))，本机输出预留为 0 → **196,608（75%）**。本机约 100 个 dsh 会话日志里（只读了事件类型和 token 数，没读对话内容）：70 次"总结压缩"、55 次"只裁剪工具输出"，触发前的用量都在约 19.6 万；输出预留设了 256,000 的那个路由（opencode-hl）从没主动压缩过——和公式吻合 | V2 两件活之间 55%（≈14.4 万）或 30 万 token 谁先到 → 一定在 dsh 自己的 75% 之前，不会压两次。30 万的绝对上限是给"路由将来写了 1M 窗口"留的，**待大海捞针实测再定** |
| "记忆条突降 30% = dsh 自己压缩了" | 按上一步算，70 次总结压缩里 42 次只掉了 12%~29%（触发看的是加上新工具输出后的压力），裁剪工具输出掉 16%~27%；没压缩却掉 10% 以上的只有 1 次（2.9 万处） | 改成"掉 10% 以上、且掉之前 ≥40%" |
| `--patch` 写错只告警 | 告警格式 `dsh: [<补丁路径>] patch: entry "…" not found`；**用户自己 profile 每次启动都有 6 条同类告警**（ui-chat 等），所以只认我们自己补丁路径的那几条 | 预检里出现、或生效的模型不是 deepseek-v4.1-flash、或 `agent-default-model` 的标题注释里没有我们的补丁 → 拒绝开工（task.failed + signal{config, warn}）；每轮 stderr 也盯着 |

### agy 原生 `/compact`（大上下文，3 次调用）
一个热进程：先发约 24 万字的资料（开头藏暗号）→ `input_tokens` 65,211 → 发 `/compact`：一个 `checkpoint` 步骤、7 秒、SUCCESS 空回复 → 问暗号：`input_tokens` **16,889**（降 74%），暗号答对。→ agy 改成**先用自带压缩，不行再交接**；同一段记忆原生压满 2 次改走交接。另：`result.usage` 是这个进程里的累计数（26,300 = 12,971 + 13,329），不能当记忆条用（适配器本来就没用）。

### agy 冷续接一个不存在的对话
真 agy 1.4.11：`--conversation <不存在的号>` → 7 秒内报 `failed to send message: not_found: trajectory not found …` 并退出。适配器按"冷续接还没动作就退出"判成续接失败。

### M3 真机冒烟（2026-10-30 22:22~22:36；独立账本 `XAY2_HOME=…/scratchpad/m3home`、管家 7882；两个临时仓库；门槛用环境变量 `XAY2_COMPACT_TURNS_DSH=2`、`XAY2_COMPACT_TURNS_AGY=2` 调成"做满 2 轮就整理"）
| 要验证的 | dsh（DeepSeek 5，em_d0ca7aaf） | agy（Gemini 1，em_4456b8b1） |
|---|---|---|
| 同一个员工多件活在同一个原生对话里 | ✓ 第 1、2 件同一个 session（中间工位空闲下班又上班，按账本里的会话号续接）；会话号第一轮才有，工位一报上来就记进账本（修了：原来会记成空号，下次上班就接不回） | ✓ 第 1 件、返工、第 2 件同一个对话；返工那次是真的冷续接（工位被叫停过），接上了 |
| 第 2 件任务卡不写上一件改了什么，他能说出来 | ✓ 开头一句"上一件活我改了 calc.py（新增 multiply）和 test_calc.py（新增 test_multiply）两个文件" | ✓（这次是在原生压缩之后）"上一件活（tk_ebfb02af）我新建并改动了 strutil.py 和 test_strutil.py" |
| 压缩一次 + 复述 | ✓ 交接压缩：员工按新的 11 个小标题写了摘要，工位附了事实块，新会话第 2 段（`employee.session{handoff}`）；第一次复述被机械检查误判"可能失忆"（他说的是"给 `calc.py` 加 `power(a, b)` 乘方函数"，和标题"给 calc.py 加乘方"字面对不上）→ 改成去掉 markdown 符号、按标题小块 / 任务卡里的代码名字比；再做一次交接后复述 ✓ `recall.checked ok` | ✓ 原生 `/compact` 17 秒，复述 ✓ |
| 故意让 agy 冷续接失败 | — | ✓ 叫停工位（进程关掉）→ 账本里的对话号换成一个不存在的 → 派第 3 件：7 秒报 trajectory not found → 当场判续接失败 → 机器版交接 → `employee.session{reason: resume_failed, segment: 2}` → 新对话重做这件活，合并成功，没记失败（复述又被误判一次：他用 `count_vowels(s)` 而不是标题"加元音计数"，同一处修好了） |
| 放行验收命令 | dsh 不需要 | 第一次没做成：agy 先跑 `ls -la`（不在放行名单里）被拒，这一轮就空手结束了（记成"说做完了但没有改动"，桌子保留、需要你）→ 补了：任务开头告诉它"只能运行这些命令"，另放行几个只读命令（ls / pwd / git status …），被拒的操作记进证据 → 返工时跑了验收命令和 `git status`，都放行了 |
| 记忆条 | 窗口 262,144（读自 dsh 配置），8%~11% | 窗口 256,000，已用 = 新输入 + 缓存读取（这次缓存读取都是 0） |
| 思考栏 | 有思考（每步一次） | 0 条思考（"这家看不到"成立；页面那边还没显示这句话，见汇报） |
| agy 调用次数 | — | 本次 6 次（V2 自己的账：第 1 件、返工、/compact、第 2 件、冷续接失败那 1 次、重做）+ 上面 `/compact` 探针 3 次 = 9 次；旧系统今天另有 20 次，合计 29 < 40 |
| 没测到的 | dsh 自己的 75% 自动压缩（要灌到约 19.6 万 token）；死循环；额度报错 / 换号（只在假 AI 和假 Antigravity Tools 上测过；真 `antigravity-tools list` 的表格能正确解析，没真切换） | 同左 |

## M3 + M4 真账本演示（2026-10-30 23:09~23:14，用户正在看的 7879 管家和 ~/.xay/v2 账本；对话 cv_20261030-230916_8fb8）
负责人开 3 个部门（文字工具部 / 计算部 / 文档部，各自 owns 对应文件），手动招 DeepSeek 1、Gemini 1、Codex 1，派 4 件活：dsh 两件相关的（倒词 → 数词数）、Codex 一件（乘法）、agy 一件（说明文档，等前三件做完）。
| 要验证的 | 结果 |
|---|---|
| 三家员工都常驻 | ✓ 每人全程只有 1 条 `employee.session`；4 件活都合并回项目、测试通过、负责人验收 |
| 自动归部门 | ✓ 管家按任务的部门把三人分别放进文字工具部 / 计算部 / 文档部 |
| 选人：老员工优先 | ✓ 第 2 件 dsh 活自动交给 DeepSeek 1，理由"这件活接着他做的「写一个把句子里的词倒过来的小工具」" |
| 依赖 | ✓ 说明文档等数词数和乘法都验收后才开工 |
| 额度失败的反应 | 中转站当天额度用完（`403 DAILY_LIMIT_EXCEEDED`）→ 工位没换线路（只会 Plus→中转站，**反方向缺失**，已交工位收尾补双向）→ 管家按反应表开"需要你"（retry / other / drop）→ 负责人把管家改回默认 Plus、选 retry → 管家写信把原因告诉员工、重新排队 → 成功 |
| 信件 | ✓ 管家的"再试一次"信在开工时交给 Codex 1；他回信给管家，原来显示"送不到：找不到收信人"→ 已修（写给管家的信不用投递） |
| agy 次数 | 本次 1 次（说明文档一轮做完） |
## 工位收尾：dsh 原生 `/compact` 复查（2026-10-30 夜，dsh 0.2.1-rc.2，deepseek-v4.1-flash，3 次调用）
| 查什么 | 结果 | 定下来的做法 |
|---|---|---|
| headless 下 `/compact` 现在能不能用 | 还是不能。用 V2 的 dsh 适配器做预检（补丁生效、模型是 flash）后在临时仓库里跑三轮：记暗号 → `/compact` → 问暗号。`/compact` 那一轮和普通一轮一模一样（开轮、一步模型调用、思考、回复、收轮），没有任何压缩事件；模型自己回了一句"OK — 上下文已压缩"（是它在顺着话说，并没有压）；`totalTokens` 20,538 → 20,694 → 20,713，只涨不降 | **dsh 照旧用交接压缩**（`compact()` 返回 False 不变）；dsh 自己 75% 的自动压缩照常靠记忆条突降识别 |
| 为什么 | 看了 dsh 包里的代码（只看代码，没看配置）：headless 配置里其实装了 `/compact` 命令插件（`command-compact`），但 headless 程序把任务文字直接当成一条用户消息交给模型（`agent.followup(createUserMessage(...))`），根本不经过命令分发；`dsh --profile headless --help` 也没有压缩相关的参数 | 以后 dsh 版本变了（工位会写 `cli_version_changed` 提醒）再复查；能用时改成"先原生、不行再交接"，和 agy 一样 |

## Codex 真机自带压缩（2026-10-31 00:54~00:56；临时账本 + 管家 7885，`XAY2_COMPACT_AT_CODEX=0.05` 把门槛临时调到 5%；Plus 线路）
| 要验证的 | 结果 |
|---|---|
| 到门槛后两件活之间整理记忆 | ✓ 第 1 件做完时记忆 6.6% → 第 2 件开工前 `context.compact_requested{threshold, native}` → `thread/compact/start` 作为单独一轮跑完 → `context.compacted{native, before 6.6, after 7.1}`（上下文本来就很小，整理后的摘要反而略大，属正常） |
| 整理后还记得 | ✓ 第 2 件开头复述，`recall.checked{ok: true}`；同一个原生对话、同一段记忆接着做完第 2 件 |
| 顺带发现 | 临时账本里桥把同一个 Claude 会话以前在旧系统跑的活镜像进了新对话，**管家把活派给了桥镜像的旧员工**（备注"旧系统 · <批次名>"，原来只认逐字"旧系统"）→ 已修成按"旧系统"开头判断，补了测试 |

## M4 刹车真机实测（2026-10-31 00:58~01:05；临时账本 + 管家 7886；agy 员工）
| 要验证的 | 结果 |
|---|---|
| 预算 100% 在一轮中途打断 | ✓ `reactions.json` 把每件活预算调到 4 万 token；agy 一轮中途用量 1.4 万 → 4.28 万（107%）→ 管家写 `budget.warned` + `budget.stopped`、开"需要你"（加预算 / 停下）、`control.requested{interrupt}` → 工位 `control.done{kill}`，这一轮结束，没有自动重试 |
| 预算 90% 先让他收尾 | ✗ 没触发：agy 两次用量报告之间一下涨了 2.8 万，直接越过 90%（工位每涨 2 万才报一次，粒度太粗）。待办：报得更细，或越过 90% 时也补一句收尾 |
| 同一件事只问一次 | ✗ 预算用完和"没做成"各开了一个"需要你"（budget + failure），应合成一个。待办 |
| 全部停下 | ✓ `xay2 stop --all` → 工位 `control.done{stop_all}` → 这一轮结束 → 工位下线（`employee.offline{stopped}`）；40 秒后工位进程和 agy 命令行进程都已不在 |
| 停下的活记成什么 | ✗ 被总闸停下、没有改动的活记成了 `task.failed`（"没做成"），按设计应是"已停下"、恢复后接着做。待办 |
| 顺带发现 | 有一个 14 小时前测试留下的假 agy 进程一直没退出（父进程已是 launchd），已手动结束。待办：测试收尾时确保杀掉假 CLI 进程 |
