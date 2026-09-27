# Real QLever baseline-header ASan reductions

These are **real-project reductions**, separate from the three self-contained
mechanism fixtures in `../lifetime-candidates`. They include unchanged QLever
headers at `cf5c9d547403c5babac97d35b6cb4868825dbf98` and cannot build without that
source tree and its dependencies. No fixed implementation is transplanted.

- `compressor.cpp`: adapts the `IncompleteIteration` regression from QLever
  PR #2812 (`7f0a75c75a4b21b162b755c4967d013447f6746b`), for DEFLATE and GZIP.
- `external.cpp`: a deterministic reduction of `stillSortingOnDestruction`,
  exercising the actual `CompressedExternalIdTableBase` template with its
  supported `BlockTransformation` parameter. A heap-backed transformation sleeps
  while baseline teardown destroys its state before the future joins. This is
  **not** the complete upstream sorter regression; confirmation is scoped to this
  baseline-header mechanism and linked sanitizer stack, not arbitrary workloads.

QLever is Apache-2.0. Upstream: https://github.com/ad-freiburg/qlever/pull/2812
Preserve the upstream source tree's LICENSE and notices when redistributing it.

```bash
PYTHONPATH=src python tools/run_public_uaf_asan.py \
  --source .poc/project-uaf-v1/qlever-base \
  --output .poc/project-uaf-v1/asan-evidence
```

The driver verifies baseline headers, builds dependencies offline, uses ASan,
frame pointers and debug information, then saves every actual build/run command,
exit code, source snapshot, binary/log hashes and raw stdout/stderr. Existing
attempts are retained. `--dependency-build <successful-attempt-dir>` can reuse a
matching instrumented dependency build.

`confirmed` requires a UAF-class ASan report, target-path execution, and a numbered
frame from `/workspace/source/<candidate path>` in that same report. Clean,
verified target execution refutes only that reproduction; failure, timeout,
zero coverage or unrelated/unsymbolized diagnostics remain inconclusive.
