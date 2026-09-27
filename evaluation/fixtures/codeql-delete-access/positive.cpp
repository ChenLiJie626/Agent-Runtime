struct Item { int value; };

int read_after_delete() {
  Item* item = new Item{7};
  delete item;
  return item->value;
}
