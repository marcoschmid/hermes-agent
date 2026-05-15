"""Flapping detection — Step 5 of pipeline.

Per (source_id, topic_id)-tuple state-change counter (sliding window).
If too many changes in window, suppresses further notifications.
Pattern from ev-charge-monitor _record_status_change.
"""
from datetime import datetime, timedelta, timezone
from typing import Optional

DEFAULT_FLAP_THRESHOLD = 5
DEFAULT_FLAP_WINDOW_SECONDS = 600

_changes: dict[tuple[str, str], list[datetime]] = {}


def _now() -> datetime:
    return datetime.now(timezone.utc)


def record_state_change(
    source_id: str,
    topic_id: str,
    timestamp: Optional[datetime] = None,
) -> None:
    """Record a state-change. Call before is_flapping check."""
    ts = timestamp or _now()
    key = (source_id, topic_id)
    history = _changes.setdefault(key, [])
    history.append(ts)
    # Trim old entries
    cutoff = ts - timedelta(seconds=DEFAULT_FLAP_WINDOW_SECONDS)
    _changes[key] = [t for t in history if t > cutoff]


def is_flapping(
    source_id: str,
    topic_id: str,
    threshold: int = DEFAULT_FLAP_THRESHOLD,
    window_seconds: int = DEFAULT_FLAP_WINDOW_SECONDS,
    now: Optional[datetime] = None,
) -> bool:
    """Returns True if state-changes in window exceed threshold."""
    current = now or _now()
    key = (source_id, topic_id)
    history = _changes.get(key, [])
    cutoff = current - timedelta(seconds=window_seconds)
    recent = [t for t in history if t > cutoff]
    return len(recent) >= threshold


def reset_flapping_state() -> None:
    """Test-helper."""
    _changes.clear()
