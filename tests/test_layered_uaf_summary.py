"""Tests for the conservative layered UAF scorecard."""

from __future__ import annotations

import importlib.util
import json
import sys
import tempfile
import unittest
from pathlib import Path

from agent_runtime import InvalidInput


ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location(
    "summarize_layered_uaf", ROOT / "tools/summarize_layered_uaf.py"
)
assert SPEC is not None and SPEC.loader is not None
SUMMARY = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = SUMMARY
SPEC.loader.exec_module(SUMMARY)


class LayeredUafSummaryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.project_id = "example/project"
        self.manifest = {
            "dataset_id": "frozen-uaf",
            "samples": [
                {
                    "sample_id": "sample-one",
                    "project_id": self.project_id,
                    "known_defects": [
                        {
                            "defect_id": "defect-one",
                            "location": {"path": "src/one.cpp", "line": 10},
                        }
                    ],
                },
                {
                    "sample_id": "sample-two",
                    "project_id": self.project_id,
                    "known_defects": [
                        {
                            "defect_id": "defect-two",
                            "location": {"path": "src/two.cpp", "line": 20},
                        }
                    ],
                },
            ],
        }
        result = {
            "sample_id": "sample-one",
            "analysis_status": "execution_failure",
            "coverage": {
                "total_translation_units": 2,
                "analyzed_translation_units": 1,
            },
            "candidates": [
                {
                    "candidate_id": "near-hit",
                    "path": "src/two.cpp",
                    "line": 21,
                    "rule_id": "cplusplus.NewDelete",
                }
            ],
        }
        second = json.loads(json.dumps(result))
        second["sample_id"] = "sample-two"
        self.run = {
            "run_id": "ctu-run",
            "dataset_id": "frozen-uaf",
            "manifest_digest": "a" * 64,
            "sample_results": [result, second],
        }
        self.report = {
            "report_id": "ctu-report",
            "run_id": "ctu-run",
            "manifest_digest": "a" * 64,
        }
        self.candidates = self._write(
            "candidates.json",
            {
                "candidates": [
                    {"path": "src/one.cpp", "line": 10, "rule_id": "lifetime"},
                    {"path": "src/two.cpp", "line": 19, "rule_id": "near"},
                ],
            },
        )
        self.non_ctu = self._write(
            "non-ctu.json",
            {
                "tools": [
                    {
                        "action_num": 2,
                        "analyzers": {
                            "clangsa": {
                                "analyzer_statistics": {
                                    "successful": 1,
                                    "failed": 1,
                                    "successful_sources": [
                                        "/workspace/source/src/one.cpp"
                                    ],
                                    "failed_sources": ["/workspace/source/src/two.cpp"],
                                }
                            }
                        },
                    }
                ],
            },
        )

    def tearDown(self):
        self.temp.cleanup()

    def _write(self, name, value):
        path = self.root / name
        path.write_text(json.dumps(value), encoding="utf-8")
        return path

    def _inputs(self, targeted=None):
        return {
            self.project_id: {
                "lifetime_candidates": self.candidates,
                "non_ctu_metadata": self.non_ctu,
                "targeted_ctu_metadata": targeted,
            }
        }

    def test_partial_inputs_report_exact_recall_and_inconclusive_evidence(self):
        summary = SUMMARY.build_summary(
            self.manifest,
            self.run,
            self.report,
            self._inputs(self.root / "targeted-not-finished.json"),
        )

        self.assertEqual(summary["coverage"]["unique_translation_units"]["value"], 0.5)
        self.assertEqual(summary["metrics"]["candidate_exact_recall"]["value"], 0.5)
        self.assertEqual(summary["metrics"]["evidence_recall"]["value"], 0)
        self.assertEqual(summary["metrics"]["confirmation_recall"]["value"], 0)
        self.assertEqual(summary["layers"]["targeted_ctu"]["status"], "not_run")
        self.assertEqual(summary["layers"]["asan"]["status"], "not_run")
        self.assertEqual(
            [item["candidate_status"] for item in summary["defects"]],
            ["exact_match", "missed"],
        )
        self.assertTrue(
            all(
                item["evidence_status"] == "inconclusive"
                and item["confirmation_status"] == "inconclusive"
                for item in summary["defects"]
            )
        )
        markdown = SUMMARY.render_markdown(summary)
        self.assertIn("Candidate exact recall | 50.0% (1/2)", markdown)
        self.assertIn("Missing or unfinished ASan evidence is `inconclusive`", markdown)

    def test_targeted_ctu_recovers_coverage_and_only_confirmed_asan_counts(self):
        targeted = self._write(
            "targeted.json",
            {
                "schema": "agent-runtime/layered-ctu-execution/v1",
                "executions": [
                    {
                        "status": "complete",
                        "successful_translation_units": 1,
                        "failed_translation_units": 0,
                        "successful_sources": ["/workspace/source/src/two.cpp"],
                        "failed_sources": [],
                    },
                    {
                        "status": "not_run",
                        "successful_translation_units": 0,
                        "failed_translation_units": 0,
                        "successful_sources": [],
                        "failed_sources": [],
                    },
                ],
            },
        )
        evidence = self._write(
            "evidence.json",
            {
                "evidence": [
                    {"defect_id": "defect-one", "status": "confirmed"},
                    {"defect_id": "defect-two", "status": "inconclusive"},
                ]
            },
        )

        summary = SUMMARY.build_summary(
            self.manifest,
            self.run,
            self.report,
            self._inputs(targeted),
            asan_evidence_path=evidence,
        )

        self.assertEqual(summary["coverage"]["unique_translation_units"]["value"], 1)
        self.assertEqual(summary["layers"]["targeted_ctu"]["status"], "partial")
        self.assertEqual(
            summary["coverage"]["projects"][self.project_id][
                "recovered_by_targeted_ctu"
            ],
            1,
        )
        self.assertEqual(summary["metrics"]["evidence_recall"]["value"], 1)
        self.assertEqual(summary["metrics"]["confirmation_recall"]["value"], 0.5)
        self.assertEqual(summary["defects"][0]["confirmation_status"], "confirmed")
        self.assertEqual(summary["defects"][1]["confirmation_status"], "inconclusive")

    def test_targeted_diagnostic_is_a_candidate_even_during_partial_execution(self):
        targeted = self._write(
            "targeted.json",
            {
                "executions": [
                    {
                        "status": "partial",
                        "successful_translation_units": 1,
                        "total_translation_units": 2,
                        "successful_sources": ["/workspace/source/src/one.cpp"],
                        "failed_sources": [],
                        "diagnostics": [
                            {
                                "path": "/workspace/source/src/two.cpp",
                                "line": 20,
                                "rule_id": "ctu",
                            }
                        ],
                    }
                ],
                "target_analysis": {"src/two.cpp": {"status": "partial"}},
            },
        )
        summary = SUMMARY.build_summary(
            self.manifest, self.run, self.report, self._inputs(targeted)
        )
        self.assertEqual(summary["metrics"]["candidate_exact_recall"]["numerator"], 2)
        self.assertEqual(
            summary["defects"][1]["targeted_ctu_candidate_status"], "exact_match"
        )
        self.assertEqual(
            summary["layers"]["targeted_ctu"]["projects"][self.project_id][
                "total_translation_units"
            ],
            2,
        )

    def test_failed_target_without_hit_is_unevaluable_not_missed(self):
        targeted = self._write(
            "failed.json",
            {
                "executions": [{"status": "execution_failure"}],
                "target_analysis": {"src/two.cpp": {"status": "execution_failure"}},
            },
        )
        summary = SUMMARY.build_summary(
            self.manifest, self.run, self.report, self._inputs(targeted)
        )
        self.assertEqual(summary["defects"][1]["candidate_status"], "unevaluable")
        self.assertEqual(summary["metrics"]["candidate_recall_upper_bound"]["value"], 1)

    def test_overlapping_tu_attempts_do_not_inflate_coverage(self):
        targeted = self._write(
            "overlap.json",
            {
                "executions": [
                    {
                        "status": "complete",
                        "successful_translation_units": 2,
                        "successful_sources": [
                            "/workspace/source/src/one.cpp",
                            "/workspace/source/src/two.cpp",
                        ],
                    }
                    for _ in range(3)
                ]
            },
        )
        summary = SUMMARY.build_summary(
            self.manifest, self.run, self.report, self._inputs(targeted)
        )
        self.assertEqual(
            summary["coverage"]["unique_translation_units"]["numerator"], 2
        )
        layer = summary["layers"]["targeted_ctu"]["projects"][self.project_id]
        self.assertEqual(layer["unique_successful_translation_units"], 2)
        self.assertEqual(layer["successful_translation_units"], 6)

    def test_zero_candidates_makes_evidence_recall_unavailable(self):
        self.candidates = self._write("none.json", {"candidates": []})
        summary = SUMMARY.build_summary(
            self.manifest, self.run, self.report, self._inputs()
        )
        self.assertIsNone(summary["metrics"]["evidence_recall"]["value"])

    def test_invalid_target_metadata_is_not_success(self):
        targeted = self._write(
            "bad.json",
            {"executions": [{"successful_translation_units": "not a number"}]},
        )
        summary = SUMMARY.build_summary(
            self.manifest, self.run, self.report, self._inputs(targeted)
        )
        self.assertEqual(
            summary["layers"]["targeted_ctu"]["projects"][self.project_id]["status"],
            "invalid",
        )

    def test_rejects_a_report_from_another_ctu_run(self):
        self.report["run_id"] = "other-run"
        with self.assertRaisesRegex(InvalidInput, "does not match the CTU run"):
            SUMMARY.build_summary(self.manifest, self.run, self.report, self._inputs())


if __name__ == "__main__":
    unittest.main()
