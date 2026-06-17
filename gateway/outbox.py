"""Persistent outbox for Hermes notification delivery.

SQLite-backed queue with idempotent enqueue (via dedup_key UNIQUE),
FIFO claim_due with atomic status transition + claim-token fencing,
mark_sent, and record_failure with exponential backoff and
dead-letter after 5 failures.

Concurrency model (Round-2 hardening for Track C):
- enqueue: INSERT OR IGNORE + re-select (race-safe via UNIQUE dedup_key)
- claim_due: atomic UPDATE...RETURNING transitions pending -> claimed,
  generates fresh claim_token per claim. Token returned to caller.
- mark_sent: requires claim_token in WHERE — stale workers cannot
  flip already-reclaimed rows.
- record_failure: requires claim_token in WHERE. Increments attempts,
  may transition to dead-lettered (after 5) or back to pending.
- recover_zombies: claimed-stuck rows past timeout -> attempts++,
  dead-letter on threshold, otherwise pending with backoff. Treats
  zombie as a failed delivery attempt (prevents infinite retry).
"""
from __future__ import annotations

import json
import logging
import sqlite3
import uuid
from contextlib import closing
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any

from ._sqlite_helpers import iso as _iso, retry_on_locked, utcnow as _utcnow


DEAD_LETTER_THRESHOLD = 5
BACKOFF_BASE_SECONDS = 30
BACKOFF_MAX_SECONDS = 3600
BUSY_TIMEOUT_MS = 30000
CONNECT_TIMEOUT_SECONDS = 30
# Re-export aliases for backward-compat with private importers
_retry_on_locked = retry_on_locked

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
    claim_token: str | None = None


