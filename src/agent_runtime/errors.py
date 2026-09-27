"""Stable errors exposed by the runtime API (SPEC 002)."""

from __future__ import annotations


class RuntimeFailure(Exception):
    code = "RuntimeFailure"

    def __init__(self, message: str, *, retryable: bool = False) -> None:
        super().__init__(message)
        self.retryable = retryable


class InvalidInput(RuntimeFailure):
    code = "InvalidInput"


class ContextBudgetExceeded(InvalidInput):
    code = "ContextBudgetExceeded"


class Conflict(RuntimeFailure):
    code = "Conflict"


class CapabilityUnavailable(RuntimeFailure):
    code = "CapabilityUnavailable"


class PolicyDenied(RuntimeFailure):
    code = "PolicyDenied"


class StaleSnapshot(RuntimeFailure):
    code = "StaleSnapshot"


class EvidenceIntegrityError(RuntimeFailure):
    code = "EvidenceIntegrityError"


class SessionUnavailable(RuntimeFailure):
    code = "SessionUnavailable"


class BackendFailure(RuntimeFailure):
    code = "BackendFailure"


class StorageFailure(RuntimeFailure):
    code = "StorageFailure"
