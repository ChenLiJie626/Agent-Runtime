# Agent Runtime 开源架构调研

调研日期：2026-09-25。本文聚焦**供其他项目引用和扩展的运行时**，补充[程序分析后端调研](01-开源调研与复用决策.md)。依据项目官方仓库、架构文档和关键实现文件作静态阅读；“尚未联调”仅描述调研当时状态，后续实测见[PoC 记录](06-S2-纵向PoC记录.md)。仓库主分支会变化，实施前须锁定版本并复核行为。当前交付范围见[规格导航](../specs/README.md)，架构选型见[ADR 010](04-最终架构决策-ADR.md)。

## 结论先行

如果首版以 Claude 为主，执行循环直接使用 [Claude Agent SDK](https://github.com/anthropics/claude-agent-sdk-python)。本包应开发的是**缺陷分析控制层**：任务与候选、角色输入、业务状态、证据账本、上下文视图、可恢复工作流和确定性裁决。不要在 SDK 外再实现一套模型调用与工具循环。

在本次调查的候选中，没有发现可以原样复用、同时满足 Claude Agent SDK、可引用 Python 基础包、程序缺陷证据语义、排除记忆这四项的项目。[Microsoft Agent Framework](https://github.com/microsoft/agent-framework)有直接接入 Claude Agent SDK 的 Python 包及通用工作流检查点，是未来若需替换编排层时最值得做**对照 PoC** 的候选；其 Claude 包安装说明仍使用 `--pre`，领域证据层也需自建。[Microsoft Conductor](https://github.com/microsoft/conductor)更适合研究 Claude SDK 工作流/会话映射，但主要是 CLI，Claude provider 标为实验性。[OpenHands Software Agent SDK](https://github.com/OpenHands/software-agent-sdk)的事件持久化和 [Deep Agents](https://github.com/langchain-ai/deepagents) 的中间件架构值得借鉴；二者各有执行循环，不宜与 Claude SDK 并行叠加。[Agent Framework Claude 包](https://github.com/microsoft/agent-framework/blob/main/python/packages/claude/README.md) · [Conductor provider 对比](https://github.com/microsoft/conductor/blob/main/docs/providers/comparison.md)

## 对照范围与判断

| 项目 | 类型 / 底层 | 实际运行时结构 | 对本项目最有价值的机制 | 集成判断 |
|---|---|---|---|---|
| [Claude Agent SDK](https://github.com/anthropics/claude-agent-sdk-python) | 官方 Python SDK；Claude Code 执行能力 | SDK 管模型和工具调用、消息流、会话、子 Agent、Hooks、结构化结果 | 保留原生工具循环和会话能力 | **首选执行适配器**；证据和任务状态自行实现。 |
| [Microsoft Agent Framework](https://github.com/microsoft/agent-framework) | 通用可引用框架，含 `agent-framework-claude` 包 | Agent/Session/ContextProvider、工作流执行器、持久检查点；`ClaudeAgent` 接入 Claude Agent SDK | 可直接复用工作流和上下文提供者接口 | **未来重评候选**；首版不依赖。Claude 包仍以 `--pre` 安装，采用前需核实会话隔离及本项目状态语义。 |
| [Microsoft Conductor](https://github.com/microsoft/conductor) | 多 provider 工作流 CLI，含 Claude Agent SDK provider | YAML 节点、路由、预算/重试、provider、检查点；SDK provider 委托原生循环 | `session_key` 映射、步骤检查点、provider 能力校验 | **学习实现，不作为核心依赖**；当前 Claude provider 实验性、产品形态偏 CLI。 |
| [Claude Agent Framework](https://github.com/uukuguy/claude-agent-framework) | 直接依赖 Claude Agent SDK 的 Python 包 | `create_session()`、角色定义、多个通用工作流模式、插件与日志 | 角色基数约束、框架 Prompt 与业务 Prompt 分层 | **轻量角色编排参考**；包元数据标为 Alpha，文档未给出可验证的业务 checkpoint/resume。 |
| [OpenHands Software Agent SDK](https://github.com/OpenHands/software-agent-sdk) | 可引用的独立 agent SDK | Conversation、EventStore、状态、工具、Workspace、Server 分包 | 追加事件、可恢复状态、历史与压缩视图分离 | **状态/事件设计样本**；整个 SDK 替代而非包装 Claude SDK。 |
| [Deep Agents](https://github.com/langchain-ai/deepagents) | LangChain/LangGraph 上的 agent harness | `create_deep_agent()` 装配模型、后端、中间件、子 Agent；LangGraph 运行/检查点 | 中间件装配、角色隔离、历史转储、后端协议 | **扩展接口样本**；避免与 Claude SDK 并行引入第二个循环。 |
| [OpenAI Agents SDK](https://github.com/openai/openai-agents-python) | 通用可引用 SDK | `Runner`、handoff、guardrail、tracing、Session 协议 | 会话存储可替换、交接输入过滤 | **跨 SDK 对照**；首版不增加其运行时依赖。 |
| [Fennec-AI/agent-runtime](https://github.com/Fennec-AI/agent-runtime) | LangChain 上复刻 Claude 风格架构的项目 | 自有循环、Session 门面、子 Agent、取消树、压缩 | 层级取消与清理思路 | **有限参考**；README 明示缺少 MCP、持久 transcript/resume，尚不适合作底座。 |

上述“集成判断”是对本项目目标的推断，不是项目维护者对自身产品的评价。

## 1. Claude Agent SDK：执行平面

[官方 SDK 文档](https://code.claude.com/docs/en/agent-sdk/python)列出 `query()` / `ClaudeSDKClient`、自定义工具、子 Agent、Hooks、结构化输出等能力；[会话文档](https://code.claude.com/docs/en/agent-sdk/sessions)给出显式 resume / fork。它已经覆盖一个 agent 从消息到工具再到结果的核心循环。本包的 `ClaudeRunner` 应只把领域任务转成 SDK 选项与输入，消费 SDK 事件，保存运行元数据，再将结构化产物交给业务校验。

SDK 会话承载模型对话，但不能等价为业务检查点。源码快照、候选集合、未查项、反证、排除、Evidence ID 和工作流事件必须由本包持久化。`allowed_tools` 也不能单独充当安全策略：它表示自动批准范围，具体禁止及文件/网络隔离须在 SDK 选项、工具服务和运行环境共同落实。[SDK Python 参考](https://code.claude.com/docs/en/agent-sdk/python)

## 2. Microsoft Agent Framework：可引用的 Claude SDK 集成候选

[Claude 包说明](https://github.com/microsoft/agent-framework/blob/main/python/packages/claude/README.md)明确其 `ClaudeAgent` 接入 Claude Agent SDK；[Agent 概念](https://learn.microsoft.com/en-us/agent-framework/concepts/agents/)把 Agent、Session、ContextProvider、中间件和工具统一到 run 接口。[Python 会话实现](https://github.com/microsoft/agent-framework/blob/main/python/packages/core/agent_framework/_sessions.py)有每次调用创建的 SessionContext、分来源的上下文提供者、会话状态存储和历史提供者。[工作流检查点](https://learn.microsoft.com/en-us/agent-framework/workflows/checkpoints)在执行 superstep 后保存执行器状态、待处理消息及共享状态，可使用内存、文件或其他 CheckpointStorage。

它是比 CLI Conductor 更像“基础架构 runtime 包”的直接复用候选。不过 Claude Python 包的 README 仍建议 `pip install agent-framework-claude --pre`；通用 checkpoint 也不会自动表达某条缺陷候选的反证、覆盖范围或 ExclusionRecord。其[已关闭的会话隔离问题](https://github.com/microsoft/agent-framework/issues/7403)说明：即便框架提供 Session 类型，多个逻辑会话复用客户端时仍需在**锁定版本**上做并发和隔离测试；不能仅凭 API 名字认定隔离正确。

**未来重评条件：** 若首版评测显示多阶段并发调度已成为瓶颈，用相同的“调查者→验证者”样例对比直接 Claude SDK adapter 与 Agent Framework `ClaudeAgent + Workflow`：会话隔离、checkpoint 边界、恢复后未决工具查询、上下文注入、工具权限、依赖体积。首版按[ADR 010](04-最终架构决策-ADR.md)直接使用 SDK，不引入后者。

## 3. Conductor：Claude SDK 上的工作流怎样续跑

[工作流语法](https://github.com/microsoft/conductor/blob/main/docs/workflow-syntax.md)显示：Conductor 把 agent 定义为工作流节点，节点有输入、输出 schema、路由、重试、预算；SDK provider 负责执行该节点。`session_key` 是工作流标签，provider 把它映射到真实 SDK session ID，映射键包含工作目录；映射写入 checkpoint，`conductor resume` 恢复。默认每次节点执行启动新会话；同 key 会把之前对话带进下一次运行。它也在校验阶段拒绝并行节点共享同一 key。这证实**工作流进度**和**SDK 对话**应两层存储，而单个 SDK 历史需要单写。[`session_key` 说明](https://github.com/microsoft/conductor/blob/main/docs/workflow-syntax.md#session-continuity-session_key)

其检查点可按节点边界或时间间隔保存；恢复从工作流状态继续。对我们的缺陷分析，更合适的是在“原始证据已保存、检查项/候选更新已提交”这样的**业务安全点**提交状态，而不能只依靠通用节点完成。原始工具结果写入到状态提交之间的失败要有 `in-doubt`/核对规则。[工作流语法](https://github.com/microsoft/conductor/blob/main/docs/workflow-syntax.md)

值得保留的警示：Conductor 的 Claude provider 不支持把任意“每 Agent 工具 allowlist”直接映射为 SDK 原生权限，因此采用能力校验与拒绝；默认禁用环境中隐式的 MCP/设置来源。加载被分析仓库的 `setting_sources` 可能连 Hooks 一起加载。本包需把仓库内容视为数据，工具清单和配置来源在任务启动前固定。[provider 对比](https://github.com/microsoft/conductor/blob/main/docs/providers/comparison.md)

**迁移到本包：** 定义 `SessionBinding(analysis, task, role, attempt, snapshot, working_dir, sdk_session_id)`；同一 binding 单写、跨角色默认新会话。显式续查复用 SDK 历史，压缩轮换则在保存领域 Handoff 后创建新会话。Conductor 的跨 Agent 共用 session 功能在本项目的独立验证角色上不应启用。

## 4. Claude Agent Framework：角色与流程封装

这个[Python 包](https://github.com/uukuguy/claude-agent-framework)在[包元数据](https://github.com/uukuguy/claude-agent-framework/blob/main/pyproject.toml)中直接依赖 `claude-agent-sdk`，公开 `create_session()`，将角色定义与业务 Agent 实例分开，并提供 pipeline、critic-actor、specialist pool 等通用模式；还有框架 Prompt/业务 Prompt 两层及生命周期插件。对本项目，最有价值的是**角色数量、输入输出与 Prompt 责任的显式定义**。但它的通用多 Agent 模式仍无法替代“按候选和必查项调度＋以原始证据为准的验证”。README 展示 transcript 与工具 JSONL 日志，没有说明崩溃恢复的业务检查点；`pyproject.toml` 标注 Alpha，因此只列为设计参考，不根据 README 的“production-ready”自述判定成熟度。[README](https://github.com/uukuguy/claude-agent-framework/blob/main/README.md) · [包元数据](https://github.com/uukuguy/claude-agent-framework/blob/main/pyproject.toml)

## 5. OpenHands：状态、事件与上下文视图

[持久化文档](https://docs.openhands.dev/sdk/guides/convo-persistence)展示一份可恢复的 `base_state.json` 加顺序 `events/event-xxxxx-id.json`：基础状态记录配置、执行状态和统计，事件文件记录消息、工具调用和观察。其 [`ConversationState`](https://github.com/OpenHands/software-agent-sdk/blob/main/openhands-sdk/openhands/sdk/conversation/state.py)在恢复时校验 conversation ID 和工具配置；[`EventStore`](https://github.com/OpenHands/software-agent-sdk/blob/main/openhands-sdk/openhands/sdk/conversation/event_store.py)提供集中追加路径。一个单独的 event log 比从最终聊天文本反推工作进度更可靠。

OpenHands 的 [`Condenser` 接口](https://github.com/OpenHands/software-agent-sdk/blob/main/openhands-sdk/openhands/sdk/context/condenser/base.py)基于事件历史构造供模型使用的视图；压缩只改变后续模型看到的上下文，不应销毁原始事件。对于缺陷挖掘，还需要额外的领域约束：触发条件、反证、未知、未查项、负证据的覆盖边界和原始 Evidence 引用都必须进入受保护的结构化部分，普通摘要不能删除它们。

**迁移到本包：** 追加式业务事件账本 + 可重建任务投影 + 独立的 `ContextView`。首版可以 SQLite 事务承载这些逻辑，无需照搬“每事件一个文件”的存储格式；数据模型和恢复语义比物理格式重要。SDK 原生消息流保留为诊断材料，业务事件只保存必要引用、状态迁移和可审计元数据，避免无上限重复存整段对话。

## 6. Deep Agents：装配式扩展与子 Agent 隔离

[架构文档](https://github.com/langchain-ai/deepagents/blob/main/libs/ARCHITECTURE.md)明确三层：LangGraph 管执行/检查点，LangChain `create_agent` 管模型工具循环，Deep Agents 在 `create_deep_agent()` 里组装规划、文件、子 Agent、压缩、权限等中间件。中间件能在模型或工具调用前后改可见工具、上下文和状态；普通工具只有被模型选择时才运行。子 Agent 有自己的中间件栈和上下文；图检查点与文件/记忆后端分开。[中间件说明](https://github.com/langchain-ai/deepagents/blob/main/libs/deepagents/deepagents/middleware/__init__.py)

它的[压缩中间件](https://github.com/langchain-ai/deepagents/blob/main/libs/deepagents/deepagents/middleware/summarization.py)在阈值触发时转储被移出模型上下文的完整消息，再给模型摘要；可按需触发。适合作为“原文留存、模型视图收缩”的机制参考，但其通用摘要没有缺陷语义保留承诺。

**迁移到本包：** 给 `ContextBuilder`、`EvidenceWriter`、`ToolPolicy`、`Telemetry` 定义可插拔阶段和顺序，明确哪些阶段可以改变模型输入，哪些只能观察。不要把 LangGraph checkpoint 再套在 Claude SDK 会话循环之上；若未来选择 Deep Agents，应作为**替代执行适配器**另写 SPEC 和兼容测试。

## 7. OpenAI Agents SDK：可替换 Session 与交接输入

[Sessions 文档](https://openai.github.io/openai-agents-python/sessions/)提供 `Session` 协议及 SQLite、Redis、SQLAlchemy 等实现；它解决对话条目的读取、追加和清空，不负责本项目的候选证据语义。[Handoffs 文档](https://openai.github.io/openai-agents-python/handoffs/)的 `input_filter` 可改变下一角色看到的内容，同时保留原始会话项目。这为“调查者输出 Claim，验证者只收原始证据及有限上下文”提供接口启发。

**迁移到本包：** `SessionStore`、`EventStore`、`EvidenceStore` 分开定义；角色交接由 `ContextViewPolicy` 决定，只传必要上下文。此处的“可替换”是我们的领域端口设计，不承诺首版兼容 OpenAI Agents SDK 的 `Session` 协议。

## 建议的基础包边界

```text
调用方 / CLI
  → DefectRuntime（公开、稳定的领域 API）
      ├─ TaskPlanner + RoleCoordinator（候选/检查、调查→独立验证）
      ├─ SessionService（Attempt、SessionBinding、轮换/恢复）
      ├─ EventJournal + TaskProjection（业务事件与当前进度）
      ├─ EvidenceStore + ExclusionIndex（原始材料、反证及失效条件）
      ├─ ContextViewBuilder（按缺口检索、受保护字段、可压缩视图）
      ├─ VerdictGate（引用/快照/覆盖/语义前提校验）
      └─ ports
          ├─ AgentExecutor → Claude Agent SDK adapter（首选）
          ├─ Stores → SQLite adapter（首版）
          └─ ProgramQuery → Joern/Clang 等 adapter（PoC 后定）
```

公开 API 不暴露 SDK 消息类型、LangGraph 状态或存储表结构。`AgentExecutor` 返回标准化运行事件与角色产物；每个执行适配器声明其能力（resume、fork、结构化输出、工具隔离等）。不支持的能力在启动前拒绝，而不是静默降级。

### 状态职责和恢复顺序

| 数据 | 主责 | 生命周期 |
|---|---|---|
| SDK session/transcript | AgentExecutor | 角色交互，可续接；丢失时不能假装恢复原历史。 |
| RoleAttempt + SessionBinding | SessionService | 一次角色执行的身份、版本、租约与恢复关系。 |
| 业务事件 + TaskProjection | EventJournal | 检查项、预算、查询、失败、交接、裁决的可重建进度。 |
| RawEvidence + ExclusionRecord | EvidenceStore | 不可变材料与有范围的排除；独立于聊天压缩。 |
| ContextView + Handoff | ContextViewBuilder | 从上述状态生成的受预算限制的模型输入和跨会话交接。 |

恢复流程：先校验快照/Profile/工具政策；读取业务事件水位和未决操作；核对 Evidence 是否已入库；确认 SDK session 是否仍可用；能原历史续查才 resume，否则从经校验 Handoff 开新 session。角色执行结束不意味着工作流检查完成；验证者也不从调查者的 SDK 对话直接继承事实。

### 对后续 SPEC 001 规则插件的具体改进

1. 在 AC 中增加**证据写入成功但 Attempt checkpoint 前崩溃**、**session transcript 丢失**、**同一 session 并发写**三种恢复分支。
2. Handoff 校验从“字段非空”升级为**受保护清单**：每条未解决条件/反证/未知/未查项都要么被保留，要么附带新证据和显式状态迁移；旧事件仍可追溯。
3. 独立验证者默认新 session，仅接收候选、Claim、原始 Evidence 引用、当前限制和主动寻找反证的任务；其 ContextView 不复用调查者自由文本摘要。
4. 执行适配器做能力协商；权限、工具配置来源和工作目录绑定在 Attempt 上，能力不匹配时启动失败。
5. 上述设计已进入基础包规格；“OpenHands/Conductor 已实现”不等于本包已完成相同能力的验收，实际进度见[开发差距表](05-开发就绪审查.md)。

## 需要做的实证与选型触发条件

| 验证项 | 要回答的问题 | 决策触发 |
|---|---|---|
| Claude SDK 版本/CLI 联调 | resume、结构化结果、Hooks、MCP、取消、会话转储在锁定版本的实际行为 | 固定首版 adapter 能力矩阵。 |
| 崩溃注入 | SDK 工具结果、Evidence 写入、事件提交、Handoff 保存各点中断怎样恢复 | 决定事务/幂等键和安全点定义。 |
| 长会话上下文 | 通用压缩与领域受保护字段在真实反证案例中的丢失率、成本 | 决定自动轮换阈值与人工审计视图。 |
| Agent Framework 未来对照 | 多阶段并发成为需求时，`ClaudeAgent + Workflow` 是否减少调度代码且保留领域账本与隔离 | 首版不作为前置门禁；只有出现复杂调度需求才重新选型。 |
| 其他备选底座 | Claude SDK 是否无法满足所需隔离/部署；OpenHands/Deep Agents 的接入成本 | 有实证约束时再评估替换执行适配器。 |

## 主要来源

- [Claude Agent SDK Python 仓库](https://github.com/anthropics/claude-agent-sdk-python)、[官方 Python 参考](https://code.claude.com/docs/en/agent-sdk/python)、[Sessions](https://code.claude.com/docs/en/agent-sdk/sessions)
- [Microsoft Agent Framework Claude 包](https://github.com/microsoft/agent-framework/blob/main/python/packages/claude/README.md)、[Python 会话实现](https://github.com/microsoft/agent-framework/blob/main/python/packages/core/agent_framework/_sessions.py)、[工作流检查点](https://learn.microsoft.com/en-us/agent-framework/workflows/checkpoints)
- [Conductor 工作流语法](https://github.com/microsoft/conductor/blob/main/docs/workflow-syntax.md)、[provider 对比](https://github.com/microsoft/conductor/blob/main/docs/providers/comparison.md)
- [Claude Agent Framework README](https://github.com/uukuguy/claude-agent-framework)、[包元数据](https://github.com/uukuguy/claude-agent-framework/blob/main/pyproject.toml)
- [OpenHands 持久化指南](https://docs.openhands.dev/sdk/guides/convo-persistence)、[ConversationState](https://github.com/OpenHands/software-agent-sdk/blob/main/openhands-sdk/openhands/sdk/conversation/state.py)、[EventStore](https://github.com/OpenHands/software-agent-sdk/blob/main/openhands-sdk/openhands/sdk/conversation/event_store.py)、[Condenser](https://github.com/OpenHands/software-agent-sdk/blob/main/openhands-sdk/openhands/sdk/context/condenser/base.py)
- [Deep Agents 架构](https://github.com/langchain-ai/deepagents/blob/main/libs/ARCHITECTURE.md)、[压缩实现](https://github.com/langchain-ai/deepagents/blob/main/libs/deepagents/deepagents/middleware/summarization.py)
- [OpenAI Agents SDK Sessions](https://openai.github.io/openai-agents-python/sessions/)、[Handoffs](https://openai.github.io/openai-agents-python/handoffs/)
- [Fennec-AI/agent-runtime README](https://github.com/Fennec-AI/agent-runtime)
