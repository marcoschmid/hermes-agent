"""Consumer-loop for telegram_callbacks dispatch (P4 Track B).

Polls ``telegram_callbacks`` for dispatch_status='pending' AND next_retry_at <= now
rows + invokes registered action-handler per callback_type + transitions row
through claim/process/fail/dead-letter states.

Round-2 hardening (Codex no-ship findings):
- HIGH-5 atomic claim with UUID claim_token. Two dispatcher instances cannot
  double-dispatch the same row (UPDATE ... RETURNING fences ownership).
- HIGH-2 transient-failure retry: handler exception increments attempts +
  schedules backoff via next_retry_at. Dead-letter at DEAD_LETTER_THRESHOLD
  attempts. Permanent-failure-class (unhandled type) → dispatch_status='failed'
  immediately (no retry).

Designed for separate LaunchAgent OR Hub-async-task. ActionHandler is
dependency-injected by host (see notification_worker_entrypoint for pattern).

See ``projects/jarvis-os-redesign/plans/2026-05-24-p4-track-b-telegram-receiver-wiring.md``.
"""
from __future__ import annotations

import logging
import signal
import sqlite3
import threading
import uuid
from contextlib import closing
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Callable

from ._sqlite_helpers import (
    iso as _strict_iso,
    retry_on_locked as _retry_on_locked,
    utcnow as _utcnow,
)


def _iso(dt: datetime) -> str:
    """Lenient ISO formatter — preserves pre-refactor behavior."""
    return _strict_iso(dt, strict_aware=False)

log = logging.getLogger(__name__)

POLL_INTERVAL_SECONDS = 5
CLAIM_BATCH_SIZE = 25
DEAD_LETTER_THRESHOLD = 5
BACKOFF_BASE_SECONDS = 30
BACKOFF_MAX_SECONDS = 3600
BUSY_TIMEOUT_MS = 30000
# Round-3 HIGH-1: claimed-stuck rows recovery (worker SIGKILL mid-dispatch).
# Default 300s = 5min. Matches notification_worker zombie-pattern.
ZOMBIE_CLAIM_TIMEOUT_SECONDS = 300


ActionHandler = Callable[[str, dict[str, Any]], None]
"""Signature: handler(issue_id, row_dict) -> None. Raises on transient failure."""


@dataclass(slots=True)
class CycleStats:
    claimed: int = 0
    dispatched: int = 0
    failed: int = 0
    dead_lettered: int = 0
    zombie_recovered: int = 0


