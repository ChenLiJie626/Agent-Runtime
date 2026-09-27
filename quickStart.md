# Agent Runtime 快速开始

> `defect-agent-runtime` 是一个“证据优先”的代码缺陷分析基础包：它负责固定分析范围、组织候选、保存工具原文、协调独立调查/验证角色，并在证据充分时给出限定范围的裁决。
>
> 当前版本：**0.3.0**。项目尚未发布到 PyPI，建议固定 Git commit 或使用本地构建的 wheel。

## 1. 它解决什么问题？

一般的代码扫描脚本很容易遇到这些问题：

- 扫描时用的源码、编译参数和最后展示的结果不是同一份；
- 工具超时、崩溃或没有跑到目标路径，却被误写成“没有缺陷”；
- Agent 给出了结论，但无法知道它看过哪些原始证据；
- 同一个问题反复分析，或者不同版本的结果被错误复用；
- 静态分析、动态复现、CodeQL、Joern 等工具各自产生孤立结果。

本包把一次缺陷分析组织成下面的流程：

```text
固定源码与策略
      ↓
发现候选 Candidate
      ↓
按受限查询读取源码、诊断或验证记录
      ↓
原始结果保存为不可变 Evidence
      ↓
Investigator 与 Verifier 独立检查
      ↓
规则评价器给出 confirmed / refuted / inconclusive
      ↓
SQLite 保存报告、会话与精确 Exclusion
```

这里最重要的原则是：**没有足够证据，就保持 `inconclusive`。** `partial`、timeout、OOM、执行失败、空结果或 no-trigger 都不会自动变成“代码安全”。

## 2. 目前可以做什么？

### 2.1 组合一条完整的缺陷分析流水线

`AnalysisPipeline` 可以把这些组件串起来：

- `CandidateDiscoverer`：发现值得检查的代码位置；
- `ProgramQuery`：以结构化、只读方式获取源码、诊断或语义结果；
- `AgentExecutor`：运行调查者、验证者或专项角色；
- `RuleEvaluator`：依据已保存的证据决定最终状态；
- `SQLiteStore`：持久化候选、证据、角色结果、报告和排除记录。

每个组件都可替换。其他项目可以复用 Runtime，只实现自己的规则、发现器和查询后端。

### 2.2 分析 C/C++ use-after-free

包内已有一条可运行的 C++ UAF 示例链路：

- 使用 Clang Static Analyzer 获取诊断；
- 支持 `compile_commands.json`；
- 支持 CodeChecker/Clang CTU 处理跨翻译单元问题；
- 将源码窗口和诊断原文保存为 Evidence；
- 由独立角色检查后，再由 `CppUseAfterFreeEvaluator` 裁决。

此外还可以扫描六类生命周期风险候选：

1. 容器扩容后的引用/指针失效；
2. 协程局部对象逆序析构；
3. 异步任务与成员逆序析构；
4. 返回资源的所有权问题；
5. 栈上下文逃逸；
6. vector 扩容后的字段复制。

这些规则产生的是**候选**，最终确认仍需 ASan、语义证据或其他明确原文。

### 2.3 保存和回放真实动态验证记录

`ValidationRecord` 1.1 可以绑定：

- 源码、工具策略、工具链和依赖摘要；
- 构建配方、测试输入和二进制摘要；
- 隔离探针结果；
- exit、signal、timeout、OOM、output-limit 等 termination；
- wall time、CPU、内存、进程数、输出量和可写空间观察；
- stdout、stderr 和其他内容寻址 artifact。

`RecordedValidationProgramQuery` 只允许回放应用预先注册的 record ID，不接受模型提供任意命令。每次回放都会重新验证 artifact。

### 2.4 运行受限的 Docker 验证任务

`DockerValidationExecutor` 是 registry-only 执行器。应用需要预先登记 suite 和 attempt；调用者不能临时传入 shell、argv、环境变量、镜像或挂载。

默认隔离策略包括：

- `--network=none`；
- 只读根文件系统；
- 非 root 用户；
- 删除全部 capabilities；
- `no-new-privileges`；
- CPU、内存、PID、输出和墙钟限制；
- 只读源码挂载和有界 tmpfs。

它适合运行项目已经审核过的固定复现配方，**不是任意代码执行 API，也不是通用云沙箱**。

### 2.5 回放固定 CodeQL、Joern 和 Clang 证据

