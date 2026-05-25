"""Contract tests for gateway.db_cleanup."""
from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from gateway.db_cleanup import (
    OUTBOX_RETENTION_DEFAULTS,
    TELEGRAM_CALLBACKS_RETENTION_DEFAULTS,
    cleanup_outbox,
    cleanup_telegram_callbacks,
    run_all,
)
from gateway.outbox import OutboxStore
from gateway.telegram_gateway import TelegramCallbackReceiver


NOW = datetime(2026, 5, 25, 12, 0, tzinfo=timezone.utc)


def _populate_outbox(db_path: Path, rows: list[dict]) -> None:
    """Insert outbox rows with explicit updated_at. Schema must already exist."""
    conn = sqlite3.connect(str(db_path))
    for r in rows:
        conn.execute(
            "INSERT INTO outbox (id, channel, payload_json, dedup_key, attempts, "
            "last_error, status, next_retry_at, created_at, updated_at, claim_token) "
            "VALUES (?,?,?,?,0,NULL,?,NULL,?,?,NULL)",
            (r["id"], "telegram", '{"message":"x"}', r["dedup"],
             r["status"], r["updated_at"], r["updated_at"]),
        )
    conn.commit()
    conn.close()


def _make_outbox(tmp_path: Path) -> Path:
    db = tmp_path / "outbox.db"
    OutboxStore(str(db)).init_schema()
    return db


def _make_callbacks_db(tmp_path: Path) -> Path:
    db = tmp_path / "callbacks.db"
    TelegramCallbackReceiver(
        db_path=str(db), webhook_secret="x",
        allowed_user_ids={"1"}, rate_limit_per_minute=60,
    ).init_schema()
    return db


def _populate_callbacks(db: Path, rows: list[dict]) -> None:
    conn = sqlite3.connect(str(db))
    for r in rows:
        conn.execute(
            "INSERT INTO telegram_callbacks "
            "(callback_id, update_id, user_id, callback_type, callback_data, "
            "received_at, accepted, dispatch_status, attempts) "
            "VALUES (?,?,'1','approve','approve:X',?,1,?,0)",
            (r["id"], r["update_id"], r["received_at"], r["status"]),
        )
    conn.commit()
    conn.close()


def _count(db: Path, table: str) -> int:
    conn = sqlite3.connect(str(db))
    try:
        return conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
    finally:
        conn.close()


# ---- outbox ----------------------------------------------------------------


def test_cleanup_outbox_deletes_old_sent_rows(tmp_path: Path):
    db = _make_outbox(tmp_path)
    old = (NOW - timedelta(days=45)).isoformat()
    fresh = (NOW - timedelta(days=10)).isoformat()
    _populate_outbox(db, [
        {"id": "old-sent", "dedup": "d1", "status": "sent", "updated_at": old},
        {"id": "fresh-sent", "dedup": "d2", "status": "sent", "updated_at": fresh},
    ])

    stats = cleanup_outbox(str(db), now=NOW)

    assert stats.deleted == 1
    assert _count(db, "outbox") == 1
    # Only fresh-sent remains
    conn = sqlite3.connect(str(db))
    row = conn.execute("SELECT id FROM outbox").fetchone()
    conn.close()
    assert row[0] == "fresh-sent"


def test_cleanup_outbox_retains_dead_lettered_rows(tmp_path: Path):
    db = _make_outbox(tmp_path)
    very_old = (NOW - timedelta(days=365)).isoformat()
    _populate_outbox(db, [
        {"id": "dl-1", "dedup": "d1", "status": "dead-lettered", "updated_at": very_old},
        {"id": "dl-2", "dedup": "d2", "status": "dead-lettered", "updated_at": very_old},
    ])

    stats = cleanup_outbox(str(db), now=NOW)

    assert stats.deleted == 0
    assert stats.skipped_protected == 2
    assert _count(db, "outbox") == 2


