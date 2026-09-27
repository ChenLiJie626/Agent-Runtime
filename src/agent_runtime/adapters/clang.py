"""Clang Static Analyzer capture and immutable diagnostic query adapter."""

from __future__ import annotations

import json
import shutil
import subprocess
import tempfile
from urllib.parse import unquote
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath
from typing import Any

from ..codec import bytes_digest, canonical_json, digest, freeze_value, validate_relative_path
from ..domain import Coverage, FixedSnapshot, QueryRequest
from ..errors import BackendFailure, InvalidInput, PolicyDenied, StaleSnapshot
from ..ports import ProgramResult, QueryOperation
from .compilation_database import ClangCompilationDatabase
from .source import FrozenSourceProgramQuery

_CPP_IMPLEMENTATIONS = frozenset({".c", ".cc", ".cpp", ".cxx"})
_UAF_MESSAGES = (
    "use of memory after it is freed",
    "use after free",
    "use-after-free",
)


@dataclass(frozen=True)
class ClangDiagnostic:
    """One normalized analyzer result with stable snapshot-relative identity."""

    diagnostic_id: str
    path: str
    translation_unit: str
    line: int
    column: int
    end_line: int
    end_column: int
    rule_id: str
    message: str
    sarif_result: dict[str, Any] = field(repr=False)

    def __post_init__(self) -> None:
        validate_relative_path(self.path)
        validate_relative_path(self.translation_unit)
        if (not self.diagnostic_id or not self.rule_id or not self.message
                or min(self.line, self.column, self.end_line, self.end_column) < 1):
            raise InvalidInput("invalid Clang diagnostic")
        object.__setattr__(self, "sarif_result", freeze_value(self.sarif_result))

    def as_dict(self) -> dict[str, Any]:
        return {
            "diagnostic_id": self.diagnostic_id,
            "path": self.path,
            "translation_unit": self.translation_unit,
            "line": self.line,
            "column": self.column,
            "end_line": self.end_line,
            "end_column": self.end_column,
            "rule_id": self.rule_id,
            "message": self.message,
            "sarif_result": self.sarif_result,
        }


@dataclass(frozen=True)
class ClangAnalysisBundle:
    """Immutable analyzer output bound to one frozen source snapshot."""

    source_digest: str
    compiler: str
    compiler_version: str
    command_profile_digest: str
    compilation_database_digest: str | None
    ctu_mode: str
    diagnostics: tuple[ClangDiagnostic, ...]
    analyses: dict[str, Any] = field(repr=False)

    def __post_init__(self) -> None:
        if not all((self.source_digest, self.compiler, self.compiler_version,
                    self.command_profile_digest, self.ctu_mode)):
            raise InvalidInput("incomplete Clang analysis bundle")
        if len({item.diagnostic_id for item in self.diagnostics}) != len(self.diagnostics):
            raise InvalidInput("duplicate Clang diagnostic IDs")
        object.__setattr__(self, "analyses", freeze_value(self.analyses))


def _normalize_tool_text(text: str, root: Path) -> str:
    replacements = {str(root), str(root.resolve()), root.as_uri()}
    normalized = text
    for value in sorted(replacements, key=len, reverse=True):
        normalized = normalized.replace(value, "$SNAPSHOT_ROOT")
    return normalized


def _message(result: Mapping[str, Any]) -> str:
    message = result.get("message", {})
    return str(message.get("text", "")) if isinstance(message, Mapping) else ""


def _location(result: Mapping[str, Any]) -> tuple[int, int, int, int] | None:
    locations = result.get("locations", ())
    if not isinstance(locations, Sequence) or not locations:
        return None
    first = locations[0]
    if not isinstance(first, Mapping):
        return None
    physical = first.get("physicalLocation", {})
    region = physical.get("region", {}) if isinstance(physical, Mapping) else {}
    if not isinstance(region, Mapping):
        return None
    values = (
        region.get("startLine"),
        region.get("startColumn", 1),
        region.get("endLine", region.get("startLine")),
        region.get("endColumn", region.get("startColumn", 1)),
    )
    if any(type(value) is not int or value < 1 for value in values):
        return None
    return values  # type: ignore[return-value]


def _diagnostic_path(
    result: Mapping[str, Any],
    sources: Mapping[str, str],
    fallback: str,
) -> str:
    locations = result.get("locations", ())
    if not isinstance(locations, Sequence) or not locations:
        return fallback
    first = locations[0]
    physical = first.get("physicalLocation", {}) if isinstance(first, Mapping) else {}
    artifact = physical.get("artifactLocation", {}) if isinstance(physical, Mapping) else {}
    uri = artifact.get("uri") if isinstance(artifact, Mapping) else None
    if not isinstance(uri, str):
        return fallback
    value = unquote(uri)
    for prefix in ("file://$SNAPSHOT_ROOT/", "$SNAPSHOT_ROOT/"):
        if value.startswith(prefix):
            value = value[len(prefix):]
            break
    if value in sources:
        return value
    return fallback


