"""Executable Q-01/Q-02 contracts for SPEC 008."""

from __future__ import annotations

import copy
import importlib.util
import unittest
from pathlib import Path

from agent_runtime import InvalidInput
from agent_runtime.codec import bytes_digest, digest
from agent_runtime.evaluation import (
    build_quality_report,
    compare_quality_reports,
    dataset_manifest_digest,
    evaluate_release_gate,
    gate_policy_digest,
    load_json_object,
    render_quality_markdown,
    validate_dataset_manifest,
    validate_evaluation_run,
    validate_gate_policy,
    verify_manifest_artifacts,
)

ROOT = Path(__file__).resolve().parents[1]
TEXT_BASELINE_SPEC = importlib.util.spec_from_file_location(
    "run_s4_text_baseline", ROOT / "tools/run_s4_text_baseline.py"
)
TEXT_BASELINE = importlib.util.module_from_spec(TEXT_BASELINE_SPEC)
TEXT_BASELINE_SPEC.loader.exec_module(TEXT_BASELINE)


def sample(sample_id="sample-one", project_id="example/project"):
    base = "1" * 40
    head = "2" * 40
    artifact_body = b"public source"
    commands = [["cmake", "-S", ".", "-B", "build"]]
    toolchain = {"cxx": "clang++", "version": "17"}
    return {
        "sample_id": sample_id,
        "project_id": project_id,
        "repository_url": f"https://github.com/{project_id}",
        "language": "C++",
        "partition": "holdout",
        "public_at": "2026-01-01T00:00:00Z",
        "pr_url": f"https://github.com/{project_id}/pull/1",
        "base_commit": base,
        "head_commit": head,
        "license": {
            "spdx": "MIT",
            "url": f"https://github.com/{project_id}/blob/{head}/LICENSE",
            "redistribution": "MIT terms",
        },
        "artifacts": [
            {
                "role": "base_source",
                "url": f"https://example.org/source/{base}.tar.gz",
                "sha256": bytes_digest(artifact_body),
                "size_bytes": len(artifact_body),
            },
            {
                "role": "head_source",
                "url": f"https://example.org/source/{head}.tar.gz",
                "sha256": bytes_digest(artifact_body),
                "size_bytes": len(artifact_body),
            },
        ],
        "build": {
            "variant_id": "clang-cxx17",
            "commands": commands,
            "toolchain": toolchain,
            "recipe_digest": digest({
                "commands": commands,
                "toolchain": toolchain,
                "network": "disabled",
            }),
            "dependency_digest": "4" * 64,
            "network": "disabled",
        },
        "input_policy": {
            "allowed_to_agent": ["base_source", "head_source"],
            "withheld_from_agent": ["known_defects", "fix_metadata"],
        },
        "surface": {
            "files": 1,
            "changed_lines": 20,
            "paths": ["src/example.cpp"],
        },
        "known_defects": [
            {
                "defect_id": f"{sample_id}-defect",
                "category": "logic",
                "difficulty": "medium",
                "location": {"path": "src/example.cpp", "line": 10},
                "label_source_url": f"https://github.com/{project_id}/pull/1#discussion_r1",
                "label_digest": "5" * 64,
            }
        ],
    }


def manifest():
    source = b"benchmark"
    return {
        "schema_version": {"major": 1, "minor": 0},
        "dataset_id": "test-public-cpp",
        "frozen_at": "2026-01-02T00:00:00Z",
        "provenance": {
            "name": "public benchmark",
            "url": "https://example.org/benchmark",
            "revision": "6" * 40,
            "license_spdx": "MIT",
            "license_url": "https://example.org/benchmark/LICENSE",
            "source_artifact": {
                "url": "https://example.org/benchmark.json",
                "sha256": bytes_digest(source),
                "size_bytes": len(source),
            },
        },
        "samples": [sample()],
    }


