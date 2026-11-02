# Claude Code 作为负责人接入说明

本文档适用于 **Claude Code**（终端 CLI 或桌面版会话）担任 xay 多 AI 协作工作流负责人的场景。

---

## 1. 角色定位与核心原则

- **职责定位**：Claude 拥有强大的架构抽象、任务拆解与逻辑推理能力，在 xay 工作流中承担**负责人（Lead）**职责，负责需求分析、依赖编排、决策裁决与最终验收。
- **不可作为执行者**：本机没有 `claude` 命令行，Claude **永远只担任负责人，不能作为被派活的执行者**。
- **严禁使用 Claude 子 Agent 做并行**：禁止使用 Claude 自身的子 Agent 工具（如内置 Agent / Task 工具）执行并行开发，这会直接按 Claude 的高昂订阅计费并加速耗尽额度；所有并行体力活一律通过 `xay-dag` 或 `xay-run` 外包给 codex、dsh、agy。
- **Token 节省**：负责人不读大文件、不读执行者原始控制流日志；依赖 `summary.md`、汇报摘要与 `xay wait` 的事件通知。

---

## 2. 身份识别与会话生命周期

Claude Code 运行时环境会自动注入 `CLAUDE_CODE_SESSION_ID`（桌面版还会注入 `CLAUDE_CODE_HOST_SESSION_ID`）。

### 2.1 开启会话（Session Begin）

在派发任何任务前，首先启动负责人会话：

```bash
X=scripts  # 或 ~/.agents/skills/xay-multiagent-workflow/scripts

# 启动负责人会话（自动识别 Claude 身份并绑定 lead_key=claude:<SESSION_ID>）
LS_ID=$($X/xay session begin --lead claude --title "本次任务大白话标题")
export XAY_LEAD_SESSION="$LS_ID"
```

### 2.2 查看并发名额（Doctor）

在拆解与派发任务前，先通过 `xay doctor` 查询当前环境各 AI 并发名额与默认等待超时：

```bash
$X/xay doctor
```
- 输出 JSON 示例：`{"slots": {"codex": 3, "dsh": 4, "agy": 2}, "wait_default_s": 240}`。
- 名额按 `xay doctor` 的动态输出为准，不写死固定数值；如果名额已满，新派发的任务会自动排队。

### 2.3 结束会话（Session End）

所有任务执行完毕并通过最终验收后，显式关闭会话：

```bash
$X/xay session end --session "$LS_ID"
```

---

## 3. 任务编排与派发

### 3.1 批量并行计划（xay-dag）

将拆解好的 DAG 依赖计划写入 `plan.json`，并执行调度：

```bash
# 校验计划与依赖层级
$X/xay-dag plan.json --dry-run

# 执行计划并启动看板服务
$X/xay-dag plan.json --watch
```

### 3.2 单任务派发与返工（xay-run）

```bash
# 派发单个实现任务
$X/xay-run codex -n api -t "写接口" -e high "任务卡内容"

# 派发测试任务
$X/xay-run dsh -n test -t "写测试" -e low "任务卡内容"

# 派发只读审查任务
$X/xay-run agy -n review --readonly "审查任务卡"

# 续接原有会话返工
$X/xay-run codex -r <会话ID> "返工修改意见"
```

---

## 4. 事件等待循环（xay wait）

派活后，Claude Code 切勿写 bash 循环不断 sleep 轮询日志，必须统一调用 `xay wait` 挂起并等待事件：

```bash
$X/xay wait --session "$LS_ID" --timeout 240
```

> [!NOTE]
> Claude Code 的 Bash 工具单次调用硬性超时上限通常为 600 秒。`xay wait` 默认超时设为 240 秒（≤240 s），完全在安全阈值之内，不会引发工具调用超时错误。

### 4.1 输出信号处理

- **`需要你：<标题/说明>`（Decision）**：
  阻塞性决策已产生（例如代码搬回冲突、最终失败裁决等），需要 Claude Code 裁决。使用 `xay decide` 命令处理：
  ```bash
  $X/xay decide <run_id> <option> --id <dec_id> --version <version> [--text "说明"]
  ```
- **`提醒：<标题/说明>`（Signal）**：
  非阻塞性通知（如静默超时预警、循环检测、越界修改等）。记录并在必要时进行干预。
- **`阶段结果：<status>（<title/run>）`**：
  任务运行完成或终态通知（如 `done`, `failed`, `needs_you`, `orphaned` 等）。
- **`无新信号，继续等`**：
  本次 wait 超时且暂无新事件。若后台仍有任务在运行，继续执行下一轮 `xay wait`。

---

## 5. 运行时控制（xay ctl）

在任务运行过程中，如发现执行者方向偏离或需要紧急干预，使用 `xay ctl` 控制：

```bash
# 1. 轮中插话（仅支持 live steer 的执行者如 codex app-server；不打断当前轮，下一次采样可见）
$X/xay ctl <run_id> steer --text "修正提示"

# 2. 打断重跑（立即打断当前执行，并带上新指示继续）
$X/xay ctl <run_id> interrupt --text "发现严重方向偏差，请按以下新要求重写"

# 3. 恢复运行（继续被中断或暂停的任务）
$X/xay ctl <run_id> resume [--text "补充说明"]

# 4. 停下任务（立即终止并丢弃其副本改动）
$X/xay ctl <run_id> stop
```

---

## 6. 看板与用户交互

- 实时看板服务地址：`http://127.0.0.1:7878`。
- 在 Claude Code 桌面版中，可引导用户通过桌面版内置浏览器打开该页面，直观查看各 Agent 的泳道图、名额占用与事件流。
- 最终验收仍以原项目代码、`summary.md` 和验收测试命令的执行结果为准，不要仅依赖看板。

---

## 7. 安全与合规红线

1. **凭证隔离**：绝不读取、记录或输出任何 API Key、Token 或认证凭据。
2. **执行者角色隔离**：所有由 xay 拉起的执行者进程均带有 `XAY_ROLE=executor`，会被 `xay` CLI 拒绝执行控制命令。
3. **版本控制**：执行者不提交代码；代码验收完成后，由负责人统一进行 git commit。