def run_clang_use_after_free_analysis(
    sources: Mapping[str, str],
    *,
    paths: Sequence[str] | None = None,
    compiler: str = "clang++",
    language_standard: str = "c++17",
    include_paths: Sequence[str] | None = None,
    compilation_database: ClangCompilationDatabase | None = None,
    ctu_mode: str = "disabled",
    timeout_seconds: int = 30,
) -> ClangAnalysisBundle:
    """Analyze frozen C/C++ sources and return normalized UAF diagnostics.

    The analyzer runs in a temporary copy. The returned bundle contains no
    temporary paths and can be replayed through ``ClangDiagnosticProgramQuery``.
    """

    source_digest = FrozenSourceProgramQuery.source_digest(sources)
    executable = shutil.which(compiler)
    if executable is None:
        raise BackendFailure(f"Clang compiler is unavailable: {compiler}")
    if (type(timeout_seconds) is not int or timeout_seconds < 1
            or not language_standard):
        raise InvalidInput("invalid Clang analyzer configuration")
    if ctu_mode not in {"disabled"}:
        raise BackendFailure(
            "CTU mode requires the CodeChecker CTU adapter; direct Clang mode is per-TU"
        )
    selected = tuple(paths) if paths is not None else tuple(
        path for path in (
            compilation_database.files if compilation_database is not None
            else sorted(sources)
        )
        if PurePosixPath(path).suffix in _CPP_IMPLEMENTATIONS
    )
    if not selected:
        raise InvalidInput("Clang analysis needs at least one C/C++ implementation")
    for path in selected:
        validate_relative_path(path)
        if path not in sources or PurePosixPath(path).suffix not in _CPP_IMPLEMENTATIONS:
            raise InvalidInput(f"Clang analysis path is not a frozen implementation: {path}")
    if include_paths is None:
        discovered_include_paths = {
            str(PurePosixPath(path).parent)
            for path in sources
            if PurePosixPath(path).suffix in {".h", ".hh", ".hpp", ".hxx"}
        }
        include_paths = tuple(sorted(
            path for path in discovered_include_paths if path != "."
        ))
    else:
        include_paths = tuple(include_paths)
    for path in include_paths:
        validate_relative_path(path)

    version_result = subprocess.run(
        [executable, "--version"],
        check=False,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        timeout=timeout_seconds,
    )
    if version_result.returncode != 0:
        raise BackendFailure("Clang version probe failed")
    compiler_version = version_result.stdout.splitlines()[0].strip()
    profile = {
        "checker": "core,cplusplus,unix.Malloc",
        "language_standard": language_standard,
        "output": "sarif",
        "include_paths": list(include_paths),
        "compilation_database_digest": (
            compilation_database.database_digest
            if compilation_database is not None else None
        ),
        "ctu_mode": ctu_mode,
    }
    diagnostics: list[ClangDiagnostic] = []
    analyses: dict[str, Any] = {}
    with tempfile.TemporaryDirectory(prefix="agent-runtime-clang-") as directory:
        root = Path(directory)
        for path, text in sources.items():
            validate_relative_path(path)
            destination = root / path
            destination.parent.mkdir(parents=True, exist_ok=True)
            destination.write_text(text, encoding="utf-8")
        for index, path in enumerate(selected):
            report_path = root / f"analysis-{index}.sarif"
            command = [
                executable,
                "--analyze",
                "-Xanalyzer",
                "-analyzer-checker=core,cplusplus,unix.Malloc",
                "--analyzer-output",
                "sarif",
            ]
            if compilation_database is not None:
                compile_arguments = compilation_database.analyzer_arguments(path, root)
                command.extend(compile_arguments)
                if not any(token.startswith("-std=") for token in compile_arguments):
                    command.append(f"-std={language_standard}")
            else:
                command.append(f"-std={language_standard}")
                command.extend(("-I", str(root)))
                for include_path in include_paths:
                    command.extend(("-I", str(root / include_path)))
            command.extend((str(root / path), "-o", str(report_path)))
            try:
                completed = subprocess.run(
                    command,
                    cwd=root,
                    check=False,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    text=True,
                    timeout=timeout_seconds,
                )
            except subprocess.TimeoutExpired as exc:
                raise BackendFailure(f"Clang analysis timed out for {path}") from exc
            stderr = _normalize_tool_text(completed.stderr, root)
            if completed.returncode != 0 or not report_path.is_file():
                detail = stderr.strip().splitlines()[-1] if stderr.strip() else "no SARIF output"
                raise BackendFailure(f"Clang analysis failed for {path}: {detail}")
            try:
                sarif_text = _normalize_tool_text(
                    report_path.read_text(encoding="utf-8"), root,
                )
                sarif = json.loads(sarif_text)
            except (OSError, json.JSONDecodeError) as exc:
                raise BackendFailure(f"invalid Clang SARIF for {path}") from exc
            normalized_command = [
                Path(executable).name,
                "--analyze",
                "-Xanalyzer",
                "-analyzer-checker=core,cplusplus,unix.Malloc",
                "--analyzer-output",
                "sarif",
            ]
            normalized_command.extend(
                _normalize_tool_text(token, root) for token in command[6:-3]
            )
            normalized_command.append(path)
            analysis_results: list[dict[str, Any]] = []
            for run in sarif.get("runs", ()):
                if not isinstance(run, Mapping):
                    continue
                for result in run.get("results", ()):
                    if not isinstance(result, Mapping):
                        continue
                    result_plain = json.loads(canonical_json(result))
                    analysis_results.append(result_plain)
                    message = _message(result)
                    location = _location(result)
                    if (location is None
                            or not any(pattern in message.casefold() for pattern in _UAF_MESSAGES)):
                        continue
                    line, column, end_line, end_column = location
                    rule_id = str(result.get("ruleId", "clang-analyzer"))
                    diagnostic_path = _diagnostic_path(result, sources, path)
                    identity = {
                        "source_digest": source_digest,
                        "path": diagnostic_path,
                        "translation_unit": path,
                        "line": line,
                        "column": column,
                        "rule_id": rule_id,
                        "message": message,
                    }
                    diagnostics.append(ClangDiagnostic(
                        diagnostic_id=digest(identity),
                        path=diagnostic_path,
                        translation_unit=path,
                        line=line,
                        column=column,
                        end_line=end_line,
                        end_column=end_column,
                        rule_id=rule_id,
                        message=message,
                        sarif_result=result_plain,
                    ))
            analyses[path] = {
                "path": path,
                "command": normalized_command,
                "exit_code": completed.returncode,
                "stderr": stderr,
                "sarif_results": analysis_results,
            }
    diagnostics.sort(key=lambda item: (
        item.path, item.line, item.column, item.translation_unit, item.diagnostic_id,
    ))
    return ClangAnalysisBundle(
        source_digest,
        executable,
        compiler_version,
        digest(profile),
        compilation_database.database_digest if compilation_database else None,
        ctu_mode,
        tuple(diagnostics),
        analyses,
    )


