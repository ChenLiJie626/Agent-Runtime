"""Materialize SPEC 005's two commits and capture a Clang baseline.

This script records raw tool output. Clang warnings are independent signals,
not ProgramQuery facts or proof that a path is feasible/infeasible.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import subprocess
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
FIXTURE = ROOT / "fixtures" / "null-return"
SOURCE_FILES = ("buffer.hpp", "buffer.cpp", "consumer.cpp")
TRANSLATION_UNITS = ("buffer.cpp", "consumer.cpp")
FLAGS = ("-std=c++17", "-O0", "-g")


def sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def canonical(value: object) -> bytes:
    return json.dumps(
        value, sort_keys=True, separators=(",", ":"), ensure_ascii=False
    ).encode()


def write_json(path: Path, value: object) -> None:
    path.write_bytes(
        json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2).encode() + b"\n"
    )


def git(repo: Path, *args: str, env: dict[str, str] | None = None) -> str:
    result = subprocess.run(
        ["git", "-C", str(repo), *args],
        capture_output=True,
        text=True,
        check=True,
        env=env,
    )
    return result.stdout.strip()


def materialize(output: Path) -> dict[str, str]:
    repo = output / "repo"
    repo.mkdir()
    git(repo, "init", "-q", "--object-format=sha1")
    git(repo, "config", "user.name", "SPEC 005 fixture")
    git(repo, "config", "user.email", "fixture@example.invalid")
    git(repo, "config", "commit.gpgsign", "false")
    git(repo, "config", "core.autocrlf", "false")
    git(repo, "config", "core.hooksPath", "/dev/null")
    commit_env = os.environ.copy()
    commit_env.update(
        {
            "GIT_AUTHOR_DATE": "2026-01-01T00:00:00+00:00",
            "GIT_COMMITTER_DATE": "2026-01-01T00:00:00+00:00",
        }
    )
    commits: dict[str, str] = {}
    for side in ("base", "head"):
        for name in SOURCE_FILES:
            shutil.copyfile(FIXTURE / side / name, repo / name)
        git(repo, "add", *SOURCE_FILES)
        git(repo, "commit", "-q", "-m", f"SPEC 005 {side}", env=commit_env)
        commits[side] = git(repo, "rev-parse", "HEAD")
    changed = git(repo, "diff", "--name-only", commits["base"], commits["head"])
    if changed != "buffer.cpp":
        raise RuntimeError(f"unexpected fixture delta: {changed}")
    for side, commit in commits.items():
        checkout = output / side
        checkout.mkdir()
        for name in SOURCE_FILES:
            (checkout / name).write_bytes(
                subprocess.run(
                    ["git", "-C", str(repo), "show", f"{commit}:{name}"],
                    capture_output=True,
                    check=True,
                ).stdout
            )
    return commits


def compile_database(checkout: Path, compiler: str) -> tuple[str, str]:
    entries = [
        {
            "directory": str(checkout),
            "file": str(checkout / name),
            "arguments": [
                compiler,
                *FLAGS,
                "-I",
                str(checkout),
                "-c",
                str(checkout / name),
            ],
        }
        for name in TRANSLATION_UNITS
    ]
    path = checkout / "compile_commands.json"
    write_json(path, entries)
    normalized = [
        {"file": entry["file"].rsplit("/", 1)[-1], "flags": FLAGS, "include": "."}
        for entry in entries
    ]
    return sha256(path.read_bytes()), sha256(canonical(normalized))


def capture(
    command: list[str], raw_dir: Path, label: str, *, cwd: Path, timeout: int = 30
) -> dict[str, object]:
    start = time.monotonic()
    try:
        result = subprocess.run(
            command, cwd=cwd, capture_output=True, timeout=timeout, check=False
        )
        status = "complete" if result.returncode == 0 else "failed"
        exit_code: int | None = result.returncode
        stdout, stderr = result.stdout, result.stderr
    except subprocess.TimeoutExpired as exc:
        status = "timeout"
        exit_code = None
        stdout, stderr = exc.stdout or b"", exc.stderr or b""
    elapsed_ms = round((time.monotonic() - start) * 1000)
    stdout_path = raw_dir / f"{label}.stdout"
    stderr_path = raw_dir / f"{label}.stderr"
    stdout_path.write_bytes(stdout)
    stderr_path.write_bytes(stderr)
    return {
        "command": command,
        "cwd": str(cwd),
        "status": status,
        "exit_code": exit_code,
        "duration_ms": elapsed_ms,
        "stdout": {"path": str(stdout_path), "sha256": sha256(stdout)},
        "stderr": {"path": str(stderr_path), "sha256": sha256(stderr)},
    }


def run(output: Path, compiler: str) -> dict[str, object]:
    output = output.resolve()
    output.mkdir(parents=True, exist_ok=True)
    if any(output.iterdir()):
        raise ValueError(f"output directory must be empty: {output}")
    resolved_compiler = shutil.which(compiler)
    if resolved_compiler is None:
        raise FileNotFoundError(compiler)
    commits = materialize(output)
    version = subprocess.run(
        [resolved_compiler, "--version"], capture_output=True, text=True, check=True
    ).stdout.splitlines()[0]
    raw_dir = output / "raw"
    raw_dir.mkdir()
    sides: dict[str, object] = {}
    for side in ("base", "head"):
        checkout = output / side
        db_digest, normalized_digest = compile_database(checkout, resolved_compiler)
        invocations: list[dict[str, object]] = []
        for name in TRANSLATION_UNITS:
            invocations.append(
                capture(
                    [
                        resolved_compiler,
                        *FLAGS,
                        "-I",
                        str(checkout),
                        "-fsyntax-only",
                        name,
                    ],
                    raw_dir,
                    f"{side}-{name}-syntax",
                    cwd=checkout,
                )
            )
            sarif = raw_dir / f"{side}-{name}.sarif"
            analysis = capture(
                [
                    resolved_compiler,
                    *FLAGS,
                    "-I",
                    str(checkout),
                    "--analyze",
                    "--analyzer-output",
                    "sarif",
                    "-o",
                    str(sarif),
                    name,
                ],
                raw_dir,
                f"{side}-{name}-analyze",
                cwd=checkout,
            )
            analysis["sarif"] = (
                {"path": str(sarif), "sha256": sha256(sarif.read_bytes())}
                if sarif.exists()
                else None
            )
            analysis["finding_count"] = (
                sum(
                    len(run.get("results", []))
                    for run in json.loads(sarif.read_text()).get("runs", [])
                )
                if sarif.exists() and analysis["status"] == "complete"
                else None
            )
            invocations.append(analysis)
        sides[side] = {
            "commit": commits[side],
            "tree": git(output / "repo", "rev-parse", f"{commits[side]}^{{tree}}"),
            "compile_database": {
                "path": str(checkout / "compile_commands.json"),
                "sha256": db_digest,
                "normalized_sha256": normalized_digest,
            },
            "source_sha256": {
                name: sha256((checkout / name).read_bytes()) for name in SOURCE_FILES
            },
            "invocations": invocations,
        }
    if (
        sides["base"]["compile_database"]["normalized_sha256"]
        != sides["head"]["compile_database"]["normalized_sha256"]
    ):
        raise RuntimeError("fixture compile configurations differ")
    manifest: dict[str, object] = {
        "schema_version": 1,
        "spec": "005",
        "runner_sha256": sha256(Path(__file__).read_bytes()),
        "backend": {"id": "clang-static-analyzer", "version": version},
        "sides": sides,
        "coverage": {
            "universe": "two translation units in the declared fixture build",
            "examined": "independent per-translation-unit Clang Static Analyzer runs",
            "completeness": "partial",
            "omissions": [
                "cross-translation-unit path and object identity not established",
                "indirect caller resolution not established",
                "zero warnings do not prove absence of a defect",
            ],
        },
        "interpretation": "Clang translation-unit warnings are cross-check signals; absence of warnings does not establish path safety or caller completeness.",
        "semantic_query_status": "pending-joern",
        "sdk_smoke_status": "pending-live-run",
    }
    write_json(output / "manifest.json", manifest)
    return manifest


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--compiler", default="clang++")
    args = parser.parse_args()
    manifest = run(args.output, args.compiler)
    print(
        json.dumps(
            {
                "output": str(args.output.resolve()),
                "commits": {
                    side: manifest["sides"][side]["commit"] for side in ("base", "head")
                },
            },
            ensure_ascii=False,
        )
    )


if __name__ == "__main__":
    main()
