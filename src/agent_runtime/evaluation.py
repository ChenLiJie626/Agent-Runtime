"""Reproducible dataset and quality reporting contracts for SPEC 008.

The evaluation layer is deliberately separate from ``DefectRuntime``.  It
validates frozen public inputs and consumes exported run observations; it does
not change a rule decision or infer that unexamined code is safe.
"""

from __future__ import annotations

import json
import ipaddress
import math
import random
import urllib.request
from collections import defaultdict
from datetime import datetime
from pathlib import Path, PurePosixPath
from typing import Any, Callable
from urllib.parse import urlparse

from .codec import bytes_digest, digest, validate_relative_path
from .errors import InvalidInput

EVALUATION_SCHEMA_VERSION = {"major": 1, "minor": 0}
PARTITIONS = {"tuning", "holdout", "shadow"}
ANALYSIS_STATUSES = {"complete", "not_run", "timeout", "build_failure", "execution_failure"}
CANDIDATE_STATUSES = {
    "confirmed", "refuted", "inconclusive", "not_run", "timeout",
    "build_failure", "execution_failure",
}
ADJUDICATIONS = {"true_defect", "false_alarm", "unresolved", "out_of_scope"}


def _fail(path: str, message: str) -> None:
    raise InvalidInput(f"{path}: {message}")


