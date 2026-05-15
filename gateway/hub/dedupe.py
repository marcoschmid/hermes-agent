"""Dedup-window tracking for hub pipeline (Step 3).

Phase 1: in-memory dict, reset on Hermes restart (acceptable — dedup is
suppression heuristic, restart triggers fresh slate).

Phase 2: persisted via dedupe_windows MC table.
"""
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Optional


DEFAULT_WINDOW_SECONDS = 3600  # 1h, Pattern from ev-charge


@dataclass
class DedupEntry:
    first_seen_at: datetime
    last_seen_at: datetime
    count: int
    suppressed_until: datetime


_dedupe_state: dict[tuple[str, str, str], DedupEntry] = {}


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _make_key(source_id: str, topic_id: str, dedupe_key: str) -> tuple[str, str, str]:
    return (source_id, topic_id, dedupe_key)


@dataclass
class DedupResult:
    """Returned by check_dedup. is_duplicate=False → proceed; True → suppress event."""
    is_duplicate: bool
    count: int
    first_seen_at: Optional[datetime] = None
    suppressed_until: Optional[datetime] = None


def check_dedup(
    source_id: str,
    topic_id: str,
    dedupe_key: Optional[str],
    window_seconds: int = DEFAULT_WINDOW_SECONDS,
    now: Optional[datetime] = None,
) -> DedupResult:
    """Step 3 of pipeline. NULL dedupe_key → never duplicate (always proceed)."""
    if not dedupe_key:
        return DedupResult(is_duplicate=False, count=0)

    current = now or _now()
    k = _make_key(source_id, topic_id, dedupe_key)
    entry = _dedupe_state.get(k)

    if entry is None or entry.suppressed_until <= current:
        # First-seen OR window expired
        new_entry = DedupEntry(
            first_seen_at=current,
            last_seen_at=current,
            count=1,
            suppressed_until=current + timedelta(seconds=window_seconds),
        )
        _dedupe_state[k] = new_entry
        return DedupResult(is_duplicate=False, count=1)

    # Within window — duplicate
    entry.last_seen_at = current
    entry.count += 1
    return DedupResult(
        is_duplicate=True,
        count=entry.count,
        first_seen_at=entry.first_seen_at,
        suppressed_until=entry.suppressed_until,
    )


def reset_dedupe_state() -> None:
    """Test-helper. Clears in-memory state."""
    _dedupe_state.clear()
