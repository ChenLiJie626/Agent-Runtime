"""Run one frozen public C++ build in a credential-free, offline container.

Dependency image preparation is deliberately outside this command. The command
records the immutable image identity and complete installed-package list, then
runs the manifest's tokenized commands with networking disabled and a bounded
container. Upstream source is extracted without links or path traversal.
"""

from __future__ import annotations

import argparse
import io
import json
import subprocess
import tarfile
import time
from pathlib import Path, PurePosixPath

from agent_runtime.codec import bytes_digest, canonical_json, digest, utc_now
from agent_runtime.errors import InvalidInput
from agent_runtime.evaluation import (
    dataset_manifest_digest,
    fetch_verified_artifact,
    load_json_object,
    validate_dataset_manifest,
)


def extract_verified_archive(body: bytes, destination: Path) -> dict[str, str]:
    """Extract regular files below one archive root and return their digests."""
    destination.mkdir(parents=True, exist_ok=False)
    files: dict[str, str] = {}
    try:
        archive = tarfile.open(fileobj=io.BytesIO(body), mode="r:gz")
    except tarfile.TarError as exc:
        raise InvalidInput("source artifact is not a readable tar.gz archive") from exc
    with archive:
        members = archive.getmembers()
        if not members:
            raise InvalidInput("source archive is empty")
        roots = {PurePosixPath(item.name).parts[0] for item in members
                 if PurePosixPath(item.name).parts}
        if len(roots) != 1:
            raise InvalidInput("source archive must have exactly one root directory")
        root = next(iter(roots))
        for member in members:
            path = PurePosixPath(member.name)
            if (path.is_absolute() or not path.parts or path.parts[0] != root
                    or ".." in path.parts or member.issym() or member.islnk()
                    or not (member.isdir() or member.isfile())):
                raise InvalidInput("source archive contains an unsafe member")
            if member.isdir():
                continue
            relative = PurePosixPath(*path.parts[1:])
            if not relative.parts:
                raise InvalidInput("source archive regular file cannot be its root")
            source = archive.extractfile(member)
            if source is None:
                raise InvalidInput("source archive member cannot be read")
            body = source.read()
            target = destination.joinpath(*relative.parts)
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(body)
            files[str(relative)] = bytes_digest(body)
    if not files:
        raise InvalidInput("source archive contains no regular files")
    return files


def normalize_compile_database(value: object) -> list[dict]:
    if not isinstance(value, list) or not value:
        raise InvalidInput("compile database must be a nonempty array")
    normalized = []
    for item in value:
        if not isinstance(item, dict) or not isinstance(item.get("file"), str):
            raise InvalidInput("compile database entry needs a file")
        if not isinstance(item.get("directory"), str):
            raise InvalidInput("compile database entry needs a directory")
        has_arguments = isinstance(item.get("arguments"), list) and all(
            isinstance(token, str) for token in item["arguments"]
        )
        has_command = isinstance(item.get("command"), str)
        if has_arguments == has_command:
            raise InvalidInput("compile database entry needs exactly one command representation")

        def relocate(text: str) -> str:
            return text.replace("/workspace", "$SOURCE")

        row = {
            "directory": relocate(item["directory"]),
            "file": relocate(item["file"]),
        }
        if has_arguments:
            row["arguments"] = [relocate(token) for token in item["arguments"]]
        else:
            row["command"] = relocate(item["command"])
        if "output" in item:
            if not isinstance(item["output"], str):
                raise InvalidInput("compile database output must be a string")
            row["output"] = relocate(item["output"])
        normalized.append(row)
    return sorted(normalized, key=lambda item: canonical_json(item))


def _verified_local_file(path_value: object, expected_digest: object, label: str) -> bytes:
    if (not isinstance(path_value, str) or not Path(path_value).is_absolute()
            or not Path(path_value).is_file()):
        raise InvalidInput(f"{label} is unavailable")
    body = Path(path_value).read_bytes()
    if bytes_digest(body) != expected_digest:
        raise InvalidInput(f"{label} digest differs")
    return body


