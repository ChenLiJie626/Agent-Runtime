"""Verify the 0.2.1 wheel from an isolated environment outside the checkout."""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import tarfile
import tempfile
import zipfile
from pathlib import Path


VERSION = "0.2.1"
LIFETIME_CONSTANTS = {
    "ASYNC_MEMBER_DESTRUCTION_RULE",
    "CONTAINER_INVALIDATION_RULE",
    "COROUTINE_DESTRUCTION_RULE",
    "RETURNED_RESOURCE_RULE",
    "STACK_CONTEXT_ESCAPE_RULE",
    "VECTOR_FIELD_COPY_RULE",
}


def _run_checked(argv: list[str], *, cwd: Path, env: dict[str, str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        argv, check=True, cwd=cwd, capture_output=True, text=True, env=env,
    )


def run(
    wheel: Path, sdist: Path, example: Path, pipeline_example: Path | None = None,
) -> dict:
    wheel = wheel.resolve(strict=True)
    sdist = sdist.resolve(strict=True)
    example = example.resolve(strict=True)
    pipeline_example = (pipeline_example or (
        Path(__file__).resolve().parents[1] / "examples/cpp_unreachable_pipeline.py"
    )).resolve(strict=True)
    license_bytes = (Path(__file__).resolve().parents[1] / "LICENSE").read_bytes()
    uv = shutil.which("uv")
    if not uv:
        raise RuntimeError("uv is required for an isolated offline install")

    with zipfile.ZipFile(wheel) as archive:
        members = set(archive.namelist())
        required = {
            "agent_runtime/py.typed",
            "agent_runtime/adapters/joern_query.sc",
            "agent_runtime/adapters/codeql_lifetime_candidates.ql",
        }
        if required - members:
            raise RuntimeError(f"wheel misses package files: {required - members}")
        if any(".poc/" in name or "__pycache__" in name
               or name.startswith("tests/") for name in members):
            raise RuntimeError("wheel contains workspace or test artifacts")
        metadata_file = next(
            (name for name in members if name.endswith(".dist-info/METADATA")),
            None,
        )
        entry_points_file = next(
            (name for name in members if name.endswith(".dist-info/entry_points.txt")),
            None,
        )
        if metadata_file is None or entry_points_file is None:
            raise RuntimeError("wheel lacks package metadata or console entry points")
        metadata_text = archive.read(metadata_file).decode("utf-8")
        entry_points_text = archive.read(entry_points_file).decode("utf-8")
        if (f"Version: {VERSION}" not in metadata_text
                or "License-Expression: MIT" not in metadata_text
                or "License-File: LICENSE" not in metadata_text
                or "Provides-Extra: claude" not in metadata_text
                or "Requires-Dist: claude-agent-sdk==0.2.159" not in metadata_text):
            raise RuntimeError(f"wheel metadata differs from the {VERSION} contract")
        if "defect-validation-record = agent_runtime.validation_cli:main" not in entry_points_text:
            raise RuntimeError("wheel misses the validation-record console script")
        wheel_license = next(
            (name for name in members if name.endswith(".dist-info/licenses/LICENSE")),
            None,
        )
        if wheel_license is None or archive.read(wheel_license) != license_bytes:
            raise RuntimeError("wheel MIT license is missing or differs from source")

    with tarfile.open(sdist) as archive:
        names = archive.getnames()
        source_members = {name.rsplit("/", 1)[-1] for name in names}
        if not {
            "pyproject.toml", "README.md", "py.typed", "LICENSE",
            "joern_query.sc", "codeql_lifetime_candidates.ql",
        }.issubset(source_members):
            raise RuntimeError("sdist misses release sources or packaged queries")
        sdist_license = next(
            (name for name in names if name.endswith("/LICENSE")), None
        )
        if (sdist_license is None
                or archive.extractfile(sdist_license).read() != license_bytes):
            raise RuntimeError("sdist MIT license is missing or differs from source")
        if any("/.poc/" in name or "__pycache__" in name
               or name.endswith((".sqlite3", ".db", ".pyc"))
               for name in names):
            raise RuntimeError("sdist contains generated workspace artifacts")

    with tempfile.TemporaryDirectory(prefix="s3-installed-consumer-") as directory:
        root = Path(directory)
        environment = root / "venv"
        subprocess.run(
            [uv, "venv", "--offline", "--python", sys.executable, str(environment)],
            check=True, cwd=root, capture_output=True, text=True,
        )
        python = environment / "bin/python"
        subprocess.run(
            [uv, "pip", "install", "--offline", "--python", str(python), str(wheel)],
            check=True, cwd=root, capture_output=True, text=True,
        )
        environment_vars = os.environ.copy()
        environment_vars.pop("PYTHONPATH", None)

        copied_example = root / "consumer.py"
        shutil.copy2(example, copied_example)
        result = _run_checked(
            [str(python), "-I", str(copied_example)], cwd=root, env=environment_vars,
        )
        consumer = json.loads(result.stdout.strip().splitlines()[-1])
        copied_pipeline = root / "cpp_pipeline.py"
        shutil.copy2(pipeline_example, copied_pipeline)
        pipeline_result = _run_checked(
            [str(python), "-I", str(copied_pipeline)], cwd=root, env=environment_vars,
        )
        pipeline = json.loads(pipeline_result.stdout)

        record_path = root / "record.json"
        inspection_script = r'''
import hashlib, importlib.metadata as metadata, importlib.resources as resources
import json, sys
import agent_runtime as runtime
from agent_runtime import (
    ValidationArtifact, ValidationBuildOutcome, ValidationExecutionOutcome,
    ValidationRecord, ValidationResource, validation_record_to_dict,
)
from agent_runtime.adapters import (
    CodeQLReplayProgramQuery, RecordedJoernProgramQuery,
    RecordedValidationProgramQuery,
)

def artifact(value):
    data = value.encode()
    return ValidationArtifact(hashlib.sha256(data).hexdigest(), len(data))

record = ValidationRecord(
    "fixture", "a" * 64, "scope", artifact("source"), artifact("policy"),
    artifact("toolchain"), artifact("dependency"), artifact("recipe"),
    artifact("input"), ValidationBuildOutcome("succeeded", artifact("binary")),
    ValidationExecutionOutcome("normal_exit", "no_trigger", 0),
    (ValidationResource("wall_time_ms", 1000, 10),),
)
with open(sys.argv[1], "w", encoding="utf-8") as output:
    json.dump(validation_record_to_dict(record), output)
query = resources.files("agent_runtime.adapters").joinpath("codeql_lifetime_candidates.ql")
print(json.dumps({
    "version": metadata.version("defect-agent-runtime"),
    "license": metadata.metadata("defect-agent-runtime").get("License-Expression"),
    "path": runtime.__file__,
    "claude_loaded": "claude_agent_sdk" in sys.modules,
    "exports_valid": len(runtime.__all__) == len(set(runtime.__all__))
        and all(hasattr(runtime, name) for name in runtime.__all__),
    "lifetime_constants": sorted(name for name in ''' + repr(LIFETIME_CONSTANTS) + r''' if hasattr(runtime, name)),
    "legacy_joern": RecordedJoernProgramQuery.__name__,
    "validation_adapter": RecordedValidationProgramQuery.__name__,
    "codeql_adapter": CodeQLReplayProgramQuery.__name__,
    "ql_packaged": query.is_file() and bool(query.read_text(encoding="utf-8")),
    "record_id": record.record_id,
}))
'''
        metadata_result = _run_checked(
            [str(python), "-I", "-c", inspection_script, str(record_path)],
            cwd=root,
            env=environment_vars,
        )
        installed = json.loads(metadata_result.stdout.strip())

        cli = environment / "bin/defect-validation-record"
        _run_checked([str(cli), "--help"], cwd=root, env=environment_vars)
        validate_result = _run_checked(
            [str(cli), "validate", str(record_path)], cwd=root, env=environment_vars,
        )
        inspect_result = _run_checked(
            [str(cli), "inspect", str(record_path)], cwd=root, env=environment_vars,
        )
        inspected = json.loads(inspect_result.stdout)
        polluted = {**inspected, "argv": ["forbidden"]}
        polluted_path = root / "polluted.json"
        polluted_path.write_text(json.dumps(polluted), encoding="utf-8")
        polluted_result = subprocess.run(
            [str(cli), "validate", str(polluted_path)],
            cwd=root,
            capture_output=True,
            text=True,
            env=environment_vars,
        )
        run_result = subprocess.run(
            [str(cli), "run", str(record_path)],
            cwd=root,
            capture_output=True,
            text=True,
            env=environment_vars,
        )

        if (installed["version"] != VERSION
                or installed["license"] != "MIT"
                or not Path(installed["path"]).is_relative_to(environment)
                or installed["claude_loaded"]
                or not installed["exports_valid"]
                or set(installed["lifetime_constants"]) != LIFETIME_CONSTANTS
                or installed["legacy_joern"] != "RecordedJoernProgramQuery"
                or installed["validation_adapter"] != "RecordedValidationProgramQuery"
                or installed["codeql_adapter"] != "CodeQLReplayProgramQuery"
                or not installed["ql_packaged"]
                or validate_result.stdout.strip() != installed["record_id"]
                or inspected.get("record_id") != installed["record_id"]
                or polluted_result.returncode == 0
                or run_result.returncode == 0
                or consumer["status"] != "passed"
                or len(pipeline.get("candidates", ())) != 1
                or pipeline["candidates"][0].get("status") != "inconclusive"):
            raise RuntimeError("installed package did not meet S3 acceptance")
        return {
            "status": "passed",
            "version": installed["version"],
            "license": installed["license"],
            "wheel": wheel.name,
            "sdist": sdist.name,
            "installed_outside_checkout": True,
            "core_import_without_claude": True,
            "public_exports": "passed",
            "package_queries": {"joern": "present", "codeql": "present"},
            "validation_cli": {
                "status": "passed",
                "validate": "passed",
                "inspect": "passed",
                "forbidden_argv_rejected": True,
                "execution_subcommand_absent": True,
            },
            "consumer": consumer,
            "cpp_pipeline": {
                "status": "passed",
                "candidate_status": pipeline["candidates"][0]["status"],
                "blockers": pipeline["candidates"][0]["blockers"],
            },
        }


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--wheel", type=Path, required=True)
    parser.add_argument("--sdist", type=Path, required=True)
    parser.add_argument("--example", type=Path, default=Path("examples/external_consumer.py"))
    parser.add_argument(
        "--pipeline-example", type=Path,
        default=Path("examples/cpp_unreachable_pipeline.py"),
    )
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    report = run(args.wheel, args.sdist, args.example, args.pipeline_example)
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2))
    print(json.dumps(report, ensure_ascii=False))
