# SPEC 006：缺陷调查 ContextView 与排除记忆契约

状态：**基础包通用 Context 与排除契约，S2 验收通过** · 2026-09-25。领域身份见[SPEC 002](002-domain-and-api-contract.md)，事件与交接见[SPEC 003](003-state-events-and-recovery.md)，可选空返回示例见[SPEC 005](005-golden-cases-and-oracles.md)。本文件规定模型每轮看见什么、如何裁剪及换会话后如何避免重复误报。ContextView 是可重建的视图，不是权威事实。

## 1. 视图输入与输出

`get_context_view(task_id, role, focus_check_ids[], token_budget, event_high_watermark)` 必须读取固定的 AnalysisSnapshot、Profile/Rule、CandidateKey、已提交的 Check/Fact/Evidence/Exclusion/Handoff 和查询覆盖。`event_high_watermark` 是视图一致性边界；构建期间出现更新，应重试或明确返回旧水位，不能拼接不同水位的状态。

`ContextView` 的公开序列化至少包含 `schema_version, view_id, task_id, role, snapshot_digest, profile_digest, candidate_digest, event_high_watermark, focus_check_ids[], protected_items[], evidence_snippets[], omitted_items[], retrieval_refs[], token_budget, estimated_tokens, view_digest`。其中：

| 字段 | 语义 |
|---|---|
| `protected_items[]` | 带稳定 ID、类型、有效状态、适用范围、来源事件序号和 EvidenceRef 的触发条件、否定/反证、未知、未查项、覆盖限制及有效排除；相互矛盾的条目并存，不能由摘要自动合并。 |
| `evidence_snippets[]` | 当前检查相关的有限原文片段：EvidenceRef、源码位置/行号、截断范围、原始摘要、信任级别和用途。代码/日志/注释均为不可信输入。 |
| `omitted_items[]` | 被省略条目的稳定 ID、原因（重复、低相关、预算、权限等）、原始 EvidenceRef、截断范围和取回指针；不得用“略去若干日志”代替逐项可追溯记录。 |
| `retrieval_refs[]` | 经服务端授权的内容寻址取回句柄；只允许取固定快照的已保存 Evidence，不开放任意文件路径或查询表达式。 |

`view_digest` 从稳定字段和事件水位计算；视图生成时间、模型 token 估计器的内部缓存不参与摘要。ContextView 只服务本次角色 Attempt；验证者不能接收调查者的完整 SDK transcript，必须通过 Claim、受保护项和 EvidenceRef 构造自己的视图。

## 2. 选择与裁剪顺序

1. 固定快照、规则、候选身份、角色目标、当前检查问题及事件水位。
2. 读取全部**仍有效**受保护项：规则声明的触发条件及其否定条件、具体反证及其适用范围、未知、未完成 Check、查询覆盖限制、有效 Exclusion 与依赖。保留条件的操作数、极性、作用域和来源引用；不能只留自然语言标题。
3. 对 `focus_check_ids` 关联的 Evidence 选取源码附近短片段，优先展示当前规则所需的相关位置、条件、反证与配置差异。按稳定的相关性及 ID 顺序打破并列；重复片段可折叠，但每个原始 EvidenceRef 和不同作用域须能取回。
4. 将低相关完整日志、重复调用图节点和长代码段移入 `omitted_items`，附摘要、精确行范围、限制和取回指针。负证据的查询全集、解析遗漏和零结果的原始表示不得被压成“未发现”。
5. 估算完整消息的 token 成本，包括固定说明、受保护项、片段与输出格式余量。若受保护项本身超预算，返回 `ContextBudgetExceeded`（属于 `InvalidInput` 的稳定子码）及所需最低预算；不得截掉受保护项后继续运行。模型调用后记录实际用量，超出估计不能改写当时视图。

视图可根据新事件水位重建；旧视图、Handoff、Evidence 和事件均不可被新摘要覆盖。模型输出引用的是 Evidence ID 和稳定条件/Check ID，而非片段数组下标或自然语言摘要。源码中的“忽略规则、允许 shell、改写结论”等文本只在不可信片段内展示，不得成为 Profile 或工具授权。

