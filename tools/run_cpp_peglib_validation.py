#!/usr/bin/env python3
"""Build and run the frozen cpp-peglib base/fix validation matrix."""
from __future__ import annotations

import argparse
import io
import json
import re
import shutil
import tarfile
from pathlib import Path

from agent_runtime.adapters.docker_validation import (
    DockerValidationExecutor,
    _tree_digest,
    load_docker_validation_suite,
)
from agent_runtime.codec import canonical_json
from agent_runtime.domain import FixedSnapshot
from agent_runtime.errors import EvidenceIntegrityError, InvalidInput
from agent_runtime.runtime import analysis_profile_digest
from agent_runtime.validation import (
    ValidationBuildOutcome,
    ValidationExecutionOutcome,
    ValidationIsolationOutcome,
    ValidationRecord,
    ValidationTermination,
    validation_record_to_dict,
)
from agent_runtime.validation_capture import ValidationArtifactStore
try:
    from tools.spec010_cpp_peglib_rule import cpp_peglib_profile, cpp_peglib_rule
except ModuleNotFoundError:  # Direct execution adds only tools/ to sys.path.
    from spec010_cpp_peglib_rule import cpp_peglib_profile, cpp_peglib_rule

ROOT = Path(__file__).parents[1]
MANIFEST = ROOT / "evaluation/manifests/public-cpp-peglib-validation-v1.json"
RUNNER = ROOT / "tools/validation_container_runner.py"
ASAN_ERROR = re.compile(
    r"ERROR: AddressSanitizer: (?:heap-use-after-free|SEGV on unknown address)"
)
FRAME = re.compile(r"^\s*#\d+.*\bpeglib\.h:\d+(?::\d+)?", re.MULTILINE)


def suite_manifest(manifest, suite_id, source, runner, attempts, *, build):
    return {
        "schema": "agent-runtime/docker-validation-suite/v1",
        "suite_id": suite_id,
        "image_ref": manifest["image_ref"],
        "image_id": manifest["image_id"],
        "source_tree_digest": _tree_digest(source),
        "runner_tree_digest": _tree_digest(runner),
        "limits": {
            "wall_time_ms": 60000 if build else 10000,
            "stdout_bytes": 16 * 1024 * 1024 if build else 4 * 1024 * 1024,
            "stderr_bytes": 4 * 1024 * 1024,
            "total_output_bytes": 20 * 1024 * 1024 if build else 6 * 1024 * 1024,
            "memory_bytes": 1024 * 1024 * 1024,
            "pids": 64,
            "cpus_millis": 1000,
            "tmp_bytes": 16 * 1024 * 1024,
            "work_bytes": 128 * 1024 * 1024,
        },
        "attempts": attempts,
    }


def source_archive(path: Path) -> bytes:
    output = io.BytesIO()
    with tarfile.open(fileobj=output, mode="w") as archive:
        for source in sorted(item for item in path.rglob("*") if item.is_file()):
            relative = source.relative_to(path).as_posix()
            body = source.read_bytes()
            info = tarfile.TarInfo(relative)
            info.size = len(body)
            info.mode = 0o644
            info.mtime = 0
            info.uid = info.gid = 0
            info.uname = info.gname = ""
            archive.addfile(info, io.BytesIO(body))
    return output.getvalue()


def observation(store, evidence):
    stdout = store.read(evidence.stdout).decode("utf-8", "replace")
    stderr = store.read(evidence.stderr).decode("utf-8", "replace")
    combined = stdout + "\n" + stderr
    block = ""
    asan_match = ASAN_ERROR.search(combined)
    if asan_match:
        block = combined[asan_match.start():]
        if "SUMMARY: AddressSanitizer:" in block:
            summary = block.index("SUMMARY: AddressSanitizer:")
            block = block[: block.find("\n", summary) if "\n" in block[summary:] else None]
    parsed = '"phase":"parsed"' in stdout.replace(" ", "")
    optimized = '"phase":"optimized"' in stdout.replace(" ", "")
    if parsed and block and FRAME.search(block):
        return "ast_optimizer_invalid_access", True
    if optimized and evidence.termination == ValidationTermination("exit", exit_code=0):
        return "optimized_completed", True
    return "no_matching_observation", False


def termination_status(termination):
    if termination.kind == "exit":
        return "normal_exit" if termination.exit_code == 0 else "nonzero_exit"
    if termination.kind == "signal":
        return "signal_exit"
    if termination.kind == "timeout":
        return "timeout"
    return "executor_failed"


