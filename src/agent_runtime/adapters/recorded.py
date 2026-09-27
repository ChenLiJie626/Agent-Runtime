"""Replay verified semantic observations through the ProgramQuery contract."""

from __future__ import annotations

from collections.abc import Mapping

from ..codec import bytes_digest, freeze_value
from ..domain import Coverage, FixedSnapshot, QueryRequest
from ..errors import InvalidInput, PolicyDenied, StaleSnapshot
from ..ports import ProgramResult, QueryOperation


class RecordedJoernProgramQuery:
    backend_id = "recorded-joern"
    read_only = True
    idempotent_retry = True
    supported_operations = frozenset({"read_recorded_joern"})
    operation_specs = {
        "read_recorded_joern": QueryOperation(
            "read_recorded_joern",
            {"candidate_id": "string", "report_digest": "string", "cpg_sha256": "string"},
            ("candidate_id", "report_digest", "cpg_sha256"),
            ("candidate_id",),
        ),
    }

    def __init__(self, snapshot: FixedSnapshot, bundle: Mapping) -> None:
        required = {"backend_version", "report_digest", "cpg_sha256", "observations"}
        if (set(bundle) < required or not isinstance(bundle["observations"], Mapping)
                or not bundle["observations"]):
            raise InvalidInput("recorded Joern bundle is incomplete")
        self.snapshot = snapshot
        self.backend_version = bundle["backend_version"]
        self.report_digest = bundle["report_digest"]
        self.cpg_sha256 = bundle["cpg_sha256"]
        self.build_status = bundle.get("build_status", "not_run")
        observations = {}
        for candidate_id, item in bundle["observations"].items():
            if (not isinstance(candidate_id, str) or not candidate_id
                    or not isinstance(item, Mapping)
                    or not isinstance(item.get("observation"), Mapping)
                    or not isinstance(item.get("raw"), bytes)):
                raise InvalidInput("recorded Joern observation is malformed")
            observations[candidate_id] = {
                "observation": freeze_value(item["observation"]),
                "raw": bytes(item["raw"]),
            }
        self.observations = observations

    def query(self, request: QueryRequest) -> ProgramResult:
        if request.snapshot_digest != self.snapshot.snapshot_digest:
            raise StaleSnapshot("recorded Joern result belongs to another snapshot")
        if request.tool_policy_digest != self.snapshot.tool_policy_digest:
            raise PolicyDenied("recorded Joern query policy differs from snapshot")
        if request.backend_id != self.backend_id or request.backend_version != self.backend_version:
            raise InvalidInput("recorded Joern backend binding differs")
        if request.operation not in self.supported_operations:
            raise InvalidInput("unsupported recorded Joern query")
        selectors = request.scope.get("selectors", {})
        self.operation_specs[request.operation].validate(request.args, selectors)
        if request.scope.get("scope_id") != self.snapshot.scope_id:
            raise StaleSnapshot("recorded Joern query scope differs from snapshot")
        candidate_id = request.args["candidate_id"]
        if selectors["candidate_id"] != candidate_id or candidate_id not in self.observations:
            raise PolicyDenied("candidate is outside the recorded Joern scope")
        if (request.args["report_digest"] != self.report_digest
                or request.args["cpg_sha256"] != self.cpg_sha256):
            raise StaleSnapshot("recorded Joern provenance differs from the verified report")
        item = self.observations[candidate_id]
        observation, raw = item["observation"], item["raw"]
        omissions = tuple(observation.get("omissions", ()))
        limitations = tuple(observation.get("limitations", ()))
        if not omissions or not limitations:
            raise InvalidInput("recorded Joern partial evidence needs limitations")
        source_mode = "captured compile-database CPG" if self.build_status != "not_run" else "surface-only CPG"
        return ProgramResult(
            "partial",
            Coverage(
                f"{observation['path']}:{observation['return_line']}->"
                f"{observation['following_line']} in {source_mode}",
                "fixed Joern method, return, following node and reverse CFG groups",
                "partial",
                omissions,
                (bytes_digest(raw),),
            ),
            raw,
            limitations,
        )
