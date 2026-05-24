"""OutboxStore consumer + dispatch worker.

Polls ``gateway.outbox.OutboxStore.claim_due()`` for pending rows due for
delivery, constructs a channel-specific ``FallbackNotificationRouter`` per row,
dispatches via Router-cascade, and persists ``mark_sent`` / ``record_failure``.

Crash-safe: rows stuck in ``claimed`` status > ``zombie_timeout_seconds``
get re-claimed via ``recover_zombies()``.

Designed for LaunchAgent ``de.marcoschmid.hermes-notification-worker``.

See ``projects/jarvis-os-redesign/plans/2026-05-24-p4-track-c-notification-worker.md``.
"""
from __future__ import annotations

import json
import logging
import signal
import threading
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Callable

from .fallback_channels import FallbackNotificationRouter, SendResult
from .outbox import OutboxRow, OutboxStore

log = logging.getLogger(__name__)

ZOMBIE_CLAIM_TIMEOUT_SECONDS = 300
POLL_INTERVAL_SECONDS = 10
CLAIM_BATCH_SIZE = 25


@dataclass(slots=True)
class CycleStats:
    claimed: int = 0
    sent: int = 0
    failed: int = 0
    dead_lettered: int = 0
    zombie_recovered: int = 0


class NotificationWorker:
    """Polls OutboxStore + dispatches via channel-specific Router-factory."""

    def __init__(
        self,
        *,
        outbox: OutboxStore,
        router_factory: Callable[[str], FallbackNotificationRouter],
        poll_interval: float = POLL_INTERVAL_SECONDS,
        claim_batch_size: int = CLAIM_BATCH_SIZE,
        zombie_timeout_seconds: int = ZOMBIE_CLAIM_TIMEOUT_SECONDS,
    ) -> None:
        self._outbox = outbox
        self._router_factory = router_factory
        self._poll_interval = poll_interval
        self._batch_size = claim_batch_size
        self._zombie_timeout = zombie_timeout_seconds
        self._shutdown_event = threading.Event()

    def run_once(self, *, now: datetime | None = None) -> CycleStats:
        """Process one batch. Returns stats. Caller controls loop-cadence."""
        now = now or _utcnow()
        stats = CycleStats()

        stats.zombie_recovered = self._outbox.recover_zombies(
            now=now, timeout_seconds=self._zombie_timeout,
        )

        rows = self._outbox.claim_due(now=now, limit=self._batch_size)
        stats.claimed = len(rows)

        for row in rows:
            self._dispatch_row(row, now=now, stats=stats)

        return stats

    def _dispatch_row(self, row: OutboxRow, *, now: datetime, stats: CycleStats) -> None:
        try:
            router = self._router_factory(row.channel)
        except Exception as exc:
            log.exception("router_factory failed for channel=%s row=%s", row.channel, row.id)
            self._record_failure(row.id, f"router_factory: {exc}", now=now, stats=stats)
            return

        message, issue = _payload_to_router_args(row)

        try:
            result: SendResult = router.send(message=message, issue=issue)
        except Exception as exc:
            log.exception("router.send raised for row=%s", row.id)
            self._record_failure(row.id, str(exc), now=now, stats=stats)
            return

        if result.ok:
            self._outbox.mark_sent(row_id=row.id, now=now)
            stats.sent += 1
            return

        err = result.error or f"cascade-failed at {result.hop}"
        self._record_failure(row.id, err, now=now, stats=stats)

    def _record_failure(self, row_id: str, error: str, *, now: datetime, stats: CycleStats) -> None:
        self._outbox.record_failure(row_id=row_id, error=error, now=now)
        refreshed = self._outbox.get_by_id(row_id)
        if refreshed and refreshed.status == "dead-lettered":
            stats.dead_lettered += 1
        else:
            stats.failed += 1

    def run_forever(self) -> None:
        """Loop run_once until SIGTERM. Designed for LaunchAgent."""
        self._install_signal_handlers()
        log.info("notification_worker starting (poll=%.1fs batch=%d zombie=%ds)",
                 self._poll_interval, self._batch_size, self._zombie_timeout)
        while not self._shutdown_event.is_set():
            try:
                stats = self.run_once()
                if stats.claimed or stats.zombie_recovered:
                    log.info("cycle: claimed=%d sent=%d failed=%d dead=%d zombie=%d",
                             stats.claimed, stats.sent, stats.failed,
                             stats.dead_lettered, stats.zombie_recovered)
            except Exception:
                log.exception("worker cycle raised; sleeping then retry")
            self._shutdown_event.wait(self._poll_interval)
        log.info("notification_worker stopped")

    def stop(self) -> None:
        """Idempotent — signals run_forever to exit on next cycle."""
        self._shutdown_event.set()

    def _install_signal_handlers(self) -> None:
        def _handler(signum: int, _frame: Any) -> None:
            log.info("received signal %d; initiating graceful shutdown", signum)
            self.stop()

        for sig in (signal.SIGTERM, signal.SIGINT):
            try:
                signal.signal(sig, _handler)
            except (ValueError, OSError):
                # Not in main thread (e.g. embedded test): skip handler install
                pass


def _payload_to_router_args(row: OutboxRow) -> tuple[str, dict[str, Any]]:
    """Convert outbox payload_json -> (message, issue-dict) for Router.send.

    Payload conventions:
    - If payload is JSON-object with 'message' key: use that as message-text.
    - 'title', 'audience', 'labels' etc. forwarded as issue fields.
    - 'id' from issue defaults to row.id; 'dedupe_key' from row.dedup_key.
    - Otherwise: treat entire payload_json as message-text (legacy/free-form).
    """
    try:
        payload = json.loads(row.payload_json)
    except (ValueError, json.JSONDecodeError):
        payload = None

    if isinstance(payload, dict) and "message" in payload:
        message = str(payload.get("message") or "")
        issue: dict[str, Any] = {
            "id": str(payload.get("id") or row.id),
            "dedupe_key": row.dedup_key,
        }
        for key in ("title", "audience", "labels", "topic", "severity"):
            if key in payload:
                issue[key] = payload[key]
        return message, issue

    return row.payload_json, {"id": row.id, "dedupe_key": row.dedup_key}


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)
