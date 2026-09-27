"""S2 contract: unrelated rules share one runtime without defect-specific fields."""

import tempfile
import unittest
import uuid
import subprocess
import sys
from pathlib import Path
from unittest.mock import patch

from agent_runtime import (
    CandidateIdentity, CandidateIdentityDraft, CheckDefinition, CheckItem,
    Claim, Assessment, ContextViewBuilder, Handoff,
    Coverage, DefectRuntime, EvidenceRef, FactAssessment, FixedSnapshot,
    CapabilityUnavailable, Conflict, ContextBudgetExceeded, InvalidInput, PolicyDenied, Profile, ProgramResult, QueryOperation, QueryRequest,
    RoleCoordinator,
    RoleDefinition, RoleRunResult, RuleDecision, RuleSpec, SessionService,
    SpecialistNote, SQLiteStore, StaleSnapshot, AgentCapabilities,
    analysis_profile_digest,
)
from agent_runtime.codec import bytes_digest, digest


class AbstractBackend:
    backend_id = "abstract"
    backend_version = "1"
    supported_operations = frozenset({"inspect"})
    idempotent_retry = True
    read_only = True
    operation_specs = {
        "inspect": QueryOperation(
            "inspect", {"label": "string"}, ("label",), ("subject",),
        )
    }

    def __init__(self, mode="complete"):
        self.mode = mode

    def query(self, request):
        if self.mode == "partial":
            return ProgramResult(
                "partial",
                Coverage("declared candidate", "direct subset", "partial",
                         ("remaining paths unresolved",)),
                b"[]",
            )
        if self.mode == "timeout":
            return ProgramResult(
                "timeout", Coverage("", "", "unknown"), b"query timed out",
                diagnostic="deadline reached",
            )
        raw = (request.check_id + ":" + request.args["label"]).encode()
        return ProgramResult(
            "complete",
            Coverage(
                "declared candidate", "declared candidate", "complete",
                (), (bytes_digest(raw),),
            ),
            raw,
        )


class EvidenceEvaluator:
    evaluator_version = "1"

    def __init__(self, evaluator_id, verdict):
        self.evaluator_id = evaluator_id
        self.verdict = verdict

    def evaluate(self, *, task, snapshot, candidate, facts, checks, claim, assessment):
        refs = tuple(facts[-1].evidence_refs)
        return RuleDecision(
            self.verdict, candidate.scope, (checks[0]["check_id"],), (),
            refs if self.verdict == "confirmed" else (),
            refs if self.verdict == "refuted" else (),
            (), self.evaluator_id, self.evaluator_version,
            digest({
                "task": task, "snapshot": snapshot, "candidate": candidate,
                "facts": facts, "checks": checks, "claim": claim,
                "assessment": assessment,
            }),
        )


class ForgedEvaluator(EvidenceEvaluator):
    def evaluate(self, **kwargs):
        decision = super().evaluate(**kwargs)
        from dataclasses import replace
        return replace(
            decision,
            support_refs=(EvidenceRef(
                "fabricated", "0" * 64,
                digest(kwargs["snapshot"]), "support",
            ),),
        )


class SubjectPathPolicy:
    def identify(self, *, snapshot_digest, rule_id, rule_version, material):
        known = {"subject": material["subject"]}
        if not material.get("path"):
            return CandidateIdentityDraft(known, material["subject"], ("path",))
        return CandidateIdentity(
            rule_id, rule_version, snapshot_digest,
            {**known, "path": material["path"]}, material["subject"],
        )


class ExtraCheckPlanner:
    planner_id = "extra-by-subject"
    planner_version = "1"

    def plan(self, rule, candidate):
        if candidate.identity["subject"] == "case-A":
            return (*rule.required_checks,
                    CheckDefinition("boundary", "boundary", ("boundary",)))
        return rule.required_checks


class ProfileAgent:
    def __init__(self, runtime, snapshot, fact, ref):
        self.runtime = runtime
        self.snapshot = snapshot
        self.fact = fact
        self.ref = ref
        self.calls = []

    def describe_capabilities(self):
        return AgentCapabilities(False, False, False, True, True, True, True, False)

    def run(self, request, tools):
        self.calls.append(request.role)
        if request.role_kind == "investigator":
            unauthorized = QueryRequest(
                str(uuid.uuid4()), request.task_id, "boundary", "inspect",
                self.snapshot.snapshot_digest, "abstract", "1",
                self.snapshot.tool_policy_digest,
                {"scope_id": self.snapshot.scope_id,
                 "selectors": {"subject": "case-A"}, "max_results": 3},
                {"label": "case-A"}, str(uuid.uuid4()), 1000,
            )
            try:
                tools.query(unauthorized)
            except PolicyDenied:
                pass
            else:
                raise AssertionError("investigator bypassed role tool policy")
            return RoleRunResult(Claim(
                str(uuid.uuid4()), request.task_id, request.attempt_id,
                (self.fact.fact_id,), (self.ref,), (), (), "case-A",
            ), f"session-{request.role}")
        if request.role_kind == "specialist":
            query = QueryRequest(
                str(uuid.uuid4()), request.task_id, "boundary", "inspect",
                self.snapshot.snapshot_digest, "abstract", "1",
                self.snapshot.tool_policy_digest,
                {"scope_id": self.snapshot.scope_id,
                 "selectors": {"subject": "case-A"}, "max_results": 3},
                {"label": "case-A"}, str(uuid.uuid4()), 1000,
            )
            outcome, ref = tools.query(query)
            self.runtime.update_check(CheckItem(
                "boundary", request.task_id, "boundary", "complete",
                "supported", (ref,), outcome.coverage,
            ))
            return RoleRunResult(SpecialistNote(
                str(uuid.uuid4()), request.task_id, request.attempt_id,
                request.role, "boundary inspected", (ref,), (), (),
            ), f"session-{request.role}")
        assert request.specialist_notes
        assert any(item["kind"] == "specialist_note"
                   for item in request.context.protected_items)
        return RoleRunResult(Assessment(
            str(uuid.uuid4()), request.task_id, request.attempt_id,
            request.claim.claim_id, ("origin", "boundary"),
            (self.ref, request.specialist_notes[0].support_refs[0]),
            (), (), "reviewed", "case-A",
        ), f"session-{request.role}")