def _mapping(value: Any, path: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        _fail(path, "must be an object")
    return value


def _sequence(value: Any, path: str) -> list[Any]:
    if not isinstance(value, list):
        _fail(path, "must be an array")
    return value


def _text(value: Any, path: str) -> str:
    if not isinstance(value, str) or not value.strip():
        _fail(path, "must be a nonempty string")
    return value


def _integer(value: Any, path: str, *, minimum: int = 0) -> int:
    if type(value) is not int or value < minimum:
        _fail(path, f"must be an integer >= {minimum}")
    return value


def _number(value: Any, path: str, *, nullable: bool = False) -> float | None:
    if value is None and nullable:
        return None
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        _fail(path, "must be a finite nonnegative number")
    value = float(value)
    if not math.isfinite(value) or value < 0:
        _fail(path, "must be a finite nonnegative number")
    return value


def _hex(value: Any, path: str, lengths: tuple[int, ...]) -> str:
    value = _text(value, path)
    if len(value) not in lengths or any(char not in "0123456789abcdef" for char in value):
        _fail(path, f"must be lowercase hexadecimal with length {lengths}")
    return value


def _public_https(value: Any, path: str) -> str:
    value = _text(value, path)
    parsed = urlparse(value)
    if (parsed.scheme != "https" or not parsed.hostname or parsed.username
            or parsed.password or parsed.hostname == "localhost"
            or parsed.hostname.endswith(".local")):
        _fail(path, "must be a public HTTPS URL without embedded credentials")
    try:
        address = ipaddress.ip_address(parsed.hostname)
    except ValueError:
        pass
    else:
        if not address.is_global:
            _fail(path, "must not use a private, loopback or reserved address")
    return value


def _timestamp(value: Any, path: str) -> str:
    value = _text(value, path)
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise InvalidInput(f"{path}: must be an ISO-8601 timestamp") from exc
    if parsed.tzinfo is None:
        _fail(path, "must include a timezone")
    return value


def _version(value: Any, path: str) -> None:
    value = _mapping(value, path)
    if value != EVALUATION_SCHEMA_VERSION:
        _fail(path, f"unsupported version; expected {EVALUATION_SCHEMA_VERSION}")


def load_json_object(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise InvalidInput(f"cannot read JSON object {path}: {exc}") from exc
    return _mapping(value, str(path))


def validate_dataset_manifest(value: dict[str, Any]) -> None:
    """Validate the frozen public C++ dataset contract used by S4."""

    value = _mapping(value, "manifest")
    _version(value.get("schema_version"), "manifest.schema_version")
    _text(value.get("dataset_id"), "manifest.dataset_id")
    _timestamp(value.get("frozen_at"), "manifest.frozen_at")

    provenance = _mapping(value.get("provenance"), "manifest.provenance")
    _text(provenance.get("name"), "manifest.provenance.name")
    _public_https(provenance.get("url"), "manifest.provenance.url")
    _hex(provenance.get("revision"), "manifest.provenance.revision", (40, 64))
    _text(provenance.get("license_spdx"), "manifest.provenance.license_spdx")
    _public_https(provenance.get("license_url"), "manifest.provenance.license_url")
    source_artifact = _mapping(
        provenance.get("source_artifact"), "manifest.provenance.source_artifact"
    )
    _public_https(source_artifact.get("url"),
                  "manifest.provenance.source_artifact.url")
    _hex(source_artifact.get("sha256"),
         "manifest.provenance.source_artifact.sha256", (64,))
    _integer(source_artifact.get("size_bytes"),
             "manifest.provenance.source_artifact.size_bytes", minimum=1)

    samples = _sequence(value.get("samples"), "manifest.samples")
    if not samples:
        _fail("manifest.samples", "must contain at least one sample")
    sample_ids: set[str] = set()
    project_partitions: dict[str, set[str]] = defaultdict(set)
    for index, raw_sample in enumerate(samples):
        path = f"manifest.samples[{index}]"
        sample = _mapping(raw_sample, path)
        sample_id = _text(sample.get("sample_id"), f"{path}.sample_id")
        if sample_id in sample_ids:
            _fail(f"{path}.sample_id", "must be unique")
        sample_ids.add(sample_id)
        project_id = _text(sample.get("project_id"), f"{path}.project_id")
        _public_https(sample.get("repository_url"), f"{path}.repository_url")
        if sample.get("language") != "C++":
            _fail(f"{path}.language", "fixed evaluation data must be C++")
        partition = _text(sample.get("partition"), f"{path}.partition")
        if partition not in PARTITIONS:
            _fail(f"{path}.partition", f"must be one of {sorted(PARTITIONS)}")
        project_partitions[project_id].add(partition)
        _timestamp(sample.get("public_at"), f"{path}.public_at")
        _public_https(sample.get("pr_url"), f"{path}.pr_url")
        base_commit = _hex(sample.get("base_commit"), f"{path}.base_commit", (40, 64))
        head_commit = _hex(sample.get("head_commit"), f"{path}.head_commit", (40, 64))
        if base_commit == head_commit:
            _fail(path, "base_commit and head_commit must differ")

        license_info = _mapping(sample.get("license"), f"{path}.license")
        _text(license_info.get("spdx"), f"{path}.license.spdx")
        _public_https(license_info.get("url"), f"{path}.license.url")
        _text(license_info.get("redistribution"),
              f"{path}.license.redistribution")

        artifact_scope = sample.get("artifact_scope", "full-project-archive")
        if artifact_scope not in {"full-project-archive", "primary-defect-file"}:
            _fail(
                f"{path}.artifact_scope",
                "must be full-project-archive or primary-defect-file",
            )

        artifacts = _sequence(sample.get("artifacts"), f"{path}.artifacts")
        roles: set[str] = set()
        for artifact_index, raw_artifact in enumerate(artifacts):
            artifact_path = f"{path}.artifacts[{artifact_index}]"
            artifact = _mapping(raw_artifact, artifact_path)
            role = _text(artifact.get("role"), f"{artifact_path}.role")
            if role in roles:
                _fail(f"{artifact_path}.role", "must be unique in a sample")
            roles.add(role)
            url = _public_https(artifact.get("url"), f"{artifact_path}.url")
            commit = base_commit if role == "base_source" else head_commit
            if role in {"base_source", "head_source"} and commit not in url:
                _fail(f"{artifact_path}.url", "must visibly bind the pinned commit")
            _hex(artifact.get("sha256"), f"{artifact_path}.sha256", (64,))
            _integer(artifact.get("size_bytes"), f"{artifact_path}.size_bytes", minimum=1)
        if not {"base_source", "head_source"}.issubset(roles):
            _fail(f"{path}.artifacts", "must include base_source and head_source")

        build = _mapping(sample.get("build"), f"{path}.build")
        _text(build.get("variant_id"), f"{path}.build.variant_id")
        recipe_digest = _hex(
            build.get("recipe_digest"), f"{path}.build.recipe_digest", (64,)
        )
        commands = _sequence(build.get("commands"), f"{path}.build.commands")
        if not commands or any(not isinstance(command, list) or not command
                               or any(not isinstance(token, str) or not token
                                      for token in command)
                               for command in commands):
            _fail(f"{path}.build.commands", "must contain tokenized commands")
        toolchain = _mapping(build.get("toolchain"), f"{path}.build.toolchain")
        _text(toolchain.get("cxx"), f"{path}.build.toolchain.cxx")
        _text(toolchain.get("version"), f"{path}.build.toolchain.version")
        _hex(build.get("dependency_digest"),
             f"{path}.build.dependency_digest", (64,))
        dependency_mode = build.get("dependency_mode")
        if dependency_mode == "pinned-ubuntu-package-set":
            lock_path = _text(build.get("dependency_lock_path"),
                              f"{path}.build.dependency_lock_path")
            lock_parts = PurePosixPath(lock_path).parts
            if (PurePosixPath(lock_path).is_absolute() or ".." in lock_parts
                    or lock_parts[:2] != ("evaluation", "builds")):
                _fail(f"{path}.build.dependency_lock_path",
                      "must be a repository-relative path below evaluation/builds")
        if build.get("network") != "disabled":
            _fail(f"{path}.build.network", "must be disabled")
        if recipe_digest != digest({
            "commands": commands,
            "toolchain": toolchain,
            "network": build["network"],
        }):
            _fail(f"{path}.build.recipe_digest",
                  "does not bind commands, toolchain and network policy")

        policy = _mapping(sample.get("input_policy"), f"{path}.input_policy")
        allowed = _sequence(policy.get("allowed_to_agent"),
                            f"{path}.input_policy.allowed_to_agent")
        withheld = _sequence(policy.get("withheld_from_agent"),
                             f"{path}.input_policy.withheld_from_agent")
        if not allowed or "known_defects" not in withheld or "fix_metadata" not in withheld:
            _fail(f"{path}.input_policy",
                  "must allow source material and withhold known_defects/fix_metadata")

        surface = _mapping(sample.get("surface"), f"{path}.surface")
        surface_files = _integer(surface.get("files"), f"{path}.surface.files", minimum=1)
        _integer(surface.get("changed_lines"),
                 f"{path}.surface.changed_lines", minimum=1)
        surface_paths = _sequence(surface.get("paths"), f"{path}.surface.paths")
        if len(surface_paths) != surface_files or len(surface_paths) != len(set(surface_paths)):
            _fail(f"{path}.surface.paths",
                  "must contain one unique path per declared surface file")
        for surface_index, surface_path in enumerate(surface_paths):
            surface_path = _text(
                surface_path, f"{path}.surface.paths[{surface_index}]"
            )
            validate_relative_path(surface_path)

        defects = _sequence(sample.get("known_defects"), f"{path}.known_defects")
        if not defects:
            _fail(f"{path}.known_defects", "must not be empty")
        defect_ids: set[str] = set()
        for defect_index, raw_defect in enumerate(defects):
            defect_path = f"{path}.known_defects[{defect_index}]"
            defect = _mapping(raw_defect, defect_path)
            defect_id = _text(defect.get("defect_id"), f"{defect_path}.defect_id")
            if defect_id in defect_ids:
                _fail(f"{defect_path}.defect_id", "must be unique in a sample")
            defect_ids.add(defect_id)
            _text(defect.get("category"), f"{defect_path}.category")
            if defect.get("difficulty") not in {"low", "medium", "high"}:
                _fail(f"{defect_path}.difficulty", "must be low, medium or high")
            location = _mapping(defect.get("location"), f"{defect_path}.location")
            location_path = _text(location.get("path"), f"{defect_path}.location.path")
            validate_relative_path(location_path)
            _integer(location.get("line"), f"{defect_path}.location.line", minimum=1)
            _public_https(defect.get("label_source_url"),
                          f"{defect_path}.label_source_url")
            _hex(defect.get("label_digest"), f"{defect_path}.label_digest", (64,))

    overlapping = {
        project: sorted(partitions)
        for project, partitions in project_partitions.items()
        if "tuning" in partitions and ({"holdout", "shadow"} & partitions)
    }
    if overlapping:
        _fail("manifest.samples",
              f"projects cannot cross tuning and held-out partitions: {overlapping}")


def dataset_manifest_digest(value: dict[str, Any]) -> str:
    validate_dataset_manifest(value)
    return digest(value)


def _validate_coverage(value: Any, path: str) -> None:
    coverage = _mapping(value, path)
    for total_name, analyzed_name in (
        ("total_targets", "analyzed_targets"),
        ("total_translation_units", "analyzed_translation_units"),
    ):
        total = _integer(coverage.get(total_name), f"{path}.{total_name}")
        analyzed = _integer(coverage.get(analyzed_name), f"{path}.{analyzed_name}")
        if analyzed > total:
            _fail(path, f"{analyzed_name} cannot exceed {total_name}")
    _integer(coverage.get("lines_scanned"), f"{path}.lines_scanned")
    _integer(coverage.get("lines_changed"), f"{path}.lines_changed")
    omissions = _sequence(coverage.get("omissions"), f"{path}.omissions")
    if any(not isinstance(item, str) or not item for item in omissions):
        _fail(f"{path}.omissions", "must contain nonempty strings")


def validate_evaluation_run(value: dict[str, Any], manifest: dict[str, Any]) -> None:
    """Ensure a run accounts for every frozen sample and known positive."""

    validate_dataset_manifest(manifest)
    run = _mapping(value, "run")
    _version(run.get("schema_version"), "run.schema_version")
    _text(run.get("run_id"), "run.run_id")
    if run.get("dataset_id") != manifest["dataset_id"]:
        _fail("run.dataset_id", "does not match the manifest")
    if run.get("manifest_digest") != digest(manifest):
        _fail("run.manifest_digest", "does not match the frozen manifest")
    _timestamp(run.get("created_at"), "run.created_at")

    bindings = _mapping(run.get("bindings"), "run.bindings")
    _text(bindings.get("code_version"), "run.bindings.code_version")
    rule = _mapping(bindings.get("rule"), "run.bindings.rule")
    _text(rule.get("id"), "run.bindings.rule.id")
    _text(rule.get("version"), "run.bindings.rule.version")
    backend = _mapping(bindings.get("backend"), "run.bindings.backend")
    _text(backend.get("id"), "run.bindings.backend.id")
    _text(backend.get("version"), "run.bindings.backend.version")
    prompt_digests = _mapping(bindings.get("prompt_digests"),
                              "run.bindings.prompt_digests")
    for name, prompt_digest in prompt_digests.items():
        _text(name, "run.bindings.prompt_digests key")
        _hex(prompt_digest, f"run.bindings.prompt_digests.{name}", (64,))
    _text(bindings.get("model_route"), "run.bindings.model_route")
    budget = _mapping(bindings.get("budget"), "run.bindings.budget")
    for name in ("wall_time_seconds", "query_limit", "token_limit", "cost_limit_usd"):
        _number(budget.get(name), f"run.bindings.budget.{name}")
    top_k = _integer(bindings.get("top_k"), "run.bindings.top_k", minimum=1)
    if top_k > 1000:
        _fail("run.bindings.top_k", "must be <= 1000")

    manifest_samples = {sample["sample_id"]: sample for sample in manifest["samples"]}
    results = _sequence(run.get("sample_results"), "run.sample_results")
    seen_samples: set[str] = set()
    coverage_scopes: dict[str, tuple[dict[str, Any], dict[str, Any]]] = {}
    for index, raw_result in enumerate(results):
        path = f"run.sample_results[{index}]"
        result = _mapping(raw_result, path)
        sample_id = _text(result.get("sample_id"), f"{path}.sample_id")
        if sample_id not in manifest_samples:
            _fail(f"{path}.sample_id", "is not in the manifest")
        if sample_id in seen_samples:
            _fail(f"{path}.sample_id", "must be unique")
        seen_samples.add(sample_id)
        if result.get("analysis_status") not in ANALYSIS_STATUSES:
            _fail(f"{path}.analysis_status",
                  f"must be one of {sorted(ANALYSIS_STATUSES)}")
        _validate_coverage(result.get("coverage"), f"{path}.coverage")
        coverage_scope_id = result.get("coverage_scope_id")
        if coverage_scope_id is not None:
            coverage_scope_id = _text(
                coverage_scope_id, f"{path}.coverage_scope_id"
            )
        resource = _mapping(result.get("resource"), f"{path}.resource")
        _number(resource.get("duration_seconds"), f"{path}.resource.duration_seconds")
        _integer(resource.get("query_count"), f"{path}.resource.query_count")
        _number(resource.get("model_tokens"), f"{path}.resource.model_tokens",
                nullable=True)
        _number(resource.get("cost_usd"), f"{path}.resource.cost_usd", nullable=True)
        if coverage_scope_id is not None:
            coverage = result["coverage"]
            shared_coverage = {
                key: coverage[key]
                for key in (
                    "total_translation_units",
                    "analyzed_translation_units",
                    "lines_scanned",
                    "omissions",
                )
            }
            shared_measurements = (shared_coverage, resource)
            previous = coverage_scopes.get(coverage_scope_id)
            if previous is not None and previous != shared_measurements:
                _fail(
                    f"{path}.coverage_scope_id",
                    "shared scopes must have identical project coverage and resource data",
                )
            coverage_scopes[coverage_scope_id] = shared_measurements

        candidates = _sequence(result.get("candidates"), f"{path}.candidates")
        candidate_ids: set[str] = set()
        candidate_by_id: dict[str, dict[str, Any]] = {}
        for candidate_index, raw_candidate in enumerate(candidates):
            candidate_path = f"{path}.candidates[{candidate_index}]"
            candidate = _mapping(raw_candidate, candidate_path)
            candidate_id = _text(candidate.get("candidate_id"),
                                 f"{candidate_path}.candidate_id")
            if candidate_id in candidate_ids:
                _fail(f"{candidate_path}.candidate_id", "must be unique")
            candidate_ids.add(candidate_id)
            candidate_by_id[candidate_id] = candidate
            if candidate.get("status") not in CANDIDATE_STATUSES:
                _fail(f"{candidate_path}.status",
                      f"must be one of {sorted(CANDIDATE_STATUSES)}")
            known_id = candidate.get("known_defect_id")
            if known_id is not None and not isinstance(known_id, str):
                _fail(f"{candidate_path}.known_defect_id", "must be string or null")

        expected_defects = {
            defect["defect_id"]
            for defect in manifest_samples[sample_id]["known_defects"]
        }
        for candidate_id, candidate in candidate_by_id.items():
            known_id = candidate.get("known_defect_id")
            if known_id is not None and known_id not in expected_defects:
                _fail(f"{path}.candidates.{candidate_id}.known_defect_id",
                      "is not in the sample manifest")
        observed_defects: set[str] = set()
        defect_results = _sequence(result.get("known_defects"),
                                   f"{path}.known_defects")
        for defect_index, raw_defect in enumerate(defect_results):
            defect_path = f"{path}.known_defects[{defect_index}]"
            defect = _mapping(raw_defect, defect_path)
            defect_id = _text(defect.get("defect_id"), f"{defect_path}.defect_id")
            if defect_id not in expected_defects:
                _fail(f"{defect_path}.defect_id", "is not in the sample manifest")
            if defect_id in observed_defects:
                _fail(f"{defect_path}.defect_id", "must be unique")
            observed_defects.add(defect_id)
            if type(defect.get("evaluable")) is not bool:
                _fail(f"{defect_path}.evaluable", "must be boolean")
            if defect.get("candidate_status") not in {"matched", "missed", "not_run"}:
                _fail(f"{defect_path}.candidate_status", "has an invalid value")
            if defect.get("evidence_status") not in {
                "complete", "partial", "missing", "not_applicable"
            }:
                _fail(f"{defect_path}.evidence_status", "has an invalid value")
            if defect.get("verdict") not in {
                "confirmed", "refuted", "inconclusive", "missed", "not_run"
            }:
                _fail(f"{defect_path}.verdict", "has an invalid value")
            if type(defect.get("excluded")) is not bool:
                _fail(f"{defect_path}.excluded", "must be boolean")
            if not defect["evaluable"] and (
                defect["candidate_status"] != "not_run"
                or defect["evidence_status"] != "not_applicable"
                or defect["verdict"] not in {"not_run", "inconclusive"}
            ):
                _fail(
                    defect_path,
                    "unevaluable defects must be not_run/not_applicable and cannot be scored as misses",
                )
            if defect["evaluable"] and defect["candidate_status"] == "not_run":
                _fail(defect_path, "evaluable defects cannot have candidate_status not_run")
            if result["analysis_status"] == "complete" and not defect["evaluable"]:
                _fail(defect_path, "a complete analysis cannot leave a known defect unevaluable")
            matched_ids = _sequence(defect.get("candidate_ids"),
                                    f"{defect_path}.candidate_ids")
            if len(matched_ids) != len(set(matched_ids)):
                _fail(f"{defect_path}.candidate_ids", "must be unique")
            if any(candidate_id not in candidate_ids for candidate_id in matched_ids):
                _fail(f"{defect_path}.candidate_ids", "references an unknown candidate")
            if defect["candidate_status"] == "matched" and not matched_ids:
                _fail(f"{defect_path}.candidate_ids", "matched defects need candidates")
            if defect["candidate_status"] != "matched" and matched_ids:
                _fail(f"{defect_path}.candidate_ids", "unmatched defects cannot link candidates")
            if any(candidate_by_id[candidate_id].get("known_defect_id") != defect_id
                   for candidate_id in matched_ids):
                _fail(f"{defect_path}.candidate_ids",
                      "linked candidates must bind the same known defect")
            if defect["verdict"] == "confirmed" and (
                defect["candidate_status"] != "matched"
                or defect["evidence_status"] != "complete"
            ):
                _fail(defect_path,
                      "confirmed known defects require a matched candidate and complete evidence")
        if observed_defects != expected_defects:
            _fail(f"{path}.known_defects",
                  f"must account for every known defect: {sorted(expected_defects)}")

        alerts = _sequence(result.get("alerts"), f"{path}.alerts")
        alert_ids: set[str] = set()
        ranks: set[int] = set()
        for alert_index, raw_alert in enumerate(alerts):
            alert_path = f"{path}.alerts[{alert_index}]"
            alert = _mapping(raw_alert, alert_path)
            alert_id = _text(alert.get("alert_id"), f"{alert_path}.alert_id")
            if alert_id in alert_ids:
                _fail(f"{alert_path}.alert_id", "must be unique")
            alert_ids.add(alert_id)
            candidate_id = _text(alert.get("candidate_id"),
                                 f"{alert_path}.candidate_id")
            if candidate_id not in candidate_by_id:
                _fail(f"{alert_path}.candidate_id", "references an unknown candidate")
            if candidate_by_id[candidate_id]["status"] != "confirmed":
                _fail(f"{alert_path}.candidate_id", "published alerts must be confirmed")
            rank = _integer(alert.get("rank"), f"{alert_path}.rank", minimum=1)
            if rank in ranks:
                _fail(f"{alert_path}.rank", "must be unique in a sample")
            ranks.add(rank)
            _text(alert.get("duplicate_group"), f"{alert_path}.duplicate_group")
            if alert.get("adjudication") not in ADJUDICATIONS:
                _fail(f"{alert_path}.adjudication",
                      f"must be one of {sorted(ADJUDICATIONS)}")

    if seen_samples != set(manifest_samples):
        _fail("run.sample_results", "must account for every manifest sample")


def _ratio(numerator: int | float, denominator: int | float) -> dict[str, Any]:
    return {
        "numerator": numerator,
        "denominator": denominator,
        "value": (numerator / denominator if denominator else None),
        "status": "available" if denominator else "unavailable",
    }


def _recall_bounds(
    defects: list[dict[str, Any]], success: Callable[[dict[str, Any]], bool]
) -> dict[str, Any]:
    """Bound full-dataset recall without treating unevaluable defects as misses."""

    evaluable = [defect for defect in defects if defect["evaluable"]]
    successes = sum(success(defect) for defect in evaluable)
    unevaluable = len(defects) - len(evaluable)
    return {
        "lower": _ratio(successes, len(defects)),
        "upper": _ratio(successes + unevaluable, len(defects)),
        "unevaluable": unevaluable,
    }


def _coverage_scope_key(result: dict[str, Any]) -> tuple[str, str]:
    scope_id = result.get("coverage_scope_id")
    if scope_id is None:
        return ("sample", result["sample_id"])
    return ("shared", scope_id)


def _unique_coverage_results(
    results: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Return one measurement per project analysis, preserving legacy runs."""

    unique: dict[tuple[str, str], dict[str, Any]] = {}
    for result in results:
        unique.setdefault(_coverage_scope_key(result), result)
    return list(unique.values())


def _unique_lines_scanned(results: list[dict[str, Any]]) -> int:
    return sum(
        result["coverage"]["lines_scanned"]
        for result in _unique_coverage_results(results)
    )


def _deduplicated_alerts(results: list[dict[str, Any]]) -> list[dict[str, Any]]:
    groups: dict[tuple[str, str], dict[str, Any]] = {}
    for result in results:
        for alert in result["alerts"]:
            key = (result["sample_id"], alert["duplicate_group"])
            previous = groups.get(key)
            if previous and previous["adjudication"] != alert["adjudication"]:
                _fail("run.sample_results.alerts",
                      f"duplicate group {key} has conflicting adjudications")
            if previous is None or alert["rank"] < previous["rank"]:
                groups[key] = {**alert, "sample_id": result["sample_id"]}
    return list(groups.values())


def _core_metrics(results: list[dict[str, Any]]) -> dict[str, Any]:
    observations = [
        (result["analysis_status"], defect)
        for result in results
        for defect in result["known_defects"]
    ]
    defects = [defect for _status, defect in observations]
    evaluable = [defect for defect in defects if defect["evaluable"]]
    matched = [defect for defect in evaluable if defect["candidate_status"] == "matched"]
    candidates = [candidate for result in results for candidate in result["candidates"]]
    alerts = _deduplicated_alerts(results)
    true_alerts = [alert for alert in alerts if alert["adjudication"] == "true_defect"]
    false_alerts = [alert for alert in alerts if alert["adjudication"] == "false_alarm"]
    unresolved_alerts = [alert for alert in alerts if alert["adjudication"] == "unresolved"]
    lines_scanned = _unique_lines_scanned(results)
    candidate_recall = _ratio(len(matched), len(evaluable))
    candidate_bounds = _recall_bounds(
        defects, lambda defect: defect["candidate_status"] == "matched"
    )
    confirmation_recall = _ratio(
        sum(defect["verdict"] == "confirmed" for defect in evaluable),
        len(evaluable),
    )
    confirmation_bounds = _recall_bounds(
        defects, lambda defect: defect["verdict"] == "confirmed"
    )
    unevaluable_by_status = {
        status: sum(
            not defect["evaluable"] and analysis_status == status
            for analysis_status, defect in observations
        )
        for status in sorted(ANALYSIS_STATUSES)
    }
    return {
        "known_positives": {
            "total": len(defects),
            "evaluable": len(evaluable),
            "unevaluable": len(defects) - len(evaluable),
        },
        # Compatibility names retain their original meaning.  Explicit names
        # make it impossible for report consumers to mistake the evaluable-only
        # rate for the lower bound over every known positive.
        "candidate_recall": candidate_recall,
        "candidate_recall_evaluable": candidate_recall,
        "candidate_recall_lower_bound": candidate_bounds["lower"],
        "candidate_recall_bounds": candidate_bounds,
        "evidence_recall": _ratio(
            sum(defect["evidence_status"] == "complete" for defect in matched),
            len(matched),
        ),
        "confirmation_recall": confirmation_recall,
        "confirmation_recall_evaluable": confirmation_recall,
        "confirmation_recall_lower_bound": confirmation_bounds["lower"],
        "confirmation_recall_bounds": confirmation_bounds,
        "known_positive_outcomes": {
            "candidate_hits": len(matched),
            "evaluable_candidate_misses": sum(
                defect["candidate_status"] != "matched" for defect in evaluable
            ),
            "confirmed": sum(
                defect["verdict"] == "confirmed" for defect in evaluable
            ),
            "evaluable_unconfirmed": sum(
                defect["verdict"] != "confirmed" for defect in evaluable
            ),
            "unevaluable_by_analysis_status": unevaluable_by_status,
        },
        "release_precision": _ratio(len(true_alerts), len(true_alerts) + len(false_alerts)),
        "release_precision_conservative_lower_bound": _ratio(
            len(true_alerts), len(true_alerts) + len(false_alerts) + len(unresolved_alerts)
        ),
        "adjudication": {
            "true_defect": len(true_alerts),
            "false_alarm": len(false_alerts),
            "unresolved": len(unresolved_alerts),
            "out_of_scope": sum(
                alert["adjudication"] == "out_of_scope" for alert in alerts
            ),
            "published_before_dedup": sum(len(result["alerts"]) for result in results),
            "published_after_dedup": len(alerts),
        },
        "false_alarm_burden_per_kloc": {
            "false_alerts": len(false_alerts),
            "lines_scanned": lines_scanned,
            "value": len(false_alerts) / (lines_scanned / 1000) if lines_scanned else None,
            "status": "available" if lines_scanned else "unavailable",
        },
        "candidate_outcomes": {
            "denominator": len(candidates),
            **{
                status: sum(candidate["status"] == status for candidate in candidates)
                for status in sorted(CANDIDATE_STATUSES)
            },
        },
        "exclusion_safety": {
            "known_positives_silently_excluded": sum(
                defect["excluded"] for defect in defects
            )
        },
    }


def _bootstrap_recall_interval(
    manifest: dict[str, Any], results: list[dict[str, Any]], *, samples: int = 2000
) -> dict[str, Any]:
    sample_project = {
        sample["sample_id"]: sample["project_id"] for sample in manifest["samples"]
    }
    by_project: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for result in results:
        by_project[sample_project[result["sample_id"]]].append(result)
    projects = sorted(by_project)
    if len(projects) < 2:
        return {
            "method": "project_bootstrap_95pct",
            "status": "insufficient_evidence",
            "project_count": len(projects),
            "candidate_recall": None,
            "confirmation_recall": None,
        }
    rng = random.Random(8)
    distributions: dict[str, list[float]] = {
        "candidate_recall": [], "confirmation_recall": []
    }
    for _ in range(samples):
        selected = [rng.choice(projects) for _ in projects]
        sampled_results = [
            result for project in selected for result in by_project[project]
        ]
        metrics = _core_metrics(sampled_results)
        for name in distributions:
            value = metrics[name]["value"]
            if value is not None:
                distributions[name].append(value)

    def interval(values: list[float]) -> list[float] | None:
        if not values:
            return None
        values.sort()
        return [
            values[int(0.025 * (len(values) - 1))],
            values[int(0.975 * (len(values) - 1))],
        ]

    return {
        "method": "project_bootstrap_95pct",
        "status": "available",
        "project_count": len(projects),
        **{name: interval(values) for name, values in distributions.items()},
    }


def _scope_summary(
    manifest: dict[str, Any], results: list[dict[str, Any]], top_k: int
) -> dict[str, Any]:
    """Calculate every gate input for one explicit evaluation scope."""

    total_targets = sum(result["coverage"]["total_targets"] for result in results)
    analyzed_targets = sum(
        result["coverage"]["analyzed_targets"] for result in results
    )
    coverage_results = _unique_coverage_results(results)
    total_units = sum(
        result["coverage"]["total_translation_units"]
        for result in coverage_results
    )
    analyzed_units = sum(
        result["coverage"]["analyzed_translation_units"]
        for result in coverage_results
    )
    lines_scanned = sum(
        result["coverage"]["lines_scanned"] for result in coverage_results
    )
    total_duration = sum(
        result["resource"]["duration_seconds"] for result in coverage_results
    )
    total_queries = sum(
        result["resource"]["query_count"] for result in coverage_results
    )
    candidate_count = sum(len(result["candidates"]) for result in results)
    tokens = [result["resource"]["model_tokens"] for result in coverage_results]
    costs = [result["resource"]["cost_usd"] for result in coverage_results]
    missing_usage = []
    if any(value is None for value in tokens):
        missing_usage.append("model_tokens")
    if any(value is None for value in costs):
        missing_usage.append("cost_usd")

    top_alerts = [
        alert
        for result in results
        for alert in _deduplicated_alerts([result])
        if alert["rank"] <= top_k
    ]
    top_true = sum(alert["adjudication"] == "true_defect" for alert in top_alerts)
    top_false = sum(alert["adjudication"] == "false_alarm" for alert in top_alerts)
    sample_project = {
        sample["sample_id"]: sample["project_id"] for sample in manifest["samples"]
    }
    projects = {sample_project[result["sample_id"]] for result in results}
    metrics = _core_metrics(results)
    metrics.update({
        "top_k_precision": {
            "k_per_sample": top_k,
            **_ratio(top_true, top_true + top_false),
        },
        "analysis_failures": {
            status: sum(result["analysis_status"] == status for result in results)
            for status in sorted(ANALYSIS_STATUSES)
        },
        "efficiency": {
            "duration_seconds": total_duration,
            "query_count": total_queries,
            "model_tokens": (
                None if "model_tokens" in missing_usage else sum(tokens)
            ),
            "cost_usd": None if "cost_usd" in missing_usage else sum(costs),
            "duration_seconds_per_kloc": (
                total_duration / (lines_scanned / 1000) if lines_scanned else None
            ),
            "queries_per_candidate": (
                total_queries / candidate_count if candidate_count else None
            ),
            "unavailable": missing_usage,
        },
    })
    return {
        "project_count": len(projects),
        "coverage": {
            "targets": _ratio(analyzed_targets, total_targets),
            "translation_units": _ratio(analyzed_units, total_units),
            "lines_scanned": lines_scanned,
            "lines_changed": sum(
                result["coverage"]["lines_changed"] for result in results
            ),
            "omissions": [
                {
                    "sample_id": result["sample_id"],
                    **(
                        {"coverage_scope_id": result["coverage_scope_id"]}
                        if result.get("coverage_scope_id") is not None
                        else {}
                    ),
                    "reason": omission,
                }
                for result in coverage_results
                for omission in result["coverage"]["omissions"]
            ],
        },
        "metrics": metrics,
        "uncertainty": _bootstrap_recall_interval(manifest, results),
        "failure_samples": [
            {
                "sample_id": result["sample_id"],
                "analysis_status": result["analysis_status"],
                "missed_defect_ids": [
                    defect["defect_id"]
                    for defect in result["known_defects"]
                    if defect["evaluable"]
                    and defect["candidate_status"] != "matched"
                ],
                "unconfirmed_defect_ids": [
                    defect["defect_id"]
                    for defect in result["known_defects"]
                    if defect["evaluable"] and defect["verdict"] != "confirmed"
                ],
                "unevaluable_defect_ids": [
                    defect["defect_id"]
                    for defect in result["known_defects"]
                    if not defect["evaluable"]
                ],
            }
            for result in results
            if result["analysis_status"] != "complete"
            or any(
                defect["evaluable"]
                and (
                    defect["candidate_status"] != "matched"
                    or defect["verdict"] != "confirmed"
                )
                for defect in result["known_defects"]
            )
        ],
    }


def build_quality_report(
    manifest: dict[str, Any], run: dict[str, Any]
) -> dict[str, Any]:
    """Build the Q-02 counts, ratios, strata and uncertainty statement."""

    validate_evaluation_run(run, manifest)
    results = run["sample_results"]
    top_k = run["bindings"]["top_k"]
    overall_scope = _scope_summary(manifest, results, top_k)
    overall = overall_scope["metrics"]

    sample_manifest = {sample["sample_id"]: sample for sample in manifest["samples"]}
    projects: dict[str, list[dict[str, Any]]] = defaultdict(list)
    build_variants: dict[str, list[dict[str, Any]]] = defaultdict(list)
    partitions: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for result in results:
        sample = sample_manifest[result["sample_id"]]
        projects[sample["project_id"]].append(result)
        build_variants[sample["build"]["variant_id"]].append(result)
        partitions[sample["partition"]].append(result)

    difficulty_defects: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for result in results:
        defect_manifest = {
            defect["defect_id"]: defect
            for defect in sample_manifest[result["sample_id"]]["known_defects"]
        }
        for defect in result["known_defects"]:
            difficulty = defect_manifest[defect["defect_id"]]["difficulty"]
            difficulty_defects[difficulty].append(defect)

    report = {
        "schema_version": EVALUATION_SCHEMA_VERSION,
        "report_id": f"report:{run['run_id']}",
        "dataset_id": manifest["dataset_id"],
        "manifest_digest": digest(manifest),
        "run_id": run["run_id"],
        "bindings": run["bindings"],
        "project_count": overall_scope["project_count"],
        "coverage": overall_scope["coverage"],
        "metrics": overall,
        "uncertainty": overall_scope["uncertainty"],
        "failure_samples": overall_scope["failure_samples"],
        "partition_scopes": {
            partition: _scope_summary(
                manifest, partitions.get(partition, []), top_k
            )
            for partition in sorted(PARTITIONS)
        },
        "strata": {
            "rule": {run["bindings"]["rule"]["id"]: overall},
            "backend": {run["bindings"]["backend"]["id"]: overall},
            "language": {"C++": overall},
            "project": {name: _core_metrics(items) for name, items in projects.items()},
            "build_variant": {
                name: _core_metrics(items) for name, items in build_variants.items()
            },
            "partition": {
                name: _core_metrics(items) for name, items in partitions.items()
            },
            "difficulty": {
                name: {
                    "candidate_recall": _ratio(
                        sum(
                            defect["candidate_status"] == "matched"
                            for defect in defects
                            if defect["evaluable"]
                        ),
                        sum(defect["evaluable"] for defect in defects),
                    ),
                    "candidate_recall_bounds": _recall_bounds(
                        defects,
                        lambda defect: defect["candidate_status"] == "matched",
                    ),
                    "confirmation_recall": _ratio(
                        sum(
                            defect["verdict"] == "confirmed"
                            for defect in defects
                            if defect["evaluable"]
                        ),
                        sum(defect["evaluable"] for defect in defects),
                    ),
                    "confirmation_recall_bounds": _recall_bounds(
                        defects,
                        lambda defect: defect["verdict"] == "confirmed",
                    ),
                }
                for name, defects in difficulty_defects.items()
            },
        },
        "evidence_sufficiency": (
            "not_assessed" if len(projects) >= 2 else "insufficient_evidence"
        ),
    }
    report["report_digest"] = digest(report)
    return report


def validate_gate_policy(policy: dict[str, Any]) -> None:
    """Validate a Q-07 policy frozen before a scored run is inspected."""

    policy = _mapping(policy, "policy")
    _version(policy.get("schema_version"), "policy.schema_version")
    _text(policy.get("policy_id"), "policy.policy_id")
    _timestamp(policy.get("registered_at"), "policy.registered_at")
    if policy.get("purpose") not in {"contract_test", "release"}:
        _fail("policy.purpose", "must be contract_test or release")
    partition = _text(policy.get("partition"), "policy.partition")
    if partition not in PARTITIONS | {"all"}:
        _fail("policy.partition", f"must be one of {sorted(PARTITIONS | {'all'})}")
    if policy["purpose"] == "release" and partition not in {"holdout", "shadow"}:
        _fail("policy.partition", "release policies must use holdout or shadow")
    _text(policy.get("dataset_id"), "policy.dataset_id")
    _hex(policy.get("manifest_digest"), "policy.manifest_digest", (64,))
    bindings = _mapping(policy.get("bindings"), "policy.bindings")
    for binding_name in ("rule", "backend"):
        binding = _mapping(bindings.get(binding_name),
                           f"policy.bindings.{binding_name}")
        _text(binding.get("id"), f"policy.bindings.{binding_name}.id")
        _text(binding.get("version"), f"policy.bindings.{binding_name}.version")

    sufficiency = _mapping(policy.get("sufficiency"), "policy.sufficiency")
    for name in (
        "min_projects", "min_evaluable_known_positives",
        "min_adjudicated_confirmed_alerts",
    ):
        _integer(sufficiency.get(name), f"policy.sufficiency.{name}", minimum=1)

    allowed_minimums = {
        "target_coverage", "translation_unit_coverage", "candidate_recall",
        "evidence_recall", "confirmation_recall", "release_precision",
        "release_precision_conservative_lower_bound", "top_k_precision",
    }
    minimums = _mapping(policy.get("minimums"), "policy.minimums")
    if not minimums:
        _fail("policy.minimums", "must contain at least one release metric")
    for name, threshold in minimums.items():
        if name not in allowed_minimums:
            _fail(f"policy.minimums.{name}", "is not a supported metric")
        threshold = _number(threshold, f"policy.minimums.{name}")
        if threshold > 1:
            _fail(f"policy.minimums.{name}", "must be between 0 and 1")

    allowed_maximums = {
        "false_alarm_burden_per_kloc", "duration_seconds", "query_count",
        "model_tokens", "cost_usd",
    }
    maximums = _mapping(policy.get("maximums"), "policy.maximums")
    for name, threshold in maximums.items():
        if name not in allowed_maximums:
            _fail(f"policy.maximums.{name}", "is not a supported metric")
        _number(threshold, f"policy.maximums.{name}")

    hard_limits = _mapping(policy.get("hard_limits"), "policy.hard_limits")
    required_hard_limits = {
        "known_positives_silently_excluded", "build_failures", "timeouts"
    }
    if set(hard_limits) != required_hard_limits:
        _fail("policy.hard_limits",
              f"must contain exactly {sorted(required_hard_limits)}")
    for name, limit in hard_limits.items():
        _integer(limit, f"policy.hard_limits.{name}")


def gate_policy_digest(policy: dict[str, Any]) -> str:
    validate_gate_policy(policy)
    return digest(policy)


def _report_metric(scope: dict[str, Any], name: str) -> float | int | None:
    metric_paths: dict[str, tuple[str, ...]] = {
        "target_coverage": ("coverage", "targets", "value"),
        "translation_unit_coverage": ("coverage", "translation_units", "value"),
        "candidate_recall": ("metrics", "candidate_recall", "value"),
        "evidence_recall": ("metrics", "evidence_recall", "value"),
        "confirmation_recall": ("metrics", "confirmation_recall", "value"),
        "release_precision": ("metrics", "release_precision", "value"),
        "release_precision_conservative_lower_bound": (
            "metrics", "release_precision_conservative_lower_bound", "value"
        ),
        "top_k_precision": ("metrics", "top_k_precision", "value"),
        "false_alarm_burden_per_kloc": (
            "metrics", "false_alarm_burden_per_kloc", "value"
        ),
        "duration_seconds": ("metrics", "efficiency", "duration_seconds"),
        "query_count": ("metrics", "efficiency", "query_count"),
        "model_tokens": ("metrics", "efficiency", "model_tokens"),
        "cost_usd": ("metrics", "efficiency", "cost_usd"),
    }
    current: Any = scope
    for component in metric_paths[name]:
        current = current[component]
    return current


def _validate_report_digest(report: dict[str, Any], path: str) -> str:
    report = _mapping(report, path)
    _version(report.get("schema_version"), f"{path}.schema_version")
    supplied = _hex(report.get("report_digest"), f"{path}.report_digest", (64,))
    report_without_digest = {
        key: value for key, value in report.items() if key != "report_digest"
    }
    if digest(report_without_digest) != supplied:
        _fail(f"{path}.report_digest", "does not bind the report contents")
    return supplied


def _report_scope(report: dict[str, Any], partition: str, path: str) -> dict[str, Any]:
    scope = report if partition == "all" else report.get("partition_scopes", {}).get(
        partition
    )
    if not isinstance(scope, dict):
        _fail(f"{path}.partition_scopes", f"does not contain partition {partition}")
    return scope


COMPARISON_METRICS = {
    "target_coverage": "higher",
    "translation_unit_coverage": "higher",
    "candidate_recall": "higher",
    "evidence_recall": "higher",
    "confirmation_recall": "higher",
    "release_precision": "higher",
    "release_precision_conservative_lower_bound": "higher",
    "top_k_precision": "higher",
    "false_alarm_burden_per_kloc": "lower",
    "duration_seconds": "lower",
    "query_count": "lower",
    "model_tokens": "lower",
    "cost_usd": "lower",
}


def compare_quality_reports(
    reports: dict[str, dict[str, Any]], *, partition: str,
    ablations: dict[str, dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Compare Q-02 reports under one manifest, partition and exact budget."""

    if set(reports) != {"baseline", "agent", "previous_release"}:
        _fail(
            "reports",
            "must contain exactly baseline, agent and previous_release",
        )
    if partition not in PARTITIONS | {"all"}:
        _fail("partition", f"must be one of {sorted(PARTITIONS | {'all'})}")
    ablations = ablations or {}
    all_reports = {**reports, **{f"ablation:{name}": value for name, value in ablations.items()}}
    if any(not isinstance(name, str) or not name.strip() for name in ablations):
        _fail("ablations", "component names must be nonempty strings")

    reference = reports["agent"]
    reference_dataset = reference.get("dataset_id")
    reference_manifest = reference.get("manifest_digest")
    reference_budget = reference.get("bindings", {}).get("budget")
    reference_top_k = reference.get("bindings", {}).get("top_k")
    scopes: dict[str, dict[str, Any]] = {}
    digests: dict[str, str] = {}
    for name, report in all_reports.items():
        path = f"reports.{name}"
        digests[name] = _validate_report_digest(report, path)
        if report.get("dataset_id") != reference_dataset:
            _fail(f"{path}.dataset_id", "does not match the agent report")
        if report.get("manifest_digest") != reference_manifest:
            _fail(f"{path}.manifest_digest", "does not match the agent report")
        bindings = _mapping(report.get("bindings"), f"{path}.bindings")
        if bindings.get("budget") != reference_budget:
            _fail(f"{path}.bindings.budget", "does not match the agent report")
        if bindings.get("top_k") != reference_top_k:
            _fail(f"{path}.bindings.top_k", "does not match the agent report")
        scopes[name] = _report_scope(report, partition, path)

    role_report_ids = [reports[name]["report_id"] for name in sorted(reports)]
    if len(set(role_report_ids)) != len(role_report_ids):
        _fail("reports", "baseline, agent and previous_release must be distinct runs")
    for component, report in ablations.items():
        if report["report_id"] == reports["agent"]["report_id"]:
            _fail(
                f"ablations.{component}",
                "must be a distinct run with the component removed",
            )

    def snapshot(name: str, report: dict[str, Any]) -> dict[str, Any]:
        scope = scopes[name]
        return {
            "report_id": report["report_id"],
            "report_digest": digests[name],
            "rule": report["bindings"]["rule"],
            "backend": report["bindings"]["backend"],
            "metrics": {
                metric: _report_metric(scope, metric)
                for metric in COMPARISON_METRICS
            },
            "analysis_failures": scope["metrics"]["analysis_failures"],
            "failure_samples": scope.get("failure_samples", []),
        }

    role_snapshots = {
        name: snapshot(name, report) for name, report in reports.items()
    }

    def deltas(left_name: str, right_name: str) -> dict[str, Any]:
        left = scopes[left_name]
        right = scopes[right_name]
        values = {}
        for metric, direction in COMPARISON_METRICS.items():
            left_value = _report_metric(left, metric)
            right_value = _report_metric(right, metric)
            if left_value is None or right_value is None:
                delta = None
                status = "unavailable"
            else:
                delta = left_value - right_value
                if delta == 0:
                    status = "equal"
                elif (delta > 0) == (direction == "higher"):
                    status = "improved"
                else:
                    status = "regressed"
            values[metric] = {
                "agent": left_value,
                "comparison": right_value,
                "delta": delta,
                "preferred_direction": direction,
                "status": status,
            }
        return values

    result = {
        "schema_version": EVALUATION_SCHEMA_VERSION,
        "comparison_id": "comparison:" + digest({
            "partition": partition,
            "reports": digests,
        })[:16],
        "dataset_id": reference_dataset,
        "manifest_digest": reference_manifest,
        "partition": partition,
        "budget": reference_budget,
        "top_k": reference_top_k,
        "roles": role_snapshots,
        "agent_deltas": {
            "vs_baseline": deltas("agent", "baseline"),
            "vs_previous_release": deltas("agent", "previous_release"),
        },
        "ablations": {
            component: {
                "report": snapshot(f"ablation:{component}", report),
                "agent_vs_ablated": deltas("agent", f"ablation:{component}"),
            }
            for component, report in ablations.items()
        },
    }
    result["comparison_digest"] = digest(result)
    return result


def evaluate_release_gate(
    report: dict[str, Any], policy: dict[str, Any]
) -> dict[str, Any]:
    """Evaluate a report against a pre-registered Q-07 policy."""

    validate_gate_policy(policy)
    report = _mapping(report, "report")
    supplied_report_digest = _validate_report_digest(report, "report")
    if report.get("dataset_id") != policy["dataset_id"]:
        _fail("report.dataset_id", "does not match the gate policy")
    if report.get("manifest_digest") != policy["manifest_digest"]:
        _fail("report.manifest_digest", "does not match the gate policy")
    for binding_name in ("rule", "backend"):
        if report.get("bindings", {}).get(binding_name) != policy["bindings"][binding_name]:
            _fail(f"report.bindings.{binding_name}", "does not match the gate policy")

    partition = policy["partition"]
    scope = _report_scope(report, partition, "report")

    sufficiency = policy["sufficiency"]
    adjudication = scope["metrics"]["adjudication"]
    actual_sufficiency = {
        "projects": scope["project_count"],
        "evaluable_known_positives": scope["metrics"]["known_positives"]["evaluable"],
        "adjudicated_confirmed_alerts": (
            adjudication["true_defect"] + adjudication["false_alarm"]
        ),
    }
    sufficiency_checks = [
        {
            "name": "projects",
            "actual": actual_sufficiency["projects"],
            "required": sufficiency["min_projects"],
            "status": (
                "passed" if actual_sufficiency["projects"] >= sufficiency["min_projects"]
                else "insufficient"
            ),
        },
        {
            "name": "evaluable_known_positives",
            "actual": actual_sufficiency["evaluable_known_positives"],
            "required": sufficiency["min_evaluable_known_positives"],
            "status": (
                "passed"
                if actual_sufficiency["evaluable_known_positives"]
                >= sufficiency["min_evaluable_known_positives"]
                else "insufficient"
            ),
        },
        {
            "name": "adjudicated_confirmed_alerts",
            "actual": actual_sufficiency["adjudicated_confirmed_alerts"],
            "required": sufficiency["min_adjudicated_confirmed_alerts"],
            "status": (
                "passed"
                if actual_sufficiency["adjudicated_confirmed_alerts"]
                >= sufficiency["min_adjudicated_confirmed_alerts"]
                else "insufficient"
            ),
        },
    ]

    metric_checks: list[dict[str, Any]] = []
    for name, threshold in policy["minimums"].items():
        actual = _report_metric(scope, name)
        status = "unavailable" if actual is None else (
            "passed" if actual >= threshold else "failed"
        )
        metric_checks.append({
            "name": name, "operator": ">=", "threshold": threshold,
            "actual": actual, "status": status,
        })
    for name, threshold in policy["maximums"].items():
        actual = _report_metric(scope, name)
        status = "unavailable" if actual is None else (
            "passed" if actual <= threshold else "failed"
        )
        metric_checks.append({
            "name": name, "operator": "<=", "threshold": threshold,
            "actual": actual, "status": status,
        })

    failures = scope["metrics"]["analysis_failures"]
    hard_actuals = {
        "known_positives_silently_excluded": scope["metrics"]
        ["exclusion_safety"]["known_positives_silently_excluded"],
        "build_failures": failures["build_failure"],
        "timeouts": failures["timeout"],
    }
    hard_checks = [
        {
            "name": name,
            "operator": "<=",
            "limit": limit,
            "actual": hard_actuals[name],
            "status": "passed" if hard_actuals[name] <= limit else "failed",
        }
        for name, limit in policy["hard_limits"].items()
    ]

    failed = any(item["status"] == "failed" for item in metric_checks + hard_checks)
    insufficient = (
        any(item["status"] == "insufficient" for item in sufficiency_checks)
        or any(item["status"] == "unavailable" for item in metric_checks)
    )
    decision = "failed" if failed else (
        "insufficient_evidence" if insufficient else "passed"
    )
    result = {
        "schema_version": EVALUATION_SCHEMA_VERSION,
        "decision": decision,
        "policy_id": policy["policy_id"],
        "policy_digest": digest(policy),
        "report_id": report["report_id"],
        "report_digest": supplied_report_digest,
        "purpose": policy["purpose"],
        "partition": partition,
        "sufficiency_checks": sufficiency_checks,
        "metric_checks": metric_checks,
        "hard_limit_checks": hard_checks,
    }
    result["decision_digest"] = digest(result)
    return result


def _format_ratio(metric: dict[str, Any]) -> str:
    value = metric.get("value")
    if value is None:
        return f"unavailable ({metric['numerator']}/{metric['denominator']})"
    return f"{value:.1%} ({metric['numerator']}/{metric['denominator']})"


def render_quality_markdown(report: dict[str, Any]) -> str:
    metrics = report["metrics"]
    coverage = report["coverage"]
    false_burden = metrics["false_alarm_burden_per_kloc"]["value"]
    false_burden_text = "unavailable" if false_burden is None else f"{false_burden:.3f}"
    lines = [
        f"# SPEC 008 quality report: {report['run_id']}",
        "",
        f"- Dataset: `{report['dataset_id']}`",
        f"- Manifest digest: `{report['manifest_digest']}`",
        f"- Rule: `{report['bindings']['rule']['id']}@{report['bindings']['rule']['version']}`",
        f"- Backend: `{report['bindings']['backend']['id']}@{report['bindings']['backend']['version']}`",
        f"- Executor: `{report['bindings'].get('executor', 'unspecified')}`",
        f"- Model route: `{report['bindings']['model_route']}`",
        f"- Evidence sufficiency: `{report['evidence_sufficiency']}`",
        "",
        "## Required metrics",
        "",
        "| Metric | Result |",
        "|---|---:|",
        f"| Target coverage | {_format_ratio(coverage['targets'])} |",
        f"| Translation-unit coverage | {_format_ratio(coverage['translation_units'])} |",
        f"| Candidate recall (evaluable only) | {_format_ratio(metrics['candidate_recall'])} |",
        f"| Candidate recall lower bound (all known positives) | "
        f"{_format_ratio(metrics['candidate_recall_bounds']['lower'])} |",
        f"| Candidate recall upper bound | "
        f"{_format_ratio(metrics['candidate_recall_bounds']['upper'])} |",
        f"| Evidence recall | {_format_ratio(metrics['evidence_recall'])} |",
        f"| Confirmation recall (evaluable only) | "
        f"{_format_ratio(metrics['confirmation_recall'])} |",
        f"| Confirmation recall lower bound (all known positives) | "
        f"{_format_ratio(metrics['confirmation_recall_bounds']['lower'])} |",
        f"| Confirmation recall upper bound | "
        f"{_format_ratio(metrics['confirmation_recall_bounds']['upper'])} |",
        f"| Release precision | {_format_ratio(metrics['release_precision'])} |",
        f"| Conservative precision | {_format_ratio(metrics['release_precision_conservative_lower_bound'])} |",
        f"| Top-{metrics['top_k_precision']['k_per_sample']} precision | {_format_ratio(metrics['top_k_precision'])} |",
        "",
        "## Counts and limitations",
        "",
        f"- Known positives: {metrics['known_positives']['total']} total, "
        f"{metrics['known_positives']['evaluable']} evaluable, "
        f"{metrics['known_positives']['unevaluable']} unevaluable.",
        "- Recall bounds treat unevaluable defects as misses for the lower bound "
        "and as hits for the upper bound; the primary recall uses evaluable defects only.",
        f"- True candidate misses on evaluable samples: "
        f"{metrics['known_positive_outcomes']['evaluable_candidate_misses']}; "
        f"unevaluable execution/build/timeout/not-run samples: "
        f"{metrics['known_positives']['unevaluable']}.",
        f"- Published alerts: {metrics['adjudication']['published_before_dedup']} before "
        f"deduplication, {metrics['adjudication']['published_after_dedup']} after.",
        f"- Discovered candidates: {metrics['candidate_outcomes']['denominator']}; "
        f"inconclusive: {metrics['candidate_outcomes']['inconclusive']}.",
        f"- Candidate execution failures: {metrics['candidate_outcomes'].get('execution_failure', 0)}; "
        f"timeouts: {metrics['candidate_outcomes']['timeout']}; "
        f"not run: {metrics['candidate_outcomes']['not_run']}.",
        f"- False-alert burden: {false_burden_text} per KLOC.",
        f"- Coverage omissions: {len(coverage['omissions'])}.",
        f"- Unavailable usage fields: {', '.join(metrics['efficiency']['unavailable']) or 'none'}.",
        f"- Uncertainty: `{report['uncertainty']['status']}` "
        f"({report['uncertainty']['project_count']} project(s)).",
    ]
    if coverage['omissions']:
        lines.extend(["", "## Coverage limitations", ""])
        lines.extend(f"- {item['sample_id']}: {item['reason']}" for item in coverage['omissions'])
    if report['bindings'].get('budget_enforcement'):
        lines.extend(["", "## Budget enforcement", ""])
        lines.extend(f"- {key}: `{value}`" for key, value in report['bindings']['budget_enforcement'].items())
    lines.extend(["", f"Report digest: `{report['report_digest']}`", ""])
    return "\n".join(lines)


ArtifactFetcher = Callable[[str, int], tuple[bytes, str]]


def _fetch_https(url: str, max_bytes: int) -> tuple[bytes, str]:
    request = urllib.request.Request(
        url, headers={"User-Agent": "defect-agent-runtime-spec008/1"}
    )
    with urllib.request.urlopen(request, timeout=60) as response:
        final_url = response.geturl()
        _public_https(final_url, "artifact.redirect_url")
        declared = response.headers.get("Content-Length")
        if declared is not None and int(declared) > max_bytes:
            raise InvalidInput(f"artifact exceeds {max_bytes} bytes: {url}")
        body = response.read(max_bytes + 1)
    if len(body) > max_bytes:
        raise InvalidInput(f"artifact exceeds {max_bytes} bytes: {url}")
    return body, final_url


def fetch_verified_artifact(
    artifact: dict[str, Any], *, fetcher: ArtifactFetcher | None = None,
    max_bytes: int = 64 * 1024 * 1024,
) -> tuple[bytes, dict[str, Any]]:
    """Fetch one public artifact and return bytes only after digest validation."""

    artifact = _mapping(artifact, "artifact")
    url = _public_https(artifact.get("url"), "artifact.url")
    expected_digest = _hex(artifact.get("sha256"), "artifact.sha256", (64,))
    expected_size = _integer(
        artifact.get("size_bytes"), "artifact.size_bytes", minimum=1
    )
    fetch = fetcher or _fetch_https
    body, final_url = fetch(url, max_bytes)
    _public_https(final_url, "artifact.redirect_url")
    actual_digest = bytes_digest(body)
    if actual_digest != expected_digest:
        raise InvalidInput(
            f"artifact digest mismatch for {url}: {actual_digest}"
        )
    if len(body) != expected_size:
        raise InvalidInput(
            f"artifact size mismatch for {url}: {len(body)}"
        )
    return body, {
        "url": url,
        "final_url": final_url,
        "sha256": actual_digest,
        "size_bytes": len(body),
        "status": "verified",
    }


def verify_manifest_artifacts(
    manifest: dict[str, Any], *, fetcher: ArtifactFetcher | None = None,
    max_bytes: int = 64 * 1024 * 1024,
) -> list[dict[str, Any]]:
    """Download public inputs without executing them and verify frozen digests."""

    validate_dataset_manifest(manifest)
    artifacts = [
        {
            "owner": "provenance",
            **manifest["provenance"]["source_artifact"],
        }
    ]
    artifacts.extend(
        {"owner": sample["sample_id"], **artifact}
        for sample in manifest["samples"] for artifact in sample["artifacts"]
    )
    results = []
    for artifact in artifacts:
        _, verified = fetch_verified_artifact(
            artifact, fetcher=fetcher, max_bytes=max_bytes
        )
        results.append({"owner": artifact["owner"], **verified})
    return results


__all__ = [
    "EVALUATION_SCHEMA_VERSION",
    "build_quality_report",
    "compare_quality_reports",
    "dataset_manifest_digest",
    "evaluate_release_gate",
    "fetch_verified_artifact",
    "gate_policy_digest",
    "load_json_object",
    "render_quality_markdown",
    "validate_dataset_manifest",
    "validate_evaluation_run",
    "validate_gate_policy",
    "verify_manifest_artifacts",
]
