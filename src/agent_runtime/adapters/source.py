"""Read bounded source windows from an immutable, application-owned snapshot.

Complete coverage means the requested text was returned, not that its semantics
or build configuration was analyzed. No repository files or commands are opened.
"""

from __future__ import annotations

from collections.abc import Mapping
from types import MappingProxyType

from ..codec import bytes_digest, canonical_json, digest, validate_relative_path
from ..domain import Coverage, FixedSnapshot, QueryRequest
from ..errors import InvalidInput, PolicyDenied, StaleSnapshot
from ..ports import ProgramResult, QueryOperation


class FrozenSourceProgramQuery:
    backend_id = "frozen-source"
    backend_version = "1"
    read_only = True
    idempotent_retry = True
    supported_operations = frozenset({"read_source"})
    operation_specs = {
        "read_source": QueryOperation(
            "read_source",
            {"path": "string", "start_line": "integer", "end_line": "integer"},
            ("path", "start_line", "end_line"),
            ("path",),
        ),
    }

    @staticmethod
    def source_digest(sources: Mapping[str, str]) -> str:
        for path, text in sources.items():
            validate_relative_path(path)
            if not isinstance(text, str):
                raise InvalidInput("frozen sources must contain text")
        return digest(dict(sources))

    def __init__(self, snapshot: FixedSnapshot, sources: Mapping[str, str], *,
                 max_lines: int = 200, max_bytes: int = 16_384) -> None:
        if self.source_digest(sources) != snapshot.source_digest:
            raise StaleSnapshot("source contents differ from snapshot")
        if type(max_lines) is not int or type(max_bytes) is not int or min(max_lines, max_bytes) < 1:
            raise InvalidInput("source query limits must be positive integers")
        self.snapshot = snapshot
        self.sources = MappingProxyType(dict(sources))
        self.max_lines = max_lines
        self.max_bytes = max_bytes

    def query(self, request: QueryRequest) -> ProgramResult:
        if request.snapshot_digest != self.snapshot.snapshot_digest:
            raise StaleSnapshot("source query belongs to another snapshot")
        if request.tool_policy_digest != self.snapshot.tool_policy_digest:
            raise PolicyDenied("source query policy differs from snapshot")
        if request.backend_id != self.backend_id or request.backend_version != self.backend_version:
            raise InvalidInput("source backend binding differs")
        if request.operation not in self.supported_operations:
            raise InvalidInput("unsupported source query")
        selectors = request.scope.get("selectors", {})
        self.operation_specs[request.operation].validate(request.args, selectors)
        if request.scope.get("scope_id") != self.snapshot.scope_id:
            raise StaleSnapshot("source query scope differs from snapshot")
        path = request.args["path"]
        validate_relative_path(path)
        if selectors["path"] != path or path not in self.sources:
            raise PolicyDenied("source path is outside the query scope")
        start, end = request.args["start_line"], request.args["end_line"]
        lines = self.sources[path].splitlines()
        if not 1 <= start <= end <= len(lines) or end - start + 1 > self.max_lines:
            raise InvalidInput("source window is outside the file or exceeds line limit")
        raw = canonical_json({
            "path": path, "start_line": start, "end_line": end,
            "source_digest": bytes_digest(self.sources[path].encode("utf-8")),
            "lines": [{"line": index + 1, "text": lines[index]}
                      for index in range(start - 1, end)],
            "trust": "untrusted-source",
            "semantic_coverage": "not_assessed",
        }).encode("utf-8")
        if len(raw) > self.max_bytes:
            raise InvalidInput("source window exceeds byte limit; request a smaller window")
        universe = f"{path}:{start}-{end} literal source text"
        return ProgramResult(
            "complete", Coverage(universe, universe, "complete", (), (bytes_digest(raw),)),
            raw, ("text retrieval does not prove control flow, preprocessing or build coverage",),
        )
