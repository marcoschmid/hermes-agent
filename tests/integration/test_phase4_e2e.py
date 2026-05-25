"""Phase-4 End-to-End integration tests (Track A2 + Track B callflows).

Exercises full callflow through real OutboxStore + NotificationWorker +
router_factory + TelegramCallbackReceiver + TelegramActionDispatcher with
only the external transports (requests.post, subprocess.run) mocked.

NOT tested here (separate suites):
- HTTP-route /telegram/webhook (test_telegram_webhook_route.py covers)
- Hub-Pipeline-Adapter-Logic (gateway/hub/* has own suites)
- Individual module behavior (per-module test files)

What these tests catch (vs unit-tests):
- Producer → outbox-row format compatible with worker's _payload_to_router_args
- Worker's claim_token wires through to mark_sent successfully
- Receiver → telegram_callbacks row format compatible with dispatcher's claim_due
- Schema-migration applied before producer/consumer use
- Cascade: hub-fail → mc-fail → direct-success end-to-end
"""
from __future__ import annotations

import json
import sqlite3
import subprocess
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from gateway.notification_worker import NotificationWorker
from gateway.outbox import OutboxStore
from gateway.outbox_cli import main as outbox_cli_main
from gateway.router_factory import make_default_router
from gateway.telegram_action_dispatcher import TelegramActionDispatcher
from gateway.telegram_gateway import TelegramCallbackReceiver


# ---- Helpers ---------------------------------------------------------------


def _hub_response_200(event_id: str = "evt-e2e") -> MagicMock:
    fake = MagicMock()
    fake.status_code = 200
    fake.json.return_value = {"data": {"event_id": event_id, "status": "delivered"}}
    return fake


def _mc_response_201(event_id: str = "mc-e2e") -> MagicMock:
    fake = MagicMock()
    fake.status_code = 201
    fake.json.return_value = {"data": {"event_id": event_id}}
    return fake


def _subprocess_success() -> subprocess.CompletedProcess:
    return subprocess.CompletedProcess(args=["bash"], returncode=0, stdout="", stderr="")


def _make_worker_router_factory(tmp_path: Path, fake_script: Path):
    """Return router_factory(channel) → make_default_router with all envs set."""
    def factory(channel: str):
        return make_default_router(
            source_slug=channel,
            target_chat_id="128314698",
            context=f"e2e-{channel}",
            hub_auth_mode="bearer",
            hub_token_env="HUB_PILOT_TOKEN",
            run_log_path=str(tmp_path / f"router-log-{channel}.jsonl"),
        )
    return factory


# ---- Track A2 End-to-End: CLI → Outbox → Worker → Router → mark_sent ------


def test_e2e_cli_enqueue_then_worker_dispatches_hub_success(tmp_path: Path, monkeypatch):
    """Bash-caller → CLI → outbox row → worker → router-hub → mark_sent."""
    outbox_db = tmp_path / "outbox.db"
    fake_script = tmp_path / "safe_telegram_send.sh"
    fake_script.write_text("#!/usr/bin/env bash\nexit 0\n")
    fake_script.chmod(0o755)

    monkeypatch.setenv("HUB_PILOT_TOKEN", "test-hub-token")
    monkeypatch.setenv("MC_HUB_TOKEN", "test-mc-token")
    monkeypatch.setenv("SAFE_TELEGRAM_SEND_SCRIPT", str(fake_script))

    # Producer: bash-caller via CLI
    rc = outbox_cli_main([
        "--channel", "telegram",
        "--target", "128314698",
        "--context", "e2e-cli",
        "--dedup-key", "e2e-cli-1",
        "--message", "E2E smoke message",
        "--db-path", str(outbox_db),
        "--quiet",
    ])
    assert rc == 0

    # Verify enqueued
    conn = sqlite3.connect(str(outbox_db))
    row = conn.execute("SELECT id, channel, status, payload_json FROM outbox").fetchone()
    conn.close()
    assert row is not None
    row_id, channel, status, payload_json = row
    assert channel == "telegram"
    assert status == "pending"
    payload = json.loads(payload_json)
    assert payload["message"] == "E2E smoke message"
    assert payload["target"] == "128314698"

    # Consumer: worker run_once
    outbox = OutboxStore(str(outbox_db))
    worker = NotificationWorker(
        outbox=outbox,
        router_factory=_make_worker_router_factory(tmp_path, fake_script),
    )

    with patch("gateway.router_factory.requests.post",
               return_value=_hub_response_200("evt-e2e-cli")) as mock_post:
        stats = worker.run_once()

    assert stats.claimed == 1
    assert stats.sent == 1
    assert mock_post.call_args.kwargs["headers"]["Authorization"] == "Bearer test-hub-token"

    # Verify row mark_sent
    conn = sqlite3.connect(str(outbox_db))
    refreshed = conn.execute("SELECT status, claim_token FROM outbox WHERE id=?", (row_id,)).fetchone()
    conn.close()
    assert refreshed[0] == "sent"
    assert refreshed[1] is None  # claim_token cleared after mark_sent


