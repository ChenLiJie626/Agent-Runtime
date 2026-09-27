"""Contract tests for the registry-only Docker validation surface."""

from __future__ import annotations

import copy

import pytest

from agent_runtime.adapters.docker_validation import (
    DockerValidationExecutor,
    load_docker_validation_suite,
    _tree_digest,
)
from agent_runtime.errors import InvalidInput
from agent_runtime.validation_capture import ValidationArtifactStore


LIMITS = {
    "wall_time_ms": 1000,
    "stdout_bytes": 1024,
    "stderr_bytes": 1024,
    "total_output_bytes": 2048,
    "memory_bytes": 128 * 1024 * 1024,
    "pids": 16,
    "cpus_millis": 500,
    "tmp_bytes": 1024 * 1024,
    "work_bytes": 1024 * 1024,
}


def manifest(source, runner):
    return {
        "schema": "agent-runtime/docker-validation-suite/v1",
        "suite_id": "isolation-v1",
        "image_ref": "example.invalid/validation@sha256:" + "1" * 64,
        "image_id": "sha256:" + "2" * 64,
        "source_tree_digest": _tree_digest(source),
        "runner_tree_digest": _tree_digest(runner),
        "limits": LIMITS,
        "attempts": [
            {"attempt_id": "normal", "recipe_id": "isolation-normal"}
        ],
    }


def roots(tmp_path):
    source = tmp_path / "source"
    runner = tmp_path / "runner"
    source.mkdir()
    runner.mkdir()
    (source / "input.txt").write_text("input")
    (runner / "validation_container_runner.py").write_text("print('fixed')")
    return source, runner


def test_manifest_has_exact_registry_only_surface(tmp_path):
    source, runner = roots(tmp_path)
    suite = load_docker_validation_suite(
        manifest(source, runner), source_root=source, runner_root=runner
    )
    executor = DockerValidationExecutor({suite.suite_id: suite})
    assert tuple(executor.registry) == ("isolation-v1",)
    with pytest.raises(InvalidInput):
        executor.execute("isolation-v1", "unregistered", ValidationArtifactStore(tmp_path / "cas"))


@pytest.mark.parametrize("field", ["command", "argv", "env", "cwd", "mounts", "flags"])
def test_manifest_recursively_rejects_execution_fields(tmp_path, field):
    source, runner = roots(tmp_path)
    value = manifest(source, runner)
    value["attempts"][0][field] = ["forbidden"]
    with pytest.raises(InvalidInput):
        load_docker_validation_suite(value, source_root=source, runner_root=runner)


def test_manifest_rejects_unknown_recipe_and_changed_mount(tmp_path):
    source, runner = roots(tmp_path)
    value = manifest(source, runner)
    unknown = copy.deepcopy(value)
    unknown["attempts"][0]["recipe_id"] = "arbitrary-shell"
    with pytest.raises(InvalidInput):
        load_docker_validation_suite(unknown, source_root=source, runner_root=runner)
    (source / "input.txt").write_text("changed")
    with pytest.raises(Exception):
        load_docker_validation_suite(value, source_root=source, runner_root=runner)
