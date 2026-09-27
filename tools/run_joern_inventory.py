"""Run fixed SPEC 005 Joern inventory queries and retain all raw output."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import subprocess
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "tools" / "joern_inventory.sc"


def sha256(path: Path) -> str:
    hasher = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            hasher.update(chunk)
    return hasher.hexdigest()


def capture(
    command: list[str],
    *,
    cwd: Path,
    raw: Path,
    label: str,
    env: dict[str, str],
    timeout: int,
) -> dict[str, object]:
    start = time.monotonic()
    try:
        result = subprocess.run(
            command, cwd=cwd, env=env, capture_output=True, timeout=timeout, check=False
        )
        stdout, stderr = result.stdout, result.stderr
        code: int | None = result.returncode
        status = "complete" if code == 0 else "failed"
    except subprocess.TimeoutExpired as exc:
        stdout, stderr = exc.stdout or b"", exc.stderr or b""
        code = None
        status = "timeout"
    stdout_path = raw / f"{label}.stdout"
    stderr_path = raw / f"{label}.stderr"
    stdout_path.write_bytes(stdout)
    stderr_path.write_bytes(stderr)
    return {
        "command": command,
        "cwd": str(cwd),
        "status": status,
        "exit_code": code,
        "duration_ms": round((time.monotonic() - start) * 1000),
        "stdout": {"path": str(stdout_path), "sha256": sha256(stdout_path)},
        "stderr": {"path": str(stderr_path), "sha256": sha256(stderr_path)},
    }


def run(
    fixture: Path, output: Path, joern_home: Path, java_home: Path
) -> dict[str, object]:
    fixture, output = fixture.resolve(), output.resolve()
    joern_home, java_home = joern_home.resolve(), java_home.resolve()
    fixture_manifest = json.loads((fixture / "manifest.json").read_text())
    output.mkdir(parents=True, exist_ok=True)
    if any(output.iterdir()):
        raise ValueError(f"output directory must be empty: {output}")
    raw = output / "raw"
    raw.mkdir()
    env = os.environ.copy()
    env["JAVA_HOME"] = str(java_home)
    env["PATH"] = os.pathsep.join(
        [
            str(java_home / "bin"),
            str(joern_home.parent.parent / "bin"),
            env.get("PATH", ""),
        ]
    )
    java_version = subprocess.run(
        [str(java_home / "bin" / "java"), "-version"],
        capture_output=True,
        text=True,
        check=True,
    ).stderr.splitlines()[0]
    results: dict[str, object] = {}
    for side in ("base", "head"):
        checkout = fixture / side
        expected = fixture_manifest["sides"][side]["source_sha256"]
        if any(sha256(checkout / name) != expected[name] for name in expected):
            raise ValueError(f"{side} source no longer matches fixture manifest")
        side_output = output / side
        side_output.mkdir()
        cpg = side_output / "cpg.bin.zip"
        parse = capture(
            [
                str(joern_home / "c2cpg.sh"),
                str(checkout),
                "--compilation-database",
                str(checkout / "compile_commands.json"),
                "--log-problems",
                "--output",
                str(cpg),
            ],
            cwd=side_output,
            raw=raw,
            label=f"{side}-parse",
            env=env,
            timeout=180,
        )
        parse["cpg"] = (
            {"path": str(cpg), "sha256": sha256(cpg)} if cpg.exists() else None
        )
        query = None
        if parse["status"] == "complete" and cpg.exists():
            query = capture(
                [
                    str(joern_home / "joern"),
                    "--script",
                    str(SCRIPT),
                    "--param",
                    f"cpgFile={cpg}",
                ],
                cwd=side_output,
                raw=raw,
                label=f"{side}-inventory",
                env=env,
                timeout=180,
            )
        results[side] = {
            "commit": fixture_manifest["sides"][side]["commit"],
            "compile_database_sha256": fixture_manifest["sides"][side][
                "compile_database"
            ]["sha256"],
            "parse": parse,
            "inventory": query,
        }
    report: dict[str, object] = {
        "spec": "005",
        "backend_id": "joern",
        "joern_release": "v4.0.636",
        "java_version": java_version,
        "script_sha256": sha256(SCRIPT),
        "sides": results,
        "interpretation": "Inventory output is source material only; flow feasibility, object identity and coverage must be assessed separately.",
    }
    (output / "manifest.json").write_text(
        json.dumps(report, indent=2, sort_keys=True, ensure_ascii=False) + "\n"
    )
    return report


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--fixture", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--joern-home", type=Path, required=True)
    parser.add_argument("--java-home", type=Path, required=True)
    args = parser.parse_args()
    report = run(args.fixture, args.output, args.joern_home, args.java_home)
    print(
        json.dumps(
            {
                side: report["sides"][side]["inventory"]["status"]
                if report["sides"][side]["inventory"]
                else "not-run"
                for side in ("base", "head")
            }
        )
    )


if __name__ == "__main__":
    main()
