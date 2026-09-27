#!/usr/bin/env python3
"""Build one conservative scorecard from layered UAF analysis artifacts."""

from __future__ import annotations

import argparse
import json
from collections import defaultdict
from pathlib import Path
from typing import Any, Iterable

from agent_runtime.codec import bytes_digest, digest, utc_now
from agent_runtime.errors import InvalidInput


SCHEMA = "agent-runtime/layered-uaf-summary/v1"


def _read_json(path: Path, label: str) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise InvalidInput(f"cannot read {label} {path}: {exc}") from exc


def _ratio(numerator: int, denominator: int) -> dict[str, Any]:
    return {
        "numerator": numerator,
        "denominator": denominator,
        "value": numerator / denominator if denominator else None,
        "status": "available" if denominator else "unavailable",
    }


def _normalize_path(value: Any) -> str | None:
    if not isinstance(value, str) or not value.strip():
        return None
    path = value.replace("\\", "/")
    marker = "/workspace/source/"
    if marker in path:
        path = path.split(marker, 1)[1]
    return path.removeprefix("./").lstrip("/")


def _combined_status(statuses: Iterable[str]) -> str:
    values = list(statuses)
    if not values or all(value == "not_run" for value in values):
        return "not_run"
    if all(value == "complete" for value in values):
        return "complete"
    if all(value in {"execution_failure", "invalid"} for value in values):
        return "execution_failure"
    return "partial"


def _empty_layer(path: Path | None, reason: str) -> dict[str, Any]:
    return {
        "path": str(path) if path is not None else None,
        "status": "not_run",
        "reason": reason,
        "total_translation_units": 0,
        "successful_translation_units": 0,
        "failed_translation_units": 0,
        "successful_sources": [],
        "failed_sources": [],
    }


def _metadata_layer(path: Path | None) -> dict[str, Any]:
    if path is None:
        return _empty_layer(None, "metadata was not supplied")
    if not path.is_file():
        return _empty_layer(path, "metadata is not available yet")
    try:
        value = _read_json(path, "layer metadata")
        records: list[dict[str, Any]] = []
        if isinstance(value, dict) and isinstance(value.get("tools"), list):
            for tool in value["tools"]:
                statistics = tool["analyzers"]["clangsa"]["analyzer_statistics"]
                records.append(
                    {
                        "status": None,
                        "successful": statistics.get("successful", 0),
                        "failed": statistics.get("failed", 0),
                        "successful_sources": statistics.get("successful_sources", []),
                        "failed_sources": statistics.get("failed_sources", []),
                        "total": tool.get("action_num"),
                    }
                )
        elif isinstance(value, dict) and isinstance(value.get("executions"), list):
            for execution in value["executions"]:
                records.append(
                    {
                        "status": execution.get("status"),
                        "successful": execution.get("successful_translation_units", 0),
                        "failed": execution.get("failed_translation_units", 0),
                        "successful_sources": execution.get("successful_sources", []),
                        "failed_sources": execution.get("failed_sources", []),
                        "total": execution.get("total_translation_units"),
                    }
                )
        elif isinstance(value, dict):
            records.append(
                {
                    "status": value.get("status"),
                    "successful": value.get("successful_translation_units", 0),
                    "failed": value.get("failed_translation_units", 0),
                    "successful_sources": value.get("successful_sources", []),
                    "failed_sources": value.get("failed_sources", []),
                    "total": value.get("total_translation_units"),
                }
            )
        if not records:
            raise KeyError("no analyzer metadata records")
        for record in records:
            for key in ("successful", "failed"):
                if type(record[key]) is not int or record[key] < 0:
                    raise InvalidInput(f"invalid analyzer count: {key}")
            for key in ("successful_sources", "failed_sources"):
                if not isinstance(record[key], list):
                    raise InvalidInput(f"invalid analyzer source array: {key}")

        successful_sources = {
            normalized
            for record in records
            for source in record["successful_sources"]
            if (normalized := _normalize_path(source)) is not None
        }
        failed_sources = {
            normalized
            for record in records
            for source in record["failed_sources"]
            if (normalized := _normalize_path(source)) is not None
        }
        successful = sum(int(record["successful"]) for record in records)
        failed = sum(int(record["failed"]) for record in records)
        declared_totals = [
            int(record["total"])
            for record in records
            if isinstance(record["total"], int)
        ]
        total = max(sum(declared_totals), successful + failed)
        record_statuses = [
            record["status"] for record in records if isinstance(record["status"], str)
        ]
        if record_statuses:
            status = _combined_status(record_statuses)
        elif failed == 0 and successful == total and total > 0:
            status = "complete"
        elif successful > 0:
            status = "partial"
        elif failed > 0:
            status = "execution_failure"
        else:
            status = "not_run"
        return {
            "path": str(path),
            "status": status,
            "reason": "metadata loaded",
            "total_translation_units": total,
            "successful_translation_units": successful,
            "failed_translation_units": failed,
            "successful_sources": sorted(successful_sources),
            "failed_sources": sorted(failed_sources),
            "metadata_sha256": bytes_digest(path.read_bytes()),
            "executions": value.get("executions", []),
            "target_analysis": value.get("target_analysis", {}),
            "diagnostics": [
                {**item, "path": normalized, "source": "targeted_ctu"}
                for execution in value.get("executions", [])
                for item in execution.get("diagnostics", [])
                if isinstance(item, dict)
                and (normalized := _normalize_path(item.get("path"))) is not None
                and type(item.get("line")) is int
                and item["line"] > 0
            ],
            "unique_successful_translation_units": len(successful_sources),
            "unique_failed_translation_units": len(failed_sources - successful_sources),
            "attempt_counts_include_overlapping_slices": bool(value.get("executions")),
        }
    except (InvalidInput, KeyError, TypeError, ValueError, AttributeError) as exc:
        return {
            **_empty_layer(path, f"invalid metadata: {exc}"),
            "status": "invalid",
        }


