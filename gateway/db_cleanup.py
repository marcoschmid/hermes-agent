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
- Per-run cap (max_batches_per_status) bounds catch-up after long outage;
  remaining rows picked up next cycle.

Dead-letter drain procedure (manual, when DEAD_LETTER_ALERT_THRESHOLD
exceeded — logged WARN by cleanup):
  1. Inspect: sqlite3 ~/.hermes/outbox.db "SELECT * FROM outbox WHERE status='dead-lettered'"
  2. Decide per row: investigate root cause + manually retry OR archive.
  3. Drain after review:
       sqlite3 ~/.hermes/outbox.db "DELETE FROM outbox \\
         WHERE status='dead-lettered' AND id IN ('<id1>', '<id2>', ...)"
  Same procedure for telegram_callbacks (column dispatch_status).

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

# Round-2 HIGH-1: status-specific timestamp anchors. Terminal-state statuses
# use processed_at (when delivery resolved) instead of received_at (when
# callback arrived). A long backlog from dispatcher-outage would otherwise
# instantly purge freshly-resolved rows.
TELEGRAM_CALLBACKS_TIMESTAMP_PER_STATUS: dict[str, str] = {
    "processed": "processed_at",
    "failed": "processed_at",
    "dead-lettered": "processed_at",
    "ignored": "received_at",  # ignored rows never get processed_at
}

DELETE_BATCH_LIMIT = 5000
# Round-2 MEDIUM-2: bound per-status catch-up work so a long-outage backlog
# doesn't monopolize SQLite writers in one 04:00 cron. Remaining work picked
# up next cycle.
MAX_BATCHES_PER_STATUS = 10
# Round-2 MEDIUM-3: alert when dead-letter retention exceeds threshold so
# Marco notices accumulation before it grows unbounded.
DEAD_LETTER_ALERT_THRESHOLD = 100


@dataclass(slots=True)
class CleanupStats:
    table: str
    deleted: int = 0
    skipped_protected: int = 0
    remaining_eligible: int = 0
    dead_letter_alert: bool = False


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
    max_batches_per_status: int = MAX_BATCHES_PER_STATUS,
) -> CleanupStats:
    """Delete sent rows older than retention[status] days. Retain protected statuses.

    Args:
        db_path: SQLite outbox DB.
        retention: status -> days mapping. None value = retain-forever.
        now: timestamp anchor for cutoff calculation.
        dry_run: count rows that would delete, do not delete.
        timestamp_column: which column to age-against (default updated_at).
        max_batches_per_status: cap catch-up work per run (Round-2 MEDIUM-2).
    """
    retention = retention or OUTBOX_RETENTION_DEFAULTS
    now = now or _utcnow()
    stats = CleanupStats(table="outbox")

    if not Path(db_path).exists():
        log.warning("cleanup_outbox: db missing at %s", db_path)
        return stats

    with closing(_connect(db_path)) as conn:
        _ensure_cleanup_index(conn, "outbox", "outbox_cleanup_idx",
                              f"status, {timestamp_column}")

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
            # Batched delete with per-run cap
            for _batch in range(max_batches_per_status):
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
            else:
                # Hit max_batches without draining — count remaining eligible
                rem = conn.execute(
                    f"SELECT COUNT(*) AS n FROM outbox "
                    f"WHERE status=? AND {timestamp_column}<?",
                    (status, cutoff),
                ).fetchone()
                stats.remaining_eligible += int(rem["n"] or 0)

        # Audit: count protected rows + dead-letter-alert threshold
        for status, days in retention.items():
            if days is not None:
                continue
            row = conn.execute(
                "SELECT COUNT(*) AS n FROM outbox WHERE status=?",
                (status,),
            ).fetchone()
            count = int(row["n"] or 0)
            stats.skipped_protected += count
            if status == "dead-lettered" and count >= DEAD_LETTER_ALERT_THRESHOLD:
                stats.dead_letter_alert = True
                log.warning(
                    "outbox dead-lettered rows >= threshold (%d >= %d). "
                    "Manual drain required. See module docstring for procedure.",
                    count, DEAD_LETTER_ALERT_THRESHOLD,
                )

    return stats


