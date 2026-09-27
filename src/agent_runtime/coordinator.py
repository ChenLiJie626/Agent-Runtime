"""Deterministic role sequence with optional focused specialist attempts."""

from __future__ import annotations

from .context import ContextViewBuilder
from .domain import Assessment, Claim, QueryRequest, RuleDecision, SpecialistNote
from .errors import CapabilityUnavailable, InvalidInput, PolicyDenied
from .ports import AgentExecutor, ProgramQuery, RoleRunRequest
from .runtime import DefectRuntime
from .session import SessionService


class _ScopedTools:
    def __init__(
        self,
        runtime: DefectRuntime,
        session: SessionService,
        backend: ProgramQuery,
        task_id: str,
        binding_id: str,
        owner_id: str,
        lease_epoch: int,
        allowed_operations: frozenset[str] | None = None,
    ) -> None:
        self.runtime = runtime
        self.session = session
        self.backend = backend
        self.task_id = task_id
        self.binding_id = binding_id
        self.owner_id = owner_id
        self.lease_epoch = lease_epoch
        self.allowed_operations = allowed_operations

    def query(self, request: QueryRequest):
        self.session.assert_lease(self.binding_id, self.owner_id, self.lease_epoch)
        if request.task_id != self.task_id:
            raise InvalidInput("tool request belongs to another task")
        if (self.allowed_operations is not None
                and request.operation not in self.allowed_operations):
            raise PolicyDenied("operation is not authorized for this role")
        return self.runtime.query_program(request, self.backend)

    def read_evidence(self, evidence_id: str) -> bytes:
        self.session.assert_lease(self.binding_id, self.owner_id, self.lease_epoch)
        ref = self.runtime.evidence_ref(evidence_id)
        self.runtime.validate_ref(self.task_id, ref)
        return self.runtime.store.read_artifact(ref.artifact_digest)

    def resolve_evidence_ref(self, evidence_id: str, role: str):
        self.session.assert_lease(self.binding_id, self.owner_id, self.lease_epoch)
        ref = self.runtime.evidence_ref(evidence_id, role)
        self.runtime.validate_ref(self.task_id, ref)
        return ref


