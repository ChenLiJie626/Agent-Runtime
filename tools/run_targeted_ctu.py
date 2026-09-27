#!/usr/bin/env python3
"""Resume isolated container CTU slices or conservatively adopt raw reports."""
from __future__ import annotations

import argparse
import hashlib
import json
import plistlib
import shutil
import subprocess
import time
import zipfile
from pathlib import Path
from typing import Any

from agent_runtime.codec import utc_now, validate_relative_path
from agent_runtime.errors import InvalidInput
from evaluate_public_uaf_ctu import read_uaf_diagnostics
from materialize_ctu_slices import _relative_source
from run_layered_ctu import _metadata_stats


RESOURCE_WRAPPER = """import json, pathlib, resource, subprocess, sys
p = subprocess.run(sys.argv[1:], check=False)
peak = pathlib.Path('/sys/fs/cgroup/memory.peak')
value = {'max_process_rss_bytes': resource.getrusage(resource.RUSAGE_CHILDREN).ru_maxrss * 1024,
         'peak_memory_bytes': int(peak.read_text()) if peak.is_file() else None}
pathlib.Path('/workspace/output/resources.json').write_text(json.dumps(value))
sys.exit(p.returncode)
"""


def _sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _write(path, value):
    temporary = path.with_suffix(".tmp")
    temporary.write_text(json.dumps(value, indent=2, sort_keys=True) + "\n")
    temporary.replace(path)


def _per_report_stats(reports: Path, selected: list[str]):
    """Recover only individually attested results when a run was interrupted."""
    successful, failed, mapping, artifacts = set(), set(), {}, []
    for source_label in reports.glob("*.plist.source"):
        report = source_label.with_suffix("")
        try:
            source = source_label.read_text().strip().removeprefix("/workspace/source/")
            if source not in selected or not report.is_file():
                continue
            plistlib.loads(report.read_bytes())
        except (OSError, ValueError, plistlib.InvalidFileException):
            continue
        successful.add(source)
        mapping[str(report)] = "/workspace/source/" + source
        artifacts.append(
            {
                "path": str(source_label),
                "sha256": _sha(source_label),
                "report_sha256": _sha(report),
            }
        )
    for archive in (reports / "failed").glob("*.zip"):
        try:
            with zipfile.ZipFile(archive) as value:
                info = value.getinfo("compilation_database.json")
                if info.file_size > 8 * 1024 * 1024:
                    continue
                rows = json.loads(value.read(info))
                # CodeChecker records the failing action first. Verify its full
                # source identity and the report's basename, not just a log word.
                source = _relative_source(rows[0], "/workspace/source")
                if source not in selected or not archive.name.startswith(
                    Path(source).name + "_clangsa_"
                ):
                    continue
        except (
            OSError,
            ValueError,
            KeyError,
            IndexError,
            TypeError,
            zipfile.BadZipFile,
        ):
            continue
        failed.add(source)
        artifacts.append({"path": str(archive), "sha256": _sha(archive)})
    successful -= failed  # A fallback non-CTU plist cannot erase CTU failure.
    return {
        "successful_translation_units": len(successful),
        "failed_translation_units": len(failed),
        "successful_sources": sorted(successful),
        "failed_sources": sorted(failed),
        "per_report_artifacts": artifacts,
        "source_identity_evidence": "per-report artifacts; aggregate analyzer metadata absent",
    }, mapping


