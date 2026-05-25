"""Contract tests for gateway.notification_worker."""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable
from unittest.mock import MagicMock

import pytest

from gateway.fallback_channels import FallbackNotificationRouter, SendResult
from gateway.notification_worker import (
    NotificationWorker,
    _payload_to_router_args,
)
from gateway.outbox import OutboxStore


# ---- Helpers ----------------------------------------------------------------


def _make_store(tmp_path: Path) -> OutboxStore:
    store = OutboxStore(str(tmp_path / "outbox.sqlite"))
    store.init_schema()
    return store


def _make_router(*, ok: bool, hop: str = "hermes", error: str = "") -> FallbackNotificationRouter:
    router = MagicMock(spec=FallbackNotificationRouter)
    router.send.return_value = SendResult(ok=ok, hop=hop, error=error)
    return router


def _enqueue_telegram(store: OutboxStore, dedup_key: str, *, body: str = "hello",
                     now: datetime | None = None) -> str:
    row = store.enqueue(
        channel="telegram",
        dedup_key=dedup_key,
        payload={"message": body, "title": "Test", "audience": "marco"},
        now=now,
    )
    return row.id


# ---- run_once: empty queue -------------------------------------------------


def test_worker_run_once_empty_queue_returns_zero_stats(tmp_path: Path):
    store = _make_store(tmp_path)
    factory = MagicMock(return_value=_make_router(ok=True))
    worker = NotificationWorker(outbox=store, router_factory=factory)

    stats = worker.run_once()

    assert stats.claimed == 0
    assert stats.sent == 0
    assert stats.zombie_recovered == 0
    factory.assert_not_called()


# ---- dispatch success -------------------------------------------------------


def test_worker_dispatches_pending_row_to_router_and_marks_sent(tmp_path: Path):
    store = _make_store(tmp_path)
    row_id = _enqueue_telegram(store, dedup_key="d-1")
    router = _make_router(ok=True, hop="hermes")
    factory = MagicMock(return_value=router)
    worker = NotificationWorker(outbox=store, router_factory=factory)

    stats = worker.run_once()

    assert stats.claimed == 1
    assert stats.sent == 1
    assert stats.failed == 0
    factory.assert_called_once_with("telegram")
    router.send.assert_called_once()
    call = router.send.call_args.kwargs
    assert call["message"] == "hello"
    assert call["issue"]["id"] == row_id
    assert call["issue"]["dedupe_key"] == "d-1"
    assert call["issue"]["title"] == "Test"

    refreshed = store.get_by_id(row_id)
    assert refreshed.status == "sent"


# ---- dispatch failure cascades to record_failure ---------------------------


def test_worker_records_failure_when_router_returns_not_ok(tmp_path: Path):
    store = _make_store(tmp_path)
    row_id = _enqueue_telegram(store, dedup_key="d-fail-1")
    router = _make_router(ok=False, hop="direct-fallback", error="all hops failed")
    worker = NotificationWorker(outbox=store, router_factory=MagicMock(return_value=router))

    stats = worker.run_once()

    assert stats.claimed == 1
    assert stats.failed == 1
    assert stats.sent == 0

    refreshed = store.get_by_id(row_id)
    assert refreshed.status == "pending"  # back to pending after first failure
    assert refreshed.attempts == 1
    assert "all hops failed" in (refreshed.last_error or "")


# ---- dispatch raises exception ---------------------------------------------


def test_worker_records_failure_when_router_send_raises(tmp_path: Path):
    store = _make_store(tmp_path)
    _enqueue_telegram(store, dedup_key="d-raise")
    router = MagicMock(spec=FallbackNotificationRouter)
    router.send.side_effect = RuntimeError("connection refused")
    worker = NotificationWorker(outbox=store, router_factory=MagicMock(return_value=router))

    stats = worker.run_once()

    assert stats.failed == 1
    assert stats.sent == 0


# ---- router_factory exception -----------------------------------------------


def test_worker_records_failure_when_router_factory_raises(tmp_path: Path):
    store = _make_store(tmp_path)
    _enqueue_telegram(store, dedup_key="d-fac")
    factory = MagicMock(side_effect=ValueError("unknown channel: telegram"))
    worker = NotificationWorker(outbox=store, router_factory=factory)

    stats = worker.run_once()

    assert stats.failed == 1
    assert stats.sent == 0
    factory.assert_called_once_with("telegram")


# ---- dead-letter after 5 failures ------------------------------------------


