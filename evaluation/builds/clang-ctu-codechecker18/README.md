# Clang 18 CTU project-analysis image

This image extends the pinned CodeChecker 6.29.1 baseline to Ubuntu 24.04 and
Clang 18. It contains the system dependencies required to configure the frozen
QLever base revision and to capture its project-level compilation database.

Build from the repository root:

```sh
docker build \
  -f evaluation/builds/clang-ctu-codechecker18/Dockerfile \
  -t agent-runtime/clang-ctu:clang18-codechecker6291 .
```

Dependency downloads performed by QLever's CMake `FetchContent` are allowed
only while preparing the build tree. The subsequent CodeChecker analysis is
run with `--network=none` against the frozen source and generated build tree.
