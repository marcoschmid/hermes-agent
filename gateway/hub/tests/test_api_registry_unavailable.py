"""Channel-isolation (5d): a transient registry (MC) failure must surface as a
clean HTTP 503 `registry_unavailable`, NOT an uncaught HTTP 500.

Background: on 2026-05-27 revoking the hermes-hub-service MC token made every
registry read return 401; the raw httpx.HTTPStatusError escaped the
PipelineError-only handlers as a 500, the hermes hop reported ok=False and the
fallback router cascaded to direct-fallback. These tests lock in that EVERY
registry-read escape path (auth, pipeline lookup, rule-match — on both the
real and the simulate endpoint) instead yields a typed 503, while a genuine
unknown-source answer still surfaces as 401 (no masking).
"""
from __future__ import annotations

from unittest.mock import AsyncMock

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from gateway.hub import api as hub_api
from gateway.hub.api import router
from gateway.hub.registry_client import RegistryUnavailable


BASIC_INTENT = {
    "source_slug": "weekly-preview",
    "topic": "home.kalender.weekly",
    "severity": "info",
    "audience": "family",
    "title": "T",
    "body": "B",
}

HMAC_HEADERS = {
    "x-hub-timestamp": "1",
    "x-hub-nonce": "nonce-1",
    "x-hub-signature": "sig-ignored",
}

VALID_SOURCE = {
    "id": "src_1",
    "slug": "weekly-preview",
    "enabled": True,
    "severity_max": "crit",
    "allowed_topics": None,
    "allowed_audiences": None,
    "hub_secret": "s3cret",
}
VALID_TOPIC = {"id": "tp_1", "slug": "home.kalender.weekly", "default_audience_slug": "family"}


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


def _pilot(monkeypatch):
    monkeypatch.setenv("HUB_PILOT_TOKEN", "pilot-token-test")


def _no_pilot(monkeypatch):
    monkeypatch.delenv("HUB_PILOT_TOKEN", raising=False)


# --- P1: auth path (HMAC) — get_source transient failure -> 503 -------------

def test_authenticate_registry_unavailable_returns_503(app_client, registry, monkeypatch):
    _no_pilot(monkeypatch)
    registry.get_source = AsyncMock(side_effect=RegistryUnavailable(401, "GET source -> 401"))
    resp = app_client.post("/v1/notifications", json=BASIC_INTENT, headers=HMAC_HEADERS)
    assert resp.status_code == 503
    assert resp.json()["detail"]["error_code"] == "registry_unavailable"


# --- P2: pipeline validate_and_lookup -> get_source transient failure -> 503 -

def test_pipeline_lookup_registry_unavailable_returns_503(app_client, registry, monkeypatch):
    _pilot(monkeypatch)
    registry.get_source = AsyncMock(side_effect=RegistryUnavailable(503, "GET source -> 503"))
    resp = app_client.post(
        "/v1/notifications", json=BASIC_INTENT,
        headers={"Authorization": "Bearer pilot-token-test"},
    )
    assert resp.status_code == 503
    assert resp.json()["detail"]["error_code"] == "registry_unavailable"


# --- P3: pipeline rule-match -> get_rules transient failure -> 503 ----------

def test_pipeline_rule_match_registry_unavailable_returns_503(app_client, registry, monkeypatch):
    _pilot(monkeypatch)
    registry.get_source = AsyncMock(return_value=VALID_SOURCE)
    registry.get_topic = AsyncMock(return_value=VALID_TOPIC)
    registry.get_rules = AsyncMock(side_effect=RegistryUnavailable(502, "GET rules -> 502"))
    resp = app_client.post(
        "/v1/notifications", json=BASIC_INTENT,
        headers={"Authorization": "Bearer pilot-token-test"},
    )
    assert resp.status_code == 503
    assert resp.json()["detail"]["error_code"] == "registry_unavailable"


# --- P4: simulate validate_and_lookup -> get_source transient failure -> 503 -

def test_simulate_lookup_registry_unavailable_returns_503(app_client, registry, monkeypatch):
    _pilot(monkeypatch)
    registry.get_source = AsyncMock(side_effect=RegistryUnavailable(401, "GET source -> 401"))
    resp = app_client.post(
        "/v1/notifications/simulate", json=BASIC_INTENT,
        headers={"Authorization": "Bearer pilot-token-test"},
    )
    assert resp.status_code == 503
    assert resp.json()["detail"]["error_code"] == "registry_unavailable"


# --- P5: simulate rule-match -> get_rules transient failure -> 503 ----------

def test_simulate_rule_match_registry_unavailable_returns_503(app_client, registry, monkeypatch):
    _pilot(monkeypatch)
    registry.get_source = AsyncMock(return_value=VALID_SOURCE)
    registry.get_topic = AsyncMock(return_value=VALID_TOPIC)
    registry.get_rules = AsyncMock(side_effect=RegistryUnavailable(500, "GET rules -> 500"))
    resp = app_client.post(
        "/v1/notifications/simulate", json=BASIC_INTENT,
        headers={"Authorization": "Bearer pilot-token-test"},
    )
    assert resp.status_code == 503
    assert resp.json()["detail"]["error_code"] == "registry_unavailable"


# --- Negative control: a genuine unknown source must NOT be masked as 503 ---

def test_unknown_source_still_returns_401_not_503(app_client, registry, monkeypatch):
    _no_pilot(monkeypatch)
    registry.get_source = AsyncMock(return_value=None)  # MC clean 'not found'
    resp = app_client.post("/v1/notifications", json=BASIC_INTENT, headers=HMAC_HEADERS)
    assert resp.status_code == 401
    assert resp.json()["detail"]["error_code"] == "unknown_source"
