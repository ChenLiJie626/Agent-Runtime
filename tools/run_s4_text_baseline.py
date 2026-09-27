"""Run a deterministic lexical unreachable-code baseline on frozen C++ surfaces.

The scanner reads verified source archives in memory and never executes or
extracts upstream content.  Findings remain ``inconclusive`` because lexical
structure alone is not a semantic reachability proof.
"""

from __future__ import annotations

import argparse
import io
import json
import tarfile
import time
from pathlib import Path, PurePosixPath

from agent_runtime import added_line_numbers, find_unreachable_after_return
from agent_runtime.codec import bytes_digest, digest, utc_now, validate_relative_path
from agent_runtime.errors import InvalidInput
from agent_runtime.evaluation import (
    dataset_manifest_digest,
    fetch_verified_artifact,
    load_json_object,
    validate_evaluation_run,
)

IMPLEMENTATION_SUFFIXES = {".c", ".cc", ".cpp", ".cxx"}


def read_surface_files(archive_bytes: bytes, paths: list[str]) -> dict[str, str]:
    """Read declared regular files without extracting archive entries."""

    wanted = set(paths)
    for path in wanted:
        validate_relative_path(path)
    found: dict[str, str] = {}
    try:
        archive = tarfile.open(fileobj=io.BytesIO(archive_bytes), mode="r:gz")
    except tarfile.TarError as exc:
        raise InvalidInput(f"invalid source archive: {exc}") from exc
    with archive:
        for member in archive.getmembers():
            if not member.isfile() or member.size > 4 * 1024 * 1024:
                continue
            member_path = PurePosixPath(member.name)
            for path in wanted:
                target_parts = PurePosixPath(path).parts
                # GitHub source archives have exactly one root directory. A
                # suffix match could substitute a nested duplicate file.
                if (len(member_path.parts) != len(target_parts) + 1
                        or member_path.parts[1:] != target_parts):
                    continue
                if path in found:
                    raise InvalidInput(f"duplicate source archive member: {path}")
                stream = archive.extractfile(member)
                if stream is None:
                    raise InvalidInput(f"cannot read archive member: {member.name}")
                body = stream.read(4 * 1024 * 1024 + 1)
                if len(body) > 4 * 1024 * 1024:
                    raise InvalidInput(f"surface file exceeds limit: {path}")
                try:
                    found[path] = body.decode("utf-8")
                except UnicodeDecodeError as exc:
                    raise InvalidInput(f"surface file is not UTF-8: {path}") from exc
                break
    missing = wanted - found.keys()
    if missing:
        raise InvalidInput(f"source archive misses declared surface files: {sorted(missing)}")
    return found


def build_run(manifest: dict) -> dict:
    started = time.monotonic()
    sample_results = []
    for sample in manifest["samples"]:
        base_artifact = next(
            artifact for artifact in sample["artifacts"]
            if artifact["role"] == "base_source"
        )
        head_artifact = next(
            artifact for artifact in sample["artifacts"]
            if artifact["role"] == "head_source"
        )
        base_archive_bytes, _ = fetch_verified_artifact(base_artifact)
        head_archive_bytes, _ = fetch_verified_artifact(head_artifact)
        base_sources = read_surface_files(
            base_archive_bytes, sample["surface"]["paths"]
        )
        sources = read_surface_files(
            head_archive_bytes, sample["surface"]["paths"]
        )
        findings = [
            finding
            for path, text in sources.items()
            for finding in find_unreachable_after_return(text, path)
            if finding["return_line"] in added_line_numbers(base_sources[path], text)
        ]
        defect_matches: dict[str, list[str]] = {}
        for defect in sample["known_defects"]:
            matches = [
                finding["candidate_id"] for finding in findings
                if finding["path"] == defect["location"]["path"]
                and finding["return_line"] == defect["location"]["line"]
            ]
            defect_matches[defect["defect_id"]] = matches
        candidate_known_ids = {
            candidate_id: defect_id
            for defect_id, candidate_ids in defect_matches.items()
            for candidate_id in candidate_ids
        }
        translation_units = sum(
            PurePosixPath(path).suffix in IMPLEMENTATION_SUFFIXES for path in sources
        )
        sample_results.append({
            "sample_id": sample["sample_id"],
            "analysis_status": "complete",
            "coverage": {
                "total_targets": 1,
                "analyzed_targets": 1,
                "total_translation_units": translation_units,
                "analyzed_translation_units": translation_units,
                "lines_scanned": sum(len(text.splitlines()) for text in sources.values()),
                "lines_changed": sample["surface"]["changed_lines"],
                "omissions": [
                    "lexical scanning does not prove semantic reachability",
                    "files outside the frozen PR surface were not scanned",
                    "only return statements added or replaced by the PR were eligible"
                ]
            },
            "resource": {
                "duration_seconds": round(time.monotonic() - started, 6),
                "query_count": 0,
                "model_tokens": 0,
                "cost_usd": 0
            },
            "known_defects": [
                {
                    "defect_id": defect["defect_id"],
                    "evaluable": True,
                    "candidate_status": (
                        "matched" if defect_matches[defect["defect_id"]] else "missed"
                    ),
                    "candidate_ids": defect_matches[defect["defect_id"]],
                    "evidence_status": (
                        "partial" if defect_matches[defect["defect_id"]] else "missing"
                    ),
                    "verdict": (
                        "inconclusive" if defect_matches[defect["defect_id"]]
                        else "missed"
                    ),
                    "excluded": False
                }
                for defect in sample["known_defects"]
            ],
            "candidates": [
                {
                    "candidate_id": finding["candidate_id"],
                    "known_defect_id": candidate_known_ids.get(finding["candidate_id"]),
                    "status": "inconclusive",
                    "location": {
                        "path": finding["path"],
                        "line": finding["return_line"]
                    },
                    "evidence": {
                        "following_line": finding["following_line"],
                        "return_text": finding["return_text"],
                        "following_text": finding["following_text"]
                    }
                }
                for finding in findings
            ],
            "alerts": []
        })
    script_digest = bytes_digest(Path(__file__).read_bytes())
    run = {
        "schema_version": {"major": 1, "minor": 0},
        "run_id": f"text-unreachable:{manifest['dataset_id']}:{script_digest[:12]}",
        "dataset_id": manifest["dataset_id"],
        "manifest_digest": dataset_manifest_digest(manifest),
        "created_at": utc_now(),
        "bindings": {
            "code_version": script_digest,
            "rule": {"id": "cpp.lexical-unreachable-after-return", "version": "1"},
            "backend": {"id": "python-text-scan", "version": "1"},
            "prompt_digests": {},
            "model_route": "not_used",
            "budget": {
                "wall_time_seconds": 120,
                "query_limit": 0,
                "token_limit": 0,
                "cost_limit_usd": 0
            },
            "top_k": 10
        },
        "sample_results": sample_results
    }
    validate_evaluation_run(run, manifest)
    return run


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
    print(json.dumps({
        "status": "passed",
        "run_id": run["run_id"],
        "candidate_count": sum(len(item["candidates"]) for item in run["sample_results"]),
        "output": str(args.output.resolve()),
    }, ensure_ascii=False))


if __name__ == "__main__":
    main()
