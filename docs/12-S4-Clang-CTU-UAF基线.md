# S4：Clang CTU 与 UAF 基线

日期：2026-09-26。范围：`compile_commands.json` 重放、CodeChecker/Clang
CTU、跨翻译单元 UAF 机制夹具，以及真实公开 UAF 数据的首批来源。

## Runtime 能力

- `ClangCompilationDatabase` 接受 `arguments` 或 `command` 形式的编译数据库，
  不通过 shell 执行；源码必须位于项目根目录内。
- 分析前移除编译输出、依赖输出和仅编译动作参数，拒绝插件、响应文件和
  直接 Clang 适配器伪装成 CTU。
- `run_codechecker_ctu_use_after_free_analysis` 在断网容器内运行固定版本的
  CodeChecker 6.29.1 与 Clang 14；源码只读挂载，报告写入独立目录。
- CodeChecker plist 被规范化为现有 `ClangAnalysisBundle`，因此继续复用
  `ClangUseAfterFreeDiscoverer`、`ClangDiagnosticProgramQuery`、
  `CppUseAfterFreeEvaluator` 和第三方可替换的 Runtime 端口。
- 同一 CodeChecker issue 从多个入口翻译单元重复出现时，按 issue hash、
  位置、规则和消息合并，并保留路径更完整的报告。
- `LayeredCtuPlanner` 从同一编译数据库生成全项目非 CTU 层、缺陷相关 CTU
  合并视图，以及每个缺陷目标各自独立的 CTU 切片。v2 计划只把全量非 CTU
  层和独立切片放入默认执行顺序；一个切片失败后继续运行其余切片。
- `tools/run_layered_ctu.py` 不经 shell 执行计划，逐层保存退出码、输出、TU
  成败及目标状态。评测器可用 `--*-layered-execution` 接收这一目标级状态，
  不再用“项目中任意 TU 失败”替代“该缺陷目标不可评估”。

## 跨翻译单元对照结果

夹具位于 `evaluation/fixtures/use-after-free-ctu`。构造、借用、释放、读取和
入口分别位于四个 `.cpp`；同一份编译数据库用于两组运行。

| 模式 | 诊断 | Runtime 结论 | 定位 |
|---|---:|---|---|
| Apple Clang 17，逐翻译单元 | 0 | 无候选 | 不适用 |
| CodeChecker 6.29.1 / Clang 14 CTU | 1（去重后） | `confirmed` | `src/borrowed_view.cpp:7:12` |

CTU 报告的入口翻译单元为 `src/main.cpp`，路径跨过
`run_cross_translation_unit_workflow`、`HeapOwner::release` 和
`BorrowedView::read`。编译数据库摘要为
`cf4f3de359100dd8ae37ae103a361b4dd55e13fc2e7fc371ff085860ed32a563`。
本次容器镜像 ID 为
`sha256:f51ce940c1b41d40ce60b7a5a6616d1f01dcca5c636f874f81867fde280b9b7e`。

复现命令见仓库根目录 README。运行产物分别位于
`.poc/use-after-free-ctu-pertu-v2/result.json` 和
`.poc/use-after-free-ctu-runtime-v5/result.json`。

## 首批真实公开 UAF 来源

首批标签采用两个 Apache-2.0 C++ 项目，并按项目隔离：

- tuning：QLever PR #2812，修复 4 个经 ASan 复现的独立 UAF，覆盖本地词表
  生命周期、协程销毁顺序、查询上下文生命周期和异步成员销毁顺序。
- holdout：Ghidra `GHSA-gqh9-2c72-wpjc`，CWE-416；`PcodeCacher`
  的 `std::vector` 扩容使已返回元素指针失效，修复改为地址稳定的容器。

`public-cpp-uaf-v1` seed manifest 固定 5 个缺陷、11 项公告/主缺陷
文件字节、base/head commit、SHA-256、构建配方、许可证和标签来源；所有远端
输入已经重新下载验签。其 `artifact_scope` 明确为 `primary-defect-file`，用于先
冻结真实标签。机制夹具的 1/1 不能写成真实数据集准确率。

## 真实项目级 CTU 结果

