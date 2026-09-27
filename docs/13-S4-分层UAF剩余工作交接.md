# S4 分层 UAF 剩余工作交接

更新时间：2026-09-27（Asia/Shanghai）。交接所列本轮缺口已闭合；历史失败和仍未满足的发布质量条件继续显式保留，不推断为安全。

## 1. 最终结果

| 指标 | 原交接值 | 第一阶段闭合 | 最终门禁 |
|---|---:|---:|---:|
| 唯一 TU 覆盖 | 215/228（94.3%） | 219/228（96.1%） | **228/228（100.0%）** |
| 精确候选召回 | 2/5（40.0%） | 4/5（80.0%） | **4/5（80.0%）** |
| 取证召回 | 0/2（0.0%） | 2/4（50.0%） | **4/4（100.0%）** |
| 确认召回 | 0/5（0.0%） | 2/5（40.0%） | **3/5（60.0%）** |

统一结果：[layered-summary.md](../.poc/project-uaf-v1/evaluation-next/layered-summary.md)、[JSON](../.poc/project-uaf-v1/evaluation-next/layered-summary.json)。summary digest 为 `266bd4be08a75ae60599c363db1ae0cb07ec8dc636960ff07e71c82f02fcd000`，JSON SHA-256 为 `9ff88a081f294ee148755ec045d3e86e613eb3d98b51d0f1bb8a2b60f2b4a0a4`。

## 2. Targeted CTU 与恢复切片

原 defect-target execution：

| Target | Selected | Success | Failed | Unfinished | Status |
|---|---:|---:|---:|---:|---|
| `Ghidra/Features/Decompiler/src/decompile/cpp/sleigh.cc` | 1 | 1 | 0 | 0 | `complete` |
| `src/engine/Server.cpp` | 1 | 1 | 0 | 0 | `complete` |
| `src/util/JoinAlgorithms/JoinAlgorithms.h` | 6 | 6 | 0 | 0 | `complete` |
| `src/engine/idTable/CompressedExternalIdTable.h` | 34 | 3 | 31 | 0 | `partial` |
| `src/util/CompressorStream.h` | 79 | 1 | 13 | 65 | `timeout` |

缺失 QLever TU 的 singleton 恢复：

| Target | Status | Duration (s) | Peak memory |
|---|---|---:|---:|
| `src/engine/GroupByImpl.cpp` | `complete` | 376.82 | 1.93 GiB |
| `src/engine/HasPredicateScan.cpp` | `complete` | 41.84 | 1.32 GiB |
| `src/engine/IndexScan.cpp` | `complete` | 63.55 | 1.67 GiB |
| `src/engine/QueryPlanner.cpp` | `complete` | 105.99 | 1.58 GiB |
| `src/engine/SpatialJoin.cpp` | `complete` | 42.51 | 1.31 GiB |
| `src/engine/TransitivePathBase.cpp` | `complete` | 84.57 | 1.39 GiB |
| `src/engine/sparqlExpressions/ConditionalExpressions.cpp` | `complete` | 34.08 | 1.42 GiB |
| `src/engine/sparqlExpressions/SparqlExpressionValueGetters.cpp` | `complete` | 138.26 | 2.27 GiB |
| `src/engine/sparqlExpressions/StringExpressions.cpp` | `complete` | 52.88 | 1.70 GiB |

9/9 均为 1 成功、0 失败、0 未完成，且三组集合互斥、并集等于所选 TU。所有容器都使用 `--network none --memory 5g --cpus 1` 和 CodeChecker `-j 1`。恢复 metadata SHA-256：`13e6861ca4e275b0cef5d23febf1c873f83570476e105aa57ffdd4c1d644738d`；QLever 合并 metadata SHA-256：`9b1c8f9f26a2fb4ca7d1a75e8e5868d8b35f0b5f719501d9c808656f2e72312f`。合并后 13 个 execution 为 11 `complete`、1 `partial`、1 `timeout`；覆盖按成功路径并集计算，不因切片重叠重复计数。

## 3. ASan 动态证据

| Defect | Location | Result | Evidence boundary |
|---|---|---|---|
| coroutine destruction | `src/util/CompressorStream.h:33` | `confirmed` | `heap-use-after-free`，同一报告块含候选文件编号帧。 |
| async member destruction | `src/engine/idTable/CompressedExternalIdTable.h:334` | `confirmed` | `heap-use-after-free`，同一报告块含候选文件编号帧。 |
| local vocab propagation | `src/util/JoinAlgorithms/JoinAlgorithms.h:1755` | `confirmed` | test-only direct reduction 调用冻结基线实现；报告块含该头文件编号帧。 |
| query context lifetime | `src/engine/Server.cpp:327` | `refuted` | 已验证插桩、目标 marker 和 clean exit；只适用于本次 reproduction。 |
| Ghidra pointer invalidation | `Ghidra/.../sleigh.cc:205` | `inconclusive` | 无 exact candidate；holdout 未用于规则调参。 |