class TelegramActionDispatcher:
    """Atomic-claim consumer-loop for telegram_callbacks dispatch.

    handlers maps callback_type -> ActionHandler. Unknown callback_types
    transition row to dispatch_status='failed' permanently (no retry, since
    callback_data shape will not change).
    """

    def __init__(
        self,
        *,
        db_path: str,
        handlers: dict[str, ActionHandler],
        poll_interval: float = POLL_INTERVAL_SECONDS,
        batch_size: int = CLAIM_BATCH_SIZE,
        dead_letter_threshold: int = DEAD_LETTER_THRESHOLD,
        zombie_timeout_seconds: int = ZOMBIE_CLAIM_TIMEOUT_SECONDS,
    ) -> None:
        self._db_path = str(db_path)
        self._handlers = handlers
        self._poll_interval = poll_interval
        self._batch_size = batch_size
        self._dead_letter_threshold = dead_letter_threshold
        self._zombie_timeout = zombie_timeout_seconds
        self._shutdown_event = threading.Event()

    def run_once(self, *, now: datetime | None = None) -> CycleStats:
        """Process one batch atomically (claim → dispatch → mark)."""
        now = now or _utcnow()
        stats = CycleStats()
        # Round-3 HIGH-1: recover claimed-stuck rows BEFORE new claims
        stats.zombie_recovered = self._recover_claimed_zombies(now=now)
        rows = self._claim_due(now=now)
        stats.claimed = len(rows)
        for row in rows:
            if self._shutdown_event.is_set():
                log.info("shutdown mid-batch; %d rows remain claimed (zombie-eligible)",
                         stats.claimed - stats.dispatched - stats.failed - stats.dead_lettered)
                break
            self._dispatch_row(row, now=now, stats=stats)
        return stats

    @_retry_on_locked
    def _recover_claimed_zombies(self, *, now: datetime) -> int:
        """Recover claimed-stuck rows (handler-SIGKILL mid-dispatch).

        Round-3 HIGH-1: claimed rows past zombie_timeout get reverted to
        pending+backoff+attempts++ (analog OutboxStore.recover_zombies).
        Dead-letter at threshold prevents infinite-retry on permanent-killers.
        """
        cutoff = _iso(now - timedelta(seconds=self._zombie_timeout))
        ts = _iso(now)
        recovered = 0
        with closing(self._conn()) as conn:
            zombies = conn.execute(
                "SELECT callback_id, attempts FROM telegram_callbacks "
                "WHERE dispatch_status='claimed' AND claimed_at < ?",
                (cutoff,),
            ).fetchall()
            for z in zombies:
                rid = z["callback_id"]
                attempts = (z["attempts"] or 0) + 1
                if attempts >= self._dead_letter_threshold:
                    new_status = "dead-lettered"
                    next_retry = None
                else:
                    new_status = "pending"
                    backoff = min(BACKOFF_MAX_SECONDS,
                                  BACKOFF_BASE_SECONDS * (2 ** (attempts - 1)))
                    next_retry = _iso(now + timedelta(seconds=backoff))
                cursor = conn.execute(
                    "UPDATE telegram_callbacks SET dispatch_status=?, attempts=?, "
                    "last_error=?, next_retry_at=?, claim_token=NULL, claimed_at=NULL "
                    "WHERE callback_id=? AND dispatch_status='claimed'",
                    (new_status, attempts, "zombie recovered", next_retry, rid),
                )
                if cursor.rowcount:
                    recovered += 1
        return recovered

    @_retry_on_locked
    def _claim_due(self, *, now: datetime) -> list[dict]:
        """Atomic claim: UPDATE pending rows due now to status='claimed' with fresh token.
        Round-3 HIGH-1: claimed_at captures claim-timestamp for zombie-recovery.
        """
        ts = _iso(now)
        token = uuid.uuid4().hex
        with closing(self._conn()) as conn:
            cursor = conn.execute(
                "UPDATE telegram_callbacks "
                "SET dispatch_status='claimed', claim_token=?, claimed_at=? "
                "WHERE callback_id IN ("
                "  SELECT callback_id FROM telegram_callbacks "
                "  WHERE dispatch_status='pending' AND accepted=1 "
                "    AND (next_retry_at IS NULL OR next_retry_at<=?) "
                "  ORDER BY received_at ASC LIMIT ?"
                ") RETURNING *",
                (token, ts, ts, self._batch_size),
            )
            return [dict(r) for r in cursor.fetchall()]

    def _dispatch_row(self, row: dict, *, now: datetime, stats: CycleStats) -> None:
        callback_id = row["callback_id"]
        claim_token = row["claim_token"]
        callback_type = row.get("callback_type") or ""
        handler = self._handlers.get(callback_type)

        if handler is None:
            log.warning("no handler for callback_type=%r row=%s — permanent-fail",
                        callback_type, callback_id)
            self._mark_permanent_failure(callback_id, claim_token,
                                         error="no handler registered", now=now)
            stats.failed += 1
            return

        issue_id = row.get("issue_id") or ""
        try:
            handler(issue_id, row)
        except Exception as exc:
            log.warning("handler raised for callback=%s: %s — schedule retry",
                        callback_id, exc)
            outcome = self._record_transient_failure(
                callback_id, claim_token, error=str(exc)[:400], now=now,
            )
            if outcome == "dead-lettered":
                stats.dead_lettered += 1
            else:
                stats.failed += 1
            return

        self._mark_processed(callback_id, claim_token, now=now)
        stats.dispatched += 1

    @_retry_on_locked
    def _mark_processed(self, callback_id: str, claim_token: str, *, now: datetime) -> None:
        ts = _iso(now)
        with closing(self._conn()) as conn:
            cursor = conn.execute(
                "UPDATE telegram_callbacks SET dispatch_status='processed', "
                "processed_at=?, claim_token=NULL, claimed_at=NULL "
                "WHERE callback_id=? AND claim_token=?",
                (ts, callback_id, claim_token),
            )
            if cursor.rowcount == 0:
                log.warning("mark_processed stale claim row=%s", callback_id)

    @_retry_on_locked
    def _mark_permanent_failure(self, callback_id: str, claim_token: str,
                                 *, error: str, now: datetime) -> None:
        ts = _iso(now)
        with closing(self._conn()) as conn:
            conn.execute(
                "UPDATE telegram_callbacks SET dispatch_status='failed', "
                "last_error=?, processed_at=?, claim_token=NULL, claimed_at=NULL "
                "WHERE callback_id=? AND claim_token=?",
                (error, ts, callback_id, claim_token),
            )

    @_retry_on_locked
    def _record_transient_failure(self, callback_id: str, claim_token: str,
                                   *, error: str, now: datetime) -> str:
        """Increment attempts + schedule backoff OR dead-letter. Returns outcome."""
        ts = _iso(now)
        with closing(self._conn()) as conn:
            row = conn.execute(
                "SELECT attempts FROM telegram_callbacks "
                "WHERE callback_id=? AND claim_token=?",
                (callback_id, claim_token),
            ).fetchone()
            if row is None:
                log.warning("record_transient_failure stale claim row=%s", callback_id)
                return "stale"
            attempts = (row["attempts"] or 0) + 1
            if attempts >= self._dead_letter_threshold:
                conn.execute(
                    "UPDATE telegram_callbacks SET dispatch_status='dead-lettered', "
                    "attempts=?, last_error=?, processed_at=?, claim_token=NULL, claimed_at=NULL "
                    "WHERE callback_id=? AND claim_token=?",
                    (attempts, error, ts, callback_id, claim_token),
                )
                return "dead-lettered"
            backoff = min(BACKOFF_MAX_SECONDS,
                          BACKOFF_BASE_SECONDS * (2 ** (attempts - 1)))
            next_retry = _iso(now + timedelta(seconds=backoff))
            conn.execute(
                "UPDATE telegram_callbacks SET dispatch_status='pending', "
                "attempts=?, last_error=?, next_retry_at=?, claim_token=NULL, claimed_at=NULL "
                "WHERE callback_id=? AND claim_token=?",
                (attempts, error, next_retry, callback_id, claim_token),
            )
            return "retry"

    def _conn(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self._db_path, isolation_level=None, timeout=30)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode = WAL")
        conn.execute(f"PRAGMA busy_timeout = {BUSY_TIMEOUT_MS}")
        return conn

    def run_forever(self) -> None:
        self._install_signal_handlers()
        log.info("telegram_action_dispatcher starting (poll=%.1fs handlers=%s)",
                 self._poll_interval, sorted(self._handlers.keys()))
        while not self._shutdown_event.is_set():
            try:
                stats = self.run_once()
                if stats.claimed:
                    log.info("cycle: claimed=%d dispatched=%d failed=%d dead=%d",
                             stats.claimed, stats.dispatched, stats.failed,
                             stats.dead_lettered)
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


