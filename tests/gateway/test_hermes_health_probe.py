"""Tests for gateway.hermes_health_probe (Jarvis-OS Phase-4 4c-1)."""
from __future__ import annotations

import json
import os
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from gateway.hermes_health_probe import (
    DEFAULT_FRESH_HEARTBEAT_SECONDS,
    DEFAULT_STALE_HEARTBEAT_SECONDS,
    HermesHealthSnapshot,
    probe_hermes_health,
)


def _write_status(tmp_path: Path, payload: dict, mtime_offset_seconds: float = 0) -> Path:
    path = tmp_path / "gateway-status.json"
    path.write_text(json.dumps(payload))
    if mtime_offset_seconds:
        new_mtime = time.time() + mtime_offset_seconds
        os.utime(path, (new_mtime, new_mtime))
    return path


def _iso(seconds_ago: float) -> str:
    return (datetime.now(timezone.utc) - timedelta(seconds=seconds_ago)).isoformat()


def test_state_down_when_no_status_file(tmp_path):
    snap = probe_hermes_health(status_path=tmp_path / "nope.json")
    assert snap.state == "down"
    assert "missing" in snap.reason.lower()


def test_state_down_when_file_stale(tmp_path):
    path = _write_status(
        tmp_path,
        {"pid": os.getpid(), "heartbeat_at": _iso(10)},
        mtime_offset_seconds=-(DEFAULT_STALE_HEARTBEAT_SECONDS + 10),
    )
    snap = probe_hermes_health(status_path=path)
    assert snap.state == "down"
    assert "older than" in snap.reason


def test_state_down_when_pid_not_running(tmp_path):
    payload = {"pid": 999_999_998, "heartbeat_at": _iso(10)}
    path = _write_status(tmp_path, payload)
    snap = probe_hermes_health(status_path=path)
    assert snap.state == "down"
    assert "not running" in snap.reason


def test_state_down_when_heartbeat_stale(tmp_path):
    payload = {"pid": os.getpid(), "heartbeat_at": _iso(DEFAULT_STALE_HEARTBEAT_SECONDS + 5)}
    path = _write_status(tmp_path, payload)
    snap = probe_hermes_health(status_path=path)
    assert snap.state == "down"
    assert "heartbeat stale" in snap.reason


def test_state_degraded_when_heartbeat_aging(tmp_path):
    payload = {"pid": os.getpid(), "heartbeat_at": _iso(DEFAULT_FRESH_HEARTBEAT_SECONDS + 10)}
    path = _write_status(tmp_path, payload)
    snap = probe_hermes_health(status_path=path)
    assert snap.state == "degraded"
    assert "heartbeat aged" in snap.reason


def test_state_degraded_when_last_exit_nonzero(tmp_path):
    payload = {"pid": os.getpid(), "heartbeat_at": _iso(10), "last_exit_code": 137}
    path = _write_status(tmp_path, payload)
    snap = probe_hermes_health(status_path=path)
    assert snap.state == "degraded"
    assert "last_exit_code=137" in snap.reason


def test_state_degraded_when_no_heartbeat_field(tmp_path):
    payload = {"pid": os.getpid()}
    path = _write_status(tmp_path, payload)
    snap = probe_hermes_health(status_path=path)
    assert snap.state == "degraded"
    assert "heartbeat_at missing" in snap.reason


def test_state_healthy_when_all_signals_green(tmp_path):
    payload = {
        "pid": os.getpid(),
        "heartbeat_at": _iso(5),
        "last_exit_code": 0,
    }
    path = _write_status(tmp_path, payload)
    snap = probe_hermes_health(status_path=path)
    assert snap.state == "healthy"
    assert snap.heartbeat_age_seconds is not None
    assert snap.heartbeat_age_seconds < DEFAULT_FRESH_HEARTBEAT_SECONDS


def test_snapshot_to_dict_round_trip():
    snap = HermesHealthSnapshot(
        state="healthy",
        pid=42,
        heartbeat_age_seconds=5.0,
        last_exit_code=0,
        status_file_age_seconds=1.0,
        reason="ok",
    )
    d = snap.to_dict()
    assert d["state"] == "healthy"
    assert d["pid"] == 42
    assert d["reason"] == "ok"


def test_corrupt_status_file_returns_down(tmp_path):
    path = tmp_path / "gateway-status.json"
    path.write_text("not-json{{{")
    snap = probe_hermes_health(status_path=path)
    assert snap.state == "down"


def test_custom_thresholds(tmp_path):
    payload = {"pid": os.getpid(), "heartbeat_at": _iso(45)}
    path = _write_status(tmp_path, payload)
    # tight threshold: 45s should be degraded
    snap = probe_hermes_health(status_path=path, fresh_heartbeat_seconds=30, stale_heartbeat_seconds=120)
    assert snap.state == "degraded"
    # loose threshold: 45s should be healthy
    snap2 = probe_hermes_health(status_path=path, fresh_heartbeat_seconds=60, stale_heartbeat_seconds=300)
    assert snap2.state == "healthy"
