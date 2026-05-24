"""Persistent outbox for Hermes notification delivery.

SQLite-backed queue with idempotent enqueue (via dedup_key UNIQUE),
FIFO claim_due with atomic status transition, mark_sent, and
record_failure with exponential backoff and dead-letter after 5 failures.

Concurrency model:
- enqueue: INSERT OR IGNORE + re-select (race-safe via UNIQUE dedup_key)
- claim_due: atomic UPDATE...RETURNING transitions 'pending' -> 'claimed'
- record_failure: 'claimed' -> 'pending' (with backoff) or 'dead-lettered'
- mark_sent: 'claimed' -> 'sent'

Zombie 'claimed' rows after worker crash require offline recovery (out of scope).
"""
from __future__ import annotations

import json
import logging
import sqlite3
import time
import uuid
from contextlib import closing
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Callable


DEAD_LETTER_THRESHOLD = 5
BACKOFF_BASE_SECONDS = 30
BACKOFF_MAX_SECONDS = 3600
BUSY_TIMEOUT_MS = 30000
CONNECT_TIMEOUT_SECONDS = 30
LOCK_RETRY_MAX = 5
LOCK_RETRY_BASE_SLEEP = 0.1

logger = logging.getLogger(__name__)


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


def _retry_on_locked(fn: Callable[..., Any]) -> Callable[..., Any]:
    """Retry SQLite operations on 'database is locked' OperationalError."""
    def wrapper(*args: Any, **kwargs: Any) -> Any:
        for attempt in range(LOCK_RETRY_MAX):
            try:
                return fn(*args, **kwargs)
            except sqlite3.OperationalError as exc:
                if "locked" not in str(exc).lower() or attempt == LOCK_RETRY_MAX - 1:
                    raise
                time.sleep(LOCK_RETRY_BASE_SLEEP * (2 ** attempt))
        raise RuntimeError("unreachable")
    return wrapper


class OutboxStore:
    """SQLite-backed outbox queue with dedup + retry + dead-letter."""

    def __init__(self, db_path: str) -> None:
        self._db_path = str(db_path)

    def init_schema(self) -> None:
        with closing(self._conn()) as conn:
            conn.executescript(_SCHEMA_SQL)
            conn.commit()

    def _conn(self) -> sqlite3.Connection:
        conn = sqlite3.connect(
            self._db_path,
            isolation_level=None,
            timeout=CONNECT_TIMEOUT_SECONDS,
        )
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON")
        conn.execute("PRAGMA journal_mode = WAL")
        conn.execute(f"PRAGMA busy_timeout = {BUSY_TIMEOUT_MS}")
        return conn

    @_retry_on_locked
    def enqueue(self, *,
                channel: str,
                dedup_key: str,
                payload_json: str | None = None,
                payload: dict[str, Any] | None = None,
                now: datetime | None = None,
                **_ignored: Any) -> OutboxRow:
        """Idempotent insert via dedup_key UNIQUE. Returns existing or new row."""
        if payload_json is None and payload is not None:
            payload_json = json.dumps(payload, sort_keys=True)
        if payload_json is None:
            raise ValueError("enqueue requires payload or payload_json")
        ts = _iso(now or _utcnow())
        row_id = uuid.uuid4().hex

        with closing(self._conn()) as conn:
            conn.execute(
                "INSERT OR IGNORE INTO outbox (id, channel, payload_json, dedup_key, "
                "attempts, last_error, status, next_retry_at, created_at, updated_at) "
                "VALUES (?,?,?,?,0,NULL,'pending',?,?,?)",
                (row_id, channel, payload_json, dedup_key, ts, ts, ts),
            )
            row = conn.execute(
                "SELECT * FROM outbox WHERE dedup_key=?", (dedup_key,)
            ).fetchone()
            return _row_to_dataclass(row)

    @_retry_on_locked
    def claim_due(self, *, now: datetime, limit: int = 10) -> list[OutboxRow]:
        """Atomically claim FIFO 'pending' rows due now. Transitions to 'claimed'."""
        ts = _iso(now)
        with closing(self._conn()) as conn:
            cursor = conn.execute(
                "UPDATE outbox SET status='claimed', updated_at=? "
                "WHERE id IN ("
                "  SELECT id FROM outbox "
                "  WHERE status='pending' AND next_retry_at<=? "
                "  ORDER BY created_at ASC, id ASC LIMIT ?"
                ") RETURNING *",
                (ts, ts, limit),
            )
            rows = [_row_to_dataclass(r) for r in cursor.fetchall()]
            rows.sort(key=lambda r: (r.created_at, r.id))
            return rows

    @_retry_on_locked
    def get_by_id(self, row_id: str) -> OutboxRow | None:
        """Return current row state (post-dispatch status-check)."""
        with closing(self._conn()) as conn:
            row = conn.execute(
                "SELECT * FROM outbox WHERE id=?", (row_id,)
            ).fetchone()
            return _row_to_dataclass(row) if row else None

    @_retry_on_locked
    def recover_zombies(self, *, now: datetime, timeout_seconds: int) -> int:
        """Reset claimed-stuck rows (worker-crash mid-dispatch) back to pending.

        Atomically transitions claimed -> pending for rows where
        updated_at < now - timeout_seconds. Returns count recovered.
        """
        cutoff = _iso(now - timedelta(seconds=timeout_seconds))
        with closing(self._conn()) as conn:
            cursor = conn.execute(
                "UPDATE outbox SET status='pending', updated_at=? "
                "WHERE status='claimed' AND updated_at < ? "
                "RETURNING id",
                (_iso(now), cutoff),
            )
            return len(cursor.fetchall())

    @_retry_on_locked
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
            cursor = conn.execute(
                "UPDATE outbox SET status='sent', updated_at=? WHERE id=?",
                (ts, rid),
            )
            if cursor.rowcount == 0:
                logger.warning("outbox.mark_sent missing row_id=%s", rid)

    @_retry_on_locked
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
                logger.warning("outbox.record_failure missing row_id=%s", rid)
                return
            attempts = (row["attempts"] or 0) + 1
            if attempts >= DEAD_LETTER_THRESHOLD:
                new_status = "dead-lettered"
                next_retry = None
            else:
                new_status = "pending"
                backoff_sec = min(
                    BACKOFF_MAX_SECONDS,
                    BACKOFF_BASE_SECONDS * (2 ** (attempts - 1)),
                )
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
    if dt.tzinfo is None or dt.utcoffset() is None:
        raise ValueError("timezone-aware datetime required")
    return dt.astimezone(timezone.utc).isoformat()


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
