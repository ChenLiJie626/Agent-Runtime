"""Per-target CTU provenance, failure containment and source identity tests."""

import importlib.util
import json
import plistlib
import sys
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest.mock import patch

from agent_runtime.errors import InvalidInput

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))
SPEC = importlib.util.spec_from_file_location(
    "run_targeted_ctu", ROOT / "tools/run_targeted_ctu.py"
)
RUNNER = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(RUNNER)
sys.path.pop(0)


class TargetedCtuRunnerTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.selection = {
            "target_path": "src/a.cpp",
            "slice_dir": "a",
            "translation_units": ["src/a.cpp", "src/b.cpp"],
        }
        (self.root / "reports").mkdir()

    def tearDown(self):
        self.temp.cleanup()

    def metadata(self, successful, failed):
        tool = {
            "command": ["CodeChecker", "--ctu-ast-mode", "parse-on-demand", "-j", "1"],
            "timestamps": {"begin": 10, "end": 25},
            "analyzers": {
                "clangsa": {
                    "analyzer_statistics": {
                        "successful": len(successful),
                        "failed": len(failed),
                        "successful_sources": [
                            "/workspace/source/" + x for x in successful
                        ],
                        "failed_sources": ["/workspace/source/" + x for x in failed],
                    }
                }
            },
        }
        (self.root / "reports/metadata.json").write_text(json.dumps({"tools": [tool]}))

    def test_complete_requires_all_selected_source_identities(self):
        self.metadata(["src/a.cpp", "src/b.cpp"], [])
        result = RUNNER.collect_execution(
            self.root, self.selection, self.root, "example", imported=True
        )
        self.assertEqual(result["status"], "complete")
        self.assertIsNone(result["exit_code"])
        self.assertEqual(result["duration_seconds"], 15)
        self.assertEqual(result["ctu_ast_mode"], "parse-on-demand")
        self.assertEqual(result["unfinished_sources"], [])

    def test_partial_does_not_hide_unfinished_tus(self):
        self.metadata(["src/a.cpp"], [])
        result = RUNNER.collect_execution(
            self.root, self.selection, self.root, "example"
        )
        self.assertEqual(result["status"], "partial")
        self.assertEqual(result["unfinished_sources"], ["src/b.cpp"])

    def test_diagnostics_survive_partial_execution(self):
        self.metadata(["src/a.cpp"], ["src/b.cpp"])
        report = {
            "files": ["/workspace/source/src/a.cpp"],
            "diagnostics": [
                {
                    "description": "Use of memory after it is freed",
                    "check_name": "cplusplus.NewDelete",
                    "location": {"file": 0, "line": 5, "col": 3},
                    "path": [],
                }
            ],
        }
        (self.root / "reports/a.plist").write_bytes(plistlib.dumps(report))
        result = RUNNER.collect_execution(
            self.root, self.selection, self.root, "example"
        )
        self.assertEqual(result["status"], "partial")
        self.assertEqual(result["diagnostics"][0]["path"], "src/a.cpp")
        self.assertEqual(len(result["diagnostics"][0]["report_sha256"]), 64)

    def test_failure_has_no_invented_success(self):
        result = RUNNER.collect_execution(
            self.root, self.selection, self.root, "example", exit_code=137
        )
        self.assertEqual(result["status"], "execution_failure")
        self.assertEqual(
            result["unfinished_sources"], self.selection["translation_units"]
        )

    def test_rejects_metadata_from_another_slice(self):
        self.metadata(["src/other.cpp"], [])
        with self.assertRaises(InvalidInput):
            RUNNER.collect_execution(self.root, self.selection, self.root, "example")

    def test_source_counts_must_agree_with_identities(self):
        self.metadata(["src/a.cpp", "src/b.cpp"], [])
        p = self.root / "reports/metadata.json"
        value = json.loads(p.read_text())
        value["tools"][0]["analyzers"]["clangsa"]["analyzer_statistics"][
            "successful"
        ] = 3
        p.write_text(json.dumps(value))
        self.assertEqual(
            RUNNER.collect_execution(self.root, self.selection, self.root, "example")[
                "status"
            ],
            "execution_failure",
        )

    def test_interrupted_run_recovers_only_attested_per_report_results(self):
        reports = self.root / "reports"
        (reports / "a.plist").write_bytes(
            plistlib.dumps({"files": [], "diagnostics": []})
        )
        (reports / "a.plist.source").write_text("/workspace/source/src/a.cpp\n")
        (reports / "interrupted.plist").write_bytes(b"incomplete plist")
        (reports / "failed").mkdir()
        archive = reports / "failed/b.cpp_clangsa_hash.plist_CTU_compile_error.zip"
        with zipfile.ZipFile(archive, "w") as value:
            value.writestr(
                "compilation_database.json",
                json.dumps(
                    [
                        {
                            "file": "/workspace/source/src/b.cpp",
                            "directory": "/workspace/source",
                        }
                    ]
                ),
            )
        result = RUNNER.collect_execution(
            self.root, self.selection, self.root, "example"
        )
        self.assertEqual(result["status"], "partial")
        self.assertFalse(result["metadata_valid"])
        self.assertEqual(result["successful_sources"], ["src/a.cpp"])
        self.assertEqual(result["failed_sources"], ["src/b.cpp"])
        self.assertEqual(len(result["per_report_artifacts"]), 2)

    def test_container_executor_small_first_and_continues_after_failure(self):
        source = self.root / "source"
        source.mkdir()
        slices = []
        for index, count in enumerate((3, 1)):
            directory = self.root / str(index)
            directory.mkdir()
            target = f"src/target{index}.cpp"
            (source / "src").mkdir(exist_ok=True)
            (source / target).write_text("int x;")
            units = [f"src/{index}_{i}.cpp" for i in range(count)]
            database = directory / "compile_commands.container.json"
            database.write_text(
                json.dumps(
                    [
                        {
                            "directory": "/workspace/source",
                            "file": "/workspace/source/" + x,
                            "arguments": ["clang++", "-c", x],
                        }
                        for x in units
                    ]
                )
            )
            slices.append(
                {
                    "target_path": target,
                    "slice_dir": str(index),
                    "translation_unit_count": count,
                    "translation_units": units,
                    "compile_database": database.name,
                    "compile_database_sha256": RUNNER._sha(database),
                }
            )
        manifest = self.root / "slices.json"
        manifest.write_text(
            json.dumps(
                {
                    "schema": "agent-runtime/container-ctu-slices/v1",
                    "container_root": "/workspace/source",
                    "slices": slices,
                }
            )
        )
        commands = []

        def run(command, **kwargs):
            from types import SimpleNamespace

            if command[1] == "run":
                commands.append(command)
                if len(commands) == 1:
                    import subprocess

                    raise subprocess.TimeoutExpired(command, 1)
                return SimpleNamespace(returncode=125)
            return SimpleNamespace(returncode=0, stdout="{}", stderr="")

        with patch.object(
            RUNNER.subprocess,
            "check_output",
            side_effect=["sha256:image", "revision", b""],
        ), patch.object(RUNNER.subprocess, "run", side_effect=run):
            result = RUNNER.execute_slices(
                manifest,
                source=source,
                project_id="example",
                image="image",
                timeout_seconds=1,
            )
        self.assertEqual(
            [x["target_path"] for x in result["executions"]],
            ["src/target1.cpp", "src/target0.cpp"],
        )
        self.assertEqual(
            [x["status"] for x in result["executions"]],
            ["timeout", "execution_failure"],
        )
        self.assertIn("--network", commands[0])
        self.assertEqual(commands[0][commands[0].index("-j") + 1], "1")
        self.assertEqual(len(list(self.root.glob("*/attempt-*/execution.json"))), 2)

        retry_commands = []

        def retry_run(command, **kwargs):
            from types import SimpleNamespace

            if command[1] == "run":
                retry_commands.append(command)
                return SimpleNamespace(returncode=125)
            return SimpleNamespace(returncode=0, stdout="{}", stderr="")

        with patch.object(
            RUNNER.subprocess,
            "check_output",
            side_effect=["sha256:image", "revision", b""],
        ), patch.object(RUNNER.subprocess, "run", side_effect=retry_run):
            retried = RUNNER.execute_slices(
                manifest,
                source=source,
                project_id="example",
                image="image",
                timeout_seconds=1,
                targets=("src/target0.cpp",),
            )
        self.assertEqual(
            [x["status"] for x in retried["executions"]],
            ["timeout", "execution_failure"],
        )
        self.assertEqual(len(retry_commands), 1)


if __name__ == "__main__":
    unittest.main()