def successful_run(dataset):
    item = dataset["samples"][0]
    defect_id = item["known_defects"][0]["defect_id"]
    return {
        "schema_version": {"major": 1, "minor": 0},
        "run_id": "run-one",
        "dataset_id": dataset["dataset_id"],
        "manifest_digest": dataset_manifest_digest(dataset),
        "created_at": "2026-01-03T00:00:00Z",
        "bindings": {
            "code_version": "abc123",
            "rule": {"id": "cpp.example", "version": "1"},
            "backend": {"id": "fixture", "version": "1"},
            "prompt_digests": {"investigator": "7" * 64},
            "model_route": "model/test",
            "budget": {
                "wall_time_seconds": 60,
                "query_limit": 10,
                "token_limit": 1000,
                "cost_limit_usd": 1,
            },
            "top_k": 1,
        },
        "sample_results": [
            {
                "sample_id": item["sample_id"],
                "analysis_status": "complete",
                "coverage": {
                    "total_targets": 1,
                    "analyzed_targets": 1,
                    "total_translation_units": 2,
                    "analyzed_translation_units": 2,
                    "lines_scanned": 1000,
                    "lines_changed": 20,
                    "omissions": [],
                },
                "resource": {
                    "duration_seconds": 2,
                    "query_count": 4,
                    "model_tokens": 500,
                    "cost_usd": 0.25,
                },
                "known_defects": [
                    {
                        "defect_id": defect_id,
                        "evaluable": True,
                        "candidate_status": "matched",
                        "candidate_ids": ["known"],
                        "evidence_status": "complete",
                        "verdict": "confirmed",
                        "excluded": False,
                    }
                ],
                "candidates": [
                    {
                        "candidate_id": "known",
                        "known_defect_id": defect_id,
                        "status": "confirmed",
                    },
                    {
                        "candidate_id": "noise",
                        "known_defect_id": None,
                        "status": "confirmed",
                    },
                ],
                "alerts": [
                    {
                        "alert_id": "known-a",
                        "candidate_id": "known",
                        "rank": 1,
                        "duplicate_group": "known-group",
                        "adjudication": "true_defect",
                    },
                    {
                        "alert_id": "known-b",
                        "candidate_id": "known",
                        "rank": 2,
                        "duplicate_group": "known-group",
                        "adjudication": "true_defect",
                    },
                    {
                        "alert_id": "noise-a",
                        "candidate_id": "noise",
                        "rank": 3,
                        "duplicate_group": "noise-group",
                        "adjudication": "false_alarm",
                    },
                ],
            }
        ],
    }


def gate_policy(dataset):
    return {
        "schema_version": {"major": 1, "minor": 0},
        "policy_id": "test-gate",
        "registered_at": "2026-01-02T12:00:00Z",
        "purpose": "release",
        "partition": "holdout",
        "dataset_id": dataset["dataset_id"],
        "manifest_digest": dataset_manifest_digest(dataset),
        "bindings": {
            "rule": {"id": "cpp.example", "version": "1"},
            "backend": {"id": "fixture", "version": "1"},
        },
        "sufficiency": {
            "min_projects": 1,
            "min_evaluable_known_positives": 1,
            "min_adjudicated_confirmed_alerts": 1,
        },
        "minimums": {
            "target_coverage": 1,
            "candidate_recall": 1,
            "confirmation_recall": 1,
            "release_precision": 0.5,
        },
        "maximums": {
            "false_alarm_burden_per_kloc": 1,
            "duration_seconds": 2,
            "query_count": 4,
            "model_tokens": 500,
            "cost_usd": 0.25,
        },
        "hard_limits": {
            "known_positives_silently_excluded": 0,
            "build_failures": 0,
            "timeouts": 0,
        },
    }


