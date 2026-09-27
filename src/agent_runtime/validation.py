"""Immutable, replay-only validation evidence records.

Validation records describe work that has already happened. They deliberately do
not contain commands, environments, paths, or any other execution surface.
"""

from __future__ import annotations

import base64
import hashlib
import json
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .codec import canonical_json
from .errors import EvidenceIntegrityError, InvalidInput

VALIDATION_RECORD_SCHEMA_VERSION = {"major": 1, "minor": 1}
VALIDATION_RECORD_SCHEMA_V1_0 = {"major": 1, "minor": 0}
_SUPPORTED_SCHEMA_VERSIONS = (
    VALIDATION_RECORD_SCHEMA_V1_0,
    VALIDATION_RECORD_SCHEMA_VERSION,
)
_MAX_INTEGER = 2**63 - 1
_FORBIDDEN_KEYS = frozenset(
    {"command", "commands", "argv", "args", "environment", "env", "cwd"}
)


def _require_sha256(value: object, field: str) -> str:
    if (
        not isinstance(value, str)
        or len(value) != 64
        or any(character not in "0123456789abcdef" for character in value)
    ):
        raise InvalidInput(f"{field} must be a lowercase SHA-256 digest")
    return value


def _require_text(value: object, field: str) -> str:
    if not isinstance(value, str) or not value:
        raise InvalidInput(f"{field} must be a nonempty string")
    return value


def _require_text_tuple(value: object, field: str) -> tuple[str, ...]:
    if not isinstance(value, (tuple, list)) or any(
        not isinstance(item, str) or not item for item in value
    ):
        raise InvalidInput(f"{field} must contain nonempty strings")
    return tuple(value)


def _bounded_integer(value: object, field: str, *, positive: bool) -> int:
    minimum = 1 if positive else 0
    if type(value) is not int or value < minimum or value > _MAX_INTEGER:
        qualifier = "positive" if positive else "nonnegative"
        raise InvalidInput(f"{field} must be a bounded {qualifier} integer")
    return value


@dataclass(frozen=True)
class ValidationArtifact:
    """A path-free reference to immutable validation input or output bytes."""

    sha256: str
    size_bytes: int

    def __post_init__(self) -> None:
        _require_sha256(self.sha256, "artifact sha256")
        _bounded_integer(self.size_bytes, "artifact size", positive=False)


@dataclass(frozen=True)
class ValidationTermination:
    """Closed process termination description used by schema 1.1 records."""

    kind: str
    exit_code: int | None = None
    signal: int | None = None
    oom_killed: bool = False
    output_limited: bool = False

    def __post_init__(self) -> None:
        kinds = {
            "not_run", "exit", "signal", "timeout", "output_limit", "oom",
            "launch_failed", "cleanup_failed",
        }
        if self.kind not in kinds:
            raise InvalidInput("invalid validation termination kind")
        if type(self.oom_killed) is not bool or type(self.output_limited) is not bool:
            raise InvalidInput("termination flags must be booleans")
        if self.exit_code is not None:
            _bounded_integer(self.exit_code, "termination exit code", positive=False)
        if self.signal is not None:
            _bounded_integer(self.signal, "termination signal", positive=True)
        if self.kind == "exit":
            valid = self.exit_code is not None and self.signal is None
        elif self.kind == "signal":
            valid = self.exit_code is None and self.signal is not None
        else:
            valid = self.exit_code is None and self.signal is None
        if not valid:
            raise InvalidInput("termination kind differs from exit code or signal")
        if self.kind == "oom" and not self.oom_killed:
            raise InvalidInput("OOM termination must carry the OOM observation")
        if self.kind == "output_limit" and not self.output_limited:
            raise InvalidInput("output-limit termination must carry the limit observation")
        if self.oom_killed and self.kind != "oom":
            raise InvalidInput("OOM observation requires OOM termination")
        if self.output_limited and self.kind != "output_limit":
            raise InvalidInput("output-limit observation requires output-limit termination")


