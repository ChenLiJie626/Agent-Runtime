"""Tests for provenance-preserving targeted CTU metadata merging."""

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
    "merge_targeted_ctu", ROOT / "tools/merge_targeted_ctu.py"
)
MERGER = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MERGER)
sys.path.pop(0)


class TargetedCtuMergeTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)

    def tearDown(self):
        self.temp.cleanup()

    def write(self, name, project, executions, target_analysis=None):
        path = self.root / name
        path.write_text(
            json.dumps(
                {
                    "schema": MERGER.SCHEMA,
                    "project_id": project,
                    "executions": executions,
                    "target_analysis": target_analysis or {},
                }
            )
        )
        return path

    def test_preserves_distinct_executions_and_input_hashes(self):
        first_execution = {
            "target_path": "src/a.cpp",
            "artifact_dir": "/runs/a",
            "compile_database_sha256": "a" * 64,
            "status": "complete",
        }
        second_execution = {
            "target_path": "src/b.cpp",
            "artifact_dir": "/runs/b",
            "compile_database_sha256": "b" * 64,
            "status": "execution_failure",
        }
        first = self.write(
            "first.json",
            "example/project",
            [first_execution],
            {"src/a.cpp": {"status": "complete"}},
        )
        second = self.write(
            "second.json",
            "example/project",
            [second_execution],
            {"src/b.cpp": {"status": "execution_failure"}},
        )
        merged = MERGER.merge_metadata([first, second])
        self.assertEqual(merged["executions"], [first_execution, second_execution])
        self.assertEqual(len(merged["merged_inputs"]), 2)
        self.assertTrue(
            all(len(item["sha256"]) == 64 for item in merged["merged_inputs"])
        )
        self.assertEqual(
            merged["target_analysis"]["src/b.cpp"]["status"], "execution_failure"
        )

    def test_deduplicates_only_same_execution_identity(self):
        execution = {
            "target_path": "src/a.cpp",
            "artifact_dir": "/runs/a",
            "compile_database_sha256": "a" * 64,
            "status": "timeout",
        }
        first = self.write("first.json", "example/project", [execution])
        second = self.write("second.json", "example/project", [execution])
        merged = MERGER.merge_metadata([first, second])
        self.assertEqual(merged["executions"], [execution])

    def test_rejects_cross_project_merge(self):
        first = self.write("first.json", "example/one", [])
        second = self.write("second.json", "example/two", [])
        with self.assertRaises(InvalidInput):
            MERGER.merge_metadata([first, second])

    def test_rejects_invalid_schema(self):
        path = self.root / "invalid.json"
        path.write_text(json.dumps({"schema": "other", "executions": []}))
        with self.assertRaises(InvalidInput):
            MERGER.merge_metadata([path])


if __name__ == "__main__":
    unittest.main()