class RoleCoordinator:
    def __init__(
        self,
        runtime: DefectRuntime,
        agent: AgentExecutor,
        backend: ProgramQuery,
        *,
        owner_id: str,
        working_dir: str,
        token_budget: int = 12_000,
    ) -> None:
        self.runtime = runtime
        self.agent = agent
        self.backend = backend
        self.owner_id = owner_id
        self.working_dir = working_dir
        self.token_budget = token_budget
        self.sessions = SessionService(runtime)
        self.context = ContextViewBuilder(runtime, self.sessions)

    def _check_capabilities(self, roles: list[dict]) -> None:
        capabilities = self.agent.describe_capabilities()
        required = (
            "isolated_session",
            "structured_output",
            "custom_tools",
            "deny_tools",
        )
        missing = [name for name in required if not getattr(capabilities, name)]
        for role in roles:
            missing.extend(
                f"{role['role_id']}:{name}"
                for name in role.get("required_capabilities", ())
                if not getattr(capabilities, name, False)
            )
        if missing:
            raise CapabilityUnavailable(
                f"agent lacks required capabilities: {', '.join(missing)}"
            )

    def run_candidate(self, task_id: str) -> RuleDecision:
        task = self.runtime.task(task_id)
        analysis = self.runtime.analysis(task["analysis_id"])
        profile = analysis.get("profile")
        roles = profile["roles"] if profile else [
            {"role_id": "investigator", "kind": "investigator",
             "focus_check_ids": [], "allowed_operations": []},
            {"role_id": "verifier", "kind": "verifier",
             "focus_check_ids": [], "allowed_operations": []},
        ]
        investigator_role = roles[0]["role_id"]
        verifier_role = roles[-1]["role_id"]

        def operations(role: dict) -> frozenset[str] | None:
            return (frozenset(role["allowed_operations"]) if profile else None)

        if task["phase"] == "terminal" and task["finding_state"] != "refuted":
            raise InvalidInput("terminal task requires an explicit reopen")
        exclusion = self.runtime.find_exclusion(task_id)
        if exclusion:
            # A previous scoped refutation remains authoritative; no new model call.
            return RuleDecision(
                "refuted",
                exclusion.scope,
                ("valid-exclusion",),
                (),
                (),
                exclusion.counter_refs,
                (),
                "exclusion-index",
                "1.0",
                exclusion.exclusion_id,
            )
        self._check_capabilities(roles)
        claim: Claim | None = None
        try:
            investigator, investigator_lease = self.sessions.start_attempt(
                task_id,
                investigator_role,
                working_dir=self.working_dir,
                owner_id=self.owner_id,
            )
            try:
                view = self.context.build(
                    task_id,
                    investigator_role,
                    focus_check_ids=tuple(
                        self.runtime.task(task_id)["required_check_ids"]
                    ),
                    token_budget=self.token_budget,
                )
                tools = _ScopedTools(
                    self.runtime,
                    self.sessions,
                    self.backend,
                    task_id,
                    investigator.binding_id,
                    self.owner_id,
                    investigator_lease.lease_epoch,
                    operations(roles[0]),
                )
                result = self.agent.run(
                    RoleRunRequest(
                        task_id, investigator.attempt_id, investigator_role, view,
                        role_kind="investigator",
                    ),
                    tools,
                )
                if (
                    not isinstance(result.output, Claim)
                    or result.output.attempt_id != investigator.attempt_id
                ):
                    raise InvalidInput("investigator did not return a matching Claim")
                if result.sdk_session_id:
                    self.sessions.bind_sdk_session(
                        investigator.binding_id,
                        result.sdk_session_id,
                        owner_id=self.owner_id,
                        lease_epoch=investigator_lease.lease_epoch,
                    )
                self.runtime.record_claim(result.output)
                claim = result.output
                self.sessions.end_attempt(
                    investigator.attempt_id,
                    owner_id=self.owner_id,
                    lease_epoch=investigator_lease.lease_epoch,
                    status="completed",
                    reason="claim recorded",
                )
            except Exception:
                self.sessions.end_attempt(
                    investigator.attempt_id,
                    owner_id=self.owner_id,
                    lease_epoch=investigator_lease.lease_epoch,
                    status="failed",
                    reason="investigator failed",
                )
                raise
            specialist_notes: list[SpecialistNote] = []
            for role in roles[1:-1]:
                focus = tuple(role["focus_check_ids"])
                if not any(
                    item["status"] != "complete"
                    for item in self.runtime.checks(task_id)
                    if item["check_id"] in focus
                ):
                    continue
                specialist, specialist_lease = self.sessions.start_attempt(
                    task_id, role["role_id"], working_dir=self.working_dir,
                    owner_id=self.owner_id,
                )
                try:
                    view = self.context.build(
                        task_id, role["role_id"], focus_check_ids=focus,
                        token_budget=self.token_budget,
                    )
                    tools = _ScopedTools(
                        self.runtime, self.sessions, self.backend, task_id,
                        specialist.binding_id, self.owner_id,
                        specialist_lease.lease_epoch, operations(role),
                    )
                    result = self.agent.run(
                        RoleRunRequest(
                            task_id, specialist.attempt_id, role["role_id"],
                            view, claim, role_kind="specialist",
                            specialist_notes=tuple(specialist_notes),
                        ),
                        tools,
                    )
                    if (not isinstance(result.output, SpecialistNote)
                            or result.output.attempt_id != specialist.attempt_id
                            or result.output.role != role["role_id"]):
                        raise InvalidInput("specialist did not return a matching note")
                    if result.sdk_session_id:
                        self.sessions.bind_sdk_session(
                            specialist.binding_id, result.sdk_session_id,
                            owner_id=self.owner_id,
                            lease_epoch=specialist_lease.lease_epoch,
                        )
                    self.runtime.record_specialist_note(result.output)
                    specialist_notes.append(result.output)
                    self.sessions.end_attempt(
                        specialist.attempt_id, owner_id=self.owner_id,
                        lease_epoch=specialist_lease.lease_epoch,
                        status="completed", reason="specialist note recorded",
                    )
                except Exception:
                    self.sessions.end_attempt(
                        specialist.attempt_id, owner_id=self.owner_id,
                        lease_epoch=specialist_lease.lease_epoch,
                        status="failed", reason="specialist failed",
                    )
                    raise
            verifier, verifier_lease = self.sessions.start_attempt(
                task_id,
                verifier_role,
                working_dir=self.working_dir,
                owner_id=self.owner_id,
            )
            try:
                view = self.context.build(
                    task_id,
                    verifier_role,
                    focus_check_ids=tuple(
                        self.runtime.task(task_id)["required_check_ids"]
                    ),
                    token_budget=self.token_budget,
                )
                tools = _ScopedTools(
                    self.runtime,
                    self.sessions,
                    self.backend,
                    task_id,
                    verifier.binding_id,
                    self.owner_id,
                    verifier_lease.lease_epoch,
                    operations(roles[-1]),
                )
                result = self.agent.run(
                    RoleRunRequest(
                        task_id, verifier.attempt_id, verifier_role, view, claim,
                        role_kind="verifier",
                        specialist_notes=tuple(specialist_notes),
                    ),
                    tools,
                )
                if (
                    not isinstance(result.output, Assessment)
                    or result.output.verifier_attempt_id != verifier.attempt_id
                ):
                    raise InvalidInput("verifier did not return a matching Assessment")
                if result.sdk_session_id:
                    self.sessions.bind_sdk_session(
                        verifier.binding_id,
                        result.sdk_session_id,
                        owner_id=self.owner_id,
                        lease_epoch=verifier_lease.lease_epoch,
                    )
                self.runtime.record_assessment(result.output)
                self.sessions.end_attempt(
                    verifier.attempt_id,
                    owner_id=self.owner_id,
                    lease_epoch=verifier_lease.lease_epoch,
                    status="completed",
                    reason="assessment recorded",
                )
            except Exception:
                self.sessions.end_attempt(
                    verifier.attempt_id,
                    owner_id=self.owner_id,
                    lease_epoch=verifier_lease.lease_epoch,
                    status="failed",
                    reason="verifier failed",
                )
                raise
            return self.runtime.evaluate_candidate(
                task_id, claim.claim_id, result.output.assessment_id
            )
        except Exception as exc:  # noqa: BLE001 - failed role becomes an explicit inconclusive task.
            return self.runtime.mark_inconclusive(
                task_id, (f"role execution failed: {type(exc).__name__}: {exc}",)
            )
