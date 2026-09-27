# SPEC 003：状态、事件与崩溃恢复契约

状态：**基础包通用恢复契约；示例事件序列不作规则门禁** · 2026-09-25。对象字段见[SPEC 002](002-domain-and-api-contract.md)，基础包验收见[SPEC 000](000-runtime-kernel.md)；[SPEC 001](001-defect-runtime.md)仅是后续规则示例。本文件规定可观察行为与事务边界，不绑定某个 ORM。

## 1. 权威状态与租约

业务事件、Evidence 和 Exclusion 是权威记录；当前任务投影、ContextView、报告缓存均可重建。SDK transcript 是对话历史，不参与证明检查已完成。首版一个进程按候选串行运行，但存储仍须阻止意外的第二进程/旧执行者写入同一 SDK 历史。

| 实体 | 状态 | 合法转换要点 |
|---|---|---|
| Analysis | `created → active → completed/partial/failed/cancelled` | `completed` 表示已安排任务均到终态；可包含 `inconclusive`。未发现集合之外的覆盖仍为 unknown。 |
| Task phase | `planned → role_running → gating → terminal`；默认角色顺序为 Investigator、Verifier | 每个角色步骤由 Profile 声明并记录 role ID；新增专项角色不新增核心枚举。工具失败/超时可进入 `terminal`，结论为 `inconclusive`；不能跳过 gate 发布确定结论。重开创建新 Attempt，并记录 `TaskReopened`。 |
| Task execution | `pending → running → completed/partial/failed/cancelled` | 与 finding state 正交；`completed + inconclusive` 合法，`failed + confirmed` 非法。 |
| Finding | `unassessed → confirmed/refuted/inconclusive` | 每次变化由新 `VerdictDecided` 事件记录；旧结论不可覆盖。若重开，从新 Attempt 的 `unassessed` 开始，历史保留。 |
| CheckItem | `unexamined → in_progress → complete/partial/failed/unsupported` | 从 partial/failed/unsupported 再查须新查询与事件；`complete` 需要该检查声明范围内的有效 Evidence 和覆盖说明。 |
| Query | `requested → running → complete/partial/timeout/failed/unsupported/denied`；`requested/running → in_doubt → running/终态` | 崩溃恢复中发现请求但无可信终态时标 `in_doubt`；核对后进入终态或生成明确的新 attempt，不将 `in_doubt` 默认为 complete。 |
| RoleAttempt | `created → running → completed/paused/failed/cancelled/interrupted` | 已终止 Attempt 不重新写入；续查生成新 Attempt，显式关联前次和同一/新 SDK session。 |
| Exclusion | `valid → needs_review/revoked`；`needs_review → valid/revoked` | 新证据或新快照可使其待复核；同一 record 的旧状态保留在事件中。不得直接删除。 |

`SessionBinding` 的租约包含 `sdk_session_id?`、`owner_id`、`lease_epoch`、`expires_at` 和 `binding_digest`。获取租约使用存储中的比较交换，递增 `lease_epoch`。每次领域写入和每次受控工具调用都核对 epoch；旧执行者即使仍持有 SDK 客户端，其新工具调用必须被拒绝，写入也被拒绝。租约过期本身不证明旧进程已停止；新拥有者先标记旧 Attempt 为 interrupted，再执行恢复核对。

## 2. 业务事件封套

每个 `RuntimeEvent` 必须含 `schema_version, analysis_id, seq, event_id, event_type, task_id?, attempt_id?, occurred_at, causation_id?, correlation_id?, payload_digest, payload`。`seq` 在一个 analysis 内单调递增且无重复，由数据库事务分配；`event_id` 全局唯一。`causation_id` 指向触发事件/请求，`correlation_id` 串联一条候选调查链。事件一经提交不得修改或删除。事件与对应投影更新在同一个 SQLite 事务内提交；投影故障可从事件重建。