@dataclass(frozen=True)
class ValidationIsolationOutcome:
    """Isolation preflight/probe result, backed only by immutable artifacts."""

    status: str
    runtime_report: ValidationArtifact
    probe_report: ValidationArtifact | None = None
    failure_phase: str | None = None
    failure_kind: str | None = None

    def __post_init__(self) -> None:
        if self.status not in {"passed", "failed"}:
            raise InvalidInput("isolation status must be passed or failed")
        if not isinstance(self.runtime_report, ValidationArtifact):
            raise InvalidInput("isolation runtime report is malformed")
        if self.probe_report is not None and not isinstance(
            self.probe_report, ValidationArtifact
        ):
            raise InvalidInput("isolation probe report is malformed")
        if self.status == "passed":
            if self.probe_report is None or self.failure_phase is not None or self.failure_kind is not None:
                raise InvalidInput("passed isolation requires a probe and no failure")
        else:
            _require_text(self.failure_phase, "isolation failure phase")
            _require_text(self.failure_kind, "isolation failure kind")


@dataclass(frozen=True)
class ValidationResource:
    """One resource limit and its observed consumption."""

    kind: str
    limit: int
    observed: int | None
    observation_status: str = "observed"

    def __post_init__(self) -> None:
        _require_text(self.kind, "resource kind")
        _bounded_integer(self.limit, "resource limit", positive=True)
        if self.observation_status not in {"observed", "unavailable"}:
            raise InvalidInput("resource observation status is invalid")
        if self.observation_status == "observed":
            _bounded_integer(self.observed, "resource observed usage", positive=False)
        elif self.observed is not None:
            raise InvalidInput("an unavailable resource cannot carry an observation")


@dataclass(frozen=True)
class ValidationBuildOutcome:
    """The build phase, its logs, and its produced binary, if any."""

    status: str
    binary: ValidationArtifact | None = None
    stdout: ValidationArtifact | None = None
    stderr: ValidationArtifact | None = None
    termination: ValidationTermination | None = None
    resource_observation: ValidationArtifact | None = None

    def __post_init__(self) -> None:
        if self.status not in {"succeeded", "failed"}:
            raise InvalidInput("build status must be succeeded or failed")
        if self.status == "succeeded" and self.binary is None:
            raise InvalidInput("a successful build requires a binary artifact")
        if self.status == "failed" and self.binary is not None:
            raise InvalidInput("a failed build cannot have a binary artifact")
        if any(
            item is not None and not isinstance(item, ValidationArtifact)
            for item in (self.binary, self.stdout, self.stderr, self.resource_observation)
        ):
            raise InvalidInput("build artifacts are malformed")
        if self.termination is not None and not isinstance(
            self.termination, ValidationTermination
        ):
            raise InvalidInput("build termination is malformed")
        if self.termination is not None:
            if self.status == "succeeded" and not (
                self.termination.kind == "exit" and self.termination.exit_code == 0
            ):
                raise InvalidInput("successful build requires a zero exit")
            if self.status == "failed" and (
                self.termination.kind == "exit" and self.termination.exit_code == 0
            ):
                raise InvalidInput("failed build cannot have a zero exit")


