"""Registry-only, fail-closed Docker validation execution.

The query/runtime layer never calls this module.  An application prepares a
closed suite, registers it here, and requests an attempt by ID.  Callers cannot
supply commands, environment, images, mounts, or Docker flags per execution.
"""

from __future__ import annotations

import base64
import hashlib
import json
import re
import shutil
import subprocess
import tempfile
import threading
import uuid
from dataclasses import dataclass
from pathlib import Path
from types import MappingProxyType
from typing import Mapping

from ..codec import canonical_json
from ..errors import EvidenceIntegrityError, InvalidInput
from ..validation import (
    ValidationArtifact,
    ValidationIsolationOutcome,
    ValidationResource,
    ValidationTermination,
)
from ..validation_capture import (
    CapturedProcess,
    ValidationArtifactStore,
    collect_bounded_process,
)

_PINNED_IMAGE = re.compile(r"^[a-z0-9][a-z0-9._/-]*@sha256:[0-9a-f]{64}$")
_IMAGE_ID = re.compile(r"^sha256:[0-9a-f]{64}$")
_ID = re.compile(r"^[a-z0-9][a-z0-9._-]{0,63}$")
_RECIPE_IDS = frozenset(
    {
        "isolation-normal",
        "isolation-nonzero",
        "isolation-signal",
        "isolation-timeout",
        "isolation-output",
        "isolation-space",
        "cpp-peglib-build",
        "cpp-peglib-run-ignored",
        "cpp-peglib-run-ordinary",
    }
)
_RECIPE_OUTPUTS = {
    "cpp-peglib-build": (("binary", "probe"), ("depfile", "probe.d")),
}
_FORBIDDEN_MANIFEST_KEYS = frozenset(
    {
        "command", "commands", "argv", "args", "environment", "env", "cwd",
        "mount", "mounts", "flags", "docker_flags", "entrypoint",
    }
)


def _closed_id(value: object, label: str) -> str:
    if not isinstance(value, str) or not _ID.fullmatch(value):
        raise InvalidInput(f"{label} is invalid")
    return value


def _tree_digest(root: Path) -> str:
    if root.is_symlink() or not root.is_dir():
        raise InvalidInput("validation mount must be a real directory")
    rows = []
    for path in sorted(root.rglob("*")):
        if path.is_symlink():
            raise InvalidInput("validation mounts cannot contain symlinks")
        if path.is_file():
            relative = path.relative_to(root).as_posix()
            rows.append(
                {
                    "path": relative,
                    "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
                    "size_bytes": path.stat().st_size,
                }
            )
        elif not path.is_dir():
            raise InvalidInput("validation mounts can contain only files and directories")
    return hashlib.sha256(canonical_json(rows).encode()).hexdigest()


@dataclass(frozen=True)
class DockerValidationLimits:
    wall_time_ms: int = 30_000
    stdout_bytes: int = 1_048_576
    stderr_bytes: int = 1_048_576
    total_output_bytes: int = 1_572_864
    memory_bytes: int = 536_870_912
    pids: int = 64
    cpus_millis: int = 1000
    tmp_bytes: int = 16_777_216
    work_bytes: int = 33_554_432

    def __post_init__(self) -> None:
        values = tuple(self.__dict__.values())
        if any(type(value) is not int or value < 1 for value in values):
            raise InvalidInput("Docker validation limits must be positive integers")
        if self.total_output_bytes < max(self.stdout_bytes, self.stderr_bytes):
            raise InvalidInput("total output limit is smaller than a stream limit")
        if self.cpus_millis > 64_000:
            raise InvalidInput("Docker validation CPU limit is unreasonable")


@dataclass(frozen=True)
class DockerValidationAttempt:
    attempt_id: str
    recipe_id: str

    def __post_init__(self) -> None:
        _closed_id(self.attempt_id, "validation attempt ID")
        if self.recipe_id not in _RECIPE_IDS:
            raise InvalidInput("validation recipe is not application registered")


