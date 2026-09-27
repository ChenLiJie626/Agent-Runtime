# SPEC 002：基础包领域对象与公开 API 契约

状态：**规则无关契约经 S2 验证；0.2.x 公开 API 经 S3 本地发行验证** · 2026-09-25。受 [SPEC 000](000-runtime-kernel.md) 和 [SPEC 007](007-release-and-extension.md) 约束。SPEC 001 的字段示例不是本文件的必填条件。

## 1. 设计边界

基础包只认识“一个固定分析范围内，某规则提出的候选、检查、证据、角色产物和裁决”。规则作者决定候选身份由哪些规范化字段组成、要检查什么、如何解释证据；程序查询后端决定可用操作及精度；模型执行器决定如何运行角色。核心不假定 Base/Head 对比、C/C++、空返回、source/sink 两位置或固定六项检查。

对外领域对象使用本包类型或稳定协议，不暴露 Claude SDK 消息、Joern 图节点、SQL 行。所有持久记录携带 schema_version；未知主版本拒绝读取。运行 ID 为不透明服务生成值；内容摘要由规范 UTF-8 JSON 的稳定字段计算。ID 负责引用，digest 负责完整性，二者不可互换。

## 2. 最小领域模型

| 对象 | 基础包负责的字段与不变量 | 由扩展者定义的部分 |
|---|---|---|
| `AnalysisSnapshot` | repository/scope 身份、固定输入摘要、Profile/工具政策摘要、可选构建变体及版本；创建后不可暗改 | 单提交、多提交、Base/Head、编译数据库或非代码材料的具体字段 |
| `RuleSpec` | rule ID/版本、检查定义、所需能力、评价器引用；版本变更产生新分析范围 | 缺陷条件、检查问题、证据要求和规则参数 |
| `Profile` | 角色配置、Prompt/Skill 摘要、工具政策、预算及规则引用的固定组合 | 各角色 Prompt 和授权工具 |
| `CandidateIdentity` | 规则命名空间、快照、稳定身份载荷的规范摘要；未知关键字段时标 provisional | 调用点、对象、路径、资源或其他能区分候选的身份字段及规范化器 |
| `CandidateTask` / `CheckItem` | task/attempt ID、检查状态、证据引用、覆盖、未知及未查原因；执行状态与裁决状态分离 | 需要哪些 Check、完成该 Check 的证明阈值 |
| `QueryRequest` / `QueryOutcome` | 注册操作、授权范围、幂等键、快照/政策、状态、原文摘要、覆盖和诊断 | 操作名、参数 schema、解析精度及后端能力 |
| `RawEvidence` / `EvidenceRef` | 服务生成 ID、不可变原文摘要、来源、快照、查询和引用用途；所有读取校验完整性 | 原文具体格式和规则解释 |
| `FactAssessment` | 极性、来源层级、作用域、EvidenceRef 和不确定项，模型解释不得冒充工具事实 | predicate_kind 的规则命名空间与语义 |
| `Claim` / `Assessment` | 角色/Attempt 归属、主张或反证、支持/反驳引用与未查项；不是最终裁决 | 角色特定的输出 schema 与细分问题 |
| `ContextView` / `Handoff` | 事件水位、受保护项、短片段、省略与取回引用、预算及轮换关系 | 按规则补充的条件/反证类型和相关性排序 |
| `ExclusionRecord` | 精确候选摘要、快照/规则版本、反证依赖、作用域、valid/needs_review/revoked | 哪类反证足以建立排除 |
| `RuleDecision` / `FindingReport` | verdict、范围、证据、blockers、评价器版本、已发现集合的覆盖与执行失败 | 确认/排除所需的规则逻辑 |

`Coverage` 至少包含 universe_description、examined_description、completeness（complete/partial/unknown）、omissions 和 basis_refs。`complete` 必须有可核查的搜索边界；`partial` 必须说明限制。零结果不能自动作为“无缺陷”反证。

`QueryOutcome.status` 为 complete、partial、timeout、failed、unsupported 或 denied。成功/部分结果即使为空也保存原始表示；失败和超时只产生诊断/可恢复原文，不自动生成能完成检查的 Evidence。`CheckItem.complete` 是规则检查达到声明的证明阈值，不等于工具进程退出码为零。

