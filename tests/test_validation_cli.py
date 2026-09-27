"""Tests for the non-executing validation-record command-line interface."""

from __future__ import annotations

from pathlib import Path

import pytest

from agent_runtime.codec import bytes_digest, canonical_json, digest
from agent_runtime.domain import FixedSnapshot
from agent_runtime.validation import (
    ValidationArtifact,
    ValidationBuildOutcome,
    ValidationExecutionOutcome,
    ValidationRecord,
    ValidationResource,
    validation_record_to_dict,
)
from agent_runtime.validation_cli import main


def artifact(value: bytes) -> ValidationArtifact:
    return ValidationArtifact(bytes_digest(value), len(value))


def record() -> ValidationRecord:
    source, policy = artifact(b"source"), artifact(b"policy")
    snapshot = FixedSnapshot("repo", "scope", source.sha256, digest("profile"), policy.sha256)
    return ValidationRecord(
        repository_id="repo", runtime_snapshot_digest=snapshot.snapshot_digest, scope_id="scope",
        source=source, tool_policy=policy, toolchain=artifact(b"toolchain"), dependency=artifact(b"dependency"),
        recipe=artifact(b"recipe"), test_input=artifact(b"input"),
        build=ValidationBuildOutcome("succeeded", artifact(b"binary"), artifact(b"build stdout"), artifact(b"build stderr")),
        execution=ValidationExecutionOutcome("normal_exit", "target_observed", 0, stdout=artifact(b"run stdout"), stderr=artifact(b"run stderr"), observation_stage="asan", observation_digest=artifact(b"observation").sha256),
        resources=(ValidationResource("wall_time_ms", 1000, 10),),
    )


def write_record(path: Path, value: ValidationRecord) -> None:
    path.write_text(canonical_json(validation_record_to_dict(value)), encoding="utf-8")


def test_validate_and_inspect_are_read_only_record_operations(tmp_path, capsys):
    value = record()
    path = tmp_path / "record.json"
    write_record(path, value)
    assert main(["validate", str(path)]) == 0
    assert capsys.readouterr().out.strip() == value.record_id
    assert main(["inspect", str(path)]) == 0
    assert '"record_id"' in capsys.readouterr().out


def test_artifact_root_checks_size_and_digest(tmp_path):
    value = record()
    path = tmp_path / "record.json"
    write_record(path, value)
    root = tmp_path / "artifacts"
    for item, content in (
        (value.source, b"source"),
        (value.tool_policy, b"policy"),
        (value.toolchain, b"toolchain"),
        (value.dependency, b"dependency"),
        (value.recipe, b"recipe"),
        (value.test_input, b"input"),
        (value.build.binary, b"binary"),
        (value.build.stdout, b"build stdout"),
        (value.build.stderr, b"build stderr"),
        (value.execution.stdout, b"run stdout"),
        (value.execution.stderr, b"run stderr"),
    ):
        assert item is not None
        target = root / item.sha256[:2] / item.sha256
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(content)
    assert main(["validate", str(path), "--artifact-root", str(root)]) == 0
    (root / value.source.sha256[:2] / value.source.sha256).write_bytes(b"tampered")
    with pytest.raises(SystemExit) as error:
        main(["validate", str(path), "--artifact-root", str(root)])
    assert error.value.code == 2


def test_cli_has_no_execution_subcommand(capsys):
    with pytest.raises(SystemExit) as error:
        main(["run", "record.json"])
    assert error.value.code == 2
    assert "invalid choice" in capsys.readouterr().err
