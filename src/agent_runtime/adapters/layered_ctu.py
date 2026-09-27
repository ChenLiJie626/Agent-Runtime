"""Plan a full non-CTU pass followed by defect-targeted CTU passes.

The planner is deliberately execution-tool agnostic.  It consumes the frozen
compilation database already used by the runtime, builds a conservative static
include graph for each translation unit, and emits two compilation databases:

* every translation unit for the inexpensive non-CTU layer;
* only translation units that are, or include, a target path for the CTU layer.

Conditional includes are treated as reachable.  This may select an extra
translation unit, but it cannot silently discard one merely because the
preprocessor condition was not evaluated by this lightweight planning step.
"""

from __future__ import annotations

import json
import re
from collections import deque
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ..codec import digest, validate_relative_path
from ..errors import InvalidInput
from .compilation_database import ClangCompilationDatabase, CompilationCommand

_INCLUDE = re.compile(
    r"^[ \t]*#[ \t]*include[ \t]*([<\"])([^>\"\r\n]+)[>\"]",
    re.MULTILINE,
)
_INCLUDE_FLAGS = ("-I", "-iquote", "-isystem")


@dataclass(frozen=True)
class IncludeChain:
    """One statically resolved path from a translation unit to a target."""

    translation_unit: str
    paths: tuple[str, ...]


@dataclass(frozen=True)
class TargetSelection:
    """Translation units selected for one defect-bearing source path."""

    target_path: str
    direct_translation_units: tuple[str, ...]
    including_translation_units: tuple[str, ...]
    include_chains: tuple[IncludeChain, ...]

    @property
    def translation_units(self) -> tuple[str, ...]:
        return tuple(sorted({
            *self.direct_translation_units,
            *self.including_translation_units,
        }))


@dataclass(frozen=True)
class AnalysisLayer:
    """A runnable analysis layer over a deterministic translation-unit set."""

    layer_id: str
    ctu_enabled: bool
    translation_units: tuple[str, ...]
    coverage_intent: str


@dataclass(frozen=True)
class LayeredCtuPlan:
    """Immutable result of selecting full-project and targeted CTU layers."""

    compile_database_digest: str
    selections: tuple[TargetSelection, ...]
    non_ctu: AnalysisLayer
    targeted_ctu: AnalysisLayer

    @property
    def target_paths(self) -> tuple[str, ...]:
        return tuple(item.target_path for item in self.selections)

    @property
    def ctu_slices(self) -> tuple[AnalysisLayer, ...]:
        """Return one independently runnable CTU layer per defect target.

        Keeping these layers separate is the failure-containment boundary: an
        AST import failure while analyzing one target must not suppress the
        non-CTU result or the CTU attempt for another target.  ``targeted_ctu``
        remains the deterministic union used for coverage summaries and for
        callers that explicitly prefer one combined CTU invocation.
        """

        return tuple(
            AnalysisLayer(
                f"targeted-ctu-{index:03d}-{digest(selection.target_path)[:8]}",
                True,
                selection.translation_units,
                f"defect-target:{selection.target_path}",
            )
            for index, selection in enumerate(self.selections, 1)
        )


