"""Executable fixture checks for SPEC 005; no semantic proof is inferred."""

import importlib.util
import json
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location(
    "run_null_return_poc", ROOT / "tools" / "run_null_return_poc.py"
)
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)


@unittest.skipUnless(
    shutil.which("clang++") and shutil.which("git"), "needs clang++ and git"
)
class FixturePoC(unittest.TestCase):
    def test_two_commits_same_build_and_raw_baseline(self):
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "poc"
            manifest = MODULE.run(output, "clang++")
            base = manifest["sides"]["base"]
            head = manifest["sides"]["head"]
            self.assertNotEqual(base["commit"], head["commit"])
            second = MODULE.run(Path(directory) / "repeat", "clang++")
            self.assertEqual(
                [base["commit"], head["commit"]],
                [second["sides"][side]["commit"] for side in ("base", "head")],
            )
            self.assertEqual(manifest["coverage"]["completeness"], "partial")
            self.assertEqual(
                base["compile_database"]["normalized_sha256"],
                head["compile_database"]["normalized_sha256"],
            )
            self.assertEqual(
                base["source_sha256"]["consumer.cpp"],
                head["source_sha256"]["consumer.cpp"],
            )
            self.assertNotEqual(
                base["source_sha256"]["buffer.cpp"],
                head["source_sha256"]["buffer.cpp"],
            )
            changed = MODULE.git(
                output / "repo", "diff", "--name-only", base["commit"], head["commit"]
            )
            self.assertEqual(changed, "buffer.cpp")
            for side in (base, head):
                self.assertEqual(len(side["invocations"]), 4)
                for invocation in side["invocations"]:
                    self.assertEqual(invocation["status"], "complete")
                    for stream in ("stdout", "stderr"):
                        artifact = invocation[stream]
                        self.assertEqual(
                            MODULE.sha256(Path(artifact["path"]).read_bytes()),
                            artifact["sha256"],
                        )
                    if invocation.get("sarif"):
                        sarif = invocation["sarif"]
                        self.assertEqual(
                            MODULE.sha256(Path(sarif["path"]).read_bytes()),
                            sarif["sha256"],
                        )
                        self.assertIn(
                            "runs", json.loads(Path(sarif["path"]).read_text())
                        )

    def test_base_entry_and_head_guard_execute(self):
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "poc"
            MODULE.run(output, "clang++")
            compiler = shutil.which("clang++")
            base_binary = output / "base-main"
            subprocess.run(
                [
                    compiler,
                    "-std=c++17",
                    str(output / "base" / "buffer.cpp"),
                    str(output / "base" / "consumer.cpp"),
                    "-o",
                    str(base_binary),
                ],
                check=True,
                capture_output=True,
            )
            self.assertEqual(
                subprocess.run([str(base_binary)], check=False).returncode, 7
            )
            harness = output / "guard_harness.cpp"
            harness.write_text(
                '#define main fixture_main\n#include "head/consumer.cpp"\n'
                "#undef main\nint main() { return guardedProcess(0); }\n"
            )
            guard_binary = output / "guard-main"
            subprocess.run(
                [
                    compiler,
                    "-std=c++17",
                    str(output / "head" / "buffer.cpp"),
                    str(harness),
                    "-o",
                    str(guard_binary),
                ],
                check=True,
                capture_output=True,
            )
            self.assertEqual(
                subprocess.run([str(guard_binary)], check=False).returncode, 0
            )


if __name__ == "__main__":
    unittest.main()