| 事件 | 必需载荷 | 产生条件与后续影响 |
|---|---|---|
| `AnalysisCreated` | `snapshot_digest, profile_digest` | 创建固定分析；同 ID 不同内容拒绝。 |
| `AnalysisStatusChanged` | `old_status, new_status, reason` | 分析状态转换；终态含部分完成与失败原因。 |
| `CandidateProposed` | `discovery_ref, provisional_fields, unresolved_identity[]` | 暂存候选；不计入精确候选分母。 |
| `CandidateBound` | `candidate_digest, source_proposal_ids[], required_check_ids[]` | 身份确立；相同 digest 幂等合并发现来源。 |
| `TaskCreated` / `TaskPhaseChanged` | `task_id, candidate_digest, required_check_ids[]` / `old_phase, new_phase, reason` | 创建任务及推进确定性流程；不得凭 SDK 消息更新阶段。 |
| `AttemptCreated` | `attempt_id, task_id, role, predecessor_attempt_id?, reason` | 新角色执行或续查的身份；恢复时不复用已终止 Attempt。 |
| `SessionLeaseAcquired` / `SessionLeaseReleased` | `binding_id, owner_id, lease_epoch, expires_at` / `binding_id, lease_epoch, reason` | 与租约 CAS 在同一事务，供审计和重放绑定；过期租约可由下一个 epoch 取代。 |
| `TaskStarted` | `task_id, attempt_id, role, binding_digest, lease_epoch` | 角色租约有效后开始运行。 |
| `QueryRequested` | `query_id, idempotency_key, request_digest, check_id` | 在调用外部工具前提交。相同 key 不同 digest 拒绝。 |
| `QueryStarted` / `QueryInDoubt` | `query_id, lease_epoch` / `query_id, reason, prior_attempt_id?` | 明确执行开始与恢复时的不确定状态；不得把不确定状态当查询空结果。 |
| `QueryFinished` | `query_id, status, coverage, diagnostics?, raw_artifact_digest?` | 一次工具尝试的终态；失败/空结果也可记录，但不必有 Evidence。 |
| `EvidenceRecorded` | `evidence_id, query_id, artifact_digest, snapshot_digest` | 与成功/部分 QueryFinished 及 Evidence 元数据在同一事务；对外返回 ID 后不得回滚。 |
| `FactRecorded` | `fact_id, check_id, predicate_kind, polarity, evidence_refs[], scope` | 记录工具事实或 Agent 解释的来源层级；模型推断不得冒充工具事实。 |
| `CheckUpdated` | `check_id, prior_status, new_status, prior_unknowns[], new_unknowns[], answer, evidence_refs[], limitations[]` | 验证合法转换、引用和覆盖后更新投影；未知项的移除可精确归因到此事件。 |
| `ClaimRecorded` / `AssessmentRecorded` | `claim_id` / `assessment_id`, `attempt_id`, `record_digest` | 保存模型主张及验证判断；都不是 Verdict。 |
| `HandoffSaved` | `handoff_id, protected_item_ids[], event_high_watermark, digest` | 校验保留项后保存不可变版本。 |
| `TransitionCreated` / `TransitionCompleted` / `SessionBound` | `transition_id, old_binding_id, new_binding_id?, handoff_id, sdk_session_id?` | 先登记轮换意图；新 Attempt、SessionBinding 和 `TransitionCompleted` 在同一事务提交；拿到新 SDK ID 后再写 `SessionBound`，不可反向改旧历史。 |
| `SessionUnavailable` | `binding_id, sdk_session_id?, reason, recovery_handoff_id?` | SDK 历史无法明确 resume；不改写领域检查状态。 |
| `AttemptEnded` / `TaskReopened` | `attempt_id, terminal_status, reason` / `new_attempt_id, reason` | 记录续查和最终/部分结束。 |
| `TaskExecutionChanged` | `old_status, new_status, reason` | 记录执行状态，不与缺陷结论混用。 |
| `VerdictDecided` | `candidate_digest, verdict, scope, rule_evaluator_version, support_refs[], counter_refs[], blockers[]` | 确定结论要求通用 gate 与 RuleEvaluator 均通过；门禁不通过只能记录 `inconclusive` 与 blockers，不得保留无效引用。 |
| `ExclusionChanged` | `exclusion_id, old_status?, new_status, reason, dependency_digests[]` | 与相关 refuted verdict 同事务创建；失效过程可审计。 |

任何新事件类型和载荷字段变更遵守 SPEC 002 的 schema 版本规则。保存 SDK 原始消息的诊断日志不得直接生成 `CheckUpdated`、`VerdictDecided` 或 `ExclusionChanged`。

## 3. 幂等键与提交顺序

