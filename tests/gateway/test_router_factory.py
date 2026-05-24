"""Contract tests for gateway.router_factory builders."""
from __future__ import annotations

import json
import subprocess
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from gateway.router_factory import (
    BODY_TRUNCATION_MARKER,
    DEFAULT_ALLOWED_HOSTS,
    DEFAULT_HUB_URL,
    DEFAULT_MC_URL,
    NOTIFICATION_BODY_MAX_CHARS,
    make_default_router,
    make_direct_sender,
    make_hub_sender,
    make_mc_sender,
)


LOOPBACK_HUB = "http://127.0.0.1:8766"
LOOPBACK_MC = "http://127.0.0.1:3334"


def _hub_success(event_id: str = "evt-42", status: str = "delivered") -> MagicMock:
    fake = MagicMock()
    fake.status_code = 200
    fake.json.return_value = {"data": {"event_id": event_id, "status": status}}
    return fake


def _mc_success(event_id: str = "mc-evt-7") -> MagicMock:
    fake = MagicMock()
    fake.status_code = 201
    fake.json.return_value = {"data": {"event_id": event_id}}
    return fake


# ---- Hub sender ------------------------------------------------------------


def test_hub_sender_bearer_success_returns_ok_with_event_id():
    sender = make_hub_sender(
        hub_url=LOOPBACK_HUB,
        auth_mode="bearer",
        bearer_token="tok-123",
        source_slug="weekly-preview",
    )

    with patch("gateway.router_factory.requests.post", return_value=_hub_success()) as mock_post:
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
        hub_url=LOOPBACK_HUB,
        auth_mode="bearer",
        bearer_token=None,
        source_slug="weekly-preview",
    )

    with patch("gateway.router_factory.requests.post") as mock_post:
        result = sender("hello", issue={"id": "wk-2026-22"})

    assert result["ok"] is False
    assert "bearer token missing" in result["error"]
    mock_post.assert_not_called()


def test_hub_sender_non_2xx_returns_not_ok_with_status_code():
    fake_response = MagicMock()
    fake_response.status_code = 503
    fake_response.text = "service unavailable"

    sender = make_hub_sender(
        hub_url=LOOPBACK_HUB,
        auth_mode="bearer",
        bearer_token="tok-123",
        source_slug="weekly-preview",
    )

    with patch("gateway.router_factory.requests.post", return_value=fake_response):
        result = sender("hello", issue={"id": "wk-2026-22"})

    assert result["ok"] is False
    assert result["status_code"] == 503


def test_hub_sender_hmac_mode_signs_request_with_x_hub_headers():
    sender = make_hub_sender(
        hub_url=LOOPBACK_HUB,
        auth_mode="hmac",
        hmac_secret="s3cr3t-key",
        source_slug="weekly-preview",
    )

    with patch("gateway.router_factory.requests.post", return_value=_hub_success(event_id="evt-9")) as mock_post:
        result = sender("hello", issue={"id": "wk-2026-22"})

    assert result["ok"] is True
    headers = mock_post.call_args.kwargs["headers"]
    assert "Authorization" not in headers
    assert headers["X-Hub-Timestamp"]
    assert headers["X-Hub-Nonce"]
    assert headers["X-Hub-Signature"]
    assert len(headers["X-Hub-Signature"]) == 64  # SHA256 hex


def test_hub_sender_rejects_non_loopback_url_at_build_time():
    with pytest.raises(ValueError, match="not in allowlist"):
        make_hub_sender(
            hub_url="http://attacker.example.com:8766",
            auth_mode="bearer",
            bearer_token="tok-123",
            source_slug="weekly-preview",
        )


def test_hub_sender_accepts_allowed_hosts_override():
    sender = make_hub_sender(
        hub_url="http://internal-hub.lan:8766",
        auth_mode="bearer",
        bearer_token="tok-123",
        source_slug="weekly-preview",
        allowed_hosts=frozenset({"internal-hub.lan"}),
    )

    with patch("gateway.router_factory.requests.post", return_value=_hub_success()):
        result = sender("hello", issue={"id": "wk-1"})
    assert result["ok"] is True


def test_hub_sender_truncates_body_above_4000_chars():
    long_body = "ä" * 5000  # German umlaut to stress UTF-8
    sender = make_hub_sender(
        hub_url=LOOPBACK_HUB,
        auth_mode="bearer",
        bearer_token="tok",
        source_slug="weekly-preview",
    )

    with patch("gateway.router_factory.requests.post", return_value=_hub_success()) as mock_post:
        sender(long_body, issue={"id": "wk-1"})

    sent_body = json.loads(mock_post.call_args.kwargs["data"].decode("utf-8"))
    assert len(sent_body["body"]) <= NOTIFICATION_BODY_MAX_CHARS
    assert sent_body["body"].endswith(BODY_TRUNCATION_MARKER)


