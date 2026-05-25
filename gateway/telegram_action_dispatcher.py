"""Consumer-loop for telegram_callbacks (P4 Track B followup).

Polls ``telegram_callbacks`` table for accepted-but-unprocessed rows + invokes
the registered action-handler per callback_type, then persists ``processed_at``
to mark done.

Designed for Hub-async-task (startup-event) OR separate LaunchAgent.

Action-Handlers are dependency-injected by the host so this module stays
test-isolated. Production wiring (Marco-side):
- approve → paperclip_issue_client.update_issue(status='approved')
- reject  → paperclip_issue_client.update_issue(status='rejected')
- skip    → log-only (no Paperclip-side action)

Crash-safe: rows with processed_at IS NULL get retried on next cycle.
Idempotency-of-action is the handler's responsibility (Paperclip status-update
is idempotent: approving an already-approved Issue is a no-op).

See ``projects/jarvis-os-redesign/plans/2026-05-24-p4-track-b-telegram-receiver-wiring.md``.
"""
from __future__ import annotations

import logging
import signal
import sqlite3
import threading
from contextlib import closing
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Callable

log = logging.getLogger(__name__)

POLL_INTERVAL_SECONDS = 5
CLAIM_BATCH_SIZE = 25

ActionHandler = Callable[[str, dict], None]
"""Signature: handler(issue_id, row_dict) -> None. Raises on action-failure."""


@dataclass(slots=True)
class CycleStats:
    fetched: int = 0
    dispatched: int = 0
    failed: int = 0


class TelegramActionDispatcher:
    """Consumer-loop for telegram_callbacks unprocessed rows.

    handlers maps callback_type -> ActionHandler. Unknown callback_types are
    marked processed-with-error (no infinite retry on bad action_data).
    """

    def __init__(
        self,
        *,
        db_path: str,
        handlers: dict[str, ActionHandler],
        poll_interval: float = POLL_INTERVAL_SECONDS,
        batch_size: int = CLAIM_BATCH_SIZE,
    ) -> None:
        self._db_path = str(db_path)
        self._handlers = handlers
        self._poll_interval = poll_interval
        self._batch_size = batch_size
        self._shutdown_event = threading.Event()

    def run_once(self) -> CycleStats:
        """Process one batch of unprocessed accepted-callbacks."""
        stats = CycleStats()
        rows = self._fetch_unprocessed()
        stats.fetched = len(rows)
        for row in rows:
            if self._shutdown_event.is_set():
                log.info("shutdown mid-batch; remaining %d rows retried next cycle",
                         stats.fetched - stats.dispatched - stats.failed)
                break
            self._dispatch_row(row, stats=stats)
        return stats

    def _fetch_unprocessed(self) -> list[dict]:
        with closing(self._conn()) as conn:
            cursor = conn.execute(
                "SELECT * FROM telegram_callbacks "
                "WHERE processed_at IS NULL AND accepted=1 "
                "ORDER BY received_at ASC LIMIT ?",
                (self._batch_size,),
            )
            return [dict(r) for r in cursor.fetchall()]

    def _dispatch_row(self, row: dict, *, stats: CycleStats) -> None:
        callback_type = row.get("callback_type") or ""
        handler = self._handlers.get(callback_type)
        if handler is None:
            log.warning("no handler for callback_type=%r row=%s",
                        callback_type, row.get("callback_id"))
            self._mark_processed(row["callback_id"], error="no handler registered")
            stats.failed += 1
            return

        issue_id = row.get("issue_id") or ""
        try:
            handler(issue_id, row)
        except Exception as exc:
            log.exception("action handler raised for callback=%s", row.get("callback_id"))
            self._mark_processed(row["callback_id"], error=str(exc)[:400])
            stats.failed += 1
            return

        self._mark_processed(row["callback_id"], error=None)
        stats.dispatched += 1

    def _mark_processed(self, callback_id: str, *, error: str | None) -> None:
        ts = _utcnow().isoformat()
        with closing(self._conn()) as conn:
            if error is None:
                conn.execute(
                    "UPDATE telegram_callbacks SET processed_at=? "
                    "WHERE callback_id=? AND processed_at IS NULL",
                    (ts, callback_id),
                )
            else:
                conn.execute(
                    "UPDATE telegram_callbacks SET processed_at=?, error=? "
                    "WHERE callback_id=? AND processed_at IS NULL",
                    (ts, error, callback_id),
                )

    def _conn(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self._db_path, isolation_level=None, timeout=30)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode = WAL")
        conn.execute("PRAGMA busy_timeout = 30000")
        return conn

    def run_forever(self) -> None:
        self._install_signal_handlers()
        log.info("telegram_action_dispatcher starting (poll=%.1fs handlers=%s)",
                 self._poll_interval, sorted(self._handlers.keys()))
        while not self._shutdown_event.is_set():
            try:
                stats = self.run_once()
                if stats.fetched:
                    log.info("cycle: fetched=%d dispatched=%d failed=%d",
                             stats.fetched, stats.dispatched, stats.failed)
            except Exception:
                log.exception("dispatcher cycle raised; sleeping then retry")
            self._shutdown_event.wait(self._poll_interval)
        log.info("telegram_action_dispatcher stopped")

    def stop(self) -> None:
        self._shutdown_event.set()

    def _install_signal_handlers(self) -> None:
        def _handler(signum: int, _frame) -> None:
            log.info("received signal %d; initiating graceful shutdown", signum)
            self.stop()
        for sig in (signal.SIGTERM, signal.SIGINT):
            try:
                signal.signal(sig, _handler)
            except (ValueError, OSError):
                pass


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)
