"""Contract tests for gateway.router_factory builders."""
from __future__ import annotations

import json
import subprocess
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from gateway.router_factory import (
    DEFAULT_HUB_URL,
    DEFAULT_MC_URL,
    make_default_router,
    make_direct_sender,
    make_hub_sender,
    make_mc_sender,
)


# ---- Hub sender ------------------------------------------------------------


def test_hub_sender_bearer_success_returns_ok_with_event_id():
    fake_response = MagicMock()
    fake_response.status_code = 200
    fake_response.json.return_value = {"data": {"event_id": "evt-42", "status": "delivered"}}

    sender = make_hub_sender(
        hub_url="http://hub.test:8766",
        auth_mode="bearer",
        bearer_token="tok-123",
        source_slug="weekly-preview",
    )

    with patch("gateway.router_factory.requests.post", return_value=fake_response) as mock_post:
        result = sender("hello", issue={"id": "wk-2026-22", "title": "Wochenvorschau"})

    assert result["ok"] is True
    assert result["event_id"] == "evt-42"
    mock_post.assert_called_once()
    call_kwargs = mock_post.call_args.kwargs
    assert call_kwargs["headers"]["Authorization"] == "Bearer tok-123"
    body = json.loads(call_kwargs["data"].decode("utf-8"))
    assert body["source_slug"] == "weekly-preview"
    assert body["dedupe_key"] == "wk-2026-22"
    assert body["body"] == "hello"


def test_hub_sender_missing_bearer_token_returns_not_ok_without_http_call():
    sender = make_hub_sender(
        hub_url="http://hub.test:8766",
        auth_mode="bearer",
        bearer_token=None,
        source_slug="weekly-preview",
    )

    with patch("gateway.router_factory.requests.post") as mock_post:
        result = sender("hello", issue={"id": "wk-2026-22"})

    assert result["ok"] is False
    assert "bearer_token missing" in result["error"]
    mock_post.assert_not_called()


def test_hub_sender_non_2xx_returns_not_ok_with_status_code():
    fake_response = MagicMock()
    fake_response.status_code = 503
    fake_response.text = "service unavailable"

    sender = make_hub_sender(
        hub_url="http://hub.test:8766",
        auth_mode="bearer",
        bearer_token="tok-123",
        source_slug="weekly-preview",
    )

    with patch("gateway.router_factory.requests.post", return_value=fake_response):
        result = sender("hello", issue={"id": "wk-2026-22"})

    assert result["ok"] is False
    assert result["status_code"] == 503


def test_hub_sender_hmac_mode_signs_request_with_x_hub_headers():
    fake_response = MagicMock()
    fake_response.status_code = 200
    fake_response.json.return_value = {"data": {"event_id": "evt-9"}}

    sender = make_hub_sender(
        hub_url="http://hub.test:8766",
        auth_mode="hmac",
        hmac_secret="s3cr3t-key",
        source_slug="weekly-preview",
    )

    with patch("gateway.router_factory.requests.post", return_value=fake_response) as mock_post:
        result = sender("hello", issue={"id": "wk-2026-22"})

    assert result["ok"] is True
    headers = mock_post.call_args.kwargs["headers"]
    assert "Authorization" not in headers
    assert headers["X-Hub-Timestamp"]
    assert headers["X-Hub-Nonce"]
    assert headers["X-Hub-Signature"]
    assert len(headers["X-Hub-Signature"]) == 64  # SHA256 hex


# ---- MC sender -------------------------------------------------------------


def test_mc_sender_201_response_treated_as_ok():
    fake_response = MagicMock()
    fake_response.status_code = 201
    fake_response.json.return_value = {"data": {"event_id": "mc-evt-7"}}

    sender = make_mc_sender(
        mc_url="http://mc.test:3334",
        bearer_token="mc-tok",
        source_slug="weekly-preview",
    )

    with patch("gateway.router_factory.requests.post", return_value=fake_response) as mock_post:
        result = sender("hello", issue={"id": "wk-2026-22", "title": "Wochenvorschau"})

    assert result["ok"] is True
    assert result["event_id"] == "mc-evt-7"
    call_url = mock_post.call_args.args[0]
    assert call_url == "http://mc.test:3334/api/board/notifications/events"


def test_mc_sender_missing_token_returns_not_ok():
    sender = make_mc_sender(
        mc_url="http://mc.test:3334",
        bearer_token=None,
        source_slug="weekly-preview",
    )

    result = sender("hello", issue={"id": "wk-2026-22"})

    assert result["ok"] is False
    assert "bearer_token missing" in result["error"]


# ---- Direct sender ---------------------------------------------------------


