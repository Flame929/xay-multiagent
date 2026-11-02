# xay V2 协议（负责人定稿 · 版本 1.36）

> 1.36（2026-11-02，命令行看板多员工共用直播连接）：`GET /api/v2/live` 新增参数 `ems=员工1,员工2,…`（与 `em` 互斥，逗号分隔，去重，最多 64 个，格式不对或空报 422），一条 SSE 连接同时推送多名员工的实时直播。支持断点续传格式 `ems=em_a@段.行,em_b`（最新一段等于该段号则只发编号大于行号的；最新一段大于该段号则按 tail 补发新一段最近几行；没带按 tail 补发）。多员工模式下每帧 `event: live`，`data` 新增字段 `em`，不发 SSE 的 `id` 行，忽略 `Last-Event-ID` 与 `since` 参数；`plain=1` 时每人独立叙述器；`demo=1` 时只发心跳。

> 1.35（2026-10-31，一键环境检查）：新命令 `xay2 doctor`：逐项检查 Python ≥ 3.11、SQLite、git、管家在不在跑、端口 7878 / 7879（被管家自己占着算正常）、$XAY2_HOME 能不能写、codex / dsh / agy / antigravity-tools 装没装（没装只提示不算错）；必需项都过退出码 0，否则 1；只读、不碰密钥。`V2/bin/xay2` 和 `scripts/xay` 入口先检查 Python 版本，不够就用大白话提示升级、退出码 2。

> 1.34（2026-10-31，员工开工前先读手册、自己拆步骤）：① 每件小任务的第一轮，工位在输入最前面附上员工手册 `V2/docs/员工手册.md`（之后的轮不附）。② 员工回复里的【计划】（编号列表）→ 新事件 `task.planned {steps:[{n, text}], round}`（subject 小任务，by station）→ 任务的 planned_steps；再写【计划】覆盖；之后【进度 n/m】的 m 以计划步数为准。③ 页面数据里小任务的步骤优先用 planned_steps，没有才用任务卡 steps，再没有用进度里出现过的；快照 tasks[] 多 planned。④ 第一轮结束还没写【计划】、也没做完的，下一轮开头提醒一句"先用【计划】把你的步骤列出来"。

