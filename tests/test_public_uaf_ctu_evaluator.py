"""Regression tests for the project CTU scorer's failure semantics."""

from __future__ import annotations

import importlib.util
import json
import sys
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location(
    "evaluate_public_uaf_ctu", ROOT / "tools/evaluate_public_uaf_ctu.py"
)
assert SPEC is not None and SPEC.loader is not None
SCORER = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = SCORER
SPEC.loader.exec_module(SCORER)


class PublicUafCtuScorerTests(unittest.TestCase):
    def _project(
        self, root: Path, project_id: str, *, complete: bool, diagnostic: bool = False
    ) -> dict:
        root.mkdir(parents=True)
        source = root / "unit.cpp"
        source.write_text("int unit() { return 0; }\n", encoding="utf-8")
        database = root / "compile_commands.json"
        database.write_text(
            json.dumps(
                [{"directory": str(root), "file": str(source), "arguments": ["clang++"]}]
            ),
            encoding="utf-8",
        )
        diagnostics = []
        if diagnostic:
            diagnostics.append(
                {
                    "candidate_id": "unmatched",
                    "path": "elsewhere.cpp",
                    "line": 9,
                    "column": 1,
                    "rule_id": "cplusplus.NewDelete",
                    "message": "Use of memory after it is freed",
                    "translation_unit": "unit.cpp",
                    "path_events": 3,
                    "report": "unit.plist",
                    "report_sha256": "0" * 64,
                }
            )
        return {
            "project_id": project_id,
            "root": str(root),
            "compile_database": {"path": str(database), "translation_units": 1},
            "successful_translation_units": 1 if complete else 0,
            "failed_translation_units": 0 if complete else 1,
            "duration_seconds": 12.5,
            "complete": complete,
            "uaf_diagnostics": diagnostics,
        }

    def test_execution_failure_is_inconclusive_and_not_evaluable(self):
        manifest = json.loads(
            (ROOT / "evaluation/manifests/public-cpp-uaf-v1.json").read_text("utf-8")
        )
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            run = SCORER.build_run(
                manifest,
                [
                    self._project(root / "qlever", "ad-freiburg/qlever", complete=False),
                    self._project(
                        root / "ghidra",
                        "NationalSecurityAgency/ghidra",
                        complete=True,
                        diagnostic=True,
                    ),
                ],
            )

        qlever = run["sample_results"][0]
        defect = qlever["known_defects"][0]
        self.assertEqual(qlever["analysis_status"], "execution_failure")
        self.assertEqual(qlever["coverage"]["analyzed_targets"], 0)
        self.assertFalse(defect["evaluable"])
        self.assertEqual(defect["candidate_status"], "not_run")
        self.assertEqual(defect["verdict"], "inconclusive")
        self.assertEqual(qlever["resource"]["duration_seconds"], 12.5)
        self.assertTrue(all(
            item["coverage_scope_id"] == "project:ad-freiburg/qlever"
            for item in run["sample_results"][:4]
        ))

        ghidra = run["sample_results"][-1]
        self.assertTrue(ghidra["known_defects"][0]["evaluable"])
        self.assertEqual(ghidra["known_defects"][0]["verdict"], "missed")
        self.assertEqual(ghidra["candidates"][0]["status"], "inconclusive")
        self.assertEqual(ghidra["alerts"], [])

    def test_container_compile_database_paths_are_relocated_to_project_root(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "src" / "unit.cpp"
            source.parent.mkdir()
            source.write_text("int unit() { return 0; }\n", encoding="utf-8")
            absolute = SCORER._compile_source_path({
                "directory": "/workspace/source",
                "file": "/workspace/source/src/unit.cpp",
            }, root)
            relative = SCORER._compile_source_path({
                "directory": "/workspace/source",
                "file": "src/unit.cpp",
            }, root)
        self.assertEqual(absolute, source)
        self.assertEqual(relative, source)

    def test_one_complete_target_is_a_real_miss_despite_other_project_failures(self):
        manifest = json.loads(
            (ROOT / "evaluation/manifests/public-cpp-uaf-v1.json").read_text("utf-8")
        )
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            qlever = self._project(
                root / "qlever", "ad-freiburg/qlever", complete=False
            )
            first_target = manifest["samples"][0]["known_defects"][0]["location"]["path"]
            qlever["target_analysis"] = {
                first_target: {"status": "complete"},
            }
            run = SCORER.build_run(
                manifest,
                [
                    qlever,
                    self._project(
                        root / "ghidra",
                        "NationalSecurityAgency/ghidra",
                        complete=True,
                    ),
                ],
            )

        first = run["sample_results"][0]
        self.assertEqual(first["analysis_status"], "complete")
        self.assertTrue(first["known_defects"][0]["evaluable"])
        self.assertEqual(first["known_defects"][0]["candidate_status"], "missed")
        self.assertEqual(first["known_defects"][0]["verdict"], "missed")
        self.assertEqual(run["sample_results"][1]["analysis_status"], "execution_failure")
        self.assertFalse(run["sample_results"][1]["known_defects"][0]["evaluable"])


if __name__ == "__main__":
    unittest.main()
