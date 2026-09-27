# agent-runtime

面向程序缺陷挖掘的可扩展 Python 基础包。架构为 **Claude Agent SDK 适配器 + 本包领域控制层**。当前本地发行线为 `0.2.1`：[S2](docs/07-S2-基础能力验收记录.md) 的 13 项基础能力门禁与 [S3](specs/007-release-and-extension.md) 的发行验收均已通过。项目采用 [MIT 许可证](LICENSE)，尚未发布到包索引。

## 安装与验证

Python 3.10+：

```bash
python -m pip install -e .
python -m unittest discover -s tests -v
```

第三方接入可先运行[完整样例](examples/external_consumer.py)：它从公开入口注册两条抽象规则、两个只读查询后端和一个角色执行器。样例用固定材料验证扩展流程，不代表真实缺陷检出率。构建 wheel 后，用[发行验证器](tools/verify_s3_release.py)在仓库外安装并运行它：

```bash
uv build --offline --out-dir dist/s3
.venv/bin/python tools/verify_s3_release.py \
  --wheel dist/s3/defect_agent_runtime-0.2.1-py3-none-any.whl \
  --sdist dist/s3/defect_agent_runtime-0.2.1.tar.gz
```

应用从 `agent_runtime` 导入 `RuleSpec`、`FixedSnapshot`、`CandidateIdentity`、`ProgramQuery`、`RuleEvaluator`、`AgentExecutor`、`SQLiteStore`、`DefectRuntime` 和 `RoleCoordinator`。先定义规则与后端，再创建分析/候选、保存查询证据和检查状态，最后运行独立角色并读取报告。详细调用顺序和版本约束见[S3 接入文档](docs/08-S3-发布与兼容.md)。`InvalidInput`、`Conflict`、`CapabilityUnavailable`、`PolicyDenied`、`StaleSnapshot`、`EvidenceIntegrityError`、`SessionUnavailable`、`BackendFailure` 和 `StorageFailure` 是公开失败分类；异常文本不作兼容承诺。

Claude 执行适配器为可选依赖，已在 Python 3.11 + `claude-agent-sdk 0.2.159` 上验证选项构造；安装后可从 `agent_runtime.adapters` 导入 `ClaudeAgentExecutor`：

```bash
python -m pip install -e '.[claude]'
```

Claude SDK 使用 `setting_sources=[]` 隔离用户与目标仓库设置。若应用通过 CC Switch 等本地网关运行，需在 `ClaudeConfig.sdk_env` 显式传入经过筛选的 `ANTHROPIC_BASE_URL` 和认证环境变量，并用 `model` 选择网关配置的 Claude 角色别名。模型到上游的映射由网关管理；本机当前 CC Switch 把这些角色映射到 Codex 配置的 `gpt-6-sol`。可用以下受限脚本检验本机路由：

```bash
.venv/bin/python tools/smoke_claude_sdk.py --cc-switch --probe-tools --output .poc/claude-sdk-smoke-cc-switch.json
```

脚本只读取本机 Claude 生效设置里的路由环境变量，要求地址为本地回环，报告不保存认证值。该联测需要 CC Switch 正在运行且其 ChatGPT OAuth 会话有效。2026-09-25 本机 CC Switch 3.20.4 的 Codex OAuth 路由需要在该 Claude 提供方「Header 覆盖」中设置 `{"version":"0.155.0"}`；设置后 `gpt-6-sol` 的结构化输出、显式 resume、新会话、自定义 MCP 工具和项目 Hook 隔离均通过。经路由的 GPT 结果只能验证 SDK 与网关链路，不能替代原生 Claude 模型的行为验证。

## 当前实现范围

