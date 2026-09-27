# Lifetime candidate ASan reproductions

These intentionally vulnerable C++ programs are mechanism fixtures, not an
accuracy benchmark.  Each fixture corresponds to one syntax-only candidate
rule and prints `TARGET_PATH_EXERCISED` immediately before the invalid access.

Run all three through the repository driver:

```bash
PYTHONPATH=src python tools/run_lifetime_reproductions.py \
  --output .poc/lifetime-reproductions.json
```

A candidate is confirmed only when the ASan report names the matching fixture
source.  A build failure, timeout, unexercised path, unrelated sanitizer report,
or non-ASan failure stays inconclusive.

## Additional syntax fixtures

`returned-resource.cpp`, `stack-context.cpp`, and `vector-field-copy.cpp` are
independent **syntax fragments**, not stand-alone executable ASan programs.
They exercise returned resource ownership, escaping lazy stack contexts, and
field access through pointers invalidated by direct/indirect vector growth.
`tests/test_lifetime_candidates.py` also checks ownership retention, owning
captures, synchronous consumers, pointer refresh, scope boundaries and renamed
allocator/field identifiers. Only the original three mechanisms are executed by
`run_lifetime_reproductions.py`; none of their results are public-project evidence.
