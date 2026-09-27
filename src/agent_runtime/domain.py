"""SDK-independent value objects and validated identities (SPEC 002)."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from .codec import SCHEMA_VERSION, digest, freeze_value, validate_relative_path
from .errors import InvalidInput


@dataclass(frozen=True)
class SourceLocation:
    side: str
    commit: str
    path: str
    start_line: int
    start_column: int
    end_line: int
    end_column: int

    def __post_init__(self) -> None:
        if self.side not in {"base", "head"}:
            raise InvalidInput("location side must be base or head")
        validate_relative_path(self.path)
        if min(self.start_line, self.start_column, self.end_line, self.end_column) < 1:
            raise InvalidInput("source positions start at one")
        if (self.end_line, self.end_column) < (self.start_line, self.start_column):
            raise InvalidInput("source end precedes start")


@dataclass(frozen=True)
class AnalysisSnapshot:
    repository_id: str
    base_commit: str
    head_commit: str
    base_tree_digest: str
    head_tree_digest: str
    build_variant_id: str
    base_build_digest: str
    head_build_digest: str
    rule_profile_digest: str
    tool_policy_digest: str
    compile_db_digest: str | None = None
    target_triple: str | None = None
    macro_digest: str | None = None
    toolchain_digest: str | None = None

    @property
    def snapshot_digest(self) -> str:
        return digest(self)


@dataclass(frozen=True)
class FixedSnapshot:
    """Rule-independent, immutable analysis input binding."""

    repository_id: str
    scope_id: str
    source_digest: str
    rule_profile_digest: str
    tool_policy_digest: str
    inputs: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not all((self.repository_id, self.scope_id, self.source_digest,
                    self.rule_profile_digest, self.tool_policy_digest)):
            raise InvalidInput("fixed snapshot needs repository, scope and digests")
        object.__setattr__(self, "inputs", freeze_value(self.inputs))
        digest(self)

    @property
    def snapshot_digest(self) -> str:
        return digest(self)


@dataclass(frozen=True)
class CheckDefinition:
    check_id: str
    question: str
    accepted_predicates: tuple[str, ...]

    def __post_init__(self) -> None:
        if (not self.check_id or not self.question or not self.accepted_predicates
                or any(not value for value in self.accepted_predicates)
                or len(self.accepted_predicates) != len(set(self.accepted_predicates))):
            raise InvalidInput("check definition needs unique predicates and a question")


@dataclass(frozen=True)
class RuleSpec:
    rule_id: str
    rule_version: str
    title: str
    required_checks: tuple[CheckDefinition, ...]
    evaluator_id: str
    evaluator_version: str

    def __post_init__(self) -> None:
        ids = [check.check_id for check in self.required_checks]
        if not ids or len(ids) != len(set(ids)):
            raise InvalidInput("rule requires unique, nonempty checks")


@dataclass(frozen=True)
class RoleDefinition:
    role_id: str
    kind: str
    focus_check_ids: tuple[str, ...] = ()
    allowed_operations: tuple[str, ...] = ()
    required_capabilities: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if not self.role_id or self.kind not in {
            "investigator", "specialist", "verifier"
        }:
            raise InvalidInput("role needs an ID and supported kind")
        if len(self.focus_check_ids) != len(set(self.focus_check_ids)):
            raise InvalidInput("role focus checks must be unique")
        if self.kind == "specialist" and not self.focus_check_ids:
            raise InvalidInput("specialist needs a focused check gap")
        capabilities = {
            "stream_events", "explicit_resume", "interrupt", "structured_output",
            "custom_tools", "deny_tools", "isolated_session", "usage_reporting",
        }
        if any(name not in capabilities for name in self.required_capabilities):
            raise InvalidInput("role requires an unknown executor capability")


@dataclass(frozen=True)
class Profile:
    profile_id: str
    version: str
    rule_id: str
    rule_version: str
    roles: tuple[RoleDefinition, ...]
    tool_policy_digest: str
    prompt_digests: dict[str, str] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not all((self.profile_id, self.version, self.rule_id,
                    self.rule_version, self.tool_policy_digest)):
            raise InvalidInput("profile needs identity, rule and tool policy")
        kinds = [role.kind for role in self.roles]
        if (len(self.roles) < 2 or kinds[0] != "investigator"
                or kinds[-1] != "verifier"
                or kinds.count("investigator") != 1
                or kinds.count("verifier") != 1
                or len({role.role_id for role in self.roles}) != len(self.roles)):
            raise InvalidInput("profile needs one investigator, optional specialists, one verifier")
        object.__setattr__(self, "prompt_digests", freeze_value(self.prompt_digests))
        digest(self)


@dataclass(frozen=True)
class SpecialistNote:
    note_id: str
    task_id: str
    attempt_id: str
    role: str
    summary: str
    support_refs: tuple[EvidenceRef, ...]
    counter_refs: tuple[EvidenceRef, ...]
    unresolved_items: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if not all((self.note_id, self.task_id, self.attempt_id,
                    self.role, self.summary)):
            raise InvalidInput("specialist note needs identity and summary")


@dataclass(frozen=True)
class CandidateKey:
    rule_id: str
    rule_version: str
    source_location: SourceLocation
    sink_location: SourceLocation
    build_variant_id: str
    object_identity: str
    path_identity: str

    def __post_init__(self) -> None:
        if not self.object_identity or not self.path_identity:
            raise InvalidInput("precise candidate requires object and path identity")

    @property
    def candidate_digest(self) -> str:
        return digest(self)


@dataclass(frozen=True)
class CandidateIdentity:
    """A rule-scoped precise identity, independent of source/sink layout."""

    rule_id: str
    rule_version: str
    snapshot_digest: str
    identity: dict[str, Any]
    scope: str

    def __post_init__(self) -> None:
        if not all((self.rule_id, self.rule_version, self.snapshot_digest,
                    self.scope)) or not self.identity:
            raise InvalidInput("precise candidate needs rule, snapshot, identity and scope")
        if any(not key or value is None for key, value in self.identity.items()):
            raise InvalidInput("candidate identity has an unresolved field")
        object.__setattr__(self, "identity", freeze_value(self.identity))
        digest(self)

    @property
    def candidate_digest(self) -> str:
        return digest(self)


@dataclass(frozen=True)
class CandidateIdentityDraft:
    """Known identity fields plus gaps that prevent precise deduplication."""

    identity: dict[str, Any]
    scope: str
    unresolved_identity: tuple[str, ...]

    def __post_init__(self) -> None:
        if not self.scope or not self.unresolved_identity:
            raise InvalidInput("identity draft needs scope and unresolved fields")
        if any(not field for field in self.unresolved_identity):
            raise InvalidInput("unresolved identity fields must be named")
        object.__setattr__(self, "identity", freeze_value(self.identity))
        digest(self)


@dataclass(frozen=True)
class DiscoveryInput:
    """Immutable source material presented to a candidate discoverer."""

    snapshot: FixedSnapshot
    base_sources: dict[str, str]
    head_sources: dict[str, str]
    metadata: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not self.head_sources:
            raise InvalidInput("discovery needs at least one head source path")
        for path, text in (*self.base_sources.items(), *self.head_sources.items()):
            validate_relative_path(path)
            if not isinstance(text, str):
                raise InvalidInput("discovery source content must be text")
        object.__setattr__(self, "base_sources", freeze_value(self.base_sources))
        object.__setattr__(self, "head_sources", freeze_value(self.head_sources))
        object.__setattr__(self, "metadata", freeze_value(self.metadata))


@dataclass(frozen=True)
class EvidencePlan:
    """One bounded backend query and its fact/check interpretation."""

    check_id: str
    operation: str
    selectors: dict[str, Any]
    args: dict[str, Any]
    predicate_kind: str
    polarity: str
    timeout_ms: int = 1_000
    max_results: int = 1

    def __post_init__(self) -> None:
        if (not all((self.check_id, self.operation, self.predicate_kind))
                or self.polarity not in {"positive", "negative", "unknown"}
                or not self.selectors
                or type(self.timeout_ms) is not int or self.timeout_ms <= 0
                or type(self.max_results) is not int
                or not 1 <= self.max_results <= 10_000):
            raise InvalidInput("invalid evidence plan")
        object.__setattr__(self, "selectors", freeze_value(self.selectors))
        object.__setattr__(self, "args", freeze_value(self.args))

    @property
    def plan_digest(self) -> str:
        return digest(self)


@dataclass(frozen=True)
class DiscoveryOrigin:
    """Immutable provenance for one discoverer's precise candidate."""

    discoverer_id: str
    discoverer_version: str
    origin_candidate_id: str
    discovery_ref: str
    material_digest: str
    plan_digests: tuple[str, ...]

    def __post_init__(self) -> None:
        if not all((self.discoverer_id, self.discoverer_version,
                    self.origin_candidate_id, self.discovery_ref,
                    self.material_digest)):
            raise InvalidInput("discovery origin needs discoverer identity and material")
        digests = (self.material_digest, *self.plan_digests)
        if (any(len(value) != 64 or any(char not in "0123456789abcdef" for char in value)
                for value in digests)
                or tuple(sorted(self.plan_digests)) != self.plan_digests
                or len(set(self.plan_digests)) != len(self.plan_digests)):
            raise InvalidInput("discovery origin digests must be unique sorted SHA-256 values")