def test_worker_dead_letters_row_after_fifth_failure(tmp_path: Path):
    store = _make_store(tmp_path)
    enq_time = datetime(2026, 5, 24, 12, 0, tzinfo=timezone.utc)
    row_id = _enqueue_telegram(store, dedup_key="d-dead", now=enq_time)
    router = _make_router(ok=False, hop="direct-fallback", error="boom")
    worker = NotificationWorker(outbox=store, router_factory=MagicMock(return_value=router))

    # Run 5 cycles, each advancing time past prior backoff to allow re-claim
    last_dead = 0
    last_failed = 0
    for i in range(5):
        # Force pending status by resetting next_retry_at past
        with store._conn() as conn:
            conn.execute(
                "UPDATE outbox SET status='pending', next_retry_at=? WHERE id=?",
                (enq_time.isoformat(), row_id),
            )
        stats = worker.run_once(now=enq_time + timedelta(hours=i))
        last_dead = stats.dead_lettered
        last_failed = stats.failed

    refreshed = store.get_by_id(row_id)
    assert refreshed.status == "dead-lettered"
    assert refreshed.attempts >= 5
    # Final cycle should have classified as dead-lettered
    assert last_dead == 1


# ---- zombie recovery -------------------------------------------------------


def test_worker_recovers_zombie_claimed_rows(tmp_path: Path):
    """Round-2: zombie recovery counts as failed attempt with backoff.
    Cycle 1: recover 1 zombie (attempts=1, pending with backoff).
    Cycle 2 (post-backoff): re-claim + dispatch.
    """
    store = _make_store(tmp_path)
    enq_time = datetime(2026, 5, 24, 12, 0, tzinfo=timezone.utc)
    _enqueue_telegram(store, dedup_key="d-zombie", now=enq_time)
    # Simulate prior worker-crash: claim, leave stuck
    store.claim_due(now=enq_time, limit=10)

    # First cycle: 10min later. Zombie-recovery sets attempts=1 + 30s backoff.
    crash_time = enq_time + timedelta(minutes=10)
    router = _make_router(ok=True)
    worker = NotificationWorker(
        outbox=store,
        router_factory=MagicMock(return_value=router),
        zombie_timeout_seconds=300,
    )

    stats1 = worker.run_once(now=crash_time)
    assert stats1.zombie_recovered == 1
    assert stats1.claimed == 0  # not yet eligible (backoff active)

    # Second cycle: past backoff
    past_backoff = crash_time + timedelta(minutes=5)
    stats2 = worker.run_once(now=past_backoff)
    assert stats2.claimed == 1
    assert stats2.sent == 1


# ---- multi-row batch -------------------------------------------------------


def test_worker_dispatches_multiple_rows_in_single_cycle(tmp_path: Path):
    store = _make_store(tmp_path)
    enq_time = datetime(2026, 5, 24, 12, 0, tzinfo=timezone.utc)
    for i in range(5):
        _enqueue_telegram(store, dedup_key=f"d-batch-{i}", now=enq_time)
    router = _make_router(ok=True)
    worker = NotificationWorker(
        outbox=store, router_factory=MagicMock(return_value=router),
        claim_batch_size=25,
    )

    stats = worker.run_once(now=enq_time)

    assert stats.claimed == 5
    assert stats.sent == 5
    assert router.send.call_count == 5


# ---- mixed-channel dispatch -------------------------------------------------


def test_worker_uses_channel_specific_router(tmp_path: Path):
    store = _make_store(tmp_path)
    enq_time = datetime(2026, 5, 24, 12, 0, tzinfo=timezone.utc)
    store.enqueue(channel="telegram", dedup_key="m-tg",
                  payload={"message": "tg msg"}, now=enq_time)
    store.enqueue(channel="pushover", dedup_key="m-pu",
                  payload={"message": "pu msg"}, now=enq_time)

    tg_router = _make_router(ok=True, hop="hermes")
    pu_router = _make_router(ok=True, hop="hermes")

    def factory(channel: str) -> FallbackNotificationRouter:
        return {"telegram": tg_router, "pushover": pu_router}[channel]

    worker = NotificationWorker(outbox=store, router_factory=factory)
    stats = worker.run_once(now=enq_time)

    assert stats.sent == 2
    tg_router.send.assert_called_once()
    pu_router.send.assert_called_once()


# ---- graceful shutdown -----------------------------------------------------