> 1.33（2026-10-31，"需要你"说人话、附文件、能交出去，主 AI 叫"负责人"）：① 各种 decision.opened 的 title / detail / 选项改成大白话（发生了什么、为什么要你、选了会怎样；merge=放进项目、rework=让他接着做、discard=不要了、manual=我已经自己放好了……），主 AI 在给人看的文字里统一叫"负责人"。② decision.opened 的 data 多 files:[{path, kind: changed|landed|review|conflict}]（≤30），快照 decisions[] 带出。③ 新接口 GET /api/v2/file?conv=&path=[&task=]（只读；有 task 读那件小任务负责人的桌子，否则读项目目录；拒绝 ..、软链接逃出、.git/、.env*、*.key/*.pem/*.p12/*.pfx、id_rsa*、.netrc 等凭据文件；文本、≤200 KB，超出截断）→ {path, text, truncated, from: desk|project}；GET /api/v2/diff?task=&path= → {diff}。④ 新事件 decision.delegated {to: rules|lead}（by user / lead，记到决定的 delegated），POST /api/v2/decision 支持 {id, delegate}；to=rules 时规则监工按 DELEGATE_RULES_DEFAULTS 默认做法表自动 decision.resolved（text"交给监工：按默认做法……"），to=lead 时留给负责人（xay2 wait 照常报，brief 标"你交给负责人了"）；页面"需要你"只显示 delegated 为空的。详细见当时的变更记录（已并入本条）。

> 1.32（2026-10-31，负责人拆活模板与防卡死提醒）：① 计划模板：`xay2 plan template [--title "大任务"]` 输出标准合法 JSON 模板，带 `_说明` 注释字段，示例按前后端与按功能划分的 2 个中任务（各带 why、check、after）及各自 2 件小任务（title 大白话、agent、writes、verify、card），写清每格填法及大白话标准；`plan apply` 自动递归忽略所有以 `_` 开头的注释键。② 零散小任务提示：`xay2 plan add` 未指定 `--part` 时小任务照常创建，并提示"这件小任务不属于任何中任务，页面上会放进'零散的活'。建议先 xay2 plan part 开一个中任务，再用 --part 放进去（或用 xay2 plan template / plan apply 一次建好整份计划）"。③ 防卡死保护：`plan add --part` 与 `plan edit --part` 把未完成小任务放进已全部完成的中任务时，若有后置中任务（after 依赖它）且含有未开工小任务，拦截并提示"这样会让『某某中任务』里的小任务一直等着，建议放进还没做完的中任务"，需加 `--force` 确认后才允许写入。

> 1.31（2026-10-31，规划工具大白话把关与层级纠正）：① 层级纠正：给人看的文字一律用"大任务（对话）→ 中任务（part，原小任务）→ 小任务（task，原活）→ 步骤"。计划文件接收 "groups"（新推荐）与 "parts"（旧），中任务内不再支持 depts（写了报错提示部门就是中任务本身，请拆成单独中任务）；xay2 dept open --part 提示建议直接用 xay2 plan part 开中任务；plan show 第一行写"大任务：<对话标题>"。② 大白话把关：每个中任务必须写 why 说明做完后用户能得到什么（存入 part.added 的 note，plan part 增加必填 --why 参数；plan show 与 --dry-run 树在每个中任务下方展示）；中任务与小任务的 title 必须是大白话，不得包含文件路径、带扩展名文件名、反引号、xxx() 括号调用、下划线英文、驼峰英文、命令且最长 30 字，不合规时整份不写入并逐条给出修改建议（规则共用 office/plain.py）。

> 1.30（2026-10-31，网络断开自动接着做）：干到一半因网络错误断开（turn.ended status=failed、evidence.error_kind="network"）且未报做完时，工位在原对话里自动补一轮（`kind = "network_continue"`），提醒改过的文件都在并接着把剩下的做完；不管改没改文件都补，每件活最多补 2 次；还断就照旧保留等负责人决定。

> 1.29（2026-10-31，员工大白话汇报与监工把关）：① 汇报格式与任务准则：增加必写项 `【给老板看】一句大白话：做成了什么、对用户有什么用（不写文件名、函数名、命令、英文术语）`；【进度 2/4】要求用大白话写在干什么；工作笔记（xay2 notes 说明）与整理记忆提示要求每条用一句大白话写。② 事件与数据结构：工位解析【给老板看】作为 `task.reported` 的 `data.plain`，存入任务的 `plain` 字段；快照 `tasks[]` 带出 `plain`；任务树每件活的 `detail` 带出 `plain`。③ 监工把关：任务报了完成（claim=done）但缺【给老板看】或其不像大白话（含斜杠、带扩展名文件名、反引号、下划线英文、驼峰英文单词、或超过 80 字），工位在原对话中补一轮提示重写大白话（`kind = "plain_retry"`，每件活最多 1 次）；补的一轮只更新 plain，不影响合并；判断规则见 `office/plain.py` 的 `is_plain`。④ 大白话播报：`task.landed` 做完的播报优先使用 `plain`（"Codex 2 做完了「…」：<plain>"），没有 plain 则照旧。

> 1.28（2026-10-31，真实 token 消耗与速度口径）：① 适配器上报 usage 事件除了 `turn_total`，新增 `turn_out`（这一轮到现在 AI 写出的 token）和 `turn_fresh`（这一轮到现在新读的 token = 输入减去缓存命中）；Codex 的 input 已含 cached 要减掉，dsh / agy 分别按自身字段提取 input 与 output。② 工位直播通道（`live.py`）用量行格式支持可选字段 `{n, kind: usage, turn, tokens, out, fresh, ts}`。③ 详情栏数据（`/api/v2/employee` 的 `person.tokens`）：`task` 与 `today` 新增字段 `out`（写出）、`fresh`（新读）、`cached`（重复读缓存 = total - out - fresh，最小 0）、`spent`（真正花的 = fresh + out）；`used` 字段保持不变供预算计算；轮结束后根据 `turn.ended.usage` 的 `in / out / cached` 同样计算这四项。④ 速度字段 `speed` 改为 `{out_per_s: 最近 60 秒每秒写出多少, fresh_per_s: 每秒新读多少, window_s}`，没有 `out` 数据的旧用量行不计算速度（不给），原 `per_s` 字段移除。⑤ 方格 `minutes`（最近 30 分钟每分钟用量）改为按 `spent`（fresh + out）计算。

> 1.27（2026-10-31，规划工具与整份计划）：① `xay2 plan apply plan.json` 支持整份计划格式（旧格式任务数组照常可用）：
>   - 计划格式：`{"title": "可选，改对话标题", "parts": [{"key": "api", "title": "定接口", "after": ["…"], "check": "rules" | "ai:codex" | "ai:dsh" | "ai:agy" | "lead" 或对象 {kind, reviewer, text}, "check_text": "可选", "depts": [{"key": "fe", "name": "前端组", "owns": ["web/**"]}], "tasks": [{"key", "title", "agent" 或 "to", "dept", "deps": ["…"], "writes": ["…"], "card": "任务卡文字", "verify": ["验收命令"], "readonly": false, "effort": "low|medium|high"}]}]}`。
>   - 整份检查规则：一条不合规整份都不写入，逐条列出大白话错误（带是第几个小任务/哪件活）。每件活必须有 title、agent 或 to、card；写代码的活（不是 readonly）必须有 writes 和至少一条 verify；deps / after 引用的 key 必须存在；不能有循环依赖；同一小任务里两件可以并行的活 writes 重叠只提醒不拦；小任务未写 check 提醒"默认自动检查"不拦。
>   - 写入顺序与幂等：检查通过后写入 `part.added`（同批 batch，计划版本只 +1）→ `dept.opened`（带 part）→ `task.added`（带 part、dept、deps 换成任务编号、verify 写进 card 的 verify 字段——card 用对象 {text, verify, effort}）。已有同 key 的小任务或任务改为写 `part.changed` / `task.changed`，重复 apply 不会重复加。
>   - `--dry-run`：只做检查并打印将要建的树，不写账本。
> ② `xay2 plan show`：把当前对话的整份计划打印成一棵树（和画布同一套颗粒度：小任务第几步、并行的标 2-A / 2-B、验收方式 → 部门 → 每件活：编号、谁做、状态、第几步 / 共几步），最多 60 行，超出折叠成"…还有 n 件"。
> ③ `xay2 plan edit <任务> --part <小任务>`：把已有的活挪到某个小任务（写 `task.changed fields.part`，用小任务编号；小任务可以写编号 / key / 标题）。
> ④ `xay2 plan open`：打印画布的网址 `http://127.0.0.1:<管家端口>/org?conv=<对话>&canvas=1`（管家没运行就提示先 office start）。

> 1.26（2026-10-31，任务树接口与依赖）：① 新接口 `GET /api/v2/plan?conv=<对话>`（`office/web.py`）：返回 `{conv, title, tree}`，无需指定员工 `em`（`em=None`，所有 `mine` 均为 false）；对话不存在返回 404；未指定 `conv` 时沿用快照默认对话规则。② 任务树节点 `task_tree`：每件活（`groups[].items` 与 `parts[].…items`）新增字段 `deps`（依赖的活编号列表，排除撤回任务，合成活使用合成后编号）与 `part`（所属小任务编号，零散活为 `""`），已有字段保持不变。

> 1.25（2026-10-31，复述检查不拦人）：整理记忆后的复述检查两次没对上不再开乙类决定（kind=amnesia）、不让员工停下等人答复；改写一条提醒 `signal.raised{kind: remind, level: remind}`（文字"……整理记忆后两次都没复述对，可能忘了些细节；已经把工作笔记和任务卡重新给他，接着干"），员工照常接活。管家启动时自动关掉已有的旧 amnesia 决定（`decision.resolved by=rules`，option ok，text "复述检查不再拦人"），员工状态不再因 amnesia 变为 blocked。

> 1.24（2026-10-31，监工审查流程）：① 新事件 `check.result {task, ok, text, round}`（subject = 被审任务，by = `lead` / `office` / `station:<员工>`；REDUCER_VERSION 10），更新任务状态 `check: {state: none|waiting|passed|failed, round, text, by}`。快照 `tasks[]` 带出 `check`。② 验收流程（`office/rules.py` 反应表）：一件活 `task.landed` 之后，看其验收方式（`task.check` 优先，否则其所属小任务的 `check`，都没有就是 `rules`；审查活本身跳过）：
>   - `ai`：管家开只读审查活 `task.added`（by=office，mode=readonly，part 同原任务，review_of=原任务编号，title="审查「原标题」"，card 说明审查要点与提交命令；owner 按 reviewer 设定：员工编号直接交给他，codex/dsh/agy 交由排程选人且排除原负责人）。原任务 `check.state = waiting`。
>   - `lead`：开一个给负责人的决定（kind="review"，title="需要你审查「原标题」"，选项 pass / fail）；负责人也可用 `xay2 check` 命令行直出结论。原任务 `check.state = waiting`。
>   - 审查结论：ok 为 True 则 `check.state = passed`；不合格第 1 次，管家写信告知原负责人并 `task.queued{retry: "check"}` 发回重改（改完再合并时 round +1，再次审查）；第 2 次不合格，开乙类决定交负责人（选项：再让他改 / 换人 / 我来看）。
> ③ 命令行：负责人 `xay2 check <任务> pass|fail "意见"`（写 `check.result by=lead`，同时关闭对应 review 决定）；执行者 `xay2 verdict pass|fail "意见"`（写入发件箱 `{type: "verdict", data: {ok, text}}`，工位读取时转为 `check.result`，仅在审查活中生效）。

> 1.23（2026-10-31，小任务与命令）：① 新事件 `part.added`、`part.changed`、`part.removed`（by=lead，subject 编号 `pt_<8位>`），任务与部门增加可选字段 `part`。② 排程规则：一件活变 ready 需其所属小任务的 `after` 里每个小任务均做完（无活的小任务算做完；认编号与 key）；去掉 `_place` 自动归部门（不再开"项目组"，不再写 `employee.joined`）；一个小任务名下没撤回的活全部 done 后，其名下的部门由管家写 `dept.closed`（by=office，reason="小任务做完了"，幂等键 `dedup="office:partdone:<部门编号>"`）。③ 快照新增 `parts`，`tasks[].part`、`depts[].part`、`tree.parts`。④ 命令行：`xay2 plan part "标题" [--key k] [--after k1,k2] [--check rules|ai:codex|ai:dsh|ai:agy|lead] [--check-text "…"] [--reason …]`；`xay2 plan part-edit <小任务> [--title …] [--after …] [--check …] [--check-text …]`；`xay2 plan part-drop <小任务>`；`xay2 plan add … --part <小任务>`；`xay2 dept open 名字 … --part <小任务>`；`xay2 brief` 有小任务的对话按小任务分组列活（每组先一行"小任务：标题（做完 x/y）"，组内照旧一行一件），保持 ≤25 行。

> 1.22（2026-10-31，看板换回旧样子 + 监工）：① 7878 / 7879 的首页 `/` 换回旧看板的页面（`V2/web/watch.html`，照抄 `scripts/xay-watch.html`，样子不改），`/board` 仍是每人一列命令行的看板（不再当首页）。页面用的旧接口由管家从 V2 账本提供：`GET /api/state`（`office/oldboard.py`：工作区按 Claude 桌面版对话所在文件夹分组——只读会话索引的目录和是否归档；没选文件夹的归"这台电脑"；/tmp、/var/folders、~/.xay 下或已不存在的归"临时文件夹"；四条线路的真实额度；没处理的"需要你"），`GET /api/events`（SSE：heartbeat、`state.changed`、`thread.update{conversation, thread:""}` 让页面重拉对话），`GET /api/conversation?id=`、`GET /api/diff?execution=&path=`、`POST /api/control`（X-Xay-Action: control）、`POST /api/decision`（X-Xay-Action: decide）由 `office/oldboard_conv.py` 实现（一件任务 = 一个线程，每轮一条 AI 消息）。看板的"公司"按钮去 `/org?conversation=<id>`（公司页同时认 `conv` 和 `conversation`），公司页的"看板"按钮回 `/#/c/<id>`。② 页面上后台自动做的事（by = office / rules）一律显示成**监工**：信件虚线从收信员工所在部门的监工席出来，自动派活的动画也从监工席飞出（`/api/v2/stream` 的帧多一个 `auto: true`）；负责人命令行里的"管家"照旧。③ 员工详情面板的"总任务"改成一行概括 + "展开任务画布"按钮（`web/lib/task-canvas.js`），数据仍是 `/api/v2/employee` 的 `tree`。

> 1.21（2026-10-31，额度按真实的算）：agy 默认**不再限每天 40 次**（用户：账号多、额度多，按真实额度 + 换号管）；`quota.agy_daily_cap()` 默认 0 = 不限，只有设了 `XAY2_AGY_DAILY_CAP` 才按次数收手，下文"每天 40 次"都按"设了上限时"理解。中转站"今天"的额度不写重置时间（它不给，实测早上 9 点左右重置，不是 0 点），用完了靠每 5 分钟的查询发现恢复。

> 1.20（2026-10-31，旧版标 legacy）：临时桥已删除，管家不再读取旧系统；账本里已有的 `bridge.*` 事件照旧重放显示，不会再有新的桥接事件。

> 1.19（2026-10-31，画布改版）：快照 `gm` 只加字段 `recent`——总经理最近做的事，本对话里 `by=lead:*`、有大白话的事件，最多 5 条、从新到旧，每条 `{seq, ts, type, text, to?, reply?}`（`to`：task.assigned 的 owner / letter.sent 的收信人；`reply`：带 reply_to 或 kind=reply 的回信）。总经理看信（`letter.delivered{how: inbox}`）没有现成文案，补一句"看了 X 的信：主题"。只往回看最近 5000 条事件。页面总经理卡片据此显示"在派活 / 在验收 / 在看信…"和最近一两个动作；拿不到就是空数组，页面写"暂无动作"。

> 1.18（2026-10-31，任务树与 token，设计见 `references/plans/ui-v2-employee-panel.md`「总任务拆解树、token」）：① 直播文件加一种行 `{n, kind: usage, turn, tokens, ts}`（工位写；`tokens` = 这一轮到现在累计用的 token，口径同 `turn.ended.usage.total`：in + out + cached）。适配器每次报 usage 带 `turn_total` 时写，没变不写，同一轮两行至少隔 1 秒（中间的只留最新，tick / flush 补写）；`n` 和其他行共用一个计数。**不进账本**；`/api/v2/live` 的推送和详情栏的步骤按 kind 过滤、看不到它（§8 推送的 kind 列表不变）。② `/api/v2/employee` 只加字段：`tree:{title, count, groups:[{id, name, mine, owners, count, items:[{id, title, status, legacy, owner, owner_name, former, mine, tries, step, of, steps:[{n, text, state: done|cur|stop|todo}], depth, chained, after:[{id, title, group}]}]}]}`——本对话全部的活；同一个人标题相同、前一次没做成或撤回的合成一件（和 `timeline` 同一条规则，所以树上 `mine` 的件数 = `count.all`），撤回的不列；部门 = 任务写的部门 → 负责人在本对话的部门 → 未分部门（`id` 为空串）；同部门里依赖前面某件的排在它下面、`chained=true`，一件后面接好几件时这几件缩一层（`depth`，最多 3），跨部门或没挂靠的依赖写在 `after`（最多 3 条）；步骤名先用任务卡 `steps`，没写的用那一步报进度时的话，做完的活每步都是 done，没在做时停在的那步是 stop。`person.tokens:{task?, today?, speed?, minutes?, minutes_from?}`——`task` / `today` = `{used, limit, pct, partial}`：task 是 `focus` 那一行各次尝试的轮（不含整理记忆的轮）加起来，limit 取最后一次那件活的 token 预算；today 是他本机 0 点以后开工的轮，今天没干过活就是 0；每一轮取 `usage.total`、`turn_tokens`、直播用量行三者最大，有一轮哪个都没有 `partial=true`；limit 来自 `reactions.json`（读不到为 null；Gemini 不按 token 管预算——`budget.NO_TOKEN_LINE`——也是 null，pct 跟着为 null）。`speed = {per_s, window_s}` 只在他这一轮 working 时给：最近 60 秒里这一轮累计数涨了多少 ÷ 秒数（开工不到 60 秒就从开工算起；不到 5 秒或这一轮还没有用量行不给）。`minutes` = 最近 30 分钟每分钟用了多少（整分钟对齐，最后一格是现在这一分钟；一格 = 这一分钟里各轮累计数涨了多少，一轮的第一行从 0 算），`minutes_from` 是第一格的开始时间；这 30 分钟没有用量行就不给。

> 1.17（2026-10-31，M5 切换）：新页面 `/board`（V2 看板：左侧对话列表、右边每名员工一列，列身是 `XayCliView.pane` 终端）；新接口 `GET /api/v2/convs`（对话列表，旧系统镜像的标 legacy）；管家除了 7879 还在看板端口 7878 提供同样的页面（被旧看板占着就每 30 秒再试，`~/.xay/v2/board.off` 关掉；启动时写了 `--port` 不开）；临时桥可关（`XAY2_BRIDGE=0` 或 `~/.xay/v2/bridge.off`）；`scripts/xay` 默认分发给 V2，旧系统才有的命令和旧会话照旧走旧版，`XAY_V1=1` 全部退回。

> 1.16（2026-10-31，四条线路真实额度）：旧系统 `scripts/xay_quota.py` 的四种官方查法移植到 `V2/xay/quota_live.py`（旧文件不动），页面、详情栏、管家决策都用真数。
> - **事件**：`quota.updated` 管家（O）也能写（§3 权限表追加；不带 subject）。管家后台线程每隔 `reactions.json` 的 `quota.refresh_s`（默认 300 秒，0 = 不查；Gemini 的 `agy -p /usage` 单独 `quota.agy_refresh_s`，默认 600 秒；每次最多等 `quota.timeout_s` 90 秒，各线路各开一个线程，不挡排程）查一次，`data = {line: codex-sub|codex-relay|dsh|agy, windows:[{key: 5h|day|week|month|<分钟>m, mins, used_pct, resets_at?}], source（大白话来源）, balance?/balance_usd?/warning?（中转站：还能用多少钱、订阅快到期）, account?/accounts?（Gemini：当前账号显示名；各账号 [{index, name, priority, current, status: ok|tight|exhausted|blocked|unknown, left, resets_at, checked_at}]，不含账号编号、邮箱）, error?}`；查不到写 `windows: []` + `error`（大白话）。变了才写（任一窗口差 ≥1 个百分点、重置时间差 ≥10 分钟、余额 / 账号 / 各账号状态 / 失败原因变了），没变也每 `quota.keepalive_s`（900 秒）写一次。Gemini 换了账号（Antigravity Tools 的当前账号变了）马上重查。查询用的 key 只在管家进程里用，不进账本、日志、页面。测试用的临时 `XAY2_HOME` 默认不查（`XAY2_QUOTA_LIVE=1/0` 强制），`XAY2_NO_QUOTA=1` 关掉这个线程。reducer 照旧不处理。
> - **快照 `quota`** 固定四条（顺序 codex-sub、codex-relay、dsh、agy）：`{line, agent, label, short, windows:[{key, name, mins, remaining_pct, resets_at}], remaining_pct（最紧的窗口）, status: ok|tight|exhausted|unknown, reached, source: live|none, source_label, fetched_at, stale, em, balance?, balance_usd?, warning?, error?, error_at?, account?, accounts?}`。有窗口的记录 = 一次读数，工位写的（Codex 通知）和管家查的谁新用谁；没窗口带 error 的只记原因、不清掉之前的读数；没窗口也没 error 的（工位在中转站上时）跳过；Gemini 读数的 account 和最新的当前账号不一样 → 作废（error"刚换了账号…"）。过了重置时间的窗口不列；读数超过 30 分钟 `stale=true`、`status=unknown`（照给页面看，不拦活）；剩余 <20% tight、≤0 exhausted。
> - **`person.quota`**：四家都按线路取快照里那条——Codex 看他现在的线路（Plus：5 小时 / 本周；中转站：今天 + 还能用多少钱），DeepSeek：5 小时 / 本周 / 本月，Gemini：当前账号 + 剩余 + 各账号 + `calls`（今天还能调几次）。`{kind: pct, line, line_name, left, status, windows:[{key, name, left, resets_at}], fetched_at, stale, source, balance?, warning?, error?, account?, accounts?, calls?}`；还没读到 `{kind: none, line, line_name, why: 还没读到|没查到：…}`；Gemini 没读到时退回 `{kind: calls, left, used, cap, line, line_name, why}`。不再有"这家看不到"/"中转站看不到"。
> - **管家决策**：规则监工每拍算"哪种 AI 的线路全用完了"（Codex 两条线路都 exhausted，机密项目只看 Plus；DeepSeek exhausted；Gemini 当前账号 exhausted 且别的账号都用完 / 停用）→ 这种 AI 的活留在 ready 不派、不起工位（只剩"合并 / 丢掉改动"那一步的照起），写 `signal.raised{kind: quota, level: warn}`"……先排着，额度恢复了自动接着派"（一次用完只写一次）；工位开工前也看一眼，全用完就先不开工。额度原因失败、工位没换过的：要换去的线路也用完了 → 工位不换，乙类 failure 的 detail 写明"中转站今天也用完了（明天 00:00 重置），没有换过去"。Gemini 换号跳过真实额度已经用完的账号（`quota.switch_agy_account(skip=…)`）。页面顶栏加额度条（四个小格，偏紧黄、用完红，鼠标停留看各窗口重置时间）。

> 1.15（2026-10-30，详情栏第 3 版，设计见 `references/plans/ui-v2-employee-panel.md`）：① 新事件 `quota.updated`（subject = em，只有工位 S 能写，§3 权限表追加）`{line: codex-sub|codex-relay, windows:[{key: 5h|week|<分钟>m, mins, used_pct, resets_at?}], reached?}`——Codex 适配器把 app-server 的 `account/rateLimits/updated`（稀疏更新，null 不清掉旧值；只看 `limitId` 为 codex 或没给的那份）合并成统一事件 `rate_limits{line, windows, reached?}`，工位变了才写（任一窗口差 ≥1 个百分点、重置时间 / 线路 / reached 变了，或距上次 ≥10 分钟）；走中转站时两个窗口都是 null，`windows` 为空。**reducer 不处理**（§0.5 认不出的类型跳过，不进现状表）。② 快照 `quota` 不再是空数组：每条线路最新的一条 `quota.updated` → `{line, agent, label, windows:[{key, name, mins, remaining_pct, resets_at}], remaining_pct（最紧的窗口）, reached, source: live, fetched_at, em}`，过了重置时间的窗口不列。③ `/api/v2/employee` 只加字段：每行 `n`（第几件：按第一次派活的先后，撤回的是 null）、`count:{all, done, working, queued, failed, needs_you, canceled}`（按合并后的行；all 不算撤回）、`focus`（"现在"块显示第几件：正在做 → 排队时的下一件 → 最近结束的一件 → 排着队的第一件）、`person.quota`（`{kind: pct, left, windows:[{key, name, left, resets_at}], line, line_name, fetched_at}` / `{kind: calls, left, used, cap}`（Gemini 今天的调用次数）/ `{kind: none, why}`（DeepSeek"这家看不到"、中转站"中转站看不到"、Plus 还没报过"还没读到"）；Codex 的线路 = 现状里员工的 `line` → 他最近一条 quota.updated 的 line → codex-sub）、`person.sees_thinking`（agy 为 false）、`person.line`、`person.budget`（同快照 `people.budget`）。④ `/api/v2/live` 加 `tail=<1..500>`：第一次补发最近多少行（默认 200，终端小屏用十几行）；不带 `plain` 时就是原样的命令、输出、回复和思考（页面"看他的屏幕"终端窗口用）。

> 1.14（2026-10-30，M4 收尾）：M3 / M4 合并后补的对接：
> - **额度失败**：额度原因 = `turn.ended.evidence.error_kind` ∈ capacity / quota / rate_limit。这件活期间（第一轮开工 → 记失败）工位已经补救过——有 `employee.session{reason: line_switch}`，或工位写的 `signal.raised{kind: quota}`（幂等键以 `agy-cap:` 开头的"今天 40 次到了"不算）——还失败 → 不再自动重试、不再换，乙类 `failure{cause: quota}` 交负责人。被限流、工位又没换的 → 等 `failure.rate_limit_wait_s`（默认 120 秒）直接 `task.queued{retry: env}` 一次（不写信）；`error_kind=daily_cap` → 直接 `task.queued{retry: env}`。
> - **一轮中途的 token**：reducer 把 `context.updated` 的 `turn_tokens` 记在 `turns[turn].turn_tokens`（只增不减，收工后的最终数也记）。预算的 tokens 每轮按 max(`usage.total`, `turn_tokens`) 算，90% 收尾 / 100% 打断在一轮中途就能触发。
> - **总闸连命令行**：reducer 把 `station.heartbeat.cli_pids` 记在 `employees[em].cli_pids`，`employee.offline` 时清掉。管家强杀工位（看门狗 SIGTERM；总闸 SIGTERM → SIGKILL）时对这些 pid 的进程组发同样的信号：只认还活着、自己是进程组组长、父进程就是这个工位的；工位已经没了的，只认上一步核对过、命令行没变的。
> - **agy 每天 40 次**：管家和工位用同一个数——次数 `xay.quota.agy_calls_today()`（V2 + 旧系统当天），上限 `quota.agy_daily_cap()`；`reactions.json` 的 `agy_daily_calls` 默认 0（= 跟工位一样），写更小的数才提前收手。不再按账本轮数另算。
> - **叫停后接着干**：`stop <员工>` 之后，甲类"需要你"选 rework、管家替你写的 `task.queued{retry: manual}`、或 `resume <员工>`（以前就有）→ 解除叫停、重新起工位。选 merge / discard 而他手上没有排着的活 → 也起工位，只做这一步（做完就下班）。总闸拉着时照旧都不起。
> - **自动发号**（§1、§4.1）：本项目同种 AI 现有的最大号 +1（和以前一样，老账重放名字不变），再跳过本项目里已被占用的显示名（任何 AI 的、手动起的、已离职的）。REDUCER_VERSION 8。另：M3 冒烟里第一个 dsh 员工叫 DeepSeek 5，不是编号跨项目了，而是桥把同一个 Claude 会话以前在旧系统里跑的活镜像进了新开的 V2 对话（§9 按别名 `claude:<uuid>` 找对话），旧系统的 4 个 dsh 员工记在了新项目名下、占了 1~4 号——桥是临时的（M5 切换时删除），不改；新员工的显示名按上面的规则跳过已占用的名字。
> - **"需要你"和页面一致**：快照 `decisions[]` 另含没挂对话（conv=null）、但属于本项目的；`people.state=blocked` 只看这一页 `decisions[]` 里的。管家写员工级的"需要你"（今天的预算、复述不出来）时挂到他正在做 / 最近做 / 排着的那件活的对话上。页面顶栏"待答需求" = `decisions[]` 条数 + 写给部门、还没回的请求信。
> - **详情栏**：agy 的"正在想"显示"这家看不到"（agy 不提供思考内容，arch-v10 §3.3）；`employee.session.native_id` 为 null 时快照和 `/api/v2/employee` 照常。

> 1.13（2026-10-30，工位收尾）：换额度线路改成双向——当前线路（Plus 或中转站）因为 `capacity / quota / rate_limit` 没做成、这一轮没改文件 → 换到另一条线路、同一个对话重做，每件活仍只换一次；机密项目只是不换去中转站（换回 Plus 可以）；`employee.session{reason: line_switch, line}` 的 `line` 可以是 `codex-sub`（换回 Plus），之后的活留在新线路上。中转站的每日额度用完报的是 `403 … DAILY_LIMIT_EXCEEDED`（"每日订阅额度不足"），归为 `error_kind=quota`（§4.1 `employee.session` 那一行原来只写了"换中转站"）；别人写的 `context.compact_requested`（L / O / R）由工位处理——`method` 多一个取值 `auto`（不写也算 auto）= 工位自己挑（能原生就原生；同一段已经原生压满 2 次、或 2 小时内 CLI 自己压了 2 次就交接）；要 `native` 但这家没有能用的自带压缩（dsh）就交接。工位只在两件活之间处理（不打断正在干的这一轮），结果照常写 `context.compacted`；员工这时的状态不是 `compacting`（比如工位上线时 `employee.online` 把它清成了空闲、或者请求是他干活时来的）就由工位再写一条自己的 `context.compact_requested`（`reason` 照抄请求，`method` 写真正用的）。**"处理过"全看账本**：请求之后有过一次 `context.compacted`（不算 `native_auto`；要求 `handoff` 的得是 `handoff / machine`）就算处理过，所以同一个请求只处理一次、工位重启也不重复；请求之前（上次整理之后）一轮活都没干过的，没有要整理的，直接不处理。注意：工位不在线时这条请求只是把员工标成 `compacting`，要等管家起工位（现在管家只在有活排队时起工位）才会真的整理。

> 1.12（2026-10-30，M3 dsh / agy 员工）：dsh / agy 员工上线补的字段：`employee.session.native_id` 可以是 null（新对话号还不知道时先清掉旧号，号报上来再补一条同段的）；`turn.ended.evidence` 加 `error_kind`；`context.compact_requested.reason` 加 `error_loop`；`signal.raised.kind` 加 `config`（设置不对、拒绝开工）、`cli_version_changed`（M2 起已在用）；任务卡（对象时）可带 `verify`（验收命令列表）、`model`、`effort`；agy 的 `context.updated.used` = 新输入 + 缓存读取；工作笔记 = 员工摘要 + 工位事实块，放进新对话前加"只作参考"围栏。按管家自动化那边的要求另补：`error_kind` 细分出 `rate_limit / auth / network`；一轮进行中 `context.updated` 带 `turn` + `turn_tokens`（这一轮到现在累计的 token）；`station.heartbeat` 带 `cli_pids`（命令行子进程，各自是进程组组长）；执行者自己 commit 的提醒改用 `executor_commit`，`error_loop` 带 `task`；规则监工的插话在员工那边写成"【管家提醒】"；叫停 / 总闸停下的活 `task.kept{reason: paused}`。见 §4.1、§4.2、§4.3、§4.4、§4.5 里标"M3"的地方。

> 1.11（2026-10-30，M4 管家自动化）：选人三步规则（task.assigned 理由照旧是大白话）；反应表 / 预算的默认值写在代码里，可用 `$XAY2_HOME/reactions.json` 覆盖；新事件 `recall.requested`；规则监工（R）可以 `decision.resolved`，但只限它自己开的、或把合并冲突自动"再合并一次"；`task.queued` 可带 `retry`；信件 `from` 多一个 `office`（管家）；乙类"需要你"多 `failure / error_loop / silent` 三种，decision 可带 `cause / budget / target`；`budget.*` 的 subject 也可以是员工；`signal.raised` 多一种 `conflict`、error_loop 可带 `task`；总闸记进现状（快照顶层 `halt`），你在页面上拉的只有你"恢复"才解除；快照 `people.budget` = `{pct, dim?, scope?}`；新接口 `GET /api/v2/halt`，`POST /api/v2/control` 的 `resume` 可以不指定员工。细节见 §3、§4.3~4.5、§8 里标"（1.11）"的地方。

> 1.10（2026-10-30，详情栏重做）：`/api/v2/employee` 的 `timeline` 改成合并后的行——同一员工、同一对话里标题相同且前一次没做成或被撤回的算"再试一次"，合进一行，加 `attempts`、`tries`；每行加 `task.short_title`、`task.first_started`（合并过才有）、`key_steps`、`result.short`；`total` 改为合并后的行数；新增 `today`；`person.now` 用短标题、`person.progress.text` 只留一句、`person.notes` 去掉任务编号。只加字段、不删字段（§8）。

> 1.9（2026-10-30，M4 信件与计划）：信件真正送到员工手里——送信规则（§4.5 末尾，写给部门的信按 head 和"最相关的成员"挑人）、工位开工时把信放在这一轮输入开头（next_turn）、Codex 干活中途当场插话（steer）、`xay2 wait` 打印写给总经理的信时记"总经理已看到"（inbox，§3 负责人可写 letter.delivered）；`letter.delivered` 带 `em`/`task`；回信（带 reply_to）关掉原来那封 request；新标记【回信 lt_…】（§6）。改计划申请：提申请时就校验、处理过的申请留档不能再处理、通过的那一版历史带 `proposal`/`approved_by`、处理完写信告诉提申请的员工。`wait` 合并"已合并 / 没做成"、"改动先保留"并进紧跟的"需要你"；`brief` 必含五块。快照：`letters[].delivery`、`plan.history[].summary/change_texts`（§8）。

> 1.8（2026-10-30，用户反馈"为什么还是没分组、头像都没了"）：管家把领了活却没分部门的员工放进部门（任务写的部门 → 管他要改的文件的部门 → 本对话的"项目组"，编号 `proj`，没有就由管家开）；`employee.offline` 的原因进现状表，正常下班（idle/stopped）在页面上显示成在休息，只有工位崩了才显示离线。

> 1.7（2026-10-30，M2 工位）：新事件 `employee.thought`；工位可写自己任务的 task.failed、自己员工的压缩请求；桥可开/关它镜像的部门；执行者从 `XAY2_TASK_FILE` 读当前任务；新接口 `/api/v2/employee`、快照 `activity` 字段、直播 `plain=1`；记忆段号只在开新对话时 +1。

> 1.6（2026-10-30）：员工显示名只在同一个项目（ws）里唯一；桥可以改它自己建的对话的标题；没有具名员工的旧活记在"<AI> 临时工"名下。

> 1.5（2026-10-30）：部门编号只在对话内唯一（内部按对话分开存）；旧系统镜像的活和信要在页面上显示（`legacy: true`）；推送里没有大白话的事件也发 `changed` 让页面刷新；页面默认对话 = 最近有动静的；`recent` 只取本对话。

> 1.4（2026-10-30）：命令行细节按测试发现补齐——批量改计划只加一次版本（`data.batch`）；执行者环境变量；`plan move` 的映射；`begin` 自动登记别名；对话不明确就报错；`wait` 的过滤、游标与 `--timeout 0`。

> 1.3（2026-10-30）：证据表补两行——只读任务却改了文件 → 保留；说做完了但失败/被打断且没有改动 → 保留（结果对不上，由人看）。

> 1.2（2026-10-30）：dsh 第二轮样本提的 6 个问题——turn.ended 带 em、proposal.made 带 from、高版本事件照常入账但 reducer 跳过、former 排序与样本里"键不存在"、申请新增任务可带 owner、管家自动分配不写计划历史。

> 1.1（2026-10-30）：按 dsh 写样本时提的 13 个问题补齐——§3 改成逐个事件的写入权限表；明确"需要你"的两类与选项；离职员工、压缩后百分比、字段合并、申请里新增任务、高版本事件、显示名、计划历史的 by、样本测试被拒后继续。

> 本文是 V2 的**唯一接口约定**：账本里有哪些对象、编号怎么起、有哪些事件、事件怎么改变现状、谁能写什么、页面拿到什么。
> 设计理由见 `references/plans/arch-v10-rebuild.md`。代码与本文冲突时以本文为准；要改本文，先在汇报里提出，由负责人改文档再改代码。
> 写代码的 AI：先读 §0 和你负责的那几节。所有字段名用英文，所有给人看的文字用大白话中文。

## 0. 总则

1. **只有一本账**：`~/.xay/v2/xay.db`（SQLite，WAL）。可用环境变量 `XAY2_HOME` 改根目录（测试必须用临时目录）。
2. **所有改动 = 追加事件**：任何程序都不直接改现状表。统一走 `core.commands` → 校验 → `core.events.append()`，它在**同一个事务**里写事件、跑 reducer、更新现状表。
3. **现状表是事件的投影**：`xay2 rebuild` 清空现状表后按 seq 顺序重放全部事件，结果必须与重放前**逐行一致**（测试项）。
4. **reducer 是纯函数**：`(现状, 事件) → 现状`，只读事件字段与现状表，**不读时钟、不读文件、不调外部程序**。需要时间就用事件的 `ts`。
5. **认不出的事件类型 → 跳过**（向前兼容）。事件 `v` 大于当前代码支持的版本 → **照常写进 events 表**（占一个 seq，留给以后的新代码），但 reducer 跳过、不改现状表、不进快照，只在 `~/.xay/v2/logs/core.log` 记一行警告；不写 signal。
6. **大白话**：凡是会出现在页面上的字段（`title`、`text`、`reason`、`summary`、`subject`）都是给小白看的中文；技术细节放 `detail`。
7. **不猜**：现状里的每个状态值都能指出是哪条事件导致的。页面不自己推断状态。

## 1. 编号

| 对象 | 格式 | 怎么来 |
|---|---|---|
| 工作区 ws | `ws_` + sha1(项目 git 根目录的真实路径；不在 git 里就用目录本身)[:8] | `core.ids.ws_for(path)` |
| 对话 conv | `cv_` + `YYYYmmdd-HHMMSS` + `_` + 4 位十六进制 | `xay2 begin` 时生成 |
| 对话别名 alias | `claude:<CLAUDE_CODE_SESSION_ID>`、`codex:<CODEX_THREAD_ID>`、`ls:<旧 ls_ id>`、`name:<自定义>` | 一个 conv 可有多个别名；一个别名只属于一个 conv |
| 员工 em | `em_` + 8 位十六进制 | 招聘时生成；显示名 `label` 另存 |
| 员工显示名 label | `<AI 名> <序号>`：Codex / DeepSeek / Gemini（agy）+ 该 ws 内该 AI 的第几个员工 | 招聘时由 reducer 分配，之后不变、不复用 |
| 小任务 pt | `pt_` + 8 位十六进制；另有可选短名 `key` | `xay2 plan part` |
| 部门 dept | 负责人给的短名（`[a-z][a-z0-9_-]{0,15}`），在 conv 内唯一 | `xay2 dept open` |
| 任务 tk | `tk_` + 8 位十六进制；另有可选短名 `key`（conv 内唯一，用于 deps 和命令） | `xay2 plan add` |
| 一轮 turn | `tn_` + 8 位十六进制 | 工位开工时生成 |
| 信件 lt / 决定 dc / 申请 pp | `lt_` / `dc_` / `pp_` + 8 位十六进制 | 写入时生成 |
| 事件 seq | 整数，账本自增，全局唯一、只增不减 | 数据库生成 |

**谁是"当前对话"**（命令行按顺序找，找到为止）：`--conv` 参数 → 环境变量 `XAY2_CONV` → 别名 `claude:$CLAUDE_CODE_SESSION_ID` → 别名 `codex:$CODEX_THREAD_ID`。都没有就报错，提示先 `xay2 begin`。**不猜**：即使账本里只有一个或有多个对话，也不自动挑"最近的"（`xay2 snapshot`/`brief` 允许 `--latest` 显式要求最近的）。执行者进程里 `XAY2_CONV` 由工位设置。

## 2. 账本表结构

```sql
CREATE TABLE events (
  seq    INTEGER PRIMARY KEY AUTOINCREMENT,
  ts     REAL    NOT NULL,          -- epoch 秒（小数）
  v      INTEGER NOT NULL DEFAULT 1,
  type   TEXT    NOT NULL,          -- 见 §4
  ws     TEXT,                      -- 大多数事件都有
  conv   TEXT,                      -- 属于某个对话的事件才有
  subject TEXT,                     -- 主要对象的编号（em_/tk_/lt_/…）
  by     TEXT    NOT NULL,          -- 谁写的，见 §3
  data   TEXT    NOT NULL,          -- JSON 对象
  dedup  TEXT UNIQUE                -- 可选幂等键：同一个键只记一次（桥、发件箱、重试用）
);
CREATE TABLE meta (k TEXT PRIMARY KEY, v TEXT);   -- schema_version=1 等
```
- 打开数据库：`PRAGMA journal_mode=WAL; PRAGMA busy_timeout=5000; PRAGMA foreign_keys=ON;` 写事务用 `BEGIN IMMEDIATE`。
- `dedup` 冲突时 `append()` 返回已有那条的 seq，**不报错**（幂等）。
- 现状表（`workspaces, convs, aliases, depts, employees, tasks, plan_versions, turns, letters, decisions, proposals, notes`）的列由实现决定，但必须能产出 §8 的快照，并满足 §0.3。
- 升级：`V2/xay/core/migrations/NNN_*.py`，按 `meta.schema_version` 依次执行。每天第一次启动管家时 `VACUUM INTO ~/.xay/v2/backup/xay-YYYYmmdd.db`，保留 7 份。

## 3. 谁能写什么（`by` 字段）

**角色**：

| by | 是谁 | 简写 |
|---|---|---|
| `lead:<conv>` | 这个对话的负责人（总经理；以后的部门负责人也用它） | L |
| `user` | 用户（从页面操作） | U |
| `office` | 管家 | O |
| `rules` | 规则监工（管家里的反应表） | R |
| `station:<em>` | 某员工的工位 | S |
| `employee:<em>` | 执行者本人（经工位校验后代写，见 §6） | E |
| `bridge` | 临时只读桥（M1，§9；已删除，只剩历史事件） | B |

**每个事件谁能写**（不在表里的角色写入 → `PermissionError`）：

| 事件 | 允许 |
|---|---|
| `conv.begun`、`conv.aliased` | L、B |
| `dept.opened`、`dept.closed`（桥只开/关它镜像的旧部门和项目组） | B（另见下行 L）；`dept.opened` 另有 O（只开本对话的"项目组" `proj`，见 §4.1） |
| `conv.titled` | L、B（桥只改它自己建、负责人没改过名的对话） |
| `conv.ended`、`dept.opened`、`dept.closed` | L |
| `employee.hired`、`employee.joined` | L、O、B |
| `employee.session`、`employee.online`、`employee.offline` | S、O |
| `employee.thought` | S（每 3 秒最多一条） |
| `employee.fired` | L、U |
| `station.heartbeat`、`desk.synced`、`turn.started`、`turn.ended`、`context.updated`、`context.compacted`、`recall.checked` | S |
| `task.added`、`task.changed`、`task.removed` | L |
| `task.assigned` | L、O |
| `task.ready`、`task.queued` | O |
| `task.progress`、`task.reported`、`notes.updated` | S、E |
| `task.landed`、`task.kept` | S、O（证据结论：工位做完合并时写 landed/kept；工位崩溃、离线等由管家写 kept/failed） |
| `task.failed` | O、L、S（只限自己的任务） |
| `task.accepted` | L、U |
| `task.canceled` | L、U、O |
| `desk.removed` | U（只有用户能删桌子） |
| `proposal.made` | L、E |
| `proposal.decided` | L、U |
| `context.compact_requested` | O、R、L、S（只限自己的员工） |
| `letter.sent` | L、U、E、O、R |
| `letter.delivered` | O、S、L（L 只在 `xay2 wait` 打印写给总经理的信时写 how=inbox） |
| `letter.answered` | L、U、E |
| `decision.opened`、`signal.raised` | O、R、S |
| `decision.resolved` | L、U；R（1.11）：只限规则监工自己开的（事情过去了自动关掉），或者把 kind=conflict 的选 merge（合并冲突自动再合并一次） |
| `recall.requested`（1.11） | O、R |
| `budget.warned`、`budget.stopped` | O、R |
| `control.requested` | L、U、R |
| `control.done` | O、S |
| `bridge.*` | B |
| 未知类型（以后版本的事件） | L、O、R、S 可以写入（reducer 跳过）；U、E、B 写入被拒 |

**身份范围**：`station:<em>` 和 `employee:<em>` 只能写"自己的"事件——subject 是自己的 em；或 subject 是一轮 turn 且 `data.em` 是自己；或 subject 是任务且该任务当前 owner 是自己（letter/proposal 不受此限，但 `data.from` 必须是自己）。`lead:<conv>` 只能写本对话（事件 `conv` 等于自己）的事件，`employee.*` 除外（员工归 ws）。

`core.commands` 按上表拒绝越权写入。命令行发现 `XAY_ROLE=executor` 时只允许 `progress / done / ask / tell / reply / propose / notes`，并且这些命令只写发件箱（§6），不碰数据库。

## 4. 事件目录（v=1）

格式：`type`｜`subject`｜`data` 字段｜reducer 做什么。`data` 里没写的字段不得出现在现状里。

### 4.1 对话、部门、员工
| type | subject | data | reducer |
|---|---|---|---|
| `conv.begun` | conv | `{title, lead_kind: claude|codex|dsh|agy|unknown, aliases:[…], cwd}` | 建 conv（`plan_version=0`），登记别名（已被别的 conv 占用的别名忽略并记警告），建 ws（若无） |
| `conv.aliased` | conv | `{alias}` | 加别名 |
| `conv.titled` | conv | `{title}` | 改标题 |
| `conv.ended` | conv | `{}` | 标结束（只读，不删任何东西） |
| `dept.opened` | dept | `{name, mission, owns:[glob], head: rules|claude}` | 建部门（conv 内）。**管家归部门**：任务有了负责人、而他在本 conv 不属于任何部门 → 管家写 `employee.joined`，去处依次是：任务的 `dept`（且开着）→ `owns` 能匹配任务 `writes` 的部门 → 本 conv 的"项目组"（`proj`，name=项目组，mission=对话标题；没有就由管家先写 `dept.opened`） |
| `dept.closed` | dept | `{}` | 标关闭；成员回到"未分部门" |
| `employee.hired` | em | `{agent: codex|dsh|agy, line?: codex-sub|codex-relay, label?, note?}` | 建员工（归 ws），`segment=1`，状态 `idle`。不给 label 就自动发号（§1）；给了 label 而且本项目里没人用过就用它（不占自动序号），同项目重名 → `ValueError`；显示名只在同一个 ws 里唯一 |
| `employee.joined` | em | `{dept}` | 员工在本 conv 内加入部门（同一员工在不同 conv 可属于不同部门） |
| `employee.session` | em | `{native_id, segment, reason: new|resume|handoff|resume_failed|line_switch, line?, cli_version?}` | 记录当前原生对话号与第几段记忆；带 `line` 时员工改用这条额度线路（Plus 满载/额度不够时工位换中转站，同一个对话接着做，只换一次）。**M3（1.12）**：`native_id` 可以是 null——交接 / 续接失败后新对话号还不知道（dsh 第一轮才有、agy 的 init 可能晚到），先写一条清掉旧号，免得下次上班又接回旧对话；号报上来后工位再写一条同 segment、同 reason 的（不重复算段）。`resume_failed` 由工位在这些情况写：agy 冷续接后 30 秒内没工具也没文字 / 没动作就结束 / init 换了对话号，dsh 不认 `--session-id`；随后交接（机器版摘要）、开新对话、同一件活重做一次 |
| `employee.online` / `employee.offline` | em | `{pid?}` / `{reason: idle|crash|stopped|fired}` | 改在线状态 |
| `employee.fired` | em | `{reason}` | 状态 `fired`；快照 `people` 里不再出现，改列在 `former`；显示名不回收；桌子要另有 `desk.removed` 才删 |
| `employee.thought` | em | `{text}` | 最近一句思考（快照 `people.thought`）；这一轮结束时清空；没有大白话，页面只刷新 |
| `station.heartbeat` | em | `{pid, cli_pids?:[pid…]}` | 只更新 `last_heartbeat`（**reducer 不改状态**；离线判断由管家写 `employee.offline`）。**M3（1.12）**：`cli_pids` = 工位起的命令行子进程（Codex app-server / 这一轮的 dsh / 常驻的 agy），每个都是 `start_new_session` 起的、自己就是进程组组长；总闸 / 管家要强杀工位时用 `killpg(pid)` 连它们一起杀（工位自己的进程组里不包括它们）。每 10 秒一条，命令行进程换了（dsh 每轮一个、agy 重启、换线路）马上补一条 |

### 4.2 计划（任务拆解是一等对象）
| type | subject | data | reducer |
|---|---|---|---|
| `task.added` | tk | `{key?, title, dept?, owner?: em, deps:[tk|key], writes:[glob], steps?:[文字], card, budget?:{tokens?, minutes?, turns?}, mode: write|readonly, reason}` | 建任务，状态 `planned`；**conv 计划版本 +1**，写一条计划历史 |
| `task.changed` | tk | `{fields:{…可改字段}, reason}` | **只改给出的字段**（数组字段整体替换）；可改：title、dept、deps、writes、steps、card、budget、mode；版本 +1 |
| `task.removed` | tk | `{reason}` | 状态 `canceled`；版本 +1 |
| `task.assigned` | tk | `{owner: em, reason}` | 改负责人，记 `assigned_reason`；版本 +1 并写计划历史。`by=office` 的自动分配**不**加版本、**不**写计划历史，只更新负责人和 `assigned_reason` |
| `proposal.made` | pp | `{from: em|gm|dept, changes:[{op: add|change|remove|assign, task, fields?}], reason, to: dept|gm}` | 建申请，状态 `open`。`op=add` 时 `task` 由提出者新生成（`tk_…`，写申请时就定），`fields` 至少有 title，还可带 owner、dept、deps、writes、steps、card、mode（同 task.added）；change/remove/assign 的 `task` 必须已存在 |
| `proposal.decided` | pp | `{verdict: accept|reject, note?}` | accept：按 changes **原子地**应用（等价于对应的 task.* 事件，但只加一次版本），版本历史 reason = 申请理由 + "（申请 pp_… 通过）"；reject：只关申请 |

**任务卡 `card`（M3，1.12）**：可以是文字，也可以是对象 `{text, verify?:[命令], model?, effort?}`。工位从 `verify`、以及卡片文字 / 步骤里写着"验收"那一行的 `` `命令` `` 取验收命令：agy 每件活的临时 `--settings` 只放行这些命令（另外总是 deny `git commit` / `git push`）；交接事实块里记它们最近一次的结果。`model` / `effort` 只对 dsh / agy 生效，开工前按硬性约束校验（dsh 只许 `deepseek-v4.1-flash`，agy 只许 `gemini-3.8-*`），不合规就 `task.failed` + `signal.raised{kind: config}`，不起任何进程。

补充（1.9）：
- `proposal.made` 入账时就校验 changes（不对 → `ValueError`，账本不收）：1~N 条；op ∈ add/change/remove/assign；add 的 `task` 是新编号、`fields.title` 必填；change/remove/assign 的任务已存在且属于本对话（也可以是同一个申请里前面 add 的）；assign 要有 `fields.owner`。通过时再校验一次（中间任务可能变了），不对就只能 reject。
- 处理过的申请**留档**：`status` = accepted / rejected，另记 `decided_by`、`decided_ts`、`note`、通过时的 `version`；快照 `proposals` 仍只列 open 的。同一个申请第二次 `proposal.decided` → `ValueError`；找不到的申请 → `ValueError`。
- assign 通过时任务的 `assigned_reason` = 申请理由（和 task.assigned 一样）。
- 通过的那一版计划历史另带 `proposal: pp_…`、`approved_by: gm|user`（页面写"Codex 1 提的申请，总经理通过了"）。
- `xay2 answer pp_… accept|reject ["说明"]` 处理完，命令行写一封 `letter.sent{from: gm, to: <提申请的员工>, kind: notice}` 告诉他结果（总经理自己提的不写）。

计划历史每条：`{version, by, ts, reason, changes:[{op, task, title, before?, after?}]}`。
- **批量**：task.added/changed/removed/assigned 可带可选 `data.batch`（一个字符串）。同一对话里**连续**的、batch 相同的事件只加一次版本，共用一条计划历史（changes 依次追加，reason 取第一条）。`xay2 plan apply` 和申请通过都用它。
- `by` 是给页面显示的身份：`lead:*` → `"gm"`，`user` → `"user"`，`employee:<em>`/`station:<em>` → `<em>`，`office`/`rules` → `"office"`。申请通过时 `by` = 提出申请的人。
- `before/after` 只含被改的字段：add 的 after = `{title, owner, dept, deps}`；change 的 before/after = 改前/改后的那几个字段；assign 的 before/after = `{owner}`；remove 的 before = `{status}`。

### 4.3 派活与一轮
| type | subject | data | reducer |
|---|---|---|---|
| `task.ready` | tk | `{}` | `planned → ready`（前置都 done） |
| `task.queued` | tk | `{owner, position, retry?: auto|env|manual|paused}` | 进员工队列，状态 `queued`。（1.11）`retry` = 规则监工重排：auto 非额度失败后让他接着修、env 开工前出状况（没对齐桌子 / 没上线等）直接重排、manual 你在"需要你"里选了再试一次、paused 叫停停下且无改动的活在工位下线后重新排队（恢复后接着做）；按种类分别计数（反应表的上限），manual 会清掉"一直报错"标记 |
| `desk.synced` | em | `{task, base, mode: reset|kept_conflict}` | 记录这件活开始时桌子的版本 |
| `turn.started` | tn | `{task, em, native_turn?, kind: first|continue|recall|compact|handoff_write|chat|denied_retry|network_confirm|network_continue|plain_retry}` | 任务 `working`，员工 `working`，`now` = 任务标题。`kind=chat` 时 `task` 为 `null`，不改任何任务状态，员工进入 `working`，结束回到 `idle`；聊天轮若改了项目文件不合并，写 `signal.raised{kind: "chat_changed_files", level: "warn"}`。`kind=denied_retry`：agy 一轮中命令被系统拒绝且无改动时，工位在同一对话里自动补发的一轮（提醒用自带工具做完，每件活最多补一次）；`kind=network_confirm`：交活前因网络错误断开（有做完汇报与改动）时，工位在同一对话里自动补发的一轮（提醒跑验收并汇报，每件活最多补一次）；`kind=network_continue`：干到一半因网络错误断开且未报做完时，工位在同一对话里自动补发的一轮（提醒改过的文件都在并接着做完，每件活最多补两次） |
| `task.progress` | tk | `{step, of, text, source: native_plan|marker|outbox}` | 更新任务和员工的进度 |
| `turn.ended` | tn | `{em, status: completed|failed|interrupted, evidence:{cli_status, claim?, changed_files:[…], diff_stat:{files,add,del}, paused?:bool, verify?:[{cmd, ok}], error?, error_kind?}, usage?:{in, out, cached, total}}` | 结束这一轮；员工回到 `idle`（队列不空则 `queued`）。**M3（1.12）**：没正常结束时 `evidence.error_kind` ∈ `capacity`（满载）/ `quota`（额度用完）/ `rate_limit`（限流，等一会儿就好）/ `context_overflow` / `auth`（登录 / 密钥不对）/ `network` / `resume_failed` / `config` / `error_loop` / `daily_cap`（agy 当天 40 次到了）/ `other`，给反应表分辨"额度原因失败"（= capacity / quota / rate_limit；工位已经先补救过一次：Codex 换线路、agy 换号）。叫停 / 总闸停下的那一轮 `evidence.paused` 为真，有改动时任务记 `task.kept{reason: paused}`（甲类"需要你"的 kind 仍是 interrupted / failed_with_changes，合并 / 接着改 / 丢掉照常），无改动时只写 `turn.ended`，由管家重新排队恢复后接着做。死循环被工位叫停的那一轮记 `failed`（不是 interrupted）。agy 无头模式里被拒绝的操作记在 `evidence.denied:[…]`（≤10 条） |
| `task.reported` | tk | `{claim: done|partial|blocked, summary}` | 记录汇报（≤600 字大白话） |
| `task.landed` | tk | `{files:[…], add, del}` | 状态 `reported`（等负责人验收），记录落地文件 |
| `task.kept` | tk | `{reason: mismatch|failed_with_changes|paused|conflict|interrupted|desk_unmerged, detail}` | 状态 `needs_you`；**桌子保留** |
| `task.accepted` | tk | `{note?}` | 负责人验收：`reported → done` |
| `task.failed` | tk | `{reason, detail}` | 状态 `failed` |
| `task.canceled` | tk | `{reason}` | 状态 `canceled` |
| `desk.removed` | em | `{approved_by: user}` | 记录删桌子（只有用户同意才写） |

**证据规则**（工位在 `turn.ended` 之后、管家决定 landed/kept/failed 时用；唯一实现是纯函数 `xay.core.evidence.verdict(cli_status, claim, has_changes, mode) → ("landed", None) | ("kept", reason) | ("failed", reason)`，从上往下取第一条匹配的行）：
| cli_status | claim | 有改动 | 结论 |
|---|---|---|---|
| completed | done / 无 | 有，且 mode=write | landed |
| completed | 任意 | 有，且 mode=readonly | kept(mismatch)："只读任务却改了文件" |
| completed | done / 无 | 无，且 mode=readonly | landed（只读任务，汇报即成果） |
| completed | done / 无 | 无，且 mode=write | kept(mismatch)："说做完了但没有改动" |
| completed | partial / blocked | 任意 | kept(mismatch) |
| failed / interrupted | done | 有 | kept(mismatch) |
| failed / interrupted | done | 无 | kept(mismatch)："说做完了，但这一轮没正常结束，也没有改动" |
| failed / interrupted | 非 done | 有 | kept(failed_with_changes) |
| failed | 非 done | 无 | failed |
| interrupted | 非 done | 无 | kept(interrupted) |

### 4.4 记忆
| type | subject | data | reducer |
|---|---|---|---|
| `context.updated` | em | `{used, window, pct, source: codex_last|dsh_total|agy_input, estimated: bool}` | 更新记忆条（工位只在变化 ≥2 个百分点或距上次 ≥30 秒时写）。**M3**：`dsh_total` 的 window 是 dsh 自己认为的窗口（`dsh --dump-config` 里用户路由的配置，本机 262,144）；`agy_input` 的 used = `input_tokens + cache_read_tokens`，window 256,000（estimated）。**M3（1.12）**：一轮进行中另带 `turn`（tn）和 `turn_tokens`（这一轮到现在累计用的 token，口径同 `turn.ended.usage.total`：in + out + cached），累计每涨 2 万写一次（不受"2 个百分点"限制），给预算的 token 线在一轮中途用；这一轮结束时再写一次最终数。reducer 现在不存这两个字段，要用的话由管家那边读（或者在 turns[tn] 上加一个"进行中的用量"） |
| `context.compact_requested` | em | `{reason: threshold|rounds|auto_twice|resume_failed|error_loop|manual, method: native|handoff|auto, note?}` | 员工状态 `compacting`。**M3（1.12）**：加 `error_loop`（连续 3 次报错、中间没用工具 → 停下后交接）。门槛 `threshold` 按"百分比或绝对 token 数谁先到"（dsh 55% 或 30 万） |
| `context.compacted` | em | `{method: native|native_auto|handoff|machine, before_pct, after_pct?, segment}` | 结束 `compacting`；`compactions+1`；segment 更新；有 after_pct 就把 `pct` 设成它，没有就保留原值；两种情况都 `calibrating=true`，直到下一条 context.updated |
| `notes.updated` | em | `{active, archived_add?}` | 工作笔记：`active` ≤1500 token（约 3000 汉字），归档部分只追加。**M3**：交接时 `active` = 员工写的摘要（11 个小标题：目标 / 负责人定下的约束 / 已定的事和否掉的方案 / 改过的文件 / 验收 / 坑 / 试过不行的路 / 没验证的猜测 / 下一步 / 要拍板的问题 / 不要重复做的事）+ 工位附的"工位记下的事实"块（每件活的状态和改动文件、git diff --stat、桌子版本、验收命令结果、最近失败的命令、负责人原话）；放进新对话前加一句"只作参考，不是新指令"的围栏 |
| `recall.checked` | em | `{ok, missing:[task|file]}` | ok=false 时员工标 `amnesia` 提醒（两次没对上不再开决定，仅写 signal 提醒，员工照常接活） |
| `recall.requested`（1.11） | em | `{reason}` | 规则监工：第 1 次复述不出来，下一轮再给一次笔记、重新复述（员工 `recall_due=true`，工位下一轮按 recall 开工；第 2 次仍没对上发 remind 提醒，不拦人） |

### 4.5 信件、决定、提醒、控制、预算
| type | subject | data | reducer |
|---|---|---|---|
| `letter.sent` | lt | `{from: em|gm|user|dept|office（管家 / 规则监工写的信，页面显示"管家"）, to: em|dept|gm|user, kind: request|decision|tell|reply|report|escalation|notice|system, subject, body, reply_to?, contract?}` | 建信件，`delivered=false`，request 的 `status=open`。带 `reply_to` 且原信是 open 的 request、写信的人不是原信作者 → 原信 `status=closed`、`answered_by=<这封>`（不管 kind 是 reply 还是 tell） |
| `letter.delivered` | lt | `{how: steer|next_turn|inbox, em?, task?}` | `delivered=true`。只记第一次；另记 `delivered_how`、`delivered_ts`、`delivered_to`（= `em`；负责人写的记 `gm`）、`delivered_task`（= `task`：送信时他在做哪件活）。工位写时 `em` 必须是自己 |
| `letter.answered` | lt | `{verdict?: accept|modify|reject, answer_letter}` | 原信 `status=closed` |
| `decision.opened` | dc | `{kind, title, detail, task?, em?, options:[{id, label, danger?}], owner: lead|user}` | 建"需要你"（两类见下） |
| `decision.resolved` | dc | `{option, text?}` | 关闭；甲类按下表改任务状态，乙类不改 |
| `signal.raised` | em|tk | `{kind: silent|loop|error_loop|context_fast|overlap|over_budget|quota|offline|outbox_rejected|version_skipped|conflict|config|cli_version_changed|executor_commit, level: remind|warn, text, task?}` | 加提醒（页面显示，`xay2 wait` 汇总）。（1.11）`error_loop`：那件活（data.task → subject 是任务 → 员工手上那件 / 最近那件）标成"管家不再自动重派"；`conflict`：合并冲突、自动再试一次。**M3（1.12）**：工位写的几种——`error_loop`（warn，死循环被叫停，带 `task`）；`quota`（agy 换号成功 remind / 换号失败 warn / 当天 40 次调用到上限 warn，活留在队列里不开工）；`config`（warn，dsh 补丁没生效或模型不合规，拒绝开工）；`executor_commit`（warn，执行者自己 git commit 了，原来误用 error_loop） |

**"需要你"分两类**：
- **甲类：保留下来的改动怎么办**——kind ∈ `mismatch / failed_with_changes / interrupted / conflict / desk_unmerged`。选项四个：`merge`（合并改动）、`rework`（让他接着改）、`discard`（丢掉改动，danger）、`manual`（我已经手动合好了）。决议后任务状态：merge → `reported`（记 desk_action = "merge"，之后工位真正合并，失败会再写 task.kept）；manual → `reported`（记 desk_action = "settle"，工位标记改动已落地并写 task.landed{manual: true}）；rework → `queued`；discard → `canceled`（记 desk_action = "discard"）。管家在 `task.kept` 之后紧接着写甲类 decision.opened。
- **乙类：其他要人拿主意的事**——kind ∈ `budget / repeated_failure / question`，（1.11）加 `failure`（没做成、自动重试用完或额度原因，交负责人）/ `error_loop`（一直报错、已停下）/ `silent`（很久没动静）；（1.25）`amnesia` 废弃不再开决定，两次没对上改写普通提醒且员工不 blocked。选项自由；打开和决议都**不改任务状态**，由后续事件处理。（1.11）规则监工开的乙类，你选了以后由它接着写后续事件（再试一次 → task.queued{retry:manual}；换个人做 → task.assigned + task.queued；加预算 → 额度 +50%；打断 / 叫停 → control.requested），事情过去了它自动 `decision.resolved`（by=rules）。decision 可带：`cause`（failure 的原因 quota|budget|failed）、`budget: {scope: task|employee, dim}`、`target: {em, turn}`（silent，不写 em，免得在干活的人显示成"等人答复"）。
- 快照 `decisions[]` 每条：`{id, kind, title, detail, task, em, options:[{id,label,danger}], owner, ts}`。

**送信规则**（1.9；唯一实现是纯函数 `xay.core.mail.recipient(现状, 信, starting?)`，按"送的那一刻"的现状算，送到后记在 `delivered_to` 上不再变）：
- 写给员工 → 他（已离职的送不到，页面写"送不到：… 已经离职"）。写给 gm → 总经理。写给 user → 不用送（页面上直接显示）。
- 写给部门 → 部门负责人：head 不是 rules（Claude 负责人）→ 现在就是本对话的负责人 gm；head=rules → 部门里跟这件事最相关的成员：① 正在做（或这一轮就要开工做）本部门任务的 ② 正在做别的活的 ③ 最近干过活的（看他的轮次，不算整理记忆）④ 名单里靠前的；写信人自己不算；部门里没有别人、部门已关 → gm。
- **谁来送**：员工的信由他的工位送——这一轮开工时放在输入最开头（"【你有 N 封信】……"，每次最多 10 封，附回信方法），这一轮真的开始后写 `letter.delivered{how: next_turn, em, task}`；干活中途来的信，能当场插话的（适配器 `capabilities.steer = live`，即 Codex）插话送达写 `{how: steer}`，插不进去的（dsh / agy、整理记忆那一轮）留到下一轮开工。上一轮开工时交代过、那一轮却失败且没改文件（比如满载换线路重做）的信，这一轮再交代一次（不再写事件）。员工没活也没在线 → 留在信箱，下次开工送。写给 gm 的信（含按规则落到 gm 的部门信）进 `xay2 wait`，打印出来时记 `{how: inbox}`（负责人写）。幂等键一律 `delivered:<lt>`。
| `budget.warned` / `budget.stopped` | tk|em（1.11：em = 员工今天的额度） | `{dim: tokens|minutes|turns, pct}` | 记录；stopped 时配合 control。（1.11）stopped 记进现状：这件活不再自动重排，乙类 budget 选了"再给一些预算"后解除；员工的在 resume 或加额度后解除 |
| `control.requested` | em|tk|无 | `{action: steer|interrupt|stop|stop_all|resume, text?, by_source: cli|page|rules}` | 只记录。**M3（1.12）**：`by_source: rules` 的 steer 在员工那边写成"【管家提醒】…"（当场插话时文字前面也加这几个字），其余的照旧是"【负责人插话】"；管家的提醒不算进交接事实块里的"负责人原话"。（1.11）`stop_all` → 现状 `halt = {seq, ts, by, source}`；不带 subject 的 `resume` → `halt = null`；by=gm 的 halt 在负责人又写 task.added/assigned/changed 时也解除，by=user（页面）的只有 resume 能解除 |
| `control.done` | em|tk | `{action, result: ok|noop|failed, detail?}` | 只记录 |

### 4.6 临时桥（M1，§9）（已删除，2026-10-31）
临时桥已删除，管家不再读取旧系统；账本里已有的 `bridge.*` 事件照旧重放显示，不会再有新的桥接事件。
`bridge.run` `{run_dir, agent, worker?, title, status, started, ended?, lead_session?, dept?, result_head?}`、`bridge.letter` `{旧 bus 行的子集}`。只用于 M1 页面展示旧系统的活，历史记录保留。

## 5. 状态（全部由 reducer 确定地算出）

**任务**：`planned → ready → queued → working ⇄ needs_you → reported → done`；任何活动态可到 `failed | canceled`。
- `needs_you` 只由 `task.kept` 进入；离开靠甲类 decision.resolved（见 §4.5）或之后的 task.queued / task.landed / task.canceled / task.failed。

**员工**（取第一个成立的）：`fired` → `offline`（最后一条在线事件是 offline）→ `compacting` → `working`（有未结束的 turn）→ `blocked`（有 owner=lead/user 的未决 decision 指向他，或他发的 request 未答复）→ `queued`（队列不空）→ `failed`（最后一轮 failed 且之后没有成功轮）→ `idle`。页面另显示（`display_state`，**快照生成时用当前时间算的展示字段，不进现状表**）：idle，或 offline 且原因是 `idle`/`stopped`（正常下班）→ 显示 `idle`，最后事件距今 >30 分钟则 `sleeping`；只有原因是 `crash` 的 offline 才显示 `offline`。

**信件**：`delivered` 真假；request 另有 `open/closed`。

## 6. 执行者怎么回报（不碰数据库）

执行者只能用两种方式，工位负责转成事件（`by=employee:<em>`，`dedup` = `outbox:<em>:<行号>` 或 `marker:<turn>:<序号>`）：

**执行者环境**：工位启动执行者时设置 `XAY_ROLE=executor`、`XAY2_EM=<em>`、`XAY2_TASK=<当前任务 tk>`、`XAY2_TASK_FILE=<桌子>/.xay/out/task`（工位每轮开工前写入当前任务编号；执行者进程会跨多件活常驻，所以命令**先读这个文件**，没有再用 `XAY2_TASK`）、`XAY2_OUTBOX=<桌子>/.xay/out/outbox.jsonl`、`XAY2_CONV=<conv>`。执行者命令**不打开数据库**（连 `open_db` 都不调用），只追加发件箱；没有 `XAY2_OUTBOX` 时用 `<当前目录所在 git 根>/.xay/out/outbox.jsonl`；没有 `XAY2_TASK` 就报错"不知道你在做哪件事"（但 `xay2 notes` 不要求有任务，只要有 `XAY2_EM` 即可，员工在两件活之间也能改笔记）。

**发件箱**：执行者在自己桌子里运行 `xay2 progress 2/4 "写测试"` 等命令，命令只向发件箱追加一行：
```json
{"v":1,"task":"tk_…","type":"progress","data":{"step":2,"of":4,"text":"写测试"}}
```
`type` 取 `progress | report | letter | proposal | notes`。工位检查：`task` 必须是这一轮的任务（`notes` 记录除外，只要员工编号是自己就收）；单行 ≤4 KB；每轮最多 200 行；字段按 §4 校验；不合格丢弃并记 `signal.raised{kind:"outbox_rejected"}`。

**笔记命令的三种写法（执行者通过发件箱精确改笔记）**：
- `xay2 notes "全文"`：整份替换（保留原行为，发件箱 `op: "set"`）。
- `xay2 notes --add "一条"`：在笔记末尾追加一行 `"- 一条"`（发件箱 `op: "add"`）。
- `xay2 notes --remove "关键词"`：删掉笔记里包含该关键词的行，不分大小写（发件箱 `op: "remove"`）。若一行都没删到，工位不改笔记，写一条 `signal.raised{kind: "outbox_rejected", level: "remind", text: "笔记里没有找到要删的内容"}`。
发件箱中的记录结构为 `{"v":1,"task":"…","type":"notes","em":"em_…","data":{"kind":"notes","op":"set"|"add"|"remove","text":"…","em":"em_…"}}`。工位收到后按 `op` 算出新笔记再写 `notes.updated`（没有 `op` 按 `set` 处理）。

**回复里的标记**（任何 AI、任何沙箱都能用）：
- `【进度 2/4】写测试` → task.progress（source=marker）
- `【结果】完成：一句话` / `【结果】没做完：…` / `【结果】卡住：…` → task.reported（claim = done/partial/blocked）
- `【写信 给 <部门名或员工名>】主题：正文` → letter.sent（kind=tell；发给部门则 request）
- `【回信 lt_…】正文` → letter.sent（带 `reply_to`，收信人 = 原信的作者；原信是 request 就算答复了）。用法只在工位交代信件时附上，不进任务卡末尾那几行。

任务卡末尾由工位自动附上这三种标记的用法（不超过 6 行）。

## 7. 命令行（`V2/bin/xay2`）

| 命令 | 写的事件 | 说明 |
|---|---|---|
| `xay2 begin --title "…" [--lead claude]` | conv.begun | 输出 conv 编号；自动把当前环境里的 `CLAUDE_CODE_SESSION_ID` / `CODEX_THREAD_ID` 登记成别名（已被别的对话占用就提示） |
| `xay2 dept open fe --name 前端部 --mission "…" [--owns 'web/**'] [--part <小任务>]` / `dept close fe` | dept.* | 可选 `--part` 挂到小任务名下 |
| `xay2 hire codex [--label …]` | employee.hired | 一般不用手动招，派活时自动 |
| `xay2 plan part "标题" [--key k] [--after k1,k2] [--check rules|ai:…|lead] [--check-text "…"] [--reason …]` | part.added | 新增小任务，写 check 验收方式 |
| `xay2 plan part-edit <小任务> [--title …] [--after …] [--check …] [--check-text …]` | part.changed | 修改小任务字段 |
| `xay2 plan part-drop <小任务>` | part.removed | 删除小任务（里面还有活时拒绝） |
| `xay2 plan add "做商品图轮播" [--key carousel] [--to Codex1|--new] [--agent codex|dsh|agy|auto] [--dept fe] [--part <小任务>] [--after k1,k2] [--writes 'web/**'] [--step "…" …] [--readonly] [--card 文件|-] [--reason "…"]` | task.added (+assigned) | `--agent` 不配 `--new`/`--to` = 任务记下 `agent`、不定人，由管家按选人规则挑（1.11）；`--to` 和 `--agent` 只能写一个；`--part` 指定所属小任务 |
| `xay2 plan apply plan.json` | 一批 task.added | 一次只加一次版本 |
| `xay2 plan move <任务> --to <员工|部门> --reason "…"` / `plan drop` / `plan edit` | task.* | move 到员工 = task.assigned（申请里 op=assign）；move 到部门 = task.changed{dept}（申请里 op=change） |
| `xay2 plan propose --reason "…" move <任务> --to <…>` | proposal.made | 执行者可用（写发件箱） |
| `xay2 answer <pp_|lt_|dc_> accept|reject|modify|<选项> ["说明"]` | proposal.decided / letter.answered / decision.resolved | |
| `xay2 ask <部门|gm> "…"` / `tell <员工> "…"` / `reply <lt_> "…"` | letter.sent | 执行者可用（写发件箱） |
| `xay2 progress 2/4 "…"` / `done "…"` / `notes "…"` | 见 §6 | 执行者用 |
| `xay2 accept <任务> [--note]` | task.accepted | 负责人验收 |
| `xay2 steer <员工> "…"` / `interrupt` / `stop <员工|任务>` / `stop --all` / `resume [员工]` | control.requested | `resume` 不带员工 = 松开总闸；总闸是用户在页面上拉的（halt.by=user）时命令行拒绝，只能用户在页面上点"恢复" |
| `xay2 compact <员工> [--method native|handoff|auto] [--reason "…"]` | context.compact_requested | 负责人请求员工整理记忆；员工可写显示名或编号，默认 `--method auto`，`--reason` 会写入 `note` |
| `xay2 notes-set <员工> <文件|->` | notes.updated | 读文件或标准输入，写 notes.updated（by=lead）；员工写显示名或编号都行；执行者不能用 |
| `xay2 wait [--since <seq>] [--timeout 240]` | letter.delivered（只给打印出来的、写给总经理的信） | 见下 |
| `xay2 brief` | — | ≤25 行纯文本现状 |
| `xay2 snapshot [--json]` / `events [--since] [--follow]` | — | 调试用 |
| `xay2 office start|stop|status` / `rebuild` / `gc --dry-run` | — | |

**`xay2 wait` 约定**（负责人的唯一等待方式）：阻塞直到本对话出现以下任一事件或超时——`decision.opened`（owner=lead）、`task.landed`、`task.kept`、`task.failed`、`proposal.made`（to=gm）、`letter.sent`（to=gm）、`signal.raised`（level=warn）。输出：
```
#<seq> 需要你：<标题>（dc_…，选项：merge / rework / discard）
#<seq> 已合并：<任务标题>（Codex 1，3 个文件）
#<seq> 提醒：<一句话>
游标 <最后 seq>
```
每条 ≤2 行，最多 15 条（同类 10 分钟内合并）。**游标 = 最后一条已打印（或已合并进打印行）事件的 seq**，没打印到的下次还能等到。只算这些：decision.opened 且 owner=lead、signal.raised 且 level=warn、proposal.made 且 to=gm、letter.sent 且 to=gm，加上 task.landed/kept/failed。`--timeout 0` = 只查一次立刻返回。退出码：0 有事件；3 超时无事件（打印"无新信号，继续等"）；2 出错。下次用 `--since <游标>`。

补充（1.9）：
- letter.sent 另算"按送信规则落到总经理的部门信"（§4.5 送信规则；总经理自己写的不算）；打印出来的写给总经理的信记 `letter.delivered{how: inbox}`（幂等，已送到的不再记）。
- "同类合并"：同一个对象的同一种提醒（signal kind + subject）合成一行"（10 分钟内同样的提醒共 N 条）"；task.landed 合成"已合并 N 件：A（Codex 1，3 个文件）、B……"；task.failed 合成"没做成 N 件：……"。要答复的（需要你、申请、来信）各带编号，不合并。
- task.kept 后面（同一批里）有这件活的 decision.opened：只打印"需要你"那一行，并写上保留原因："需要你：「X」的改动先保留了，要怎么办？——结果对不上（dc_…，选项：merge / rework / discard）"。
- 申请一行写清改什么："Codex 1 申请改计划：把「算运费」从 Codex 1 改给 Codex 2；理由：…（pp_…，选项：accept / reject）"；来信一行带正文开头和回信方法："Codex 1 来信：主题——正文开头（lt_…，等你回复：xay2 reply lt_… "…"）"。每段截短，一条不换行。

**`xay2 brief`**：≤25 行纯文本，固定五块（总闸拉下时第二行另写"总闸：已拉下（时间），恢复：……"）：① 对话（标题、编号、计划第几版）② 团队（每人在做什么、第几步）③ 需要你决定的（"需要你"、待批申请、等你回复的信，最多列 5 件，其余提示用 wait 看）④ 信（"还没送到的信 N 封：给 Codex 1 的 2 封、……"，都送到了也写一行）⑤ 任务清单（大白话状态："在做，第 2/4 步：写测试"、"做完了，等你验收（xay2 accept k1）"）；列不下时先列等你决定 / 在做 / 待验收 / 没做成的，其余按状态计数。

## 8. 页面接口（管家，`127.0.0.1:7879`）

- 只绑定 127.0.0.1；所有请求检查 `Host ∈ {127.0.0.1:7879, localhost:7879}`；POST 还要 `X-Xay-Action` 头 + 同源 `Origin`；不回 CORS。
- `GET /api/v2/snapshot?ws=&conv=` → 快照（不传 conv 就是该 ws 下**最近一条事件**所属的对话；不传 ws 就是最近有事件的 ws）。`recent` 是本对话最近 20 条有大白话的事件：
```json
{"v":1, "seq":1234, "now":1793310000,
 "ws":{"id":"ws_…","name":"xay-multiagent","path":"…"},
 "conversation":{"id":"cv_…","title":"…","lead_kind":"claude","plan_version":3} ,
 "gm":{"agent":"claude","state":"working|idle"},
 "people":{"em_…":{"key":"em_…","label":"Codex 1","agent":"codex","dept":"fe","state":"working","display_state":"working",
     "now":"做商品图轮播","thought":"最近一句思考或空","queue":["tk_…"],
     "context":{"pct":62,"segment":2,"compactions":1,"calibrating":false,"estimated":false},
     "progress":{"step":2,"of":4,"text":"写测试"},"budget":{"pct":40},"amnesia":false,"online":true}},
 "depts":[{"id":"fe","name":"前端部","mission":"…","head":"rules","members":["em_…"],"counts":{"members":2,"working":1,"open_requests":0}}],
 "tasks":[{"id":"tk_…","key":"carousel","title":"…","owner":"em_…","dept":"fe","status":"working","deps":[],"writes":[],"progress":{…},"result":"…","assigned_reason":"…"}],
 "plan":{"version":3,"history":[{"version":3,"by":"gm","ts":0,"reason":"…","changes":[…]}]},
 "letters":[{"id":"lt_…","ts":0,"from":"em_…","to":"fe","kind":"request","subject":"…","body":"…","reply_to":null,"status":"open","verdict":null,"delivered":true}],
 "decisions":[{"id":"dc_…","kind":"mismatch","title":"…","detail":"…","task":"tk_…","em":"em_…","options":[{"id":"merge","label":"合并改动"},{"id":"rework","label":"让他接着改"},{"id":"discard","label":"丢掉改动","danger":true}],"owner":"lead","ts":0}],
 "proposals":[…open 的…], "signals":[…最近 20 条…], "former":[{"key":"em_…","label":"Codex 2","agent":"codex"}],   // 按离职先后排序
 "quota":[…沿用旧版 xay quota 的结构…],
 "recent":[{"seq":1230,"ts":0,"type":"letter.sent","text":"Codex 1 给前端部写信：…","from":"em_…","to":"fe"}]}
```
- 页面拿不到快照时显示"读不到数据：<原因>"，**禁止回落到示例数据**。示例模式只在 `?demo=1` 时，由管家把 `V2/fixtures/demo.events.jsonl` 回放进一个**内存里的临时账本**后给出同样结构的快照。
- `GET /api/v2/stream?ws=&conv=&since=<seq>`（SSE，支持 `Last-Event-ID`）：有大白话的事件发 `event: ledger`，`id: <seq>`，`data` = `recent` 同款的一条；没有大白话的事件（心跳、桥接之外的技术事件等）合并成 `event: changed`，`data` = `{seq}`（页面只重拉快照、不放动画）；每 15 秒 `event: heartbeat`。页面收到后 300 ms 防抖重拉快照，并用这条事件放动画（谁给谁写信、谁做完了）。
- `GET /api/v2/live?em=<em>&since=<n>` 或 `?ems=<em_list>[@段.行]`（SSE）：直播通道，单员工模式 `data` = `{n, kind: thinking|text|tool|tool_output|status, text, ts, seg}` 带 `id`；多员工模式 `data` 额外带 `em`，不发 `id` 行。来源 `~/.xay/v2/live/<em>/<segment>.jsonl`（工位写，已脱敏，单行 ≤2 KB）。
- `GET /api/v2/employee?em=&conv=`：员工详情栏数据 `{person, timeline:[{task, ask, steps, key_steps, result:{claim,summary,detail,short}, files, attempts, tries}], total, today, count, focus, memory}`，全部大白话（规则在 `office/plain.py`）；旧系统的活只读旧 run 目录里固定的几个文件。
  - `memory`：`{"notes": 完整笔记文本, "notes_ts": 最近一次 notes.updated 的时间, "notes_by": 最近一次谁改的（"员工" / "工位" / "用户" / "负责人"）, "handoff": 最近一次交接摘要（这名员工最近一条 context.compacted 的 method 是 handoff 或 machine 时，它之前紧挨着的那条 notes.updated 的 active 全文；没有就 null）, "compactions": [{"n": 第几次, "ts", "method", "before_pct", "after_pct"}]（这名员工所有 context.compacted，旧的在前）}`。
  - `timeline` 每一行是**一件事**：按时间看，同一员工、同一对话里 `short_title` 相同、而且前一次是 failed / canceled 的，后一次算"再试一次"，合进同一行（前一次已经做成的，后面同名的是新活）。这一行的 `task`、结果、步骤都取最后一次；`attempts:[{id, status, started, ended, ran, note}]` 按先后列出每一次（`note` 是一句话经过，如"没做成：AI 那边太忙（满载了）。"、"派了又撤回了：<理由>。"）；`tries` 只数真的动手做过（`ran`）的次数。页面工位小清单（`web/lib/utils.js` 的 `mergeAttempts`）用同一条规则。
  - `task.short_title`：只留第一句正事，去掉"汇报时……/不要……"这类写给 AI 的要求、括号里的补充、【进度】标记、反引号；`task.first_started`：合并过的行第一次开始的时间。
  - `key_steps`：最多 3 条大白话（优先用员工自己报的进度）；`result.short`：结果一句话；`steps` 和 `result.detail`（原话）只放在页面折叠的"看他的原话和技术细节"里。
  - `total`：合并后的行数（`timeline` 最多 25 行）；`today:{done, failed, needs_you, working, queued}`：按合并后的行数，做完 / 没做成只数本机时间今天结束的，等你决定、在做、排队不管哪天。
  - `person.now` 是短标题；`person.progress.text` 去掉【进度】和开头重复的"第 x 步："，只留一句；`person.notes` 去掉任务编号、引号里的任务名换成短标题。
- 快照另有 `activity`（每个部门最近动静时间，页面用来把超过 24 小时没动静的部门收进"以往的部门"）；`/api/v2/live` 加 `plain=1` 时每条附一句大白话 `plain`。
- 1.9：`letters[]` 每条另有 `delivery: {state: delivered|waiting|stuck|none, text, to}`（`text` 是页面直接显示的大白话："已当场告诉 Codex 1" / "已交给 Codex 1（开工时一起交代）" / "总经理已看到" / "还没送到：等 Codex 1 下次开工" / "还没送到：等总经理来看" / "送不到：Codex 2 已经离职"；写给 user 的是 none、不显示；旧系统镜像的信没有这个字段），以及 reducer 记的 `delivered_how/delivered_to/delivered_ts/delivered_task/answered_by`。`plan.history[]` 每版另有 `summary`（这一版改了什么的一句话："把「算运费」改给页面部"）和 `change_texts`（与 `changes` 一一对应的大白话），通过的申请那一版还有 `proposal`、`approved_by`；页面"计划版本"栏写成"第 3 版：<summary>，原因：<reason>"。文案模板在 `core/humanize.py`（`plan_change_text`）和 `core/mail.py`（`delivery`）。
- `POST /api/v2/control`（头 `X-Xay-Action: control`）`{em|task, action, text?}` → 写 control.requested（by=user）。（1.11）`stop_all` 和 `resume` 可以不指定员工 = 拉下 / 松开总闸（页面顶栏的红按钮）。
- （1.11）`GET /api/v2/halt` → `{halt: null|{seq, ts, by, source}, working, online}`：总闸状态（页面顶栏用）。快照顶层也有同样的 `halt`；`people.budget` = `{pct, dim?, scope?: task|employee}`（手上这件活和他今天的额度取用得多的那个）。
- `POST /api/v2/letter`（头 `X-Xay-Action: letter`）`{to, subject, body}` → letter.sent（from=user）。
- `POST /api/v2/decision`（头 `X-Xay-Action: decide`）`{id, option, text?}` → decision.resolved（by=user）。
- `POST /api/v2/notes`（头 `X-Xay-Action: notes`）`{em, text}` → 写 notes.updated{active: text}（by=user）。超过 6000 字返回 400。同源且只接受本机。

**大白话事件文案**（`recent[].text`，模板集中在 `core/humanize.py`，页面不自己拼）：例如 `task.added`→"总经理把「X」排进了计划（第 3 版）"、`task.assigned`→"「X」交给 Codex 1：<理由>"、`task.progress`→"Codex 1：第 2 步 / 共 4 步，写测试"、`context.compacted`→"Codex 1 整理了一下记忆（62% → 18%）"、`task.kept`→"Gemini 1 说做完了，但系统记成失败——改动先保留，等你决定"。全部模板由负责人审定。

## 9. 临时只读桥（M1）（已删除，2026-10-31）

临时桥已删除，管家不再读取旧系统；账本里已有的 `bridge.*` 事件照旧重放显示，不会再有新的桥接事件。

管家每 2 秒增量读旧系统的 `~/.xay/registry.jsonl`（只读；记住读到的字节位置）与对应 run 目录的 `start.json/meta.json/result.md` 第一段，以及 `~/.xay/org/*/bus.jsonl`，写 `bridge.*` 事件（`dedup = bridge:<run_dir>:<status>` / `bridge:bus:<msg id>`）。
- 归属：旧 run 的 `lead_session`（Claude uuid）→ 找别名 `claude:<uuid>` 的 conv，没有就自动 `conv.begun{title: 旧对话 + 标题, aliases:[claude:<uuid>]}`（by=bridge 允许写这一种 conv.begun）。
- 旧的具名 worker → 自动 `employee.hired{note:"旧系统"}`，label 照常发号；没有 worker 名的 run → 记在每个（ws，AI）一个的真实员工"<AI 名> 临时工"名下（幂等招聘），这样旧活有负责人、页面上有人坐在工位上。上一版桥写入、还没有员工的旧活，用带 `adopt: true` 的 bridge.run 补认（不播报）。
- 对话标题：别名 `claude:<uuid>` → 只读 Claude 桌面版会话索引里那条记录的 `title` 字段（其他字段一律不读）；`ls:` → 旧 `sessions/<ls>.json` 的标题；都没有就"旧对话 <第一件活的标题>"。用 `conv.titled`（by=bridge）写入，负责人改过名的不动。
- 只用于展示；桥写入的东西**不触发**排程、反应表或任何控制动作。桥已删除，`bridge.*` 的处理分支只为重放旧账保留。
- **页面上怎么显示**：bridge.run 进 `tasks`（`legacy: true`；状态映射 running/active→working、queued→queued、done→done、failed/error/timeout/stale→failed、stopped→canceled），正在跑时对应员工 `state=working`、`now`=标题；bridge.letter 进 `letters`（`legacy: true`）。大白话："（旧系统）{员工} 开始做「{标题}」" / "做完了「…」" / "没做成「…」"；信件同 letter.sent 加"（旧系统）"前缀。

## 10. 验收用样本（`V2/fixtures/golden/`）

每个样本两份文件：`NN-<名字>.events.jsonl`（`by/type/subject/data/ts`，seq 由测试按行号分配）和 `NN-<名字>.expect.json`（`[{"path":"people.em_a.state","equals":"working"}, …]`，path 指快照里的位置，列表用下标）。测试：临时目录建账本 → 逐行 append（某行带 `$error` 期望时，断言这一行抛出该错误、不写入，然后**继续**下一行）→ 生成快照（`now` 取最后一行的 ts）→ 检查每条 expect（path 指向的键不存在时取值为 `null`）；再 `rebuild` 一次结果完全一样。必须覆盖：计划加/改/删与版本历史、申请通过与拒绝、员工发号不复用、证据表每一行、需要你的打开与解决、记忆更新与压缩、离线、信件送达与答复、越权写入被拒、dedup 幂等、未知事件类型被跳过。