新增项目级 evidence 位于 `.poc/project-uaf-v1/asan-project-evidence/evidence.json`，SHA-256 为 `6f8277bb5be8c3a7921cd4f9c3ddf6fcba51cfed6ad17f9ad956a11a9057b524`。它与原 Compressor/External bundle 合并为 `.poc/project-uaf-v1/asan-evidence/evidence-all.json`，SHA-256 为 `d3f3f0a0645ff8f8ff9d58978c114cef1e5dc2c9022c7878d31d68a28d2aca71`；合并记录保留两个输入路径及各自摘要。 最终 test-only patch SHA-256 为 `1ee82c1d8d661bc605d1d774f1b315b962f72ee30eebf577084f5a8ab2cd8996`，ASan `compile_commands.json` SHA-256 为 `504ab4bc81ce8718dd8278741a46bac65346c6dee36c630b21c47a86f3c249ba`；最终 binary、stdout/stderr 和 memory-peak 摘要均保存在对应 evidence record。`build-history.json` 仅索引最初两次 full-build attempt，后续尝试及最终运行以各前缀记录和最终 `evidence.json` 为准。

最终 worktree 固定在 `cf5c9d547403c5babac97d35b6cb4868825dbf98`，生产 `src/` 无 tracked diff；只修改 `test/JoinAlgorithmsTest.cpp` 与 `test/ServerTest.cpp`，未移植生产修复。Join 构建/运行峰值为 1.43 GiB/78.5 MiB，Server 为 4.86 GiB/352.5 MiB，四个最终容器状态均 `OOMKilled=false`。一次更早的完整构建达到精确 5 GiB，但 inspect 同样为 `OOMKilled=false`；失败来自测试使用冻结版本不存在的 API，而非伪装成资源失败。

所有中间尝试均保留，包括首次依赖挂载失败、基线 API 编译失败、FSST 未对齐 UBSan、仍持有 owner 的 clean run、移动 owner 后断言和间接 OptionalJoin 未触发。最终 `confirmed` 只来自同一被识别 ASan UAF 报告块中的目标源码编号栈帧；Server `refuted` 不表示缺陷不存在或整个代码安全。

## 4. 规则与保守计分

六条冻结候选规则为 container invalidation、coroutine local destruction、async member destruction、returned resource owner、stack context escape、vector field copy。规则冻结文件 SHA-256 为 `a715347b3778867807ca3d3c64bea42def74fb327e9188ebf9b33a8bb19a3505`。QLever 是 tuning partition；Ghidra 是 holdout，未依据修复或 holdout 结果调整规则。

精确命中仍为四个 QLever 缺陷；Ghidra `sleigh.cc:205` 未命中。取证召回分母是四个 exact candidate，确认只计算 `confirmed`，因此 Server 的 conclusive `refuted` 增加取证召回但不增加确认召回。历史 `partial`、`timeout`、OOM、execution failure 或缺失证据均不解释为安全；失败目标无候选应为 `unevaluable`，而不是伪造 `missed`。

## 5. 验收清单

- [x] 真实执行 9 个 singleton CTU 恢复切片，9/9 `complete`。
- [x] 保留每个 execution 的独立产物、资源、日志、数据库摘要和成功/失败/未完成分区。
- [x] Join 得到真实 `confirmed`，Server 得到仅限本次路径的真实 `refuted`。
- [x] 原有与新增 ASan evidence 通过独立 merger 合并，冲突 identity 会被拒绝。
- [x] targeted CTU 原始与恢复 metadata 合并，历史 `partial/timeout` 未覆盖。
- [x] 统一 summary 重建并逐一验证 7 个输入 SHA-256。
- [x] QLever 冻结生产源码无修改，本轮命名容器无残留。
- [x] README、S4 质量记录、CTU 专题与本交接文档使用同一最终数字。
- [x] 全量回归 **156 passed, 3 skipped**；`python3 -m compileall -q src tools tests` 通过。

本轮工程门禁已闭合。仍未完成且不属于本轮“假成功”的事项：Ghidra holdout 标签仍未命中；两个历史大切片仍分别是 `partial`/`timeout`；语法规则仍不是完整所有权证明；更大样本、双人盲评和 Q-01～Q-07 发布放行继续独立推进。

## 6. 最终复现命令

```bash
# 合并 QLever 原始 target execution 与 9 个恢复 execution。
PYTHONPATH=src python3 tools/merge_targeted_ctu.py \
  .poc/project-uaf-v1/layered-ctu-runs/qlever/metadata.json \
  .poc/project-uaf-v1/layered-ctu-recovery/qlever/metadata.json \
  --output .poc/project-uaf-v1/layered-ctu-runs/qlever/metadata-with-recovery.json

# 合并四条 ASan evidence；不覆盖原二项 bundle。
PYTHONPATH=src python3 tools/merge_asan_evidence.py \
  .poc/project-uaf-v1/asan-evidence/evidence.json \
  .poc/project-uaf-v1/asan-project-evidence/evidence.json \
  --output .poc/project-uaf-v1/asan-evidence/evidence-all.json

# summarize_layered_uaf.py 使用 metadata-with-recovery.json 与 evidence-all.json；
# 其它 manifest、run、report、candidate、non-CTU、Ghidra 参数同上一版命令。
PYTHONPATH=src /opt/anaconda3/bin/python3 -m pytest -q
python3 -m compileall -q src tools tests
```

本机 `.venv` 未安装 pytest，因此全量回归使用已有 pytest 的 `/opt/anaconda3/bin/python3`。根工作区不是 Git 仓库，没有提交或推送；宿主外部访问若需重取 GitHub 材料，使用 `http://127.0.0.1:17891`，证据容器始终保持 `--network none`。
