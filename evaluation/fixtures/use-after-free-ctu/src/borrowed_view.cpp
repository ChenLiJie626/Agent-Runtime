#include "borrowed_view.hpp"

BorrowedView::BorrowedView(int* value) : value_(value) {}

int BorrowedView::read() const {
    // Intentional cross-translation-unit use after free.
    return *value_;
}