def _candidate_layer(path: Path | None) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    if path is None:
        return _empty_layer(None, "candidate JSON was not supplied"), []
    if not path.is_file():
        return _empty_layer(path, "candidate JSON is not available yet"), []
    try:
        value = _read_json(path, "lifetime candidate JSON")
        raw_candidates = value.get("candidates") if isinstance(value, dict) else None
        if not isinstance(raw_candidates, list):
            raise InvalidInput("lifetime candidate JSON needs a candidates array")
        candidates = []
        for raw in raw_candidates:
            if not isinstance(raw, dict):
                continue
            candidate_path = _normalize_path(raw.get("path"))
            line = raw.get("line")
            if candidate_path is None or type(line) is not int or line < 1:
                continue
            candidates.append({**raw, "path": candidate_path, "line": line})
        layer = {
            "path": str(path),
            "status": "complete",
            "reason": "candidate JSON loaded",
            "candidate_count": len(candidates),
        }
        return layer, candidates
    except InvalidInput as exc:
        return {
            "path": str(path),
            "status": "invalid",
            "reason": str(exc),
            "candidate_count": 0,
        }, []


def _evidence_items(path: Path | None) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    if path is None:
        return {
            "path": None,
            "status": "not_run",
            "reason": "ASan evidence was not supplied",
            "evidence_count": 0,
        }, []
    if not path.is_file():
        return {
            "path": str(path),
            "status": "not_run",
            "reason": "ASan evidence is not available yet",
            "evidence_count": 0,
        }, []
    try:
        value = _read_json(path, "ASan evidence")
        if isinstance(value, list):
            raw_items = value
        elif isinstance(value, dict):
            raw_items = value.get("evidence", value.get("results", []))
        else:
            raw_items = []
        if not isinstance(raw_items, list):
            raise InvalidInput("ASan evidence needs an evidence or results array")
        items = [item for item in raw_items if isinstance(item, dict)]
        statuses = [item.get("status") for item in items]
        layer_status = (
            "complete"
            if items and all(status in {"confirmed", "refuted"} for status in statuses)
            else "partial" if items else "not_run"
        )
        return {
            "path": str(path),
            "status": layer_status,
            "reason": "ASan evidence loaded" if items else "ASan evidence is empty",
            "evidence_count": len(items),
            "confirmed_count": sum(status == "confirmed" for status in statuses),
        }, items
    except InvalidInput as exc:
        return {
            "path": str(path),
            "status": "invalid",
            "reason": str(exc),
            "evidence_count": 0,
        }, []


def _baseline_by_project(
    manifest: dict[str, Any], run: dict[str, Any]
) -> dict[str, dict[str, Any]]:
    sample_projects = {
        sample["sample_id"]: sample["project_id"] for sample in manifest["samples"]
    }
    grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for result in run["sample_results"]:
        project_id = sample_projects.get(result.get("sample_id"))
        if project_id is not None:
            grouped[project_id].append(result)
    baseline = {}
    for project_id, results in grouped.items():
        representative = results[0]
        statuses = [
            result.get("analysis_status", "execution_failure") for result in results
        ]
        normalized = [
            "complete" if status == "complete" else "execution_failure"
            for status in statuses
        ]
        baseline[project_id] = {
            "status": _combined_status(normalized),
            "analysis_statuses": sorted(set(statuses)),
            "total_translation_units": representative["coverage"][
                "total_translation_units"
            ],
            "successful_translation_units": representative["coverage"][
                "analyzed_translation_units"
            ],
        }
    return baseline


