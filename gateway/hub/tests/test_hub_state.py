"""Tests for gateway.hub.hub_state."""

import pytest

from gateway.hub.hub_state import connect


@pytest.mark.asyncio
async def test_migration_idempotent(tmp_path, monkeypatch) -> None:
    db_path = tmp_path / "hub_state.db"
    monkeypatch.setenv("HUB_STATE_DB_PATH", str(db_path))

    db = await connect()
    try:
        await db.migrate()
        first_schema = await db.fetchall(
            """
            SELECT type, name, tbl_name, sql
            FROM sqlite_master
            WHERE type IN ('table', 'index')
            ORDER BY type, name
            """
        )
        first_migrations = await db.fetchall(
            "SELECT version, applied_at FROM schema_migrations ORDER BY version"
        )

        await db.migrate()
        second_schema = await db.fetchall(
            """
            SELECT type, name, tbl_name, sql
            FROM sqlite_master
            WHERE type IN ('table', 'index')
            ORDER BY type, name
            """
        )
        second_migrations = await db.fetchall(
            "SELECT version, applied_at FROM schema_migrations ORDER BY version"
        )
    finally:
        await db.close()

    assert [tuple(row) for row in second_schema] == [tuple(row) for row in first_schema]
    assert [row["version"] for row in second_migrations] == [1, 2, 3]
    assert [tuple(row) for row in second_migrations] == [tuple(row) for row in first_migrations]


@pytest.mark.asyncio
async def test_insert_and_fetch(tmp_path) -> None:
    db = await connect(tmp_path / "hub_state.db")
    try:
        await db.migrate()
        await db.execute(
            """
            INSERT INTO hub_events_log (
                event_id, source_slug, topic_slug, status, payload, created_at
            )
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (
                "evt_123",
                "paperclip",
                "builds",
                "received",
                '{"message":"ok"}',
                1_765_000_000,
            ),
        )

        row = await db.fetchone(
            "SELECT * FROM hub_events_log WHERE event_id = ?",
            ("evt_123",),
        )
    finally:
        await db.close()

    assert row is not None
    assert row["event_id"] == "evt_123"
    assert row["source_slug"] == "paperclip"
    assert row["topic_slug"] == "builds"
    assert row["status"] == "received"
    assert row["payload"] == '{"message":"ok"}'
    assert row["created_at"] == 1_765_000_000


@pytest.mark.asyncio
async def test_wal_mode_active(tmp_path) -> None:
    db = await connect(tmp_path / "hub_state.db")
    try:
        await db.migrate()
        row = await db.fetchone("PRAGMA journal_mode")
    finally:
        await db.close()

    assert row[0] == "wal"