def load_build_report(path: Path, manifest: dict) -> dict[str, dict]:
    """Validate a persisted build report and every referenced raw artifact."""
    report_bytes = path.read_bytes()
    try:
        report = json.loads(report_bytes)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise InvalidInput("build report must be a UTF-8 JSON object") from exc
    if not isinstance(report, dict):
        raise InvalidInput("build report must be a JSON object")
    sample = next((item for item in manifest["samples"]
                   if item["sample_id"] == report.get("sample_id")), None)
    if sample is None:
        raise InvalidInput("build report sample is not in the manifest")
    head_artifact = next(item for item in sample["artifacts"]
                         if item["role"] == "head_source")
    build = report.get("build")
    container = report.get("container")
    compile_db = report.get("compile_database")
    if (not isinstance(build, dict) or not isinstance(container, dict)
            or not isinstance(compile_db, dict)
            or report.get("dataset_id") != manifest["dataset_id"]
            or report.get("manifest_digest") != dataset_manifest_digest(manifest)
            or report.get("head_commit") != sample["head_commit"]
            or report.get("head_artifact_sha256") != head_artifact["sha256"]
            or build.get("variant_id") != sample["build"]["variant_id"]
            or build.get("recipe_digest") != sample["build"]["recipe_digest"]
            or build.get("recipe_matches_manifest") is not True
            or build.get("manifest_dependency_digest") != sample["build"]["dependency_digest"]
            or build.get("dependency_lock_path") != sample["build"].get("dependency_lock_path")
            or build.get("dependency_lock_matches_manifest") is not True
            or build.get("status") not in {"complete", "build_failure", "timeout"}
            or build.get("instrumentation_environment") != {"CMAKE_EXPORT_COMPILE_COMMANDS": "ON"}
            or container.get("network_during_build") != "none"
            or container.get("credentials_mounted") is not False
            or container.get("os") != "linux"
            or not isinstance(container.get("image_id"), str)
            or not container["image_id"].startswith("sha256:")):
        raise InvalidInput("build report is not bound to the frozen sample")
    commands = build.get("commands")
    if (not isinstance(commands, list) or not commands
            or len(commands) > len(sample["build"]["commands"])):
        raise InvalidInput("build report command list is invalid")
    for index, item in enumerate(commands):
        if (not isinstance(item, dict)
                or item.get("manifest_command") != sample["build"]["commands"][index]
                or item.get("network") != "none"
                or item.get("status") not in {"complete", "failed", "timeout"}):
            raise InvalidInput("build command does not match the frozen recipe")
        _verified_local_file(item.get("stdout_path"), item.get("stdout_sha256"),
                             "build stdout")
        _verified_local_file(item.get("stderr_path"), item.get("stderr_sha256"),
                             "build stderr")
    if build["status"] == "complete" and (
        len(commands) != len(sample["build"]["commands"])
        or any(item["status"] != "complete" for item in commands)
    ):
        raise InvalidInput("complete build report needs all successful commands")
    package_bytes = _verified_local_file(
        container.get("package_lock_path"), container.get("package_lock_sha256"),
        "container package lock",
    )
    if (len([line for line in package_bytes.splitlines() if line]) != container.get("package_count")
            or bytes_digest(package_bytes) != sample["build"]["dependency_digest"]):
        raise InvalidInput("container package inventory differs from its frozen lock")
    raw_compile_db = _verified_local_file(
        compile_db.get("path"), compile_db.get("sha256"), "build compile database",
    )
    try:
        normalized = normalize_compile_database(json.loads(raw_compile_db))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise InvalidInput("build compile database must be JSON") from exc
    if (digest(normalized) != compile_db.get("normalized_sha256")
            or len(normalized) != compile_db.get("translation_units")):
        raise InvalidInput("normalized compile database metadata differs")
    return {sample["sample_id"]: {
        "report": report,
        "report_digest": bytes_digest(report_bytes),
        "compile_database": raw_compile_db,
        "package_lock": package_bytes,
    }}


