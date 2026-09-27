"""Live, bounded foundation runtime probe through an active local CC Switch route."""

from __future__ import annotations

import argparse
import json
import tempfile
import uuid
from pathlib import Path
from urllib.parse import urlsplit

from agent_runtime import (
    CandidateIdentity, CheckDefinition, CheckItem, Coverage, DefectRuntime,
    FactAssessment, FixedSnapshot, ProgramResult,
    QueryOperation, QueryRequest, RoleCoordinator, RuleDecision, RuleSpec,
    SQLiteStore,
)
from agent_runtime.adapters import ClaudeAgentExecutor, ClaudeConfig
from agent_runtime.codec import bytes_digest, digest


class FixtureBackend:
    backend_id = "foundation-fixture"
    backend_version = "1"
    supported_operations = frozenset({"inspect"})
    operation_specs = {
        "inspect": QueryOperation(
            "inspect", {"subject": "string"}, ("subject",), ("subject",),
        )
    }
    idempotent_retry = True
    read_only = True

    def query(self, request):
        raw = b"Fixture fact: candidate case-A has condition observed=true."
        return ProgramResult(
            "complete",
            Coverage("case-A only", "case-A only", "complete", (),
                     (bytes_digest(raw),)),
            raw,
        )


class FixtureEvaluator:
    evaluator_id = "foundation-fixture-evaluator"
    evaluator_version = "1"

    def evaluate(self, *, task, snapshot, candidate, facts, checks,
                 claim, assessment):
        matched = (
            len(facts) == 1 and facts[0].fact_id in claim.fact_ids
            and checks[0]["status"] == "complete"
            and "observed" in assessment.checked_predicates
            and not claim.unresolved_items and not assessment.unresolved_items
        )
        refs = tuple(facts[0].evidence_refs) if matched else ()
        blockers = () if matched else ("role output did not verify fixture fact",)
        return RuleDecision(
            "confirmed" if matched else "inconclusive",
            candidate.scope, ("observed",) if matched else (), (),
            refs, (), blockers, self.evaluator_id, self.evaluator_version,
            digest({
                "task": task, "snapshot": snapshot, "candidate": candidate,
                "facts": facts, "checks": checks, "claim": claim,
                "assessment": assessment,
            }),
        )


def local_route():
    settings = json.loads((Path.home() / ".claude/settings.json").read_text())
    source = settings.get("env", {})
    parsed = urlsplit(source.get("ANTHROPIC_BASE_URL", ""))
    if parsed.scheme != "http" or parsed.hostname not in {"127.0.0.1", "localhost"}:
        raise RuntimeError("active CC Switch route must be local loopback HTTP")
    sdk_env = {
        key: value for key, value in source.items()
        if key.startswith(("ANTHROPIC_", "CLAUDE_CODE_"))
        if isinstance(value, str)
    }
    model = source.get("ANTHROPIC_DEFAULT_SONNET_MODEL", "claude-sonnet-4-6")
    return sdk_env, model, parsed.netloc


def run():
    sdk_env, model, route_host = local_route()
    with tempfile.TemporaryDirectory(prefix="foundation-runtime-") as directory:
        root = Path(directory)
        store = SQLiteStore(root / "state.sqlite3", root / "artifacts")
        try:
            runtime = DefectRuntime(store, evaluator=FixtureEvaluator())
            rule = RuleSpec(
                "foundation.fixture", "1", "Fixture evidence investigation",
                (CheckDefinition("observed", "Is the fixture condition observed?",
                                 ("observed",)),),
                FixtureEvaluator.evaluator_id, FixtureEvaluator.evaluator_version,
            )
            snapshot = FixedSnapshot(
                "fixture-repository", "fixture-scope", "a" * 64,
                digest(rule), "b" * 64, {"test_kind": "foundation-sdk"},
            )
            analysis_id = runtime.create_analysis(snapshot, rule)
            candidate = CandidateIdentity(
                rule.rule_id, rule.rule_version, snapshot.snapshot_digest,
                {"subject": "case-A"}, "case-A",
            )
            task = runtime.propose_candidate(
                analysis_id, candidate, discovery_ref="synthetic-fixture",
            )
            backend = FixtureBackend()
            request = QueryRequest(
                str(uuid.uuid4()), task.task_id, "observed", "inspect",
                snapshot.snapshot_digest, backend.backend_id,
                backend.backend_version, snapshot.tool_policy_digest,
                {"scope_id": snapshot.scope_id,
                 "selectors": {"subject": "case-A"}, "max_results": 1},
                {"subject": "case-A"}, str(uuid.uuid4()), 1000,
            )
            outcome, ref = runtime.query_program(request, backend)
            fact = FactAssessment(
                str(uuid.uuid4()), task.task_id, "observed", "observed",
                "positive", (ref,), "case-A", "tool_observed",
            )
            runtime.record_fact(fact)
            runtime.update_check(CheckItem(
                "observed", task.task_id, "Is the fixture condition observed?",
                "complete", "supported", (ref,), outcome.coverage,
            ))
            agent = ClaudeAgentExecutor(ClaudeConfig(
                str(root), backend.backend_id, backend.backend_version,
                snapshot.tool_policy_digest, model=model, max_turns=8,
                max_budget_usd=0.5, sdk_env=sdk_env,
            ))
            coordinator = RoleCoordinator(
                runtime, agent, backend, owner_id="foundation-live-probe",
                working_dir=str(root), token_budget=5000,
            )
            decision = coordinator.run_candidate(task.task_id)
            bindings = store.list("binding", analysis_id)
            claims = store.list("claim", analysis_id)
            assessments = store.list("assessment", analysis_id)
            return {
                "status": "passed" if len(bindings) == 2
                          and len({item["sdk_session_id"] for item in bindings}) == 2
                          and store.list("claim", analysis_id)
                          and store.list("assessment", analysis_id)
                          else "failed",
                "verdict": decision.verdict,
                "blockers": decision.blockers,
                "roles": [item["role"] for item in bindings],
                "session_count": len({item["sdk_session_id"] for item in bindings}),
                "route_host": route_host,
                "model_alias": model,
                "query_status": outcome.status,
                "claim_has_fact": bool(claims and fact.fact_id in claims[0]["fact_ids"]),
                "verifier_checked_observed": bool(
                    assessments and "observed" in assessments[0]["checked_predicates"]
                ),
                "claim_unresolved_count": len(claims[0]["unresolved_items"]) if claims else None,
                "verifier_unresolved_count": len(assessments[0]["unresolved_items"])
                if assessments else None,
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
