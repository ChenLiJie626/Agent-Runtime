#include <cstdio>
#include <vector>

int main() {
  std::vector<int> values;
  values.reserve(1);
  values.push_back(7);
  int* borrowed = values.data();
  values.push_back(11);
  std::fprintf(stderr, "TARGET_PATH_EXERCISED\n");
  return *borrowed;
}