def collect_execution(
    directory: Path,
    selection: dict[str, Any],
    source: Path,
    project_id: str,
    *,
    exit_code=None,
    imported=False,
) -> dict[str, Any]:
    stats = _metadata_stats(directory / "reports")
    tool = {}
    recovered = False
    if not (directory / "reports/metadata.json").is_file():
        observed, mapping = _per_report_stats(
            directory / "reports", selection["translation_units"]
        )
        stats.update(observed)
        tool = {"result_source_files": mapping}
        recovered = True
    if stats["metadata_valid"]:
        tool = json.loads((directory / "reports/metadata.json").read_text())["tools"][0]

    def normalized(paths):
        return sorted({p.removeprefix("/workspace/source/") for p in paths})

    successful = normalized(stats["successful_sources"])
    failed = normalized(stats["failed_sources"])
    selected = selection["translation_units"]
    if not set(successful + failed) <= set(selected):
        raise InvalidInput("analyzer source paths do not belong to the selected slice")
    unfinished = sorted(set(selected) - set(successful) - set(failed))
    # Counts without source identities cannot establish per-target coverage.
    valid = (
        (stats["metadata_valid"] or recovered)
        and len(successful) == stats["successful_translation_units"]
        and len(failed) == stats["failed_translation_units"]
    )
    status = (
        "complete"
        if stats["metadata_valid"]
        and valid
        and not failed
        and not unfinished
        and exit_code in (0, None)
        else "partial" if valid and successful else "execution_failure"
    )
    stamps = tool.get("timestamps", {})
    begin, end = stamps.get("begin"), stamps.get("end")
    return {
        "target_path": selection["target_path"],
        "layer_id": selection["slice_dir"],
        "status": status,
        "ctu_enabled": True,
        "selected_sources": selected,
        **stats,
        "successful_sources": successful,
        "failed_sources": failed,
        "unfinished_sources": unfinished,
        "total_translation_units": len(selected),
        "command": tool.get("command", []),
        "exit_code": exit_code,
        "duration_seconds": (
            max(0, end - begin)
            if isinstance(begin, (float, int)) and isinstance(end, (float, int))
            else None
        ),
        "analyzer_version": tool.get("analyzers", {})
        .get("clangsa", {})
        .get("analyzer_statistics", {})
        .get("version"),
        "ctu_ast_mode": (
            tool.get("command", [])[tool["command"].index("--ctu-ast-mode") + 1]
            if "--ctu-ast-mode" in tool.get("command", [])
            else None
        ),
        "diagnostics": read_uaf_diagnostics(
            directory / "reports",
            source,
            project_id,
            tool,
            report_names=(
                {Path(value).name for value in tool.get("result_source_files", {})}
                if recovered
                else None
            ),
        ),
        "metadata_sha256": (
            _sha(directory / "reports/metadata.json")
            if stats["metadata_valid"]
            else None
        ),
        "artifact_dir": str(directory.resolve()),
        "imported_raw_reports": imported,
        "jobs": (
            int(tool["command"][tool["command"].index("-j") + 1])
            if "-j" in tool.get("command", [])
            else None
        ),
        "log_artifacts": {
            name: {"path": str(directory / name), "sha256": _sha(directory / name)}
            for name in ("run.stdout.log", "run.stderr.log")
            if (directory / name).is_file()
        },
        "reason": (
            "analyzer statistics and source identities verified"
            if valid
            else "missing or inconsistent analyzer metadata"
        ),
    }


def _collect_or_failure(directory, selection, source, project_id, **kwargs):
    try:
        return collect_execution(directory, selection, source, project_id, **kwargs)
    except (InvalidInput, OSError, ValueError, KeyError, TypeError) as exc:
        return {
            "target_path": selection["target_path"],
            "status": "execution_failure",
            "selected_sources": selection["translation_units"],
            "successful_sources": [],
            "failed_sources": [],
            "unfinished_sources": selection["translation_units"],
            "successful_translation_units": 0,
            "failed_translation_units": 0,
            "total_translation_units": len(selection["translation_units"]),
            "diagnostics": [],
            "artifact_dir": str(directory),
            "reason": f"invalid analyzer artifacts: {exc}",
        }