def _candidate_identity(candidate: dict[str, Any]) -> tuple[Any, ...]:
    return (
        candidate.get("source"),
        candidate.get("rule_id"),
        candidate.get("path"),
        candidate.get("line"),
    )


def _evidence_matches(evidence: dict[str, Any], defect: dict[str, Any]) -> bool:
    if evidence.get("defect_id") == defect["defect_id"]:
        return True
    location = evidence.get("location")
    candidate = evidence.get("candidate")
    evidence_path = _normalize_path(
        evidence.get("path")
        or (location.get("path") if isinstance(location, dict) else None)
        or (candidate.get("path") if isinstance(candidate, dict) else None)
    )
    evidence_line = (
        evidence.get("line")
        or (location.get("line") if isinstance(location, dict) else None)
        or (candidate.get("line") if isinstance(candidate, dict) else None)
    )
    project_id = evidence.get("project_id")
    return (
        (project_id is None or project_id == defect["project_id"])
        and evidence_path == defect["path"]
        and evidence_line == defect["line"]
    )


def build_summary(
    manifest: dict[str, Any],
    ctu_run: dict[str, Any],
    ctu_report: dict[str, Any],
    project_inputs: dict[str, dict[str, Path | None]],
    *,
    asan_evidence_path: Path | None = None,
) -> dict[str, Any]:
    """Combine incomplete layered outputs without inferring that missing means safe."""

    if manifest.get("dataset_id") != ctu_run.get("dataset_id"):
        raise InvalidInput("CTU run does not match the frozen manifest dataset")
    if ctu_report.get("run_id") != ctu_run.get("run_id"):
        raise InvalidInput("CTU report does not match the CTU run")
    if ctu_report.get("manifest_digest") != ctu_run.get("manifest_digest"):
        raise InvalidInput("CTU report and run bind different manifests")

    manifest_projects = {sample["project_id"] for sample in manifest["samples"]}
    unknown_projects = set(project_inputs) - manifest_projects
    if unknown_projects:
        raise InvalidInput(
            f"project inputs are not in the manifest: {sorted(unknown_projects)}"
        )

    baseline = _baseline_by_project(manifest, ctu_run)
    project_layers: dict[str, dict[str, Any]] = {}
    lifetime_candidates: dict[str, list[dict[str, Any]]] = defaultdict(list)
    coverage_projects: dict[str, dict[str, Any]] = {}
    for project_id in sorted(manifest_projects):
        inputs = project_inputs.get(project_id, {})
        candidate_layer, candidates = _candidate_layer(
            inputs.get("lifetime_candidates")
        )
        lifetime_candidates[project_id].extend(candidates)
        non_ctu = _metadata_layer(inputs.get("non_ctu_metadata"))
        targeted = _metadata_layer(inputs.get("targeted_ctu_metadata"))
        baseline_layer = baseline.get(
            project_id,
            {
                "status": "not_run",
                "analysis_statuses": [],
                "total_translation_units": 0,
                "successful_translation_units": 0,
            },
        )
        project_layers[project_id] = {
            "corrected_ctu": baseline_layer,
            "lifetime_candidates": candidate_layer,
            "non_ctu": non_ctu,
            "targeted_ctu": targeted,
        }

        total = non_ctu["total_translation_units"]
        if total == 0:
            total = baseline_layer["total_translation_units"]
        base_success = set(non_ctu["successful_sources"])
        target_success = set(targeted["successful_sources"])
        base_universe = base_success | set(non_ctu["failed_sources"])
        if total and len(base_universe) == total:
            analyzed = len((base_success | target_success) & base_universe)
        elif non_ctu["successful_translation_units"]:
            recovered = len(target_success & set(non_ctu["failed_sources"]))
            analyzed = min(
                total,
                non_ctu["successful_translation_units"] + recovered,
            )
        else:
            analyzed = min(total, baseline_layer["successful_translation_units"])
        coverage_projects[project_id] = {
            "analyzed_translation_units": analyzed,
            "total_translation_units": total,
            "value": analyzed / total if total else None,
            "status": "available" if total else "unavailable",
            "recovered_by_targeted_ctu": max(
                0, analyzed - non_ctu["successful_translation_units"]
            ),
        }

    ctu_candidates: dict[str, list[dict[str, Any]]] = defaultdict(list)
    sample_project = {
        sample["sample_id"]: sample["project_id"] for sample in manifest["samples"]
    }
    for result in ctu_run["sample_results"]:
        project_id = sample_project.get(result.get("sample_id"))
        if project_id is None:
            continue
        for candidate in result.get("candidates", []):
            path = _normalize_path(candidate.get("path"))
            line = candidate.get("line")
            if path is not None and type(line) is int:
                ctu_candidates[project_id].append(
                    {
                        **candidate,
                        "source": "corrected_ctu",
                        "path": path,
                    }
                )
    for project_id, candidates in lifetime_candidates.items():
        lifetime_candidates[project_id] = [
            {**candidate, "source": "lifetime_candidates"} for candidate in candidates
        ]

    asan_layer, evidence_items = _evidence_items(asan_evidence_path)
    defects: list[dict[str, Any]] = []
    for sample in manifest["samples"]:
        project_id = sample["project_id"]
        available_candidates = [
            *ctu_candidates[project_id],
            *lifetime_candidates[project_id],
            *project_layers[project_id]["targeted_ctu"].get("diagnostics", []),
        ]
        for known in sample["known_defects"]:
            location = known["location"]
            defect = {
                "sample_id": sample["sample_id"],
                "project_id": project_id,
                "defect_id": known["defect_id"],
                "path": _normalize_path(location["path"]),
                "line": location["line"],
            }
            exact_candidates = {
                _candidate_identity(candidate): candidate
                for candidate in available_candidates
                if candidate["path"] == defect["path"]
                and candidate["line"] == defect["line"]
            }
            matching_evidence = [
                item for item in evidence_items if _evidence_matches(item, defect)
            ]
            conclusive_evidence = [
                item
                for item in matching_evidence
                if item.get("status") in {"confirmed", "refuted"}
            ]
            confirmed = any(
                item.get("status") == "confirmed" for item in matching_evidence
            )
            refuted = (
                bool(matching_evidence)
                and not confirmed
                and any(item.get("status") == "refuted" for item in matching_evidence)
            )
            target_layer = project_layers[project_id]["targeted_ctu"]
            target_status = (
                target_layer.get("target_analysis", {})
                .get(defect["path"], {})
                .get(
                    "status",
                    "invalid" if target_layer["status"] == "invalid" else "not_run",
                )
            )
            unevaluable = not exact_candidates and target_status in {
                "partial",
                "timeout",
                "execution_failure",
                "invalid",
                "running",
            }
            defects.append(
                {
                    **defect,
                    "candidate_status": (
                        "exact_match"
                        if exact_candidates
                        else "unevaluable" if unevaluable else "missed"
                    ),
                    "targeted_ctu_status": target_status,
                    "targeted_ctu_candidate_status": (
                        "exact_match"
                        if any(
                            x.get("source") == "targeted_ctu"
                            for x in exact_candidates.values()
                        )
                        else "missed" if target_status == "complete" else "unevaluable"
                    ),
                    "exact_candidates": list(exact_candidates.values()),
                    "evidence_status": (
                        "available" if conclusive_evidence else "inconclusive"
                    ),
                    "asan_evidence": matching_evidence,
                    "confirmation_status": (
                        "confirmed"
                        if confirmed
                        else "refuted" if refuted else "inconclusive"
                    ),
                }
            )

    exact_defects = [
        item for item in defects if item["candidate_status"] == "exact_match"
    ]
    total_units = sum(
        item["total_translation_units"] for item in coverage_projects.values()
    )
    analyzed_units = sum(
        item["analyzed_translation_units"] for item in coverage_projects.values()
    )
    layer_summary = {
        "corrected_ctu": {
            "status": _combined_status(
                item["corrected_ctu"]["status"] for item in project_layers.values()
            ),
            "projects": {
                project_id: layers["corrected_ctu"]
                for project_id, layers in project_layers.items()
            },
        },
        "lifetime_candidates": {
            "status": _combined_status(
                item["lifetime_candidates"]["status"]
                for item in project_layers.values()
            ),
            "projects": {
                project_id: layers["lifetime_candidates"]
                for project_id, layers in project_layers.items()
            },
        },
        "non_ctu": {
            "status": _combined_status(
                item["non_ctu"]["status"] for item in project_layers.values()
            ),
            "projects": {
                project_id: layers["non_ctu"]
                for project_id, layers in project_layers.items()
            },
        },
        "targeted_ctu": {
            "status": _combined_status(
                item["targeted_ctu"]["status"] for item in project_layers.values()
            ),
            "projects": {
                project_id: layers["targeted_ctu"]
                for project_id, layers in project_layers.items()
            },
        },
        "asan": asan_layer,
    }
    summary = {
        "schema": SCHEMA,
        "created_at": utc_now(),
        "dataset_id": manifest["dataset_id"],
        "manifest_digest": ctu_run["manifest_digest"],
        "ctu_run_id": ctu_run["run_id"],
        "ctu_report_id": ctu_report.get("report_id"),
        "coverage": {
            "unique_translation_units": _ratio(analyzed_units, total_units),
            "projects": coverage_projects,
            "method": (
                "union successful source paths across non-CTU and targeted CTU; "
                "fall back to corrected CTU coverage when layered metadata is absent"
            ),
        },
        "metrics": {
            "candidate_exact_recall": _ratio(len(exact_defects), len(defects)),
            "evidence_recall": _ratio(
                sum(item["evidence_status"] == "available" for item in exact_defects),
                len(exact_defects),
            ),
            "candidate_recall_upper_bound": _ratio(
                len(exact_defects)
                + sum(item["candidate_status"] == "unevaluable" for item in defects),
                len(defects),
            ),
            "candidate_evaluable_recall": _ratio(
                len(exact_defects),
                sum(item["candidate_status"] != "unevaluable" for item in defects),
            ),
            "confirmation_recall": _ratio(
                sum(item["confirmation_status"] == "confirmed" for item in defects),
                len(defects),
            ),
        },
        "layers": layer_summary,
        "defects": defects,
        "limitations": [
            "Candidate recall requires an exact normalized path and line match and is an all-defect observed lower bound.",
            "Failed target CTU without an exact candidate is unevaluable, not a demonstrated miss.",
            "Evidence recall is ASan evidence available per exact candidate.",
            "Only ASan evidence with status confirmed contributes to confirmation recall.",
            "Missing or unfinished evidence remains inconclusive and is never labeled missed.",
        ],
    }
    summary["input_artifacts"] = {
        str(path): bytes_digest(path.read_bytes())
        for inputs in project_inputs.values()
        for path in inputs.values()
        if path is not None and path.is_file()
    }
    if asan_evidence_path and asan_evidence_path.is_file():
        summary["input_artifacts"][str(asan_evidence_path)] = bytes_digest(
            asan_evidence_path.read_bytes()
        )
    summary["summary_digest"] = digest(summary)
    return summary


