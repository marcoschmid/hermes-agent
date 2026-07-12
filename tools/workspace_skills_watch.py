#!/usr/bin/env python3
"""Watch workspace skills and sync them into Hermes skills."""

from __future__ import annotations

import json
import logging
import os
import signal
import tempfile
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from tools.skills_sync import (
    SYNC_IGNORED_DIRS,
    SYNC_IGNORED_FILES,
    SYNC_IGNORED_SUFFIXES,
    _rename_noreplace,
    is_sync_ignored_path,
    sync_skills,
)
from watchdog.events import FileSystemEventHandler
from watchdog.observers import Observer

logger = logging.getLogger(__name__)

QUIET_WINDOW_SECONDS = 2.0
MAX_BURST_SECONDS = 30.0
STALE_LOCK_SECONDS = 10 * 60
IGNORED_NAMES = SYNC_IGNORED_FILES
IGNORED_DIRS = SYNC_IGNORED_DIRS
IGNORED_SUFFIXES = tuple(sorted(SYNC_IGNORED_SUFFIXES))
_owned_locks: dict[str, tuple[int, int, bytes]] = {}
_owned_locks_guard = threading.Lock()


@dataclass(frozen=True)
class WatchConfig:
    source_dir: Path
    target_dir: Path
    manifest_file: Path
    lock_file: Path


def resolve_config() -> WatchConfig:
    source = Path(
        os.getenv(
            "HERMES_WORKSPACE_SKILLS",
            str(Path.home() / ".openclaw/workspace/skills"),
        )
    ).expanduser()
    hermes_home = Path(
        os.getenv("HERMES_HOME", str(Path.home() / ".hermes"))
    ).expanduser()
    target = hermes_home / "skills"
    return WatchConfig(
        source_dir=source,
        target_dir=target,
        manifest_file=target / ".workspace_manifest",
        lock_file=target / ".workspace_sync.lock",
    )


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _pid_exists(pid: int) -> bool:
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def _read_lock_pid(lock_file: Path) -> int | None:
    try:
        raw = lock_file.read_text(encoding="utf-8").strip()
        return int(raw) if raw else None
    except (OSError, ValueError):
        return None


def _lock_is_stale(lock_file: Path) -> bool:
    try:
        stat = lock_file.stat()
    except OSError:
        return False
    if time.time() - stat.st_mtime <= STALE_LOCK_SECONDS:
        return False
    pid = _read_lock_pid(lock_file)
    return pid is None or not _pid_exists(pid)


def _lock_snapshot(lock_file: Path) -> tuple[int, int, bytes] | None:
    """Read a stable lock identity and payload, or fail closed on a race."""
    try:
        before = lock_file.lstat()
        payload = lock_file.read_bytes()
        after = lock_file.lstat()
    except OSError:
        return None
    before_identity = (before.st_dev, before.st_ino)
    if before_identity != (after.st_dev, after.st_ino):
        return None
    return (*before_identity, payload)


def _unique_quarantine(lock_file: Path, purpose: str) -> Path:
    fd, name = tempfile.mkstemp(
        dir=str(lock_file.parent),
        prefix=f".{lock_file.name}.{purpose}-retained-",
    )
    os.close(fd)
    os.unlink(name)
    return Path(name)


def _lock_mutation_barrier(_purpose: str) -> None:
    """Test seam immediately between lock ownership check and rename."""
    return None


def _quarantine_lock_if_owned(
    lock_file: Path,
    expected: tuple[int, int, bytes],
    purpose: str,
) -> bool:
    """Move an exact lock to retained quarantine without deleting a raced owner."""
    quarantine = _unique_quarantine(lock_file, purpose)
    _lock_mutation_barrier(purpose)
    try:
        _rename_noreplace(lock_file, quarantine)
    except (FileNotFoundError, FileExistsError):
        return False
    observed = _lock_snapshot(quarantine)
    if observed == expected:
        return True
    if not os.path.lexists(lock_file):
        try:
            _rename_noreplace(quarantine, lock_file)
        except OSError:
            logger.error("Retained raced lock at %s", quarantine, exc_info=True)
    return False


