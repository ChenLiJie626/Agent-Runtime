# 空返回规则插件 PoC 历史记录（原 S2）

状态：**后续规则插件探索记录；不是当前基础包 S2 门禁** · 2026-09-25。原 S2 计划下的实体夹具、Clang 基线、Joern 六种受限查询的部分覆盖 Evidence、F-05 构建差异与真实超时已实测；Claude SDK 经目标 `gpt-6-sol` 路由的会话、工具、角色隔离及底层中断探针通过。空返回完整语义证明和 F-08 仍待插件阶段完成。当前 S2 基础包门禁见[规格导航](../specs/README.md)和[SPEC 000](../specs/000-runtime-kernel.md)。以下把预期事实与工具观测分开；已有 EvidenceRef 均为 `partial`，不能用于确定裁决。

## 可复现输入与产物

源码保存在 `fixtures/null-return/{base,head}`。`tools/run_null_return_poc.py` 在空目录中生成两个 Git 提交、两侧源码与编译数据库，并逐翻译单元运行 Clang 语法检查和静态分析。命令：

```bash
python tools/run_null_return_poc.py --output .poc/s2-null-return
python -m unittest discover -s tests -v
# 安装 Joern 与 JRE 后，在同一实体夹具上运行固定查询：
python tools/run_joern_inventory.py \
  --fixture .poc/s2-null-return \
  --output .poc/s2-joern \
  --joern-home /path/to/joern-cli \
  --java-home /path/to/jre/Contents/Home
```

本机产物在 `.poc/s2-null-return-v3/`，其中 `manifest.json` 记录工具版本、命令、耗时、状态、源码/编译数据库及原始 stdout/stderr/SARIF 的 SHA-256；`raw/` 保留原文。生成目录默认不纳入版本控制，换机需重跑。脚本本身的 SHA-256 也在 manifest 中。提交的源码仅 `buffer.cpp` 不同，Head 新增 `count == 0 → nullptr`。

| 项目 | Base | Head |
|---|---|---|
| 提交 | `c7de9e3522336a56adab511131f7bf7025072218` | `70e63e0fe49c0205e49e3a965046611a57a6af32` |
| 树 | `c1132e9d9f2bff4eb8f826be3505c80e87d18d94` | `adf1806dfe703c7a8e5cc59e49e8278dbabcaa58` |
| 规范化编译配置摘要 | `2ad69fe358ac3da210fc6f4850720f3b8be85a46bed4fb0c5b5e416fe6375824` | 相同 |

实际 `compile_commands.json` 包含各自输出目录的绝对路径，所以文件字节摘要不同；规范化摘要用于证明此夹具的编译参数相同。Clang 为 `Apple clang version 17.0.0 (clang-1700.0.13.5)`。两侧各两个翻译单元的语法检查与 `--analyze --analyzer-output sarif` 均成功，四份 SARIF 的告警数均为 **0**。Base 默认入口执行返回 7，Head 零输入的 `guardedProcess` 测试入口执行返回 0。Head 默认 `main → process(0)` 具有空解引用，不以执行未定义行为作为测试手段。

Joern 固定查询使用 `v4.0.636` 与 Temurin JRE `21.0.12.1`。发行 ZIP 经 GitHub 发布资产的 SHA-256 `ef897bb86ecdc23cbf7a7ad966bd596962d814490eb1b5fcce7654932c0d073a` 校验；JRE TAR 经其 GitHub 发布资产 SHA-256 `dec50fc6f9fcd4fe3ae8cabf5a5fa68f6afc48841f7698e468e9aa5d54beed84` 校验。本机通过 0dcloud 系统代理 `127.0.0.1:17891` 下载，工具只解压到 `.poc/tooling/`，没有改系统 Java。`tools/joern_inventory.sc` 是固定的 PoC 查询，不接受 Agent 传入 DSL。`tools/run_joern_inventory.py` 用两侧编译数据库分别建图，保存命令、耗时、stdout/stderr、CPG 与脚本摘要。本机最终原文在 `.poc/s2-joern-v4/`：

| Joern 产物 | Base SHA-256 | Head SHA-256 |
|---|---|---|
| CPG | `cf2db1ed59aff6538039e0d2dd0b99ff49a1994982fdb71223584023a127896e` | `aab5691397576b9c50e872d9675d1c7441b7f41bdfa94a6911fc46cd43850d7a` |
| 查询原文 stdout | `f1d563d84578182f94f451c477fad8c81ffe35ad6b886bc97cf72311a9171ae4` | `e488ef27ab7cf12886f4ab24e04d7a5c00b502182ce11a8f22546d655fb4d276` |