def _format_ratio(value: dict[str, Any]) -> str:
    if value["value"] is None:
        return f"unavailable ({value['numerator']}/{value['denominator']})"
    return f"{value['value']:.1%} ({value['numerator']}/{value['denominator']})"


def _format_memory(execution: dict[str, Any]) -> str:
    peak = execution.get("peak_memory_bytes")
    observed = execution.get("observed_peak_memory_bytes")
    if isinstance(peak, int) and peak > 0:
        return f"{peak / 2**30:.2f} GiB"
    if isinstance(observed, int) and observed > 0:
        return f"{observed / 2**30:.2f} GiB observed (final unavailable)"
    return "unavailable"


def render_markdown(summary: dict[str, Any]) -> str:
    metrics = summary["metrics"]
    lines = [
        f"# Layered UAF summary: {summary['dataset_id']}",
        "",
        f"- CTU run: `{summary['ctu_run_id']}`",
        f"- Manifest digest: `{summary['manifest_digest']}`",
        "",
        "## Scores",
        "",
        "| Metric | Result | Denominator |",
        "|---|---:|---|",
        f"| Unique TU coverage | "
        f"{_format_ratio(summary['coverage']['unique_translation_units'])} | "
        "Unique project translation units |",
        f"| Candidate exact recall | "
        f"{_format_ratio(metrics['candidate_exact_recall'])} | All frozen defects |",
        f"| Evidence recall | {_format_ratio(metrics['evidence_recall'])} | "
        "Exact candidate matches |",
        f"| Confirmation recall | "
        f"{_format_ratio(metrics['confirmation_recall'])} | All frozen defects |",
        "",
        "## Layer execution",
        "",
        "| Layer | Status |",
        "|---|---|",
    ]
    lines.extend(
        f"| {name} | `{layer['status']}` |" for name, layer in summary["layers"].items()
    )
    lines.extend(
        [
            "",
            "## Frozen defects",
            "",
            "| Defect | Candidate | Evidence | Confirmation |",
            "|---|---|---|---|",
        ]
    )
    lines.extend(
        f"| `{item['defect_id']}` | `{item['candidate_status']}` | "
        f"`{item['evidence_status']}` | `{item['confirmation_status']}` |"
        for item in summary["defects"]
    )
    lines.extend(
        [
            "",
            "## Targeted CTU executions",
            "",
            "| Project / target | Status | Successful / selected TU | Failed / unfinished TU | Duration (s) | Peak memory |",
            "|---|---|---:|---:|---:|---|",
            *[
                f"| `{project} / {execution.get('target_path')}` | `{execution['status']}` | "
                f"{execution.get('successful_translation_units', 0)}/{execution.get('total_translation_units', len(execution.get('selected_sources', [])))} | "
                f"{execution.get('failed_translation_units', 0)} / {len(execution.get('unfinished_sources', []))} | "
                f"{execution.get('duration_seconds')} | {_format_memory(execution)} |"
                for project, layer in summary["layers"]["targeted_ctu"][
                    "projects"
                ].items()
                for execution in layer.get("executions", [])
            ],
            "",
            "## Interpretation",
            "",
            "- Candidate recall uses exact normalized path and line matching; near hits do not count. This is an observed all-defect lower bound.",
            "- Incomplete target CTU without an exact hit is unevaluable, not a demonstrated miss; overlapping TU attempts are deduplicated for coverage.",
            "- Evidence recall counts conclusive ASan records for exact candidate matches.",
            "- Confirmation requires an ASan record whose status is `confirmed`.",
            "- Missing or unfinished ASan evidence is `inconclusive`, not `missed`.",
            "",
            f"Summary digest: `{summary['summary_digest']}`",
            "",
        ]
    )
    return "\n".join(lines)


