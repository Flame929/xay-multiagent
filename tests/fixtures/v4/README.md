# v4 事件归一固定样本

`arch-v4.md` §12.2 WP4 的固定输入。四个执行通道 + xay-agent 侧车的原始行都是 JSONL，一行一条。

| 文件 | 来源 | 是否构造 | 说明 |
|---|---|---|---|
| `codex.jsonl` | `codex exec --json` | 否 | 原样复制 `tests/fixtures/codex.jsonl`（真实录制） |
| `dsh.jsonl` | dsh headless | 否 | 原样复制 `tests/fixtures/dsh.jsonl`（真实录制） |
| `agy.jsonl` | agy stream-json | 否 | 原样复制 `tests/fixtures/agy.jsonl`（真实录制） |
| `appserver.jsonl` | codex app-server | **是** | 按 `references/plans/research/codex.md` §2/§3 的 JSON-RPC 通知、服务端请求与响应手工构造；含 delta、审批/拒绝、用户提问、三种 turn 状态 |
| `control.jsonl` | xay-agent 侧车 | **是** | 控制 socket 的 requested/acked/failed、信号、决定、笔记；阶段 1 由 xay-agent 产生，本文件用于固定格式 |

机器可读的标注见 `manifest.json`（`constructed` 字段）。

## 构造样本的约束

- `appserver.jsonl` 不是真实录制，字段形状来自协议文档与实测记录；真实环境字段若有出入，改这一份样本，不要改契约。
- delta 行（`item/agentMessage/delta`、`item/commandExecution/outputDelta`）故意留在样本里，
  用来断言归一器不会把单条 delta 写进 `events.norm.jsonl`；最终回复与命令输出的聚合结果仍会落盘。
- `control.jsonl` 的 `type` 已经是 xay-agent 侧车的事件名；归一器只补信封与字段裁剪，不重新解释控制协议。
