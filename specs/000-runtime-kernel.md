# SPEC 000：可复用的缺陷挖掘 Agent Runtime 基础包

状态：**基础包范围已重整，S2 的 KC-01 至 KC-13 均已通过** · 2026-09-25。规范导航见[README](README.md)，具体契约见 [SPEC 002](002-domain-and-api-contract.md)、[SPEC 003](003-state-events-and-recovery.md)、[SPEC 004](004-adapter-capabilities-and-query.md) 与 [SPEC 006](006-defect-context-and-memory.md)。

## 1. 目标与边界

第三方 Python 项目导入本包，注入自己的规则/Profile、候选身份策略、程序查询后端、规则裁决器及 Agent 执行器，然后创建、运行或恢复缺陷调查。基础包提供**执行与状态内核**，不内置特定缺陷的发现算法或语义证明。Claude Agent SDK 为首个可选执行适配器；领域对象不依赖它。默认协同角色为 Investigator 与独立 Verifier，后续可注册有明确输入/输出的专项角色。

当前交付包含：角色编排、SessionService、事件/证据存储、缺陷调查 Context、Handoff、排除记忆、能力协商、只读程序查询端口、通用引用与覆盖门禁、报告。具体规则的触发条件、检查清单、证明阈值和 `confirmed/refuted` 语义由调用方的 RuleSpec、CandidateIdentity、RuleEvaluator 和 ProgramQuery 决定。

**当前不交付：** 对“新增空返回”等指定规则的跨函数路径证明、内置的全库调用者枚举、Joern/Clang 规则算法、真实项目准确率指标、自动修复、分布式调度。SPEC 001/005 和既有 PoC 是后续规则插件示例，不参与本 SPEC 的 P0 验收。

## 2. P0 基础契约

### K-01 包装与扩展边界

- 核心包在未安装 Claude SDK、Joern、Clang 的环境中可导入、实例化和运行纯领域测试；SDK/静态工具类型不进入公开领域 API。
- 调用方可注册规则定义、候选身份生成、只读 ProgramQuery、确定性 RuleEvaluator 和 AgentExecutor；缺少所需能力时在执行前得到明确 CapabilityUnavailable，不得静默改用 Prompt 保证。
- 首版单进程、SQLite 与内容寻址 Evidence 即可，但公开端口应允许替换执行器、程序查询和存储。添加第二种规则或后端不应修改核心状态机。

### K-02 角色协同与能力

- 确定性协调器管理任务/Attempt，不让“总控 Agent”决定租约、证据入库或最终裁决。默认调查者提出 Claim，独立验证者在不同会话中核查并主动寻找反证；角色的 Prompt、工具政策、预算和输出 schema 由 Profile 固定。
- AgentExecutor 报告真实支持的结构化输出、工具限制、独立会话、显式 resume、事件流、中断和用量；任务所需能力逐项协商。工具权限由执行适配器与服务端共同校验，不靠模型承诺。
- 可增加专项角色，但每个角色必须有稳定输入/输出、授权工具、会话隔离和失败语义。首版不要求任意角色图或模型自治派单。

### K-03 Session 与恢复

- Analysis、CandidateTask、RoleAttempt、SessionBinding、SDK session 和 Handoff 分离。绑定快照/Profile/工具政策/工作目录；同一 SDK 历史单写，租约带 epoch，旧写入者被隔离。
- `resume` 使用显式 session ID，并校验归属与绑定；`rotate` 在安全点保存经验证的 Handoff/Transition 后创建新会话。SDK transcript 丢失时从领域状态继续，未查项仍未查。
- 预算、超时、取消、执行失败与缺陷结论分开记录；`completed + inconclusive` 是合法状态。异常或重试不得擦除已提交事件与 Evidence。

### K-04 证据、查询与通用门禁

- ProgramQuery 只接受注册的结构化操作和授权范围；返回状态、覆盖全集/已查范围、遗漏、诊断与原始材料。`complete/partial/timeout/failed/unsupported/denied` 含义统一，但具体操作由后端插件声明。
- 工具请求先以幂等键持久化，再执行；原文按内容哈希保存并校验后才生成 Evidence ID。未确认请求为 `in_doubt`，空结果没有完整搜索边界时不能作反证。
- 通用 VerdictGate 核对引用存在性、摘要、快照/候选作用域、覆盖和独立角色来源。缺陷语义交给注入的 RuleEvaluator；基础包不根据模型一致意见或工具名字自行判定漏洞。

### K-05 缺陷调查 Context 与排除记忆

- ContextView 从不可变 Evidence 与业务事件按当前问题重建。长日志与重复代码可折叠；条件、否定、反证、未知、未查项、覆盖限制和引用受保护。每个省略项都可定位并按权限取回。
- Handoff 保存事件水位和受保护项；删除仍有效条目必须有显式状态迁移与合法 Evidence。受保护内容超预算时返回错误，不悄悄丢弃。
- 精确 CandidateKey 的有效 Exclusion 依赖快照、规则版本和反证摘要；同一候选换会话不重复发布，邻近候选或新快照不得被连带排除。身份不足的 provisional 候选不做长期排除。

