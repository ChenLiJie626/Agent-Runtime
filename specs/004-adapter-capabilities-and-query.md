# SPEC 004：执行与程序查询适配器契约

状态：**基础包适配协议；Joern/Clang 细节改为后续插件示例** · 2026-09-25。领域对象见[SPEC 002](002-domain-and-api-contract.md)，状态/事件见[SPEC 003](003-state-events-and-recovery.md)；[S2 实测记录](../docs/06-S2-纵向PoC记录.md)列出已验证与未验证项。本文规定本包端口，不把 SDK 或 Joern 的类型作为公开 API；SPEC 001 的语义操作不是当前 S2 门禁。

## 1. AgentExecutor 端口

`AgentExecutor` 负责把一次 `RoleRunRequest` 送入一个模型 Agent 执行，并至少返回终态产物；声明 `stream_events=yes` 时还须按顺序产生 `RoleRunEvent`。它不得直接更新 CheckItem、Verdict 或 Exclusion；这些更新由领域服务读取已保存的 Evidence 和角色产物后完成。声明为 `no/unknown` 的可选能力不能被角色流程调用；任务必需时启动前返回 `CapabilityUnavailable`。

| 输入 / 操作 | 必需内容 | 输出 / 约束 |
|---|---|---|
| `describe_capabilities()` | 无 | 声明 `stream_events, explicit_resume, interrupt, structured_output, custom_tools, deny_tools, isolated_session, usage_reporting` 及版本；不能以“模型可能遵守 Prompt”宣称权限能力。 |
| `start(RoleRunRequest)` | `task_id, attempt_id, role, snapshot_digest, profile_digest, working_dir, ContextView, output_schema, tool_policy, budget, deadline, lease_epoch` | 新执行会话；返回 session ID 与最终产物/错误；声明 stream_events 时额外提供有序事件流。 |
| `resume(RoleRunRequest, sdk_session_id)` | 上述绑定与明确 session ID | 只在 SessionService 已核对绑定和租约时执行；不按最近一次历史猜测。 |
| `interrupt(attempt_id)` | 活动 Attempt；仅在声明 interrupt=yes 时可调用 | 尽可能取消模型及在途工具；调用方仍须按事件水位核对副作用。未声明能力时由上层截止时间及租约失效完成保守停止，不伪称已中断 SDK。 |
| `close(attempt_id)` | 活动或终止 Attempt | 释放 SDK 进程/连接；幂等，不能删除业务状态或 Evidence。 |

声明 `stream_events=yes` 时，`RoleRunEvent` 至少区分 `started, sdk_session_bound, tool_requested, tool_finished, structured_output, usage, interrupted, failed, ended`；未发生的事件不需伪造。事件带适配器本地序号和 Attempt ID；业务 EventJournal 自己分配权威 `seq`。工具事件携带 `query_id` 和结果摘要，不直接暴露未授权原文。结构化输出失败可进行有限修正，但每次修正计入预算；超过上限作为角色失败，不伪造 Assessment。

`ToolPolicy` 至少定义可见工具、允许操作/路径、只读要求、最大查询范围、超时、网络/进程限制和 SDK 设置来源。策略 digest 绑定 Attempt 与 SessionBinding。角色可以有不同策略，但验证者不能通过继承调查者的 SDK 历史获得额外工具授权。对受控 ProgramQuery 的调用在服务端再次核验策略和 lease epoch。

## 2. Claude Agent SDK 适配器映射

