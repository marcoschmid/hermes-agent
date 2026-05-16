"""Replay-protection nonce store backed by hub_state.hub_nonces table.

remember_or_replay(state, nonce, source_slug) → bool
  True if first-seen (insert succeeded), False if already-stored (replay).

prune_old(state, max_age_seconds) → int
  Deletes rows older than max_age_seconds. Returns count.

Caller schedule: prune_old on startup + periodically (e.g. hourly).
"""
from __future__ import annotations

import time

from gateway.hub.hub_state import HubState


async def remember_or_replay(state: HubState, nonce: str, source_slug: str) -> bool:
    """Insert nonce. Returns True if first-seen, False on replay (PK conflict)."""
    try:
        await state.execute(
            "INSERT INTO hub_nonces (nonce, source_slug, created_at) VALUES (?, ?, ?)",
            (nonce, source_slug, int(time.time())),
        )
        return True
    except Exception as exc:
        # sqlite3.IntegrityError or wrapper — PK constraint = replay
        if "UNIQUE" in str(exc) or "PRIMARY" in str(exc).upper() or "constraint" in str(exc).lower():
            return False
        raise


async def prune_old(state: HubState, max_age_seconds: int = 3600) -> int:
    """Delete nonces older than max_age_seconds. Returns delete count."""
    cutoff = int(time.time()) - max_age_seconds
    # SQLite returns no rowcount via fetchone — we count first.
    rows = await state.fetchall(
        "SELECT COUNT(*) AS n FROM hub_nonces WHERE created_at < ?", (cutoff,)
    )
    count = rows[0]["n"] if rows else 0
    if count:
        await state.execute("DELETE FROM hub_nonces WHERE created_at < ?", (cutoff,))
    return int(count)
