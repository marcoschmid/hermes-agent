"""Phase-6b Step 6: the HMAC auth path must read the per-source hub_secret from
the local secret store when the registry source-dict carries none.

Background: in mirror mode (Step 5) `get_source` returns a secret-free source
row (the Step-4a list endpoint omits all secrets), so `source.get("hub_secret")`
is None and the request would fail the no-secret 401 at api.py:~208. Step 6
adds a file-backed fallback (~/.hermes/registry_secrets.json) consulted ONLY
when the source-dict has no hub_secret. In default mc-mode the source-dict
still carries hub_secret, so the store is never read — zero behaviour change.

These tests touch only the SECRET SOURCE, never the HMAC verify logic itself
(hmac_verify, nonce-replay, the 401 paths stay byte-for-byte).
"""
from __future__ import annotations

import json
from datetime import datetime, timezone
from unittest.mock import AsyncMock

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from gateway.hub import api as hub_api
from gateway.hub.api import router
from gateway.hub.hmac_sign import sign


# --- shared intent + registry shapes (mirror VALID_SOURCE has NO hub_secret) -

BASIC_INTENT = {
    "source_slug": "weekly-preview",
    "topic": "home.kalender.weekly",
    "severity": "info",
    "audience": "family",
    "title": "T",
    "body": "B",
}

# Mirror-mode source row: secret-free (no hub_secret key), exactly like
# RegistryClient._mirror_get_source returns.
MIRROR_SOURCE = {
    "id": "src_1",
    "slug": "weekly-preview",
    "enabled": True,
    "severity_max": "crit",
    "allowed_topics": None,
    "allowed_audiences": None,
}
# mc-mode source row: carries hub_secret inline.
MC_SOURCE = {**MIRROR_SOURCE, "hub_secret": "mc-inline-secret"}

VALID_TOPIC = {
    "id": "tp_1",
    "slug": "home.kalender.weekly",
    "default_audience_slug": "family",
}

SECRET_VALUE = "store-secret-value-xyz"


def _iso_now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _hmac_headers(secret: str, body: bytes, *, nonce: str = "nonce-ss-1") -> dict:
    """Build a genuine, valid X-Hub-* header trio for `body` signed with `secret`."""
    ts = _iso_now()
    sig = sign(secret.encode(), ts, nonce, body)
    return {
        "x-hub-timestamp": ts,
        "x-hub-nonce": nonce,
        "x-hub-signature": sig,
    }


@pytest.fixture
def app_client() -> TestClient:
    app = FastAPI()
    app.include_router(router)
    return TestClient(app, raise_server_exceptions=False)


@pytest.fixture
def registry(monkeypatch):
    reg = AsyncMock()
    reg.post_audit = AsyncMock(return_value=None)
    monkeypatch.setattr(hub_api, "get_registry", lambda: reg)
    return reg


@pytest.fixture(autouse=True)
def _no_pilot(monkeypatch):
    """Force the HMAC path (not the pilot-token bypass) for every test here."""
    monkeypatch.delenv("HUB_PILOT_TOKEN", raising=False)


@pytest.fixture(autouse=True)
def _mock_hub_state(monkeypatch):
    """Let nonce-replay run against an AsyncMock state (first-seen → True)."""
    monkeypatch.setattr(hub_api, "get_hub_state", AsyncMock(return_value=AsyncMock()))


@pytest.fixture(autouse=True)
def _reset_secret_store_singleton():
    """Clear the module-level secret-store singleton between tests so each test
    gets a store bound to its own tmp file / env."""
    from gateway.hub import secret_store as ss

    ss._secret_store = None
    yield
    ss._secret_store = None


def _write_store(tmp_path, monkeypatch, mapping: dict) -> str:
    path = tmp_path / "registry_secrets.json"
    path.write_text(json.dumps(mapping), encoding="utf-8")
    monkeypatch.setenv("HUB_REGISTRY_SECRETS_PATH", str(path))
    return str(path)


# --- T1: mirror mode → secret comes from the store, request authenticates ----


