# 验收样本（golden）

25 份协议样本，每份两个文件：

- `NN-<英文短名>.events.jsonl`：一行一个事件，字段为 `ts / by / type / subject / ws / conv / data`，可选 `v`（默认 1）和 `dedup`；不写 `seq`，由测试按行号分配。
- `NN-<英文短名>.expect.json`：`[{"path":"…","equals":…}]`，`path` 指 §8 快照里的位置；列表用下标（如 `tasks[0].status`），`people` 用员工编号做键（如 `people.em_codex01.state`）。

读法约定：

- 每份样本各自在一个空的临时账本里回放，互不影响；统一用 `ws_demo01`，对话用 `cv_demo01`～`cv_demo25`。
- 整批样本的时间戳从 `1793300000` 开始，按行递增 10 秒；跨样本也保持严格递增。
- 特别路径 `$error`：该条 expect 带 `"line": 行号`，表示第 N 行 append 应抛 `PermissionError`，且这一行不进入快照；测试断言后**继续**回放下一行（§10）。
- `"equals": []` 表示快照里对应的列表为空（`proposals` / `decisions` 只放未解决、未关闭的）。
- `"equals": null` 表示快照里没有这个键；样本 06 用 `people.em_codex01 = null` 表示离职员工不在 `people`，样本 01/02/03/21/22/23 用 `before/after` 里的 `null` 表示该字段没有被改。
- 事件带 `"v": 99` 表示高版本事件，reducer 应跳过；样本 24 用它测向前兼容。
- 样本 08、09 在 `task.kept` 处截断，专门验证证据结论；按 §4.5 管家会紧接着写甲类 `decision.opened`，完整“需要你”流程见 10、11、25。

| 编号 | 样本 | 测什么（一句话） |
| --- | --- | --- |
| 01 | plan-add-change | 加一件活后只改标题；检查未给出的字段不变、版本 1→2、历史 `by=gm` 和 add/change 的 before/after。 |
| 02 | plan-remove | 计划里加两件活后删掉一件，检查任务变 `canceled`、版本 +1、remove 历史的 `before.status`。 |
| 03 | plan-assign-version | 管家自动改派不加版本、负责人改派加版本，检查最终负责人、版本和 assign 的 before/after。 |
| 04 | proposal-accept | 执行者申请改任务、负责人通过，检查只加一次版本、字段生效、历史 `by=员工` 和 before/after、理由带申请编号。 |
| 05 | proposal-reject | 执行者申请删任务被拒，检查计划版本和任务状态都没变、申请不再挂着。 |
| 06 | label-no-reuse | Codex、Gemini 离职后再招人，检查显示名继续往后编号、离职员工进 `former` 且不在 `people`。 |
| 07 | evidence-landed | 证据表第 1、2 行（有改动完成、只读任务无改动完成），工位写 `task.landed`，检查任务进入 `reported`。 |
| 08 | evidence-kept | 证据表第 3、4、5 行（写任务无改动、部分完成、失败但称完成），工位写 `task.kept`，检查 `needs_you`。 |
| 09 | evidence-failed | 证据表第 6、7、8 行（失败有改动、失败无改动、中断无改动），管家写 `kept/failed`，检查 `needs_you / failed / needs_you`。 |
| 10 | decision-open | “需要你”打开后，检查任务停在 `needs_you`、员工 `blocked`、选项是固定的 merge/rework/discard。 |
| 11 | decision-resolve | 甲类决定选 `merge`，检查未决决定清空、员工回 `idle`、任务变 `reported`。 |
| 12 | unknown-event | 两条未来事件类型夹在正常事件中间，检查被跳过且后面的事件照常生效。 |
| 13 | dedup-idempotent | 同一个 dedup 键的进度写两次，检查重复行不落库、已有进度不被旧值覆盖。 |
| 14 | permission-denied | 工位越权加任务、员工改别人的任务两行被拒，后面的行继续写；检查两条 `PermissionError`、`seq=9` 和最终状态。 |
| 15 | employee-offline | 上线、心跳、离线，检查员工状态 `offline`、`online=false`。 |
| 16 | context-compact | 记忆用满后请求并完成原生压缩，检查压缩次数、段号、`calibrating` 和压缩后的 `pct=18`。 |
| 17 | context-updated | 更新 dsh 员工的记忆条，检查 `pct`、`estimated`、`calibrating`。 |
| 18 | letter-answer | 员工向部门发 request、送达、总经理回信并答复，检查送达、关闭和 `verdict`。 |
| 19 | queue-and-progress | 两件活排队、第一件完成并由管家写 `landed`、负责人验收，检查 `done / queued`、员工队列和进度。 |
| 20 | letter-pending | 员工向部门发 request 但未送达未答复，检查 `delivered=false`、`open`、员工 `blocked`。 |
| 21 | proposal-add-task | 执行者申请新增任务、负责人通过；检查新任务、只加一次版本、历史 `by=提出者` 和 add 的 after。 |
| 22 | task-changed-fields | 只改 `writes`；检查 title/dept/deps/owner/status 不变、数组整体替换、before/after。 |
| 23 | plan-history-before-after | 加、管家改派、负责人改派、删；检查历史 `by=gm`、三种 before/after，以及管家改派不占版本。 |
| 24 | version-skipped | `v=99` 的事件夹在中间；检查被跳过、标题和版本只受后一条影响。 |
| 25 | decision-rework-discard | 两个甲类决定分别选 `rework` / `discard`；检查任务变 `queued` / `canceled`、决定清空。 |

## 协议问题

以下是第一轮写样本时发现的 13 个问题在 1.1 中的处理结果，以及这一轮新发现的问题。仍标“仍有问题”的，样本按最保守的方式处理，没有自行补协议规则。

