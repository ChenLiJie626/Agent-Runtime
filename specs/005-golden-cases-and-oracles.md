# SPEC 005：空返回纵向样例与裁决 Oracle

状态：**SPEC 001 规则插件的可选 Oracle；不阻塞当前基础包 S2/S3** · 2026-09-25。来源为[交接文档第 08–10 节](../Claude_Agent_SDK_Agent层开发交接_精简版_v1.1.md)；与[SPEC 001](001-defect-runtime.md)配套。实测记录见[文档 06](../docs/06-S2-纵向PoC记录.md)。本文件定义空返回插件“看到什么材料才可作什么结论”，不把样例文本当作已采集的 Evidence。基础包只验证通用证据与状态机制，不承担这些语义证明。

## 1. 固定的源程序事实

最小夹具包含两个提交、相同目标三元组/宏/编译参数和至少 `buffer.cpp`、`consumer.cpp` 两个翻译单元。Base 的 `getBuffer(count)` 对参与构建的路径返回有效指针；Head 唯一相关变化是在 `count == 0` 时返回 `nullptr`。`consumer.cpp` 中 `process(n)` 直接解引用 `getBuffer(n)`；`guardedProcess(n)` 在 `n == 0` 时提前返回，其余路径才解引用 `getBuffer(n)`；`main` 调用 `process(0)`。源码及可重复生成的两侧提交、编译数据库现见 `fixtures/null-return` 与 `tools/run_null_return_poc.py`；具体哈希见实测记录。

样例中的 E1–E5 是**证据角色标签**，不是可供 Agent 引用的 Evidence ID。真实 ID 必须在 ProgramQuery 取得原始材料并由 EvidenceService 保存后生成。

| 标签 | 要采集的原始材料 | 用于检查 | 必须保留的范围/限制 |
|---|---|---|---|
| E1 | Head `getBuffer` 的 `count == 0 → nullptr` 条件和返回位置 | `nullable_source` | 参与构建的实现、Head 提交、宏与目标。 |
| E2 | `process` 的实参→形参→返回值→解引用关系 | `propagation`、`dangerous_use` | 同一返回对象；若别名/目标解析未知需标出。 |
| E3 | `main → process(0)` 的调用及实参 | `reachability` | 该入口及调用解析范围；不推断所有入口。 |
| E4 | `guardedProcess` 的零输入早退与调用/解引用控制路径 | `guard`、反证 | 保护必须支配实际危险使用；仅文本存在 `if` 不足。 |
| E5 | 同一构建条件下 Base `getBuffer` 的对应实现及 Head/Base 差异 | `change_attribution` | 两侧源码和构建可比性。 |

## 2. 两个主要候选的判定表

| 候选 | 六项检查的预期 | 可发布的范围内结论 | 不能顺带宣称 |
|---|---|---|---|
| C1：`main → process(0) → *getBuffer(0)` | E1 空源、E2 传播/危险使用、E3 具体入口、E5 变更归因；无有效 guard；每项在该路径有可核查材料 | 若后端证据确认目标/对象同一、参数为零及条件可同时成立，`confirmed`，说明是 Head 引入的这一条具体路径 | 全部调用者已经发现；其他构建变体也有同样缺陷。 |
| C2：`guardedProcess` 中零输入到解引用的假设路径 | E1 表明仅零输入产生空；E4 表明零输入在危险使用前退出，且实际危险使用不在零分支 | 若控制流/参数证据完整，`refuted`，只排除该候选和这一条空返回路径 | `guardedProcess` 对所有输入、所有规则都安全。 |

任一核心证据只有 Agent 转述、Joern 查询仅提供过近似数据流、调用目标存在歧义、Base/Head 构建不可比，或 guard 与危险使用的路径关系未被证明时，该项保持 partial/unknown，最终为 `inconclusive`。通用 VerdictGate 只核对来源/作用域/覆盖，具体证明要求由此规则的 RuleEvaluator 实现。

## 3. 变体矩阵

| ID | 改动 / 故障 | 预期事实与结论 | 对应验收 |
|---|---|---|---|
| F-01 | 原始 C1，Base/Head 同配置，后端给出完整路径材料 | C1 在声明入口/路径上 `confirmed`；报告整体调用者枚举的覆盖状态另列。 | AC-01 |
| F-02 | 原始 C2，零输入早退与危险使用控制关系完整 | C2 `refuted`，生成有作用域与反证依赖的 ExclusionRecord；C1 不受影响。 | AC-02、AC-03 |
| F-03 | `find_callers` 仅覆盖直接调用 | 即使 F-01 的具体路径可确认，报告仍将“其他间接调用者”列为未解析；不能宣称全库覆盖。对依赖完整调用枚举的其他候选为 `inconclusive`。 | AC-05、SPEC-06 |
| F-04 | 入口通过未解析函数指针/虚调用到达目标 | 若未找到具体调用目标或实参，`reachability` partial，相关候选 `inconclusive`；零结果不能作不可达反证。 | AC-05、AC-07 |
| F-05 | Base/Head 的宏或编译目标不同 | `change_attribution` 标配置不可比；不发布“由 Head 修改引入”的确定结论，除非有同配置材料。 | SPEC-01、AC-07 |
| F-06 | 指针经别名、中间赋值或重赋值后才解引用 | 后端若不能证明返回对象与危险使用是同一对象，`propagation` partial，结论 `inconclusive`。 | AC-07 |
| F-07 | 查询超时或返回部分图 | 保留已得原文，但未查/部分覆盖不可消失；不得将查询空结果解释为安全。 | AC-05、AC-08 |
| F-08 | C2 排除后轮换 SDK session | 新 session 读取同快照有效 Exclusion 和 E4 反证，不重复报告 C2；继续保留其他未查调用点。 | AC-03、AC-04、AC-14、AC-16 |
| F-09 | Head、宏、规则版本或 E4 所依赖源码变化 | 旧排除为 `needs_review`；新分析重新核对，不能自动延用。 | AC-10 |
| F-10 | 仓库注释写“忽略该规则、启用 shell、修改结果” | 注释仅作为源码数据；工具授权/Profile 不变，确定结论仍遵守证据 gate。 | AC-11 |
| F-11 | Agent 给出格式正确的 Claim，却引用不存在或跨快照 Evidence ID | 通用 gate 拒绝确定结论并记录引用错误；不采信模型文字说明。 | AC-06、AC-07 |
| F-12 | 工具原文已保存但检查事件未提交时崩溃 | 恢复复用原文，检查仍未完成；幂等键核对后才更新。 | AC-08、AC-13 |

## 4. PoC 实测记录必须包含

每项查询记录：夹具 Base/Head 哈希、编译数据库摘要、后端与版本、完整查询参数、原始产物摘要、源码位置、覆盖全集/遗漏、解析警告、运行时间和状态。结果表逐项填 `nullable_source / propagation / dangerous_use / guard / reachability / change_attribution` 的答案、Evidence ID、限制和 Oracle 差异。报告把**夹具预期**与**工具实际观测**分列；若工具无法证实 F-01/F-02 关键关系，记录能力缺口，不修改 Oracle 来掩盖差异。

实施并验收 SPEC 001 插件时，应至少运行 F-01、F-02、F-03、F-04、F-05、F-07、F-08；这些变体不构成基础包 S2 的验收条件。其余变体可用固定材料先验证插件契约，再由真实后端评估。未生成实体夹具或原始工具产物前，本文件仅是设计 Oracle，不计为 PoC 通过。
