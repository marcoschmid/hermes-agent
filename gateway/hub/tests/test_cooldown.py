"""Tests for gateway.hub.cooldown."""
import pytest
from datetime import datetime, timedelta, timezone

from gateway.hub.cooldown import (
    is_in_cooldown,
    record_cooldown,
    reset_cooldown_state,
)


@pytest.fixture(autouse=True)
def _reset() -> None:
    reset_cooldown_state()


def test_no_prior_record_not_in_cooldown() -> None:
    assert is_in_cooldown("src_1", "chn_1", "tpc_1") is False


def test_after_record_is_in_cooldown() -> None:
    now = datetime.now(timezone.utc)
    record_cooldown("src_1", "chn_1", "tpc_1", now=now)
    assert is_in_cooldown("src_1", "chn_1", "tpc_1", now=now) is True


def test_after_cooldown_expires_returns_false() -> None:
    now = datetime.now(timezone.utc)
    record_cooldown("src_1", "chn_1", "tpc_1", now=now)
    later = now + timedelta(seconds=61)
    assert is_in_cooldown("src_1", "chn_1", "tpc_1", cooldown_seconds=60, now=later) is False


def test_different_channel_independent() -> None:
    now = datetime.now(timezone.utc)
    record_cooldown("src_1", "chn_1", "tpc_1", now=now)
    assert is_in_cooldown("src_1", "chn_2", "tpc_1", now=now) is False


def test_different_source_independent() -> None:
    now = datetime.now(timezone.utc)
    record_cooldown("src_1", "chn_1", "tpc_1", now=now)
    assert is_in_cooldown("src_2", "chn_1", "tpc_1", now=now) is False
