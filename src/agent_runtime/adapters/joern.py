"""Conservative, read-only Joern CLI adapter for fixed symbol queries.

The model supplies an operation and a simple symbol. It cannot submit Joern DSL,
paths, shell arguments, or script text. The adapter intentionally reports caller
enumeration as partial until indirect targets and parse coverage are proven.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from importlib.resources import files
from pathlib import Path

from ..codec import bytes_digest, canonical_json, validate_relative_path
from ..domain import Coverage, QueryRequest
from ..errors import InvalidInput, StaleSnapshot
from ..ports import ProgramResult, QueryOperation

_SYMBOL = re.compile(r"[A-Za-z_][A-Za-z_0-9]{0,127}\Z")
_BEGIN = "AGENT_RUNTIME_QUERY_BEGIN\n"
_END = "AGENT_RUNTIME_QUERY_END"


def _file_sha256(path: Path) -> str:
    hasher = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            hasher.update(chunk)
    return hasher.hexdigest()


@dataclass(frozen=True)
class JoernConfig:
    joern_executable: str
    java_home: str
    cpg_file: str
    cpg_sha256: str
    snapshot_digest: str
    release: str
    max_timeout_ms: int = 180_000


class JoernProgramQuery:
    """Query a prebuilt CPG using package-owned Scala script operations."""

    backend_id = "joern"
    supported_operations = frozenset(
        {
            "get_function",
            "find_callers",
            "get_guards",
            "map_arguments",
            "trace_value",
            "check_reachability",
            "inspect_unreachable_after_return",
        }
    )
    operation_specs = {
        "get_function": QueryOperation("get_function", {"symbol": "string"},
                                       ("symbol",)),
        "find_callers": QueryOperation("find_callers", {"symbol": "string"},
                                      ("symbol",)),
        "get_guards": QueryOperation("get_guards", {"symbol": "string"},
                                    ("symbol",)),
        "map_arguments": QueryOperation(
            "map_arguments", {"symbol": "string", "call_line": "integer"},
            ("symbol", "call_line"),
        ),
        "trace_value": QueryOperation(
            "trace_value", {"symbol": "string", "context_symbol": "string"},
            ("symbol", "context_symbol"),
        ),
        "check_reachability": QueryOperation(
            "check_reachability", {"symbol": "string", "context_symbol": "string"},
            ("symbol", "context_symbol"),
        ),
        "inspect_unreachable_after_return": QueryOperation(
            "inspect_unreachable_after_return",
            {
                "symbol": "string", "source_path": "string",
                "return_line": "integer", "following_line": "integer",
            },
            ("symbol", "source_path", "return_line", "following_line"),
        ),
    }
    idempotent_retry = True
    read_only = True

    def __init__(self, config: JoernConfig) -> None:
        self.config = config
        self.backend_version = config.release
        self._cpg = Path(config.cpg_file).resolve()
        self._joern = Path(config.joern_executable).resolve()
        self._java_home = Path(config.java_home).resolve()
        if (
            not self._joern.is_file()
            or not (self._java_home / "bin" / "java").is_file()
        ):
            raise InvalidInput("Joern executable or Java home does not exist")
        if not self._cpg.is_file() or _file_sha256(self._cpg) != config.cpg_sha256:
            raise InvalidInput("CPG is missing or differs from the pinned digest")
        if not config.snapshot_digest or not config.release:
            raise InvalidInput("Joern adapter needs pinned snapshot and release")

    def query(self, request: QueryRequest) -> ProgramResult:
        if request.snapshot_digest != self.config.snapshot_digest:
            raise StaleSnapshot("Joern CPG belongs to another snapshot")
        if _file_sha256(self._cpg) != self.config.cpg_sha256:
            raise StaleSnapshot("Joern CPG changed after adapter construction")
        if request.operation not in self.supported_operations:
            raise InvalidInput("unsupported Joern operation")
        expected_args = {
            "map_arguments": {"symbol", "call_line"},
            "trace_value": {"symbol", "context_symbol"},
            "check_reachability": {"symbol", "context_symbol"},
            "inspect_unreachable_after_return": {
                "symbol", "source_path", "return_line", "following_line"
            },
        }.get(request.operation, {"symbol"})
        if set(request.args) != expected_args or not isinstance(
            request.args["symbol"], str
        ):
            raise InvalidInput("Joern query arguments do not match the operation")
        call_line = request.args.get("call_line", 0)
        if type(call_line) is not int or (
            request.operation == "map_arguments" and not 1 <= call_line <= 1_000_000
        ):
            raise InvalidInput("Joern call line must be a positive integer")
        symbol = request.args["symbol"]
        if not _SYMBOL.fullmatch(symbol):
            raise InvalidInput("Joern symbol must be a simple identifier")
        context_symbol = request.args.get("context_symbol", "")
        if request.operation in {"trace_value", "check_reachability"} and (
            not isinstance(context_symbol, str) or not _SYMBOL.fullmatch(context_symbol)
        ):
            raise InvalidInput("Joern context symbol must be a simple identifier")
        if symbol not in request.scope.get("symbols", ()):
            raise InvalidInput("Joern symbol is outside the query scope")
        if request.operation in {
            "trace_value",
            "check_reachability",
        } and context_symbol not in request.scope.get("symbols", ()):
            raise InvalidInput("Joern context symbol is outside the query scope")
        source_path = request.args.get("source_path", "")
        return_line = request.args.get("return_line", 0)
        following_line = request.args.get("following_line", 0)
        if request.operation == "inspect_unreachable_after_return":
            validate_relative_path(source_path)
            if (
                type(return_line) is not int
                or type(following_line) is not int
                or not 1 <= return_line < following_line <= 1_000_000
            ):
                raise InvalidInput("Joern candidate lines must be ordered positive integers")
            if source_path not in request.scope.get("repository_paths", ()):
                raise InvalidInput("Joern source path is outside the query scope")
        elif request.scope.get("repository_paths"):
            raise InvalidInput("Joern first slice supports symbol-only scopes")
        if not 1 <= request.timeout_ms <= self.config.max_timeout_ms:
            raise InvalidInput("Joern query timeout is outside policy")
        maximum = request.scope.get("max_results")
        if type(maximum) is not int or not 1 <= maximum <= 10_000:
            raise InvalidInput("Joern query needs a bounded result count")
        if request.operation in {"trace_value", "check_reachability"} and maximum > 100:
            raise InvalidInput("Joern flow query may return at most 100 paths")

        script = files("agent_runtime.adapters").joinpath("joern_query.sc")
        with tempfile.TemporaryDirectory(prefix="agent-runtime-joern-") as directory:
            workdir = Path(directory)
            env = os.environ.copy()
            env["JAVA_HOME"] = str(self._java_home)
            search_path = [str(self._java_home / "bin")]
            if sys.platform == "darwin" and not any(
                (Path(path) / "greadlink").is_file()
                for path in env.get("PATH", "").split(os.pathsep)
            ):
                helper = workdir / "bin"
                helper.mkdir()
                link = helper / "greadlink"
                link.write_text(
                    "#!/bin/sh\nexec python3 -c 'import os,sys; "
                    'print(os.path.realpath(sys.argv[-1]))\' "$@"\n'
                )
                link.chmod(0o700)
                search_path.append(str(helper))
            env["PATH"] = os.pathsep.join([*search_path, env.get("PATH", "")])
            command = [
                str(self._joern),
                "--script",
                str(script),
                "--param",
                f"cpgFile={self._cpg}",
                "--param",
                f"operation={request.operation}",
                "--param",
                f"symbol={symbol}",
                "--param",
                f"callLine={call_line}",
                "--param",
                f"contextSymbol={context_symbol}",
                "--param",
                f"sourcePath={source_path}",
                "--param",
                f"returnLine={return_line}",
                "--param",
                f"followingLine={following_line}",
                "--param",
                f"maxResults={maximum}",
            ]
            try:
                completed = subprocess.run(
                    command,
                    cwd=workdir,
                    env=env,
                    capture_output=True,
                    check=False,
                    timeout=request.timeout_ms / 1000,
                )
                stdout, stderr = completed.stdout, completed.stderr
                status = "failed" if completed.returncode else "partial"
            except subprocess.TimeoutExpired as exc:
                stdout, stderr = exc.stdout or b"", exc.stderr or b""
                status = "timeout"
            if _file_sha256(self._cpg) != self.config.cpg_sha256:
                raise StaleSnapshot("Joern CPG changed during query")
            payload = {
                "operation": request.operation,
                "symbol": symbol,
                "call_line": call_line
                if request.operation == "map_arguments"
                else None,
                "context_symbol": context_symbol or None,
                "source_path": source_path or None,
                "return_line": return_line or None,
                "following_line": following_line or None,
                "cpg_sha256": self.config.cpg_sha256,
                "script_sha256": bytes_digest(script.read_bytes()),
                "stdout": stdout.decode("utf-8", errors="replace"),
                "stderr": stderr.decode("utf-8", errors="replace"),
            }
            rows: list[dict] = []
            if status == "partial":
                try:
                    tail = payload["stdout"].split(_BEGIN, 1)[1]
                    body = tail.split(_END, 1)[0]
                    if _END not in tail:
                        raise ValueError("Joern result end marker missing")
                    if request.operation in {"trace_value", "check_reachability"}:
                        payload["flow_text"] = body.strip()
                    else:
                        rows = json.loads(body)
                        if not isinstance(rows, list):
                            raise TypeError("Joern result is not an array")
                except (IndexError, TypeError, ValueError, json.JSONDecodeError):
                    status = "failed"
            if status == "partial":
                if request.operation == "get_function":
                    rows = [row for row in rows if not row.get("isExternal", False)]
                if request.operation not in {"trace_value", "check_reachability"}:
                    payload["rows"] = rows[:maximum]
            raw = canonical_json(payload).encode("utf-8")
            if status in {"failed", "timeout"}:
                return ProgramResult(
                    status,
                    Coverage("declared CPG scope", "query incomplete", "unknown"),
                    raw,
                    ("Joern query did not finish",),
                    payload["stderr"][-1000:] or status,
                )
            omissions = []
            if len(rows) > maximum:
                omissions.append("result limit truncated output")
            if request.operation == "find_callers":
                omissions.append("indirect or unresolved targets not enumerated")
                omissions.append("call-site location and parse completeness not proven")
            elif request.operation == "get_guards":
                omissions.append(
                    "path feasibility and early-return exclusion of the use not proven"
                )
                omissions.append(
                    "control structures outside the resolved method not enumerated"
                )
            elif request.operation == "map_arguments":
                omissions.append("same-name calls and targets may remain ambiguous")
                omissions.append("argument-to-parameter object identity not proven")
            elif request.operation == "trace_value":
                omissions.append("data-flow path may overapproximate executable paths")
                omissions.append(
                    "control feasibility and cross-function return flow not proven"
                )
            elif request.operation == "check_reachability":
                omissions.append(
                    "zero-value data flow does not prove an executable path"
                )
                omissions.append("unresolved call targets and path conditions remain")
            elif request.operation == "inspect_unreachable_after_return":
                omissions.append(
                    "CPG parse was not proven equivalent to the frozen build and preprocessing"
                )
                omissions.append(
                    "CFG disconnection establishes a tool observation, not whole-build coverage"
                )
            else:
                omissions.append("unique parsed function in scoped build not proven")
            coverage = Coverage(
                f"{symbol} in pinned CPG {self.config.cpg_sha256}",
                "fixed Joern symbol query",
                "partial",
                tuple(omissions),
                (bytes_digest(raw),),
            )
            return ProgramResult("partial", coverage, raw, tuple(omissions))
