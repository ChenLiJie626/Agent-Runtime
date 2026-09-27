"""Optional Claude Agent SDK executor. Importing the core package needs no SDK."""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any

from ..codec import canonical_json, digest, to_plain
from ..domain import Assessment, Claim, EvidenceRef, QueryRequest, SpecialistNote
from ..errors import BackendFailure, InvalidInput, SessionUnavailable
from ..ports import (
    AgentCapabilities,
    DomainTools,
    RoleRunRequest,
    RoleRunResult,
)

REF_LIST = {"type": "array", "items": {"type": "string"}}


def _role_schema(role: str) -> dict[str, Any]:
    common = {
        "scope": {"type": "string"},
        "support_evidence_ids": REF_LIST,
        "counter_evidence_ids": REF_LIST,
        "unresolved_items": REF_LIST,
    }
    if role == "investigator":
        properties = {**common, "fact_ids": REF_LIST}
    elif role == "verifier":
        properties = {
            **common,
            "checked_predicates": REF_LIST,
            "recommendation": {"type": "string"},
        }
    elif role == "specialist":
        properties = {**common, "summary": {"type": "string"}}
    else:
        raise InvalidInput("unsupported Claude role kind")
    return {
        "type": "object",
        "properties": properties,
        "required": list(properties),
        "additionalProperties": False,
    }


QUERY_INPUT = {
    "type": "object",
    "properties": {
        "check_id": {"type": "string"},
        "operation": {"type": "string"},
        "scope": {"type": "object"},
        "args": {"type": "object"},
        "timeout_ms": {"type": "integer", "minimum": 1},
    },
    "required": ["check_id", "operation", "scope", "args", "timeout_ms"],
    "additionalProperties": False,
}

READ_INPUT = {
    "type": "object",
    "properties": {"evidence_id": {"type": "string"}},
    "required": ["evidence_id"],
    "additionalProperties": False,
}


@dataclass(frozen=True)
class ClaudeConfig:
    working_dir: str
    backend_id: str
    backend_version: str
    tool_policy_digest: str
    model: str | None = None
    max_turns: int = 12
    max_budget_usd: float | None = None
    max_tool_text_chars: int = 4000
    sdk_env: Mapping[str, str] = field(default_factory=dict, repr=False)
    role_timeout_seconds: float | None = None

    def __post_init__(self) -> None:
        if self.role_timeout_seconds is not None and (
            isinstance(self.role_timeout_seconds, bool)
            or not isinstance(self.role_timeout_seconds, (int, float))
            or not 0 < self.role_timeout_seconds < float("inf")
        ):
            raise InvalidInput("role timeout must be a finite positive number")


