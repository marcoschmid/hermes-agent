"""Tests for scripts/hermes_health_observer (Phase-4 4c-2 decide-logic)."""
from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

import pytest

SCRIPT_PATH = Path(__file__).resolve().parents[2] / "scripts" / "hermes_health_observer.py"


@pytest.fixture
def observer_module(monkeypatch, tmp_path):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.setenv("PAPERCLIP_API_TOKEN", "tok")
    if "hermes_health_observer" in sys.modules:
        del sys.modules["hermes_health_observer"]
    spec = importlib.util.spec_from_file_location("hermes_health_observer", SCRIPT_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules["hermes_health_observer"] = module
    spec.loader.exec_module(module)
    return module


def test_decide_idle_with_short_history(observer_module):
    assert observer_module.decide(["healthy"], 4, 2) == "idle"
    assert observer_module.decide(["down"], 4, 2) == "idle"


def test_decide_fire_after_threshold_consecutive_non_healthy(observer_module):
    history = ["down", "down", "down", "down"]
    assert observer_module.decide(history, 4, 2) == "fire-fallback"


def test_decide_fire_with_mixed_degraded_and_down(observer_module):
    history = ["degraded", "down", "degraded", "down"]
    assert observer_module.decide(history, 4, 2) == "fire-fallback"


def test_decide_no_fire_when_healthy_in_window(observer_module):
    history = ["down", "down", "healthy", "down"]
    assert observer_module.decide(history, 4, 2) == "idle"


def test_decide_recover_after_threshold_consecutive_healthy(observer_module):
    history = ["healthy", "healthy"]
    assert observer_module.decide(history, 4, 2) == "recover"


def test_decide_fire_wins_over_recover_when_both_window_match(observer_module):
    history = ["down", "down", "down", "down", "healthy", "healthy"]
    assert observer_module.decide(history, 4, 2) == "recover"


def test_state_path_creates_dir(observer_module, tmp_path):
    p = observer_module._state_path()
    assert p.parent.is_dir()


def test_load_history_empty_initially(observer_module):
    assert observer_module._load_history() == []


def test_save_and_load_roundtrip(observer_module):
    from gateway.hermes_health_probe import HermesHealthSnapshot

    snap = HermesHealthSnapshot(
        state="healthy",
        pid=42,
        heartbeat_age_seconds=5.0,
        last_exit_code=0,
        status_file_age_seconds=1.0,
        reason="ok",
    )
    observer_module._save_history(["healthy", "healthy"], snap, "recover")
    assert observer_module._load_history() == ["healthy", "healthy"]


def test_corrupt_state_file_handled(observer_module):
    observer_module._state_path().write_text("not-json{{{")
    assert observer_module._load_history() == []


def test_max_history_truncation(observer_module):
    from gateway.hermes_health_probe import HermesHealthSnapshot

    snap = HermesHealthSnapshot(
        state="healthy",
        pid=42,
        heartbeat_age_seconds=5.0,
        last_exit_code=0,
        status_file_age_seconds=1.0,
        reason="ok",
    )
    observer_module._save_history(["x"] * 50, snap, "idle")
    loaded = observer_module._load_history()
    assert len(loaded) <= observer_module.MAX_HISTORY