## 3. 扩展协议

| 端口 | 调用方提供 | 基础包保证 |
|---|---|---|
| `CandidateIdentityPolicy` | 从候选材料生成规范化身份、指出缺失字段 | 相同身份幂等；provisional 不进入长期排除 |
| `RuleSpec` / `CheckPlanner` | 检查清单、依赖、角色关注点 | 为每个候选建立正确清单和状态；不同规则不互相污染 |
| `ProgramQuery` | read_only、supported_operations、参数 schema、快照/范围校验、实际覆盖 | 查询请求/结果/原文/Evidence 的统一生命周期 |
| `AgentExecutor` | 能力声明、角色执行、结构化产物、会话/中断机制 | 按任务需要协商能力，绑定 SessionService，不信任模型自报证据 |
| `RuleEvaluator` | 对同一固定输入的确定性规则裁决 | 通用门禁先校验引用、版本、覆盖和角色独立性；不足时只允许 inconclusive |
| `Storage` | 事件、Evidence 和投影持久化实现 | 事务、幂等、哈希核对及恢复语义与 SPEC 003 一致 |

基础包可以内置空返回规则作为演示插件，但运行内核不得以它生成所有候选的检查清单、解释所有 `predicate_kind` 或替代调用方提供的评价器。

## 4. 对外操作与失败语义

| 操作 | 输入 | 输出/约束 |
|---|---|---|
| `create_analysis` | Snapshot、Profile、注册端口及能力要求 | 固定 analysis ID 与摘要；版本/能力冲突明确失败 |
| `propose_candidate` | analysis、候选载荷、规则引用、发现来源 | 精确 Task 或 provisional；稳定去重 |
| `run_candidate` / `run_analysis` | task/analysis、预算、角色策略 | Attempt/事件/结构化角色产物；执行与裁决分离 |
| `query_program` | 结构化 QueryRequest | QueryOutcome 和合规时的 EvidenceRef；先存原文再返 ID |
| `get_context_view` | task、role、关注检查、预算、水位 | 可溯源且有界的视图；受保护项不能静默截掉 |
| `checkpoint_handoff` / `rotate_session` | task、有效 Attempt、受保护状态 | 不可变 Handoff 和新绑定；旧历史不被覆盖 |
| `evaluate_candidate` | 已提交事实、Claim、Assessment、注册评价器 | 通用门禁 + 规则评价器的 scoped decision；缺口为 inconclusive |
| `get_report` | analysis | 候选状态、证据/反证、未查项、覆盖和执行失败 |

公开错误至少区分 InvalidInput（含 ContextBudgetExceeded）、Conflict、CapabilityUnavailable、PolicyDenied、StaleSnapshot、EvidenceIntegrityError、SessionUnavailable、BackendFailure 和 StorageFailure；异常文本不作为稳定 API。partial/timeout 等工具业务结果优先用 QueryOutcome 表示。

## 5. 验收与兼容

S2 用两条**抽象测试规则**验证检查清单和评价器可替换；用假后端验证证据、覆盖及恢复；用真实 SDK 探针验证执行协议。成功接入第二规则不要求它发现真实缺陷。SPEC 001/005 的空返回 Oracle 属于 S4 插件验收。

当前代码已增加 `FixedSnapshot`、`CandidateIdentity`、`CandidateIdentityPolicy`、Profile 角色定义、版本绑定的候选级 `CheckPlanner`、查询操作 schema 与多评价器注册；旧 `AnalysisSnapshot`、`CandidateKey` 和 `null_return_rule()` 继续作为兼容示例。通用查询故障窗口、Handoff 保护项与多规则裁决已有 S2 测试；S3 的[包外扩展样例](../examples/external_consumer.py)和[公开 API 审查](../docs/08-S3-发布与兼容.md)已完成本地验证。新增可选事件字段后写入封套的 schema_version 为 1.1，读取端接受 1.0/1.1，未来 minor 与未知 major 拒绝。迁移时提供显式适配层或主版本变更，保留已存记录的读取路径。
