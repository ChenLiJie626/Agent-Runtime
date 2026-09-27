#include "borrowed_int.hpp"
#include "heap_buffer.hpp"
#include "report_reader.hpp"
#include "workflow.hpp"

int run_expired_view_workflow() {
    HeapBuffer owner(42);
    BorrowedInt cached_view(owner.data());
    ReportReader reader;

    owner.release();
    return reader.read_report(cached_view);
}

