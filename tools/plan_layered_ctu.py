"""Create full non-CTU and defect-targeted CTU CodeChecker inputs."""

from __future__ import annotations

import argparse
from pathlib import Path

from agent_runtime.adapters.compilation_database import ClangCompilationDatabase
from agent_runtime.adapters.layered_ctu import LayeredCtuPlanner


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("compile_database", type=Path)
    parser.add_argument(
        "--project-root", type=Path,
        help="project root (defaults to the compile database directory)",
    )
    parser.add_argument(
        "--target", action="append", required=True,
        help="project-relative defect path; repeat for multiple defects",
    )
    parser.add_argument("--output-dir", type=Path, required=True)
    args = parser.parse_args()

    compile_database = args.compile_database.resolve()
    project_root = (args.project_root or compile_database.parent).resolve()
    database = ClangCompilationDatabase.from_file(
        compile_database, project_root=project_root,
    )
    planner = LayeredCtuPlanner(database)
    plan_path = planner.write_bundle(planner.plan(args.target), args.output_dir)
    print(plan_path)


if __name__ == "__main__":
    main()