在相应 base commit 的干净检出上生成了生产源码项目级编译数据库，并对全部条目
执行 CodeChecker 6.29.1 CTU；分析阶段断网。严格用 manifest 中的缺陷文件和行号
匹配候选，不能用同项目的其它 UAF 代替已知正例。

| 项目 | 编译数据库 | CTU 完成 | UAF 候选 | 已知缺陷命中 |
|---|---:|---:|---:|---:|
| QLever | 204 TU，SHA-256 `2d3085cde8abead0673447b8ed6d48d7934c191a035779f07cd06d4525f291e3` | 63 成功 / 141 失败 | 0 | 0/4 |
| Ghidra Decompiler | 24 TU，SHA-256 `836b4007cf1c9a9ef75a7e12d0bfd36365e8922b0799a797b48063f162c8ba13` | 24 成功 / 0 失败 | 1 个非标签候选 | 0/1 |

按失败分离后的口径，候选召回下界为 **0/5**，可评估样本候选召回为
**0/1**；取证召回为 **不可得（0/0）**；确认召回下界为 **0/5**，可评估
样本确认召回为 **0/1**。Ghidra 的唯一候选位于 `slghpatexpress.cc:330`，不是冻结标签
`sleigh.cc:205`，故保留为未裁决告警而不计命中。QLever 全部 204 个 TU 都完成
预分析和分析尝试，但 Clang CTU ASTImporter 不支持
`UnnamedGlobalConstant`、`TemplateParamObject`、`CoroutineBodyStmt`，并在少量
导入上崩溃，因此它的 4 个标签记为 `execution_failure/unevaluable`，不是 4 个
真实漏报，也不能把零候选解释为项目安全。召回下界保守地把不可评估项按未命中
计入全量分母；可评估样本召回只使用真正完成目标分析的样本。

## 首批生命周期候选与动态确认（历史对照）

`agent_runtime.lifetime_candidates` 增加三类语法级候选：容器扩容/擦除后继续使用
引用、迭代器、指针或 span；协程帧销毁时，较早声明的 owner 在析构中访问较晚
声明且已先析构的对象；持有 `std::future` 的成员声明早于异步任务所访问的成员，
导致成员逆序析构时任务仍读已销毁状态。这些规则只产生候选和证据提示。

`tools/run_lifetime_reproductions.py` 使用 `-fsanitize=address`、帧指针和调试信息
构建三个机制夹具。2026-09-27 在 Apple Clang 17 上三例均返回匹配源码栈帧的
`heap-use-after-free`（3/3 `confirmed`）。确认器要求目标路径 marker 和候选源码
栈帧；无 ASan、未覆盖、超时、无关 ASan 报告或普通非零退出均保持
`inconclusive`。这 3/3 只证明规则机制与确认链路，不代表公开项目召回。

结构化产物位于 `.poc/project-uaf-v1/evaluation/`；生成器为
`tools/evaluate_public_uaf_ctu.py`。QLever 使用 Clang 21 解决了旧版对
`ConceptDecl`/`RequiresExpr` 的缺失支持；Ghidra 使用已验证的 Clang 14 构建镜像。


## 2026-09-27 分层 UAF 最终闭合

统一汇总的正式结果为：唯一 TU 覆盖 **228/228（100.0%）**；精确候选召回 **4/5（80.0%）**；取证召回 **4/4（100.0%）**；确认召回 **3/5（60.0%）**。完整测试为 **156 passed, 3 skipped**，`compileall` 通过。

### 定向 CTU 恢复

原有五个 defect-target execution 继续原样保留：Ghidra、Server、JoinAlgorithms 为 `complete`，ExternalIdTable 为 `partial`，CompressorStream 为 `timeout`。另对历史未覆盖的 9 个 QLever TU 各生成一个独立、禁网、5 GiB、1 CPU、CodeChecker `-j 1` 切片；9/9 均为 `complete`，每片都是 1 成功、0 失败、0 未完成：

- `src/engine/GroupByImpl.cpp`
- `src/engine/HasPredicateScan.cpp`
- `src/engine/IndexScan.cpp`
- `src/engine/QueryPlanner.cpp`
- `src/engine/SpatialJoin.cpp`
- `src/engine/TransitivePathBase.cpp`
- `src/engine/sparqlExpressions/ConditionalExpressions.cpp`
- `src/engine/sparqlExpressions/SparqlExpressionValueGetters.cpp`
- `src/engine/sparqlExpressions/StringExpressions.cpp`

