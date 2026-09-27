"""Ports implemented by application adapters (SPEC 004)."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, Protocol

from .codec import canonical_json, freeze_value
from .domain import (
    Assessment,
    CandidateIdentity,
    CandidateIdentityDraft,
    CandidateKey,
    CheckDefinition,
    Claim,
    ContextView,
    Coverage,
    DiscoveryInput,
    DiscoveredCandidate,
    EvidencePlan,
    EvidenceRef,
    FactAssessment,
    QueryOutcome,
    QueryRequest,
    RuleDecision,
    RuleSpec,
    SpecialistNote,
)
from .errors import InvalidInput


@dataclass(frozen=True)
class ProgramResult:
    status: str
    coverage: Coverage
    raw: bytes | None = None
    limitations: tuple[str, ...] = ()
    diagnostic: str | None = None


@dataclass(frozen=True)
class QueryOperation:
    """Small, strict parameter contract for one backend operation."""

    name: str
    parameter_types: dict[str, str]
    required_parameters: tuple[str, ...] = ()
    required_selectors: tuple[str, ...] = ()
    max_argument_bytes: int = 16_384

    def __post_init__(self) -> None:
        if (not self.name or self.max_argument_bytes < 1
                or set(self.required_parameters) - set(self.parameter_types)
                or any(kind not in {"string", "integer", "boolean", "object", "array"}
                       for kind in self.parameter_types.values())):
            raise InvalidInput("invalid query operation descriptor")
        object.__setattr__(self, "parameter_types", freeze_value(self.parameter_types))

    def validate(self, args: dict[str, Any], selectors: dict[str, Any]) -> None:
        if (set(args) - set(self.parameter_types)
                or set(self.required_parameters) - set(args)
                or set(self.required_selectors) - set(selectors)):
            raise InvalidInput("query arguments or scope do not match operation schema")
        if len(canonical_json(args).encode("utf-8")) > self.max_argument_bytes:
            raise InvalidInput("query arguments exceed operation limit")
        checks = {
            "string": lambda value: isinstance(value, str),
            "integer": lambda value: type(value) is int,
            "boolean": lambda value: type(value) is bool,
            "object": lambda value: isinstance(value, Mapping),
            "array": lambda value: isinstance(value, (list, tuple)),
        }
        for name, value in args.items():
            kind = self.parameter_types[name]
            if kind not in checks or not checks[kind](value):
                raise InvalidInput(f"invalid query argument: {name}")


class ProgramQuery(Protocol):
    backend_id: str
    backend_version: str
    supported_operations: frozenset[str]
    idempotent_retry: bool
    read_only: bool
    operation_specs: dict[str, QueryOperation]

    def query(self, request: QueryRequest) -> ProgramResult:
        """Return source material and honest coverage for a fixed snapshot."""


class CandidateIdentityPolicy(Protocol):
    def identify(
        self, *, snapshot_digest: str, rule_id: str, rule_version: str,
        material: dict[str, Any],
    ) -> CandidateIdentity | CandidateIdentityDraft:
        """Return a precise key or explicit gaps; never guess missing fields."""


class CandidateDiscoverer(Protocol):
    discoverer_id: str
    discoverer_version: str

    def discover(self, input: DiscoveryInput) -> tuple[DiscoveredCandidate, ...]:
        """Return deterministic candidates and bounded evidence plans."""


@dataclass(frozen=True)
class EvidencePlanSelection:
    """One persisted planner choice from the remaining exact plan digests."""

    plan_digest: str
    reason: str

    def __post_init__(self) -> None:
        if (len(self.plan_digest) != 64
                or any(char not in "0123456789abcdef" for char in self.plan_digest)
                or not self.reason or len(self.reason) > 500):
            raise InvalidInput("evidence plan selection is invalid")


class AdaptiveEvidencePlanner(Protocol):
    planner_id: str
    planner_version: str

    def select_next(
        self,
        *,
        rule: RuleSpec,
        candidate: DiscoveredCandidate,
        pending: tuple[EvidencePlan, ...],
        outcomes: tuple[QueryOutcome, ...],
    ) -> EvidencePlanSelection | None:
        """Select one still-pending exact plan, or stop without implying success."""


class CheckPlanner(Protocol):
    planner_id: str
    planner_version: str

    def plan(
        self, rule: RuleSpec, candidate: CandidateKey | CandidateIdentity
    ) -> tuple[CheckDefinition, ...]:
        """Include the rule's required checks and optional candidate checks."""


class RuleEvaluator(Protocol):
    evaluator_id: str
    evaluator_version: str

    def evaluate(
        self,
        *,
        task: dict,
        snapshot: dict,
        candidate: CandidateKey | CandidateIdentity,
        facts: list[FactAssessment],
        checks: list[dict],
        claim: Claim,
        assessment: Assessment,
    ) -> RuleDecision:
        """Deterministically decide a scoped candidate from committed material."""


@dataclass(frozen=True)
class AgentCapabilities:
    stream_events: bool
    explicit_resume: bool
    interrupt: bool
    structured_output: bool
    custom_tools: bool
    deny_tools: bool
    isolated_session: bool
    usage_reporting: bool


class AgentExecutor(Protocol):
    def describe_capabilities(self) -> AgentCapabilities:
        """Report mechanisms actually implemented, not prompt promises."""

    def run(self, request: RoleRunRequest, tools: DomainTools) -> RoleRunResult:
        """Run one role attempt and return a structured role artifact."""


@dataclass(frozen=True)
class RoleRunRequest:
    task_id: str
    attempt_id: str
    role: str
    context: ContextView
    claim: Claim | None = None
    resume_session_id: str | None = None
    role_kind: str | None = None
    specialist_notes: tuple[SpecialistNote, ...] = ()


@dataclass(frozen=True)
class RoleRunResult:
    output: Claim | Assessment | SpecialistNote
    sdk_session_id: str | None = None


class DomainTools(Protocol):
    def query(self, request: QueryRequest) -> tuple[object, object]:
        """Execute a whitelisted query only while the role lease remains valid."""

    def read_evidence(self, evidence_id: str) -> bytes:
        """Read an existing immutable artifact within the task scope."""

    def resolve_evidence_ref(self, evidence_id: str, role: str) -> EvidenceRef:
        """Resolve an ID only after server-side scope and digest checks."""
