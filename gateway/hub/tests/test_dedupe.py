"""Tests for gateway.hub.dedupe."""
import pytest
from datetime import datetime, timedelta, timezone

from gateway.hub.dedupe import check_dedup, reset_dedupe_state, DEFAULT_WINDOW_SECONDS


@pytest.fixture(autouse=True)
def _reset() -> None:
    reset_dedupe_state()


def test_first_event_not_duplicate() -> None:
    result = check_dedup("src_1", "tpc_1", "key_a")
    assert result.is_duplicate is False
    assert result.count == 1


def test_second_event_within_window_is_duplicate() -> None:
    check_dedup("src_1", "tpc_1", "key_a")
    result = check_dedup("src_1", "tpc_1", "key_a")
    assert result.is_duplicate is True
    assert result.count == 2


def test_window_expired_resets() -> None:
    now = datetime.now(timezone.utc)
    check_dedup("src_1", "tpc_1", "key_a", window_seconds=10, now=now)
    later = now + timedelta(seconds=15)
    result = check_dedup("src_1", "tpc_1", "key_a", window_seconds=10, now=later)
    assert result.is_duplicate is False
    assert result.count == 1


def test_null_dedupe_key_never_duplicate() -> None:
    check_dedup("src_1", "tpc_1", None)
    result = check_dedup("src_1", "tpc_1", None)
    assert result.is_duplicate is False


def test_different_dedupe_keys_independent() -> None:
    check_dedup("src_1", "tpc_1", "key_a")
    result = check_dedup("src_1", "tpc_1", "key_b")
    assert result.is_duplicate is False


def test_same_key_different_source_independent() -> None:
    check_dedup("src_1", "tpc_1", "key_a")
    result = check_dedup("src_2", "tpc_1", "key_a")
    assert result.is_duplicate is False


def test_count_increments_on_each_duplicate() -> None:
    for _ in range(5):
        check_dedup("src_1", "tpc_1", "key_a")
    result = check_dedup("src_1", "tpc_1", "key_a")
    assert result.count == 6
