"""Fixed-surface CodeQL capture replay adapter.

The adapter accepts only a pre-registered normalized result ID.  It owns the
CLI invocation and never accepts model-selected QL, paths, flags, or process
environment values.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import tempfile
import time
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from types import MappingProxyType
from typing import Any
from urllib.parse import unquote

from ..codec import bytes_digest, canonical_json, digest, validate_relative_path
from ..domain import Coverage, FixedSnapshot, QueryRequest
from ..errors import InvalidInput, PolicyDenied, StaleSnapshot
from ..ports import ProgramResult, QueryOperation

_QUERY_RESOURCE = Path(__file__).with_name("codeql_pack") / "PotentialAccessAfterDelete.ql"
_NORMALIZER_VERSION = "2"


def _sha256(value: str, label: str) -> None:
    if len(value) != 64 or any(char not in "0123456789abcdef" for char in value):
        raise InvalidInput(f"{label} must be a lowercase SHA-256 digest")


def _manifest(directory: Path) -> dict[str, str]:
    if not directory.is_dir() or directory.is_symlink():
        raise InvalidInput("CodeQL database must be a real directory")
    files: dict[str, str] = {}
    for root, dirs, names in os.walk(directory, followlinks=False):
        root_path = Path(root)
        if root_path.is_symlink() or any((root_path / name).is_symlink() for name in dirs):
            raise InvalidInput("CodeQL database manifest rejects symlinks")
        for name in names:
            path = root_path / name
            if path.is_symlink() or not path.is_file():
                raise InvalidInput("CodeQL database manifest accepts regular files only")
            relative = path.relative_to(directory).as_posix()
            validate_relative_path(relative)
            files[relative] = bytes_digest(path.read_bytes())
    return dict(sorted(files.items()))


@dataclass(frozen=True)
class CodeQLCaptureBundle:
    """A pre-built database and complete immutable manifest for one snapshot."""

    snapshot_digest: str
    source_digest: str
    compile_db_digest: str
    database_directory: str
    database_manifest: Mapping[str, str]
    manifest_digest: str
    result_ids: tuple[str, ...]
    source_root: str | None = None
    allowed_uri_base_ids: tuple[str, ...] = ()
    query_digest: str | None = None
    query_pack_digest: str | None = None
    query_lock_digest: str | None = None
    compiled_query_digest: str | None = None
    toolchain_manifest_digest: str | None = None

    def __post_init__(self) -> None:
        for value, label in ((self.snapshot_digest, "snapshot"),
                             (self.source_digest, "source"),
                             (self.compile_db_digest, "compile database"),
                             (self.manifest_digest, "manifest")):
            _sha256(value, label)
        if not self.result_ids or len(set(self.result_ids)) != len(self.result_ids):
            raise InvalidInput("CodeQL bundle needs unique registered result IDs")
        for result_id in self.result_ids:
            _sha256(result_id, "result ID")
        database_input = Path(self.database_directory)
        if database_input.is_symlink():
            raise InvalidInput("CodeQL database must not be a symlink")
        try:
            database = database_input.resolve(strict=True)
        except OSError as exc:
            raise InvalidInput("CodeQL database directory is unavailable") from exc
        supplied = dict(self.database_manifest)
        if not supplied or any(not isinstance(path, str) or not isinstance(value, str)
                               for path, value in supplied.items()):
            raise InvalidInput("CodeQL database manifest is invalid")
        for path, value in supplied.items():
            validate_relative_path(path)
            _sha256(value, "database file")
        supplied = dict(sorted(supplied.items()))
        if digest(supplied) != self.manifest_digest:
            raise InvalidInput("CodeQL database manifest digest differs")
        if _manifest(database) != supplied:
            raise StaleSnapshot("CodeQL database differs from capture manifest")
        optional_digests = (
            (self.query_digest, "query"),
            (self.query_pack_digest, "query pack"),
            (self.query_lock_digest, "query lock"),
            (self.compiled_query_digest, "compiled query"),
            (self.toolchain_manifest_digest, "toolchain manifest"),
        )
        for value, label in optional_digests:
            if value is not None:
                _sha256(value, label)
        source_root = None
        if self.source_root is not None:
            source_input = Path(self.source_root)
            if source_input.is_symlink():
                raise InvalidInput("CodeQL source root must not be a symlink")
            try:
                source_root = str(source_input.resolve(strict=True))
            except OSError as exc:
                raise InvalidInput("CodeQL source root is unavailable") from exc
            if not Path(source_root).is_dir():
                raise InvalidInput("CodeQL source root must be a directory")
        if len(set(self.allowed_uri_base_ids)) != len(self.allowed_uri_base_ids) or any(
            not isinstance(value, str) or not value for value in self.allowed_uri_base_ids
        ):
            raise InvalidInput("CodeQL URI base allowlist is invalid")
        object.__setattr__(self, "database_directory", str(database))
        object.__setattr__(self, "database_manifest", MappingProxyType(supplied))
        object.__setattr__(self, "result_ids", tuple(sorted(self.result_ids)))
        object.__setattr__(self, "source_root", source_root)
        object.__setattr__(self, "allowed_uri_base_ids", tuple(sorted(self.allowed_uri_base_ids)))


@dataclass(frozen=True)
class CodeQLReplayConfig:
    """Fixed executable and bounded process resources for replay."""

    cli_path: str
    cli_digest: str
    release: str
    timeout_ms: int = 30_000
    max_output_bytes: int = 1_000_000
    poll_interval_ms: int = 25

    def __post_init__(self) -> None:
        path_input = Path(self.cli_path)
        if path_input.is_symlink():
            raise InvalidInput("CodeQL CLI must not be a symlink")
        try:
            path = path_input.resolve(strict=True)
        except OSError as exc:
            raise InvalidInput("CodeQL CLI path is unavailable") from exc
        _sha256(self.cli_digest, "CodeQL CLI")
        if (not self.release or type(self.timeout_ms) is not int
                or type(self.max_output_bytes) is not int
                or type(self.poll_interval_ms) is not int
                or min(self.timeout_ms, self.max_output_bytes, self.poll_interval_ms) < 1
                or path.is_symlink() or not path.is_file()
                or not os.access(path, os.X_OK)
                or bytes_digest(path.read_bytes()) != self.cli_digest):
            raise InvalidInput("CodeQL replay configuration is invalid")
        object.__setattr__(self, "cli_path", str(path))


class CodeQLReplayProgramQuery:
    """Re-run one package-owned CodeQL query against a captured database."""

    backend_id = "codeql-replay"
    read_only = True
    idempotent_retry = True
    supported_operations = frozenset({"replay_codeql_result_v1"})
    operation_specs = {
        "replay_codeql_result_v1": QueryOperation(
            "replay_codeql_result_v1", {"result_id": "string"}, ("result_id",),
            ("result_id",), max_argument_bytes=512,
        ),
    }

    def __init__(
        self, snapshot: FixedSnapshot, bundle: CodeQLCaptureBundle,
        config: CodeQLReplayConfig,
    ) -> None:
        expected_compile_db = snapshot.inputs.get("compile_db_digest")
        if (snapshot.snapshot_digest != bundle.snapshot_digest
                or snapshot.source_digest != bundle.source_digest
                or not isinstance(expected_compile_db, str)
                or expected_compile_db != bundle.compile_db_digest):
            raise StaleSnapshot("CodeQL capture differs from fixed snapshot")
        if not _QUERY_RESOURCE.is_file() or _QUERY_RESOURCE.is_symlink():
            raise InvalidInput("packaged CodeQL query resource is missing or unsafe")
        self.snapshot = snapshot
        self.bundle = bundle
        self.config = config
        self._query_digest = bytes_digest(_QUERY_RESOURCE.read_bytes())
        if bundle.query_digest is not None and bundle.query_digest != self._query_digest:
            raise StaleSnapshot("packaged CodeQL query differs from capture binding")
        self.backend_version = digest({
            "release": config.release,
            "cli_digest": config.cli_digest,
            "manifest_digest": bundle.manifest_digest,
            "query_digest": self._query_digest,
            "query_pack_digest": bundle.query_pack_digest,
            "query_lock_digest": bundle.query_lock_digest,
            "compiled_query_digest": bundle.compiled_query_digest,
            "toolchain_manifest_digest": bundle.toolchain_manifest_digest,
            "normalizer_version": _NORMALIZER_VERSION,
        })[:16]

    def _verify_inputs(self) -> None:
        if bytes_digest(Path(self.config.cli_path).read_bytes()) != self.config.cli_digest:
            raise StaleSnapshot("CodeQL CLI differs from fixed replay configuration")
        if bytes_digest(_QUERY_RESOURCE.read_bytes()) != self._query_digest:
            raise StaleSnapshot("packaged CodeQL query differs from fixed replay configuration")
        if _manifest(Path(self.bundle.database_directory)) != dict(self.bundle.database_manifest):
            raise StaleSnapshot("CodeQL database differs from capture manifest")

    @staticmethod
    def _uri(
        artifact: Mapping[str, Any], uri_bases: Mapping[str, Any],
        source_root: str | None, allowed_uri_base_ids: tuple[str, ...],
    ) -> str:
        value = artifact.get("uri")
        if not isinstance(value, str) or not value:
            raise InvalidInput("SARIF result lacks a repository-relative URI")
        uri = unquote(value)
        base_id = artifact.get("uriBaseId")
        if base_id is not None:
            if not isinstance(base_id, str) or base_id not in allowed_uri_base_ids:
                raise InvalidInput("SARIF result uses an unregistered URI base")
            base = uri_bases.get(base_id)
            if base is None and base_id == "%SRCROOT%" and source_root is not None:
                pass
            elif not isinstance(base, Mapping) or not isinstance(base.get("uri"), str):
                raise InvalidInput("SARIF URI base is missing or malformed")
            else:
                base_uri = unquote(base["uri"])
                if not base_uri.startswith("file://") or source_root is None:
                    raise InvalidInput("SARIF URI base is not bound to the capture source")
                base_path = Path(base_uri.removeprefix("file://")).resolve()
                if base_path != Path(source_root):
                    raise InvalidInput("SARIF URI base escapes the capture source")
        elif uri.startswith("file://"):
            if source_root is None:
                raise InvalidInput("absolute SARIF URI lacks a bound source root")
            try:
                uri = Path(uri.removeprefix("file://")).resolve().relative_to(
                    Path(source_root)
                ).as_posix()
            except ValueError as exc:
                raise InvalidInput("SARIF URI escapes the capture source") from exc
        for prefix in ("file://$SNAPSHOT_ROOT/", "$SNAPSHOT_ROOT/"):
            if uri.startswith(prefix):
                uri = uri[len(prefix):]
                break
        validate_relative_path(uri)
        return uri

    @classmethod
    def _normalize_result(
        cls, result: Mapping[str, Any], *, uri_bases: Mapping[str, Any] | None = None,
        source_root: str | None = None,
        allowed_uri_base_ids: tuple[str, ...] = (),
    ) -> dict[str, Any]:
        rule_id = result.get("ruleId")
        message = result.get("message")
        locations = result.get("locations")
        if (not isinstance(rule_id, str) or not rule_id or not isinstance(message, Mapping)
                or not isinstance(message.get("text"), str) or not message["text"]
                or not isinstance(locations, list) or not locations
                or not isinstance(locations[0], Mapping)):
            raise InvalidInput("CodeQL SARIF result is malformed")
        physical = locations[0].get("physicalLocation")
        if not isinstance(physical, Mapping):
            raise InvalidInput("CodeQL SARIF result lacks physical location")
        artifact = physical.get("artifactLocation")
        region = physical.get("region")
        if not isinstance(artifact, Mapping) or not isinstance(region, Mapping):
            raise InvalidInput("CodeQL SARIF result location is malformed")
        values = (
            region.get("startLine"), region.get("startColumn", 1),
            region.get("endLine", region.get("startLine")),
            region.get("endColumn", region.get("startColumn", 1)),
        )
        if any(type(value) is not int or value < 1 for value in values):
            raise InvalidInput("CodeQL SARIF region must use positive coordinates")
        if (values[2], values[3]) < (values[0], values[1]):
            raise InvalidInput("CodeQL SARIF region ends before it starts")
        message_text = message["text"]
        tokens = (token.strip("()[]{}<>,.;:\"'") for token in message_text.split())
        if (
            len(message_text.encode("utf-8")) > 4096
            or "\x00" in message_text
            or any(
                token.startswith(("/", "file://"))
                or (len(token) > 2 and token[1] == ":" and token[2] in "\\/")
                for token in tokens
            )
        ):
            raise InvalidInput("CodeQL SARIF message contains unstable host data")
        fingerprints = result.get("partialFingerprints", {})
        if (
            not isinstance(fingerprints, Mapping)
            or len(fingerprints) > 64
            or any(
                not isinstance(key, str)
                or not isinstance(value, str)
                or not key
                or not value
                or len(key.encode("utf-8")) > 256
                or len(value.encode("utf-8")) > 1024
                for key, value in fingerprints.items()
            )
        ):
            raise InvalidInput("CodeQL SARIF fingerprints are malformed")
        stable = {
            "rule_id": rule_id,
            "message": message_text,
            "uri": cls._uri(
                artifact, uri_bases or {}, source_root, allowed_uri_base_ids
            ),
            "region": {
                "start_line": values[0], "start_column": values[1],
                "end_line": values[2], "end_column": values[3],
            },
        }
        normalized = {
            **stable,
            "fingerprints": [
                {"name": key, "value": fingerprints[key]} for key in sorted(fingerprints)
            ],
        }
        return {"result_id": digest(stable), **normalized}

    def _parse_sarif(self, output: Path) -> tuple[dict[str, Any], ...]:
        try:
            content = output.read_bytes()
        except OSError as exc:
            raise InvalidInput("CodeQL did not write SARIF output") from exc
        if len(content) > self.config.max_output_bytes:
            raise InvalidInput("CodeQL SARIF output exceeds fixed limit")
        try:
            document = json.loads(content)
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise InvalidInput("CodeQL output is not valid SARIF JSON") from exc
        runs = document.get("runs") if isinstance(document, Mapping) else None
        if not isinstance(runs, list):
            raise InvalidInput("CodeQL SARIF lacks runs")
        normalized: list[dict[str, Any]] = []
        for run in runs:
            if not isinstance(run, Mapping) or not isinstance(run.get("results", []), list):
                raise InvalidInput("CodeQL SARIF run lacks results")
            uri_bases = run.get("originalUriBaseIds", {})
            if not isinstance(uri_bases, Mapping):
                raise InvalidInput("CodeQL SARIF URI bases are malformed")
            for result in run.get("results", []):
                if not isinstance(result, Mapping):
                    raise InvalidInput("CodeQL SARIF result is malformed")
                normalized.append(self._normalize_result(
                    result, uri_bases=uri_bases,
                    source_root=self.bundle.source_root,
                    allowed_uri_base_ids=self.bundle.allowed_uri_base_ids,
                ))
        identifiers = [item["result_id"] for item in normalized]
        if len(identifiers) != len(set(identifiers)):
            raise InvalidInput("CodeQL SARIF contains duplicate normalized results")
        return tuple(sorted(normalized, key=lambda item: item["result_id"]))

    @staticmethod
    def _file_size(path: Path) -> int:
        try:
            return path.stat().st_size
        except FileNotFoundError:
            return 0

    @staticmethod
    def _bounded_read(path: Path, limit: int) -> bytes:
        try:
            with path.open("rb") as handle:
                return handle.read(limit)
        except FileNotFoundError:
            return b""

    def _run(
        self, output: Path, database: Path, *, timeout_ms: int
    ) -> tuple[str, bytes, bytes]:
        argv = [
            self.config.cli_path,
            "database",
            "analyze",
            str(database),
            str(_QUERY_RESOURCE),
            "--format=sarif-latest",
            f"--output={output}",
            "--threads=1",
        ]
        environment = {"PATH": os.defpath, "LANG": "C", "LC_ALL": "C"}
        stdout_path = output.parent / "codeql.stdout"
        stderr_path = output.parent / "codeql.stderr"
        deadline = time.monotonic() + timeout_ms / 1000
        state = "complete"
        with stdout_path.open("wb") as stdout_handle, stderr_path.open("wb") as stderr_handle:
            process = subprocess.Popen(
                argv,
                stdin=subprocess.DEVNULL,
                stdout=stdout_handle,
                stderr=stderr_handle,
                shell=False,
                env=environment,
                cwd=str(output.parent),
            )
            while process.poll() is None:
                total_size = sum(
                    self._file_size(path)
                    for path in (output, stdout_path, stderr_path)
                )
                if total_size > self.config.max_output_bytes:
                    state = "failed"
                    break
                if time.monotonic() >= deadline:
                    state = "timeout"
                    break
                time.sleep(self.config.poll_interval_ms / 1000)
            if process.poll() is None:
                process.terminate()
                try:
                    process.wait(
                        timeout=max(0.1, self.config.poll_interval_ms / 1000)
                    )
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait()
            elif process.returncode != 0:
                state = "failed"
        stdout = self._bounded_read(stdout_path, self.config.max_output_bytes)
        stderr = self._bounded_read(stderr_path, self.config.max_output_bytes)
        if sum(
            self._file_size(path) for path in (output, stdout_path, stderr_path)
        ) > self.config.max_output_bytes:
            state = "failed"
        return state, stdout, stderr

    def query(self, request: QueryRequest) -> ProgramResult:
        if request.snapshot_digest != self.snapshot.snapshot_digest:
            raise StaleSnapshot("CodeQL replay belongs to another snapshot")
        if request.tool_policy_digest != self.snapshot.tool_policy_digest:
            raise PolicyDenied("CodeQL replay policy differs from snapshot")
        if (request.backend_id != self.backend_id
                or request.backend_version != self.backend_version):
            raise InvalidInput("CodeQL replay backend binding differs")
        if request.operation != "replay_codeql_result_v1":
            raise InvalidInput("unsupported CodeQL replay operation")
        selectors = request.scope.get("selectors", {})
        if set(selectors) != {"result_id"}:
            raise InvalidInput("CodeQL replay accepts only the registered result selector")
        self.operation_specs[request.operation].validate(request.args, selectors)
        if request.scope.get("scope_id") != self.snapshot.scope_id:
            raise StaleSnapshot("CodeQL replay scope differs from snapshot")
        result_id = request.args["result_id"]
        if selectors["result_id"] != result_id or result_id not in self.bundle.result_ids:
            raise PolicyDenied("CodeQL result is outside the registered capture")
        self._verify_inputs()
        with tempfile.TemporaryDirectory(prefix="agent-runtime-codeql-") as directory:
            scratch = Path(directory)
            output = scratch / "result.sarif"
            scratch_database = scratch / "database"
            shutil.copytree(self.bundle.database_directory, scratch_database)
            status, stdout, stderr = self._run(
                output, scratch_database,
                timeout_ms=min(request.timeout_ms, self.config.timeout_ms),
            )
            self._verify_inputs()
            if status == "timeout":
                return ProgramResult(
                    "timeout", Coverage("registered CodeQL signal", "", "unknown"),
                    limitations=("fixed CodeQL replay deadline exceeded",),
                    diagnostic=(stderr or stdout).decode("utf-8", "replace")[:500],
                )
            if status == "failed":
                return ProgramResult(
                    "failed", Coverage("registered CodeQL signal", "", "unknown"),
                    limitations=("fixed CodeQL replay failed or exceeded output limit",),
                    diagnostic=(stderr or stdout).decode("utf-8", "replace")[:500],
                )
            try:
                normalized = self._parse_sarif(output)
            except InvalidInput as exc:
                return ProgramResult(
                    "failed", Coverage("registered CodeQL signal", "", "unknown"),
                    limitations=("CodeQL emitted malformed or oversized SARIF",),
                    diagnostic=str(exc),
                )
        matching = next(
            (item for item in normalized if item["result_id"] == result_id), None
        )
        raw = canonical_json({
            "backend": {
                "id": self.backend_id,
                "version": self.backend_version,
                "tool_digest": self.config.cli_digest,
                "tool_release": self.config.release,
                "database_manifest_digest": self.bundle.manifest_digest,
                "query_digest": self._query_digest,
                "query_pack_digest": self.bundle.query_pack_digest,
                "query_lock_digest": self.bundle.query_lock_digest,
                "compiled_query_digest": self.bundle.compiled_query_digest,
                "toolchain_manifest_digest": self.bundle.toolchain_manifest_digest,
                "normalizer_version": _NORMALIZER_VERSION,
            },
            "source_digest": self.bundle.source_digest,
            "compile_database_digest": self.bundle.compile_db_digest,
            "limits": {
                "timeout_ms": min(request.timeout_ms, self.config.timeout_ms),
                "max_output_bytes": self.config.max_output_bytes,
            },
            "requested_result_id": result_id,
            "result": matching,
        }).encode("utf-8")
        if matching is None:
            return ProgramResult(
                "partial",
                Coverage("registered CodeQL signal", "no matching normalized result", "partial",
                         ("the fixed query did not reproduce this registered signal",),
                         (bytes_digest(raw),)),
                raw,
                ("absence of a CodeQL signal is not negative semantic evidence",),
            )
        universe = f"registered CodeQL signal {result_id}"
        return ProgramResult(
            "complete", Coverage(universe, universe, "complete", (), (bytes_digest(raw),)), raw,
            ("complete coverage applies only to this fixed CodeQL candidate signal",),
        )
