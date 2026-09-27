"""Compatibility and integrity contracts for validation schema 1.1."""

from __future__ import annotations

import json
import subprocess
import sys

import pytest

from agent_runtime.adapters.validation import RecordedValidationProgramQuery
from agent_runtime.codec import digest
from agent_runtime.domain import FixedSnapshot, QueryRequest
from agent_runtime.errors import EvidenceIntegrityError, InvalidInput
from agent_runtime.validation import (
    ValidationBuildOutcome,
    ValidationExecutionOutcome,
    ValidationIsolationOutcome,
    ValidationRecord,
    ValidationResource,
    ValidationTermination,
    load_validation_record,
    record_artifacts,
    validation_evidence_bundle_bytes,
    validation_record_bytes,
    validation_record_to_dict,
)
from agent_runtime.validation_capture import (
    ValidationArtifactStore,
    collect_bounded_process,
)


def rich_record(tmp_path, *, execution=None, isolation=None):
    store = ValidationArtifactStore(tmp_path / "cas")
    refs = {
        name: store.put(name.encode())
        for name in (
            "source", "policy", "toolchain", "dependency", "recipe", "input",
            "binary", "build-out", "build-err", "build-resources", "run-out",
            "run-err", "run-resources", "runtime-report", "probe-report",
        )
    }
    snapshot = FixedSnapshot(
        "repo", "scope", refs["source"].sha256, digest("profile"), refs["policy"].sha256
    )
    execution = execution or ValidationExecutionOutcome(
        "normal_exit",
        "target_observed",
        0,
        stdout=refs["run-out"],
        stderr=refs["run-err"],
        observation_stage="sanitizer",
        observation_digest=digest("observation"),
        termination=ValidationTermination("exit", exit_code=0),
        resource_observation=refs["run-resources"],
    )
    isolation = isolation or ValidationIsolationOutcome(
        "passed", refs["runtime-report"], refs["probe-report"]
    )
    record = ValidationRecord(
        repository_id="repo",
        runtime_snapshot_digest=snapshot.snapshot_digest,
        scope_id="scope",
        source=refs["source"],
        tool_policy=refs["policy"],
        toolchain=refs["toolchain"],
        dependency=refs["dependency"],
        recipe=refs["recipe"],
        test_input=refs["input"],
        build=ValidationBuildOutcome(
            "succeeded",
            refs["binary"],
            refs["build-out"],
            refs["build-err"],
            ValidationTermination("exit", exit_code=0),
            refs["build-resources"],
        ),
        execution=execution,
        resources=(
            ValidationResource("wall_time_ms", 1000, 20),
            ValidationResource("cpu_time_ms", 1000, None, "unavailable"),
        ),
        omissions=("one fixed input",),
        limitations=("not a project safety claim",),
        isolation=isolation,
        schema_version=(1, 1),
    )
    return record, snapshot, store, refs


def query(snapshot, record_id):
    return QueryRequest(
        query_id="query",
        task_id="task",
        check_id="validation",
        operation="read_recorded_validation",
        snapshot_digest=snapshot.snapshot_digest,
        backend_id="recorded-validation",
        backend_version="1",
        tool_policy_digest=snapshot.tool_policy_digest,
        scope={"scope_id": snapshot.scope_id, "selectors": {"record_id": record_id}},
        args={"record_id": record_id},
        idempotency_key="key",
        timeout_ms=1000,
    )


def test_schema_v11_round_trip_and_bundle_contains_every_artifact(tmp_path):
    record, snapshot, store, _ = rich_record(tmp_path)
    assert load_validation_record(validation_record_bytes(record)) == record
    bundle = validation_evidence_bundle_bytes(record, store.root)
    decoded = json.loads(bundle)
    assert decoded["record"]["record_id"] == record.record_id
    assert {item["sha256"] for item in decoded["artifacts"]} == {
        item.sha256 for item in record_artifacts(record)
    }
    result = RecordedValidationProgramQuery(
        snapshot, {record.record_id: record}, store.root
    ).query(query(snapshot, record.record_id))
    assert result.status == "complete"
    assert result.raw == bundle


