"""Reusable candidate discovery implementations."""

from __future__ import annotations

import difflib
import re
from collections.abc import Mapping
from pathlib import PurePosixPath
from typing import TYPE_CHECKING, Any

from .codec import digest, freeze_value, to_plain
from .domain import (
    CandidateIdentity,
    DiscoveredCandidate,
    DiscoveryInput,
    DiscoveryOrigin,
    EvidencePlan,
)
from .errors import InvalidInput, StaleSnapshot

if TYPE_CHECKING:
    from .adapters.clang import ClangAnalysisBundle

RETURN_LINE = re.compile(r"^\s*return(?:\s+[^;]+)?;\s*(?://.*)?$")
IMPLEMENTATION_SUFFIXES = frozenset({".c", ".cc", ".cpp", ".cxx"})


class PortfolioDiscoverer:
    """Deterministically fuse exact identities from several discoverers."""

    discoverer_id = "portfolio"
    discoverer_version = "1"

    def __init__(self, *discoverers: Any) -> None:
        if not discoverers:
            raise InvalidInput("portfolio needs at least one discoverer")
        keyed = []
        for discoverer in discoverers:
            identifier = getattr(discoverer, "discoverer_id", "")
            version = getattr(discoverer, "discoverer_version", "")
            if not identifier or not version:
                raise InvalidInput("portfolio child needs ID and version")
            keyed.append(((identifier, version), discoverer))
        if len({key for key, _ in keyed}) != len(keyed):
            raise InvalidInput("portfolio has duplicate discoverer ID/version")
        self.children = tuple(item for _key, item in sorted(keyed))

    @staticmethod
    def _origin(candidate: DiscoveredCandidate, discoverer: Any) -> DiscoveryOrigin:
        return DiscoveryOrigin(
            discoverer.discoverer_id,
            discoverer.discoverer_version,
            candidate.candidate_id,
            candidate.discovery_ref,
            digest(candidate.material),
            tuple(sorted(plan.plan_digest for plan in candidate.evidence_plans)),
        )

    def discover(self, input: DiscoveryInput) -> tuple[DiscoveredCandidate, ...]:
        grouped: dict[str, list[tuple[DiscoveredCandidate, Any]]] = {}
        for child in self.children:
            candidates = tuple(child.discover(input))
            candidate_ids = [candidate.candidate_id for candidate in candidates]
            if len(candidate_ids) != len(set(candidate_ids)):
                raise InvalidInput("portfolio child returned duplicate candidate IDs")
            for candidate in candidates:
                if not isinstance(candidate, DiscoveredCandidate):
                    raise InvalidInput("portfolio child returned an invalid candidate")
                if "_portfolio" in candidate.material:
                    raise InvalidInput("portfolio child material reserves _portfolio")
                grouped.setdefault(candidate.identity.candidate_digest, []).append(
                    (candidate, child)
                )

        fused: list[DiscoveredCandidate] = []
        for candidate_digest in sorted(grouped):
            entries = sorted(
                grouped[candidate_digest],
                key=lambda item: (
                    item[1].discoverer_id,
                    item[1].discoverer_version,
                    item[0].candidate_id,
                ),
            )
            primary, _primary_discoverer = entries[0]
            plans: dict[str, EvidencePlan] = {}
            origin_entries: dict[str, tuple[DiscoveryOrigin, dict[str, Any]]] = {}
            for candidate, discoverer in entries:
                if candidate.identity != primary.identity:
                    raise InvalidInput("candidate digest collision has unequal identity")
                child_origins = candidate.origins or (
                    self._origin(candidate, discoverer),
                )
                for origin in child_origins:
                    origin_entries.setdefault(
                        digest(origin), (origin, dict(candidate.material))
                    )
                for plan in candidate.evidence_plans:
                    existing = plans.get(plan.check_id)
                    if existing is not None and existing != plan:
                        raise InvalidInput(
                            f"portfolio evidence plans conflict for check: {plan.check_id}"
                        )
                    plans[plan.check_id] = plan
            ordered_entries = tuple(
                sorted(
                    origin_entries.values(),
                    key=lambda item: (
                        item[0].discoverer_id,
                        item[0].discoverer_version,
                        item[0].origin_candidate_id,
                        item[0].discovery_ref,
                    ),
                )
            )
            ordered_origins = tuple(origin for origin, _material in ordered_entries)
            fused_id = digest(
                {
                    "portfolio_version": self.discoverer_version,
                    "candidate_digest": candidate_digest,
                }
            )
            material = {
                **dict(primary.material),
                "_portfolio": {
                    "candidate_digest": candidate_digest,
                    "origins": [
                        {"origin": to_plain(origin), "material": to_plain(origin_material)}
                        for origin, origin_material in ordered_entries
                    ],
                },
            }
            fused.append(
                DiscoveredCandidate(
                    fused_id,
                    primary.identity,
                    f"portfolio:{fused_id}",
                    tuple(plans[key] for key in sorted(plans)),
                    material,
                    ordered_origins,
                )
            )
        return tuple(fused)

