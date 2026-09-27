"""Pure boundary tests for the isolated public-source build runner."""

import importlib.util
import io
import json
import tarfile
import tempfile
import unittest
from pathlib import Path

from agent_runtime.errors import InvalidInput

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location(
    "run_s4_container_build", ROOT / "tools/run_s4_container_build.py",
)
BUILD = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(BUILD)


def archive(entries):
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w:gz") as output:
        for name, body, kind in entries:
            item = tarfile.TarInfo(name)
            item.type = kind
            if kind == tarfile.REGTYPE:
                item.size = len(body)
                output.addfile(item, io.BytesIO(body))
            else:
                item.linkname = body.decode()
                output.addfile(item)
    return buffer.getvalue()


class S4BuildTests(unittest.TestCase):
    def test_archive_extraction_accepts_regular_files_only(self):
        with tempfile.TemporaryDirectory() as directory:
            destination = Path(directory) / "source"
            digests = BUILD.extract_verified_archive(
                archive([("root/src/a.cpp", b"int main() {}\n", tarfile.REGTYPE)]),
                destination,
            )
            self.assertEqual((destination / "src/a.cpp").read_text(), "int main() {}\n")
            self.assertEqual(set(digests), {"src/a.cpp"})

    def test_archive_extraction_rejects_links_and_traversal(self):
        for name, body, kind in (
            ("root/link", b"/etc/passwd", tarfile.SYMTYPE),
            ("root/../escape", b"bad", tarfile.REGTYPE),
            ("other/file", b"bad", tarfile.REGTYPE),
        ):
            with self.subTest(name=name), tempfile.TemporaryDirectory() as directory:
                with self.assertRaises(InvalidInput):
                    BUILD.extract_verified_archive(
                        archive([("root/a", b"ok", tarfile.REGTYPE), (name, body, kind)]),
                        Path(directory) / "source",
                    )

    def test_compile_database_normalization_is_relocatable_and_strict(self):
        value = [{
            "directory": "/workspace/build", "file": "/workspace/src/a.cpp",
            "arguments": ["clang++-14", "-I/workspace/include", "-c", "/workspace/src/a.cpp"],
        }]
        normalized = BUILD.normalize_compile_database(value)
        self.assertNotIn("/workspace", json.dumps(normalized))
        self.assertIn("$SOURCE/src/a.cpp", json.dumps(normalized))
        for invalid in ([], [{"file": "a.cpp", "directory": "."}],
                        [{"file": "a.cpp", "directory": ".", "arguments": [], "command": "cc"}]):
            with self.assertRaises(InvalidInput):
                BUILD.normalize_compile_database(invalid)

    def test_build_report_binds_all_referenced_raw_artifacts(self):
        manifest = json.loads(
            (ROOT / "evaluation/manifests/public-cpp-smoke-v1.json").read_text()
        )
        sample = manifest["samples"][1]
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            stdout = root / "stdout"
            stderr = root / "stderr"
            packages = root / "packages.lock"
            compile_db = root / "compile_commands.json"
            stdout.write_bytes(b"configured\n")
            stderr.write_bytes(b"failed at source\n")
            packages.write_bytes(b"clang-14:arm64\t1:14.0.0-1ubuntu1.1\n")
            sample["build"]["dependency_digest"] = BUILD.bytes_digest(packages.read_bytes())
            database = [{
                "directory": "/workspace/build", "file": "/workspace/src/api/hocrrenderer.cpp",
                "arguments": ["clang++-14", "-c", "/workspace/src/api/hocrrenderer.cpp"],
            }]
            compile_db.write_text(json.dumps(database))
            raw_compile_db = compile_db.read_bytes()
            report = {
                "dataset_id": manifest["dataset_id"],
                "manifest_digest": BUILD.dataset_manifest_digest(manifest),
                "sample_id": sample["sample_id"], "head_commit": sample["head_commit"],
                "head_artifact_sha256": next(item["sha256"] for item in sample["artifacts"]
                                               if item["role"] == "head_source"),
                "build": {
                    "variant_id": sample["build"]["variant_id"], "status": "build_failure",
                    "recipe_digest": sample["build"]["recipe_digest"],
                    "recipe_matches_manifest": True,
                    "manifest_dependency_digest": sample["build"]["dependency_digest"],
                    "dependency_lock_path": sample["build"].get("dependency_lock_path"),
                    "dependency_lock_matches_manifest": True,
                    "instrumentation_environment": {"CMAKE_EXPORT_COMPILE_COMMANDS": "ON"},
                    "commands": [{
                        "manifest_command": sample["build"]["commands"][0],
                        "network": "none", "status": "failed",
                        "stdout_path": str(stdout), "stdout_sha256": BUILD.bytes_digest(stdout.read_bytes()),
                        "stderr_path": str(stderr), "stderr_sha256": BUILD.bytes_digest(stderr.read_bytes()),
                    }],
                },
                "container": {
                    "network_during_build": "none", "credentials_mounted": False,
                    "os": "linux", "image_id": "sha256:" + "a" * 64,
                    "package_lock_path": str(packages),
                    "package_lock_sha256": BUILD.bytes_digest(packages.read_bytes()),
                    "package_count": 1,
                },
                "compile_database": {
                    "path": str(compile_db), "sha256": BUILD.bytes_digest(raw_compile_db),
                    "normalized_sha256": BUILD.digest(BUILD.normalize_compile_database(database)),
                    "translation_units": 1,
                },
            }
            report_path = root / "report.json"
            report_path.write_text(json.dumps(report))
            self.assertIn(sample["sample_id"], BUILD.load_build_report(report_path, manifest))
            stderr.write_bytes(b"tampered")
            with self.assertRaises(InvalidInput):
                BUILD.load_build_report(report_path, manifest)


if __name__ == "__main__":
    unittest.main()