def test_schema_v11_rejects_missing_unknown_and_inconsistent_fields(tmp_path):
    record, _, _, refs = rich_record(tmp_path)
    encoded = validation_record_to_dict(record)
    missing = json.loads(json.dumps(encoded))
    missing["execution"].pop("termination")
    with pytest.raises(InvalidInput):
        load_validation_record(missing)
    unknown = json.loads(json.dumps(encoded))
    unknown["isolation"]["container"] = "forbidden"
    with pytest.raises(InvalidInput):
        load_validation_record(unknown)
    with pytest.raises(InvalidInput):
        ValidationTermination("exit", exit_code=1, oom_killed=True)
    with pytest.raises(InvalidInput):
        ValidationResource("memory_bytes", 1, 0, "unavailable")
    with pytest.raises(InvalidInput):
        ValidationIsolationOutcome("passed", refs["runtime-report"])


def test_schema_v11_artifact_missing_symlink_and_tamper_fail_closed(tmp_path):
    record, snapshot, store, refs = rich_record(tmp_path)
    with pytest.raises(InvalidInput):
        RecordedValidationProgramQuery(snapshot, {record.record_id: record})

    alias = tmp_path / "cas-alias"
    alias.symlink_to(store.root, target_is_directory=True)
    with pytest.raises(InvalidInput):
        RecordedValidationProgramQuery(snapshot, {record.record_id: record}, alias)

    target = store.path(refs["probe-report"].sha256)
    target.write_bytes(b"tampered")
    with pytest.raises(EvidenceIntegrityError):
        RecordedValidationProgramQuery(snapshot, {record.record_id: record}, store.root)


def test_schema_v11_conservative_lifecycle_mapping(tmp_path):
    record, snapshot, store, refs = rich_record(tmp_path)
    cases = (
        (
            ValidationExecutionOutcome(
                "timeout", "not_run", termination=ValidationTermination("timeout"),
                resource_observation=refs["run-resources"],
            ),
            "timeout",
        ),
        (
            ValidationExecutionOutcome(
                "executor_failed", "not_run",
                termination=ValidationTermination("oom", oom_killed=True),
                resource_observation=refs["run-resources"],
            ),
            "failed",
        ),
        (
            ValidationExecutionOutcome(
                "normal_exit", "no_trigger", 0,
                stdout=refs["run-out"], stderr=refs["run-err"],
                termination=ValidationTermination("exit", exit_code=0),
                resource_observation=refs["run-resources"],
            ),
            "partial",
        ),
    )
    for execution, expected in cases:
        changed = ValidationRecord(
            **{**record.__dict__, "record_id": None, "execution": execution}
        )
        result = RecordedValidationProgramQuery(
            snapshot, {changed.record_id: changed}, store.root
        ).query(query(snapshot, changed.record_id))
        assert result.status == expected
        assert result.status != "complete"


def test_failed_isolation_can_record_preflight_without_execution(tmp_path):
    record, _, _, refs = rich_record(tmp_path)
    failed = ValidationRecord(
        **{
            **record.__dict__,
            "record_id": None,
            "isolation": ValidationIsolationOutcome(
                "failed", refs["runtime-report"], failure_phase="probe",
                failure_kind="network_available",
            ),
            "build": ValidationBuildOutcome(
                "failed",
                stdout=refs["build-out"],
                stderr=refs["build-err"],
                termination=ValidationTermination("launch_failed"),
                resource_observation=refs["build-resources"],
            ),
            "execution": ValidationExecutionOutcome(
                "not_run", "not_run", termination=ValidationTermination("not_run"),
                resource_observation=refs["run-resources"],
            ),
        }
    )
    assert failed.isolation.status == "failed"


def test_bounded_collector_preserves_streams_and_classifies_boundaries():
    process = subprocess.Popen(
        [sys.executable, "-c", "import sys; print('out'); print('err', file=sys.stderr)"],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        start_new_session=True,
    )
    captured = collect_bounded_process(
        process, timeout_ms=1000, stdout_limit=100, stderr_limit=100,
        total_limit=200, kill_process_group=True,
    )
    assert captured.termination == ValidationTermination("exit", exit_code=0)
    assert captured.stdout == b"out\n"
    assert captured.stderr == b"err\n"

    flood = subprocess.Popen(
        [sys.executable, "-c", "import sys; sys.stdout.write('x' * 10000)"],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        start_new_session=True,
    )
    limited = collect_bounded_process(
        flood, timeout_ms=1000, stdout_limit=20, stderr_limit=20,
        total_limit=40, kill_process_group=True,
    )
    assert limited.termination.kind == "output_limit"
    assert len(limited.stdout) == 20
