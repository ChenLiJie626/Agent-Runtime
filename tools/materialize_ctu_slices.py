"""Materialize one container-compatible compilation database per CTU target."""

from __future__ import annotations

import argparse
import hashlib
import json
import posixpath
import re
from pathlib import Path, PurePosixPath
from typing import Any


def _relative_source(row: dict[str, Any], container_root: str) -> str:
    root_path = PurePosixPath(container_root)
    if not root_path.is_absolute() or ".." in root_path.parts or str(root_path) == "/":
        raise ValueError("container root must be an absolute non-root path")
    directory = row.get("directory")
    source = row.get("file")
    if not isinstance(directory, str) or not isinstance(source, str):
        raise ValueError("compile command needs string directory and file")
    normalized_directory = posixpath.normpath(directory)
    root = container_root.rstrip("/")
    if normalized_directory != root and not normalized_directory.startswith(root + "/"):
        raise ValueError(f"compile directory is outside container root: {directory}")
    if source.startswith("/"):
        absolute = posixpath.normpath(source)
    else:
        absolute = posixpath.normpath(posixpath.join(directory, source))
    root = container_root.rstrip("/")
    if absolute == root or not absolute.startswith(root + "/"):
        raise ValueError(f"compile source is outside container root: {absolute}")
    relative = absolute[len(root) + 1 :]
    parsed = PurePosixPath(relative)
    if not relative or ".." in parsed.parts:
        raise ValueError(f"invalid container source path: {relative}")
    return relative


def _slug(target: str) -> str:
    readable = re.sub(r"[^a-z0-9]+", "-", target.casefold()).strip("-")[-48:]
    suffix = hashlib.sha256(target.encode()).hexdigest()[:8]
    return f"{readable}-{suffix}"


def materialize(
    plan: dict[str, Any],
    container_database: list[dict[str, Any]],
    output_dir: Path,
    *,
    container_root: str = "/workspace/source",
) -> dict[str, Any]:
    """Write independent target slices and return their manifest."""

    by_source: dict[str, dict[str, Any]] = {}
    for row in container_database:
        source = _relative_source(row, container_root)
        if source in by_source:
            raise ValueError(f"duplicate container compile command: {source}")
        by_source[source] = row
    output_dir.mkdir(parents=True, exist_ok=True)
    slices: list[dict[str, Any]] = []
    for selection in plan.get("selections", []):
        target = selection.get("target_path")
        translation_units = selection.get("translation_units")
        if not isinstance(target, str) or not isinstance(translation_units, list):
            raise ValueError("invalid target selection in layered plan")
        missing = sorted(set(translation_units).difference(by_source))
        if missing:
            raise ValueError(
                f"container compile database lacks TUs for {target}: {', '.join(missing)}"
            )
        if len(set(translation_units)) != len(translation_units):
            raise ValueError("target selection repeats a translation unit")
        rows = [by_source[path] for path in translation_units]
        serialized = json.dumps(rows, ensure_ascii=False, indent=2) + "\n"
        if "/Users/" in serialized or "$PROJECT_ROOT" in serialized:
            raise ValueError("slice contains a host-only path")
        slice_dir = output_dir / _slug(target)
        slice_dir.mkdir(parents=True, exist_ok=True)
        database_path = slice_dir / "compile_commands.container.json"
        database_path.write_text(serialized, encoding="utf-8")
        item = {
            "target_path": target,
            "slice_dir": slice_dir.name,
            "compile_database": database_path.name,
            "translation_unit_count": len(rows),
            "translation_units": translation_units,
            "compile_database_sha256": hashlib.sha256(
                serialized.encode("utf-8")
            ).hexdigest(),
        }
        (slice_dir / "slice.json").write_text(
            json.dumps(item, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        slices.append(item)
    manifest = {
        "schema": "agent-runtime/container-ctu-slices/v1",
        "container_root": container_root,
        "slices": slices,
    }
    (output_dir / "slices.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    return manifest


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("plan", type=Path)
    parser.add_argument("container_compile_database", type=Path)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--container-root", default="/workspace/source")
    args = parser.parse_args()
    plan = json.loads(args.plan.read_text(encoding="utf-8"))
    database = json.loads(args.container_compile_database.read_text(encoding="utf-8"))
    manifest = materialize(
        plan,
        database,
        args.output_dir,
        container_root=args.container_root,
    )
    print(args.output_dir / "slices.json")
    for item in manifest["slices"]:
        print(f"{item['target_path']}: {item['translation_unit_count']} TU")


if __name__ == "__main__":
    main()