class LayeredCtuPlanner:
    """Build and persist a two-layer CodeChecker analysis plan."""

    def __init__(self, database: ClangCompilationDatabase) -> None:
        self.database = database
        self.project_root = database.project_root
        self._include_cache: dict[str, tuple[tuple[bool, str], ...]] = {}
        self._resolved_include_cache: dict[
            tuple[str, str, bool, tuple[Path, ...]], str | None
        ] = {}

    def plan(self, target_paths: Iterable[str]) -> LayeredCtuPlan:
        """Select CTU translation units for existing project-relative targets."""

        if isinstance(target_paths, (str, bytes)):
            raise InvalidInput("target paths must be an iterable of paths")
        targets: list[str] = []
        for raw in target_paths:
            if not isinstance(raw, str):
                raise InvalidInput("target path must be a string")
            validate_relative_path(raw)
            path = self.project_root / raw
            if not path.is_file():
                raise InvalidInput(f"target path does not exist: {raw}")
            if raw not in targets:
                targets.append(raw)
        if not targets:
            raise InvalidInput("at least one target path is required")

        selections = self._select(tuple(sorted(targets)))
        selected = tuple(sorted({
            translation_unit
            for selection in selections
            for translation_unit in selection.translation_units
        }))
        return LayeredCtuPlan(
            compile_database_digest=self.database.database_digest,
            selections=selections,
            non_ctu=AnalysisLayer(
                "non-ctu-full", False, self.database.files, "full-project",
            ),
            targeted_ctu=AnalysisLayer(
                "targeted-ctu", True, selected, "defect-relevant",
            ),
        )

    def write_bundle(self, plan: LayeredCtuPlan, output_dir: Path) -> Path:
        """Write the plan and its two CodeChecker-compatible databases."""

        if plan.compile_database_digest != self.database.database_digest:
            raise InvalidInput("plan belongs to a different compilation database")
        output_dir.mkdir(parents=True, exist_ok=True)
        layer_files = {
            plan.non_ctu.layer_id: "non-ctu.compile_commands.json",
            plan.targeted_ctu.layer_id: "targeted-ctu.compile_commands.json",
        }
        for index, layer in enumerate(plan.ctu_slices, 1):
            layer_files[layer.layer_id] = f"targeted-ctu-{index:03d}.compile_commands.json"
        for layer in (plan.non_ctu, plan.targeted_ctu, *plan.ctu_slices):
            body = self.compilation_database_json(layer.translation_units)
            (output_dir / layer_files[layer.layer_id]).write_text(
                json.dumps(body, ensure_ascii=False, indent=2) + "\n",
                encoding="utf-8",
            )
        value = self.to_json(plan, layer_files=layer_files)
        plan_path = output_dir / "plan.json"
        plan_path.write_text(
            json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        return plan_path

    def to_json(
        self,
        plan: LayeredCtuPlan,
        *,
        layer_files: dict[str, str] | None = None,
    ) -> dict[str, Any]:
        """Return a stable JSON value, including executable CodeChecker hints."""

        files = layer_files or {
            plan.non_ctu.layer_id: "non-ctu.compile_commands.json",
            plan.targeted_ctu.layer_id: "targeted-ctu.compile_commands.json",
            **{
                layer.layer_id: f"targeted-ctu-{index:03d}.compile_commands.json"
                for index, layer in enumerate(plan.ctu_slices, 1)
            },
        }
        layers = [
            self._layer_json(plan.non_ctu, files[plan.non_ctu.layer_id]),
            self._layer_json(plan.targeted_ctu, files[plan.targeted_ctu.layer_id]),
        ]
        ctu_slices = [
            {
                **self._layer_json(layer, files[layer.layer_id]),
                "target_path": selection.target_path,
                "failure_isolation": "target-slice",
            }
            for selection, layer in zip(plan.selections, plan.ctu_slices, strict=True)
        ]
        selections = [{
            "target_path": item.target_path,
            "direct_translation_units": list(item.direct_translation_units),
            "including_translation_units": list(item.including_translation_units),
            "translation_units": list(item.translation_units),
            "include_chains": [{
                "translation_unit": chain.translation_unit,
                "paths": list(chain.paths),
            } for chain in item.include_chains],
        } for item in plan.selections]
        content = {
            "schema": "agent-runtime/layered-ctu-plan/v2",
            "compile_database_digest": plan.compile_database_digest,
            "target_paths": list(plan.target_paths),
            "selections": selections,
            "layers": layers,
            "execution_order": [
                plan.non_ctu.layer_id,
                *(layer.layer_id for layer in plan.ctu_slices),
            ],
            "ctu_slices": ctu_slices,
            "failure_policy": {
                "continue_after_layer_failure": True,
                "score_failed_target_as_unevaluable": True,
                "aggregate_targeted_ctu_layer_is_summary_only": True,
            },
        }
        return {**content, "plan_digest": digest(content)}

    def compilation_database_json(
        self,
        translation_units: Sequence[str],
    ) -> list[dict[str, Any]]:
        """Render a TU subset as input accepted by ``CodeChecker analyze``."""

        wanted = set(translation_units)
        unknown = wanted.difference(self.database.files)
        if unknown:
            raise InvalidInput(
                "plan references unknown translation units: " + ", ".join(sorted(unknown))
            )
        return [
            self._command_json(command)
            for command in self.database.commands
            if command.file in wanted
        ]

    def _select(self, targets: tuple[str, ...]) -> tuple[TargetSelection, ...]:
        direct = {
            target: (target,) if target in self.database.files else ()
            for target in targets
        }
        # A path that is itself a translation unit is already covered directly;
        # searching for unusual ``#include "other.cpp"`` edges only increases
        # work and does not improve the targeted analysis set.
        header_targets = frozenset(target for target in targets if not direct[target])
        chains: dict[str, list[IncludeChain]] = {target: [] for target in targets}
        for command in self.database.commands:
            for target, chain in self._find_include_chains(
                command, header_targets,
            ).items():
                chains[target].append(IncludeChain(command.file, chain))
        return tuple(TargetSelection(
            target_path=target,
            direct_translation_units=direct[target],
            including_translation_units=tuple(
                item.translation_unit for item in chains[target]
            ),
            include_chains=tuple(chains[target]),
        ) for target in targets)

    def _find_include_chains(
        self,
        command: CompilationCommand,
        targets: frozenset[str],
    ) -> dict[str, tuple[str, ...]]:
        if not targets:
            return {}
        found: dict[str, tuple[str, ...]] = {}
        include_roots = self._include_roots(command)
        pending = deque([(command.file, (command.file,))])
        visited = {command.file}
        while pending and len(found) < len(targets):
            current, chain = pending.popleft()
            for quoted, include in self._includes(current):
                resolved = self._resolve_include(
                    current, include, quoted=quoted, include_roots=include_roots,
                )
                if resolved is None or resolved in visited:
                    continue
                visited.add(resolved)
                next_chain = (*chain, resolved)
                if resolved in targets:
                    found[resolved] = next_chain
                pending.append((resolved, next_chain))
        return found

    def _find_include_chain(
        self,
        command: CompilationCommand,
        target: str,
    ) -> tuple[str, ...] | None:
        """Compatibility helper for callers interested in a single target."""

        return self._find_include_chains(command, frozenset({target})).get(target)

    def _includes(self, relative_path: str) -> tuple[tuple[bool, str], ...]:
        cached = self._include_cache.get(relative_path)
        if cached is not None:
            return cached
        try:
            body = (self.project_root / relative_path).read_text(
                encoding="utf-8", errors="replace",
            )
        except OSError as exc:
            raise InvalidInput(f"cannot read source while resolving includes: {relative_path}") from exc
        includes = tuple(
            (delimiter == '"', name.strip())
            for delimiter, name in _INCLUDE.findall(body)
            if name.strip()
        )
        self._include_cache[relative_path] = includes
        return includes

    def _resolve_include(
        self,
        including_path: str,
        include: str,
        *,
        quoted: bool,
        include_roots: tuple[Path, ...],
    ) -> str | None:
        cache_key = (including_path, include, quoted, include_roots)
        if cache_key in self._resolved_include_cache:
            return self._resolved_include_cache[cache_key]
        if "\\" in include:
            self._resolved_include_cache[cache_key] = None
            return None
        candidates: list[Path] = []
        if quoted:
            candidates.append((self.project_root / including_path).parent / include)
        candidates.extend(root / include for root in include_roots)
        for candidate in candidates:
            try:
                resolved = candidate.resolve()
                relative = resolved.relative_to(self.project_root).as_posix()
            except (OSError, ValueError):
                continue
            if resolved.is_file():
                self._resolved_include_cache[cache_key] = relative
                return relative
        self._resolved_include_cache[cache_key] = None
        return None

    def _include_roots(self, command: CompilationCommand) -> tuple[Path, ...]:
        directory = Path(self._expand(command.directory))
        roots: list[Path] = []
        arguments = command.arguments
        index = 0
        while index < len(arguments):
            token = arguments[index]
            value: str | None = None
            if token in _INCLUDE_FLAGS and index + 1 < len(arguments):
                value = arguments[index + 1]
                index += 1
            else:
                for flag in _INCLUDE_FLAGS:
                    if token.startswith(flag) and len(token) > len(flag):
                        value = token[len(flag):]
                        break
            if value:
                path = Path(self._expand(value))
                if not path.is_absolute():
                    path = directory / path
                try:
                    path = path.resolve()
                    path.relative_to(self.project_root)
                except (OSError, ValueError):
                    pass
                else:
                    if path not in roots:
                        roots.append(path)
            index += 1
        return tuple(roots)

    def _command_json(self, command: CompilationCommand) -> dict[str, Any]:
        return {
            "directory": self._expand(command.directory),
            "file": str(self.project_root / command.file),
            "arguments": [
                command.compiler,
                *(self._expand(token) for token in command.arguments),
            ],
        }

    def _expand(self, value: str) -> str:
        return value.replace("$PROJECT_ROOT", str(self.project_root))

    @staticmethod
    def _layer_json(layer: AnalysisLayer, compile_commands: str) -> dict[str, Any]:
        arguments = [
            "CodeChecker", "analyze", compile_commands,
            "--output", f"reports/{layer.layer_id}",
        ]
        if layer.ctu_enabled:
            arguments.append("--ctu")
        return {
            "layer_id": layer.layer_id,
            "coverage_intent": layer.coverage_intent,
            "ctu_enabled": layer.ctu_enabled,
            "enabled": bool(layer.translation_units),
            "translation_unit_count": len(layer.translation_units),
            "translation_units": list(layer.translation_units),
            "compile_commands": compile_commands,
            "codechecker_arguments": arguments,
        }
