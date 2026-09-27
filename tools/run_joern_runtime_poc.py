"""Register real, conservatively scoped Joern queries in DefectRuntime."""

from __future__ import annotations

import argparse
import json
import uuid
from pathlib import Path

from agent_runtime import (
    AnalysisSnapshot,
    CandidateKey,
    DefectRuntime,
    QueryRequest,
    SourceLocation,
    SQLiteStore,
    null_return_rule,
)
from agent_runtime.adapters import JoernConfig, JoernProgramQuery
from agent_runtime.codec import digest


def run(
    fixture: Path, joern_manifest: Path, output: Path, joern_home: Path, java_home: Path
) -> dict[str, object]:
    fixture = fixture.resolve()
    joern_manifest = joern_manifest.resolve()
    output = output.resolve()
    output.mkdir(parents=True, exist_ok=True)
    if any(output.iterdir()):
        raise ValueError(f"output directory must be empty: {output}")
    fixture_data = json.loads((fixture / "manifest.json").read_text())
    joern_data = json.loads(joern_manifest.read_text())
    rule = null_return_rule()
    build_digest = fixture_data["sides"]["head"]["compile_database"][
        "normalized_sha256"
    ]
    snapshot = AnalysisSnapshot(
        repository_id="spec005-null-return",
        base_commit=fixture_data["sides"]["base"]["commit"],
        head_commit=fixture_data["sides"]["head"]["commit"],
        base_tree_digest=digest(fixture_data["sides"]["base"]["source_sha256"]),
        head_tree_digest=digest(fixture_data["sides"]["head"]["source_sha256"]),
        build_variant_id="clang-cxx17-O0",
        base_build_digest=build_digest,
        head_build_digest=build_digest,
        rule_profile_digest=digest(rule),
        tool_policy_digest=digest(
            {
                "operations": [
                    "get_function",
                    "find_callers",
                    "get_guards",
                    "map_arguments",
                    "trace_value",
                    "check_reachability",
                ]
            }
        ),
        compile_db_digest=build_digest,
    )
    head_cpg = joern_data["sides"]["head"]["parse"]["cpg"]
    backend = JoernProgramQuery(
        JoernConfig(
            str((joern_home / "joern").resolve()),
            str(java_home.resolve()),
            head_cpg["path"],
            head_cpg["sha256"],
            snapshot.snapshot_digest,
            joern_data["joern_release"],
        )
    )
    store = SQLiteStore(output / "state.sqlite3", output / "artifacts")
    try:
        runtime = DefectRuntime(store)
        analysis_id = runtime.create_analysis(snapshot, rule)
        candidate = CandidateKey(
            rule.rule_id,
            rule.rule_version,
            SourceLocation("head", snapshot.head_commit, "buffer.cpp", 4, 5, 4, 35),
            SourceLocation("head", snapshot.head_commit, "consumer.cpp", 5, 5, 5, 18),
            snapshot.build_variant_id,
            "getBuffer(0)-return-value",
            "main->process(0)->*value",
        )
        task = runtime.propose_candidate(
            analysis_id, candidate, discovery_ref="Joern SPEC 005"
        )
        guarded_candidate = CandidateKey(
            rule.rule_id,
            rule.rule_version,
            candidate.source_location,
            SourceLocation("head", snapshot.head_commit, "consumer.cpp", 13, 5, 13, 18),
            snapshot.build_variant_id,
            "getBuffer(0)-return-value",
            "guardedProcess(0)->*value",
        )
        guarded_task = runtime.propose_candidate(
            analysis_id, guarded_candidate, discovery_ref="Joern SPEC 005 guard"
        )
        observations = []
        for query_task, check_id, operation, symbol, call_line, context_symbol in (
            (task, "nullable_source", "get_function", "getBuffer", None, None),
            (task, "reachability", "find_callers", "getBuffer", None, None),
            (
                task,
                "reachability",
                "check_reachability",
                "getBuffer",
                None,
                "main",
            ),
            (task, "propagation", "map_arguments", "getBuffer", 4, None),
            (task, "propagation", "trace_value", "getBuffer", None, "process"),
            (guarded_task, "guard", "get_guards", "guardedProcess", None, None),
            (guarded_task, "propagation", "map_arguments", "getBuffer", 12, None),
            (
                guarded_task,
                "propagation",
                "trace_value",
                "getBuffer",
                None,
                "guardedProcess",
            ),
        ):
            query = QueryRequest(
                str(uuid.uuid4()),
                query_task.task_id,
                check_id,
                operation,
                snapshot.snapshot_digest,
                backend.backend_id,
                backend.backend_version,
                snapshot.tool_policy_digest,
                {
                    "repository_paths": [],
                    "symbols": [symbol, context_symbol] if context_symbol else [symbol],
                    "build_variant_id": snapshot.build_variant_id,
                    "max_results": 100,
                    "include_indirect": True,
                },
                {
                    "symbol": symbol,
                    **({"call_line": call_line} if call_line else {}),
                    **({"context_symbol": context_symbol} if context_symbol else {}),
                },
                str(uuid.uuid4()),
                30_000,
            )
            outcome, reference = runtime.query_program(query, backend)
            observations.append(
                {
                    "task_id": query_task.task_id,
                    "operation": operation,
                    "symbol": symbol,
                    "call_line": call_line,
                    "context_symbol": context_symbol,
                    "status": outcome.status,
                    "coverage": outcome.coverage.completeness,
                    "limitations": outcome.limitations,
                    "evidence_id": reference.evidence_id if reference else None,
                    "raw_artifact_digest": outcome.raw_artifact_digest,
                }
            )
        timeout_query = QueryRequest(
            str(uuid.uuid4()),
            task.task_id,
            "reachability",
            "find_callers",
            snapshot.snapshot_digest,
            backend.backend_id,
            backend.backend_version,
            snapshot.tool_policy_digest,
            {
                "repository_paths": [],
                "symbols": ["getBuffer"],
                "build_variant_id": snapshot.build_variant_id,
                "max_results": 100,
                "include_indirect": True,
            },
            {"symbol": "getBuffer"},
            str(uuid.uuid4()),
            1,
        )
        timeout_outcome, timeout_ref = runtime.query_program(timeout_query, backend)
        timeout_probe = {
            "status": timeout_outcome.status,
            "raw_artifact_digest": timeout_outcome.raw_artifact_digest,
            "raw_readable": bool(
                timeout_outcome.raw_artifact_digest
                and store.read_artifact(timeout_outcome.raw_artifact_digest)
            ),
            "evidence_created": timeout_ref is not None,
            "check_status": next(
                check["status"]
                for check in runtime.checks(task.task_id)
                if check["check_id"] == "reachability"
            ),
        }
        report: dict[str, object] = {
            "snapshot_digest": snapshot.snapshot_digest,
            "candidate_digest": candidate.candidate_digest,
            "analysis_id": analysis_id,
            "task_id": task.task_id,
            "guarded_task_id": guarded_task.task_id,
            "observations": observations,
            "timeout_probe": timeout_probe,
            "finding_state": runtime.task(task.task_id)["finding_state"],
            "note": "Partial Joern evidence is archived; no complete checks or verdict were created.",
        }
        (output / "report.json").write_text(
            json.dumps(report, indent=2, ensure_ascii=False) + "\n"
        )
        return report
    finally:
        store.close()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--fixture", type=Path, required=True)
    parser.add_argument("--joern-manifest", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--joern-home", type=Path, required=True)
    parser.add_argument("--java-home", type=Path, required=True)
    args = parser.parse_args()
    report = run(
        args.fixture,
        args.joern_manifest,
        args.output,
        args.joern_home,
        args.java_home,
    )
    print(
        json.dumps(
            {
                "finding_state": report["finding_state"],
                "observations": report["observations"],
            }
        )
    )


if __name__ == "__main__":
    main()