@dataclass(frozen=True)
class DiscoveredCandidate:
    """A precise candidate plus the bounded evidence queries it requires."""

    candidate_id: str
    identity: CandidateIdentity
    discovery_ref: str
    evidence_plans: tuple[EvidencePlan, ...]
    material: dict[str, Any] = field(default_factory=dict)
    origins: tuple[DiscoveryOrigin, ...] = ()

    def __post_init__(self) -> None:
        if (not self.candidate_id or not self.discovery_ref or not self.evidence_plans
                or len({plan.check_id for plan in self.evidence_plans})
                   != len(self.evidence_plans)
                or any(not isinstance(origin, DiscoveryOrigin) for origin in self.origins)):
            raise InvalidInput("discovered candidate needs one evidence plan per check")
        object.__setattr__(self, "material", freeze_value(self.material))
        object.__setattr__(self, "origins", tuple(self.origins))
        digest(self)


@dataclass(frozen=True)
class GenericProvisionalCandidate:
    proposal_id: str
    analysis_id: str
    rule_id: str
    rule_version: str
    snapshot_digest: str
    identity: dict[str, Any]
    scope: str
    discovery_ref: str
    unresolved_identity: tuple[str, ...]

    def __post_init__(self) -> None:
        if not self.discovery_ref or not self.unresolved_identity:
            raise InvalidInput("provisional candidate needs discovery and identity gaps")
        object.__setattr__(self, "identity", freeze_value(self.identity))
        digest(self)


