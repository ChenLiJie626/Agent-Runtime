auto schedule() {
  ExecutionContext context;
  auto task = [&context] { context.consume(); };
  return task;
}