当前适配器包括：

- `CodeQLReplayProgramQuery`：只运行包内固定 query，对预建数据库做 fresh scratch replay；
- `RecordedJoernProgramQuery`：回放已经记录的 Joern 查询材料；
- `JoernProgramQuery`：提供固定、受限的语义查询；
- `ClangDiagnosticProgramQuery`：读取绑定到固定快照的 Clang 诊断；
- `FrozenSourceProgramQuery`：读取内存中冻结源码的有限窗口；
- `CompositeProgramQuery`：把操作互不冲突的多个只读后端组合起来。

CodeQL 和静态分析结果默认只是 candidate signal。空 SARIF 或零结果不自动表示安全。

### 2.6 使用 Agent，但不把裁决权完全交给 Agent

安装可选依赖后，可以用 `ClaudeAgentExecutor` 运行独立角色：

```bash
python -m pip install 'defect-agent-runtime[claude] @ git+https://github.com/ChenLiJie626/Agent-Runtime.git@3176c434fcc1ee0cca67664dd83a4cc99bcf84fd'
```

Agent 只能使用 Runtime 提供的受限工具查询程序和读取 Evidence。最终结论仍需通过规则评价器、证据引用、覆盖范围和角色独立性门禁。

## 3. 三分钟体验

### 3.1 获取源码并安装

```bash
git clone https://github.com/ChenLiJie626/Agent-Runtime.git
cd Agent-Runtime

python3 -m venv .venv
source .venv/bin/activate
python -m pip install -e .
```

确认导入和版本：

```bash
python - <<'PY'
from importlib.metadata import version
import agent_runtime

print(version("defect-agent-runtime"))
print(agent_runtime.__file__)
PY
```

预期版本为 `0.3.0`。

### 3.2 运行最小流水线

```bash
python examples/cpp_unreachable_pipeline.py
```

这个例子比较两份内存中的 C++ 源码，发现 `return` 后的语句，保存源码 Evidence，并运行确定性的调查/验证角色。输出类似：

```json
{
  "analysis_id": "...",
  "candidates": [
    {
      "status": "inconclusive",
      "blockers": [
        "semantic_validation lacks complete scoped coverage"
      ]
    }
  ]
}
```

`inconclusive` 是正确结果：示例只有源码文字证据，没有完整控制流语义，因此 Runtime 不会假装已经确认缺陷。

### 3.3 运行真实 C++ UAF 示例

本机需要 `clang++`：

```bash
python examples/use_after_free_pipeline.py
```

仓库自带的故意缺陷夹具应产生一个 `confirmed` 候选。原始 Evidence、SQLite 和最终 JSON 默认写入：

```text
.poc/use-after-free-runtime/
├── artifacts/
├── runtime.sqlite3
└── result.json
```

## 4. 在其他项目中使用

### 4.1 推荐：固定 Git commit

在目标项目的 `requirements.txt` 中加入：

```text
defect-agent-runtime @ git+https://github.com/ChenLiJie626/Agent-Runtime.git@3176c434fcc1ee0cca67664dd83a4cc99bcf84fd
```

然后安装：

```bash
python -m pip install -r requirements.txt
```

固定完整 commit 比直接依赖 `main` 更可复现。升级时显式修改 commit，并重新跑目标项目的验收。

也可以使用本仓库构建的 wheel：

```bash
uv build --offline --out-dir dist/runtime
python -m pip install \
  dist/runtime/defect_agent_runtime-0.3.0-py3-none-any.whl
```

### 4.2 对自己的 C++ 项目运行 UAF 流水线

在本仓库 checkout 中执行示例入口：

```bash
python examples/use_after_free_pipeline.py \
  --project /absolute/path/to/your-cpp-project \
  --output /absolute/path/to/your-cpp-project/.analysis/uaf
```

真实项目通常应提供编译数据库：

```bash
cmake -S /absolute/path/to/your-cpp-project \
      -B /absolute/path/to/your-cpp-project/build \
      -DCMAKE_EXPORT_COMPILE_COMMANDS=ON

python examples/use_after_free_pipeline.py \
  --project /absolute/path/to/your-cpp-project \
  --compile-commands /absolute/path/to/your-cpp-project/build/compile_commands.json \
  --output /absolute/path/to/your-cpp-project/.analysis/uaf
```

读取结果：

```bash
python -m json.tool /absolute/path/to/your-cpp-project/.analysis/uaf/result.json
```