def test_cleanup_outbox_does_not_touch_pending_or_claimed(tmp_path: Path):
    db = _make_outbox(tmp_path)
    very_old = (NOW - timedelta(days=365)).isoformat()
    _populate_outbox(db, [
        {"id": "p-1", "dedup": "d1", "status": "pending", "updated_at": very_old},
        {"id": "c-1", "dedup": "d2", "status": "claimed", "updated_at": very_old},
    ])

    stats = cleanup_outbox(str(db), now=NOW)

    assert stats.deleted == 0
    assert _count(db, "outbox") == 2


def test_cleanup_outbox_dry_run_counts_without_deleting(tmp_path: Path):
    db = _make_outbox(tmp_path)
    old = (NOW - timedelta(days=45)).isoformat()
    _populate_outbox(db, [
        {"id": "old-sent-1", "dedup": "d1", "status": "sent", "updated_at": old},
        {"id": "old-sent-2", "dedup": "d2", "status": "sent", "updated_at": old},
    ])

    stats = cleanup_outbox(str(db), now=NOW, dry_run=True)

    assert stats.deleted == 2
    assert _count(db, "outbox") == 2  # rows still present


def test_cleanup_outbox_custom_retention_override(tmp_path: Path):
    db = _make_outbox(tmp_path)
    moderately_old = (NOW - timedelta(days=10)).isoformat()
    _populate_outbox(db, [
        {"id": "m-1", "dedup": "d1", "status": "sent", "updated_at": moderately_old},
    ])

    stats = cleanup_outbox(str(db), now=NOW, retention={"sent": 7})

    assert stats.deleted == 1


def test_cleanup_outbox_missing_db_returns_zero_stats(tmp_path: Path):
    stats = cleanup_outbox(str(tmp_path / "nonexistent.db"), now=NOW)
    assert stats.deleted == 0


# ---- telegram_callbacks -----------------------------------------------------


def test_cleanup_callbacks_deletes_processed_after_30d(tmp_path: Path):
    db = _make_callbacks_db(tmp_path)
    old = (NOW - timedelta(days=45)).isoformat()
    fresh = (NOW - timedelta(days=10)).isoformat()
    _populate_callbacks(db, [
        {"id": "p-old", "update_id": 1, "received_at": old, "status": "processed"},
        {"id": "p-fresh", "update_id": 2, "received_at": fresh, "status": "processed"},
    ])

    stats = cleanup_telegram_callbacks(str(db), now=NOW)

    assert stats.deleted == 1
    assert _count(db, "telegram_callbacks") == 1


def test_cleanup_callbacks_retains_dead_lettered_rows(tmp_path: Path):
    db = _make_callbacks_db(tmp_path)
    very_old = (NOW - timedelta(days=365)).isoformat()
    _populate_callbacks(db, [
        {"id": "dl-1", "update_id": 1, "received_at": very_old, "status": "dead-lettered"},
    ])

    stats = cleanup_telegram_callbacks(str(db), now=NOW)

    assert stats.deleted == 0
    assert stats.skipped_protected == 1


def test_cleanup_callbacks_failed_retention_longer_than_processed(tmp_path: Path):
    """failed default = 90d > processed 30d. Verify."""
    db = _make_callbacks_db(tmp_path)
    medium = (NOW - timedelta(days=60)).isoformat()
    _populate_callbacks(db, [
        {"id": "p-medium", "update_id": 1, "received_at": medium, "status": "processed"},
        {"id": "f-medium", "update_id": 2, "received_at": medium, "status": "failed"},
    ])

    stats = cleanup_telegram_callbacks(str(db), now=NOW)

    # processed (60d > 30d retention) deleted; failed (60d < 90d retention) kept
    assert stats.deleted == 1
    conn = sqlite3.connect(str(db))
    remaining = conn.execute("SELECT callback_id FROM telegram_callbacks").fetchone()
    conn.close()
    assert remaining[0] == "f-medium"


