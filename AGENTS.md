# 给 AI 的开发规则（改动本仓库前必读）

用户是非程序员。和用户说话用简体中文、先说结论、少用术语。

## 1. 开工前按顺序读
1. `README.md`：两份副本的关系、文件夹说明。
2. `references/ARCHITECTURE.md`：当前生效的设计是哪几份，各管什么。
3. 你要改的那部分对应的设计文档（**设计文档是唯一依据**；要改设计，先改文档并说明原因，再改代码）。
4. `references/backlog.md`：别重复做已有计划，也别做已经决定"不做"的事。

## 2. 哪些能动、哪些不能动

| 范围 | 规则 |
| --- | --- |
| `~/.agents/skills/xay-multiagent-workflow/`（安装副本） | **禁止直接修改**。只能通过 `scripts/xay-deploy` 从本仓库发布 |
| `scripts/legacy/` | 冻结，不改 |
| `V2/` | xay 从底层重构的新代码（用户 2026-10-30 批准的例外顶层目录）。按 `references/plans/arch-v10-rebuild.md` 和 `V2/docs/protocol.md` 开发。M5 切换起 `SKILL.md` 用 `xay2`、`scripts/xay` 默认转给 V2（`XAY_V1=1` 退回旧版）、7878 看板由 V2 管家提供；旧系统（V1）已于 2026-10-31 标为 legacy：代码保留、冻结不改，只作退路（旧版用法见 `references/legacy-v1.md`） |
| `tests/evidence/` | 真实验收证据，只读 |
| `references/plans/arch-*.md`、`ui-*.md` | 设计约定。改动必须同时说明理由，并在 `ARCHITECTURE.md` 更新状态 |
| `SKILL.md` | 所有负责人 AI 都按它干活，每次整篇读，**保持短**（`tests/test_skill_doc.py` 守着篇幅和硬性约束）。改动要小而准，不要整篇重写；新功能写进对应小节 |
| `references/scorecard.md` | 只追加，不删历史记录 |
| 其余 `scripts/`、`prompts/`、`tests/` | 可以改；改了代码要补或改测试 |

不要新建顶层文件夹；新文档放进 `references/`，新设计放进 `references/plans/`，调研放进 `references/plans/research/`。

## 3. 改完必须做
1. `python3 -m unittest discover -s tests` 全部通过（失败要修好，不能删测试凑数）。
2. 改了看板页面：用浏览器实际打开看一遍（真实数据 + `?demo=1`，宽屏和手机宽度，明暗两色）。
3. 在 `git` 里按小步提交，提交说明用中文写清"改了什么、为什么"。
4. 需要发布时，先问用户，再运行 `scripts/xay-deploy`。

## 4. 硬性约束（任何改动都不能违反）
- 执行者 AI 不 `git commit` / `git push`，不读取或输出任何密钥、token、cookie。
- agy 只用 `gemini-3.8-*` 模型；dsh 只用 `deepseek-v4.1-flash`。
- 允许且支持自动切换 agy CLI 的账号（换的是 Antigravity Tools 管着的那套账号凭据 / cookie），统一通过用户自己的 Antigravity Tools 命令行：`antigravity-tools list` 看账号、`antigravity-tools switch <序号> --target agy` 切换。由用户授权并自行承担多账号调度风险（2026-10-30 用户明确决定，2026-10-31 再次要求"agy 自动换号请你保留并完善"）。xay 负责额度预检和自动换号，尽量让 agy 额度不中断。这是用户的决定，不是违规，**不要删掉或回退这部分功能**。
- 换号时 xay 自己不读取、不提取、不保存 token 和 cookie，不做设备指纹伪装；读 Antigravity Tools 的账号记录只取额度和优先级字段，命令输出里的邮箱一律丢掉。
- 改了 V2 协议的活：说明写成新文件 `V2/docs/changes/<日期>-<任务 key>.md`，不要直接改 `V2/docs/protocol.md` 最上面（几件活一起改会冲突）；负责人验收时再合进去。
- agy 执行者不要跑仓库根目录那套旧测试（`python3 -m unittest discover -s tests`）：在 agy 的沙盒里会卡死不返回（2026-10-31 两次各卡 30~75 分钟）；只跑任务卡点名的测试，整套由负责人在外面跑。
- 不安装 claude 命令行工具。
- 读取 Claude 桌面版会话文件时，只取标题、目录、时间、是否归档这几个字段，不读对话内容。

## 5. 多个 AI 同时改时
- 每个 AI 只改任务卡里写明的文件；需要改别人的文件，先在汇报里提出，由负责人协调。
- 并行开发用独立工作区（xay 默认自动隔离），不要在同一个目录里同时改同一个文件。
