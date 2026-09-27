"""Contract tests for the first runnable defect-analysis slice."""

import tempfile
import unittest
import uuid
from dataclasses import replace
from pathlib import Path

from agent_runtime import (
    AgentCapabilities,
    AnalysisSnapshot,
    Assessment,
    CandidateKey,
    CheckItem,
    Claim,
    Conflict,
    ContextBudgetExceeded,
    ContextViewBuilder,
    Coverage,
    DefectRuntime,
    EvidenceIntegrityError,
    EvidenceRef,
    FactAssessment,
    Handoff,
    InvalidInput,
    ProgramResult,
    QueryRequest,
    RoleCoordinator,
    RoleRunResult,
    SessionService,
    SourceLocation,
    SQLiteStore,
    null_return_rule,
)
from agent_runtime.codec import bytes_digest, digest
from agent_runtime.runtime import CHECK_NAMES


class FakeBackend:
    backend_id = "fixture"
    backend_version = "1"
    supported_operations = frozenset({"check_predicate"})
    idempotent_retry = True
    read_only = True

    def __init__(self, partial=False):
        self.partial = partial
        self.calls = 0

    def query(self, request):
        self.calls += 1
        raw = (request.check_id + ":" + str(request.scope)).encode()
        if self.partial:
            return ProgramResult(
                "partial",
                Coverage(
                    "all callers",
                    "direct callers",
                    "partial",
                    ("indirect calls unresolved",),
                    (bytes_digest(raw),),
                ),
                raw,
            )
        return ProgramResult(
            "complete",
            Coverage(
                "declared candidate path",
                "declared candidate path",
                "complete",
                (),
                (bytes_digest(raw),),
            ),
            raw,
        )


