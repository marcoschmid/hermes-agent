"""Tests for paperclip notify SQLite dedupe layer."""
import sqlite3

import pytest

from gateway.paperclip_notify_dedupe import Dedupe


@pytest.fixture
def db_path(tmp_path):
    return str(tmp_path / "dedupe.db")


def test_first_call_returns_send(db_path):
    d = Dedupe(db_path)
    assert d.should_send("check1", "hash1", "ok", "ok") is True


def test_second_call_same_hash_returns_skip(db_path):
    d = Dedupe(db_path)
    d.record("check1", "hash1")
    assert d.should_send("check1", "hash1", "ok", "ok") is False


def test_state_change_overrides_dedupe(db_path):
    d = Dedupe(db_path)
    d.record("check1", "hash1")
    assert d.should_send("check1", "hash1", "ok", "warn") is True


def test_previous_status_none_uses_hash_lookup(db_path):
    d = Dedupe(db_path)
    d.record("check1", "hash1")
    assert d.should_send("check1", "hash1", None, "ok") is False
    assert d.should_send("check1", "hash2", None, "ok") is True


class _BoomConn:
    """Stand-in connection whose execute() always raises DatabaseError."""

    def execute(self, *a, **k):
        raise sqlite3.DatabaseError("disk image malformed")

    def commit(self):
        pass


def test_corruption_fallback_returns_send(db_path):
    d = Dedupe(db_path)
    d._conn = _BoomConn()
    assert d.should_send("check1", "hash1", "ok", "ok") is True


def test_record_failure_does_not_raise(db_path):
    d = Dedupe(db_path)
    d._conn = _BoomConn()
    d.record("check1", "hash1")


def test_creates_parent_directory(tmp_path):
    nested = tmp_path / "deep" / "nest" / "dedupe.db"
    Dedupe(str(nested))
    assert nested.parent.exists()
