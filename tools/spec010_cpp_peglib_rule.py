"""Application-owned rule semantics for the cpp-peglib SPEC 010 acceptance."""
from __future__ import annotations

from agent_runtime.domain import (
    Assessment,
    CandidateIdentity,
    CandidateKey,
    CheckDefinition,
    Claim,
    EvidenceRef,
    FactAssessment,
    Profile,
    RoleDefinition,
    RuleDecision,
    RuleSpec,
)
from agent_runtime.runtime import decision_input_digest


def cpp_peglib_rule() -> RuleSpec:
    return RuleSpec(
        "cpp-peglib.ast-optimizer-invalid-access",
        "1",
        "The fixed cpp-peglib probe observes an AST optimizer invalid access",
        (
            CheckDefinition(
                "dynamic_validation",
                "Classify the fixed isolated sanitizer observation",
                ("peglib_validation_observation",),
            ),
        ),
        "cpp-peglib.validation-observation",
        "1",
    )


def cpp_peglib_profile(tool_policy_digest: str) -> Profile:
    return Profile(
        "cpp-peglib-validation-review",
        "1",
        "cpp-peglib.ast-optimizer-invalid-access",
        "1",
        (
            RoleDefinition(
                "investigator", "investigator",
                focus_check_ids=("dynamic_validation",),
                allowed_operations=("read_recorded_validation",),
            ),
            RoleDefinition(
                "verifier", "verifier",
                focus_check_ids=("dynamic_validation",),
                allowed_operations=("read_recorded_validation",),
            ),
        ),
        tool_policy_digest,
    )


class CppPeglibValidationEvaluator:
    evaluator_id = "cpp-peglib.validation-observation"
    evaluator_version = "1"

    def evaluate(
        self,
        *,
        task,
        snapshot,
        candidate: CandidateKey | CandidateIdentity,
        facts: list[FactAssessment],
        checks,
        claim: Claim,
        assessment: Assessment,
    ) -> RuleDecision:
        scope = (
            candidate.scope
            if isinstance(candidate, CandidateIdentity)
            else candidate.path_identity
        )
        input_digest = decision_input_digest(
            task=task,
            snapshot=snapshot,
            candidate=candidate,
            facts=facts,
            checks=checks,
            claim=claim,
            assessment=assessment,
        )
        blockers = []
        if claim.scope != scope or assessment.scope != scope:
            blockers.append("role scope differs from candidate scope")
        required = set(task["required_check_ids"])
        if not required.issubset(assessment.checked_predicates):
            blockers.append("verifier did not check every required predicate")
        if claim.unresolved_items or assessment.unresolved_items:
            blockers.append("roles report unresolved validation evidence")
        check_map = {item["check_id"]: item for item in checks}
        for check_id in required:
            item = check_map.get(check_id)
            coverage = item.get("coverage", {}) if item else {}
            if (
                not item
                or item.get("status") != "complete"
                or coverage.get("completeness") != "complete"
                or item.get("unknowns")
            ):
                blockers.append(f"{check_id} lacks complete scoped coverage")
        claimed = {fact_id for fact_id in claim.fact_ids}
        usable = [
            fact for fact in facts
            if fact.fact_id in claimed
            and fact.scope == scope
            and fact.predicate_kind == "peglib_validation_observation"
            and fact.confidence_class == "tool_observed"
            and not fact.assumptions
        ]
        polarities = {fact.polarity for fact in usable}
        if "unknown" in polarities or not polarities:
            blockers.append("validation observation is not definitive")
        if {"positive", "negative"}.issubset(polarities):
            blockers.append("validation observations conflict")
        if blockers:
            return RuleDecision(
                "inconclusive", scope, (), (), (), (),
                tuple(dict.fromkeys(blockers)), self.evaluator_id,
                self.evaluator_version, input_digest,
            )
        if "negative" in polarities:
            refs = tuple(sorted({
                EvidenceRef(
                    ref.evidence_id, ref.artifact_digest,
                    ref.snapshot_digest, "counter",
                )
                for fact in usable if fact.polarity == "negative"
                for ref in fact.evidence_refs
            }, key=lambda ref: (
                ref.evidence_id, ref.artifact_digest, ref.snapshot_digest, ref.role
            )))
            return RuleDecision(
                "refuted", scope, tuple(required), (), (), refs, (),
                self.evaluator_id, self.evaluator_version, input_digest,
            )
        refs = tuple(sorted({
            EvidenceRef(
                ref.evidence_id, ref.artifact_digest,
                ref.snapshot_digest, "support",
            )
            for fact in usable if fact.polarity == "positive"
            for ref in fact.evidence_refs
        }, key=lambda ref: (
            ref.evidence_id, ref.artifact_digest, ref.snapshot_digest, ref.role
        )))
        return RuleDecision(
            "confirmed", scope, tuple(required), (), refs, (), (),
            self.evaluator_id, self.evaluator_version, input_digest,
        )
