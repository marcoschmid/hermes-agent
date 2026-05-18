"""Tests for /paperclip/notify FastAPI router."""
from typing import List

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from gateway.paperclip_notify import build_router


@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.setenv("PAPERCLIP_NOTIFY_TOKEN", "secret123")
    monkeypatch.setenv("PAPERCLIP_NOTIFY_DB", str(tmp_path / "d.db"))
    return monkeypatch


@pytest.fixture
def client(env):
    sent: List[str] = []

    def telegram_send(message: str) -> None:
        sent.append(message)

    app = FastAPI()
    app.include_router(build_router(telegram_send=telegram_send))
    c = TestClient(app)
    c.sent = sent  # type: ignore[attr-defined]
    return c


def payload(**overrides):
    p = {
        "check": "drift",
        "status": "warn",
        "previous_status": "ok",
        "findings": 3,
        "summary": "x",
        "content_hash": "h1",
        "scheduled_for": "2026-04-30T09:00:00+00:00",
        "details_hint": "paperclip checks history drift --limit 1",
    }
    p.update(overrides)
    return p


def test_no_token_returns_401(client):
    r = client.post("/paperclip/notify", json=payload())
    assert r.status_code == 401
    assert client.sent == []


def test_wrong_token_returns_401(client):
    r = client.post(
        "/paperclip/notify", json=payload(), headers={"Authorization": "Bearer wrong"}
    )
    assert r.status_code == 401
    assert client.sent == []


def test_valid_token_sends_telegram(client):
    r = client.post(
        "/paperclip/notify",
        json=payload(),
        headers={"Authorization": "Bearer secret123"},
    )
    assert r.status_code == 200
    assert r.json() == {"sent": True}
    assert len(client.sent) == 1


def test_send_failure_returns_502_and_does_not_dedupe(env):
    attempts: List[str] = []

    def telegram_send(message: str) -> None:
        attempts.append(message)
        raise RuntimeError("telegram unavailable")

    app = FastAPI()
    app.include_router(build_router(telegram_send=telegram_send))
    c = TestClient(app)
    headers = {"Authorization": "Bearer secret123"}

    r1 = c.post("/paperclip/notify", json=payload(), headers=headers)
    r2 = c.post("/paperclip/notify", json=payload(), headers=headers)

    assert r1.status_code == 502
    assert r2.status_code == 502
    assert len(attempts) == 2


def test_dedupe_second_call_no_send(client):
    h = {"Authorization": "Bearer secret123"}
    p = payload(previous_status="warn", status="warn")
    r1 = client.post("/paperclip/notify", json=p, headers=h)
    r2 = client.post("/paperclip/notify", json=p, headers=h)
    assert r1.status_code == 200 and r2.status_code == 200
    assert r1.json() == {"sent": True}
    assert r2.json() == {"sent": False, "deduped": True}
    assert len(client.sent) == 1


def test_state_change_overrides_dedupe(client):
    h = {"Authorization": "Bearer secret123"}
    client.post(
        "/paperclip/notify",
        json=payload(previous_status="warn", status="warn"),
        headers=h,
    )
    client.post(
        "/paperclip/notify",
        json=payload(previous_status="warn", status="ok"),
        headers=h,
    )
    assert len(client.sent) == 2


def test_message_includes_check_name_and_hint(client):
    h = {"Authorization": "Bearer secret123"}
    client.post(
        "/paperclip/notify",
        json=payload(check="drift", details_hint="paperclip checks history drift"),
        headers=h,
    )
    assert any("drift" in m and "paperclip checks history drift" in m for m in client.sent)


def test_missing_required_field_returns_422(client):
    h = {"Authorization": "Bearer secret123"}
    bad = payload()
    del bad["check"]
    r = client.post("/paperclip/notify", json=bad, headers=h)
    assert r.status_code == 422
    assert client.sent == []


def test_token_from_secrets_file_via_hermes_home(tmp_path, monkeypatch):
    monkeypatch.delenv("PAPERCLIP_NOTIFY_TOKEN", raising=False)
    hermes_home = tmp_path / "alt-hermes"
    (hermes_home / "secrets").mkdir(parents=True)
    (hermes_home / "secrets" / "notify-token").write_text("filetoken123\n")
    monkeypatch.setenv("HERMES_HOME", str(hermes_home))
    monkeypatch.setenv("PAPERCLIP_NOTIFY_DB", str(tmp_path / "d.db"))

    sent: List[str] = []
    app = FastAPI()
    app.include_router(build_router(telegram_send=lambda m: sent.append(m)))
    c = TestClient(app)

    r = c.post(
        "/paperclip/notify",
        json=payload(),
        headers={"Authorization": "Bearer filetoken123"},
    )
    assert r.status_code == 200
    assert len(sent) == 1


def test_dedupe_db_path_respects_hermes_home(tmp_path, monkeypatch):
    monkeypatch.delenv("PAPERCLIP_NOTIFY_DB", raising=False)
    hermes_home = tmp_path / "alt-hermes"
    hermes_home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(hermes_home))
    monkeypatch.setenv("PAPERCLIP_NOTIFY_TOKEN", "tkn")

    sent: List[str] = []
    app = FastAPI()
    app.include_router(build_router(telegram_send=lambda m: sent.append(m)))
    c = TestClient(app)
    r = c.post(
        "/paperclip/notify", json=payload(), headers={"Authorization": "Bearer tkn"}
    )
    assert r.status_code == 200
    assert (hermes_home / "cron" / "paperclip_notify_dedupe.db").exists()
