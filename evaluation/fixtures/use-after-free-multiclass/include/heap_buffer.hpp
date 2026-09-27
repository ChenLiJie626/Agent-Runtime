#pragma once

class HeapBuffer {
public:
    explicit HeapBuffer(int value) : data_(new int(value)) {}

    HeapBuffer(const HeapBuffer&) = delete;
    HeapBuffer& operator=(const HeapBuffer&) = delete;

    ~HeapBuffer() {
        release();
    }

    int* data() const {
        return data_;
    }

    void release() {
        delete data_;
        data_ = nullptr;
    }

private:
    int* data_;
};

