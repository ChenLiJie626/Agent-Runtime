#!/usr/bin/env python3
"""Execute a v2 layered CTU plan without letting one layer abort the rest."""

from __future__ import annotations

import argparse
import json
import shutil
import subprocess
from pathlib import Path
from typing import Any

from agent_runtime.codec import utc_now, validate_relative_path
from agent_runtime.errors import InvalidInput


def _metadata_stats(report_dir: Path) -> dict[str, Any]:
    metadata_path = report_dir / "metadata.json"
    try:
        metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
        tools = metadata["tools"]
        statistics = tools[0]["analyzers"]["clangsa"]["analyzer_statistics"]
        successful = int(statistics["successful"])
        failed = int(statistics["failed"])
    except (OSError, ValueError, KeyError, IndexError, TypeError, json.JSONDecodeError):
        return {
            "metadata": str(metadata_path),
            "successful_translation_units": 0,
            "failed_translation_units": 0,
            "successful_sources": [],
            "failed_sources": [],
            "metadata_valid": False,
        }
    return {
        "metadata": str(metadata_path),
        "successful_translation_units": successful,
        "failed_translation_units": failed,
        "successful_sources": list(statistics.get("successful_sources") or []),
        "failed_sources": list(statistics.get("failed_sources") or []),
        "metadata_valid": True,
    }


def execute_plan(
    plan_path: Path,
    *,
    codechecker: str = "CodeChecker",
    timeout_seconds: int = 1800,
) -> dict[str, Any]:
    """Run the full non-CTU layer and every isolated target CTU slice."""

    try:
        plan = json.loads(plan_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise InvalidInput(f"cannot read layered CTU plan: {exc}") from exc
    if plan.get("schema") != "agent-runtime/layered-ctu-plan/v2":
        raise InvalidInput("run_layered_ctu requires a v2 layered CTU plan")
    if type(timeout_seconds) is not int or timeout_seconds < 1:
        raise InvalidInput("timeout_seconds must be a positive integer")
    executable = shutil.which(codechecker)
    if executable is None:
        raise InvalidInput(f"CodeChecker executable is unavailable: {codechecker}")

    root = plan_path.resolve().parent
    slices = plan.get("ctu_slices")
    layers = plan.get("layers")
    if not isinstance(slices, list) or not isinstance(layers, list):
        raise InvalidInput("layered CTU plan has invalid layers")
    full = next(
        (item for item in layers if item.get("layer_id") == "non-ctu-full"), None
    )
    runnable = [full, *slices]
    if full is None or any(not isinstance(item, dict) for item in runnable):
        raise InvalidInput("layered CTU plan is missing the non-CTU layer")

    executions: list[dict[str, Any]] = []
    target_analysis: dict[str, dict[str, Any]] = {}
    for layer in runnable:
        assert isinstance(layer, dict)
        layer_id = layer.get("layer_id")
        database_name = layer.get("compile_commands")
        if not isinstance(layer_id, str) or not isinstance(database_name, str):
            raise InvalidInput("layer entry is missing its identity or compile database")
        validate_relative_path(database_name)
        database_path = root / database_name
        report_dir = root / "reports" / layer_id
        if not layer.get("enabled", False):
            execution = {
                "layer_id": layer_id,
                "ctu_enabled": bool(layer.get("ctu_enabled")),
                "status": "not_run",
                "reason": "the slice selected no translation units",
                "command": [],
                "exit_code": None,
            }
        else:
            command = [
                executable,
                "analyze",
                str(database_path),
                "--output",
                str(report_dir),
                "--analyzers",
                "clangsa",
                "--capture-analysis-output",
            ]
            if layer.get("ctu_enabled"):
                command.append("--ctu")
            try:
                completed = subprocess.run(
                    command,
                    cwd=root,
                    check=False,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    text=True,
                    timeout=timeout_seconds,
                )
            except subprocess.TimeoutExpired as exc:
                execution = {
                    "layer_id": layer_id,
                    "ctu_enabled": bool(layer.get("ctu_enabled")),
                    "status": "timeout",
                    "reason": f"layer exceeded {timeout_seconds} seconds",
                    "command": command,
                    "exit_code": None,
                    "stdout": exc.stdout or "",
                    "stderr": exc.stderr or "",
                }
            else:
                stats = _metadata_stats(report_dir)
                if completed.returncode != 0 or not stats["metadata_valid"]:
                    status = "execution_failure"
                elif stats["failed_translation_units"]:
                    status = "execution_failure"
                else:
                    status = "complete"
                execution = {
                    "layer_id": layer_id,
                    "ctu_enabled": bool(layer.get("ctu_enabled")),
                    "status": status,
                    "reason": (
                        "all selected translation units completed"
                        if status == "complete"
                        else "CodeChecker failed or left translation units unanalyzed"
                    ),
                    "command": command,
                    "exit_code": completed.returncode,
                    "stdout": completed.stdout,
                    "stderr": completed.stderr,
                    **stats,
                }
        target_path = layer.get("target_path")
        if isinstance(target_path, str):
            validate_relative_path(target_path)
            target_analysis[target_path] = {
                "layer_id": layer_id,
                "status": execution["status"],
                "reason": execution["reason"],
            }
        executions.append(execution)

    return {
        "schema": "agent-runtime/layered-ctu-execution/v1",
        "created_at": utc_now(),
        "plan": str(plan_path.resolve()),
        "plan_digest": plan.get("plan_digest"),
        "continue_after_layer_failure": True,
        "executions": executions,
        "target_analysis": target_analysis,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("plan", type=Path)
    parser.add_argument("--codechecker", default="CodeChecker")
    parser.add_argument("--timeout-seconds", type=int, default=1800)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    result = execute_plan(
        args.plan,
        codechecker=args.codechecker,
        timeout_seconds=args.timeout_seconds,
    )
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(
        json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    print(args.output.resolve())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
