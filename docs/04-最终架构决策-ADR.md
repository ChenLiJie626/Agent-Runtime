# ADR 010：缺陷挖掘 Agent Runtime 的最终技术方案

状态：**架构决定保留；实施范围按基础包 SPEC 修订** · 2026-09-25。依据：[需求基线](00-需求基线与范围.md)、[runtime 开源架构调研](03-Agent-Runtime开源架构调研.md)、[规格导航](../specs/README.md)和[SPEC 000](../specs/000-runtime-kernel.md)。Claude SDK + 自有领域控制层的选型不变；原 0.1.0 纵向切片已演进为 0.2.0 本地发行线，验证结果见[S3 记录](09-S3-验收记录.md)。

## 决定

首版采用 **Claude Agent SDK Python + 本项目自有的确定性缺陷分析控制层**。SDK 负责模型、工具调用循环、消息流、会话与结构化输出；本包负责规则展开、候选任务、角色协同、Evidence、SessionService、业务事件、缺陷专用 Context、排除记忆、恢复和 VerdictGate。首个执行适配器以 Claude 为默认推荐，但核心领域 API 不依赖 Claude SDK 类型。包以一个 Python 发行物提供，Claude 适配器放在可选依赖组；CLI 只是薄入口，供其他项目直接导入使用。[Claude SDK Python 参考](https://code.claude.com/docs/en/agent-sdk/python)

**融合设计思想，不叠加多个 Agent 框架。** 借鉴 Conductor 的 session 映射与单写规则、OpenHands 的事件/状态分离、Deep Agents 的分层扩展、audit 的窄范围调查与反证验证；它们均不成为首版运行时依赖。[Conductor 会话设计](https://github.com/microsoft/conductor/blob/main/docs/workflow-syntax.md#session-continuity-session_key) · [OpenHands 持久化](https://docs.openhands.dev/sdk/guides/convo-persistence) · [Deep Agents 架构](https://github.com/langchain-ai/deepagents/blob/main/libs/ARCHITECTURE.md) · [audit](https://github.com/evilsocket/audit)

## 为什么选这条路线

| 方案 | 符合需求的部分 | 首版决定与原因 |
|---|---|---|
| **直接 Claude Agent SDK + 领域控制层** | 官方循环、工具、显式 resume、会话与子 Agent；本包能精确控制每次任务和证据事务 | **采用。** 架构层数最少，缺陷语义和公开 API 由本项目定义。 |
| [Microsoft Agent Framework](https://github.com/microsoft/agent-framework/blob/main/python/packages/claude/README.md) 的 ClaudeAgent + Workflow | 可引用包，支持 Claude SDK、Session/ContextProvider、工作流 checkpoint | **暂不依赖。** Claude 包当前文档仍按 `--pre` 安装；[checkpoint 在 superstep 边界保存](https://learn.microsoft.com/en-us/agent-framework/workflows/checkpoints)，无法单独保证工具原文写入与候选状态更新的原子关系。本包仍要实现领域账本，会增加第二套工作流状态。未来工作流复杂到多阶段并发时可再评估。 |
| [Microsoft Conductor](https://github.com/microsoft/conductor) | Claude SDK provider、路由、session_key、checkpoint | **仅借鉴。** 主要交付 CLI/YAML 工作流；Claude provider [标为实验性](https://github.com/microsoft/conductor/blob/main/docs/providers/comparison.md)。直接作为库底座会引入与本包公开 API 不一致的工作流模型。 |
| [Claude Agent Framework](https://github.com/uukuguy/claude-agent-framework) | Claude SDK 上的角色、Prompt、流程模板 | **仅借鉴角色配置。** [包元数据标为 Alpha](https://github.com/uukuguy/claude-agent-framework/blob/main/pyproject.toml)，现有文档未给出缺陷业务状态及崩溃恢复契约。 |
| [OpenHands Software Agent SDK](https://github.com/OpenHands/software-agent-sdk) | 完整事件存储、Conversation、工具与上下文压缩 | **借鉴状态模型。** 它已有自己的 Agent 循环；整体采用会改变 Claude SDK 优先的路线，且仍缺候选反证语义。 |
| [Deep Agents](https://github.com/langchain-ai/deepagents) / LangGraph | 中间件、子 Agent、checkpoint、历史转储 | **借鉴扩展方式。** 其循环由 LangChain/LangGraph 驱动；直接套在 Claude SDK 上会重复调度、历史与检查点。 |
| [evilsocket/audit](https://github.com/evilsocket/audit) | 窄任务、独立反证、到达性门槛 | **借鉴缺陷工作流。** 它是审计应用，不是供第三方导入的通用领域基础包；不照搬八阶段。 |

上述“无法单独保证”和“会增加”是根据本项目原子性要求与框架 checkpoint 粒度作出的设计判断，并非宣称这些项目不能扩展。

## 首版包边界

```text
调用方应用 / 薄 CLI
    │  Rule + CandidateIdentity + Snapshot + ProgramQuery + RuleEvaluator + Agent config
    ▼
DefectRuntime（稳定的公开领域 API）
    ├─ ProfileResolver / CandidatePlanner
    ├─ RoleCoordinator（确定性流程，首版串行）
    ├─ SessionService（RoleAttempt、SessionBinding、Transition）
    ├─ EventJournal + TaskProjection（业务状态）
    ├─ EvidenceService + ExclusionIndex（原始材料与排除）
    ├─ ContextViewBuilder + HandoffValidator
    └─ VerdictGate + RuleEvaluator + ReportBuilder
         │
         ├─ AgentExecutor port → Claude SDK adapter（首个实现）
         ├─ ProgramQuery port → 调用方注入的结构化只读后端
         │                      Joern/Clang PoC 为后续插件探索
         └─ Storage ports → SQLite + 内容寻址原文存储（首版）
```

核心 API 只接受本包的 Snapshot、Rule、Candidate、EvidenceRef、Claim、Assessment、Report 等领域对象。SDK 消息、SQL 表、Joern 图节点形状只存在于适配器或原始证据中。对调用方开放规则、程序查询、角色配置和存储端口；不要求调用方实现一套 Agent 循环。

### 执行与角色

默认提供两个模型角色：

1. **Investigator（调查者）**：围绕一个精确候选逐项填充必查条件；可以发起受控程序查询，输出 Claim、支持/反驳引用和未查项。
2. **Verifier（独立验证者）**：在**另一个顶层 SDK 会话**中接收 Claim 作为待证伪对象，通过原始 EvidenceRef、有限代码切片和限制信息验证条件能否同时成立，主动寻找保护、对象不同一或不可达的反证，输出 Assessment。

候选发现、检查分派、重试、预算、去重、裁决与报告由普通程序负责；不设置“总控 Agent”。专项 Agent 可按明确的缺口、输入/输出和工具授权注册。一个候选的规则定义检查由同一 Investigator 按缺口推进。`VerdictGate` 检查证据真实性、版本与覆盖，规则绑定的确定性 `RuleEvaluator` 判断本缺陷类型的触发/反证关系；两者都不能凭两个模型的同意证明程序路径可达。语义材料不足时返回 `inconclusive`。[audit 的窄任务/反证流程](https://github.com/evilsocket/audit)

### Claude SDK 适配器

首版每个 RoleAttempt 独占一个 `ClaudeSDKClient` 生命周期，以便流式事件、工具审批与中断；续查时用明确的 session ID 创建新客户端，不依赖“最近一次会话”。SDK 的 [Python 参考](https://code.claude.com/docs/en/agent-sdk/python)区分 `query()` 的单次调用和可交互、可中断的 `ClaudeSDKClient`。角色工具面只暴露受控的程序查询和证据读取；固定 `setting_sources=[]`、MCP 来源及工具策略，在锁定版本上验证其效果。`allowed_tools` 是自动批准清单，真正的拒绝须结合 `tools`/`disallowed_tools`、`can_use_tool`、MCP 服务授权和只读运行环境。[权限字段](https://code.claude.com/docs/en/agent-sdk/python) SDK 0.1.59 及以前的 `setting_sources=[]` 行为与预期不同，版本联测必须覆盖这一点。[官方说明](https://code.claude.com/docs/en/agent-sdk/python)

SDK `output_format` 只负责输出结构；模型给出的 EvidenceRef、快照、条件和覆盖仍由本包核查。SDK 自带 session resume/fork 只处理对话：同一调查续查可以 resume，验证者必须新建会话，跨会话压缩必须先保存领域 Handoff 再开新会话。首版不把 fork 用作压缩或独立验证。[Sessions](https://code.claude.com/docs/en/agent-sdk/sessions)

### 持久化与 SessionService

使用 SQLite 保存分析快照、候选/检查、RoleAttempt、SessionBinding、业务事件、证据元数据、排除记录、Handoff 与报告投影。原始查询产物按内容哈希保存；小结果可在 SQLite 内保存，较大结果先写临时文件并原子改名，再在 SQLite 事务中登记引用。文件与数据库之间的崩溃窗口由哈希核对和孤儿文件清理处理，不能宣称跨两者的原子事务。首版单进程、候选串行；对每个 SDK session 使用独占租约和单调代际号防止旧执行者继续写入。之后如需并发，仅允许**不同候选且不同 session**并行。

业务事件与 SDK 对话严格分开。事件以分析内单调序号记录 `TaskCreated / QueryRequested / EvidenceRecorded / CheckUpdated / HandoffSaved / AttemptEnded / VerdictDecided` 等状态迁移，TaskProjection 可重建。查询请求先写入幂等键；Evidence 原文落盘且元数据/完成事件提交后才向模型返回 ID。发生崩溃时，`requested` 但无完成事件的查询进入 `in_doubt`；只读且幂等的后端可核对或重试，不把空缺解释为“没有缺陷”。这个业务事务边界由本包保证，不委托 SDK 或通用 workflow checkpoint。

`resume` 前校验源码/编译快照、Profile、工具政策、session 归属和单写租约。原 SDK transcript 不可用时记录续接失败，从已校验 Handoff 建新会话，未查项仍未查。`rotate` 必须在无未决工具写入的安全点进行：保存 Handoff 和 Transition，再新建 SDK session；旧材料保留。跨主机恢复首版以**业务 Handoff 新会话**为标准路径；若确需保留完整 SDK 对话，再评估官方 `SessionStore` 镜像能力。[Claude Sessions](https://code.claude.com/docs/en/agent-sdk/sessions) · [SessionStore 字段](https://code.claude.com/docs/en/agent-sdk/python)

### 缺陷专用 Context 与排除记忆

从不可变 RawEvidence 和业务事件生成每次调用的 `ContextView`，不把聊天摘要当证据。视图分四层：固定快照/规则/候选；**受保护的触发条件、反证、未知、未查项和排除范围**；当前缺口相关的短代码片段与定位；可按 EvidenceRef 取回的完整原文。按规则检查缺口选取查询材料，再做重复代码/日志折叠。省略和截断必须带来源、覆盖、可取回指针；负证据须保留查询全集与限制。[OpenHands Condenser](https://github.com/OpenHands/software-agent-sdk/blob/main/openhands-sdk/openhands/sdk/context/condenser/base.py) · [Deep Agents 压缩实现](https://github.com/langchain-ai/deepagents/blob/main/libs/deepagents/deepagents/middleware/summarization.py)

压缩只更换模型视图；原始证据、业务事件和旧 Handoff 保留。`HandoffValidator` 比较前后受保护条目，每条消失都必须有新 EvidenceRef 和显式状态迁移。`ExclusionRecord` 使用**规则版本＋规则生成的精确候选键＋固定快照＋反证依赖哈希**标识作用域；同快照再遇到同一候选时复用，源码/编译/规则/依赖变化时失效待查。身份不足的候选只能暂存为 provisional，不做跨会话永久排除。

### 程序语义后端

基础包只要求 `ProgramQuery` 的结构化操作、授权、原文归档、覆盖范围和失败语义。已有 **Joern + Clang** 探索保留为 SPEC 001 空返回插件材料；能否证明对象同一性、保护条件、路径可达和 Base/Head 差异，由该插件自己的 Oracle 验证。不能证明的检查保持 partial/unknown，结果为 `inconclusive`。`codebadger` 和 CodeQL CLI 仍按后续插件需要及许可单独评估。详见[分析后端调研](01-开源调研与复用决策.md)。

## 首版交付与门禁

| 阶段 | 产出 | 通过条件 |
|---|---|
| S1 基础契约 | SPEC 000/002/003/004/006、对象/事件 schema、端口与能力表 | 通用状态迁移与失败路径有预期结果。 |
| S2 基础能力联调 | 最小测试规则、可控后端、真实 Claude SDK 协议探针 | KC-01 至 KC-13 可复现；规则/后端不写死，崩溃后可恢复。 |
| S3 可引用发行物 | 稳定公开 API、可安装包、第三方规则/查询后端接入 | 第二规则和第二后端无需修改核心状态机。 |
| S4 规则插件与评测 | SPEC 001/005 等指定规则及真实项目分析 | 各插件按自身语义 Oracle 和覆盖要求验收。 |

**只有当实测表明**单进程确定性协调器已成为复杂工作流瓶颈，才重新比较 Microsoft Agent Framework 或其他工作流引擎。将通用工作流引擎接入时，业务事件/Evidence 仍是权威状态；替换执行或编排适配器，不迁移成聊天历史事实。

## 尚未由架构决定代替的产品输入

首个真实 C/C++ 仓库及编译方式、是否允许运行目标代码、源码外发政策、费用上限、数据保留期和发行许可仍须由实际部署场景给出；这些不改变上述**SDK + 领域控制层**的首版架构，而影响工具策略、存储配置和评测数据。见[需求基线](00-需求基线与范围.md)。
