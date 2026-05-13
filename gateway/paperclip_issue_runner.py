"""High-level Paperclip issue runner — Jarvis-OS Phase-4 (4a-3 wave 3).

Wraps PaperclipIssueRunsClient with a contextmanager that handles the full
acquire → execute → heartbeat-loop → release lifecycle. Callers supply the
actual work as a callable and the runner manages the lock.

Typical use from a Hermes worker:

    runner = PaperclipIssueRunner(client=client, locked_by=worker_id)
    with runner.run(company_id=..., issue_id=..., executor="hermes") as run:
        result = do_actual_work(run.run_id)
        run.complete(exit_code=0, result_summary="…")

If the body raises, the runner releases with status='failed'. If the heartbeat
loop loses the lock, the runner sets a flag the body can poll via run.lock_active.
"""
from __future__ import annotations

import logging
import threading
from contextlib import contextmanager
from typing import Iterator, Literal, Optional

from .paperclip_issue_client import (
    IssueAlreadyRunning,
    IssueRunRecord,
    LockLost,
    PaperclipIssueRunsClient,
)

logger = logging.getLogger(__name__)

DEFAULT_HEARTBEAT_INTERVAL_SECONDS = 60.0


class IssueRunHandle:
    """Lifecycle handle yielded to the caller."""

    def __init__(self, run: IssueRunRecord) -> None:
        self.run = run
        self.run_id = run.run_id
        self.issue_id = run.issue_id
        self.company_id = run.company_id
        self._lock_active = True
        self._explicit_release: Optional[Literal["completed", "failed"]] = None
        self._exit_code: Optional[int] = None
        self._result_summary: Optional[str] = None

    @property
    def lock_active(self) -> bool:
        """False once the heartbeat-loop observes a LockLost."""
        return self._lock_active

    def complete(self, *, exit_code: int = 0, result_summary: Optional[str] = None) -> None:
        """Mark this run for release with status='completed'."""
        self._explicit_release = "completed"
        self._exit_code = exit_code
        self._result_summary = result_summary

    def fail(self, *, exit_code: int = 1, result_summary: Optional[str] = None) -> None:
        """Mark this run for release with status='failed'."""
        self._explicit_release = "failed"
        self._exit_code = exit_code
        self._result_summary = result_summary


class PaperclipIssueRunner:
    def __init__(
        self,
        *,
        client: PaperclipIssueRunsClient,
        locked_by: str,
        heartbeat_interval_seconds: float = DEFAULT_HEARTBEAT_INTERVAL_SECONDS,
    ) -> None:
        self._client = client
        self._locked_by = locked_by
        self._heartbeat_interval = heartbeat_interval_seconds

    @contextmanager
    def run(
        self,
        *,
        company_id: str,
        issue_id: str,
        executor: Literal["hermes", "mc-dispatch"],
        ttl_seconds: Optional[int] = None,
        prompt_snapshot_path: Optional[str] = None,
    ) -> Iterator[IssueRunHandle]:
        try:
            record = self._client.acquire(
                company_id=company_id,
                issue_id=issue_id,
                executor=executor,
                locked_by=self._locked_by,
                ttl_seconds=ttl_seconds,
                prompt_snapshot_path=prompt_snapshot_path,
            )
        except IssueAlreadyRunning:
            logger.info("issue %s already running; skipping", issue_id)
            raise

        handle = IssueRunHandle(record)
        stop_heartbeat = threading.Event()
        heartbeat_thread = threading.Thread(
            target=self._heartbeat_loop,
            args=(handle, stop_heartbeat),
            name=f"issue-runs-heartbeat-{record.run_id}",
            daemon=True,
        )
        heartbeat_thread.start()

        status: Literal["completed", "failed"] = "completed"
        try:
            yield handle
            status = handle._explicit_release or "completed"
        except BaseException as exc:
            status = "failed"
            if handle._result_summary is None:
                handle._result_summary = f"{type(exc).__name__}: {exc}"
            raise
        finally:
            stop_heartbeat.set()
            heartbeat_thread.join(timeout=5.0)
            if handle.lock_active:
                try:
                    self._client.release(
                        run_id=record.run_id,
                        locked_by=self._locked_by,
                        status=status,
                        exit_code=handle._exit_code,
                        result_summary=handle._result_summary,
                    )
                except LockLost:
                    logger.warning(
                        "lock for run %s was lost before release; recovery already cleaned up",
                        record.run_id,
                    )

    def _heartbeat_loop(self, handle: IssueRunHandle, stop: threading.Event) -> None:
        while not stop.wait(self._heartbeat_interval):
            try:
                self._client.heartbeat(run_id=handle.run_id, locked_by=self._locked_by)
            except LockLost:
                logger.warning("heartbeat lost lock for run %s", handle.run_id)
                handle._lock_active = False
                return
            except Exception as exc:
                logger.warning("heartbeat error for run %s: %s", handle.run_id, exc)
