# Cross translation unit UAF fixture

Allocation, borrowing, release and dereference are implemented in separate
`.cpp` translation units. Per-TU Clang analysis is expected to miss this
fixture. A CTU-capable backend must recover the complete path.

The checked-in `compile_commands.json` is intentionally relocatable. It keeps
the fixture reproducible across the host and the isolated Linux CTU image.
