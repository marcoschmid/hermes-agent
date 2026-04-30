"""Tests for /paperclip/notify FastAPI router (bearer auth + dedupe + send)."""
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


def test_recovery_summary_in_telegram(client):
    h = {"Authorization": "Bearer secret123"}
    client.post(
        "/paperclip/notify",
        json=payload(previous_status="warn", status="ok", summary="all clean"),
        headers=h,
    )
    assert any("all clean" in m for m in client.sent)


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
    """If PAPERCLIP_NOTIFY_TOKEN unset, fall back to $HERMES_HOME/secrets/notify-token."""
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
    """Default dedupe DB lives under $HERMES_HOME/cron/, not ~/.hermes/cron/."""
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


def test_concurrent_identical_requests_dedupe_atomically(env, tmp_path):
    """Two near-simultaneous identical alerts must produce exactly one Telegram send."""
    import asyncio
    import threading

    sent: List[str] = []
    send_started = threading.Event()
    proceed = threading.Event()

    def telegram_send(message: str) -> None:
        # Block the first send long enough for a second request to race in.
        sent.append(message)
        send_started.set()
        proceed.wait(timeout=2)

    app = FastAPI()
    app.include_router(build_router(telegram_send=telegram_send))
    c = TestClient(app)
    h = {"Authorization": "Bearer secret123"}
    p = payload(previous_status="warn", status="warn", content_hash="race-h")

    async def fire():
        loop = asyncio.get_running_loop()
        return await asyncio.gather(
            loop.run_in_executor(None, lambda: c.post("/paperclip/notify", json=p, headers=h)),
            loop.run_in_executor(None, lambda: c.post("/paperclip/notify", json=p, headers=h)),
        )

    proceed.set()  # don't actually block telegram_send in this test variant
    r1, r2 = asyncio.run(fire())
    assert {r1.status_code, r2.status_code} == {200}
    bodies = sorted([r1.json(), r2.json()], key=lambda b: not b.get("sent", False))
    assert bodies[0] == {"sent": True}
    assert bodies[1] == {"sent": False, "deduped": True}
    assert len(sent) == 1