def _code_line(line: str) -> str:
    line = line.split("//", 1)[0]
    line = re.sub(r'"(?:\\.|[^"\\])*"', '""', line)
    line = re.sub(r"'(?:\\.|[^'\\])*'", "''", line)
    return line.strip()


def find_unreachable_after_return(text: str, path: str) -> list[dict[str, Any]]:
    """Find a statement following an unconditional return in the same block."""

    if not isinstance(text, str):
        raise InvalidInput("C/C++ discovery needs source text")
    if PurePosixPath(path).suffix not in IMPLEMENTATION_SUFFIXES:
        return []
    lines = text.splitlines()
    depths: list[int] = []
    depth = 0
    block_comment = False
    cleaned: list[str] = []
    for raw in lines:
        line = raw
        if block_comment:
            if "*/" not in line:
                cleaned.append("")
                depths.append(depth)
                continue
            line = line.split("*/", 1)[1]
            block_comment = False
        while "/*" in line:
            before, after = line.split("/*", 1)
            if "*/" in after:
                line = before + after.split("*/", 1)[1]
            else:
                line = before
                block_comment = True
                break
        code = _code_line(line)
        depths.append(depth)
        cleaned.append(code)
        depth = max(depth + code.count("{") - code.count("}"), 0)

    findings: list[dict[str, Any]] = []
    for index, code in enumerate(cleaned):
        if not RETURN_LINE.match(code) or depths[index] < 1:
            continue
        return_depth = depths[index]
        for next_index in range(index + 1, len(cleaned)):
            following = cleaned[next_index]
            if not following or following.startswith("#"):
                continue
            if depths[next_index] < return_depth:
                break
            if following.startswith("}") and depths[next_index] == return_depth:
                break
            if depths[next_index] == return_depth:
                finding = {
                    "path": path,
                    "return_line": index + 1,
                    "following_line": next_index + 1,
                    "return_text": code,
                    "following_text": following[:200],
                }
                finding["candidate_id"] = digest(finding)
                findings.append(finding)
                break
    return findings


def added_line_numbers(base_text: str, head_text: str) -> set[int]:
    """Return one-based head line numbers introduced or replaced by a change."""

    matcher = difflib.SequenceMatcher(
        a=base_text.splitlines(), b=head_text.splitlines(), autojunk=True,
    )
    added: set[int] = set()
    for tag, _base_start, _base_end, head_start, head_end in matcher.get_opcodes():
        if tag in {"insert", "replace"}:
            added.update(range(head_start + 1, head_end + 1))
    return added


class CppUnreachableDiscoverer:
    """Conservative lexical discovery for code after a newly added return.

    Optional recorded Joern provenance adds a semantic evidence plan for an
    exact candidate. It does not change the lexical candidate set.
    """

    discoverer_id = "cpp.unreachable-after-return"
    discoverer_version = "1"

    def __init__(
        self,
        *,
        rule_id: str = "cpp.unreachable-after-return",
        rule_version: str = "1",
        source_window_radius: int = 12,
        recorded_joern: Mapping[str, Mapping[str, Any]] | None = None,
    ) -> None:
        if (not rule_id or not rule_version or type(source_window_radius) is not int
                or not 0 <= source_window_radius <= 500):
            raise InvalidInput("invalid C++ unreachable discoverer configuration")
        self.rule_id = rule_id
        self.rule_version = rule_version
        self.source_window_radius = source_window_radius
        self.recorded_joern = freeze_value(recorded_joern or {})

    def discover(self, input: DiscoveryInput) -> tuple[DiscoveredCandidate, ...]:
        findings = []
        for path in sorted(input.head_sources):
            if PurePosixPath(path).suffix not in IMPLEMENTATION_SUFFIXES:
                continue
            changed = added_line_numbers(input.base_sources.get(path, ""), input.head_sources[path])
            findings.extend(
                item for item in find_unreachable_after_return(input.head_sources[path], path)
                if item["return_line"] in changed
            )
        candidates = []
        for finding in findings:
            path = finding["path"]
            line_count = len(input.head_sources[path].splitlines())
            plans = [EvidencePlan(
                "source_window",
                "read_source",
                {"path": path},
                {
                    "path": path,
                    "start_line": max(1, finding["return_line"] - self.source_window_radius),
                    "end_line": min(line_count, finding["following_line"] + self.source_window_radius),
                },
                "source_window",
                "positive",
            )]
            provenance = self.recorded_joern.get(finding["candidate_id"])
            if provenance is not None:
                if (not isinstance(provenance, Mapping)
                        or not isinstance(provenance.get("report_digest"), str)
                        or not isinstance(provenance.get("cpg_sha256"), str)):
                    raise InvalidInput("recorded Joern provenance is incomplete")
                plans.append(EvidencePlan(
                    "semantic_validation",
                    "read_recorded_joern",
                    {"candidate_id": finding["candidate_id"]},
                    {
                        "candidate_id": finding["candidate_id"],
                        "report_digest": provenance["report_digest"],
                        "cpg_sha256": provenance["cpg_sha256"],
                    },
                    "semantic_unreachable",
                    "unknown",
                ))
            identity = CandidateIdentity(
                self.rule_id,
                self.rule_version,
                input.snapshot.snapshot_digest,
                {
                    "path": path,
                    "line": finding["return_line"],
                    "following_line": finding["following_line"],
                },
                f"{path}:{finding['return_line']}",
            )
            candidates.append(DiscoveredCandidate(
                finding["candidate_id"],
                identity,
                f"{self.discoverer_id}@{self.discoverer_version}:{finding['candidate_id']}",
                tuple(plans),
                finding,
            ))
        return tuple(candidates)


