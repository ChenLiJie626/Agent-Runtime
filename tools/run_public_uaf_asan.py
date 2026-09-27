#!/usr/bin/env python3
"""Run real QLever baseline-header ASan reductions, preserving all attempts."""
from __future__ import annotations

import argparse
import hashlib
import json
import shlex
import shutil
import subprocess
import time
from pathlib import Path

from agent_runtime.lifetime_candidates import LifetimeCandidate, normalize_asan_result

ROOT = Path(__file__).resolve().parents[1]
BASE = "cf5c9d547403c5babac97d35b6cb4868825dbf98"
CASES = (
    (
        "compressor",
        "qlever-2812-coroutine-destruction-order-uaf",
        "src/util/CompressorStream.h",
        33,
    ),
    (
        "external",
        "qlever-2812-async-member-destruction-uaf",
        "src/engine/idTable/CompressedExternalIdTable.h",
        334,
    ),
)


def sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def capture(command, directory, name, timeout):
    """Always retain the command, logs and exit/timeout facts."""
    begin = time.time()
    with (directory / f"{name}.stdout.log").open("w") as stdout, (
        directory / f"{name}.stderr.log"
    ).open("w") as stderr:
        try:
            result = subprocess.run(
                command, stdout=stdout, stderr=stderr, timeout=timeout, check=False
            )
            code, status = result.returncode, "complete"
        except subprocess.TimeoutExpired:
            code, status = None, "timeout"
            # The named container can outlive the Docker client.
            if "--name" in command:
                subprocess.run(
                    ["docker", "rm", "-f", command[command.index("--name") + 1]],
                    capture_output=True,
                    check=False,
                )
    record = {
        "command": command,
        "exit_code": code,
        "status": status,
        "duration_seconds": time.time() - begin,
        "stdout": f"{name}.stdout.log",
        "stderr": f"{name}.stderr.log",
        "stdout_sha256": sha(directory / f"{name}.stdout.log"),
        "stderr_sha256": sha(directory / f"{name}.stderr.log"),
    }
    (directory / f"{name}.json").write_text(json.dumps(record, indent=2) + "\n")
    return record


