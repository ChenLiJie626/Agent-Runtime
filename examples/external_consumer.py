"""Two external rules and two backends using only the installed public API.

Run from a directory outside the source checkout after installing the wheel.
The fixture material is synthetic; this checks extension wiring, not defects.
"""

from __future__ import annotations

import argparse
import json
import tempfile
import uuid
from pathlib import Path

from agent_runtime import (
    AgentCapabilities, Assessment, CandidateIdentity, CheckDefinition,
    CheckItem, Claim, Coverage, DefectRuntime, FactAssessment, FixedSnapshot,
    ProgramResult, QueryOperation, QueryRequest, RoleCoordinator, RoleRunResult,
    RuleDecision, RuleSpec, SQLiteStore, analysis_profile_digest,
    bytes_digest, canonical_json, decision_input_digest, digest,
)


class SymbolBackend:
    backend_id = "external.symbols"
    backend_version = "1"
    supported_operations = frozenset({"inspect_symbol"})
    operation_specs = {
        "inspect_symbol": QueryOperation(
            "inspect_symbol", {"symbol": "string"}, ("symbol",), ("symbol",),
        ),
    }
    read_only = True
    idempotent_retry = True

    def query(self, request: QueryRequest) -> ProgramResult:
        symbol = request.args["symbol"]
        raw = canonical_json({"symbol": symbol, "present": True}).encode("utf-8")
        return ProgramResult(
            "complete",
            Coverage("one named symbol", "one named symbol", "complete",
                     (), (bytes_digest(raw),)),
            raw,
        )


class ManifestBackend:
    backend_id = "external.manifest"
    backend_version = "1"
    supported_operations = frozenset({"inspect_entry"})
    operation_specs = {
        "inspect_entry": QueryOperation(
            "inspect_entry", {"entry": "string"}, ("entry",), ("entry",),
        ),
    }
    read_only = True
    idempotent_retry = True

    def query(self, request: QueryRequest) -> ProgramResult:
        entry = request.args["entry"]
        raw = canonical_json({"entry": entry, "allowed": False}).encode("utf-8")
        return ProgramResult(
            "complete",
            Coverage("one manifest entry", "one manifest entry", "complete",
                     (), (bytes_digest(raw),)),
            raw,
        )


class FixtureEvaluator:
    evaluator_version = "1"

    def __init__(self, evaluator_id: str, expected_polarity: str) -> None:
        self.evaluator_id = evaluator_id
        self.expected_polarity = expected_polarity

    def evaluate(self, *, task, snapshot, candidate, facts, checks,
                 claim, assessment) -> RuleDecision:
        matching = [
            fact for fact in facts
            if fact.fact_id in claim.fact_ids
            and fact.polarity == self.expected_polarity
            and fact.confidence_class == "tool_observed"
        ]
        complete = all(
            check["status"] == "complete"
            and check["coverage"]["completeness"] == "complete"
            for check in checks
        )
        ready = bool(matching) and complete and not (
            claim.unresolved_items or assessment.unresolved_items
        )
        references = tuple(matching[0].evidence_refs) if ready else ()
        verdict = (
            "confirmed" if self.expected_polarity == "positive" else "refuted"
        ) if ready else "inconclusive"
        return RuleDecision(
            verdict, candidate.scope,
            (matching[0].check_id,) if ready else (), (),
            references if verdict == "confirmed" else (),
            references if verdict == "refuted" else (),
            () if ready else ("fixture check is incomplete",),
            self.evaluator_id, self.evaluator_version,
            decision_input_digest(
                task=task, snapshot=snapshot, candidate=candidate, facts=facts,
                checks=checks, claim=claim, assessment=assessment,
            ),
        )


class FixtureAgent:
    """A replaceable executor for this example; real SDKs use the same port."""

    def __init__(self) -> None:
        self.material: dict[str, tuple[FactAssessment, object, str]] = {}

    def describe_capabilities(self) -> AgentCapabilities:
        return AgentCapabilities(False, False, False, True, True, True, True,
                                 False)

    def run(self, request, tools) -> RoleRunResult:
        fact, ref, polarity = self.material[request.task_id]
        support = (ref,) if polarity == "positive" else ()
        counter = (ref,) if polarity == "negative" else ()
        if request.role_kind == "investigator":
            output = Claim(
                str(uuid.uuid4()), request.task_id, request.attempt_id,
                (fact.fact_id,), support, counter, (), fact.scope,
            )
        else:
            output = Assessment(
                str(uuid.uuid4()), request.task_id, request.attempt_id,
                request.claim.claim_id, (fact.check_id,), support, counter,
                (), "reviewed source", fact.scope,
            )
        return RoleRunResult(output, str(uuid.uuid4()))


