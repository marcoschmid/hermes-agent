"""Shared SQLite + datetime helpers for gateway modules (Phase-4 refactor).

Consolidates utilities previously duplicated across outbox.py,
telegram_gateway.py, telegram_action_dispatcher.py, notification_worker.py,
db_cleanup.py: ``retry_on_locked`` decorator + timezone-aware ISO formatters.

NO behavior change. All three previously-private symbols re-exported under
the same names from their original modules for backward-compat (importers
use module-prefixed names where needed).
"""
from __future__ import annotations

import sqlite3
import time
from datetime import datetime, timezone
from typing import Any, Callable

DEFAULT_LOCK_RETRY_MAX = 5
DEFAULT_LOCK_RETRY_BASE_SLEEP = 0.1


def retry_on_locked(
    fn: Callable[..., Any],
    *,
    max_retries: int = DEFAULT_LOCK_RETRY_MAX,
    base_sleep: float = DEFAULT_LOCK_RETRY_BASE_SLEEP,
) -> Callable[..., Any]:
    """Decorator: retry sqlite3 operations on 'database is locked' OperationalError.

    Exponential backoff: base_sleep * 2**attempt (default 100ms, 200ms, 400ms, 800ms).
    Raises after max_retries-1 attempts (last attempt re-raises).
    """
    def wrapper(*args: Any, **kwargs: Any) -> Any:
        for attempt in range(max_retries):
            try:
                return fn(*args, **kwargs)
            except sqlite3.OperationalError as exc:
                if "locked" not in str(exc).lower() or attempt == max_retries - 1:
                    raise
                time.sleep(base_sleep * (2 ** attempt))
        raise RuntimeError("unreachable")
    return wrapper


def utcnow() -> datetime:
    """Return current UTC time as timezone-aware datetime."""
    return datetime.now(timezone.utc)


def iso(dt: datetime, *, strict_aware: bool = True) -> str:
    """Format datetime as ISO-8601 UTC string.

    Args:
        dt: datetime to format.
        strict_aware: if True (default), raise ValueError on naive datetime.
                      If False, treat naive as UTC (legacy permissive behavior).
    """
    if dt.tzinfo is None or dt.utcoffset() is None:
        if strict_aware:
            raise ValueError("timezone-aware datetime required")
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc).isoformat()
