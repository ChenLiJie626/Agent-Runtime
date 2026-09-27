"""Run the bundled C++ discovery pipeline with source-only evidence."""

from __future__ import annotations

import json
import tempfile
from pathlib import Path

from agent_runtime import (
    AnalysisPipeline,
    CppUnreachableDiscoverer,
    CppUnreachableEvaluator,
    DefectRuntime,
    EvidenceReviewExecutor,
    FixedSnapshot,
    SQLiteStore,
    analysis_profile_digest,
    cpp_unreachable_profile,
    cpp_unreachable_rule,
    digest,
)
from agent_runtime.adapters import FrozenSourceProgramQuery


def run(root: Path) -> dict:
    base = {"src/example.cpp": "int f() {\n  return 0;\n}\n"}
    head = {"src/example.cpp": "int f() {\n  return 1;\n  use();\n}\n"}
    rule = cpp_unreachable_rule()
    policy = digest({"operations": ["read_source"], "max_lines": 200})
    profile = cpp_unreachable_profile(policy)
    snapshot = FixedSnapshot(
        "example/project",
        "base..head",
        FrozenSourceProgramQuery.source_digest(head),
        analysis_profile_digest(rule, profile=profile),
        policy,
        {"base_commit": "base", "head_commit": "head"},
    )
    store = SQLiteStore(root / "runtime.sqlite3", root / "artifacts")
    try:
        result = AnalysisPipeline(
            DefectRuntime(store),
            rule=rule,
            profile=profile,
            discoverer=CppUnreachableDiscoverer(),
            backend=FrozenSourceProgramQuery(snapshot, head),
            evaluator=CppUnreachableEvaluator(),
            executor=EvidenceReviewExecutor(),
            owner_id="example-worker",
            working_dir=str(root),
        ).run(snapshot, base_sources=base, head_sources=head)
        return {
            "analysis_id": result.analysis_id,
            "candidates": [
                {
                    "candidate_id": item.candidate_id,
                    "task_id": item.task_id,
                    "status": item.status,
                    "blockers": list(item.decision.blockers) if item.decision else [],
                }
                for item in result.candidates
            ],
        }
    finally:
        store.close()


if __name__ == "__main__":
    with tempfile.TemporaryDirectory(prefix="cpp-unreachable-") as directory:
        print(json.dumps(run(Path(directory)), ensure_ascii=False, indent=2))