| 能力 | 状态 |
|---|---|
| 规则、候选与裁决 | `FixedSnapshot`、`CandidateIdentity`、暂存绑定、动态 `CheckPlanner` 与多评价器注册已接入；旧空返回类型保留为示例。 |
| SQLite 业务事件、版本封套、内容寻址 Evidence、幂等查询与投影重建 | 已实现；覆盖未决查询与文件/数据库提交窗口的基础测试。 |
| SessionService 单写租约、显式同角色 resume、轮换 Handoff、ContextView 与精确 Exclusion | 已实现首版；旧 SDK transcript 丢失时可从领域 Handoff 新建会话，受保护未知项的移除需要证据与状态迁移。 |
| 角色协同；Claude SDK 可选执行适配器 | Profile 可配置调查、按缺口触发的专项角色和独立验证；各角色独立 Session、查询授权及 Note 交接通过确定性测试。包入口的真实 SDK 合成规则联调取得两个独立会话与结构化角色产物，未知项保守裁为 `inconclusive`。 |
| 真实 C++ 夹具与 Clang 基线 | SPEC 005 双提交、同配置编译数据库和原始 SARIF 已生成；Clang 零告警不作为安全证明。 |
| 可复用分析流水线 | `AnalysisPipeline` 已将候选发现、Evidence 查询、事实/检查落库、独立角色和规则裁决组合为公开 API；内置 `CppUnreachableDiscoverer`、`CppUnreachableEvaluator`、`EvidenceReviewExecutor`、`CompositeProgramQuery` 与 recorded-Joern 后端，第三方可逐项替换。 |
| 通用动态验证记录 | `ValidationRecord` 使用封闭 schema 绑定源码、策略、工具链、依赖、配方、输入、产物、资源与执行结果；`RecordedValidationProgramQuery` 只按已注册 ID 回放，`defect-validation-record` 只校验/检查记录，不执行命令。 |
| Portfolio、证据规划与 CodeQL replay | `PortfolioDiscoverer` 按 candidate digest 精确融合并持久保存全部来源；Pipeline 可选受限自适应 planner，默认仍为 eager；CodeQL 后端只运行包内固定查询和预建数据库，输出仅是候选 signal，空结果或失败不作负证据。相关新增指标为 non-gating。 |
| C++ use after free 规则 | 已内置 Clang Static Analyzer 捕获、`compile_commands.json` 安全重放、CodeChecker/Clang CTU 隔离适配器、`ClangUseAfterFreeDiscoverer`、只读诊断查询后端和 `CppUseAfterFreeEvaluator`；随附最小、跨类及真实跨翻译单元夹具。 |
| Joern 固定查询与受限 ProgramQuery | 已对 SPEC 005 两侧建图；`JoernProgramQuery` 支持 `get_function/find_callers/get_guards/map_arguments/trace_value/check_reachability`，真实原文已通过运行时保存为部分覆盖 Evidence；F-05 构建差异和真实超时已联测；超时不产生 EvidenceRef。完整条件路径尚不能证明。 |
| SPEC 008 评测契约 | 已实现公开 C++ 数据 manifest 校验、固定下载摘要核验、隔离容器构建记录、运行结果完整性检查、分阶段指标/分层/项目级不确定性报告、预注册门禁和 no-findings 负对照。UAF seed 集包含 QLever 4 个 ASan UAF 和 Ghidra 1 个 CWE-416；旧项目级 CTU 原始结果按新口径为候选/确认召回下界 0/5、可评估样本召回 0/1，QLever 的 4 个样本因 141 个 TU 的 ASTImporter 执行失败单列为不可评估，不再伪装成真实漏报。 |
| 分层 CTU 与生命周期候选 | 编译数据库可生成“全项目非 CTU + 每个缺陷目标独立 CTU 切片”，执行器会在单层失败后继续；另有容器引用失效、协程局部对象逆序析构、异步成员逆序析构，以及返回值资源所有权、栈上下文逃逸、vector 扩容后字段复制六类保守候选规则。三类机制夹具均已由 ASan 确认，规则输出本身仍不等于确诊。 |
| 真实 SDK 会话与准确率评估 | CC Switch 路由到目标 `gpt-6-sol` 的会话、工具、项目设置与角色隔离，以及 SDK 底层中断探针已通过。当前本包尚未开放中断接口；真实缺陷准确率与 F-08 跨会话排除尚未验证。当前领域测试中的程序后端是固定材料假实现。 |

公开入口在 `src/agent_runtime/__init__.py`；应用注入 `ProgramQuery`、`AgentExecutor`、规则身份策略与 `RuleEvaluator`。`DefectRuntime` 保持 SDK 独立；Agent 输出形成 Claim、SpecialistNote 或 Assessment，确定结论仍需已保存证据及规则评价器。合成测试不代表真实缺陷发现准确率。