def test_cleanup_callbacks_ignored_short_retention(tmp_path: Path):
    """ignored default = 7d (callback-spam piles up; safe-fast-drop)."""
    db = _make_callbacks_db(tmp_path)
    week_old = (NOW - timedelta(days=10)).isoformat()
    yesterday = (NOW - timedelta(days=1)).isoformat()
    _populate_callbacks(db, [
        {"id": "i-old", "update_id": 1, "received_at": week_old, "status": "ignored"},
        {"id": "i-new", "update_id": 2, "received_at": yesterday, "status": "ignored"},
    ])

    stats = cleanup_telegram_callbacks(str(db), now=NOW)
    assert stats.deleted == 1


def test_cleanup_callbacks_legacy_schema_without_dispatch_status_no_op(tmp_path: Path):
    """Pre-Round-2 telegram_callbacks (no dispatch_status column) → no-op."""
    db = tmp_path / "legacy.db"
    conn = sqlite3.connect(str(db))
    conn.executescript("""
    CREATE TABLE telegram_callbacks (
        callback_id TEXT PRIMARY KEY, update_id INTEGER NOT NULL,
        user_id TEXT NOT NULL, callback_type TEXT NOT NULL,
        callback_data TEXT NOT NULL, received_at TEXT NOT NULL,
        processed_at TEXT, accepted INTEGER NOT NULL, error TEXT
    );
    INSERT INTO telegram_callbacks VALUES (
        'legacy-1', 1, '1', 'approve', 'approve:X',
        '2024-01-01T00:00:00+00:00', NULL, 1, NULL);
    """)
    conn.commit()
    conn.close()

    stats = cleanup_telegram_callbacks(str(db), now=NOW)
    assert stats.deleted == 0
    assert _count(db, "telegram_callbacks") == 1  # untouched


# ---- run_all ----------------------------------------------------------------


def test_run_all_returns_stats_per_table(tmp_path: Path, monkeypatch):
    outbox_db = _make_outbox(tmp_path)
    cb_db = _make_callbacks_db(tmp_path)
    very_old = (NOW - timedelta(days=200)).isoformat()
    _populate_outbox(outbox_db, [
        {"id": "o-1", "dedup": "d1", "status": "sent", "updated_at": very_old},
    ])
    _populate_callbacks(cb_db, [
        {"id": "c-1", "update_id": 1, "received_at": very_old, "status": "processed"},
    ])

    results = run_all(outbox_db=str(outbox_db), telegram_callbacks_db=str(cb_db))

    assert len(results) == 2
    by_table = {r.table: r for r in results}
    assert by_table["outbox"].deleted == 1
    assert by_table["telegram_callbacks"].deleted == 1


def test_run_all_handles_missing_dbs_gracefully(tmp_path: Path):
    results = run_all(
        outbox_db=str(tmp_path / "no-outbox.db"),
        telegram_callbacks_db=str(tmp_path / "no-cb.db"),
    )
    assert len(results) == 2
    for r in results:
        assert r.deleted == 0


# ---- Round-2 fixes ---------------------------------------------------------


def test_callbacks_retention_uses_processed_at_not_received_at(tmp_path: Path):
    """Round-2 HIGH-1: processed rows aged from processed_at, not received_at.
    Backlog-recovery scenario: received 60d ago, processed yesterday → KEEP."""
    db = _make_callbacks_db(tmp_path)
    # Directly insert with explicit processed_at controlling age
    old_received = (NOW - timedelta(days=60)).isoformat()
    recent_processed = (NOW - timedelta(days=1)).isoformat()
    conn = sqlite3.connect(str(db))
    conn.execute(
        "INSERT INTO telegram_callbacks "
        "(callback_id, update_id, user_id, callback_type, callback_data, "
        "received_at, processed_at, accepted, dispatch_status, attempts) "
        "VALUES ('backlog-1', 1, '1', 'approve', 'approve:X', ?, ?, 1, 'processed', 1)",
        (old_received, recent_processed),
    )
    conn.commit()
    conn.close()

    stats = cleanup_telegram_callbacks(str(db), now=NOW)

    assert stats.deleted == 0
    assert _count(db, "telegram_callbacks") == 1


