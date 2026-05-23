"""Telegram callback receiver for Hermes notifications.

Handles incoming webhook POSTs from Telegram Bot API:
- verifies X-Telegram-Bot-Api-Secret-Token
- parses callback_query payload
- persists callback row to telegram_callbacks SQLite table
- enforces per-user rate-limit (in-memory ringbuffer, 60s sliding window)
- returns CallbackResult dataclass (success/duplicate/ignored/rate_limited/secret-mismatch)

Out-of-scope: action dispatch (lives in mission-control / hermes-cron).
"""
from __future__ import annotations

import logging
import sqlite3
from collections import deque
from contextlib import closing
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any

log = logging.getLogger(__name__)

SECRET_HEADER = "X-Telegram-Bot-Api-Secret-Token"
KNOWN_CALLBACK_TYPES = frozenset({"approve", "reject", "skip"})


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
            conn.commit()

    def _conn(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self._db_path, isolation_level=None)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode = WAL")
        return conn

    def handle_webhook(self, *,
                       headers: dict[str, str],
                       payload: dict[str, Any],
                       now: datetime | None = None) -> CallbackResult:
        now = now or _utcnow()

        provided = headers.get(SECRET_HEADER) or headers.get(SECRET_HEADER.lower())
        if provided != self._secret:
            return CallbackResult(
                accepted=False, status_code=401, error="invalid_secret",
            )

        callback = payload.get("callback_query") or {}
        callback_id = str(callback.get("id") or "")
        if not callback_id:
            return CallbackResult(
                accepted=False, status_code=400, error="missing_callback_id",
            )

        user_id = str((callback.get("from") or {}).get("id") or "")
        callback_data = str(callback.get("data") or "")
        update_id = int(payload.get("update_id") or 0)
        message = callback.get("message") or {}
        chat = message.get("chat") or {}
        chat_id = str(chat.get("id") or "")
        message_id = int(message.get("message_id") or 0)

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
            try:
                conn.execute(
                    "INSERT INTO telegram_callbacks "
                    "(callback_id, update_id, user_id, chat_id, message_id, "
                    "callback_type, callback_data, issue_id, received_at, "
                    "processed_at, accepted, error) "
                    "VALUES (?,?,?,?,?,?,?,?,?,NULL,?,?)",
                    (callback_id, update_id, user_id, chat_id, message_id,
                     callback_type, callback_data, issue_id, ts,
                     accepted_flag, error_msg),
                )
            except sqlite3.IntegrityError:
                # Race: concurrent webhook for same callback_id won the INSERT
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


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _iso(dt: datetime) -> str:
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.isoformat()
