"""S3 persisted envelope and public surface compatibility checks."""

import json
import tempfile
import unittest
from pathlib import Path

import agent_runtime
from agent_runtime import InvalidInput, Mutation, SQLiteStore, canonical_json, digest


class ReleaseContracts(unittest.TestCase):
    def test_public_exports_exist_and_canonical_digest_is_stable(self):
        self.assertEqual(len(agent_runtime.__all__),
                         len(set(agent_runtime.__all__)))
        for name in agent_runtime.__all__:
            self.assertTrue(hasattr(agent_runtime, name), name)
        self.assertEqual(canonical_json({"b": 2, "a": 1}), '{"a":1,"b":2}')
        self.assertEqual(digest({"b": 2, "a": 1}),
                         digest({"a": 1, "b": 2}))

    def test_v1_0_envelope_rebuilds_and_new_events_use_v1_1(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            store = SQLiteStore(root / "state.sqlite3", root / "artifacts")
            try:
                store.append("analysis", Mutation(
                    "FixtureCreated", {"value": 1}, "fixture", "one",
                    {"value": 1},
                ))
                row = store.connection.execute(
                    "SELECT * FROM events WHERE analysis_id=? AND seq=1",
                    ("analysis",),
                ).fetchone()
                projection = json.loads(row["projection_json"])
                projection["schema_version"] = {"major": 1, "minor": 0}
                projection_json = canonical_json(projection)
                payload = json.loads(row["payload_json"])
                event_digest = digest({
                    "payload": payload,
                    "projection_kind": row["projection_kind"],
                    "projection_id": row["projection_id"],
                    "projection": projection,
                })
                store.connection.execute(
                    "UPDATE events SET schema_version=?, projection_json=?, "
                    "payload_digest=? WHERE analysis_id=? AND seq=1",
                    (canonical_json({"major": 1, "minor": 0}),
                     projection_json, event_digest, "analysis"),
                )
                store.connection.execute(
                    "UPDATE records SET data_json=? WHERE kind=? AND record_id=?",
                    (projection_json, "fixture", "one"),
                )
                self.assertEqual(store.get("fixture", "one"), {"value": 1})
                self.assertEqual(store.events("analysis")[0]["schema_version"],
                                 {"major": 1, "minor": 0})
                store.rebuild_projections("analysis")
                self.assertEqual(store.get("fixture", "one"), {"value": 1})
                store.append("analysis", Mutation(
                    "FixtureUpdated", {"value": 2}, "fixture", "one",
                    {"value": 2},
                ))
                self.assertEqual(store.events("analysis")[-1]["schema_version"],
                                 {"major": 1, "minor": 1})
                self.assertEqual(store.get("fixture", "one"), {"value": 2})
            finally:
                store.close()

    def test_future_minor_and_unknown_major_are_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            store = SQLiteStore(root / "state.sqlite3", root / "artifacts")
            try:
                store.append("analysis", Mutation(
                    "FixtureCreated", {"value": 1}, "fixture", "one",
                    {"value": 1},
                ))
                for version in ({"major": 1, "minor": 2},
                                {"major": 2, "minor": 0},
                                {"major": 1, "minor": True}):
                    with self.subTest(version=version):
                        store.connection.execute(
                            "UPDATE records SET data_json=? WHERE kind=? AND record_id=?",
                            (canonical_json({"schema_version": version,
                                             "data": {"value": 1}}),
                             "fixture", "one"),
                        )
                        with self.assertRaises(InvalidInput):
                            store.get("fixture", "one")
            finally:
                store.close()


if __name__ == "__main__":
    unittest.main()