关注字段：

- `diagnostic_count`：Clang 捕获的原始诊断数；
- `candidates[].status`：`confirmed`、`refuted` 或 `inconclusive`；
- `candidates[].supporting_evidence_ids`：支持结论的 Evidence；
- `candidates[].blockers`：为什么还不能给出确定结论；
- `report.candidate_reports[].checks[].coverage`：每项检查的实际覆盖范围，而不是计划覆盖范围。

如果缺陷跨多个 `.cpp`，可以使用 CTU 模式：

```bash
docker build -t agent-runtime/clang-ctu:clang14-codechecker6291 \
  evaluation/builds/clang-ctu-codechecker

python examples/use_after_free_pipeline.py \
  --project /absolute/path/to/your-cpp-project \
  --compile-commands /absolute/path/to/your-cpp-project/build/compile_commands.json \
  --ctu \
  --output /absolute/path/to/your-cpp-project/.analysis/uaf-ctu
```

CTU 失败、超时或只覆盖部分翻译单元时，报告会保留 `partial`/失败信息，不会把零候选当成安全。

### 4.3 在应用代码中嵌入 Runtime

下面是一个最小的源码分析示例。它使用包内组件发现 `return` 后的语句：

```python
from pathlib import Path

from agent_runtime import (
    AnalysisPipeline,
    CppUnreachableDiscoverer,
    CppUnreachableEvaluator,
    DefectRuntime,
    EvidenceReviewExecutor,
    FixedSnapshot,
    SQLiteStore,
    analysis_profile_digest,
    cpp_unreachable_profile,
    cpp_unreachable_rule,
    digest,
)
from agent_runtime.adapters import FrozenSourceProgramQuery

base = {"src/demo.cpp": "int f() { return 0; }\n"}
head = {
    "src/demo.cpp": """int f() {
  return 0;
  do_work();
}
"""
}

rule = cpp_unreachable_rule()
policy_digest = digest({"operations": ["read_source"], "max_lines": 200})
profile = cpp_unreachable_profile(policy_digest)
snapshot = FixedSnapshot(
    "my-team/my-project",
    "change-123",
    FrozenSourceProgramQuery.source_digest(head),
    analysis_profile_digest(rule, profile=profile),
    policy_digest,
    {"base_commit": "abc", "head_commit": "def"},
)

state = Path(".analysis/runtime")
state.mkdir(parents=True, exist_ok=True)
store = SQLiteStore(state / "runtime.sqlite3", state / "artifacts")
try:
    result = AnalysisPipeline(
        DefectRuntime(store),
        rule=rule,
        profile=profile,
        discoverer=CppUnreachableDiscoverer(),
        backend=FrozenSourceProgramQuery(snapshot, head),
        evaluator=CppUnreachableEvaluator(),
        executor=EvidenceReviewExecutor(),
        owner_id="local-worker",
        working_dir=str(state.resolve()),
    ).run(snapshot, base_sources=base, head_sources=head)

    for candidate in result.candidates:
        print(candidate.candidate_id, candidate.status)
        if candidate.decision:
            print("blockers:", candidate.decision.blockers)
finally:
    store.close()
```

这个例子的关键不是某条 C++ 规则，而是组装方式：

1. 定义 `RuleSpec` 和 `Profile`；
2. 用 `FixedSnapshot` 固定源码、规则配置和工具策略；
3. Discoverer 产生精确候选；
4. ProgramQuery 只读取允许的材料；
5. Runtime 保存 Evidence；
6. 独立角色审查；
7. Evaluator 在通用门禁下输出裁决；
8. SQLite 保存结果，进程重启后仍可读取。

### 4.4 校验别人交付的 ValidationRecord

CLI 只读取和验证记录，不执行任何命令：

```bash
# 只验证 record 结构并打印 record ID
defect-validation-record validate record.json

# 同时验证 record 引用的全部内容寻址 artifact
defect-validation-record validate record.json --artifact-root ./cas

# 输出规范化后的完整记录
defect-validation-record inspect record.json --artifact-root ./cas
```

artifact root 的布局为：

```text
cas/<sha256前两位>/<完整sha256>
```

schema 1.1 record 若要进入 `RecordedValidationProgramQuery`，必须提供并验证 artifact root。

## 5. 接入自定义规则

一个自定义规则通常实现四个 Protocol：

