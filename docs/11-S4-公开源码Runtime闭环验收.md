# S4：公开源码 Runtime 闭环验收

日期：2026-09-26。依据 [SPEC 009](../specs/009-public-source-runtime-integration.md)，承接 SPEC 008 Q-01/Q-02。该验收证明真实公开源码已接入 runtime，不证明完整语义规则或真实项目发布质量已达标。

## 实现与结果

| 范围 | 本轮交付 |
|---|---|
| 通用源码查询 | `FrozenSourceProgramQuery` 绑定固定源码摘要，提供受限、只读的行窗口；跨快照、策略、路径、行数和字节越界被拒绝。 |
| 分析输入隔离 | `analysis_input` 只允许源码归档、路径和提交进入分析；golden、fix metadata、review comments 在 Runtime 裁决后离线匹配。测试含标签污染输入。 |
| 真实 Evidence | Tesseract 的新增 return 文本候选自动发现，原始源码窗口持久化为 Evidence，文字可用性 Fact 与 Check 绑定同一 Task。 |
| 独立角色 | 确定性 reference reader 和真实 Claude SDK 分别运行。真实 SDK 调查/验证使用两个独立 Session，各自读取保存的源码与 Joern 原始 Evidence；reference 不代表模型效果。 |
| 保守裁决 | 纯源码运行保留 `semantic_validation` 未查项；Joern 回放运行把它推进到 `partial/unknown`。构建失败进入分析状态和覆盖分母。结果均为 `inconclusive`，不发布告警、不创建 Exclusion。过度自信角色测试也不能完成语义检查。 |
| 失败与预算 | 新增 `execution_failure` 计数；SDK 超时、正常弃判、候选未执行分别报告。候选执行上限、实际后端查询上限、单角色超时可约束；Token 上限及实际 SDK 用量仍不可得。 |
| 可复现代码与存储 | 执行前保存本包和入口源码及摘要；代码在运行中改变则拒绝导出。SQLite 重开后的 runtime 报告与原报告一致。 |
| 报告可读性 | Markdown 报告显示执行器、模型路由、失败/超时/未运行数量、具体覆盖遗漏及预算限制。源码扫描的覆盖不冒充构建或语义索引覆盖。 |
| 数据摘要 | 重新下载核验 benchmark JSON 与四份源码归档；`.poc/s4-dataset-verification.json` 的 manifest digest 已与当前清单一致。 |
| 隔离构建 | 新增固定 Ubuntu 22.04/Clang 14 镜像与构建入口。Tesseract 的 manifest 命令在禁网、只读根文件系统、无宿主凭据和受限资源的容器中逐条执行；配置成功，目标在 `hocrrenderer.cpp:503` 编译失败。报告保存镜像 ID、183 项包清单、stdout/stderr、216 项编译数据库及其摘要。 |
| Joern 语义探针与回放 | 受限 `inspect_unreachable_after_return` 操作只接收已授权路径、简单函数名和有界行号，不接收 DSL。完整验签源码与捕获的编译数据库生成 build-aware CPG；第 504 行 return 的逆向 CFG 到达方法入口，第 505 行语句未到达。入口核对 manifest、commit、归档、构建报告、编译数据库、候选位置和原文摘要，再通过只读 `recorded-joern` 后端保存原始查询。因目标构建失败，结果保持 `partial`，未触发确认或发布。 |
| 可复用 Pipeline | 新增公开 `CandidateDiscoverer`、`EvidencePlan` 与 `AnalysisPipeline`；S4 词法发现器、recorded-Joern 后端、不可达代码评价器和确定性 Evidence 执行器已进入安装包。组合后端和端到端测试证明第三方组件可替换，并覆盖确认、反证、弃判和候选预算。 |

### 公开样本结果

固定数据仍是 `public-cpp-smoke-v1` 的两个公开 C++ 样本：

