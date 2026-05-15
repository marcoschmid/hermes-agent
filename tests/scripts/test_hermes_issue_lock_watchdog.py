"""Tests for scripts/hermes_issue_lock_watchdog.py (Jarvis-OS Phase-4 4a-4 wave 4b)."""
from __future__ import annotations

import importlib.util
import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

SCRIPT_PATH = Path(__file__).resolve().parents[2] / "scripts" / "hermes_issue_lock_watchdog.py"


@pytest.fixture
def watchdog_module(monkeypatch, tmp_path):
    """Import the watchdog script as a module with isolated HERMES_HOME."""
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.setenv("PAPERCLIP_API_TOKEN", "tok")
    if "hermes_issue_lock_watchdog" in sys.modules:
        del sys.modules["hermes_issue_lock_watchdog"]
    spec = importlib.util.spec_from_file_location("hermes_issue_lock_watchdog", SCRIPT_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules["hermes_issue_lock_watchdog"] = module
    spec.loader.exec_module(module)
    return module


def test_state_path_creates_dir(watchdog_module, tmp_path):
    path = watchdog_module._state_path()
    assert path.parent.is_dir()
    assert path.name == "issue-lock-watchdog.json"


def test_load_history_empty_when_no_file(watchdog_module):
    assert watchdog_module._load_history() == []


def test_save_and_load_roundtrip(watchdog_module):
    history = [datetime.now(timezone.utc).isoformat()]
    watchdog_module._save_history(history)
    loaded = watchdog_module._load_history()
    assert loaded == history


def test_prune_history_drops_old_entries(watchdog_module):
    now = datetime.now(timezone.utc)
    history = [
        (now - timedelta(hours=2)).isoformat(),
        (now - timedelta(minutes=30)).isoformat(),
        now.isoformat(),
    ]
    cutoff = now - timedelta(hours=1)
    pruned = watchdog_module._prune_history(history, cutoff)
    assert len(pruned) == 2


def test_load_history_handles_corrupt_file(watchdog_module):
    path = watchdog_module._state_path()
    path.write_text("not-json{{{")
    assert watchdog_module._load_history() == []


def test_main_dry_run_does_not_persist_history(watchdog_module, monkeypatch):
    mock_client = MagicMock()
    mock_client.recover_stale.return_value = {
        "trigger": "watchdog",
        "dryRun": True,
        "candidates": [{"runId": "r1"}],
        "recovered": [],
    }
    monkeypatch.setattr(watchdog_module, "PaperclipIssueRunsClient", lambda: mock_client)
    monkeypatch.setattr(sys, "argv", ["watchdog", "--dry-run"])

    exit_code = watchdog_module.main()
    assert exit_code == 0
    assert not watchdog_module._state_path().exists()


def test_main_appends_history_on_recovery(watchdog_module, monkeypatch):
    mock_client = MagicMock()
    mock_client.recover_stale.return_value = {
        "trigger": "watchdog",
        "dryRun": False,
        "candidates": [{"runId": "r1"}],
        "recovered": [{"runId": "r1", "recoveredAt": datetime.now(timezone.utc).isoformat()}],
    }
    monkeypatch.setattr(watchdog_module, "PaperclipIssueRunsClient", lambda: mock_client)
    monkeypatch.setattr(sys, "argv", ["watchdog"])

    assert watchdog_module.main() == 0
    history = watchdog_module._load_history()
    assert len(history) == 1


def test_main_emits_alert_above_threshold(watchdog_module, monkeypatch):
    now = datetime.now(timezone.utc)
    watchdog_module._save_history([(now - timedelta(minutes=10)).isoformat()])

    mock_client = MagicMock()
    mock_client.recover_stale.return_value = {
        "trigger": "watchdog",
        "dryRun": False,
        "candidates": [{"runId": "r2"}],
        "recovered": [{"runId": "r2", "recoveredAt": now.isoformat()}],
    }
    monkeypatch.setattr(watchdog_module, "PaperclipIssueRunsClient", lambda: mock_client)
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "12345")
    monkeypatch.setattr(sys, "argv", ["watchdog"])

    alert_calls: list[tuple[str, str]] = []

    def fake_alert(message: str, chat_id: str) -> None:
        alert_calls.append((message, chat_id))

    monkeypatch.setattr(watchdog_module, "_send_alert", fake_alert)

    assert watchdog_module.main() == 0
    assert len(alert_calls) == 1
    assert "12345" == alert_calls[0][1]


def test_main_no_alert_below_threshold(watchdog_module, monkeypatch):
    mock_client = MagicMock()
    mock_client.recover_stale.return_value = {
        "trigger": "watchdog",
        "dryRun": False,
        "candidates": [{"runId": "r1"}],
        "recovered": [{"runId": "r1", "recoveredAt": datetime.now(timezone.utc).isoformat()}],
    }
    monkeypatch.setattr(watchdog_module, "PaperclipIssueRunsClient", lambda: mock_client)
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "12345")
    monkeypatch.setattr(sys, "argv", ["watchdog"])

    alert_calls: list[tuple[str, str]] = []

    def fake_alert(message: str, chat_id: str) -> None:
        alert_calls.append((message, chat_id))

    monkeypatch.setattr(watchdog_module, "_send_alert", fake_alert)

    assert watchdog_module.main() == 0
    assert alert_calls == []
