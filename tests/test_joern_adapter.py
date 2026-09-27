"""Policy and snapshot boundaries of the optional Joern adapter."""

import tempfile
import unittest
import uuid
from dataclasses import replace
from pathlib import Path

from agent_runtime.adapters import JoernConfig, JoernProgramQuery
from agent_runtime.codec import bytes_digest
from agent_runtime.domain import QueryRequest
from agent_runtime.errors import InvalidInput, StaleSnapshot


class JoernAdapterContracts(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        root = Path(self.temporary.name)
        (root / "bin").mkdir()
        (root / "bin" / "java").write_bytes(b"fixture")
        (root / "joern").write_bytes(b"fixture")
        (root / "cpg.bin.zip").write_bytes(b"fixed CPG")
        self.config = JoernConfig(
            str(root / "joern"),
            str(root),
            str(root / "cpg.bin.zip"),
            bytes_digest(b"fixed CPG"),
            "snapshot",
            "v4-test",
        )
        self.backend = JoernProgramQuery(self.config)

    def tearDown(self):
        self.temporary.cleanup()

    def request(
        self, symbol="getBuffer", snapshot="snapshot", operation="get_function"
    ):
        return QueryRequest(
            str(uuid.uuid4()),
            "task",
            "nullable_source",
            operation,
            snapshot,
            "joern",
            "v4-test",
            "policy",
            {
                "repository_paths": (
                    ["src/example.cpp"]
                    if operation == "inspect_unreachable_after_return" else []
                ),
                "symbols": [symbol],
                "build_variant_id": "debug",
                "max_results": 10,
                "include_indirect": False,
            },
            {
                "symbol": symbol,
                **({"call_line": 4} if operation == "map_arguments" else {}),
                **({"context_symbol": "process"} if operation == "trace_value" else {}),
                **(
                    {"context_symbol": "main"}
                    if operation == "check_reachability"
                    else {}
                ),
                **(
                    {
                        "source_path": "src/example.cpp",
                        "return_line": 10,
                        "following_line": 11,
                    }
                    if operation == "inspect_unreachable_after_return"
                    else {}
                ),
            },
            str(uuid.uuid4()),
            1000,
        )

    def test_rejects_model_supplied_dsl_before_launch(self):
        self.assertIn("get_guards", self.backend.supported_operations)
        self.assertIn("map_arguments", self.backend.supported_operations)
        self.assertIn("trace_value", self.backend.supported_operations)
        self.assertIn("check_reachability", self.backend.supported_operations)
        self.assertIn(
            "inspect_unreachable_after_return", self.backend.supported_operations
        )
        with self.assertRaises(InvalidInput):
            self.backend.query(self.request("getBuffer;system('shell')"))
        request = self.request()
        request = replace(request, args={**request.args, "dsl": "cpg.method.l"})
        with self.assertRaises(InvalidInput):
            self.backend.query(request)
        invalid_line = self.request(operation="map_arguments")
        invalid_line = replace(
            invalid_line, args={**invalid_line.args, "call_line": True}
        )
        with self.assertRaises(InvalidInput):
            self.backend.query(invalid_line)
        bad_context = self.request(operation="trace_value")
        bad_context = replace(
            bad_context, args={**bad_context.args, "context_symbol": "process;Bash"}
        )
        with self.assertRaises(InvalidInput):
            self.backend.query(bad_context)
        out_of_scope = self.request(operation="check_reachability")
        with self.assertRaises(InvalidInput):
            self.backend.query(out_of_scope)
        semantic = self.request(operation="inspect_unreachable_after_return")
        for args in (
            {**semantic.args, "source_path": "../example.cpp"},
            {**semantic.args, "following_line": 10},
            {**semantic.args, "return_line": True},
        ):
            with self.assertRaises(InvalidInput):
                self.backend.query(replace(semantic, args=args))
        with self.assertRaises(InvalidInput):
            self.backend.query(replace(
                semantic,
                scope={**semantic.scope, "repository_paths": ["src/other.cpp"]},
            ))

    def test_rejects_cross_snapshot_and_tampered_cpg(self):
        with self.assertRaises(StaleSnapshot):
            self.backend.query(self.request(snapshot="other"))
        Path(self.config.cpg_file).write_bytes(b"changed")
        with self.assertRaises(StaleSnapshot):
            self.backend.query(self.request())
        with self.assertRaises(InvalidInput):
            JoernProgramQuery(self.config)


if __name__ == "__main__":
    unittest.main()