def test_hub_sender_resolves_token_env_at_send_time(monkeypatch):
    """Token rotation post-construction must be observed (no build-time cache)."""
    sender = make_hub_sender(
        hub_url=LOOPBACK_HUB,
        auth_mode="bearer",
        bearer_token_env="ROTATING_HUB_TOKEN",
        source_slug="weekly-preview",
    )

    monkeypatch.delenv("ROTATING_HUB_TOKEN", raising=False)
    result = sender("hello", issue={"id": "wk-1"})
    assert result["ok"] is False
    assert "bearer token missing" in result["error"]

    monkeypatch.setenv("ROTATING_HUB_TOKEN", "rotated-tok-v2")
    with patch("gateway.router_factory.requests.post", return_value=_hub_success()) as mock_post:
        result = sender("hello", issue={"id": "wk-1"})
    assert result["ok"] is True
    assert mock_post.call_args.kwargs["headers"]["Authorization"] == "Bearer rotated-tok-v2"


def test_hub_sender_2xx_non_json_returns_not_ok():
    fake_response = MagicMock()
    fake_response.status_code = 200
    fake_response.json.side_effect = ValueError("not JSON")
    fake_response.text = "<html>error page</html>"

    sender = make_hub_sender(
        hub_url=LOOPBACK_HUB,
        auth_mode="bearer",
        bearer_token="tok",
        source_slug="weekly-preview",
    )

    with patch("gateway.router_factory.requests.post", return_value=fake_response):
        result = sender("hello", issue={"id": "wk-1"})

    assert result["ok"] is False
    assert "not JSON" in result["error"]


def test_hub_sender_2xx_missing_event_id_returns_not_ok():
    fake_response = MagicMock()
    fake_response.status_code = 200
    fake_response.json.return_value = {"data": {}}  # no event_id

    sender = make_hub_sender(
        hub_url=LOOPBACK_HUB,
        auth_mode="bearer",
        bearer_token="tok",
        source_slug="weekly-preview",
    )

    with patch("gateway.router_factory.requests.post", return_value=fake_response):
        result = sender("hello", issue={"id": "wk-1"})

    assert result["ok"] is False
    assert "missing data.event_id" in result["error"]


# ---- MC sender -------------------------------------------------------------


def test_mc_sender_201_response_treated_as_ok():
    sender = make_mc_sender(
        mc_url=LOOPBACK_MC,
        bearer_token="mc-tok",
        source_slug="weekly-preview",
    )

    with patch("gateway.router_factory.requests.post", return_value=_mc_success()) as mock_post:
        result = sender("hello", issue={"id": "wk-2026-22", "title": "Wochenvorschau"})

    assert result["ok"] is True
    assert result["event_id"] == "mc-evt-7"
    call_url = mock_post.call_args.args[0]
    assert call_url == f"{LOOPBACK_MC}/api/board/notifications/events"


def test_mc_sender_missing_token_returns_not_ok():
    sender = make_mc_sender(
        mc_url=LOOPBACK_MC,
        bearer_token=None,
        source_slug="weekly-preview",
    )

    result = sender("hello", issue={"id": "wk-2026-22"})

    assert result["ok"] is False
    assert "bearer_token missing" in result["error"]


def test_mc_sender_rejects_non_loopback_url_at_build_time():
    with pytest.raises(ValueError, match="not in allowlist"):
        make_mc_sender(
            mc_url="http://attacker.example.com:3334",
            bearer_token="mc-tok",
            source_slug="weekly-preview",
        )


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


def test_direct_sender_always_passes_dedupe_key_even_without_issue_id(tmp_path):
    """Round-2 fix: deterministic dedupe via hash-fallback when issue.id missing."""
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
        sender("hello world", issue=None)

    cmd = mock_run.call_args.args[0]
    assert "--dedupe-key" in cmd
    dedupe_idx = cmd.index("--dedupe-key") + 1
    assert len(cmd[dedupe_idx]) == 32  # SHA256-32 hex prefix


def test_direct_sender_whitespace_id_falls_back_to_hash(tmp_path):
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
        sender("hello", issue={"id": "   "})  # whitespace-only

    cmd = mock_run.call_args.args[0]
    dedupe_idx = cmd.index("--dedupe-key") + 1
    assert cmd[dedupe_idx] != "   "
    assert len(cmd[dedupe_idx]) == 32


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

    with patch("gateway.router_factory.requests.post", return_value=_hub_success(event_id="evt-end-to-end")) as mock_post:
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