- holdout `nlohmann/json#5163`：零候选、零角色调用，已知正例漏检。
- tuning `tesseract-ocr/tesseract#4138`：一个文本候选，隔离构建为 `build_failure`；确定性控制和真实 SDK 的调查/验证角色均读取源码与 build-aware Joern 两份原始 Evidence，`semantic_validation=partial/unknown`，保持 `inconclusive`。真实 SDK 两个 Session ID 不同。

因此目标覆盖为 **2/2**，翻译单元覆盖为 **0/216**，候选召回为 **1/2**，取证召回为 **0/1**，确认召回为 **0/2**，精度不可得。候选发现沿用原文本算法，召回未提升；本轮新增构建输入与失败证据的可验证传递。成功构建、完整语义取证、人工告警复核和更多保留样本仍是后续工作。

运行通过本地 CC Switch `127.0.0.1:15721`，SDK 使用 `claude-sonnet-5` 角色别名。网关未向当前适配器回传实际上游模型，报告记为 `actual_upstream=unavailable`；不能由别名推断实际模型。Token/费用也保持 `unavailable`。没有把这次运行作为原生 Claude 性能、同预算比较或发布放行成绩。

## 验收映射

| 项 | 结果 |
|---|---|
| RI-01/RI-02 | 标签隔离、固定源码绑定、范围和超限回归通过。 |
| RI-03/RI-04 | 公开源码 Evidence、两角色原文读取与真实独立 SDK Session 已实测。 |
| RI-05/RI-06 | 故障、超时、候选/查询预算和过度自信回归通过；未查项保留。 |
| RI-07 | 公开样本持久报告重开一致；导出通过 SPEC 008 校验并生成报告。 |
| RI-08 | Joern 报告与原文摘要绑定、篡改拒绝、partial Check 和两角色读取已实测。 |
| RI-09 | 安全解包、路径与链接拒绝、构建原文保存、包清单与编译数据库摘要绑定、篡改拒绝及失败状态传播已实测。 |
| RI-10 | 四类公开扩展、统一 Pipeline、组合查询路由和包外示例已实现；内置组件与第三方测试组件均通过端到端运行。 |

完整测试 **85/85 通过**。SPEC 009 的首版集成、隔离构建记录、可复用 Pipeline 和 build-aware partial Evidence 回放通过；SPEC 008 的 Q-01/Q-02/Q-04 完整质量门禁仍未通过。公开数据可能已有训练污染，本轮未检验泛化。

本轮 `dist/s4/` 的 `0.2.0` wheel/sdist 已通过包外安装、核心无 SDK 强制依赖、外部两规则/两后端、持久报告、精确排除与 MIT 验证，记录为 `.poc/s4-release-verification.json`。尚未发布到包索引。

最新实测产物：

- [隔离构建报告](../.poc/s4-tesseract-container-build-report.json)：状态 `build_failure`，报告摘要 `6d574d0e32282d6aa2e8cfa72a30e4c5b8811fd297c73e4733316cd54fee06bd`，镜像 ID `sha256:4d1aa9423a4a8dbd4929e4784b21417d3d3812d8f2fc64ce266116f4fc5250d5`，包清单摘要 `7491d221cf44bf7d95acf3794019b4c720b5f91e5099e255e158a42c52ad06e0`，规范化编译数据库摘要 `f8e800109d3e1d31b4056b98757a6f125cddb975cc0575311a758285b90cc90a`。
- [build-aware Joern 探针](../.poc/s4-joern-build-aware-report.json)：CPG 摘要 `5b8952dcf4de5a8cf3dac0eec415b6512a39c8ec78075d973aa00ff69a4133d8`，固定查询原文摘要 `d3e773b74edc46c92655d4ec2f2577a08efc12010428756d37e20f620a4a7b5b`，结论为 `partial_semantic_evidence_only`。
- [真实 SDK build-aware 运行](../.poc/s4-runtime-claude-build-aware-run.json)与[质量报告](../.poc/s4-runtime-claude-build-aware-report.md)：迁移到公开 Runtime 组件后两个角色仍在不同 Session 完成，均读取源码与语义原文，零超时、零角色执行失败；报告摘要 `accd06c675e06814b9cdb553890b03d15d9790dd5866e2bd55dd174063699f8b`。样本构建失败，实际上游与用量不可得。
- [确定性 build-aware 控制运行](../.poc/s4-runtime-reference-build-aware-run.json)与[质量报告](../.poc/s4-runtime-reference-build-aware-report.md)，用于对照连接和持久化机制。
- 最新真实运行代码快照为 `.poc/s4-runtime-claude-reusable-v1/code-snapshot/`，`code_version=04657f4e9a51b86c5395ed5e6e46575321a7863880bd66cdc2cc13c98d267dcb`；摘要与导出运行绑定已经核对。

