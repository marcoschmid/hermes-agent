"""Telegram callback receiver for Hermes notifications.

Handles incoming webhook POSTs from Telegram Bot API:
- verifies X-Telegram-Bot-Api-Secret-Token (constant-time compare)
- parses callback_query payload with defensive type-guards (graceful 400 on malformed)
- persists callback row to telegram_callbacks SQLite table
- enforces per-user rate-limit (in-memory ringbuffer, 60s sliding window)
- returns CallbackResult dataclass

Out-of-scope: action dispatch (lives in mission-control / hermes-cron).

Deployment note: rate-limit state is process-local. For multi-worker deploys
behind a load-balancer, run as single worker OR migrate rate-buckets to
SQLite/Redis. Current Hermes deploy is single-process so this is acceptable.
"""
from __future__ import annotations

import hmac
import logging
import sqlite3
from collections import deque
from contextlib import closing
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any

from ._sqlite_helpers import iso as _strict_iso, retry_on_locked, utcnow as _utcnow


def _iso(dt: datetime) -> str:
    """Lenient ISO formatter — preserves pre-refactor behavior (naive=UTC silently)."""
    return _strict_iso(dt, strict_aware=False)

log = logging.getLogger(__name__)

SECRET_HEADER = "X-Telegram-Bot-Api-Secret-Token"
KNOWN_CALLBACK_TYPES = frozenset({"approve", "reject", "skip"})
BUSY_TIMEOUT_MS = 30000
CONNECT_TIMEOUT_SECONDS = 30
# Re-export for backward-compat
_retry_on_locked = retry_on_locked


@dataclass(slots=True)
class CallbackResult:
    accepted: bool
    duplicate: bool = False
    ignored: bool = False
    rate_limited: bool = False
    status_code: int = 200
    callback_type: str = ""
    issue_id: str = ""
    error: str = ""


