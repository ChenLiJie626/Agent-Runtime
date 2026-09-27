"""Bundled rule plugins built on the public runtime contracts."""

from __future__ import annotations

from typing import Any

from .domain import (
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
from .runtime import decision_input_digest

CPP_UNREACHABLE_UNKNOWN = (
    "successful-build preprocessing, complete CFG coverage and executable "
    "reachability remain unproven"
)

CPP_USE_AFTER_FREE_UNKNOWN = (
    "an exact successful Clang use-after-free diagnostic remains unproven"
)


def cpp_unreachable_rule() -> RuleSpec:
    return RuleSpec(
        "cpp.unreachable-after-return",
        "1",
        "A statement follows an unconditional return in the same C++ block",
        (
            CheckDefinition(
                "source_window",
                "Retrieve the exact return and following source statement",
                ("source_window",),
            ),
            CheckDefinition(
                "semantic_validation",
                "Establish reachability with complete successful-build semantics",
                ("semantic_unreachable",),
            ),
        ),
        "cpp.unreachable-after-return",
        "1",
    )


def cpp_unreachable_profile(
    tool_policy_digest: str,
    *,
    allowed_operations: tuple[str, ...] = ("read_source",),
) -> Profile:
    return Profile(
        "cpp-unreachable-review",
        "1",
        "cpp.unreachable-after-return",
        "1",
        (
            RoleDefinition(
                "investigator",
                "investigator",
                allowed_operations=allowed_operations,
            ),
            RoleDefinition(
                "verifier",
                "verifier",
                allowed_operations=allowed_operations,
            ),
        ),
        tool_policy_digest,
    )


def _unique_refs(refs: list[EvidenceRef]) -> tuple[EvidenceRef, ...]:
    unique: dict[tuple[str, str, str], EvidenceRef] = {}
    for ref in refs:
        unique[(ref.evidence_id, ref.artifact_digest, ref.role)] = ref
    return tuple(unique.values())


class CppUnreachableEvaluator:
    """Confirm only complete build-equivalent unreachable-code evidence."""

    evaluator_id = "cpp.unreachable-after-return"
    evaluator_version = "1"

    def evaluate(
        self,
        *,
        task: dict[str, Any],
        snapshot: dict[str, Any],
        candidate: CandidateKey | CandidateIdentity,
        facts: list[FactAssessment],
        checks: list[dict[str, Any]],
        claim: Claim,
        assessment: Assessment,
    ) -> RuleDecision:
        scope = candidate.scope if isinstance(candidate, CandidateIdentity) else candidate.path_identity
        payload_digest = decision_input_digest(
            task=task,
            snapshot=snapshot,
            candidate=candidate,
            facts=facts,
            checks=checks,
            claim=claim,
            assessment=assessment,
        )
        blockers: list[str] = []
        if claim.scope != scope or assessment.scope != scope:
            blockers.append("role scope differs from candidate scope")
        required = set(task["required_check_ids"])
        if not required.issubset(assessment.checked_predicates):
            blockers.append("verifier did not check every required predicate")
        if claim.unresolved_items or assessment.unresolved_items:
            blockers.append("roles report unresolved items")

        check_map = {item["check_id"]: item for item in checks}
        for check_id in required:
            item = check_map.get(check_id)
            coverage = item.get("coverage", {}) if item else {}
            if (not item or item.get("status") != "complete"
                    or coverage.get("completeness") != "complete"
                    or item.get("unknowns")):
                blockers.append(f"{check_id} lacks complete scoped coverage")

        claimed = {fact.fact_id for fact in facts if fact.fact_id in claim.fact_ids}
        usable = [
            fact for fact in facts
            if fact.fact_id in claimed
            and fact.scope == scope
            and fact.confidence_class == "tool_observed"
            and not fact.assumptions
        ]
        source = [fact for fact in usable if fact.predicate_kind == "source_window"]
        semantic = [
            fact for fact in usable
            if fact.predicate_kind == "semantic_unreachable" and not fact.limitations
        ]
        if not any(fact.polarity == "positive" for fact in source):
            blockers.append("exact source window is unproven")
        semantic_polarities = {fact.polarity for fact in semantic}
        if {"positive", "negative"}.issubset(semantic_polarities):
            blockers.append("semantic evidence conflicts")

        if blockers or not semantic_polarities.intersection({"positive", "negative"}):
            if not blockers:
                blockers.append(CPP_UNREACHABLE_UNKNOWN)
            return RuleDecision(
                "inconclusive",
                scope,
                (),
                (),
                (),
                (),
                tuple(dict.fromkeys(blockers)),
                self.evaluator_id,
                self.evaluator_version,
                payload_digest,
            )

        if "negative" in semantic_polarities:
            refs = _unique_refs([
                EvidenceRef(ref.evidence_id, ref.artifact_digest, ref.snapshot_digest, "counter")
                for fact in semantic if fact.polarity == "negative" for ref in fact.evidence_refs
            ])
            return RuleDecision(
                "refuted",
                scope,
                tuple(task["required_check_ids"]),
                (),
                (),
                refs,
                (),
                self.evaluator_id,
                self.evaluator_version,
                payload_digest,
            )

        refs = _unique_refs([
            EvidenceRef(ref.evidence_id, ref.artifact_digest, ref.snapshot_digest, "support")
            for fact in (*source, *semantic) if fact.polarity == "positive"
            for ref in fact.evidence_refs
        ])
        return RuleDecision(
            "confirmed",
            scope,
            tuple(task["required_check_ids"]),
            (),
            refs,
            (),
            (),
            self.evaluator_id,
            self.evaluator_version,
            payload_digest,
        )


def cpp_use_after_free_rule() -> RuleSpec:
    return RuleSpec(
        "cpp.use-after-free",
        "1",
        "Freed C++ memory is accessed on a Clang Static Analyzer path",
        (
            CheckDefinition(
                "source_window",
                "Retrieve the exact source around the reported access",
                ("source_window",),
            ),
            CheckDefinition(
                "analyzer_diagnostic",
                "Establish an exact successful Clang use-after-free diagnostic",
                ("clang_use_after_free",),
            ),
        ),
        "cpp.use-after-free",
        "1",
    )


def cpp_use_after_free_profile(
    tool_policy_digest: str,
    *,
    allowed_operations: tuple[str, ...] = (
        "read_source", "read_clang_diagnostic",
    ),
) -> Profile:
    return Profile(
        "cpp-use-after-free-review",
        "1",
        "cpp.use-after-free",
        "1",
        (
            RoleDefinition(
                "investigator",
                "investigator",
                allowed_operations=allowed_operations,
            ),
            RoleDefinition(
                "verifier",
                "verifier",
                allowed_operations=allowed_operations,
            ),
        ),
        tool_policy_digest,
    )


class CppUseAfterFreeEvaluator:
    """Confirm a candidate only from source plus exact Clang UAF evidence."""

    evaluator_id = "cpp.use-after-free"
    evaluator_version = "1"

    def evaluate(
        self,
        *,
        task: dict[str, Any],
        snapshot: dict[str, Any],
        candidate: CandidateKey | CandidateIdentity,
        facts: list[FactAssessment],
        checks: list[dict[str, Any]],
        claim: Claim,
        assessment: Assessment,
    ) -> RuleDecision:
        scope = candidate.scope if isinstance(candidate, CandidateIdentity) else candidate.path_identity
        payload_digest = decision_input_digest(
            task=task,
            snapshot=snapshot,
            candidate=candidate,
            facts=facts,
            checks=checks,
            claim=claim,
            assessment=assessment,
        )
        blockers: list[str] = []
        if claim.scope != scope or assessment.scope != scope:
            blockers.append("role scope differs from candidate scope")
        required = set(task["required_check_ids"])
        if not required.issubset(assessment.checked_predicates):
            blockers.append("verifier did not check every required predicate")
        if claim.unresolved_items or assessment.unresolved_items:
            blockers.append("roles report unresolved items")

        check_map = {item["check_id"]: item for item in checks}
        for check_id in required:
            item = check_map.get(check_id)
            coverage = item.get("coverage", {}) if item else {}
            if (not item or item.get("status") != "complete"
                    or coverage.get("completeness") != "complete"
                    or item.get("unknowns")):
                blockers.append(f"{check_id} lacks complete scoped coverage")

        claimed = {fact.fact_id for fact in facts if fact.fact_id in claim.fact_ids}
        usable = [
            fact for fact in facts
            if fact.fact_id in claimed
            and fact.scope == scope
            and fact.confidence_class == "tool_observed"
            and not fact.assumptions
        ]
        source = [fact for fact in usable if fact.predicate_kind == "source_window"]
        analyzer = [
            fact for fact in usable
            if fact.predicate_kind == "clang_use_after_free" and not fact.limitations
        ]
        if not any(fact.polarity == "positive" for fact in source):
            blockers.append("exact source window is unproven")
        analyzer_polarities = {fact.polarity for fact in analyzer}
        if {"positive", "negative"}.issubset(analyzer_polarities):
            blockers.append("Clang analyzer evidence conflicts")

        if blockers or not analyzer_polarities.intersection({"positive", "negative"}):
            if not blockers:
                blockers.append(CPP_USE_AFTER_FREE_UNKNOWN)
            return RuleDecision(
                "inconclusive",
                scope,
                (),
                (),
                (),
                (),
                tuple(dict.fromkeys(blockers)),
                self.evaluator_id,
                self.evaluator_version,
                payload_digest,
            )

        if "negative" in analyzer_polarities:
            refs = _unique_refs([
                EvidenceRef(ref.evidence_id, ref.artifact_digest, ref.snapshot_digest, "counter")
                for fact in analyzer if fact.polarity == "negative" for ref in fact.evidence_refs
            ])
            return RuleDecision(
                "refuted",
                scope,
                tuple(task["required_check_ids"]),
                (),
                (),
                refs,
                (),
                self.evaluator_id,
                self.evaluator_version,
                payload_digest,
            )

        refs = _unique_refs([
            EvidenceRef(ref.evidence_id, ref.artifact_digest, ref.snapshot_digest, "support")
            for fact in (*source, *analyzer) if fact.polarity == "positive"
            for ref in fact.evidence_refs
        ])
        return RuleDecision(
            "confirmed",
            scope,
            tuple(task["required_check_ids"]),
            (),
            refs,
            (),
            (),
            self.evaluator_id,
            self.evaluator_version,
            payload_digest,
        )
