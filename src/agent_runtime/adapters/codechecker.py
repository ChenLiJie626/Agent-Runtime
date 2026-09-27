"""Isolated CodeChecker/Clang cross-translation-unit analysis adapter."""

from __future__ import annotations

import json
import plistlib
import re
import shutil
import subprocess
import tempfile
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ..codec import digest, validate_relative_path
from ..errors import BackendFailure, InvalidInput
from .clang import ClangAnalysisBundle, ClangDiagnostic
from .compilation_database import ClangCompilationDatabase
from .source import FrozenSourceProgramQuery

_IMAGE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._/:@-]*$")
_UAF_MESSAGES = ("use of memory after it is freed", "use after free", "use-after-free")


@dataclass(frozen=True)
class CodeCheckerCtuConfig:
    """Pinned execution settings for a network-isolated CTU analysis."""

    image: str = "agent-runtime/clang-ctu:clang14-codechecker6291"
    docker: str = "docker"
    compiler: str = "clang++-14"
    ast_mode: str = "load-from-pch"
    timeout_seconds: int = 300
    workspace_root: Path | None = None

    def __post_init__(self) -> None:
        if (not _IMAGE.fullmatch(self.image)
                or not self.docker
                or not self.compiler
                or "/" in self.compiler
                or self.ast_mode not in {"load-from-pch", "parse-on-demand"}
                or type(self.timeout_seconds) is not int
                or self.timeout_seconds < 1
                or (self.workspace_root is not None
                    and not isinstance(self.workspace_root, Path))):
            raise InvalidInput("invalid CodeChecker CTU configuration")


def _relative_source_path(value: str, sources: Mapping[str, str]) -> str | None:
    normalized = value.replace("\\", "/")
    for prefix in ("/workspace/source/", "$PROJECT_ROOT/", "$SNAPSHOT_ROOT/"):
        if normalized.startswith(prefix):
            normalized = normalized[len(prefix):]
            break
    if normalized in sources:
        return normalized
    matches = [path for path in sources if normalized.endswith("/" + path)]
    return matches[0] if len(matches) == 1 else None