def test_make_default_router_token_rotation_observed(monkeypatch, tmp_path):
    """LaunchAgent processes can pick up rotated tokens without restart."""
    fake_script = tmp_path / "safe_telegram_send.sh"
    fake_script.write_text("#!/usr/bin/env bash\nexit 0\n")
    fake_script.chmod(0o755)
    run_log = tmp_path / "router-run-log.jsonl"

    monkeypatch.delenv("HERMES_HUB_BEARER_TOKEN", raising=False)
    monkeypatch.setenv("MC_HUB_TOKEN", "mc-old")
    monkeypatch.setenv("SAFE_TELEGRAM_SEND_SCRIPT", str(fake_script))

    router = make_default_router(
        source_slug="weekly-preview",
        target_chat_id="128314698",
        context="weekly-preview",
        run_log_path=str(run_log),
    )

    # First send: no hub-token -> degrades to MC.
    with patch("gateway.router_factory.requests.post", return_value=_mc_success(event_id="mc-1")):
        result1 = router.send(message="m1", issue={"id": "wk-1"})
    assert result1.hop == "mission-control"

    # Now Hub-token appears mid-process — rotate-in.
    monkeypatch.setenv("HERMES_HUB_BEARER_TOKEN", "hub-rotated")
    with patch("gateway.router_factory.requests.post", return_value=_hub_success(event_id="evt-2")) as mock_post:
        result2 = router.send(message="m2", issue={"id": "wk-2"})
    assert result2.hop == "hermes"
    assert mock_post.call_args.kwargs["headers"]["Authorization"] == "Bearer hub-rotated"


def test_make_default_router_hmac_mode_signs_with_x_hub_headers(monkeypatch, tmp_path):
    """HMAC mode: per-source secret from env, X-Hub-* headers, no Bearer."""
    fake_script = tmp_path / "safe_telegram_send.sh"
    fake_script.write_text("#!/usr/bin/env bash\nexit 0\n")
    fake_script.chmod(0o755)
    run_log = tmp_path / "router-run-log.jsonl"

    monkeypatch.setenv("WEEKLY_PREVIEW_HUB_HMAC_SECRET", "8e2fd0c566cf02bd")
    monkeypatch.setenv("MC_HUB_TOKEN", "mc-tok")
    monkeypatch.setenv("SAFE_TELEGRAM_SEND_SCRIPT", str(fake_script))

    router = make_default_router(
        source_slug="weekly-preview",
        target_chat_id="128314698",
        context="weekly-preview",
        hub_auth_mode="hmac",
        hub_hmac_secret_env="WEEKLY_PREVIEW_HUB_HMAC_SECRET",
        run_log_path=str(run_log),
    )

    with patch("gateway.router_factory.requests.post", return_value=_hub_success(event_id="evt-hmac")) as mock_post:
        result = router.send(message="hmac smoke", issue={"id": "wk-1", "title": "T"})

    assert result.ok is True
    assert result.hop == "hermes"
    headers = mock_post.call_args.kwargs["headers"]
    assert "Authorization" not in headers
    assert headers["X-Hub-Timestamp"]
    assert headers["X-Hub-Nonce"]
    assert len(headers["X-Hub-Signature"]) == 64


def test_make_default_router_hmac_mode_missing_secret_cascades_to_mc(monkeypatch, tmp_path):
    """HMAC mode: secret missing -> hub stage fails, cascade to MC."""
    fake_script = tmp_path / "safe_telegram_send.sh"
    fake_script.write_text("#!/usr/bin/env bash\nexit 0\n")
    fake_script.chmod(0o755)
    run_log = tmp_path / "router-run-log.jsonl"

    monkeypatch.delenv("WEEKLY_PREVIEW_HUB_HMAC_SECRET", raising=False)
    monkeypatch.setenv("MC_HUB_TOKEN", "mc-tok")
    monkeypatch.setenv("SAFE_TELEGRAM_SEND_SCRIPT", str(fake_script))

    router = make_default_router(
        source_slug="weekly-preview",
        target_chat_id="128314698",
        context="weekly-preview",
        hub_auth_mode="hmac",
        hub_hmac_secret_env="WEEKLY_PREVIEW_HUB_HMAC_SECRET",
        run_log_path=str(run_log),
    )

    with patch("gateway.router_factory.requests.post", return_value=_mc_success(event_id="mc-h")):
        result = router.send(message="m", issue={"id": "wk-1"})

    assert result.ok is True
    assert result.hop == "mission-control"
