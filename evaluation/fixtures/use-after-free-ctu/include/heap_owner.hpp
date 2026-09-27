#pragma once

class HeapOwner {
public:
    explicit HeapOwner(int value);
    ~HeapOwner();

    HeapOwner(const HeapOwner&) = delete;
    HeapOwner& operator=(const HeapOwner&) = delete;

    int* borrow() const;
    void release();

private:
    int* value_;
};

