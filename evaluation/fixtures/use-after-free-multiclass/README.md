# Multi-class use after free fixture

This test project deliberately passes a non-owning pointer from `HeapBuffer` to
`BorrowedInt`. `HeapBuffer::release()` deletes the allocation and
`ReportReader` later calls `BorrowedInt::read()`, which dereferences the stale
pointer. The call path crosses three classes and several project files.

