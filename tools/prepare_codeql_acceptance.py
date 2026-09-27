#!/usr/bin/env python3
"""Prepare the pinned official CodeQL bundle through the required proxy."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import subprocess
import tarfile
from pathlib import Path, PurePosixPath

from agent_runtime.codec import bytes_digest, canonical_json, validate_relative_path
from agent_runtime.errors import InvalidInput

ROOT = Path(__file__).parents[1]
MANIFEST = ROOT / "evaluation/manifests/codeql-cpp-v2.27.1.json"
PROXY = "http://127.0.0.1:17891"


def file_sha256(path: Path) -> str:
    value = hashlib.sha256()
    with path.open("rb") as handle:
        while chunk := handle.read(1024 * 1024):
            value.update(chunk)
    return value.hexdigest()


def verify_archive(path: Path, manifest) -> None:
    expected = manifest["archive"]
    if path.stat().st_size != expected["size"] or file_sha256(path) != expected["sha256"]:
        raise InvalidInput("CodeQL archive size or SHA-256 differs from manifest")


def download(path: Path, manifest) -> None:
    if os.environ.get("HTTP_PROXY") != PROXY or os.environ.get("HTTPS_PROXY") != PROXY:
        raise InvalidInput("CodeQL preparation requires the fixed 0cloud proxy")
    if path.exists():
        verify_archive(path, manifest)
        return
    partial = path.with_suffix(path.suffix + ".partial")
    result = subprocess.run(
        ["curl", "--fail", "--location", "--retry", "3", "--output", str(partial),
         manifest["archive"]["url"]],
        stdin=subprocess.DEVNULL, check=False,
        env={"PATH": os.defpath, "LANG": "C", "LC_ALL": "C",
             "HTTP_PROXY": PROXY, "HTTPS_PROXY": PROXY},
    )
    if result.returncode != 0:
        raise InvalidInput("fixed CodeQL archive download failed")
    verify_archive(partial, manifest)
    partial.replace(path)


def extract_verified(archive: Path, destination: Path) -> None:
    if destination.exists():
        raise InvalidInput("CodeQL extraction destination already exists")
    destination.mkdir()
    seen = set()
    decoder = subprocess.Popen(
        ["zstd", "-dc", str(archive)], stdout=subprocess.PIPE,
        stdin=subprocess.DEVNULL, stderr=subprocess.PIPE,
    )
    assert decoder.stdout is not None
    try:
        with tarfile.open(fileobj=decoder.stdout, mode="r|") as bundle:
            for member in bundle:
                pure = PurePosixPath(member.name)
                if (not member.name or pure.is_absolute() or ".." in pure.parts
                        or member.name.startswith("./../") or member.name in seen):
                    raise InvalidInput("CodeQL archive has an unsafe or duplicate path")
                seen.add(member.name)
                relative = pure.as_posix().removeprefix("./")
                validate_relative_path(relative)
                target = destination.joinpath(*PurePosixPath(relative).parts)
                if member.isdir():
                    target.mkdir(parents=True, exist_ok=True)
                elif member.isfile():
                    target.parent.mkdir(parents=True, exist_ok=True)
                    source = bundle.extractfile(member)
                    if source is None:
                        raise InvalidInput("CodeQL archive regular file is unreadable")
                    with target.open("xb") as output:
                        shutil.copyfileobj(source, output, length=1024 * 1024)
                    target.chmod(0o555 if member.mode & 0o111 else 0o444)
                else:
                    raise InvalidInput("CodeQL archive links and special files are forbidden")
    except Exception:
        decoder.kill()
        decoder.wait()
        raise
    stderr = decoder.communicate()[1]
    if decoder.returncode != 0:
        raise InvalidInput(f"CodeQL archive decompression failed: {stderr[:500]!r}")
    for path in sorted(destination.rglob("*"), reverse=True):
        if path.is_dir():
            path.chmod(0o555)
    destination.chmod(0o555)


def tree_manifest(directory: Path) -> dict[str, str]:
    values = {}
    for path in sorted(directory.rglob("*")):
        if path.is_symlink():
            raise InvalidInput("extracted CodeQL tree contains a symlink")
        if path.is_file():
            relative = path.relative_to(directory).as_posix()
            validate_relative_path(relative)
            values[relative] = file_sha256(path)
    if not values:
        raise InvalidInput("extracted CodeQL tree is empty")
    return values


def bounded(command, *, timeout_ms):
    result = subprocess.run(
        command, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
        stderr=subprocess.PIPE, check=False, timeout=timeout_ms / 1000,
        env={"PATH": os.defpath, "LANG": "C", "LC_ALL": "C"},
    )
    if result.returncode != 0 or len(result.stdout) + len(result.stderr) > 16 * 1024 * 1024:
        raise InvalidInput(f"CodeQL verification command failed: {result.stderr[:500]!r}")
    return result.stdout


def run(cache_root: Path):
    manifest = json.loads(MANIFEST.read_text())
    cache_root.mkdir(parents=True, exist_ok=True)
    archive = cache_root / "codeql-bundle-osx64.tar.zst"
    download(archive, manifest)
    extracted = cache_root / "extracted"
    if not extracted.exists():
        extract_verified(archive, extracted)
    tool_root = extracted / "codeql"
    cli = tool_root / "codeql"
    if not cli.is_file() or cli.is_symlink():
        raise InvalidInput("CodeQL CLI is absent from the verified extraction")
    files = tree_manifest(extracted)
    tree_digest = bytes_digest(canonical_json(files).encode())
    version_raw = bounded([str(cli), "version", "--format=json"], timeout_ms=120000)
    version = json.loads(version_raw)
    if version.get("version") != manifest["cli_version"]:
        raise InvalidInput("CodeQL CLI version differs from manifest")
    packs_raw = bounded(
        [str(cli), "resolve", "qlpacks", "--format=json"], timeout_ms=120000
    )
    packs = json.loads(packs_raw)
    if "codeql/cpp-all" not in canonical_json(packs):
        raise InvalidInput("official bundle does not resolve codeql/cpp-all")
    report = {
        "schema": "agent-runtime/codeql-preparation/v1",
        "toolchain_id": manifest["toolchain_id"],
        "archive_sha256": manifest["archive"]["sha256"],
        "archive_size": archive.stat().st_size,
        "extracted_tree_digest": tree_digest,
        "extracted_file_count": len(files),
        "cli_path": str(cli.resolve()),
        "cli_sha256": file_sha256(cli),
        "version": version,
        "resolved_qlpacks_digest": bytes_digest(canonical_json(packs).encode()),
        "status": "prepared",
    }
    (cache_root / "tree-manifest.json").write_text(canonical_json(files) + "\n")
    (cache_root / "prepared.json").write_text(json.dumps(report, indent=2, sort_keys=True) + "\n")
    return report


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--toolchain", required=True, choices=["codeql-cpp-v2.27.1"])
    parser.add_argument("--cache-root", type=Path, required=True)
    args = parser.parse_args()
    print(json.dumps(run(args.cache_root), sort_keys=True))


if __name__ == "__main__":
    main()