0.2.1 的新增能力保持保守边界：validation replay 的 `complete` 只覆盖一个已绑定输入和观测，CodeQL fixture 测试只验证适配器与规范化机制。SPEC 010 的隔离执行器、cpp-peglib 真实基线/修复复现，以及真实 CodeQL 数据库上的质量收益尚未闭合，不能由 mock、空 SARIF 或成功退出推断。

0.2.1 本地发行门禁（2026-09-27）已通过：`pytest` **179 passed, 3 skipped**，`unittest` 兼容发现 **170 tests, 3 skipped**，`compileall` 通过；wheel/sdist 已离线构建，并在 checkout 外的干净虚拟环境完成公开导入、旧 `RecordedJoernProgramQuery` 兼容、六类生命周期常量、`.ql/.sc` 包资源、validation CLI `validate|inspect` 及禁止 `argv`/执行子命令的验收。验收记录为 [`docs/evidence/s3-release-0.2.1.json`](docs/evidence/s3-release-0.2.1.json)。宿主未安装 CodeQL CLI，真实 `.ql` 编译与预建数据库 replay 明确跳过；本门禁因此不构成真实 CodeQL 检测率证据。

## 规格与文档

先读[规格导航](specs/README.md)：SPEC 000/002/003/004/006/007 是当前基础包门禁；SPEC 001/005 是后续空返回规则插件示例。

1. [原始交接文档](Claude_Agent_SDK_Agent层开发交接_精简版_v1.1.md)：问题背景与跨文件空返回样例。
2. [需求基线与范围](docs/00-需求基线与范围.md)：使用者、P0 需求、边界与待决问题。
3. [开源调研与复用决策](docs/01-开源调研与复用决策.md)：Claude Agent SDK、audit、Joern、codebadger、CodeChecker、Clang、CodeQL 等项目的适配判断。
4. [Agent Runtime 开源架构调研](docs/03-Agent-Runtime开源架构调研.md)：Microsoft Agent Framework、Conductor、Claude Agent Framework、OpenHands、Deep Agents 等项目的运行循环、状态与扩展机制。
5. [最终架构决策 ADR 010](docs/04-最终架构决策-ADR.md)：依赖选择、融合来源、包边界和交付门禁。
6. [技术路线与接口](docs/02-技术路线与接口.md)：模块划分、状态/证据/上下文契约与阶段门禁。
7. [SPEC 000](specs/000-runtime-kernel.md)：可引用基础包的通用内核契约与验收。
8. [SPEC 002](specs/002-domain-and-api-contract.md)：规则无关的领域对象、公开 API 与扩展端口。
9. [SPEC 003](specs/003-state-events-and-recovery.md)：状态迁移、事件、事务与崩溃恢复。
10. [SPEC 004](specs/004-adapter-capabilities-and-query.md)：Claude 执行、程序查询和存储适配器协议。
11. [SPEC 006](specs/006-defect-context-and-memory.md)：缺陷专用 ContextView、裁剪、受保护项与排除记忆。
12. [基础包 S2 开发验收与后续工作](docs/05-开发就绪审查.md)：现有实现、验收结果与后续发行工作。
13. [SPEC 001](specs/001-defect-runtime.md)与[SPEC 005](specs/005-golden-cases-and-oracles.md)：后续 C/C++ 空返回规则插件及 Oracle。
14. [空返回插件 PoC 历史记录](docs/06-S2-纵向PoC记录.md)：实体夹具、Clang/Joern 原始材料和待完成的规则语义工作。
15. [S2 基础能力验收记录](docs/07-S2-基础能力验收记录.md)：KC-01 至 KC-13 的状态、可复现命令及尚未覆盖的组合场景。
16. [SPEC 007](specs/007-release-and-extension.md)与[S3 接入文档](docs/08-S3-发布与兼容.md)：公开扩展边界、包外样例、兼容与发行验证。
17. [S3 验收记录](docs/09-S3-验收记录.md)：S3-01 至 S3-06 的验收证据与发行状态。
18. [SPEC 008](specs/008-defect-quality-evaluation.md)：S4 的盲评数据、分阶段质量指标、改进流水线与放行门禁。
19. [S4 质量评测实施记录](docs/10-S4-质量评测实施记录.md)：公开 C++ 冻结清单、Q-01/Q-02 可执行契约、负对照报告及剩余门禁。

