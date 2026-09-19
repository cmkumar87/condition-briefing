"""Immutable per-run corpus snapshots.

A briefing must be regenerable and auditable months later against exactly the
evidence it was built from. That is the whole reason this store exists, and it
is why writes are append-only: a run that can be silently rewritten cannot
answer "what did we actually know on the day we decided this?"

SQLite for the prototype. The schema is deliberately boring and the access layer
thin, so the move to Postgres is a driver swap rather than a redesign.
"""

from __future__ import annotations

import sqlite3
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path

from app.models import ConditionProfile, CorpusSnapshot, FieldVelocity, SourceRecord

SCHEMA = """
CREATE TABLE IF NOT EXISTS runs (
    run_id            TEXT PRIMARY KEY,
    query             TEXT NOT NULL,
    created_at        TEXT NOT NULL,
    contract_version  TEXT NOT NULL,
    profile_json      TEXT NOT NULL,
    velocity_json     TEXT,
    record_count      INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS records (
    run_id       TEXT NOT NULL,
    source_id    TEXT NOT NULL,
    source_type  TEXT NOT NULL,
    provider     TEXT NOT NULL,
    sort_date    TEXT,
    text_sha     TEXT NOT NULL,
    record_json  TEXT NOT NULL,
    PRIMARY KEY (run_id, source_id),
    FOREIGN KEY (run_id) REFERENCES runs(run_id) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_runs_created ON runs(created_at DESC);
CREATE INDEX IF NOT EXISTS idx_records_run   ON records(run_id);
"""


class SnapshotExistsError(RuntimeError):
    """Raised on an attempt to overwrite an existing run.

    Not a convenience guard — immutability is the audit property the whole
    design rests on.
    """


class CorpusStore:
    def __init__(self, path: Path | str) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as conn:
            conn.executescript(SCHEMA)

    @contextmanager
    def _connect(self) -> Iterator[sqlite3.Connection]:
        conn = sqlite3.connect(self.path)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON")
        try:
            yield conn
            conn.commit()
        finally:
            conn.close()

    # -- writes ------------------------------------------------------------

    @staticmethod
    def new_run_id() -> str:
        return f"run-{uuid.uuid4().hex[:12]}"

    def save(self, snapshot: CorpusSnapshot) -> str:
        if self.exists(snapshot.run_id):
            raise SnapshotExistsError(
                f"run {snapshot.run_id} already exists; snapshots are immutable. "
                "Start a new run rather than rewriting history."
            )
        with self._connect() as conn:
            conn.execute(
                "INSERT INTO runs (run_id, query, created_at, contract_version, "
                "profile_json, velocity_json, record_count) VALUES (?,?,?,?,?,?,?)",
                (
                    snapshot.run_id,
                    snapshot.query,
                    snapshot.created_at.isoformat(),
                    snapshot.contract_version,
                    snapshot.profile.model_dump_json(),
                    snapshot.velocity.model_dump_json() if snapshot.velocity else None,
                    len(snapshot.records),
                ),
            )
            conn.executemany(
                "INSERT INTO records (run_id, source_id, source_type, provider, "
                "sort_date, text_sha, record_json) VALUES (?,?,?,?,?,?,?)",
                [
                    (
                        snapshot.run_id,
                        r.source_id,
                        r.source_type.value,
                        r.provider,
                        r.sort_date.isoformat() if r.sort_date else None,
                        r.text_sha,
                        r.model_dump_json(),
                    )
                    for r in snapshot.records
                ],
            )
        return snapshot.run_id

    def build_and_save(
        self,
        *,
        query: str,
        profile: ConditionProfile,
        records: list[SourceRecord],
        velocity: FieldVelocity | None = None,
        run_id: str | None = None,
    ) -> CorpusSnapshot:
        snapshot = CorpusSnapshot(
            run_id=run_id or self.new_run_id(),
            query=query,
            profile=profile,
            created_at=datetime.now(UTC),
            records=records,
            velocity=velocity,
        )
        self.save(snapshot)
        return snapshot

    # -- reads -------------------------------------------------------------

    def exists(self, run_id: str) -> bool:
        with self._connect() as conn:
            row = conn.execute("SELECT 1 FROM runs WHERE run_id = ?", (run_id,)).fetchone()
        return row is not None

    def load(self, run_id: str) -> CorpusSnapshot:
        with self._connect() as conn:
            run = conn.execute("SELECT * FROM runs WHERE run_id = ?", (run_id,)).fetchone()
            if run is None:
                raise KeyError(f"no such run: {run_id}")
            rows = conn.execute(
                "SELECT record_json FROM records WHERE run_id = ? ORDER BY source_id",
                (run_id,),
            ).fetchall()

        return CorpusSnapshot(
            run_id=run["run_id"],
            query=run["query"],
            profile=ConditionProfile.model_validate_json(run["profile_json"]),
            created_at=datetime.fromisoformat(run["created_at"]),
            records=[SourceRecord.model_validate_json(r["record_json"]) for r in rows],
            velocity=(
                FieldVelocity.model_validate_json(run["velocity_json"])
                if run["velocity_json"]
                else None
            ),
            contract_version=run["contract_version"],
        )

    def list_runs(self, *, limit: int = 50) -> list[dict]:
        """Lightweight listing that does not deserialise any records."""
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT run_id, query, created_at, record_count, contract_version "
                "FROM runs ORDER BY created_at DESC LIMIT ?",
                (limit,),
            ).fetchall()
        return [dict(r) for r in rows]

    def load_record(self, run_id: str, source_id: str) -> SourceRecord:
        """Fetch one record without loading the whole snapshot.

        Verification resolves citations one source at a time; a briefing with
        200 citations should not deserialise a multi-megabyte corpus 200 times.
        """
        with self._connect() as conn:
            row = conn.execute(
                "SELECT record_json FROM records WHERE run_id = ? AND source_id = ?",
                (run_id, source_id),
            ).fetchone()
        if row is None:
            raise KeyError(f"no record {source_id} in run {run_id}")
        return SourceRecord.model_validate_json(row["record_json"])
