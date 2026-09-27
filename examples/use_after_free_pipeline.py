"""Detect the intentional UAF fixture through the reusable Runtime pipeline."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from agent_runtime import (
    AnalysisPipeline,
    ClangUseAfterFreeDiscoverer,
    CppUseAfterFreeEvaluator,
    DefectRuntime,
    EvidenceReviewExecutor,
    FixedSnapshot,
    SQLiteStore,
    analysis_profile_digest,
    cpp_use_after_free_profile,
    cpp_use_after_free_rule,
    digest,
)
from agent_runtime.adapters import (
    ClangCompilationDatabase,
    ClangDiagnosticProgramQuery,
    CodeCheckerCtuConfig,
    CompositeProgramQuery,
    FrozenSourceProgramQuery,
    run_codechecker_ctu_use_after_free_analysis,
    run_clang_use_after_free_analysis,
)

SOURCE_SUFFIXES = frozenset({".c", ".cc", ".cpp", ".cxx", ".h", ".hh", ".hpp", ".hxx"})
IMPLEMENTATION_SUFFIXES = frozenset({".c", ".cc", ".cpp", ".cxx"})


def load_sources(project_root: Path) -> dict[str, str]:
    sources = {
        path.relative_to(project_root).as_posix(): path.read_text(encoding="utf-8")
        for path in sorted(project_root.rglob("*"))
        if path.is_file() and path.suffix in SOURCE_SUFFIXES
    }
    if not sources:
        raise ValueError(f"no C/C++ sources under {project_root}")
    return sources


def run(
    project_root: Path,
    output_root: Path,
    *,
    compiler: str = "clang++",
    compile_commands: Path | None = None,
    ctu: bool = False,
    ctu_image: str = CodeCheckerCtuConfig().image,
) -> dict:
    project_root = project_root.resolve()
    output_root.mkdir(parents=True, exist_ok=True)
    sources = load_sources(project_root)
    compilation_database = (
        ClangCompilationDatabase.from_file(
            compile_commands.resolve(), project_root=project_root,
        )
        if compile_commands is not None else None
    )
    implementation_paths = (
        compilation_database.files if compilation_database is not None else tuple(
            path for path in sorted(sources)
            if Path(path).suffix in IMPLEMENTATION_SUFFIXES
        )
    )
    if ctu:
        if compilation_database is None:
            raise ValueError("--ctu requires --compile-commands")
        bundle = run_codechecker_ctu_use_after_free_analysis(
            sources,
            compilation_database=compilation_database,
            config=CodeCheckerCtuConfig(
                image=ctu_image, workspace_root=output_root,
            ),
        )
    else:
        bundle = run_clang_use_after_free_analysis(
            sources,
            paths=implementation_paths,
            compiler=compiler,
            compilation_database=compilation_database,
        )
    rule = cpp_use_after_free_rule()
    policy = digest({
        "operations": ["read_source", "read_clang_diagnostic"],
            "analyzer_profile": bundle.command_profile_digest,
            "compile_commands": bundle.compilation_database_digest,
    })
    profile = cpp_use_after_free_profile(policy)
    snapshot = FixedSnapshot(
        "fixture/use-after-free-simple",
        "full-project",
        FrozenSourceProgramQuery.source_digest(sources),
        analysis_profile_digest(rule, profile=profile),
        policy,
        {
            "project_root": project_root.name,
            "compiler_version": bundle.compiler_version,
        },
    )
    backend = CompositeProgramQuery(
        FrozenSourceProgramQuery(snapshot, sources),
        ClangDiagnosticProgramQuery(snapshot, bundle),
    )
    store = SQLiteStore(output_root / "runtime.sqlite3", output_root / "artifacts")
    try:
        result = AnalysisPipeline(
            DefectRuntime(store),
            rule=rule,
            profile=profile,
            discoverer=ClangUseAfterFreeDiscoverer(bundle),
            backend=backend,
            evaluator=CppUseAfterFreeEvaluator(),
            executor=EvidenceReviewExecutor(),
            owner_id="use-after-free-example",
            working_dir=str(output_root),
        ).run(snapshot, base_sources=sources, head_sources=sources)
        output = {
            "analysis_id": result.analysis_id,
            "snapshot_digest": snapshot.snapshot_digest,
            "compiler_version": bundle.compiler_version,
            "diagnostic_count": len(bundle.diagnostics),
            "compilation_database_digest": bundle.compilation_database_digest,
            "ctu_mode": bundle.ctu_mode,
            "candidates": [
                {
                    "candidate_id": item.candidate_id,
                    "task_id": item.task_id,
                    "status": item.status,
                    "material": item.material,
                    "supporting_evidence_ids": [
                        ref.evidence_id for ref in item.decision.support_refs
                    ] if item.decision else [],
                    "blockers": list(item.decision.blockers) if item.decision else [],
                }
                for item in result.candidates
            ],
            "report": result.report,
        }
        (output_root / "result.json").write_text(
            json.dumps(output, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        return output
    finally:
        store.close()


def main() -> int:
    repository_root = Path(__file__).resolve().parents[1]
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--project",
        type=Path,
        default=repository_root / "evaluation/fixtures/use-after-free-simple",
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=repository_root / ".poc/use-after-free-runtime",
    )
    parser.add_argument("--compiler", default="clang++")
    parser.add_argument("--compile-commands", type=Path)
    parser.add_argument("--ctu", action="store_true")
    parser.add_argument("--ctu-image", default=CodeCheckerCtuConfig().image)
    args = parser.parse_args()
    result = run(
        args.project,
        args.output,
        compiler=args.compiler,
        compile_commands=args.compile_commands,
        ctu=args.ctu,
        ctu_image=args.ctu_image,
    )
    print(json.dumps({
        "analysis_id": result["analysis_id"],
        "compiler_version": result["compiler_version"],
        "diagnostic_count": result["diagnostic_count"],
        "compilation_database_digest": result["compilation_database_digest"],
        "ctu_mode": result["ctu_mode"],
        "candidates": result["candidates"],
        "result_path": str((args.output / "result.json").resolve()),
    }, ensure_ascii=False, indent=2))
    return 0 if result["candidates"] and all(
        item["status"] == "confirmed" for item in result["candidates"]
    ) else 1


if __name__ == "__main__":
    raise SystemExit(main())
