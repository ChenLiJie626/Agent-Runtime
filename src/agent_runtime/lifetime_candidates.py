"""Conservative, explainable C++ lifetime candidate discovery.

The rules in this module are intentionally syntax based.  They produce review
candidates and evidence hints; they do not claim that a defect is confirmed.
Keeping discovery separate from confirmation makes the rules useful even when
Clang CTU cannot import a project's complete AST.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
import hashlib
import re
from pathlib import Path
from typing import Iterable, Literal


CONTAINER_INVALIDATION_RULE = "cpp.lifetime.container-invalidation.v1"
COROUTINE_DESTRUCTION_RULE = "cpp.lifetime.coroutine-local-destruction.v1"
ASYNC_MEMBER_DESTRUCTION_RULE = "cpp.lifetime.async-member-destruction.v1"
RETURNED_RESOURCE_RULE = "cpp.lifetime.returned-resource-owner.v1"
STACK_CONTEXT_ESCAPE_RULE = "cpp.lifetime.stack-context-escape.v1"
VECTOR_FIELD_COPY_RULE = "cpp.lifetime.vector-field-copy.v1"


@dataclass(frozen=True)
class LifetimeCandidate:
    """One static lifetime-risk candidate with a human-auditable explanation."""

    rule_id: str
    path: str
    line: int
    reason: str
    evidence_hints: tuple[str, ...]

    def to_dict(self) -> dict[str, object]:
        value = asdict(self)
        value["evidence_hints"] = list(self.evidence_hints)
        return value


@dataclass(frozen=True)
class AsanChecklist:
    """A reproducible ASan evidence request; it does not execute commands."""

    candidate: LifetimeCandidate
    build_command: str
    reproduce_command: str
    required_compile_flags: tuple[str, ...]
    required_environment: tuple[str, ...]
    acceptance_checks: tuple[str, ...]

    def to_dict(self) -> dict[str, object]:
        return {
            "candidate": self.candidate.to_dict(),
            "build_command": self.build_command,
            "reproduce_command": self.reproduce_command,
            "required_compile_flags": list(self.required_compile_flags),
            "required_environment": list(self.required_environment),
            "acceptance_checks": list(self.acceptance_checks),
        }


AsanStatus = Literal["confirmed", "inconclusive", "refuted"]


@dataclass(frozen=True)
class AsanEvidenceResult:
    """Normalized result of one externally executed ASan reproduction."""

    status: AsanStatus
    reason: str
    command: str
    exit_code: int | None
    sanitizer_finding: str | None
    matched_frames: tuple[str, ...]
    log_sha256: str

    def to_dict(self) -> dict[str, object]:
        value = asdict(self)
        value["matched_frames"] = list(self.matched_frames)
        return value


@dataclass(frozen=True)
class _Borrow:
    name: str
    owner: str
    offset: int
    line: int


def _line_number(text: str, offset: int) -> int:
    return text.count("\n", 0, offset) + 1


def _mask_comments(text: str) -> str:
    """Replace comments with spaces while retaining offsets and newlines."""

    def replace(match: re.Match[str]) -> str:
        return "".join("\n" if char == "\n" else " " for char in match.group())

    return re.sub(r"//[^\n]*|/\*.*?\*/", replace, text, flags=re.DOTALL)


def _container_candidates(path: str, source: str) -> list[LifetimeCandidate]:
    masked = _mask_comments(source)
    borrow_patterns = (
        # auto& value = values[index];
        re.compile(
            r"\b(?:const\s+)?auto\s*&\s*(?P<name>[A-Za-z_]\w*)\s*=\s*"
            r"(?P<owner>[A-Za-z_]\w*)\s*\["
        ),
        # const Value& value = values[index];
        re.compile(
            r"\b(?:const\s+)?[A-Za-z_]\w*(?:::\w+)*(?:\s*<[^;{}]+>)?"
            r"\s*&\s*(?P<name>[A-Za-z_]\w*)\s*=\s*"
            r"(?P<owner>[A-Za-z_]\w*)\s*\["
        ),
        # auto iterator = values.begin(); / auto pointer = values.data();
        re.compile(
            r"\bauto\s+(?P<name>[A-Za-z_]\w*)\s*=\s*"
            r"(?P<owner>[A-Za-z_]\w*)\s*\.\s*"
            r"(?:begin|cbegin|rbegin|data|front|back)\s*\("
        ),
        # Value* pointer = values.data();
        re.compile(
            r"\b(?:const\s+)?[A-Za-z_]\w*(?:::\w+)*(?:\s*<[^;{}]+>)?"
            r"\s*\*\s*(?P<name>[A-Za-z_]\w*)\s*=\s*"
            r"(?P<owner>[A-Za-z_]\w*)\s*\.\s*data\s*\("
        ),
        # std::span<T> view = values; (also handles qualified span aliases)
        re.compile(
            r"\b(?:std::)?span\s*<[^;>]+>\s+(?P<name>[A-Za-z_]\w*)\s*=\s*"
            r"(?P<owner>[A-Za-z_]\w*)\b"
        ),
    )
    borrows: list[_Borrow] = []
    for pattern in borrow_patterns:
        for match in pattern.finditer(masked):
            borrows.append(
                _Borrow(
                    name=match.group("name"),
                    owner=match.group("owner"),
                    offset=match.start(),
                    line=_line_number(masked, match.start()),
                )
            )

    candidates: list[LifetimeCandidate] = []
    mutators = (
        "push_back|emplace_back|insert|erase|reserve|resize|clear|assign|swap|"
        "shrink_to_fit"
    )
    for borrow in borrows:
        # Bound the heuristic to a nearby basic-block-sized region.
        tail_lines = masked[borrow.offset :].splitlines(keepends=True)[:80]
        tail = "".join(tail_lines)
        mutation = re.search(
            rf"\b{re.escape(borrow.owner)}\s*\.\s*(?P<op>{mutators})\s*\(",
            tail,
        )
        if mutation is None:
            continue
        subsequent = tail[mutation.end() :]
        if re.search(rf"\b{re.escape(borrow.name)}\b", subsequent) is None:
            continue
        operation = mutation.group("op")
        candidates.append(
            LifetimeCandidate(
                rule_id=CONTAINER_INVALIDATION_RULE,
                path=path,
                line=borrow.line,
                reason=(
                    f"'{borrow.name}' borrows storage from '{borrow.owner}', then "
                    f"'{borrow.owner}.{operation}(...)' may invalidate that storage "
                    "before the borrow is used again."
                ),
                evidence_hints=(
                    "Confirm the borrow aliases an element, iterator, pointer, or span of the container.",
                    f"Check whether {borrow.owner}.{operation}(...) can reallocate or erase the borrowed element on this path.",
                    "Exercise the mutation and later use with an AddressSanitizer build.",
                ),
            )
        )
    return candidates


_DECLARATION = re.compile(
    r"(?m)^(?P<indent>[ \t]+)"
    r"(?P<type>(?:const\s+)?[A-Za-z_]\w*(?:::\w+)*(?:\s*<[^;{}]+>)?)"
    r"\s+(?P<name>[A-Za-z_]\w*)\s*(?:[;{=(])"
)


def _coroutine_candidates(path: str, source: str) -> list[LifetimeCandidate]:
    masked = _mask_comments(source)
    if re.search(r"\bco_(?:await|yield|return)\b", masked) is None:
        return []

    declarations: dict[str, tuple[int, int]] = {}
    for match in _DECLARATION.finditer(masked):
        declarations.setdefault(
            match.group("name"),
            (match.start(), _line_number(masked, match.start())),
        )

    candidates: list[LifetimeCandidate] = []
    # Stream/filter/owner objects commonly retain a sink such as a back inserter.
    relation = re.compile(
        r"\b(?P<owner>[A-Za-z_]\w*)\s*\.\s*"
        r"(?:push|attach|set_sink|set_output|reset)\s*\("
        r"(?P<args>.{0,600}?)\)\s*;",
        re.DOTALL,
    )
    for match in relation.finditer(masked):
        owner = match.group("owner")
        if owner not in declarations:
            continue
        owner_offset, owner_line = declarations[owner]
        for target, (target_offset, _target_line) in declarations.items():
            if (
                target == owner
                or re.search(rf"\b{re.escape(target)}\b", match.group("args")) is None
            ):
                continue
            if not owner_offset < target_offset < match.start():
                continue
            region_start = max(0, owner_offset - 2000)
            region_end = min(len(masked), match.end() + 4000)
            if (
                re.search(
                    r"\bco_(?:await|yield|return)\b",
                    masked[region_start:region_end],
                )
                is None
            ):
                continue
            candidates.append(
                LifetimeCandidate(
                    rule_id=COROUTINE_DESTRUCTION_RULE,
                    path=path,
                    line=owner_line,
                    reason=(
                        f"coroutine local '{owner}' is declared before '{target}' but "
                        f"retains a reference to it; reverse local destruction destroys "
                        f"'{target}' first when the coroutine frame is abandoned."
                    ),
                    evidence_hints=(
                        f"Verify that {owner} retains rather than copies the sink/reference to {target}.",
                        f"Check destructor behavior of {owner} for reads or writes through the retained reference.",
                        "Destroy or cancel the coroutine while it is suspended and capture the ASan log.",
                    ),
                )
            )
            break
    return candidates


def _call_text(text: str, start: int) -> str:
    """Return one function call using balanced parentheses."""

    opening = text.find("(", start)
    if opening < 0:
        return ""
    depth = 0
    quote: str | None = None
    escaped = False
    for index in range(opening, len(text)):
        char = text[index]
        if quote is not None:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == quote:
                quote = None
            continue
        if char in {'"', "'"}:
            quote = char
        elif char == "(":
            depth += 1
        elif char == ")":
            depth -= 1
            if depth == 0:
                return text[start : index + 1]
    return ""


def _async_member_candidates(path: str, source: str) -> list[LifetimeCandidate]:
    masked = _mask_comments(source)
    future_pattern = re.compile(
        r"(?m)^(?P<indent>[ \t]+)std::future\s*<[^;]+>\s+"
        r"(?P<name>[A-Za-z_]\w*_)\s*(?:\{[^;]*\})?\s*;"
    )
    member_pattern = re.compile(
        r"(?m)^(?P<indent>[ \t]+)(?:\[\[[^\]]+\]\]\s*)?"
        r"[A-Za-z_]\w*(?:::\w+)*(?:\s*<[^;]+>)?"
        r"(?:\s*[*&])?\s+(?P<name>[A-Za-z_]\w*_)\s*"
        r"(?:\{[^;]*\}|=[^;]*)?\s*;"
    )
    async_calls = [
        (match.start(), _call_text(masked, match.start()))
        for match in re.finditer(r"\bstd::async\s*\(", masked)
    ]
    async_calls = [
        (offset, call)
        for offset, call in async_calls
        if call and re.search(r"\[[^\]]*\bthis\b[^\]]*\]", call)
    ]
    if not async_calls:
        return []

    # An explicit destructor barrier is strong enough to suppress this
    # syntax-only rule.  The dynamic evidence stage can still test it.
    destructor_bodies = re.findall(
        r"~[A-Za-z_]\w*\s*\([^)]*\)\s*(?:noexcept\s*)?\{(.{0,800}?)\}",
        masked,
        flags=re.DOTALL,
    )

    candidates: list[LifetimeCandidate] = []
    for future in future_pattern.finditer(masked):
        future_name = future.group("name")
        if any(
            re.search(
                rf"\b(?:{re.escape(future_name)}\s*\.\s*(?:get|wait)|waitForFuture)\s*\(",
                body,
            )
            for body in destructor_bodies
        ):
            continue
        setter_names = []
        for setter in re.finditer(
            r"\b(?P<name>[A-Za-z_]\w*)\s*\([^)]*std::future[^)]*\)\s*\{"
            r"(?P<body>.{0,800}?)\}",
            masked,
            flags=re.DOTALL,
        ):
            if re.search(rf"\b{re.escape(future_name)}\b\s*=", setter.group("body")):
                setter_names.append(setter.group("name"))
        linked_async_blocks = []
        for offset, call in async_calls:
            prefix = masked[max(0, offset - 240) : offset]
            direct_store = re.search(rf"\b{re.escape(future_name)}\b\s*=\s*$", prefix)
            setter_store = any(
                re.search(rf"\b{re.escape(name)}\s*\([^;]*$", prefix)
                for name in setter_names
            )
            if direct_store or setter_store:
                linked_async_blocks.append(call)
        if not linked_async_blocks:
            continue
        indent = future.group("indent")
        for member in member_pattern.finditer(masked, future.end()):
            if member.group("indent") != indent:
                continue
            member_name = member.group("name")
            if member_name == future_name:
                continue
            if not any(
                re.search(rf"\b(?:this\s*->\s*)?{re.escape(member_name)}\b", block)
                for block in linked_async_blocks
            ):
                continue
            candidates.append(
                LifetimeCandidate(
                    rule_id=ASYNC_MEMBER_DESTRUCTION_RULE,
                    path=path,
                    line=_line_number(masked, member.start()),
                    reason=(
                        f"async work retained by '{future_name}' captures this and uses "
                        f"later-declared member '{member_name}'. Reverse member destruction "
                        f"destroys '{member_name}' before '{future_name}' can join the work."
                    ),
                    evidence_hints=(
                        f"Confirm the std::async task stored in {future_name} can still be running during destruction.",
                        f"Confirm the task dereferences this->{member_name} after teardown begins.",
                        f"Add or verify a destructor barrier that waits on {future_name} before member destruction, then rerun ASan.",
                    ),
                )
            )
    return candidates


def _code_scopes(source: str) -> tuple[str, dict[int, int]]:
    """Mask literals and pair braces once, so rules stay within lexical scopes."""
    masked = _mask_comments(source)
    masked = re.sub(
        r'"(?:\\.|[^"\\])*"|\'(?:\\.|[^\'\\])*\'',
        lambda m: "".join("\n" if c == "\n" else " " for c in m.group()),
        masked,
    )
    stack: list[int] = []
    scopes: dict[int, int] = {}
    for offset, char in enumerate(masked):
        if char == "{":
            stack.append(offset)
        elif char == "}" and stack:
            scopes[stack.pop()] = offset
    return masked, scopes


def _scope_end(scopes: dict[int, int], offset: int, default: int) -> int:
    enclosing = [(start, end) for start, end in scopes.items() if start < offset < end]
    return max(enclosing)[1] if enclosing else default


def _returned_resource_candidates(path: str, source: str) -> list[LifetimeCandidate]:
    masked, scopes = _code_scopes(source)
    candidates = []
    for declaration in _DECLARATION.finditer(masked):
        value, value_type = declaration.group("name"), declaration.group("type")
        # Primitive/STL value copies own their payload; custom value aggregates
        # may instead copy IDs/proxies referring to an external resource arena.
        if value_type in {
            "auto",
            "int",
            "bool",
            "double",
            "size_t",
        } or value_type.startswith(("std::", "const std::")):
            continue
        end = _scope_end(scopes, declaration.start(), len(masked))
        tail = masked[declaration.end() : end]
        returned = re.search(
            rf"\breturn\s+(?:std::move\s*\(\s*)?{re.escape(value)}\s*\)?\s*;", tail
        )
        if returned is None:
            continue
        copied = re.search(
            rf"\b{re.escape(value)}\s*\.\s*(?:push_back|emplace_back)\s*\(\s*(?P<view>\w+)\s*\[",
            tail[: returned.start()],
        )
        if copied is None:
            continue
        view = copied.group("view")
        # Require explicit reference/view provenance rather than an arbitrary
        # subscripted source. Ownership-preserving APIs suppress the candidate.
        prefix = tail[: copied.start()]
        if not re.search(
            rf"(?:auto\s*&|(?:const\s+)?\w+\s*&|\bauto)\s+{re.escape(view)}\s*=.*(?:View|view|span|\b\w+\s*\[)",
            prefix,
            re.DOTALL,
        ):
            continue
        if re.search(
            rf"\b{re.escape(value)}\s*\.\s*(?:retain|keepAlive|setOwner|mergeResources|retainOwner)\s*\(",
            tail[: returned.start()],
        ):
            continue
        candidates.append(
            LifetimeCandidate(
                RETURNED_RESOURCE_RULE,
                path,
                _line_number(masked, declaration.end() + returned.start()),
                f"returned custom value '{value}' copies elements from borrowed view '{view}' without an explicit resource-owner transfer; copied IDs/proxies may outlive their arena",
                (
                    "Check whether copied elements own their payload or refer to a local vocabulary/resource arena.",
                    "Verify the returned object retains every resource owner of the source view.",
                    "Destroy the source owner before reading the returned value under ASan.",
                ),
            )
        )
    return candidates


def _stack_context_candidates(path: str, source: str) -> list[LifetimeCandidate]:
    masked, scopes = _code_scopes(source)
    candidates = []
    for declaration in _DECLARATION.finditer(masked):
        name, type_name = declaration.group("name"), declaration.group("type")
        if type_name.startswith(("std::shared_ptr", "std::unique_ptr")):
            continue
        enclosing = [
            (start, stop)
            for start, stop in scopes.items()
            if start < declaration.start() < stop
        ]
        if not enclosing:
            continue
        opening, end = max(enclosing)
        if re.search(
            r"\b(?:class|struct|namespace)\s+[^{};]+$",
            masked[max(0, opening - 300) : opening],
        ):
            continue  # A class member is not a stack local.
        tail = masked[declaration.end() : end]
        escaped = re.search(
            rf"\b(?:co_)?return\s+(?:&\s*{re.escape(name)}\b|"
            rf"\[[^\]]*&\s*{re.escape(name)}\b[^\]]*\]|"
            rf"[^;{{}}]*(?:task|generator|view|async|defer|lazy)\w*\s*\([^;{{}}]*(?<!&)\&\s*{re.escape(name)}\b)",
            tail,
            re.IGNORECASE,
        )
        # Also cover a local lazy/context wrapper returned by value while its
        # constructor stores a reference_wrapper or explicit address. Moving the
        # wrapper does not extend the lifetime of the referenced context.
        call = (
            _call_text(masked, declaration.end() - 1)
            if masked[declaration.end() - 1] == "("
            else ""
        )
        borrowed_constructor = (
            re.search(r"\b(?:\w*Context|\w*Generator|\w*Task|\w*View)\b", type_name)
            and re.search(r"\bstd::(?:c?ref)\s*\(|&\s*\w+", call)
            and re.search(rf"\b(?:co_)?return\s+[^;]*\b{re.escape(name)}\b", tail)
        )
        # A lambda variable returned later rather than inline is a common lazy
        # escape; require an explicit reference capture of this local.
        captured = re.search(
            rf"\bauto\s+(\w+)\s*=\s*\[[^\]]*&\s*{re.escape(name)}\b[^\]]*\]", tail
        )
        captured_escape = captured and re.search(
            rf"\b(?:co_)?return\s+{re.escape(captured.group(1))}\s*;", tail
        )
        if not (escaped or borrowed_constructor or captured_escape):
            continue
        candidates.append(
            LifetimeCandidate(
                STACK_CONTEXT_ESCAPE_RULE,
                path,
                _line_number(masked, declaration.start()),
                f"returned/lazy object escapes with a borrowed stack context associated with '{name}'; moving a wrapper does not extend referenced lifetimes",
                (
                    "Verify the returned object retains an address/reference rather than an owning copy.",
                    "Trace caller-owned and local context lifetime through coroutine/async consumers.",
                    "Resume the lazy result after context teardown with stack-use-after-return instrumentation.",
                ),
            )
        )
    return candidates


def _vector_field_copy_candidates(path: str, source: str) -> list[LifetimeCandidate]:
    masked, scopes = _code_scopes(source)
    # Summarize same-file allocators that grow vector-like storage and return an
    # element address. No project-specific function or variable names are used.
    allocators: set[str] = set()
    for function in re.finditer(r"\b(\w+)\s*\([^;{}]*\)\s*(?:const\s*)?\{", masked):
        opening = function.end() - 1
        body = masked[opening : scopes.get(opening, opening)]
        result = re.search(
            r"\breturn\s*&\s*(\w+)\s*(?:\[|\.\s*(?:back|front)\s*\()", body
        )
        if result and re.search(
            rf"\b{re.escape(result.group(1))}\s*\.\s*(?:resize|reserve|push_back|emplace_back|insert)\s*\(",
            body,
        ):
            allocators.add(function.group(1))
    borrows = re.compile(
        r"\b(?:auto|\w+(?:::\w+)*)\s*\*\s*(?P<name>\w+)\s*=\s*"
        r"(?:(?:&\s*(?P<vector>\w+)\s*\[)|(?P<owner>\w+)\s*(?:->|\.)\s*(?P<allocator>\w+)\s*\()"
    )
    candidates = []
    for borrow in borrows.finditer(masked):
        name, vector, owner, allocator = (
            borrow.group(key) for key in ("name", "vector", "owner", "allocator")
        )
        if allocator and allocator not in allocators:
            continue
        if vector and not re.search(
            rf"\bstd::vector\s*<[^;]+>\s*{re.escape(vector)}\b",
            masked[: borrow.start()],
        ):
            continue
        end = _scope_end(scopes, borrow.start(), len(masked))
        tail = masked[borrow.end() : end]
        pattern = (
            rf"\b{re.escape(vector)}\s*\.\s*(?:resize|reserve|push_back|emplace_back|insert)\s*\("
            if vector
            else rf"\b{re.escape(owner)}\s*(?:->|\.)\s*{re.escape(allocator)}\s*\("
        )
        mutation = re.search(pattern, tail)
        if mutation is None:
            continue
        later = tail[mutation.end() :]
        copied = re.search(rf"\b{re.escape(name)}\s*->\s*\w+", later)
        if copied is None or re.search(
            rf"\b{re.escape(name)}\s*=(?!=)",
            tail[: mutation.end()] + later[: copied.start()],
        ):
            continue
        candidates.append(
            LifetimeCandidate(
                VECTOR_FIELD_COPY_RULE,
                path,
                _line_number(masked, borrow.end() + mutation.end() + copied.start()),
                f"element pointer '{name}' is dereferenced for field access after '{vector or owner}' grows its storage; allocator returns element addresses that may be invalidated",
                (
                    "Verify the allocation helper uses std::vector or other relocating storage.",
                    "Check capacity growth and whether the old pointer is refreshed before copying fields.",
                    "Force reallocation between allocation and field access and capture ASan.",
                ),
            )
        )
    return candidates


def scan_cpp_source(path: str, source: str) -> tuple[LifetimeCandidate, ...]:
    """Run all lifetime candidate rules over one C++ source file."""

    found = [
        *_container_candidates(path, source),
        *_coroutine_candidates(path, source),
        *_async_member_candidates(path, source),
        *_returned_resource_candidates(path, source),
        *_stack_context_candidates(path, source),
        *_vector_field_copy_candidates(path, source),
    ]
    unique = {
        (candidate.rule_id, candidate.path, candidate.line, candidate.reason): candidate
        for candidate in found
    }
    return tuple(
        sorted(unique.values(), key=lambda item: (item.path, item.line, item.rule_id))
    )


def scan_cpp_paths(
    paths: Iterable[Path], root: Path | None = None
) -> tuple[LifetimeCandidate, ...]:
    """Scan explicit files/directories and return deterministic candidates."""

    suffixes = {".c", ".cc", ".cpp", ".cxx", ".h", ".hh", ".hpp", ".hxx"}
    files: set[Path] = set()
    for path in paths:
        if path.is_dir():
            files.update(
                item for item in path.rglob("*") if item.suffix.lower() in suffixes
            )
        elif path.suffix.lower() in suffixes:
            files.add(path)
    candidates: list[LifetimeCandidate] = []
    for file_path in sorted(files):
        display_path: str
        if root is not None:
            try:
                display_path = (
                    file_path.resolve().relative_to(root.resolve()).as_posix()
                )
            except ValueError:
                display_path = file_path.as_posix()
        else:
            display_path = file_path.as_posix()
        candidates.extend(
            scan_cpp_source(
                display_path, file_path.read_text(encoding="utf-8", errors="replace")
            )
        )
    return tuple(
        sorted(candidates, key=lambda item: (item.path, item.line, item.rule_id))
    )


def build_asan_checklist(
    candidate: LifetimeCandidate,
    *,
    build_command: str,
    reproduce_command: str,
) -> AsanChecklist:
    """Create the minimum evidence checklist for one candidate."""

    return AsanChecklist(
        candidate=candidate,
        build_command=build_command,
        reproduce_command=reproduce_command,
        required_compile_flags=(
            "-fsanitize=address",
            "-fno-omit-frame-pointer",
            "-g",
        ),
        required_environment=(
            "ASAN_OPTIONS=halt_on_error=1:detect_stack_use_after_return=1",
        ),
        acceptance_checks=(
            "Record the exact build and reproduction commands.",
            "Record the process exit code and complete stdout/stderr.",
            "Show that the vulnerable operation was exercised; a clean unexercised run is inconclusive.",
            f"For confirmation, the ASan UAF report must contain a frame from {candidate.path}.",
        ),
    )


_ASAN_UAF = re.compile(
    r"(?:ERROR:\s*AddressSanitizer:\s*|SUMMARY:\s*AddressSanitizer:\s*)"
    r"(?P<kind>heap-use-after-free|stack-use-after-return|stack-use-after-scope|"
    r"use-after-poison|double-free)",
    re.IGNORECASE,
)


def normalize_asan_result(
    candidate: LifetimeCandidate,
    *,
    command: str,
    exit_code: int | None,
    stdout: str = "",
    stderr: str = "",
    instrumented: bool,
    exercised: bool,
    source_roots: tuple[str, ...] = (),
) -> AsanEvidenceResult:
    """Normalize actual ASan output without inventing missing execution facts.

    ``refuted`` means only that this complete, instrumented reproduction did not
    trigger the candidate.  It is not a proof that no other execution can do so.
    """

    log = "\n".join((stdout, stderr))
    # Match numbered frames inside the same sanitizer report. Compiler errors,
    # echoed commands and another file with the same basename are not evidence.
    finding = None
    matched_frames: tuple[str, ...] = ()
    source_path = candidate.path.removeprefix("./")
    prefixes = "|".join(re.escape(root.rstrip("/")) + "/" for root in source_roots)
    prefix = rf"(?:(?:{prefixes})|)" if source_roots else r"(?:/[^\s:]*?/)?"
    source_pattern = re.compile(
        rf"(?<![\w./-]){prefix}{re.escape(source_path)}:\d+(?::\d+)?(?:\s|$)"
    )
    starts = list(re.finditer(r"ERROR:\s*AddressSanitizer:", log))
    for index, start in enumerate(starts):
        end = starts[index + 1].start() if index + 1 < len(starts) else len(log)
        report = log[start.start() : end]
        current = _ASAN_UAF.search(report)
        if current is None:
            continue
        frames = tuple(
            line.strip()
            for line in report.splitlines()
            if re.match(r"\s*#\d+\s+", line) and source_pattern.search(line)
        )
        finding = current
        if frames:
            matched_frames = frames
            break
    log_sha256 = hashlib.sha256(log.encode("utf-8")).hexdigest()

    if (
        finding is not None
        and matched_frames
        and instrumented
        and exercised
        and exit_code is not None
    ):
        return AsanEvidenceResult(
            status="confirmed",
            reason=(
                f"AddressSanitizer reported {finding.group('kind').lower()} and "
                "the same report contains a frame from the exercised candidate source file."
            ),
            command=command,
            exit_code=exit_code,
            sanitizer_finding=finding.group("kind").lower(),
            matched_frames=matched_frames,
            log_sha256=log_sha256,
        )
    if finding is not None:
        return AsanEvidenceResult(
            status="inconclusive",
            reason=(
                f"AddressSanitizer reported {finding.group('kind').lower()}, but verified execution and a "
                "linked source frame are required for this candidate."
            ),
            command=command,
            exit_code=exit_code,
            sanitizer_finding=finding.group("kind").lower(),
            matched_frames=(),
            log_sha256=log_sha256,
        )
    if instrumented and exercised and exit_code == 0:
        return AsanEvidenceResult(
            status="refuted",
            reason=(
                "This ASan-instrumented reproduction exercised the target and "
                "completed cleanly; the candidate is refuted for this reproduction only."
            ),
            command=command,
            exit_code=exit_code,
            sanitizer_finding=None,
            matched_frames=matched_frames,
            log_sha256=log_sha256,
        )
    missing = []
    if not instrumented:
        missing.append("ASan instrumentation was not verified")
    if not exercised:
        missing.append("target execution was not verified")
    if exit_code is None:
        missing.append("exit code is missing")
    elif exit_code != 0:
        missing.append(f"process exited with {exit_code} without a linked ASan UAF")
    return AsanEvidenceResult(
        status="inconclusive",
        reason="; ".join(missing)
        or "the supplied log contains no decisive ASan evidence",
        command=command,
        exit_code=exit_code,
        sanitizer_finding=None,
        matched_frames=matched_frames,
        log_sha256=log_sha256,
    )
