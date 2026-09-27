#!/usr/bin/env python3
"""Exercise SPEC 010 VE-04 through VE-06 with real cpp-peglib records."""
from __future__ import annotations

import argparse
import json
import shutil
import uuid
from pathlib import Path

from agent_runtime.adapters.validation import RecordedValidationProgramQuery
from agent_runtime.domain import (
    CandidateIdentity,
    CheckItem,
    FactAssessment,
    FixedSnapshot,
    QueryRequest,
)
from agent_runtime.errors import EvidenceIntegrityError, InvalidInput
from agent_runtime.executors import EvidenceReviewExecutor
from agent_runtime.coordinator import RoleCoordinator
from agent_runtime.runtime import DefectRuntime
from agent_runtime.store import SQLiteStore
from agent_runtime.validation import load_validation_record, record_artifacts
try:
    from tools.spec010_cpp_peglib_rule import (
        CppPeglibValidationEvaluator,
        cpp_peglib_profile,
        cpp_peglib_rule,
    )
except ModuleNotFoundError:
    from spec010_cpp_peglib_rule import (
        CppPeglibValidationEvaluator,
        cpp_peglib_profile,
        cpp_peglib_rule,
    )


def snapshot_for(record):
    rule = cpp_peglib_rule()
    profile = cpp_peglib_profile(record.tool_policy.sha256)
    snapshot = FixedSnapshot(
        record.repository_id,
        record.scope_id,
        record.source.sha256,
        __import__("agent_runtime").analysis_profile_digest(rule, profile=profile),
        record.tool_policy.sha256,
    )
    if snapshot.snapshot_digest != record.runtime_snapshot_digest:
        raise InvalidInput("validation record is not bound to the application rule profile")
    return rule, profile, snapshot


def identity_for(record, snapshot, row):
    return CandidateIdentity(
        "cpp-peglib.ast-optimizer-invalid-access",
        "1",
        snapshot.snapshot_digest,
        {
            "revision_id": row["revision_id"],
            "commit": row["commit"],
            "source_digest": record.source.sha256,
            "input_digest": record.test_input.sha256,
            "record_id": record.record_id,
            "toolchain_digest": record.toolchain.sha256,
            "recipe_digest": record.recipe.sha256,
            "observer_contract": "cpp-peglib-asan-uaf-v1",
        },
        f"{row['revision_id']}:{row['input']}:{record.record_id}",
    )


def request_for(snapshot, task_id, record_id):
    return QueryRequest(
        str(uuid.uuid4()),
        task_id,
        "dynamic_validation",
        "read_recorded_validation",
        snapshot.snapshot_digest,
        "recorded-validation",
        "1",
        snapshot.tool_policy_digest,
        {"scope_id": snapshot.scope_id, "selectors": {"record_id": record_id}, "max_results": 1},
        {"record_id": record_id},
        f"validation-{record_id}",
        5000,
    )


def polarity(row):
    if row["classification"] == "ast_optimizer_invalid_access":
        return "positive"
    if row["classification"] == "optimized_completed":
        return "negative"
    return "unknown"


