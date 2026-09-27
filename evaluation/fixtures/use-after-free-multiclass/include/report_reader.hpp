#pragma once

#include "borrowed_int.hpp"

class ReportReader {
public:
    int read_report(const BorrowedInt& input) const {
        return input.read();
    }
};