### K-06 报告与可观察性

- 对每个候选区分 `confirmed/refuted/inconclusive` 与执行状态，列出证据、反证、未查项、覆盖范围、工具失败及裁决器版本。创建 Analysis 前必须注册规则绑定的 RuleEvaluator；缺失时返回 `CapabilityUnavailable`，不启动调查。已启动调查若证明不充分，只能 `inconclusive`，不能借用内置示例规则推断。
- 审计记录角色/Attempt、SDK session、工具查询、耗时、可用的用量/成本和状态迁移；未提供的用量明确标为 unavailable。只报告**已发现集合**的完成比例，不将其说成全库覆盖。

## 3. S2 基础能力验收

S2 使用一个**最小测试规则**：调用方注入两项抽象检查、一条精确候选与一条相邻候选、可控 ProgramQuery 和确定性 RuleEvaluator。测试只验证运行机制，不声称发现真实缺陷。真实 Claude SDK 联测验证会话、结构化输出、权限隔离和工具协议；规则语义由可控材料验证。Joern/Clang PoC 可保留，但不是完成门槛。

| ID | 场景 | 通过条件 |
|---|---|---|
| KC-01 | 不安装 SDK/Joern 导入包并接入假执行器/假后端 | 核心 API 可用；缺失可选能力仅在使用时明确失败。 |
| KC-02 | 注册不同检查清单的第二条测试规则 | 无需改核心状态机；任务按各自 RuleSpec 建立检查与候选身份。 |
| KC-03 | 同一精确候选重复发现；身份未定的 provisional 候选 | 精确候选幂等，provisional 不生成长期排除；相邻候选独立。 |
| KC-04 | 调查者完成后启动独立验证者 | SessionBinding 不同，验证者只接收 Claim、授权 ContextView 与 EvidenceRef。 |
| KC-05 | 同一 SDK session 被并发写入或错误角色/快照 resume | 租约或绑定校验拒绝，已提交状态不损坏。 |
| KC-06 | ProgramQuery 返回 partial 空结果或 timeout | 保存原文/诊断和遗漏；Check 不自动完成，不把零结果解释为安全。 |
| KC-07 | 原文保存、事件提交、检查更新的任一边界中断 | 恢复按幂等键/哈希核对；已提交 Evidence 可复用，未确认操作保持未决。 |
| KC-08 | 轮换或丢失 SDK transcript | 新会话从有效 Handoff 恢复条件、反证、未知和未查项；不虚构旧对话。 |
| KC-09 | 压缩试图删除受保护反证/未知，或预算无法容纳 | 拒绝保存或返回预算错误；旧 Handoff/Evidence 仍可用。 |
| KC-10 | 同快照换会话再遇已排除精确候选 | 命中有效 Exclusion 并附反证；相邻候选及新快照不被抑制。 |
| KC-11 | Agent 引用不存在、跨快照或损坏的 Evidence | 通用门禁阻止确定结论并记录错误；模型推荐不能越权。 |
| KC-12 | AgentExecutor 缺少任务要求的工具隔离、结构化输出或 resume | 启动前给出具体能力差异；未实现的流/中断/用量不能宣称已支持。 |
| KC-13 | 同一完整输入重复交给注入的 RuleEvaluator | 相同裁决和 input digest；缺少绑定评价器在创建 Analysis 前失败；已启动调查材料不足时为 `inconclusive`。 |

## 4. 交付与阶段

- **S1 契约：** SPEC 000/002/003/004/006 对通用对象、端口、失败与恢复一致；示例规则被明确移出核心门禁。
- **S2 基础能力纵向联调：** KC-01 至 KC-13 有可复现测试与真实 SDK 协议探针；发现的实现缺口列入差距表。规则语义后端不阻塞 S2。
- **S3 可引用发行物：** 第三方可依文档实现第二规则与第二 ProgramQuery，完成公开 API 稳定性和打包验证。
- **S4 规则插件/评测：** 先按 [SPEC 008](008-defect-quality-evaluation.md)建立跨规则的可复现盲评和质量指标；SPEC 001/005 等各自完成语义证据与真实项目评价，不回写成基础包的隐含前置条件。

现有代码已从 0.1.0 空返回纵向切片演进为 0.2.0 本地发行线，通用身份、动态检查规划、查询参数契约、Profile 角色和多评价器已有测试；包入口真实 SDK 合成规则与丢失 transcript 恢复探针均已通过。S2 逐项结果见[验收记录](../docs/07-S2-基础能力验收记录.md)；S3 公开 API、兼容与包外第二规则/后端的本地验证见[记录](../docs/09-S3-验收记录.md)。
