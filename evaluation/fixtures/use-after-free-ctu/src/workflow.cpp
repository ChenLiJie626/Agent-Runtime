#include "borrowed_view.hpp"
#include "heap_owner.hpp"
#include "workflow.hpp"

int run_cross_translation_unit_workflow() {
    HeapOwner owner(42);
    BorrowedView view(owner.borrow());
    owner.release();
    return view.read();
}

