"""Tests for ASan evidence bundle merging."""

import importlib.util
import json
import sys
import tempfile
import unittest
from pathlib import Path

from agent_runtime.errors import InvalidInput

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))
SPEC = importlib.util.spec_from_file_location(
    "merge_asan_evidence", ROOT / "tools/merge_asan_evidence.py"
)
MERGER = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MERGER)
sys.path.pop(0)


class AsanEvidenceMergeTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)

    def tearDown(self):
        self.temp.cleanup()

    def write(self, name, evidence):
        path = self.root / name
        path.write_text(json.dumps({"schema": MERGER.SCHEMA, "evidence": evidence}))
        return path

    def test_merges_independent_items_and_records_inputs(self):
        first_item = {"defect_id": "one", "status": "confirmed"}
        second_item = {"defect_id": "two", "status": "inconclusive"}
        result = MERGER.merge_evidence(
            [
                self.write("one.json", [first_item]),
                self.write("two.json", [second_item]),
            ]
        )
        self.assertEqual(result["evidence"], [first_item, second_item])
        self.assertEqual(len(result["merged_inputs"]), 2)
        self.assertTrue(
            all(len(item["sha256"]) == 64 for item in result["merged_inputs"])
        )

    def test_exact_duplicate_is_deduplicated(self):
        item = {"defect_id": "one", "status": "refuted"}
        result = MERGER.merge_evidence(
            [self.write("one.json", [item]), self.write("copy.json", [item])]
        )
        self.assertEqual(result["evidence"], [item])

    def test_conflicting_duplicate_is_rejected(self):
        first = self.write("one.json", [{"defect_id": "one", "status": "refuted"}])
        second = self.write("two.json", [{"defect_id": "one", "status": "confirmed"}])
        with self.assertRaises(InvalidInput):
            MERGER.merge_evidence([first, second])

    def test_rejects_unknown_status(self):
        path = self.write("bad.json", [{"defect_id": "one", "status": "success"}])
        with self.assertRaises(InvalidInput):
            MERGER.merge_evidence([path])


if __name__ == "__main__":
    unittest.main()