def test_direct_sender_subprocess_invokes_safe_telegram_send(tmp_path):
    fake_script = tmp_path / "safe_telegram_send.sh"
    fake_script.write_text("#!/usr/bin/env bash\nexit 0\n")
    fake_script.chmod(0o755)

    sender = make_direct_sender(
        script_path=str(fake_script),
        target_chat_id="128314698",
        context="weekly-preview",
    )

    fake_result = subprocess.CompletedProcess(
        args=["bash", str(fake_script)], returncode=0, stdout="", stderr="",
    )
    with patch("gateway.router_factory.subprocess.run", return_value=fake_result) as mock_run:
        result = sender("hello", issue={"id": "wk-2026-22"})

    assert result["ok"] is True
    cmd = mock_run.call_args.args[0]
    assert cmd[0] == "bash"
    assert cmd[1] == str(fake_script)
    assert "--target" in cmd and "128314698" in cmd
    assert "--context" in cmd and "weekly-preview" in cmd
    assert "--message" in cmd and "hello" in cmd
    assert "--dedupe-key" in cmd and "wk-2026-22" in cmd


def test_direct_sender_missing_script_returns_not_ok(tmp_path):
    sender = make_direct_sender(
        script_path=str(tmp_path / "nonexistent.sh"),
        target_chat_id="128314698",
        context="weekly-preview",
    )

    result = sender("hello", issue={"id": "wk-2026-22"})

    assert result["ok"] is False
    assert "missing" in result["error"]


def test_direct_sender_subprocess_failure_returns_returncode(tmp_path):
    fake_script = tmp_path / "safe_telegram_send.sh"
    fake_script.write_text("#!/usr/bin/env bash\nexit 7\n")
    fake_script.chmod(0o755)

    sender = make_direct_sender(
        script_path=str(fake_script),
        target_chat_id="128314698",
        context="weekly-preview",
    )

    fake_result = subprocess.CompletedProcess(
        args=["bash", str(fake_script)], returncode=7, stdout="", stderr="telegram api 500",
    )
    with patch("gateway.router_factory.subprocess.run", return_value=fake_result):
        result = sender("hello", issue={"id": "wk-2026-22"})

    assert result["ok"] is False
    assert result["returncode"] == 7
    assert "telegram api 500" in result["error"]


# ---- Default router builder ------------------------------------------------


def test_make_default_router_constructs_3_stage_router_from_env(monkeypatch, tmp_path):
    fake_script = tmp_path / "safe_telegram_send.sh"
    fake_script.write_text("#!/usr/bin/env bash\nexit 0\n")
    fake_script.chmod(0o755)
    run_log = tmp_path / "router-run-log.jsonl"

    monkeypatch.setenv("HERMES_HUB_BEARER_TOKEN", "hub-tok")
    monkeypatch.setenv("MC_HUB_TOKEN", "mc-tok")
    monkeypatch.setenv("SAFE_TELEGRAM_SEND_SCRIPT", str(fake_script))

    router = make_default_router(
        source_slug="weekly-preview",
        target_chat_id="128314698",
        context="weekly-preview",
        run_log_path=str(run_log),
    )

    # Verify Router-Wiring: success on hub stage uses our env-token
    fake_response = MagicMock()
    fake_response.status_code = 200
    fake_response.json.return_value = {"data": {"event_id": "evt-end-to-end"}}
    with patch("gateway.router_factory.requests.post", return_value=fake_response) as mock_post:
        result = router.send(message="hello", issue={"id": "wk-2026-22", "title": "T"})

    assert result.ok is True
    assert result.hop == "hermes"
    assert mock_post.call_args.kwargs["headers"]["Authorization"] == "Bearer hub-tok"
    assert run_log.exists()
    log_lines = run_log.read_text().strip().split("\n")
    assert len(log_lines) == 1
    assert json.loads(log_lines[0])["hop"] == "hermes"


def test_make_default_router_missing_tokens_cascades_to_direct(monkeypatch, tmp_path):
    fake_script = tmp_path / "safe_telegram_send.sh"
    fake_script.write_text("#!/usr/bin/env bash\nexit 0\n")
    fake_script.chmod(0o755)
    run_log = tmp_path / "router-run-log.jsonl"

    monkeypatch.delenv("HERMES_HUB_BEARER_TOKEN", raising=False)
    monkeypatch.delenv("MC_HUB_TOKEN", raising=False)
    monkeypatch.setenv("SAFE_TELEGRAM_SEND_SCRIPT", str(fake_script))

    router = make_default_router(
        source_slug="weekly-preview",
        target_chat_id="128314698",
        context="weekly-preview",
        run_log_path=str(run_log),
    )

    fake_result = subprocess.CompletedProcess(
        args=["bash", str(fake_script)], returncode=0, stdout="", stderr="",
    )
    with patch("gateway.router_factory.subprocess.run", return_value=fake_result):
        result = router.send(message="hello", issue={"id": "wk-2026-22"})

    assert result.ok is True
    assert result.hop == "direct-fallback"
    log_lines = run_log.read_text().strip().split("\n")
    assert len(log_lines) == 3
    hops = [json.loads(line)["hop"] for line in log_lines]
    assert hops == ["hermes", "mission-control", "direct-fallback"]
