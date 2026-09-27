#!/usr/bin/env python3
"""Merge independent ASan evidence bundles while preserving input provenance."""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
from typing import Any

from agent_runtime.codec import utc_now
from agent_runtime.errors import InvalidInput

SCHEMA = "agent-runtime/public-uaf-asan/v1"
STATUSES = {"confirmed", "refuted", "inconclusive"}


def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _identity(item: dict[str, Any]) -> tuple[Any, ...]:
    defect_id = item.get("defect_id")
    if isinstance(defect_id, str) and defect_id:
        return ("defect_id", defect_id)
    return ("location", item.get("project_id"), item.get("path"), item.get("line"))


def merge_evidence(paths: list[Path]) -> dict[str, Any]:
    if not paths:
        raise InvalidInput("at least one ASan evidence input is required")
    evidence: list[dict[str, Any]] = []
    seen: dict[tuple[Any, ...], dict[str, Any]] = {}
    inputs = []
    for path in paths:
        try:
            value = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError) as exc:
            raise InvalidInput(f"cannot read ASan evidence {path}: {exc}") from exc
        if value.get("schema") != SCHEMA or not isinstance(value.get("evidence"), list):
            raise InvalidInput(f"invalid ASan evidence bundle: {path}")
        for item in value["evidence"]:
            if not isinstance(item, dict) or item.get("status") not in STATUSES:
                raise InvalidInput(f"invalid ASan evidence item: {path}")
            identity = _identity(item)
            if identity in seen:
                if item != seen[identity]:
                    raise InvalidInput(f"conflicting ASan evidence for {identity}")
                continue
            seen[identity] = item
            evidence.append(item)
        inputs.append({"path": str(path.resolve()), "sha256": _sha(path)})
    return {
        "schema": SCHEMA,
        "created_at": utc_now(),
        "evidence": evidence,
        "merged_inputs": inputs,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("inputs", nargs="+", type=Path)
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()
    result = merge_evidence(args.inputs)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n")
    print(args.output.resolve())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
