"""Contract tests for gateway._sqlite_helpers shared module."""
from __future__ import annotations

import sqlite3
import time
from datetime import datetime, timezone
from unittest.mock import patch

import pytest

from gateway._sqlite_helpers import iso, retry_on_locked, utcnow


# ---- retry_on_locked --------------------------------------------------------


def test_retry_on_locked_passes_through_success():
    @retry_on_locked
    def ok():
        return "result"

    assert ok() == "result"


def test_retry_on_locked_retries_on_database_locked_then_succeeds():
    attempts = {"n": 0}

    @retry_on_locked
    def flaky():
        attempts["n"] += 1
        if attempts["n"] < 3:
            raise sqlite3.OperationalError("database is locked")
        return "success-after-retry"

    with patch("gateway._sqlite_helpers.time.sleep"):
        result = flaky()

    assert result == "success-after-retry"
    assert attempts["n"] == 3


def test_retry_on_locked_reraises_after_max_retries():
    @retry_on_locked
    def always_locked():
        raise sqlite3.OperationalError("database is locked")

    with patch("gateway._sqlite_helpers.time.sleep"):
        with pytest.raises(sqlite3.OperationalError, match="locked"):
            always_locked()


def test_retry_on_locked_does_not_retry_other_operational_errors():
    @retry_on_locked
    def syntax_error():
        raise sqlite3.OperationalError("near 'SELEKT': syntax error")

    with pytest.raises(sqlite3.OperationalError, match="syntax"):
        syntax_error()


def test_retry_on_locked_custom_max_retries():
    attempts = {"n": 0}

    def flaky():
        attempts["n"] += 1
        raise sqlite3.OperationalError("database is locked")

    wrapped = retry_on_locked(flaky, max_retries=2)
    with patch("gateway._sqlite_helpers.time.sleep"):
        with pytest.raises(sqlite3.OperationalError):
            wrapped()
    assert attempts["n"] == 2


# ---- utcnow -----------------------------------------------------------------


def test_utcnow_returns_timezone_aware():
    now = utcnow()
    assert now.tzinfo is not None
    assert now.tzinfo == timezone.utc


# ---- iso --------------------------------------------------------------------


def test_iso_aware_datetime_returns_iso_string():
    dt = datetime(2026, 5, 25, 12, 0, tzinfo=timezone.utc)
    assert iso(dt) == "2026-05-25T12:00:00+00:00"


def test_iso_naive_strict_raises():
    naive = datetime(2026, 5, 25, 12, 0)
    with pytest.raises(ValueError, match="timezone-aware"):
        iso(naive)


def test_iso_naive_lenient_coerces_to_utc():
    naive = datetime(2026, 5, 25, 12, 0)
    assert iso(naive, strict_aware=False) == "2026-05-25T12:00:00+00:00"


def test_iso_non_utc_timezone_converts_to_utc():
    from datetime import timedelta
    cet = timezone(timedelta(hours=2))
    dt_cet = datetime(2026, 5, 25, 14, 0, tzinfo=cet)
    # 14:00 CET = 12:00 UTC
    assert iso(dt_cet) == "2026-05-25T12:00:00+00:00"
