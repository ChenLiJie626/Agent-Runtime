"""Score project-level CodeChecker CTU reports against the frozen UAF labels."""

from __future__ import annotations

import argparse
import json
import plistlib
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from agent_runtime.codec import bytes_digest, digest, utc_now
from agent_runtime.evaluation import (
    build_quality_report,
    dataset_manifest_digest,
    load_json_object,
    render_quality_markdown,
)
from agent_runtime.errors import InvalidInput

UAF_MESSAGES = ("use of memory after it is freed", "use after free", "use-after-free")


@dataclass(frozen=True)
class ProjectInput:
    project_id: str
    root: Path
    reports: Path
    image: str
    image_id: str
    ast_mode: str
    layered_execution: Path | None = None


def _relative_path(value: str, root: Path) -> str:
    normalized = value.replace("\\", "/")
    if normalized.startswith("/workspace/source/"):
        return normalized.removeprefix("/workspace/source/")
    try:
        return Path(value).resolve().relative_to(root.resolve()).as_posix()
    except (OSError, ValueError):
        return normalized


def _compile_source_path(row: dict[str, Any], root: Path) -> Path:
    source = Path(row["file"])
    if source.is_absolute():
        # The frozen reports/compile database may have been produced in the
        # analysis container.  Those paths are evidence labels, not host paths.
        normalized = source.as_posix()
        if normalized == "/workspace/source":
            return root
        if normalized.startswith("/workspace/source/"):
            return root / normalized.removeprefix("/workspace/source/")
        return source
    directory = Path(row["directory"])
    directory_text = directory.as_posix()
    if directory_text == "/workspace/source":
        directory = root
    elif directory.is_absolute() and directory_text.startswith("/workspace/source/"):
        directory = root / directory_text.removeprefix("/workspace/source/")
    if not directory.is_absolute():
        directory = root / directory
    return directory / source


def read_uaf_diagnostics(
    reports: Path,
    root: Path,
    project_id: str,
    tool: dict[str, Any],
    *,
    report_names: set[str] | None = None,
) -> list[dict[str, Any]]:
    """Read hashed UAF candidates independently of coverage/completion status."""
    result_sources = {
        Path(report).name: _relative_path(source, root)
        for report, source in tool.get("result_source_files", {}).items()
    }
    diagnostics: list[dict[str, Any]] = []
    for report in sorted(reports.glob("*.plist")):
        if report_names is not None and report.name not in report_names:
            continue
        value = plistlib.loads(report.read_bytes())
        files = [_relative_path(str(path), root) for path in value.get("files", [])]
        translation_unit = result_sources.get(report.name)
        for raw in value.get("diagnostics", []):
            message = str(raw.get("description", ""))
            if not any(marker in message.casefold() for marker in UAF_MESSAGES):
                continue
            location = raw.get("location", {})
            file_index = location.get("file")
            line = location.get("line")
            column = location.get("col", 1)
            if (
                type(file_index) is not int
                or not 0 <= file_index < len(files)
                or type(line) is not int
                or line < 1
                or type(column) is not int
                or column < 1
            ):
                continue
            identity = {
                "project_id": project_id,
                "path": files[file_index],
                "line": line,
                "column": column,
                "rule_id": str(raw.get("check_name", "clang-analyzer")),
                "message": message,
                "translation_unit": translation_unit,
            }
            diagnostics.append(
                {
                    "candidate_id": "ctu:" + digest(identity)[:24],
                    **identity,
                    "path_events": len(raw.get("path", [])),
                    "report": report.name,
                    "report_sha256": bytes_digest(report.read_bytes()),
                }
            )
    deduplicated = {candidate["candidate_id"]: candidate for candidate in diagnostics}
    diagnostics = sorted(
        deduplicated.values(),
        key=lambda row: (row["path"], row["line"], row["column"], row["candidate_id"]),
    )
    return diagnostics