def run(evidence_root: Path, state_root: Path):
    report = json.loads((evidence_root / "report.json").read_text())
    rows = report["records"]
    if len(rows) != 4:
        raise InvalidInput("cpp-peglib acceptance requires four real records")
    state_root.mkdir(parents=True, exist_ok=False)
    store = SQLiteStore(state_root / "runtime.sqlite", state_root / "artifacts")
    evaluator = CppPeglibValidationEvaluator()
    runtime = DefectRuntime(store, evaluator=evaluator)
    outcomes = []
    task_material = {}
    for row in rows:
        record = load_validation_record(Path(row["record_path"]))
        rule, profile, snapshot = snapshot_for(record)
        backend = RecordedValidationProgramQuery(
            snapshot, {record.record_id: record}, evidence_root / "cas"
        )
        analysis_id = runtime.create_analysis(snapshot, rule, profile=profile)
        identity = identity_for(record, snapshot, row)
        task = runtime.propose_candidate(
            analysis_id, identity, discovery_ref="real-cpp-peglib-validation"
        )
        query_outcome, evidence_ref = runtime.query_program(
            request_for(snapshot, task.task_id, record.record_id), backend
        )
        if evidence_ref is None:
            raise InvalidInput("real validation replay did not save evidence")
        fact_polarity = polarity(row)
        fact = FactAssessment(
            str(uuid.uuid4()),
            task.task_id,
            "dynamic_validation",
            "peglib_validation_observation",
            fact_polarity,
            (evidence_ref,),
            identity.scope,
            "tool_observed" if fact_polarity != "unknown" else "unknown",
            limitations=(
                "observation is scoped to one revision, input, and configuration",
            ),
        )
        runtime.record_fact(fact)
        complete = query_outcome.status == "complete" and fact_polarity != "unknown"
        runtime.update_check(CheckItem(
            "dynamic_validation",
            task.task_id,
            "Classify the fixed isolated sanitizer observation",
            "complete" if complete else query_outcome.status,
            (
                "supported" if fact_polarity == "positive"
                else "refuted" if fact_polarity == "negative"
                else "unknown"
            ),
            (evidence_ref,),
            query_outcome.coverage,
            () if complete else ("no matching target observation",),
            tuple(query_outcome.limitations),
        ))
        decision = RoleCoordinator(
            runtime,
            EvidenceReviewExecutor(),
            backend,
            owner_id="spec010-acceptance",
            working_dir=str(state_root),
        ).run_candidate(task.task_id)
        attempts = store.list("attempt", analysis_id)
        role_attempts = [
            {"attempt_id": item["attempt_id"], "role": item["role"], "status": item["status"]}
            for item in attempts if item["task_id"] == task.task_id
        ]
        if len({item["attempt_id"] for item in role_attempts}) != 2:
            raise InvalidInput("investigator and verifier attempts were not independent")
        expected = {
            "positive": "confirmed", "negative": "refuted", "unknown": "inconclusive"
        }[fact_polarity]
        if decision.verdict != expected:
            raise InvalidInput("application evaluator verdict differs from real observation")
        key = row["revision_id"] + "/" + row["input"]
        outcomes.append({
            "case": key,
            "task_id": task.task_id,
            "candidate_digest": identity.candidate_digest,
            "query_status": query_outcome.status,
            "polarity": fact_polarity,
            "verdict": decision.verdict,
            "evidence_id": evidence_ref.evidence_id,
            "runtime_artifact_digest": evidence_ref.artifact_digest,
            "role_attempts": role_attempts,
        })
        task_material[key] = (
            record, snapshot, backend, analysis_id, identity, task, evidence_ref
        )

    negative_key = "base/ordinary"
    record, snapshot, backend, analysis_id, identity, task, evidence_ref = task_material[negative_key]
    if runtime.find_exclusion(task.task_id) is None:
        raise InvalidInput("scoped negative observation did not create an exclusion")
    store.close()

    class MustNotRun:
        def describe_capabilities(self):
            return EvidenceReviewExecutor().describe_capabilities()

        def run(self, request, tools):
            raise AssertionError("exact exclusion should bypass role execution")

    reopened_store = SQLiteStore(state_root / "runtime.sqlite", state_root / "artifacts")
    reopened = DefectRuntime(reopened_store, evaluator=evaluator)
    replay = RoleCoordinator(
        reopened, MustNotRun(), backend,
        owner_id="spec010-reopen", working_dir=str(state_root),
    ).run_candidate(task.task_id)
    if replay.verdict != "refuted" or replay.evaluator_id != "exclusion-index":
        raise InvalidInput("exact exclusion was not reused after process reopen")

    changed_fields = (
        "revision_id", "source_digest", "input_digest", "record_id",
        "toolchain_digest", "recipe_digest", "observer_contract",
    )
    non_reuse = {}
    for field in changed_fields:
        values = dict(identity.identity)
        values[field] = str(values[field]) + "-different"
        changed = CandidateIdentity(
            identity.rule_id, identity.rule_version, identity.snapshot_digest,
            values, identity.scope + ":" + field,
        )
        changed_task = reopened.propose_candidate(
            analysis_id, changed, discovery_ref=f"changed-{field}"
        )
        non_reuse[field] = reopened.find_exclusion(changed_task.task_id) is None
    if not all(non_reuse.values()):
        raise InvalidInput("an inexact candidate reused the scoped exclusion")

    external_root = state_root / "external-cas-copy"
    for artifact in record_artifacts(record):
        source = evidence_root / "cas" / artifact.sha256[:2] / artifact.sha256
        target = external_root / artifact.sha256[:2] / artifact.sha256
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(source, target)
    external_backend = RecordedValidationProgramQuery(
        snapshot, {record.record_id: record}, external_root
    )
    external_target = external_root / record.test_input.sha256[:2] / record.test_input.sha256
    external_target.write_bytes(b"tampered")
    external_tamper_rejected = False
    try:
        external_backend.query(request_for(snapshot, task.task_id, record.record_id))
    except EvidenceIntegrityError:
        external_tamper_rejected = True
    if not external_tamper_rejected:
        raise InvalidInput("tampered external validation artifact was accepted")

    runtime_path = reopened_store.artifact_path(evidence_ref.artifact_digest)
    runtime_path.write_bytes(b"tampered")
    runtime_tamper_rejected = False
    try:
        reopened.find_exclusion(task.task_id)
    except EvidenceIntegrityError:
        runtime_tamper_rejected = True
    if not runtime_tamper_rejected:
        raise InvalidInput("tampered Runtime evidence bundle was accepted")
    reopened_store.close()

    result = {
        "schema": "agent-runtime/spec010-runtime-acceptance/v1",
        "records": outcomes,
        "verdict_counts": {
            verdict: sum(item["verdict"] == verdict for item in outcomes)
            for verdict in ("confirmed", "refuted", "inconclusive")
        },
        "exact_exclusion_reused_after_reopen": True,
        "identity_field_non_reuse": non_reuse,
        "external_artifact_tamper_rejected": external_tamper_rejected,
        "runtime_bundle_tamper_rejected": runtime_tamper_rejected,
        "status": "passed",
        "limitations": [
            "all conclusions are scoped to the recorded revision, input, and fixed configuration",
            "no-trigger and failed execution remain inconclusive and cannot create exclusions",
        ],
    }
    (state_root / "report.json").write_text(json.dumps(result, indent=2, sort_keys=True) + "\n")
    return result


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--evidence-root", type=Path, required=True)
    parser.add_argument("--state-root", type=Path, required=True)
    args = parser.parse_args()
    result = run(args.evidence_root, args.state_root)
    print(json.dumps({"status": result["status"], "verdict_counts": result["verdict_counts"]}, sort_keys=True))


if __name__ == "__main__":
    main()