def _assignment(value: str) -> tuple[str, Path]:
    project_id, separator, raw_path = value.partition("=")
    if not separator or not project_id or not raw_path:
        raise argparse.ArgumentTypeError("expected PROJECT_ID=PATH")
    return project_id, Path(raw_path)


def _assignments(values: list[tuple[str, Path]]) -> dict[str, Path]:
    result: dict[str, Path] = {}
    for project_id, path in values:
        if project_id in result:
            raise InvalidInput(f"duplicate input for project {project_id}")
        result[project_id] = path
    return result


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", type=Path, required=True)
    parser.add_argument("--ctu-run", type=Path, required=True)
    parser.add_argument("--ctu-report", type=Path, required=True)
    parser.add_argument(
        "--lifetime-candidates",
        action="append",
        default=[],
        type=_assignment,
        metavar="PROJECT_ID=PATH",
    )
    parser.add_argument(
        "--non-ctu-metadata",
        action="append",
        default=[],
        type=_assignment,
        metavar="PROJECT_ID=PATH",
    )
    parser.add_argument(
        "--targeted-ctu-metadata",
        action="append",
        default=[],
        type=_assignment,
        metavar="PROJECT_ID=PATH",
    )
    parser.add_argument("--asan-evidence", type=Path)
    parser.add_argument("--output-json", type=Path, required=True)
    parser.add_argument("--output-md", type=Path, required=True)
    args = parser.parse_args()

    candidates = _assignments(args.lifetime_candidates)
    non_ctu = _assignments(args.non_ctu_metadata)
    targeted = _assignments(args.targeted_ctu_metadata)
    project_ids = set(candidates) | set(non_ctu) | set(targeted)
    project_inputs = {
        project_id: {
            "lifetime_candidates": candidates.get(project_id),
            "non_ctu_metadata": non_ctu.get(project_id),
            "targeted_ctu_metadata": targeted.get(project_id),
        }
        for project_id in project_ids
    }
    summary = build_summary(
        _read_json(args.manifest, "manifest"),
        _read_json(args.ctu_run, "CTU run"),
        _read_json(args.ctu_report, "CTU report"),
        project_inputs,
        asan_evidence_path=args.asan_evidence,
    )
    args.output_json.parent.mkdir(parents=True, exist_ok=True)
    args.output_md.parent.mkdir(parents=True, exist_ok=True)
    args.output_json.write_text(
        json.dumps(summary, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    args.output_md.write_text(render_markdown(summary), encoding="utf-8")
    print(args.output_json.resolve())
    print(args.output_md.resolve())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
