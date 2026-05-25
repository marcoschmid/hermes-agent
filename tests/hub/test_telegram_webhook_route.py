"""Integration tests for Hub /telegram/webhook FastAPI route (P4 Track B)."""
from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from gateway.hub.telegram_webhook_route import (
    build_receiver_from_env,
    register_telegram_webhook,
    router as telegram_router,
)
from gateway.telegram_gateway import TelegramCallbackReceiver


SECRET = "test-webhook-secret"
ALLOWED_USER = "128314698"


def _make_app_with_receiver(tmp_path: Path) -> FastAPI:
    """Build minimal FastAPI app with telegram_receiver pre-wired (test-isolated)."""
    app = FastAPI()
    receiver = TelegramCallbackReceiver(
        db_path=str(tmp_path / "callbacks.sqlite"),
        webhook_secret=SECRET,
        allowed_user_ids={ALLOWED_USER},
        rate_limit_per_minute=60,
    )
    receiver.init_schema()
    app.state.telegram_receiver = receiver
    app.include_router(telegram_router)
    return app


def _payload(callback_id: str, *, data: str = "approve:ISS-1",
             user_id: int = 128314698, update_id: int = 1001) -> dict[str, Any]:
    return {
        "update_id": update_id,
        "callback_query": {
            "id": callback_id,
            "from": {"id": user_id, "first_name": "Marco"},
            "message": {"chat": {"id": 128314698}, "message_id": 42},
            "data": data,
        },
    }


# ---- Happy path ------------------------------------------------------------


def test_webhook_persists_callback_returns_200(tmp_path: Path):
    app = _make_app_with_receiver(tmp_path)
    client = TestClient(app)

    resp = client.post(
        "/telegram/webhook",
        json=_payload("cb-route-1", data="approve:ISS-42"),
        headers={"X-Telegram-Bot-Api-Secret-Token": SECRET},
    )

    assert resp.status_code == 200
    body = resp.json()
    assert body["accepted"] is True
    assert body["duplicate"] is False
    assert body["callback_type"] == "approve"
    assert body["issue_id"] == "ISS-42"


# ---- Wrong secret rejected --------------------------------------------------


def test_webhook_wrong_secret_returns_401(tmp_path: Path):
    app = _make_app_with_receiver(tmp_path)
    client = TestClient(app)

    resp = client.post(
        "/telegram/webhook",
        json=_payload("cb-401"),
        headers={"X-Telegram-Bot-Api-Secret-Token": "wrong"},
    )

    assert resp.status_code == 401


# ---- Duplicate idempotency --------------------------------------------------


def test_webhook_duplicate_callback_id_returns_duplicate_true(tmp_path: Path):
    app = _make_app_with_receiver(tmp_path)
    client = TestClient(app)
    headers = {"X-Telegram-Bot-Api-Secret-Token": SECRET}
    payload = _payload("cb-dup", data="reject:ISS-7")

    first = client.post("/telegram/webhook", json=payload, headers=headers)
    second = client.post("/telegram/webhook", json=payload, headers=headers)

    assert first.status_code == 200 and second.status_code == 200
    assert first.json()["duplicate"] is False
    assert second.json()["duplicate"] is True


# ---- Unknown callback-type logged + ignored ---------------------------------


def test_webhook_unknown_callback_type_returns_ignored(tmp_path: Path):
    app = _make_app_with_receiver(tmp_path)
    client = TestClient(app)

    resp = client.post(
        "/telegram/webhook",
        json=_payload("cb-unknown", data="weird:ISS-99"),
        headers={"X-Telegram-Bot-Api-Secret-Token": SECRET},
    )

    assert resp.status_code == 200
    body = resp.json()
    assert body["accepted"] is False
    assert body["ignored"] is True


# ---- Rate-limit -------------------------------------------------------------


def test_webhook_rate_limit_returns_429(tmp_path: Path):
    app = FastAPI()
    receiver = TelegramCallbackReceiver(
        db_path=str(tmp_path / "rate.sqlite"),
        webhook_secret=SECRET,
        allowed_user_ids={ALLOWED_USER},
        rate_limit_per_minute=1,  # tiny
    )
    receiver.init_schema()
    app.state.telegram_receiver = receiver
    app.include_router(telegram_router)
    client = TestClient(app)
    headers = {"X-Telegram-Bot-Api-Secret-Token": SECRET}

    r1 = client.post("/telegram/webhook",
                     json=_payload("cb-rate-1", update_id=1), headers=headers)
    r2 = client.post("/telegram/webhook",
                     json=_payload("cb-rate-2", update_id=2), headers=headers)

    assert r1.status_code == 200
    assert r2.status_code == 429


# ---- Missing receiver -> 500 ------------------------------------------------


def test_webhook_without_receiver_state_returns_500(tmp_path: Path):
    app = FastAPI()
    app.include_router(telegram_router)
    client = TestClient(app)

    resp = client.post(
        "/telegram/webhook",
        json=_payload("cb-no-state"),
        headers={"X-Telegram-Bot-Api-Secret-Token": "any"},
    )
    assert resp.status_code == 500


# ---- build_receiver_from_env --------------------------------------------------


def test_build_receiver_from_env_requires_secret(monkeypatch, tmp_path):
    monkeypatch.delenv("TELEGRAM_WEBHOOK_SECRET", raising=False)
    with pytest.raises(RuntimeError, match="TELEGRAM_WEBHOOK_SECRET"):
        build_receiver_from_env()


def test_build_receiver_from_env_reads_config(monkeypatch, tmp_path):
    monkeypatch.setenv("TELEGRAM_WEBHOOK_SECRET", "env-secret")
    monkeypatch.setenv("TELEGRAM_CALLBACKS_DB", str(tmp_path / "env-cb.sqlite"))
    monkeypatch.setenv("TELEGRAM_ALLOWED_USER_IDS", "111,222,333")
    monkeypatch.setenv("TELEGRAM_RATE_LIMIT_PER_MINUTE", "120")

    receiver = build_receiver_from_env()
    assert receiver._secret == "env-secret"
    assert receiver._allowed == {"111", "222", "333"}
    assert receiver._rate_limit == 120


def test_register_telegram_webhook_silently_skips_without_secret(monkeypatch):
    monkeypatch.delenv("TELEGRAM_WEBHOOK_SECRET", raising=False)
    app = FastAPI()
    register_telegram_webhook(app)  # must NOT raise

    # No state attached, no route mounted
    assert not hasattr(app.state, "telegram_receiver")
    client = TestClient(app)
    resp = client.post("/telegram/webhook", json={}, headers={})
    assert resp.status_code == 404  # route not mounted
