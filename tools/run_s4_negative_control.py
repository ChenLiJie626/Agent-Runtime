"""Create a no-findings control run that proves misses remain in the denominator."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from agent_runtime.evaluation import dataset_manifest_digest, load_json_object


def build_run(manifest: dict) -> dict:
    return {
        "schema_version": {"major": 1, "minor": 0},
        "run_id": f"negative-control:{manifest['dataset_id']}",
        "dataset_id": manifest["dataset_id"],
        "manifest_digest": dataset_manifest_digest(manifest),
        "created_at": manifest["frozen_at"],
        "bindings": {
            "code_version": "negative-control-v1",
            "rule": {"id": "control.no-findings", "version": "1"},
            "backend": {"id": "none", "version": "1"},
            "prompt_digests": {},
            "model_route": "not_used",
            "budget": {
                "wall_time_seconds": 0,
                "query_limit": 0,
                "token_limit": 0,
                "cost_limit_usd": 0
            },
            "top_k": 10
        },
        "sample_results": [
            {
                "sample_id": sample["sample_id"],
                "analysis_status": "not_run",
                "coverage": {
                    "total_targets": 1,
                    "analyzed_targets": 0,
                    "total_translation_units": sample.get("surface", {}).get("files", 0),
                    "analyzed_translation_units": 0,
                    "lines_scanned": 0,
                    "lines_changed": sample.get("surface", {}).get("changed_lines", 0),
                    "omissions": ["negative control intentionally performs no analysis"]
                },
                "resource": {
                    "duration_seconds": 0,
                    "query_count": 0,
                    "model_tokens": 0,
                    "cost_usd": 0
                },
                "known_defects": [
                    {
                        "defect_id": defect["defect_id"],
                        "evaluable": True,
                        "candidate_status": "not_run",
                        "candidate_ids": [],
                        "evidence_status": "not_applicable",
                        "verdict": "not_run",
                        "excluded": False
                    }
                    for defect in sample["known_defects"]
                ],
                "candidates": [],
                "alerts": []
            }
            for sample in manifest["samples"]
        ]
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    run = build_run(load_json_object(args.manifest))
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(
        json.dumps(run, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    print(json.dumps({"status": "passed", "output": str(args.output.resolve())}))


if __name__ == "__main__":
    main()
