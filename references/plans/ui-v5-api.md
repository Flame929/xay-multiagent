# 看板 v5 接口约定（负责人定稿，前后端唯一依据）

需求见 `ui-v5-brief.md`。

## 1. 负责人对话的识别与标题（数据层）

- **识别**：派活时没有传 `--session` 的，按以下顺序取负责人对话 ID：
  1. `XAY_SESSION`
  2. `CLAUDE_CODE_SESSION_ID`，记为 `lead_kind=claude`
  3. `CODEX_THREAD_ID` 或 `CODEX_SESSION_ID`，记为 `lead_kind=codex`
  4. 其他 CLI 的同类变量，没有就是空

  识别结果写进 start.json、meta.json 和 registry 的 run/dag 行：`lead_session`（上面拿到的原始 ID）、`lead_kind`、`lead_host`（`CLAUDE_CODE_HOST_SESSION_ID`，可为空）。
- **Claude 对话的标题与工作区**：读 `~/Library/Application Support/Claude/claude-code-sessions/*/*/local_*.json`。用 `cliSessionId == lead_session` 匹配，只取 `title`、`cwd`、`originCwd`、`createdAt`、`lastActivityAt`、`isArchived`，其他字段一律不读。结果按文件的 mtime 缓存。
- **历史回填**：一次性脚本 `scripts/xay-backfill-leads`，可重复运行。
  - 对 registry 里还没有 `lead_session` 的 run 和 dag，到 `~/.claude/projects/*/*.jsonl`（文件名就是会话 ID）里搜索它的 run_id 或 dag stamp 字符串，第一个命中的会话就是负责人对话。
  - 结果写进 `~/.xay/lead_map.json`，格式为 `{run_dir|dag_dir: lead_session}`。
  - 只做字符串匹配，不保存、不输出对话内容。
- 看板服务读取 `lead_session` 时，先看 registry、start.json、meta.json，取不到再查 lead_map.json。仍然没有的（旧数据），**不分目录**，全部合并到"临时文件夹"分组里的一个对话"以前的任务"（id 固定为 `legacy:temp`）。
- **对话所属的工作区**（与 Claude 桌面版侧栏一致）：
  - Claude 对话取 `originCwd`，没有就用 `cwd`；不是 Claude 的对话，取它第一次派活的 `origin`。
  - 路径在 `~/Library/Application Support/Claude/scratch-workspaces/` 下（桌面版"未选文件夹"的对话）→ `{path: "this-computer", name: "这台电脑"}`，这些对话合并到同一组。
  - 路径在系统临时目录下（`/tmp`、`/private/tmp`、`/var/folders`）、为空或确定不了 → `{path: "temp", name: "临时文件夹"}`。
  - 其余：在 git 仓库里取仓库根目录，否则取目录本身；`path` 为绝对路径，显示名为目录名。
- **线程标题**：有 `-t` 标题就用它；没有（xay-run 用任务首行兜底的也算没有）时，跳过"先读 …。"这类前缀句和"背景："等标签，取第一句有意义的话，最多 20 字；仍取不到就用"<AI 名> 的活"。

## 2. 对象

```
Conversation {
  id: lead_session 或 "legacy:<workspace_path>",
  title,                      // Claude 标题 > session begin 标题 > 第一次派活的任务名 > "以前的任务"
  lead_kind,                  // claude | codex | dsh | agy | unknown
  workspace: {path, name},    // path 是绝对路径，或固定 id "this-computer" / "temp"（见 §1）
  legacy,                     // true 表示这是"以前的任务"
  status,                     // running | needs_you | idle | done
  updated,                    // epoch 秒
  counts: {running, done, failed},
  agents: ["codex", "dsh", "agy"]          // 出现过的 AI，按这个顺序
}

Thread {                      // 一个 AI 执行的一件事：一个 DAG 子任务或一次单独派活，包含它的全部返工轮次
  id,                         // 第一轮的 run_dir
  agent, title, status,       // status 沿用 arch-v4 §3.3 的任务状态
  started, updated, phase_title,           // phase_title 是所属 DAG 的标题（单独派活为空）
  capabilities: {steer: "live"|"none", interrupt: "soft"|"kill"},
  active_run,                 // 进行中那一轮的 run_dir，没有则为 null
  messages: [Message]
}

Message =
  {kind: "lead", id, ts, text, summary, source}
    // source: task（派活任务卡）| steer（插话）| resume（续接/返工指示）| interrupt_text
    // text 是任务卡里"# 任务"那一段（不含系统附加的准则），summary 是第一行或 title
| {kind: "ai", id, ts, run, turn, status, streaming,
   steps: [{kind, text, raw, state}],     // humanize 后的一行化动作，按顺序
   thinking: "折叠用的思考摘要或空",
   text: "当前已产生的回复文字（流式时不断变长）",
   final: "最终回复（result.md）或 null",
   files: ["改动文件"],
   usage: {tokens}|null}
```

## 3. 接口

鉴权沿用 v4（Host、Origin、Sec-Fetch-Site、X-Xay-Action）。

- `GET /api/state`：`{now, seq, workspaces: [{path, name, legacy, conversations: [Conversation]}], decisions, agents, host}`
  - 侧栏、首页都用这一份数据。
  - workspaces 按最近活动排序，`legacy: true` 的"临时文件夹"固定排在最后，前端默认折叠；组内"以前的任务"排在最后。
  - `decisions` 和 `agents` 沿用 v4，每条 decision 增加 `conversation` 字段。
- `GET /api/conversation?id=<id>`：`{conversation: Conversation, threads: [Thread]}`
  - threads 按 agent 分组，组内按 started 排序。
- `GET /api/events?since=<seq>`（SSE，支持 `Last-Event-ID`），事件：
  - `state.changed`：数据为空，前端收到后重新拉一次 /api/state；有防抖，最多每秒一次。
  - `message.append` `{conversation, thread, message}`：新消息，可能是负责人消息，也可能是 AI 消息的开头。
  - `message.delta` `{conversation, thread, message_id, text_delta?, step?}`：流式增量，文字片段或新增的一行动作。
  - `message.update` `{conversation, thread, message_id, patch}`：状态、最终回复、文件等字段变化。
  - `thread.update` `{conversation, thread, patch}`
- 控制接口 `POST /api/control` 沿用 v4，请求体 `{id: run_dir, op, text?}`。

## 4. 流式数据来源

- `xay_agent.py` 把文字增量追加到 `<run_dir>/stream.jsonl`，每行 `{ts, turn, text_delta}`。新一轮开始时清空这个文件，轮次结束后保留最后一轮，最多 512KB。
- 看板服务用增量 tail（按 offset 读）读取 `events.norm.jsonl` 和 `stream.jsonl`，转换成上面的 SSE 事件。
- **旧版运行**：没有 `events.norm.jsonl` 的（第三版的 events.raw），用现有的 EventParser 解析成 steps 和 text，标记为不流式。
