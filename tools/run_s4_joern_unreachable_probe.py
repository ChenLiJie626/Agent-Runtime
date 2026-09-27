"""Build a surface-only Joern CPG and inspect lexical return candidates.

The probe writes only verified, manifest-declared source files. It does not run
upstream build scripts. Results stay partial until the CPG is reproduced from
the frozen build and preprocessing configuration.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import time
import uuid
from pathlib import Path

from agent_runtime.adapters import JoernConfig, JoernProgramQuery
from agent_runtime.codec import bytes_digest, digest, utc_now
from agent_runtime.domain import QueryRequest
from agent_runtime.errors import InvalidInput
from agent_runtime.evaluation import (
    dataset_manifest_digest,
    fetch_verified_artifact,
    load_json_object,
)
from run_s4_text_baseline import (
    added_line_numbers,
    find_unreachable_after_return,
    read_surface_files,
)
from run_s4_container_build import extract_verified_archive, load_build_report

FUNCTION = re.compile(
    r"(?:[A-Za-z_][A-Za-z_0-9]*::)*([A-Za-z_][A-Za-z_0-9]*)\s*\([^;{}]*\)"
    r"\s*(?:const\s*)?(?:noexcept\s*)?\{\s*$"
)


def enclosing_symbol(source: str, line: int) -> str | None:
    """Return a nearby function name; ambiguity is left unresolved."""

    rows = source.splitlines()
    for end in range(line - 2, max(-1, line - 14), -1):
        start = max(0, end - 5)
        signature = " ".join(row.strip() for row in rows[start:end + 1])
        match = FUNCTION.search(signature)
        if match:
            return match.group(1)
    return None


def _tool_env(java_home: Path, work_dir: Path) -> dict[str, str]:
    helper = work_dir / "bin"
    helper.mkdir()
    greadlink = helper / "greadlink"
    greadlink.write_text(
        "#!/bin/sh\nexec python3 -c 'import os,sys; "
        'print(os.path.realpath(sys.argv[-1]))\' "$@"\n',
        encoding="utf-8",
    )
    greadlink.chmod(0o700)
    env = os.environ.copy()
    env["JAVA_HOME"] = str(java_home)
    env["PATH"] = os.pathsep.join(
        [str(java_home / "bin"), str(helper), env.get("PATH", "")]
    )
    return env


def _summary(payload: dict) -> dict:
    rows = {item["kind"]: item["nodes"] for item in payload.get("rows", [])}
    required = {
        "candidate_methods", "return_nodes", "following_nodes",
        "return_cfg_ancestors", "following_cfg_ancestors",
    }
    if set(rows) != required:
        raise InvalidInput("Joern semantic result misses required result groups")

    def method_ids(items):
        return {item["_id"] for item in items if item.get("_label") == "METHOD"}

    return {
        "method_count": len(rows["candidate_methods"]),
        "return_count": len(rows["return_nodes"]),
        "following_node_count": len(rows["following_nodes"]),
        "return_reaches_method_entry": bool(
            method_ids(rows["return_cfg_ancestors"])
        ),
        "following_reaches_method_entry": bool(
            method_ids(rows["following_cfg_ancestors"])
        ),
    }


def _load_build_report(path: Path, manifest: dict, sample: dict) -> tuple[dict, bytes]:
    bundle = load_build_report(path, manifest).get(sample["sample_id"])
    if bundle is None:
        raise InvalidInput("build report does not belong to the selected sample")
    return bundle["report"], path.read_bytes()


def run(
    manifest_path: Path, sample_id: str, work_dir: Path,
    joern_home: Path, java_home: Path, *, timeout_seconds: int,
    build_report_path: Path | None = None,
) -> dict:
    manifest = load_json_object(manifest_path)
    sample = next(
        (item for item in manifest["samples"] if item["sample_id"] == sample_id),
        None,
    )
    if sample is None:
        raise InvalidInput(f"sample is not in manifest: {sample_id}")
    work_dir = work_dir.resolve()
    work_dir.mkdir(parents=True, exist_ok=False)
    source_dir = work_dir / "source"
    raw_dir = work_dir / "raw"
    raw_dir.mkdir()

    artifacts = {
        item["role"]: item
        for item in sample["artifacts"]
        if item["role"] in {"base_source", "head_source"}
    }
    base_archive, _ = fetch_verified_artifact(artifacts["base_source"])
    head_archive, head_verification = fetch_verified_artifact(
        artifacts["head_source"]
    )
    paths = sample["surface"]["paths"]
    base = read_surface_files(base_archive, paths)
    head = read_surface_files(head_archive, paths)
    build_report = None
    build_report_bytes = None
    compilation_database = None
    if build_report_path:
        build_report, build_report_bytes = _load_build_report(
            build_report_path, manifest, sample,
        )
        source_files = extract_verified_archive(head_archive, source_dir)
        if digest(source_files) != build_report.get("source_tree_digest"):
            raise InvalidInput("build report source tree differs from verified archive")
        source_compile_db = Path(build_report["compile_database"]["path"])
        entries = json.loads(source_compile_db.read_bytes())
        source_root = str(source_dir.resolve())

        def relocate(value):
            if isinstance(value, str):
                return value.replace("/workspace", source_root)
            if isinstance(value, list):
                return [relocate(item) for item in value]
            return value

        compilation_database = raw_dir / "compile_commands.relocated.json"
        compilation_database.write_text(
            json.dumps([{key: relocate(value) for key, value in item.items()}
                        for item in entries], indent=2) + "\n",
            encoding="utf-8",
        )
    else:
        source_dir.mkdir()
        for path, source in head.items():
            destination = source_dir / path
            destination.parent.mkdir(parents=True, exist_ok=True)
            destination.write_text(source, encoding="utf-8")

    candidates = [
        finding
        for path, source in head.items()
        for finding in find_unreachable_after_return(source, path)
        if finding["return_line"] in added_line_numbers(base[path], source)
    ]
    joern_home = joern_home.resolve()
    java_home = java_home.resolve()
    c2cpg = joern_home / "c2cpg.sh"
    joern = joern_home / "joern"
    if not c2cpg.is_file() or not joern.is_file() or not (java_home / "bin/java").is_file():
        raise InvalidInput("Joern CLI or Java home is incomplete")
    cpg = work_dir / "cpg.bin.zip"
    command = [
        str(c2cpg), str(source_dir), "--log-problems", "--output", str(cpg)
    ]
    if compilation_database:
        command[2:2] = ["--compilation-database", str(compilation_database)]
    started = time.monotonic()
    completed = subprocess.run(
        command, cwd=work_dir, env=_tool_env(java_home, work_dir),
        capture_output=True, check=False, timeout=timeout_seconds,
    )
    (raw_dir / "c2cpg.stdout").write_bytes(completed.stdout)
    (raw_dir / "c2cpg.stderr").write_bytes(completed.stderr)
    if completed.returncode or not cpg.is_file():
        raise InvalidInput(f"c2cpg failed with exit code {completed.returncode}")

    cpg_digest = bytes_digest(cpg.read_bytes())
    snapshot_digest = digest({
        "sample_id": sample_id,
        "head_commit": sample["head_commit"],
        "head_artifact": head_verification["sha256"],
        "surface_paths": paths,
        "cpg_sha256": cpg_digest,
        "build_report_sha256": bytes_digest(build_report_bytes) if build_report_bytes else None,
        "compile_database_sha256": (build_report["compile_database"]["sha256"]
                                        if build_report else None),
    })
    backend = JoernProgramQuery(JoernConfig(
        str(joern), str(java_home), str(cpg), cpg_digest,
        snapshot_digest, "v4.0.636", timeout_seconds * 1000,
    ))
    observations = []
    for index, finding in enumerate(candidates):
        symbol = enclosing_symbol(head[finding["path"]], finding["return_line"])
        if symbol is None:
            observations.append({
                "candidate_id": finding["candidate_id"],
                "status": "not_run",
                "limitation": "enclosing function symbol was not resolved",
            })
            continue
        request = QueryRequest(
            str(uuid.uuid4()), f"probe:{finding['candidate_id']}",
            "semantic_validation", "inspect_unreachable_after_return",
            snapshot_digest, backend.backend_id, backend.backend_version,
            "surface-only-joern-v1",
            {
                "repository_paths": [finding["path"]],
                "symbols": [symbol],
                "build_variant_id": "surface-only",
                "max_results": 100,
                "include_indirect": False,
            },
            {
                "symbol": symbol,
                "source_path": finding["path"],
                "return_line": finding["return_line"],
                "following_line": finding["following_line"],
            },
            f"semantic:{finding['candidate_id']}", timeout_seconds * 1000,
        )
        result = backend.query(request)
        raw_path = raw_dir / f"query-{index}.json"
        raw_path.write_bytes(result.raw or b"")
        payload = json.loads(result.raw) if result.raw else {}
        limitations = [
            "CFG disconnection establishes a tool observation, not whole-build coverage",
        ]
        if build_report:
            limitations.append(
                "the configured target build failed, so the compile-database CPG cannot establish a successful-build path"
                if build_report["build"]["status"] != "complete" else
                "the compile-database CPG does not by itself prove whole-build path feasibility"
            )
            limitations.extend(build_report.get("limitations", []))
        else:
            limitations.insert(0, "CPG parse was not proven equivalent to the frozen build and preprocessing")
        observations.append({
            **finding,
            "symbol": symbol,
            "status": result.status,
            "coverage": result.coverage.completeness,
            "omissions": limitations,
            "limitations": limitations,
            "raw_sha256": bytes_digest(result.raw or b""),
            "raw_path": str(raw_path.resolve()),
            "cfg_observation": _summary(payload),
        })

    return {
        "schema_version": {"major": 1, "minor": 0},
        "created_at": utc_now(),
        "dataset_id": manifest["dataset_id"],
        "manifest_digest": dataset_manifest_digest(manifest),
        "sample_id": sample_id,
        "head_commit": sample["head_commit"],
        "head_artifact_sha256": head_verification["sha256"],
        "analysis_input": {"surface_paths": paths},
        "backend": {"id": "joern", "version": "v4.0.636"},
        "cpg": {
            "sha256": cpg_digest,
            "source_mode": ("verified full head source with captured compile database"
                            if build_report else "verified manifest surface only"),
            "build_recipe_applied": bool(build_report),
            "build_status": build_report["build"]["status"] if build_report else "not_run",
            "build_report_sha256": bytes_digest(build_report_bytes) if build_report_bytes else None,
            "compile_database_sha256": (build_report["compile_database"]["sha256"]
                                           if build_report else None),
            "compile_database_normalized_sha256": (
                build_report["compile_database"]["normalized_sha256"] if build_report else None
            ),
        },
        "resource": {
            "duration_seconds": round(time.monotonic() - started, 6),
            "query_count": len([item for item in observations if "raw_sha256" in item]),
        },
        "observations": observations,
        "conclusion": "partial_semantic_evidence_only",
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", type=Path, required=True)
    parser.add_argument("--sample-id", required=True)
    parser.add_argument("--work-dir", type=Path, required=True)
    parser.add_argument("--joern-home", type=Path, required=True)
    parser.add_argument("--java-home", type=Path, required=True)
    parser.add_argument("--build-report", type=Path,
                        help="Verified container-build report and compile database")
    parser.add_argument("--timeout-seconds", type=int, default=60)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    if not 1 <= args.timeout_seconds <= 600:
        parser.error("timeout must be between 1 and 600 seconds")
    report = run(
        args.manifest, args.sample_id, args.work_dir,
        args.joern_home, args.java_home,
        timeout_seconds=args.timeout_seconds,
        build_report_path=args.build_report,
    )
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(
        json.dumps(report, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    print(json.dumps({
        "status": report["conclusion"],
        "observations": len(report["observations"]),
        "output": str(args.output.resolve()),
    }, ensure_ascii=False))


if __name__ == "__main__":
    main()