class OutboxStore:
    """SQLite-backed outbox queue with dedup + retry + dead-letter + claim-fence."""

    def __init__(self, db_path: str) -> None:
        self._db_path = str(db_path)

    def init_schema(self) -> None:
        with closing(self._conn()) as conn:
            conn.executescript(_SCHEMA_SQL)
            self._ensure_claim_token_column(conn)
            conn.commit()

    @staticmethod
    def _ensure_claim_token_column(conn: sqlite3.Connection) -> None:
        """Idempotent migration: add claim_token to existing pre-Round-2 tables.

        Round-2 (Track A2 MEDIUM-5): concurrent CLI startups could both see the
        column missing and race the ALTER. We catch 'duplicate column' so the
        loser of the race continues without error.
        """
        cols = {row[1] for row in conn.execute("PRAGMA table_info(outbox)").fetchall()}
        if "claim_token" in cols:
            return
        try:
            conn.execute("ALTER TABLE outbox ADD COLUMN claim_token TEXT")
        except sqlite3.OperationalError as exc:
            if "duplicate column" not in str(exc).lower():
                raise

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
                status: str = "pending",
                now: datetime | None = None,
                **_ignored: Any) -> OutboxRow:
        """Idempotent insert via dedup_key UNIQUE. Returns existing or new row.

        ``status`` is the initial status (default ``pending`` → worker claims +
        delivers). A terminal status such as ``logged`` records the row for the
        ledger without ever being claimed (Inbox-First: log everything, push
        only on demand).
        """
        if payload_json is None and payload is not None:
            payload_json = json.dumps(payload, sort_keys=True)
        if payload_json is None:
            raise ValueError("enqueue requires payload or payload_json")
        ts = _iso(now or _utcnow())
        row_id = uuid.uuid4().hex

        with closing(self._conn()) as conn:
            conn.execute(
                "INSERT OR IGNORE INTO outbox (id, channel, payload_json, dedup_key, "
                "attempts, last_error, status, next_retry_at, created_at, updated_at, claim_token) "
                "VALUES (?,?,?,?,0,NULL,?,?,?,?,NULL)",
                (row_id, channel, payload_json, dedup_key, status, ts, ts, ts),
            )
            row = conn.execute(
                "SELECT * FROM outbox WHERE dedup_key=?", (dedup_key,)
            ).fetchone()
            return _row_to_dataclass(row)

    @_retry_on_locked
    def claim_due(self, *, now: datetime, limit: int = 10) -> list[OutboxRow]:
        """Atomically claim FIFO pending rows due now. Returns rows with fresh claim_token."""
        ts = _iso(now)
        token = uuid.uuid4().hex
        with closing(self._conn()) as conn:
            cursor = conn.execute(
                "UPDATE outbox SET status='claimed', updated_at=?, claim_token=? "
                "WHERE id IN ("
                "  SELECT id FROM outbox "
                "  WHERE status='pending' AND next_retry_at<=? "
                "  ORDER BY created_at ASC, id ASC LIMIT ?"
                ") RETURNING *",
                (ts, token, ts, limit),
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
    def get_by_id_via_dedup_key(self, dedup_key: str) -> OutboxRow | None:
        """Return row by dedup_key (idempotency check for callers/CLI)."""
        with closing(self._conn()) as conn:
            row = conn.execute(
                "SELECT * FROM outbox WHERE dedup_key=?", (dedup_key,)
            ).fetchone()
            return _row_to_dataclass(row) if row else None

    @_retry_on_locked
    def recover_zombies(self, *, now: datetime, timeout_seconds: int) -> int:
        """Recover claimed-stuck rows as failed delivery attempts.

        Atomically transitions claimed -> (pending with backoff | dead-lettered)
        for rows where updated_at < now - timeout_seconds. Each zombie counts
        as one failed attempt (attempts++, last_error='zombie recovered',
        dead-letter at threshold). Returns count of rows touched.
        """
        cutoff = _iso(now - timedelta(seconds=timeout_seconds))
        ts = _iso(now)
        recovered = 0
        with closing(self._conn()) as conn:
            zombies = conn.execute(
                "SELECT id, attempts FROM outbox "
                "WHERE status='claimed' AND updated_at < ?",
                (cutoff,),
            ).fetchall()
            for zombie in zombies:
                rid = zombie["id"]
                attempts = (zombie["attempts"] or 0) + 1
                if attempts >= DEAD_LETTER_THRESHOLD:
                    new_status = "dead-lettered"
                    next_retry = None
                else:
                    new_status = "pending"
                    backoff_sec = min(
                        BACKOFF_MAX_SECONDS,
                        BACKOFF_BASE_SECONDS * (2 ** (attempts - 1)),
                    )
                    next_retry = _iso(now + timedelta(seconds=backoff_sec))
                cursor = conn.execute(
                    "UPDATE outbox SET status=?, attempts=?, last_error=?, "
                    "next_retry_at=?, updated_at=?, claim_token=NULL "
                    "WHERE id=? AND status='claimed'",
                    (new_status, attempts, "zombie recovered", next_retry, ts, rid),
                )
                if cursor.rowcount:
                    recovered += 1
        return recovered

    @_retry_on_locked
    def mark_sent(self, *,
                  row_id: str | None = None,
                  id: str | None = None,
                  message_id: str | None = None,
                  claim_token: str | None = None,
                  now: datetime | None = None,
                  **_ignored: Any) -> bool:
        """Mark claimed row as sent. Returns True on success, False if stale claim or missing."""
        rid = row_id or id or message_id
        if not rid:
            raise ValueError("mark_sent requires row_id|id|message_id")
        ts = _iso(now or _utcnow())
        with closing(self._conn()) as conn:
            if claim_token is None:
                cursor = conn.execute(
                    "UPDATE outbox SET status='sent', updated_at=?, claim_token=NULL "
                    "WHERE id=? AND status='claimed'",
                    (ts, rid),
                )
            else:
                cursor = conn.execute(
                    "UPDATE outbox SET status='sent', updated_at=?, claim_token=NULL "
                    "WHERE id=? AND status='claimed' AND claim_token=?",
                    (ts, rid, claim_token),
                )
            if cursor.rowcount == 0:
                logger.warning("outbox.mark_sent stale-or-missing row_id=%s token=%s", rid, claim_token)
                return False
            return True

    @_retry_on_locked
    def record_failure(self, *,
                       row_id: str | None = None,
                       id: str | None = None,
                       message_id: str | None = None,
                       claim_token: str | None = None,
                       error: str | None = None,
                       last_error: str | None = None,
                       now: datetime | None = None,
                       **_ignored: Any) -> bool:
        """Record failed dispatch. Returns True on success, False if stale/missing."""
        rid = row_id or id or message_id
        if not rid:
            raise ValueError("record_failure requires row_id|id|message_id")
        err = error or last_error or "unknown"
        now_dt = now or _utcnow()
        ts = _iso(now_dt)

        with closing(self._conn()) as conn:
            if claim_token is None:
                row = conn.execute(
                    "SELECT attempts FROM outbox WHERE id=? AND status='claimed'",
                    (rid,),
                ).fetchone()
            else:
                row = conn.execute(
                    "SELECT attempts FROM outbox "
                    "WHERE id=? AND status='claimed' AND claim_token=?",
                    (rid, claim_token),
                ).fetchone()
            if row is None:
                logger.warning("outbox.record_failure stale-or-missing row_id=%s token=%s",
                               rid, claim_token)
                return False
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

            if claim_token is None:
                cursor = conn.execute(
                    "UPDATE outbox SET attempts=?, last_error=?, status=?, "
                    "next_retry_at=?, updated_at=?, claim_token=NULL "
                    "WHERE id=? AND status='claimed'",
                    (attempts, err, new_status, next_retry, ts, rid),
                )
            else:
                cursor = conn.execute(
                    "UPDATE outbox SET attempts=?, last_error=?, status=?, "
                    "next_retry_at=?, updated_at=?, claim_token=NULL "
                    "WHERE id=? AND status='claimed' AND claim_token=?",
                    (attempts, err, new_status, next_retry, ts, rid, claim_token),
                )
            return cursor.rowcount > 0


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
    updated_at    TEXT NOT NULL,
    claim_token   TEXT
);
CREATE UNIQUE INDEX IF NOT EXISTS outbox_dedup_key_idx ON outbox(dedup_key);
CREATE INDEX IF NOT EXISTS outbox_due_idx ON outbox(status, next_retry_at);
"""


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
        claim_token=row["claim_token"] if "claim_token" in row.keys() else None,
    )
