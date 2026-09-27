# SPEC 010：可复现构建与执行证据

状态：包级记录与回放核心已实现，隔离执行证据待闭合 · 2026-09-27。承接 SPEC 008 Q-01/Q-02/Q-04/Q-05 与 SPEC 009 后续工作；基础包不内置具体语言的缺陷规则。

## 目标

让调用方把隔离执行器产生的构建/运行记录作为 `ProgramQuery` 材料接入 Runtime。记录须绑定源码、工具链、依赖、配方、测试输入和原文摘要。`complete` 只指一次限定执行的记录完整，不能据此推断未执行路径安全。明确执行失败、构建失败、超时与实际观察到的程序失败之间的区别。

首个示例采用公开 MIT C++ 项目 `yhirose/cpp-peglib`：固定上游真实父提交 `14305f9f53cde207568f21675a1b9294a3ab28b4` 与修复提交 `0061f393de54cf0326621c079dc2988336d1ebb3`。这是开发集中的历史复现，不进入原 `public-cpp-smoke-v1` 保留集，不宣称盲评成绩。两侧使用同一受控 API 探针、编译器及配置；只验证指定输入的 AST 路径。来源：[issue #121](https://github.com/yhirose/cpp-peglib/issues/121)、[修复](https://github.com/yhirose/cpp-peglib/commit/0061f393de54cf0326621c079dc2988336d1ebb3)。

## 验收契约

| ID | 要求 | 验证 |
|---|---|---|
| VE-01 | 通用已记录验证后端，只允许注册 ID；记录摘要、Snapshot 源码/策略/范围全部匹配；模型不能传命令 | 跨快照/策略/ID、改写记录、添加 argv 拒绝 |
| VE-02 | 执行器净化环境，禁网、拒绝宿主私有文件、限制可写目录及 CPU/墙钟/输出，隔离不可用即失败 | 无密钥哨兵文件与网络拒绝探针；超时、输出和退出状态回归 |
| VE-03 | 源码与编译器输入前后摘要一致；编译器/SDK 版本、规范化配方、实际头文件依赖摘要和二进制摘要保存 | 公开源码修复前后同配置编译与完整原文记录 |
| VE-04 | 程序异常退出只有与具体源码、输入、观察及阶段匹配时才支持具体命题；成功测试不能证明全库安全 | 正例、近邻正常输入、修复版本、不完整/失败记录保持限定范围或 unknown |
| VE-05 | 规则评价器由应用注入；调查/验证独立引用记录；领域证据与检查门禁仍生效 | Runtime 真实材料裁决；过度自信、未知、缺证据被拒绝 |
| VE-06 | 相同反例跨进程重开后复用精确排除；不同输入/版本/正例不被抑制；原文损坏拒绝复用 | 公开材料排除安全回归与抑制计数 |

## 当前实施状态（0.2.1）

已实现的包级契约：

- 封闭且不可变的 validation record，记录 ID 覆盖源码、工具链、依赖、配方、输入、产物、资源和结果；未知字段及 `command/argv/env/cwd` 被拒绝。
- `RecordedValidationProgramQuery` 只接受应用预注册的 `record_id`，逐次复核 Snapshot/策略/范围/摘要，并复用 Runtime 内容寻址 Evidence。
- `defect-validation-record validate|inspect` 只读校验记录与可选 artifact store，不构建或执行目标。
- Runtime/SQLite 的精确候选 identity、Evidence 完整性和 exclusion 重开行为由集成测试覆盖；不同输入或记录 ID 不共享排除。

尚未闭合：VE-02 的隔离执行器，VE-03 的真实 cpp-peglib base/fix 同配置记录，以及 VE-04～VE-06 在该真实材料上的完整验收。因此 SPEC 010 整体仍处于实施中；fixture-backed 测试不代表真实缺陷确认或项目安全。CodeQL 固定 replay、portfolio 和 adaptive planner 是 0.2.1 的通用扩展能力，不替代上述动态验证门禁。

本轮包级门禁：`pytest` 179 passed、3 skipped；`unittest` 兼容发现 170 tests、3 skipped；`compileall`、离线 wheel/sdist 构建和 checkout 外干净安装验收通过。发行验收同时确认 validation CLI 无执行面、包内 `.ql/.sc` 资源、新旧公开导出和核心导入不加载 Claude 可选依赖。宿主无 CodeQL CLI，因此真实查询编译和数据库 replay 未运行，保留为显式缺口。

## 实施边界

核心新增只读 `RecordedValidationProgramQuery`，不包含执行上游代码的能力。调用方可用容器或远端执行器产出同契约记录；当前仓库未交付通用隔离执行器，也未提供任意构建命令入口。记录中的资源限制和观测值只是对既有执行的绑定，不能自行证明 VM/container 级隔离。未执行完整项目构建、所有测试或所有配置时必须列遗漏。

编译与执行记录分别保存。真实崩溃是指定路径的正证据；测试未触发只能作为该输入下的观察。首次复现的实际结果决定后续可验证命题，不用预设 golden 填充 Runtime Facts。人工仲裁仍待独立审查，不能由脚本或 Agent 模拟。