def test_e2e_cli_enqueue_worker_cascade_to_direct_on_hub_mc_fail(tmp_path: Path, monkeypatch):
    """Hub + MC both fail → cascade to direct (safe_telegram_send mock)."""
    outbox_db = tmp_path / "outbox.db"
    fake_script = tmp_path / "safe_telegram_send.sh"
    fake_script.write_text("#!/usr/bin/env bash\nexit 0\n")
    fake_script.chmod(0o755)

    monkeypatch.delenv("HUB_PILOT_TOKEN", raising=False)
    monkeypatch.delenv("MC_HUB_TOKEN", raising=False)
    monkeypatch.setenv("SAFE_TELEGRAM_SEND_SCRIPT", str(fake_script))

    # Enqueue
    rc = outbox_cli_main([
        "--channel", "telegram",
        "--target", "128314698",
        "--context", "e2e-cascade",
        "--dedup-key", "e2e-cascade-1",
        "--message", "cascade test",
        "--db-path", str(outbox_db),
        "--quiet",
    ])
    assert rc == 0

    # Worker dispatch — hub-token + mc-token missing → cascade to direct
    outbox = OutboxStore(str(outbox_db))
    worker = NotificationWorker(
        outbox=outbox,
        router_factory=_make_worker_router_factory(tmp_path, fake_script),
    )

    with patch("gateway.router_factory.subprocess.run",
               return_value=_subprocess_success()) as mock_run:
        stats = worker.run_once()

    assert stats.claimed == 1
    assert stats.sent == 1
    # Verify direct-sender invoked
    cmd = mock_run.call_args.args[0]
    assert "--target" in cmd and "128314698" in cmd
    assert "--message" in cmd and "cascade test" in cmd

    # Run-log captures all 3 hops
    run_log = tmp_path / "router-log-telegram.jsonl"
    assert run_log.exists()
    hops = [json.loads(l)["hop"] for l in run_log.read_text().strip().split("\n")]
    assert "hermes" in hops
    assert "mission-control" in hops
    assert "direct-fallback" in hops


def test_e2e_cli_dedupe_collision_second_call_skipped(tmp_path: Path, monkeypatch):
    """Same dedup_key from CLI twice → 2nd call's payload dropped, 1st delivered."""
    outbox_db = tmp_path / "outbox.db"

    # First call
    rc1 = outbox_cli_main([
        "--channel", "telegram",
        "--dedup-key", "e2e-dedupe-1",
        "--message", "FIRST",
        "--db-path", str(outbox_db),
        "--quiet",
    ])
    assert rc1 == 0

    # Second call same dedup_key
    rc2 = outbox_cli_main([
        "--channel", "telegram",
        "--dedup-key", "e2e-dedupe-1",
        "--message", "SECOND",
        "--db-path", str(outbox_db),
        "--quiet",
    ])
    assert rc2 == 0

    # Only 1 row, payload from FIRST
    conn = sqlite3.connect(str(outbox_db))
    rows = conn.execute("SELECT payload_json FROM outbox").fetchall()
    conn.close()
    assert len(rows) == 1
    assert json.loads(rows[0][0])["message"] == "FIRST"


