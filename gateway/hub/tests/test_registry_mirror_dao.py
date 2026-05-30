"""Tests for the Phase 6b local registry mirror schema (migration 003).

Step 3 scope: only the mirror schema migration plus validation that it creates
the expected tables, is idempotent, and supports the UPSERT / replace access
patterns that registry_sync (a later step) will rely on. No registry_sync,
read-backend swap, or secret store here.
"""

import pytest

from gateway.hub.hub_state import connect

# Tables that migration 003 must create for the registry mirror.
MIRROR_TABLES = {
    "registry_audiences",
    "registry_sources",
    "registry_topics",
    "registry_rules",
    "registry_channels",
    "registry_channel_sets",
    "registry_channel_set_members",
    "registry_sync_meta",
}


async def _table_names(db) -> set[str]:
    rows = await db.fetchall(
        "SELECT name FROM sqlite_master WHERE type = 'table' ORDER BY name"
    )
    return {row["name"] for row in rows}


@pytest.mark.asyncio
async def test_migration_003_creates_mirror_tables(tmp_path, monkeypatch) -> None:
    db_path = tmp_path / "hub_state.db"
    monkeypatch.setenv("HUB_STATE_DB_PATH", str(db_path))

    db = await connect()
    try:
        await db.migrate()
        tables = await _table_names(db)
    finally:
        await db.close()

    assert MIRROR_TABLES.issubset(tables)


@pytest.mark.asyncio
async def test_migration_003_is_idempotent(tmp_path, monkeypatch) -> None:
    db_path = tmp_path / "hub_state.db"
    monkeypatch.setenv("HUB_STATE_DB_PATH", str(db_path))

    db = await connect()
    try:
        await db.migrate()
        # A second run must not raise (executescript-safe / IF NOT EXISTS).
        await db.migrate()
        tables = await _table_names(db)

        # Each mirror table appears exactly once.
        for table in MIRROR_TABLES:
            count_row = await db.fetchone(
                "SELECT COUNT(*) AS n FROM sqlite_master "
                "WHERE type = 'table' AND name = ?",
                (table,),
            )
            assert count_row["n"] == 1
    finally:
        await db.close()

    assert MIRROR_TABLES.issubset(tables)


@pytest.mark.asyncio
async def test_upsert_source_on_conflict_updates(tmp_path, monkeypatch) -> None:
    db_path = tmp_path / "hub_state.db"
    monkeypatch.setenv("HUB_STATE_DB_PATH", str(db_path))

    upsert_sql = (
        "INSERT INTO registry_sources (id, slug, name, synced_at) "
        "VALUES (?, ?, ?, ?) "
        "ON CONFLICT(id) DO UPDATE SET "
        "slug = excluded.slug, name = excluded.name, synced_at = excluded.synced_at"
    )

    db = await connect()
    try:
        await db.migrate()

        async with db.transaction() as tx:
            await tx.execute(upsert_sql, ("src_1", "source-one", "First name", 100))

        async with db.transaction() as tx:
            await tx.execute(upsert_sql, ("src_1", "source-one", "Second name", 200))

        count_row = await db.fetchone("SELECT COUNT(*) AS n FROM registry_sources")
        name_row = await db.fetchone(
            "SELECT name, synced_at FROM registry_sources WHERE id = ?",
            ("src_1",),
        )
    finally:
        await db.close()

    assert count_row["n"] == 1
    assert name_row["name"] == "Second name"
    assert name_row["synced_at"] == 200


@pytest.mark.asyncio
async def test_channel_set_members_pk_replace(tmp_path, monkeypatch) -> None:
    db_path = tmp_path / "hub_state.db"
    monkeypatch.setenv("HUB_STATE_DB_PATH", str(db_path))

    insert_sql = (
        "INSERT OR REPLACE INTO registry_channel_set_members "
        "(channel_set_id, channel_id, position, required) "
        "VALUES (?, ?, ?, ?)"
    )

    db = await connect()
    try:
        await db.migrate()

        async with db.transaction() as tx:
            await tx.execute(insert_sql, ("cs_1", "chan_1", 0, 0))
        # Same (channel_set_id, channel_id) PK → INSERT OR REPLACE replaces.
        async with db.transaction() as tx:
            await tx.execute(insert_sql, ("cs_1", "chan_1", 5, 1))

        # A naked duplicate INSERT (no OR REPLACE) must hit the PK conflict.
        with pytest.raises(Exception):
            async with db.transaction() as tx:
                await tx.execute(
                    "INSERT INTO registry_channel_set_members "
                    "(channel_set_id, channel_id, position) VALUES (?, ?, ?)",
                    ("cs_1", "chan_1", 9),
                )

        count_row = await db.fetchone(
            "SELECT COUNT(*) AS n FROM registry_channel_set_members "
            "WHERE channel_set_id = ?",
            ("cs_1",),
        )
        member_row = await db.fetchone(
            "SELECT position, required FROM registry_channel_set_members "
            "WHERE channel_set_id = ? AND channel_id = ?",
            ("cs_1", "chan_1"),
        )

        # DELETE-by-set removes all members for the set.
        async with db.transaction() as tx:
            await tx.execute(
                "DELETE FROM registry_channel_set_members WHERE channel_set_id = ?",
                ("cs_1",),
            )
        after_delete = await db.fetchone(
            "SELECT COUNT(*) AS n FROM registry_channel_set_members "
            "WHERE channel_set_id = ?",
            ("cs_1",),
        )
    finally:
        await db.close()

    assert count_row["n"] == 1
    assert member_row["position"] == 5
    assert member_row["required"] == 1
    assert after_delete["n"] == 0
