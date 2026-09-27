#!/usr/bin/env python3
"""Run the mandatory real Docker containment and boundary acceptance suite."""
from __future__ import annotations

import argparse
import json
import shutil
from pathlib import Path

from agent_runtime.adapters.docker_validation import (
    DockerValidationExecutor,
    _tree_digest,
    load_docker_validation_suite,
)
from agent_runtime.codec import canonical_json
from agent_runtime.errors import InvalidInput
from agent_runtime.validation_capture import ValidationArtifactStore

IMAGE_REF = "agent-runtime/clang-ctu@sha256:4f3a6f74ec2dcabbe51d95744a0d6afef145bb09fda347bd5a866fd3dd85eff8"
IMAGE_ID = "sha256:4f3a6f74ec2dcabbe51d95744a0d6afef145bb09fda347bd5a866fd3dd85eff8"
EXPECTED = {
    "normal": ("exit", 0, None),
    "nonzero": ("exit", 23, None),
    "signal": ("signal", None, 9),
    "timeout": ("timeout", None, None),
    "output": ("output_limit", None, None),
    "space": ("exit", 0, None),
}


def manifest(source: Path, runner: Path) -> dict:
    return {
        "schema": "agent-runtime/docker-validation-suite/v1",
        "suite_id": "isolation-v1",
        "image_ref": IMAGE_REF,
        "image_id": IMAGE_ID,
        "source_tree_digest": _tree_digest(source),
        "runner_tree_digest": _tree_digest(runner),
        "limits": {
            "wall_time_ms": 750,
            "stdout_bytes": 32768,
            "stderr_bytes": 32768,
            "total_output_bytes": 49152,
            "memory_bytes": 268435456,
            "pids": 32,
            "cpus_millis": 500,
            "tmp_bytes": 4194304,
            "work_bytes": 1048576,
        },
        "attempts": [
            {"attempt_id": name, "recipe_id": f"isolation-{name}"}
            for name in EXPECTED
        ],
    }


def run(output_root: Path) -> dict:
    output_root.mkdir(parents=True, exist_ok=False)
    source = output_root / "source"
    runner = output_root / "runner"
    source.mkdir()
    runner.mkdir()
    (source / "public-input.txt").write_text("non-sensitive validation input\n")
    shutil.copyfile(
        Path(__file__).with_name("validation_container_runner.py"),
        runner / "validation_container_runner.py",
    )
    value = manifest(source, runner)
    (output_root / "suite.json").write_text(canonical_json(value) + "\n")
    suite = load_docker_validation_suite(value, source_root=source, runner_root=runner)
    executor = DockerValidationExecutor({suite.suite_id: suite})
    store = ValidationArtifactStore(output_root / "cas")
    attempts = []
    failures = []
    for attempt_id, expected in EXPECTED.items():
        evidence = executor.execute(suite.suite_id, attempt_id, store)
        actual = (
            evidence.termination.kind, evidence.exit_code, evidence.signal
        )
        passed = actual == expected and evidence.cleanup_succeeded
        if attempt_id == "space":
            passed = passed and b"space_exhausted" in store.read(evidence.stdout)
        if not passed:
            failures.append({"attempt_id": attempt_id, "expected": expected, "actual": actual})
        attempts.append({
            "attempt_id": attempt_id,
            "termination": evidence.termination.kind,
            "exit_code": evidence.exit_code,
            "signal": evidence.signal,
            "cleanup_succeeded": evidence.cleanup_succeeded,
            "isolation_status": evidence.isolation.status,
            "runtime_report_sha256": evidence.isolation.runtime_report.sha256,
            "probe_report_sha256": evidence.isolation.probe_report.sha256,
            "stdout_sha256": evidence.stdout.sha256,
            "stderr_sha256": evidence.stderr.sha256,
            "resource_observation_sha256": evidence.resource_observation.sha256,
            "resources": [item.__dict__ for item in evidence.resources],
        })
    report = {
        "schema": "agent-runtime/spec010-isolation-acceptance/v1",
        "suite_id": suite.suite_id,
        "image_ref": suite.image_ref,
        "image_id": suite.image_id,
        "source_tree_digest": suite.source_tree_digest,
        "runner_tree_digest": suite.runner_tree_digest,
        "attempts": attempts,
        "failures": failures,
        "status": "passed" if not failures else "failed",
        "limitations": [
            "containment applies only to this Docker daemon, image, and fixed profile",
            "timeout and output-limit are expected boundary observations, not target safety evidence",
        ],
    }
    (output_root / "report.json").write_text(json.dumps(report, indent=2, sort_keys=True) + "\n")
    if failures:
        raise InvalidInput("one or more Docker isolation acceptance attempts failed")
    return report


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output-root", type=Path, required=True)
    args = parser.parse_args()
    report = run(args.output_root)
    print(json.dumps({
        "status": report["status"],
        "attempts": len(report["attempts"]),
        "report": str((args.output_root / "report.json").resolve()),
    }, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
