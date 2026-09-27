"""Run SPEC 005 F-05 with a real Head macro mismatch and a pinned Joern CPG.

This is a PoC input generator: it records actual compiler/Joern outputs and
keeps the two build digests distinct. It never labels partial graph data as a
complete change-attribution check.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import uuid
from pathlib import Path

from run_joern_inventory import capture, sha256
from run_null_return_poc import (
    FLAGS,
    SOURCE_FILES,
    TRANSLATION_UNITS,
    canonical,
    write_json,
)

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

MACRO = "-DF05_BUILD_VARIANT=1"


def run(fixture: Path, output: Path, joern_home: Path, java_home: Path) -> dict:
    fixture, output = fixture.resolve(), output.resolve()
    joern_home, java_home = joern_home.resolve(), java_home.resolve()
    source_manifest = json.loads((fixture / "manifest.json").read_text())
    output.mkdir(parents=True, exist_ok=True)
    if any(output.iterdir()):
        raise ValueError(f"output directory must be empty: {output}")
    head = output / "head-variant"
    raw = output / "raw"
    head.mkdir()
    raw.mkdir()
    original_head = fixture / "head"
    for side in ("base", "head"):
        for name in SOURCE_FILES:
            expected = source_manifest["sides"][side]["source_sha256"][name]
            if sha256(fixture / side / name) != expected:
                raise ValueError(f"{side} source changed: {name}")
    for name in SOURCE_FILES:
        shutil.copyfile(original_head / name, head / name)

    original_db = json.loads((original_head / "compile_commands.json").read_text())
    entries = []
    for entry in original_db:
        name = Path(entry["file"]).name
        if name not in TRANSLATION_UNITS:
            raise ValueError(f"unexpected translation unit: {name}")
        compiler = entry["arguments"][0]
        entries.append(
            {
                "directory": str(head),
                "file": str(head / name),
                "arguments": [
                    compiler,
                    *FLAGS,
                    MACRO,
                    "-I",
                    str(head),
                    "-c",
                    str(head / name),
                ],
            }
        )
    write_json(head / "compile_commands.json", entries)
    normalized = [
        {"file": Path(entry["file"]).name, "flags": (*FLAGS, MACRO), "include": "."}
        for entry in entries
    ]
    base_build_digest = source_manifest["sides"]["base"]["compile_database"][
        "normalized_sha256"
    ]
    head_build_digest = hashlib.sha256(canonical(normalized)).hexdigest()
    if head_build_digest == base_build_digest:
        raise AssertionError("F-05 build variant did not change the build digest")

    env = os.environ.copy()
    env["JAVA_HOME"] = str(java_home)
    env["PATH"] = os.pathsep.join(
        [
            str(java_home / "bin"),
            str(joern_home.parent.parent / "bin"),
            env.get("PATH", ""),
        ]
    )
    clang_results = []
    for entry in entries:
        name = Path(entry["file"]).name
        command = [
            arg if arg != "-c" else "-fsyntax-only" for arg in entry["arguments"]
        ]
        clang_results.append(
            capture(
                command, cwd=head, raw=raw, label=f"clang-{name}", env=env, timeout=30
            )
        )
    cpg = output / "head-variant.cpg.bin.zip"
    parse = capture(
        [
            str(joern_home / "c2cpg.sh"),
            str(head),
            "--compilation-database",
            str(head / "compile_commands.json"),
            "--log-problems",
            "--output",
            str(cpg),
        ],
        cwd=output,
        raw=raw,
        label="joern-parse",
        env=env,
        timeout=180,
    )
    if parse["status"] != "complete" or not cpg.is_file():
        raise RuntimeError("Joern could not parse F-05 Head variant")

    rule = null_return_rule()
    snapshot = AnalysisSnapshot(
        repository_id="spec005-f05-build-mismatch",
        base_commit=source_manifest["sides"]["base"]["commit"],
        head_commit=source_manifest["sides"]["head"]["commit"],
        base_tree_digest=digest(source_manifest["sides"]["base"]["source_sha256"]),
        head_tree_digest=digest(source_manifest["sides"]["head"]["source_sha256"]),
        build_variant_id="clang-cxx17-O0-F05",
        base_build_digest=base_build_digest,
        head_build_digest=head_build_digest,
        rule_profile_digest=digest(rule),
        tool_policy_digest=digest({"operations": ["get_function"]}),
        compile_db_digest=sha256(head / "compile_commands.json"),
    )
    backend = JoernProgramQuery(
        JoernConfig(
            str(joern_home / "joern"),
            str(java_home),
            str(cpg),
            sha256(cpg),
            snapshot.snapshot_digest,
            "v4.0.636",
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
            analysis_id, candidate, discovery_ref="F-05 mismatched Head build"
        )
        query = QueryRequest(
            str(uuid.uuid4()),
            task.task_id,
            "change_attribution",
            "get_function",
            snapshot.snapshot_digest,
            backend.backend_id,
            backend.backend_version,
            snapshot.tool_policy_digest,
            {
                "repository_paths": [],
                "symbols": ["getBuffer"],
                "build_variant_id": snapshot.build_variant_id,
                "max_results": 100,
                "include_indirect": False,
            },
            {"symbol": "getBuffer"},
            str(uuid.uuid4()),
            30_000,
        )
        outcome, evidence = runtime.query_program(query, backend)
        report = {
            "variant": "F-05",
            "source_commits_unchanged": True,
            "head_added_macro": MACRO,
            "base_build_digest": base_build_digest,
            "head_build_digest": head_build_digest,
            "builds_comparable": base_build_digest == head_build_digest,
            "head_compile_database_sha256": sha256(head / "compile_commands.json"),
            "clang": clang_results,
            "joern_parse": {**parse, "cpg_sha256": sha256(cpg)},
            "query_status": outcome.status,
            "query_coverage": outcome.coverage.completeness,
            "query_limitations": outcome.limitations,
            "raw_artifact_digest": outcome.raw_artifact_digest,
            "evidence_id": evidence.evidence_id if evidence else None,
            "check_status": next(
                check["status"]
                for check in runtime.checks(task.task_id)
                if check["check_id"] == "change_attribution"
            ),
            "finding_state": runtime.task(task.task_id)["finding_state"],
            "interpretation": "Different build digests block Head-change attribution; partial Joern material cannot complete the check.",
        }
    finally:
        store.close()
    (output / "report.json").write_text(json.dumps(report, indent=2) + "\n")
    return report


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--fixture", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--joern-home", type=Path, required=True)
    parser.add_argument("--java-home", type=Path, required=True)
    args = parser.parse_args()
    report = run(args.fixture, args.output, args.joern_home, args.java_home)
    print(
        json.dumps(
            {
                key: report[key]
                for key in (
                    "variant",
                    "builds_comparable",
                    "query_status",
                    "check_status",
                    "finding_state",
                )
            }
        )
    )


if __name__ == "__main__":
    main()
