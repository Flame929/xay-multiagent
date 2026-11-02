# agy（Antigravity CLI 1.4.9）控制接口调研

> 2026-10-29。依据：本机 `agy --help`、各子命令 help、`agy changelog`（1.0.0–1.4.8）、二进制字符串、1 次真实运行（两轮）。
> 标记：【实测】本机跑过；【文档】help/changelog 有写；【推断】从二进制字符串或旁证推出，未验证。

## 1. 控制接口清单

| 接口 | 结论 | 来源 |
|---|---|---|
| ACP（Agent Client Protocol） | **没有**。help、changelog、二进制里都找不到 ACP 入口 | 【文档】 |
| agy 作为 MCP server | **没有**。`agy mcp` 只管理 agy 自己要连的 MCP 客户端配置 | 【文档】 |
| `--input-format stream-json`（配合 `--output-format stream-json -p=`） | **一个进程承载一个会话、多轮对话**：stdin 每行一条 NDJSON，每条跑一轮，stdin 关闭后进程退出 | 【实测】 |
| `remote-control start/status/stop`、`--remote-control` | 经 Google 中继（`jetski-webchannel.googleapis.com`）把会话交给 antigravity.google.com 网页或手机端遥控。协议私有、没有文档，**不适合编排** | 【实测】【文档】 |
| 隐藏子命令 `remote-control serve` | 内嵌 language server 会在 localhost 随机端口开 gRPC(HTTPS)+HTTP 服务，同时连上中继。属内部接口，**不要接** | 【实测】见第 6 节事故 |
| 中断 | 没有协议层 cancel。stdin 里不认识的 event 会被忽略并警告。只能给进程发 SIGINT/SIGTERM；`--print-timeout` 到时返回已有的部分输出并以 0 退出 | 【文档】 |
| 续接 | `--conversation <ID>`、`-c`；同一会话被两个进程同时打开时会提示改用 `/fork` | 【文档】 |
| 暂停/恢复 | 没有。只能“结束进程，再用 `--conversation` 续接” | 【文档】 |
| 只读斜杠命令 | `-p "/usage"`、`/help`、`/model` 等直接返回结构化结果，不消耗配额 | 【实测】 |
| 多 Agent | 模型可用的工具里有 `invoke_subagent / define_subagent / manage_subagents / send_message / manage_inbox`；可用 `--agent <名字>` 选自定义 agent；stream-json 有 `subagent_info`。`/boost` 只能当 prompt 前缀发；`/teamwork-preview` 需要交互访谈，没有对外 API | 【文档】【实测 init.tools】 |
| 二进制里的 `agent.session.input.cancel/message`、`response.steer` | 内部远程 agent API 的类型，CLI 没有暴露 | 【推断】 |

### stream-json 输入格式【实测可用】
```
{"event":"user","user":{"role":"user","content":"..."},"message":{"role":"user","content":"..."}}
```
- 每行必须有 `event` 字段，否则报 `missing the "event" field`。
- content 可以是字符串，也可以是 `[{"type":"text","text":"..."}]`。内容块只支持 `text`。
- 实测时同时带了 `user` 和 `message` 两个键，所以还不确定哪个是必需的。下次第一次接入时去掉其中一个试一下。
- 输出：先发 `init`（含 conversation_id），之后每轮发若干 `step_update`，**每轮结束都会发一个 `result`**（含 `num_turns`，`usage` 是累计值）。

## 2. 推荐接入方式
1. **短任务**：保持现状，一个进程跑一次 `-p ... --output-format stream-json`。
2. **需要返工或纠偏的任务**：改成长驻进程 `agy --input-format stream-json --output-format stream-json --model gemini-3.8-flash-* -p=`，由 xay 一直握着 stdin。每轮以收到 `result` 为结束；发下一条消息就是“返工/追加指令”。空闲超过 N 分钟就关闭 stdin，让进程退出。之后需要时用 `--conversation <ID>` 再拉起。
3. 注意：`-p` 后面要接值。写成 `-p --input-format` 会把 flag 当成 prompt 而报错（实测）。要用 `-p=` 或者把 `-p=` 放在最后。
4. 别接 remote-control / serve / 内部 gRPC：协议私有，会连 Google 中继，还会写入设备注册信息。

## 3. Steering 可行性
- **轮间纠偏：可行**【实测】。当前轮的 `result` 出来后发下一条消息，上下文保留（实测第二轮的 input_tokens 是第一轮的约 2 倍）。
- **轮中插话：不可行或没有保证**。print 模式按“一条消息一轮”顺序处理【文档】。交互模式的 `queuedMessages: send-immediately` 能在轮中插话，但没有证据表明它对 stream-json 生效【推断】。不改配置的前提下没有测。
- **轮中中断**：只能杀进程，然后 `--conversation` 续接。中断的工具调用会以 error 形式回给 agent（1.4.8 修复）【文档】。
- 结论：xay 能做到“每轮结束后纠偏 + 硬中断后续接”，做不到软中断或轮中插话。

