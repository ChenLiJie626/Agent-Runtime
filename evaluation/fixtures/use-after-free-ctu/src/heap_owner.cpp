#include "heap_owner.hpp"

HeapOwner::HeapOwner(int value) : value_(new int(value)) {}

HeapOwner::~HeapOwner() {
    release();
}

int* HeapOwner::borrow() const {
    return value_;
}

void HeapOwner::release() {
    delete value_;
    value_ = nullptr;
}

