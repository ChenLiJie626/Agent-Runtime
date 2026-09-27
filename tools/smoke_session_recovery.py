"""Bounded live SDK probe: missing transcript -> domain Handoff -> fresh session."""

from __future__ import annotations

import argparse
import json
import tempfile
import uuid
from pathlib import Path

from agent_runtime import (
    CandidateIdentity, CheckDefinition, CheckItem, Claim, ContextViewBuilder,
    DefectRuntime, FactAssessment, FixedSnapshot, Handoff, QueryRequest,
    RoleRunRequest, RuleSpec, SQLiteStore, SessionService, SessionUnavailable,
)
from agent_runtime.adapters import ClaudeAgentExecutor, ClaudeConfig
from agent_runtime.codec import digest
from agent_runtime.coordinator import _ScopedTools

from smoke_foundation_runtime import FixtureBackend, FixtureEvaluator, local_route


def run() -> dict:
    sdk_env, model, route_host = local_route()
    with tempfile.TemporaryDirectory(prefix="foundation-recovery-") as directory:
        root = Path(directory)
        store = SQLiteStore(root / "state.sqlite3", root / "artifacts")
        try:
            runtime = DefectRuntime(store, evaluator=FixtureEvaluator())
            rule = RuleSpec(
                "foundation.recovery", "1", "Transcript recovery fixture",
                (CheckDefinition("observed", "Is the condition observed?",
                                 ("observed",)),),
                FixtureEvaluator.evaluator_id, FixtureEvaluator.evaluator_version,
            )
            snapshot = FixedSnapshot(
                "fixture-repository", "fixture-scope", "a" * 64,
                digest(rule), "b" * 64,
            )
            analysis_id = runtime.create_analysis(snapshot, rule)
            task = runtime.propose_candidate(
                analysis_id,
                CandidateIdentity(
                    rule.rule_id, rule.rule_version, snapshot.snapshot_digest,
                    {"subject": "case-A"}, "case-A",
                ),
                discovery_ref="synthetic-fixture",
            )
            backend = FixtureBackend()
            outcome, ref = runtime.query_program(
                QueryRequest(
                    str(uuid.uuid4()), task.task_id, "observed", "inspect",
                    snapshot.snapshot_digest, backend.backend_id,
                    backend.backend_version, snapshot.tool_policy_digest,
                    {"scope_id": snapshot.scope_id,
                     "selectors": {"subject": "case-A"}, "max_results": 1},
                    {"subject": "case-A"}, str(uuid.uuid4()), 1000,
                ), backend,
            )
            fact = FactAssessment(
                str(uuid.uuid4()), task.task_id, "observed", "observed",
                "positive", (ref,), "case-A", "tool_observed",
            )
            runtime.record_fact(fact)
            runtime.update_check(CheckItem(
                "observed", task.task_id, "Is the condition observed?",
                "complete", "supported", (ref,), outcome.coverage,
            ))
            sessions = SessionService(runtime)
            lost_id = str(uuid.uuid4())
            old, old_lease = sessions.start_attempt(
                task.task_id, "investigator", working_dir=str(root),
                owner_id="old", sdk_session_id=lost_id,
            )
            handoff = Handoff(
                str(uuid.uuid4()), task.task_id, old.attempt_id,
                task.snapshot_digest, task.candidate_digest,
                store.high_watermark(analysis_id),
                tuple(sorted(sessions.protected_items(task.task_id))),
            )
            sessions.checkpoint_handoff(handoff)
            sessions.end_attempt(
                old.attempt_id, owner_id="old",
                lease_epoch=old_lease.lease_epoch, status="paused",
                reason="probe transcript loss",
            )
            executor = ClaudeAgentExecutor(ClaudeConfig(
                str(root), backend.backend_id, backend.backend_version,
                snapshot.tool_policy_digest, model=model, max_turns=8,
                max_budget_usd=0.5, sdk_env=sdk_env,
            ))
            old_context = ContextViewBuilder(runtime, sessions).build(
                task.task_id, "investigator", focus_check_ids=("observed",),
                token_budget=5000,
            )
            detected = False
            try:
                executor.run(
                    RoleRunRequest(
                        task.task_id, old.attempt_id, "investigator",
                        old_context, resume_session_id=lost_id,
                    ),
                    _ScopedTools(
                        runtime, sessions, backend, task.task_id,
                        old.binding_id, "old", old_lease.lease_epoch,
                    ),
                )
            except SessionUnavailable:
                detected = True
            if not detected:
                raise RuntimeError("missing transcript was not detected")
            new, lease = sessions.start_fresh_from_handoff(
                old.binding_id, reason="SDK transcript unavailable",
                owner_id="new", working_dir=str(root),
            )
            view = ContextViewBuilder(runtime, sessions).build(
                task.task_id, "investigator", focus_check_ids=("observed",),
                token_budget=5000,
            )
            result = executor.run(
                RoleRunRequest(
                    task.task_id, new.attempt_id, "investigator", view,
                ),
                _ScopedTools(
                    runtime, sessions, backend, task.task_id,
                    new.binding_id, "new", lease.lease_epoch,
                ),
            )
            sessions.bind_sdk_session(
                new.binding_id, result.sdk_session_id,
                owner_id="new", lease_epoch=lease.lease_epoch,
            )
            runtime.record_claim(result.output)
            sessions.end_attempt(
                new.attempt_id, owner_id="new", lease_epoch=lease.lease_epoch,
                status="completed", reason="fresh claim recorded",
            )
            transition = store.list("transition", analysis_id)[0]
            bound_session = store.get("binding", new.binding_id)["sdk_session_id"]
            events = [event["event_type"] for event in store.events(analysis_id)]
            passed = (
                isinstance(result.output, Claim) and detected
                and result.sdk_session_id and result.sdk_session_id != lost_id
                and bound_session == result.sdk_session_id
                and transition["status"] == "completed"
                and transition["new_binding_id"] == new.binding_id
                and "SessionUnavailable" in events
                and "TransitionCompleted" in events
                and runtime.checks(task.task_id)[0]["status"] == "complete"
            )
            return {
                "status": "passed" if passed else "failed",
                "missing_transcript_detected": detected,
                "new_sdk_session": bool(result.sdk_session_id),
                "transition_status": transition["status"],
                "claim_recorded": bool(store.get("claim", result.output.claim_id)),
                "check_status": runtime.checks(task.task_id)[0]["status"],
                "route_host": route_host,
                "model_alias": model,
            }
        finally:
            store.close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    report = run()
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2))
    print(json.dumps(report, ensure_ascii=False))
    if report["status"] != "passed":
        raise SystemExit(1)
