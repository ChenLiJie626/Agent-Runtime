"""Persistent ASan attempt recording; no synthetic project confirmations."""

import importlib.util
import json
import subprocess
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location(
    "run_public_uaf_asan", ROOT / "tools/run_public_uaf_asan.py"
)
ASAN = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(ASAN)


class PublicUafAsanCaptureTests(unittest.TestCase):
    def test_preserves_nonzero_exit_command_and_log_hashes(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)

            def run(command, **kwargs):
                kwargs["stderr"].write("linker failed\n")
                return SimpleNamespace(returncode=1)

            with patch.object(ASAN.subprocess, "run", side_effect=run):
                value = ASAN.capture(["compiler", "repro.cpp"], root, "build", 1)
            self.assertEqual(value["exit_code"], 1)
            self.assertEqual(value["command"], ["compiler", "repro.cpp"])
            self.assertEqual(
                value["stderr_sha256"], ASAN.sha(root / "build.stderr.log")
            )
            self.assertEqual(json.loads((root / "build.json").read_text()), value)

    def test_timeout_cleans_named_container_and_preserves_record(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            with patch.object(
                ASAN.subprocess,
                "run",
                side_effect=[
                    subprocess.TimeoutExpired("docker", 1),
                    SimpleNamespace(returncode=0),
                ],
            ) as run:
                value = ASAN.capture(
                    ["docker", "run", "--name", "owned-attempt"], root, "run", 1
                )
            self.assertEqual(value["status"], "timeout")
            self.assertIsNone(value["exit_code"])
            self.assertEqual(
                run.call_args_list[-1].args[0], ["docker", "rm", "-f", "owned-attempt"]
            )

    def test_nonbaseline_revision_is_rejected_before_execution(self):
        with tempfile.TemporaryDirectory() as directory, patch.object(
            ASAN.subprocess, "check_output", return_value="different revision\n"
        ):
            with self.assertRaisesRegex(ValueError, "frozen baseline"):
                ASAN.run(Path(directory), Path(directory) / "out", "image")


if __name__ == "__main__":
    unittest.main()
