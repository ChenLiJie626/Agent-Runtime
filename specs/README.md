# SPEC 导航与当前交付范围

状态：**范围重整，基础包优先** · 2026-09-25。

## 规范层级

| 文档 | 定位 | 当前是否为验收门禁 |
|---|---|---|
| [SPEC 000](000-runtime-kernel.md) | 基础包目标、范围、P0 验收与阶段 | 是，最高优先级 |
| [SPEC 002](002-domain-and-api-contract.md) | 与规则无关的领域对象、端口和公开 API | 是 |
| [SPEC 003](003-state-events-and-recovery.md) | Session、租约、事件、证据和恢复 | 是 |
| [SPEC 004](004-adapter-capabilities-and-query.md) | Agent SDK 与 ProgramQuery 适配协议 | 是；具体 Joern 操作不是门禁 |
| [SPEC 006](006-defect-context-and-memory.md) | 缺陷调查 Context、交接和排除记忆 | 是 |
| [SPEC 007](007-release-and-extension.md) | S3 公开扩展边界、兼容和发行验证 | 是 |
| [SPEC 008](008-defect-quality-evaluation.md) | S4 跨规则质量指标、评测数据与持续改进门禁 | S4 门禁；Q-01/Q-02/Q-03/Q-07 首版机制已实现，完整门禁未通过 |
| [SPEC 009](009-public-source-runtime-integration.md) | 公开源码候选接入 Runtime 的证据、角色和报告闭环 | S4 集成验收；语义取证与质量放行仍按 SPEC 008 |
| [SPEC 001](001-defect-runtime.md) | C/C++ 新增空返回规则示例 | 否；后续规则插件规格 |
| [SPEC 005](005-golden-cases-and-oracles.md) | SPEC 001 的黄金样例与 Oracle | 否；用于验证插件，不阻塞基础包 |

若示例规则和基础包契约出现冲突，以 SPEC 000 与通用契约为准。现有 Joern/Clang 与空返回 PoC 的实测记录保留在[文档 06](../docs/06-S2-纵向PoC记录.md)，作为探索结果，不作为当前 S2 的完成条件。

## 当前要交付的基础能力

调用方可以安装并导入核心包，注册自己的规则/Profile、候选身份策略、只读程序查询后端、规则裁决器和 Agent 执行器。包负责两个默认角色的协同、会话绑定与轮换、业务事件和 Evidence 持久化、缺陷调查 Context 构建、精确排除记忆、通用证据门禁及可恢复报告。Claude Agent SDK 是首个可选执行适配器；其他 SDK 可按同一端口接入。

当前不要求基础包自己发现任何指定缺陷、证明 C/C++ 空返回的跨函数路径、内置 Joern/Clang 规则算法、达到某种真实仓库检出率，或产出空返回案例的 `confirmed/refuted`。这些属于规则/后端插件及后续评测。

## 当前阶段与门禁

| 阶段 | 目标 | 完成判据 |
|---|---|---|
| S1 契约 | 定义通用领域对象、端口、状态/失败语义 | SPEC 000/002/003/004/006 一致，示例规则不再是核心门禁 |
| S2 基础能力纵向联调 | 用最小测试规则与可控假后端贯通角色、Session、Evidence、Context、Handoff、排除和恢复；真实 SDK 只验证协议能力 | [SPEC 000](000-runtime-kernel.md) 的 KC 场景有可复现结果；不依赖 Joern 语义证明 |
| S3 基础包收敛 | 稳定可安装发行物、公开扩展 API、迁移/兼容和文档 | 第三方能按公开端口接入第二个规则与后端，核心包无需修改 |
| S4 规则插件与真实项目评测 | 先按 SPEC 008 建立盲评与基线，再推进 SPEC 001/005 及其他规则 | 每个规则有语义 Oracle；按统一契约报告候选/取证/确认召回、发布精度、弃判、覆盖和成本，并完成真实项目影子运行 |

## 已有代码与新规格的差距

当前 `0.2.0` 本地发行线从原 0.1.0 纵向切片演进而来。S2 的 KC-01 至 KC-13 已通过；S3 的公开端口、两个规则与两个查询后端的包外接入、旧记录兼容、独立安装和 MIT 许可证验证见[S3 验收记录](../docs/09-S3-验收记录.md)。旧 `AnalysisSnapshot`、`CandidateKey` 与 `null_return_rule()` 作为兼容示例保留。Claude 适配器尚未向调用方开放事件流、中断与用量，其能力声明仍为 false。`0.2.0` 尚未发布到包索引。
