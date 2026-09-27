"""Replay sealed validation records without exposing an execution surface."""

from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path
from types import MappingProxyType

from ..codec import bytes_digest
from ..domain import Coverage, FixedSnapshot, QueryRequest
from ..errors import InvalidInput, PolicyDenied, StaleSnapshot
from ..ports import ProgramResult, QueryOperation
from ..validation import (
    ValidationRecord,
    load_validation_record,
    validation_evidence_bundle_bytes,
    validation_record_bytes,
    verify_artifact_root,
)


class RecordedValidationProgramQuery:
    """Serve application-registered, immutable validation records by ID only."""

    backend_id = "recorded-validation"
    backend_version = "1"
    read_only = True
    idempotent_retry = True
    supported_operations = frozenset({"read_recorded_validation"})
    operation_specs = {
        "read_recorded_validation": QueryOperation(
            "read_recorded_validation", {"record_id": "string"}, ("record_id",), ("record_id",)
        ),
    }

    def __init__(
        self,
        snapshot: FixedSnapshot,
        registry: Mapping[str, ValidationRecord],
        artifact_root: str | Path | None = None,
    ) -> None:
        if not isinstance(registry, Mapping) or not registry:
            raise InvalidInput("recorded validation registry must be nonempty")
        verified: dict[str, ValidationRecord] = {}
        root_path = Path(artifact_root) if artifact_root is not None else None
        if root_path is not None and root_path.is_symlink():
            raise InvalidInput("validation artifact root must not be a symlink")
        root = root_path.resolve(strict=True) if root_path is not None else None
        if root is not None and not root.is_dir():
            raise InvalidInput("validation artifact root must be a directory")
        for key, record in registry.items():
            if not isinstance(key, str) or not isinstance(record, ValidationRecord):
                raise InvalidInput("recorded validation registry is malformed")
            # Round-trip through the exact parser, both to detach from the application
            # object and to verify the supplied canonical record identity.
            sealed = load_validation_record(validation_record_bytes(record))
            if key != sealed.record_id:
                raise InvalidInput("validation registry key must equal record ID")
            self._verify_snapshot_binding(snapshot, sealed)
            if sealed.schema_version == (1, 1):
                if root is None:
                    raise InvalidInput("schema 1.1 validation requires an artifact root")
                verify_artifact_root(sealed, root)
            verified[key] = sealed
        self.snapshot = snapshot
        self.registry = MappingProxyType(verified)
        self.artifact_root = root

    @staticmethod
    def _verify_snapshot_binding(snapshot: FixedSnapshot, record: ValidationRecord) -> None:
        if record.repository_id != snapshot.repository_id:
            raise StaleSnapshot("validation record belongs to another repository")
        if record.runtime_snapshot_digest != snapshot.snapshot_digest:
            raise StaleSnapshot("validation record belongs to another snapshot")
        if record.scope_id != snapshot.scope_id:
            raise StaleSnapshot("validation record belongs to another scope")
        if record.source.sha256 != snapshot.source_digest:
            raise StaleSnapshot("validation record source differs from snapshot")
        if record.tool_policy.sha256 != snapshot.tool_policy_digest:
            raise PolicyDenied("validation record tool policy differs from snapshot")

    def query(self, request: QueryRequest) -> ProgramResult:
        if request.snapshot_digest != self.snapshot.snapshot_digest:
            raise StaleSnapshot("recorded validation query belongs to another snapshot")
        if request.tool_policy_digest != self.snapshot.tool_policy_digest:
            raise PolicyDenied("recorded validation query policy differs from snapshot")
        if request.backend_id != self.backend_id or request.backend_version != self.backend_version:
            raise InvalidInput("recorded validation backend binding differs")
        if request.operation not in self.supported_operations:
            raise InvalidInput("unsupported recorded validation query")
        if request.scope.get("scope_id") != self.snapshot.scope_id:
            raise StaleSnapshot("recorded validation query scope differs from snapshot")
        selectors = request.scope.get("selectors", {})
        if set(selectors) != {"record_id"}:
            raise InvalidInput("recorded validation accepts only the record ID selector")
        self.operation_specs[request.operation].validate(request.args, selectors)
        record_id = request.args["record_id"]
        if selectors["record_id"] != record_id:
            raise PolicyDenied("validation record is outside the query scope")
        record = self.registry.get(record_id)
        if record is None:
            raise PolicyDenied("validation record is not application registered")
        self._verify_snapshot_binding(self.snapshot, record)
        # Revalidate on every use, protecting against an application retaining a
        # reference to a record object with an invalid externally supplied ID.
        record = load_validation_record(validation_record_bytes(record))
        if record.schema_version == (1, 1):
            if self.artifact_root is None:
                raise InvalidInput("schema 1.1 validation requires an artifact root")
            raw = validation_evidence_bundle_bytes(record, self.artifact_root)
        else:
            raw = validation_record_bytes(record)
        raw_digest = bytes_digest(raw)
        return self._result(record, raw, raw_digest)

    @staticmethod
    def _result(record: ValidationRecord, raw: bytes, raw_digest: str) -> ProgramResult:
        base_limitations = tuple(record.limitations)
        if record.schema_version == (1, 1):
            isolation = record.isolation
            build_term = record.build.termination
            execution_term = record.execution.termination
            if isolation is None or isolation.status != "passed":
                return ProgramResult(
                    "failed", Coverage("recorded validation attempt", "failed isolation", "unknown"), raw,
                    base_limitations + ("validation isolation did not pass",),
                )
            failure_terms = {"output_limit", "oom", "launch_failed", "cleanup_failed"}
            if build_term.kind in failure_terms or execution_term.kind in failure_terms:
                return ProgramResult(
                    "failed", Coverage("recorded validation attempt", "failed execution", "unknown"), raw,
                    base_limitations + ("recorded validation exceeded or failed an execution boundary",),
                )
            if build_term.kind == "timeout" or execution_term.kind == "timeout":
                return ProgramResult(
                    "timeout", Coverage("recorded validation attempt", "timed out execution", "unknown"), raw,
                    base_limitations + ("timeout cannot establish a definitive validation conclusion",),
                )
        if record.build.status == "failed" or record.execution.status == "executor_failed":
            return ProgramResult(
                "failed", Coverage("recorded validation attempt", "failed execution", "unknown"), raw,
                base_limitations + ("recorded validation did not complete successfully",),
            )
        if record.execution.status == "timeout":
            return ProgramResult(
                "timeout", Coverage("recorded validation attempt", "timed out execution", "unknown"), raw,
                base_limitations + ("timeout cannot establish a definitive validation conclusion",),
            )
        if record.execution.target_status == "target_observed":
            stage = record.execution.observation_stage
            return ProgramResult(
                "complete",
                Coverage(
                    f"recorded validation input {record.test_input.sha256}",
                    f"target observed at {stage}",
                    "complete",
                    (),
                    (raw_digest,),
                ),
                raw,
                base_limitations + (
                    "complete only for this recorded input, target observation, and fixed configuration",
                ),
            )
        omissions = tuple(record.omissions) + (
            "other validation inputs, paths, and configurations were not covered",
        )
        state = "no target trigger" if record.execution.target_status == "no_trigger" else "validation was not run"
        return ProgramResult(
            "partial",
            Coverage(
                f"recorded validation input {record.test_input.sha256}", state, "partial", omissions, (raw_digest,)
            ),
            raw,
            base_limitations + ("absence of a trigger is not a definitive negative conclusion",),
        )