class NeverRunAgent:
    def describe_capabilities(self):
        return AgentCapabilities(False, False, False, True, True, True, True, False)

    def run(self, request, tools):
        raise AssertionError("valid exclusion must suppress a new model session")


class FoundationContracts(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        root = Path(self.temp.name)
        self.store = SQLiteStore(root / "state.sqlite3", root / "artifacts")
        self.runtime = DefectRuntime(self.store)
        self.backend = AbstractBackend()
        self.runtime.register_evaluator(EvidenceEvaluator("alpha-eval", "confirmed"))
        self.runtime.register_evaluator(EvidenceEvaluator("beta-eval", "refuted"))

    def tearDown(self):
        self.store.close()
        self.temp.cleanup()

    def make_analysis(self, name, checks, evaluator_id):
        rule = RuleSpec(
            name, "1", name,
            tuple(CheckDefinition(check, check, (check,)) for check in checks),
            evaluator_id, "1",
        )
        snapshot = FixedSnapshot(
            "repository", f"scope-{name}", "a" * 64, digest(rule), "b" * 64,
            {"language": "any", "revision": "r1"},
        )
        return self.runtime.create_analysis(snapshot, rule), snapshot, rule

    def complete_first_check(self, task, snapshot, check_id, label):
        request = QueryRequest(
            str(uuid.uuid4()), task.task_id, check_id, "inspect",
            snapshot.snapshot_digest, "abstract", "1",
            snapshot.tool_policy_digest,
            {"scope_id": snapshot.scope_id, "selectors": {"subject": label},
             "max_results": 3},
            {"label": label}, str(uuid.uuid4()), 1000,
        )
        outcome, ref = self.runtime.query_program(request, self.backend)
        fact = FactAssessment(
            str(uuid.uuid4()), task.task_id, check_id, check_id, "positive",
            (ref,), label, "tool_observed",
        )
        self.runtime.record_fact(fact)
        self.runtime.update_check(CheckItem(
            check_id, task.task_id, check_id, "complete", "supported",
            (ref,), outcome.coverage,
        ))
        return ref, fact

    def complete_roles(self, task, ref, fact, scope):
        saved_facts = [
            item for item in self.store.list("fact", task.analysis_id)
            if item["task_id"] == task.task_id
        ]
        refs = tuple(dict.fromkeys(
            EvidenceRef(**item)
            for saved in saved_facts for item in saved["evidence_refs"]
        ))
        service = SessionService(self.runtime)
        investigator, lease = service.start_attempt(
            task.task_id, "investigator", working_dir=self.temp.name,
            owner_id="s2",
        )
        claim = Claim(
            str(uuid.uuid4()), task.task_id, investigator.attempt_id,
            tuple(item["fact_id"] for item in saved_facts), refs,
            (), (), scope,
        )
        self.runtime.record_claim(claim)
        service.end_attempt(
            investigator.attempt_id, owner_id="s2",
            lease_epoch=lease.lease_epoch, status="completed", reason="saved",
        )
        verifier, lease = service.start_attempt(
            task.task_id, "verifier", working_dir=self.temp.name, owner_id="s2",
        )
        assessment = Assessment(
            str(uuid.uuid4()), task.task_id, verifier.attempt_id,
            claim.claim_id, task.required_check_ids, refs, (), (),
            "reviewed", scope,
        )
        self.runtime.record_assessment(assessment)
        service.end_attempt(
            verifier.attempt_id, owner_id="s2",
            lease_epoch=lease.lease_epoch, status="completed", reason="saved",
        )
        return claim, assessment

    def test_two_rules_have_distinct_checks_decisions_and_scoped_exclusion(self):
        specs = (
            ("alpha", ("origin", "boundary"), "alpha-eval", "confirmed"),
            ("beta", ("resource",), "beta-eval", "refuted"),
        )
        for name, checks, evaluator_id, verdict in specs:
            with self.subTest(rule=name):
                analysis_id, snapshot, rule = self.make_analysis(
                    name, checks, evaluator_id,
                )
                identity = CandidateIdentity(
                    rule.rule_id, rule.rule_version, snapshot.snapshot_digest,
                    {"subject": f"{name}-one", "variant": 1}, f"{name}-one",
                )
                task = self.runtime.propose_candidate(
                    analysis_id, identity, discovery_ref="test",
                )
                self.assertEqual(task.required_check_ids, checks)
                self.assertEqual(
                    self.runtime.propose_candidate(
                        analysis_id, identity, discovery_ref="duplicate",
                    ).task_id,
                    task.task_id,
                )
                nearby = CandidateIdentity(
                    rule.rule_id, rule.rule_version, snapshot.snapshot_digest,
                    {"subject": f"{name}-two", "variant": 1}, f"{name}-two",
                )
                other = self.runtime.propose_candidate(
                    analysis_id, nearby, discovery_ref="test",
                )
                self.assertNotEqual(other.task_id, task.task_id)
                ref, fact = self.complete_first_check(
                    task, snapshot, checks[0], identity.scope,
                )
                for remaining in checks[1:]:
                    self.complete_first_check(
                        task, snapshot, remaining, identity.scope,
                    )
                claim, assessment = self.complete_roles(
                    task, ref, fact, identity.scope,
                )
                decision = self.runtime.evaluate_candidate(
                    task.task_id, claim.claim_id, assessment.assessment_id,
                )
                self.assertEqual(decision.verdict, verdict)
                self.assertEqual(
                    self.runtime.evaluate_candidate(
                        task.task_id, claim.claim_id, assessment.assessment_id,
                    ),
                    decision,
                )
                self.assertEqual(self.runtime.find_exclusion(other.task_id), None)
                if verdict == "refuted":
                    self.assertIsNotNone(
                        self.runtime.find_exclusion(task.task_id)
                    )
                    root = Path(self.temp.name)
                    fresh_store = SQLiteStore(
                        root / "state.sqlite3", root / "artifacts",
                    )
                    try:
                        fresh_runtime = DefectRuntime(fresh_store)
                        fresh_runtime.register_evaluator(
                            EvidenceEvaluator("beta-eval", "refuted")
                        )
                        fresh_coordinator = RoleCoordinator(
                            fresh_runtime, NeverRunAgent(), self.backend,
                            owner_id="new-process", working_dir=self.temp.name,
                        )
                        self.assertEqual(
                            fresh_coordinator.run_candidate(task.task_id).verdict,
                            "refuted",
                        )
                    finally:
                        fresh_store.close()
                    next_snapshot = FixedSnapshot(
                        "repository", snapshot.scope_id, "c" * 64,
                        digest(rule), snapshot.tool_policy_digest,
                    )
                    next_analysis = self.runtime.create_analysis(next_snapshot, rule)
                    next_key = CandidateIdentity(
                        rule.rule_id, rule.rule_version,
                        next_snapshot.snapshot_digest,
                        {"subject": "beta-one", "variant": 1}, "beta-one",
                    )
                    next_task = self.runtime.propose_candidate(
                        next_analysis, next_key, discovery_ref="new-snapshot",
                    )
                    self.assertIsNone(self.runtime.find_exclusion(next_task.task_id))
                self.store.rebuild_projections(analysis_id)
                self.assertEqual(self.runtime.task(task.task_id)["finding_state"],
                                 verdict)

    def test_generic_identity_and_query_cannot_cross_snapshots(self):
        analysis_id, snapshot, rule = self.make_analysis(
            "alpha", ("origin",), "alpha-eval",
        )
        key = CandidateIdentity(
            rule.rule_id, rule.rule_version, "wrong",
            {"subject": "x"}, "x",
        )
        with self.assertRaises(StaleSnapshot):
            self.runtime.propose_candidate(analysis_id, key, discovery_ref="test")
        key = CandidateIdentity(
            rule.rule_id, rule.rule_version, snapshot.snapshot_digest,
            {"subject": "x"}, "x",
        )
        task = self.runtime.propose_candidate(analysis_id, key, discovery_ref="test")
        request = QueryRequest(
            str(uuid.uuid4()), task.task_id, "origin", "inspect",
            snapshot.snapshot_digest, "abstract", "1",
            snapshot.tool_policy_digest,
            {"scope_id": "wrong", "selectors": {"subject": "x"}, "max_results": 3},
            {"label": "x"}, str(uuid.uuid4()), 1000,
        )
        with self.assertRaises(StaleSnapshot):
            self.runtime.query_program(request, self.backend)
        malformed = QueryRequest(
            str(uuid.uuid4()), task.task_id, "origin", "inspect",
            snapshot.snapshot_digest, "abstract", "1",
            snapshot.tool_policy_digest,
            {"scope_id": snapshot.scope_id,
             "selectors": {"subject": "x"}, "max_results": 3},
            {"label": "x", "extra": "unregistered"},
            str(uuid.uuid4()), 1000,
        )
        with self.assertRaises(InvalidInput):
            self.runtime.query_program(malformed, self.backend)
        self.assertEqual(self.store.list("query", analysis_id), [])
        with self.assertRaises(InvalidInput):
            CandidateIdentity(rule.rule_id, rule.rule_version,
                              snapshot.snapshot_digest, {"subject": None}, "x")

    def test_identity_is_immutable_after_caller_changes_input(self):
        material = {"subject": {"path": ["entry", "target"]}}
        key = CandidateIdentity("rule", "1", "s" * 64, material, "target")
        original = key.candidate_digest
        material["subject"]["path"].append("changed")
        self.assertEqual(key.candidate_digest, original)
        with self.assertRaises(TypeError):
            key.identity["subject"] = "replacement"

    def test_provisional_generic_identity_needs_explicit_binding(self):
        analysis_id, snapshot, rule = self.make_analysis(
            "alpha", ("origin",), "alpha-eval",
        )
        proposal = self.runtime.propose_from_material(
            analysis_id, {"subject": "case-A"}, SubjectPathPolicy(),
            discovery_ref="detector",
        )
        self.assertEqual(proposal.unresolved_identity, ("path",))
        self.assertEqual(self.store.list("task", analysis_id), [])
        wrong = CandidateIdentity(
            rule.rule_id, rule.rule_version, snapshot.snapshot_digest,
            {"subject": "case-B", "path": "branch-1"}, "case-A",
        )
        with self.assertRaises(InvalidInput):
            self.runtime.bind_provisional(proposal.proposal_id, wrong)
        key = CandidateIdentity(
            rule.rule_id, rule.rule_version, snapshot.snapshot_digest,
            {"subject": "case-A", "path": "branch-1"}, "case-A",
        )
        task = self.runtime.bind_provisional(proposal.proposal_id, key)
        self.assertEqual(task.candidate_digest, key.candidate_digest)
        self.assertEqual(
            self.runtime.bind_provisional(proposal.proposal_id, key).task_id,
            task.task_id,
        )

    def test_common_gate_rejects_evaluator_fabricated_evidence(self):
        self.runtime.register_evaluator(ForgedEvaluator("forged-eval", "confirmed"))
        analysis_id, snapshot, rule = self.make_analysis(
            "gamma", ("origin",), "forged-eval",
        )
        key = CandidateIdentity(
            rule.rule_id, rule.rule_version, snapshot.snapshot_digest,
            {"subject": "gamma"}, "gamma",
        )
        task = self.runtime.propose_candidate(
            analysis_id, key, discovery_ref="test",
        )
        ref, fact = self.complete_first_check(task, snapshot, "origin", "gamma")
        claim, assessment = self.complete_roles(task, ref, fact, "gamma")
        with self.assertRaises(InvalidInput):
            self.runtime.evaluate_candidate(
                task.task_id, claim.claim_id, assessment.assessment_id,
            )
        self.assertEqual(self.runtime.task(task.task_id)["phase"], "verifying")

    def test_common_gate_rejects_definite_verdict_from_partial_evidence(self):
        analysis_id, snapshot, rule = self.make_analysis(
            "alpha", ("origin",), "alpha-eval",
        )
        task = self.runtime.propose_candidate(
            analysis_id,
            CandidateIdentity(rule.rule_id, rule.rule_version,
                              snapshot.snapshot_digest, {"subject": "x"}, "x"),
            discovery_ref="test",
        )
        complete_outcome, complete_ref = self.runtime.query_program(
            QueryRequest(
                str(uuid.uuid4()), task.task_id, "origin", "inspect",
                snapshot.snapshot_digest, "abstract", "1",
                snapshot.tool_policy_digest,
                {"scope_id": snapshot.scope_id,
                 "selectors": {"subject": "x"}, "max_results": 3},
                {"label": "x"}, str(uuid.uuid4()), 1000,
            ), self.backend,
        )
        self.runtime.update_check(CheckItem(
            "origin", task.task_id, "origin", "complete", "supported",
            (complete_ref,), complete_outcome.coverage,
        ))
        _, ref = self.runtime.query_program(
            QueryRequest(
                str(uuid.uuid4()), task.task_id, "origin", "inspect",
                snapshot.snapshot_digest, "abstract", "1",
                snapshot.tool_policy_digest,
                {"scope_id": snapshot.scope_id,
                 "selectors": {"subject": "x"}, "max_results": 3},
                {"label": "x"}, str(uuid.uuid4()), 1000,
            ), AbstractBackend("partial"),
        )
        fact = FactAssessment(
            str(uuid.uuid4()), task.task_id, "origin", "origin", "positive",
            (ref,), "x", "tool_observed",
        )
        self.runtime.record_fact(fact)
        claim, assessment = self.complete_roles(task, ref, fact, "x")
        with self.assertRaisesRegex(InvalidInput, "incomplete coverage"):
            self.runtime.evaluate_candidate(
                task.task_id, claim.claim_id, assessment.assessment_id,
            )
        self.assertEqual(self.runtime.task(task.task_id)["phase"], "verifying")

    def test_common_gate_rejects_definite_verdict_with_open_check(self):
        analysis_id, snapshot, rule = self.make_analysis(
            "alpha", ("origin", "boundary"), "alpha-eval",
        )
        task = self.runtime.propose_candidate(
            analysis_id,
            CandidateIdentity(rule.rule_id, rule.rule_version,
                              snapshot.snapshot_digest, {"subject": "x"}, "x"),
            discovery_ref="test",
        )
        ref, fact = self.complete_first_check(task, snapshot, "origin", "x")
        claim, assessment = self.complete_roles(task, ref, fact, "x")
        with self.assertRaisesRegex(InvalidInput, "incomplete required checks"):
            self.runtime.evaluate_candidate(
                task.task_id, claim.claim_id, assessment.assessment_id,
            )
        self.assertEqual(self.runtime.checks(task.task_id)[1]["status"],
                         "unexamined")

    def test_coordinator_reports_inconclusive_for_open_required_check(self):
        analysis_id, snapshot, rule = self.make_analysis(
            "alpha", ("origin", "boundary"), "alpha-eval",
        )
        task = self.runtime.propose_candidate(
            analysis_id,
            CandidateIdentity(rule.rule_id, rule.rule_version,
                              snapshot.snapshot_digest, {"subject": "x"}, "x"),
            discovery_ref="test",
        )
        ref, fact = self.complete_first_check(task, snapshot, "origin", "x")

        class IncompleteAgent:
            def describe_capabilities(self):
                return AgentCapabilities(False, False, False, True,
                                         True, True, True, False)

            def run(self, request, tools):
                if request.role_kind == "investigator":
                    return RoleRunResult(Claim(
                        str(uuid.uuid4()), request.task_id, request.attempt_id,
                        (fact.fact_id,), (ref,), (), (), "x",
                    ), "sdk-investigator")
                return RoleRunResult(Assessment(
                    str(uuid.uuid4()), request.task_id, request.attempt_id,
                    request.claim.claim_id, ("origin", "boundary"),
                    (ref,), (), (), "reviewed", "x",
                ), "sdk-verifier")

        decision = RoleCoordinator(
            self.runtime, IncompleteAgent(), self.backend, owner_id="worker",
            working_dir=self.temp.name,
        ).run_candidate(task.task_id)
        self.assertEqual(decision.verdict, "inconclusive")
        self.assertTrue(any("incomplete required checks" in blocker
                            for blocker in decision.blockers))
        self.assertEqual(self.runtime.task(task.task_id)["execution_state"],
                         "partial")

    def test_common_gate_rejects_conflicting_observed_facts(self):
        analysis_id, snapshot, rule = self.make_analysis(
            "alpha", ("origin",), "alpha-eval",
        )
        task = self.runtime.propose_candidate(
            analysis_id,
            CandidateIdentity(rule.rule_id, rule.rule_version,
                              snapshot.snapshot_digest, {"subject": "x"}, "x"),
            discovery_ref="test",
        )
        ref, fact = self.complete_first_check(task, snapshot, "origin", "x")
        self.runtime.record_fact(FactAssessment(
            str(uuid.uuid4()), task.task_id, "origin", "origin", "negative",
            (ref,), "x", "tool_observed",
        ))
        claim, assessment = self.complete_roles(task, ref, fact, "x")
        with self.assertRaisesRegex(InvalidInput, "conflicting observed facts"):
            self.runtime.evaluate_candidate(
                task.task_id, claim.claim_id, assessment.assessment_id,
            )

    def test_common_gate_requires_verifier_to_cite_decisive_evidence(self):
        analysis_id, snapshot, rule = self.make_analysis(
            "alpha", ("origin",), "alpha-eval",
        )
        task = self.runtime.propose_candidate(
            analysis_id,
            CandidateIdentity(rule.rule_id, rule.rule_version,
                              snapshot.snapshot_digest, {"subject": "x"}, "x"),
            discovery_ref="test",
        )
        ref, fact = self.complete_first_check(task, snapshot, "origin", "x")
        claim, assessment = self.complete_roles(task, ref, fact, "x")
        unreviewed = Assessment(
            str(uuid.uuid4()), task.task_id, assessment.verifier_attempt_id,
            claim.claim_id, ("origin",), (), (), (), "no source reviewed", "x",
        )
        self.runtime.record_assessment(unreviewed)
        with self.assertRaisesRegex(InvalidInput, "not reviewed by verifier"):
            self.runtime.evaluate_candidate(
                task.task_id, claim.claim_id, unreviewed.assessment_id,
            )

    def test_unregistered_evaluator_fails_before_analysis_creation(self):
        rule = RuleSpec(
            "unbound", "1", "unbound",
            (CheckDefinition("origin", "origin", ("origin",)),),
            "missing-evaluator", "1",
        )
        snapshot = FixedSnapshot(
            "repository", "scope-unbound", "a" * 64,
            digest(rule), "b" * 64,
        )
        with self.assertRaises(CapabilityUnavailable):
            self.runtime.create_analysis(
                snapshot, rule, analysis_id="unbound-analysis",
            )
        self.assertIsNone(self.store.get("analysis", "unbound-analysis"))

    def test_profile_runs_focused_specialist_in_separate_session(self):
        rule = RuleSpec(
            "profile-rule", "1", "profile-rule",
            (CheckDefinition("origin", "origin", ("origin",)),
             CheckDefinition("boundary", "boundary", ("boundary",))),
            "profile-eval", "1",
        )
        profile = Profile(
            "profile", "1", rule.rule_id, rule.rule_version,
            (RoleDefinition("inv-custom", "investigator"),
             RoleDefinition("boundary-expert", "specialist", ("boundary",),
                            ("inspect",)),
             RoleDefinition("verify-custom", "verifier")),
            "b" * 64,
        )
        snapshot = FixedSnapshot(
            "repository", "profile-scope", "a" * 64,
            digest(profile), profile.tool_policy_digest,
        )
        self.runtime.register_evaluator(EvidenceEvaluator("profile-eval", "confirmed"))
        analysis_id = self.runtime.create_analysis(snapshot, rule, profile=profile)
        key = CandidateIdentity(
            rule.rule_id, rule.rule_version, snapshot.snapshot_digest,
            {"subject": "case-A", "path": "one"}, "case-A",
        )
        task = self.runtime.propose_candidate(analysis_id, key, discovery_ref="test")
        ref, fact = self.complete_first_check(task, snapshot, "origin", "case-A")
        agent = ProfileAgent(self.runtime, snapshot, fact, ref)
        coordinator = RoleCoordinator(
            self.runtime, agent, self.backend, owner_id="worker",
            working_dir=self.temp.name,
        )
        decision = coordinator.run_candidate(task.task_id)
        self.assertEqual(decision.verdict, "confirmed")
        self.assertEqual(agent.calls,
                         ["inv-custom", "boundary-expert", "verify-custom"])
        notes = self.store.list("specialist_note", analysis_id)
        self.assertEqual(len(notes), 1)
        bindings = self.store.list("binding", analysis_id)
        self.assertEqual(len({item["sdk_session_id"] for item in bindings}), 3)
        self.assertEqual({item["role"] for item in bindings}, set(agent.calls))

    def test_profile_capability_is_checked_before_attempt(self):
        rule = RuleSpec(
            "requires-interrupt", "1", "requires-interrupt",
            (CheckDefinition("origin", "origin", ("origin",)),),
            "alpha-eval", "1",
        )
        profile = Profile(
            "interrupt", "1", rule.rule_id, rule.rule_version,
            (RoleDefinition("investigate", "investigator",
                            required_capabilities=("interrupt",)),
             RoleDefinition("verify", "verifier")),
            "b" * 64,
        )
        snapshot = FixedSnapshot(
            "repository", "capability-scope", "a" * 64,
            digest(profile), profile.tool_policy_digest,
        )
        analysis_id = self.runtime.create_analysis(snapshot, rule, profile=profile)
        task = self.runtime.propose_candidate(
            analysis_id,
            CandidateIdentity(rule.rule_id, rule.rule_version,
                              snapshot.snapshot_digest, {"subject": "x"}, "x"),
            discovery_ref="test",
        )
        coordinator = RoleCoordinator(
            self.runtime, NeverRunAgent(), self.backend, owner_id="worker",
            working_dir=self.temp.name,
        )
        with self.assertRaises(CapabilityUnavailable):
            coordinator.run_candidate(task.task_id)
        self.assertEqual(self.store.list("attempt", analysis_id), [])

    def test_version_bound_planner_persists_candidate_specific_checks(self):
        rule = RuleSpec(
            "planned", "1", "planned",
            (CheckDefinition("origin", "origin", ("origin",)),),
            "alpha-eval", "1",
        )
        planner = ExtraCheckPlanner()
        snapshot = FixedSnapshot(
            "repository", "planned-scope", "a" * 64,
            analysis_profile_digest(rule, check_planner=planner), "b" * 64,
        )
        analysis_id = self.runtime.create_analysis(
            snapshot, rule, check_planner=planner,
        )
        tasks = []
        for subject in ("case-A", "case-B"):
            key = CandidateIdentity(
                rule.rule_id, rule.rule_version, snapshot.snapshot_digest,
                {"subject": subject}, subject,
            )
            tasks.append(self.runtime.propose_candidate(
                analysis_id, key, discovery_ref="test",
            ))
        self.assertEqual(tasks[0].required_check_ids, ("origin", "boundary"))
        self.assertEqual(tasks[1].required_check_ids, ("origin",))
        self.assertEqual(
            [item["check_id"] for item in self.runtime.checks(tasks[0].task_id)],
            ["origin", "boundary"],
        )
        self.store.rebuild_projections(analysis_id)
        self.assertEqual(
            self.runtime.task(tasks[0].task_id)["required_check_ids"],
            ["origin", "boundary"],
        )

    def test_partial_empty_and_timeout_remain_unexamined(self):
        analysis_id, snapshot, rule = self.make_analysis(
            "alpha", ("origin",), "alpha-eval",
        )
        task = self.runtime.propose_candidate(
            analysis_id,
            CandidateIdentity(rule.rule_id, rule.rule_version,
                              snapshot.snapshot_digest, {"subject": "x"}, "x"),
            discovery_ref="test",
        )
        for mode in ("partial", "timeout"):
            with self.subTest(mode=mode):
                request = QueryRequest(
                    str(uuid.uuid4()), task.task_id, "origin", "inspect",
                    snapshot.snapshot_digest, "abstract", "1",
                    snapshot.tool_policy_digest,
                    {"scope_id": snapshot.scope_id,
                     "selectors": {"subject": "x"}, "max_results": 3},
                    {"label": "x"}, str(uuid.uuid4()), 1000,
                )
                outcome, ref = self.runtime.query_program(
                    request, AbstractBackend(mode),
                )
                self.assertEqual(outcome.status, mode)
                self.assertEqual(ref is None, mode == "timeout")
                self.assertEqual(self.runtime.checks(task.task_id)[0]["status"],
                                 "unexamined")

    def test_generic_handoff_rotation_keeps_open_checks_and_facts(self):
        analysis_id, snapshot, rule = self.make_analysis(
            "alpha", ("origin", "boundary"), "alpha-eval",
        )
        key = CandidateIdentity(
            rule.rule_id, rule.rule_version, snapshot.snapshot_digest,
            {"subject": "case-A"}, "case-A",
        )
        task = self.runtime.propose_candidate(analysis_id, key,
                                              discovery_ref="test")
        self.complete_first_check(task, snapshot, "origin", "case-A")
        service = SessionService(self.runtime)
        old, lease = service.start_attempt(
            task.task_id, "investigator", working_dir=self.temp.name,
            owner_id="before", sdk_session_id="sdk-before",
        )
        protected = service.protected_items(task.task_id)
        self.assertIn("check:boundary", protected)
        self.assertTrue(any(value.startswith("fact:") for value in protected))
        handoff = Handoff(
            str(uuid.uuid4()), task.task_id, old.attempt_id,
            task.snapshot_digest, task.candidate_digest,
            self.store.high_watermark(analysis_id), tuple(sorted(protected)),
        )
        new, new_lease = service.rotate(
            handoff, owner_id="before", lease_epoch=lease.lease_epoch,
            next_owner_id="after", working_dir=self.temp.name,
        )
        self.assertIsNone(new.sdk_session_id)
        transition = self.store.list("transition", analysis_id)[0]
        self.assertEqual(transition["status"], "completed")
        self.assertEqual(transition["new_binding_id"], new.binding_id)
        service.bind_sdk_session(new.binding_id, "sdk-after",
                                 owner_id="after",
                                 lease_epoch=new_lease.lease_epoch)
        view = ContextViewBuilder(self.runtime, service).build(
            task.task_id, "investigator", focus_check_ids=("boundary",),
            token_budget=4000,
        )
        visible = {item["id"] for item in view.protected_items}
        self.assertTrue(protected.issubset(visible))
        self.assertEqual(self.runtime.checks(task.task_id)[1]["status"],
                         "unexamined")

    def test_generic_query_recovery_reuses_committed_evidence(self):
        analysis_id, snapshot, rule = self.make_analysis(
            "alpha", ("origin",), "alpha-eval",
        )
        task = self.runtime.propose_candidate(
            analysis_id,
            CandidateIdentity(rule.rule_id, rule.rule_version,
                              snapshot.snapshot_digest, {"subject": "x"}, "x"),
            discovery_ref="test",
        )
        request = QueryRequest(
            str(uuid.uuid4()), task.task_id, "origin", "inspect",
            snapshot.snapshot_digest, "abstract", "1",
            snapshot.tool_policy_digest,
            {"scope_id": snapshot.scope_id,
             "selectors": {"subject": "x"}, "max_results": 3},
            {"label": "x"}, "stable-generic-key", 1000,
        )
        self.store.reserve_query(
            analysis_id, key=request.idempotency_key,
            request_digest=request.request_digest, query_id=request.query_id,
            request_record={
                "check_id": request.check_id, "request": request.__dict__,
                "request_digest": request.request_digest, "status": "requested",
                "outcome": None, "evidence_id": None,
            }, task_id=task.task_id,
        )
        self.assertEqual(self.runtime.recover_in_doubt(analysis_id),
                         [request.query_id])
        first, ref = self.runtime.query_program(request, self.backend)
        second, same_ref = self.runtime.query_program(request, self.backend)
        self.assertEqual(first, second)
        self.assertEqual(ref.evidence_id, same_ref.evidence_id)
        self.assertEqual(self.runtime.checks(task.task_id)[0]["status"],
                         "unexamined")

    def test_generic_query_crash_windows_keep_check_and_evidence_consistent(self):
        for window in ("before-artifact", "after-artifact", "before-event",
                       "after-event-before-check"):
            with self.subTest(window=window):
                analysis_id, snapshot, rule = self.make_analysis(
                    f"crash-{window}", ("origin",), "alpha-eval",
                )
                task = self.runtime.propose_candidate(
                    analysis_id,
                    CandidateIdentity(rule.rule_id, rule.rule_version,
                                      snapshot.snapshot_digest,
                                      {"subject": "x"}, "x"),
                    discovery_ref="test",
                )
                request = QueryRequest(
                    str(uuid.uuid4()), task.task_id, "origin", "inspect",
                    snapshot.snapshot_digest, "abstract", "1",
                    snapshot.tool_policy_digest,
                    {"scope_id": snapshot.scope_id,
                     "selectors": {"subject": "x"}, "max_results": 3},
                    {"label": "x"}, str(uuid.uuid4()), 1000,
                )
                raw_digest = bytes_digest(b"origin:x")
                if window in {"before-artifact", "after-artifact"}:
                    original_put = self.store.put_artifact

                    def interrupted_put(raw):
                        if window == "after-artifact":
                            original_put(raw)
                        raise RuntimeError("simulated artifact boundary")

                    with patch.object(self.store, "put_artifact",
                                      side_effect=interrupted_put):
                        with self.assertRaises(RuntimeError):
                            self.runtime.query_program(request, self.backend)
                    self.assertEqual(
                        self.store.artifact_path(raw_digest).exists(),
                        window == "after-artifact",
                    )
                elif window == "before-event":
                    original_append = self.store.append

                    def interrupted_append(analysis, *mutations):
                        if any(item.event_type == "QueryFinished"
                               for item in mutations):
                            raise RuntimeError("simulated event boundary")
                        return original_append(analysis, *mutations)

                    with patch.object(self.store, "append",
                                      side_effect=interrupted_append):
                        with self.assertRaises(RuntimeError):
                            self.runtime.query_program(request, self.backend)
                    self.assertEqual(self.store.read_artifact(raw_digest),
                                     b"origin:x")
                else:
                    first, first_ref = self.runtime.query_program(request,
                                                                  self.backend)
                    self.assertEqual(first.status, "complete")
                    self.assertEqual(self.runtime.recover_in_doubt(analysis_id), [])

                if window != "after-event-before-check":
                    self.assertEqual(self.runtime.recover_in_doubt(analysis_id),
                                     [request.query_id])
                    self.assertEqual(
                        self.store.get("query", request.query_id)["status"],
                        "in_doubt",
                    )
                outcome, ref = self.runtime.query_program(request, self.backend)
                again, same_ref = self.runtime.query_program(request,
                                                              self.backend)
                self.assertEqual(outcome, again)
                self.assertEqual(ref.evidence_id, same_ref.evidence_id)
                if window == "after-event-before-check":
                    self.assertEqual(ref.evidence_id, first_ref.evidence_id)
                self.assertEqual(ref.artifact_digest, raw_digest)
                self.assertEqual(self.runtime.checks(task.task_id)[0]["status"],
                                 "unexamined")
                self.runtime.update_check(CheckItem(
                    "origin", task.task_id, "origin", "complete", "supported",
                    (ref,), outcome.coverage,
                ))
                self.store.rebuild_projections(analysis_id)
                self.assertEqual(self.runtime.checks(task.task_id)[0]["status"],
                                 "complete")

    def test_core_import_does_not_load_optional_sdk(self):
        script = (
            "import builtins,sys\n"
            "original=builtins.__import__\n"
            "def guarded(name,*args,**kwargs):\n"
            "    if name.startswith(('claude_agent_sdk','joern')):\n"
            "        raise ImportError('optional dependency blocked')\n"
            "    return original(name,*args,**kwargs)\n"
            "builtins.__import__=guarded\n"
            "import agent_runtime\n"
            "assert 'claude_agent_sdk' not in sys.modules\n"
        )
        result = subprocess.run(
            [sys.executable, "-c", script], text=True,
            capture_output=True, check=False,
        )
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_missing_sdk_history_starts_fresh_from_generic_handoff(self):
        analysis_id, snapshot, rule = self.make_analysis(
            "alpha", ("origin",), "alpha-eval",
        )
        task = self.runtime.propose_candidate(
            analysis_id,
            CandidateIdentity(rule.rule_id, rule.rule_version,
                              snapshot.snapshot_digest, {"subject": "x"}, "x"),
            discovery_ref="test",
        )
        service = SessionService(self.runtime)
        old, lease = service.start_attempt(
            task.task_id, "investigator", working_dir=self.temp.name,
            owner_id="old", sdk_session_id="lost-history",
        )
        handoff = Handoff(
            str(uuid.uuid4()), task.task_id, old.attempt_id,
            task.snapshot_digest, task.candidate_digest,
            self.store.high_watermark(analysis_id),
            tuple(sorted(service.protected_items(task.task_id))),
        )
        service.checkpoint_handoff(handoff)
        service.end_attempt(
            old.attempt_id, owner_id="old", lease_epoch=lease.lease_epoch,
            status="paused", reason="transcript missing",
        )
        new, _ = service.start_fresh_from_handoff(
            old.binding_id, reason="SDK transcript unavailable",
            owner_id="new", working_dir=self.temp.name,
        )
        self.assertIsNone(new.sdk_session_id)
        self.assertNotEqual(new.attempt_id, old.attempt_id)
        transition = self.store.list("transition", analysis_id)[0]
        self.assertEqual(transition["status"], "completed")
        self.assertEqual(transition["new_binding_id"], new.binding_id)
        events = [item["event_type"] for item in self.store.events(analysis_id)]
        self.assertIn("SessionUnavailable", events)
        self.assertEqual(self.runtime.checks(task.task_id)[0]["status"],
                         "unexamined")
        with self.assertRaises(Conflict):
            service.start_fresh_from_handoff(
                old.binding_id, reason="retry", owner_id="third",
                working_dir=self.temp.name,
            )

    def test_unknown_can_leave_handoff_only_after_evidence_resolution(self):
        analysis_id, snapshot, rule = self.make_analysis(
            "alpha", ("origin",), "alpha-eval",
        )
        task = self.runtime.propose_candidate(
            analysis_id,
            CandidateIdentity(rule.rule_id, rule.rule_version,
                              snapshot.snapshot_digest, {"subject": "x"}, "x"),
            discovery_ref="test",
        )
        self.runtime.update_check(CheckItem(
            "origin", task.task_id, "origin", "partial", "unknown", (),
            Coverage("", "", "unknown"), ("may-be-guarded",),
        ))
        service = SessionService(self.runtime)
        binding, lease = service.start_attempt(
            task.task_id, "investigator", working_dir=self.temp.name,
            owner_id="worker",
        )
        first = Handoff(
            str(uuid.uuid4()), task.task_id, binding.attempt_id,
            task.snapshot_digest, task.candidate_digest,
            self.store.high_watermark(analysis_id),
            tuple(sorted(service.protected_items(task.task_id))),
        )
        service.checkpoint_handoff(first)
        self.assertIn("unknown:may-be-guarded", first.protected_item_ids)
        outcome, ref = self.runtime.query_program(
            QueryRequest(
                str(uuid.uuid4()), task.task_id, "origin", "inspect",
                snapshot.snapshot_digest, "abstract", "1",
                snapshot.tool_policy_digest,
                {"scope_id": snapshot.scope_id,
                 "selectors": {"subject": "x"}, "max_results": 3},
                {"label": "x"}, str(uuid.uuid4()), 1000,
            ), self.backend,
        )
        self.runtime.update_check(CheckItem(
            "origin", task.task_id, "origin", "complete", "supported",
            (ref,), outcome.coverage,
        ))
        pending_view = ContextViewBuilder(self.runtime, service).build(
            task.task_id, "investigator", focus_check_ids=("origin",),
            token_budget=4000,
        )
        self.assertIn("unknown:may-be-guarded",
                      {item["id"] for item in pending_view.protected_items})
        candidate_handoff = lambda: Handoff(
            str(uuid.uuid4()), task.task_id, binding.attempt_id,
            task.snapshot_digest, task.candidate_digest,
            self.store.high_watermark(analysis_id), (),
        )
        with self.assertRaises(InvalidInput):
            service.checkpoint_handoff(candidate_handoff())
        for item_id in first.protected_item_ids:
            service.resolve_protected_item(
                task.task_id, item_id, evidence_refs=(ref,),
                reason="complete query resolved the open condition",
            )
        service.checkpoint_handoff(candidate_handoff())
        self.assertEqual(service.latest_handoff(task.task_id).protected_item_ids,
                         ())
        resolved_view = ContextViewBuilder(self.runtime, service).build(
            task.task_id, "investigator", focus_check_ids=("origin",),
            token_budget=4000,
        )
        self.assertNotIn("unknown:may-be-guarded",
                         {item["id"] for item in resolved_view.protected_items})
        service.end_attempt(
            binding.attempt_id, owner_id="worker",
            lease_epoch=lease.lease_epoch, status="completed", reason="resolved",
        )

    def test_old_unknown_cannot_be_resolved_by_unrelated_later_check(self):
        analysis_id, snapshot, rule = self.make_analysis(
            "alpha", ("origin", "boundary"), "alpha-eval",
        )
        task = self.runtime.propose_candidate(
            analysis_id,
            CandidateIdentity(rule.rule_id, rule.rule_version,
                              snapshot.snapshot_digest, {"subject": "x"}, "x"),
            discovery_ref="test",
        )
        self.runtime.update_check(CheckItem(
            "origin", task.task_id, "origin", "partial", "unknown", (),
            Coverage("", "", "unknown"), ("old-unknown",),
        ))
        service = SessionService(self.runtime)
        binding, lease = service.start_attempt(
            task.task_id, "investigator", working_dir=self.temp.name,
            owner_id="worker",
        )

        def save_handoff(protected):
            handoff = Handoff(
                str(uuid.uuid4()), task.task_id, binding.attempt_id,
                task.snapshot_digest, task.candidate_digest,
                self.store.high_watermark(analysis_id), tuple(sorted(protected)),
            )
            service.checkpoint_handoff(handoff)
            return handoff

        first = save_handoff(service.protected_items(task.task_id))
        _, ref = self.runtime.query_program(
            QueryRequest(
                str(uuid.uuid4()), task.task_id, "origin", "inspect",
                snapshot.snapshot_digest, "abstract", "1",
                snapshot.tool_policy_digest,
                {"scope_id": snapshot.scope_id,
                 "selectors": {"subject": "x"}, "max_results": 3},
                {"label": "x"}, str(uuid.uuid4()), 1000,
            ), self.backend,
        )
        self.runtime.update_check(CheckItem(
            "origin", task.task_id, "origin", "complete", "supported",
            (ref,), Coverage("declared candidate", "declared candidate",
                             "complete", (), (ref.artifact_digest,)),
        ))
        second = save_handoff(first.protected_item_ids)
        self.runtime.update_check(CheckItem(
            "boundary", task.task_id, "boundary", "partial", "unknown", (),
            Coverage("", "", "unknown"), ("different-unknown",),
        ))
        with self.assertRaises(Conflict):
            service.resolve_protected_item(
                task.task_id, "unknown:old-unknown", evidence_refs=(ref,),
                reason="unrelated check changed",
            )
        self.assertEqual(service.latest_handoff(task.task_id).handoff_id,
                         second.handoff_id)
        service.end_attempt(
            binding.attempt_id, owner_id="worker",
            lease_epoch=lease.lease_epoch, status="completed", reason="saved",
        )

    def test_refutation_and_exclusion_stay_protected_in_handoff(self):
        analysis_id, snapshot, rule = self.make_analysis(
            "beta", ("origin",), "beta-eval",
        )
        task = self.runtime.propose_candidate(
            analysis_id,
            CandidateIdentity(rule.rule_id, rule.rule_version,
                              snapshot.snapshot_digest, {"subject": "x"}, "x"),
            discovery_ref="test",
        )
        ref, fact = self.complete_first_check(task, snapshot, "origin", "x")
        claim, assessment = self.complete_roles(task, ref, fact, "x")
        self.assertEqual(self.runtime.evaluate_candidate(
            task.task_id, claim.claim_id, assessment.assessment_id,
        ).verdict, "refuted")
        service = SessionService(self.runtime)
        protected = service.protected_items(task.task_id)
        self.assertIn(f"fact:{fact.fact_id}", protected)
        self.assertTrue(any(item.startswith("exclusion:") for item in protected))
        handoff = Handoff(
            str(uuid.uuid4()), task.task_id, assessment.verifier_attempt_id,
            task.snapshot_digest, task.candidate_digest,
            self.store.high_watermark(analysis_id), tuple(sorted(protected)),
        )
        service.checkpoint_handoff(handoff)
        with self.assertRaises(InvalidInput):
            service.checkpoint_handoff(Handoff(
                str(uuid.uuid4()), task.task_id,
                assessment.verifier_attempt_id,
                task.snapshot_digest, task.candidate_digest,
                self.store.high_watermark(analysis_id), (),
            ))
        with self.assertRaises(ContextBudgetExceeded):
            ContextViewBuilder(self.runtime, service).build(
                task.task_id, "verifier", focus_check_ids=("origin",),
                token_budget=1,
            )
        view = ContextViewBuilder(self.runtime, service).build(
            task.task_id, "verifier", focus_check_ids=("origin",),
            token_budget=4000,
        )
        self.assertTrue(protected.issubset(
            {item["id"] for item in view.protected_items}
        ))
