#include <cstdlib>

int read_after_free() {
    int* value = static_cast<int*>(std::malloc(sizeof(int)));
    if (value == nullptr) {
        return -1;
    }

    *value = 42;
    std::free(value);

    // Intentional defect for the Runtime UAF example.
    return *value;
}

int main() {
    return read_after_free();
}