S4 已开始：当前已跑通[SPEC 008](specs/008-defect-quality-evaluation.md)的 Q-01/Q-02/Q-03/Q-07 首版机制、公开 C++ 负对照、隔离构建记录和 build-aware partial Evidence 回放。Q-04 仍需成功构建、规则 Oracle 与近邻反例。`0.2.1` 的本地构建和验证不等于公开发布。

## S4 质量评测

### Use after free 端到端示例

本机安装 `clang++` 后，可直接分析仓库内故意含缺陷的最小 C++ 项目：

```bash
PYTHONPATH=src .venv/bin/python examples/use_after_free_pipeline.py
```

示例先在临时快照副本上运行 Clang Static Analyzer，再由 Runtime 的发现器生成精确候选，通过组合查询后端保存源码窗口和原始 Clang 诊断，最后由独立调查/验证角色及规则评价器裁决。结果、SQLite 状态和内容寻址 Evidence 默认写入 `.poc/use-after-free-runtime/`。成功输出应包含：

```text
diagnostic_count: 1
path: src/main.cpp
rule_id: unix.Malloc
message: Use of memory after it is freed
status: confirmed
```

测试项目见 [`evaluation/fixtures/use-after-free-simple`](evaluation/fixtures/use-after-free-simple)，组装代码见 [`examples/use_after_free_pipeline.py`](examples/use_after_free_pipeline.py)。传入其他项目可使用 `--project /absolute/path/to/cpp-project`。项目有专属宏、include 或语言选项时，先由 CMake 等构建系统导出数据库，再传给 Runtime：

```bash
cmake -S /path/to/project -B /path/to/build -DCMAKE_EXPORT_COMPILE_COMMANDS=ON
PYTHONPATH=src .venv/bin/python examples/use_after_free_pipeline.py \
  --project /path/to/project \
  --compile-commands /path/to/build/compile_commands.json \
  --output .poc/uaf-with-compdb
```

适配器不经 shell 执行编译命令，会移除输出/依赖生成参数，拒绝插件和响应文件，并把项目绝对路径重定位到冻结快照。

仓库还提供跨三个类、多个头文件和两个 `.cpp` 的交互测试。`HeapBuffer` 释放对象后，`ReportReader` 经 `BorrowedInt` 读取悬空指针：

```bash
PYTHONPATH=src .venv/bin/python examples/use_after_free_pipeline.py \
  --project evaluation/fixtures/use-after-free-multiclass \
  --output .poc/use-after-free-multiclass
```

该例会把候选精确定位到 `include/borrowed_int.hpp:9`，并记录产生该路径的翻译单元 `src/workflow.cpp`。函数实现分别位于多个 `.cpp` 时，可使用隔离的 CodeChecker/Clang CTU 后端：

```bash
docker build -t agent-runtime/clang-ctu:clang14-codechecker6291 \
  evaluation/builds/clang-ctu-codechecker
PYTHONPATH=src .venv/bin/python examples/use_after_free_pipeline.py \
  --project evaluation/fixtures/use-after-free-ctu \
  --compile-commands evaluation/fixtures/use-after-free-ctu/compile_commands.json \
  --ctu \
  --output .poc/use-after-free-ctu
```

CTU 容器默认禁网，源码只读挂载，报告写入独立目录；输出中的编译数据库摘要、容器镜像摘要和 `ctu_mode` 会进入后端版本与 Evidence。逐翻译单元模式预期漏掉该跨 `.cpp` 夹具，CTU 模式用于恢复完整的释放到使用路径。

Runtime 现在提供正式的可组合分析入口。下面的示例使用内置发现器、源码查询后端、评价器和确定性 Evidence 执行器；由于没有完整语义后端，结果会保守保持 `inconclusive`：

```bash
PYTHONPATH=src .venv/bin/python examples/cpp_unreachable_pipeline.py
```

完整组装代码见 [`cpp_unreachable_pipeline.py`](examples/cpp_unreachable_pipeline.py)。应用可以替换任何 `CandidateDiscoverer`、`ProgramQuery`、`RuleEvaluator` 或 `AgentExecutor`；多个只读查询后端可通过 `CompositeProgramQuery` 合并为一个受限操作集合。完整语义 Evidence 达到成功构建、完整覆盖并经独立验证后，内置评价器才能输出 `confirmed/refuted`。

