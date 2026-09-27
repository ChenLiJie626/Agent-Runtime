"""Deterministic helpers for the public-source Joern semantic probe."""

import importlib.util
import sys
import unittest
from pathlib import Path

from agent_runtime.errors import InvalidInput

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))
SPEC = importlib.util.spec_from_file_location(
    "run_s4_joern_unreachable_probe",
    ROOT / "tools/run_s4_joern_unreachable_probe.py",
)
PROBE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(PROBE)
sys.path.remove(str(ROOT / "tools"))


class JoernProbeTests(unittest.TestCase):
    def test_enclosing_symbol_is_bounded_and_handles_qualified_methods(self):
        source = """\
bool Type::method(int value) {
  use(value);
  return true;
  dead();
}
"""
        self.assertEqual(PROBE.enclosing_symbol(source, 3), "method")
        self.assertIsNone(PROBE.enclosing_symbol("return true;\ndead();\n", 1))

    def test_cfg_summary_requires_all_groups_and_preserves_disconnection(self):
        method = {"_id": 1, "_label": "METHOD"}
        payload = {"rows": [
            {"kind": "candidate_methods", "nodes": [method]},
            {"kind": "return_nodes", "nodes": [{"_label": "RETURN"}]},
            {"kind": "following_nodes", "nodes": [{"_label": "CALL"}]},
            {"kind": "return_cfg_ancestors", "nodes": [method]},
            {"kind": "following_cfg_ancestors", "nodes": []},
        ]}
        summary = PROBE._summary(payload)
        self.assertTrue(summary["return_reaches_method_entry"])
        self.assertFalse(summary["following_reaches_method_entry"])
        with self.assertRaises(InvalidInput):
            PROBE._summary({"rows": payload["rows"][:-1]})


if __name__ == "__main__":
    unittest.main()
