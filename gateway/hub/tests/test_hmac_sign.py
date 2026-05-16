"""Tests for gateway.hub.hmac_sign — sign + verify roundtrip + failure modes."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from gateway.hub.hmac_sign import HmacError, sign, verify


_NOW = datetime(2026, 5, 16, 15, 30, 0, tzinfo=timezone.utc)
_SECRET = b"test-secret-bytes-32-chars-long-x"


def _iso(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


def test_sign_deterministic() -> None:
    a = sign(_SECRET, _iso(_NOW), "n1", b"body")
    b = sign(_SECRET, _iso(_NOW), "n1", b"body")
    assert a == b
    assert len(a) == 64


def test_sign_diff_for_different_body() -> None:
    a = sign(_SECRET, _iso(_NOW), "n1", b"body-a")
    b = sign(_SECRET, _iso(_NOW), "n1", b"body-b")
    assert a != b


def test_verify_roundtrip_within_window() -> None:
    ts = _iso(_NOW)
    sig = sign(_SECRET, ts, "n2", b"hello")
    verify(_SECRET, ts, "n2", b"hello", sig, now=_NOW)


def test_verify_stale_timestamp_raises() -> None:
    ts = _iso(_NOW - timedelta(seconds=90))
    sig = sign(_SECRET, ts, "n3", b"hello")
    with pytest.raises(HmacError) as exc:
        verify(_SECRET, ts, "n3", b"hello", sig, now=_NOW, max_age_seconds=60)
    assert exc.value.error_code == "stale_timestamp"
    assert exc.value.status_code == 401


def test_verify_future_timestamp_raises() -> None:
    ts = _iso(_NOW + timedelta(seconds=90))
    sig = sign(_SECRET, ts, "n4", b"hello")
    with pytest.raises(HmacError) as exc:
        verify(_SECRET, ts, "n4", b"hello", sig, now=_NOW)
    assert exc.value.error_code == "stale_timestamp"


def test_verify_bad_signature_raises() -> None:
    ts = _iso(_NOW)
    with pytest.raises(HmacError) as exc:
        verify(_SECRET, ts, "n5", b"hello", "deadbeef" * 8, now=_NOW)
    assert exc.value.error_code == "bad_signature"


def test_verify_missing_headers_raises() -> None:
    with pytest.raises(HmacError) as exc:
        verify(_SECRET, "", "n6", b"hello", "sig", now=_NOW)
    assert exc.value.error_code == "missing_header"
    assert exc.value.status_code == 422


def test_verify_bad_timestamp_format_raises() -> None:
    with pytest.raises(HmacError) as exc:
        verify(_SECRET, "not-a-timestamp", "n7", b"hello", "sig", now=_NOW)
    assert exc.value.error_code == "bad_timestamp"
    assert exc.value.status_code == 422


def test_sign_diff_for_different_nonce() -> None:
    ts = _iso(_NOW)
    a = sign(_SECRET, ts, "n8a", b"body")
    b = sign(_SECRET, ts, "n8b", b"body")
    assert a != b


def test_sign_diff_for_different_secret() -> None:
    ts = _iso(_NOW)
    a = sign(b"secret-a", ts, "n9", b"body")
    b = sign(b"secret-b", ts, "n9", b"body")
    assert a != b