@dataclass(frozen=True)
class StructuredQueryScope:
    """Minimal bounded scope for a rule-defined ProgramQuery operation."""

    scope_id: str
    selectors: dict[str, Any]
    max_results: int

    def __post_init__(self) -> None:
        if not self.scope_id or not self.selectors or not 1 <= self.max_results <= 10_000:
            raise InvalidInput("query needs fixed scope, selectors and bounded results")
        object.__setattr__(self, "selectors", freeze_value(self.selectors))
        digest(self)


@dataclass(frozen=True)
class ProvisionalCandidate:
    proposal_id: str
    analysis_id: str
    rule_id: str
    rule_version: str
    source_location: SourceLocation
    sink_location: SourceLocation | None
    discovery_ref: str
    unresolved_identity: tuple[str, ...]

    def __post_init__(self) -> None:
        if not self.discovery_ref or not self.unresolved_identity:
            raise InvalidInput(
                "provisional candidate needs discovery and identity gaps"
            )


@dataclass(frozen=True)
class Coverage:
    universe_description: str
    examined_description: str
    completeness: str
    omissions: tuple[str, ...] = ()
    basis_refs: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if self.completeness not in {"complete", "partial", "unknown"}:
            raise InvalidInput("invalid coverage completeness")
        if self.completeness == "complete" and (
            not self.universe_description or not self.basis_refs
        ):
            raise InvalidInput(
                "complete coverage requires universe and basis references"
            )
        if self.completeness == "partial" and not self.omissions:
            raise InvalidInput("partial coverage requires omissions")


@dataclass(frozen=True)
class EvidenceRef:
    evidence_id: str
    artifact_digest: str
    snapshot_digest: str
    role: str

    def __post_init__(self) -> None:
        if self.role not in {"support", "counter", "context"}:
            raise InvalidInput("invalid evidence role")


@dataclass(frozen=True)
class QueryScope:
    repository_paths: tuple[str, ...]
    symbols: tuple[str, ...]
    build_variant_id: str
    max_results: int
    include_indirect: bool

    def __post_init__(self) -> None:
        if not self.repository_paths and not self.symbols:
            raise InvalidInput("query scope must name paths or symbols")
        if not self.build_variant_id or not 1 <= self.max_results <= 10_000:
            raise InvalidInput("invalid query variant or result limit")
        if not isinstance(self.include_indirect, bool):
            raise InvalidInput("include_indirect must be boolean")
        for path in self.repository_paths:
            validate_relative_path(path)


@dataclass(frozen=True)
class QueryRequest:
    query_id: str
    task_id: str
    check_id: str
    operation: str
    snapshot_digest: str
    backend_id: str
    backend_version: str
    tool_policy_digest: str
    scope: dict[str, Any]
    args: dict[str, Any]
    idempotency_key: str
    timeout_ms: int

    def __post_init__(self) -> None:
        if not all((self.query_id, self.task_id, self.check_id, self.operation,
                    self.snapshot_digest, self.backend_id, self.backend_version,
                    self.tool_policy_digest, self.idempotency_key)):
            raise InvalidInput("query request needs identity and binding")
        if self.timeout_ms <= 0:
            raise InvalidInput("query needs positive timeout")
        object.__setattr__(self, "scope", freeze_value(self.scope))
        object.__setattr__(self, "args", freeze_value(self.args))

    @property
    def request_digest(self) -> str:
        return digest(
            {
                key: getattr(self, key)
                for key in (
                    "snapshot_digest",
                    "backend_id",
                    "backend_version",
                    "operation",
                    "scope",
                    "args",
                    "check_id",
                    "timeout_ms",
                    "tool_policy_digest",
                )
            }
        )


