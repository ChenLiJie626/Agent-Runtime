"""Reusable discovery, backend, evaluator, executor and pipeline contracts."""

import json
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path

from agent_runtime import (
    AnalysisPipeline,
    CppUnreachableDiscoverer,
    CppUnreachableEvaluator,
    DefectRuntime,
    EvidencePlan,
    EvidenceReviewExecutor,
    FixedSnapshot,
    InvalidInput,
    ProgramResult,
    QueryOperation,
    SQLiteStore,
    analysis_profile_digest,
    bytes_digest,
    cpp_unreachable_profile,
    cpp_unreachable_rule,
    digest,
)
from agent_runtime.adapters import CompositeProgramQuery, FrozenSourceProgramQuery
from agent_runtime.domain import Coverage, DiscoveryInput


class SemanticBackend:
    backend_id = "fixture.semantic"
    backend_version = "1"
    read_only = True
    idempotent_retry = True
    supported_operations = frozenset({"check_unreachable"})
    operation_specs = {
        "check_unreachable": QueryOperation(
            "check_unreachable",
            {"candidate_id": "string"},
            ("candidate_id",),
            ("candidate_id",),
        ),
    }

    def query(self, request):
        raw = json.dumps({"candidate_id": request.args["candidate_id"], "unreachable": True}).encode()
        return ProgramResult(
            "complete",
            Coverage("one exact CFG candidate", "one exact CFG candidate", "complete", (), (bytes_digest(raw),)),
            raw,
        )


class SemanticDiscoverer:
    discoverer_id = "fixture.semantic-discovery"
    discoverer_version = "1"

    def __init__(self, polarity="positive"):
        self.inner = CppUnreachableDiscoverer()
        self.polarity = polarity

    def discover(self, input: DiscoveryInput):
        result = []
        for candidate in self.inner.discover(input):
            semantic = EvidencePlan(
                "semantic_validation",
                "check_unreachable",
                {"candidate_id": candidate.candidate_id},
                {"candidate_id": candidate.candidate_id},
                "semantic_unreachable",
                self.polarity,
            )
            result.append(replace(
                candidate,
                discovery_ref=f"fixture:{candidate.candidate_id}",
                evidence_plans=(*candidate.evidence_plans, semantic),
            ))
        return tuple(result)


class PipelineTests(unittest.TestCase):
    def run_pipeline(self, *, polarity="positive", max_candidates=10):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        root = Path(directory.name)
        base = {"src/a.cpp": "int f() {\n  return 0;\n}\n"}
        head = {"src/a.cpp": "int f() {\n  return 1;\n  use();\n}\n"}
        rule = cpp_unreachable_rule()
        policy = digest({"operations": ["read_source", "check_unreachable"]})
        profile = cpp_unreachable_profile(
            policy, allowed_operations=("read_source", "check_unreachable"),
        )
        snapshot = FixedSnapshot(
            "fixture/project",
            "change",
            FrozenSourceProgramQuery.source_digest(head),
            analysis_profile_digest(rule, profile=profile),
            policy,
        )
        backend = CompositeProgramQuery(
            FrozenSourceProgramQuery(snapshot, head), SemanticBackend(),
        )
        store = SQLiteStore(root / "runtime.sqlite3", root / "artifacts")
        self.addCleanup(store.close)
        pipeline = AnalysisPipeline(
            DefectRuntime(store),
            rule=rule,
            profile=profile,
            discoverer=SemanticDiscoverer(polarity),
            backend=backend,
            evaluator=CppUnreachableEvaluator(),
            executor=EvidenceReviewExecutor(),
            owner_id="fixture-worker",
            working_dir=str(root),
        )
        return pipeline.run(
            snapshot,
            base_sources=base,
            head_sources=head,
            max_candidates=max_candidates,
        )

    def test_bundled_components_confirm_complete_semantic_evidence(self):
        result = self.run_pipeline()
        self.assertEqual(len(result.candidates), 1)
        self.assertEqual(result.candidates[0].status, "confirmed")
        self.assertEqual(result.report["candidate_reports"][0]["verdict"], "confirmed")

    def test_third_party_discoverer_can_drive_refutation(self):
        result = self.run_pipeline(polarity="negative")
        self.assertEqual(result.candidates[0].status, "refuted")
        self.assertEqual(result.report["candidate_reports"][0]["verdict"], "refuted")

    def test_candidate_limit_preserves_discovery_without_execution(self):
        result = self.run_pipeline(max_candidates=0)
        self.assertEqual(result.candidates[0].status, "not_run")
        self.assertIsNone(result.candidates[0].decision)

    def test_source_only_discovery_keeps_semantics_inconclusive(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        root = Path(directory.name)
        base = {"a.cpp": "int f() {\n return 0;\n}\n"}
        head = {"a.cpp": "int f() {\n return 1;\n use();\n}\n"}
        rule = cpp_unreachable_rule()
        policy = digest({"operations": ["read_source"]})
        profile = cpp_unreachable_profile(policy)
        snapshot = FixedSnapshot(
            "fixture", "change", FrozenSourceProgramQuery.source_digest(head),
            analysis_profile_digest(rule, profile=profile), policy,
        )
        store = SQLiteStore(root / "runtime.sqlite3", root / "artifacts")
        self.addCleanup(store.close)
        result = AnalysisPipeline(
            DefectRuntime(store), rule=rule, profile=profile,
            discoverer=CppUnreachableDiscoverer(),
            backend=FrozenSourceProgramQuery(snapshot, head),
            evaluator=CppUnreachableEvaluator(), executor=EvidenceReviewExecutor(),
            owner_id="fixture-worker", working_dir=str(root),
        ).run(snapshot, base_sources=base, head_sources=head)
        self.assertEqual(result.candidates[0].status, "inconclusive")
        self.assertTrue(result.candidates[0].decision.blockers)

    def test_discoverer_accepts_a_new_source_file(self):
        head = {"new.cpp": "int f() {\n return 1;\n use();\n}\n"}
        rule = cpp_unreachable_rule()
        policy = digest({"operations": ["read_source"]})
        profile = cpp_unreachable_profile(policy)
        snapshot = FixedSnapshot(
            "fixture", "new-file", FrozenSourceProgramQuery.source_digest(head),
            analysis_profile_digest(rule, profile=profile), policy,
        )
        found = CppUnreachableDiscoverer().discover(
            DiscoveryInput(snapshot, {}, head),
        )
        self.assertEqual(len(found), 1)
        self.assertEqual(found[0].material["path"], "new.cpp")

    def test_composite_backend_rejects_operation_collisions(self):
        with self.assertRaises(InvalidInput):
            CompositeProgramQuery(SemanticBackend(), SemanticBackend())


if __name__ == "__main__":
    unittest.main()
