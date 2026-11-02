# 示例计划

三份现成的"大任务计划"，可以直接拿来看、改、试。每份都是 `xay2 plan template` 那套格式，已经用 `xay2 plan apply --dry-run` 检查通过。

平时你不用自己写计划：对负责人 AI 说一句"用 xay 帮我做 ××"，它会替你拆好任务、填好计划、再交给程序去派活。这里放出来，是让你看清它拆出来的东西长什么样。

| 示例 | 干什么 | 怎么分工 | 看点 |
| --- | --- | --- | --- |
| [`01-product-page.plan.json`](01-product-page.plan.json) | 给网店做新版商品页 | 页面、数据、测试三个小组；测试要等前两组都做完 | 和视频里回放的是同一个故事：三个小组同时开工，最后一步由负责人亲自审 |
| [`02-expense-cli.plan.json`](02-expense-cli.plan.json) | 做一个命令行记账小工具 | 先做核心功能，再补测试和说明 | 最小的一份：两步、四件小任务，适合第一次试 |
| [`03-research-report.plan.json`](03-research-report.plan.json) | 调研三种方案并写出对比报告 | 三个 AI 各查一种方案，最后汇总成一份对比 | 没有代码，只有文档；展示"同时查、再汇总" |

## 怎么试

先体检一下环境：

```bash
xay2 doctor
```

只**检查**计划，不会真的派活，也不花额度：

```bash
xay2 begin --title "做一个命令行记账小工具"
xay2 plan apply --dry-run docs/examples/02-expense-cli.plan.json
```

看到任务树没有报错，就说明计划格式没问题。

**真的开跑**，把 `--dry-run` 去掉：

```bash
xay2 plan apply docs/examples/02-expense-cli.plan.json
xay2 plan show        # 看任务树
xay2 plan open        # 打开任务画布
```

> ⚠️ 真开跑会让 Codex、DeepSeek、Gemini 真的干活，会消耗它们的额度。
> 示例里的文件名（比如 `web/product/index.html`）都是虚构的：用在你自己的项目里之前，先把每件小任务的 `writes`（允许改哪些文件）、`verify`（怎么验收）和 `card`（任务说明）改成你项目里真实的样子。

## 计划文件里每个字段是什么意思

- `title`：大任务的名字，用大白话。
- `groups`：中任务（一组活）。`why` 写做完能得到什么；`after` 写要等哪些组先做完；`check` 写谁来验收（`rules` 程序自动查、`lead` 负责人亲自审、`ai:codex` 等让某个 AI 审）。
- `tasks`：小任务，交给某一个 AI（`agent`：`codex` / `dsh` / `agy`）。`writes` 是它允许改的文件，`verify` 是做完后跑的验收命令，`card` 是详细说明，`deps` 是要等的前面的小任务。
- 标题都要说成大白话：说清做什么、能达成什么，不写文件名、函数名、英文单词。

想要带中文注释的完整模板：`xay2 plan template`。

## 不装 AI 也能先看效果

启动后打开 <http://127.0.0.1:7879/org?demo=1>，是一个编好的小故事（就是示例 01 那件事）的回放，不消耗额度，也不会读到你的真实数据。仓库里的 [演示视频](../media/demo.mp4) 就是这样录的。