## 4. 并行策略
- 端口：每个 agy 进程自带一个 language server，用随机端口，**互不冲突**【实测日志】。
- 会话库：每个会话是一个 `conversations/<id>.db`，有文件锁（1.2.9 修复过并发进程把锁弄丢的问题）。**同一个 conversation ID 不能被两个进程同时续接**（会交错写入），要并行就各开新会话或用 `/fork`【文档】。
- 配置：`config.json` 已经改成原子写入，并发安全【文档】。同一工作区里并行写文件没有任何隔离，写任务要各自用独立的 worktree。
- 配额：所有并发进程共用同一账号的桶（Gemini 有 5 小时桶和每周桶；实测本周剩 80%、5 小时剩 96%）。遇到限流会按服务端要求的延迟重试，超过 30 秒或碰到日/账单上限就直接失败【文档】。
- **推荐并发数 2，最多 3**。理由：每个进程约 0.7GB 内存（见第 5 节），冷启动约 8 秒，配额共享，还有第 6 节的账号风险。`/boost` 本身就会再开 worker，跑 `/boost` 时同一时间只跑 1 个。

## 5. 资源数据【实测，一次 2 轮的 flash-low、plan 模式运行】
| 项目 | 数值 |
|---|---|
| agy 主进程 RSS | 启动约 175MB，峰值约 250MB |
| 子进程 | 8 个 node MCP server（读 `~/.gemini/config/mcp_config.json` 后全部拉起），合计约 450–515MB |
| 进程树峰值 | **约 755MB** |
| 冷启动 | language server 约 1 秒，静默认证约 4 秒，开始发第一条消息约 8.5 秒 |
| 两轮总耗时 | 31 秒（第一轮约 9.6 秒，第二轮约 7.5 秒），两个回复都只有一个词 |
| token | 第一轮 input 约 12.3k（系统提示 + 工具声明）；两轮累计约 25.1k |
| 用户自己的交互式 agy（已运行 19 小时） | 主进程约 134MB |
- 内存大头是 MCP 子进程。如果 xay 能用某种方式给子任务关掉 MCP（目前没找到对应 flag，而且不允许改配置），单进程能降到约 250MB。
- 存储：`~/.gemini/antigravity-cli/` 共 60MB（conversations 33MB/28 个 db，brain 13MB，bin 12MB，log 1.1MB/34 个文件，cli.log 是指向最新日志的软链接）。一次极小会话写了约 450KB db 和 48KB brain。`~/.gemini/config/` 148KB。App 的数据在 `~/.gemini/antigravity/`，共 3.9GB（conversations 2.3GB，brain 1.5GB），**与 CLI 分开存放**。`--log-file` 可以把单次运行的日志改写到别处。

## 6. 风险与对编排设计的影响
- **长上下文失忆**：长驻多轮会话的成本会越来越高（每轮都要重发全部上下文，cache_read 为 0），compaction 之后还容易忘事。约定：**一个会话最多 3 轮**，超过就开新会话并把背景重新写进任务卡；进度落到 `.xay/notes/`。
- **denied_actions**：headless 下需要授权的操作一律被拒绝，agent 会遵守拒绝、不再绕路（1.4.x）。子 agent 的提问也会被跳过。xay 需要解析 `result.denied_actions`；遇到需要授权的情况，要么在 settings.json 里预先放行，要么用 `--dangerously-skip-permissions` 并配合独立 worktree。权限被拒不会导致非零退出码；模型或agent 出错时退出码是 3，并输出 `AGY_ERROR`。
- **个人账号被第三方调用**：官方论坛有人问过“账号额度能否被外部编排调用 headless 模式”，目前没看到官方答复。长驻进程加上并发会让调用模式更像机器人。建议并发数低、不轮换账号、不碰内部 gRPC 和中继接口，也不要启用 remote-control。
- **本次调研造成的副作用（需要用户知晓）**：执行 `agy remote-control serve --help` 时，隐藏的 `serve` 子命令**忽略了 --help 直接运行**了约 2 分钟（已用 SIGTERM 停掉，MCP 子进程已清理，`remote-control status` 显示没在运行）。期间它连上了 Google 遥控中继，并在 `~/.gemini/config/config.json` 写入了 `cliRemoteControlHostname`（`mac-mini-stellar-meteor`）和一个安装 UUID，还生成了 `remote-control-serve-telemetry.json`。按“不改配置”的约束，我没有回滚这些写入。

参考：[论坛：外部编排 headless 是否被允许](https://discuss.ai.google.dev/t/is-external-orchestration-of-antigravity-cli-headless-mode-supported-with-account-based-usage/183051) · [1.1.8 release（stream-json）](https://newreleases.io/project/github/google-antigravity/antigravity-cli/release/1.1.8) · [CLI cheat sheet](https://computingforgeeks.com/antigravity-cli-cheat-sheet/)
