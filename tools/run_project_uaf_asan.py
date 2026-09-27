#!/usr/bin/env python3
"""Run the remaining QLever project-level ASan regressions on the frozen base."""
from __future__ import annotations

import argparse
import hashlib
import json
import shlex
import subprocess
import time
from pathlib import Path
from typing import Any

from agent_runtime.lifetime_candidates import LifetimeCandidate, normalize_asan_result

BASE = "cf5c9d547403c5babac97d35b6cb4868825dbf98"
ALLOWED_TEST_CHANGES = {
    "test/ServerTest.cpp",
    "test/JoinAlgorithmsTest.cpp",
}
CASES = (
    {
        "name": "join",
        "defect_id": "qlever-2812-local-vocab-propagation-uaf",
        "path": "src/util/JoinAlgorithms/JoinAlgorithms.h",
        "line": 1755,
        "target": "JoinAlgorithmsTest",
        "binary": "build/test/JoinAlgorithmsTest",
        "filter": "JoinAlgorithms.SpecialOptionalJoinLocalVocabOwnership",
    },
    {
        "name": "server",
        "defect_id": "qlever-2812-query-context-lifetime-uaf",
        "path": "src/engine/Server.cpp",
        "line": 327,
        "target": "ServerTest",
        "binary": "build/test/ServerTest",
        "filter": "ServerLifetimeRegression.queryContextOutlivesCoroutineFrame",
    },
)


def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _write(path: Path, value: Any) -> None:
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n")


def _capture(
    command: list[str], attempt: Path, name: str, timeout: int
) -> dict[str, Any]:
    begin = time.time()
    stdout_path = attempt / f"{name}.stdout.log"
    stderr_path = attempt / f"{name}.stderr.log"
    container_name = (
        command[command.index("--name") + 1] if "--name" in command else None
    )
    with stdout_path.open("w") as stdout, stderr_path.open("w") as stderr:
        try:
            process = subprocess.run(
                command, stdout=stdout, stderr=stderr, check=False, timeout=timeout
            )
            exit_code, status = process.returncode, "complete"
        except subprocess.TimeoutExpired:
            exit_code, status = None, "timeout"
            if container_name is not None:
                observed = subprocess.run(
                    [
                        "docker",
                        "exec",
                        container_name,
                        "cat",
                        "/sys/fs/cgroup/memory.peak",
                    ],
                    capture_output=True,
                    text=True,
                    check=False,
                )
                if observed.returncode == 0:
                    (attempt / f"{name}.memory-peak.txt").write_text(observed.stdout)
                subprocess.run(
                    ["docker", "stop", "-t", "5", container_name],
                    capture_output=True,
                    check=False,
                )
    container_state = None
    if container_name is not None:
        inspected = subprocess.run(
            ["docker", "inspect", container_name, "--format", "{{json .State}}"],
            capture_output=True,
            text=True,
            check=False,
        )
        if inspected.returncode == 0:
            container_state = json.loads(inspected.stdout)
        subprocess.run(
            ["docker", "rm", "-f", container_name],
            capture_output=True,
            check=False,
        )
    resource = attempt / f"{name}.memory-peak.txt"
    record = {
        "command": command,
        "exit_code": exit_code,
        "status": status,
        "duration_seconds": time.time() - begin,
        "stdout": stdout_path.name,
        "stderr": stderr_path.name,
        "stdout_sha256": _sha(stdout_path),
        "stderr_sha256": _sha(stderr_path),
        "peak_memory_bytes": (
            int(resource.read_text().strip()) if resource.is_file() else None
        ),
        "resource_path": resource.name if resource.is_file() else None,
        "resource_sha256": _sha(resource) if resource.is_file() else None,
        "resource_method": "cgroup v2 memory.peak",
        "container_state": container_state,
    }
    _write(attempt / f"{name}.json", record)
    return record


