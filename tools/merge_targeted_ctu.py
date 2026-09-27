#!/usr/bin/env python3
"""Merge independently executed targeted CTU metadata without rewriting attempts."""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
from typing import Any

from agent_runtime.codec import utc_now
from agent_runtime.errors import InvalidInput

SCHEMA = "agent-runtime/layered-ctu-execution/v1"


def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def merge_metadata(paths: list[Path]) -> dict[str, Any]:
    if not paths:
        raise InvalidInput("at least one targeted CTU metadata input is required")
    executions: list[dict[str, Any]] = []
    target_analysis: dict[str, Any] = {}
    project_id: str | None = None
    inputs = []
    seen = set()
    for path in paths:
        try:
            value = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError) as exc:
            raise InvalidInput(
                f"cannot read targeted CTU metadata {path}: {exc}"
            ) from exc
        if value.get("schema") != SCHEMA or not isinstance(
            value.get("executions"), list
        ):
            raise InvalidInput(f"invalid targeted CTU metadata: {path}")
        current_project = value.get("project_id")
        if not isinstance(current_project, str) or not current_project:
            raise InvalidInput(f"missing project identity: {path}")
        if project_id is None:
            project_id = current_project
        elif current_project != project_id:
            raise InvalidInput(
                "cannot merge targeted CTU metadata from different projects"
            )
        for execution in value["executions"]:
            if not isinstance(execution, dict):
                raise InvalidInput(f"invalid execution record: {path}")
            identity = (
                execution.get("target_path"),
                execution.get("artifact_dir"),
                execution.get("compile_database_sha256"),
            )
            if identity in seen:
                continue
            seen.add(identity)
            executions.append(execution)
        analysis = value.get("target_analysis", {})
        if not isinstance(analysis, dict):
            raise InvalidInput(f"invalid target analysis map: {path}")
        target_analysis.update(analysis)
        inputs.append({"path": str(path.resolve()), "sha256": _sha(path)})
    return {
        "schema": SCHEMA,
        "created_at": utc_now(),
        "project_id": project_id,
        "continue_after_layer_failure": True,
        "executions": executions,
        "target_analysis": target_analysis,
        "merged_inputs": inputs,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("inputs", nargs="+", type=Path)
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()
    result = merge_metadata(args.inputs)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n")
    print(args.output.resolve())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
