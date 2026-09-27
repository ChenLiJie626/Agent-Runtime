"""SQLite event journal, rebuildable projections and immutable raw artifacts."""

from __future__ import annotations

import json
import os
import sqlite3
import tempfile
import uuid
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .codec import (
    SCHEMA_VERSION,
    bytes_digest,
    canonical_json,
    digest,
    utc_now,
    validate_schema,
)
from .errors import Conflict, EvidenceIntegrityError, InvalidInput


@dataclass(frozen=True)
class Mutation:
    event_type: str
    payload: dict[str, Any]
    kind: str | None = None
    record_id: str | None = None
    record: dict[str, Any] | None = None
    task_id: str | None = None
    attempt_id: str | None = None
    causation_id: str | None = None
    correlation_id: str | None = None


class SQLiteStore:
    """One process may own this object; SQLite transactions arbitrate other writers."""

    def __init__(self, database: str | Path, artifacts: str | Path) -> None:
        self.database = str(database)
        self.artifacts = Path(artifacts)
        self.artifacts.mkdir(parents=True, exist_ok=True)
        self.connection = sqlite3.connect(
            self.database, isolation_level=None, timeout=30
        )
        self.connection.row_factory = sqlite3.Row
        self.connection.execute("PRAGMA journal_mode=WAL")
        self.connection.execute("PRAGMA synchronous=FULL")
        self.connection.execute("PRAGMA foreign_keys=ON")
        self.connection.executescript("""
            CREATE TABLE IF NOT EXISTS events (
                analysis_id TEXT NOT NULL,
                seq INTEGER NOT NULL,
                event_id TEXT NOT NULL UNIQUE,
                event_type TEXT NOT NULL,
                task_id TEXT,
                attempt_id TEXT,
                occurred_at TEXT NOT NULL,
                schema_version TEXT NOT NULL,
                causation_id TEXT,
                correlation_id TEXT,
                payload_digest TEXT NOT NULL,
                payload_json TEXT NOT NULL,
                projection_kind TEXT,
                projection_id TEXT,
                projection_json TEXT,
                PRIMARY KEY (analysis_id, seq)
            );
            CREATE TABLE IF NOT EXISTS records (
                kind TEXT NOT NULL,
                record_id TEXT NOT NULL,
                analysis_id TEXT NOT NULL,
                updated_seq INTEGER NOT NULL,
                data_json TEXT NOT NULL,
                PRIMARY KEY (kind, record_id)
            );
            CREATE INDEX IF NOT EXISTS records_by_analysis ON records (analysis_id, kind);
            CREATE TABLE IF NOT EXISTS query_keys (
                analysis_id TEXT NOT NULL,
                idempotency_key TEXT NOT NULL,
                request_digest TEXT NOT NULL,
                query_id TEXT NOT NULL,
                PRIMARY KEY (analysis_id, idempotency_key)
            );
            CREATE TABLE IF NOT EXISTS candidate_keys (
                analysis_id TEXT NOT NULL,
                candidate_digest TEXT NOT NULL,
                task_id TEXT NOT NULL,
                PRIMARY KEY (analysis_id, candidate_digest)
            );
            CREATE TABLE IF NOT EXISTS leases (
                binding_id TEXT PRIMARY KEY,
                sdk_session_id TEXT UNIQUE,
                owner_id TEXT NOT NULL,
                lease_epoch INTEGER NOT NULL,
                expires_at REAL NOT NULL,
                binding_digest TEXT NOT NULL
            );
        """)

    def close(self) -> None:
        self.connection.close()

    def _append_locked(
        self, analysis_id: str, mutations: Iterable[Mutation]
    ) -> list[int]:
        seq = self.connection.execute(
            "SELECT COALESCE(MAX(seq), 0) FROM events WHERE analysis_id=?",
            (analysis_id,),
        ).fetchone()[0]
        assigned: list[int] = []
        for mutation in mutations:
            if mutation.kind is None and (
                mutation.record_id is not None or mutation.record is not None
            ):
                raise InvalidInput("incomplete projection mutation")
            if mutation.kind is not None and (
                mutation.record_id is None or mutation.record is None
            ):
                raise InvalidInput("incomplete projection mutation")
            seq += 1
            payload_json = canonical_json(mutation.payload)
            record_json = (
                canonical_json(
                    {"schema_version": SCHEMA_VERSION, "data": mutation.record}
                )
                if mutation.record is not None
                else None
            )
            event_digest = digest(
                {
                    "payload": mutation.payload,
                    "projection_kind": mutation.kind,
                    "projection_id": mutation.record_id,
                    "projection": json.loads(record_json) if record_json else None,
                }
            )
            self.connection.execute(
                "INSERT INTO events VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    analysis_id,
                    seq,
                    str(uuid.uuid4()),
                    mutation.event_type,
                    mutation.task_id,
                    mutation.attempt_id,
                    utc_now(),
                    canonical_json(SCHEMA_VERSION),
                    mutation.causation_id,
                    mutation.correlation_id,
                    event_digest,
                    payload_json,
                    mutation.kind,
                    mutation.record_id,
                    record_json,
                ),
            )
            if mutation.kind is not None:
                self.connection.execute(
                    "INSERT INTO records VALUES (?,?,?,?,?) ON CONFLICT(kind,record_id) DO UPDATE SET "
                    "analysis_id=excluded.analysis_id,updated_seq=excluded.updated_seq,data_json=excluded.data_json",
                    (mutation.kind, mutation.record_id, analysis_id, seq, record_json),
                )
            assigned.append(seq)
        return assigned

    def append(self, analysis_id: str, *mutations: Mutation) -> list[int]:
        try:
            self.connection.execute("BEGIN IMMEDIATE")
            result = self._append_locked(analysis_id, mutations)
            self.connection.execute("COMMIT")
            return result
        except Exception:
            if self.connection.in_transaction:
                self.connection.execute("ROLLBACK")
            raise

    def high_watermark(self, analysis_id: str) -> int:
        row = self.connection.execute(
            "SELECT COALESCE(MAX(seq), 0) AS seq FROM events WHERE analysis_id=?",
            (analysis_id,),
        ).fetchone()
        return int(row["seq"])

    def get(self, kind: str, record_id: str) -> dict[str, Any] | None:
        row = self.connection.execute(
            "SELECT data_json FROM records WHERE kind=? AND record_id=?",
            (kind, record_id),
        ).fetchone()
        if not row:
            return None
        envelope = json.loads(row["data_json"])
        validate_schema(envelope)
        return envelope["data"]

    def list(self, kind: str, analysis_id: str) -> list[dict[str, Any]]:
        rows = self.connection.execute(
            "SELECT data_json FROM records WHERE kind=? AND analysis_id=? ORDER BY record_id",
            (kind, analysis_id),
        ).fetchall()
        result = []
        for row in rows:
            envelope = json.loads(row["data_json"])
            validate_schema(envelope)
            result.append(envelope["data"])
        return result

    def events(self, analysis_id: str) -> list[dict[str, Any]]:
        rows = self.connection.execute(
            "SELECT * FROM events WHERE analysis_id=? ORDER BY seq", (analysis_id,)
        ).fetchall()
        result = []
        for row in rows:
            payload = json.loads(row["payload_json"])
            if (
                digest(
                    {
                        "payload": payload,
                        "projection_kind": row["projection_kind"],
                        "projection_id": row["projection_id"],
                        "projection": json.loads(row["projection_json"])
                        if row["projection_json"]
                        else None,
                    }
                )
                != row["payload_digest"]
            ):
                raise EvidenceIntegrityError("event payload digest mismatch")
            validate_schema({"schema_version": json.loads(row["schema_version"])})
            result.append(
                {
                    "seq": row["seq"],
                    "event_id": row["event_id"],
                    "event_type": row["event_type"],
                    "task_id": row["task_id"],
                    "attempt_id": row["attempt_id"],
                    "payload": payload,
                    "schema_version": json.loads(row["schema_version"]),
                    "causation_id": row["causation_id"],
                    "correlation_id": row["correlation_id"],
                }
            )
        return result

    def rebuild_projections(self, analysis_id: str) -> None:
        """Recreate this analysis's derived records from the immutable journal."""
        try:
            self.connection.execute("BEGIN IMMEDIATE")
            rows = self.connection.execute(
                "SELECT seq,projection_kind,projection_id,projection_json,payload_json,payload_digest "
                "FROM events WHERE analysis_id=? ORDER BY seq",
                (analysis_id,),
            ).fetchall()
            checked = []
            for row in rows:
                if (
                    digest(
                        {
                            "payload": json.loads(row["payload_json"]),
                            "projection_kind": row["projection_kind"],
                            "projection_id": row["projection_id"],
                            "projection": json.loads(row["projection_json"])
                            if row["projection_json"]
                            else None,
                        }
                    )
                    != row["payload_digest"]
                ):
                    raise EvidenceIntegrityError("event payload digest mismatch")
                if row["projection_json"] is not None:
                    validate_schema(json.loads(row["projection_json"]))
                    checked.append(row)
            self.connection.execute(
                "DELETE FROM records WHERE analysis_id=?", (analysis_id,)
            )
            self.connection.execute(
                "DELETE FROM query_keys WHERE analysis_id=?", (analysis_id,)
            )
            self.connection.execute(
                "DELETE FROM candidate_keys WHERE analysis_id=?", (analysis_id,)
            )
            for row in checked:
                self.connection.execute(
                    "INSERT INTO records VALUES (?,?,?,?,?) ON CONFLICT(kind,record_id) DO UPDATE SET "
                    "analysis_id=excluded.analysis_id,updated_seq=excluded.updated_seq,data_json=excluded.data_json",
                    (
                        row["projection_kind"],
                        row["projection_id"],
                        analysis_id,
                        row["seq"],
                        row["projection_json"],
                    ),
                )
            query_rows = self.connection.execute(
                "SELECT payload_json FROM events WHERE analysis_id=? AND event_type='QueryRequested' ORDER BY seq",
                (analysis_id,),
            ).fetchall()
            for row in query_rows:
                payload = json.loads(row["payload_json"])
                self.connection.execute(
                    "INSERT INTO query_keys VALUES (?,?,?,?)",
                    (
                        analysis_id,
                        payload["idempotency_key"],
                        payload["request_digest"],
                        payload["query_id"],
                    ),
                )
            candidate_rows = self.connection.execute(
                "SELECT payload_json FROM events WHERE analysis_id=? AND event_type='TaskCreated' ORDER BY seq",
                (analysis_id,),
            ).fetchall()
            for row in candidate_rows:
                payload = json.loads(row["payload_json"])
                self.connection.execute(
                    "INSERT INTO candidate_keys VALUES (?,?,?)",
                    (analysis_id, payload["candidate_digest"], payload["task_id"]),
                )
            self.connection.execute("COMMIT")
        except Exception:
            if self.connection.in_transaction:
                self.connection.execute("ROLLBACK")
            raise

    def artifact_path(self, artifact_digest: str) -> Path:
        if len(artifact_digest) != 64 or any(
            char not in "0123456789abcdef" for char in artifact_digest
        ):
            raise InvalidInput("invalid artifact digest")
        return self.artifacts / artifact_digest[:2] / artifact_digest

    def put_artifact(self, content: bytes) -> str:
        artifact_digest = bytes_digest(content)
        target = self.artifact_path(artifact_digest)
        target.parent.mkdir(parents=True, exist_ok=True)
        if target.exists():
            self.read_artifact(artifact_digest)
            return artifact_digest
        handle, temporary = tempfile.mkstemp(dir=target.parent, prefix=".pending-")
        try:
            with os.fdopen(handle, "wb") as output:
                output.write(content)
                output.flush()
                os.fsync(output.fileno())
            os.replace(temporary, target)
            directory = os.open(target.parent, os.O_RDONLY)
            try:
                os.fsync(directory)
            finally:
                os.close(directory)
        finally:
            if os.path.exists(temporary):
                os.unlink(temporary)
        return artifact_digest

    def read_artifact(self, artifact_digest: str) -> bytes:
        try:
            content = self.artifact_path(artifact_digest).read_bytes()
        except OSError as exc:
            raise EvidenceIntegrityError(
                f"artifact missing: {artifact_digest}"
            ) from exc
        if bytes_digest(content) != artifact_digest:
            raise EvidenceIntegrityError(f"artifact digest mismatch: {artifact_digest}")
        return content

    def reserve_query(
        self,
        analysis_id: str,
        *,
        key: str,
        request_digest: str,
        query_id: str,
        request_record: dict[str, Any],
        task_id: str,
    ) -> tuple[str, bool]:
        """Return (original query ID, created); reject conflicting reuse."""
        try:
            self.connection.execute("BEGIN IMMEDIATE")
            row = self.connection.execute(
                "SELECT request_digest,query_id FROM query_keys WHERE analysis_id=? AND idempotency_key=?",
                (analysis_id, key),
            ).fetchone()
            if row:
                if row["request_digest"] != request_digest:
                    raise Conflict("idempotency key has different request digest")
                self.connection.execute("COMMIT")
                return str(row["query_id"]), False
            self.connection.execute(
                "INSERT INTO query_keys VALUES (?,?,?,?)",
                (analysis_id, key, request_digest, query_id),
            )
            self._append_locked(
                analysis_id,
                [
                    Mutation(
                        "QueryRequested",
                        {
                            "query_id": query_id,
                            "idempotency_key": key,
                            "request_digest": request_digest,
                            "check_id": request_record["check_id"],
                        },
                        "query",
                        query_id,
                        request_record,
                        task_id,
                    )
                ],
            )
            self.connection.execute("COMMIT")
            return query_id, True
        except Exception:
            if self.connection.in_transaction:
                self.connection.execute("ROLLBACK")
            raise

    def reserve_candidate(
        self,
        analysis_id: str,
        *,
        candidate_digest: str,
        task_id: str,
        mutations: Iterable[Mutation],
        origin_records: Iterable[tuple[str, dict[str, Any]]] = (),
    ) -> str:
        """Atomically deduplicate a candidate while retaining every provenance origin."""
        try:
            self.connection.execute("BEGIN IMMEDIATE")
            row = self.connection.execute(
                "SELECT task_id FROM candidate_keys WHERE analysis_id=? AND candidate_digest=?",
                (analysis_id, candidate_digest),
            ).fetchone()
            actual_task_id = str(row["task_id"]) if row else task_id
            origin_mutations = []
            for origin_id, origin in origin_records:
                record = {
                    **origin,
                    "analysis_id": analysis_id,
                    "task_id": actual_task_id,
                }
                existing = self.connection.execute(
                    "SELECT data_json FROM records "
                    "WHERE kind='candidate_origin' AND record_id=?",
                    (origin_id,),
                ).fetchone()
                if existing:
                    envelope = json.loads(existing["data_json"])
                    validate_schema(envelope)
                    if envelope["data"] != record:
                        raise Conflict("candidate origin is already bound differently")
                    continue
                origin_mutations.append(Mutation(
                    "CandidateOriginRecorded",
                    {
                        "candidate_digest": candidate_digest,
                        "origin_id": origin_id,
                        "discoverer_id": origin["discoverer_id"],
                        "discoverer_version": origin["discoverer_version"],
                        "origin_candidate_id": origin["origin_candidate_id"],
                        "discovery_ref": origin["discovery_ref"],
                    },
                    "candidate_origin",
                    origin_id,
                    record,
                    actual_task_id,
                ))
            if row:
                if origin_mutations:
                    self._append_locked(analysis_id, origin_mutations)
                self.connection.execute("COMMIT")
                return actual_task_id
            self.connection.execute(
                "INSERT INTO candidate_keys VALUES (?,?,?)",
                (analysis_id, candidate_digest, task_id),
            )
            self._append_locked(analysis_id, (*mutations, *origin_mutations))
            self.connection.execute("COMMIT")
            return task_id
        except Exception:
            if self.connection.in_transaction:
                self.connection.execute("ROLLBACK")
            raise
