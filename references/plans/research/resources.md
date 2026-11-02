# 资源占用审计（2026-10-29 负责人实测）

## 硬盘
- xay 自己很小：38 个运行目录合计 7.4 MB，其中 events.raw 5.9 MB；单个最大 events.raw 1.9 MB（codex 调研任务）。~/.xay 共 14 MB（含 1 个冲突保留的 worktree）。
- 大头在各 CLI 自己的会话存储（xay 每次派活都会在这里留下完整会话）：
  - ~/.gemini 12 GB：antigravity-ide 4.1G、antigravity-backup 4.1G、antigravity 3.9G、browser-profile 140M
  - ~/.codex 5.0 GB：sessions 1.5G、thread_history_1.sqlite 1.4G、archived_sessions 618M、packages 582M、plugins 427M
  - ~/.dsh 542 MB
- worktree：每个写任务一个完整工作副本（git worktree add），大仓库会成倍占盘；node_modules 等被忽略目录不复制，导致 worktree 里跑不了测试的风险。

## 内存（同时有若干任务在跑时）
- codex exec：单个约 180 MB
- agy 的 language_server：单个约 680 MB（最重）
- 执行者相关进程 25 个，合计约 1.36 GB
- 每个任务一个独立 CLI 进程，没有复用；xay-run 自身 bash 进程很轻（1-2 MB）。
