"""Bundled executor implementations for deterministic and SDK-backed roles."""

from __future__ import annotations

import uuid

from .domain import Assessment, Claim, EvidenceRef
from .errors import InvalidInput
from .ports import AgentCapabilities, DomainTools, RoleRunRequest, RoleRunResult


class EvidenceReviewExecutor:
    """Deterministic evidence transport and gate executor.

    It reads every referenced artifact and mirrors committed facts/checks into
    role artifacts. It is useful for offline pipelines and contract tests; it
    does not infer new program semantics.
    """

    def describe_capabilities(self) -> AgentCapabilities:
        return AgentCapabilities(
            stream_events=False,
            explicit_resume=False,
            interrupt=False,
            structured_output=True,
            custom_tools=True,
            deny_tools=True,
            isolated_session=True,
            usage_reporting=False,
        )

    def run(self, request: RoleRunRequest, tools: DomainTools) -> RoleRunResult:
        items = tuple(request.context.protected_items)
        facts = [item["value"] for item in items if item["kind"] == "fact"]
        checks = [item["value"] for item in items if item["kind"] == "check"]
        if not facts:
            raise InvalidInput("evidence review requires at least one committed fact")
        scopes = {item["scope"] for item in facts}
        if len(scopes) != 1:
            raise InvalidInput("evidence review facts have conflicting scopes")

        evidence_roles: dict[str, str] = {}
        for fact in facts:
            role = "counter" if fact["polarity"] == "negative" else "support"
            for ref in fact["evidence_refs"]:
                evidence_roles.setdefault(ref["evidence_id"], role)
        refs: list[EvidenceRef] = []
        for evidence_id in request.context.retrieval_refs:
            tools.read_evidence(evidence_id)
            refs.append(tools.resolve_evidence_ref(
                evidence_id, evidence_roles.get(evidence_id, "context"),
            ))
        support = tuple(ref for ref in refs if ref.role != "counter")
        counter = tuple(ref for ref in refs if ref.role == "counter")
        unresolved = tuple(
            f"{item['check_id']}:{item['status']}"
            for item in checks
            if item["status"] != "complete"
        )
        scope = next(iter(scopes))
        if request.role_kind == "investigator":
            output = Claim(
                str(uuid.uuid4()),
                request.task_id,
                request.attempt_id,
                tuple(item["fact_id"] for item in facts),
                support,
                counter,
                unresolved,
                scope,
            )
        elif request.role_kind == "verifier":
            if request.claim is None:
                raise InvalidInput("verifier requires an investigator claim")
            output = Assessment(
                str(uuid.uuid4()),
                request.task_id,
                request.attempt_id,
                request.claim.claim_id,
                tuple(request.context.focus_check_ids),
                support,
                counter,
                unresolved,
                "deterministic review of committed evidence and check coverage",
                scope,
            )
        else:
            raise InvalidInput("evidence review supports investigator and verifier roles")
        return RoleRunResult(output)
