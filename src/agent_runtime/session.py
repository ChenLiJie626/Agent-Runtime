"""Attempt, binding, lease and handoff services (SPEC 003/006)."""

from __future__ import annotations

import time
import uuid
from dataclasses import dataclass

from .codec import digest, to_plain
from .domain import EvidenceRef, Handoff
from .errors import Conflict, InvalidInput, SessionUnavailable
from .runtime import DefectRuntime
from .store import Mutation


@dataclass(frozen=True)
class SessionBinding:
    binding_id: str
    analysis_id: str
    task_id: str
    attempt_id: str
    role: str
    snapshot_digest: str
    profile_digest: str
    tool_policy_digest: str
    working_dir: str
    sdk_session_id: str | None = None

    @property
    def binding_digest(self) -> str:
        return digest(self)


@dataclass(frozen=True)
class Lease:
    binding_id: str
    owner_id: str
    lease_epoch: int
    expires_at: float


class SessionService:
    def __init__(self, runtime: DefectRuntime) -> None:
        self.runtime = runtime
        self.store = runtime.store

    def start_attempt(
        self,
        task_id: str,
        role: str,
        *,
        working_dir: str,
        owner_id: str,
        lease_seconds: float = 60.0,
        sdk_session_id: str | None = None,
        predecessor_attempt_id: str | None = None,
        _transition_id: str | None = None,
    ) -> tuple[SessionBinding, Lease]:
        if not working_dir or not owner_id or lease_seconds <= 0:
            raise InvalidInput(
                "attempt needs working directory, owner and positive lease"
            )
        task = self.runtime.task(task_id)
        analysis = self.runtime.analysis(task["analysis_id"])
        kind = self.runtime.role_kind(task["analysis_id"], role)
        expected_phases = {
            "investigator": {"planned", "investigating"},
            "specialist": {"investigating"},
            "verifier": {"investigating", "verifying"},
        }
        if task["phase"] not in expected_phases[kind]:
            raise Conflict("role cannot start from the current task phase")
        previous_binding = None
        if predecessor_attempt_id:
            predecessor = self.store.get("attempt", predecessor_attempt_id)
            previous_binding = next(
                (
                    item
                    for item in self.store.list("binding", task["analysis_id"])
                    if item["attempt_id"] == predecessor_attempt_id
                ),
                None,
            )
            if (
                not predecessor
                or predecessor["task_id"] != task_id
                or predecessor["role"] != role
            ):
                raise SessionUnavailable("predecessor role/task differs")
            if predecessor["status"] not in {
                "completed",
                "paused",
                "failed",
                "cancelled",
                "interrupted",
            }:
                raise Conflict("predecessor attempt is still active")
            if sdk_session_id and (
                not previous_binding
                or previous_binding["sdk_session_id"] != sdk_session_id
                or previous_binding["working_dir"] != working_dir
                or previous_binding["snapshot_digest"] != task["snapshot_digest"]
                or previous_binding["profile_digest"]
                != analysis["snapshot"]["rule_profile_digest"]
                or previous_binding["tool_policy_digest"]
                != analysis["snapshot"]["tool_policy_digest"]
            ):
                raise SessionUnavailable("explicit resume binding differs")
        if sdk_session_id:
            matching_history = []
            for prior in self.store.list("binding", task["analysis_id"]):
                if prior["sdk_session_id"] == sdk_session_id and (
                    prior["task_id"] != task_id or prior["role"] != role
                ):
                    raise Conflict("SDK history belongs to another task or role")
                if prior["sdk_session_id"] == sdk_session_id:
                    if self.store.connection.execute(
                        "SELECT 1 FROM leases WHERE binding_id=?",
                        (prior["binding_id"],),
                    ).fetchone():
                        raise Conflict("SDK session already has a writer")
                    matching_history.append(prior["attempt_id"])
            if matching_history and predecessor_attempt_id not in matching_history:
                raise SessionUnavailable(
                    "SDK resume requires an explicit predecessor attempt"
                )
        attempt_id = str(uuid.uuid4())
        binding = SessionBinding(
            str(uuid.uuid4()),
            task["analysis_id"],
            task_id,
            attempt_id,
            role,
            task["snapshot_digest"],
            analysis["snapshot"]["rule_profile_digest"],
            analysis["snapshot"]["tool_policy_digest"],
            working_dir,
            sdk_session_id,
        )
        lease = Lease(binding.binding_id, owner_id, 1, time.time() + lease_seconds)
        attempt = {
            "attempt_id": attempt_id,
            "task_id": task_id,
            "role": role,
            "status": "running",
            "predecessor_attempt_id": predecessor_attempt_id,
            "binding_id": binding.binding_id,
        }
        connection = self.store.connection
        try:
            connection.execute("BEGIN IMMEDIATE")
            transition = None
            if _transition_id:
                transition = self.store.get("transition", _transition_id)
                if (transition is None or transition["status"] != "pending"
                        or transition["task_id"] != task_id
                        or previous_binding is None
                        or transition["old_binding_id"]
                        != previous_binding["binding_id"]):
                    raise Conflict("session transition is not pending for predecessor")
            if (
                sdk_session_id
                and connection.execute(
                    "SELECT 1 FROM leases WHERE sdk_session_id=?", (sdk_session_id,)
                ).fetchone()
            ):
                raise Conflict("SDK session already has a writer")
            connection.execute(
                "INSERT INTO leases VALUES (?,?,?,?,?,?)",
                (
                    binding.binding_id,
                    sdk_session_id,
                    owner_id,
                    1,
                    lease.expires_at,
                    binding.binding_digest,
                ),
            )
            mutations = []
            phase = "verifying" if kind == "verifier" else "investigating"
            projected_task = task
            if task["phase"] != phase:
                projected_task = {**projected_task, "phase": phase}
                mutations.append(
                    Mutation(
                        "TaskPhaseChanged",
                        {
                            "old_phase": task["phase"],
                            "new_phase": phase,
                            "reason": f"{role} started",
                        },
                        "task",
                        task_id,
                        projected_task,
                        task_id,
                    )
                )
            if task["execution_state"] == "pending":
                projected_task = {**projected_task, "execution_state": "running"}
                mutations.append(
                    Mutation(
                        "TaskExecutionChanged",
                        {
                            "old_status": "pending",
                            "new_status": "running",
                            "reason": "first role started",
                        },
                        "task",
                        task_id,
                        projected_task,
                        task_id,
                    )
                )
            mutations.extend(
                [
                    Mutation(
                        "AttemptCreated",
                        {
                            "attempt_id": attempt_id,
                            "task_id": task_id,
                            "role": role,
                            "predecessor_attempt_id": predecessor_attempt_id,
                        },
                        "attempt",
                        attempt_id,
                        attempt,
                        task_id,
                        attempt_id,
                    ),
                    Mutation(
                        "SessionLeaseAcquired",
                        {
                            "binding_id": binding.binding_id,
                            "owner_id": owner_id,
                            "lease_epoch": 1,
                            "expires_at": lease.expires_at,
                        },
                        "binding",
                        binding.binding_id,
                        to_plain(binding),
                        task_id,
                        attempt_id,
                    ),
                    Mutation(
                        "TaskStarted",
                        {
                            "task_id": task_id,
                            "attempt_id": attempt_id,
                            "role": role,
                            "binding_digest": binding.binding_digest,
                            "lease_epoch": 1,
                        },
                        task_id=task_id,
                        attempt_id=attempt_id,
                    ),
                ]
            )
            if transition is not None:
                mutations.append(
                    Mutation(
                        "TransitionCompleted",
                        {"transition_id": _transition_id,
                         "old_binding_id": transition["old_binding_id"],
                         "new_binding_id": binding.binding_id,
                         "handoff_id": transition["handoff_id"]},
                        "transition", _transition_id,
                        {**transition, "new_binding_id": binding.binding_id,
                         "status": "completed"},
                        task_id, attempt_id,
                    )
                )
            self.store._append_locked(task["analysis_id"], mutations)
            connection.execute("COMMIT")
        except Exception:
            if connection.in_transaction:
                connection.execute("ROLLBACK")
            raise
        return binding, lease

    def assert_lease(self, binding_id: str, owner_id: str, lease_epoch: int) -> None:
        row = self.store.connection.execute(
            "SELECT owner_id,lease_epoch,expires_at FROM leases WHERE binding_id=?",
            (binding_id,),
        ).fetchone()
        if (
            not row
            or row["owner_id"] != owner_id
            or row["lease_epoch"] != lease_epoch
            or row["expires_at"] <= time.time()
        ):
            raise Conflict("stale or expired session lease")

    def bind_sdk_session(
        self, binding_id: str, sdk_session_id: str, *, owner_id: str, lease_epoch: int
    ) -> SessionBinding:
        self.assert_lease(binding_id, owner_id, lease_epoch)
        raw = self.store.get("binding", binding_id)
        if raw is None:
            raise InvalidInput("binding does not exist")
        if raw["sdk_session_id"] and raw["sdk_session_id"] != sdk_session_id:
            raise Conflict("binding already has another SDK session")
        matching_history = []
        for prior in self.store.list("binding", raw["analysis_id"]):
            if prior["sdk_session_id"] == sdk_session_id and (
                prior["task_id"] != raw["task_id"] or prior["role"] != raw["role"]
            ):
                raise Conflict("SDK history belongs to another task or role")
            if (
                prior["sdk_session_id"] == sdk_session_id
                and prior["binding_id"] != binding_id
            ):
                matching_history.append(prior["attempt_id"])
        if matching_history:
            attempt = self.store.get("attempt", raw["attempt_id"])
            if attempt["predecessor_attempt_id"] not in matching_history:
                raise SessionUnavailable(
                    "SDK resume requires an explicit predecessor attempt"
                )
        updated = SessionBinding(**{**raw, "sdk_session_id": sdk_session_id})
        connection = self.store.connection
        try:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT owner_id,lease_epoch,expires_at FROM leases WHERE binding_id=?",
                (binding_id,),
            ).fetchone()
            if (
                not row
                or row["owner_id"] != owner_id
                or row["lease_epoch"] != lease_epoch
                or row["expires_at"] <= time.time()
            ):
                raise Conflict("stale session lease")
            connection.execute(
                "UPDATE leases SET sdk_session_id=?,binding_digest=? WHERE binding_id=?",
                (sdk_session_id, updated.binding_digest, binding_id),
            )
            self.store._append_locked(
                raw["analysis_id"],
                [
                    Mutation(
                        "SessionBound",
                        {"binding_id": binding_id, "sdk_session_id": sdk_session_id},
                        "binding",
                        binding_id,
                        to_plain(updated),
                        raw["task_id"],
                        raw["attempt_id"],
                    )
                ],
            )
            connection.execute("COMMIT")
        except Exception:
            if connection.in_transaction:
                connection.execute("ROLLBACK")
            raise
        return updated

    def end_attempt(
        self,
        attempt_id: str,
        *,
        owner_id: str,
        lease_epoch: int,
        status: str,
        reason: str,
    ) -> None:
        if status not in {"completed", "paused", "failed", "cancelled", "interrupted"}:
            raise InvalidInput("invalid attempt terminal status")
        attempt = self.store.get("attempt", attempt_id)
        if attempt is None:
            raise InvalidInput("attempt does not exist")
        self.assert_lease(attempt["binding_id"], owner_id, lease_epoch)
        task = self.runtime.task(attempt["task_id"])
        connection = self.store.connection
        try:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT owner_id,lease_epoch,expires_at FROM leases WHERE binding_id=?",
                (attempt["binding_id"],),
            ).fetchone()
            if (
                not row
                or row["owner_id"] != owner_id
                or row["lease_epoch"] != lease_epoch
                or row["expires_at"] <= time.time()
            ):
                raise Conflict("stale session lease")
            self.store._append_locked(
                task["analysis_id"],
                [
                    Mutation(
                        "AttemptEnded",
                        {
                            "attempt_id": attempt_id,
                            "terminal_status": status,
                            "reason": reason,
                        },
                        "attempt",
                        attempt_id,
                        {**attempt, "status": status},
                        attempt["task_id"],
                        attempt_id,
                    ),
                    Mutation(
                        "SessionLeaseReleased",
                        {
                            "binding_id": attempt["binding_id"],
                            "lease_epoch": lease_epoch,
                            "reason": status,
                        },
                        task_id=attempt["task_id"],
                        attempt_id=attempt_id,
                    ),
                ],
            )
            connection.execute(
                "DELETE FROM leases WHERE binding_id=?", (attempt["binding_id"],)
            )
            connection.execute("COMMIT")
        except Exception:
            if connection.in_transaction:
                connection.execute("ROLLBACK")
            raise

    def protected_items(self, task_id: str) -> set[str]:
        task = self.runtime.task(task_id)
        protected: set[str] = set()
        for check in self.runtime.checks(task_id):
            if check["status"] != "complete":
                protected.add(f"check:{check['check_id']}")
            protected.update(f"unknown:{unknown}" for unknown in check["unknowns"])
        for fact in self.store.list("fact", task["analysis_id"]):
            if fact["task_id"] == task_id:
                protected.add(f"fact:{fact['fact_id']}")
        for note in self.store.list("specialist_note", task["analysis_id"]):
            if note["task_id"] == task_id:
                protected.add(f"specialist_note:{note['note_id']}")
        for exclusion in self.store.list("exclusion", task["analysis_id"]):
            if (
                exclusion["candidate_digest"] == task["candidate_digest"]
                and exclusion["status"] == "valid"
            ):
                protected.add(f"exclusion:{exclusion['exclusion_id']}")
        return protected

    def effective_protected_items(self, task_id: str) -> set[str]:
        """Keep prior handoff entries visible until their resolution is recorded."""
        task = self.runtime.task(task_id)
        current = self.protected_items(task_id)
        previous = self.latest_handoff(task_id)
        if previous is None:
            return current
        resolved = {
            item["item_id"]
            for item in self.store.list("protected_resolution", task["analysis_id"])
            if item["task_id"] == task_id
            and item["prior_handoff_id"] == previous.handoff_id
        }
        return current | (set(previous.protected_item_ids) - resolved)

    def resolve_protected_item(
        self, task_id: str, item_id: str, *,
        evidence_refs: tuple[EvidenceRef, ...], reason: str,
    ) -> str:
        """Authorize removal from the next Handoff after a traced state change."""
        task = self.runtime.task(task_id)
        previous = self.latest_handoff(task_id)
        if (previous is None or item_id not in previous.protected_item_ids
                or item_id in self.protected_items(task_id)
                or not evidence_refs or not reason):
            raise InvalidInput("protected item is unresolved or has no evidence")
        for ref in evidence_refs:
            self.runtime.validate_ref(task_id, ref)
        kind, _, value = item_id.partition(":")
        if kind not in {"check", "unknown", "exclusion"} or not value:
            raise InvalidInput("unsupported protected item resolution")
        changed = any(
            event["seq"] > previous.event_high_watermark
            and event["task_id"] == task_id
            and (
                (kind == "check"
                 and event["event_type"] == "CheckUpdated"
                 and event["payload"]["check_id"] == value
                 and event["payload"]["prior_status"] != "complete"
                 and event["payload"]["new_status"] == "complete")
                or (kind == "unknown"
                    and event["event_type"] == "CheckUpdated"
                    and value in event["payload"].get("prior_unknowns", ())
                    and value not in event["payload"].get("new_unknowns", ()))
                or (kind == "exclusion"
                    and event["event_type"] == "ExclusionChanged"
                    and event["payload"]["exclusion_id"] == value)
            )
            for event in self.store.events(task["analysis_id"])
        )
        if not changed:
            raise Conflict("protected item lacks a later state transition")
        resolution_id = str(uuid.uuid4())
        self.store.append(
            task["analysis_id"],
            Mutation(
                "ProtectedItemResolved",
                {"resolution_id": resolution_id, "item_id": item_id,
                 "reason": reason,
                 "evidence_refs": to_plain(evidence_refs),
                 "prior_handoff_id": previous.handoff_id},
                "protected_resolution", resolution_id,
                {"resolution_id": resolution_id, "task_id": task_id,
                 "item_id": item_id, "reason": reason,
                 "evidence_refs": to_plain(evidence_refs),
                 "prior_handoff_id": previous.handoff_id},
                task_id,
            ),
        )
        return resolution_id

    def checkpoint_handoff(self, handoff: Handoff) -> None:
        task = self.runtime.task(handoff.task_id)
        attempt = self.store.get("attempt", handoff.from_attempt_id)
        if not attempt or attempt["task_id"] != handoff.task_id:
            raise InvalidInput("handoff attempt does not belong to task")
        if (
            handoff.snapshot_digest != task["snapshot_digest"]
            or handoff.candidate_digest != task["candidate_digest"]
        ):
            raise InvalidInput("handoff scope differs from task")
        current = self.store.high_watermark(task["analysis_id"])
        if handoff.event_high_watermark != current:
            raise Conflict("handoff event watermark is stale")
        previous = [
            item
            for item in self.store.list("handoff", task["analysis_id"])
            if item["task_id"] == handoff.task_id
        ]
        required = self.protected_items(handoff.task_id)
        if previous:
            newest = max(previous, key=lambda item: item["event_high_watermark"])
            removed = set(newest["protected_item_ids"]) - set(
                handoff.protected_item_ids
            )
            resolutions = {
                item["item_id"]
                for item in self.store.list("protected_resolution", task["analysis_id"])
                if item["task_id"] == handoff.task_id
                and item["prior_handoff_id"] == newest["handoff_id"]
            }
            unresolved_removals = removed - resolutions
            if unresolved_removals:
                raise InvalidInput(
                    f"handoff drops unresolved prior items: {sorted(unresolved_removals)}"
                )
        missing = required - set(handoff.protected_item_ids)
        if missing:
            raise InvalidInput(f"handoff drops protected items: {sorted(missing)}")
        if self.store.get("handoff", handoff.handoff_id):
            raise Conflict("handoff ID already exists")
        self.store.append(
            task["analysis_id"],
            Mutation(
                "HandoffSaved",
                {
                    "handoff_id": handoff.handoff_id,
                    "protected_item_ids": handoff.protected_item_ids,
                    "event_high_watermark": current,
                    "digest": digest(handoff),
                },
                "handoff",
                handoff.handoff_id,
                to_plain(handoff),
                handoff.task_id,
                handoff.from_attempt_id,
            ),
        )

    def latest_handoff(self, task_id: str) -> Handoff | None:
        task = self.runtime.task(task_id)
        candidates = [
            item
            for item in self.store.list("handoff", task["analysis_id"])
            if item["task_id"] == task_id
        ]
        if not candidates:
            return None
        raw = max(candidates, key=lambda item: item["event_high_watermark"])
        return Handoff(**{
            **raw,
            "protected_item_ids": tuple(raw["protected_item_ids"]),
            "next_actions": tuple(raw.get("next_actions", ())),
        })

    def start_fresh_from_handoff(
        self, old_binding_id: str, *, reason: str, owner_id: str,
        working_dir: str,
    ) -> tuple[SessionBinding, Lease]:
        """Record an unavailable SDK history and continue from domain state."""
        old = self.store.get("binding", old_binding_id)
        if old is None:
            raise InvalidInput("old session binding does not exist")
        if not reason:
            raise InvalidInput("unavailable history needs a reason")
        attempt = self.store.get("attempt", old["attempt_id"])
        if attempt is None or attempt["status"] not in {
            "completed", "paused", "failed", "cancelled", "interrupted",
        }:
            raise Conflict("old attempt must end before starting a fresh session")
        if self.store.connection.execute(
            "SELECT 1 FROM leases WHERE binding_id=?", (old_binding_id,)
        ).fetchone():
            raise Conflict("old SDK history still has a writer")
        task = self.runtime.task(old["task_id"])
        if any(
            item["predecessor_attempt_id"] == old["attempt_id"]
            for item in self.store.list("attempt", task["analysis_id"])
        ):
            raise Conflict("old attempt already has a recovery successor")
        analysis = self.runtime.analysis(task["analysis_id"])
        handoff = self.latest_handoff(old["task_id"])
        if (handoff is None or handoff.from_attempt_id != old["attempt_id"]
                or handoff.snapshot_digest != task["snapshot_digest"]
                or handoff.candidate_digest != task["candidate_digest"]
                or old["profile_digest"]
                != analysis["snapshot"]["rule_profile_digest"]
                or old["tool_policy_digest"]
                != analysis["snapshot"]["tool_policy_digest"]
                or self.protected_items(old["task_id"])
                - set(handoff.protected_item_ids)):
            raise SessionUnavailable("handoff no longer covers the bound task")
        transition_id = str(uuid.uuid4())
        self.store.append(
            task["analysis_id"],
            Mutation(
                "SessionUnavailable",
                {"binding_id": old_binding_id,
                 "sdk_session_id": old["sdk_session_id"],
                 "reason": reason,
                 "recovery_handoff_id": handoff.handoff_id},
                task_id=old["task_id"], attempt_id=old["attempt_id"],
            ),
            Mutation(
                "TransitionCreated",
                {"transition_id": transition_id,
                 "old_binding_id": old_binding_id,
                 "new_binding_id": None,
                 "handoff_id": handoff.handoff_id},
                "transition", transition_id,
                {"transition_id": transition_id, "task_id": old["task_id"],
                 "old_binding_id": old_binding_id, "new_binding_id": None,
                 "handoff_id": handoff.handoff_id, "status": "pending"},
                old["task_id"], old["attempt_id"],
            ),
        )
        return self.start_attempt(
            old["task_id"], old["role"], working_dir=working_dir,
            owner_id=owner_id, predecessor_attempt_id=old["attempt_id"],
            _transition_id=transition_id,
        )

    def recover_expired_lease(self, binding_id: str) -> None:
        """Fence a dead writer before an explicit resume or fresh session."""
        binding = self.store.get("binding", binding_id)
        if binding is None:
            raise InvalidInput("binding does not exist")
        attempt = self.store.get("attempt", binding["attempt_id"])
        connection = self.store.connection
        try:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT lease_epoch,expires_at FROM leases WHERE binding_id=?",
                (binding_id,),
            ).fetchone()
            if not row or row["expires_at"] > time.time():
                raise Conflict("lease is not expired")
            self.store._append_locked(
                binding["analysis_id"],
                [
                    Mutation(
                        "AttemptEnded",
                        {
                            "attempt_id": binding["attempt_id"],
                            "terminal_status": "interrupted",
                            "reason": "expired lease recovery",
                        },
                        "attempt",
                        binding["attempt_id"],
                        {**attempt, "status": "interrupted"},
                        binding["task_id"],
                        binding["attempt_id"],
                    ),
                    Mutation(
                        "SessionLeaseReleased",
                        {
                            "binding_id": binding_id,
                            "lease_epoch": row["lease_epoch"],
                            "reason": "expired",
                        },
                        task_id=binding["task_id"],
                        attempt_id=binding["attempt_id"],
                    ),
                ],
            )
            connection.execute("DELETE FROM leases WHERE binding_id=?", (binding_id,))
            connection.execute("COMMIT")
        except Exception:
            if connection.in_transaction:
                connection.execute("ROLLBACK")
            raise

    def rotate(
        self,
        handoff: Handoff,
        *,
        owner_id: str,
        lease_epoch: int,
        next_owner_id: str,
        working_dir: str,
    ) -> tuple[SessionBinding, Lease]:
        """Checkpoint, close the old writer, then prepare a fresh SDK session."""
        task = self.runtime.task(handoff.task_id)
        old_attempt = self.store.get("attempt", handoff.from_attempt_id)
        if not old_attempt or old_attempt["task_id"] != handoff.task_id:
            raise InvalidInput("rotation attempt differs from task")
        self.assert_lease(old_attempt["binding_id"], owner_id, lease_epoch)
        pending = [
            query
            for query in self.store.list("query", task["analysis_id"])
            if query["request"]["task_id"] == handoff.task_id
            and query["status"] in {"requested", "running", "in_doubt"}
        ]
        if pending:
            raise Conflict("rotation requires no pending query")
        self.checkpoint_handoff(handoff)
        transition_id = str(uuid.uuid4())
        self.store.append(
            task["analysis_id"],
            Mutation(
                "TransitionCreated",
                {
                    "transition_id": transition_id,
                    "old_binding_id": old_attempt["binding_id"],
                    "new_binding_id": None,
                    "handoff_id": handoff.handoff_id,
                },
                "transition",
                transition_id,
                {
                    "transition_id": transition_id,
                    "task_id": handoff.task_id,
                    "old_binding_id": old_attempt["binding_id"],
                    "new_binding_id": None,
                    "handoff_id": handoff.handoff_id,
                    "status": "pending",
                },
                handoff.task_id,
                handoff.from_attempt_id,
            ),
        )
        self.end_attempt(
            handoff.from_attempt_id,
            owner_id=owner_id,
            lease_epoch=lease_epoch,
            status="paused",
            reason="session rotation",
        )
        return self.start_attempt(
            handoff.task_id,
            old_attempt["role"],
            working_dir=working_dir,
            owner_id=next_owner_id,
            predecessor_attempt_id=handoff.from_attempt_id,
            _transition_id=transition_id,
        )