def execute_slices(
    manifest_path: Path,
    *,
    source: Path,
    project_id: str,
    image: str,
    ast_mode="parse-on-demand",
    timeout_seconds=1800,
    memory="5g",
    adopt_existing=False,
    targets=(),
) -> dict[str, Any]:
    manifest = json.loads(manifest_path.read_text())
    if (
        manifest.get("schema") != "agent-runtime/container-ctu-slices/v1"
        or manifest.get("container_root") != "/workspace/source"
    ):
        raise InvalidInput("expected container CTU slices rooted at /workspace/source")
    image_id = subprocess.check_output(
        ["docker", "image", "inspect", image, "--format", "{{.Id}}"], text=True
    ).strip()
    if timeout_seconds < 1:
        raise InvalidInput("timeout must be positive")
    revision = subprocess.check_output(
        ["git", "-C", str(source), "rev-parse", "HEAD"], text=True
    ).strip()
    tree_diff = subprocess.check_output(
        ["git", "-C", str(source), "diff", "--binary", "HEAD"]
    )
    tree_diff_digest = hashlib.sha256(tree_diff).hexdigest()
    root = manifest_path.resolve().parent
    executions = []
    target_analysis = {}
    output = root / "metadata.json"

    def checkpoint():
        value = {
            "schema": "agent-runtime/layered-ctu-execution/v1",
            "created_at": utc_now(),
            "project_id": project_id,
            "continue_after_layer_failure": True,
            "executions": executions,
            "target_analysis": target_analysis,
        }
        _write(output, value)
        return value

    slices = sorted(
        manifest["slices"],
        key=lambda x: (x["translation_unit_count"], x["target_path"]),
    )
    if targets and not set(targets) <= {x["target_path"] for x in slices}:
        raise InvalidInput("unknown target selection")
    for selection in slices:
        validate_relative_path(selection["target_path"])
        validate_relative_path(selection["slice_dir"])
        validate_relative_path(selection["compile_database"])
        directory = root / selection["slice_dir"]
        database = directory / selection["compile_database"]
        if _sha(database) != selection["compile_database_sha256"]:
            raise InvalidInput("slice compile database digest mismatch")
        rows = json.loads(database.read_text())
        sources = [_relative_source(row, "/workspace/source") for row in rows]
        if sorted(sources) != sorted(selection["translation_units"]):
            raise InvalidInput("slice TU selection differs from compile database")
        if any(
            "/Users/" in json.dumps(row) or "$PROJECT_ROOT" in json.dumps(row)
            for row in rows
        ):
            raise InvalidInput("container slice contains host paths")
        binding = {
            "compile_database_sha256": _sha(database),
            "source_revision": revision,
            "target_sha256": _sha(source / selection["target_path"]),
            "image_id": image_id,
            "ctu_ast_mode": ast_mode,
            "jobs": 1,
            "memory_limit": memory,
            "source_tree_diff_sha256": tree_diff_digest,
        }
        imported = adopt_existing and (directory / "reports/metadata.json").is_file()
        if imported:
            execution = _collect_or_failure(
                directory, selection, source, project_id, imported=True
            )
            execution.update(
                {
                    "compile_database_sha256": binding["compile_database_sha256"],
                    "source_revision_at_collection": revision,
                    "target_sha256_at_collection": binding["target_sha256"],
                    "image_id": None,
                    "image_id_at_collection": image_id,
                    "peak_memory_bytes": None,
                    "provenance_limitations": [
                        "historical container image, exit code and peak memory were not recorded",
                        "source revision/hash describe collection time, not independently attested execution time",
                    ],
                }
            )
        else:
            # Never overwrite reports. When only some targets are selected, keep
            # matching terminal checkpoints for all other slices in the aggregate
            # instead of regressing their status to ``not_run``.
            saved = directory / "execution.json"
            previous = json.loads(saved.read_text()) if saved.is_file() else {}
            binding_matches = previous.get("binding") == binding
            target_selected = not targets or selection["target_path"] in targets
            if not target_selected and binding_matches and previous.get("status") != "running":
                execution = previous
            elif not target_selected:
                execution = {
                    "target_path": selection["target_path"],
                    "status": "not_run",
                    "successful_sources": [],
                    "failed_sources": [],
                    "diagnostics": [],
                    "unfinished_sources": sources,
                    "total_translation_units": len(sources),
                    "reason": "target not selected for this invocation",
                }
            elif binding_matches and previous.get("status") == "complete":
                execution = previous
            else:
                attempt = directory / f"attempt-{time.time_ns()}"
                attempt.mkdir()
                shutil.copyfile(database, attempt / "compile_commands.container.json")
                name = f"ctu-{attempt.name}"
                command = [
                    "docker",
                    "run",
                    "--name",
                    name,
                    "--network",
                    "none",
                    "--memory",
                    memory,
                    "--cpus",
                    "1",
                    "--entrypoint",
                    "/opt/codechecker/bin/python3",
                    "-v",
                    f"{source.resolve()}:/workspace/source:ro",
                    "-v",
                    f"{attempt}:/workspace/output",
                    "-w",
                    "/workspace/source",
                    image_id,
                    "-c",
                    RESOURCE_WRAPPER,
                    "/opt/codechecker/bin/CodeChecker",
                    "analyze",
                    "/workspace/output/compile_commands.container.json",
                    "--output",
                    "/workspace/output/reports",
                    "--analyzers",
                    "clangsa",
                    "--ctu",
                    "--ctu-ast-mode",
                    ast_mode,
                    "-j",
                    "1",
                    "--capture-analysis-output",
                ]
                pending = {
                    **binding,
                    "status": "running",
                    "container_command": command,
                    "started_at": utc_now(),
                }
                _write(attempt / "execution.json", pending)
                with (attempt / "run.stdout.log").open("w") as stdout, (
                    attempt / "run.stderr.log"
                ).open("w") as stderr:
                    begin = time.monotonic()
                    try:
                        result = subprocess.run(
                            command,
                            stdout=stdout,
                            stderr=stderr,
                            check=False,
                            timeout=timeout_seconds,
                        )
                        exit_code, timed_out = result.returncode, False
                    except subprocess.TimeoutExpired:
                        exit_code, timed_out = None, True
                        try:
                            observed = subprocess.run(
                                [
                                    "docker",
                                    "exec",
                                    name,
                                    "/opt/codechecker/bin/python3",
                                    "-c",
                                    "import json,pathlib; print(json.dumps({'peak_memory_bytes':int(pathlib.Path('/sys/fs/cgroup/memory.peak').read_text())}))",
                                ],
                                capture_output=True,
                                text=True,
                                check=False,
                                timeout=5,
                            )
                        except subprocess.TimeoutExpired:
                            observed = None
                        if observed is not None and observed.returncode == 0:
                            try:
                                value = json.loads(observed.stdout)
                                value.update(
                                    {
                                        "observed_at": time.time(),
                                        "resource_method": "cgroup v2 memory.peak observed before timeout cleanup; not an attested whole-run peak",
                                    }
                                )
                                _write(attempt / "resource-observation.json", value)
                            except (ValueError, TypeError):
                                pass
                        subprocess.run(
                            ["docker", "stop", "-t", "5", name],
                            capture_output=True,
                            check=False,
                        )
                inspect = subprocess.run(
                    ["docker", "inspect", name, "--format", "{{json .State}}"],
                    capture_output=True,
                    text=True,
                    check=False,
                )
                subprocess.run(["docker", "rm", name], capture_output=True, check=False)
                execution = _collect_or_failure(
                    attempt, selection, source, project_id, exit_code=exit_code
                )
                execution.update(
                    {
                        **pending,
                        "binding": binding,
                        "status": "timeout" if timed_out else execution["status"],
                        "duration_seconds": time.monotonic() - begin,
                        "exit_code": exit_code,
                        "container_state": (
                            json.loads(inspect.stdout)
                            if inspect.returncode == 0
                            else None
                        ),
                        "peak_memory_bytes": None,
                        "resource_method": "cgroup v2 memory.peak; Linux child maximum RSS reported separately",
                    }
                )
                execution["command"] = command[
                    command.index("/opt/codechecker/bin/CodeChecker") :
                ]
                execution["blockers"] = []
                if timed_out:
                    execution["reason"] = (
                        "analysis budget exhausted; individually attested partial results retained"
                    )
                    execution["blockers"].append("analysis budget exhausted")
                if (execution.get("container_state") or {}).get("OOMKilled"):
                    execution["blockers"].append("container reported OOMKilled=true")
                if execution.get("failed_translation_units"):
                    execution["blockers"].append(
                        "Clang analyzer TU failures; inspect retained logs"
                    )
                observation = attempt / "resource-observation.json"
                if observation.is_file():
                    value = json.loads(observation.read_text())
                    execution["observed_peak_memory_bytes"] = value.get(
                        "peak_memory_bytes"
                    )
                    execution["resource_observation"] = {
                        "path": str(observation),
                        "sha256": _sha(observation),
                        **value,
                    }
                resources = attempt / "resources.json"
                if resources.is_file():
                    execution.update(json.loads(resources.read_text()))
                _write(attempt / "execution.json", execution)
                _write(saved, execution)
        executions.append(execution)
        target_analysis[selection["target_path"]] = {
            "status": execution["status"],
            "reason": execution.get("reason"),
        }
        _write(
            directory
            / ("imported-execution.json" if imported else "latest-execution.json"),
            execution,
        )
        checkpoint()
        print(f"{selection['target_path']}: {execution['status']}", flush=True)
    return checkpoint()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("slices", type=Path)
    parser.add_argument("--source", type=Path, required=True)
    parser.add_argument("--project-id", required=True)
    parser.add_argument("--image", required=True)
    parser.add_argument(
        "--ast-mode",
        choices=("parse-on-demand", "load-from-pch"),
        default="parse-on-demand",
    )
    parser.add_argument("--timeout-seconds", type=int, default=1800)
    parser.add_argument("--memory", default="5g")
    parser.add_argument("--adopt-existing", action="store_true")
    parser.add_argument("--target", action="append", default=[])
    args = parser.parse_args()
    result = execute_slices(
        args.slices,
        source=args.source,
        project_id=args.project_id,
        image=args.image,
        ast_mode=args.ast_mode,
        timeout_seconds=args.timeout_seconds,
        memory=args.memory,
        adopt_existing=args.adopt_existing,
        targets=args.target,
    )
    return (
        0 if all(item["status"] == "complete" for item in result["executions"]) else 1
    )


if __name__ == "__main__":
    raise SystemExit(main())
