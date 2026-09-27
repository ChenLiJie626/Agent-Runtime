"""Public-source boundaries and real runtime integration, without network calls."""

import asyncio
import copy
import importlib.util
import json
import io
import tarfile
import sys
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path

from agent_runtime import (
    FixedSnapshot, InvalidInput, PolicyDenied, QueryRequest, StaleSnapshot, digest,
)
from agent_runtime.adapters import ClaudeAgentExecutor, ClaudeConfig, FrozenSourceProgramQuery

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))
SPEC = importlib.util.spec_from_file_location("run_s4_runtime", ROOT / "tools/run_s4_runtime.py")
PIPELINE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(PIPELINE)
sys.path.remove(str(ROOT / "tools"))


class SourceRuntimeTests(unittest.TestCase):
    def setUp(self):
        self.sources = {"a.cpp": "int f() {\n return 1;\n int x=2;\n}\n"}
        self.snapshot = FixedSnapshot("fixture", "surface", FrozenSourceProgramQuery.source_digest(self.sources),
                                      digest("profile"), digest("policy"))
        self.backend = FrozenSourceProgramQuery(self.snapshot, self.sources)
        self.query = QueryRequest("query", "task", "source", "read_source", self.snapshot.snapshot_digest,
                                  "frozen-source", "1", self.snapshot.tool_policy_digest,
                                  {"scope_id": "surface", "selectors": {"path": "a.cpp"}},
                                  {"path": "a.cpp", "start_line": 1, "end_line": 4}, "key", 1000)

    def test_source_is_copied_and_complete_is_only_text_coverage(self):
        self.sources["a.cpp"] = "modified"
        result = self.backend.query(self.query)
        raw = json.loads(result.raw)
        self.assertEqual(raw["lines"][1], {"line": 2, "text": " return 1;"})
        self.assertEqual(raw["semantic_coverage"], "not_assessed")
        self.assertEqual(result.coverage.completeness, "complete")
        self.assertTrue(result.limitations)
        with self.assertRaises(TypeError):
            self.backend.sources["a.cpp"] = "changed"

    def test_source_and_query_snapshot_integrity(self):
        with self.assertRaises(StaleSnapshot):
            FrozenSourceProgramQuery(self.snapshot, {"a.cpp": "changed"})
        for request in (replace(self.query, snapshot_digest=digest("other")),
                        replace(self.query, scope={"scope_id": "other", "selectors": {"path": "a.cpp"}})):
            with self.assertRaises(StaleSnapshot):
                self.backend.query(request)
        with self.assertRaises(PolicyDenied):
            self.backend.query(replace(self.query, tool_policy_digest=digest("other")))
        with self.assertRaises(PolicyDenied):
            self.backend.query(replace(self.query, scope={"scope_id": "surface", "selectors": {"path": "b.cpp"}}))

    def test_source_windows_reject_invalid_ranges_types_and_traversal(self):
        for args in ({"path": "../a.cpp", "start_line": 1, "end_line": 2},
                     {"path": "a.cpp", "start_line": 0, "end_line": 2},
                     {"path": "a.cpp", "start_line": 1, "end_line": 5},
                     {"path": "a.cpp", "start_line": True, "end_line": 2}):
            with self.assertRaises(InvalidInput):
                self.backend.query(replace(self.query, args=args))
        for options in ({"max_lines": 2}, {"max_bytes": 10}):
            with self.assertRaises(InvalidInput):
                FrozenSourceProgramQuery(self.snapshot, self.sources, **options).query(self.query)

    def semantic_bundle(self):
        finding = PIPELINE.find_unreachable_after_return(self.sources["a.cpp"], "a.cpp")[0]
        raw = PIPELINE.canonical_json({
            "operation": "inspect_unreachable_after_return",
            "cpg_sha256": "c" * 64,
            "source_path": "a.cpp",
            "return_line": finding["return_line"],
            "following_line": finding["following_line"],
            "rows": [
                {"kind": "candidate_methods", "nodes": [{"_id": 1, "_label": "METHOD"}]},
                {"kind": "return_nodes", "nodes": [{"_id": 2, "_label": "RETURN"}]},
                {"kind": "following_nodes", "nodes": [{"_id": 3, "_label": "CALL"}]},
                {"kind": "return_cfg_ancestors", "nodes": [{"_id": 1, "_label": "METHOD"}]},
                {"kind": "following_cfg_ancestors", "nodes": [{"_id": 3, "_label": "CALL"}]},
            ],
        }).encode()
        observation = {
            **finding, "status": "partial", "coverage": "partial",
            "omissions": ["surface-only CPG"], "limitations": ["frozen build not applied"],
            "cfg_observation": {
                "method_count": 1, "return_count": 1, "following_node_count": 1,
                "return_reaches_method_entry": True,
                "following_reaches_method_entry": False,
            },
        }
        return {
            "backend_version": "fixture-joern", "report_digest": "d" * 64,
            "cpg_sha256": "c" * 64,
            "observations": {finding["candidate_id"]: {"observation": observation, "raw": raw}},
        }

    def run_pipeline(self, agent=None, head=None, max_candidates=10, semantic_bundle=None):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        view = {"project_id": "fixture", "sample_id": "one", "base_commit": "a", "head_commit": "b"}
        agent = agent or PIPELINE.ReferenceReader()
        return PIPELINE.analyze_sources(view, {"a.cpp": "int f() {\n return 0;\n}\n"},
                                        head or self.sources, Path(directory.name) / "run",
                                        lambda snapshot, root: agent, max_candidates=max_candidates,
                                        semantic_bundle=semantic_bundle)

    def test_roles_read_original_evidence_and_reopen_preserves_report(self):
        result = self.run_pipeline()
        self.assertTrue(result["reopened_report_verified"])
        self.assertEqual([item["role"] for item in result["role_calls"]], ["investigator", "verifier"])
        self.assertEqual(len({item["attempt_id"] for item in result["role_calls"]}), 2)
        reads = [item["evidence_reads"][0] for item in result["role_calls"]]
        self.assertEqual(reads[0], reads[1])
        candidate = result["candidates"][0]
        self.assertEqual(candidate["status"], "inconclusive")
        self.assertEqual(candidate["checks"][1]["status"], "unexamined")

    def test_role_failure_and_timeout_are_not_successful_abstentions(self):
        for error, status in ((RuntimeError("fixture failure"), "execution_failure"), (TimeoutError(), "timeout")):
            class Failing(PIPELINE.ReferenceReader):
                def run(self, request, tools):
                    raise error
            result = self.run_pipeline(Failing())
            self.assertEqual(result["candidates"][0]["status"], status)
            self.assertEqual(result["role_calls"][0]["status"], "failed")
            self.assertEqual(result["candidates"][0]["verdict"], "inconclusive")

    def test_overconfident_roles_do_not_complete_semantic_checks(self):
        class Overconfident(PIPELINE.ReferenceReader):
            def run(self, request, tools):
                result = super().run(request, tools)
                return replace(result, output=replace(result.output, unresolved_items=()))
        result = self.run_pipeline(Overconfident())
        self.assertEqual(result["candidates"][0]["verdict"], "inconclusive")
        self.assertEqual(result["candidates"][0]["checks"][1]["status"], "unexamined")

    def test_partial_joern_evidence_is_read_by_both_roles_and_cannot_confirm(self):
        result = self.run_pipeline(semantic_bundle=self.semantic_bundle())
        candidate = result["candidates"][0]
        self.assertEqual(result["query_count"], 2)
        self.assertEqual(candidate["verdict"], "inconclusive")
        self.assertEqual(candidate["checks"][1]["status"], "partial")
        self.assertEqual(candidate["checks"][1]["answer"], "unknown")
        self.assertEqual(candidate["semantic_report_digest"], "d" * 64)
        self.assertEqual([len(call["evidence_reads"]) for call in result["role_calls"]], [2, 2])
        self.assertEqual(
            {read["artifact_digest"] for call in result["role_calls"] for read in call["evidence_reads"]},
            {candidate["artifact_digest"], candidate["semantic_artifact_digest"]},
        )

    def test_joern_report_rejects_tampered_raw_artifact(self):
        manifest = json.loads((ROOT / "evaluation/manifests/public-cpp-smoke-v1.json").read_text())
        sample = manifest["samples"][1]
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            raw_path = root / "raw.json"
            raw = PIPELINE.canonical_json({
                "operation": "inspect_unreachable_after_return", "cpg_sha256": "c" * 64,
                "source_path": sample["surface"]["paths"][0], "return_line": 504,
                "following_line": 505,
                "rows": [
                    {"kind": "candidate_methods", "nodes": [{"_id": 1, "_label": "METHOD"}]},
                    {"kind": "return_nodes", "nodes": [{"_id": 2, "_label": "RETURN"}]},
                    {"kind": "following_nodes", "nodes": [{"_id": 3, "_label": "CALL"}]},
                    {"kind": "return_cfg_ancestors", "nodes": [{"_id": 1, "_label": "METHOD"}]},
                    {"kind": "following_cfg_ancestors", "nodes": [{"_id": 3, "_label": "CALL"}]},
                ],
            }).encode()
            raw_path.write_bytes(raw)
            report = {
                "dataset_id": manifest["dataset_id"],
                "manifest_digest": PIPELINE.dataset_manifest_digest(manifest),
                "sample_id": sample["sample_id"], "head_commit": sample["head_commit"],
                "head_artifact_sha256": next(item["sha256"] for item in sample["artifacts"]
                                               if item["role"] == "head_source"),
                "analysis_input": {"surface_paths": sample["surface"]["paths"]},
                "backend": {"id": "joern", "version": "fixture-joern"},
                "cpg": {"sha256": "c" * 64, "source_mode": "verified manifest surface only",
                        "build_recipe_applied": False},
                "conclusion": "partial_semantic_evidence_only",
                "observations": [{
                    "candidate_id": "candidate", "path": sample["surface"]["paths"][0],
                    "return_line": 504, "following_line": 505, "status": "partial", "coverage": "partial",
                    "omissions": ["surface-only CPG"], "limitations": ["frozen build not applied"],
                    "raw_path": str(raw_path), "raw_sha256": PIPELINE.bytes_digest(raw),
                    "cfg_observation": {"method_count": 1, "return_count": 1,
                                        "following_node_count": 1,
                                        "return_reaches_method_entry": True,
                                        "following_reaches_method_entry": False},
                }],
            }
            report_path = root / "report.json"
            report_path.write_text(json.dumps(report))
            loaded = PIPELINE.load_joern_evidence(report_path, manifest)
            self.assertIn(sample["sample_id"], loaded)
            raw_path.write_bytes(b"tampered")
            with self.assertRaises(InvalidInput):
                PIPELINE.load_joern_evidence(report_path, manifest)

    def test_candidate_budget_preserves_discovered_unexecuted_candidates(self):
        result = self.run_pipeline(head={"a.cpp": "int f() {\n return 1;\n int x=2;\n}\n"
                                        "int g() {\n return 3;\n int y=4;\n}\n"}, max_candidates=1)
        self.assertEqual([item["status"] for item in result["candidates"]], ["inconclusive", "not_run"])
        self.assertEqual(len(result["role_calls"]), 2)
        budget = PIPELINE.QueryBudget(self.backend, 1)
        budget.query(self.query)
        with self.assertRaises(InvalidInput):
            budget.query(self.query)

    def test_oracle_fields_never_cross_analysis_boundary(self):
        sample = json.loads((ROOT / "evaluation/manifests/public-cpp-smoke-v1.json").read_text())["samples"][0]
        polluted = copy.deepcopy(sample)
        polluted["known_defects"] = "LABEL_SENTINEL"
        polluted["fix_metadata"] = "FIX_SENTINEL"
        polluted["review_comments"] = "REVIEW_SENTINEL"
        polluted["artifacts"][0]["review_comments"] = "ARTIFACT_SENTINEL"
        self.assertEqual(PIPELINE.analysis_input(sample), PIPELINE.analysis_input(polluted))
        for sentinel in ("LABEL_SENTINEL", "FIX_SENTINEL", "REVIEW_SENTINEL", "ARTIFACT_SENTINEL"):
            self.assertNotIn(sentinel, json.dumps(PIPELINE.analysis_input(polluted)))

    def test_sdk_role_timeout_cancels_the_execution(self):
        cancelled = []
        class Slow(ClaudeAgentExecutor):
            async def _run_async(self, request, tools):
                try:
                    await asyncio.sleep(10)
                finally:
                    cancelled.append(True)
        executor = Slow(ClaudeConfig(".", "backend", "1", "policy", role_timeout_seconds=0.01))
        class Request:
            role_kind = "investigator"
            role = "investigator"
            resume_session_id = None
        with self.assertRaises(TimeoutError):
            executor.run(Request(), None)
        self.assertEqual(cancelled, [True])
        for timeout in (True, 0, -1, float("nan"), float("inf")):
            with self.assertRaises(InvalidInput):
                ClaudeConfig(".", "backend", "1", "policy", role_timeout_seconds=timeout)

    def test_archive_paths_cannot_substitute_nested_or_duplicate_source(self):
        def archive(entries):
            buffer = io.BytesIO()
            with tarfile.open(fileobj=buffer, mode="w:gz") as tar:
                for name, body in entries:
                    info = tarfile.TarInfo(name)
                    info.size = len(body)
                    tar.addfile(info, io.BytesIO(body))
            return buffer.getvalue()
        reader = PIPELINE.read_surface_files
        body = archive([("root/nested/src/a.cpp", b"wrong"), ("root/src/a.cpp", b"right")])
        self.assertEqual(reader(body, ["src/a.cpp"]), {"src/a.cpp": "right"})
        for entries in ([('root/nested/src/a.cpp', b'wrong')],
                        [('root/src/a.cpp', b'one'), ('root/src/a.cpp', b'two')],
                        [('root/src/a.cpp', b'\xff')]):
            with self.assertRaises(InvalidInput):
                reader(archive(entries), ["src/a.cpp"])

    def test_frozen_code_can_be_recovered_and_detects_live_changes(self):
        with tempfile.TemporaryDirectory() as root:
            frozen = PIPELINE.freeze_code(Path(root))
            for path, expected in frozen["files"].items():
                self.assertEqual(PIPELINE.bytes_digest((Path(root) / 'code-snapshot' / path).read_bytes()), expected)
            PIPELINE.assert_code_unchanged(frozen)
            frozen["files"][next(iter(frozen["files"]))] = "0" * 64
            with self.assertRaises(InvalidInput):
                PIPELINE.assert_code_unchanged(frozen)


if __name__ == "__main__":
    unittest.main()