@dataclass(frozen=True)
class DockerValidationSuite:
    suite_id: str
    image_ref: str
    image_id: str
    source_root: Path
    runner_root: Path
    source_tree_digest: str
    runner_tree_digest: str
    attempts: tuple[DockerValidationAttempt, ...]
    limits: DockerValidationLimits = DockerValidationLimits()

    def __post_init__(self) -> None:
        _closed_id(self.suite_id, "validation suite ID")
        if not isinstance(self.image_ref, str) or not _PINNED_IMAGE.fullmatch(self.image_ref):
            raise InvalidInput("validation image must be a repository digest reference")
        if not isinstance(self.image_id, str) or not _IMAGE_ID.fullmatch(self.image_id):
            raise InvalidInput("validation image ID is invalid")
        if not isinstance(self.source_root, Path) or not isinstance(self.runner_root, Path):
            raise InvalidInput("validation mounts must be Path objects")
        source = self.source_root.resolve(strict=True)
        runner = self.runner_root.resolve(strict=True)
        if source == runner or source in runner.parents or runner in source.parents:
            raise InvalidInput("validation source and runner mounts must be disjoint")
        if _tree_digest(source) != self.source_tree_digest:
            raise EvidenceIntegrityError("validation source tree digest differs")
        if _tree_digest(runner) != self.runner_tree_digest:
            raise EvidenceIntegrityError("validation runner tree digest differs")
        if not self.attempts or any(
            not isinstance(item, DockerValidationAttempt) for item in self.attempts
        ):
            raise InvalidInput("validation suite must contain typed attempts")
        if len({item.attempt_id for item in self.attempts}) != len(self.attempts):
            raise InvalidInput("validation attempt IDs must be unique")
        object.__setattr__(self, "source_root", source)
        object.__setattr__(self, "runner_root", runner)


@dataclass(frozen=True)
class DockerOutputArtifact:
    name: str
    artifact: ValidationArtifact

    def __post_init__(self) -> None:
        _closed_id(self.name, "Docker output artifact name")
        if not isinstance(self.artifact, ValidationArtifact):
            raise InvalidInput("Docker output artifact reference is malformed")


@dataclass(frozen=True)
class DockerAttemptEvidence:
    attempt_id: str
    isolation: ValidationIsolationOutcome
    termination: ValidationTermination
    stdout: ValidationArtifact
    stderr: ValidationArtifact
    resource_observation: ValidationArtifact
    resources: tuple[ValidationResource, ...]
    exit_code: int | None
    signal: int | None
    cleanup_succeeded: bool
    outputs: tuple[DockerOutputArtifact, ...] = ()


