"""Cooldown tracking — Step 4 of pipeline.

Per (source_id, channel_id, topic_id)-tuple last-dispatch timestamp.
Cooldown blocks repeat-send within the configured window (default 60s).
In-memory; reset on Hermes restart (acceptable for short-lived heuristic).
"""
from datetime import datetime, timedelta, timezone
from typing import Optional

DEFAULT_COOLDOWN_SECONDS = 60

_cooldown_state: dict[tuple[str, str, str], datetime] = {}


def _now() -> datetime:
    return datetime.now(timezone.utc)


def is_in_cooldown(
    source_id: str,
    channel_id: str,
    topic_id: str,
    cooldown_seconds: int = DEFAULT_COOLDOWN_SECONDS,
    now: Optional[datetime] = None,
) -> bool:
    """Returns True if the channel was used for this source+topic within cooldown_seconds."""
    current = now or _now()
    last = _cooldown_state.get((source_id, channel_id, topic_id))
    if last is None:
        return False
    return (current - last) < timedelta(seconds=cooldown_seconds)


def record_cooldown(
    source_id: str,
    channel_id: str,
    topic_id: str,
    now: Optional[datetime] = None,
) -> None:
    """Mark channel-dispatch timestamp. Call after successful send."""
    _cooldown_state[(source_id, channel_id, topic_id)] = now or _now()


def reset_cooldown_state() -> None:
    """Test-helper."""
    _cooldown_state.clear()