def acquire_lock(lock_file: Path | None = None) -> bool:
    """Acquire the inter-process sync lock.

    Returns False when another live process owns the lock.
    """
    active_lock = Path(lock_file) if lock_file is not None else resolve_config().lock_file
    active_lock.parent.mkdir(parents=True, exist_ok=True)

    while True:
        try:
            fd = os.open(str(active_lock), os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o644)
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                handle.write(str(os.getpid()))
                handle.flush()
                os.fsync(handle.fileno())
                created_stat = os.fstat(handle.fileno())
            owned = _lock_snapshot(active_lock)
            expected_payload = str(os.getpid()).encode("utf-8")
            if owned != (created_stat.st_dev, created_stat.st_ino, expected_payload):
                return False
            with _owned_locks_guard:
                _owned_locks[str(active_lock)] = owned
            return True
        except FileExistsError:
            if not _lock_is_stale(active_lock):
                return False
            stale = _lock_snapshot(active_lock)
            if stale is None:
                continue
            try:
                stale_pid = int(stale[2].decode("utf-8").strip())
            except (UnicodeDecodeError, ValueError):
                stale_pid = None
            try:
                age = time.time() - active_lock.lstat().st_mtime
            except OSError:
                continue
            if age <= STALE_LOCK_SECONDS or (
                stale_pid is not None and _pid_exists(stale_pid)
            ):
                return False
            if _quarantine_lock_if_owned(active_lock, stale, "stale"):
                logger.warning("Recovered stale workspace sync lock: %s", active_lock)
                continue
            return False


def release_lock(lock_file: Path | None = None) -> None:
    active_lock = Path(lock_file) if lock_file is not None else resolve_config().lock_file
    with _owned_locks_guard:
        owned = _owned_locks.pop(str(active_lock), None)
    if owned is None:
        return
    _quarantine_lock_if_owned(active_lock, owned, "release")


def _count(value: Any) -> int:
    if value is None:
        return 0
    if isinstance(value, int):
        return value
    try:
        return len(value)
    except TypeError:
        return 0


def run_once(reason: str, event_count: int = 0, config: WatchConfig | None = None) -> dict[str, Any]:
    active_config = config or resolve_config()
    active_config.target_dir.mkdir(parents=True, exist_ok=True)

    if not acquire_lock(active_config.lock_file):
        logger.info(
            json.dumps(
                {
                    "reason": reason,
                    "event_count": event_count,
                    "sync_skipped": "lock_busy",
                },
                sort_keys=True,
            )
        )
        return {"sync_skipped": "lock_busy"}

    started_monotonic = time.monotonic()
    started_at = _utc_now()
    result: dict[str, Any] = {}
    error: str | None = None

    try:
        result = sync_skills(
            quiet=True,
            source_dir=active_config.source_dir,
            target_dir=active_config.target_dir,
            manifest_file=active_config.manifest_file,
            remove_deleted=True,
        )
    except Exception as exc:  # pragma: no cover - defensive service boundary
        error = f"{type(exc).__name__}: {exc}"
        logger.exception("Workspace skills sync failed")
    finally:
        done_at = _utc_now()
        duration_ms = int((time.monotonic() - started_monotonic) * 1000)
        release_lock(active_config.lock_file)

    payload = {
        "reason": reason,
        "event_count": event_count,
        "sync_started_at": started_at,
        "sync_done_at": done_at,
        "duration_ms": duration_ms,
        "copied": _count(result.get("copied")),
        "updated": _count(result.get("updated")),
        "removed": _count(result.get("removed")),
        "skipped": _count(result.get("skipped")),
        "user_modified": _count(result.get("user_modified")),
        "validation_errors": _count(result.get("validation_errors")),
        "copy_errors": result.get("copy_errors", []),
        "copy_error_count": _count(result.get("copy_errors")),
        "total_source": result.get("total_source", result.get("total_bundled", 0)),
    }
    if payload["copy_error_count"]:
        error = f"skills sync completed with {payload['copy_error_count']} copy error(s)"
    if error:
        payload["error"] = error

    logger.info(json.dumps(payload, sort_keys=True))
    return payload