| 组件 | 负责什么 | 不应该做什么 |
|---|---|---|
| `CandidateDiscoverer` | 从固定输入发现候选，生成稳定 identity 和 EvidencePlan | 不直接下最终结论 |
| `ProgramQuery` | 执行结构化、只读、有限范围的查询 | 不接受模型提供任意 shell/路径/查询语言 |
| `AgentExecutor` | 让调查者与验证者阅读 Evidence 并输出结构化结果 | 不绕过 Runtime 直接改数据库 |
| `RuleEvaluator` | 将 Facts、Checks 和角色结果解释为规则裁决 | 不引用不存在或未保存的 Evidence |

完整的包外实现见 [`examples/external_consumer.py`](examples/external_consumer.py)。它演示了：

- 两条第三方规则；
- 两个自定义 ProgramQuery；
- 自定义 evaluator 和 executor；
- `confirmed` 与 scoped `refuted`；
- 重开 SQLite 后读取报告和复用精确 Exclusion。

## 6. 结果状态怎么理解？

| 状态 | 通俗解释 | 可以得出什么结论？ |
|---|---|---|
| `confirmed` | 规则要求的正证据、覆盖和独立验证均满足 | 只确认该 candidate identity 和 scope 下的命题 |
| `refuted` | 有完整、限定范围的反证 | 只排除完全相同的 revision/input/configuration |
| `inconclusive` | 证据不足、角色仍有疑问或必要检查未完成 | 不能确认，也不能说安全 |
| `partial` | 工具只完成部分覆盖，或没有匹配到已注册结果 | 不能把零结果算作真实漏报或安全 |
| `timeout` | 查询或执行超过固定时间 | 只能说明本次执行超时 |
| `failed` | 隔离、启动、清理、OOM、输出上限或执行器失败 | 不能产生确定负证据 |
| `not_run` | 因预算或计划没有执行 | 必须保留在报告分母或遗漏项中 |

## 7. 常见选择

### 我只想在本地扫描 C++ UAF

从 `examples/use_after_free_pipeline.py` 开始，真实项目优先提供 `compile_commands.json`。

### 我已经有自己的扫描器

把扫描器封装成 `CandidateDiscoverer`，把原始查询封装成只读 `ProgramQuery`；让 Runtime 负责 Evidence、角色、裁决和持久化。

### 我已经有 ASan/测试复现记录

把它转成 `ValidationRecord` 1.1，保存全部引用 artifact，再用 `RecordedValidationProgramQuery` 回放。不要只保存一句“测试通过/失败”。

### 我想接入 Claude

安装 `[claude]` extra，使用 `ClaudeAgentExecutor`。先在目标部署环境验证 SDK/CLI、模型和网关组合；核心 Runtime 不依赖 Claude SDK。

### 我想接入 CodeQL

使用应用预先准备、内容寻址的 database 和包内固定 query。不要让模型提供 QL、database path、CLI flags 或 environment。CodeQL result 默认作为候选信号，再结合规则 Evidence 进行裁决。

## 8. 当前边界

本包目前不是：

- 一键覆盖所有语言和所有缺陷的扫描器；
- 自动证明整个项目安全的工具；
- 通用 Docker/远程代码执行平台；
- 分布式任务调度服务；
- 已发布到 PyPI 的稳定服务；
- 用单次 fixture 结果代表真实检测率的评测系统。

本包目前最适合作为：

- 代码审计 Agent 的领域控制层；
- 多种静态/动态分析工具的证据汇聚层；
- 需要可重放、可审计、fail-closed 语义的缺陷分析基座；
- 自定义缺陷规则和 Agent 工作流的 Python 扩展框架。

## 9. 下一步阅读

- [README](README.md)：项目能力总览和完整命令；
- [SPEC 000](specs/000-runtime-kernel.md)：Runtime 内核契约；
- [SPEC 007](specs/007-release-and-extension.md)：公开扩展 API；
- [SPEC 010](specs/010-reproducible-validation-evidence.md)：隔离执行与可复现验证证据；
- [包外扩展示例](examples/external_consumer.py)：自定义规则、后端、Agent 和 evaluator；
- [C++ UAF 示例](examples/use_after_free_pipeline.py)：Clang/CTU 到 Runtime 的完整流水线。

遇到结果不确定时，优先查看 `blockers`、`coverage`、`limitations` 和原始 Evidence，而不是只看候选数量。
