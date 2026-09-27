#include "buffer.hpp"

int* getBuffer(int count) {
    if (count == 0) return nullptr;
    (void)count;
    static int value = 7;
    return &value;
}
