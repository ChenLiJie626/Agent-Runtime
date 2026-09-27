"""Defect-specific, provenance-preserving context views (SPEC 006)."""

from __future__ import annotations

import uuid
from typing import Any

from .codec import canonical_json, digest
from .domain import ContextView
from .errors import Conflict, ContextBudgetExceeded, InvalidInput
from .runtime import DefectRuntime
from .session import SessionService


def _tokens(value: Any) -> int:
    # An upper-bound planning estimate; SDK usage remains authoritative.
    return (len(canonical_json(value).encode("utf-8")) + 2) // 3


class ContextViewBuilder:
    def __init__(
        self, runtime: DefectRuntime, session: SessionService | None = None
    ) -> None:
        self.runtime = runtime
        self.session = session or SessionService(runtime)

    def build(
        self,
        task_id: str,
        role: str,
        *,
        focus_check_ids: tuple[str, ...],
        token_budget: int,
        max_snippet_chars: int = 800,
    ) -> ContextView:
        task = self.runtime.task(task_id)
        self.runtime.role_kind(task["analysis_id"], role)
        if token_budget <= 0:
            raise InvalidInput("invalid context budget")
        if any(item not in task["required_check_ids"] for item in focus_check_ids):
            raise InvalidInput("focus check does not belong to task")
        analysis = self.runtime.analysis(task["analysis_id"])
        high_watermark = self.runtime.store.high_watermark(task["analysis_id"])
        protected_ids = self.session.effective_protected_items(task_id)
        protected: list[dict[str, Any]] = []
        checks = {item["check_id"]: item for item in self.runtime.checks(task_id)}
        facts = {
            item["fact_id"]: item
            for item in self.runtime.store.list("fact", task["analysis_id"])
            if item["task_id"] == task_id
        }
        exclusions = {
            item["exclusion_id"]: item
            for item in self.runtime.store.list("exclusion", task["analysis_id"])
            if item["candidate_digest"] == task["candidate_digest"]
        }
        notes = {
            item["note_id"]: item
            for item in self.runtime.store.list("specialist_note", task["analysis_id"])
            if item["task_id"] == task_id
        }
        for identifier in sorted(protected_ids):
            kind, record_id = identifier.split(":", 1)
            if kind == "check":
                value = checks[record_id]
            elif kind == "fact":
                value = facts[record_id]
            elif kind == "exclusion":
                value = exclusions[record_id]
            elif kind == "specialist_note":
                value = notes[record_id]
            elif kind == "unknown":
                value = {"unknown_id": record_id, "status": "open"}
            else:
                raise InvalidInput("unknown protected item kind")
            protected.append({"id": identifier, "kind": kind, "value": value})
        fixed = {
            "role": role,
            "task_id": task_id,
            "snapshot_digest": task["snapshot_digest"],
            "candidate_digest": task["candidate_digest"],
            "rule": analysis["rule"],
            "protected_items": protected,
            "focus_check_ids": focus_check_ids,
        }
        reserved = _tokens(fixed) + 64
        if reserved > token_budget:
            raise ContextBudgetExceeded(
                f"protected context needs at least {reserved} tokens"
            )
        remaining = token_budget - reserved
        evidence_ids: list[str] = []
        for check_id in (*focus_check_ids, *task["required_check_ids"]):
            for ref in checks[check_id]["evidence_refs"]:
                if ref["evidence_id"] not in evidence_ids:
                    evidence_ids.append(ref["evidence_id"])
        for fact in facts.values():
            for ref in fact["evidence_refs"]:
                if ref["evidence_id"] not in evidence_ids:
                    evidence_ids.append(ref["evidence_id"])
        for note in notes.values():
            for ref in (*note["support_refs"], *note["counter_refs"]):
                if ref["evidence_id"] not in evidence_ids:
                    evidence_ids.append(ref["evidence_id"])
        snippets: list[dict[str, Any]] = []
        omitted: list[dict[str, Any]] = []
        retrieval: list[str] = []
        seen_artifacts: dict[str, str] = {}
        for evidence_id in evidence_ids:
            ref = self.runtime.evidence_ref(evidence_id)
            self.runtime.validate_ref(task_id, ref)
            artifact = self.runtime.store.read_artifact(ref.artifact_digest)
            retrieval.append(evidence_id)
            if ref.artifact_digest in seen_artifacts:
                omitted.append(
                    {
                        "evidence_id": evidence_id,
                        "reason": "duplicate-content",
                        "same_as": seen_artifacts[ref.artifact_digest],
                        "artifact_digest": ref.artifact_digest,
                        "retrieval_ref": evidence_id,
                    }
                )
                continue
            seen_artifacts[ref.artifact_digest] = evidence_id
            text = artifact.decode("utf-8", errors="replace")
            excerpt = text[:max_snippet_chars]
            snippet = {
                "evidence_id": evidence_id,
                "artifact_digest": ref.artifact_digest,
                "text": excerpt,
                "truncated": len(text) > len(excerpt),
                "trust": "untrusted-source",
                "retrieval_ref": evidence_id,
            }
            needed = _tokens(snippet)
            if needed <= remaining:
                snippets.append(snippet)
                remaining -= needed
            else:
                omitted.append(
                    {
                        "evidence_id": evidence_id,
                        "reason": "budget",
                        "artifact_digest": ref.artifact_digest,
                        "retrieval_ref": evidence_id,
                    }
                )
        body = {
            **fixed,
            "event_high_watermark": high_watermark,
            "evidence_snippets": snippets,
            "omitted_items": omitted,
            "retrieval_refs": retrieval,
            "token_budget": token_budget,
        }
        if self.runtime.store.high_watermark(task["analysis_id"]) != high_watermark:
            raise Conflict("context changed while building view")
        estimated = _tokens(body) + 32
        if estimated > token_budget:
            # Metadata itself is protected; report the true minimum rather than dropping it.
            raise ContextBudgetExceeded(
                f"context metadata needs at least {estimated} tokens"
            )
        return ContextView(
            str(uuid.uuid4()),
            task_id,
            role,
            task["snapshot_digest"],
            analysis["snapshot"]["rule_profile_digest"],
            task["candidate_digest"],
            high_watermark,
            focus_check_ids,
            tuple(protected),
            tuple(snippets),
            tuple(omitted),
            tuple(retrieval),
            token_budget,
            estimated,
            digest(body),
        )
