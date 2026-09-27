#!/usr/bin/env python3
"""Run fixed CodeQL compile, database, dual-analysis, and replay acceptance."""
from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import uuid
from pathlib import Path

from agent_runtime.adapters.codeql import (
    CodeQLCaptureBundle,
    CodeQLReplayConfig,
    CodeQLReplayProgramQuery,
    _manifest,
)
from agent_runtime.codec import bytes_digest, canonical_json, digest
from agent_runtime.domain import FixedSnapshot, QueryRequest
from agent_runtime.errors import InvalidInput
from agent_runtime.validation_capture import collect_bounded_process
from tools.prepare_codeql_acceptance import file_sha256, tree_manifest

ROOT = Path(__file__).parents[1]
MANIFEST = ROOT / "evaluation/manifests/codeql-cpp-v2.27.1.json"
PACK = ROOT / "src/agent_runtime/adapters/codeql_pack"
FIXTURE = ROOT / "evaluation/fixtures/codeql-delete-access"
PEGLIB = ROOT / ".poc/spec010/cache/inputs"
RULE_ID = "cpp/potential-access-after-delete-candidate"


def fixed_environment(home: Path):
    home.mkdir(parents=True, exist_ok=True)
    return {
        "PATH": os.defpath,
        "LANG": "C",
        "LC_ALL": "C",
        "HOME": str(home),
        "CODEQL_RAM": "2048",
    }


def run_command(argv, *, cwd: Path, env, timeout_ms: int, output_limit: int):
    process = subprocess.Popen(
        argv, cwd=str(cwd), env=env, shell=False,
        stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        start_new_session=True,
    )
    captured = collect_bounded_process(
        process, timeout_ms=timeout_ms,
        stdout_limit=output_limit // 2, stderr_limit=output_limit // 2,
        total_limit=output_limit, kill_process_group=True,
    )
    if captured.termination.kind != "exit" or captured.termination.exit_code != 0:
        raise InvalidInput(
            f"fixed CodeQL command failed ({captured.termination.kind}): "
            + captured.stderr.decode("utf-8", "replace")[-2000:]
        )
    return captured


def copy_source(name: str, destination: Path):
    destination.mkdir()
    if name in {"positive", "control"}:
        shutil.copyfile(FIXTURE / f"{name}.cpp", destination / f"{name}.cpp")
        compile_file = destination / f"{name}.cpp"
        include = None
    else:
        source = PEGLIB / name
        if not source.is_dir():
            raise InvalidInput(f"prepared cpp-peglib {name} input is unavailable")
        shutil.copyfile(source / "peglib.h", destination / "peglib.h")
        shutil.copyfile(source / "probe.cpp", destination / "probe.cpp")
        compile_file = destination / "probe.cpp"
        include = destination
    return compile_file, include


def source_manifest(directory: Path):
    return {
        path.relative_to(directory).as_posix(): file_sha256(path)
        for path in sorted(directory.rglob("*")) if path.is_file()
    }


def normalize_sarif(path: Path, *, source_root: Path, max_bytes: int):
    content = path.read_bytes()
    if len(content) > max_bytes:
        raise InvalidInput("CodeQL acceptance SARIF exceeds fixed limit")
    try:
        document = json.loads(content)
    except json.JSONDecodeError as exc:
        raise InvalidInput("CodeQL acceptance SARIF is malformed") from exc
    runs = document.get("runs") if isinstance(document, dict) else None
    if not isinstance(runs, list):
        raise InvalidInput("CodeQL acceptance SARIF lacks runs")
    normalized = []
    for run in runs:
        if not isinstance(run, dict) or not isinstance(run.get("results", []), list):
            raise InvalidInput("CodeQL acceptance SARIF run is malformed")
        bases = run.get("originalUriBaseIds", {})
        if not isinstance(bases, dict):
            raise InvalidInput("CodeQL acceptance SARIF URI bases are malformed")
        for result in run.get("results", []):
            normalized.append(CodeQLReplayProgramQuery._normalize_result(
                result, uri_bases=bases, source_root=str(source_root.resolve()),
                allowed_uri_base_ids=tuple(sorted(set(bases) | {"%SRCROOT%"})),
            ))
    ids = [item["result_id"] for item in normalized]
    if len(ids) != len(set(ids)):
        raise InvalidInput("CodeQL acceptance SARIF has duplicate semantic results")
    return tuple(sorted(normalized, key=lambda item: item["result_id"]))


