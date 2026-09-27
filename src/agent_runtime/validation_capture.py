"""Capture primitives for application-controlled validation executors.

This module writes immutable artifacts and drains processes that callers have
already constructed.  It deliberately does not accept or execute command lines.
"""

from __future__ import annotations

import os
import selectors
import signal
import subprocess
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from .codec import bytes_digest, canonical_json
from .errors import EvidenceIntegrityError, InvalidInput
from .validation import ValidationArtifact, ValidationTermination


class ValidationArtifactStore:
    """Atomic content-addressed storage compatible with validation records."""

    def __init__(self, root: str | Path) -> None:
        root_path = Path(root)
        if root_path.is_symlink():
            raise InvalidInput("validation artifact root must not be a symlink")
        root_path.mkdir(parents=True, exist_ok=True)
        self.root = root_path.resolve(strict=True)
        if not self.root.is_dir():
            raise InvalidInput("validation artifact root must be a directory")

    def path(self, artifact_digest: str) -> Path:
        if (
            len(artifact_digest) != 64
            or any(character not in "0123456789abcdef" for character in artifact_digest)
        ):
            raise InvalidInput("invalid validation artifact digest")
        return self.root / artifact_digest[:2] / artifact_digest

    def put(self, content: bytes) -> ValidationArtifact:
        if not isinstance(content, bytes):
            raise InvalidInput("validation artifact content must be bytes")
        artifact_digest = bytes_digest(content)
        target = self.path(artifact_digest)
        target.parent.mkdir(parents=True, exist_ok=True)
        if target.is_symlink() or target.parent.is_symlink():
            raise EvidenceIntegrityError("validation artifact path must not be a symlink")
        if target.exists():
            existing = target.read_bytes()
            if existing != content:
                raise EvidenceIntegrityError("existing validation artifact differs")
            return ValidationArtifact(artifact_digest, len(content))
        descriptor, temporary = tempfile.mkstemp(dir=target.parent, prefix=".pending-")
        try:
            with os.fdopen(descriptor, "wb") as output:
                output.write(content)
                output.flush()
                os.fsync(output.fileno())
            os.replace(temporary, target)
            directory = os.open(target.parent, os.O_RDONLY)
            try:
                os.fsync(directory)
            finally:
                os.close(directory)
        finally:
            if os.path.exists(temporary):
                os.unlink(temporary)
        return ValidationArtifact(artifact_digest, len(content))

    def put_json(self, value: object) -> ValidationArtifact:
        return self.put(canonical_json(value).encode("utf-8"))

    def read(self, artifact: ValidationArtifact) -> bytes:
        try:
            content = self.path(artifact.sha256).read_bytes()
        except OSError as exc:
            raise EvidenceIntegrityError(
                f"validation artifact is missing: {artifact.sha256}"
            ) from exc
        if len(content) != artifact.size_bytes or bytes_digest(content) != artifact.sha256:
            raise EvidenceIntegrityError(
                f"validation artifact integrity check failed: {artifact.sha256}"
            )
        return content


@dataclass(frozen=True)
class CapturedProcess:
    """Bounded result from an already-started child process."""

    termination: ValidationTermination
    stdout: bytes
    stderr: bytes
    wall_time_ms: int


def _stop_process(
    process: subprocess.Popen[bytes], *, kill_process_group: bool
) -> None:
    if process.poll() is not None:
        return
    try:
        if kill_process_group:
            os.killpg(process.pid, signal.SIGTERM)
        else:
            process.terminate()
    except (OSError, ProcessLookupError):
        pass
    try:
        process.wait(timeout=1)
    except subprocess.TimeoutExpired:
        try:
            if kill_process_group:
                os.killpg(process.pid, signal.SIGKILL)
            else:
                process.kill()
        except (OSError, ProcessLookupError):
            pass
        process.wait()


def collect_bounded_process(
    process: subprocess.Popen[bytes],
    *,
    timeout_ms: int,
    stdout_limit: int,
    stderr_limit: int,
    total_limit: int,
    on_boundary: Callable[[str], None] | None = None,
    kill_process_group: bool = False,
) -> CapturedProcess:
    """Drain two pipes concurrently and stop on deadline or byte limit.

    The caller owns process construction and may supply ``on_boundary`` to stop
    an associated named container before the local client process is reaped.
    """

    if (
        process.stdout is None
        or process.stderr is None
        or min(timeout_ms, stdout_limit, stderr_limit, total_limit) < 1
        or total_limit < max(stdout_limit, stderr_limit)
    ):
        raise InvalidInput("bounded process capture configuration is invalid")

    started = time.monotonic()
    deadline = started + timeout_ms / 1000
    selector = selectors.DefaultSelector()
    streams = {process.stdout: bytearray(), process.stderr: bytearray()}
    limits = {process.stdout: stdout_limit, process.stderr: stderr_limit}
    for stream in streams:
        os.set_blocking(stream.fileno(), False)
        selector.register(stream, selectors.EVENT_READ)

    boundary: str | None = None
    try:
        while selector.get_map():
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                boundary = "timeout"
                break
            events = selector.select(min(remaining, 0.05))
            if not events and process.poll() is not None:
                events = [(key, selectors.EVENT_READ) for key in selector.get_map().values()]
            for key, _ in events:
                stream = key.fileobj
                try:
                    chunk = os.read(stream.fileno(), 65536)
                except BlockingIOError:
                    continue
                if not chunk:
                    selector.unregister(stream)
                    continue
                target = streams[stream]
                allowed = min(
                    limits[stream] - len(target),
                    total_limit - sum(len(value) for value in streams.values()),
                )
                if allowed > 0:
                    target.extend(chunk[:allowed])
                if len(chunk) > allowed:
                    boundary = "output_limit"
                    break
            if boundary is not None:
                break
    finally:
        selector.close()

    cleanup_failed = False
    if boundary is not None:
        try:
            if on_boundary is not None:
                on_boundary(boundary)
        except Exception:
            cleanup_failed = True
        finally:
            _stop_process(process, kill_process_group=kill_process_group)
    else:
        process.wait()

    elapsed = max(0, round((time.monotonic() - started) * 1000))
    if cleanup_failed:
        termination = ValidationTermination("cleanup_failed")
    elif boundary == "timeout":
        termination = ValidationTermination("timeout")
    elif boundary == "output_limit":
        termination = ValidationTermination("output_limit", output_limited=True)
    elif process.returncode is None:
        termination = ValidationTermination("cleanup_failed")
    elif process.returncode < 0:
        termination = ValidationTermination("signal", signal=-process.returncode)
    else:
        termination = ValidationTermination("exit", exit_code=process.returncode)
    return CapturedProcess(
        termination,
        bytes(streams[process.stdout]),
        bytes(streams[process.stderr]),
        elapsed,
    )
