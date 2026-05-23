"""Persistent outbox for Hermes notification delivery.

SQLite-backed queue with idempotent enqueue (via dedup_key UNIQUE),
FIFO claim_due, mark_sent, and record_failure with exponential backoff
and dead-letter after 5 failures.
"""
from __future__ import annotations

import json
import sqlite3
import uuid
from contextlib import closing
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any


DEAD_LETTER_THRESHOLD = 5
BACKOFF_BASE_SECONDS = 30
BACKOFF_MAX_SECONDS = 3600


@dataclass(slots=True)
class OutboxRow:
    id: str
    channel: str
    payload_json: str
    dedup_key: str
    attempts: int
    last_error: str | None
    status: str
    next_retry_at: str | None
    created_at: str
    updated_at: str


class OutboxStore:
    """SQLite-backed outbox queue with dedup + retry + dead-letter."""

    def __init__(self, db_path: str) -> None:
        self._db_path = str(db_path)

    def init_schema(self) -> None:
        with closing(self._conn()) as conn:
            conn.executescript(_SCHEMA_SQL)
            conn.commit()

    def _conn(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self._db_path, isolation_level=None)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON")
        conn.execute("PRAGMA journal_mode = WAL")
        return conn

    def enqueue(self, *,
                channel: str,
                dedup_key: str,
                payload_json: str | None = None,
                payload: dict[str, Any] | None = None,
                now: datetime | None = None,
                **_ignored: Any) -> OutboxRow:
        """Idempotent insert. If dedup_key exists, returns existing row."""
        if payload_json is None and payload is not None:
            payload_json = json.dumps(payload, sort_keys=True)
        if payload_json is None:
            raise ValueError("enqueue requires payload or payload_json")
        ts = _iso(now or _utcnow())

        with closing(self._conn()) as conn:
            existing = conn.execute(
                "SELECT * FROM outbox WHERE dedup_key=?", (dedup_key,)
            ).fetchone()
            if existing:
                return _row_to_dataclass(existing)

            row_id = uuid.uuid4().hex
            conn.execute(
                "INSERT INTO outbox (id, channel, payload_json, dedup_key, "
                "attempts, last_error, status, next_retry_at, created_at, updated_at) "
                "VALUES (?,?,?,?,0,NULL,'pending',?,?,?)",
                (row_id, channel, payload_json, dedup_key, ts, ts, ts),
            )
            return OutboxRow(
                id=row_id, channel=channel, payload_json=payload_json,
                dedup_key=dedup_key, attempts=0, last_error=None,
                status="pending", next_retry_at=ts,
                created_at=ts, updated_at=ts,
            )

    def claim_due(self, *, now: datetime, limit: int = 10) -> list[OutboxRow]:
        """Return rows with status='pending' AND next_retry_at <= now, FIFO."""
        ts = _iso(now)
        with closing(self._conn()) as conn:
            cursor = conn.execute(
                "SELECT * FROM outbox WHERE status='pending' AND next_retry_at<=? "
                "ORDER BY created_at ASC, id ASC LIMIT ?",
                (ts, limit),
            )
            return [_row_to_dataclass(r) for r in cursor.fetchall()]

    def mark_sent(self, *,
                  row_id: str | None = None,
                  id: str | None = None,
                  message_id: str | None = None,
                  now: datetime | None = None,
                  **_ignored: Any) -> None:
        rid = row_id or id or message_id
        if not rid:
            raise ValueError("mark_sent requires row_id|id|message_id")
        ts = _iso(now or _utcnow())
        with closing(self._conn()) as conn:
            conn.execute(
                "UPDATE outbox SET status='sent', updated_at=? WHERE id=?",
                (ts, rid),
            )

    def record_failure(self, *,
                       row_id: str | None = None,
                       id: str | None = None,
                       message_id: str | None = None,
                       error: str | None = None,
                       last_error: str | None = None,
                       now: datetime | None = None,
                       **_ignored: Any) -> None:
        rid = row_id or id or message_id
        if not rid:
            raise ValueError("record_failure requires row_id|id|message_id")
        err = error or last_error or "unknown"
        now_dt = now or _utcnow()
        ts = _iso(now_dt)

        with closing(self._conn()) as conn:
            row = conn.execute("SELECT attempts FROM outbox WHERE id=?", (rid,)).fetchone()
            if row is None:
                return
            attempts = (row["attempts"] or 0) + 1
            if attempts >= DEAD_LETTER_THRESHOLD:
                new_status = "dead-lettered"
                next_retry = None
            else:
                new_status = "pending"
                backoff_sec = min(BACKOFF_MAX_SECONDS, BACKOFF_BASE_SECONDS * (2 ** (attempts - 1)))
                next_retry = _iso(now_dt + timedelta(seconds=backoff_sec))

            conn.execute(
                "UPDATE outbox SET attempts=?, last_error=?, status=?, "
                "next_retry_at=?, updated_at=? WHERE id=?",
                (attempts, err, new_status, next_retry, ts, rid),
            )


_SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS outbox (
    id            TEXT PRIMARY KEY,
    channel       TEXT NOT NULL,
    payload_json  TEXT NOT NULL,
    dedup_key     TEXT NOT NULL,
    attempts      INTEGER NOT NULL DEFAULT 0,
    last_error    TEXT,
    status        TEXT NOT NULL,
    next_retry_at TEXT,
    created_at    TEXT NOT NULL,
    updated_at    TEXT NOT NULL
);
CREATE UNIQUE INDEX IF NOT EXISTS outbox_dedup_key_idx ON outbox(dedup_key);
CREATE INDEX IF NOT EXISTS outbox_due_idx ON outbox(status, next_retry_at);
"""


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _iso(dt: datetime) -> str:
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.isoformat()


def _row_to_dataclass(row: sqlite3.Row) -> OutboxRow:
    return OutboxRow(
        id=row["id"],
        channel=row["channel"],
        payload_json=row["payload_json"],
        dedup_key=row["dedup_key"],
        attempts=row["attempts"] or 0,
        last_error=row["last_error"],
        status=row["status"],
        next_retry_at=row["next_retry_at"],
        created_at=row["created_at"],
        updated_at=row["updated_at"],
    )
