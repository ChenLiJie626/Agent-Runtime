// Adapted from QLever PR #2812's IncompleteIteration regression (Apache-2.0).
// Includes the unchanged baseline implementation; no fixed header or shim.
#include <cstdio>
#include "util/CompressorStream.h"

cppcoro::generator<std::string> generateNChars(size_t n) {
  for (size_t i = 0; i < n; ++i) {
    co_yield "A";
  }
}
int main(int argc, char**) {
  using ad_utility::content_encoding::CompressionMethod;
  auto method = argc > 1 ? CompressionMethod::GZIP : CompressionMethod::DEFLATE;
  std::fprintf(stderr, "TARGET_PATH_EXERCISED: IncompleteIteration\n");
  auto generator = ad_utility::streams::compressStream(generateNChars(100'000'000), method);
  auto iterator = generator.begin();
  if (iterator == generator.end()) return 2;
  [[maybe_unused]] auto batch = *iterator;
  ++iterator;
  if (iterator == generator.end()) return 3;
}