def run(source, output, image, timeout=600, dependency_build=None):
    if timeout < 1:
        raise ValueError("timeout must be positive")
    revision = subprocess.check_output(
        ["git", "-C", str(source), "rev-parse", "HEAD"], text=True
    ).strip()
    if revision != BASE:
        raise ValueError("ASan source is not the frozen baseline revision")
    for _, _, path, _ in CASES:
        expected = subprocess.check_output(
            ["git", "-C", str(source), "show", f"{BASE}:{path}"]
        )
        if (source / path).read_bytes() != expected:
            raise ValueError(f"baseline header was modified: {path}")
    if subprocess.check_output(
        ["git", "-C", str(source), "diff", "--name-only", BASE, "--", "src"], text=True
    ).strip():
        raise ValueError("tracked baseline implementation files were modified")
    output.mkdir(parents=True, exist_ok=True)
    image_id = subprocess.check_output(
        ["docker", "image", "inspect", image, "--format", "{{.Id}}"], text=True
    ).strip()
    attempt = output / f"attempt-{time.time_ns()}"
    attempt.mkdir()
    fixtures = attempt / "sources"
    fixtures.mkdir()
    for fixture in (ROOT / "evaluation/fixtures/public-uaf-asan").glob("*.cpp"):
        shutil.copyfile(fixture, fixtures / fixture.name)

    def docker(*args):
        return [
            "docker",
            "run",
            "--rm",
            "--name",
            f"uaf-asan-{attempt.name}",
            "--network",
            "none",
            "--memory",
            "4g",
            "--cpus",
            "1",
            "--entrypoint",
            "/bin/bash",
            "-v",
            f"{source.resolve()}:/workspace/source:ro",
            "-v",
            f"{fixtures}:/workspace/repros:ro",
            *(
                [
                    "-v",
                    f"{dependency_build.resolve()}/abseil:/workspace/dependencies:ro",
                ]
                if dependency_build
                else []
            ),
            "-v",
            f"{attempt}:/workspace/output",
            "-w",
            "/workspace/output",
            image_id,
            "-lc",
            shlex.join(args),
        ]

    common_flags = "-fsanitize=address -fno-omit-frame-pointer -g"
    if dependency_build:
        build = json.loads((dependency_build / "build-dependencies.json").read_text())
        configure = json.loads(
            (dependency_build / "configure-dependencies.json").read_text()
        )
        if (
            build["exit_code"] != 0
            or image_id not in configure["command"]
            or common_flags not in shlex.join(configure["command"])
        ):
            raise ValueError(
                "dependency build provenance does not match sanitizer configuration"
            )
        build = {
            **build,
            "artifact_dir": str(dependency_build.resolve()),
            "reused": True,
        }
    else:
        configure = capture(
            docker(
                "cmake",
                "-S",
                "/workspace/source/build-ctu/_deps/abseil-src",
                "-B",
                "/workspace/output/abseil",
                "-DCMAKE_CXX_COMPILER=clang++",
                "-DCMAKE_BUILD_TYPE=Debug",
                "-DABSL_BUILD_TESTING=OFF",
                "-DCMAKE_CXX_STANDARD=20",
                f"-DCMAKE_CXX_FLAGS={common_flags}",
            ),
            attempt,
            "configure-dependencies",
            timeout,
        )
        build = (
            capture(
                docker(
                    "cmake", "--build", "/workspace/output/abseil", "--parallel", "1"
                ),
                attempt,
                "build-dependencies",
                timeout,
            )
            if configure["exit_code"] == 0
            else configure
        )
    rows = json.loads((source / "compile_commands.container.json").read_text())
    tokens = rows[0].get("arguments") or shlex.split(rows[0]["command"])
    includes = []
    for index, token in enumerate(tokens):
        if token.startswith(("-I", "-D")):
            includes.append(token)
        elif token == "-isystem":
            includes.extend([token, tokens[index + 1]])
    dependency_dir = (
        dependency_build / "abseil" if dependency_build else attempt / "abseil"
    )
    library_root = (
        "/workspace/dependencies/" if dependency_build else "/workspace/output/abseil/"
    )
    libraries = [
        library_root + p.relative_to(dependency_dir).as_posix()
        for p in sorted(dependency_dir.rglob("*.a"))
    ]
    evidence = []
    for name, defect_id, path, line in CASES:
        candidate = LifetimeCandidate(
            "public-uaf-asan", path, line, "frozen baseline lifetime candidate", ()
        )
        command = docker(
            "clang++",
            "-std=c++20",
            "-O0",
            *shlex.split(common_flags),
            "-pthread",
            *includes,
            f"/workspace/repros/{name}.cpp",
            *(
                ["/workspace/source/src/util/MemorySize/MemorySize.cpp"]
                if name == "external"
                else []
            ),
            "-Wl,--start-group",
            *libraries,
            "-Wl,--end-group",
            "-lboost_iostreams",
            "-lboost_container",
            "-lz",
            "-lzstd",
            "-lssl",
            "-lcrypto",
            "-o",
            f"/workspace/output/{name}",
        )
        built = capture(command, attempt, f"{name}-build", timeout)
        runs = []
        if built["exit_code"] == 0:
            for variant in (
                (("deflate", []), ("gzip", ["gzip"]))
                if name == "compressor"
                else (("async", []),)
            ):
                run_command = docker(
                    "env",
                    "ASAN_OPTIONS=halt_on_error=1:detect_stack_use_after_return=1",
                    "ASAN_SYMBOLIZER_PATH=/usr/lib/llvm-21/bin/llvm-symbolizer",
                    f"/workspace/output/{name}",
                    *variant[1],
                )
                record = capture(run_command, attempt, f"{name}-{variant[0]}", timeout)
                stdout = (attempt / record["stdout"]).read_text(errors="replace")
                stderr = (attempt / record["stderr"]).read_text(errors="replace")
                normalized = normalize_asan_result(
                    candidate,
                    command=shlex.join(run_command),
                    exit_code=record["exit_code"],
                    stdout=stdout,
                    stderr=stderr,
                    instrumented=True,
                    exercised="TARGET_PATH_EXERCISED" in stderr,
                    source_roots=("/workspace/source",),
                )
                runs.append({**record, **normalized.to_dict()})
        chosen = next(
            (x for x in runs if x["status"] == "confirmed"), runs[0] if runs else {}
        )
        item = {
            "defect_id": defect_id,
            "project_id": "ad-freiburg/qlever",
            "path": path,
            "line": line,
            "revision": revision,
            "header_sha256": sha(source / path),
            "source_sha256": sha(fixtures / f"{name}.cpp"),
            "image_id": image_id,
            "attempt_dir": str(attempt),
            "build": built,
            "runs": runs,
            "binary_sha256": (
                sha(attempt / name) if (attempt / name).is_file() else None
            ),
            "support_source_sha256": (
                sha(source / "src/util/MemorySize/MemorySize.cpp")
                if name == "external"
                else None
            ),
            "dependency_build": build,
            "status": "inconclusive",
            "blocker": None,
            **chosen,
        }
        if not runs:
            item["blocker"] = "ASan reproduction build failed; see complete build logs"
            item["command"] = built["command"]
            item["exit_code"] = built["exit_code"]
        evidence.append(item)
        (attempt / "evidence.json").write_text(
            json.dumps({"evidence": evidence}, indent=2) + "\n"
        )
        print(f"{name}: {item['status']}", flush=True)
    result = {"schema": "agent-runtime/public-uaf-asan/v1", "evidence": evidence}
    (output / "evidence.json").write_text(json.dumps(result, indent=2) + "\n")
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument(
        "--image", default="agent-runtime/clang-ctu:clang21-codechecker6291"
    )
    parser.add_argument("--timeout-seconds", type=int, default=600)
    parser.add_argument("--dependency-build", type=Path)
    args = parser.parse_args()
    result = run(
        args.source,
        args.output.resolve(),
        args.image,
        args.timeout_seconds,
        args.dependency_build,
    )
    return (
        0
        if all(x["status"] in {"confirmed", "refuted"} for x in result["evidence"])
        else 1
    )


if __name__ == "__main__":
    raise SystemExit(main())