def _load_project(item: ProjectInput) -> dict[str, Any]:
    compile_db_path = item.root / "compile_commands.json"
    compile_db = json.loads(compile_db_path.read_text(encoding="utf-8"))
    metadata = json.loads((item.reports / "metadata.json").read_text(encoding="utf-8"))
    tool = metadata["tools"][0]
    stats = tool["analyzers"]["clangsa"]["analyzer_statistics"]
    diagnostics = read_uaf_diagnostics(item.reports, item.root, item.project_id, tool)
    total = len(compile_db)
    successful = int(stats["successful"])
    failed = int(stats["failed"])
    timestamps = tool.get("timestamps", {})
    begin = timestamps.get("begin")
    end = timestamps.get("end")
    duration_seconds = (
        max(0.0, float(end) - float(begin))
        if isinstance(begin, (int, float))
        and not isinstance(begin, bool)
        and isinstance(end, (int, float))
        and not isinstance(end, bool)
        else 0.0
    )
    target_analysis: dict[str, Any] = {}
    if item.layered_execution is not None:
        try:
            layered = json.loads(item.layered_execution.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError) as exc:
            raise InvalidInput(
                f"cannot read layered execution {item.layered_execution}: {exc}"
            ) from exc
        if layered.get(
            "schema"
        ) != "agent-runtime/layered-ctu-execution/v1" or not isinstance(
            layered.get("target_analysis"), dict
        ):
            raise InvalidInput("invalid layered CTU execution report")
        target_analysis = layered["target_analysis"]
    return {
        "project_id": item.project_id,
        "root": str(item.root.resolve()),
        "head": False,
        "compile_database": {
            "path": str(compile_db_path.resolve()),
            "sha256": bytes_digest(compile_db_path.read_bytes()),
            "translation_units": total,
        },
        "reports": str(item.reports.resolve()),
        "image": item.image,
        "image_id": item.image_id,
        "codechecker_version": tool["version"],
        "ctu_mode": item.ast_mode,
        "successful_translation_units": successful,
        "failed_translation_units": failed,
        "successful_sources": [
            _relative_path(path, item.root)
            for path in (stats.get("successful_sources") or [])
        ],
        "failed_sources": [
            _relative_path(path, item.root)
            for path in (stats.get("failed_sources") or [])
        ],
        "duration_seconds": duration_seconds,
        "complete": successful == total and failed == 0,
        "target_analysis": target_analysis,
        "uaf_diagnostics": diagnostics,
    }


def _candidate(
    candidate: dict[str, Any], known_id: str | None, complete: bool
) -> dict[str, Any]:
    return {
        "candidate_id": candidate["candidate_id"],
        "known_defect_id": known_id,
        # A clean analyzer run confirms the observation, not that an unmatched
        # observation is a true defect.  Only a frozen-label match can be
        # published as confirmed by this scorer.
        "status": "confirmed" if known_id is not None and complete else "inconclusive",
        "path": candidate["path"],
        "line": candidate["line"],
        "column": candidate["column"],
        "rule_id": candidate["rule_id"],
        "message": candidate["message"],
        "translation_unit": candidate["translation_unit"],
        "path_events": candidate["path_events"],
        "evidence": {
            "report": candidate["report"],
            "report_sha256": candidate["report_sha256"],
        },
    }


def _target_status(project: dict[str, Any], target_path: str) -> str:
    """Resolve whether one labelled target was actually analyzable.

    Layered runners can provide an explicit per-target status.  Legacy project
    reports fall back to per-TU metadata for implementation files and finally
    to the old project-wide completeness bit.  This prevents an unrelated AST
    import failure from turning a successfully scanned target into ``not_run``.
    """

    explicit = project.get("target_analysis", {}).get(target_path)
    if isinstance(explicit, dict):
        explicit = explicit.get("status")
    if explicit in {
        "complete",
        "not_run",
        "timeout",
        "build_failure",
        "execution_failure",
    }:
        return explicit
    successful = set(project.get("successful_sources", ()))
    failed = set(project.get("failed_sources", ()))
    if target_path in successful:
        return "complete"
    if target_path in failed:
        return "execution_failure"
    return "complete" if project["complete"] else "execution_failure"


