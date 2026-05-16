"""Tests for nonce_store — insert-then-check + prune."""
from __future__ import annotations

import time

import pytest

from gateway.hub.hub_state import connect
from gateway.hub.nonce_store import prune_old, remember_or_replay


@pytest.mark.asyncio
async def test_first_insert_returns_true(tmp_path) -> None:
    db = await connect(tmp_path / "hub_state.db")
    try:
        await db.migrate()
        assert await remember_or_replay(db, "nonce-1", "source-a") is True
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_replay_returns_false(tmp_path) -> None:
    db = await connect(tmp_path / "hub_state.db")
    try:
        await db.migrate()
        await remember_or_replay(db, "nonce-2", "source-a")
        assert await remember_or_replay(db, "nonce-2", "source-a") is False
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_different_source_same_nonce_is_replay(tmp_path) -> None:
    db = await connect(tmp_path / "hub_state.db")
    try:
        await db.migrate()
        await remember_or_replay(db, "nonce-3", "source-a")
        assert await remember_or_replay(db, "nonce-3", "source-b") is False
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_prune_old_removes_stale(tmp_path) -> None:
    db = await connect(tmp_path / "hub_state.db")
    try:
        await db.migrate()
        await remember_or_replay(db, "nonce-4", "source-a")
        await db.execute(
            "UPDATE hub_nonces SET created_at = ? WHERE nonce = ?",
            (int(time.time()) - 3600, "nonce-4"),
        )
        deleted = await prune_old(db, max_age_seconds=60)
        assert deleted == 1
        assert await remember_or_replay(db, "nonce-4", "source-a") is True
    finally:
        await db.close()
