# 公开 C++ 复现探针

目标：`yhirose/cpp-peglib` 公开历史 issue #121。`probe.cpp` 为本项目受控测试入口，使用库的 parser/AST API；不包含上游源码，不执行上游 CMake、Hook 或脚本。

`ignored` 和 `ordinary` 只指定测试输入，不是给 Agent 的真假标签。同一探针在两个固定上游提交上编译；观察结果与裁决来源于原始进程记录。上游 `peglib.h` 在临时验证目录保留 MIT LICENSE，来源与许可地址记录在冻结清单中。当前 SDK/toolchain 变体独立于上游原 issue 的 Ubuntu/GCC5/ASan 配置。
