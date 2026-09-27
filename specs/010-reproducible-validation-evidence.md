# SPEC 010：可复现构建与执行证据

状态：VE-01～VE-06 已由真实隔离执行、公开源码记录和 Runtime 回放闭合 · 2026-09-27。承接 SPEC 008 Q-01/Q-02/Q-04/Q-05 与 SPEC 009 后续工作；基础包不内置具体语言的最终缺陷判定。

## 目标

让调用方把隔离执行器产生的构建/运行记录作为 `ProgramQuery` 材料接入 Runtime。记录须绑定源码、工具链、依赖、配方、测试输入和原文摘要。`complete` 只指一次限定执行的记录完整，不能据此推断未执行路径安全。明确执行失败、构建失败、超时、输出上限与实际观察到的程序失败之间的区别。

首个示例采用公开 MIT C++ 项目 `yhirose/cpp-peglib`：固定上游真实父提交 `14305f9f53cde207568f21675a1b9294a3ab28b4` 与修复提交 `0061f393de54cf0326621c079dc2988336d1ebb3`。这是开发集中的历史复现，不进入原 `public-cpp-smoke-v1` 保留集，不宣称盲评成绩。两侧使用同一受控 API 探针、编译器及配置；只验证指定输入的 AST 路径。来源：[issue #121](https://github.com/yhirose/cpp-peglib/issues/121)、[修复](https://github.com/yhirose/cpp-peglib/commit/0061f393de54cf0326621c079dc2988336d1ebb3)。

## 验收契约

| ID | 要求 | 实际验证 |
|---|---|---|
| VE-01 | 通用已记录验证后端，只允许注册 ID；记录摘要、Snapshot 源码/策略/范围全部匹配；模型不能传命令 | schema 1.0 canonical bytes/ID 保持兼容；schema 1.1 增加隔离、termination 和资源观测；跨快照/策略/ID、改写 artifact、添加 `argv` 均拒绝 |
| VE-02 | 执行器净化环境，禁网、拒绝宿主私有文件、限制可写目录及 CPU/墙钟/输出，隔离不可用即失败 | registry-only Docker executor 的 normal、nonzero、signal、timeout、output-limit、space 六个真实 attempt 全部通过 containment 与 cleanup 门禁 |
| VE-03 | 源码与编译器输入前后摘要一致；编译器/SDK 版本、规范化配方、实际头文件依赖摘要和二进制摘要保存 | cpp-peglib base/fix 各构建一次，同 revision 的两个输入复用同一二进制；四条 schema 1.1 record 完整绑定源码、工具链、依赖、配方和产物 |
| VE-04 | 程序异常退出只有与具体源码、输入、观察及阶段匹配时才支持具体命题；成功测试不能证明全库安全 | `base/ignored` 在 parsed phase 后触发 UBSan 与 ASan，ASan 同一报告块含 `peglib.h:3650` 编号栈帧；`base/ordinary` 与 `fix/ordinary` 只形成 scoped negative，`fix/ignored` no-trigger 保持 unknown |
| VE-05 | 规则评价器由应用注入；调查/验证独立引用记录；领域证据与检查门禁仍生效 | 四案例均由独立 investigator/verifier attempts 读取 Runtime Evidence；结果为 1 `confirmed`、2 `refuted`、1 `inconclusive` |
| VE-06 | 相同反例跨进程重开后复用精确排除；不同输入/版本/正例不被抑制；原文损坏拒绝复用 | SQLite close/reopen 后 exact identity 在调用 Agent 前复用；revision/source/input/record/toolchain/recipe/observer 任一改变均不复用；外部 artifact 与 Runtime bundle 篡改均 fail closed |

## 0.3.0 实施结果

### ValidationRecord 1.1 与只读回放

- schema 1.0 的精确字段、canonical bytes 与 record ID 保持不变；1.1 增加 `ValidationTermination`、`ValidationIsolationOutcome`、build/execution resource observation artifact，以及 `observed | unavailable` 资源状态。
- record 继续只容纳封闭类型、整数和内容寻址引用；`command/argv/env/cwd/image/mounts` 等执行字段被拒绝。
- `validation_evidence_bundle_bytes()` 将 record 与全部引用 artifact 打成确定性 Runtime Evidence bundle。schema 1.1 的 `RecordedValidationProgramQuery` 必须提供 artifact root，并在构造及每次查询时重验原文。
- replay 保守映射：隔离失败、OOM、output-limit、launch/cleanup/executor failure 为 `failed`；timeout 为 `timeout`；no-trigger/not-run 为 `partial`；只有隔离通过、构建成功、执行完整并匹配目标观察才为 `complete`。

### VE-02 隔离执行

固定 suite/attempt registry 是唯一执行入口；调用方与模型不能提供命令、参数、环境、工作目录、镜像、挂载或 Docker flags。执行器固定 `--network=none`、只读根、非 root UID/GID、drop all capabilities、`no-new-privileges`、pids/memory/cpu/ulimit、有界 tmpfs、只读 source/runner mounts 和最小环境。Docker、镜像或探针不可用时 fail closed，不回退宿主。

当前镜像 `agent-runtime/clang-ctu@sha256:4f3a6f74ec2dcabbe51d95744a0d6afef145bb09fda347bd5a866fd3dd85eff8` 上，六个 self-test attempt 均确认隔离探针通过且容器清理成功：normal=`exit 0`、nonzero=`exit 23`、signal=`SIGKILL`、timeout=`timeout`、output=`output_limit`、space=`exit 0`。timeout 与 output-limit 是预期边界观察，不是目标程序安全证据。小型摘要见 [`docs/evidence/spec010-isolation-v1.json`](../docs/evidence/spec010-isolation-v1.json)。

### cpp-peglib 四象限与 Runtime

同一 recipe/toolchain/policy 下的真实结果：

| revision/input | 动态观察 | 查询状态 | Runtime 裁决 |
|---|---|---|---|
| base/ignored | `ast_optimizer_invalid_access`；UBSan `peglib.h:3647/3650`，ASan `SEGV on unknown address` 且同一 block 有 `peglib.h:3650` frame | `complete` | `confirmed` |
| base/ordinary | `optimized_completed` | `complete` | scoped `refuted` |
| fix/ignored | `no_matching_observation` | `partial` | `inconclusive` |
| fix/ordinary | `optimized_completed` | `complete` | scoped `refuted` |

修复侧 no-trigger 没有被解释为安全或 refuted；两个 scoped negative 也只覆盖各自 revision/input/configuration。完整 record/result ID、候选 digest 和完整性门禁摘要见 [`docs/evidence/spec010-cpp-peglib-v1.json`](../docs/evidence/spec010-cpp-peglib-v1.json)。原始 CAS、日志、SQLite 与失败 attempt 只保留在忽略的 `.poc/`，不进入发行包。

### 固定 CodeQL 2.27.1 实证

官方 `codeql-bundle-v2.27.1` 已按 archive SHA-256 安全解包并绑定 37,160 个文件的 tree manifest、CLI、resolved qlpacks、query、pack、lock 与生成的 QLX。固定 package rule `cpp/potential-access-after-delete-candidate` 对四个真实数据库各执行两次，且每次都复制 canonical database 到 fresh scratch：

- positive fixture：1 个稳定语义结果，位置 `positive.cpp:6:10-14`，随后由 `CodeQLReplayProgramQuery` 真实 replay 为 `complete`；
- nearby control、cpp-peglib base、cpp-peglib fix：均为 0 result、`partial`，不能解释为安全、missed 或检测率；
- 两次 normalized semantic result 一致，fingerprint 稳定，canonical databases 未被原地修改；result ID 不依赖 fingerprint；
- SARIF URI 仅接受 capture source root 下已注册 base，兼容 CodeQL 2.27.1 的精确隐式 `%SRCROOT%`，拒绝 traversal、absolute/cross-root 和未注册 base。

该结果证明固定候选查询的 compile/database/analyze/replay 链路，不是缺陷 verdict，也不构成真实检测率估计。摘要见 [`docs/evidence/codeql-v2.27.1-live-v1.json`](../docs/evidence/codeql-v2.27.1-live-v1.json)。CodeQL CLI、QLX、database、SARIF 和 scratch 均为外部忽略生成物。

## 发行门禁

0.3.0 全量回归通过：`pytest` 203 passed、3 skipped；`unittest` 兼容发现 172 tests、3 skipped；`compileall` 通过。wheel/sdist 已离线构建，并在 checkout 外干净虚拟环境验证公开导出、schema 1.0 兼容、1.1 新导出、Docker adapter、旧 Joern 导入、CodeQL query pack/lock、只读 validation CLI 与外部消费者样例。摘要见 [`docs/evidence/s3-release-0.3.0.json`](../docs/evidence/s3-release-0.3.0.json)。

## 实施边界

公开核心仍是只读 `RecordedValidationProgramQuery`；执行能力位于 registry-only 应用适配器，不加入 `defect-validation-record` CLI，也不提供任意命令入口。调用方可用受控容器或远端执行器产出同契约记录。资源限制和观测只证明本次固定执行的边界；未执行完整项目构建、所有测试或所有配置时必须列明遗漏。

编译与执行记录分别保存。真实崩溃是指定路径的正证据；成功或 no-trigger 只说明该输入下的观察。`partial`、timeout、OOM、output-limit、execution failure 和缺失证据不得改写为安全、missed 或确定负证据。人工仲裁仍待独立审查，不能由脚本或 Agent 模拟。