def is_ignored_path(path: str | None) -> bool:
    if not path:
        return False
    return is_sync_ignored_path(Path(path))


class DebouncedHandler(FileSystemEventHandler):
    def __init__(
        self,
        sync_func: Callable[[str, int], Any] | None = None,
        quiet_window: float = QUIET_WINDOW_SECONDS,
        max_burst: float = MAX_BURST_SECONDS,
    ) -> None:
        super().__init__()
        self.sync_func = sync_func or (lambda reason, count: run_once(reason, count))
        self.quiet_window = quiet_window
        self.max_burst = max_burst
        self._lock = threading.Lock()
        self._timer: threading.Timer | None = None
        self._first_event_at: float | None = None
        self._event_count = 0
        self._sync_running = False
        self._pending = False

    def dispatch(self, event: Any) -> None:
        source = getattr(event, "src_path", None)
        destination = getattr(event, "dest_path", None)
        if getattr(event, "event_type", None) == "moved":
            # A move crossing the managed/ignored boundary changes the sync tree.
            # Suppress only a fully specified ignored -> ignored move.
            if (
                source
                and destination
                and is_ignored_path(source)
                and is_ignored_path(destination)
            ):
                return
        elif source and is_ignored_path(source):
            return
        super().dispatch(event)

    def on_any_event(self, event: Any) -> None:
        self.enqueue()

    def enqueue(self) -> None:
        now = time.monotonic()
        with self._lock:
            self._event_count += 1
            if self._first_event_at is None:
                self._first_event_at = now
            if self._sync_running:
                self._pending = True
                return

            elapsed = now - self._first_event_at
            delay = 0.0 if elapsed >= self.max_burst else self.quiet_window
            self._schedule_locked(delay)

    def _schedule_locked(self, delay: float) -> None:
        if self._timer is not None:
            self._timer.cancel()
        self._timer = threading.Timer(delay, self._fire_sync)
        self._timer.daemon = True
        self._timer.start()

    def _fire_sync(self) -> None:
        with self._lock:
            if self._sync_running:
                self._pending = True
                return
            event_count = self._event_count
            self._event_count = 0
            self._first_event_at = None
            self._sync_running = True
            self._timer = None

        try:
            self.sync_func("watch", event_count)
        except Exception:
            logger.exception("Workspace skills watch sync failed")
        finally:
            with self._lock:
                self._sync_running = False
                if self._pending:
                    self._pending = False
                    self._first_event_at = time.monotonic()
                    self._schedule_locked(self.quiet_window)


def watch_loop(config: WatchConfig | None = None, stop_event: threading.Event | None = None) -> None:
    active_config = config or resolve_config()
    active_config.source_dir.mkdir(parents=True, exist_ok=True)
    active_config.target_dir.mkdir(parents=True, exist_ok=True)

    run_once("startup", config=active_config)

    stop = stop_event or threading.Event()
    observer = Observer()
    handler = DebouncedHandler(
        sync_func=lambda reason, count: run_once(reason, count, active_config)
    )
    observer.schedule(handler, str(active_config.source_dir), recursive=True)
    observer.start()
    logger.info("Watching workspace skills at %s", active_config.source_dir)

    try:
        while not stop.wait(1.0):
            pass
    finally:
        observer.stop()
        observer.join()


def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(message)s",
    )
    stop = threading.Event()

    def _handle_signal(signum: int, _frame: Any) -> None:
        logger.info("Received signal %s, stopping workspace skills watcher", signum)
        stop.set()

    signal.signal(signal.SIGTERM, _handle_signal)
    signal.signal(signal.SIGINT, _handle_signal)

    watch_loop(stop_event=stop)


if __name__ == "__main__":
    main()