公开源码候选现已接入 Runtime 的持久 Evidence、独立调查/验证 Session 和报告，见 [SPEC 009](specs/009-public-source-runtime-integration.md)与[闭环验收](docs/11-S4-公开源码Runtime闭环验收.md)。通用 `FrozenSourceProgramQuery` 可供应用复用。Tesseract 的 manifest 命令已在固定 Ubuntu 22.04/Clang 14 容器中禁网执行，实际 183 项包清单与[仓库内依赖锁](evaluation/builds/tesseract-ubuntu22-clang14/packages.lock)匹配；配置成功，目标在 `hocrrenderer.cpp:503` 编译失败。捕获的 216 项编译数据库和完整验签源码用于生成 build-aware Joern CPG；受限 CFG 探针观测到 return 可达而后续语句与方法入口断开。验签后的查询原文作为 `semantic_validation` Evidence 接入 Runtime，确定性控制和真实 SDK 的两个独立角色均读取源码与语义原文。构建失败使翻译单元覆盖保持 0/216，状态仍为 `partial/unknown`，不能代替成功构建、规则 Oracle 或发布质量验收。

首个冻结 manifest 位于 [`evaluation/manifests/public-cpp-smoke-v1.json`](evaluation/manifests/public-cpp-smoke-v1.json)。它只引用两个公开 C++ 项目，按项目分成 tuning/holdout，固定上游提交、变更面、构建配方、许可证和 SHA-256；下载与文本扫描不会解包到磁盘或执行上游内容。

真实 UAF seed manifest 位于 [`evaluation/manifests/public-cpp-uaf-v1.json`](evaluation/manifests/public-cpp-uaf-v1.json)，包含 QLever PR #2812 的 4 个 UAF 和 Ghidra `GHSA-gqh9-2c72-wpjc`。manifest 的 artifact scope 仍是 `primary-defect-file`；另行检出的 base commit 已生成项目级数据库（QLever 204 TU、Ghidra 24 TU）并完成 CTU 尝试。首轮项目级 CTU（历史对照）的严格位置匹配候选召回下界为 0/5、可评估样本候选召回为 0/1，取证召回不可得（0/0），确认召回同为下界 0/5、可评估样本 0/1；QLever 仅 63/204 TU 成功，其 4 个标签属于执行失败后的不可评估样本，不能把零候选解释为安全或真实漏报。详情见 [`docs/12-S4-Clang-CTU-UAF基线.md`](docs/12-S4-Clang-CTU-UAF基线.md)。

本轮分层闭合（2026-09-27）：9 个缺失 QLever TU 已用独立 singleton 切片全部恢复，唯一 TU 覆盖 **228/228（100.0%）**；精确候选召回 **4/5（80.0%）**；取证召回 **4/4（100.0%）**；确认召回 **3/5（60.0%）**。Compressor、ExternalIdTable 与 JoinAlgorithms 的真实 ASan 证据为 `confirmed`；Server 为已插桩且已覆盖的本次复现路径 `refuted`，不代表其它路径安全。完整测试 **156 passed, 3 skipped**，`compileall` 通过。定向 CTU 的历史 partial/timeout 及所有 ASan 失败尝试均保留；详见[闭合交接](docs/13-S4-分层UAF剩余工作交接.md)。

大项目应先生成并执行分层计划。`plan.json` 保留一个全量非 CTU 层，并为每个目标生成独立 CTU 数据库；`run_layered_ctu.py` 按层继续执行，单个 AST 导入失败不会取消其余切片：

```bash
PYTHONPATH=src python tools/plan_layered_ctu.py /path/to/compile_commands.json \
  --project-root /path/to/project \
  --target src/path/to/defect.cpp \
  --target include/path/to/defect.hpp \
  --output-dir .poc/layered-plan

# CodeChecker 已安装在当前隔离环境时执行；容器调用方也可逐项消费 plan.json。
PYTHONPATH=src python tools/run_layered_ctu.py .poc/layered-plan/plan.json \
  --output .poc/layered-plan/execution.json
```

