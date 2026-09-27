# Clang CTU analysis image

This image provides Clang 14, `clang-extdef-mapping` and CodeChecker 6.29.1.
It is used only as an isolated analysis driver. Target sources are mounted
read-only and CodeChecker output is written to a separate mounted directory.
`packages.lock` records the complete apt package set and is checked during the
build. `requirements.lock` constrains every installed Python package observed
in the verified image.

Build with:

```bash
docker build -t agent-runtime/clang-ctu:clang14-codechecker6291 \
  evaluation/builds/clang-ctu-codechecker
```