@dataclass(frozen=True)
class ValidationExecutionOutcome:
    """The execution and target-observation lifecycle for one recorded input."""

    status: str
    target_status: str
    exit_code: int | None = None
    signal: int | None = None
    stdout: ValidationArtifact | None = None
    stderr: ValidationArtifact | None = None
    observation_stage: str | None = None
    observation_digest: str | None = None
    termination: ValidationTermination | None = None
    resource_observation: ValidationArtifact | None = None

    def __post_init__(self) -> None:
        terminal = {"normal_exit", "nonzero_exit", "signal_exit"}
        if self.status not in {
            "not_run",
            "executor_failed",
            "timeout",
            *terminal,
        }:
            raise InvalidInput("invalid execution status")
        if self.target_status not in {"target_observed", "no_trigger", "not_run"}:
            raise InvalidInput("invalid target observation status")
        if self.exit_code is not None:
            _bounded_integer(self.exit_code, "execution exit code", positive=False)
        if self.signal is not None:
            _bounded_integer(self.signal, "execution signal", positive=True)
        if any(
            item is not None and not isinstance(item, ValidationArtifact)
            for item in (self.stdout, self.stderr, self.resource_observation)
        ):
            raise InvalidInput("execution output artifacts are malformed")
        if self.termination is not None and not isinstance(
            self.termination, ValidationTermination
        ):
            raise InvalidInput("execution termination is malformed")

        if self.status == "normal_exit":
            valid_termination = self.exit_code == 0 and self.signal is None
        elif self.status == "nonzero_exit":
            valid_termination = (
                self.exit_code is not None
                and self.exit_code > 0
                and self.signal is None
            )
        elif self.status == "signal_exit":
            valid_termination = self.exit_code is None and self.signal is not None
        else:
            valid_termination = self.exit_code is None and self.signal is None
        if not valid_termination:
            raise InvalidInput("execution status does not match exit code or signal")
        if self.termination is not None:
            expected = {
                "not_run": "not_run",
                "executor_failed": None,
                "timeout": "timeout",
                "normal_exit": "exit",
                "nonzero_exit": "exit",
                "signal_exit": "signal",
            }[self.status]
            if self.status == "executor_failed":
                allowed = {"output_limit", "oom", "launch_failed", "cleanup_failed"}
                if self.termination.kind not in allowed:
                    raise InvalidInput("executor failure has an invalid termination")
            elif self.termination.kind != expected:
                raise InvalidInput("execution termination differs from lifecycle status")
            if self.termination.exit_code != self.exit_code or self.termination.signal != self.signal:
                raise InvalidInput("execution termination code differs")

        if self.status == "not_run" and (
            self.target_status != "not_run"
            or self.stdout is not None
            or self.stderr is not None
        ):
            raise InvalidInput("unexecuted validation cannot have execution output")
        if self.status not in terminal and self.target_status != "not_run":
            raise InvalidInput("incomplete execution cannot classify a target observation")
        if self.target_status == "target_observed":
            _require_text(self.observation_stage, "observation stage")
            _require_sha256(self.observation_digest, "observation digest")
        elif self.observation_stage is not None or self.observation_digest is not None:
            raise InvalidInput("only an observed target may carry observation data")


