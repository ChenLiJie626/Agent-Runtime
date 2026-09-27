#pragma once

class BorrowedInt {
public:
    explicit BorrowedInt(int* value) : value_(value) {}

    int read() const {
        // Intentional defect: the owner may already have released value_.
        return *value_;
    }

private:
    int* value_;
};