def test_worker_stop_signals_run_forever_to_exit():
    store = MagicMock(spec=OutboxStore)
    store.recover_zombies.return_value = 0
    store.claim_due.return_value = []
    worker = NotificationWorker(
        outbox=store,
        router_factory=lambda _ch: _make_router(ok=True),
        poll_interval=0.05,
    )

    import threading
    thread = threading.Thread(target=worker.run_forever, daemon=True)
    thread.start()

    import time
    time.sleep(0.15)  # allow ≥1 cycle
    worker.stop()
    thread.join(timeout=2.0)

    assert not thread.is_alive()


# ---- payload conversion -----------------------------------------------------


def test_payload_to_router_args_extracts_message_and_issue_fields():
    from gateway.outbox import OutboxRow
    row = OutboxRow(
        id="row-1", channel="telegram",
        payload_json=json.dumps({"message": "hello", "title": "T", "audience": "marco"}),
        dedup_key="d-1", attempts=0, last_error=None, status="claimed",
        next_retry_at=None, created_at="2026-05-24T12:00:00Z",
        updated_at="2026-05-24T12:00:00Z",
    )
    message, issue = _payload_to_router_args(row)
    assert message == "hello"
    assert issue["id"] == "row-1"  # defaults to row.id
    assert issue["dedupe_key"] == "d-1"
    assert issue["title"] == "T"
    assert issue["audience"] == "marco"


def test_payload_to_router_args_handles_legacy_freeform_payload():
    from gateway.outbox import OutboxRow
    row = OutboxRow(
        id="row-2", channel="telegram",
        payload_json="just a string body, not JSON",
        dedup_key="d-2", attempts=0, last_error=None, status="claimed",
        next_retry_at=None, created_at="2026-05-24T12:00:00Z",
        updated_at="2026-05-24T12:00:00Z",
    )
    message, issue = _payload_to_router_args(row)
    assert message == "just a string body, not JSON"
    assert issue["id"] == "row-2"


def test_payload_to_router_args_forwards_target_and_context():
    """Round-2 HIGH-2: target/context from outbox payload must reach issue dict
    so direct-sender uses caller-supplied chat-id, not factory-default."""
    from gateway.outbox import OutboxRow
    row = OutboxRow(
        id="row-3", channel="telegram",
        payload_json=json.dumps({
            "message": "hello",
            "target": "999888777",
            "context": "cert_expiry",
            "title": "T",
        }),
        dedup_key="d-3", attempts=0, last_error=None, status="claimed",
        next_retry_at=None, created_at="2026-05-24T12:00:00Z",
        updated_at="2026-05-24T12:00:00Z",
    )
    message, issue = _payload_to_router_args(row)
    assert issue["target"] == "999888777"
    assert issue["context"] == "cert_expiry"
    assert issue["title"] == "T"


# ---- Round-2 fence tests ----------------------------------------------------


def test_worker_passes_claim_token_to_mark_sent(tmp_path: Path):
    """Round-2: mark_sent receives claim_token so stale workers fenced out."""
    store = _make_store(tmp_path)
    enq_time = datetime(2026, 5, 24, 12, 0, tzinfo=timezone.utc)
    _enqueue_telegram(store, dedup_key="d-fence-1", now=enq_time)
    router = _make_router(ok=True)
    worker = NotificationWorker(outbox=store, router_factory=MagicMock(return_value=router))

    stats = worker.run_once(now=enq_time)
    assert stats.sent == 1
    # If claim_token wiring was broken, mark_sent would silently fail-stale
    # and stats.sent would be 0 / stats.failed=1


def test_worker_stops_dispatching_mid_batch_on_shutdown(tmp_path: Path):
    """Round-2 MEDIUM-4: SIGTERM should abandon remaining batch rows."""
    import threading

    store = _make_store(tmp_path)
    enq_time = datetime(2026, 5, 24, 12, 0, tzinfo=timezone.utc)
    for i in range(5):
        _enqueue_telegram(store, dedup_key=f"d-shut-{i}", now=enq_time)

    shutdown_invoked = {"flag": False}

    def slow_router(_ch: str) -> FallbackNotificationRouter:
        # On first call, signal shutdown then return slow-success
        if not shutdown_invoked["flag"]:
            shutdown_invoked["flag"] = True
            worker.stop()
        r = MagicMock(spec=FallbackNotificationRouter)
        r.send.return_value = SendResult(ok=True, hop="hermes")
        return r

    worker = NotificationWorker(outbox=store, router_factory=slow_router)

    stats = worker.run_once(now=enq_time)
    assert stats.claimed == 5
    assert stats.sent == 1  # First row sent before shutdown noticed
    # Remaining 4 rows untouched (will be zombie-recovered later)
