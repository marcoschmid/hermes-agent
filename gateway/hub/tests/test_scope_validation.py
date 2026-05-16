"""Tests for per-source scope validation in the notification hub pipeline."""
from unittest.mock import AsyncMock

import pytest

from gateway.hub.adapter_registry import AdapterResult
from gateway.hub.cooldown import reset_cooldown_state
from gateway.hub.dedupe import reset_dedupe_state
from gateway.hub.flapping import reset_flapping_state
from gateway.hub.pipeline import PipelineError, run_pipeline
from gateway.hub.registry_client import RegistryClient
from gateway.hub.schemas import NotificationIntent


class FakeRegistry(RegistryClient):
    def __init__(self, source: dict) -> None:
        self._source = source
        self.post_audit = AsyncMock(return_value=None)

    async def get_source(self, slug: str, token_hash: str | None = None) -> dict | None:
        return self._source

    async def get_topic(self, slug: str) -> dict | None:
        return {
            "id": "top_ops",
            "slug": slug,
            "default_audience_id": "aud_marco",
            "default_audience_slug": "marco",
        }

    async def get_rules(
        self,
        topic: str,
        audience: str,
        severity: str,
        urgency: str = "none",
        actionability: str = "info",
        source_id: str | None = None,
    ) -> list[dict]:
        return [
            {
                "id": "rule_scope",
                "slug": "scope-default",
                "priority": 5,
                "channel_set_id": "cs_scope",
            }
        ]

    async def get_channel_set_expanded(self, channel_set_id: str) -> dict | None:
        return {
            "id": channel_set_id,
            "enabled": True,
            "members": [
                {"position": 1, "channel": {"id": "ch_inbox", "type": "inbox_mc", "enabled": True}},
            ],
        }


class FakeInboxMcAdapter:
    async def send(self, event: dict, channel: dict) -> AdapterResult:
        return AdapterResult(status="delivered", provider_message_id="evt_scope", latency_ms=0)


@pytest.fixture(autouse=True)
def _reset_pipeline_state(monkeypatch):
    reset_dedupe_state()
    reset_cooldown_state()
    reset_flapping_state()
    monkeypatch.setattr("gateway.hub.pipeline.get_adapter", lambda channel_type: FakeInboxMcAdapter())
    yield
    reset_dedupe_state()
    reset_cooldown_state()
    reset_flapping_state()


def make_source(**overrides) -> dict:
    source = {
        "id": "src_scope",
        "slug": "paperclip",
        "enabled": True,
        "severity_max": "crit",
        "allowed_topics": None,
        "allowed_audiences": None,
        "max_severity": None,
    }
    source.update(overrides)
    return source


def make_intent(**overrides) -> NotificationIntent:
    intent = {
        "source_slug": "paperclip",
        "topic": "weekly-preview",
        "severity": "notice",
        "urgency": "none",
        "actionability": "info",
        "audience": "marco",
        "title": "Weekly preview",
        "body": "Legacy sender payload",
    }
    intent.update(overrides)
    return NotificationIntent(**intent)


@pytest.mark.asyncio
async def test_allowed_topics_wildcard_passes() -> None:
    registry = FakeRegistry(make_source(allowed_topics=None))

    result = await run_pipeline(make_intent(), source_token_hash="h", registry=registry)

    assert result.status == "delivered"
    actions = [call.kwargs.get("action") for call in registry.post_audit.await_args_list]
    assert "scope_check_passed" in actions


@pytest.mark.asyncio
async def test_intent_topic_in_allowed_topics_passes() -> None:
    registry = FakeRegistry(make_source(allowed_topics=["weekly-preview", "ops.deploy"]))

    result = await run_pipeline(make_intent(topic="ops.deploy"), source_token_hash="h", registry=registry)

    assert result.status == "delivered"
    actions = [call.kwargs.get("action") for call in registry.post_audit.await_args_list]
    assert "scope_check_passed" in actions


@pytest.mark.asyncio
async def test_intent_topic_not_allowed_raises_403() -> None:
    registry = FakeRegistry(make_source(allowed_topics=["ops.deploy"]))

    with pytest.raises(PipelineError) as excinfo:
        await run_pipeline(make_intent(topic="weekly-preview"), source_token_hash="h", registry=registry)

    assert excinfo.value.status_code == 403
    assert excinfo.value.error_code == "topic_not_allowed"
    actions = [call.kwargs.get("action") for call in registry.post_audit.await_args_list]
    assert "scope_violation" in actions


@pytest.mark.asyncio
async def test_max_severity_notice_blocks_warn_with_403() -> None:
    registry = FakeRegistry(make_source(max_severity="notice"))

    with pytest.raises(PipelineError) as excinfo:
        await run_pipeline(make_intent(severity="warn"), source_token_hash="h", registry=registry)

    assert excinfo.value.status_code == 403
    assert excinfo.value.error_code == "severity_exceeded"
    actions = [call.kwargs.get("action") for call in registry.post_audit.await_args_list]
    assert "scope_violation" in actions