class DockerValidationExecutor:
    """Execute only IDs from immutable, application-owned suite registries."""

    def __init__(self, registry: Mapping[str, DockerValidationSuite]) -> None:
        if not isinstance(registry, Mapping) or not registry:
            raise InvalidInput("Docker validation registry must be nonempty")
        verified = {}
        for key, suite in registry.items():
            if not isinstance(key, str) or not isinstance(suite, DockerValidationSuite):
                raise InvalidInput("Docker validation registry is malformed")
            if key != suite.suite_id:
                raise InvalidInput("Docker validation registry key differs from suite ID")
            verified[key] = suite
        self.registry = MappingProxyType(verified)

    def execute(
        self,
        suite_id: str,
        attempt_id: str,
        artifact_store: ValidationArtifactStore,
    ) -> DockerAttemptEvidence:
        if set((suite_id, attempt_id)) & {""} or not isinstance(
            artifact_store, ValidationArtifactStore
        ):
            raise InvalidInput("Docker validation execution selector is invalid")
        suite = self.registry.get(suite_id)
        if suite is None:
            raise InvalidInput("Docker validation suite is not registered")
        attempt = next((item for item in suite.attempts if item.attempt_id == attempt_id), None)
        if attempt is None:
            raise InvalidInput("Docker validation attempt is not registered")
        self._verify_suite_inputs(suite)
        runtime = self._preflight(suite, artifact_store)
        (
            probe, _probe_stderr, _probe_resource, probe_capture, probe_state,
            probe_cleanup, _probe_outputs,
        ) = self._run_container(
            suite, "isolation-probe", artifact_store, is_probe=True
        )
        probe_termination, _, _ = self._classify(
            probe_capture, probe_state, probe_cleanup
        )
        probe_value = self._parse_probe(artifact_store, probe)
        if (
            probe_termination != ValidationTermination("exit", exit_code=0)
            or not probe_value["passed"]
        ):
            raise InvalidInput("Docker isolation probe failed closed")
        isolation = ValidationIsolationOutcome("passed", runtime, probe)
        stdout, stderr, resource, captured, state, cleanup, outputs = self._run_container(
            suite, attempt.recipe_id, artifact_store, is_probe=False
        )
        self._verify_suite_inputs(suite)
        termination, exit_code, signal_number = self._classify(captured, state, cleanup)
        resource_value = self._read_resource(resource, artifact_store)
        resources = self._resources(suite.limits, captured, resource_value)
        return DockerAttemptEvidence(
            attempt.attempt_id,
            isolation,
            termination,
            stdout,
            stderr,
            resource,
            resources,
            exit_code,
            signal_number,
            cleanup,
            outputs,
        )

    @staticmethod
    def _verify_suite_inputs(suite: DockerValidationSuite) -> None:
        if _tree_digest(suite.source_root) != suite.source_tree_digest:
            raise EvidenceIntegrityError("validation source changed during execution")
        if _tree_digest(suite.runner_root) != suite.runner_tree_digest:
            raise EvidenceIntegrityError("validation runner changed during execution")

    @staticmethod
    def _docker_call(argv: list[str], *, timeout_ms: int = 30_000) -> CapturedProcess:
        try:
            process = subprocess.Popen(
                argv,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                start_new_session=True,
            )
        except OSError as exc:
            raise InvalidInput("Docker client could not be launched") from exc
        return collect_bounded_process(
            process,
            timeout_ms=timeout_ms,
            stdout_limit=1_048_576,
            stderr_limit=1_048_576,
            total_limit=1_572_864,
            kill_process_group=True,
        )

    def _preflight(
        self, suite: DockerValidationSuite, store: ValidationArtifactStore
    ) -> ValidationArtifact:
        if shutil.which("docker") is None:
            raise InvalidInput("Docker is unavailable; host fallback is forbidden")
        version = self._docker_call(
            ["docker", "version", "--format", "{{json .Server}}"]
        )
        image = self._docker_call(["docker", "image", "inspect", suite.image_ref])
        if version.termination != ValidationTermination("exit", exit_code=0):
            raise InvalidInput("Docker daemon is unavailable; host fallback is forbidden")
        if image.termination != ValidationTermination("exit", exit_code=0):
            raise InvalidInput("pinned Docker image is unavailable; pull is forbidden")
        try:
            server = json.loads(version.stdout)
            image_value = json.loads(image.stdout)[0]
        except (UnicodeDecodeError, json.JSONDecodeError, IndexError, TypeError) as exc:
            raise InvalidInput("Docker preflight report is malformed") from exc
        if server.get("Os") != "linux" or image_value.get("Os") != "linux":
            raise InvalidInput("Docker validation requires a Linux daemon and image")
        if image_value.get("Id") != suite.image_id:
            raise EvidenceIntegrityError("pinned Docker image ID differs")
        report = {
            "schema": "agent-runtime/docker-validation-runtime/v1",
            "server": {
                "os": server.get("Os"),
                "arch": server.get("Arch"),
                "version": server.get("Version"),
                "api_version": server.get("ApiVersion"),
            },
            "image_ref": suite.image_ref,
            "image_id": suite.image_id,
            "source_tree_digest": suite.source_tree_digest,
            "runner_tree_digest": suite.runner_tree_digest,
            "profile": self._profile(suite.limits),
        }
        return store.put_json(report)

    @staticmethod
    def _profile(limits: DockerValidationLimits) -> dict:
        return {
            "network": "none",
            "root_filesystem": "read_only",
            "uid": 65532,
            "gid": 65532,
            "capabilities": "none",
            "no_new_privileges": True,
            "pids": limits.pids,
            "memory_bytes": limits.memory_bytes,
            "memory_swap_bytes": limits.memory_bytes,
            "cpus_millis": limits.cpus_millis,
            "tmp_bytes": limits.tmp_bytes,
            "work_bytes": limits.work_bytes,
            "source_mount": "read_only",
            "runner_mount": "read_only",
        }

    def _container_argv(
        self,
        suite: DockerValidationSuite,
        name: str,
        recipe_id: str,
        sentinel_path: str,
    ) -> list[str]:
        limits = suite.limits
        return [
            "docker", "create", "--name", name,
            "--network=none", "--read-only", "--user", "65532:65532",
            "--cap-drop=ALL", "--security-opt=no-new-privileges",
            "--pids-limit", str(limits.pids),
            "--memory", str(limits.memory_bytes),
            "--memory-swap", str(limits.memory_bytes),
            "--cpus", f"{limits.cpus_millis / 1000:.3f}",
            "--ulimit", "nofile=64:64",
            "--tmpfs", f"/tmp:rw,noexec,nosuid,nodev,size={limits.tmp_bytes},mode=1777",
            "--tmpfs", f"/work:rw,nosuid,nodev,size={limits.work_bytes},mode=700,uid=65532,gid=65532",
            "--env", "HOME=/nonexistent", "--env", "LANG=C.UTF-8",
            "--env", "LC_ALL=C.UTF-8",
            "--env", "ASAN_OPTIONS=halt_on_error=1:detect_stack_use_after_return=1",
            "--env", "UBSAN_OPTIONS=halt_on_error=0:print_stacktrace=1",
            "--mount", f"type=bind,src={suite.source_root},dst=/source,readonly",
            "--mount", f"type=bind,src={suite.runner_root},dst=/runner,readonly",
            suite.image_ref,
            "python3", "-I", "/runner/validation_container_runner.py",
            recipe_id, sentinel_path,
        ]

    def _run_container(
        self,
        suite: DockerValidationSuite,
        recipe_id: str,
        store: ValidationArtifactStore,
        *,
        is_probe: bool,
    ):
        name = f"agent-runtime-validation-{uuid.uuid4().hex}"
        with tempfile.TemporaryDirectory(prefix="validation-sentinel-") as directory:
            sentinel = Path(directory) / uuid.uuid4().hex
            sentinel.write_text("not mounted into validation container")
            create = self._docker_call(
                self._container_argv(suite, name, recipe_id, str(sentinel))
            )
        if create.termination != ValidationTermination("exit", exit_code=0):
            raise InvalidInput("Docker container creation failed closed")

        cleanup = False
        try:
            process = subprocess.Popen(
                ["docker", "start", "--attach", name],
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                start_new_session=True,
            )
            timer = None
            if recipe_id == "isolation-signal":
                timer = threading.Timer(0.2, self._signal_kill, args=(name,))
                timer.start()
            try:
                captured = collect_bounded_process(
                    process,
                    timeout_ms=(10_000 if is_probe else suite.limits.wall_time_ms),
                    stdout_limit=suite.limits.stdout_bytes,
                    stderr_limit=suite.limits.stderr_bytes,
                    total_limit=suite.limits.total_output_bytes,
                    on_boundary=lambda _: self._kill(name),
                    kill_process_group=True,
                )
            finally:
                if timer is not None:
                    timer.cancel()
                    timer.join()
            inspect = self._docker_call(["docker", "inspect", name])
            try:
                state = json.loads(inspect.stdout)[0]["State"]
            except (UnicodeDecodeError, json.JSONDecodeError, IndexError, KeyError, TypeError):
                state = {"inspection_error": True}
            resource_bytes = self._copy_resource(name, captured.stdout)
            outputs = self._copy_outputs(
                name, recipe_id, store, captured.stdout,
                size_limit=suite.limits.work_bytes
            )
        finally:
            remove = self._docker_call(["docker", "rm", "--force", name])
            cleanup = remove.termination == ValidationTermination("exit", exit_code=0)

        stdout = store.put(captured.stdout)
        stderr = store.put(captured.stderr)
        resource = store.put(resource_bytes)
        return stdout, stderr, resource, captured, state, cleanup, outputs

    def _copy_outputs(
        self,
        name: str,
        recipe_id: str,
        store: ValidationArtifactStore,
        stdout: bytes,
        *,
        size_limit: int,
    ) -> tuple[DockerOutputArtifact, ...]:
        streamed = {}
        for line in stdout.splitlines():
            if not line.startswith(b'{"content_base64"'):
                continue
            try:
                value = json.loads(line)
                if value.get("schema") != "agent-runtime/docker-output/v1":
                    continue
                name_value = _closed_id(value.get("name"), "Docker output name")
                body = base64.b64decode(value.get("content_base64"), validate=True)
            except (UnicodeDecodeError, json.JSONDecodeError, ValueError, TypeError) as exc:
                raise EvidenceIntegrityError("streamed Docker output is malformed") from exc
            if len(body) > size_limit or name_value in streamed:
                raise EvidenceIntegrityError("streamed Docker output violates its bounds")
            streamed[name_value] = store.put(body)
        collected = []
        for output_name, filename in _RECIPE_OUTPUTS.get(recipe_id, ()):
            if output_name in streamed:
                collected.append(DockerOutputArtifact(output_name, streamed[output_name]))
                continue
            with tempfile.TemporaryDirectory(prefix="validation-output-") as directory:
                target = Path(directory) / filename
                copied = self._docker_call(
                    ["docker", "cp", f"{name}:/work/artifacts/{filename}", str(target)],
                    timeout_ms=10_000,
                )
                if copied.termination != ValidationTermination("exit", exit_code=0):
                    continue
                if target.is_symlink() or not target.is_file():
                    raise EvidenceIntegrityError("Docker output is not a regular file")
                body = target.read_bytes()
                if len(body) > size_limit:
                    raise InvalidInput("Docker output exceeds the writable-space limit")
                collected.append(DockerOutputArtifact(output_name, store.put(body)))
        return tuple(collected)

    def _signal_kill(self, name: str) -> None:
        self._docker_call(
            ["docker", "kill", "--signal", "KILL", name], timeout_ms=5_000
        )

    def _kill(self, name: str) -> None:
        # Boundary classification is preserved here; final forced removal below is
        # the authoritative cleanup gate if this best-effort stop races with exit.
        self._docker_call(["docker", "kill", name], timeout_ms=5_000)

    def _copy_resource(self, name: str, stdout: bytes) -> bytes:
        for line in reversed(stdout.splitlines()):
            try:
                value = json.loads(line)
            except (UnicodeDecodeError, json.JSONDecodeError):
                continue
            if (
                isinstance(value, dict)
                and value.get("schema") == "agent-runtime/docker-resource/v1"
            ):
                return canonical_json(value).encode()
        with tempfile.TemporaryDirectory(prefix="validation-resource-") as directory:
            target = Path(directory) / "resource.json"
            copied = self._docker_call(
                ["docker", "cp", f"{name}:/work/resource.json", str(target)],
                timeout_ms=5_000,
            )
            if copied.termination != ValidationTermination("exit", exit_code=0):
                return canonical_json(
                    {"schema": "agent-runtime/docker-resource/v1", "status": "unavailable"}
                ).encode()
            try:
                body = target.read_bytes()
                value = json.loads(body)
            except (OSError, UnicodeDecodeError, json.JSONDecodeError):
                return canonical_json(
                    {"schema": "agent-runtime/docker-resource/v1", "status": "unavailable"}
                ).encode()
            if not isinstance(value, dict) or value.get("schema") != "agent-runtime/docker-resource/v1":
                return canonical_json(
                    {"schema": "agent-runtime/docker-resource/v1", "status": "unavailable"}
                ).encode()
            return canonical_json(value).encode()

    @staticmethod
    def _parse_probe(
        store: ValidationArtifactStore,
        probe_ref: ValidationArtifact,
    ) -> dict:
        try:
            lines = store.read(probe_ref).decode().splitlines()
            candidates = [
                json.loads(line) for line in lines
                if line.lstrip().startswith("{")
            ]
            value = next(
                item for item in candidates
                if isinstance(item, dict)
                and item.get("schema") == "agent-runtime/isolation-probe/v1"
            )
        except (UnicodeDecodeError, json.JSONDecodeError, StopIteration) as exc:
            raise InvalidInput("Docker isolation probe output is malformed") from exc
        checks = value.get("checks") if isinstance(value, dict) else None
        required = {
            "non_root", "no_new_privileges", "capabilities_empty", "environment_clean",
            "sentinel_unreadable", "network_unavailable", "route_unavailable",
            "root_read_only", "source_read_only", "work_writable", "work_bounded",
        }
        passed = (
            isinstance(checks, dict)
            and set(checks) == required
            and all(item is True for item in checks.values())
        )
        return {"passed": passed, "checks": checks}

    @staticmethod
    def _classify(captured: CapturedProcess, state: dict, cleanup: bool):
        if not cleanup or state.get("inspection_error"):
            return ValidationTermination("cleanup_failed"), None, None
        if captured.termination.kind in {"timeout", "output_limit", "cleanup_failed"}:
            return captured.termination, None, None
        if state.get("OOMKilled") is True:
            return ValidationTermination("oom", oom_killed=True), None, None
        exit_code = state.get("ExitCode")
        if type(exit_code) is not int or exit_code < 0:
            return ValidationTermination("cleanup_failed"), None, None
        if exit_code >= 128 and exit_code <= 255:
            signal_number = exit_code - 128
            return ValidationTermination("signal", signal=signal_number), None, signal_number
        return ValidationTermination("exit", exit_code=exit_code), exit_code, None

    @staticmethod
    def _read_resource(ref: ValidationArtifact, store: ValidationArtifactStore) -> dict:
        try:
            value = json.loads(store.read(ref))
        except (UnicodeDecodeError, json.JSONDecodeError):
            return {"status": "unavailable"}
        return value if isinstance(value, dict) else {"status": "unavailable"}

    @staticmethod
    def _resources(
        limits: DockerValidationLimits, captured: CapturedProcess, value: dict
    ) -> tuple[ValidationResource, ...]:
        def item(kind: str, limit: int, key: str) -> ValidationResource:
            number = value.get(key)
            if type(number) is int and number >= 0:
                return ValidationResource(kind, limit, number)
            return ValidationResource(kind, limit, None, "unavailable")

        return (
            ValidationResource("wall_time_ms", limits.wall_time_ms, captured.wall_time_ms),
            item("cpu_time_usec", limits.wall_time_ms * 1000, "cpu_time_usec"),
            item("memory_peak_bytes", limits.memory_bytes, "memory_peak_bytes"),
            item("pids_peak", limits.pids, "pids_peak"),
            ValidationResource("stdout_bytes", limits.stdout_bytes, len(captured.stdout)),
            ValidationResource("stderr_bytes", limits.stderr_bytes, len(captured.stderr)),
            item("writable_bytes", limits.work_bytes, "writable_bytes"),
        )