QLever 合并 metadata 共 13 个 execution（11 `complete`、1 `partial`、1 `timeout`）；覆盖按成功源码路径并集计数，重叠 attempt 不重复累计。恢复 metadata SHA-256 为 `13e6861ca4e275b0cef5d23febf1c873f83570476e105aa57ffdd4c1d644738d`，合并 metadata SHA-256 为 `9b1c8f9f26a2fb4ca7d1a75e8e5868d8b35f0b5f719501d9c808656f2e72312f`。

### 真实 ASan 证据

| 冻结缺陷位置 | 终态 | 解释 |
|---|---|---|
| `src/util/CompressorStream.h:33` | `confirmed` | `heap-use-after-free`；同一报告块含候选文件编号栈帧。 |
| `src/engine/idTable/CompressedExternalIdTable.h:334` | `confirmed` | `heap-use-after-free`；同一报告块含候选文件编号栈帧。 |
| `src/util/JoinAlgorithms/JoinAlgorithms.h:1755` | `confirmed` | 直接调用冻结基线 `specialOptionalJoinForBlocks` 的 test-only reduction；报告块含原始头文件编号栈帧。 |
| `src/engine/Server.cpp:327` | `refuted` | ASan 插桩与目标 marker 均已验证，测试干净结束；只否定本次复现路径，不能泛化为缺陷不存在或代码安全。 |
| `Ghidra/.../sleigh.cc:205` | `inconclusive` | holdout 无精确候选，未据此调规则。 |

QLever 生产 `src/` 在取证后仍与冻结提交 `cf5c9d547403c5babac97d35b6cb4868825dbf98` 一致，仅有两个测试文件进入最终 patch。Join 构建/运行的 cgroup 峰值分别为 1.43 GiB/78.5 MiB，容器 `OOMKilled=false`；Server 分别为 4.86 GiB/352.5 MiB，亦为 `OOMKilled=false`。更早一次完整构建观测到精确 5 GiB，但最终状态仍为 `OOMKilled=false`，失败原因是测试使用冻结基线不存在的 API。无关 FSST UBSan 未对齐告警、间接 OptionalJoin clean run、断言失败及首次链接/编译失败均以独立前缀保存，没有改写或删除。

四项 ASan bundle SHA-256 为 `d3f3f0a0645ff8f8ff9d58978c114cef1e5dc2c9022c7878d31d68a28d2aca71`。统一 summary digest 为 `266bd4be08a75ae60599c363db1ae0cb07ec8dc636960ff07e71c82f02fcd000`，JSON 文件 SHA-256 为 `9ff88a081f294ee148755ec045d3e86e613eb3d98b51d0f1bb8a2b60f2b4a0a4`；其中 7 个输入摘要已逐一重新计算核对。

### 证据边界与技术限制

- 六类生命周期规则仍是保守语法启发式，不是完整 C++ 所有权或跨函数数据流证明；QLever 是 tuning，Ghidra 是 holdout。
- 精确候选召回要求规范化路径和行号同时匹配；取证分母只含 4 个精确候选；只有 `confirmed` ASan 证据计入确认召回。
- 旧 ExternalIdTable/CompressorStream execution 的 `partial`/`timeout` 仍保留。执行失败且无候选时是 `unevaluable`，不得伪装成 `missed`、成功或安全。
- Ghidra `sleigh.cc:205` 仍未命中；旧 CTU 的 `slghpatexpress.cc:330` 是未裁决的非目标候选。
- 本轮只闭合交接定义的工程门禁，不等于扩大样本、人工盲评或 Q-01～Q-07 发布质量门禁已完成。

产物：`.poc/project-uaf-v1/evaluation-next/layered-summary.json/md`、`layered-ctu-runs/qlever/metadata-with-recovery.json`、`layered-ctu-recovery/qlever/metadata.json`、`asan-evidence/evidence-all.json`、`asan-project-evidence/evidence.json`、`*-layered/lifetime-candidates-final.json`、`rules-freeze-release.json`。