class ClangDiagnosticProgramQuery:
    """Replay exact diagnostics captured from a successful analyzer run."""

    backend_id = "clang-static-analyzer"
    read_only = True
    idempotent_retry = True
    supported_operations = frozenset({"read_clang_diagnostic"})
    operation_specs = {
        "read_clang_diagnostic": QueryOperation(
            "read_clang_diagnostic",
            {"diagnostic_id": "string"},
            ("diagnostic_id",),
            ("diagnostic_id",),
        ),
    }

    def __init__(self, snapshot: FixedSnapshot, bundle: ClangAnalysisBundle) -> None:
        if bundle.source_digest != snapshot.source_digest:
            raise StaleSnapshot("Clang analysis differs from snapshot sources")
        self.snapshot = snapshot
        self.bundle = bundle
        self.backend_version = digest({
            "compiler_version": bundle.compiler_version,
            "command_profile_digest": bundle.command_profile_digest,
            "compilation_database_digest": bundle.compilation_database_digest,
            "ctu_mode": bundle.ctu_mode,
        })[:16]
        self._diagnostics = {item.diagnostic_id: item for item in bundle.diagnostics}

    def query(self, request: QueryRequest) -> ProgramResult:
        if request.snapshot_digest != self.snapshot.snapshot_digest:
            raise StaleSnapshot("Clang query belongs to another snapshot")
        if request.tool_policy_digest != self.snapshot.tool_policy_digest:
            raise PolicyDenied("Clang query policy differs from snapshot")
        if (request.backend_id != self.backend_id
                or request.backend_version != self.backend_version):
            raise InvalidInput("Clang backend binding differs")
        if request.operation not in self.supported_operations:
            raise InvalidInput("unsupported Clang query")
        selectors = request.scope.get("selectors", {})
        self.operation_specs[request.operation].validate(request.args, selectors)
        if request.scope.get("scope_id") != self.snapshot.scope_id:
            raise StaleSnapshot("Clang query scope differs from snapshot")
        diagnostic_id = request.args["diagnostic_id"]
        if selectors["diagnostic_id"] != diagnostic_id:
            raise PolicyDenied("Clang diagnostic is outside the query scope")
        try:
            diagnostic = self._diagnostics[diagnostic_id]
        except KeyError as exc:
            raise PolicyDenied("unknown Clang diagnostic") from exc
        analysis = self.bundle.analyses[diagnostic.translation_unit]
        raw = canonical_json({
            "analyzer": {
                "compiler": Path(self.bundle.compiler).name,
                "compiler_version": self.bundle.compiler_version,
                "command_profile_digest": self.bundle.command_profile_digest,
                "compilation_database_digest": self.bundle.compilation_database_digest,
                "ctu_mode": self.bundle.ctu_mode,
                "command": analysis["command"],
                "exit_code": analysis["exit_code"],
                "stderr": analysis["stderr"],
            },
            "diagnostic": diagnostic.as_dict(),
            "source_digest": self.bundle.source_digest,
        }).encode("utf-8")
        universe = f"Clang UAF diagnostic {diagnostic_id}"
        return ProgramResult(
            "complete",
            Coverage(universe, universe, "complete", (), (bytes_digest(raw),)),
            raw,
        )