def analyze_twice(
    cli: Path, query: Path, database: Path, source: Path, root: Path,
    *, env, limits,
):
    before = _manifest(database)
    results = []
    runs = []
    for index in (1, 2):
        run_root = root / f"analyze-{index}"
        run_root.mkdir()
        scratch = run_root / "database"
        shutil.copytree(database, scratch)
        sarif = run_root / "result.sarif"
        captured = run_command([
            str(cli), "database", "analyze", str(scratch), str(query),
            "--format=sarif-latest", f"--output={sarif}", "--threads=1",
        ], cwd=run_root, env=env, timeout_ms=limits["analyze_timeout_ms"],
            output_limit=limits["max_process_output_bytes"])
        normalized = normalize_sarif(
            sarif, source_root=source, max_bytes=limits["max_sarif_bytes"]
        )
        results.append(normalized)
        runs.append({
            "normalized": normalized,
            "sarif_sha256": file_sha256(sarif),
            "stdout_sha256": bytes_digest(captured.stdout),
            "stderr_sha256": bytes_digest(captured.stderr),
            "wall_time_ms": captured.wall_time_ms,
        })
    semantic_runs = [
        tuple({key: value for key, value in item.items() if key != "fingerprints"}
              for item in result)
        for result in results
    ]
    if semantic_runs[0] != semantic_runs[1]:
        raise InvalidInput("CodeQL normalized semantic results differ across runs")
    fingerprint_stable = results[0] == results[1]
    if _manifest(database) != before:
        raise InvalidInput("canonical CodeQL database changed during analysis")
    return results[0], runs, fingerprint_stable