class RuntimeContracts(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        root = Path(self.temporary.name)
        self.store = SQLiteStore(root / "state.sqlite3", root / "artifacts")
        self.runtime = DefectRuntime(self.store)
        self.rule = null_return_rule()
        self.snapshot = AnalysisSnapshot(
            "example",
            "base-commit",
            "head-commit",
            "a" * 64,
            "b" * 64,
            "debug",
            "c" * 64,
            "c" * 64,
            digest(self.rule),
            "d" * 64,
        )
        self.analysis_id = self.runtime.create_analysis(self.snapshot, self.rule)
        self.backend = FakeBackend()

    def tearDown(self):
        self.store.close()
        self.temporary.cleanup()

    def candidate(self, function="process"):
        line = 10 if function == "process" else 20
        return CandidateKey(
            self.rule.rule_id,
            self.rule.rule_version,
            SourceLocation("head", "head-commit", "buffer.cpp", 3, 1, 3, 20),
            SourceLocation("head", "head-commit", "consumer.cpp", line, 1, line, 20),
            "debug",
            "getBuffer-return",
            f"main->{function}(0)",
        )

    def request(self, task_id, check_id, key=None):
        return QueryRequest(
            str(uuid.uuid4()),
            task_id,
            check_id,
            "check_predicate",
            self.snapshot.snapshot_digest,
            "fixture",
            "1",
            self.snapshot.tool_policy_digest,
            {
                "repository_paths": ["consumer.cpp"],
                "symbols": [],
                "build_variant_id": "debug",
                "max_results": 100,
                "include_indirect": False,
            },
            {"predicate": check_id},
            key or str(uuid.uuid4()),
            1000,
        )

    def populate(self, task_id, polarities, scope):
        refs = []
        facts = []
        for name in CHECK_NAMES:
            outcome, ref = self.runtime.query_program(
                self.request(task_id, name), self.backend
            )
            fact = FactAssessment(
                str(uuid.uuid4()),
                task_id,
                name,
                name,
                polarities[name],
                (ref,),
                scope,
                "tool_observed",
            )
            self.runtime.record_fact(fact)
            self.runtime.update_check(
                CheckItem(
                    name,
                    task_id,
                    name,
                    "complete",
                    "refuted" if polarities[name] == "negative" else "supported",
                    (ref,),
                    outcome.coverage,
                )
            )
            refs.append(ref)
            facts.append(fact)
        return tuple(refs), tuple(facts)

    def roles(self, task_id, refs, facts, scope):
        service = SessionService(self.runtime)
        inv, inv_lease = service.start_attempt(
            task_id, "investigator", working_dir=self.temporary.name, owner_id="worker"
        )
        claim = Claim(
            str(uuid.uuid4()),
            task_id,
            inv.attempt_id,
            tuple(fact.fact_id for fact in facts),
            refs,
            (),
            (),
            scope,
        )
        self.runtime.record_claim(claim)
        service.end_attempt(
            inv.attempt_id,
            owner_id="worker",
            lease_epoch=inv_lease.lease_epoch,
            status="completed",
            reason="claim saved",
        )
        ver, ver_lease = service.start_attempt(
            task_id, "verifier", working_dir=self.temporary.name, owner_id="worker"
        )
        assessment = Assessment(
            str(uuid.uuid4()),
            task_id,
            ver.attempt_id,
            claim.claim_id,
            CHECK_NAMES,
            refs,
            (),
            (),
            "inspect facts",
            scope,
        )
        self.runtime.record_assessment(assessment)
        service.end_attempt(
            ver.attempt_id,
            owner_id="worker",
            lease_epoch=ver_lease.lease_epoch,
            status="completed",
            reason="assessment saved",
        )
        return claim, assessment

    def test_confirmed_is_scoped_and_replayable(self):
        key = self.candidate()
        task = self.runtime.propose_candidate(
            self.analysis_id, key, discovery_ref="fixture"
        )
        polarities = {
            name: "negative" if name == "guard" else "positive" for name in CHECK_NAMES
        }
        refs, facts = self.populate(task.task_id, polarities, key.path_identity)
        claim, assessment = self.roles(task.task_id, refs, facts, key.path_identity)
        decision = self.runtime.evaluate_candidate(
            task.task_id, claim.claim_id, assessment.assessment_id
        )
        self.assertEqual(decision.verdict, "confirmed")
        self.assertEqual(decision.scope, key.path_identity)
        self.assertIsNone(self.runtime.find_exclusion(task.task_id))
        report = self.runtime.get_report(self.analysis_id)
        self.assertTrue(report["unresolved_scope"])
        self.assertEqual(
            report["candidate_reports"][0]["decision"]["scope"], key.path_identity
        )
        self.assertTrue(report["candidate_reports"][0]["decision"]["support_refs"])
        before = self.runtime.task(task.task_id)
        self.store.rebuild_projections(self.analysis_id)
        self.assertEqual(before, self.runtime.task(task.task_id))
        self.assertEqual(
            self.runtime.propose_candidate(
                self.analysis_id, key, discovery_ref="replayed"
            ).task_id,
            task.task_id,
        )

    def test_guarded_path_refuted_and_exact_exclusion(self):
        key = self.candidate("guardedProcess")
        task = self.runtime.propose_candidate(
            self.analysis_id, key, discovery_ref="fixture"
        )
        polarities = {name: "positive" for name in CHECK_NAMES}
        polarities["reachability"] = "negative"
        refs, facts = self.populate(task.task_id, polarities, key.path_identity)
        claim, assessment = self.roles(task.task_id, refs, facts, key.path_identity)
        decision = self.runtime.evaluate_candidate(
            task.task_id, claim.claim_id, assessment.assessment_id
        )
        self.assertEqual(decision.verdict, "refuted")
        exclusion = self.runtime.find_exclusion(task.task_id)
        self.assertEqual(exclusion.scope, key.path_identity)
        view = ContextViewBuilder(self.runtime).build(
            task.task_id,
            "investigator",
            focus_check_ids=("reachability", "guard"),
            token_budget=20_000,
        )
        protected = {item["id"]: item for item in view.protected_items}
        self.assertIn(f"exclusion:{exclusion.exclusion_id}", protected)
        self.assertTrue(
            protected[f"exclusion:{exclusion.exclusion_id}"]["value"]["counter_refs"]
        )

        class ExcludedCandidateMustNotRun:
            def describe_capabilities(self):
                return AgentCapabilities(True, True, True, True, True, True, True, True)

            def run(self, request, tools):
                raise AssertionError("valid exclusion must bypass the model")

        replay = RoleCoordinator(
            self.runtime,
            ExcludedCandidateMustNotRun(),
            self.backend,
            owner_id="replay",
            working_dir=self.temporary.name,
        ).run_candidate(task.task_id)
        self.assertEqual(replay.verdict, "refuted")
        self.assertEqual(replay.evaluator_id, "exclusion-index")
        duplicate = self.runtime.propose_candidate(
            self.analysis_id, key, discovery_ref="again"
        )
        self.assertEqual(duplicate.task_id, task.task_id)
        other = self.runtime.propose_candidate(
            self.analysis_id, self.candidate(), discovery_ref="other path"
        )
        self.assertIsNone(self.runtime.find_exclusion(other.task_id))
        self.assertTrue(
            all(
                check["status"] == "unexamined"
                for check in self.runtime.checks(other.task_id)
            )
        )

    def test_partial_result_cannot_complete_check(self):
        task = self.runtime.propose_candidate(
            self.analysis_id, self.candidate(), discovery_ref="fixture"
        )
        backend = FakeBackend(partial=True)
        outcome, ref = self.runtime.query_program(
            self.request(task.task_id, "reachability"), backend
        )
        self.assertEqual(outcome.status, "partial")
        with self.assertRaises(InvalidInput):
            self.runtime.update_check(
                CheckItem(
                    "reachability",
                    task.task_id,
                    "reachability",
                    "complete",
                    "refuted",
                    (ref,),
                    Coverage("path", "path", "complete", (), (ref.artifact_digest,)),
                )
            )

    def test_different_build_config_blocks_head_attribution(self):
        self.snapshot = replace(self.snapshot, head_build_digest="e" * 64)
        self.analysis_id = self.runtime.create_analysis(self.snapshot, self.rule)
        key = self.candidate()
        task = self.runtime.propose_candidate(
            self.analysis_id, key, discovery_ref="F-05 fixture"
        )
        polarities = {
            name: "negative" if name == "guard" else "positive" for name in CHECK_NAMES
        }
        refs, facts = self.populate(task.task_id, polarities, key.path_identity)
        claim, assessment = self.roles(task.task_id, refs, facts, key.path_identity)
        decision = self.runtime.evaluate_candidate(
            task.task_id, claim.claim_id, assessment.assessment_id
        )
        self.assertEqual(decision.verdict, "inconclusive")
        self.assertIn("Base/Head build configurations differ", decision.blockers)

    def test_timeout_preserves_raw_without_negative_evidence(self):
        class TimeoutBackend(FakeBackend):
            def query(self, request):
                return ProgramResult(
                    "timeout",
                    Coverage("candidate path", "partial graph", "unknown"),
                    b"partial graph before timeout",
                    ("query deadline exceeded",),
                    "deadline exceeded",
                )

        task = self.runtime.propose_candidate(
            self.analysis_id, self.candidate(), discovery_ref="F-07 fixture"
        )
        outcome, ref = self.runtime.query_program(
            self.request(task.task_id, "reachability"), TimeoutBackend()
        )
        self.assertEqual(outcome.status, "timeout")
        self.assertIsNone(ref)
        self.assertEqual(
            self.store.read_artifact(outcome.raw_artifact_digest),
            b"partial graph before timeout",
        )
        self.assertEqual(self.runtime.checks(task.task_id)[4]["status"], "unexamined")

    def test_forged_evidence_rejected(self):
        task = self.runtime.propose_candidate(
            self.analysis_id, self.candidate(), discovery_ref="fixture"
        )
        forged = EvidenceRef(
            "invented", "a" * 64, self.snapshot.snapshot_digest, "support"
        )
        with self.assertRaises(EvidenceIntegrityError):
            self.runtime.record_fact(
                FactAssessment(
                    str(uuid.uuid4()),
                    task.task_id,
                    "guard",
                    "guard",
                    "positive",
                    (forged,),
                    self.candidate().path_identity,
                    "tool_observed",
                )
            )

    def test_event_projection_tampering_blocks_rebuild(self):
        task = self.runtime.propose_candidate(
            self.analysis_id, self.candidate(), discovery_ref="fixture"
        )
        self.store.connection.execute(
            "UPDATE events SET projection_json=? WHERE analysis_id=? AND event_type='TaskCreated'",
            (
                '{"schema_version":{"major":1,"minor":0},"data":{"finding_state":"confirmed"}}',
                self.analysis_id,
            ),
        )
        with self.assertRaises(EvidenceIntegrityError):
            self.store.rebuild_projections(self.analysis_id)
        self.assertEqual(self.runtime.task(task.task_id)["finding_state"], "unassessed")

    def test_provisional_candidate_cannot_be_excluded_until_bound(self):
        key = self.candidate()
        proposal = self.runtime.propose_provisional(
            self.analysis_id,
            source_location=key.source_location,
            sink_location=None,
            discovery_ref="call graph hint",
            unresolved_identity=("callee alias", "input path"),
        )
        self.assertEqual(
            self.runtime.get_report(self.analysis_id)["discovered_denominator"], 0
        )
        self.assertEqual(self.store.list("exclusion", self.analysis_id), [])
        task = self.runtime.bind_provisional(proposal.proposal_id, key)
        self.assertEqual(
            self.runtime.task(task.task_id)["candidate_digest"], key.candidate_digest
        )
        self.assertEqual(
            self.runtime.get_report(self.analysis_id)["discovered_denominator"], 1
        )

    def test_in_doubt_retry_is_idempotent(self):
        task = self.runtime.propose_candidate(
            self.analysis_id, self.candidate(), discovery_ref="fixture"
        )
        request = self.request(task.task_id, "nullable_source", key="stable-key")
        self.store.reserve_query(
            self.analysis_id,
            key=request.idempotency_key,
            request_digest=request.request_digest,
            query_id=request.query_id,
            request_record={
                "check_id": request.check_id,
                "request": request.__dict__,
                "request_digest": request.request_digest,
                "status": "requested",
                "outcome": None,
                "evidence_id": None,
            },
            task_id=task.task_id,
        )
        orphan = self.store.put_artifact(b"orphan")
        self.assertEqual(
            self.runtime.recover_in_doubt(self.analysis_id), [request.query_id]
        )
        first, ref = self.runtime.query_program(request, self.backend)
        second, same = self.runtime.query_program(request, self.backend)
        self.assertEqual(first, second)
        self.assertEqual(ref.evidence_id, same.evidence_id)
        self.assertEqual(self.backend.calls, 1)
        self.assertEqual(self.store.read_artifact(orphan), b"orphan")

    def test_pending_query_waits_for_writer_fencing(self):
        task = self.runtime.propose_candidate(
            self.analysis_id, self.candidate(), discovery_ref="fixture"
        )
        session = SessionService(self.runtime)
        binding, lease = session.start_attempt(
            task.task_id,
            "investigator",
            working_dir=self.temporary.name,
            owner_id="worker",
        )
        request = self.request(task.task_id, "nullable_source")
        self.store.reserve_query(
            self.analysis_id,
            key=request.idempotency_key,
            request_digest=request.request_digest,
            query_id=request.query_id,
            request_record={
                "check_id": request.check_id,
                "request": request.__dict__,
                "request_digest": request.request_digest,
                "status": "requested",
                "outcome": None,
                "evidence_id": None,
            },
            task_id=task.task_id,
        )
        with self.assertRaises(Conflict):
            self.runtime.recover_in_doubt(self.analysis_id)
        session.end_attempt(
            binding.attempt_id,
            owner_id="worker",
            lease_epoch=lease.lease_epoch,
            status="interrupted",
            reason="fixture crash",
        )
        self.assertEqual(
            self.runtime.recover_in_doubt(self.analysis_id), [request.query_id]
        )

    def test_lease_handoff_and_context_budget(self):
        task = self.runtime.propose_candidate(
            self.analysis_id, self.candidate(), discovery_ref="fixture"
        )
        service = SessionService(self.runtime)
        binding, lease = service.start_attempt(
            task.task_id,
            "investigator",
            working_dir=self.temporary.name,
            owner_id="one",
            sdk_session_id="sdk-one",
        )
        with self.assertRaises(Conflict):
            service.start_attempt(
                task.task_id,
                "investigator",
                working_dir=self.temporary.name,
                owner_id="two",
                sdk_session_id="sdk-one",
            )
        protected = tuple(sorted(service.protected_items(task.task_id)))
        handoff = Handoff(
            str(uuid.uuid4()),
            task.task_id,
            binding.attempt_id,
            task.snapshot_digest,
            task.candidate_digest,
            self.store.high_watermark(self.analysis_id),
            protected,
        )
        service.checkpoint_handoff(handoff)
        with self.assertRaises(InvalidInput):
            service.checkpoint_handoff(
                Handoff(
                    str(uuid.uuid4()),
                    task.task_id,
                    binding.attempt_id,
                    task.snapshot_digest,
                    task.candidate_digest,
                    self.store.high_watermark(self.analysis_id),
                    (),
                )
            )
        with self.assertRaises(ContextBudgetExceeded):
            ContextViewBuilder(self.runtime, service).build(
                task.task_id, "investigator", focus_check_ids=("guard",), token_budget=1
            )
        service.end_attempt(
            binding.attempt_id,
            owner_id="one",
            lease_epoch=lease.lease_epoch,
            status="paused",
            reason="handoff",
        )
        with self.assertRaises(Conflict):
            service.assert_lease(binding.binding_id, "one", lease.lease_epoch)
        with self.assertRaises(Conflict):
            service.start_attempt(
                task.task_id,
                "verifier",
                working_dir=self.temporary.name,
                owner_id="two",
                sdk_session_id="sdk-one",
            )
        resumed, _ = service.start_attempt(
            task.task_id,
            "investigator",
            working_dir=self.temporary.name,
            owner_id="two",
            sdk_session_id="sdk-one",
            predecessor_attempt_id=binding.attempt_id,
        )
        self.assertEqual(resumed.sdk_session_id, "sdk-one")

    def test_expired_writer_is_fenced_before_resume(self):
        task = self.runtime.propose_candidate(
            self.analysis_id, self.candidate(), discovery_ref="fixture"
        )
        service = SessionService(self.runtime)
        old, lease = service.start_attempt(
            task.task_id,
            "investigator",
            working_dir=self.temporary.name,
            owner_id="old",
            sdk_session_id="sdk-old",
        )
        self.store.connection.execute(
            "UPDATE leases SET expires_at=0 WHERE binding_id=?", (old.binding_id,)
        )
        service.recover_expired_lease(old.binding_id)
        with self.assertRaises(Conflict):
            service.assert_lease(old.binding_id, "old", lease.lease_epoch)
        new, _ = service.start_attempt(
            task.task_id,
            "investigator",
            working_dir=self.temporary.name,
            owner_id="new",
            sdk_session_id="sdk-old",
            predecessor_attempt_id=old.attempt_id,
        )
        self.assertNotEqual(new.attempt_id, old.attempt_id)

    def test_rotation_starts_fresh_sdk_history(self):
        task = self.runtime.propose_candidate(
            self.analysis_id, self.candidate(), discovery_ref="fixture"
        )
        service = SessionService(self.runtime)
        old, lease = service.start_attempt(
            task.task_id,
            "investigator",
            working_dir=self.temporary.name,
            owner_id="old",
            sdk_session_id="sdk-old",
        )
        handoff = Handoff(
            str(uuid.uuid4()),
            task.task_id,
            old.attempt_id,
            task.snapshot_digest,
            task.candidate_digest,
            self.store.high_watermark(self.analysis_id),
            tuple(sorted(service.protected_items(task.task_id))),
        )
        new, new_lease = service.rotate(
            handoff,
            owner_id="old",
            lease_epoch=lease.lease_epoch,
            next_owner_id="new",
            working_dir=self.temporary.name,
        )
        self.assertIsNone(new.sdk_session_id)
        self.assertNotEqual(new.attempt_id, old.attempt_id)
        service.bind_sdk_session(
            new.binding_id, "sdk-new", owner_id="new", lease_epoch=new_lease.lease_epoch
        )
        self.assertEqual(
            service.latest_handoff(task.task_id).handoff_id, handoff.handoff_id
        )

    def test_coordinator_uses_distinct_role_attempts(self):
        runtime = self.runtime
        task = runtime.propose_candidate(
            self.analysis_id, self.candidate(), discovery_ref="fixture"
        )
        test = self

        class DeterministicAgent:
            def __init__(self):
                self.seen_sessions = []

            def describe_capabilities(self):
                return AgentCapabilities(True, True, True, True, True, True, True, True)

            def run(self, request, tools):
                self.seen_sessions.append(request.role)
                if request.role == "investigator":
                    refs = []
                    fact_ids = []
                    for name in CHECK_NAMES:
                        outcome, ref = tools.query(test.request(task.task_id, name))
                        fact = FactAssessment(
                            str(uuid.uuid4()),
                            task.task_id,
                            name,
                            name,
                            "negative" if name == "guard" else "positive",
                            (ref,),
                            test.candidate().path_identity,
                            "tool_observed",
                        )
                        runtime.record_fact(fact)
                        runtime.update_check(
                            CheckItem(
                                name,
                                task.task_id,
                                name,
                                "complete",
                                "refuted" if name == "guard" else "supported",
                                (ref,),
                                outcome.coverage,
                            )
                        )
                        refs.append(ref)
                        fact_ids.append(fact.fact_id)
                    return RoleRunResult(
                        Claim(
                            str(uuid.uuid4()),
                            task.task_id,
                            request.attempt_id,
                            tuple(fact_ids),
                            tuple(refs),
                            (),
                            (),
                            test.candidate().path_identity,
                        ),
                        "sdk-investigator",
                    )
                return RoleRunResult(
                    Assessment(
                        str(uuid.uuid4()),
                        task.task_id,
                        request.attempt_id,
                        request.claim.claim_id,
                        CHECK_NAMES,
                        request.claim.support_refs,
                        (),
                        (),
                        "confirm",
                        test.candidate().path_identity,
                    ),
                    "sdk-verifier",
                )

        agent = DeterministicAgent()
        coordinator = RoleCoordinator(
            runtime,
            agent,
            self.backend,
            owner_id="worker",
            working_dir=self.temporary.name,
            token_budget=20_000,
        )
        decision = coordinator.run_candidate(task.task_id)
        self.assertEqual(decision.verdict, "confirmed")
        self.assertEqual(agent.seen_sessions, ["investigator", "verifier"])


if __name__ == "__main__":
    unittest.main()