def _plain_plist(value: Any) -> Any:
    """Convert plist values to canonical-JSON-compatible data."""

    if isinstance(value, Mapping):
        return {str(key): _plain_plist(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_plain_plist(item) for item in value]
    if isinstance(value, bytes):
        return value.hex()
    return value


def _load_codechecker_diagnostics(
    report_root: Path,
    sources: Mapping[str, str],
    source_digest: str,
) -> tuple[tuple[ClangDiagnostic, ...], dict[str, Any]]:
    try:
        metadata = json.loads((report_root / "metadata.json").read_text("utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise BackendFailure("CodeChecker did not produce valid metadata") from exc
    tools = metadata.get("tools")
    if not isinstance(tools, list) or not tools or not isinstance(tools[0], Mapping):
        raise BackendFailure("CodeChecker metadata has no analysis tool record")
    raw_mapping = tools[0].get("result_source_files", {})
    result_sources = {
        Path(str(report)).name: _relative_source_path(str(source), sources)
        for report, source in raw_mapping.items()
    } if isinstance(raw_mapping, Mapping) else {}

    diagnostics: list[ClangDiagnostic] = []
    analyses: dict[str, Any] = {}
    for report in sorted(report_root.glob("*.plist")):
        try:
            plist = plistlib.loads(report.read_bytes())
        except (OSError, plistlib.InvalidFileException) as exc:
            raise BackendFailure(f"invalid CodeChecker plist: {report.name}") from exc
        if not isinstance(plist, Mapping):
            raise BackendFailure(f"invalid CodeChecker plist root: {report.name}")
        raw_files = plist.get("files", [])
        files = [
            _relative_source_path(str(item), sources)
            for item in raw_files
        ] if isinstance(raw_files, list) else []
        translation_unit = result_sources.get(report.name)
        if translation_unit is None:
            translation_unit = next((item for item in reversed(files) if item), None)
        if translation_unit is None:
            continue
        validate_relative_path(translation_unit)
        analyses[translation_unit] = {
            "path": translation_unit,
            "command": ["CodeChecker", "analyze", "compile_commands.json", "--ctu"],
            "exit_code": 0,
            "stderr": "",
            "report": report.name,
        }
        raw_diagnostics = plist.get("diagnostics", [])
        if not isinstance(raw_diagnostics, list):
            continue
        for raw in raw_diagnostics:
            if not isinstance(raw, Mapping):
                continue
            message = str(raw.get("description", ""))
            if not any(pattern in message.casefold() for pattern in _UAF_MESSAGES):
                continue
            location = raw.get("location", {})
            if not isinstance(location, Mapping):
                continue
            file_index = location.get("file")
            line = location.get("line")
            column = location.get("col", 1)
            if (type(file_index) is not int or not 0 <= file_index < len(files)
                    or type(line) is not int or line < 1
                    or type(column) is not int or column < 1
                    or files[file_index] is None):
                continue
            path = files[file_index]
            assert path is not None
            rule_id = str(raw.get("check_name", "clang-analyzer"))
            normalized = {
                "format": "codechecker-plist",
                "files": files,
                "diagnostic": _plain_plist(raw),
            }
            identity = {
                "source_digest": source_digest,
                "path": path,
                "translation_unit": translation_unit,
                "line": line,
                "column": column,
                "rule_id": rule_id,
                "message": message,
            }
            diagnostics.append(ClangDiagnostic(
                diagnostic_id=digest(identity),
                path=path,
                translation_unit=translation_unit,
                line=line,
                column=column,
                end_line=line,
                end_column=column,
                rule_id=rule_id,
                message=message,
                sarif_result=normalized,
            ))
    deduplicated: dict[tuple[Any, ...], ClangDiagnostic] = {}
    for diagnostic in diagnostics:
        raw = diagnostic.sarif_result.get("diagnostic", {})
        report_hash = raw.get("issue_hash_content_of_line_in_context")
        key = (
            report_hash or "",
            diagnostic.path,
            diagnostic.line,
            diagnostic.column,
            diagnostic.rule_id,
            diagnostic.message,
        )
        previous = deduplicated.get(key)
        current_path = raw.get("path", [])
        previous_path = (
            previous.sarif_result.get("diagnostic", {}).get("path", [])
            if previous is not None else []
        )
        if previous is None or len(current_path) > len(previous_path):
            deduplicated[key] = diagnostic
    diagnostics = list(deduplicated.values())
    diagnostics.sort(key=lambda item: (
        item.path, item.line, item.column, item.translation_unit, item.diagnostic_id,
    ))
    return tuple(diagnostics), analyses


def run_codechecker_ctu_use_after_free_analysis(
    sources: Mapping[str, str],
    *,
    compilation_database: ClangCompilationDatabase,
    config: CodeCheckerCtuConfig = CodeCheckerCtuConfig(),
) -> ClangAnalysisBundle:
    """Run Clang CTU in an isolated container and normalize its UAF reports."""

    executable = shutil.which(config.docker)
    if executable is None:
        raise BackendFailure(f"container runtime is unavailable: {config.docker}")
    source_digest = FrozenSourceProgramQuery.source_digest(sources)
    for path in compilation_database.files:
        if path not in sources:
            raise InvalidInput(f"compile database source is not frozen: {path}")

    try:
        inspected = subprocess.run(
            [executable, "image", "inspect", "--format={{.Id}}", config.image],
            check=False, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            text=True, timeout=30,
        )
    except subprocess.TimeoutExpired as exc:
        raise BackendFailure("container image inspection timed out") from exc
    if inspected.returncode != 0 or not inspected.stdout.strip():
        raise BackendFailure(f"CodeChecker CTU image is unavailable: {config.image}")
    image_digest = inspected.stdout.strip()

    temporary_parent = (
        config.workspace_root.resolve() if config.workspace_root is not None else None
    )
    if temporary_parent is not None:
        temporary_parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(
        prefix="agent-runtime-codechecker-", dir=temporary_parent,
    ) as directory:
        root = Path(directory)
        source_root = root / "source"
        report_root = root / "output" / "reports"
        source_root.mkdir()
        report_root.parent.mkdir()
        for path, text in sources.items():
            validate_relative_path(path)
            destination = source_root / path
            destination.parent.mkdir(parents=True, exist_ok=True)
            destination.write_text(text, encoding="utf-8")
        commands = []
        container_root = Path("/workspace/source")
        for path in compilation_database.files:
            args = compilation_database.analyzer_arguments(path, container_root)
            commands.append({
                "directory": str(container_root),
                "file": str(container_root / path),
                "arguments": [
                    config.compiler, *args, "-c", str(container_root / path),
                ],
            })
        (source_root / "compile_commands.json").write_text(
            json.dumps(commands, sort_keys=True), encoding="utf-8",
        )
        command = [
            executable, "run", "--rm", "--network", "none",
            "--mount", f"type=bind,src={source_root},dst=/workspace/source,readonly",
            "--mount", f"type=bind,src={report_root.parent},dst=/workspace/output",
            "--tmpfs", "/tmp:rw,nosuid,nodev,size=512m",
            config.image,
            "CodeChecker", "analyze", "/workspace/source/compile_commands.json",
            "-o", "/workspace/output/reports", "--analyzers", "clangsa",
            "--ctu", "--ctu-ast-mode", config.ast_mode,
            "--capture-analysis-output",
        ]
        try:
            completed = subprocess.run(
                command, check=False, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                text=True, timeout=config.timeout_seconds,
            )
        except subprocess.TimeoutExpired as exc:
            raise BackendFailure("CodeChecker CTU analysis timed out") from exc
        if completed.returncode != 0:
            detail = (completed.stderr or completed.stdout).strip().splitlines()
            raise BackendFailure(
                "CodeChecker CTU analysis failed: "
                + (" | ".join(detail[-8:]) if detail else "no diagnostic output")
            )
        diagnostics, analyses = _load_codechecker_diagnostics(
            report_root, sources, source_digest,
        )

    profile = {
        "analyzer": "CodeChecker/clangsa",
        "checker": "core,cplusplus,unix.Malloc",
        "compilation_database_digest": compilation_database.database_digest,
        "ctu_mode": "codechecker",
        "ctu_ast_mode": config.ast_mode,
        "container_image": config.image,
        "container_image_digest": image_digest,
    }
    version = f"CodeChecker 6.29.1 / Clang 14 / {image_digest}"
    return ClangAnalysisBundle(
        source_digest=source_digest,
        compiler=f"container:{config.image}",
        compiler_version=version,
        command_profile_digest=digest(profile),
        compilation_database_digest=compilation_database.database_digest,
        ctu_mode="codechecker",
        diagnostics=diagnostics,
        analyses=analyses,
    )


__all__ = ["CodeCheckerCtuConfig", "run_codechecker_ctu_use_after_free_analysis"]
