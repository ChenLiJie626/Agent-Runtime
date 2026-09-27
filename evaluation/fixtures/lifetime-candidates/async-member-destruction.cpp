#include <chrono>
#include <cstdio>
#include <future>
#include <memory>
#include <thread>

class Owner {
  std::future<void> worker_;
  std::unique_ptr<int> state_ = std::make_unique<int>(23);

 public:
  void start() {
    int* cached = state_.get();
    worker_ = std::async(std::launch::async, [this, cached] {
      std::this_thread::sleep_for(std::chrono::milliseconds(200));
      std::fprintf(stderr, "TARGET_PATH_EXERCISED\n");
      std::fprintf(stderr, "%d\n", *cached + (state_ ? 0 : 0));
    });
  }
};

int main() {
  Owner owner;
  owner.start();
  return 0;
}
