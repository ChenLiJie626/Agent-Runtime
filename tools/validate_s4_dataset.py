"""Validate a SPEC 008 public C++ manifest and optionally verify downloads."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from agent_runtime.evaluation import (
    dataset_manifest_digest,
    load_json_object,
    verify_manifest_artifacts,
)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", type=Path, required=True)
    parser.add_argument("--verify-artifacts", action="store_true")
    parser.add_argument(
        "--max-artifact-mib", type=int, default=64,
        help="maximum bytes fetched per artifact, in MiB (default: 64)",
    )
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    if args.max_artifact_mib < 1:
        parser.error("--max-artifact-mib must be positive")

    manifest = load_json_object(args.manifest)
    result = {
        "status": "passed",
        "dataset_id": manifest.get("dataset_id"),
        "manifest_digest": dataset_manifest_digest(manifest),
        "sample_count": len(manifest["samples"]),
        "artifact_verification": (
            verify_manifest_artifacts(
                manifest, max_bytes=args.max_artifact_mib * 1024 * 1024,
            ) if args.verify_artifacts else []
        ),
    }
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(
            json.dumps(result, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
    print(json.dumps(result, ensure_ascii=False))


if __name__ == "__main__":
    main()