1. **证据结论由谁写（§3 与 §4.3/附录 A 矛盾）**：1.1 已解决（§3 改成逐事件权限表，明确 `task.landed`/`task.kept` 允许 `station`、`office`，`task.failed` 允许 `office`、`lead`；样本 07/08 用工位写 landed/kept，09/19 用管家写 kept/failed/landed，不再用负责人代写）。
2. **计划历史的 `by`（§4.2 与 §8）**：1.1 已解决（§4.2 给出映射：`lead:*`→`gm`、`user`→`user`、`employee/station`→员工编号、`office/rules`→`office`；样本 01/02/03/04/21/23 断言 `by` 和 before/after）。
3. **快照里的 `decisions`（§8 与 §5）**：1.1 已解决（§4.5 定义每条决定的字段、甲类固定选项 merge/rework/discard，以及决议后的任务状态；样本 10/11/25 断言）。
4. **`employee.fired` 之后（§4.1 与 §5）**：1.1 已解决（§4.1 明确 `people` 里不再出现、改列在 `former`、显示名不回收；样本 06 断言 `former[0]/[1]` 和 `people.em_codex01 = null`）。
5. **压缩后的 `context.pct`（§4.4）**：1.1 已解决（§4.4 明确有 `after_pct` 就把 `pct` 设成它，没有就保留原值；样本 16 断言 `pct=18`）。
6. **改字段是合并还是替换（§4.2）**：1.1 已解决（§4.2 明确“只改给出的字段，数组字段整体替换”，并定义 before/after；样本 01/22/23 断言未给出的字段不变和 before/after）。
7. **申请里新增任务的编号（§4.2）**：1.1 已解决（§4.2 明确 `op=add` 的 `task` 由提出者写申请时生成，`fields` 至少有 title；样本 21 断言新任务和只加一次版本）。
8. **高版本事件（§0.5）**：1.1 已解决（§0.5 明确跳过、写 `core.log`、不进快照；样本 24 断言高版本那条不改标题/版本。`signals` 的歧义见新问题 16）。
9. **未知事件类型的写入权限（§3）**：1.1 已解决（§3 最后一行明确未知类型 `lead/office/rules/station` 可写，`user/employee/bridge` 写入被拒；样本 12 用 `lead` 写，照常跳过）。
10. **`employee.hired.label?`（§1 与 §4.1）**：1.1 已解决（§4.1 明确给了没人用过的 label 就用它且不占自动序号，重名 `ValueError`；样本 06 只测自动号不复用，不使用 label）。
11. **被拒行之后怎么继续（验收约定）**：1.1 已解决（§10 明确被拒行抛出错误、不写入，然后继续下一行；样本 14 把两条越权行放在中间，后面继续写并断言 `seq=9`）。
12. **`desk.landed/kept` 与 `task.landed/kept`（§3 与 §4.3）**：1.1 已解决（§3 权限表只列 `task.landed`/`task.kept`，不再有 `desk.landed/kept`；样本统一按 §4.3 用 `task.*`）。
13. **`employee.joined` 与 `desk.removed` 的写入者（§3）**：1.1 已解决（§3 明确 `employee.joined` 允许 `lead/office/bridge`，`desk.removed` 允许 `user`；样本 01/18/20 用 `lead` 写 joined，未使用 `desk.removed`）。

新发现的问题：

14. **`turn.ended` 没有 `em`（§3 与 §4.3）**：仍有问题。§3 身份范围要求“subject 是一轮 turn 且 `data.em` 是自己”，但 §4.3 的 `turn.ended` data 只有 `status/evidence/usage?`，没有 `em`。样本给 `turn.ended` 补了 `data.em`（状态/证据字段不变），否则严格的 §3 校验会拒绝工位写 `turn.ended`。建议 §4.3 补上该字段，或说明 turn 的归属从 `turn.started` 的记录取。
15. **`proposal.made` 没有 `from`（§3 与 §4.2）**：仍有问题。§3 要求 `employee/station` 写的 letter/proposal `data.from` 必须是自己，但 §4.2 `proposal.made` 的 data 字段表里没有 `from`。样本 04/05/21 按 §3 补了 `data.from`；建议协议字段表补上。
16. **高版本事件“不写账本”与 `version_skipped` 信号（§0.5、§4.5）**：仍有问题。§0.5 说高版本事件“不写账本、不进快照”，但事件本身已经由 append 写入 `events`（否则测试无法逐行回放）；§4.5 的 `signal.raised` 又列了 `kind=version_skipped`，没有说跳过时到底写不写 signal。样本 24 只断言现状不受影响（标题/版本），不断言 `seq` 和 `signals`，避免把一种解释写死；建议明确“不写账本”指不写现状表，并说明是否写 `version_skipped` 信号。
17. **`former` 的顺序与缺失键表示（§8）**：仍有问题。§8 只给 `former` 数组，没有定义顺序；expect 也没有定义“键不存在”怎么表示。样本 06 按离职先后断言 `former[0]/[1]`（本例与编号顺序一致），并用 `people.em_codex01 = null` 表示不在 `people`。建议在测试约定里写明 `former` 排序和缺失键返回 null。
18. **申请 add 的 `fields` 能否带 owner（§4.2）**：仍有问题。§4.2 只说 `op=add` 的 `fields` 至少有 title，没有说明能否带 owner；而 `task.changed` 的可改字段不含 owner。样本 21 的 add 不带 owner，新任务保持未指派。如果协议允许申请新增任务时直接指定负责人，需要补一句。
19. **管家自动改派是否写计划历史（§4.2）**：仍有问题。§4.2 说 `by=office` 的自动分配“不加版本，只写理由”，没有说是否写计划历史。样本 03/23 按“不写历史”断言（`history[1]` 是负责人那次改派才产生的）；建议协议明确。