def test_e2e_worker_retries_failed_dispatch_then_succeeds(tmp_path: Path, monkeypatch):
    """First worker-cycle fails (Hub 503), backoff bypassed, second succeeds."""
    outbox_db = tmp_path / "outbox.db"
    fake_script = tmp_path / "safe_telegram_send.sh"
    fake_script.write_text("#!/usr/bin/env bash\nexit 0\n")
    fake_script.chmod(0o755)

    monkeypatch.setenv("HUB_PILOT_TOKEN", "tok")
    monkeypatch.delenv("MC_HUB_TOKEN", raising=False)
    monkeypatch.setenv("SAFE_TELEGRAM_SEND_SCRIPT", str(fake_script))

    rc = outbox_cli_main([
        "--channel", "telegram",
        "--dedup-key", "e2e-retry-1",
        "--message", "retry test",
        "--db-path", str(outbox_db),
        "--quiet",
    ])
    assert rc == 0

    outbox = OutboxStore(str(outbox_db))
    worker = NotificationWorker(
        outbox=outbox,
        router_factory=_make_worker_router_factory(tmp_path, fake_script),
    )

    # Cycle 1: Hub-503 → MC missing → direct fails (mock subprocess fail)
    hub_fail = MagicMock(status_code=503, text="upstream down")
    direct_fail = subprocess.CompletedProcess(
        args=["bash"], returncode=1, stdout="", stderr="telegram unreachable",
    )
    with patch("gateway.router_factory.requests.post", return_value=hub_fail), \
         patch("gateway.router_factory.subprocess.run", return_value=direct_fail):
        stats1 = worker.run_once()
    assert stats1.failed == 1

    # Force row pending + bypass backoff
    conn = sqlite3.connect(str(outbox_db))
    conn.execute(
        "UPDATE outbox SET status='pending', next_retry_at=datetime('now', '-1 hour') "
        "WHERE dedup_key='e2e-retry-1'"
    )
    conn.commit()
    conn.close()

    # Cycle 2: Hub succeeds
    with patch("gateway.router_factory.requests.post", return_value=_hub_response_200()):
        stats2 = worker.run_once()
    assert stats2.sent == 1


# ---- Track B End-to-End: Webhook → Receiver → Dispatcher → handler --------


def test_e2e_webhook_callback_then_dispatcher_invokes_handler(tmp_path: Path):
    """Telegram webhook callback → receiver persist → dispatcher action-handler."""
    callbacks_db = tmp_path / "telegram_callbacks.db"
    receiver = TelegramCallbackReceiver(
        db_path=str(callbacks_db),
        webhook_secret="e2e-secret",
        allowed_user_ids={"128314698"},
        rate_limit_per_minute=60,
    )
    receiver.init_schema()

    # Producer: webhook arrives
    result = receiver.handle_webhook(
        headers={"X-Telegram-Bot-Api-Secret-Token": "e2e-secret"},
        payload={
            "update_id": 1001,
            "callback_query": {
                "id": "e2e-cb-1",
                "from": {"id": 128314698, "first_name": "Marco"},
                "message": {"chat": {"id": 128314698}, "message_id": 42},
                "data": "approve:ISS-E2E",
            },
        },
    )
    assert result.accepted is True
    assert result.callback_type == "approve"

    # Verify persisted with pending dispatch_status
    conn = sqlite3.connect(str(callbacks_db))
    row = conn.execute(
        "SELECT dispatch_status, issue_id FROM telegram_callbacks WHERE callback_id=?",
        ("e2e-cb-1",),
    ).fetchone()
    conn.close()
    assert row[0] == "pending"
    assert row[1] == "ISS-E2E"

    # Consumer: dispatcher
    approve_handler = MagicMock()
    dispatcher = TelegramActionDispatcher(
        db_path=str(callbacks_db),
        handlers={"approve": approve_handler, "reject": MagicMock(), "skip": MagicMock()},
    )

    stats = dispatcher.run_once()
    assert stats.claimed == 1
    assert stats.dispatched == 1
    approve_handler.assert_called_once()
    assert approve_handler.call_args.args[0] == "ISS-E2E"

    # Verify processed
    conn = sqlite3.connect(str(callbacks_db))
    refreshed = conn.execute(
        "SELECT dispatch_status, processed_at, claim_token "
        "FROM telegram_callbacks WHERE callback_id=?",
        ("e2e-cb-1",),
    ).fetchone()
    conn.close()
    assert refreshed[0] == "processed"
    assert refreshed[1] is not None
    assert refreshed[2] is None  # claim_token cleared