def composite_isolation(store, build, run):
    runtime = store.put_json({
        "schema": "agent-runtime/composite-isolation-runtime/v1",
        "build": json.loads(store.read(build.isolation.runtime_report)),
        "execution": json.loads(store.read(run.isolation.runtime_report)),
    })
    probe = store.put_json({
        "schema": "agent-runtime/composite-isolation-probe/v1",
        "build": store.read(build.isolation.probe_report).decode(),
        "execution": store.read(run.isolation.probe_report).decode(),
    })
    return ValidationIsolationOutcome("passed", runtime, probe)


def run(manifest, cache_root: Path, output_root: Path):
    prepared = json.loads((cache_root / "prepared.json").read_text())
    if prepared.get("schema") != "agent-runtime/spec010-prepared-inputs/v1":
        raise InvalidInput("prepared cpp-peglib inputs are unavailable")
    output_root.mkdir(parents=True, exist_ok=False)
    runner = output_root / "runner"
    runner.mkdir()
    shutil.copyfile(RUNNER, runner / RUNNER.name)
    store = ValidationArtifactStore(output_root / "cas")
    records_dir = output_root / "records"
    records_dir.mkdir()
    run_inputs = output_root / "run-inputs"
    run_inputs.mkdir()
    policy = store.put_json({
        "schema": "agent-runtime/validation-policy/v1",
        "executor": "registry-only-docker",
        "network": "none",
        "observer_id": manifest["observer_id"],
    })
    rule = cpp_peglib_rule()
    profile = cpp_peglib_profile(policy.sha256)
    profile_digest = analysis_profile_digest(rule, profile=profile)
    recipe = store.put_json({
        "schema": "agent-runtime/cpp-peglib-recipe/v1",
        "recipe_id": manifest["recipe_id"],
        "language": "c++17",
        "optimization": "O1",
        "sanitizers": ["address", "undefined"],
        "frame_pointers": True,
    })
    toolchain = store.put_json({
        "schema": "agent-runtime/toolchain/v1",
        "image_ref": manifest["image_ref"],
        "image_id": manifest["image_id"],
        "compiler": "clang++ 21",
    })
    all_records = []
    builds = {}
    for revision in manifest["revisions"]:
        revision_id = revision["revision_id"]
        source = cache_root / "inputs" / revision_id
        build_value = suite_manifest(
            manifest, f"peglib-{revision_id}-build", source, runner,
            [{"attempt_id": "build", "recipe_id": "cpp-peglib-build"}], build=True,
        )
        build_suite = load_docker_validation_suite(
            build_value, source_root=source, runner_root=runner
        )
        build_evidence = DockerValidationExecutor(
            {build_suite.suite_id: build_suite}
        ).execute(build_suite.suite_id, "build", store)
        outputs = {item.name: item.artifact for item in build_evidence.outputs}
        if (
            build_evidence.termination != ValidationTermination("exit", exit_code=0)
            or set(outputs) != {"binary", "depfile"}
        ):
            raise InvalidInput(f"cpp-peglib {revision_id} build did not complete")
        binary_root = run_inputs / revision_id
        binary_root.mkdir()
        binary_path = binary_root / "probe"
        binary_path.write_bytes(store.read(outputs["binary"]))
        binary_path.chmod(0o555)
        run_value = suite_manifest(
            manifest, f"peglib-{revision_id}-run", binary_root, runner,
            [
                {"attempt_id": mode, "recipe_id": f"cpp-peglib-run-{mode}"}
                for mode in manifest["inputs"]
            ], build=False,
        )
        run_suite = load_docker_validation_suite(
            run_value, source_root=binary_root, runner_root=runner
        )
        executor = DockerValidationExecutor({run_suite.suite_id: run_suite})
        source_ref = store.put(source_archive(source))
        dependency = store.put_json({
            "schema": "agent-runtime/cpp-peglib-dependency/v1",
            "revision_id": revision_id,
            "commit": revision["commit"],
            "header_sha256": revision["header"]["sha256"],
            "license_sha256": revision["license_file"]["sha256"],
            "depfile_sha256": outputs["depfile"].sha256,
        })
        builds[revision_id] = {
            "binary_sha256": outputs["binary"].sha256,
            "depfile_sha256": outputs["depfile"].sha256,
            "stdout_sha256": build_evidence.stdout.sha256,
            "stderr_sha256": build_evidence.stderr.sha256,
            "termination": build_evidence.termination.kind,
        }
        for mode in manifest["inputs"]:
            evidence = executor.execute(run_suite.suite_id, mode, store)
            classification, matched = observation(store, evidence)
            input_ref = store.put(mode.encode())
            scope_id = f"{revision_id}-{mode}"
            snapshot = FixedSnapshot(
                "yhirose/cpp-peglib",
                scope_id,
                source_ref.sha256,
                profile_digest,
                policy.sha256,
            )
            status = termination_status(evidence.termination)
            target_status = "target_observed" if matched else (
                "no_trigger" if status in {"normal_exit", "nonzero_exit", "signal_exit"}
                else "not_run"
            )
            execution = ValidationExecutionOutcome(
                status=status,
                target_status=target_status,
                exit_code=evidence.exit_code,
                signal=evidence.signal,
                stdout=evidence.stdout,
                stderr=evidence.stderr,
                observation_stage=classification if matched else None,
                observation_digest=(
                    evidence.stderr.sha256
                    if matched and classification == "ast_optimizer_invalid_access"
                    else evidence.stdout.sha256 if matched else None
                ),
                termination=evidence.termination,
                resource_observation=evidence.resource_observation,
            )
            record = ValidationRecord(
                repository_id="yhirose/cpp-peglib",
                runtime_snapshot_digest=snapshot.snapshot_digest,
                scope_id=scope_id,
                source=source_ref,
                tool_policy=policy,
                toolchain=toolchain,
                dependency=dependency,
                recipe=recipe,
                test_input=input_ref,
                build=ValidationBuildOutcome(
                    "succeeded",
                    outputs["binary"],
                    build_evidence.stdout,
                    build_evidence.stderr,
                    build_evidence.termination,
                    build_evidence.resource_observation,
                ),
                execution=execution,
                resources=evidence.resources,
                omissions=("other inputs and project paths were not executed",),
                limitations=(
                    "one fixed probe does not establish project safety",
                    "development-set reproduction is not a holdout quality score",
                ),
                isolation=composite_isolation(store, build_evidence, evidence),
                schema_version=(1, 1),
            )
            record_path = records_dir / f"{revision_id}-{mode}.json"
            record_path.write_text(json.dumps(
                validation_record_to_dict(record), indent=2, sort_keys=True
            ) + "\n")
            all_records.append({
                "revision_id": revision_id,
                "commit": revision["commit"],
                "input": mode,
                "record_id": record.record_id,
                "record_path": str(record_path.resolve()),
                "classification": classification,
                "matched_target_observation": matched,
                "termination": evidence.termination.kind,
                "exit_code": evidence.exit_code,
                "signal": evidence.signal,
                "stdout_sha256": evidence.stdout.sha256,
                "stderr_sha256": evidence.stderr.sha256,
            })
    shared = {
        "toolchain": len({json.load(open(row["record_path"]))["toolchain"]["sha256"] for row in all_records}) == 1,
        "recipe": len({json.load(open(row["record_path"]))["recipe"]["sha256"] for row in all_records}) == 1,
        "policy": len({json.load(open(row["record_path"]))["tool_policy"]["sha256"] for row in all_records}) == 1,
    }
    if not all(shared.values()):
        raise EvidenceIntegrityError("cpp-peglib matrix did not use shared configuration")
    report = {
        "schema": "agent-runtime/spec010-cpp-peglib-run/v1",
        "suite_id": manifest["suite_id"],
        "image_ref": manifest["image_ref"],
        "builds": builds,
        "records": all_records,
        "shared_configuration": shared,
        "status": "complete",
        "limitations": manifest["limitations"],
    }
    (output_root / "report.json").write_text(json.dumps(report, indent=2, sort_keys=True) + "\n")
    return report


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--suite", required=True, choices=["public-cpp-peglib-validation-v1"])
    parser.add_argument("--cache-root", type=Path, required=True)
    parser.add_argument("--output-root", type=Path, required=True)
    args = parser.parse_args()
    manifest = json.loads(MANIFEST.read_text())
    report = run(manifest, args.cache_root, args.output_root)
    print(json.dumps({
        "status": report["status"],
        "records": len(report["records"]),
        "observations": {
            row["revision_id"] + "/" + row["input"]: row["classification"]
            for row in report["records"]
        },
    }, sort_keys=True))


if __name__ == "__main__":
    main()