class ClaudeAgentExecutor:
    """A fresh ClaudeSDKClient is used for each role attempt.

    The application must run the SDK integration smoke test for its pinned
    CLI/SDK combination before using this executor on a real repository.
    """

    def __init__(self, config: ClaudeConfig) -> None:
        self.config = config

    def describe_capabilities(self) -> AgentCapabilities:
        # Report this executor's public surface, not every method offered by
        # ClaudeSDKClient. run() is synchronous and returns only the final
        # artifact/session ID; it exposes no event stream, live interrupt or
        # usage record to its caller yet.
        return AgentCapabilities(
            stream_events=False,
            explicit_resume=True,
            interrupt=False,
            structured_output=True,
            custom_tools=True,
            deny_tools=True,
            isolated_session=True,
            usage_reporting=False,
        )

    def _tools(self, request: RoleRunRequest, domain_tools: DomainTools):
        try:
            from claude_agent_sdk import create_sdk_mcp_server, tool
        except ImportError as exc:
            raise BackendFailure("install defect-agent-runtime[claude]") from exc

        @tool("query_program", "Run a scoped, read-only program query", QUERY_INPUT)
        async def query_program(arguments: dict[str, Any]) -> dict[str, Any]:
            try:
                request_key = digest(
                    {
                        "task_id": request.task_id,
                        "check_id": arguments["check_id"],
                        "operation": arguments["operation"],
                        "scope": arguments["scope"],
                        "args": arguments["args"],
                        "timeout_ms": arguments["timeout_ms"],
                    }
                )
                query = QueryRequest(
                    str(uuid.uuid4()),
                    request.task_id,
                    arguments["check_id"],
                    arguments["operation"],
                    request.context.snapshot_digest,
                    self.config.backend_id,
                    self.config.backend_version,
                    self.config.tool_policy_digest,
                    arguments["scope"],
                    arguments["args"],
                    request_key,
                    arguments["timeout_ms"],
                )
                outcome, reference = domain_tools.query(query)
                result = {
                    "query_id": outcome.query_id,
                    "status": outcome.status,
                    "coverage": to_plain(outcome.coverage),
                    "limitations": list(outcome.limitations),
                    "evidence_id": reference.evidence_id if reference else None,
                    "diagnostic": outcome.diagnostic,
                }
                return {"content": [{"type": "text", "text": canonical_json(result)}]}
            except Exception as exc:  # noqa: BLE001 - MCP must return a tool error, not crash the SDK loop.
                return {
                    "content": [
                        {"type": "text", "text": f"{type(exc).__name__}: {exc}"}
                    ],
                    "isError": True,
                }

        @tool("read_evidence", "Read a saved artifact by its evidence ID", READ_INPUT)
        async def read_evidence(arguments: dict[str, Any]) -> dict[str, Any]:
            try:
                reference = domain_tools.resolve_evidence_ref(
                    arguments["evidence_id"], "context"
                )
                raw = domain_tools.read_evidence(reference.evidence_id)
                excerpt = raw[: self.config.max_tool_text_chars].decode(
                    "utf-8", errors="replace"
                )
                result = {
                    "evidence_id": reference.evidence_id,
                    "artifact_digest": reference.artifact_digest,
                    "excerpt": excerpt,
                    "truncated": len(raw) > self.config.max_tool_text_chars,
                }
                return {"content": [{"type": "text", "text": canonical_json(result)}]}
            except Exception as exc:  # noqa: BLE001 - MCP must return a tool error, not crash the SDK loop.
                return {
                    "content": [
                        {"type": "text", "text": f"{type(exc).__name__}: {exc}"}
                    ],
                    "isError": True,
                }

        return create_sdk_mcp_server(
            name="defect_runtime", tools=[query_program, read_evidence]
        )

    def build_options(self, request: RoleRunRequest, domain_tools: DomainTools):
        try:
            from claude_agent_sdk import ClaudeAgentOptions
        except ImportError as exc:
            raise BackendFailure("install defect-agent-runtime[claude]") from exc
        server = self._tools(request, domain_tools)
        names = [
            "mcp__defect_runtime__query_program",
            "mcp__defect_runtime__read_evidence",
        ]
        return ClaudeAgentOptions(
            tools=names,
            allowed_tools=names,
            disallowed_tools=[
                "Bash",
                "Read",
                "Write",
                "Edit",
                "Glob",
                "Grep",
                "WebFetch",
                "WebSearch",
                "Agent",
                "NotebookEdit",
            ],
            permission_mode="dontAsk",
            setting_sources=[],
            env=dict(self.config.sdk_env),
            strict_mcp_config=True,
            mcp_servers={"defect_runtime": server},
            cwd=self.config.working_dir,
            model=self.config.model,
            max_turns=self.config.max_turns,
            max_budget_usd=self.config.max_budget_usd,
            output_format={"type": "json_schema", "schema": _role_schema(request.role_kind or request.role)},
            resume=request.resume_session_id,
        )

    def _prompt(self, request: RoleRunRequest) -> str:
        kind = request.role_kind or request.role
        instruction = (
            "Investigate the candidate and answer every required check. "
            "Use fact and evidence IDs already present in ContextView; query only "
            "when a required check lacks material. Preserve counterevidence and "
            "unknowns. Never invent an ID."
            if kind == "investigator" else
            "Focus only on the assigned open checks. Return a concise sourced note, "
            "including counterevidence and unresolved conditions. Existing "
            "ContextView evidence IDs may be cited without requerying."
            if kind == "specialist" else
            "Independently challenge the investigator Claim and specialist notes. "
            "Use the rule's exact check IDs in checked_predicates. Check scope "
            "and counterevidence. Existing ContextView evidence may be cited "
            "without requerying. Report unresolved conditions; never invent IDs."
        )
        content = {
            "role": request.role,
            "role_kind": kind,
            "task_id": request.task_id,
            "attempt_id": request.attempt_id,
            "context": to_plain(request.context),
        }
        if request.claim:
            content["claim"] = to_plain(request.claim)
        if request.specialist_notes:
            content["specialist_notes"] = to_plain(request.specialist_notes)
        return instruction + "\n\n" + canonical_json(content)

    def _parse(
        self, request: RoleRunRequest, value: dict[str, Any], domain_tools: DomainTools
    ) -> Claim | Assessment | SpecialistNote:
        def resolve(ids: list[str], role: str) -> tuple[EvidenceRef, ...]:
            return tuple(domain_tools.resolve_evidence_ref(item, role) for item in ids)

        support = resolve(value["support_evidence_ids"], "support")
        counter = resolve(value["counter_evidence_ids"], "counter")
        kind = request.role_kind or request.role
        if kind == "investigator":
            return Claim(
                str(uuid.uuid4()),
                request.task_id,
                request.attempt_id,
                tuple(value["fact_ids"]),
                support,
                counter,
                tuple(value["unresolved_items"]),
                value["scope"],
            )
        if kind == "specialist":
            return SpecialistNote(
                str(uuid.uuid4()), request.task_id, request.attempt_id,
                request.role, value["summary"], support, counter,
                tuple(value["unresolved_items"]),
            )
        if request.claim is None:
            raise InvalidInput("verifier needs a Claim")
        return Assessment(
            str(uuid.uuid4()),
            request.task_id,
            request.attempt_id,
            request.claim.claim_id,
            tuple(value["checked_predicates"]),
            support,
            counter,
            tuple(value["unresolved_items"]),
            value["recommendation"],
            value["scope"],
        )

    async def _run_async(
        self, request: RoleRunRequest, domain_tools: DomainTools
    ) -> RoleRunResult:
        try:
            from claude_agent_sdk import ClaudeSDKClient, ResultMessage
        except ImportError as exc:
            raise BackendFailure("install defect-agent-runtime[claude]") from exc
        options = self.build_options(request, domain_tools)
        final = None
        async with ClaudeSDKClient(options=options) as client:
            await client.query(self._prompt(request))
            async for message in client.receive_response():
                if isinstance(message, ResultMessage):
                    final = message
        if final is None or final.is_error or final.subtype != "success":
            raise BackendFailure(
                f"Claude role run failed: {getattr(final, 'subtype', 'no result')}"
            )
        if not isinstance(final.structured_output, dict):
            raise BackendFailure("Claude did not return structured role output")
        output = self._parse(request, final.structured_output, domain_tools)
        return RoleRunResult(output, final.session_id)

    def run(self, request: RoleRunRequest, tools: DomainTools) -> RoleRunResult:
        if (request.role_kind or request.role) not in {
            "investigator", "specialist", "verifier"
        }:
            raise InvalidInput("unsupported Claude role")
        if request.resume_session_id:
            try:
                from claude_agent_sdk import get_session_messages
            except ImportError as exc:
                raise BackendFailure("install defect-agent-runtime[claude]") from exc
            try:
                history = get_session_messages(
                    request.resume_session_id, directory=self.config.working_dir
                )
            except (OSError, ValueError) as exc:
                raise SessionUnavailable(
                    "SDK transcript cannot be read for explicit resume"
                ) from exc
            if not history:
                raise SessionUnavailable(
                    "SDK transcript is missing for explicit resume"
                )
        async def bounded_run():
            return await asyncio.wait_for(
                self._run_async(request, tools), timeout=self.config.role_timeout_seconds
            )

        return asyncio.run(bounded_run())