两侧 `c2cpg` 和查询进程退出码均为 0，`--log-problems` 没有记录解析错误。图查询日志明确提示 `callback(n)` 的动态目标未能链接。上表 CPG/固定查询产物是工具原文，未逐查询登记；上述 SHA 不能冒充 Evidence ID。

另以包内 `JoernProgramQuery` 首版对 Head CPG 执行 `get_function(getBuffer)` 与 `find_callers(getBuffer)`，并通过 `DefectRuntime.query_program` 登记原文与 Evidence。该适配器只接受简单符号，脚本为包内固定资源；两个操作均保守返回 `partial`。结果在 `.poc/s2-runtime-joern-v1/report.json` 与相邻的 SQLite/内容寻址原文目录：

| 操作 | Evidence ID | 原文 SHA-256 | 限制 |
|---|---|---|---|
| `get_function` | `9be160ce-b684-4493-ae73-44541d142562` | `1ef60e77d3c80156cc2cd7760e5a9e5bf57c651ccd14007d13548042a5ac0c43` | 尚未证明唯一解析函数及构建内解析全集。 |
| `find_callers` | `8e5b092b-ccff-46b6-a0f8-2ab338c7857d` | `41a5cb11b3fda09712aa44225d0d8ba8d3973ca1c84d0afd85f1a02dee787574` | 间接调用、调用点位置及解析全集未完成。 |

候选仍为 `unassessed`，没有完成六项检查，也没有生成 Verdict 或 Exclusion。上表 ID 只在本次运行时存储中有效。

受限 Joern 适配器第二轮新增 `get_guards(guardedProcess)`，只接收授权的简单符号，固定脚本提取控制结构。`.poc/s2-runtime-joern-v6/report.json` 将 C1 的 `get_function/find_callers` 和 C2 的 `get_guards` 分别登记在两个 Task；C2 得到 Evidence ID `afb4c613-976d-4a40-a717-f3142afc4e92`、原文 SHA-256 `a57ddf036bdd44e15083dc0a124a3cfffa062d237580790ea9dedc2e608307df`。原文包含第 9 行 `if (n == 0) { return 0; }` 的 IF 节点、真分支 AST（含第 10 行 `return 0`）、解引用的 1 个控制依赖节点和 9 个支配节点；状态仍为 `partial`，因为尚未联合证明零值传播、可行路径及早退对该使用点的排除，不能据此生成 C2 Exclusion。同轮将 `find_callers` 截止时间设为 1 ms，真实 Joern 进程返回 `timeout`；原文摘要 `0cc0a7a35b16a7a425a6fb95f0cf8d6fe6f55608efc38598bcd48d13aa9cc3f2` 可读，未生成 EvidenceRef，`reachability` Check 保持 `unexamined`。这是 F-07 的实际后端故障注入。

第三轮将 `map_arguments`、`trace_value`、`check_reachability` 加入固定 Joern 脚本，输入只接受授权符号和有界调用行号/结果数。`.poc/s2-runtime-joern-v10/report.json` 在两个 Task 上保存了 8 次查询（六种操作）的原文与 Evidence：`map_arguments` 对第 4、12 行分别观测到 `getBuffer(n)` 的实参 `n`（索引 1）及 `getBuffer` 形参 `count`（序号 1）；`trace_value` 在 `process` 和 `guardedProcess` 内各观测到 `getBuffer(n) → value → *value` 的局部流；`check_reachability` 观测到 `main` 第 21 行零实参经 `process(int n)` 到第 4 行 `getBuffer(n)` 参数的数据流。三项均为 `partial`：同名目标绑定、跨函数空返回、路径条件与对象同一性尚未联合证明。`check_reachability` 的 Evidence ID 为 `f29ff719-8805-4878-80d7-84f7278841ae`，原文 SHA-256 为 `2f925f5c63f5c3fca3b7f27081298335955af80068238eb6b77afbc05e3c19cb`。局部流不是可执行路径证明，零结果也不能用于全局不可达反证。

