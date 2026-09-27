# Intentional use after free fixture

This project is test data. `src/main.cpp` deliberately reads `value` after
`std::free(value)` so the bundled Clang Static Analyzer adapter has a stable,
small use after free diagnostic to feed into the Runtime.

