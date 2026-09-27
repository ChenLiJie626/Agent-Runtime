"""Selection and serialization tests for the layered CTU planner."""

import json
import importlib.util
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from agent_runtime.adapters.compilation_database import ClangCompilationDatabase
from agent_runtime.adapters.layered_ctu import LayeredCtuPlanner


ROOT = Path(__file__).resolve().parents[1]
RUNNER_SPEC = importlib.util.spec_from_file_location(
    "run_layered_ctu", ROOT / "tools/run_layered_ctu.py"
)
assert RUNNER_SPEC is not None and RUNNER_SPEC.loader is not None
RUNNER = importlib.util.module_from_spec(RUNNER_SPEC)
sys.modules[RUNNER_SPEC.name] = RUNNER
RUNNER_SPEC.loader.exec_module(RUNNER)


class LayeredCtuPlannerTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name).resolve()
        (self.root / "build").mkdir()
        (self.root / "src").mkdir()
        (self.root / "include/detail").mkdir(parents=True)
        (self.root / "src/direct.cpp").write_text("int direct() { return 1; }\n")
        (self.root / "src/uses_target.cpp").write_text(
            '#include "public.h"\nint use_target() { return target(); }\n'
        )
        (self.root / "src/unrelated.cpp").write_text(
            "int unrelated() { return 0; }\n"
        )
        (self.root / "include/public.h").write_text('#include "detail/target.h"\n')
        (self.root / "include/detail/target.h").write_text(
            "inline int target() { return 2; }\n"
        )
        entries = [self._command(path) for path in (
            "src/direct.cpp", "src/uses_target.cpp", "src/unrelated.cpp",
        )]
        self.database = ClangCompilationDatabase(entries, project_root=self.root)
        self.planner = LayeredCtuPlanner(self.database)

    def tearDown(self):
        self.temp.cleanup()

    def _command(self, path):
        return {
            "directory": str(self.root / "build"),
            "file": str(self.root / path),
            "arguments": [
                "clang++", "-I", str(self.root / "include"),
                "-c", str(self.root / path), "-o", f"{Path(path).stem}.o",
            ],
        }

    def test_cpp_target_selects_its_direct_translation_unit(self):
        plan = self.planner.plan(["src/direct.cpp"])

        selection = plan.selections[0]
        self.assertEqual(selection.direct_translation_units, ("src/direct.cpp",))
        self.assertEqual(selection.including_translation_units, ())
        self.assertEqual(plan.targeted_ctu.translation_units, ("src/direct.cpp",))
        self.assertEqual(plan.non_ctu.translation_units, self.database.files)

    def test_header_target_selects_tu_through_transitive_include(self):
        plan = self.planner.plan(["include/detail/target.h"])

        selection = plan.selections[0]
        self.assertEqual(selection.direct_translation_units, ())
        self.assertEqual(selection.including_translation_units, ("src/uses_target.cpp",))
        self.assertEqual(selection.include_chains[0].paths, (
            "src/uses_target.cpp", "include/public.h", "include/detail/target.h",
        ))
        self.assertEqual(plan.targeted_ctu.translation_units, ("src/uses_target.cpp",))

    def test_bundle_contains_runnable_codechecker_inputs_for_both_layers(self):
        plan = self.planner.plan([
            "src/direct.cpp", "include/detail/target.h",
        ])
        output = self.root / "analysis-plan"
        plan_path = self.planner.write_bundle(plan, output)

        value = json.loads(plan_path.read_text())
        layers = {item["layer_id"]: item for item in value["layers"]}
        self.assertEqual(layers["non-ctu-full"]["translation_unit_count"], 3)
        self.assertNotIn("--ctu", layers["non-ctu-full"]["codechecker_arguments"])
        self.assertEqual(layers["targeted-ctu"]["translation_unit_count"], 2)
        self.assertIn("--ctu", layers["targeted-ctu"]["codechecker_arguments"])
        self.assertEqual(len(value["ctu_slices"]), 2)
        self.assertEqual(len(value["execution_order"]), 3)
        self.assertEqual(value["execution_order"][0], "non-ctu-full")
        self.assertTrue(value["failure_policy"]["continue_after_layer_failure"])
        self.assertTrue(all(
            item["failure_isolation"] == "target-slice"
            and item["target_path"] in {
                "src/direct.cpp", "include/detail/target.h",
            }
            for item in value["ctu_slices"]
        ))

        full_database = json.loads(
            (output / "non-ctu.compile_commands.json").read_text()
        )
        targeted_database = json.loads(
            (output / "targeted-ctu.compile_commands.json").read_text()
        )
        self.assertEqual(len(full_database), 3)
        self.assertEqual(
            {Path(item["file"]).relative_to(self.root).as_posix()
             for item in targeted_database},
            {"src/direct.cpp", "src/uses_target.cpp"},
        )
        self.assertTrue(all(Path(item["directory"]).is_absolute()
                            for item in targeted_database))
        isolated_databases = sorted(output.glob("targeted-ctu-0??.compile_commands.json"))
        self.assertEqual(len(isolated_databases), 2)
        self.assertTrue(all(json.loads(path.read_text()) for path in isolated_databases))

    def test_executor_continues_after_one_ctu_slice_fails(self):
        plan_path = self.planner.write_bundle(
            self.planner.plan(["src/direct.cpp", "include/detail/target.h"]),
            self.root / "isolated-run",
        )
        calls = []

        def run(command, **_kwargs):
            calls.append(command)
            report_dir = Path(command[command.index("--output") + 1])
            # Fail only the first CTU slice.  The final slice must still run.
            if len(calls) == 2:
                return SimpleNamespace(returncode=1, stdout="", stderr="AST import failed")
            report_dir.mkdir(parents=True)
            (report_dir / "metadata.json").write_text(json.dumps({
                "tools": [{
                    "analyzers": {"clangsa": {"analyzer_statistics": {
                        "successful": 1,
                        "failed": 0,
                        "successful_sources": [],
                        "failed_sources": [],
                    }}},
                }],
            }))
            return SimpleNamespace(returncode=0, stdout="ok", stderr="")

        with mock.patch.object(RUNNER.shutil, "which", return_value="/bin/CodeChecker"), \
                mock.patch.object(RUNNER.subprocess, "run", side_effect=run):
            result = RUNNER.execute_plan(plan_path)

        self.assertEqual(len(calls), 3)
        self.assertEqual(
            [item["status"] for item in result["executions"]],
            ["complete", "execution_failure", "complete"],
        )
        self.assertEqual(len(result["target_analysis"]), 2)


if __name__ == "__main__":
    unittest.main()
