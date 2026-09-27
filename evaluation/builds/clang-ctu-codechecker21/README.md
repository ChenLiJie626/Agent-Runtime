# Clang 21 CTU project-analysis image

This image extends the local Clang 18/CodeChecker 6.29.1 baseline with Clang
21. Clang 21 is required
for CTU analysis of native C++20 projects because its AST importer supports
`ConceptDecl` and `RequiresExpr`; Clang 18 and 20 do not.

Build from this directory after building the Clang 18 baseline:

```sh
docker build \
  -t agent-runtime/clang-ctu:clang21-codechecker6291 .
```

Allow the network only while building the image and configuring the frozen
project. Run the subsequent CodeChecker analysis with `--network=none`.
