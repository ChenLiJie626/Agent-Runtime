"""Deterministic domain services for the first defect-analysis slice."""

from __future__ import annotations

import time
import uuid
from dataclasses import replace
from typing import Any

from .codec import SCHEMA_VERSION, digest, to_plain
from .domain import (
    AnalysisSnapshot,
    Assessment,
    CandidateIdentity,
    CandidateIdentityDraft,
    CandidateKey,
    CandidateTask,
    CheckDefinition,
    CheckItem,
    Claim,
    Coverage,
    DiscoveryOrigin,
    EvidenceRef,
    ExclusionRecord,
    FactAssessment,
    FixedSnapshot,
    GenericProvisionalCandidate,
    Profile,
    ProvisionalCandidate,
    QueryOutcome,
    QueryRequest,
    QueryScope,
    RuleDecision,
    RuleSpec,
    SpecialistNote,
    SourceLocation,
    StructuredQueryScope,
)
from .errors import (
    BackendFailure,
    CapabilityUnavailable,
    Conflict,
    EvidenceIntegrityError,
    InvalidInput,
    PolicyDenied,
    StaleSnapshot,
)
from .ports import (
    CandidateIdentityPolicy, CheckPlanner, EvidencePlanSelection, ProgramQuery, ProgramResult, QueryOperation,
    RuleEvaluator,
)
from .store import Mutation, SQLiteStore

CHECK_NAMES = (
    "nullable_source",
    "propagation",
    "dangerous_use",
    "guard",
    "reachability",
    "change_attribution",
)


def null_return_rule() -> RuleSpec:
    from .domain import CheckDefinition

    return RuleSpec(
        "cpp.new-null-return",
        "1.0",
        "New null return reaches a dereference",
        tuple(
            CheckDefinition(name, name.replace("_", " "), (name,))
            for name in CHECK_NAMES
        ),
        "null-return-v1",
        "1.0",
    )


def _new_id() -> str:
    return str(uuid.uuid4())


def analysis_profile_digest(
    rule: RuleSpec, *, profile: Profile | None = None,
    check_planner: CheckPlanner | None = None,
) -> str:
    base = profile or rule
    if check_planner is None:
        return digest(base)
    return digest({
        "base": base,
        "check_planner": {
            "planner_id": check_planner.planner_id,
            "planner_version": check_planner.planner_version,
        },
    })


def decision_input_digest(
    *, task: dict[str, Any], snapshot: dict[str, Any],
    candidate: CandidateKey | CandidateIdentity,
    facts: list[FactAssessment], checks: list[dict[str, Any]],
    claim: Claim, assessment: Assessment,
) -> str:
    """Stable digest expected from an external RuleEvaluator decision."""
    return digest({
        "task": task,
        "snapshot": snapshot,
        "candidate": candidate,
        "facts": facts,
        "checks": checks,
        "claim": claim,
        "assessment": assessment,
    })


def _ref(value: dict[str, Any] | EvidenceRef) -> EvidenceRef:
    return value if isinstance(value, EvidenceRef) else EvidenceRef(**value)


def _coverage(value: dict[str, Any] | Coverage) -> Coverage:
    return (
        value
        if isinstance(value, Coverage)
        else Coverage(
            **{
                **value,
                "omissions": tuple(value.get("omissions", ())),
                "basis_refs": tuple(value.get("basis_refs", ())),
            }
        )
    )


def _fact(value: dict[str, Any]) -> FactAssessment:
    return FactAssessment(
        **{
            **value,
            "evidence_refs": tuple(_ref(item) for item in value["evidence_refs"]),
        }
    )


def _decision(value: dict[str, Any]) -> RuleDecision:
    return RuleDecision(
        **{
            field: (
                tuple(_ref(item) for item in value[field])
                if field in {"support_refs", "counter_refs"}
                else tuple(value[field])
                if field in {"satisfied_obligations", "failed_obligations", "blockers"}
                else value[field]
            )
            for field in RuleDecision.__dataclass_fields__
        }
    )


def _candidate(value: dict[str, Any]) -> CandidateKey | CandidateIdentity:
    if "identity" in value:
        return CandidateIdentity(**value)
    return CandidateKey(
        **{
            **value,
            "source_location": SourceLocation(**value["source_location"]),
            "sink_location": SourceLocation(**value["sink_location"]),
        }
    )


def _candidate_scope(candidate: CandidateKey | CandidateIdentity) -> str:
    return candidate.scope if isinstance(candidate, CandidateIdentity) else candidate.path_identity


def _check_definition(value: dict[str, Any]) -> CheckDefinition:
    return CheckDefinition(
        value["check_id"], value["question"],
        tuple(value["accepted_predicates"]),
    )


def _rule(value: dict[str, Any]) -> RuleSpec:
    return RuleSpec(
        value["rule_id"], value["rule_version"], value["title"],
        tuple(_check_definition(item) for item in value["required_checks"]),
        value["evaluator_id"], value["evaluator_version"],
    )


class NullReturnEvaluator:
    """Conservative first rule: accepts only scoped, observed predicate facts."""

    evaluator_id = "null-return-v1"
    evaluator_version = "1.0"

    def evaluate(
        self,
        *,
        task: dict[str, Any],
        snapshot: dict[str, Any],
        candidate: CandidateKey,
        facts: list[FactAssessment],
        checks: list[dict[str, Any]],
        claim: Claim,
        assessment: Assessment,
    ) -> RuleDecision:
        scope = claim.scope
        blockers: list[str] = []
        if snapshot["base_build_digest"] != snapshot["head_build_digest"]:
            blockers.append("Base/Head build configurations differ")
        if scope != candidate.path_identity:
            blockers.append("claim scope differs from candidate path")
        if assessment.scope != scope:
            blockers.append("claim and verification scopes differ")
        if not set(CHECK_NAMES).issubset(assessment.checked_predicates):
            blockers.append("verifier did not check every required predicate")
        if claim.unresolved_items or assessment.unresolved_items:
            blockers.append("roles report unresolved items")
        check_map = {item["check_id"]: item for item in checks}
        claimed_names = {
            fact.check_id for fact in facts if fact.fact_id in claim.fact_ids
        }
        if not set(CHECK_NAMES).issubset(claimed_names):
            blockers.append("claim does not address every required check")
        for name in CHECK_NAMES:
            item = check_map.get(name)
            if not item or item["status"] != "complete":
                blockers.append(f"{name} is not complete")
            elif _coverage(item["coverage"]).completeness != "complete":
                blockers.append(f"{name} lacks complete scoped coverage")
        observed: dict[str, set[str]] = {}
        fact_refs: dict[str, list[EvidenceRef]] = {}
        for fact in facts:
            if (
                fact.scope != scope
                or fact.confidence_class != "tool_observed"
                or fact.limitations
            ):
                continue
            observed.setdefault(fact.predicate_kind, set()).add(fact.polarity)
            fact_refs.setdefault(fact.predicate_kind, []).extend(fact.evidence_refs)
        for name, polarities in observed.items():
            if "positive" in polarities and "negative" in polarities:
                blockers.append(f"conflicting facts: {name}")
        if "positive" in observed.get("guard", set()) and "positive" in observed.get(
            "reachability", set()
        ):
            blockers.append("guard and reachability facts conflict on the same path")
        if any(name not in observed for name in CHECK_NAMES):
            blockers.extend(
                f"no observed fact: {name}"
                for name in CHECK_NAMES
                if name not in observed
            )
        payload_digest = decision_input_digest(
            task=task, snapshot=snapshot, candidate=candidate, facts=facts,
            checks=checks, claim=claim, assessment=assessment,
        )
        if blockers:
            return RuleDecision(
                "inconclusive",
                scope,
                (),
                (),
                (),
                (),
                tuple(blockers),
                self.evaluator_id,
                self.evaluator_version,
                payload_digest,
            )
        required_positive = (
            "nullable_source",
            "propagation",
            "dangerous_use",
            "reachability",
            "change_attribution",
        )
        refuted_by = [
            name
            for name in ("guard", "propagation", "reachability")
            if (name == "guard" and "positive" in observed[name])
            or (name != "guard" and "negative" in observed[name])
        ]
        if refuted_by:
            refs = tuple(
                dict.fromkeys(ref for name in refuted_by for ref in fact_refs[name])
            )
            return RuleDecision(
                "refuted",
                scope,
                tuple(refuted_by),
                (),
                (),
                refs,
                (),
                self.evaluator_id,
                self.evaluator_version,
                payload_digest,
            )
        missing = [
            name for name in required_positive if "positive" not in observed[name]
        ]
        if "negative" not in observed["guard"]:
            missing.append("guard absence")
        if missing:
            return RuleDecision(
                "inconclusive",
                scope,
                (),
                tuple(missing),
                (),
                (),
                tuple(f"unproven: {name}" for name in missing),
                self.evaluator_id,
                self.evaluator_version,
                payload_digest,
            )
        refs = tuple(
            dict.fromkeys(ref for name in CHECK_NAMES for ref in fact_refs[name])
        )
        return RuleDecision(
            "confirmed",
            scope,
            tuple(CHECK_NAMES),
            (),
            refs,
            (),
            (),
            self.evaluator_id,
            self.evaluator_version,
            payload_digest,
        )


