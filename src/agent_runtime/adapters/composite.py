"""Operation router for composing independent read-only ProgramQuery adapters."""

from __future__ import annotations

from dataclasses import replace

from ..codec import digest
from ..domain import QueryRequest
from ..errors import InvalidInput
from ..ports import ProgramQuery, ProgramResult, QueryOperation


class CompositeProgramQuery:
    """Expose disjoint operations from several backends through one binding."""

    read_only = True

    def __init__(
        self, *backends: ProgramQuery, backend_id: str = "composite-program-query",
    ) -> None:
        if not backend_id or not backends:
            raise InvalidInput("composite backend needs an ID and child backends")
        routes: dict[str, ProgramQuery] = {}
        specs: dict[str, QueryOperation] = {}
        for backend in backends:
            if not backend.read_only:
                raise InvalidInput("composite backend accepts read-only children only")
            for operation in backend.supported_operations:
                if operation in routes:
                    raise InvalidInput(f"duplicate composite operation: {operation}")
                spec = backend.operation_specs.get(operation)
                if not isinstance(spec, QueryOperation):
                    raise InvalidInput("composite child lacks an operation schema")
                routes[operation] = backend
                specs[operation] = spec
        self.backends = tuple(backends)
        self.backend_id = backend_id
        self.backend_version = digest([
            {
                "backend_id": backend.backend_id,
                "backend_version": backend.backend_version,
                "operations": sorted(backend.supported_operations),
            }
            for backend in backends
        ])[:16]
        self.supported_operations = frozenset(routes)
        self.operation_specs = specs
        self.idempotent_retry = all(backend.idempotent_retry for backend in backends)
        self._routes = routes

    def query(self, request: QueryRequest) -> ProgramResult:
        if (request.backend_id != self.backend_id
                or request.backend_version != self.backend_version):
            raise InvalidInput("composite backend binding differs")
        try:
            backend = self._routes[request.operation]
        except KeyError as exc:
            raise InvalidInput("unsupported composite operation") from exc
        return backend.query(replace(
            request,
            backend_id=backend.backend_id,
            backend_version=backend.backend_version,
        ))