@dataclass(frozen=True)
class ValidationRecord:
    """A sealed, canonical description of one validation attempt."""

    repository_id: str
    runtime_snapshot_digest: str
    scope_id: str
    source: ValidationArtifact
    tool_policy: ValidationArtifact
    toolchain: ValidationArtifact
    dependency: ValidationArtifact
    recipe: ValidationArtifact
    test_input: ValidationArtifact
    build: ValidationBuildOutcome
    execution: ValidationExecutionOutcome
    resources: tuple[ValidationResource, ...]
    omissions: tuple[str, ...] = ()
    limitations: tuple[str, ...] = ()
    record_id: str | None = None
    isolation: ValidationIsolationOutcome | None = None
    schema_version: tuple[int, int] = (1, 0)

    def __post_init__(self) -> None:
        _require_text(self.repository_id, "repository ID")
        _require_sha256(self.runtime_snapshot_digest, "runtime snapshot digest")
        _require_text(self.scope_id, "scope ID")
        artifact_fields = (
            self.source,
            self.tool_policy,
            self.toolchain,
            self.dependency,
            self.recipe,
            self.test_input,
        )
        if any(not isinstance(item, ValidationArtifact) for item in artifact_fields):
            raise InvalidInput("validation record artifacts are malformed")
        if not isinstance(self.build, ValidationBuildOutcome) or not isinstance(
            self.execution, ValidationExecutionOutcome
        ):
            raise InvalidInput("validation record outcomes are malformed")
        if not isinstance(self.resources, tuple) or not self.resources:
            raise InvalidInput("validation record needs one or more resources")
        if any(not isinstance(item, ValidationResource) for item in self.resources):
            raise InvalidInput("validation resources are malformed")
        if len({item.kind for item in self.resources}) != len(self.resources):
            raise InvalidInput("validation resource kinds must be unique")
        object.__setattr__(
            self, "omissions", _require_text_tuple(self.omissions, "omissions")
        )
        object.__setattr__(
            self, "limitations", _require_text_tuple(self.limitations, "limitations")
        )
        if self.schema_version not in {(1, 0), (1, 1)}:
            raise InvalidInput("unsupported validation record schema version")
        if self.schema_version == (1, 0):
            if self.isolation is not None:
                raise InvalidInput("schema 1.0 cannot carry isolation evidence")
            if any(
                value is not None
                for value in (
                    self.build.termination,
                    self.build.resource_observation,
                    self.execution.termination,
                    self.execution.resource_observation,
                )
            ) or any(resource.observation_status != "observed" for resource in self.resources):
                raise InvalidInput("schema 1.0 cannot carry schema 1.1 fields")
        else:
            if not isinstance(self.isolation, ValidationIsolationOutcome):
                raise InvalidInput("schema 1.1 requires isolation evidence")
            if self.build.termination is None or self.build.resource_observation is None:
                raise InvalidInput("schema 1.1 requires complete build lifecycle evidence")
            if self.execution.termination is None or self.execution.resource_observation is None:
                raise InvalidInput("schema 1.1 requires complete execution lifecycle evidence")
            if self.isolation.status != "passed" and self.execution.status not in {
                "not_run", "executor_failed"
            }:
                raise InvalidInput("failed isolation cannot have a completed execution")
        if self.build.status == "failed" and self.execution.status != "not_run":
            raise InvalidInput("a failed build requires execution not_run")
        if self.execution.status != "not_run" and self.build.status != "succeeded":
            raise InvalidInput("execution requires a successful build")
        computed = _record_id(self._payload())
        if self.record_id is not None and (
            _require_sha256(self.record_id, "record ID") != computed
        ):
            raise EvidenceIntegrityError(
                "validation record ID does not match canonical payload"
            )
        object.__setattr__(self, "record_id", computed)

    def _payload(self) -> dict[str, Any]:
        payload = {
            "schema_version": {"major": self.schema_version[0], "minor": self.schema_version[1]},
            "repository_id": self.repository_id,
            "runtime_snapshot_digest": self.runtime_snapshot_digest,
            "scope_id": self.scope_id,
            "source": _artifact_to_dict(self.source),
            "tool_policy": _artifact_to_dict(self.tool_policy),
            "toolchain": _artifact_to_dict(self.toolchain),
            "dependency": _artifact_to_dict(self.dependency),
            "recipe": _artifact_to_dict(self.recipe),
            "test_input": _artifact_to_dict(self.test_input),
            "build": _build_to_dict(self.build, rich=self.schema_version == (1, 1)),
            "execution": _execution_to_dict(
                self.execution, rich=self.schema_version == (1, 1)
            ),
            "resources": [
                _resource_to_dict(item, rich=self.schema_version == (1, 1))
                for item in self.resources
            ],
            "omissions": list(self.omissions),
            "limitations": list(self.limitations),
        }
        if self.schema_version == (1, 1):
            payload["isolation"] = _isolation_to_dict(self.isolation)
        return payload


def _record_id(payload: Mapping[str, Any]) -> str:
    return hashlib.sha256(canonical_json(payload).encode("utf-8")).hexdigest()


def _artifact_to_dict(value: ValidationArtifact) -> dict[str, Any]:
    return {"sha256": value.sha256, "size_bytes": value.size_bytes}


