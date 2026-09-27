struct Item { int value; };

int read_before_delete() {
  Item* item = new Item{7};
  int value = item->value;
  delete item;
  return value;
}
