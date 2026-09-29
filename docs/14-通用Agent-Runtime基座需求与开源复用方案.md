# 通用 Agent Runtime 基座：需求、运行时分层与开源复用方案

- 状态：**方向讨论稿，不代表已经实施**
- 日期：2026-09-29
- 适用范围：当前 `agent-runtime` 仓库从“缺陷分析产品内核”回归“可供多类业务 Agent 二次开发的通用基座”

## 0. 结论先行

当前项目虽然名称是 `agent-runtime`，但公开对象、协调流程和大部分适配器已经围绕程序缺陷分析展开。它更准确的定位是一个**证据优先的缺陷分析 Domain Pack**，还不是通用 Agent Runtime。

新的目标应定义为：

> 提供一个以 Agent SDK 为执行引擎、以开放 Skill 格式为扩展单元、以可恢复运行和安全策略为基础的 Python Agent Runtime；用户可以在其上组合自己的 Agent、Skill、Tool、Workflow 和领域数据模型，而不需要修改 Runtime 内核。

本方案提出五项核心决定：

1. **不把现有 `DefectRuntime` 直接改名为通用 Runtime。** 先抽出真正通用的运行、状态、Artifact、策略和扩展能力，再把现有缺陷能力保留为 `defect-analysis` Domain Pack。
2. **采用开放的 [Agent Skills 规范](https://agentskills.io/specification) 作为 Skill 包格式。** 用户 Skill 和预置 Skill 使用同一种格式，不发明私有的 `SKILL.md` 方言。
3. **首选基于 [Microsoft Agent Framework](https://github.com/microsoft/agent-framework) 继续建设，而不是继续自研完整 Agent 循环。** 优先复用其 Agent、Session、Middleware、Workflow/Checkpoint、OpenTelemetry 和 Python `SkillsProvider`，本项目聚焦 Runtime 门面、Skill 治理、Artifact/Provenance、策略与 Domain Pack。
4. **保留 Claude Agent SDK，但把它降为可替换的执行适配器。** `agent-framework-claude` 可以承接现有 Claude Agent SDK 路径，但该包当前仍标记为 Beta，因此必须先做兼容 PoC；OpenAI、Anthropic 直连或其他 Provider 不应改变上层 Agent 定义。
5. **Skill 导入不等于 Skill 获得执行权限。** Skill 的发现、安装、启用、读取资源和执行脚本必须分别受版本锁定、信任检查、Policy 和审批控制。

推荐的总体结构是：

```text
业务应用
  └─ Domain Pack：证据收集 / 判定裁决 / 语义建模 / 缺陷分析 / 用户自定义
       ├─ Agent 定义与 Workflow
       ├─ Skill 集合与领域 Tools
       ├─ 输入输出 Schema / Evaluator
       └─ 领域 Policy
            │
            ▼
通用 Agent Runtime（本项目）
  ├─ AgentRuntime / WorkflowRuntime / SubAgentRuntime
  ├─ AgentContext / ToolRuntime / SkillRuntime / ModelRuntime
  ├─ StateStore / ArtifactStore / Provenance
  ├─ Retry / Budget / Policy / Approval
  └─ Event / Trace / Observability
            │
            ▼
Microsoft Agent Framework（首选上游）
  ├─ Agent loop / provider abstraction
  ├─ Session / Middleware / HITL
  ├─ Workflow / Checkpoint
  ├─ SkillsProvider
  └─ OpenTelemetry
            │
            ▼
模型与执行适配器
  ├─ Claude Agent SDK
  ├─ OpenAI / Anthropic / 其他模型
  └─ MCP / 本地 Tools / Sandbox
```

## 1. 为什么需要重新划分边界

### 1.1 当前项目已经是具体业务 Runtime

当前发行物名为 `defect-agent-runtime`，公开 API 包含 `RuleSpec`、`CandidateTask`、`CheckItem`、`Claim`、`Assessment`、`RuleDecision`、`ExclusionRecord`、`ProgramQuery` 和 `DefectRuntime`。默认协调流程固定为候选发现、调查、独立验证和规则裁决；Clang、Joern、CodeQL、UAF 与 ValidationRecord 也已进入包内。

这些设计对缺陷分析是有价值的，但它们不是所有 Agent 都需要的共同抽象：

- 证据收集 Agent 不一定存在“缺陷候选”和“规则裁决”；
- 判定 Agent 可能围绕 Case、Issue、Standard 和 Decision 工作；
- 语义建模 Agent 更关心 Entity、Relation、Constraint、Provenance 和 ModelVersion；
- 内容 Agent、运维 Agent 或数据分析 Agent 甚至不需要 Claim/Assessment 二阶段流程。

因此，继续给现有类型加可选字段会形成“所有领域都伪装成缺陷分析”的抽象泄漏。

### 1.2 现有实现中仍有可复用资产

不是推倒重来，而是区分“通用机制”和“业务语义”。

| 当前能力 | 通用性判断 | 新位置建议 |
|---|---|---|
| `codec.py` 的规范化 JSON、内容摘要 | 高 | Runtime Core 的 Identity/Serialization 工具。 |
| `SQLiteStore` 的追加事件、投影重建、内容寻址 Artifact、幂等预留 | 高，但事件名仍含 Candidate | 抽成 Event Journal、Projection Store、Artifact Store；业务事件由 Domain Pack 注册。 |
| SessionBinding、租约、单写、轮换与恢复 | 中高，但依赖 `DefectRuntime` 和 Check/Exclusion | 保留状态机思想；底层会话交给上游框架，跨 Agent/Provider 的绑定、租约和运行快照由本项目提供。 |
| `AgentExecutor` 能力声明 | 高，但请求/返回类型绑定 Claim/Assessment | 改成通用 Run/Message/Artifact/StructuredOutput 契约，领域输出由 Schema 参数化。 |
| `ClaudeAgentExecutor` | 中；SDK 装配可复用，Prompt 与输出解析是缺陷专用 | Claude Provider/Agent SDK 适配交给 Agent Framework 或薄适配层；缺陷 Prompt/Schema 移入 Domain Pack。 |
| ContextView/Handoff 的受保护项思想 | 高，但当前保护项是条件、反证、未查项 | Core 提供不可丢失字段和压缩协议；各 Domain Pack 定义自己的受保护 Schema。 |
| ValidationRecord、隔离执行与资源观测 | 中高 | 泛化为 Sandbox Execution Record；缺陷复现语义留在缺陷 Pack。 |
| Candidate、Rule、Check、Claim、Assessment、VerdictGate | 领域专用 | `defect-analysis` Pack；不要进入通用 Core。 |
| Clang/Joern/CodeQL/UAF/生命周期规则 | 领域专用 | 缺陷 Pack 的 Tools、Skills、Evaluators。 |

### 1.3 新方向不是否定原 ADR

[ADR 010](04-最终架构决策-ADR.md) 的目标是以最少框架层实现可审计的缺陷分析，因此选择“Claude Agent SDK + 自有缺陷控制层”是合理的。现在目标变为通用 Agent 开发基座，决策权重发生了变化：

- Skill 兼容与导入从附属能力变成核心能力；
- Provider/Agent SDK 可替换性从未来需求变成基础要求；
- 通用 Middleware、Workflow、HITL、Telemetry 不应全部自研；
- 缺陷 Evidence 语义不再适合充当全系统的领域模型。

因此需要新增方向 ADR，而不是悄悄修改旧 ADR 的含义。旧实现和验收仍是 `defect-analysis` Pack 的有效资产。

## 2. 产品定位与首要场景

### 2.1 产品定位

通用 Agent Runtime 是一个**应用拥有、可嵌入、可扩展的 Python 运行基座**，不是面向终端用户的聊天产品，也不是新的模型 SDK。

它负责：

- 装配和版本化 Agent；
- 管理一次 Run 和跨 Run 的 Session；
- 为 Agent 发现并按需加载 Skills；
- 暴露受策略约束的 Tools/MCP；
- 持久化事件、Artifact、运行快照和必要的 Provenance；
- 支持暂停、审批、恢复、取消、重试和观测；
- 让 Domain Pack 注册自己的 Schema、Workflow、Evaluator 与默认 Skills。

它不负责：

- 自研 LLM 推理协议；
- 把所有业务抽象成统一 Claim/Verdict 模型；
- 内置无限自治的“Agent 群”；
- 默认执行任意互联网 Skill 中的代码；
- 在 P0 阶段提供多租户 SaaS、Skill 市场、向量数据库或可视化编排器。

### 2.2 目标用户

| 用户 | 想完成的工作 | Runtime 应提供的能力 |
|---|---|---|
| Agent 开发者 | 开发证据收集、判定、语义建模等 Agent | 声明 Agent、模型、输出 Schema、Skills、Tools、Workflow 与 Policy。 |
| Skill 作者 | 编写可复用流程、知识、模板和脚本 | 标准 `SKILL.md`、本地校验、版本与兼容性声明、测试夹具。 |
| 平台开发者 | 嵌入业务系统、替换模型或存储 | 稳定 Runtime API、Provider/Store/Sandbox 适配器、事件流。 |
| 安全与运维人员 | 控制数据、网络、工具和成本 | Policy、审批、审计、隔离、预算、追踪和撤销。 |
| 领域专家 | 维护判定标准和领域流程 | 不改 Core 即可发布 Domain Pack 或 Skill 版本。 |

### 2.3 三类首要验证场景

#### A. 证据收集 Agent

输入一个研究目标和允许的数据源，生成带来源、时间、定位、摘要、冲突和覆盖限制的 `EvidenceBundle`。Agent 可以使用检索和文档工具，但不能把自己的总结当作原始来源。

该场景验证：Skill 按需加载、Tool/MCP、Artifact/Provenance、长任务恢复、来源冲突和结构化输出。

#### B. 判定/裁决 Agent

输入 Case、适用标准、证据包和约束，输出 `DecisionDraft`：待判问题、适用标准、支持材料、反对材料、未知、推理摘要、置信与需人工确认项。高影响决定必须暂停并请求人工批准。

该场景验证：多角色或 Workflow、独立复核、HITL、受保护字段、审计和 Evaluator。这里的“裁决”是通用决策工作流，不在 Core 中固化法律结论语义。

#### C. 语义建模 Agent

输入文档或已有数据模型，输出带版本的 `SemanticModel`：概念、实体、关系、属性、约束、命名冲突、来源映射和未解决问题，并允许校验器拒绝不符合 Schema 的结果。

该场景验证：大上下文、结构化 Artifact、迭代版本、领域校验器和可复用建模 Skills。

只有当这三种不同工作负载能够使用同一 Core、通过组合而非修改 Core 完成时，才能称为通用基座。

## 3. 核心概念模型

### 3.1 Agent、Skill、Tool、Workflow 和 Domain Pack 的边界

| 概念 | 含义 | 是否直接执行动作 | 生命周期 |
|---|---|---:|---|
| `AgentDefinition` | 一个角色的模型、指令、输出 Schema、可用能力和策略快照 | 通过 Agent SDK 调度 | 可版本化、可实例化为 Session。 |
| `SkillPackage` | 按需加载的流程知识、参考材料、模板和可选脚本 | 指令本身不执行；脚本经 Policy/Sandbox 执行 | 可导入、校验、安装、启用、撤销。 |
| `Tool` | 具有明确输入输出和副作用等级的可调用能力 | 是 | 在每次 Run 前解析并授权。 |
| `Workflow` | 普通程序控制的步骤、分支、并行、暂停和恢复关系 | 调用 Agent 或 Tool | 有独立 Checkpoint；不等于聊天历史。 |
| `DomainPack` | 某类业务 Agent 的完整扩展包 | 组合上述对象 | 独立发布和版本化。 |
| `Run` | 对某个 Agent/Workflow 的一次执行 | 是 | 有开始、事件、暂停和终止状态。 |
| `Session` | 跨多个 Run 的上下文与状态容器 | 否 | 可恢复、轮换或关闭。 |
| `Artifact` | 输入、工具结果、文档、模型或报告等不可变/版本化材料 | 否 | 内容寻址或外部引用。 |
| `Provenance` | Artifact 的来源、生成方式、时间、版本和依赖关系 | 否 | 随 Artifact 保存并可审计。 |

### 3.2 Core 中保留的最小对象

建议 Core 只定义以下稳定对象族：

- Agent：`AgentSpec`、`AgentVersion`、`AgentRef`；
- 执行：`Run`、`RunStatus`、`RunEvent`、`RunResult`、`Interrupt`；
- 会话：`Session`、`SessionBinding`、`CheckpointRef`；
- 能力：`SkillRef`、`ToolRef`、`CapabilitySnapshot`；
- 材料：`ArtifactRef`、`ArtifactMetadata`、`Provenance`；
- 安全：`PolicyDecision`、`ApprovalRequest`、`Budget`；
- 输出：`StructuredOutputRef`、`ValidationResult`；
- 扩展：`DomainPackManifest`。

`EvidenceBundle`、`DecisionDraft`、`SemanticModel`、`CandidateTask` 等属于 Domain Pack，不进入 Core。

## 4. Runtime 分层与组件职责

### 4.1 四个平面

通用基座应按职责分成四个平面，避免每个 Runtime 都自行保存状态、重试和打日志。

| 平面 | 组件 | 核心职责 |
|---|---|---|
| 执行平面 | AgentRuntime、WorkflowRuntime、SubAgentRuntime、ToolRuntime、SkillRuntime、ModelRuntime | 推进一次执行，产生事件和 Artifact。 |
| 状态平面 | StateStore、CheckpointStore、ArtifactStore、MemoryStore、Provenance | 保存可恢复状态与业务材料。 |
| 治理平面 | Policy、Approval、Retry、Budget、CapabilitySnapshot | 决定什么可以执行、执行多少次、使用多少资源。 |
| 观测平面 | Event、Trace、Log、Metric、Evaluation | 重建状态、诊断性能、衡量质量。 |

组件依赖方向必须单向：上层 Runtime 可以调用下层 Runtime；下层不能反过来读取某个 Domain Pack 的业务对象。

```text
WorkflowRuntime
  ├─ AgentRuntime
  │    ├─ ModelRuntime
  │    ├─ ToolRuntime
  │    ├─ SkillRuntime
  │    └─ SubAgentRuntime ──> child AgentRuntime / child WorkflowRuntime
  └─ ToolRuntime

所有执行组件共同依赖：
  AgentContext + State/Artifact + Policy/Retry/Budget + Event/Trace
```

### 4.2 AgentRuntime

`AgentRuntime` 是面向调用方的单 Agent 执行门面，但**不重新实现一套模型 Tool loop**。实际循环优先委托上游 Agent Framework 或具体 Agent SDK。

职责：

- 解析 `AgentRef`，固定 `AgentVersion`；
- 建立 Run、SessionBinding 和 CapabilitySnapshot；
- 通过 AgentContext 注入本轮输入、状态、Skills、Tools、Policy 和预算；
- 调用上游 Agent SDK，统一流式事件；
- 协调 ModelRuntime、ToolRuntime、SkillRuntime 和 SubAgentRuntime；
- 校验结构化输出并登记 Artifact；
- 处理暂停、审批、取消和终态；
- 输出统一 `RunResult`。

不负责：

- 业务 Workflow 的步骤拓扑；
- 领域结论是否正确；
- 直接执行 Tool 或 Skill 脚本；
- 把 transcript 当作唯一状态。

一个 Agent Run 只能有一个控制者。AgentRuntime 可以被 WorkflowRuntime 调用，也可以直接被应用调用。

### 4.3 WorkflowRuntime

`WorkflowRuntime` 执行确定性的步骤图或函数式流程。它是“程序决定下一步”的地方；模型可以提供某一步的输出，但不能暗中改变 Workflow 的拓扑、审批点或预算上限。

职责：

- 执行顺序、条件、有限并行、join、handoff 和循环；
- 调用 AgentRuntime、ToolRuntime 或子 Workflow；
- 在安全点生成 Checkpoint；
- 管理外部等待、人工审批和恢复；
- 传播取消、截止时间和剩余预算；
- 区分步骤输出、Workflow 最终输出和中间进度。

关键边界：

- Workflow Checkpoint 保存“走到哪里、哪些请求待处理”；
- Agent Session 保存“这个 Agent 对话过什么”；
- Artifact 保存“产生了什么业务材料”；
- 三者不可互相替代。

工作流恢复时，不应因为某个节点有聊天历史就推断该节点已经提交业务结果；以 Checkpoint 和 Artifact/Event 的事务水位为准。

### 4.4 AgentContext

`AgentContext` 不是新的长期数据库，也不等同于 Prompt。它是**某次 Run 在某一时刻可见能力与数据的受控视图**。

建议包含：

| 内容 | 说明 |
|---|---|
| Identity | tenant/app、AgentVersion、Run、Session、Workflow/Step、parent run。 |
| Input | 本轮用户输入和结构化参数。 |
| Instructions | 平台、Agent、Domain Pack、Skill 的分层指令及来源。 |
| Capabilities | 本轮已解析的 SkillRef、ToolRef、ModelRef 和能力矩阵。 |
| State view | Session/Workflow/Domain 状态的只读或受控代理。 |
| Artifact view | 可读 Artifact 的引用，不默认内联全部原文。 |
| Memory view | 本轮检索到的短期/长期记忆及来源。 |
| Policy | 权限、数据边界、审批和副作用规则。 |
| Budget | 剩余 token、费用、时间、轮数、Tool call、子 Agent 数。 |
| Runtime handles | 事件发送、Artifact 写入、取消信号等受控句柄。 |

约束：

- AgentContext 按 Run/Step 创建，尽量视为不可变快照；预算消耗和新增 Artifact 通过受控服务更新；
- 不把数据库连接、Provider client、明文密钥直接暴露给 Skill；
- Prompt 只由 AgentContext 的一部分渲染而来；
- Domain Pack 可以声明受保护字段，Context 压缩时必须保留或显式解决；
- 子 Agent 默认获得裁剪后的 child context，而不是父 Context 的完整复制。

### 4.5 SubAgentRuntime

`SubAgentRuntime` 不是第二套 AgentRuntime，而是**创建和治理子 Run 的策略层**。实际子任务仍由 AgentRuntime 或 WorkflowRuntime 执行。

职责：

- 校验可委派的 Agent/Workflow allowlist；
- 创建 parent-child Run 关系和独立 Context；
- 分配子预算、最大深度、最大 fan-out 和并发数；
- 决定使用 agent-as-tool、handoff 还是独立 child workflow；
- 隔离 Session、Tools、Skills、Workspace 和敏感 Artifact；
- 汇总结构化结果，不自动把完整子会话注入父会话；
- 传播取消与超时；
- 防止循环委派和无限递归。

建议默认值：

- 子 Agent 不继承父 Agent 的全部工具权限；
- 子 Agent 不继承父 Session，除非 Agent 定义明确允许；
- 只传任务所需 ArtifactRef 与 Handoff；
- 子预算从父预算中预留，未用部分可归还；
- 子 Run 的事件和 Trace 独立，但通过 `parent_run_id` 关联。

### 4.6 ToolRuntime

`ToolRuntime` 是所有可调用动作的统一执行边界，包括 Python Function、MCP Tool、Provider 托管 Tool、本地命令、外部 API 和 Domain Tool。

职责：

- Tool 注册、发现、版本和输入输出 Schema；
- 参数校验与规范化；
- 在执行前调用 Policy/Approval；
- 绑定幂等键、超时、重试和取消；
- 按副作用等级选择进程内、远程或 Sandbox executor；
- 将原始结果持久化为 Artifact，再向 Agent 返回受限视图；
- 记录 ToolStarted/ToolCompleted/ToolFailed 等事件和 Trace span；
- 对输出大小、敏感信息和内容类型做限制。

Tool 的副作用分类建议至少包括：

| 等级 | 示例 | 默认策略 |
|---|---|---|
| Pure | 计算、Schema 校验 | 可自动执行。 |
| Read | 读取已授权文件、数据库查询、检索 | 限定范围后可自动执行。 |
| Write reversible | 创建草稿、写临时工作区 | 需要显式授权，可配置审批。 |
| External side effect | 发消息、提交工单、更新生产数据 | 默认人工审批和幂等键。 |
| Code execution / privileged | Shell、脚本、安装包、读密钥 | Sandbox、最小权限，通常需审批。 |

`allowed_tools`、模型 Tool choice 或 Skill 建议都不能绕过 ToolRuntime 的权威 Policy。

### 4.7 SkillRuntime

`SkillRuntime` 管理可移植的流程知识与资源，不拥有 Agent loop。Skill 本身也不是 Tool；只有 Skill 中声明的脚本在获准后才通过 ToolRuntime/Sandbox 执行。

职责分成两部分：

1. 控制面：发现、导入、验证、安装、版本锁定、启用、禁用、撤销和冲突处理；
2. 运行面：advertise、load instructions、read resource、request script execution。

与其他组件的边界：

- Skill 指令进入 AgentContext，但不能改变平台 Policy；
- Skill 资源通过安全路径读取并成为 Artifact 或 Context 片段；
- Skill 脚本转换成受 ToolRuntime 治理的执行请求；
- Skill 激活和资源读取产生 Event/Trace；
- AgentVersion 锁定 Skill digest，正在运行的 Session 不自动漂移到新版。

详细生命周期和安全要求见第 6 节。

### 4.8 State 与 Artifact

这是最容易混淆的一层。建议分成五类存储：

| 存储 | 保存什么 | 权威性与生命周期 |
|---|---|---|
| Run/Event Store | Run 状态迁移和可重建事件 | 执行状态的权威来源，追加式。 |
| Session Store | 对话历史、Session metadata、Provider session mapping | 会话连续性来源，不是业务事实来源。 |
| Workflow Checkpoint Store | 节点状态、待处理请求、控制流位置 | Workflow 恢复来源。 |
| Artifact Store | 输入、Tool 原文、结构化输出、报告、模型等材料 | 业务材料来源；内容寻址或版本化。 |
| Memory Store | 可跨 Run 召回的偏好、知识或摘要 | 辅助上下文；必须有来源和作用域。 |

Artifact 最少需要：

- `artifact_id` 与内容 digest；
- media type/schema；
- producer：Run/Agent/Tool/Workflow step；
- input dependencies；
- source URI/locator 和采集时间；
- sensitivity/tenant/retention；
- immutable/versioned 标记；
- 完整性校验和可读权限。

Core 使用 `Artifact` 和 `Provenance`；证据收集 Pack 可以进一步定义 `EvidenceRecord`，语义建模 Pack 可以定义 `ModelArtifact`。不要在 Core 中假设所有 Artifact 都是证据。

事务原则：

- 先持久化原始 Tool/Model 结果并取得 ArtifactRef，再提交“步骤完成”事件；
- Artifact 已写入但事件未提交时，恢复流程通过幂等键和 digest 核对；
- 事件已提交但 Artifact 缺失视为完整性错误，不靠重新问模型补造；
- 大文件和数据库无法共享单事务时，明确 orphan/in-doubt 回收协议。

### 4.9 ModelRuntime

`ModelRuntime` 是一次模型调用的适配与治理层，不是完整 AgentRuntime。Agent 的多轮 Tool loop 由上游 Agent SDK/Framework 管理，但每次模型使用都应能被统一观测和预算控制。

职责：

- `ModelRef` 到 Provider client/model 的解析；
- 能力协商：Tool calling、structured output、vision、reasoning、streaming 等；
- 统一请求元数据、超时、取消、usage 和错误分类；
- 模型选择、允许的 fallback 和区域/数据策略；
- 结构化输出的 Provider 配置与本地二次校验；
- 模型调用 Trace span 与成本记账；
- 可选的缓存、限流和熔断。

边界：

- ModelRuntime 不负责业务重试决策；它报告可重试错误和已消耗 usage，由 Retry/Budget Policy 决定下一步；
- fallback 不能静默改变安全、数据驻留或能力语义；
- AgentVersion 可以声明模型策略，但 Run 必须记录实际使用的 Provider/model；
- Claude Agent SDK 这类“自带完整 loop 的 Agent runtime”通过 AgentRuntime adapter 接入，而不是硬塞成单次 ModelRuntime 调用。

### 4.10 Retry 与 Budget

Retry 和 Budget 是贯穿所有 Runtime 的治理能力，不应散落在各适配器的 `try/except` 中。

#### Retry

每个操作声明：

- 是否可重试；
- 幂等键或补偿动作；
- 最大次数、退避和 jitter；
- 哪些错误可重试；
- 是否消耗新预算；
- 重试后如何判定原请求是否已经成功。

默认原则：

- 纯读取在后端保证幂等时可自动重试；
- 模型限流/瞬时错误可有限重试，但每次 usage 计入预算；
- 外部写入在没有幂等键或状态核对时不得自动重试；
- Validation/Policy 拒绝不是瞬时错误，不重试；
- 结构化输出修复属于受限的语义重试，必须有独立次数上限；
- Workflow 节点重试不能悄悄重复已经提交的子 Run 或 Tool side effect。

#### Budget

预算至少支持：

- wall-clock deadline；
- 模型 token/费用；
- Agent turns；
- Tool calls；
- Workflow steps；
- 子 Agent 数、深度和并发；
- Artifact/上下文大小；
- Retry 次数。

预算采用树形账户：Workflow/父 Run 持有总预算，Agent、Tool 和子 Agent 获得子额度。所有消耗产生事件；预算不足时返回明确的 `budget_exhausted`，不能伪装为业务完成或无结果。

### 4.11 Event、Trace 与 Observability

这些概念必须分开，否则系统会错误地用日志恢复状态，或把完整业务数据全部送入遥测后端。

| 信号 | 用途 | 是否必须持久 | 是否可采样 | 是否可驱动恢复 |
|---|---|---:|---:|---:|
| Event | Run/业务状态迁移，如 RunStarted、ToolCompleted、ArtifactCommitted | 是，关键事件必须持久 | 否 | 是 |
| Trace/Span | 一次执行的调用树、耗时、错误和因果关系 | 生产环境按策略 | 可以 | 否 |
| Log | 面向人和机器的诊断文本 | 按级别/保留策略 | 可以 | 否 |
| Metric | 聚合计数、延迟、成本、成功率、队列深度 | 聚合保存 | 是 | 否 |
| Evaluation Result | 对输出质量、安全和行为的评测 | 作为 Artifact/Event 保存 | 不应随意采样基准集 | 不直接驱动恢复 |

建议统一关联字段：

- tenant/application；
- trace/span；
- run/session/parent run；
- workflow/step/attempt；
- AgentVersion；
- Skill digest；
- Tool/Model/Provider version；
- ArtifactRef；
- PolicyDecision/Approval；
- budget account。

安全约束：

- Trace/Log 默认只存摘要和引用，不默认复制 Prompt、Skill 全文、密钥或敏感 Artifact；
- Event schema 要版本化；未知事件必须可跳过或显式拒绝，不能静默错误投影；
- Observability exporter 失败不能破坏业务事务，但必须有丢失告警；
- Event Journal 是恢复权威，OTel backend 不是。

### 4.12 一次 Run 的标准时序

```text
1. 调用方提交 AgentRef + input + SessionRef
2. AgentRuntime 解析并锁定 AgentVersion
3. 解析 Model/Skill/Tool，形成 CapabilitySnapshot
4. Policy 预检，Budget 开户，RunStarted 进入 Event Store
5. ContextBuilder 生成 AgentContext
6. 上游 Agent SDK 发起模型调用，ModelRuntime 记录 usage/trace
7. 模型请求 Skill：SkillRuntime load/read，事件与上下文更新
8. 模型请求 Tool：ToolRuntime 校验 → Policy/Approval → 执行
9. Tool 原始结果先写 Artifact，再提交 ToolCompleted
10. 如需子 Agent：SubAgentRuntime 分配 child context/budget 并启动 child Run
11. 如位于 Workflow：WorkflowRuntime 在安全点保存 Checkpoint
12. Agent 产出结构化结果，本地校验并写 Artifact
13. 结算 Budget，提交 RunCompleted/Failed/Cancelled
14. 返回 RunResult；Trace/Metric 异步导出
```

### 4.13 上游复用与本项目自建边界

| 组件 | 优先复用 Agent Framework | 本项目保留 |
|---|---|---|
| AgentRuntime | Agent run/stream、Session、Middleware、Tool loop | Catalog、AgentVersion、CapabilitySnapshot、统一 Run API。 |
| WorkflowRuntime | Workflow、orchestration、checkpoint、HITL | 跨存储提交规则、Domain Pack workflow 注册、统一事件。 |
| AgentContext | Context Provider/Middleware | 通用 Context schema、受保护字段、Artifact/Policy/Budget 视图。 |
| SubAgentRuntime | agent-as-tool、handoff、orchestrator | child policy、预算树、隔离、parent-child 事件。 |
| ToolRuntime | Function Tool、MCP、approval middleware | Tool Registry、副作用分类、幂等、Artifact-first 规则。 |
| SkillRuntime | Python `SkillsProvider` 的发现和渐进披露 | Registry、导入锁定、信任、撤销、Sandbox policy。 |
| State | Agent Session、Workflow Checkpoint 接口 | Event Journal、Projection、Session binding 和存储适配。 |
| Artifact | 不假定上游提供统一业务 Artifact | 内容寻址 Artifact/Provenance 是本项目差异化能力。 |
| ModelRuntime | Provider clients 和能力 | ModelRef/Policy、统一错误/usage、允许的 fallback。 |
| Retry/Budget | 复用可用的 middleware/错误类型 | 跨 Agent/Workflow/Tool 的统一策略和预算树。 |
| Observability | OpenTelemetry 接入 | 关联字段、事件契约、质量指标和数据脱敏。 |

## 5. 通用功能需求

### R1. Agent 定义与版本

- 支持 Python API 和声明式文件两种定义方式；
- 固定模型策略、指令、输出 Schema、Skills、Tools、Middleware、预算和 Policy；
- 每次 Run 保存不可变的 AgentVersion 与 CapabilitySnapshot；
- Agent 可直接运行、作为 Tool 被调用或成为 Workflow 节点；
- 公共 Agent 定义不暴露 Provider SDK 消息类型。

### R2. Provider 与能力协商

P0 至少验证 Claude Agent SDK 和一个非 Claude Provider。统一能力矩阵至少包含 streaming、structured output、tools、MCP、session/resume、cancel、approval、usage、workspace 和 sub-agent/agent-as-tool。

不支持的能力必须在 Run 启动前失败，不能静默降级。

### R3. Run、Session 与恢复

- Run 状态至少包括 `queued/running/waiting_approval/suspended/completed/failed/cancelled`；
- Session 与 Run 分离，一个 Run 固定一个 AgentVersion；
- 支持事件流、超时、取消、重试和幂等；
- 恢复时校验 AgentVersion、Skill digest、Tool policy、Provider 能力和 Workspace；
- 原 transcript 不可用时可从受校验 Handoff 开新会话，但不得伪装原历史已恢复；
- 同一 Provider Session 同时只有一个写入者。

### R4. Tool、MCP 与副作用

- Tool 具有 Schema、版本、来源、副作用、网络与敏感级别；
- 支持 Python Function、MCP、Provider Tool、外部 API 和 Domain Tool；
- 每次 Tool call 记录授权、输入摘要、结果 Artifact、错误和耗时；
- 副作用 Tool 的重试需要幂等语义；
- 默认只读，写入/外部通信/代码执行逐级审批。

### R5. Context、Memory 与 Handoff

- 区分系统/Agent/Skill 指令、Session 历史、Memory、当前工作集和 Artifact；
- Skill 指令不能覆盖平台策略；
- Domain Pack 可声明受保护字段；
- 摘要不是新 Artifact，也不替代原文；
- 长期 Memory 是可选适配器，不把向量数据库设为 P0 前提；
- Handoff 传递最小必要状态，而不是共享全部聊天历史。

### R6. Guardrail、Schema 与 Evaluator

- 输入、Tool 调用和最终输出可配置 Guardrail；
- 结构化输出必须本地校验；
- Domain Pack 可注册确定性 Evaluator 或 LLM Judge，Judge 需标注来源；
- 高影响输出支持强制人工批准；
- Guardrail 失败、业务结论和 Run 失败是不同状态。

### R7. 评测

- 提供可重放的测试 Model/Tool；
- Domain Pack 提供数据集、Evaluator 和门禁；
- Skill 有格式、触发、行为和安全测试；
- Runtime 升级有跨 Provider、恢复、权限和 Skill 兼容回归；
- 模拟测试与真实模型结果分别报告。

## 6. Skill 子系统详细设计

### 6.1 采用开放格式

[Agent Skills 规范](https://agentskills.io/specification)规定 Skill 至少包含 `SKILL.md`，可附带 `scripts/`、`references/` 和 `assets/`；启动时只暴露名称与描述，正文和资源按需加载。Runtime 不添加破坏兼容性的必填 frontmatter。

额外运行信息采用两种方式保存：

- 可移植信息放在标准 `metadata` 中，值保持字符串；
- 安装来源、digest、信任、审批和实际权限保存在 Runtime Registry/锁记录中，不写回用户 Skill。

### 6.2 Skill 生命周期

```text
discovered
  → quarantined
  → validated
  → installed
  → enabled for an AgentVersion
  → loaded in a Run
  → resource read / script requested
  → policy decision / approval
  → executed or denied
  → disabled / revoked / superseded
```

`installed` 不意味着 `enabled`，`enabled` 不意味着脚本获得执行授权。

### 6.3 导入来源与校验

P0 来源：随包安装的 Built-in、本地目录、ZIP。P1 再加入固定 Git commit、MCP Skill archive 和受控组织 Registry。

P0 校验至少包括：

- `SKILL.md` 数量、frontmatter、名称与目录一致性；
- 文件数、单文件大小、总大小和解压后大小限制；
- 拒绝绝对路径、`..`、ZIP slip、Symlink/Junction/Reparse Point 越界；
- 资源与脚本扩展名 allowlist；
- 许可证、兼容性、文件清单和内容 digest；
- 脚本依赖、网络需求和潜在副作用清单；
- Skill 正文和参考材料按不可信指令处理。

自动扫描不能证明 Skill 安全。含脚本、网络、外部写入或凭据需求的 Skill 默认进入人工审查。

### 6.4 身份、版本与冲突

内部身份建议为 `<source>/<publisher>/<name>@<content-digest>`。语义版本用于展示和兼容判断，Run 的真实锁定值是内容 digest。

- Built-in、组织、项目和用户来源均有显式命名空间；
- 默认不采用“后加载覆盖前加载”；
- 如果 Agent 作者选择覆盖，AgentVersion 记录实际 qualified identity；
- Skill 更新不改变正在运行或历史 Session；
- 支持禁用和撤销有风险的 Skill 版本。

### 6.5 Progressive disclosure 与权限

统一四步协议：advertise、load、read resource、run script。

默认策略：

- 加载正文：可信 Skill 自动允许；
- 读取资源：只读且限定 Skill 根，可按信任级别自动允许；
- 运行脚本：默认需审批并进入 Sandbox；
- 网络、写宿主文件、读取密钥、外部通信：单独审批；
- `allowed-tools` 是兼容性/建议字段，不能授予权限。

### 6.6 首批预置 Skills

| Skill | 责任 | 主要产出 |
|---|---|---|
| `research-plan` | 拆分问题、来源计划、停止条件和覆盖限制 | `ResearchPlan` Artifact。 |
| `source-capture` | 保存来源、定位、时间、摘要和原文引用 | `SourceRecord` Artifact。 |
| `evidence-quality` | 检查来源质量、冲突、新鲜度、缺口和不当推断 | `EvidenceQualityReport`。 |
| `structured-decision` | 按问题、标准、支持材料、反对材料、未知组织决定草案 | `DecisionDraft`，不自动赋予最终决策权。 |
| `semantic-modeling` | 提取实体、关系、属性、约束、命名冲突和来源映射 | `SemanticModel`。 |
| `review-packet` | 把高风险或不确定项整理为人工复核包 | `ApprovalRequest`/`ReviewPacket`。 |
| `artifact-reporting` | 将结构化 Artifact 生成可审阅报告 | Markdown/JSON/文档 Artifact。 |
| `skill-authoring` | 创建、检查和测试新的 Agent Skill | 新 Skill 包和验证报告。 |

预置 Skill 只给出流程和输出约定。法律规则、企业政策或本体规范应由独立 Domain Pack 或用户 Skill 提供。

## 7. 开源项目对比

调研快照：2026-09-29。以下判断基于项目官方仓库或官方文档；“适合度”是对本项目新目标的工程判断。

| 项目 | 通用 Runtime | Agent Skills | 状态/工作流 | Provider/SDK | 结论 |
|---|---|---|---|---|---|
| [Microsoft Agent Framework](https://github.com/microsoft/agent-framework) | Agent、Session、Middleware、Tools、Workflow、HITL、OTel；[Python Core 1.19.0](https://github.com/microsoft/agent-framework/blob/main/python/packages/core/pyproject.toml) 标为 Production/Stable | Python [`SkillsProvider`](https://learn.microsoft.com/en-us/agent-framework/agents/skills) 支持文件、代码定义和 MCP archive，渐进披露、资源、脚本、审批和安全校验 | Graph/functional workflow、checkpoint、恢复、并行、handoff | OpenAI、Anthropic、Claude Agent SDK 等；[`agent-framework-claude`](https://github.com/microsoft/agent-framework/blob/main/python/packages/claude/pyproject.toml) 当前为 Beta | **首选上游。** 最贴合“通用基座＋自定义 Skill＋保留 Claude SDK”；先验证 Beta Claude 适配。 |
| [Pydantic AI](https://github.com/pydantic/pydantic-ai) | 强类型 Agent、Schema、Capabilities、Tools、MCP、Evals、OTel | [Harness Skills](https://pydantic.dev/docs/ai/harness/skills/) 支持把 `SKILL.md` 作为按需 Capability；内置 Skills 当前只加载指令，完整资源/脚本需额外能力层 | 支持 Temporal、DBOS、Prefect、Restate 等 durable execution | 模型中立、Provider 广 | **强备选。** 类型化输出和 durable engine 很强；完整 Skill 治理还需补层。 |
| [Deep Agents](https://github.com/langchain-ai/deepagents) | 长任务 harness，含文件、子 Agent、Context、HITL、Tools/MCP | 原生 `SkillsMiddleware`，多来源和 progressive disclosure | 复用 LangGraph persistence/checkpoint | 模型中立 | **快速构建型备选。** 能力完整但 Todo/Filesystem/Subagent 预设较强，会引入 LangChain/LangGraph 全栈。 |
| [OpenAI Agents SDK](https://developers.openai.com/api/docs/guides/agents/sdk) | 轻量 Agent、Tools、handoff、guardrails、sessions、HITL、tracing | [OpenAI Skills](https://developers.openai.com/api/docs/guides/tools-skills) 支持版本化 Skills、local/hosted shell 和 Agent Skills；自托管 SDK 应用仍需 Registry/Policy 层 | Session 和可恢复运行；复杂 Workflow 需应用组合 | OpenAI 最顺滑，也允许自定义 Provider | **OpenAI-first 很合适。** 供应商中立和本地 Skill 治理覆盖不如 MAF 直接。 |
| [Claude Agent SDK](https://github.com/anthropics/claude-agent-sdk-python) | 强项是 Coding Agent 环境、工具、Hooks、MCP、Session、权限和 Workspace | 可结合 Agent Skills/Claude 设置体系，需谨慎隔离设置来源与权限 | SDK Session、resume/fork；业务 Workflow 仍需上层 | Claude/Claude Code runtime 专用 | **继续作为执行适配器。** 现有项目有实测资产，不适合独自承担多 Provider Core。 |
| [OpenHands Software Agent SDK](https://github.com/OpenHands/software-agent-sdk) | Conversation、Event、Workspace、Tools、Agent Server 完整 | 不是以通用 Skill Registry 为主抽象 | 事件持久化和 Workspace 恢复强 | 多模型，中心是软件开发 Agent | **机制参考。** Event/Workspace 值得借鉴；对判定和语义建模过于偏代码 Agent。 |

### 7.1 为什么首选 Microsoft Agent Framework

它覆盖了本项目不应继续自研的通用部分：Agent loop、Session、Middleware、agent-as-tool、handoff、Workflow、checkpoint、HITL、streaming、OTel、多 Provider，以及已经接入审批的 Python `SkillsProvider`。

本项目仍有明确增值空间：

- Agent/Skill/Domain Pack 的发行、锁定和兼容门面；
- 统一 Policy 与 CapabilitySnapshot；
- 内容寻址 Artifact 与 Provenance；
- 当前已验证的单写 Session 绑定和恢复约束；
- 三类首批 Domain Pack；
- 与业务无关的验收套件和安全默认值。

### 7.2 为什么不直接 Fork

推荐“依赖＋薄扩展”，不 Fork：

- 只通过公开 API 组合 Agent、SkillsProvider、Middleware 和 Workflow；
- 缺失且有普遍价值的能力优先向上游贡献；
- 不调用上游私有模块，不复制 Agent loop；
- 用契约测试控制升级；
- PoC 失败时可替换为 Pydantic AI，Domain Pack/Skill 格式保持稳定。

### 7.3 采用前必须完成的 PoC

1. 同一 Agent 定义可分别运行在 Claude Agent SDK 和非 Claude Provider；
2. 用户 Skill 完成 advertise → load → resource → script/deny；
3. Skill 脚本默认审批并在隔离环境运行；
4. Workflow 在审批点暂停，进程重启后从 checkpoint 恢复；
5. Session、Workflow Checkpoint、Event 和 Artifact 状态互不替代；
6. OTel trace 关联 AgentVersion、Skill digest、Tool call 和 Artifact；
7. `agent-framework-claude` 满足工作目录、MCP、结构化输出、resume、取消和权限隔离；
8. 不满足时可回退为“MAF Core + 当前 Claude adapter”，且不影响上层 Runtime API。

## 8. 目标架构与 Domain Pack

### 8.1 Core 产品面

- `AgentCatalog`：注册、版本化和解析 Agent；
- `RunService`：启动、流式读取、暂停、恢复、取消；
- `SessionService`：Session 绑定、租约、轮换和 Handoff；
- `SkillRegistry`：导入、验证、锁定、启用、撤销；
- `ToolRegistry`：Tool/MCP 发现、Schema 与版本；
- `PolicyEngine`：权限、审批、预算和数据策略；
- `EventJournal`：运行事件；
- `ArtifactStore`/`ProvenanceService`：材料和来源关系；
- `Telemetry`/`Evaluation`：观测与评测。

### 8.2 Domain Pack 内容

一个 Domain Pack 至少包含：manifest、Agent 定义、Workflow、默认 Skills、Tools/MCP、输入输出 Schema、Evaluator/Guardrail、Policy 默认值、测试数据和最小示例。

建议首批四个 Pack：

```text
packs/
  evidence-collection/
  structured-adjudication/
  semantic-modeling/
  defect-analysis/        # 现有能力迁移目标
```

## 9. 现有仓库迁移方案

### S0：冻结业务基线

- 保持 `defect-agent-runtime 0.3.x` 可测试、可回放；
- 不继续向 `DefectRuntime` 添加通用概念；
- 本文作为新方向输入，不立即破坏公开 API。

### S1：开源底座 PoC

- 只验证第 7.3 节；
- 不迁移 Clang/Joern/CodeQL；
- MAF 和 Pydantic AI 使用同一组验收用例，保留可比较备选。

### S2：建立新 Core

- 使用新的通用包命名空间；
- 落 Agent/Run/Session/Skill/Tool/Artifact/Policy 最小契约；
- 复用 codec 和 Store 中已证明的机制，但移除 Candidate/Rule 事件名；
- Provider SDK 类型不得进入公开 API。

### S3：Skill 基座与预置 Skills

- 实现导入、锁定、信任和 progressive disclosure；
- 发布第 6.6 节预置 Skills；
- 用恶意 ZIP、Symlink、超大包、未授权脚本、提示注入和重名做安全回归。

### S4：三个通用 Domain Pack

- 实现证据收集、结构化判定和语义建模的最小纵向切片；
- 三者共享 Core，各有自己的 Schema/Evaluator；
- 若加入第三个 Pack 仍需修改 Core，说明抽象不够通用。

### S5：迁移缺陷分析

- DefectRuntime、Candidate、Rule、Check、Claim/Assessment、Clang/Joern/CodeQL 移入 `defect-analysis`；
- 提供兼容门面或保留旧包一个迁移周期；
- 旧测试验证领域语义，新测试验证 Pack 不访问 Core 私有实现。

## 10. P0 验收标准

### 10.1 通用性

- 三个新 Pack 均不修改 Core；
- Core API 不出现 Candidate、Defect、Rule、Verdict 等单一业务词；
- 第三方只依赖公开 API 即可创建第四种 Agent。

### 10.2 Skill

- 可从本地目录和 ZIP 导入标准 Skill；
- Built-in 与用户 Skill 走同一链路；
- Run 锁定精确 digest；
- 资源读取不能越过 Skill 根；
- 脚本默认不在宿主直接执行；
- 更新、冲突、禁用、撤销均有确定行为和审计事件。

### 10.3 运行与恢复

- 至少两个 Provider/Agent SDK 通过同一契约测试；
- Run 可流式输出、取消、等待审批和恢复；
- 崩溃后区分已提交 Artifact、未决 Tool call 和已完成步骤；
- 同一 Session 无并发写入；
- 历史 Run 可重建 Agent/Skill/Tool 版本快照。

### 10.4 安全与质量

- 默认只读、无网络、无隐式密钥；
- 仓库内容、Skill 和 Tool 输出均视为不可信输入；
- 高影响 Tool 明确审批；
- OTel、Event、Artifact 和 PolicyDecision 可关联；
- 每个预置 Skill 有正触发、负触发、边界和安全测试；
- 模拟 Provider 与真实模型结果分开标注。

## 11. 非目标与待确认项

P0 不做公共 Skill 市场、任意互联网一键安装、多租户托管控制面、GUI Workflow Builder、自研模型网关、自研向量数据库、无限 Agent 群或不受限代码执行。

| 问题 | 建议默认值 |
|---|---|
| 首版交付形态 | Python 可嵌入库＋薄 CLI。 |
| 首要 Provider | 保留 Claude Agent SDK，新增 OpenAI/标准 Provider 证明可替换性。 |
| 上游框架 | MAF 首选；PoC 失败再转 Pydantic AI。 |
| Skill 格式 | Agent Skills 标准。 |
| Skill 脚本 | 可导入但默认不自动执行；必须 Sandbox＋Policy。 |
| 用户 Skill 来源 | P0 仅本地目录和 ZIP；远程必须固定 commit/digest。 |
| 默认权限 | 只读、无网络、无密钥；按 Agent/Run 显式增加。 |
| 长期 Memory | 接口先行、实现可选。 |
| 缺陷分析 | 作为成熟 Domain Pack 保留。 |
| 兼容策略 | 新 Core 使用新主版本/发行物；旧包提供迁移期。 |

## 12. 下一步文档产出

在写正式实现前，依次产出：

1. 新方向 ADR：确认 MAF 首选、Agent Skills、缺陷能力下沉；
2. Runtime 分层 SPEC：本文件第 4 节各组件的接口与状态机；
3. 通用对象与事件 SPEC；
4. Skill 安全与生命周期 SPEC；
5. PoC 验收清单；
6. 三个通用 Domain Pack 的最小 Schema 与场景；
7. 现有 API 到新 Core/Pack 的迁移映射。

完成文档并通过 PoC 后再开始代码重构，避免先把业务类大规模重命名，最后仍然得到一个不可复用的“通用”Runtime。

## 主要来源

- [Agent Skills 规范](https://agentskills.io/specification)
- [Microsoft Agent Framework](https://github.com/microsoft/agent-framework)
- [Microsoft Agent Framework：Agent Skills](https://learn.microsoft.com/en-us/agent-framework/agents/skills)
- [Microsoft Agent Framework Python Skills 实现](https://github.com/microsoft/agent-framework/blob/main/python/packages/core/agent_framework/_skills.py)
- [Microsoft Agent Framework Claude Agent SDK 集成](https://github.com/microsoft/agent-framework/tree/main/python/packages/claude)
- [OpenAI Agents SDK 官方文档](https://developers.openai.com/api/docs/guides/agents/sdk)
- [OpenAI Skills 官方文档](https://developers.openai.com/api/docs/guides/tools-skills)
- [Pydantic AI](https://github.com/pydantic/pydantic-ai)
- [Pydantic AI Skills](https://pydantic.dev/docs/ai/harness/skills/)
- [Deep Agents](https://github.com/langchain-ai/deepagents)
- [OpenHands Software Agent SDK](https://github.com/OpenHands/software-agent-sdk)
- [Claude Agent SDK Python](https://github.com/anthropics/claude-agent-sdk-python)