def test_authenticate_uses_secret_store_in_mirror_mode(
    app_client, registry, monkeypatch, tmp_path
):
    """Source-dict has NO hub_secret (mirror), but the store does → the valid
    HMAC verifies and the request is NOT rejected with 401."""
    _write_store(tmp_path, monkeypatch, {"weekly-preview": SECRET_VALUE})
    registry.get_source = AsyncMock(return_value=dict(MIRROR_SOURCE))
    registry.get_topic = AsyncMock(return_value=VALID_TOPIC)
    # No rule match → simulate returns a clean 200 with no_rule_match. The point
    # is purely that AUTH passed (no 401 / no no_hub_secret).
    registry.get_rules = AsyncMock(return_value=[])

    body = json.dumps(BASIC_INTENT).encode()
    headers = _hmac_headers(SECRET_VALUE, body)
    resp = app_client.post(
        "/v1/notifications/simulate", content=body, headers=headers
    )

    assert resp.status_code != 401, resp.text
    assert resp.status_code == 200, resp.text


# --- T2: no secret anywhere → existing no_hub_secret 401 path is preserved ----


def test_no_hub_secret_anywhere_returns_401(
    app_client, registry, monkeypatch, tmp_path
):
    """Neither the source-dict nor the store has a secret for this slug → the
    pre-existing api.py:~208 no_hub_secret 401 still fires."""
    _write_store(tmp_path, monkeypatch, {"some-other-source": "x"})
    registry.get_source = AsyncMock(return_value=dict(MIRROR_SOURCE))

    body = json.dumps(BASIC_INTENT).encode()
    headers = _hmac_headers("irrelevant", body)
    resp = app_client.post(
        "/v1/notifications/simulate", content=body, headers=headers
    )

    assert resp.status_code == 401
    assert resp.json()["detail"]["error_code"] == "no_hub_secret"


# --- T3: mc mode (source has hub_secret) → store is NEVER consulted -----------


def test_mc_mode_secret_store_not_consulted(
    app_client, registry, monkeypatch, tmp_path
):
    """When the source-dict already carries hub_secret (default mc-mode), the
    fallback short-circuits and get_hub_secret is never called."""
    _write_store(tmp_path, monkeypatch, {"weekly-preview": "store-should-not-win"})
    registry.get_source = AsyncMock(return_value=dict(MC_SOURCE))
    registry.get_topic = AsyncMock(return_value=VALID_TOPIC)
    registry.get_rules = AsyncMock(return_value=[])

    from gateway.hub import secret_store as ss

    sentinel = {"called": False}

    def _boom(_slug):  # pragma: no cover - asserted not-called
        sentinel["called"] = True
        return "store-should-not-win"

    monkeypatch.setattr(ss.get_secret_store(), "get_hub_secret", _boom)

    body = json.dumps(BASIC_INTENT).encode()
    # Sign with the MC inline secret — must verify via the source-dict path.
    headers = _hmac_headers(MC_SOURCE["hub_secret"], body)
    resp = app_client.post(
        "/v1/notifications/simulate", content=body, headers=headers
    )

    assert resp.status_code != 401, resp.text
    assert sentinel["called"] is False


# --- T4: store.get returns None for an unknown slug --------------------------


def test_secret_store_get_returns_none_for_missing_slug(monkeypatch, tmp_path):
    from gateway.hub.secret_store import SecretStore

    path = tmp_path / "registry_secrets.json"
    path.write_text(json.dumps({"weekly-preview": SECRET_VALUE}), encoding="utf-8")
    store = SecretStore(path=str(path))

    assert store.get_hub_secret("weekly-preview") == SECRET_VALUE
    assert store.get_hub_secret("does-not-exist") is None


# --- T5: the secret value is NEVER logged ------------------------------------


def test_secret_store_never_logs_value(monkeypatch, tmp_path, caplog):
    import logging

    from gateway.hub.secret_store import SecretStore

    path = tmp_path / "registry_secrets.json"
    path.write_text(json.dumps({"weekly-preview": SECRET_VALUE}), encoding="utf-8")

    with caplog.at_level(logging.DEBUG):
        store = SecretStore(path=str(path))
        store.get_hub_secret("weekly-preview")
        store.get_hub_secret("missing")

    assert SECRET_VALUE not in caplog.text
