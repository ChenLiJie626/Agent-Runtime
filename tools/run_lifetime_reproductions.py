#!/usr/bin/env python3
"""Compile and run the three lifetime mechanism fixtures with AddressSanitizer."""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import tempfile
from pathlib import Path
from typing import Any

from agent_runtime.lifetime_candidates import (
    ASYNC_MEMBER_DESTRUCTION_RULE,
    CONTAINER_INVALIDATION_RULE,
    COROUTINE_DESTRUCTION_RULE,
    normalize_asan_result,
    scan_cpp_source,
)


ROOT = Path(__file__).resolve().parents[1]
FIXTURES = ROOT / "evaluation/fixtures/lifetime-candidates"
CASES = (
    ("container-invalidation.cpp", CONTAINER_INVALIDATION_RULE),
    ("coroutine-destruction.cpp", COROUTINE_DESTRUCTION_RULE),
    ("async-member-destruction.cpp", ASYNC_MEMBER_DESTRUCTION_RULE),
)


def run_reproductions(
    *, compiler: str = "clang++", timeout_seconds: int = 20
) -> dict[str, Any]:
    executable = shutil.which(compiler)
    results: list[dict[str, Any]] = []
    if executable is None:
        return {
            "schema": "agent-runtime/lifetime-reproductions/v1",
            "compiler": compiler,
            "status": "inconclusive",
            "results": [],
            "reason": "compiler is unavailable",
        }

    with tempfile.TemporaryDirectory(prefix="lifetime-asan-") as directory:
        build_root = Path(directory)
        for filename, rule_id in CASES:
            source_path = FIXTURES / filename
            display_path = source_path.relative_to(ROOT).as_posix()
            source = source_path.read_text(encoding="utf-8")
            candidates = [
                item for item in scan_cpp_source(display_path, source)
                if item.rule_id == rule_id
            ]
            if len(candidates) != 1:
                results.append({
                    "fixture": display_path,
                    "rule_id": rule_id,
                    "status": "inconclusive",
                    "reason": f"expected one candidate, found {len(candidates)}",
                })
                continue
            output = build_root / source_path.stem
            build_command = [
                executable,
                "-std=c++20",
                "-O0",
                "-g",
                "-fsanitize=address",
                "-fno-omit-frame-pointer",
                "-pthread",
                str(source_path),
                "-o",
                str(output),
            ]
            built = subprocess.run(
                build_command,
                check=False,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                timeout=timeout_seconds,
            )
            if built.returncode != 0:
                results.append({
                    "fixture": display_path,
                    "rule_id": rule_id,
                    "status": "inconclusive",
                    "reason": "ASan fixture build failed",
                    "build_command": build_command,
                    "build_exit_code": built.returncode,
                    "build_stderr": built.stderr,
                })
                continue
            environment = os.environ.copy()
            environment["ASAN_OPTIONS"] = (
                "halt_on_error=1:detect_stack_use_after_return=1:abort_on_error=1"
            )
            command = [str(output)]
            try:
                completed = subprocess.run(
                    command,
                    check=False,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    text=True,
                    env=environment,
                    timeout=timeout_seconds,
                )
            except subprocess.TimeoutExpired as exc:
                results.append({
                    "fixture": display_path,
                    "rule_id": rule_id,
                    "status": "inconclusive",
                    "reason": "ASan reproduction timed out",
                    "command": command,
                    "stdout": exc.stdout or "",
                    "stderr": exc.stderr or "",
                })
                continue
            log = completed.stdout + "\n" + completed.stderr
            evidence = normalize_asan_result(
                candidates[0],
                command=" ".join(command),
                exit_code=completed.returncode,
                stdout=completed.stdout,
                stderr=completed.stderr,
                instrumented=True,
                exercised="TARGET_PATH_EXERCISED" in log,
            )
            results.append({
                "fixture": display_path,
                "rule_id": rule_id,
                "build_command": build_command,
                **evidence.to_dict(),
            })

    confirmed = sum(item["status"] == "confirmed" for item in results)
    return {
        "schema": "agent-runtime/lifetime-reproductions/v1",
        "compiler": executable,
        "status": "confirmed" if confirmed == len(CASES) else "inconclusive",
        "confirmed": confirmed,
        "total": len(CASES),
        "results": results,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--compiler", default="clang++")
    parser.add_argument("--timeout-seconds", type=int, default=20)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    result = run_reproductions(
        compiler=args.compiler, timeout_seconds=args.timeout_seconds
    )
    text = json.dumps(result, ensure_ascii=False, indent=2) + "\n"
    if args.output is None:
        print(text, end="")
    else:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(text, encoding="utf-8")
        print(args.output.resolve())
    return 0 if result["status"] == "confirmed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
