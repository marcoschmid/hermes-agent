"""Tests for POST /v1/notifications/simulate (Phase 2 routing-simulator)."""
from __future__ import annotations

from unittest.mock import AsyncMock

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from gateway.hub import api as hub_api
from gateway.hub.api import router


@pytest.fixture
def client(monkeypatch) -> TestClient:
    # Bypass auth: enable HUB_PILOT_TOKEN back-compat path.
    monkeypatch.setenv("HUB_PILOT_TOKEN", "pilot-token-test")
    app = FastAPI()
    app.include_router(router)
    return TestClient(app)


@pytest.fixture
def fake_registry(monkeypatch):
    """Stub registry exposing the four methods the simulator + pipeline touch."""
    registry = AsyncMock()
    registry.post_audit = AsyncMock(return_value=None)
    monkeypatch.setattr(hub_api, "get_registry", lambda: registry)
    return registry


BASIC_INTENT = {
    "source_slug": "weekly-preview",
    "topic": "home.kalender.weekly",
    "severity": "info",
    "audience": "family",
    "title": "T",
    "body": "B",
}


def _bypass_pipeline_validate(monkeypatch, *, scope_ok=True, rule_match=True):
    """Wire validate_and_lookup + validate_scope + find_matching_rule with fakes."""
    from gateway.hub import pipeline as pipeline_mod
    from gateway.hub import rule_matcher as rule_mod

    ctx = pipeline_mod.ResolvedContext(
        source={"id": "src_1", "slug": "weekly-preview"},
        topic={"id": "tp_1", "slug": "home.kalender.weekly"},
        audience_slug="family",
    )
    monkeypatch.setattr(pipeline_mod, "validate_and_lookup", AsyncMock(return_value=ctx))

    if scope_ok:
        monkeypatch.setattr(pipeline_mod, "validate_scope", AsyncMock(return_value=None))
    else:
        async def deny(*a, **kw):
            raise pipeline_mod.PipelineError(
                status_code=403, error_code="scope_denied", message="audience not allowed"
            )
        monkeypatch.setattr(pipeline_mod, "validate_scope", deny)

    if rule_match:
        rule = {"id": "rule_1", "slug": "global-default-family"}
        channel_set = {
            "id": "cset_1",
            "enabled": True,
            "members": [
                {"required": True, "channel": {"id": "ch_inbox", "slug": "inbox-mc", "type": "inbox_mc", "enabled": True}},
                {"required": False, "channel": {"id": "ch_tg", "slug": "tg-marco", "type": "telegram", "enabled": True}},
                {"required": False, "channel": {"id": "ch_disabled", "slug": "tg-old", "type": "telegram", "enabled": False}},
            ],
        }
        monkeypatch.setattr(rule_mod, "find_matching_rule", AsyncMock(return_value=(rule, channel_set)))
    else:
        monkeypatch.setattr(rule_mod, "find_matching_rule", AsyncMock(return_value=(None, None)))


def test_simulate_returns_match_and_enabled_channels(client, fake_registry, monkeypatch):
    _bypass_pipeline_validate(monkeypatch)
    response = client.post(
        "/v1/notifications/simulate",
        json=BASIC_INTENT,
        headers={"Authorization": "Bearer pilot-token-test"},
    )
    assert response.status_code == 200
    data = response.json()["data"]
    assert data["would_match_rule"] == "global-default-family"
    assert data["suppression"] is None
    deliver_ids = [d["channel_id"] for d in data["would_deliver_to"]]
    assert deliver_ids == ["ch_inbox", "ch_tg"]  # disabled channel excluded
    assert "rule_matched" in data["audit_trace"]


def test_simulate_no_rule_match_returns_suppression(client, fake_registry, monkeypatch):
    _bypass_pipeline_validate(monkeypatch, rule_match=False)
    response = client.post(
        "/v1/notifications/simulate",
        json=BASIC_INTENT,
        headers={"Authorization": "Bearer pilot-token-test"},
    )
    assert response.status_code == 200
    data = response.json()["data"]
    assert data["would_match_rule"] is None
    assert data["would_deliver_to"] == []
    assert data["suppression"]["reason"] == "no_rule_match"
    assert "no_rule_match" in data["audit_trace"]


def test_simulate_scope_denied_returns_suppression(client, fake_registry, monkeypatch):
    _bypass_pipeline_validate(monkeypatch, scope_ok=False)
    response = client.post(
        "/v1/notifications/simulate",
        json=BASIC_INTENT,
        headers={"Authorization": "Bearer pilot-token-test"},
    )
    assert response.status_code == 200
    data = response.json()["data"]
    assert data["would_match_rule"] is None
    assert data["suppression"]["reason"] == "scope_denied"


def test_simulate_skips_dispatch_no_audit_writes(client, fake_registry, monkeypatch):
    """Simulator must NOT write audit rows — read-only contract."""
    _bypass_pipeline_validate(monkeypatch)
    response = client.post(
        "/v1/notifications/simulate",
        json=BASIC_INTENT,
        headers={"Authorization": "Bearer pilot-token-test"},
    )
    assert response.status_code == 200
    fake_registry.post_audit.assert_not_called()


def test_simulate_requires_auth(client, fake_registry, monkeypatch):
    monkeypatch.delenv("HUB_PILOT_TOKEN", raising=False)
    response = client.post("/v1/notifications/simulate", json=BASIC_INTENT)
    assert response.status_code == 401


def test_simulate_rejects_invalid_payload(client, fake_registry, monkeypatch):
    bad = {"source_slug": "x"}  # missing topic/title/body
    response = client.post(
        "/v1/notifications/simulate",
        json=bad,
        headers={"Authorization": "Bearer pilot-token-test"},
    )
    assert response.status_code == 422
