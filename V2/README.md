# xay V2（M1~M5 已完成并发布；旧版还能用 XAY_V1=1 退回）

2026-10-30 用户批准方案：`references/plans/arch-v10-rebuild.md`（先读它的"一页纸看懂"）。新代码全部放在这里。M5 起负责人按 `SKILL.md` 用 `xay2`；旧系统（仓库根目录的 `scripts/` 等）已标为 legacy：代码保留、冻结不改，退回办法见下面"M5 切换"和 `references/legacy-v1.md`。

## 一句话
一本账本（`~/.xay/v2/xay.db`，SQLite）+ 一个常驻管家 + 每个员工一个独立工位；员工归项目、长期在岗、一人一张桌子；记忆条和压缩有人管；页面（公司页 7879/org，看板 7878）只照账本画。

## 目录
| 路径 | 内容 |
| --- | --- |
| `docs/protocol.md` | 协议细则：对象、编号、事件、状态、谁能写什么、命令（**写代码前必读，唯一依据**） |
| `docs/probes.md` | 探针日结果：三家 CLI 实测（续接、用量字段、压缩、权限） |
| `probes/` | 探针脚本（一次性实测用） |
| `fixtures/` | 事件样本与期望快照（协议测试用） |
| `bin/xay2` | 命令入口（M1 起） |
| `xay/` | 代码：`core`（账本）、`office`（管家）、`station`（工位）、`adapters`（各家 AI）、`desk`（桌子/工作区）、`providers.py` |
| `web/` | 页面（从 `scripts/xay-org.html`、`xay-watch.html`、`xay-chars/` 移植，保留外观） |
| `tests/` | `python3 -m unittest discover -s V2/tests` |

## 进度
| 里程碑 | 状态 |
| --- | --- |
| M0 协议 + 探针日 | 完成（`docs/protocol.md`，现为 1.14；`docs/probes.md`） |
| M1 真页面 | 完成：账本、管家（`bin/xay2 office start` → http://127.0.0.1:7879/org）、旧系统只读桥、公司页读真实数据；另已做好 M2/M3 的桌子与适配层 |
| M2 Codex 员工 | 完成：工位常驻、同一对话接着干、记忆条、原生/交接压缩、满载换线路、员工详情栏；真机冒烟见 `docs/probes.md` 末尾（压缩还没在真机上触发过） |
| M3 dsh + agy 员工 | 完成：两家常驻、同一对话接着干（dsh 会话号晚到也记得住）、dsh 按它自己的窗口（262,144）算记忆条并守住补丁和模型、agy 记忆条改对 + `/compact` 先原生、交接摘要附机器事实、续接失败自动交接重做、死循环叫停、agy 额度用完换号重做、每天 40 次；真机冒烟见 `docs/probes.md` 末尾（死循环、额度换号、dsh 自己的自动压缩只在假 AI 上测过） |
| M4 公司自动化 | 完成：选人三步规则（老员工优先）、自动归部门、反应表（失败发回重修、额度失败交你决定、死循环不再重派、沉默先问、复述不出再给笔记、文件重叠提醒、冲突重试、失败 3 次交你）、预算三条线（一轮中途也管得住）、agy 每天 40 次、总闸（连各家命令行一起停）+ 页面红按钮、信件送达与回信、改计划申请、`xay2 wait/brief/resume`。真账本演示见 `docs/probes.md`"M3 + M4 真账本演示"（预算、总闸、死循环只在假 AI 上测过） |
| M5 切换 | **已发布（2026-10-31）**：`scripts/xay` 默认走 V2（`XAY_V1=1` 退回）；7878 由 V2 管家提供看板；`SKILL.md` 改用 xay2 并瘦身（旧版用法在 `references/legacy-v1.md`）；发布时也跑 V2 测试。已完全用 V2 做完真实的活：本仓库的自我改进对话 `cv_20261031-004916_f306` 由管家派给 agy / Codex Plus / DeepSeek 员工做完 15 件、负责人逐件验收合并。旧版已标为 legacy，临时桥已删除（2026-10-31） |