def run(toolchain_root: Path, work_root: Path):
    toolchain_root = toolchain_root.resolve()
    work_root = work_root.resolve()
    manifest = json.loads(MANIFEST.read_text())
    prepared = json.loads((toolchain_root / "prepared.json").read_text())
    files = json.loads((toolchain_root / "tree-manifest.json").read_text())
    extracted = toolchain_root / "extracted"
    if tree_manifest(extracted) != files:
        raise InvalidInput("CodeQL extracted tree differs from prepared manifest")
    if digest(files) != prepared["extracted_tree_digest"]:
        raise InvalidInput("CodeQL prepared tree digest is inconsistent")
    cli = Path(prepared["cli_path"])
    if file_sha256(cli) != prepared["cli_sha256"]:
        raise InvalidInput("CodeQL CLI differs from prepared digest")
    work_root.mkdir(parents=True, exist_ok=False)
    env = fixed_environment(work_root / "home")
    limits = manifest["limits"]

    pack = work_root / "query-pack"
    shutil.copytree(PACK, pack)
    install = run_command([
        str(cli), "pack", "install", str(pack),
        f"--additional-packs={extracted / 'codeql'}",
    ], cwd=work_root, env=env, timeout_ms=limits["compile_timeout_ms"],
        output_limit=limits["max_process_output_bytes"])
    lock = pack / "codeql-pack.lock.yml"
    if not lock.is_file():
        raise InvalidInput("CodeQL did not generate a query pack lock")
    compile_result = run_command([
        str(cli), "query", "compile", str(pack / "PotentialAccessAfterDelete.ql"),
        f"--search-path={extracted / 'codeql'}", "--precompile", "--threads=1",
    ], cwd=work_root, env=env, timeout_ms=limits["compile_timeout_ms"],
        output_limit=limits["max_process_output_bytes"])
    qlx = pack / "PotentialAccessAfterDelete.qlx"
    if not qlx.is_file() or qlx.is_symlink():
        raise InvalidInput("CodeQL query compile did not produce the package QLX")
    query = pack / "PotentialAccessAfterDelete.ql"
    query_digest = file_sha256(query)
    lock_digest = file_sha256(lock)
    pack_digest = digest({
        "qlpack.yml": file_sha256(pack / "qlpack.yml"),
        "codeql-pack.lock.yml": lock_digest,
        "PotentialAccessAfterDelete.ql": query_digest,
    })
    qlx_digest = file_sha256(qlx)

    cases = []
    captures = {}
    for case in ("positive", "control", "base", "fix"):
        case_root = work_root / case
        case_root.mkdir()
        source = case_root / "source"
        compile_file, include = copy_source(case, source)
        database = case_root / "database"
        object_file = case_root / "probe.o"
        command = ["/usr/bin/clang++", "-std=c++17", "-c", str(compile_file)]
        if include is not None:
            command.extend(["-I", str(include)])
        command.extend(["-o", str(object_file)])
        build_command = " ".join(command)
        database_result = run_command([
            str(cli), "database", "create", str(database), "--language=cpp",
            f"--source-root={source}", f"--command={build_command}",
            "--threads=1", "--overwrite",
        ], cwd=case_root, env=env, timeout_ms=limits["database_timeout_ms"],
            output_limit=limits["max_process_output_bytes"])
        database_manifest = _manifest(database)
        database_manifest_digest = digest(database_manifest)
        normalized, analysis_runs, fingerprint_stable = analyze_twice(
            cli, query, database, source, case_root, env=env, limits=limits
        )
        rule_results = tuple(item for item in normalized if item["rule_id"] == RULE_ID)
        if len(rule_results) != len(normalized):
            raise InvalidInput("CodeQL emitted results outside the package-owned rule")
        status = "complete" if case == "positive" and rule_results else "partial"
        if case == "positive" and not any(item["uri"] == "positive.cpp" for item in rule_results):
            raise InvalidInput("CodeQL fixture positive did not produce the package rule")
        if case == "control" and rule_results:
            raise InvalidInput("CodeQL nearby control unexpectedly produced a candidate")
        row = {
            "case": case,
            "status": status,
            "result_count": len(rule_results),
            "results": rule_results,
            "source_manifest_digest": digest(source_manifest(source)),
            "database_manifest_digest": database_manifest_digest,
            "database_file_count": len(database_manifest),
            "database_stdout_sha256": bytes_digest(database_result.stdout),
            "database_stderr_sha256": bytes_digest(database_result.stderr),
            "analysis_runs": analysis_runs,
            "fingerprints_stable": fingerprint_stable,
        }
        cases.append(row)
        captures[case] = (source, database, database_manifest, rule_results)

    source, database, database_manifest, positive_results = captures["positive"]
    result_ids = tuple(item["result_id"] for item in positive_results)
    source_digest = digest(source_manifest(source))
    compile_db_digest = digest({"case": "positive", "command": "fixed-clang-cpp17"})
    policy_digest = digest({"operations": ["replay_codeql_result_v1"]})
    snapshot = FixedSnapshot(
        "agent-runtime/codeql-delete-access", "positive", source_digest,
        digest({"rule": RULE_ID, "query": query_digest}), policy_digest,
        {"compile_db_digest": compile_db_digest},
    )
    bundle = CodeQLCaptureBundle(
        snapshot.snapshot_digest, source_digest, compile_db_digest,
        str(database), database_manifest, digest(database_manifest), result_ids,
        str(source), tuple({"%SRCROOT%", "SRCROOT"}), query_digest,
        pack_digest, lock_digest, qlx_digest, prepared["extracted_tree_digest"],
    )
    config = CodeQLReplayConfig(
        str(cli), prepared["cli_sha256"], manifest["release"],
        timeout_ms=limits["analyze_timeout_ms"],
        max_output_bytes=limits["max_sarif_bytes"],
    )
    backend = CodeQLReplayProgramQuery(snapshot, bundle, config)
    replay_rows = []
    for result_id in result_ids:
        request = QueryRequest(
            str(uuid.uuid4()), "acceptance-task", "codeql-signal",
            "replay_codeql_result_v1", snapshot.snapshot_digest,
            backend.backend_id, backend.backend_version, snapshot.tool_policy_digest,
            {"scope_id": snapshot.scope_id,
             "selectors": {"result_id": result_id}, "max_results": 1},
            {"result_id": result_id}, f"replay-{result_id}",
            limits["analyze_timeout_ms"],
        )
        outcome = backend.query(request)
        if outcome.status != "complete" or outcome.raw is None:
            raise InvalidInput("registered real CodeQL result did not replay")
        replay_rows.append({
            "result_id": result_id,
            "status": outcome.status,
            "evidence_sha256": bytes_digest(outcome.raw),
        })
    if tree_manifest(extracted) != files:
        raise InvalidInput("CodeQL toolchain changed during acceptance")
    report = {
        "schema": "agent-runtime/codeql-live-acceptance/v1",
        "toolchain": {
            "release": manifest["release"],
            "archive_sha256": prepared["archive_sha256"],
            "tree_manifest_digest": prepared["extracted_tree_digest"],
            "cli_sha256": prepared["cli_sha256"],
        },
        "query": {
            "rule_id": RULE_ID,
            "query_sha256": query_digest,
            "pack_digest": pack_digest,
            "lock_sha256": lock_digest,
            "qlx_sha256": qlx_digest,
            "compile_stdout_sha256": bytes_digest(compile_result.stdout),
            "compile_stderr_sha256": bytes_digest(compile_result.stderr),
        },
        "cases": cases,
        "replay": replay_rows,
        "status": "passed",
        "limitations": [
            "fixture positive demonstrates one fixed candidate query, not a defect verdict or detection-rate estimate",
            "empty public-source or control results remain partial and do not establish safety or a miss",
        ],
    }
    (work_root / "report.json").write_text(json.dumps(report, indent=2, sort_keys=True) + "\n")
    print(json.dumps({
        "status": report["status"],
        "cases": {row["case"]: {"status": row["status"], "results": row["result_count"]} for row in cases},
        "replayed": len(replay_rows),
    }, sort_keys=True))
    return report


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--toolchain-root", type=Path, required=True)
    parser.add_argument("--work-root", type=Path, required=True)
    args = parser.parse_args()
    run(args.toolchain_root, args.work_root)


if __name__ == "__main__":
    main()
