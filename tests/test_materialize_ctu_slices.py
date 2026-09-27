"""Container-path boundary tests for per-target CTU slices."""

import importlib.util
import json
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location(
    "materialize_ctu_slices",
    ROOT / "tools/materialize_ctu_slices.py",
)
SLICES = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(SLICES)


class MaterializeCtuSlicesTests(unittest.TestCase):
    def test_materializes_independent_container_databases(self):
        plan = {
            "selections": [
                {
                    "target_path": "include/target.h",
                    "translation_units": ["src/a.cpp", "src/b.cpp"],
                }
            ]
        }
        database = [
            {
                "directory": "/workspace/source/build",
                "file": f"/workspace/source/src/{name}.cpp",
                "arguments": ["clang++", "-c", f"/workspace/source/src/{name}.cpp"],
            }
            for name in ("a", "b", "unrelated")
        ]
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory)
            manifest = SLICES.materialize(plan, database, output)
            item = manifest["slices"][0]
            rows = json.loads(
                (output / item["slice_dir"] / item["compile_database"]).read_text()
            )
            self.assertEqual(len(rows), 2)
            self.assertNotIn("unrelated.cpp", json.dumps(rows))
            self.assertNotIn("/Users/", json.dumps(rows))

    def test_rejects_host_compile_database(self):
        plan = {
            "selections": [
                {
                    "target_path": "src/a.cpp",
                    "translation_units": ["src/a.cpp"],
                }
            ]
        }
        database = [
            {
                "directory": "/Users/example/project",
                "file": "/Users/example/project/src/a.cpp",
                "command": "clang++ -c /Users/example/project/src/a.cpp",
            }
        ]
        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaisesRegex(ValueError, "outside container root"):
                SLICES.materialize(plan, database, Path(directory))

    def test_rejects_host_working_directory_even_with_container_file(self):
        row = {
            "directory": "/home/example/source",
            "file": "/workspace/source/src/a.cpp",
        }
        with self.assertRaisesRegex(ValueError, "directory is outside"):
            SLICES._relative_source(row, "/workspace/source")

    def test_rejects_invalid_container_roots_and_missing_target_tu(self):
        row = {"directory": "/workspace/source", "file": "/workspace/source/src/a.cpp"}
        for root in ("relative", "/", "/workspace/../source"):
            with self.assertRaises(ValueError):
                SLICES._relative_source(row, root)
        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaisesRegex(ValueError, "lacks TUs"):
                SLICES.materialize(
                    {
                        "selections": [
                            {
                                "target_path": "src/missing.cpp",
                                "translation_units": ["src/missing.cpp"],
                            }
                        ]
                    },
                    [row],
                    Path(directory),
                )


if __name__ == "__main__":
    unittest.main()