def build_run(
    manifest: dict[str, Any], projects: list[dict[str, Any]]
) -> dict[str, Any]:
    project_by_id = {project["project_id"].casefold(): project for project in projects}
    project_samples: dict[str, list[dict[str, Any]]] = {}
    for sample in manifest["samples"]:
        project_samples.setdefault(sample["project_id"].casefold(), []).append(sample)

    sample_results = []
    for project_key, samples in project_samples.items():
        project = project_by_id[project_key]
        allocated: set[str] = set()
        for sample_index, sample in enumerate(samples):
            defect = sample["known_defects"][0]
            expected_path = defect["location"]["path"]
            expected_line = defect["location"]["line"]
            target_status = _target_status(project, expected_path)
            target_complete = target_status == "complete"
            exact = [
                row
                for row in project["uaf_diagnostics"]
                if row["path"] == expected_path and row["line"] == expected_line
            ]
            for row in exact:
                allocated.add(row["candidate_id"])
            visible = list(exact)
            if sample_index == len(samples) - 1:
                visible.extend(
                    row
                    for row in project["uaf_diagnostics"]
                    if row["candidate_id"] not in allocated
                )
            candidates = [
                _candidate(
                    row,
                    defect["defect_id"] if row in exact else None,
                    target_complete,
                )
                for row in visible
            ]
            matched_ids = [row["candidate_id"] for row in exact]
            if exact and target_complete:
                evidence_status, verdict = "complete", "confirmed"
            elif exact:
                evidence_status, verdict = "partial", "inconclusive"
            elif not target_complete:
                evidence_status, verdict = "not_applicable", "inconclusive"
            else:
                evidence_status, verdict = "missing", "missed"
            evaluable = target_complete or bool(exact)
            candidate_status = (
                "matched" if exact else "missed" if evaluable else "not_run"
            )
            omissions = []
            if project["failed_translation_units"]:
                omissions.append(
                    f"{project['failed_translation_units']} translation units failed analysis"
                )
            sample_results.append(
                {
                    "sample_id": sample["sample_id"],
                    # Multiple frozen defects from one project share a single
                    # analyzer execution.  The report layer uses this identity
                    # to avoid multiplying project coverage and duration.
                    "coverage_scope_id": "project:" + project["project_id"],
                    "analysis_status": "complete" if target_complete else target_status,
                    "coverage": {
                        "total_targets": 1,
                        "analyzed_targets": 1 if evaluable else 0,
                        "total_translation_units": project["compile_database"][
                            "translation_units"
                        ],
                        "analyzed_translation_units": project[
                            "successful_translation_units"
                        ],
                        "lines_scanned": sum(
                            len(
                                _compile_source_path(row, Path(project["root"]))
                                .read_text(encoding="utf-8", errors="replace")
                                .splitlines()
                            )
                            for row in json.loads(
                                Path(project["compile_database"]["path"]).read_text(
                                    encoding="utf-8"
                                )
                            )
                        ),
                        "lines_changed": sample["surface"]["changed_lines"],
                        "omissions": omissions,
                    },
                    "resource": {
                        "duration_seconds": project["duration_seconds"],
                        "query_count": project["compile_database"]["translation_units"],
                        "model_tokens": 0,
                        "cost_usd": 0,
                    },
                    "known_defects": [
                        {
                            "defect_id": defect["defect_id"],
                            "evaluable": evaluable,
                            "candidate_status": candidate_status,
                            "candidate_ids": matched_ids,
                            "evidence_status": evidence_status,
                            "verdict": verdict,
                            "excluded": False,
                        }
                    ],
                    "candidates": candidates,
                    "alerts": [
                        {
                            "alert_id": "alert:" + row["candidate_id"],
                            "candidate_id": row["candidate_id"],
                            "rank": rank,
                            "duplicate_group": row["candidate_id"],
                            "adjudication": (
                                "true_defect" if row in exact else "unresolved"
                            ),
                        }
                        for rank, row in enumerate(visible, 1)
                        if target_complete and row in exact
                    ],
                }
            )

    return {
        "schema_version": {"major": 1, "minor": 0},
        "run_id": "clang-ctu-project:public-cpp-uaf-v1",
        "dataset_id": manifest["dataset_id"],
        "manifest_digest": dataset_manifest_digest(manifest),
        "created_at": utc_now(),
        "bindings": {
            "code_version": "project-ctu-v1",
            "rule": {"id": "cpp.use-after-free", "version": "1"},
            "backend": {"id": "CodeChecker.clangsa.ctu", "version": "6.29.1"},
            "prompt_digests": {},
            "model_route": "not_used",
            "budget": {
                "wall_time_seconds": sum(
                    project["duration_seconds"] for project in projects
                ),
                "query_limit": sum(
                    project["compile_database"]["translation_units"]
                    for project in projects
                ),
                "token_limit": 0,
                "cost_limit_usd": 0,
            },
            "top_k": 10,
        },
        "sample_results": sample_results,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", type=Path, required=True)
    parser.add_argument("--qlever-project", type=Path, required=True)
    parser.add_argument("--qlever-reports", type=Path, required=True)
    parser.add_argument("--qlever-image-id", required=True)
    parser.add_argument("--qlever-layered-execution", type=Path)
    parser.add_argument("--ghidra-project", type=Path, required=True)
    parser.add_argument("--ghidra-reports", type=Path, required=True)
    parser.add_argument("--ghidra-image-id", required=True)
    parser.add_argument("--ghidra-layered-execution", type=Path)
    parser.add_argument("--output-dir", type=Path, required=True)
    args = parser.parse_args()

    manifest = load_json_object(args.manifest)
    projects = [
        _load_project(
            ProjectInput(
                "ad-freiburg/qlever",
                args.qlever_project,
                args.qlever_reports,
                "agent-runtime/clang-ctu:clang21-codechecker6291",
                args.qlever_image_id,
                "parse-on-demand",
                args.qlever_layered_execution,
            )
        ),
        _load_project(
            ProjectInput(
                "NationalSecurityAgency/ghidra",
                args.ghidra_project,
                args.ghidra_reports,
                "agent-runtime/clang-ctu:ghidra-build",
                args.ghidra_image_id,
                "load-from-pch",
                args.ghidra_layered_execution,
            )
        ),
    ]
    run = build_run(manifest, projects)
    report = build_quality_report(manifest, run)
    args.output_dir.mkdir(parents=True, exist_ok=True)
    outputs = {
        "projects.json": {"projects": projects},
        "run.json": run,
        "report.json": report,
    }
    for name, value in outputs.items():
        (args.output_dir / name).write_text(
            json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )
    (args.output_dir / "report.md").write_text(
        render_quality_markdown(report), encoding="utf-8"
    )
    print(
        json.dumps(
            {
                "candidate_recall_evaluable": report["metrics"][
                    "candidate_recall_evaluable"
                ],
                "candidate_recall_lower_bound": report["metrics"][
                    "candidate_recall_lower_bound"
                ],
                "evidence_recall": report["metrics"]["evidence_recall"],
                "confirmation_recall_evaluable": report["metrics"][
                    "confirmation_recall_evaluable"
                ],
                "confirmation_recall_lower_bound": report["metrics"][
                    "confirmation_recall_lower_bound"
                ],
                "output_dir": str(args.output_dir.resolve()),
            },
            ensure_ascii=False,
        )
    )


if __name__ == "__main__":
    main()