class TelegramCallbackReceiver:
    def __init__(self, *,
                 db_path: str,
                 webhook_secret: str,
                 allowed_user_ids: set[str],
                 rate_limit_per_minute: int = 60) -> None:
        self._db_path = str(db_path)
        self._secret = webhook_secret
        self._allowed = {str(u) for u in allowed_user_ids}
        self._rate_limit = rate_limit_per_minute
        self._rate_buckets: dict[str, deque[datetime]] = {}

    def init_schema(self) -> None:
        with closing(self._conn()) as conn:
            conn.executescript(_SCHEMA_SQL)
            self._ensure_dispatch_columns(conn)
            conn.commit()

    @staticmethod
    def _ensure_dispatch_columns(conn: sqlite3.Connection) -> None:
        """Idempotent migration: add dispatcher-state columns to legacy tables.

        Round-2 (Track B HIGH-5 fence + HIGH-2 retry): claim_token enables
        ownership-fencing for action-dispatcher; attempts/last_error/
        dispatch_status/next_retry_at enable transient-failure retry.
        Concurrent-init tolerated via duplicate-column catch.
        """
        cols = {row[1] for row in conn.execute("PRAGMA table_info(telegram_callbacks)").fetchall()}
        for col_name, ddl in [
            ("claim_token", "TEXT"),
            ("attempts", "INTEGER NOT NULL DEFAULT 0"),
            ("last_error", "TEXT"),
            ("dispatch_status", "TEXT NOT NULL DEFAULT 'pending'"),
            ("next_retry_at", "TEXT"),
            # Round-3 HIGH-1: zombie-recovery anchor for claimed rows
            ("claimed_at", "TEXT"),
        ]:
            if col_name in cols:
                continue
            try:
                conn.execute(f"ALTER TABLE telegram_callbacks ADD COLUMN {col_name} {ddl}")
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
        conn.execute("PRAGMA journal_mode = WAL")
        conn.execute(f"PRAGMA busy_timeout = {BUSY_TIMEOUT_MS}")
        return conn

    @_retry_on_locked
    def handle_webhook(self, *,
                       headers: dict[str, str],
                       payload: dict[str, Any],
                       now: datetime | None = None) -> CallbackResult:
        now = now or _utcnow()

        provided = headers.get(SECRET_HEADER) or headers.get(SECRET_HEADER.lower())
        if not hmac.compare_digest(provided or "", self._secret):
            return CallbackResult(
                accepted=False, status_code=401, error="invalid_secret",
            )

        if not isinstance(payload, dict):
            return CallbackResult(
                accepted=False, status_code=400, error="payload_not_object",
            )

        callback = _as_dict(payload.get("callback_query"))
        callback_id = str(callback.get("id") or "")
        if not callback_id:
            return CallbackResult(
                accepted=False, status_code=400, error="missing_callback_id",
            )

        from_obj = _as_dict(callback.get("from"))
        user_id = str(from_obj.get("id") or "")
        callback_data = str(callback.get("data") or "")
        update_id = _as_int(payload.get("update_id"))
        if update_id is None:
            return CallbackResult(
                accepted=False, status_code=400, error="invalid_update_id",
            )
        message = _as_dict(callback.get("message"))
        chat = _as_dict(message.get("chat"))
        chat_id = str(chat.get("id") or "")
        message_id = _as_int(message.get("message_id")) or 0

        if user_id not in self._allowed:
            return CallbackResult(
                accepted=False, status_code=403, error="user_not_allowed",
            )

        if self._is_rate_limited(user_id, now):
            return CallbackResult(
                accepted=False, status_code=429, rate_limited=True,
            )

        with closing(self._conn()) as conn:
            existing = conn.execute(
                "SELECT callback_type, accepted FROM telegram_callbacks "
                "WHERE callback_id=?",
                (callback_id,),
            ).fetchone()
            if existing:
                return CallbackResult(
                    accepted=bool(existing["accepted"]),
                    duplicate=True,
                    callback_type=existing["callback_type"],
                )

            parts = callback_data.split(":", 1)
            raw_type = parts[0] if parts else ""
            issue_id = parts[1] if len(parts) > 1 else ""
            is_known = raw_type in KNOWN_CALLBACK_TYPES
            callback_type = raw_type if is_known else "unknown"

            if not is_known:
                log.warning(
                    "Unknown callback type received: %r (callback_id=%s, user=%s)",
                    raw_type, callback_id, user_id,
                )

            ts = _iso(now)
            accepted_flag = 1 if is_known else 0
            error_msg = None if is_known else "unknown_callback_type"
            # Round-2: rows with accepted=1 become dispatcher-pending; unknown
            # (accepted=0) get dispatch_status='ignored' so dispatcher skips them.
            initial_dispatch = "pending" if is_known else "ignored"
            try:
                conn.execute(
                    "INSERT INTO telegram_callbacks "
                    "(callback_id, update_id, user_id, chat_id, message_id, "
                    "callback_type, callback_data, issue_id, received_at, "
                    "processed_at, accepted, error, "
                    "claim_token, attempts, last_error, dispatch_status, next_retry_at) "
                    "VALUES (?,?,?,?,?,?,?,?,?,NULL,?,?,NULL,0,NULL,?,?)",
                    (callback_id, update_id, user_id, chat_id, message_id,
                     callback_type, callback_data, issue_id, ts,
                     accepted_flag, error_msg,
                     initial_dispatch, ts),
                )
            except sqlite3.IntegrityError:
                row = conn.execute(
                    "SELECT callback_type, accepted FROM telegram_callbacks "
                    "WHERE callback_id=?",
                    (callback_id,),
                ).fetchone()
                if row is not None:
                    return CallbackResult(
                        accepted=bool(row["accepted"]),
                        duplicate=True,
                        callback_type=row["callback_type"],
                    )
                raise

            self._record_rate(user_id, now)

            if not is_known:
                return CallbackResult(
                    accepted=False, ignored=True, callback_type=callback_type,
                )

            return CallbackResult(
                accepted=True, callback_type=callback_type, issue_id=issue_id,
            )

    def _is_rate_limited(self, user_id: str, now: datetime) -> bool:
        bucket = self._rate_buckets.setdefault(user_id, deque())
        cutoff = now - timedelta(minutes=1)
        while bucket and bucket[0] < cutoff:
            bucket.popleft()
        return len(bucket) >= self._rate_limit

    def _record_rate(self, user_id: str, now: datetime) -> None:
        bucket = self._rate_buckets.setdefault(user_id, deque())
        bucket.append(now)


_SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS telegram_callbacks (
    callback_id   TEXT PRIMARY KEY,
    update_id     INTEGER NOT NULL,
    user_id       TEXT NOT NULL,
    chat_id       TEXT,
    message_id    INTEGER,
    callback_type TEXT NOT NULL,
    callback_data TEXT NOT NULL,
    issue_id      TEXT,
    received_at   TEXT NOT NULL,
    processed_at  TEXT,
    accepted      INTEGER NOT NULL,
    error         TEXT
);
CREATE INDEX IF NOT EXISTS telegram_callbacks_user_received_idx
    ON telegram_callbacks(user_id, received_at);
"""


def _as_dict(value: Any) -> dict[str, Any]:
    """Coerce nested payload field to dict — defensive against malformed input."""
    return value if isinstance(value, dict) else {}


def _as_int(value: Any) -> int | None:
    """Coerce numeric-looking field to int. Returns None on non-numeric input."""
    if value is None:
        return None
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    try:
        return int(value)
    except (TypeError, ValueError):
        return None