def run(
    source: Path,
    cache: Path,
    attempt: Path,
    image: str,
    timeout: int,
) -> dict[str, Any]:
    source, cache, attempt = source.resolve(), cache.resolve(), attempt.resolve()
    revision = subprocess.check_output(
        ["git", "-C", str(source), "rev-parse", "HEAD"], text=True
    ).strip()
    if revision != BASE:
        raise ValueError("project ASan source is not the frozen baseline")
    changed = set(
        subprocess.check_output(
            ["git", "-C", str(source), "diff", "--name-only", "HEAD"], text=True
        ).splitlines()
    )
    if changed - ALLOWED_TEST_CHANGES or not changed:
        raise ValueError(f"unexpected project ASan worktree changes: {sorted(changed)}")
    if subprocess.check_output(
        ["git", "-C", str(source), "diff", "--name-only", "HEAD", "--", "src"],
        text=True,
    ).strip():
        raise ValueError("frozen production implementation was modified")
    for case in CASES:
        expected = subprocess.check_output(
            ["git", "-C", str(source), "show", f"{BASE}:{case['path']}"]
        )
        if (source / case["path"]).read_bytes() != expected:
            raise ValueError(f"frozen target differs from baseline: {case['path']}")
    compile_commands = attempt / "build/compile_commands.json"
    if not compile_commands.is_file():
        raise ValueError("configured ASan build is missing compile_commands.json")
    compile_text = compile_commands.read_text(encoding="utf-8")
    if (
        "-fsanitize=address" not in compile_text
        or "-fno-omit-frame-pointer" not in compile_text
    ):
        raise ValueError("build database does not attest ASan instrumentation")
    attempt.mkdir(parents=True, exist_ok=True)
    patch = subprocess.check_output(
        ["git", "-C", str(source), "diff", "--binary", "HEAD"]
    )
    (attempt / "test-only.patch").write_bytes(patch)
    image_id = subprocess.check_output(
        ["docker", "image", "inspect", image, "--format", "{{.Id}}"], text=True
    ).strip()

    def docker(name: str, *args: str, cache_mount: bool = False) -> list[str]:
        command = [
            "docker",
            "run",
            "--name",
            f"project-uaf-{name}-{time.time_ns()}",
            "--network",
            "none",
            "--memory",
            "5g",
            "--cpus",
            "1",
            "--entrypoint",
            "/bin/bash",
            "-v",
            f"{source}:/workspace/source:ro",
        ]
        if cache_mount:
            command.extend(["-v", f"{cache}:/workspace/cache/_deps:ro"])
        resource_name = name + ".memory-peak.txt"
        shell = (
            f"set +e; {shlex.join(args)}; code=$?; "
            f"cat /sys/fs/cgroup/memory.peak > /workspace/output/{resource_name}; "
            "exit $code"
        )
        command.extend(
            [
                "-v",
                f"{attempt}:/workspace/output",
                "-w",
                "/workspace/output",
                image_id,
                "-lc",
                shell,
            ]
        )
        return command

    evidence = []
    for case in CASES:
        build = _capture(
            docker(
                f"{case['name']}-build-final",
                "cmake",
                "--build",
                "/workspace/output/build",
                "--target",
                case["target"],
                "--parallel",
                "1",
                cache_mount=True,
            ),
            attempt,
            f"{case['name']}-build-final",
            timeout,
        )
        runs = []
        binary = attempt / case["binary"]
        if build["exit_code"] == 0 and binary.is_file():
            command = docker(
                f"{case['name']}-run",
                "env",
                "ASAN_OPTIONS=halt_on_error=1:detect_stack_use_after_return=1",
                "UBSAN_OPTIONS=halt_on_error=0:print_stacktrace=1",
                "ASAN_SYMBOLIZER_PATH=/usr/lib/llvm-21/bin/llvm-symbolizer",
                f"/workspace/output/{case['binary']}",
                f"--gtest_filter={case['filter']}",
                "--gtest_color=no",
            )
            record = _capture(command, attempt, f"{case['name']}-run", timeout)
            stdout = (attempt / record["stdout"]).read_text(errors="replace")
            stderr = (attempt / record["stderr"]).read_text(errors="replace")
            started = "TARGET_PATH_EXERCISED" in stdout + stderr
            candidate = LifetimeCandidate(
                "public-uaf-asan",
                case["path"],
                case["line"],
                "frozen baseline lifetime candidate",
                (),
            )
            normalized = normalize_asan_result(
                candidate,
                command=shlex.join(command),
                exit_code=record["exit_code"],
                stdout=stdout,
                stderr=stderr,
                instrumented=True,
                exercised=started,
                source_roots=("/workspace/source",),
            )
            runs.append({**record, "gtest_started": started, **normalized.to_dict()})
        chosen = next(
            (item for item in runs if item["status"] == "confirmed"),
            runs[0] if runs else {},
        )
        item = {
            "defect_id": case["defect_id"],
            "project_id": "ad-freiburg/qlever",
            "path": case["path"],
            "line": case["line"],
            "revision": revision,
            "target_sha256": _sha(source / case["path"]),
            "test_patch_sha256": _sha(attempt / "test-only.patch"),
            "compile_database_sha256": _sha(compile_commands),
            "image_id": image_id,
            "attempt_dir": str(attempt),
            "build": build,
            "runs": runs,
            "binary_sha256": _sha(binary) if binary.is_file() else None,
            "status": "inconclusive",
            "blocker": None,
            **chosen,
        }
        if not runs:
            item["blocker"] = "ASan project regression build failed; see retained logs"
            item["command"] = build["command"]
            item["exit_code"] = build["exit_code"]
        evidence.append(item)
        _write(attempt / "evidence.json", {"evidence": evidence})
        print(f"{case['name']}: {item['status']}", flush=True)
    final_patch = subprocess.check_output(
        ["git", "-C", str(source), "diff", "--binary", "HEAD"]
    )
    if hashlib.sha256(final_patch).hexdigest() != _sha(attempt / "test-only.patch"):
        raise RuntimeError("project ASan source changed during evidence collection")
    result = {
        "schema": "agent-runtime/public-uaf-asan/v1",
        "source_revision": revision,
        "allowed_test_changes": sorted(changed),
        "source_tree_diff_sha256": _sha(attempt / "test-only.patch"),
        "evidence": evidence,
    }
    _write(attempt / "evidence.json", result)
    return result


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, required=True)
    parser.add_argument("--dependency-cache", type=Path, required=True)
    parser.add_argument("--attempt", type=Path, required=True)
    parser.add_argument(
        "--image", default="agent-runtime/clang-ctu:clang21-codechecker6291"
    )
    parser.add_argument("--timeout-seconds", type=int, default=1200)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    result = run(
        args.source,
        args.dependency_cache,
        args.attempt,
        args.image,
        args.timeout_seconds,
    )
    output = args.output or args.attempt / "evidence.json"
    _write(output, result)
    return (
        0
        if all(x["status"] in {"confirmed", "refuted"} for x in result["evidence"])
        else 1
    )


if __name__ == "__main__":
    raise SystemExit(main())