def _resource_to_dict(value: ValidationResource, *, rich: bool) -> dict[str, Any]:
    payload = {"kind": value.kind, "limit": value.limit, "observed": value.observed}
    if rich:
        payload["observation_status"] = value.observation_status
    return payload


def _termination_to_dict(value: ValidationTermination) -> dict[str, Any]:
    return {
        "kind": value.kind,
        "exit_code": value.exit_code,
        "signal": value.signal,
        "oom_killed": value.oom_killed,
        "output_limited": value.output_limited,
    }


def _isolation_to_dict(value: ValidationIsolationOutcome | None) -> dict[str, Any]:
    if value is None:
        raise InvalidInput("schema 1.1 isolation evidence is missing")
    return {
        "status": value.status,
        "runtime_report": _artifact_to_dict(value.runtime_report),
        "probe_report": _optional_artifact(value.probe_report),
        "failure_phase": value.failure_phase,
        "failure_kind": value.failure_kind,
    }


def _build_to_dict(value: ValidationBuildOutcome, *, rich: bool) -> dict[str, Any]:
    payload = {
        "status": value.status,
        "binary": _optional_artifact(value.binary),
        "stdout": _optional_artifact(value.stdout),
        "stderr": _optional_artifact(value.stderr),
    }
    if rich:
        payload.update({
            "termination": _termination_to_dict(value.termination),
            "resource_observation": _artifact_to_dict(value.resource_observation),
        })
    return payload


def _optional_artifact(value: ValidationArtifact | None) -> dict[str, Any] | None:
    return _artifact_to_dict(value) if value is not None else None


def _execution_to_dict(
    value: ValidationExecutionOutcome, *, rich: bool
) -> dict[str, Any]:
    payload = {
        "status": value.status,
        "target_status": value.target_status,
        "exit_code": value.exit_code,
        "signal": value.signal,
        "stdout": _optional_artifact(value.stdout),
        "stderr": _optional_artifact(value.stderr),
        "observation_stage": value.observation_stage,
        "observation_digest": value.observation_digest,
    }
    if rich:
        payload.update({
            "termination": _termination_to_dict(value.termination),
            "resource_observation": _artifact_to_dict(value.resource_observation),
        })
    return payload


def validation_record_to_dict(record: ValidationRecord) -> dict[str, Any]:
    """Return the sole canonical external representation of a record."""
    if not isinstance(record, ValidationRecord):
        raise InvalidInput("expected a ValidationRecord")
    return {**record._payload(), "record_id": record.record_id}


def validation_record_bytes(record: ValidationRecord) -> bytes:
    return canonical_json(validation_record_to_dict(record)).encode("utf-8")