def test_e2e_webhook_unknown_callback_type_dispatcher_skips(tmp_path: Path):
    """Unknown callback_type → receiver persists ignored → dispatcher skips."""
    callbacks_db = tmp_path / "telegram_callbacks.db"
    receiver = TelegramCallbackReceiver(
        db_path=str(callbacks_db),
        webhook_secret="s",
        allowed_user_ids={"128314698"},
        rate_limit_per_minute=60,
    )
    receiver.init_schema()

    result = receiver.handle_webhook(
        headers={"X-Telegram-Bot-Api-Secret-Token": "s"},
        payload={
            "update_id": 2001,
            "callback_query": {
                "id": "e2e-unknown",
                "from": {"id": 128314698, "first_name": "Marco"},
                "message": {"chat": {"id": 128314698}, "message_id": 50},
                "data": "rare_action:X-99",
            },
        },
    )
    assert result.accepted is False
    assert result.ignored is True

    # Dispatcher must NOT process ignored rows
    handler = MagicMock()
    dispatcher = TelegramActionDispatcher(
        db_path=str(callbacks_db),
        handlers={"approve": handler, "reject": handler, "skip": handler},
    )
    stats = dispatcher.run_once()
    assert stats.claimed == 0
    handler.assert_not_called()


def test_e2e_webhook_then_dispatcher_handler_transient_failure_retries(tmp_path: Path):
    """Handler raises → dispatcher schedules retry → second cycle succeeds."""
    from datetime import datetime, timedelta, timezone

    callbacks_db = tmp_path / "telegram_callbacks.db"
    receiver = TelegramCallbackReceiver(
        db_path=str(callbacks_db),
        webhook_secret="s",
        allowed_user_ids={"128314698"},
        rate_limit_per_minute=60,
    )
    receiver.init_schema()
    receiver.handle_webhook(
        headers={"X-Telegram-Bot-Api-Secret-Token": "s"},
        payload={
            "update_id": 3001,
            "callback_query": {
                "id": "e2e-retry",
                "from": {"id": 128314698, "first_name": "Marco"},
                "message": {"chat": {"id": 128314698}, "message_id": 60},
                "data": "reject:ISS-RETRY",
            },
        },
    )

    call_count = {"n": 0}
    def flaky_handler(issue_id: str, row: dict) -> None:
        call_count["n"] += 1
        if call_count["n"] == 1:
            raise RuntimeError("paperclip transient")

    dispatcher = TelegramActionDispatcher(
        db_path=str(callbacks_db),
        handlers={"approve": MagicMock(), "reject": flaky_handler, "skip": MagicMock()},
    )

    now = datetime(2026, 5, 25, 12, 0, tzinfo=timezone.utc)

    # Cycle 1: fails, scheduled retry
    stats1 = dispatcher.run_once(now=now)
    assert stats1.failed == 1
    conn = sqlite3.connect(str(callbacks_db))
    r1 = conn.execute(
        "SELECT dispatch_status, attempts FROM telegram_callbacks WHERE callback_id=?",
        ("e2e-retry",),
    ).fetchone()
    conn.close()
    assert r1[0] == "pending"
    assert r1[1] == 1

    # Force next_retry_at past
    conn = sqlite3.connect(str(callbacks_db))
    conn.execute("UPDATE telegram_callbacks SET next_retry_at=? WHERE callback_id=?",
                 (now.isoformat(), "e2e-retry"))
    conn.commit()
    conn.close()

    # Cycle 2: succeeds
    stats2 = dispatcher.run_once(now=now + timedelta(minutes=10))
    assert stats2.dispatched == 1
    assert call_count["n"] == 2