## 复现

`--work-dir` 必须是新目录，防止不同运行状态混用。下载可继续通过当前 0cloud 代理：

```bash
docker build --pull=false \
  -t agent-runtime/tesseract-build:ubuntu22-clang14 \
  evaluation/builds/tesseract-ubuntu22-clang14
PYTHONPATH=src .venv/bin/python tools/run_s4_container_build.py \
  --manifest evaluation/manifests/public-cpp-smoke-v1.json \
  --sample-id tesseract-pr-4138 \
  --image agent-runtime/tesseract-build:ubuntu22-clang14 \
  --work-dir .poc/s4-tesseract-container-build-next \
  --timeout-seconds 600 \
  --output .poc/s4-tesseract-container-build-report.json
PYTHONPATH=src .venv/bin/python tools/run_s4_joern_unreachable_probe.py \
  --manifest evaluation/manifests/public-cpp-smoke-v1.json \
  --sample-id tesseract-pr-4138 \
  --build-report .poc/s4-tesseract-container-build-report.json \
  --work-dir .poc/s4-joern-build-aware-next \
  --joern-home .poc/tooling/joern-v4.0.636/joern-cli \
  --java-home .poc/tooling/jre21/jdk-21.0.12.1+1-jre/Contents/Home \
  --output .poc/s4-joern-build-aware-report.json

HTTPS_PROXY=http://127.0.0.1:17891 HTTP_PROXY=http://127.0.0.1:17891 \
PYTHONPATH=src .venv/bin/python tools/run_s4_runtime.py \
  --manifest evaluation/manifests/public-cpp-smoke-v1.json \
  --build-report .poc/s4-tesseract-container-build-report.json \
  --joern-report .poc/s4-joern-build-aware-report.json \
  --executor claude --cc-switch --role-timeout 90 --max-candidates 2 \
  --work-dir .poc/s4-runtime-claude-build-aware-next \
  --output .poc/s4-runtime-claude-build-aware-run.json

PYTHONPATH=src .venv/bin/python tools/generate_s4_report.py \
  --manifest evaluation/manifests/public-cpp-smoke-v1.json \
  --run .poc/s4-runtime-claude-build-aware-run.json \
  --json-output .poc/s4-runtime-claude-build-aware-report.json \
  --markdown-output .poc/s4-runtime-claude-build-aware-report.md

.venv/bin/python -m unittest discover -s tests -q
```

将 `--executor claude --cc-switch` 换为 `--executor reference` 并另选新目录，可以无需模型验证传输与持久化。真实调用需要本地网关运行且 OAuth 有效；不会创建公开 PR、评审评论或外部消息。

本地保存：构建原文与摘要、编译数据库、Joern 报告、运行 JSON、质量 JSON/Markdown、每个样本的 SQLite/原文/runtime 报告、执行代码快照及摘要。均位于 `.poc/`；构建入口只将安全解包后的固定源码挂载到受限容器。

## 下一步

解决 Tesseract 当前构建失败或选择可成功构建的固定版本，再由规则 Oracle 解释已经登记的 partial CFG Evidence；随后扩充适用样本与近邻反例、完成真实跨会话排除验证，再冻结质量阈值进行保留集盲评。首版通用源码后端无需为每种规则重复实现。
