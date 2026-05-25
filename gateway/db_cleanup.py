"""Retention-driven cleanup for Phase-4 SQLite stores.

Deletes terminal-state rows older than configured retention windows from
``gateway.outbox.OutboxStore`` and ``gateway.telegram_gateway`` tables.

Retention defaults (override per-call):
- outbox.status='sent' → delete after 30 days
- outbox.status='dead-lettered' → RETAIN (never auto-delete; Marco-review)
- telegram_callbacks.dispatch_status='processed' → delete after 30 days
- telegram_callbacks.dispatch_status='failed' → delete after 90 days
- telegram_callbacks.dispatch_status='dead-lettered' → RETAIN
- telegram_callbacks.dispatch_status='ignored' → delete after 7 days (unknown
  callback_type rows pile up; safe to drop fast)

Safety:
- Each delete bounded by LIMIT to prevent multi-million-row stalls.
- Idempotent: running multiple times = same end-state.
- read-only-dry-run mode for verification.
- VACUUM is OPT-IN (SQLite-locks DB; out-of-scope for cron).

Designed for LaunchAgent ``de.marcoschmid.hermes-db-cleanup`` daily 04:00.
"""
from __future__ import annotations

import logging
import sqlite3
from contextlib import closing
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)

# Default retention (days) per status. None = retain-forever.
OUTBOX_RETENTION_DEFAULTS: dict[str, int | None] = {
    "sent": 30,
    "dead-lettered": None,
}

TELEGRAM_CALLBACKS_RETENTION_DEFAULTS: dict[str, int | None] = {
    "processed": 30,
    "failed": 90,
    "dead-lettered": None,
    "ignored": 7,
}

DELETE_BATCH_LIMIT = 5000


@dataclass(slots=True)
class CleanupStats:
    table: str
    deleted: int = 0
    skipped_protected: int = 0


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _iso(dt: datetime) -> str:
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc).isoformat()


def _connect(db_path: str) -> sqlite3.Connection:
    conn = sqlite3.connect(db_path, isolation_level=None, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode = WAL")
    conn.execute("PRAGMA busy_timeout = 30000")
    return conn


def cleanup_outbox(
    db_path: str,
    *,
    retention: dict[str, int | None] | None = None,
    now: datetime | None = None,
    dry_run: bool = False,
    timestamp_column: str = "updated_at",
) -> CleanupStats:
    """Delete sent rows older than retention[status] days. Retain protected statuses.

    Args:
        db_path: SQLite outbox DB.
        retention: status -> days mapping. None value = retain-forever.
        now: timestamp anchor for cutoff calculation.
        dry_run: count rows that would delete, do not delete.
        timestamp_column: which column to age-against (default updated_at).
    """
    retention = retention or OUTBOX_RETENTION_DEFAULTS
    now = now or _utcnow()
    stats = CleanupStats(table="outbox")

    if not Path(db_path).exists():
        log.warning("cleanup_outbox: db missing at %s", db_path)
        return stats

    with closing(_connect(db_path)) as conn:
        for status, days in retention.items():
            if days is None:
                continue
            cutoff = _iso(now - timedelta(days=days))
            if dry_run:
                count_row = conn.execute(
                    f"SELECT COUNT(*) AS n FROM outbox "
                    f"WHERE status=? AND {timestamp_column}<?",
                    (status, cutoff),
                ).fetchone()
                stats.deleted += int(count_row["n"] or 0)
                continue
            # Batched delete to avoid million-row stalls
            while True:
                cursor = conn.execute(
                    f"DELETE FROM outbox "
                    f"WHERE rowid IN ("
                    f"  SELECT rowid FROM outbox "
                    f"  WHERE status=? AND {timestamp_column}<? LIMIT ?"
                    f")",
                    (status, cutoff, DELETE_BATCH_LIMIT),
                )
                rc = cursor.rowcount
                stats.deleted += rc
                if rc < DELETE_BATCH_LIMIT:
                    break

        # Audit: count protected rows (informational)
        for status, days in retention.items():
            if days is not None:
                continue
            row = conn.execute(
                "SELECT COUNT(*) AS n FROM outbox WHERE status=?",
                (status,),
            ).fetchone()
            stats.skipped_protected += int(row["n"] or 0)

    return stats


def cleanup_telegram_callbacks(
    db_path: str,
    *,
    retention: dict[str, int | None] | None = None,
    now: datetime | None = None,
    dry_run: bool = False,
    timestamp_column: str = "received_at",
) -> CleanupStats:
    """Delete telegram_callbacks rows by dispatch_status retention."""
    retention = retention or TELEGRAM_CALLBACKS_RETENTION_DEFAULTS
    now = now or _utcnow()
    stats = CleanupStats(table="telegram_callbacks")

    if not Path(db_path).exists():
        log.warning("cleanup_telegram_callbacks: db missing at %s", db_path)
        return stats

    with closing(_connect(db_path)) as conn:
        # Verify dispatch_status column exists (legacy DBs may lack it)
        cols = {row[1] for row in conn.execute(
            "PRAGMA table_info(telegram_callbacks)"
        ).fetchall()}
        if "dispatch_status" not in cols:
            log.warning("cleanup_telegram_callbacks: legacy schema without "
                        "dispatch_status; skipping")
            return stats

        for status, days in retention.items():
            if days is None:
                continue
            cutoff = _iso(now - timedelta(days=days))
            if dry_run:
                count_row = conn.execute(
                    f"SELECT COUNT(*) AS n FROM telegram_callbacks "
                    f"WHERE dispatch_status=? AND {timestamp_column}<?",
                    (status, cutoff),
                ).fetchone()
                stats.deleted += int(count_row["n"] or 0)
                continue
            while True:
                cursor = conn.execute(
                    f"DELETE FROM telegram_callbacks "
                    f"WHERE rowid IN ("
                    f"  SELECT rowid FROM telegram_callbacks "
                    f"  WHERE dispatch_status=? AND {timestamp_column}<? LIMIT ?"
                    f")",
                    (status, cutoff, DELETE_BATCH_LIMIT),
                )
                rc = cursor.rowcount
                stats.deleted += rc
                if rc < DELETE_BATCH_LIMIT:
                    break

        for status, days in retention.items():
            if days is not None:
                continue
            row = conn.execute(
                "SELECT COUNT(*) AS n FROM telegram_callbacks WHERE dispatch_status=?",
                (status,),
            ).fetchone()
            stats.skipped_protected += int(row["n"] or 0)

    return stats


def run_all(
    *,
    outbox_db: str | None = None,
    telegram_callbacks_db: str | None = None,
    dry_run: bool = False,
) -> list[CleanupStats]:
    """Run all cleanup-jobs with default paths from env or convention."""
    home = Path.home()
    outbox_db = outbox_db or str(home / ".hermes" / "outbox.db")
    telegram_callbacks_db = telegram_callbacks_db or str(
        home / ".hermes" / "telegram_callbacks.db"
    )

    results = []
    results.append(cleanup_outbox(outbox_db, dry_run=dry_run))
    results.append(cleanup_telegram_callbacks(telegram_callbacks_db, dry_run=dry_run))
    return results
