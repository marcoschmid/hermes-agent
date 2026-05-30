"""Tests for the admin POST /v1/admin/registry-sync-now endpoint (Step 4b).

The endpoint is a pilot-token-guarded manual trigger for a full-table mirror
sync. It reuses the same HUB_PILOT_TOKEN Bearer guard as the rest of the hub
(compare_digest, see api.py:_authenticate) and surfaces a transient MC failure
as a typed 503 `registry_unavailable` — never an uncaught 500.

Pattern mirrors test_api_registry_unavailable.py: a bare FastAPI()+include_router
host + TestClient(raise_server_exceptions=False) + monkeypatch.
"""

from __future__ import annotations

from unittest.mock import AsyncMock

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from gateway.hub import api as hub_api
from gateway.hub.api import router
from gateway.hub.registry_client import RegistryUnavailable


PILOT = "pilot-token-test"


@pytest.fixture
def app_client() -> TestClient:
    app = FastAPI()
    app.include_router(router)
    return TestClient(app, raise_server_exceptions=False)


@pytest.fixture(autouse=True)
def _pilot(monkeypatch):
    monkeypatch.setenv("HUB_PILOT_TOKEN", PILOT)


def test_admin_sync_now_requires_pilot_token(app_client, monkeypatch):
    """No Bearer → 401, sync_once never reached."""
    sync_called = {"n": 0}

    async def fake_sync_once():
        sync_called["n"] += 1
        return {}

    monkeypatch.setattr(
        hub_api,
        "RegistrySync",
        lambda *a, **k: AsyncMock(sync_once=AsyncMock(side_effect=fake_sync_once)),
    )

    resp = app_client.post("/v1/admin/registry-sync-now")
    assert resp.status_code == 401
    assert sync_called["n"] == 0


def test_admin_sync_now_rejects_wrong_token(app_client):
    resp = app_client.post(
        "/v1/admin/registry-sync-now",
        headers={"Authorization": "Bearer wrong-token"},
    )
    assert resp.status_code == 401


def test_admin_sync_now_with_token_unset_returns_401(app_client, monkeypatch):
    """HUB_PILOT_TOKEN not configured at all → 401 (fail-closed), and sync is
    never reached. Even a Bearer that looks valid cannot pass when there is no
    configured token to compare against."""
    monkeypatch.delenv("HUB_PILOT_TOKEN", raising=False)
    sync_called = {"n": 0}

    async def fake_sync_once():
        sync_called["n"] += 1
        return {}

    monkeypatch.setattr(
        hub_api,
        "RegistrySync",
        lambda *a, **k: AsyncMock(sync_once=AsyncMock(side_effect=fake_sync_once)),
    )

    resp = app_client.post(
        "/v1/admin/registry-sync-now",
        headers={"Authorization": "Bearer anything"},
    )
    assert resp.status_code == 401
    assert sync_called["n"] == 0


def test_admin_sync_now_triggers_sync(app_client, monkeypatch):
    """Valid Bearer → sync_once() runs, per-table counts returned in body."""
    counts = {
        "sources": 2,
        "topics": 1,
        "audiences": 2,
        "channels": 2,
        "channel_sets": 2,
        "channel_set_members": 3,
        "rules": 2,
    }
    fake = AsyncMock()
    fake.sync_once = AsyncMock(return_value=counts)
    monkeypatch.setattr(hub_api, "RegistrySync", lambda *a, **k: fake)
    # Avoid real registry/hub_state construction in the route.
    monkeypatch.setattr(hub_api, "get_registry", lambda: AsyncMock())
    monkeypatch.setattr(hub_api, "get_hub_state", AsyncMock(return_value=AsyncMock()))

    resp = app_client.post(
        "/v1/admin/registry-sync-now",
        headers={"Authorization": f"Bearer {PILOT}"},
    )
    assert resp.status_code == 200
    body = resp.json()["data"]
    assert body["sources"] == 2
    assert body["channel_set_members"] == 3
    assert body["rules"] == 2
    fake.sync_once.assert_awaited_once()


def test_admin_sync_now_registry_unavailable_returns_503(app_client, monkeypatch):
    """A transient MC failure during the triggered sync → 503, not 500."""
    fake = AsyncMock()
    fake.sync_once = AsyncMock(side_effect=RegistryUnavailable(503, "MC down"))
    monkeypatch.setattr(hub_api, "RegistrySync", lambda *a, **k: fake)
    monkeypatch.setattr(hub_api, "get_registry", lambda: AsyncMock())
    monkeypatch.setattr(hub_api, "get_hub_state", AsyncMock(return_value=AsyncMock()))

    resp = app_client.post(
        "/v1/admin/registry-sync-now",
        headers={"Authorization": f"Bearer {PILOT}"},
    )
    assert resp.status_code == 503
    assert resp.json()["detail"]["error_code"] == "registry_unavailable"
