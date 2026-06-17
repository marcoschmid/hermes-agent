"""TDD: OutboxStore.enqueue accepts a terminal initial status.

Drives the `notify` ledger-first design: severity < warn must be recorded
as a terminal `logged` row (Inbox-First) that the worker never claims/sends.
"""
from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

from gateway.outbox import OutboxStore


def _store(tmp_path: Path) -> OutboxStore:
    store = OutboxStore(str(tmp_path / "outbox.db"))
    store.init_schema()
    return store


def test_enqueue_defaults_to_pending(tmp_path: Path) -> None:
    store = _store(tmp_path)
    row = store.enqueue(channel="pushover", dedup_key="d1", payload={"message": "x"})
    assert row.status == "pending"


def test_enqueue_accepts_logged_status(tmp_path: Path) -> None:
    store = _store(tmp_path)
    row = store.enqueue(
        channel="pushover",
        dedup_key="d2",
        payload={"message": "x"},
        status="logged",
    )
    assert row.status == "logged"


def test_logged_row_is_never_claimed(tmp_path: Path) -> None:
    store = _store(tmp_path)
    now = datetime(2026, 6, 17, tzinfo=timezone.utc)
    store.enqueue(
        channel="pushover", dedup_key="d-logged",
        payload={"message": "info only"}, status="logged", now=now,
    )
    store.enqueue(
        channel="pushover", dedup_key="d-pending",
        payload={"message": "warn"}, status="pending", now=now,
    )
    claimed = store.claim_due(now=now, limit=10)
    keys = {r.dedup_key for r in claimed}
    assert "d-pending" in keys
    assert "d-logged" not in keys
