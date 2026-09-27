"""Validated, relocatable C/C++ compilation database support."""

from __future__ import annotations

import json
import shlex
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ..codec import digest, validate_relative_path
from ..errors import InvalidInput, PolicyDenied

_DROP_WITH_VALUE = frozenset({
    "-o", "-MF", "-MT", "-MQ", "-MJ", "--serialize-diagnostics",
})
_DROP_STANDALONE = frozenset({"-c", "-S", "-E", "--analyze"})
_DANGEROUS_PREFIXES = (
    "-fplugin=", "-plugin=", "-wrapper=", "--config=", "@",
)


@dataclass(frozen=True)
class CompilationCommand:
    file: str
    directory: str
    compiler: str
    arguments: tuple[str, ...]


class ClangCompilationDatabase:
    """A frozen compile database with commands normalized to a project root.

    Commands are never passed to a shell. Link/output actions and compiler
    plugins are removed or rejected before flags are replayed by the analyzer.
    """

    def __init__(
        self,
        entries: Sequence[Mapping[str, Any]],
        *,
        project_root: Path,
    ) -> None:
        root_aliases = (project_root.absolute(), project_root.resolve())
        root = project_root.resolve()
        normalized: list[CompilationCommand] = []
        seen: set[str] = set()
        for index, raw in enumerate(entries):
            if not isinstance(raw, Mapping):
                raise InvalidInput(f"compile_commands[{index}] must be an object")
            directory_raw = raw.get("directory")
            file_raw = raw.get("file")
            if not isinstance(directory_raw, str) or not isinstance(file_raw, str):
                raise InvalidInput("compile command needs directory and file")
            directory = Path(directory_raw)
            if not directory.is_absolute():
                directory = root / directory
            directory = directory.resolve()
            source = Path(file_raw)
            if not source.is_absolute():
                source = directory / source
            source = source.resolve()
            try:
                relative = source.relative_to(root).as_posix()
            except ValueError as exc:
                raise PolicyDenied("compile command source is outside project root") from exc
            validate_relative_path(relative)
            if relative in seen:
                raise InvalidInput(f"duplicate compile command for {relative}")
            seen.add(relative)

            if "arguments" in raw:
                arguments = raw["arguments"]
                if (not isinstance(arguments, list) or not arguments
                        or any(not isinstance(item, str) or not item for item in arguments)):
                    raise InvalidInput("compile command arguments must be nonempty strings")
                tokens = list(arguments)
            elif isinstance(raw.get("command"), str) and raw["command"].strip():
                try:
                    tokens = shlex.split(raw["command"])
                except ValueError as exc:
                    raise InvalidInput("compile command cannot be tokenized") from exc
            else:
                raise InvalidInput("compile command needs arguments or command")
            if not tokens:
                raise InvalidInput("compile command is empty")
            compiler = tokens[0]
            normalized_args = tuple(
                self._normalize_token(token, root_aliases) for token in tokens[1:]
            )
            normalized.append(CompilationCommand(
                relative,
                self._normalize_token(str(directory), root_aliases),
                compiler,
                normalized_args,
            ))
        if not normalized:
            raise InvalidInput("compilation database must not be empty")
        self.project_root = root
        self.commands = tuple(sorted(normalized, key=lambda item: item.file))
        self._by_file = {item.file: item for item in self.commands}
        self.database_digest = digest(self.commands)

    @classmethod
    def from_json(
        cls,
        value: str | bytes | Sequence[Mapping[str, Any]],
        *,
        project_root: Path,
    ) -> "ClangCompilationDatabase":
        if isinstance(value, bytes):
            try:
                value = value.decode("utf-8")
            except UnicodeDecodeError as exc:
                raise InvalidInput("compile_commands.json is not UTF-8") from exc
        if isinstance(value, str):
            try:
                value = json.loads(value)
            except json.JSONDecodeError as exc:
                raise InvalidInput("compile_commands.json is invalid JSON") from exc
        if not isinstance(value, Sequence) or isinstance(value, (str, bytes)):
            raise InvalidInput("compile_commands.json must contain an array")
        return cls(value, project_root=project_root)

    @classmethod
    def from_file(
        cls,
        path: Path,
        *,
        project_root: Path,
    ) -> "ClangCompilationDatabase":
        try:
            raw = path.read_bytes()
        except OSError as exc:
            raise InvalidInput(f"cannot read compilation database: {path}") from exc
        return cls.from_json(raw, project_root=project_root)

    @staticmethod
    def _normalize_token(token: str, roots: Sequence[Path]) -> str:
        for root in roots:
            root_text = str(root)
            if token == root_text:
                return "$PROJECT_ROOT"
            if token.startswith(root_text + "/"):
                return "$PROJECT_ROOT/" + token[len(root_text) + 1:]
            for prefix in ("-I", "-isystem", "-iquote", "--sysroot="):
                value = token[len(prefix):] if token.startswith(prefix) else ""
                if value == root_text:
                    return prefix + "$PROJECT_ROOT"
                if value.startswith(root_text + "/"):
                    return prefix + "$PROJECT_ROOT/" + value[len(root_text) + 1:]
        return token

    @property
    def files(self) -> tuple[str, ...]:
        return tuple(item.file for item in self.commands)

    def analyzer_arguments(self, path: str, snapshot_root: Path) -> tuple[str, ...]:
        validate_relative_path(path)
        try:
            command = self._by_file[path]
        except KeyError as exc:
            raise InvalidInput(f"no compile command for {path}") from exc
        source_tokens = {
            path,
            f"$PROJECT_ROOT/{path}",
            str(self.project_root / path),
        }
        kept: list[str] = []
        skip_value = False
        previous_xclang = False
        for token in command.arguments:
            if skip_value:
                skip_value = False
                continue
            if token in _DROP_WITH_VALUE:
                skip_value = True
                continue
            if token in _DROP_STANDALONE or token in source_tokens:
                continue
            if token.startswith(tuple(flag + "=" for flag in _DROP_WITH_VALUE)):
                continue
            if token.startswith(_DANGEROUS_PREFIXES):
                raise PolicyDenied(f"compile command contains forbidden option: {token}")
            if previous_xclang and token in {"-load", "-plugin", "-add-plugin"}:
                raise PolicyDenied("compile command attempts to load a compiler plugin")
            previous_xclang = token == "-Xclang"
            kept.append(token.replace("$PROJECT_ROOT", str(snapshot_root)))
        if skip_value:
            raise InvalidInput("compile command option is missing its value")
        return tuple(kept)
