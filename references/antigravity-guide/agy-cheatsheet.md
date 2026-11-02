# agy 速查表：调用 Antigravity 时怎么用得更聪明

> 整理自 Antigravity 官方文档（docs/slash-commands、docs/cli/reference、docs/cli/best-practices、docs/settings）和 2026-10-28 本机实测。
> 同目录下的 `README.md` 与 `references/*.md` 是 Antigravity 自带的 antigravity-guide 原文（App / CLI / IDE / SDK 的站点地图）；需要最新细节时，按里面的链接查官方在线文档。

## 1. 选对“档位”：普通 / 斜杠命令

| 档位 | 用法 | 适合 | 非交互（`agy -p` / `xay-run agy`）能否用 |
|---|---|---|---|
| 普通单 Agent | 直接写任务 | 短小的审查、把需求翻译成 spec、给用户看的文案和说明、UI 对照、小改动（长任务会失忆） | ✅ |
| `/boost <任务>` | 多 Agent 深度推理：Orchestrator → DeepCoder/DeepInvestigator → 隔离 workers，会自己跑测试验证 | **疑难 bug、竞态条件、算法优化**；耗时从几秒到几小时，token 消耗大；需要付费套餐 | ⚠️ 能启动，但它要跑命令，不加 `--full` 会被拒绝、返回空结果（实测）。用 `xay-run agy --full -C <独立 worktree> "/boost …"` |
| `/goal <目标>` | 自主工作直到目标验证完成，中途不停 | 目标明确、可验证的较长任务 | ✅（同样需要跑命令时加 `--full`） |
| `/browser <任务>` | 启动沙箱 Chrome 子 Agent | 网页调研、UI 检查、浏览器内调试 | ✅（未实测） |
| `/plan` | 先调研、问需求，再出可审阅的计划 | 方案设计 | ❌ 会向用户提问，适合交互使用 |
| `/teamwork-preview` | Agent 团队，先由 Sentinel 做范围访谈，写出约束规格，再长时间执行 | **仓库级迁移、子系统重写、多日研究**；耗时数小时到数天；付费套餐 | ❌ 开头必须交互访谈。由负责人写好需求说明，建议用户在 Antigravity App 或 `agy` 里亲自运行 |
| `/grill-me` `/learn` `/schedule` `/btw` | 逐条追问设计 / 沉淀规则 / 定时 / 旁路提问 | 交互场景 | ❌ |

`xay-run` 会让斜杠命令保持在 prompt 最前面，附加的说明接在后面。

## 2. 模型：只用 Gemini 3.8

用户规定 agy 只能用 Gemini 3.8：`gemini-3.8-flash-low` / `-medium` / `-high`。`xay-run agy -e <强度>` 会自动选择对应档位（off/minimal/low → low，medium → medium，high 及以上 → high），并同时传 `--effort`。`xay-run` 会拒绝其他模型。

注意 agy 的长上下文会失忆：任务要短小、自成一体，长任务加 `--long`。

## 3. 权限（非交互模式最常见的坑）

非交互模式无法弹窗授权，需要授权的操作会被**自动拒绝**，结果里 `denied_actions` 会列出来（`xay-run` 会提示）。三种解决方式：

1. **只读任务**：在任务里写“不要运行任何命令”，配合 `--readonly`（`--mode plan`）。
2. **精确放行**（推荐）：在 `~/.gemini/antigravity-cli/settings.json` 里加规则，格式是 `动作(目标)`，括号前不能有空格；优先级是 deny > ask > allow。
   ```json
   {
     "permissions": {
       "allow": ["command(python3 -m unittest)", "command(npm test)", "read_url(github.com)"],
       "deny":  ["write_file(.env)", "command(git push)"]
     }
   }
   ```
3. **全部放行**：`xay-run agy --full`（即 `--dangerously-skip-permissions`），只在独立的 worktree 或临时目录里用。

其他相关设置：`toolPermission`（`request-review` 默认 / `proceed-in-sandbox` / `strict` / `always-proceed`），`enableTerminalSandbox: true` 把命令限制在系统沙箱里运行。

## 4. 让结果更好的写法（官方最佳实践）

- 在工作区根目录放 `AGENTS.md` 或 `GEMINI.md`，写清代码规范和测试命令，agy 启动时会读取。
- 给它一个本地测试或构建命令，并要求它**每次改动后都运行**，这是自主改代码最可靠的方式（需要放行该命令）。
- 先探索、再计划、后执行：大任务先派 `--readonly` 让它出计划，负责人审核后再派执行。
- 续接对话用 `--conversation <ID>`（`xay-run agy -r <ID>`），在原上下文里返工。
- 登录和 Antigravity App 共用；`/teamwork-preview` 和 `/boost` 需要付费套餐。