首版一个 RoleAttempt 独占 `ClaudeSDKClient`，新 Attempt 创建新客户端；同一角色同一任务续查时可通过明确 session ID resume。映射使用 SDK 的结构化输出、自定义工具/MCP、权限回调、Hooks 和中断能力；任何 SDK 选项行为都须在锁定版本上验证。官方 [Python 参考](https://code.claude.com/docs/en/agent-sdk/python)区分 `query()` 与 `ClaudeSDKClient`，并说明 `allowed_tools` 只是自动批准，`disallowed_tools` 用于禁止。SDK 的 [`setting_sources=[]` 旧版本注意事项](https://code.claude.com/docs/en/agent-sdk/python)要求联测不能只检查选项值，还要验证实际没有加载目标仓库/用户的 Hooks 与设置。

| 本包要求 | SDK 机制候选 | 联测判据 |
|---|---|---|
| 独立会话 | 新 `ClaudeSDKClient` / 明确 `resume` | 两个 fresh RoleAttempt 不共享对话；resume 只续接指定 ID。[Sessions](https://code.claude.com/docs/en/agent-sdk/sessions) |
| 中断和关闭 | Client interrupt / 生命周期 | 在途模型停止后，未决工具仍可由业务事件核对。 |
| 结构化 Claim/Assessment | `output_format` | JSON 形状正确；伪造 Evidence ID 仍被领域层拒绝。 |
| 工具最小化 | 显式工具清单、`disallowed_tools`、`can_use_tool`、MCP 服务政策 | 调查者不能写目标源码、调用任意 shell/网络；验证者只见授权工具。 |
| 设置来源隔离 | `setting_sources=[]`、受控工作目录、明确 MCP 配置 | 被分析仓库的 `.claude` 设置/Hook 不运行；锁定 SDK 版本覆盖旧行为差异。 |
| 用量 | SDK 事件/结果 | 每 Attempt 记录 token/成本或明确标 unavailable，不猜测。 |

首版不要求 SDK 跨主机 transcript 镜像；跨主机只保证从领域 Handoff 开新会话。若部署要求原历史跨主机续接，再按官方 [SessionStore](https://code.claude.com/docs/en/agent-sdk/python) 单独扩展，并保留业务状态为权威。

网关部署时，SDK 设置隔离仍保持 `setting_sources=[]`。应用通过适配器配置显式传入最小必要的环境变量（本地网关地址与认证占位符），并使用 SDK 可识别的 Claude 角色模型名；CC Switch 等网关负责把角色映射到上游模型。不得仅靠 SDK 请求中的角色名记录实际模型。运行记录应保存非密钥的路由提供方、上游模型、映射版本或摘要；映射在 Attempt 中变化时，应结束当前 Attempt 并重新绑定，不能把不同模型的产物伪装为同一会话。通过 CC Switch 路由到 GPT 的联测可验证协议、结构化输出、权限和 resume，但不能证明原生 Claude 模型的推理表现。

## 3. ProgramQuery 端口

核心只规定操作注册、参数 schema、授权范围、结果/覆盖/原文生命周期；调用方可接入自己的语言与工具。通用快照下的后端必须声明 `QueryOperation`（操作名、参数类型、必填参数、必需选择器及参数大小限制），基础包在调用后端前校验；后端仍须自行校验参数值的程序语义。`ProgramQuery` 接受受限的结构化查询，不允许模型提交任意 Joern DSL、shell 命令或文件系统路径。适配器在固定 Snapshot 和授权范围执行，并返回原始结果、覆盖与诊断；`EvidenceService` 负责归档并生成 Evidence ID。

下表列出 SPEC 001 插件可能注册的操作，**不是基础包的固定操作清单或验收要求**。

| 操作 | 输入范围 | 需要返回的材料 | 不可暗示的能力 |
|---|---|---|---|
| `get_change` | Base/Head、变更符号/位置 | 两侧源码位置、差异、构建可比性 | 仅文本差异不能证明新路径可达。 |
| `get_function` | 固定提交、符号、构建变体 | 函数体/签名、解析歧义、原文哈希 | 同名函数不能自动当同一实现。 |
| `find_callers` | 目标符号、调用范围 | 调用点、解析方式、直接/间接覆盖全集 | 只找到直接调用不能宣称所有调用者。 |
| `map_arguments` | 调用点、目标函数 | 实参与形参映射、常量/别名信息 | 文本相同不等于对象相同。 |
| `trace_value` | 来源值、目标使用点、范围 | 数据流路径/切片、断点、过近似标记 | 数据流可达不自动证明控制路径可行。 |
| `get_guards` | 使用点、条件范围 | 分支、早退、控制依赖/支配关系及位置 | 存在 `if` 不等于它保护实际使用路径。 |
| `check_reachability` | 入口、目标点、路径约束 | 候选路径、前提、无法解析的调用/条件 | 不得把未找到路径当全局不可达，除非证明搜索全集。 |

每种操作的 `QueryOutcome` 必须包含 `status, backend_id/version, snapshot_digest, query_digest, raw_artifact_digest?, coverage, limitations, locations`；适配器另向 EvidenceService 交付原始字节/流，公开对象不内嵌原文。`complete` 和 `partial` 即使返回零条结果，也须保存可复核的原始表示、查询参数和搜索边界；`timeout/failed/unsupported/denied` 返回诊断且不能产生“无问题”负证据。后端必须声明 `read_only, deterministic_for_snapshot, idempotent_retry` 及每个已注册操作的参数 schema、可用范围与覆盖/精度；能力可为 `yes/no/unknown` 并附限制。`supports_base_head`、调用者覆盖、路径可行性与别名精度仅由需要这些能力的规则查询，不是所有后端的固定必填项；不允许仅凭工具名称推断能力。

## 4. 后端插件示例（不阻塞 S2）

S4 的 `FrozenSourceProgramQuery` 是通用文字材料后端：绑定完整源码映射的摘要，只提供按相对路径和行范围读取内存窗口的 `read_source`。完整文字覆盖与语义覆盖分别表达，前者不完成规则的语义检查。查询不执行构建或打开宿主文件；范围、字节和内容校验见 [SPEC 009](009-public-source-runtime-integration.md)。

Joern 原生适配器是已完成探索的图查询 PoC。官方文档展示了 [CPG](https://docs.joern.io/code-property-graph/)、[控制依赖/支配查询](https://docs.joern.io/cpgql/control-flow-steps/)和[数据流查询](https://docs.joern.io/cpgql/complex-steps/)；这些说明它可作为材料来源，**不证明**本项目的跨文件空返回路径在目标配置下已经可精确裁决。若后续推进 SPEC 001 插件，应记录实际 Joern 版本、导入参数、解析失败、调用图/数据流输出和原文定位。

Clang Static Analyzer / CodeChecker 作为独立基线，提供编译数据库、告警和 Base/Head 对照信号；不能将“无告警”转成 `find_callers` 完整结果。Joern 的零结果若来自未解析间接调用、宏或别名，coverage 为 partial/unknown。SPEC 001 所需对象同一性或条件可行性无法证明时，其插件 RuleEvaluator 返回 `inconclusive`；这不影响基础包通用端口验收。

## 5. 存储端口与失败语义

| 端口 | 最小能力 | 失败要求 |
|---|---|---|
| `EventStore` | 按 analysis 原子追加事件、唯一 seq/幂等键、按水位读取 | 写入未确认不得更新对外投影；损坏 payload digest 须报 IntegrityError。 |
| `EvidenceStore` | 内容寻址写入/读取、哈希验证、Evidence 元数据查询 | 原文缺失或摘要不符时阻止确定结论。 |
| `TaskStore` | Task/Check/Attempt/SessionBinding 投影与租约比较交换 | stale epoch 拒绝写入，能从事件重建。 |
| `HandoffStore` | 不可变版本保存、按 task/水位读取 | 受保护项校验失败不得覆盖前一版本。 |
| `ExclusionStore` | 精确 CandidateKey 查找、状态迁移、依赖摘要核对 | 不得在新快照或身份不完整时返回有效排除。 |

首版适配器使用 SQLite 与内容寻址原文存储；事务边界遵守[SPEC 003](003-state-events-and-recovery.md)。其他存储实现只要满足相同契约即可替换。

## 6. 历史 PoC 环境与基础包缺口

2026-09-25 本机环境：系统默认 `python3` 为 **3.9.6**，隔离环境的 Python 为 **3.11.15**；`claude` CLI 为 **2.1.259**；Apple clang 为 **17.0.0**。隔离环境已安装 `claude-agent-sdk 0.2.159`；直连真实模型请求返回 API 403。CC Switch 升至 **3.20.4** 并恢复独立 ChatGPT OAuth 后，目标 `gpt-6-sol` 首次请求仍因客户端版本头过旧被上游以 400 拒绝。在该 Claude 提供方「Header 覆盖」设置 `version: 0.155.0` 后，实际转发到 `gpt-6-sol` 的真实 SDK 联测通过结构化输出、显式 resume、新会话、自定义 MCP 工具及项目 Hook 设置隔离。SDK 底层在途 MCP 工具中断与角色隔离已实测；本包 AgentExecutor 尚未暴露中断、事件流和用量上报，能力声明均为 false。SPEC 001 的 F-08 真实规则联测未完成，但不阻塞基础包；通用跨会话排除已按 KC-10 验收通过。Clang 在实体夹具上已运行，四次静态分析为零告警且只覆盖单个翻译单元。Joern **v4.0.636** 已在项目内临时目录使用 JRE 21 对实体夹具两侧建图并执行固定查询；受限适配器的 `get_function/find_callers/get_guards/map_arguments/trace_value/check_reachability` 已保存真实的部分覆盖 Evidence；真实后端 1 ms 超时查询保留原文而不生成 EvidenceRef。F-05 构建差异变体已运行；跨函数空返回路径仍未证实，间接调用目标明确未解析。`CodeChecker` 未联测。具体原文和覆盖见[S2 记录](../docs/06-S2-纵向PoC记录.md)。这些事实仅适用于本机，不等于目标部署环境的能力承诺。

## 7. 基础包验收映射

| 协议 | 当前门禁 |
|---|---|
| SDK 会话、角色隔离与能力协商 | KC-01、KC-04、KC-05、KC-12 |
| 结构化查询、覆盖与负证据 | KC-06、KC-11 |
| 存储完整性、恢复与交接 | KC-07、KC-08、KC-09、KC-10 |
