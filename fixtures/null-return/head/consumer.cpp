#include "buffer.hpp"

int process(int n) {
    int* value = getBuffer(n);
    return *value;
}

int guardedProcess(int n) {
    if (n == 0) {
        return 0;
    }
    int* value = getBuffer(n);
    return *value;
}

int indirectEntry(int (*callback)(int), int n) {
    return callback(n);
}

int main() {
    return process(0);
}