def test_callbacks_retention_legacy_null_processed_at_falls_back_to_received_at(tmp_path: Path):
    """Round-2 HIGH-1: when processed_at NULL (legacy), fall back to received_at
    so legacy rows still drain after retention."""
    db = _make_callbacks_db(tmp_path)
    old_received = (NOW - timedelta(days=60)).isoformat()
    conn = sqlite3.connect(str(db))
    conn.execute(
        "INSERT INTO telegram_callbacks "
        "(callback_id, update_id, user_id, callback_type, callback_data, "
        "received_at, processed_at, accepted, dispatch_status, attempts) "
        "VALUES ('legacy-null-pa', 1, '1', 'approve', 'approve:X', ?, NULL, 1, 'processed', 1)",
        (old_received,),
    )
    conn.commit()
    conn.close()

    stats = cleanup_telegram_callbacks(str(db), now=NOW)

    assert stats.deleted == 1


def test_cleanup_outbox_max_batches_caps_per_run(tmp_path: Path):
    """Round-2 MEDIUM-2: per-run cap. With max_batches=1 + LIMIT=5000, only 5000 deleted."""
    db = _make_outbox(tmp_path)
    very_old = (NOW - timedelta(days=200)).isoformat()
    # Insert 100 old-sent rows (way under LIMIT but tests max_batches semantic)
    rows = [
        {"id": f"old-{i}", "dedup": f"d-{i}", "status": "sent", "updated_at": very_old}
        for i in range(100)
    ]
    _populate_outbox(db, rows)

    # max_batches_per_status=0 → skip delete-loop entirely
    stats = cleanup_outbox(str(db), now=NOW, max_batches_per_status=0)

    assert stats.deleted == 0
    assert stats.remaining_eligible == 100


def test_cleanup_outbox_dead_letter_threshold_alert(tmp_path: Path):
    """Round-2 MEDIUM-3: dead-letter count >= threshold flags alert."""
    db = _make_outbox(tmp_path)
    long_ago = (NOW - timedelta(days=365)).isoformat()
    # Patch threshold via direct import
    from gateway import db_cleanup as dc_module
    original = dc_module.DEAD_LETTER_ALERT_THRESHOLD
    dc_module.DEAD_LETTER_ALERT_THRESHOLD = 3
    try:
        rows = [
            {"id": f"dl-{i}", "dedup": f"dlk-{i}",
             "status": "dead-lettered", "updated_at": long_ago}
            for i in range(5)
        ]
        _populate_outbox(db, rows)

        stats = cleanup_outbox(str(db), now=NOW)
    finally:
        dc_module.DEAD_LETTER_ALERT_THRESHOLD = original

    assert stats.dead_letter_alert is True
    assert stats.skipped_protected == 5


def test_cleanup_outbox_dead_letter_under_threshold_no_alert(tmp_path: Path):
    db = _make_outbox(tmp_path)
    long_ago = (NOW - timedelta(days=365)).isoformat()
    _populate_outbox(db, [
        {"id": "dl-1", "dedup": "d1", "status": "dead-lettered", "updated_at": long_ago},
    ])

    stats = cleanup_outbox(str(db), now=NOW)

    assert stats.dead_letter_alert is False
    assert stats.skipped_protected == 1


def test_cleanup_creates_cleanup_indexes_idempotent(tmp_path: Path):
    """Round-2 MEDIUM-2: cleanup indexes created on first run, no-op on second."""
    db = _make_outbox(tmp_path)
    cleanup_outbox(str(db), now=NOW)

    conn = sqlite3.connect(str(db))
    indexes = {row[1] for row in conn.execute(
        "SELECT * FROM sqlite_master WHERE type='index'"
    ).fetchall()}
    conn.close()
    assert "outbox_cleanup_idx" in indexes

    # Second run must not raise
    cleanup_outbox(str(db), now=NOW)