def cleanup_telegram_callbacks(
    db_path: str,
    *,
    retention: dict[str, int | None] | None = None,
    now: datetime | None = None,
    dry_run: bool = False,
    timestamp_per_status: dict[str, str] | None = None,
    max_batches_per_status: int = MAX_BATCHES_PER_STATUS,
) -> CleanupStats:
    """Delete telegram_callbacks rows by dispatch_status retention.

    Round-2 HIGH-1: timestamp_per_status maps each status to the right
    age-anchor. Terminal-state statuses (processed/failed/dead-lettered) use
    processed_at so post-outage-resolved rows aren't insta-purged when
    received_at is already past retention. ignored rows never get
    processed_at, so received_at is used. COALESCE with received_at falls
    back gracefully for legacy rows where processed_at is NULL.
    """
    retention = retention or TELEGRAM_CALLBACKS_RETENTION_DEFAULTS
    timestamp_per_status = timestamp_per_status or TELEGRAM_CALLBACKS_TIMESTAMP_PER_STATUS
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

        _ensure_cleanup_index(conn, "telegram_callbacks",
                              "telegram_callbacks_cleanup_idx",
                              "dispatch_status, processed_at")

        for status, days in retention.items():
            if days is None:
                continue
            cutoff = _iso(now - timedelta(days=days))
            ts_col = timestamp_per_status.get(status, "received_at")
            # COALESCE(processed_at, received_at) lets legacy rows with NULL
            # processed_at fall back gracefully without leaking.
            age_expr = f"COALESCE({ts_col}, received_at)" if ts_col == "processed_at" else ts_col
            if dry_run:
                count_row = conn.execute(
                    f"SELECT COUNT(*) AS n FROM telegram_callbacks "
                    f"WHERE dispatch_status=? AND {age_expr}<?",
                    (status, cutoff),
                ).fetchone()
                stats.deleted += int(count_row["n"] or 0)
                continue
            for _batch in range(max_batches_per_status):
                cursor = conn.execute(
                    f"DELETE FROM telegram_callbacks "
                    f"WHERE rowid IN ("
                    f"  SELECT rowid FROM telegram_callbacks "
                    f"  WHERE dispatch_status=? AND {age_expr}<? LIMIT ?"
                    f")",
                    (status, cutoff, DELETE_BATCH_LIMIT),
                )
                rc = cursor.rowcount
                stats.deleted += rc
                if rc < DELETE_BATCH_LIMIT:
                    break
            else:
                rem = conn.execute(
                    f"SELECT COUNT(*) AS n FROM telegram_callbacks "
                    f"WHERE dispatch_status=? AND {age_expr}<?",
                    (status, cutoff),
                ).fetchone()
                stats.remaining_eligible += int(rem["n"] or 0)

        for status, days in retention.items():
            if days is not None:
                continue
            row = conn.execute(
                "SELECT COUNT(*) AS n FROM telegram_callbacks WHERE dispatch_status=?",
                (status,),
            ).fetchone()
            count = int(row["n"] or 0)
            stats.skipped_protected += count
            if status == "dead-lettered" and count >= DEAD_LETTER_ALERT_THRESHOLD:
                stats.dead_letter_alert = True
                log.warning(
                    "telegram_callbacks dead-lettered rows >= threshold "
                    "(%d >= %d). Manual drain required.",
                    count, DEAD_LETTER_ALERT_THRESHOLD,
                )

    return stats


def _ensure_cleanup_index(conn: sqlite3.Connection, table: str,
                          index_name: str, columns: str) -> None:
    """Round-2 MEDIUM-2: cleanup-specific indexes to speed batched deletes."""
    try:
        conn.execute(f"CREATE INDEX IF NOT EXISTS {index_name} ON {table}({columns})")
    except sqlite3.OperationalError as exc:
        # If column doesn't exist yet (mid-migration), skip silently
        if "no such column" in str(exc).lower():
            log.warning("cleanup index %s skipped: %s", index_name, exc)
            return
        raise


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