def run(root: Path) -> dict:
    store = SQLiteStore(root / "runtime.sqlite3", root / "artifacts")
    specs = (
        ("external.symbol-presence", "symbol-evaluator", "presence", "symbol",
         "inspect_symbol", SymbolBackend(), "positive", "confirmed"),
        ("external.manifest-policy", "manifest-evaluator", "policy", "entry",
         "inspect_entry", ManifestBackend(), "negative", "refuted"),
    )
    agent = FixtureAgent()
    runtime = DefectRuntime(store)
    for _, evaluator_id, _, _, _, _, polarity, _ in specs:
        runtime.register_evaluator(FixtureEvaluator(evaluator_id, polarity))
    completed: list[dict] = []
    try:
        for rule_id, evaluator_id, check_id, selector_name, operation, backend, polarity, expected in specs:
            rule = RuleSpec(
                rule_id, "1", rule_id,
                (CheckDefinition(check_id, check_id, (check_id,)),),
                evaluator_id, "1",
            )
            snapshot = FixedSnapshot(
                "fixture-repository", rule_id, digest({"source": rule_id}),
                analysis_profile_digest(rule), digest({"policy": rule_id}),
                {"fixture": "S3 package extension"},
            )
            analysis_id = runtime.create_analysis(snapshot, rule)
            subject = "case-A"
            task = runtime.propose_candidate(
                analysis_id,
                CandidateIdentity(
                    rule.rule_id, rule.rule_version, snapshot.snapshot_digest,
                    {selector_name: subject}, subject,
                ),
                discovery_ref="external fixture",
            )
            outcome, ref = runtime.query_program(
                QueryRequest(
                    str(uuid.uuid4()), task.task_id, check_id, operation,
                    snapshot.snapshot_digest, backend.backend_id,
                    backend.backend_version, snapshot.tool_policy_digest,
                    {"scope_id": snapshot.scope_id,
                     "selectors": {selector_name: subject}, "max_results": 1},
                    {selector_name: subject}, str(uuid.uuid4()), 1000,
                ), backend,
            )
            fact = FactAssessment(
                str(uuid.uuid4()), task.task_id, check_id, check_id,
                polarity, (ref,), subject, "tool_observed",
            )
            runtime.record_fact(fact)
            runtime.update_check(CheckItem(
                check_id, task.task_id, check_id, "complete", "supported",
                (ref,), outcome.coverage,
            ))
            agent.material[task.task_id] = (fact, ref, polarity)
            decision = RoleCoordinator(
                runtime, agent, backend, owner_id="external-worker",
                working_dir=str(root),
            ).run_candidate(task.task_id)
            if decision.verdict != expected:
                raise AssertionError((rule_id, decision.verdict, decision.blockers))
            completed.append({
                "rule_id": rule_id, "analysis_id": analysis_id,
                "task_id": task.task_id, "verdict": decision.verdict,
                "backend_id": backend.backend_id,
            })
    finally:
        store.close()

    reopened = SQLiteStore(root / "runtime.sqlite3", root / "artifacts")
    try:
        reader = DefectRuntime(reopened)
        reports = [reader.get_report(item["analysis_id"]) for item in completed]
        exclusion = reader.find_exclusion(completed[1]["task_id"])
        assert exclusion is not None
        assert all(report["discovered_denominator"] == 1 for report in reports)
        assert all(report["usage"]["status"] == "unavailable" for report in reports)
        return {
            "status": "passed",
            "package": "defect-agent-runtime",
            "rules": [item["rule_id"] for item in completed],
            "backends": [item["backend_id"] for item in completed],
            "verdicts": [item["verdict"] for item in completed],
            "reopened_reports": len(reports),
            "exclusion_reused": exclusion is not None,
            "report_schema": reports[0]["schema_version"],
        }
    finally:
        reopened.close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    with tempfile.TemporaryDirectory(prefix="external-runtime-") as directory:
        result = run(Path(directory))
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(json.dumps(result, ensure_ascii=False, indent=2))
    print(json.dumps(result, ensure_ascii=False))
