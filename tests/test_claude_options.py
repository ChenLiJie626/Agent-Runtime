"""SDK adapter configuration smoke test; no model request or account needed."""

import importlib.util
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from agent_runtime.adapters import ClaudeAgentExecutor, ClaudeConfig
from agent_runtime.errors import SessionUnavailable
from agent_runtime.ports import RoleRunRequest


@unittest.skipUnless(
    importlib.util.find_spec("claude_agent_sdk"), "Claude optional extra not installed"
)
class ClaudeOptionsTests(unittest.TestCase):
    def test_missing_transcript_is_detected_before_sdk_run(self):
        executor = ClaudeAgentExecutor(
            ClaudeConfig("/tmp", "fixture", "1", "policy")
        )
        request = RoleRunRequest(
            "task", "attempt", "investigator",
            SimpleNamespace(snapshot_digest="snapshot"),
            resume_session_id="00000000-0000-4000-8000-000000000000",
        )
        with patch("claude_agent_sdk.get_session_messages", return_value=[]):
            with self.assertRaises(SessionUnavailable):
                executor.run(request, SimpleNamespace())

    def test_capability_claims_match_executor_surface(self):
        capabilities = ClaudeAgentExecutor(
            ClaudeConfig("/tmp", "fixture", "1", "policy")
        ).describe_capabilities()
        self.assertFalse(capabilities.stream_events)
        self.assertFalse(capabilities.interrupt)
        self.assertFalse(capabilities.usage_reporting)
        self.assertTrue(capabilities.explicit_resume)
        self.assertTrue(capabilities.structured_output)
        self.assertTrue(capabilities.custom_tools)
        self.assertTrue(capabilities.deny_tools)
        self.assertTrue(capabilities.isolated_session)

    def test_roles_use_only_runtime_tools_and_isolated_settings(self):
        executor = ClaudeAgentExecutor(
            ClaudeConfig(
                "/tmp",
                "fixture",
                "1",
                "policy",
                model="gpt-6-sol",
                sdk_env={
                    "ANTHROPIC_BASE_URL": "http://127.0.0.1:15721",
                    "ANTHROPIC_AUTH_TOKEN": "placeholder",
                },
            )
        )
        for role, kind in (("investigator", "investigator"),
                           ("verifier", "verifier"),
                           ("boundary-expert", "specialist")):
            request = RoleRunRequest(
                "task", "attempt", role, SimpleNamespace(snapshot_digest="snapshot"),
                role_kind=kind,
            )
            options = executor.build_options(request, SimpleNamespace())
            self.assertEqual(options.setting_sources, [])
            self.assertEqual(options.model, "gpt-6-sol")
            self.assertEqual(
                options.env["ANTHROPIC_BASE_URL"], "http://127.0.0.1:15721"
            )
            self.assertEqual(options.env["ANTHROPIC_AUTH_TOKEN"], "placeholder")
            self.assertEqual(options.permission_mode, "dontAsk")
            self.assertTrue(options.strict_mcp_config)
            self.assertEqual(
                set(options.tools),
                {
                    "mcp__defect_runtime__query_program",
                    "mcp__defect_runtime__read_evidence",
                },
            )
            self.assertEqual(options.output_format["type"], "json_schema")
            if kind == "specialist":
                self.assertIn("summary", options.output_format["schema"]["required"])
            self.assertEqual(set(options.mcp_servers), {"defect_runtime"})


if __name__ == "__main__":
    unittest.main()
