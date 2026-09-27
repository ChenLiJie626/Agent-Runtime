"""Focused Route B portfolio, provenance, planning and CodeQL contracts."""

from __future__ import annotations

import json
import tempfile
import unittest
import uuid
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

from agent_runtime import (
    AnalysisSnapshot,
    CandidateIdentity,
    CandidateKey,
    DefectRuntime,
    EvidencePlan,
    FixedSnapshot,
    QueryRequest,
    SQLiteStore,
    SourceLocation,
    bytes_digest,
    digest,
    null_return_rule,
)
from agent_runtime.errors import Conflict, InvalidInput
from agent_runtime.adapters.codeql import (
    CodeQLCaptureBundle,
    CodeQLReplayConfig,
    CodeQLReplayProgramQuery,
)
from agent_runtime.discovery import PortfolioDiscoverer
from agent_runtime.domain import DiscoveredCandidate, DiscoveryInput
from agent_runtime.ports import EvidencePlanSelection


class _Discoverer:
    def __init__(self, identifier, candidate, version="1"):
        self.discoverer_id = identifier
        self.discoverer_version = version
        self.candidate = candidate

    def discover(self, input):
        return (self.candidate,)


class PortfolioAndProvenanceTests(unittest.TestCase):
    def setUp(self):
        self.sources = {"a.cpp": "int f() { return 0; }\n"}
        self.snapshot = FixedSnapshot(
            "fixture", "scope", digest(self.sources), digest("profile"), digest("policy"),
        )
        self.identity = CandidateIdentity(
            "fixture.rule", "1", self.snapshot.snapshot_digest,
            {"path": "a.cpp", "line": 1}, "a.cpp:1",
        )

    def candidate(self, candidate_id, check, operation="read_source"):
        return DiscoveredCandidate(
            candidate_id, self.identity, f"fixture:{candidate_id}",
            (EvidencePlan(check, operation, {"id": candidate_id}, {"id": candidate_id}, check, "positive"),),
            {"candidate": candidate_id},
        )

    def test_portfolio_fuses_only_exact_identity_and_retains_origins(self):
        first = self.candidate("left", "source")
        second = self.candidate("right", "semantic", "semantic_check")
        portfolio = PortfolioDiscoverer(_Discoverer("z", first), _Discoverer("a", second))
        result = portfolio.discover(DiscoveryInput(self.snapshot, {}, self.sources))
        self.assertEqual(len(result), 1)
        self.assertEqual([plan.check_id for plan in result[0].evidence_plans], ["semantic", "source"])
        self.assertEqual([origin.discoverer_id for origin in result[0].origins], ["a", "z"])
        self.assertIn("_portfolio", result[0].material)
        with self.assertRaises(Exception):
            PortfolioDiscoverer(
                _Discoverer("same", first), _Discoverer("same", second),
            )

    def test_portfolio_order_duplicate_ids_and_plan_conflicts_are_deterministic(self):
        first = self.candidate("left", "source")
        second = self.candidate("right", "semantic", "semantic_check")
        forward = PortfolioDiscoverer(
            _Discoverer("z", first), _Discoverer("a", second)
        ).discover(DiscoveryInput(self.snapshot, {}, self.sources))
        reverse = PortfolioDiscoverer(
            _Discoverer("a", second), _Discoverer("z", first)
        ).discover(DiscoveryInput(self.snapshot, {}, self.sources))
        self.assertEqual(forward, reverse)

        class DuplicateChild(_Discoverer):
            def discover(self, input):
                return (self.candidate, self.candidate)

        with self.assertRaises(InvalidInput):
            PortfolioDiscoverer(DuplicateChild("dup", first)).discover(
                DiscoveryInput(self.snapshot, {}, self.sources)
            )
        conflicting = self.candidate("right", "source")
        with self.assertRaises(InvalidInput):
            PortfolioDiscoverer(
                _Discoverer("a", first), _Discoverer("z", conflicting)
            ).discover(DiscoveryInput(self.snapshot, {}, self.sources))

    def test_duplicate_candidate_appends_durable_provenance(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            store = SQLiteStore(root / "state.sqlite", root / "artifacts")
            self.addCleanup(store.close)
            runtime = DefectRuntime(store)
            rule = null_return_rule()
            snapshot = AnalysisSnapshot(
                "fixture", "base", "head", "a" * 64, "b" * 64, "debug",
                "c" * 64, "c" * 64, digest(rule), "d" * 64,
            )
            analysis_id = runtime.create_analysis(snapshot, rule)
            key = CandidateKey(
                rule.rule_id, rule.rule_version,
                SourceLocation("head", "head", "a.cpp", 1, 1, 1, 1),
                SourceLocation("head", "head", "a.cpp", 2, 1, 2, 1),
                "debug", "object", "path",
            )
            first = runtime.propose_candidate(analysis_id, key, discovery_ref="first")
            second = runtime.propose_candidate(analysis_id, key, discovery_ref="second")
            self.assertEqual(first.task_id, second.task_id)
            self.assertEqual(len(store.list("candidate_origin", analysis_id)), 2)
            store.rebuild_projections(analysis_id)
            report = runtime.get_report(analysis_id)
            self.assertEqual(report["supplemental_metrics"]["status"], "non_gating")
            self.assertEqual(report["supplemental_metrics"]["candidate_provenance"]["origin_count"], 2)

    def test_provenance_is_analysis_scoped_and_survives_reopen(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            database = root / "state.sqlite"
            artifacts = root / "artifacts"
            store = SQLiteStore(database, artifacts)
            runtime = DefectRuntime(store)
            rule = null_return_rule()
            snapshot = AnalysisSnapshot(
                "fixture", "base", "head", "a" * 64, "b" * 64, "debug",
                "c" * 64, "c" * 64, digest(rule), "d" * 64,
            )
            first_analysis = runtime.create_analysis(snapshot, rule)
            second_analysis = runtime.create_analysis(snapshot, rule)
            key = CandidateKey(
                rule.rule_id, rule.rule_version,
                SourceLocation("head", "head", "a.cpp", 1, 1, 1, 1),
                SourceLocation("head", "head", "a.cpp", 2, 1, 2, 1),
                "debug", "object", "path",
            )
            runtime.propose_candidate(first_analysis, key, discovery_ref="same")
            runtime.propose_candidate(second_analysis, key, discovery_ref="same")
            self.assertEqual(len(store.list("candidate_origin", first_analysis)), 1)
            self.assertEqual(len(store.list("candidate_origin", second_analysis)), 1)
            store.rebuild_projections(first_analysis)
            store.close()

            reopened = SQLiteStore(database, artifacts)
            try:
                self.assertEqual(
                    reopened.list("candidate_origin", first_analysis)[0]["analysis_id"],
                    first_analysis,
                )
                self.assertEqual(
                    reopened.list("candidate_origin", second_analysis)[0]["analysis_id"],
                    second_analysis,
                )
            finally:
                reopened.close()

    def test_selection_is_persisted_before_any_query(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            store = SQLiteStore(root / "state.sqlite", root / "artifacts")
            self.addCleanup(store.close)
            runtime = DefectRuntime(store)
            rule = null_return_rule()
            snapshot = AnalysisSnapshot(
                "fixture", "base", "head", "a" * 64, "b" * 64, "debug",
                "c" * 64, "c" * 64, digest(rule), "d" * 64,
            )
            analysis_id = runtime.create_analysis(snapshot, rule)
            key = CandidateKey(
                rule.rule_id, rule.rule_version,
                SourceLocation("head", "head", "a.cpp", 1, 1, 1, 1),
                SourceLocation("head", "head", "a.cpp", 2, 1, 2, 1),
                "debug", "object", "path",
            )
            task = runtime.propose_candidate(analysis_id, key, discovery_ref="fixture")
            selection = EvidencePlanSelection("e" * 64, "test selection")
            runtime.record_plan_selection(
                task.task_id, selection=selection, planner_id="fixture", planner_version="1",
                check_id="nullable_source", operation="fixture", query_idempotency_key="stable-key",
            )
            events = store.events(analysis_id)
            self.assertEqual(events[-1]["event_type"], "EvidencePlanSelected")
            self.assertEqual(store.list("query", analysis_id), [])
            runtime.record_plan_selection(
                task.task_id, selection=selection, planner_id="fixture", planner_version="1",
                check_id="nullable_source", operation="fixture", query_idempotency_key="stable-key",
            )
            with self.assertRaises(Conflict):
                runtime.record_plan_selection(
                    task.task_id,
                    selection=EvidencePlanSelection("e" * 64, "tampered reason"),
                    planner_id="fixture", planner_version="1",
                    check_id="nullable_source", operation="fixture",
                    query_idempotency_key="stable-key",
                )


class AdaptivePipelineTests(unittest.TestCase):
    def test_planner_stop_preserves_unexamined_checks(self):
        from agent_runtime import (
            AnalysisPipeline, CppUnreachableDiscoverer, CppUnreachableEvaluator,
            EvidenceReviewExecutor, analysis_profile_digest, cpp_unreachable_profile,
            cpp_unreachable_rule,
        )
        from agent_runtime.adapters.source import FrozenSourceProgramQuery

        class StopPlanner:
            planner_id = "fixture.stop"
            planner_version = "1"

            def select_next(self, **kwargs):
                return None

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            base = {"a.cpp": "int f() {\n return 0;\n}\n"}
            head = {"a.cpp": "int f() {\n return 1;\n use();\n}\n"}
            rule = cpp_unreachable_rule()
            policy = digest({"operations": ["read_source"]})
            profile = cpp_unreachable_profile(policy)
            snapshot = FixedSnapshot(
                "fixture", "scope", FrozenSourceProgramQuery.source_digest(head),
                analysis_profile_digest(rule, profile=profile), policy,
            )
            store = SQLiteStore(root / "state.sqlite", root / "artifacts")
            self.addCleanup(store.close)
            runtime = DefectRuntime(store)
            result = AnalysisPipeline(
                runtime, rule=rule, profile=profile,
                discoverer=CppUnreachableDiscoverer(),
                backend=FrozenSourceProgramQuery(snapshot, head),
                evaluator=CppUnreachableEvaluator(), executor=EvidenceReviewExecutor(),
                owner_id="fixture", working_dir=str(root), evidence_planner=StopPlanner(),
            ).run(snapshot, base_sources=base, head_sources=head)
            self.assertEqual(result.candidates[0].status, "inconclusive")
            self.assertEqual(store.list("query", result.analysis_id), [])
            self.assertEqual(
                [item["status"] for item in runtime.checks(result.candidates[0].task_id)],
                ["unexamined", "unexamined"],
            )
            self.assertIn(
                "EvidencePlanningStopped",
                [event["event_type"] for event in store.events(result.analysis_id)],
            )


class CodeQLReplayTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.cli = self.root / "codeql"
        self.cli.write_bytes(b"fixture-cli")
        self.cli.chmod(0o700)
        self.database = self.root / "database"
        self.database.mkdir()
        (self.database / "db.zip").write_bytes(b"database")
        self.sarif_result = {
            "ruleId": "cpp/lifetime-candidate",
            "message": {"text": "candidate"},
            "locations": [{"physicalLocation": {"artifactLocation": {"uri": "src/a.cpp"},
                "region": {"startLine": 3, "startColumn": 2}}}],
            "partialFingerprints": {"primaryLocationLineHash": "abc"},
        }
        normalized = CodeQLReplayProgramQuery._normalize_result(self.sarif_result)
        self.result_id = normalized["result_id"]
        self.snapshot = FixedSnapshot(
            "fixture", "scope", "a" * 64, digest("profile"), digest("policy"),
            {"compile_db_digest": "b" * 64},
        )
        manifest = {"db.zip": bytes_digest(b"database")}
        self.bundle = CodeQLCaptureBundle(
            self.snapshot.snapshot_digest, self.snapshot.source_digest, "b" * 64,
            str(self.database), manifest, digest(manifest), (self.result_id,),
        )
        self.config = CodeQLReplayConfig(str(self.cli), bytes_digest(b"fixture-cli"), "fixture-release")
        self.backend = CodeQLReplayProgramQuery(self.snapshot, self.bundle, self.config)

    def request(self, result_id=None):
        return QueryRequest(
            str(uuid.uuid4()), "task", "signal", "replay_codeql_result_v1",
            self.snapshot.snapshot_digest, self.backend.backend_id, self.backend.backend_version,
            self.snapshot.tool_policy_digest,
            {"scope_id": "scope", "selectors": {"result_id": result_id or self.result_id}},
            {"result_id": result_id or self.result_id}, str(uuid.uuid4()), 1_000,
        )

    def test_replay_uses_fixed_argv_and_normalizes_sarif(self):
        calls = []
        sarif = {"runs": [{"results": [self.sarif_result]}]}

        class Process:
            returncode = 0
            def __init__(self, argv, **kwargs):
                calls.append((argv, kwargs))
                Path(next(item.split("=", 1)[1] for item in argv if item.startswith("--output="))).write_text(
                    __import__("json").dumps(sarif), encoding="utf-8",
                )
            def poll(self): return 0
            def communicate(self, timeout=None): return b"", b""

        with patch("agent_runtime.adapters.codeql.subprocess.Popen", Process):
            result = self.backend.query(self.request())
        self.assertEqual(result.status, "complete")
        self.assertIn(self.result_id.encode(), result.raw)
        argv, kwargs = calls[0]
        self.assertEqual(argv[:3], [str(self.cli.resolve()), "database", "analyze"])
        self.assertNotEqual(Path(argv[3]), self.database.resolve())
        self.assertEqual(Path(argv[3]).name, "database")
        self.assertEqual(
            argv[4],
            str(Path(__file__).parents[1] / "src/agent_runtime/adapters/codeql_pack/PotentialAccessAfterDelete.ql"),
        )
        self.assertFalse(kwargs["shell"])
        self.assertNotIn("PYTHONPATH", kwargs["env"])

    def _query_with_immediate_process(self, sarif, *, returncode=0, stderr=b""):
        class Process:
            def __init__(process_self, argv, **kwargs):
                process_self.returncode = returncode
                if sarif is not None:
                    output = Path(next(
                        item.split("=", 1)[1] for item in argv
                        if item.startswith("--output=")
                    ))
                    if isinstance(sarif, bytes):
                        output.write_bytes(sarif)
                    else:
                        output.write_text(json.dumps(sarif), encoding="utf-8")
                if stderr:
                    kwargs["stderr"].write(stderr)
                    kwargs["stderr"].flush()

            def poll(process_self):
                return process_self.returncode

        with patch("agent_runtime.adapters.codeql.subprocess.Popen", Process):
            return self.backend.query(self.request())

    def test_replay_statuses_preserve_empty_malformed_nonzero_timeout_and_oversize(self):
        empty = self._query_with_immediate_process({"runs": [{"results": []}]})
        self.assertEqual(empty.status, "partial")
        self.assertIn(b'"result":null', empty.raw)

        malformed = self._query_with_immediate_process(b"not-json")
        self.assertEqual(malformed.status, "failed")
        nonzero = self._query_with_immediate_process(None, returncode=2, stderr=b"failed")
        self.assertEqual(nonzero.status, "failed")

        small_config = CodeQLReplayConfig(
            str(self.cli), bytes_digest(b"fixture-cli"), "fixture-release",
            timeout_ms=1, max_output_bytes=64, poll_interval_ms=1,
        )
        backend = CodeQLReplayProgramQuery(self.snapshot, self.bundle, small_config)

        class NeverCompletes:
            returncode = None

            def __init__(process_self, argv, **kwargs):
                pass

            def poll(process_self):
                return process_self.returncode

            def terminate(process_self):
                process_self.returncode = -15

            def wait(process_self, timeout=None):
                return process_self.returncode

        with patch("agent_runtime.adapters.codeql.subprocess.Popen", NeverCompletes):
            self.assertEqual(backend.query(self.request()).status, "timeout")

        class WritesTooMuch(NeverCompletes):
            def __init__(process_self, argv, **kwargs):
                kwargs["stderr"].write(b"x" * 65)
                kwargs["stderr"].flush()

        with patch("agent_runtime.adapters.codeql.subprocess.Popen", WritesTooMuch):
            self.assertEqual(backend.query(self.request()).status, "failed")

    def test_malformed_and_traversal_sarif_cannot_become_evidence(self):
        malformed = {"runs": [{"results": [{"ruleId": "x"}]}]}
        self.assertEqual(self._query_with_immediate_process(malformed).status, "failed")
        traversal = json.loads(json.dumps(self.sarif_result))
        traversal["locations"][0]["physicalLocation"]["artifactLocation"]["uri"] = "../outside.cpp"
        self.assertEqual(
            self._query_with_immediate_process({"runs": [{"results": [traversal]}]}).status,
            "failed",
        )


    def test_result_id_ignores_fingerprint_changes(self):
        changed = json.loads(json.dumps(self.sarif_result))
        changed["partialFingerprints"] = {"primaryLocationLineHash": "different"}
        first = CodeQLReplayProgramQuery._normalize_result(self.sarif_result)
        second = CodeQLReplayProgramQuery._normalize_result(changed)
        self.assertEqual(first["result_id"], second["result_id"])
        self.assertNotEqual(first["fingerprints"], second["fingerprints"])

    def test_registered_sarif_uri_base_is_normalized_to_repository_path(self):
        source = self.root / "source"
        source.mkdir()
        result = json.loads(json.dumps(self.sarif_result))
        result["locations"][0]["physicalLocation"]["artifactLocation"] = {
            "uri": "src/a.cpp", "uriBaseId": "%SRCROOT%",
        }
        normalized = CodeQLReplayProgramQuery._normalize_result(
            result,
            uri_bases={"%SRCROOT%": {"uri": source.resolve().as_uri() + "/"}},
            source_root=str(source.resolve()),
            allowed_uri_base_ids=("%SRCROOT%",),
        )
        self.assertEqual(normalized["uri"], "src/a.cpp")
        with self.assertRaises(InvalidInput):
            CodeQLReplayProgramQuery._normalize_result(
                result,
                uri_bases={"%SRCROOT%": {"uri": self.root.resolve().as_uri() + "/"}},
                source_root=str(source.resolve()),
                allowed_uri_base_ids=("%SRCROOT%",),
            )

    def test_capture_and_cli_symlinks_are_rejected(self):
        database_link = self.root / "database-link"
        database_link.symlink_to(self.database, target_is_directory=True)
        with self.assertRaises(InvalidInput):
            replace(self.bundle, database_directory=str(database_link))
        cli_link = self.root / "codeql-link"
        cli_link.symlink_to(self.cli)
        with self.assertRaises(InvalidInput):
            replace(self.config, cli_path=str(cli_link))

    def test_request_cannot_inject_query_or_path(self):
        with self.assertRaises(Exception):
            self.backend.query(__import__("dataclasses").replace(
                self.request(), args={"result_id": self.result_id, "flags": "--search-path=/tmp"},
            ))
        with self.assertRaises(Exception):
            self.backend.query(__import__("dataclasses").replace(
                self.request(), scope={"scope_id": "scope", "selectors": {
                    "result_id": self.result_id, "database": "/tmp/injected",
                }},
            ))
        with self.assertRaises(Exception):
            self.backend.query(self.request("f" * 64))


if __name__ == "__main__":
    unittest.main()