## M5 切换（2026-10-31 已发布，旧版已标为 legacy）

**改了什么**
- `scripts/xay` 默认转给 `V2/bin/xay2`（分发规则在 `scripts/xay_switch.py`）：只有旧版有的子命令（`session / ctl / decide / doctor / status / quota / agy-accounts / agy-switch / workers / send / inbox …`）照旧走旧版；两边同名的（`wait / dept / answer / ask / tell / reply`）看参数：`--session`、`m_` 编号、`dept create` → 旧版，`--since`、`lt_ / dc_ / pp_` 编号、`dept open` → 新版，分不出来时旧系统的会话（设了 `XAY_LEAD_SESSION`、或这个 Claude 会话在旧系统里开着会话）走旧版、其余走新版。旧系统派的执行者全部走旧版。强制：`xay v1 …` / `xay v2 …`；全部退回：`XAY_V1=1`；看走向：`xay --which …`。
- 看板：`/`（也叫 `/board`）是 V2 看板，样子沿用旧看板（左侧对话列表，右边每名员工一列，单列 / 并排），列身是"看他的屏幕"同一套终端（`XayCliView.pane`）；对话列表接口 `GET /api/v2/convs`。公司页顶栏"看板"按钮跳到这里。
- 端口：管家照旧在 **7879**，另外**同时在 7878** 提供同样的页面（选这个方案的理由：7879 的公司页书签、7878 的看板书签和旧通知链接都不用改，两个端口看到的是同一个管家）。只有不写 `--port` 启动时才开 7878（测试和临时管家写了 `--port`，不会去占 7878）；7878 被占着（旧看板还开着）不影响管家，每 30 秒再试一次，旧看板一停就自动接上。控制：`--board-port N`（0 = 不开）、`XAY2_BOARD_PORT`、`$XAY2_HOME/board.off`（有这个文件就不开，退回旧看板用）。`xay2 office status` 会说看板现在在哪个端口。
- 临时桥已删除，旧系统派的活不再出现在新版页面上，以前镜像进来的旧活照旧能看。
- `scripts/xay-deploy`：发布前跑旧的 `tests/` 和 `V2/tests/`；`--restart-board` 改为停掉 7878 上的旧看板、用安装副本重启 V2 管家。

**负责人发布时的步骤**
1. 合并到 `main`，在开发仓库跑 `scripts/xay-deploy --restart-board`（会跑两边测试 → 发布到安装副本 → 停掉 7878 上的旧看板 → 用安装副本里的 V2 重启管家；正在干活的工位是独立进程，不受影响）。
2. 看 `~/.agents/skills/xay-multiagent-workflow/V2/bin/xay2 office status`：应写"看板 http://127.0.0.1:7878/"。如果写"7878 端口被别的程序占着"，用 `lsof -nP -iTCP:7878 -sTCP:LISTEN` 看是谁，停掉后 30 秒内自动接上。
3. 打开 http://127.0.0.1:7878/ 和 http://127.0.0.1:7879/org 看一眼；新开的负责人会话会读到新的 `SKILL.md`（已经开着的会话照旧用旧命令，`scripts/xay` 会把它们分给旧版）。
4. 验收：已完成。旧版已标为 legacy，临时桥已删除，旧账历史仍能重放显示。

**退回**
- 只退命令：负责人每条命令前加 `XAY_V1=1`（或 `xay v1 …`），旧版用法见 `references/legacy-v1.md`。
- 7878 换回旧看板：`XAY_V1=1 scripts/xay-deploy --skip-tests --restart-board`（建 `~/.xay/v2/board.off`、重启管家让出 7878、在 7878 起旧看板）。再切回新版：`scripts/xay-deploy --skip-tests --restart-board`（会删掉 `board.off`）。

