"""Canonical JSON and schema helpers for persisted domain data."""

from __future__ import annotations

import dataclasses
import hashlib
import json
from collections.abc import Mapping
from datetime import datetime, timezone
from pathlib import PurePosixPath
from types import MappingProxyType
from typing import Any

from .errors import InvalidInput

SCHEMA_VERSION = {"major": 1, "minor": 1}


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def to_plain(value: Any) -> Any:
    if dataclasses.is_dataclass(value):
        return {
            field.name: to_plain(getattr(value, field.name))
            for field in dataclasses.fields(value)
        }
    if isinstance(value, tuple):
        return [to_plain(item) for item in value]
    if isinstance(value, list):
        return [to_plain(item) for item in value]
    if isinstance(value, Mapping):
        return {str(key): to_plain(item) for key, item in value.items()}
    return value


def freeze_value(value: Any) -> Any:
    """Detach a public identity/snapshot from mutable caller-owned containers."""
    if isinstance(value, Mapping):
        if any(not isinstance(key, str) or not key for key in value):
            raise InvalidInput("structured keys must be nonempty strings")
        return MappingProxyType({key: freeze_value(item) for key, item in value.items()})
    if isinstance(value, (list, tuple)):
        return tuple(freeze_value(item) for item in value)
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    raise InvalidInput("structured value is not JSON compatible")


def canonical_json(value: Any) -> str:
    try:
        return json.dumps(
            to_plain(value),
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        )
    except (TypeError, ValueError) as exc:
        raise InvalidInput(f"value cannot be canonically serialized: {exc}") from exc


def digest(value: Any) -> str:
    return hashlib.sha256(canonical_json(value).encode("utf-8")).hexdigest()


def bytes_digest(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def validate_relative_path(path: str) -> None:
    parsed = PurePosixPath(path)
    if (
        not path
        or path.startswith("/")
        or "\\" in path
        or ".." in parsed.parts
        or str(parsed) != path
    ):
        raise InvalidInput(f"invalid repository-relative POSIX path: {path!r}")


def validate_schema(value: dict[str, Any]) -> None:
    version = value.get("schema_version")
    if (not isinstance(version, dict)
            or type(version.get("major")) is not int
            or version["major"] != SCHEMA_VERSION["major"]):
        raise InvalidInput("unsupported schema major version")
    minor = version.get("minor")
    if (type(minor) is not int or minor < 0
            or minor > SCHEMA_VERSION["minor"]):
        raise InvalidInput("unsupported schema minor version")
