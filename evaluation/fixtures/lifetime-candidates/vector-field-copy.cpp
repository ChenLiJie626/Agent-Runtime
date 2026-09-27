struct Arena {
  std::vector<Node> storage;
  Node* acquire() {
    auto index = storage.size();
    storage.resize(index + 1);
    return &storage[index];
  }
};
void copy(Arena* arena) {
  Node* old = arena->acquire();
  Node* next = arena->acquire();
  next->field = old->field;
}
