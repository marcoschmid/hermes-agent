#!/usr/bin/env python3
"""Watch workspace skills and sync them into Hermes skills."""

from __future__ import annotations

import json
import logging
import os
import signal
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from fnmatch import fnmatch
from pathlib import Path
from typing import Any, Callable

from tools.skills_sync import sync_skills
from watchdog.events import FileSystemEventHandler
from watchdog.observers import Observer

logger = logging.getLogger(__name__)

QUIET_WINDOW_SECONDS = 2.0
MAX_BURST_SECONDS = 30.0
STALE_LOCK_SECONDS = 10 * 60
IGNORED_NAMES = {".DS_Store"}
IGNORED_DIRS = {".git", ".github", "__pycache__"}
IGNORED_SUFFIXES = (".swp", ".swo", ".tmp", "~")


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
            return True
        except FileExistsError:
            if not _lock_is_stale(active_lock):
                return False
            try:
                active_lock.unlink()
                logger.warning("Recovered stale workspace sync lock: %s", active_lock)
            except FileNotFoundError:
                continue
            except OSError:
                return False


def release_lock(lock_file: Path | None = None) -> None:
    active_lock = Path(lock_file) if lock_file is not None else resolve_config().lock_file
    pid = _read_lock_pid(active_lock)
    if pid not in (None, os.getpid()):
        return
    try:
        active_lock.unlink()
    except FileNotFoundError:
        pass


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
        "total_source": result.get("total_source", result.get("total_bundled", 0)),
    }
    if error:
        payload["error"] = error

    logger.info(json.dumps(payload, sort_keys=True))
    return payload


def is_ignored_path(path: str | None) -> bool:
    if not path:
        return False
    candidate = Path(path)
    name = candidate.name
    if name in IGNORED_NAMES:
        return True
    if any(part in IGNORED_DIRS for part in candidate.parts):
        return True
    if any(fnmatch(name, f"*{suffix}") for suffix in IGNORED_SUFFIXES):
        return True
    return False


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
        paths = [getattr(event, "src_path", None), getattr(event, "dest_path", None)]
        if any(is_ignored_path(path) for path in paths):
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