class ClangUseAfterFreeDiscoverer:
    """Create precise UAF candidates from a frozen Clang analysis bundle."""

    discoverer_id = "clang.use-after-free"
    discoverer_version = "1"

    def __init__(
        self,
        bundle: ClangAnalysisBundle,
        *,
        rule_id: str = "cpp.use-after-free",
        rule_version: str = "1",
        source_window_radius: int = 6,
    ) -> None:
        if (not rule_id or not rule_version or type(source_window_radius) is not int
                or not 0 <= source_window_radius <= 500):
            raise InvalidInput("invalid Clang UAF discoverer configuration")
        self.bundle = bundle
        self.rule_id = rule_id
        self.rule_version = rule_version
        self.source_window_radius = source_window_radius

    def discover(self, input: DiscoveryInput) -> tuple[DiscoveredCandidate, ...]:
        if self.bundle.source_digest != input.snapshot.source_digest:
            raise StaleSnapshot("Clang discovery differs from snapshot sources")
        candidates = []
        for diagnostic in self.bundle.diagnostics:
            try:
                line_count = len(input.head_sources[diagnostic.path].splitlines())
            except KeyError as exc:
                raise StaleSnapshot("Clang diagnostic path is absent from snapshot") from exc
            if diagnostic.line > line_count:
                raise StaleSnapshot("Clang diagnostic line is outside snapshot source")
            identity = CandidateIdentity(
                self.rule_id,
                self.rule_version,
                input.snapshot.snapshot_digest,
                {
                    "path": diagnostic.path,
                    "line": diagnostic.line,
                    "column": diagnostic.column,
                    "diagnostic_id": diagnostic.diagnostic_id,
                    "translation_unit": diagnostic.translation_unit,
                },
                f"{diagnostic.path}:{diagnostic.line}:{diagnostic.column}",
            )
            plans = (
                EvidencePlan(
                    "source_window",
                    "read_source",
                    {"path": diagnostic.path},
                    {
                        "path": diagnostic.path,
                        "start_line": max(1, diagnostic.line - self.source_window_radius),
                        "end_line": min(line_count, diagnostic.end_line + self.source_window_radius),
                    },
                    "source_window",
                    "positive",
                ),
                EvidencePlan(
                    "analyzer_diagnostic",
                    "read_clang_diagnostic",
                    {"diagnostic_id": diagnostic.diagnostic_id},
                    {"diagnostic_id": diagnostic.diagnostic_id},
                    "clang_use_after_free",
                    "positive",
                ),
            )
            candidates.append(DiscoveredCandidate(
                diagnostic.diagnostic_id,
                identity,
                f"{self.discoverer_id}@{self.discoverer_version}:{diagnostic.diagnostic_id}",
                plans,
                {
                    "path": diagnostic.path,
                    "line": diagnostic.line,
                    "column": diagnostic.column,
                    "rule_id": diagnostic.rule_id,
                    "message": diagnostic.message,
                    "translation_unit": diagnostic.translation_unit,
                    "compiler_version": self.bundle.compiler_version,
                },
            ))
        return tuple(candidates)
