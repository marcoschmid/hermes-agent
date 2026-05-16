"""Tests for dev_guard.is_dev_source and pipeline integration."""
from unittest.mock import AsyncMock

import pytest

import gateway.hub.adapter_registry as adapter_registry
from gateway.hub.adapter_registry import AdapterResult
from gateway.hub.cooldown import reset_cooldown_state
from gateway.hub.dedupe import reset_dedupe_state
from gateway.hub.dev_guard import is_dev_source
from gateway.hub.flapping import reset_flapping_state
from gateway.hub.pipeline import run_pipeline
from gateway.hub.registry_client import RegistryClient
from gateway.hub.schemas import NotificationIntent


# ---------- unit tests: is_dev_source ----------

@pytest.mark.parametrize("slug", [
    "paperclip-dev",
    "ha-pilot-test",
    "smoketest-v4a",
    "tmp-hub-probe",
    "Foo-DEV",       # case-insensitive
    "BAR-SMOKETEST",
])
def test_dev_source_matches(slug: str) -> None:
    assert is_dev_source(slug) is True


@pytest.mark.parametrize("slug", [
    "paperclip",
    "ha-pilot",
    "restic-backup",
    "weekly-preview",
    "developer",      # contains "dev" but does not match suffix/prefix
    "fastest",        # contains "test" mid-word
    "",
    None,
])
def test_prod_source_does_not_match(slug) -> None:
    assert is_dev_source(slug) is False


# ---------- integration: pipeline filters channels for dev sources ----------

class FakeRegistry(RegistryClient):
    def __init__(self, source, topic, rules, channel_set) -> None:
        self._source = source
        self._topic = topic
        self._rules = rules
        self._channel_set = channel_set
        self.post_audit = AsyncMock(return_value=None)

    async def get_source(self, slug, token_hash=None):
        return self._source

    async def get_topic(self, slug):
        return self._topic

    async def get_rules(self, topic, audience, severity, urgency="none",
                        actionability="info", source_id=None):
        return self._rules

    async def get_channel_set_expanded(self, channel_set_id):
        return self._channel_set


class FakeTelegramAdapter:
    name = "telegram"
    calls: list = []

    async def send(self, event: dict, channel: dict) -> AdapterResult:
        FakeTelegramAdapter.calls.append((event, channel))
        return AdapterResult(status="delivered", provider_message_id="tg-1", latency_ms=0)


class FakeInboxMcAdapter:
    name = "inbox_mc"
    calls: list = []

    async def send(self, event: dict, channel: dict) -> AdapterResult:
        FakeInboxMcAdapter.calls.append((event, channel))
        return AdapterResult(status="delivered", provider_message_id="mc-1", latency_ms=0)


@pytest.fixture(autouse=True)
def _reset() -> None:
    FakeTelegramAdapter.calls.clear()
    FakeInboxMcAdapter.calls.clear()
    reset_dedupe_state()
    reset_cooldown_state()
    reset_flapping_state()
    yield
    reset_dedupe_state()
    reset_cooldown_state()
    reset_flapping_state()


@pytest.fixture(autouse=True)
def _stub_adapters(monkeypatch) -> None:
    def fake_get_adapter(channel_type: str):
        if channel_type == "telegram":
            return FakeTelegramAdapter()
        if channel_type == "inbox_mc":
            return FakeInboxMcAdapter()
        return None

    monkeypatch.setattr("gateway.hub.pipeline.get_adapter", fake_get_adapter)


def _channel_set_with_telegram_and_inbox() -> dict:
    return {
        "id": "cs_mixed",
        "members": [
            {"position": 1, "channel": {"id": "ch_tg", "type": "telegram", "target_ref": "123"}},
            {"position": 2, "channel": {"id": "ch_inbox", "type": "inbox_mc"}},
        ],
    }


def _intent(**overrides) -> NotificationIntent:
    defaults = dict(
        source_slug="paperclip",
        topic="ops.deploy",
        severity="info",
        urgency="none",
        actionability="info",
        audience="marco",
        title="hello",
        body="world",
    )
    defaults.update(overrides)
    return NotificationIntent(**defaults)


@pytest.mark.asyncio
async def test_dev_source_skips_telegram_keeps_inbox() -> None:
    """Source slug ending in -test → telegram channel filtered, inbox_mc delivered."""
    rule = {"id": "r1", "slug": "ops-info", "priority": 3, "channel_set_id": "cs_mixed"}
    registry = FakeRegistry(
        source={"id": "src_dev", "slug": "paperclip-test", "enabled": True, "severity_max": "warn"},
        topic={"id": "t1", "slug": "ops.deploy",
               "default_audience_id": "aud_marco", "default_audience_slug": "marco"},
        rules=[rule],
        channel_set=_channel_set_with_telegram_and_inbox(),
    )
    result = await run_pipeline(
        _intent(source_slug="paperclip-test"),
        source_token_hash="h", registry=registry,
    )

    # Telegram adapter must NOT have been called for a dev source.
    assert FakeTelegramAdapter.calls == []
    # Inbox-MC adapter MUST still receive the event (audit trail).
    assert len(FakeInboxMcAdapter.calls) == 1

    # Delivery list contains both channels — telegram failed via guard, inbox delivered.
    statuses = {d.channel_id: d.status for d in result.deliveries}
    assert statuses["ch_tg"] == "failed"
    assert statuses["ch_inbox"] == "delivered"

    # Audit must include dev_guard_skip for the telegram channel.
    actions = [c.kwargs.get("action") for c in registry.post_audit.await_args_list]
    assert "dev_guard_skip" in actions


@pytest.mark.asyncio
async def test_prod_source_unchanged_dispatches_to_all_channels() -> None:
    """Source slug without dev pattern → both telegram and inbox_mc receive the event."""
    rule = {"id": "r1", "slug": "ops-info", "priority": 3, "channel_set_id": "cs_mixed"}
    registry = FakeRegistry(
        source={"id": "src_prod", "slug": "paperclip", "enabled": True, "severity_max": "warn"},
        topic={"id": "t1", "slug": "ops.deploy",
               "default_audience_id": "aud_marco", "default_audience_slug": "marco"},
        rules=[rule],
        channel_set=_channel_set_with_telegram_and_inbox(),
    )
    result = await run_pipeline(_intent(), source_token_hash="h", registry=registry)

    assert len(FakeTelegramAdapter.calls) == 1
    assert len(FakeInboxMcAdapter.calls) == 1
    assert result.status == "delivered"

    actions = [c.kwargs.get("action") for c in registry.post_audit.await_args_list]
    assert "dev_guard_skip" not in actions
