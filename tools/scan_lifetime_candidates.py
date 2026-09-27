#!/usr/bin/env python3
"""Scan C++ lifetime candidates and normalize externally produced ASan logs."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from agent_runtime.lifetime_candidates import (
    LifetimeCandidate,
    build_asan_checklist,
    normalize_asan_result,
    scan_cpp_paths,
)


def _candidate(path: Path) -> LifetimeCandidate:
    value = json.loads(path.read_text(encoding="utf-8"))
    return LifetimeCandidate(
        rule_id=value["rule_id"],
        path=value["path"],
        line=int(value["line"]),
        reason=value["reason"],
        evidence_hints=tuple(value["evidence_hints"]),
    )


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(description=__doc__)
    subcommands = result.add_subparsers(dest="operation", required=True)

    scan = subcommands.add_parser("scan", help="scan C/C++ files or directories")
    scan.add_argument("paths", nargs="+", type=Path)
    scan.add_argument("--root", type=Path)
    scan.add_argument("--output", type=Path)

    checklist = subcommands.add_parser("checklist", help="create an ASan evidence checklist")
    checklist.add_argument("--candidate", type=Path, required=True)
    checklist.add_argument("--build-command", required=True)
    checklist.add_argument("--reproduce-command", required=True)

    asan = subcommands.add_parser("asan", help="normalize existing ASan logs")
    asan.add_argument("--candidate", type=Path, required=True)
    asan.add_argument("--command", required=True)
    asan.add_argument("--exit-code", type=int)
    asan.add_argument("--stdout-log", type=Path)
    asan.add_argument("--stderr-log", type=Path)
    asan.add_argument("--instrumented", action="store_true")
    asan.add_argument("--exercised", action="store_true")
    return result


def main() -> int:
    args = parser().parse_args()
    if args.operation == "scan":
        candidates = scan_cpp_paths(args.paths, root=args.root)
        output = json.dumps(
            {"candidates": [item.to_dict() for item in candidates]},
            indent=2,
            sort_keys=True,
        ) + "\n"
        if args.output:
            args.output.write_text(output, encoding="utf-8")
        else:
            print(output, end="")
        return 0

    candidate = _candidate(args.candidate)
    if args.operation == "checklist":
        value = build_asan_checklist(
            candidate,
            build_command=args.build_command,
            reproduce_command=args.reproduce_command,
        ).to_dict()
    else:
        stdout = args.stdout_log.read_text(encoding="utf-8", errors="replace") if args.stdout_log else ""
        stderr = args.stderr_log.read_text(encoding="utf-8", errors="replace") if args.stderr_log else ""
        value = normalize_asan_result(
            candidate,
            command=args.command,
            exit_code=args.exit_code,
            stdout=stdout,
            stderr=stderr,
            instrumented=args.instrumented,
            exercised=args.exercised,
        ).to_dict()
    print(json.dumps(value, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
