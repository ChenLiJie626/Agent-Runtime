#include "buffer.hpp"

int* getBuffer(int count) {
    (void)count;
    static int value = 7;
    return &value;
}
