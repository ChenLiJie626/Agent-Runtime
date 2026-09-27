#pragma once

class BorrowedView {
public:
    explicit BorrowedView(int* value);
    int read() const;

private:
    int* value_;
};