`QueryRequest.request_digest` 包含 `snapshot_digest + backend_id/version + operation + scope + args + check_id + timeout_ms + tool_policy_digest` 的规范 JSON 摘要；`QueryOutcome.query_digest` 必须等于该摘要。`idempotency_key` 在同一分析内绑定此 digest。重复请求若参数完全一致，可返回已提交的 QueryOutcome/EvidenceRef；若相同 key 对应不同 digest，返回 `Conflict`。

1. **请求事务：** 校验 task、租约、政策和预算；插入 `QueryRequested` 与 Query 投影；提交后写 `QueryStarted`，再调用 ProgramQuery。若已提交请求却未写开始事件，恢复时仍按未决处理，不推断工具未执行。
2. **工具执行：** 只读查询在固定 Snapshot 上执行。结果先保留原文；大产物先写临时文件、计算哈希并原子改名。此时尚不得把 Evidence ID 返回给 Agent。
3. **结果事务：** 验证 QueryOutcome 与原文哈希；写 `QueryFinished`、Evidence 元数据、`EvidenceRecorded` 和投影。小产物可与这些数据在同一 SQLite 事务中保存。大文件与 SQLite 不是一个原子事务，崩溃后用摘要核对；未登记的文件是孤儿，不能被引用。
4. **检查事务：** Rule/Check 验证该结果对特定问题的证明范围后，另写 `CheckUpdated`。结果已经保存但检查尚未更新时，恢复到“证据可复用、检查未完成”状态。
5. **交接/轮换事务：** 无未决工具写入时，基于最新事件水位保存 Handoff 和 Transition 意图；新 Attempt、领域 SessionBinding 与 `TransitionCompleted` 原子提交；之后创建新 SDK session 并记录 `SessionBound`。创建中断或新 session ID 未知时，原业务状态与 Handoff 仍是恢复点。
6. **裁决事务：** gate 与 RuleEvaluator 只读已提交材料，写 `VerdictDecided`、需要的 `ExclusionChanged` 和报告投影；对同一 task/attempt/evaluator 输入 digest 重复裁决幂等。

默认不自动重试有副作用的工具。首版 ProgramQuery 只承诺只读语义；编译/索引命令如果会产生文件，必须在隔离工作区进行，且不得作为 Agent 任意执行的工具暴露。

## 4. 恢复决策表

| 中断位置 / 观察到的状态 | 恢复动作 | 禁止动作 |
|---|---|---|
| `QueryRequested` 已提交，工具结果未记录 | 写 `QueryInDoubt` 标记不确定；核对后端是否已完成，若只读且幂等可重试并保留原 query 链 | 直接把空结果当无缺陷，或对有副作用查询盲目重放。 |
| 大文件已原子改名，SQLite 未登记 Evidence | 文件作为孤儿；可按 digest 核对并完成原请求，或清理 | 仅凭文件存在向 Agent 返回 Evidence ID。 |
| Evidence 元数据/事件已提交，`CheckUpdated` 未提交 | 复用已存 Evidence；重新按 Rule 检查证明条件，再写更新 | 重跑工具并生成不一致事实，或直接标 complete。 |
| `CheckUpdated` 已提交，SDK 消息尚未结束 | 业务检查保持已提交状态；新 Attempt 读投影和 Handoff，核对 SDK 历史后续查 | 依据不完整 SDK 消息回退已提交 Evidence。 |
| Handoff/Transition 已提交，新 session 未绑定 | 从 Handoff 重试新 session；旧 session 保留只读；若发现孤立 SDK session，记录诊断 | Resume 旧历史冒充轮换完成。 |
| SDK transcript 丢失，业务状态完好 | 记录 `SessionUnavailable`，从有效 Handoff 创建新 session | 虚构同一历史、删除未查项或重复发布旧排除。 |
| 租约过期，但旧进程仍试图执行工具 | 工具服务和存储拒绝旧 epoch；新持有者核对未决 Query | 两个执行者共同写同一 SDK 历史。 |
| Verdict 已提交，报告缓存缺失 | 从事件/Evidence 重建报告，保持原 verdict ID 和作用域 | 再调用模型取得“同一”结论。 |

## 5. Handoff 与 Exclusion 的安全迁移