class EvaluationContracts(unittest.TestCase):
    def test_text_baseline_finds_only_statements_after_same_block_return(self):
        source = """\
bool broken() {
  return true;
  work();
}
bool complete() {
  if (ready) return true;
  return false;
}
"""
        findings = TEXT_BASELINE.find_unreachable_after_return(
            source, "src/example.cpp"
        )
        self.assertEqual(len(findings), 1)
        self.assertEqual(findings[0]["return_line"], 2)
        self.assertEqual(findings[0]["following_line"], 3)
        self.assertEqual(
            TEXT_BASELINE.added_line_numbers(
                "bool f() {\n  work();\n}\n",
                "bool f() {\n  return true;\n  work();\n}\n",
            ),
            {2},
        )

    def test_committed_public_cpp_manifest_is_valid(self):
        value = load_json_object(
            ROOT / "evaluation/manifests/public-cpp-smoke-v1.json"
        )
        validate_dataset_manifest(value)
        self.assertEqual(len(dataset_manifest_digest(value)), 64)
        self.assertEqual(len(value["samples"]), 2)
        self.assertTrue(all(sample["language"] == "C++" for sample in value["samples"]))

        uaf = load_json_object(
            ROOT / "evaluation/manifests/public-cpp-uaf-v1.json"
        )
        validate_dataset_manifest(uaf)
        self.assertEqual(len(uaf["samples"]), 5)
        self.assertEqual(
            {sample["partition"] for sample in uaf["samples"]},
            {"tuning", "holdout"},
        )
        self.assertTrue(all(
            sample["artifact_scope"] == "primary-defect-file"
            for sample in uaf["samples"]
        ))

    def test_committed_q07_policies_are_valid_and_bound_to_manifest(self):
        value = load_json_object(
            ROOT / "evaluation/manifests/public-cpp-smoke-v1.json"
        )
        policy_paths = sorted((ROOT / "evaluation/policies").glob("*.json"))
        self.assertEqual(len(policy_paths), 2)
        for policy_path in policy_paths:
            with self.subTest(policy=policy_path.name):
                policy = load_json_object(policy_path)
                validate_gate_policy(policy)
                self.assertEqual(
                    policy["manifest_digest"], dataset_manifest_digest(value)
                )
                self.assertEqual(len(gate_policy_digest(policy)), 64)

    def test_manifest_rejects_non_cpp_and_private_urls(self):
        value = manifest()
        value["samples"][0]["language"] = "C"
        with self.assertRaisesRegex(InvalidInput, "must be C\\+\\+"):
            validate_dataset_manifest(value)
        value = manifest()
        value["samples"][0]["repository_url"] = "file:///private/repo"
        with self.assertRaisesRegex(InvalidInput, "public HTTPS"):
            validate_dataset_manifest(value)

    def test_artifact_verification_checks_digest_and_size_without_execution(self):
        value = manifest()

        def fetch(url, max_bytes):
            body = b"benchmark" if url.endswith("benchmark.json") else b"public source"
            return body, url

        verified = verify_manifest_artifacts(value, fetcher=fetch)
        self.assertEqual(len(verified), 3)
        self.assertTrue(all(item["status"] == "verified" for item in verified))

        def tampered(url, max_bytes):
            return b"tampered", url

        with self.assertRaisesRegex(InvalidInput, "digest mismatch"):
            verify_manifest_artifacts(value, fetcher=tampered)

    def test_run_cannot_hide_a_known_positive(self):
        value = manifest()
        run = successful_run(value)
        run["sample_results"][0]["known_defects"] = []
        with self.assertRaisesRegex(InvalidInput, "must account for every known defect"):
            validate_evaluation_run(run, value)

    def test_report_uses_deduped_alerts_and_preserves_raw_denominators(self):
        value = manifest()
        report = build_quality_report(value, successful_run(value))
        metrics = report["metrics"]
        self.assertEqual(metrics["candidate_recall"]["value"], 1)
        self.assertEqual(metrics["evidence_recall"]["value"], 1)
        self.assertEqual(metrics["confirmation_recall"]["value"], 1)
        self.assertEqual(metrics["release_precision"]["value"], 0.5)
        self.assertEqual(metrics["adjudication"]["published_before_dedup"], 3)
        self.assertEqual(metrics["adjudication"]["published_after_dedup"], 2)
        self.assertEqual(metrics["false_alarm_burden_per_kloc"]["value"], 1)
        self.assertEqual(metrics["top_k_precision"]["value"], 1)
        self.assertEqual(report["evidence_sufficiency"], "insufficient_evidence")
        self.assertIn("Candidate recall (evaluable only) | 100.0% (1/1)",
                      render_quality_markdown(report))

    def test_shared_coverage_scope_deduplicates_project_measurements(self):
        value = manifest()
        second_sample = sample("sample-two")
        value["samples"].append(second_sample)
        run = successful_run(value)
        first = run["sample_results"][0]
        second = copy.deepcopy(first)
        first["coverage_scope_id"] = "example-project-analysis"
        second["coverage_scope_id"] = "example-project-analysis"
        second["sample_id"] = second_sample["sample_id"]
        second["coverage"]["lines_changed"] = 30
        second_defect_id = second_sample["known_defects"][0]["defect_id"]
        second["known_defects"][0]["defect_id"] = second_defect_id
        second["candidates"][0]["known_defect_id"] = second_defect_id
        run["sample_results"].append(second)
        run["manifest_digest"] = dataset_manifest_digest(value)

        report = build_quality_report(value, run)

        self.assertEqual(report["coverage"]["targets"]["denominator"], 2)
        self.assertEqual(
            report["coverage"]["translation_units"]["denominator"], 2
        )
        self.assertEqual(report["coverage"]["lines_scanned"], 1000)
        self.assertEqual(report["coverage"]["lines_changed"], 50)
        self.assertEqual(report["metrics"]["efficiency"]["duration_seconds"], 2)
        self.assertEqual(report["metrics"]["efficiency"]["query_count"], 4)

        second["resource"]["query_count"] = 5
        with self.assertRaisesRegex(InvalidInput, "shared scopes must have identical"):
            validate_evaluation_run(run, value)

    def test_unevaluable_defects_produce_full_dataset_recall_bounds(self):
        value = manifest()
        second_sample = sample("sample-two")
        value["samples"].append(second_sample)
        run = successful_run(value)
        second = copy.deepcopy(run["sample_results"][0])
        second["sample_id"] = second_sample["sample_id"]
        second_defect_id = second_sample["known_defects"][0]["defect_id"]
        second["known_defects"] = [{
            "defect_id": second_defect_id,
            "evaluable": False,
            "candidate_status": "not_run",
            "candidate_ids": [],
            "evidence_status": "not_applicable",
            "verdict": "not_run",
            "excluded": False,
        }]
        second["analysis_status"] = "execution_failure"
        second["coverage"]["analyzed_targets"] = 0
        second["coverage"]["omissions"] = ["analyzer execution failed"]
        second["candidates"] = []
        second["alerts"] = []
        run["sample_results"].append(second)
        run["manifest_digest"] = dataset_manifest_digest(value)

        report = build_quality_report(value, run)
        metrics = report["metrics"]

        self.assertEqual(metrics["candidate_recall"]["value"], 1)
        self.assertEqual(metrics["candidate_recall"]["denominator"], 1)
        self.assertEqual(metrics["candidate_recall_bounds"]["lower"]["value"], 0.5)
        self.assertEqual(metrics["candidate_recall_bounds"]["upper"]["value"], 1)
        self.assertEqual(metrics["candidate_recall_evaluable"]["value"], 1)
        self.assertEqual(metrics["candidate_recall_lower_bound"]["value"], 0.5)
        self.assertEqual(
            metrics["known_positive_outcomes"]["evaluable_candidate_misses"], 0
        )
        self.assertEqual(
            metrics["known_positive_outcomes"]["unevaluable_by_analysis_status"]["execution_failure"],
            1,
        )
        self.assertEqual(
            metrics["confirmation_recall_bounds"]["lower"]["value"], 0.5
        )
        self.assertEqual(
            metrics["confirmation_recall_bounds"]["upper"]["value"], 1
        )
        markdown = render_quality_markdown(report)
        self.assertIn("Candidate recall lower bound (all known positives) | 50.0% (1/2)", markdown)
        self.assertIn("1 unevaluable", markdown)

    def test_gate_metrics_are_scoped_to_preregistered_partition(self):
        value = manifest()
        value["samples"][0]["partition"] = "tuning"
        holdout_sample = sample("sample-two", "other/project")
        value["samples"].append(holdout_sample)
        run = successful_run(value)
        first = run["sample_results"][0]
        second = copy.deepcopy(first)
        second["sample_id"] = holdout_sample["sample_id"]
        second["known_defects"] = [{
            "defect_id": holdout_sample["known_defects"][0]["defect_id"],
            "evaluable": True,
            "candidate_status": "missed",
            "candidate_ids": [],
            "evidence_status": "missing",
            "verdict": "missed",
            "excluded": False,
        }]
        second["candidates"] = []
        second["alerts"] = []
        run["sample_results"].append(second)
        run["manifest_digest"] = dataset_manifest_digest(value)

        report = build_quality_report(value, run)
        self.assertEqual(report["metrics"]["candidate_recall"]["value"], 0.5)
        self.assertEqual(
            report["partition_scopes"]["holdout"]["metrics"]
            ["candidate_recall"]["value"],
            0,
        )
        policy = gate_policy(value)
        policy["minimums"]["candidate_recall"] = 0.5
        decision = evaluate_release_gate(report, policy)
        recall_check = next(
            item for item in decision["metric_checks"]
            if item["name"] == "candidate_recall"
        )
        self.assertEqual(decision["partition"], "holdout")
        self.assertEqual(recall_check["actual"], 0)
        self.assertEqual(decision["decision"], "failed")

    def test_release_policy_rejects_tuning_partition(self):
        value = manifest()
        policy = gate_policy(value)
        policy["partition"] = "tuning"
        with self.assertRaisesRegex(
            InvalidInput, "release policies must use holdout or shadow"
        ):
            validate_gate_policy(policy)

    def test_same_budget_comparison_reports_deltas_and_ablations(self):
        value = manifest()
        agent = build_quality_report(value, successful_run(value))

        def missed_report(run_id, rule_id):
            run = successful_run(value)
            run["run_id"] = run_id
            run["bindings"]["rule"]["id"] = rule_id
            result = run["sample_results"][0]
            defect = result["known_defects"][0]
            defect.update({
                "candidate_status": "missed",
                "candidate_ids": [],
                "evidence_status": "missing",
                "verdict": "missed",
            })
            result["candidates"] = []
            result["alerts"] = []
            return build_quality_report(value, run)

        baseline = missed_report("baseline-run", "cpp.baseline")
        previous = missed_report("previous-run", "cpp.previous")
        comparison = compare_quality_reports(
            {
                "baseline": baseline,
                "agent": agent,
                "previous_release": previous,
            },
            partition="holdout",
            ablations={"path-verifier": baseline},
        )
        candidate_delta = comparison["agent_deltas"]["vs_baseline"][
            "candidate_recall"
        ]
        self.assertEqual(candidate_delta["delta"], 1)
        self.assertEqual(candidate_delta["status"], "improved")
        self.assertEqual(
            comparison["roles"]["baseline"]["failure_samples"][0][
                "missed_defect_ids"
            ],
            ["sample-one-defect"],
        )
        self.assertIn("path-verifier", comparison["ablations"])
        self.assertEqual(
            compare_quality_reports(
                {
                    "baseline": baseline,
                    "agent": agent,
                    "previous_release": previous,
                },
                partition="all",
            )["roles"]["baseline"]["failure_samples"][0]["sample_id"],
            "sample-one",
        )

        mismatched = copy.deepcopy(baseline)
        mismatched["bindings"]["budget"]["query_limit"] += 1
        mismatched["report_digest"] = digest({
            key: value for key, value in mismatched.items()
            if key != "report_digest"
        })
        with self.assertRaisesRegex(InvalidInput, "budget.*does not match"):
            compare_quality_reports(
                {
                    "baseline": mismatched,
                    "agent": agent,
                    "previous_release": previous,
                },
                partition="holdout",
            )

    def test_manifest_digest_binds_run(self):
        value = manifest()
        run = successful_run(value)
        changed = copy.deepcopy(value)
        changed["samples"][0]["build"]["variant_id"] = "different"
        with self.assertRaisesRegex(InvalidInput, "frozen manifest"):
            validate_evaluation_run(run, changed)

    def test_preregistered_gate_passes_fails_or_reports_insufficient_evidence(self):
        value = manifest()
        report = build_quality_report(value, successful_run(value))
        policy = gate_policy(value)
        self.assertEqual(evaluate_release_gate(report, policy)["decision"], "passed")

        failing = copy.deepcopy(policy)
        failing["minimums"]["release_precision"] = 0.9
        decision = evaluate_release_gate(report, failing)
        self.assertEqual(decision["decision"], "failed")
        self.assertEqual(
            next(item for item in decision["metric_checks"]
                 if item["name"] == "release_precision")["status"],
            "failed",
        )

        insufficient = copy.deepcopy(policy)
        insufficient["sufficiency"]["min_projects"] = 2
        self.assertEqual(
            evaluate_release_gate(report, insufficient)["decision"],
            "insufficient_evidence",
        )

    def test_gate_rejects_a_tampered_report(self):
        value = manifest()
        report = build_quality_report(value, successful_run(value))
        report["metrics"]["candidate_recall"]["value"] = 0
        with self.assertRaisesRegex(InvalidInput, "does not bind"):
            evaluate_release_gate(report, gate_policy(value))


if __name__ == "__main__":
    unittest.main()
