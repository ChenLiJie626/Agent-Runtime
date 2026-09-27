# SPEC 007：S3 可引用发行物与扩展兼容

状态：**S3 实施规格，验收见记录** · 2026-09-26。受 [SPEC 000](000-runtime-kernel.md)、[SPEC 002](002-domain-and-api-contract.md)、[SPEC 003](003-state-events-and-recovery.md) 与 [SPEC 004](004-adapter-capabilities-and-query.md) 约束。S2 的机制验收见[记录](../docs/07-S2-基础能力验收记录.md)，S3 的结果见[记录](../docs/09-S3-验收记录.md)。

## 1. 目标与边界

第三方从安装好的 Python 发行物导入 `agent_runtime`，实现自己的规则、候选身份、确定性评价器、只读 `ProgramQuery` 和 `AgentExecutor`，无需修改核心包源码。S3 固定扩展边界和持久格式兼容，不承诺内置指定缺陷规则、真实仓库准确率、分布式调度或 Claude SDK 的事件流/中断/用量能力。

## 2. 公开 API 与版本

- 公开入口为 `agent_runtime.__all__` 和明确列出的 `agent_runtime.adapters` 可选适配器。`agent_runtime.codec`、`agent_runtime.coordinator._ScopedTools`、SQLite 表与事件投影记录属于内部实现；扩展样例不能导入它们。
- 公开摘要函数须覆盖固定输入、原文字节及 RuleEvaluator 的完整输入。其规范是 UTF-8、键排序、紧凑 JSON、禁止 NaN 的 SHA-256；值对象与映射的序列化结果稳定。调用方不需要复制核心的摘要拼装逻辑。
- 当前发行线为 `0.2.x`。同一 `0.2.x` 内公开符号、调用签名与已说明语义保持兼容；破坏性公开 API 变化进入下一个 minor，持久格式不兼容变化另升 `schema_version.major`。包版本与持久封套版本独立。
- `schema_version=1.0` 的已存事件/投影可继续读取和重建；写入使用 `1.1`。未知 major、非法 minor 或未来 minor 明确失败，不默默按旧语义写入。旧空返回类型作为兼容示例保留，新增扩展不依赖它们。
- 仅 `RuleDecision` 和角色产物可以表达裁决建议；运行时通用门禁继续负责引用、覆盖、角色独立性与排除范围。扩展不得直接修改 SQLite 表或伪造 EvidenceRef。

## 3. 包外样例

样例位于仓库的 `examples/`，执行时必须从构建好的 wheel 安装到独立虚拟环境，工作目录在仓库外，且不设置 `PYTHONPATH`。样例至少实现两个抽象规则和两个具有不同操作 schema 的程序查询后端；创建固定快照、精确候选、查询原文/Evidence、Fact/Check、独立角色产物和 scoped verdict，并在重开存储后读取报告或精确排除。样例只从公开入口导入，不借助源码路径。

## 4. 发行验证门禁

| ID | 场景 | 通过条件 |
|---|---|---|
| S3-01 | 构建 wheel 和 sdist；检查发行内容 | 两种工件可构建；核心导入无需 Claude/Joern；包含 `py.typed` 与必要适配资源，不包含 `.poc`、凭据或测试产物。 |
| S3-02 | 在干净虚拟环境安装 wheel，仓库外运行样例 | 两规则、两后端无需改核心；结构化查询、裁决和持久报告贯通。 |
| S3-03 | 公开 API 审查 | README 列出最小接入路径、端口、错误与能力边界；样例不导入内部模块。 |
| S3-04 | 旧封套与版本保护 | 旧 `1.0` 记录可读/可重建并继续写 `1.1`；未来 minor 和未知 major 拒绝；兼容策略成文。 |
| S3-05 | 发行物一致性 | wheel 中元数据版本、公开导入、可选 Claude 依赖与资源一致；发行验证使用安装物而非当前源码。 |
| S3-06 | 开源发布元数据 | 仓库包含项目所有者选定的 `MIT` 许可证全文；wheel/sdist 均包含同一文本，包元数据声明 `License-Expression: MIT`。 |

S3-01 至 S3-06 通过后可称为“具备开源发行条件的 0.2.x 基础包”。本地构建通过不等于已发布到包索引。具体规则的语义 Oracle 与真实项目评测仍由 S4 完成。