class DefectRuntime:
    def __init__(
        self, store: SQLiteStore, *, evaluator: RuleEvaluator | None = None
    ) -> None:
        self.store = store
        initial = evaluator or NullReturnEvaluator()
        self.evaluator = initial  # Compatibility with the 0.1.0 single-rule API.
        self._evaluators: dict[tuple[str, str], RuleEvaluator] = {}
        self._check_planners: dict[tuple[str, str, str, str], CheckPlanner] = {}
        self.register_evaluator(initial)

    def register_evaluator(self, evaluator: RuleEvaluator) -> None:
        key = (evaluator.evaluator_id, evaluator.evaluator_version)
        previous = self._evaluators.get(key)
        if previous is not None and previous is not evaluator:
            raise Conflict("evaluator ID and version are already registered")
        self._evaluators[key] = evaluator

    def register_check_planner(
        self, rule_id: str, rule_version: str, planner: CheckPlanner
    ) -> None:
        if not all((rule_id, rule_version, planner.planner_id,
                    planner.planner_version)):
            raise InvalidInput("planner registration needs rule and version")
        key = (rule_id, rule_version, planner.planner_id, planner.planner_version)
        previous = self._check_planners.get(key)
        if previous is not None and previous is not planner:
            raise Conflict("check planner ID and version are already registered")
        self._check_planners[key] = planner

    def _evaluator_for(self, analysis: dict[str, Any]) -> RuleEvaluator:
        rule = analysis["rule"]
        key = (rule["evaluator_id"], rule["evaluator_version"])
        try:
            return self._evaluators[key]
        except KeyError as exc:
            raise CapabilityUnavailable(
                "rule evaluator is not registered at the required version"
            ) from exc

    def create_analysis(
        self,
        snapshot: AnalysisSnapshot | FixedSnapshot,
        rule: RuleSpec,
        *,
        profile: Profile | None = None,
        check_planner: CheckPlanner | None = None,
        analysis_id: str | None = None,
    ) -> str:
        if snapshot.rule_profile_digest != analysis_profile_digest(
            rule, profile=profile, check_planner=check_planner
        ):
            raise InvalidInput("snapshot profile digest does not match rule")
        if profile is not None:
            if ((profile.rule_id, profile.rule_version)
                    != (rule.rule_id, rule.rule_version)
                    or profile.tool_policy_digest != snapshot.tool_policy_digest
                    or any(check not in {item.check_id for item in rule.required_checks}
                           for role in profile.roles for check in role.focus_check_ids)):
                raise InvalidInput("profile differs from rule, checks or tool policy")
        if (rule.evaluator_id, rule.evaluator_version) not in self._evaluators:
            raise CapabilityUnavailable(
                "rule evaluator is not registered at the required version"
            )
        if check_planner is not None:
            self.register_check_planner(rule.rule_id, rule.rule_version, check_planner)
        planner_ref = (
            {"planner_id": check_planner.planner_id,
             "planner_version": check_planner.planner_version}
            if check_planner else None
        )
        analysis_id = analysis_id or _new_id()
        previous = self.store.get("analysis", analysis_id)
        record = {
            "analysis_id": analysis_id,
            "snapshot": to_plain(snapshot),
            "snapshot_digest": snapshot.snapshot_digest,
            "rule": to_plain(rule),
            "profile": to_plain(profile) if profile else None,
            "check_planner": planner_ref,
            "status": "created",
        }
        if previous:
            if (previous["snapshot_digest"] != snapshot.snapshot_digest
                    or previous["rule"] != to_plain(rule)
                    or previous.get("profile") != record["profile"]
                    or previous.get("check_planner") != planner_ref):
                raise Conflict("analysis ID already binds another snapshot or rule")
            return analysis_id
        self.store.append(
            analysis_id,
            Mutation(
                "AnalysisCreated",
                {
                    "snapshot_digest": snapshot.snapshot_digest,
                    "profile_digest": snapshot.rule_profile_digest,
                },
                "analysis",
                analysis_id,
                record,
            ),
        )
        return analysis_id

    def analysis(self, analysis_id: str) -> dict[str, Any]:
        result = self.store.get("analysis", analysis_id)
        if result is None:
            raise InvalidInput("analysis does not exist")
        return result

    def role_kind(self, analysis_id: str, role_id: str) -> str:
        profile = self.analysis(analysis_id).get("profile")
        if profile is None:
            if role_id in {"investigator", "verifier"}:
                return role_id
        else:
            for role in profile["roles"]:
                if role["role_id"] == role_id:
                    return role["kind"]
        raise InvalidInput("role is not registered in the analysis profile")

    def propose_provisional(
        self,
        analysis_id: str,
        *,
        source_location: SourceLocation,
        sink_location: SourceLocation | None,
        discovery_ref: str,
        unresolved_identity: tuple[str, ...],
    ) -> ProvisionalCandidate:
        analysis = self.analysis(analysis_id)
        snapshot = analysis["snapshot"]
        for location in (source_location, sink_location):
            if location is None:
                continue
            expected = (
                snapshot["base_commit"]
                if location.side == "base"
                else snapshot["head_commit"]
            )
            if location.commit != expected:
                raise StaleSnapshot("provisional location commit differs from snapshot")
        proposal = ProvisionalCandidate(
            _new_id(),
            analysis_id,
            analysis["rule"]["rule_id"],
            analysis["rule"]["rule_version"],
            source_location,
            sink_location,
            discovery_ref,
            unresolved_identity,
        )
        self.store.append(
            analysis_id,
            Mutation(
                "CandidateProposed",
                {
                    "discovery_ref": discovery_ref,
                    "provisional_fields": to_plain(proposal),
                    "unresolved_identity": unresolved_identity,
                },
                "proposal",
                proposal.proposal_id,
                to_plain(proposal),
            ),
        )
        return proposal

    def propose_from_material(
        self, analysis_id: str, material: dict[str, Any],
        policy: CandidateIdentityPolicy, *, discovery_ref: str,
    ) -> CandidateTask | GenericProvisionalCandidate:
        analysis = self.analysis(analysis_id)
        rule = analysis["rule"]
        if not discovery_ref:
            raise InvalidInput("candidate needs discovery source")
        identified = policy.identify(
            snapshot_digest=analysis["snapshot_digest"],
            rule_id=rule["rule_id"],
            rule_version=rule["rule_version"],
            material=material,
        )
        if isinstance(identified, CandidateIdentity):
            return self.propose_candidate(
                analysis_id, identified, discovery_ref=discovery_ref
            )
        if not isinstance(identified, CandidateIdentityDraft):
            raise InvalidInput("identity policy returned an unsupported result")
        proposal = GenericProvisionalCandidate(
            _new_id(), analysis_id, rule["rule_id"], rule["rule_version"],
            analysis["snapshot_digest"], identified.identity, identified.scope,
            discovery_ref, identified.unresolved_identity,
        )
        self.store.append(
            analysis_id,
            Mutation(
                "CandidateProposed",
                {
                    "discovery_ref": discovery_ref,
                    "provisional_fields": to_plain(proposal),
                    "unresolved_identity": proposal.unresolved_identity,
                },
                "proposal", proposal.proposal_id, to_plain(proposal),
            ),
        )
        return proposal

    def bind_provisional(
        self, proposal_id: str, key: CandidateKey | CandidateIdentity
    ) -> CandidateTask:
        raw = self.store.get("proposal", proposal_id)
        if raw is None:
            raise InvalidInput("provisional candidate does not exist")
        if (raw["rule_id"], raw["rule_version"]) != (key.rule_id, key.rule_version):
            raise InvalidInput("bound rule differs from proposal")
        if "identity" in raw:
            if not isinstance(key, CandidateIdentity):
                raise InvalidInput("generic proposal needs a generic identity")
            if (key.snapshot_digest != raw["snapshot_digest"]
                    or key.scope != raw["scope"]
                    or any(key.identity.get(name) != value
                           for name, value in raw["identity"].items())
                    or any(name not in key.identity
                           for name in raw["unresolved_identity"])):
                raise InvalidInput("bound identity does not resolve this proposal")
            return self.propose_candidate(
                raw["analysis_id"], key, discovery_ref=raw["discovery_ref"],
                source_proposal_ids=(proposal_id,),
            )
        if not isinstance(key, CandidateKey):
            raise InvalidInput("legacy proposal needs a legacy CandidateKey")
        if raw["source_location"] != to_plain(key.source_location):
            raise InvalidInput("bound source differs from proposal")
        if raw["sink_location"] is not None and raw["sink_location"] != to_plain(
            key.sink_location
        ):
            raise InvalidInput("bound sink differs from proposal")
        return self.propose_candidate(
            raw["analysis_id"],
            key,
            discovery_ref=raw["discovery_ref"],
            source_proposal_ids=(proposal_id,),
        )

    def propose_candidate(
        self,
        analysis_id: str,
        key: CandidateKey | CandidateIdentity,
        *,
        discovery_ref: str,
        source_proposal_ids: tuple[str, ...] = (),
        origins: tuple[DiscoveryOrigin, ...] = (),
    ) -> CandidateTask:
        analysis = self.analysis(analysis_id)
        snapshot = analysis["snapshot"]
        rule = analysis["rule"]
        if (key.rule_id, key.rule_version) != (rule["rule_id"], rule["rule_version"]):
            raise InvalidInput("candidate rule differs from analysis")
        if isinstance(key, CandidateIdentity):
            if key.snapshot_digest != analysis["snapshot_digest"]:
                raise StaleSnapshot("candidate identity differs from fixed snapshot")
        else:
            if "base_commit" not in snapshot:
                raise InvalidInput("legacy CandidateKey requires a Base/Head snapshot")
            if key.build_variant_id != snapshot["build_variant_id"]:
                raise StaleSnapshot("candidate build variant differs from snapshot")
            for location in (key.source_location, key.sink_location):
                expected = (
                    snapshot["base_commit"]
                    if location.side == "base"
                    else snapshot["head_commit"]
                )
                if location.commit != expected:
                    raise StaleSnapshot("candidate location commit differs from snapshot")
        if not discovery_ref:
            raise InvalidInput("candidate needs discovery source")
        origin_values = tuple(origins) or (DiscoveryOrigin(
            "legacy.unattributed",
            "1",
            key.candidate_digest,
            discovery_ref,
            digest({"candidate": to_plain(key), "discovery_ref": discovery_ref}),
            (),
        ),)
        if any(not isinstance(origin, DiscoveryOrigin) for origin in origin_values):
            raise InvalidInput("candidate origins must be discovery origins")
        required_definitions = tuple(
            _check_definition(item) for item in rule["required_checks"]
        )
        planner_ref = analysis.get("check_planner")
        if planner_ref:
            planner_key = (
                rule["rule_id"], rule["rule_version"],
                planner_ref["planner_id"], planner_ref["planner_version"],
            )
            try:
                planner = self._check_planners[planner_key]
            except KeyError as exc:
                raise CapabilityUnavailable("analysis check planner is unavailable") from exc
            definitions = tuple(planner.plan(_rule(rule), key))
        else:
            definitions = required_definitions
        if not definitions or any(
            not isinstance(item, CheckDefinition) for item in definitions
        ):
            raise InvalidInput("check planner returned invalid definitions")
        ids = [item.check_id for item in definitions]
        if len(ids) != len(set(ids)):
            raise InvalidInput("check planner returned duplicate definitions")
        planned = {item.check_id: item for item in definitions}
        if any(planned.get(item.check_id) != item for item in required_definitions):
            raise InvalidInput("check planner omitted or changed a required check")
        task_id = _new_id()
        task = CandidateTask(
            task_id,
            analysis_id,
            analysis["snapshot_digest"],
            key.candidate_digest,
            tuple(ids),
        )
        mutations = []
        if analysis["status"] == "created":
            mutations.append(
                Mutation(
                    "AnalysisStatusChanged",
                    {
                        "old_status": "created",
                        "new_status": "active",
                        "reason": "first precise candidate",
                    },
                    "analysis",
                    analysis_id,
                    {**analysis, "status": "active"},
                )
            )
        mutations.extend(
            [
                Mutation(
                    "CandidateBound",
                    {
                        "candidate_digest": key.candidate_digest,
                        "source_proposal_ids": source_proposal_ids,
                        "required_check_ids": task.required_check_ids,
                    },
                    "candidate",
                    key.candidate_digest,
                    to_plain(key),
                    task_id,
                ),
                Mutation(
                    "TaskCreated",
                    {
                        "task_id": task_id,
                        "candidate_digest": key.candidate_digest,
                        "required_check_ids": task.required_check_ids,
                        "discovery_ref": discovery_ref,
                    },
                    "task",
                    task_id,
                    {**to_plain(task), "check_definitions": to_plain(definitions)},
                    task_id,
                ),
            ]
        )
        for definition in definitions:
            item = CheckItem(
                definition.check_id,
                task_id,
                definition.question,
                "unexamined",
                "unknown",
                (),
                Coverage("", "", "unknown"),
            )
            mutations.append(
                Mutation(
                    "CheckCreated",
                    {"check_id": item.check_id},
                    "check",
                    f"{task_id}:{item.check_id}",
                    to_plain(item),
                    task_id,
                )
            )
        origin_records = tuple(
            (
                digest({
                    "analysis_id": analysis_id,
                    "candidate_digest": key.candidate_digest,
                    "discoverer_id": origin.discoverer_id,
                    "discoverer_version": origin.discoverer_version,
                    "origin_candidate_id": origin.origin_candidate_id,
                    "discovery_ref": origin.discovery_ref,
                }),
                {
                    "analysis_id": analysis_id,
                    "candidate_digest": key.candidate_digest,
                    "identity_digest": digest(to_plain(key)),
                    "origin": to_plain(origin),
                    **to_plain(origin),
                },
            )
            for origin in origin_values
        )
        actual_task_id = self.store.reserve_candidate(
            analysis_id,
            candidate_digest=key.candidate_digest,
            task_id=task_id,
            mutations=mutations,
            origin_records=origin_records,
        )
        if actual_task_id != task_id:
            prior = self.task(actual_task_id)
            return CandidateTask(
                **{field: prior[field] for field in CandidateTask.__dataclass_fields__}
            )
        return task

    def task(self, task_id: str) -> dict[str, Any]:
        task = self.store.get("task", task_id)
        if task is None:
            raise InvalidInput("task does not exist")
        return task

    def checks(self, task_id: str) -> list[dict[str, Any]]:
        task = self.task(task_id)
        return [
            self.store.get("check", f"{task_id}:{name}")
            for name in task["required_check_ids"]
        ]

    def record_plan_selection(
        self,
        task_id: str,
        *,
        selection: EvidencePlanSelection,
        planner_id: str,
        planner_version: str,
        check_id: str,
        operation: str,
        query_idempotency_key: str,
    ) -> None:
        """Persist a planner's exact choice before its backend request exists."""
        if not all((planner_id, planner_version, check_id, operation, query_idempotency_key)):
            raise InvalidInput("plan selection needs planner, plan and query binding")
        task = self.task(task_id)
        selection_id = digest({
            "task_id": task_id,
            "plan_digest": selection.plan_digest,
        })
        record = {
            "selection_id": selection_id,
            "task_id": task_id,
            "plan_digest": selection.plan_digest,
            "reason": selection.reason,
            "planner_id": planner_id,
            "planner_version": planner_version,
            "check_id": check_id,
            "operation": operation,
            "query_idempotency_key": query_idempotency_key,
        }
        previous = self.store.get("plan_selection", selection_id)
        if previous is not None:
            if previous != record:
                raise Conflict("plan selection ID is already bound differently")
            return
        self.store.append(
            task["analysis_id"],
            Mutation(
                "EvidencePlanSelected",
                {
                    "selection_id": selection_id,
                    "plan_digest": selection.plan_digest,
                    "planner_id": planner_id,
                    "planner_version": planner_version,
                    "check_id": check_id,
                    "operation": operation,
                    "reason": selection.reason,
                    "query_idempotency_key": query_idempotency_key,
                },
                "plan_selection",
                selection_id,
                record,
                task_id,
            ),
        )

    def record_planning_stopped(
        self,
        task_id: str,
        *,
        planner_id: str,
        planner_version: str,
        reason: str,
        pending_plan_digests: tuple[str, ...],
    ) -> None:
        if not planner_id or not planner_version or not reason or len(reason) > 500:
            raise InvalidInput("planning stop needs planner identity and reason")
        if (
            not pending_plan_digests
            or tuple(sorted(pending_plan_digests)) != pending_plan_digests
            or len(set(pending_plan_digests)) != len(pending_plan_digests)
            or any(
                len(value) != 64
                or any(character not in "0123456789abcdef" for character in value)
                for value in pending_plan_digests
            )
        ):
            raise InvalidInput("planning stop needs sorted pending plan digests")
        task = self.task(task_id)
        record_id = digest({
            "task_id": task_id,
            "planner_id": planner_id,
            "planner_version": planner_version,
            "pending": pending_plan_digests,
        })
        record = {
            "task_id": task_id,
            "planner_id": planner_id,
            "planner_version": planner_version,
            "reason": reason,
            "pending_plan_digests": list(pending_plan_digests),
        }
        previous = self.store.get("planning_stop", record_id)
        if previous is not None:
            if previous != record:
                raise Conflict("planning stop is already bound differently")
            return
        self.store.append(task["analysis_id"], Mutation(
            "EvidencePlanningStopped",
            {
                "planner_id": planner_id,
                "planner_version": planner_version,
                "reason": reason,
                "pending_plan_digests": list(pending_plan_digests),
            },
            "planning_stop", record_id, record, task_id,
        ))

    def query_program(
        self, request: QueryRequest, backend: ProgramQuery
    ) -> tuple[QueryOutcome, EvidenceRef | None]:
        task = self.task(request.task_id)
        analysis = self.analysis(task["analysis_id"])
        if request.snapshot_digest != task["snapshot_digest"]:
            raise StaleSnapshot("query snapshot differs from task")
        if request.tool_policy_digest != analysis["snapshot"]["tool_policy_digest"]:
            raise PolicyDenied("query tool policy differs from analysis")
        if request.check_id not in task["required_check_ids"]:
            raise InvalidInput("query check does not belong to task")
        if (
            request.backend_id != backend.backend_id
            or request.backend_version != backend.backend_version
        ):
            raise CapabilityUnavailable("requested backend version unavailable")
        if (
            request.operation not in backend.supported_operations
            or not backend.read_only
        ):
            raise CapabilityUnavailable(
                "query operation is unavailable or not read-only"
            )
        try:
            if "scope_id" in analysis["snapshot"]:
                scope = StructuredQueryScope(**request.scope)
                if scope.scope_id != analysis["snapshot"]["scope_id"]:
                    raise StaleSnapshot("query scope differs from analysis")
                operations = getattr(backend, "operation_specs", None)
                operation = (
                    operations.get(request.operation)
                    if isinstance(operations, dict) else None
                )
                if (not isinstance(operation, QueryOperation)
                        or operation.name != request.operation):
                    raise CapabilityUnavailable(
                        "generic backend lacks an operation schema"
                    )
                operation.validate(request.args, scope.selectors)
            else:
                scope = QueryScope(**request.scope)
                if scope.build_variant_id != analysis["snapshot"]["build_variant_id"]:
                    raise StaleSnapshot("query scope build variant differs from analysis")
        except TypeError as exc:
            raise InvalidInput("query scope does not match snapshot scope schema") from exc
        if request.timeout_ms <= 0:
            raise InvalidInput("query needs positive timeout")
        analysis_id = task["analysis_id"]
        requested_record = {
            "request": to_plain(request),
            "request_digest": request.request_digest,
            "status": "requested",
            "outcome": None,
            "evidence_id": None,
        }
        original_id, created = self.store.reserve_query(
            analysis_id,
            key=request.idempotency_key,
            request_digest=request.request_digest,
            query_id=request.query_id,
            request_record={"check_id": request.check_id, **requested_record},
            task_id=request.task_id,
        )
        existing = self.store.get("query", original_id)
        assert existing is not None
        if existing["status"] in {
            "complete",
            "partial",
            "timeout",
            "failed",
            "unsupported",
            "denied",
        }:
            outcome = QueryOutcome(
                **{
                    **existing["outcome"],
                    "coverage": _coverage(existing["outcome"]["coverage"]),
                    "limitations": tuple(existing["outcome"]["limitations"]),
                }
            )
            ref = (
                self.evidence_ref(existing["evidence_id"])
                if existing["evidence_id"]
                else None
            )
            return outcome, ref
        if original_id != request.query_id:
            request = replace(request, query_id=original_id)
        if not created and existing["status"] in {"running", "requested"}:
            raise Conflict("query is in progress")
        if existing["status"] == "in_doubt" and not backend.idempotent_retry:
            raise BackendFailure("in-doubt query cannot be retried safely")
        running = {**existing, "status": "running"}
        self.store.append(
            analysis_id,
            Mutation(
                "QueryStarted",
                {"query_id": request.query_id},
                "query",
                request.query_id,
                running,
                request.task_id,
            ),
        )
        try:
            result = backend.query(request)
        except Exception as exc:  # noqa: BLE001 - backend failures are recorded as QueryOutcome.
            result = ProgramResult(
                "failed",
                Coverage("", "", "unknown"),
                diagnostic=f"{type(exc).__name__}: {exc}",
            )
        if result.status in {"complete", "partial"} and result.raw is None:
            raise BackendFailure("backend claimed success without raw result")
        raw_digest = (
            self.store.put_artifact(result.raw) if result.raw is not None else None
        )
        if result.status == "complete" and raw_digest not in result.coverage.basis_refs:
            raise BackendFailure(
                "complete coverage must cite its saved raw result as a basis"
            )
        outcome = QueryOutcome(
            request.query_id,
            result.status,
            backend.backend_id,
            backend.backend_version,
            request.snapshot_digest,
            request.request_digest,
            result.coverage,
            result.limitations,
            raw_digest,
            result.diagnostic,
        )
        evidence_id = (
            _new_id()
            if raw_digest is not None and result.status in {"complete", "partial"}
            else None
        )
        finished = {
            **running,
            "status": outcome.status,
            "outcome": to_plain(outcome),
            "evidence_id": evidence_id,
        }
        mutations = [
            Mutation(
                "QueryFinished",
                {
                    "query_id": request.query_id,
                    "status": outcome.status,
                    "coverage": to_plain(outcome.coverage),
                    "raw_artifact_digest": raw_digest,
                },
                "query",
                request.query_id,
                finished,
                request.task_id,
            )
        ]
        if evidence_id:
            evidence = {
                "evidence_id": evidence_id,
                "analysis_id": analysis_id,
                "task_id": request.task_id,
                "query_id": request.query_id,
                "snapshot_digest": request.snapshot_digest,
                "artifact_digest": raw_digest,
                "coverage": to_plain(result.coverage),
                "backend_id": backend.backend_id,
                "backend_version": backend.backend_version,
            }
            mutations.append(
                Mutation(
                    "EvidenceRecorded",
                    {
                        "evidence_id": evidence_id,
                        "query_id": request.query_id,
                        "artifact_digest": raw_digest,
                        "snapshot_digest": request.snapshot_digest,
                    },
                    "evidence",
                    evidence_id,
                    evidence,
                    request.task_id,
                )
            )
        self.store.append(analysis_id, *mutations)
        return outcome, self.evidence_ref(evidence_id) if evidence_id else None

    def recover_in_doubt(self, analysis_id: str) -> list[str]:
        changed = []
        for query in self.store.list("query", analysis_id):
            if query["status"] not in {"requested", "running"}:
                continue
            request = query["request"]
            active = [
                binding
                for binding in self.store.list("binding", analysis_id)
                if binding["task_id"] == request["task_id"]
                and (
                    row := self.store.connection.execute(
                        "SELECT expires_at FROM leases WHERE binding_id=?",
                        (binding["binding_id"],),
                    ).fetchone()
                )
                is not None
                and row["expires_at"] > time.time()
            ]
            if active:
                raise Conflict(
                    "cannot recover a query while its role writer has an active lease"
                )
            self.store.append(
                analysis_id,
                Mutation(
                    "QueryInDoubt",
                    {
                        "query_id": request["query_id"],
                        "reason": "missing terminal event",
                    },
                    "query",
                    request["query_id"],
                    {**query, "status": "in_doubt"},
                    request["task_id"],
                ),
            )
            changed.append(request["query_id"])
        return changed

    def evidence_ref(
        self, evidence_id: str | None, role: str = "support"
    ) -> EvidenceRef:
        if evidence_id is None:
            raise InvalidInput("evidence ID is required")
        evidence = self.store.get("evidence", evidence_id)
        if evidence is None:
            raise EvidenceIntegrityError("unknown evidence ID")
        self.store.read_artifact(evidence["artifact_digest"])
        return EvidenceRef(
            evidence_id, evidence["artifact_digest"], evidence["snapshot_digest"], role
        )

    def validate_ref(self, task_id: str, ref: EvidenceRef) -> None:
        task = self.task(task_id)
        evidence = self.store.get("evidence", ref.evidence_id)
        if not evidence or evidence["analysis_id"] != task["analysis_id"]:
            raise EvidenceIntegrityError("evidence is unknown or outside analysis")
        if (
            evidence["snapshot_digest"] != task["snapshot_digest"]
            or ref.snapshot_digest != task["snapshot_digest"]
        ):
            raise StaleSnapshot("evidence snapshot differs from task")
        if evidence["artifact_digest"] != ref.artifact_digest:
            raise EvidenceIntegrityError("evidence digest differs from reference")
        if evidence["task_id"] != task_id:
            raise EvidenceIntegrityError(
                "evidence has not been scoped to this candidate"
            )
        self.store.read_artifact(ref.artifact_digest)

    def record_fact(self, fact: FactAssessment) -> None:
        task = self.task(fact.task_id)
        if fact.check_id not in task["required_check_ids"]:
            raise InvalidInput("fact check does not belong to task")
        analysis = self.analysis(task["analysis_id"])
        definition = next(
            item
            for item in task.get("check_definitions",
                                 analysis["rule"]["required_checks"])
            if item["check_id"] == fact.check_id
        )
        if fact.predicate_kind not in definition["accepted_predicates"]:
            raise InvalidInput("fact predicate is not accepted by this check")
        if not fact.evidence_refs:
            raise InvalidInput("fact needs original evidence")
        for ref in fact.evidence_refs:
            self.validate_ref(fact.task_id, ref)
        if self.store.get("fact", fact.fact_id):
            raise Conflict("fact ID already exists")
        self.store.append(
            task["analysis_id"],
            Mutation(
                "FactRecorded",
                {
                    "fact_id": fact.fact_id,
                    "check_id": fact.check_id,
                    "predicate_kind": fact.predicate_kind,
                    "polarity": fact.polarity,
                    "scope": fact.scope,
                    "evidence_refs": to_plain(fact.evidence_refs),
                },
                "fact",
                fact.fact_id,
                to_plain(fact),
                fact.task_id,
            ),
        )

    def update_check(self, item: CheckItem) -> None:
        task = self.task(item.task_id)
        previous = self.store.get("check", f"{item.task_id}:{item.check_id}")
        if previous is None:
            raise InvalidInput("check does not exist")
        if item.status not in {
            "in_progress",
            "complete",
            "partial",
            "failed",
            "unsupported",
        }:
            raise InvalidInput("invalid check status")
        if item.status == "complete":
            if (
                item.answer not in {"supported", "refuted"}
                or item.coverage.completeness != "complete"
            ):
                raise InvalidInput(
                    "complete check needs supported/refuted answer and complete coverage"
                )
            if not item.evidence_refs:
                raise InvalidInput("complete check needs original evidence")
        for ref in item.evidence_refs:
            self.validate_ref(item.task_id, ref)
        if item.status == "complete" and not any(
            self.store.get("evidence", ref.evidence_id)["coverage"]["completeness"]
            == "complete"
            for ref in item.evidence_refs
        ):
            raise InvalidInput("complete check needs complete backend evidence")
        if item.status == "complete" and not any(
            set(item.coverage.basis_refs)
            & set(self.store.get("evidence", ref.evidence_id)["coverage"]["basis_refs"])
            for ref in item.evidence_refs
        ):
            raise InvalidInput("check coverage basis is not backed by its evidence")
        self.store.append(
            task["analysis_id"],
            Mutation(
                "CheckUpdated",
                {
                    "check_id": item.check_id,
                    "prior_status": previous["status"],
                    "new_status": item.status,
                    "answer": item.answer,
                    "evidence_refs": to_plain(item.evidence_refs),
                    "limitations": item.limitations,
                    "prior_unknowns": previous["unknowns"],
                    "new_unknowns": item.unknowns,
                },
                "check",
                f"{item.task_id}:{item.check_id}",
                to_plain(item),
                item.task_id,
            ),
        )

    def record_claim(self, claim: Claim) -> None:
        task = self.task(claim.task_id)
        attempt = self.store.get("attempt", claim.attempt_id)
        if (
            not attempt
            or attempt["task_id"] != claim.task_id
            or self.role_kind(task["analysis_id"], attempt["role"])
            != "investigator"
        ):
            raise InvalidInput("claim requires investigator attempt")
        for ref in (*claim.support_refs, *claim.counter_refs):
            self.validate_ref(claim.task_id, ref)
        for fact_id in claim.fact_ids:
            fact = self.store.get("fact", fact_id)
            if not fact or fact["task_id"] != claim.task_id:
                raise InvalidInput("claim refers to unrelated fact")
        if self.store.get("claim", claim.claim_id):
            raise Conflict("claim ID already exists")
        self.store.append(
            task["analysis_id"],
            Mutation(
                "ClaimRecorded",
                {
                    "claim_id": claim.claim_id,
                    "attempt_id": claim.attempt_id,
                    "record_digest": digest(claim),
                },
                "claim",
                claim.claim_id,
                to_plain(claim),
                claim.task_id,
                claim.attempt_id,
            ),
        )

    def record_assessment(self, assessment: Assessment) -> None:
        claim = self.store.get("claim", assessment.claim_id)
        attempt = self.store.get("attempt", assessment.verifier_attempt_id)
        if not claim or claim["task_id"] != assessment.task_id:
            raise InvalidInput("assessment needs same-task claim")
        if (
            not attempt
            or attempt["task_id"] != assessment.task_id
            or self.role_kind(self.task(assessment.task_id)["analysis_id"], attempt["role"])
            != "verifier"
        ):
            raise InvalidInput("assessment needs independent verifier attempt")
        if assessment.verifier_attempt_id == claim["attempt_id"]:
            raise InvalidInput("verifier cannot reuse investigator attempt")
        for ref in (*assessment.support_refs, *assessment.counter_refs):
            self.validate_ref(assessment.task_id, ref)
        if self.store.get("assessment", assessment.assessment_id):
            raise Conflict("assessment ID already exists")
        task = self.task(assessment.task_id)
        self.store.append(
            task["analysis_id"],
            Mutation(
                "AssessmentRecorded",
                {
                    "assessment_id": assessment.assessment_id,
                    "attempt_id": assessment.verifier_attempt_id,
                    "record_digest": digest(assessment),
                },
                "assessment",
                assessment.assessment_id,
                to_plain(assessment),
                assessment.task_id,
                assessment.verifier_attempt_id,
            ),
        )

    def record_specialist_note(self, note: SpecialistNote) -> None:
        task = self.task(note.task_id)
        attempt = self.store.get("attempt", note.attempt_id)
        if (not attempt or attempt["task_id"] != note.task_id
                or attempt["role"] != note.role
                or self.role_kind(task["analysis_id"], note.role) != "specialist"):
            raise InvalidInput("specialist note needs a registered specialist attempt")
        for ref in (*note.support_refs, *note.counter_refs):
            self.validate_ref(note.task_id, ref)
        if self.store.get("specialist_note", note.note_id):
            raise Conflict("specialist note ID already exists")
        self.store.append(
            task["analysis_id"],
            Mutation(
                "SpecialistNoteRecorded",
                {"note_id": note.note_id, "attempt_id": note.attempt_id,
                 "record_digest": digest(note)},
                "specialist_note", note.note_id, to_plain(note),
                note.task_id, note.attempt_id,
            ),
        )

    def evaluate_candidate(
        self, task_id: str, claim_id: str, assessment_id: str
    ) -> RuleDecision:
        task = self.task(task_id)
        analysis = self.analysis(task["analysis_id"])
        if task["phase"] == "terminal":
            for prior in self.store.list("verdict", task["analysis_id"]):
                if (prior["task_id"] == task_id
                        and prior.get("claim_id") == claim_id
                        and prior.get("assessment_id") == assessment_id):
                    return _decision(prior)
            raise Conflict("terminal candidate requires an explicit reopen")
        raw_claim = self.store.get("claim", claim_id)
        raw_assessment = self.store.get("assessment", assessment_id)
        if (
            not raw_claim
            or not raw_assessment
            or raw_claim["task_id"] != task_id
            or raw_assessment["task_id"] != task_id
        ):
            raise InvalidInput("claim/assessment do not belong to candidate")
        claim = Claim(
            **{
                **raw_claim,
                "support_refs": tuple(_ref(x) for x in raw_claim["support_refs"]),
                "counter_refs": tuple(_ref(x) for x in raw_claim["counter_refs"]),
            }
        )
        assessment = Assessment(
            **{
                **raw_assessment,
                "support_refs": tuple(_ref(x) for x in raw_assessment["support_refs"]),
                "counter_refs": tuple(_ref(x) for x in raw_assessment["counter_refs"]),
            }
        )
        refs = (
            *claim.support_refs,
            *claim.counter_refs,
            *assessment.support_refs,
            *assessment.counter_refs,
        )
        for ref in refs:
            self.validate_ref(task_id, ref)
        facts = [
            _fact(raw)
            for raw in self.store.list("fact", task["analysis_id"])
            if raw["task_id"] == task_id
        ]
        for fact in facts:
            for ref in fact.evidence_refs:
                self.validate_ref(task_id, ref)
        checks = self.checks(task_id)
        candidate = _candidate(self.store.get("candidate", task["candidate_digest"]))
        evaluator = self._evaluator_for(analysis)
        decision = evaluator.evaluate(
            task=task,
            snapshot=analysis["snapshot"],
            candidate=candidate,
            facts=facts,
            checks=checks,
            claim=claim,
            assessment=assessment,
        )
        if (
            assessment.verifier_attempt_id == claim.attempt_id
            or analysis["rule"]["evaluator_id"] != evaluator.evaluator_id
        ):
            raise InvalidInput("invalid evaluator or non-independent verification")
        expected_input_digest = decision_input_digest(
            task=task, snapshot=analysis["snapshot"], candidate=candidate,
            facts=facts, checks=checks, claim=claim, assessment=assessment,
        )
        if (decision.evaluator_id, decision.evaluator_version) != (
            evaluator.evaluator_id, evaluator.evaluator_version
        ) or decision.input_digest != expected_input_digest:
            raise InvalidInput("evaluator returned an unbound decision")
        if decision.verdict not in {"confirmed", "refuted", "inconclusive"}:
            raise InvalidInput("evaluator returned an unknown verdict")
        permitted_refs = {
            (ref.evidence_id, ref.artifact_digest)
            for fact in facts for ref in fact.evidence_refs
        } | {
            (ref.evidence_id, ref.artifact_digest) for ref in refs
        }
        decision_refs = (*decision.support_refs, *decision.counter_refs)
        if any((ref.evidence_id, ref.artifact_digest) not in permitted_refs
               for ref in decision_refs):
            raise InvalidInput("decision cites evidence absent from its inputs")
        for ref in decision_refs:
            self.validate_ref(task_id, ref)
        if decision.verdict != "inconclusive":
            observed: dict[tuple[str, str, str], set[str]] = {}
            for fact in facts:
                if (fact.confidence_class == "tool_observed"
                        and fact.polarity in {"positive", "negative"}):
                    observed.setdefault(
                        (fact.check_id, fact.predicate_kind, fact.scope), set()
                    ).add(fact.polarity)
            if any(len(polarities) > 1 for polarities in observed.values()):
                raise InvalidInput(
                    "definite decision has conflicting observed facts"
                )
            if not set(task["required_check_ids"]).issubset(
                assessment.checked_predicates
            ):
                raise InvalidInput(
                    "definite decision lacks independent verification of checks"
                )
            if any(
                check["status"] != "complete"
                or _coverage(check["coverage"]).completeness != "complete"
                or check["unknowns"]
                for check in checks
            ):
                raise InvalidInput(
                    "definite decision has incomplete required checks"
                )
            if any(
                self.store.get("evidence", ref.evidence_id)["coverage"]["completeness"]
                != "complete"
                for ref in decision_refs
            ):
                raise InvalidInput(
                    "definite decision cites evidence with incomplete coverage"
                )
            verified_refs = {
                (ref.evidence_id, ref.artifact_digest)
                for ref in (*assessment.support_refs, *assessment.counter_refs)
            }
            if any(
                (ref.evidence_id, ref.artifact_digest) not in verified_refs
                for ref in decision_refs
            ):
                raise InvalidInput(
                    "definite decision cites evidence not reviewed by verifier"
                )
            if (decision.scope != claim.scope or decision.scope != assessment.scope
                    or decision.scope != _candidate_scope(candidate)
                    or claim.unresolved_items or assessment.unresolved_items
                    or decision.blockers or not decision_refs
                    or (decision.verdict == "confirmed" and not decision.support_refs)
                    or (decision.verdict == "refuted" and not decision.counter_refs)):
                raise InvalidInput("definite decision failed the common evidence gate")
        elif not decision.blockers:
            raise InvalidInput("inconclusive decision must explain its blockers")
        verdict_id = digest(
            {
                "task_id": task_id,
                "input_digest": decision.input_digest,
                "evaluator_version": decision.evaluator_version,
            }
        )
        existing = self.store.get("verdict", verdict_id)
        if existing:
            return decision
        if task["phase"] != "verifying":
            raise Conflict(
                "candidate must finish independent verification before gating"
            )
        updated_task = {
            **task,
            "finding_state": decision.verdict,
            "execution_state": "completed",
            "phase": "terminal",
        }
        mutations = [
            Mutation(
                "TaskPhaseChanged",
                {
                    "old_phase": "verifying",
                    "new_phase": "gating",
                    "reason": "roles completed",
                },
                "task",
                task_id,
                {**task, "phase": "gating"},
                task_id,
            ),
            Mutation(
                "VerdictDecided",
                {
                    "verdict_id": verdict_id,
                    "candidate_digest": task["candidate_digest"],
                    "claim_id": claim_id,
                    "assessment_id": assessment_id,
                    "verdict": decision.verdict,
                    "scope": decision.scope,
                    "rule_evaluator_version": decision.evaluator_version,
                    "support_refs": to_plain(decision.support_refs),
                    "counter_refs": to_plain(decision.counter_refs),
                    "blockers": decision.blockers,
                },
                "verdict",
                verdict_id,
                {
                    "verdict_id": verdict_id,
                    "task_id": task_id,
                    "claim_id": claim_id,
                    "assessment_id": assessment_id,
                    **to_plain(decision),
                },
                task_id,
            ),
        ]
        if decision.verdict == "refuted":
            exclusion = ExclusionRecord(
                _new_id(),
                task["candidate_digest"],
                task["snapshot_digest"],
                analysis["rule"]["rule_id"],
                analysis["rule"]["rule_version"],
                "scoped-counterevidence",
                decision.counter_refs,
                tuple(sorted({ref.artifact_digest for ref in decision.counter_refs})),
                decision.scope,
            )
            mutations.append(
                Mutation(
                    "ExclusionChanged",
                    {
                        "exclusion_id": exclusion.exclusion_id,
                        "new_status": "valid",
                        "reason": exclusion.reason_code,
                        "dependency_digests": exclusion.dependency_digests,
                    },
                    "exclusion",
                    exclusion.exclusion_id,
                    to_plain(exclusion),
                    task_id,
                )
            )
        mutations.append(
            Mutation(
                "TaskPhaseChanged",
                {
                    "old_phase": "gating",
                    "new_phase": "terminal",
                    "reason": "verdict recorded",
                },
                "task",
                task_id,
                {**task, "phase": "terminal", "finding_state": decision.verdict},
                task_id,
            )
        )
        mutations.append(
            Mutation(
                "TaskExecutionChanged",
                {"old_status": task["execution_state"], "new_status": "completed"},
                "task",
                task_id,
                updated_task,
                task_id,
            )
        )
        self.store.append(task["analysis_id"], *mutations)
        return decision

    def find_exclusion(self, task_id: str) -> ExclusionRecord | None:
        task = self.task(task_id)
        analysis = self.analysis(task["analysis_id"])
        for raw in self.store.list("exclusion", task["analysis_id"]):
            if (
                raw["candidate_digest"] != task["candidate_digest"]
                or raw["snapshot_digest"] != task["snapshot_digest"]
                or raw["rule_id"] != analysis["rule"]["rule_id"]
                or raw["rule_version"] != analysis["rule"]["rule_version"]
                or raw["status"] != "valid"
            ):
                continue
            refs = tuple(_ref(value) for value in raw["counter_refs"])
            for ref in refs:
                self.validate_ref(task_id, ref)
            if tuple(sorted({ref.artifact_digest for ref in refs})) != tuple(
                raw["dependency_digests"]
            ):
                raise EvidenceIntegrityError("exclusion dependency mismatch")
            return ExclusionRecord(**{**raw, "counter_refs": refs})
        return None

    def mark_inconclusive(
        self, task_id: str, blockers: tuple[str, ...]
    ) -> RuleDecision:
        """Record a bounded nonfinding when execution cannot reach semantic evaluation."""
        if not blockers:
            raise InvalidInput("inconclusive result requires blockers")
        task = self.task(task_id)
        candidate = _candidate(self.store.get("candidate", task["candidate_digest"]))
        analysis = self.analysis(task["analysis_id"])
        evaluator = self._evaluator_for(analysis)
        input_digest = digest(
            {
                "task": task,
                "blockers": blockers,
                "event_high_watermark": self.store.high_watermark(task["analysis_id"]),
            }
        )
        decision = RuleDecision(
            "inconclusive",
            _candidate_scope(candidate),
            (),
            (),
            (),
            (),
            blockers,
            evaluator.evaluator_id,
            evaluator.evaluator_version,
            input_digest,
        )
        verdict_id = digest({"task_id": task_id, "input_digest": input_digest})
        self.store.append(
            task["analysis_id"],
            Mutation(
                "TaskPhaseChanged",
                {
                    "old_phase": task["phase"],
                    "new_phase": "terminal",
                    "reason": blockers,
                },
                "task",
                task_id,
                {**task, "phase": "terminal"},
                task_id,
            ),
            Mutation(
                "VerdictDecided",
                {
                    "verdict_id": verdict_id,
                    "candidate_digest": task["candidate_digest"],
                    "verdict": "inconclusive",
                    "scope": decision.scope,
                    "blockers": blockers,
                },
                "verdict",
                verdict_id,
                {"verdict_id": verdict_id, "task_id": task_id, **to_plain(decision)},
                task_id,
            ),
            Mutation(
                "TaskExecutionChanged",
                {
                    "old_status": task["execution_state"],
                    "new_status": "partial",
                    "reason": blockers,
                },
                "task",
                task_id,
                {
                    **task,
                    "finding_state": "inconclusive",
                    "execution_state": "partial",
                    "phase": "terminal",
                },
                task_id,
            ),
        )
        return decision

    def get_report(self, analysis_id: str) -> dict[str, Any]:
        analysis = self.analysis(analysis_id)
        tasks = self.store.list("task", analysis_id)
        verdict_records = {
            record["verdict_id"]: record
            for record in self.store.list("verdict", analysis_id)
        }
        latest_verdict: dict[str, dict[str, Any]] = {}
        for event in self.store.events(analysis_id):
            if event["event_type"] == "VerdictDecided":
                record = verdict_records.get(event["payload"]["verdict_id"])
                if record and event["task_id"]:
                    latest_verdict[event["task_id"]] = record
        query_failures = [
            {
                "query_id": query["request"]["query_id"],
                "status": query["status"],
                "diagnostic": (query["outcome"] or {}).get("diagnostic"),
            }
            for query in self.store.list("query", analysis_id)
            if query["status"]
            in {"timeout", "failed", "unsupported", "denied", "in_doubt"}
        ]
        origins = self.store.list("candidate_origin", analysis_id)
        selections = self.store.list("plan_selection", analysis_id)
        queries = self.store.list("query", analysis_id)
        by_candidate: dict[str, int] = {}
        by_discoverer: dict[str, int] = {}
        for origin in origins:
            by_candidate[origin["candidate_digest"]] = by_candidate.get(origin["candidate_digest"], 0) + 1
            name = f"{origin['discoverer_id']}@{origin['discoverer_version']}"
            by_discoverer[name] = by_discoverer.get(name, 0) + 1
        outcome_by_key = {
            query["request"]["idempotency_key"]: query["status"] for query in queries
        }
        yield_statuses = {name: 0 for name in (
            "produced_evidence", "partial", "timeout", "failed", "unsupported", "denied"
        )}
        planner_reason: dict[str, int] = {}
        for selection in selections:
            outcome = outcome_by_key.get(selection["query_idempotency_key"])
            if outcome in yield_statuses:
                yield_statuses[outcome] += 1
            if outcome in {"complete", "partial"}:
                yield_statuses["produced_evidence"] += 1
            key = f"{selection['planner_id']}@{selection['planner_version']}:{selection['reason']}"
            planner_reason[key] = planner_reason.get(key, 0) + 1
        supplemental_metrics = {
            "status": "non_gating",
            "candidate_provenance": {
                "unique_candidates": len(tasks),
                "origin_count": len(origins),
                "fused_candidates": sum(count > 1 for count in by_candidate.values()),
                "collapsed_duplicates": max(0, len(origins) - len(by_candidate)),
                "by_discoverer": dict(sorted(by_discoverer.items())),
            },
            "evidence_plan_yield": {
                "selected_plans": len(selections),
                **yield_statuses,
                "by_planner_reason": dict(sorted(planner_reason.items())),
            },
        }
        return {
            "schema_version": dict(SCHEMA_VERSION),
            "analysis_id": analysis_id,
            "snapshot_digest": analysis["snapshot_digest"],
            "discovered_denominator": len(tasks),
            "completed_count": sum(
                task["execution_state"] == "completed" for task in tasks
            ),
            "candidate_reports": [
                {
                    "task_id": task["task_id"],
                    "candidate_digest": task["candidate_digest"],
                    "verdict": task["finding_state"],
                    "decision": latest_verdict.get(task["task_id"]),
                    "checks": self.checks(task["task_id"]),
                }
                for task in tasks
            ],
            "unresolved_scope": [
                "coverage outside discovered candidates remains unknown"
            ],
            "tool_failures": query_failures,
            "supplemental_metrics": supplemental_metrics,
            "usage": {
                "status": "unavailable",
                "reason": "usage is not recorded by this runtime version",
            },
        }