def _capture(command: list[str], *, cwd: Path, timeout: int) -> dict:
    started = time.monotonic()
    try:
        result = subprocess.run(
            command, cwd=cwd, capture_output=True, check=False, timeout=timeout,
        )
        return {
            "command": command,
            "status": "complete" if result.returncode == 0 else "failed",
            "returncode": result.returncode,
            "stdout": result.stdout,
            "stderr": result.stderr,
            "duration_seconds": round(time.monotonic() - started, 6),
        }
    except subprocess.TimeoutExpired as exc:
        return {
            "command": command,
            "status": "timeout",
            "returncode": None,
            "stdout": exc.stdout or b"",
            "stderr": exc.stderr or b"",
            "duration_seconds": round(time.monotonic() - started, 6),
        }


def run(manifest_path: Path, sample_id: str, image: str, work_dir: Path,
        *, timeout_seconds: int) -> dict:
    manifest = load_json_object(manifest_path)
    validate_dataset_manifest(manifest)
    sample = next((item for item in manifest["samples"]
                   if item["sample_id"] == sample_id), None)
    if sample is None:
        raise InvalidInput(f"sample is not in manifest: {sample_id}")
    if not 1 <= timeout_seconds <= 3600:
        raise InvalidInput("build timeout must be between 1 and 3600 seconds")
    work_dir = work_dir.resolve()
    work_dir.mkdir(parents=True, exist_ok=False)
    raw_dir = work_dir / "raw"
    raw_dir.mkdir()

    inspect = _capture(
        ["docker", "image", "inspect", image], cwd=work_dir, timeout=30,
    )
    if inspect["status"] != "complete":
        raise InvalidInput("pinned build image is unavailable")
    image_info = json.loads(inspect["stdout"])[0]
    image_id = image_info.get("Id")
    if (not isinstance(image_id, str) or not image_id.startswith("sha256:")
            or image_info.get("Os") != "linux"):
        raise InvalidInput("build image lacks a Linux content identity")

    package_result = _capture([
        "docker", "run", "--rm", "--network=none", "--read-only",
        image, "dpkg-query", "-W", "-f=${binary:Package}\\t${Version}\\n",
    ], cwd=work_dir, timeout=60)
    if package_result["status"] != "complete":
        raise InvalidInput("cannot inventory build image packages")
    package_lines = sorted(filter(None, package_result["stdout"].decode().splitlines()))
    package_bytes = ("\n".join(package_lines) + "\n").encode()
    (raw_dir / "packages.lock").write_bytes(package_bytes)
    build = sample["build"]
    lock_value = build.get("dependency_lock_path")
    if not isinstance(lock_value, str):
        raise InvalidInput("pinned build needs a committed dependency lock")
    repository_root = manifest_path.resolve().parents[2]
    committed_lock = (repository_root / lock_value).resolve()
    if (repository_root not in committed_lock.parents or not committed_lock.is_file()
            or bytes_digest(committed_lock.read_bytes()) != build["dependency_digest"]):
        raise InvalidInput("committed dependency lock does not match the manifest")

    head_artifact = next(item for item in sample["artifacts"]
                         if item["role"] == "head_source")
    archive, verification = fetch_verified_artifact(head_artifact)
    source_dir = work_dir / "source"
    source_files = extract_verified_archive(archive, source_dir)
    source_tree_digest = digest(source_files)

    command_results = []
    for index, manifest_command in enumerate(sample["build"]["commands"]):
        docker_command = [
            "docker", "run", "--rm", "--network=none", "--read-only",
            "--cap-drop=ALL", "--security-opt=no-new-privileges",
            "--pids-limit=512", "--memory=6g", "--cpus=4",
            "--tmpfs", "/tmp:rw,nosuid,nodev,size=512m",
            "-e", "CMAKE_EXPORT_COMPILE_COMMANDS=ON",
            "-v", f"{source_dir}:/workspace:rw", image,
            *manifest_command,
        ]
        result = _capture(docker_command, cwd=work_dir, timeout=timeout_seconds)
        stdout_path = raw_dir / f"command-{index}.stdout"
        stderr_path = raw_dir / f"command-{index}.stderr"
        stdout_path.write_bytes(result.pop("stdout"))
        stderr_path.write_bytes(result.pop("stderr"))
        command_results.append({
            **result,
            "manifest_command": manifest_command,
            "network": "none",
            "stdout_path": str(stdout_path),
            "stdout_sha256": bytes_digest(stdout_path.read_bytes()),
            "stderr_path": str(stderr_path),
            "stderr_sha256": bytes_digest(stderr_path.read_bytes()),
        })
        if result["status"] != "complete":
            break

    compile_db_path = source_dir / "build" / "compile_commands.json"
    compile_database = None
    if compile_db_path.is_file():
        raw_compile_db = compile_db_path.read_bytes()
        try:
            compile_database = normalize_compile_database(json.loads(raw_compile_db))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise InvalidInput("generated compile database is invalid JSON") from exc
        (raw_dir / "compile_commands.json").write_bytes(raw_compile_db)
        compile_db = {
            "path": str((raw_dir / "compile_commands.json").resolve()),
            "sha256": bytes_digest(raw_compile_db),
            "normalized_sha256": digest(compile_database),
            "translation_units": len(compile_database),
            "surface_translation_units": sum(
                any(item["file"].endswith(path) for path in sample["surface"]["paths"])
                for item in compile_database
            ),
        }
    else:
        compile_db = None

    recipe_digest = digest({
        "commands": build["commands"], "toolchain": build["toolchain"],
        "network": build["network"],
    })
    status = ("complete" if len(command_results) == len(build["commands"])
              and all(item["status"] == "complete" for item in command_results)
              else "timeout" if any(item["status"] == "timeout" for item in command_results)
              else "build_failure")
    package_lock_digest = bytes_digest(package_bytes)
    report = {
        "schema_version": {"major": 1, "minor": 0},
        "created_at": utc_now(),
        "dataset_id": manifest["dataset_id"],
        "manifest_digest": dataset_manifest_digest(manifest),
        "sample_id": sample_id,
        "head_commit": sample["head_commit"],
        "head_artifact_sha256": verification["sha256"],
        "source_tree_digest": source_tree_digest,
        "build": {
            "variant_id": build["variant_id"],
            "status": status,
            "recipe_digest": recipe_digest,
            "recipe_matches_manifest": recipe_digest == build["recipe_digest"],
            "manifest_dependency_digest": build["dependency_digest"],
            "dependency_lock_path": build["dependency_lock_path"],
            "dependency_lock_matches_manifest": package_lock_digest == build["dependency_digest"],
            "commands": command_results,
            "instrumentation_environment": {"CMAKE_EXPORT_COMPILE_COMMANDS": "ON"},
        },
        "container": {
            "image": image,
            "image_id": image_id,
            "repo_digests": image_info.get("RepoDigests", []),
            "os": image_info.get("Os"),
            "architecture": image_info.get("Architecture"),
            "package_count": len(package_lines),
            "package_lock_path": str((raw_dir / "packages.lock").resolve()),
            "package_lock_sha256": package_lock_digest,
            "network_during_build": "none",
            "credentials_mounted": False,
        },
        "compile_database": compile_db,
        "limitations": [
            "the observed package list differs from the frozen dependency lock",
        ] if package_lock_digest != build["dependency_digest"] else [],
    }
    if status != "complete":
        report["limitations"].append("the configured target did not build successfully")
    (work_dir / "report.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8",
    )
    return report


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", type=Path, required=True)
    parser.add_argument("--sample-id", required=True)
    parser.add_argument("--image", required=True)
    parser.add_argument("--work-dir", type=Path, required=True)
    parser.add_argument("--timeout-seconds", type=int, default=600)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    report = run(args.manifest, args.sample_id, args.image, args.work_dir,
                 timeout_seconds=args.timeout_seconds)
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(
            json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8",
        )
    print(json.dumps({
        "sample_id": report["sample_id"], "status": report["build"]["status"],
        "image_id": report["container"]["image_id"],
        "compile_database": report["compile_database"],
    }, ensure_ascii=False))


if __name__ == "__main__":
    main()