F-05 用 `tools/run_f05_build_variant.py` 在同一 Base/Head 源码上只给 Head 编译数据库加入 `-DF05_BUILD_VARIANT=1`，Clang 两个翻译单元语法检查、Joern 建图和受限查询均实际运行，原文与摘要见 `.poc/s2-f05-build-mismatch-v2/report.json`。Base 规范化构建摘要 `2ad69fe358ac3da210fc6f4850720f3b8be85a46bed4fb0c5b5e416fe6375824` 与 Head 的 `a8ca230592210991e64d21823ff599106db2b4a732cb293f0712770f13b6fa5d` 不同；`builds_comparable=false`，Joern 查询仍为 `partial`，`change_attribution` Check 保持 `unexamined`，没有生成确定的“Head 引入”结论。此变体证明构建不同比较必须受阻，不证明新增宏改变了样例代码语义。

| 检查 | Joern 实际观测 | 能力边界 |
|---|---|---|
| `nullable_source` | Head 有 `nullptr` 字面量及 `getBuffer` 对应方法源码；Base 无该字面量。 | 图节点支持定位源代码；`nullptr` 返回值到所有调用点的跨函数流未由本次查询证明。 |
| `propagation` / `dangerous_use` | 两侧各有两个 `getBuffer(n)` 调用和 `*value`；以解引用的参数节点为 sink，`getBuffer(n) → value → *value` 局部流出现。 | 以解引用调用节点为 sink 返回空流；对象同一性和跨函数返回流仍是 partial。 |
| `reachability` | `main` 的 `process(0)` 调用点及零实参到 `getBuffer(n)` 参数的流出现。 | 没有联合 `count == 0 → nullptr → 同一对象解引用` 的可行路径证明。 |
| `guard` | `guardedProcess` 的条件、true 分支 `return 0`、解引用控制依赖于 `n == 0` 均出现。 | `controlledBy` 本身没有给出分支极性；仅靠该输出尚不能自动排除 C2。 |
| 调用者覆盖 | `callIn(getBuffer)` 找到 `process` 和 `guardedProcess` 的两个直接调用；日志提示 `indirectEntry` 中的 `callback(n)` 无法链接。 | `find_callers` 仅能宣称直接调用范围，含间接调用的全集为 `partial`。 |
| `change_attribution` | 实体 Git 差异仅为 Head `buffer.cpp` 增加空返回，规范化编译配置摘要相同。 | 这是夹具/构建证据；尚未和 Joern 图路径合成自动可核查的 Head 归因。 |

## Oracle 与目前观测

| 变体 | 夹具 Oracle | 本次观测 / 后端限制 |
|---|---|---|
| F-01 C1 | 同构建下具体 `main → process(0) → *getBuffer(0)`，完整路径证据可确认 | Joern 找到必要的局部材料，但 `nullptr` 返回值到具体解引用的整条流查询为 `List()`；可行性/对象同一性尚未证明，运行时结论仍应为 `inconclusive`。Clang 零告警不改变此结论。 |
| F-02 C2 | `guardedProcess(0)` 在解引用前退出，可在该路径排除 | 真实执行返回 0；Joern 提供 guard 的 true 分支早退和解引用控制关系，但当前查询未给出足够的分支极性/完整路径依据。尚不创建 Exclusion。 |
| F-03/F-04 | 直接调用枚举不覆盖间接调用 | Joern 枚举出两个直接调用，并明确记录 `callback(n)` 无法链接；含间接调用者的覆盖为 `partial`。 |
| F-05 | 不同构建配置不能作 Head 归因 | 主夹具两侧配置相同；真实 F-05 变体只给 Head 增加宏，Clang/Joern 成功，构建摘要不同且归因检查未完成。 |
| F-07 | 超时/部分图不能转为反证 | Joern 1 ms 截止时间产生真实 `timeout`；原文已保存可读，无 EvidenceRef，Check 仍未查。 |
| F-08 | 新 SDK session 延续 C2 排除记忆 | 领域层已有固定材料契约测试；真实 SDK session 联测尚未运行。 |

F-05/F-07 已补固定材料领域契约测试：配置摘要不同时裁决为 `inconclusive`；查询超时的部分原文可读取，但不会生成可用于完成检查的 EvidenceRef。F-07 已另以真实 Joern 超时验证；F-05 已另以真实构建差异变体联测；两侧配置不同时仍不能作 Head 归因。