容器版独立执行入口为 `tools/run_targeted_ctu.py`；先用 `tools/materialize_ctu_slices.py` 从已验证容器数据库切片，按 TU 数升序、`-j 1` 运行，保存独立尝试与聚合元数据。`tools/merge_targeted_ctu.py` 可在不改写历史 attempt 的前提下合并恢复切片。原始宿主 `plan.json` 数据库不能直接传入 Linux 容器。真实基线头文件的最小复现入口为 `tools/run_public_uaf_asan.py`，项目测试 reduction 入口为 `tools/run_project_uaf_asan.py`，再由 `tools/merge_asan_evidence.py` 合并；这些证据与机制夹具分开计分。完整命令见[闭合交接](docs/13-S4-分层UAF剩余工作交接.md)。

六类生命周期候选规则可独立扫描，并由 ASan/复现日志做确定性确认。仓库内三个最小复现都应得到 `confirmed`；未执行到目标路径或只有无关崩溃时保持 `inconclusive`：

```bash
PYTHONPATH=src python tools/scan_lifetime_candidates.py scan /path/to/project \
  --root /path/to/project --output .poc/lifetime-candidates.json
PYTHONPATH=src python tools/run_lifetime_reproductions.py \
  --output .poc/lifetime-reproductions.json
```

```bash
PYTHONPATH=src .venv/bin/python tools/validate_s4_dataset.py \
  --manifest evaluation/manifests/public-cpp-smoke-v1.json \
  --verify-artifacts \
  --output .poc/s4-dataset-verification.json

PYTHONPATH=src .venv/bin/python tools/validate_s4_dataset.py \
  --manifest evaluation/manifests/public-cpp-uaf-v1.json \
  --verify-artifacts \
  --output .poc/public-cpp-uaf-v1-verification.json

PYTHONPATH=src .venv/bin/python tools/run_s4_negative_control.py \
  --manifest evaluation/manifests/public-cpp-smoke-v1.json \
  --output .poc/s4-negative-control-run.json

PYTHONPATH=src .venv/bin/python tools/generate_s4_report.py \
  --manifest evaluation/manifests/public-cpp-smoke-v1.json \
  --run .poc/s4-negative-control-run.json \
  --json-output .poc/s4-negative-control-report.json \
  --markdown-output .poc/s4-negative-control-report.md

PYTHONPATH=src .venv/bin/python tools/evaluate_s4_gate.py \
  --report .poc/s4-negative-control-report.json \
  --policy evaluation/policies/s4-contract-smoke-v1.json \
  --output .poc/s4-negative-control-gate.json

PYTHONPATH=src .venv/bin/python tools/run_s4_text_baseline.py \
  --manifest evaluation/manifests/public-cpp-smoke-v1.json \
  --output .poc/s4-text-baseline-run.json

PYTHONPATH=src .venv/bin/python tools/generate_s4_report.py \
  --manifest evaluation/manifests/public-cpp-smoke-v1.json \
  --run .poc/s4-text-baseline-run.json \
  --json-output .poc/s4-text-baseline-report.json \
  --markdown-output .poc/s4-text-baseline-report.md

PYTHONPATH=src .venv/bin/python tools/evaluate_s4_gate.py \
  --report .poc/s4-text-baseline-report.json \
  --policy evaluation/policies/s4-text-baseline-contract-v1.json \
  --output .poc/s4-text-baseline-gate.json

# 三个角色完成同预算运行后；--ablation 可重复。
PYTHONPATH=src .venv/bin/python tools/compare_s4_reports.py \
  --baseline BASELINE_REPORT.json \
  --agent AGENT_REPORT.json \
  --previous-release PREVIOUS_REPORT.json \
  --partition holdout \
  --ablation path-verifier=ABLATION_REPORT.json \
  --output .poc/s4-comparison.json
```

负对照故意不分析任何代码，用于证明未运行、零候选和漏检会如实进入覆盖率与召回分母。文本基线只扫描 PR 新增或替换的无条件 `return`，发现的候选保持 `inconclusive`；两者都不是发布质量成绩。预注册门禁目前为 `contract_test`，文本基线只按 holdout 分区计分，因没有人工仲裁的已确认告警而返回 `insufficient_evidence`。Q-03 比较器只接受同 manifest、同分区、同预算和同 Top-K 报告；当前两条运行预算不同，未作横向比较。
