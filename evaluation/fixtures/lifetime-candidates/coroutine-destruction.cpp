#include <coroutine>
#include <cstdio>
#include <exception>

struct Target {
  int* value = new int(17);
  ~Target() { delete value; }
};

struct RetainingOwner {
  Target* target = nullptr;
  void attach(Target& value) { target = &value; }
  ~RetainingOwner() {
    std::fprintf(stderr, "TARGET_PATH_EXERCISED\n");
    std::fprintf(stderr, "%d\n", *target->value);
  }
};

struct Task {
  struct promise_type {
    Task get_return_object() {
      return Task{std::coroutine_handle<promise_type>::from_promise(*this)};
    }
    std::suspend_never initial_suspend() noexcept { return {}; }
    std::suspend_always final_suspend() noexcept { return {}; }
    void return_void() noexcept {}
    void unhandled_exception() { std::terminate(); }
  };

  std::coroutine_handle<promise_type> handle;
  ~Task() { handle.destroy(); }
};

Task reproduce() {
  RetainingOwner owner;
  Target target;
  owner.attach(target);
  co_await std::suspend_always{};
}

int main() {
  auto task = reproduce();
  return 0;
}
