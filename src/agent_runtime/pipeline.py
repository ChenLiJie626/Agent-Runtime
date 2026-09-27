"""Reusable orchestration from discovery through evidence, roles and verdicts."""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from typing import Any

from .codec import digest
from .coordinator import RoleCoordinator
from .domain import (
    CheckItem,
    Coverage,
    DiscoveryInput,
    QueryOutcome,
    FactAssessment,
    FixedSnapshot,
    QueryRequest,
    RuleDecision,
    RuleSpec,
    Profile,
)
from .errors import InvalidInput
from .ports import (
    AdaptiveEvidencePlanner,
    AgentExecutor,
    CandidateDiscoverer,
    EvidencePlanSelection,
    ProgramQuery,
    RuleEvaluator,
)
from .runtime import DefectRuntime


@dataclass(frozen=True)
class PipelineCandidateResult:
    candidate_id: str
    task_id: str
    status: str
    decision: RuleDecision | None
    material: dict[str, Any]


@dataclass(frozen=True)
class PipelineRunResult:
    analysis_id: str
    candidates: tuple[PipelineCandidateResult, ...]
    report: dict[str, Any]


class AnalysisPipeline:
    """Compose public extension ports into one evidence-first analysis run."""

    def __init__(
        self,
        runtime: DefectRuntime,
        *,
        rule: RuleSpec,
        profile: Profile,
        discoverer: CandidateDiscoverer,
        backend: ProgramQuery,
        evaluator: RuleEvaluator,
        executor: AgentExecutor,
        owner_id: str,
        working_dir: str,
        token_budget: int = 12_000,
        evidence_planner: AdaptiveEvidencePlanner | None = None,
    ) -> None:
        if (not owner_id or not working_dir or token_budget <= 0
                or not getattr(discoverer, "discoverer_id", "")
                or not getattr(discoverer, "discoverer_version", "")
                or (profile.rule_id, profile.rule_version)
                   != (rule.rule_id, rule.rule_version)
                or (evaluator.evaluator_id, evaluator.evaluator_version)
                   != (rule.evaluator_id, rule.evaluator_version)):
            raise InvalidInput("pipeline components do not satisfy one rule profile")
        if not backend.read_only:
            raise InvalidInput("analysis pipeline requires a read-only backend")
        if evidence_planner is not None and (
            not getattr(evidence_planner, "planner_id", "")
            or not getattr(evidence_planner, "planner_version", "")
        ):
            raise InvalidInput("adaptive evidence planner needs ID and version")
        allowed = {
            operation for role in profile.roles for operation in role.allowed_operations
        }
        if not allowed.issubset(backend.supported_operations):
            raise InvalidInput("profile authorizes an operation absent from the backend")
        self.runtime = runtime
        self.rule = rule
        self.profile = profile
        self.discoverer = discoverer
        self.backend = backend
        self.evaluator = evaluator
        self.executor = executor
        self.owner_id = owner_id
        self.working_dir = working_dir
        self.token_budget = token_budget
        self.evidence_planner = evidence_planner

    def run(
        self,
        snapshot: FixedSnapshot,
        *,
        base_sources: dict[str, str],
        head_sources: dict[str, str],
        metadata: dict[str, Any] | None = None,
        max_candidates: int = 100,
        analysis_id: str | None = None,
    ) -> PipelineRunResult:
        if type(max_candidates) is not int or max_candidates < 0:
            raise InvalidInput("pipeline candidate limit must be a nonnegative integer")
        self.runtime.register_evaluator(self.evaluator)
        analysis_id = self.runtime.create_analysis(
            snapshot, self.rule, profile=self.profile, analysis_id=analysis_id,
        )
        discovered = self.discoverer.discover(DiscoveryInput(
            snapshot, base_sources, head_sources, metadata or {},
        ))
        if len({item.candidate_id for item in discovered}) != len(discovered):
            raise InvalidInput("discoverer returned duplicate candidate IDs")
        required = {item.check_id: item for item in self.rule.required_checks}
        coordinator = RoleCoordinator(
            self.runtime,
            self.executor,
            self.backend,
            owner_id=self.owner_id,
            working_dir=self.working_dir,
            token_budget=self.token_budget,
        )
        results: list[PipelineCandidateResult] = []
        for index, candidate in enumerate(discovered):
            if ((candidate.identity.rule_id, candidate.identity.rule_version)
                    != (self.rule.rule_id, self.rule.rule_version)
                    or candidate.identity.snapshot_digest != snapshot.snapshot_digest):
                raise InvalidInput("discovered candidate differs from pipeline rule or snapshot")
            if any(plan.check_id not in required for plan in candidate.evidence_plans):
                raise InvalidInput("evidence plan addresses an unknown check")
            task = self.runtime.propose_candidate(
                analysis_id,
                candidate.identity,
                discovery_ref=candidate.discovery_ref,
                origins=candidate.origins,
            )
            if index >= max_candidates:
                results.append(PipelineCandidateResult(
                    candidate.candidate_id, task.task_id, "not_run", None,
                    dict(candidate.material),
                ))
                continue
            pending = {plan.plan_digest: plan for plan in candidate.evidence_plans}
            outcomes: list[QueryOutcome] = []
            while pending:
                if self.evidence_planner is None:
                    plan_digest = next(iter(pending))
                    selection = EvidencePlanSelection(plan_digest, "eager fixed plan order")
                    planner_id, planner_version = "analysis-pipeline.eager", "1"
                else:
                    selection = self.evidence_planner.select_next(
                        rule=self.rule,
                        candidate=candidate,
                        pending=tuple(pending.values()),
                        outcomes=tuple(outcomes),
                    )
                    planner_id = self.evidence_planner.planner_id
                    planner_version = self.evidence_planner.planner_version
                    if selection is None:
                        self.runtime.record_planning_stopped(
                            task.task_id,
                            planner_id=planner_id,
                            planner_version=planner_version,
                            reason="planner returned no selection",
                            pending_plan_digests=tuple(sorted(pending)),
                        )
                        break
                    if not isinstance(selection, EvidencePlanSelection):
                        raise InvalidInput("adaptive planner returned an invalid selection")
                    if selection.plan_digest not in pending:
                        raise InvalidInput("adaptive planner selected a non-pending plan")
                plan = pending.pop(selection.plan_digest)
                idempotency_key = digest({
                    "task_id": task.task_id,
                    "plan_digest": plan.plan_digest,
                })
                self.runtime.record_plan_selection(
                    task.task_id,
                    selection=selection,
                    planner_id=planner_id,
                    planner_version=planner_version,
                    check_id=plan.check_id,
                    operation=plan.operation,
                    query_idempotency_key=idempotency_key,
                )
                outcome, initial_ref = self.runtime.query_program(
                    QueryRequest(
                        str(uuid.uuid4()),
                        task.task_id,
                        plan.check_id,
                        plan.operation,
                        snapshot.snapshot_digest,
                        self.backend.backend_id,
                        self.backend.backend_version,
                        snapshot.tool_policy_digest,
                        {
                            "scope_id": snapshot.scope_id,
                            "selectors": dict(plan.selectors),
                            "max_results": plan.max_results,
                        },
                        dict(plan.args),
                        idempotency_key,
                        plan.timeout_ms,
                    ),
                    self.backend,
                )
                outcomes.append(outcome)
                definition = required[plan.check_id]
                if initial_ref is None:
                    status = "unsupported" if outcome.status == "unsupported" else "failed"
                    self.runtime.update_check(CheckItem(
                        plan.check_id,
                        task.task_id,
                        definition.question,
                        status,
                        "unknown",
                        (),
                        outcome.coverage,
                        (outcome.diagnostic or outcome.status,),
                        tuple(outcome.limitations),
                    ))
                    continue
                role = "counter" if plan.polarity == "negative" else "support"
                ref = self.runtime.evidence_ref(initial_ref.evidence_id, role)
                self.runtime.record_fact(FactAssessment(
                    str(uuid.uuid4()),
                    task.task_id,
                    plan.check_id,
                    plan.predicate_kind,
                    plan.polarity,
                    (ref,),
                    candidate.identity.scope,
                    "tool_observed",
                    limitations=tuple(outcome.limitations),
                ))
                complete = outcome.status == "complete" and outcome.coverage.completeness == "complete"
                self.runtime.update_check(CheckItem(
                    plan.check_id,
                    task.task_id,
                    definition.question,
                    "complete" if complete else "partial",
                    ("refuted" if plan.polarity == "negative" else "supported")
                    if complete else "unknown",
                    (ref,),
                    outcome.coverage,
                    () if complete else tuple(outcome.limitations or outcome.coverage.omissions),
                    tuple(outcome.limitations),
                ))
            decision = coordinator.run_candidate(task.task_id)
            results.append(PipelineCandidateResult(
                candidate.candidate_id,
                task.task_id,
                decision.verdict,
                decision,
                dict(candidate.material),
            ))
        return PipelineRunResult(
            analysis_id,
            tuple(results),
            self.runtime.get_report(analysis_id),
        )
