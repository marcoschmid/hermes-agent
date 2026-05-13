"""Tests for gateway.hub.flapping."""
import pytest
from datetime import datetime, timedelta, timezone

from gateway.hub.flapping import (
    is_flapping,
    record_state_change,
    reset_flapping_state,
)


@pytest.fixture(autouse=True)
def _reset() -> None:
    reset_flapping_state()


def test_under_threshold_not_flapping() -> None:
    now = datetime.now(timezone.utc)
    for _ in range(4):
        record_state_change("src_1", "tpc_1", timestamp=now)
    assert is_flapping("src_1", "tpc_1", threshold=5, now=now) is False


def test_at_or_over_threshold_is_flapping() -> None:
    now = datetime.now(timezone.utc)
    for _ in range(5):
        record_state_change("src_1", "tpc_1", timestamp=now)
    assert is_flapping("src_1", "tpc_1", threshold=5, now=now) is True


def test_old_entries_excluded_from_window() -> None:
    base = datetime.now(timezone.utc)
    # 5 changes long ago (outside window)
    old = base - timedelta(seconds=1200)
    for _ in range(5):
        record_state_change("src_1", "tpc_1", timestamp=old)
    # is_flapping at "now" — old entries outside 600s window
    assert is_flapping("src_1", "tpc_1", threshold=5, window_seconds=600, now=base) is False


def test_different_topic_same_source_independent() -> None:
    now = datetime.now(timezone.utc)
    for _ in range(5):
        record_state_change("src_1", "tpc_1", timestamp=now)
    assert is_flapping("src_1", "tpc_2", threshold=5, now=now) is False