Clang 本次按翻译单元独立运行，未启用跨翻译单元分析。官方 [Clang 文档](https://clang.llvm.org/docs/analyzer/user-docs/CommandLineUsage.html)说明 `--analyze` 与 SARIF 输出及 CodeChecker 编译数据库流程；[跨翻译单元说明](https://clang.llvm.org/docs/analyzer/user-docs/CrossTranslationUnit.html)另有 CTU 配置。因此零告警只是一项独立基线信号。

## 空返回插件未完成事项

1. Joern 固定查询已归档，六种操作已映射到受限 `ProgramQuery` 并保存部分覆盖 Evidence。还需深化 `get_guards`、跨函数返回传播和条件路径分析，明确源码位置、可行性与解析全集。C1 的跨函数空返回流和 C2 的受保护路径仍需更强证明或明确保持 `inconclusive`。Joern 官方 [数据流查询](https://docs.joern.io/cpgql/data-flow-steps/)并不保证条件路径可行，数据流路径不能单独当可行路径证明。
2. F-07 的真实超时已联测。F-05 构建差异已联测；F-03/F-04 的间接调用覆盖目前在公开 QueryOutcome 中以通用遗漏项表达，还需记录具体未解析调用点及解析全集。
3. `tools/smoke_claude_sdk.py` 已使用 SDK `0.2.159` 与 CLI `2.1.259` 实际发起受限会话。直连首个请求返回 API `403`、费用 `0`、无结构化输出；摘要在 `.poc/claude-sdk-smoke.json`。设置 0dcloud 代理后仍以 `api_error` 结束，见 `.poc/claude-sdk-smoke-proxy.json`。本机 CC Switch 现为 **3.20.4**，独立 ChatGPT OAuth 重新登录成功；Claude 本地路由已启用，当前模型角色映射到 Codex 配置的 `gpt-6-sol`。最初目标请求上游返回 `400`；在 3.20.1 与 3.20.4 均复现，报告为 `.poc/claude-sdk-smoke-cc-switch.json` 和 `.poc/claude-sdk-smoke-cc-switch-3204.json`。OpenAI 的[模型目录](https://github.com/openai/codex/blob/main/codex-rs/models-manager/models.json)将 `gpt-6-sol` 最低客户端版本列为 `0.155.0`；CC Switch 3.20.4 的[OAuth 代理代码](https://github.com/farion1231/cc-switch/blob/main/src-tauri/src/proxy/providers/codex_oauth_auth.rs)发送 `0.153.4`。据此推断版本头是拒绝原因；在 CC Switch 提供方「Header 覆盖」加入 `version: 0.155.0` 后，同一目标模型请求成功。CC Switch 以 Claude 角色别名接收 SDK 请求再映射到 GPT，上游模型不能通过 SDK 的 `model` 字段单独推断。

   为检验 SDK 协议而短暂把 CC Switch 映射到 `gpt-5.6-sol`，运行真实受限会话后立即恢复 `gpt-6-sol`。`.poc/claude-sdk-smoke-cc-switch-gpt56-mapped.json` 记录首个会话、显式 resume 与 fresh 会话均成功。进一步在 `.poc/claude-sdk-smoke-cc-switch-tools.json` 中，自定义 MCP 工具在隔离配置与正向对照中各调用一次，返回不可预测 marker；`setting_sources=[]` 时项目 Hook 未执行，显式 `setting_sources=["project"]` 时同一 Hook 执行。加入版本头后，`.poc/claude-sdk-smoke-cc-switch-gpt6-version.json` 对目标 `gpt-6-sol` 复验全部通过：resume 的 session ID 与首个相同，fresh 的不同，三个结构化 marker 分别正确；两次 MCP 工具调用和 Hook 隔离/正向对照均符合预期。CC Switch 请求日志中这些请求的实际上游模型和计费模型均为 `gpt-6-sol`，状态为 `200`。这些结果验证 SDK/网关会话与工具机制，**不代表**原生 Claude 推理质量。`.poc/claude-sdk-smoke-gpt6-role-isolation.json` 让调查者仅在自己的会话接收随机标记；新建的验证者会话具有不同 ID，输出 `unknown`，没有继承标记。`.poc/claude-sdk-smoke-gpt6-interrupt.json` 在 MCP 工具等待期间调用 `ClaudeSDKClient.interrupt()`；调用返回，最终 `ResultMessage` 为 `error_during_execution` 且 `is_error=true`。CC Switch 近期请求日志中的实际上游模型仍为 `gpt-6-sol`。当前包的 `ClaudeAgentExecutor.run()` 是同步终态接口，尚未向调用方开放中断、事件流或用量，能力声明已改为 false。F-08 的真实排除反证仍待 Joern 语义证明后联测。

上述事项属于 SPEC 001/005 插件的语义验收。基础包 S2 应先用规则无关的最小测试规则验证角色协同、状态恢复、Evidence、Context、排除和可替换端口；详见[SPEC 000](../specs/000-runtime-kernel.md)。
