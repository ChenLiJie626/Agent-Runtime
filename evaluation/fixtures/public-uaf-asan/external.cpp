// A deterministic reduction of the async-member regression in QLever PR #2812
// (Apache-2.0), exercising the unchanged CompressedExternalIdTableBase template.
#include <cstdio>
#include <chrono>
#include <thread>
#include "engine/idTable/CompressedExternalIdTable.h"

// This callback is the template's supported BlockTransformation parameter.
// Its heap-backed state is destroyed before the baseline future joins the task.
struct SlowTransformation {
  std::vector<size_t> data_ = {1, 2, 3};
  void operator()(IdTableStatic<0>&) {
    std::this_thread::sleep_for(std::chrono::milliseconds(200));
    [[maybe_unused]] volatile auto value = data_[0];
  }
};
struct Table : ad_utility::CompressedExternalIdTableBase<0, SlowTransformation> {
  using Base = ad_utility::CompressedExternalIdTableBase<0, SlowTransformation>;
  using Base::Base;
};
int main() {
  using namespace ad_utility::memory_literals;
  auto allocator = ad_utility::AllocatorWithLimit<Id>{
      ad_utility::makeAllocationMemoryLeftThreadsafeObject(ad_utility::MemorySize::max())};
  std::fprintf(stderr, "TARGET_PATH_EXERCISED: async member destruction\n");
  Table table{"/workspace/output/external.dat", 4, 10_kB, allocator};
  std::array<Id, 4> row{};
  for (size_t i = 0; i < 200; ++i) table.push(row);
}