Handoff 的 protected set 由 `condition_id / fact_id / counter_id / unresolved_id / open_check_id / exclusion_id` 组成。新 Handoff 的集合可以增加；减少任一条目时，必须存在已提交的 `CheckUpdated`、`FactRecorded` 或 `ExclusionChanged` 事件，载明解决依据和 EvidenceRef。仅用模型自然语言“已解决”不能删除。交接校验还核对事件水位不倒退、Evidence 引用存在、快照/Profile/规则版本一致、partial 不升级为 complete。

新 AnalysisSnapshot 对旧 Exclusion 不做隐式复用；旧记录可作为历史提示显示，但状态设为 `needs_review`，只有在新快照上重新取证并裁决，才可建立新 valid Exclusion。相同快照内，CandidateKey 的任何组成部分未知或发生变化时也不得抑制候选。

## 6. 事件序列示例（不规定缺陷语义）

下表 C1/C2 是 SPEC 001 的**历史事件序列示意**，其中 `investigating`/`verifying` 表示 `role_running` 阶段的不同 role ID；S2 应使用注入的测试规则复现同类状态迁移，以本节通用状态表为准。下面只列业务事件，SDK 流式消息不替代其中任何一项。相同 `analysis_id` 的 `seq` 严格递增；括号中的条件表示可选分支，而不是同一事务外的隐式写入。

| 场景 | 预期顺序与检查点 |
|---|---|
| C1 正例 | `AnalysisCreated → AnalysisStatusChanged(active) → CandidateProposed → CandidateBound → TaskCreated → TaskPhaseChanged(investigating) → AttemptCreated(investigator) → SessionLeaseAcquired → TaskStarted(investigator) → QueryRequested → QueryStarted → QueryFinished → EvidenceRecorded → FactRecorded → CheckUpdated`；六项完成后 `ClaimRecorded → AttemptEnded → TaskPhaseChanged(verifying) → AttemptCreated(verifier) → SessionLeaseAcquired → TaskStarted(verifier) → AssessmentRecorded → AttemptEnded → TaskPhaseChanged(gating) → VerdictDecided(confirmed) → TaskPhaseChanged(terminal) → TaskExecutionChanged(completed)`。租约释放事件在各 Attempt 结束时出现；每次实际查询重复中段事件，不要求一次查询完成所有检查。 |
| C2 排除后轮换 | 与 C1 相同的调查/验证前缀；`VerdictDecided(refuted)` 与 `ExclusionChanged(valid)` 同事务。轮换中的角色若需跨会话继续，使用 `HandoffSaved → TransitionCreated → AttemptCreated → TransitionCompleted → SessionBound`；再次发现完全相同 CandidateKey 时读取现有排除并保留反证。 |
| 原文已保存但检查未更新 | `QueryRequested → QueryStarted → QueryFinished → EvidenceRecorded` 后中断；恢复先核对摘要，再写 `FactRecorded → CheckUpdated`。不能倒填不存在的旧 SDK 消息。 |
| 请求后结果未知 | `QueryRequested → (QueryStarted) → QueryInDoubt`；核对/安全重试后才可 `QueryFinished`。失败或无法核对时保留未完成 Check 和 `inconclusive` blockers。 |
| SDK 历史丢失 | `AttemptEnded(interrupted) → HandoffSaved → SessionUnavailable → TransitionCreated → AttemptCreated → SessionLeaseAcquired → TransitionCompleted → SessionBound`；新 Attempt 取得租约，既有 Evidence/Check/Exclusion 投影保持。若已有有效 Handoff 可直接引用，不复制旧内容。 |

`VerdictDecided(inconclusive)` 可在证据不足、门禁失败或预算耗尽时写入，必须携带 `blockers[]`；非法 EvidenceRef 不能列入 support/counter。事件重放按 `seq` 检验每个状态转换，缺少必要前置事件时报完整性错误，而不是从最终状态猜测经过的步骤。

## 7. 验收映射

| 要点 | 对应场景 |
|---|---|
| 单写租约和陈旧执行者 | KC-05；SPEC 001 的 AC-09 为可选示例 |
| 请求/证据/检查事务与中断 | KC-06、KC-07 |
| transcript 丢失与新会话交接 | KC-08 |
| protected set 与排除失效 | KC-09、KC-10、KC-11 |
