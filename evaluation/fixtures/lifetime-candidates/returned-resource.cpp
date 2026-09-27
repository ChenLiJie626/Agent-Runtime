// A custom value contains arena-backed IDs, not independently owned payloads.
ValueTable materialize(const Blocks& blocks) {
  ValueTable result;
  for (const auto& block : blocks) {
    const auto& borrowed = block.asView();
    result.push_back(borrowed[0]);
  }
  return result;
}