def _mapping(value: object, name: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise InvalidInput(f"{name} must be an object")
    if _FORBIDDEN_KEYS & set(value):
        raise InvalidInput("validation records cannot include executable instructions")
    return value


def _exact(value: object, keys: set[str], name: str) -> Mapping[str, Any]:
    mapping = _mapping(value, name)
    if set(mapping) != keys:
        raise InvalidInput(f"{name} has unknown or missing fields")
    return mapping


def _parse_artifact(value: object, name: str) -> ValidationArtifact:
    item = _exact(value, {"sha256", "size_bytes"}, name)
    return ValidationArtifact(item["sha256"], item["size_bytes"])


def _parse_optional_artifact(value: object, name: str) -> ValidationArtifact | None:
    return None if value is None else _parse_artifact(value, name)


def _parse_resource(value: object, *, rich: bool) -> ValidationResource:
    keys = {"kind", "limit", "observed", "observation_status"} if rich else {
        "kind", "limit", "observed"
    }
    item = _exact(value, keys, "resource")
    return ValidationResource(
        item["kind"], item["limit"], item["observed"],
        item["observation_status"] if rich else "observed",
    )


def _parse_termination(value: object, name: str) -> ValidationTermination:
    item = _exact(
        value,
        {"kind", "exit_code", "signal", "oom_killed", "output_limited"},
        name,
    )
    return ValidationTermination(
        item["kind"], item["exit_code"], item["signal"],
        item["oom_killed"], item["output_limited"],
    )


def _parse_isolation(value: object) -> ValidationIsolationOutcome:
    item = _exact(
        value,
        {"status", "runtime_report", "probe_report", "failure_phase", "failure_kind"},
        "isolation",
    )
    return ValidationIsolationOutcome(
        item["status"],
        _parse_artifact(item["runtime_report"], "isolation runtime report"),
        _parse_optional_artifact(item["probe_report"], "isolation probe report"),
        item["failure_phase"],
        item["failure_kind"],
    )


def _parse_build(value: object, *, rich: bool) -> ValidationBuildOutcome:
    keys = {"status", "binary", "stdout", "stderr"}
    if rich:
        keys |= {"termination", "resource_observation"}
    item = _exact(value, keys, "build")
    return ValidationBuildOutcome(
        item["status"],
        _parse_optional_artifact(item["binary"], "build binary"),
        _parse_optional_artifact(item["stdout"], "build stdout"),
        _parse_optional_artifact(item["stderr"], "build stderr"),
        _parse_termination(item["termination"], "build termination") if rich else None,
        _parse_artifact(item["resource_observation"], "build resource observation") if rich else None,
    )


def _parse_execution(value: object, *, rich: bool) -> ValidationExecutionOutcome:
    keys = {
        "status", "target_status", "exit_code", "signal", "stdout", "stderr",
        "observation_stage", "observation_digest",
    }
    if rich:
        keys |= {"termination", "resource_observation"}
    item = _exact(value, keys, "execution")
    return ValidationExecutionOutcome(
        status=item["status"],
        target_status=item["target_status"],
        exit_code=item["exit_code"],
        signal=item["signal"],
        stdout=_parse_optional_artifact(item["stdout"], "execution stdout"),
        stderr=_parse_optional_artifact(item["stderr"], "execution stderr"),
        observation_stage=item["observation_stage"],
        observation_digest=item["observation_digest"],
        termination=_parse_termination(item["termination"], "execution termination") if rich else None,
        resource_observation=(
            _parse_artifact(item["resource_observation"], "execution resource observation")
            if rich else None
        ),
    )


def load_validation_record(
    value: str | bytes | bytearray | Mapping[str, Any] | Path,
) -> ValidationRecord:
    """Strictly parse a canonical record and verify its supplied record ID."""
    if isinstance(value, Path):
        try:
            value = value.read_bytes()
        except OSError as exc:
            raise InvalidInput(f"cannot read validation record: {exc}") from exc
    if isinstance(value, (bytes, bytearray)):
        try:
            value = value.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise InvalidInput("validation record must be UTF-8 JSON") from exc
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except json.JSONDecodeError as exc:
            raise InvalidInput("validation record is not valid JSON") from exc
    base_keys = {
        "schema_version", "record_id", "repository_id", "runtime_snapshot_digest",
        "scope_id", "source", "tool_policy", "toolchain", "dependency", "recipe",
        "test_input", "build", "execution", "resources", "omissions", "limitations",
    }
    mapping = _mapping(value, "validation record")
    version = _exact(mapping.get("schema_version"), {"major", "minor"}, "schema version")
    version_dict = dict(version)
    if version_dict not in _SUPPORTED_SCHEMA_VERSIONS:
        raise InvalidInput("unsupported validation record schema version")
    rich = version_dict == VALIDATION_RECORD_SCHEMA_VERSION
    item = _exact(mapping, base_keys | ({"isolation"} if rich else set()), "validation record")
    resources = item["resources"]
    if not isinstance(resources, list):
        raise InvalidInput("resources must be an array")
    return ValidationRecord(
        repository_id=item["repository_id"],
        runtime_snapshot_digest=item["runtime_snapshot_digest"],
        scope_id=item["scope_id"],
        source=_parse_artifact(item["source"], "source"),
        tool_policy=_parse_artifact(item["tool_policy"], "tool policy"),
        toolchain=_parse_artifact(item["toolchain"], "toolchain"),
        dependency=_parse_artifact(item["dependency"], "dependency"),
        recipe=_parse_artifact(item["recipe"], "recipe"),
        test_input=_parse_artifact(item["test_input"], "test input"),
        build=_parse_build(item["build"], rich=rich),
        execution=_parse_execution(item["execution"], rich=rich),
        resources=tuple(_parse_resource(resource, rich=rich) for resource in resources),
        omissions=_require_text_tuple(item["omissions"], "omissions"),
        limitations=_require_text_tuple(item["limitations"], "limitations"),
        record_id=item["record_id"],
        isolation=_parse_isolation(item["isolation"]) if rich else None,
        schema_version=(version_dict["major"], version_dict["minor"]),
    )


def record_artifacts(record: ValidationRecord) -> tuple[ValidationArtifact, ...]:
    """Return every artifact that may be checked in a read-only artifact root."""
    artifacts: list[ValidationArtifact | None] = [
        record.source,
        record.tool_policy,
        record.toolchain,
        record.dependency,
        record.recipe,
        record.test_input,
        record.build.binary,
        record.build.stdout,
        record.build.stderr,
        record.build.resource_observation,
        record.execution.stdout,
        record.execution.stderr,
        record.execution.resource_observation,
        record.isolation.runtime_report if record.isolation else None,
        record.isolation.probe_report if record.isolation else None,
    ]
    return tuple(item for item in artifacts if item is not None)


def validation_evidence_bundle_bytes(
    record: ValidationRecord, artifact_root: str | Path
) -> bytes:
    """Archive a record and all referenced bytes into one deterministic Runtime artifact."""
    verify_artifact_root(record, artifact_root)
    root = Path(artifact_root)
    unique = {artifact.sha256: artifact for artifact in record_artifacts(record)}
    artifacts = []
    for artifact_digest in sorted(unique):
        artifact = unique[artifact_digest]
        data = (root / artifact.sha256[:2] / artifact.sha256).read_bytes()
        artifacts.append({
            "sha256": artifact.sha256,
            "size_bytes": artifact.size_bytes,
            "content_base64": base64.b64encode(data).decode("ascii"),
        })
    return canonical_json({
        "bundle_schema": {"major": 1, "minor": 0},
        "record": validation_record_to_dict(record),
        "artifacts": artifacts,
    }).encode("utf-8")


def verify_artifact_root(record: ValidationRecord, artifact_root: str | Path) -> None:
    """Verify path-free references against ``<root>/<prefix>/<digest>``."""
    supplied_root = Path(artifact_root)
    if supplied_root.is_symlink():
        raise InvalidInput("validation artifact root must not be a symlink")
    try:
        root = supplied_root.resolve(strict=True)
    except OSError as exc:
        raise EvidenceIntegrityError("validation artifact root is missing") from exc
    if not root.is_dir():
        raise InvalidInput("validation artifact root must be a directory")
    for artifact in record_artifacts(record):
        prefix = root / artifact.sha256[:2]
        path = prefix / artifact.sha256
        if prefix.is_symlink() or path.is_symlink():
            raise EvidenceIntegrityError(
                f"validation artifact path must not be a symlink: {artifact.sha256}"
            )
        try:
            data = path.read_bytes()
        except OSError as exc:
            raise EvidenceIntegrityError(
                f"validation artifact is missing: {artifact.sha256}"
            ) from exc
        if (
            len(data) != artifact.size_bytes
            or hashlib.sha256(data).hexdigest() != artifact.sha256
        ):
            raise EvidenceIntegrityError(
                f"validation artifact integrity check failed: {artifact.sha256}"
            )