def load_docker_validation_suite(
    value: Mapping[str, object], *, source_root: Path, runner_root: Path
) -> DockerValidationSuite:
    """Load the path-free, exact-key portion of an application suite manifest."""

    def reject_forbidden(item: object) -> None:
        if isinstance(item, Mapping):
            if _FORBIDDEN_MANIFEST_KEYS & set(item):
                raise InvalidInput("Docker validation manifest contains execution fields")
            for child in item.values():
                reject_forbidden(child)
        elif isinstance(item, list):
            for child in item:
                reject_forbidden(child)

    reject_forbidden(value)
    expected = {
        "schema", "suite_id", "image_ref", "image_id", "source_tree_digest",
        "runner_tree_digest", "limits", "attempts",
    }
    if not isinstance(value, Mapping) or set(value) != expected:
        raise InvalidInput("Docker validation suite has unknown or missing fields")
    if value["schema"] != "agent-runtime/docker-validation-suite/v1":
        raise InvalidInput("unsupported Docker validation suite schema")
    limit_keys = set(DockerValidationLimits.__dataclass_fields__)
    limits = value["limits"]
    if not isinstance(limits, Mapping) or set(limits) != limit_keys:
        raise InvalidInput("Docker validation limits have unknown or missing fields")
    attempts = value["attempts"]
    if not isinstance(attempts, list):
        raise InvalidInput("Docker validation attempts must be an array")
    parsed_attempts = []
    for item in attempts:
        if not isinstance(item, Mapping) or set(item) != {"attempt_id", "recipe_id"}:
            raise InvalidInput("Docker validation attempt has unknown or missing fields")
        parsed_attempts.append(DockerValidationAttempt(item["attempt_id"], item["recipe_id"]))
    return DockerValidationSuite(
        suite_id=value["suite_id"],
        image_ref=value["image_ref"],
        image_id=value["image_id"],
        source_root=source_root,
        runner_root=runner_root,
        source_tree_digest=value["source_tree_digest"],
        runner_tree_digest=value["runner_tree_digest"],
        attempts=tuple(parsed_attempts),
        limits=DockerValidationLimits(**dict(limits)),
    )