@dataclass(frozen=True)
class QueryOutcome:
    query_id: str
    status: str
    backend_id: str
    backend_version: str
    snapshot_digest: str
    query_digest: str
    coverage: Coverage
    limitations: tuple[str, ...] = ()
    raw_artifact_digest: str | None = None
    diagnostic: str | None = None

    def __post_init__(self) -> None:
        if self.status not in {
            "complete",
            "partial",
            "timeout",
            "failed",
            "unsupported",
            "denied",
        }:
            raise InvalidInput("invalid query status")
        if self.status in {"complete", "partial"} and not self.raw_artifact_digest:
            raise InvalidInput("successful query needs raw artifact")


@dataclass(frozen=True)
class FactAssessment:
    fact_id: str
    task_id: str
    check_id: str
    predicate_kind: str
    polarity: str
    evidence_refs: tuple[EvidenceRef, ...]
    scope: str
    confidence_class: str
    assumptions: tuple[str, ...] = ()
    limitations: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if self.polarity not in {"positive", "negative", "unknown"}:
            raise InvalidInput("invalid fact polarity")
        if self.confidence_class not in {"tool_observed", "agent_inferred", "unknown"}:
            raise InvalidInput("invalid confidence class")


@dataclass(frozen=True)
class Claim:
    claim_id: str
    task_id: str
    attempt_id: str
    fact_ids: tuple[str, ...]
    support_refs: tuple[EvidenceRef, ...]
    counter_refs: tuple[EvidenceRef, ...]
    unresolved_items: tuple[str, ...]
    scope: str


@dataclass(frozen=True)
class Assessment:
    assessment_id: str
    task_id: str
    verifier_attempt_id: str
    claim_id: str
    checked_predicates: tuple[str, ...]
    support_refs: tuple[EvidenceRef, ...]
    counter_refs: tuple[EvidenceRef, ...]
    unresolved_items: tuple[str, ...]
    recommendation: str
    scope: str


@dataclass(frozen=True)
class RuleDecision:
    verdict: str
    scope: str
    satisfied_obligations: tuple[str, ...]
    failed_obligations: tuple[str, ...]
    support_refs: tuple[EvidenceRef, ...]
    counter_refs: tuple[EvidenceRef, ...]
    blockers: tuple[str, ...]
    evaluator_id: str
    evaluator_version: str
    input_digest: str


@dataclass(frozen=True)
class CandidateTask:
    task_id: str
    analysis_id: str
    snapshot_digest: str
    candidate_digest: str
    required_check_ids: tuple[str, ...]
    execution_state: str = "pending"
    finding_state: str = "unassessed"
    phase: str = "planned"


@dataclass(frozen=True)
class CheckItem:
    check_id: str
    task_id: str
    question: str
    status: str
    answer: str
    evidence_refs: tuple[EvidenceRef, ...]
    coverage: Coverage
    unknowns: tuple[str, ...] = ()
    limitations: tuple[str, ...] = ()


@dataclass(frozen=True)
class ExclusionRecord:
    exclusion_id: str
    candidate_digest: str
    snapshot_digest: str
    rule_id: str
    rule_version: str
    reason_code: str
    counter_refs: tuple[EvidenceRef, ...]
    dependency_digests: tuple[str, ...]
    scope: str
    status: str = "valid"


@dataclass(frozen=True)
class ContextView:
    view_id: str
    task_id: str
    role: str
    snapshot_digest: str
    profile_digest: str
    candidate_digest: str
    event_high_watermark: int
    focus_check_ids: tuple[str, ...]
    protected_items: tuple[dict[str, Any], ...]
    evidence_snippets: tuple[dict[str, Any], ...]
    omitted_items: tuple[dict[str, Any], ...]
    retrieval_refs: tuple[str, ...]
    token_budget: int
    estimated_tokens: int
    view_digest: str


@dataclass(frozen=True)
class Handoff:
    handoff_id: str
    task_id: str
    from_attempt_id: str
    snapshot_digest: str
    candidate_digest: str
    event_high_watermark: int
    protected_item_ids: tuple[str, ...]
    next_actions: tuple[str, ...] = ()
    schema_version: dict[str, int] = field(
        default_factory=lambda: SCHEMA_VERSION.copy()
    )