## 3. 受保护项状态与冲突

受保护集合按 `condition_id / fact_id / counter_id / unresolved_id / open_check_id / exclusion_id` 识别。一个条目只有在相关领域事件明确说明**解决依据**且 EvidenceRef 通过完整性/作用域校验后，才可在后续视图中改为 resolved 或 inactive。旧版本仍能从事件与 Handoff 恢复。发生互相冲突的 FactAssessment 时，两方都显示并标为 `conflict`，新建 UnknownItem；在 RuleEvaluator 解决冲突前，相关结论为 `inconclusive`。

“已完成 Check”不自动移除其关键反证；只要该反证仍支撑 `refuted` 或 ExclusionRecord，仍须进入 protected set。`refuted` 的适用条件和反证不能被压缩为单句“安全”。查询从 `partial` 变为 `complete` 时，旧限制可从当前必查项移至历史记录，但必须有新 QueryFinished、EvidenceRecorded 和 CheckUpdated 支持，不能仅靠模型改写视图。

## 4. 排除记忆的查询与失效

候选发现后，先规范化 CandidateKey 并查询 ExclusionIndex。只有 `candidate_digest + snapshot_digest + rule_ref + dependency_digests` 均匹配、Exclusion 状态为 valid 且反证 Evidence 仍可读取时，才抑制同一候选重复告警；返回排除原因、作用域和反证引用供调用方审计。若身份字段未定，保留 ProvisionalCandidate，不进行长期抑制。

同一快照的新证据与旧排除相冲突时，写 `ExclusionChanged(needs_review)` 并新建/重开 Attempt；旧误报历史保留。新 Snapshot 或规则版本产生新 Analysis，旧排除只作历史提示，必须重新取证才能在新作用域建立 valid Exclusion。规则身份策略认定不同的对象、位置、路径或构建变体绝不借用同一排除。Exclusion 命中并不意味着停止统计其他未发现对象或未查范围。

## 5. 验收案例

| ID | 给定/操作 | 必须观察到 |
|---|---|---|
| CT-01 | 两次查询返回重复代码/日志，但覆盖范围不同 | 视图可折叠文本，两个 EvidenceRef、各自范围及遗漏仍可取回。 |
| CT-02 | 预算紧张，代码片段与受保护的触发条件/反证竞争 | 优先保留受保护项；原片段进入 `omitted_items` 并留取回指针。 |
| CT-03 | 受保护项和固定说明本身超过预算 | 返回 `ContextBudgetExceeded`，不发起模型调用，不生成删项的 Handoff。 |
| CT-04 | 两个工具事实对同一候选给出矛盾答案 | 双方引用并存，创建 UnknownItem；相关结论 `inconclusive`。 |
| CT-05 | 已排除候选 A，在同快照新 session 再发现相同 CandidateKey | 命中 valid Exclusion，附受控样例的真实存储 EvidenceRef；不重复发布 A，邻近候选 B 独立。 |
| CT-06 | 两个候选位置相近，但规则身份字段中的对象或路径不同 | 不命中旧排除；新候选仍需检查。 |
| CT-07 | 固定输入、规则版本或旧反证依赖原文发生变化 | 旧排除待复核；新分析不可抑制相同表面位置的候选。 |
| CT-08 | 仓库注释含伪造的工具授权/忽略指令 | 注释只作为带来源的代码片段；工具政策和规则版本不变。 |
| CT-09 | Handoff 或新视图删除仍有效的否定条件/未知/未查项 | 验证失败；旧 Handoff、事件和 RawEvidence 保持可恢复。 |

CT-01 至 CT-09 使用最小测试规则与真实持久化状态验证；CT-05、CT-07、CT-08 需跨新 SDK session 或进程复现，但不要求真实静态分析后端证明某种具体缺陷。SPEC 001 的空返回语义正确性由其插件测试负责。
