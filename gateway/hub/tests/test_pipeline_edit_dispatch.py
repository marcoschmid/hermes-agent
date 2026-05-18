"""Tests for v4d-A CORE: fingerprint-aware edit-in-place dispatch.

Verifies that run_pipeline calls adapter.edit() instead of adapter.send()
when a live event with the same fingerprint exists AND a prior delivery
on this same channel has a provider_message_id.

Fallback to send() must happen if adapter.edit() raises AdapterDeliveryError.
"""
from unittest.mock import AsyncMock

import pytest

import gateway.hub.adapter_registry as adapter_registry
from gateway.hub.adapter_registry import AdapterResult
from gateway.hub.adapters.errors import AdapterDeliveryError
from gateway.hub.cooldown import reset_cooldown_state
from gateway.hub.dedupe import reset_dedupe_state
from gateway.hub.flapping import reset_flapping_state
from gateway.hub.pipeline import run_pipeline
from gateway.hub.registry_client import RegistryClient
from gateway.hub.schemas import NotificationIntent


# ---------------------------------------------------------------------------
# Fixtures + fakes
# ---------------------------------------------------------------------------

class FakeRegistry(RegistryClient):
    """Test double — overrides every network method touched by the pipeline.

    `live_event` is what get_live_event_by_fingerprint returns (None = no prior).
    """

    def __init__(
        self,
        source: dict | None = None,
        topic: dict | None = None,
        rules: list[dict] | None = None,
        channel_set: dict | None = None,
        live_event: dict | None = None,
    ) -> None:
        # Skip super().__init__ — no httpx client needed.
        self._source = source
        self._topic = topic
        self._rules = rules if rules is not None else []
        self._channel_set = channel_set
        self._live_event = live_event
        self.post_audit = AsyncMock(return_value=None)
        self.get_live_event_by_fingerprint = AsyncMock(
            return_value=self._live_event
        )

    async def get_source(self, slug, token_hash=None):
        return self._source

    async def get_topic(self, slug):
        return self._topic

    async def get_rules(self, topic, audience, severity,
                        urgency="none", actionability="info", source_id=None):
        return self._rules

    async def get_channel_set_expanded(self, channel_set_id):
        return self._channel_set


class FakeTelegramAdapter:
    """Records send/edit calls so tests can assert routing decisions."""
    name = "telegram"

    def __init__(self) -> None:
        self.send = AsyncMock(
            return_value=AdapterResult(
                status="delivered",
                provider_message_id="tg_new_999",
                latency_ms=12,
            )
        )
        self.edit = AsyncMock(
            return_value=AdapterResult(
                status="edited",
                provider_message_id="tg_existing_777",
                latency_ms=8,
            )
        )


@pytest.fixture(autouse=True)
def _reset_pipeline_state():
    reset_dedupe_state()
    reset_cooldown_state()
    reset_flapping_state()
    yield
    reset_dedupe_state()
    reset_cooldown_state()
    reset_flapping_state()


@pytest.fixture
def fake_telegram(monkeypatch) -> FakeTelegramAdapter:
    """Replace the telegram adapter singleton with our spy."""
    adapter = FakeTelegramAdapter()

    def fake_get_adapter(channel_type: str):
        if channel_type == "telegram":
            return adapter
        return None

    monkeypatch.setattr("gateway.hub.pipeline.get_adapter", fake_get_adapter)
    return adapter


def make_intent(**overrides) -> NotificationIntent:
    defaults = dict(
        source_slug="drobo-backup",
        topic="ops.deploy",
        severity="info",
        urgency="none",
        actionability="info",
        audience="marco",
        title="Hello",
        body="World",
    )
    defaults.update(overrides)
    return NotificationIntent(**defaults)


def make_source(**overrides) -> dict:
    defaults = {"id": "src_drobo", "slug": "drobo-backup",
                "enabled": True, "severity_max": "warn"}
    defaults.update(overrides)
    return defaults


def make_topic(**overrides) -> dict:
    defaults = {"id": "top_1", "slug": "ops.deploy",
                "default_audience_id": "aud_marco",
                "default_audience_slug": "marco"}
    defaults.update(overrides)
    return defaults


def make_rule_and_channel_set(channel_id="ch_tg_marco"):
    rule = {"id": "rule_1", "slug": "ops-info",
            "priority": 3, "channel_set_id": "cs_1"}
    channel_set = {
        "id": "cs_1",
        "members": [
            {"position": 1,
             "channel": {"id": channel_id, "type": "telegram",
                         "target_ref": "12345"}},
        ],
    }
    return rule, channel_set


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_dispatch_calls_edit_when_fingerprint_has_live_event_on_same_channel(
    fake_telegram,
):
    """v4d-A core: if event has fingerprint matching a live prior event
    AND prior delivery is on this same channel with provider_message_id,
    call adapter.edit() instead of adapter.send()."""
    rule, channel_set = make_rule_and_channel_set(channel_id="ch_tg_marco")
    prior_event = {
        "id": "evt_42",
        "provider_message_id": "777",
        "channel_id": "ch_tg_marco",
    }
    registry = FakeRegistry(
        source=make_source(),
        topic=make_topic(),
        rules=[rule],
        channel_set=channel_set,
        live_event=prior_event,
    )

    intent = make_intent(fingerprint="drobo/error/2026-05-18")
    result = await run_pipeline(intent, source_token_hash="h", registry=registry)

    # registry was consulted for the fingerprint, and channel_id of the
    # dispatch channel was plumbed through (Task 3.5b) so MC filters its
    # delivery JOIN to the same channel.
    registry.get_live_event_by_fingerprint.assert_awaited_once_with(
        "drobo-backup", "drobo/error/2026-05-18", channel_id="ch_tg_marco"
    )
    # adapter.edit was called, NOT send
    fake_telegram.edit.assert_awaited_once()
    fake_telegram.send.assert_not_called()
    # Edit was passed the prior message_id
    edit_kwargs = fake_telegram.edit.await_args.kwargs
    assert edit_kwargs["message_id"] == "777"
    assert edit_kwargs["channel"]["id"] == "ch_tg_marco"
    # Pipeline returns 'delivered' (edited maps to delivered for aggregate)
    assert result.status == "delivered"
    assert len(result.deliveries) == 1
    assert result.deliveries[0].status == "edited"


@pytest.mark.asyncio
async def test_dispatch_calls_send_when_no_fingerprint(fake_telegram):
    """No fingerprint → always send (no registry lookup)."""
    rule, channel_set = make_rule_and_channel_set()
    registry = FakeRegistry(
        source=make_source(),
        topic=make_topic(),
        rules=[rule],
        channel_set=channel_set,
        live_event=None,
    )

    intent = make_intent()  # no fingerprint
    result = await run_pipeline(intent, source_token_hash="h", registry=registry)

    # No registry lookup when no fingerprint present
    registry.get_live_event_by_fingerprint.assert_not_called()
    fake_telegram.send.assert_awaited_once()
    fake_telegram.edit.assert_not_called()
    assert result.status == "delivered"


@pytest.mark.asyncio
async def test_dispatch_calls_send_when_fingerprint_lookup_returns_none(
    fake_telegram,
):
    """Fingerprint present but no live prior event → send (no edit)."""
    rule, channel_set = make_rule_and_channel_set()
    registry = FakeRegistry(
        source=make_source(),
        topic=make_topic(),
        rules=[rule],
        channel_set=channel_set,
        live_event=None,
    )

    intent = make_intent(fingerprint="drobo/error/first-fire")
    result = await run_pipeline(intent, source_token_hash="h", registry=registry)

    registry.get_live_event_by_fingerprint.assert_awaited_once()
    fake_telegram.send.assert_awaited_once()
    fake_telegram.edit.assert_not_called()
    assert result.status == "delivered"


@pytest.mark.asyncio
async def test_dispatch_falls_back_to_send_when_edit_raises(fake_telegram):
    """If adapter.edit raises AdapterDeliveryError (e.g. message deleted, >48h),
    fall back to adapter.send. Notification must NOT be lost."""
    rule, channel_set = make_rule_and_channel_set(channel_id="ch_tg_marco")
    prior_event = {
        "id": "evt_42",
        "provider_message_id": "777",
        "channel_id": "ch_tg_marco",
    }
    registry = FakeRegistry(
        source=make_source(),
        topic=make_topic(),
        rules=[rule],
        channel_set=channel_set,
        live_event=prior_event,
    )

    # edit raises; send still succeeds
    fake_telegram.edit.side_effect = AdapterDeliveryError(
        400, "Bad Request: message to edit not found"
    )

    intent = make_intent(fingerprint="drobo/error/old")
    result = await run_pipeline(intent, source_token_hash="h", registry=registry)

    # Both adapters were called: edit first (raised), then send fallback
    fake_telegram.edit.assert_awaited_once()
    fake_telegram.send.assert_awaited_once()
    # Final delivery is from the send-path (status delivered, fresh msg id)
    assert result.status == "delivered"
    assert len(result.deliveries) == 1
    assert result.deliveries[0].status == "delivered"
    assert result.deliveries[0].provider_message_id == "tg_new_999"


@pytest.mark.asyncio
async def test_dispatch_calls_send_when_prior_delivery_is_different_channel(
    fake_telegram,
):
    """Edit only applies if prior delivery is on THIS channel.
    Multi-channel safety: prior was on ch_tg_other, now dispatching to
    ch_tg_marco → must fresh-send, not edit a foreign channel's message."""
    rule, channel_set = make_rule_and_channel_set(channel_id="ch_tg_marco")
    prior_event = {
        "id": "evt_42",
        "provider_message_id": "999",
        "channel_id": "ch_tg_other",  # NOT the channel we're dispatching to
    }
    registry = FakeRegistry(
        source=make_source(),
        topic=make_topic(),
        rules=[rule],
        channel_set=channel_set,
        live_event=prior_event,
    )

    intent = make_intent(fingerprint="drobo/error/multichan")
    result = await run_pipeline(intent, source_token_hash="h", registry=registry)

    fake_telegram.send.assert_awaited_once()
    fake_telegram.edit.assert_not_called()
    assert result.status == "delivered"


@pytest.mark.asyncio
async def test_dispatch_calls_send_when_prior_event_has_no_provider_message_id(
    fake_telegram,
):
    """Live prior event exists but has no provider_message_id (delivery
    failed or pending) → cannot edit, must send fresh."""
    rule, channel_set = make_rule_and_channel_set(channel_id="ch_tg_marco")
    prior_event = {
        "id": "evt_42",
        "provider_message_id": None,
        "channel_id": "ch_tg_marco",
    }
    registry = FakeRegistry(
        source=make_source(),
        topic=make_topic(),
        rules=[rule],
        channel_set=channel_set,
        live_event=prior_event,
    )

    intent = make_intent(fingerprint="drobo/error/pending")
    result = await run_pipeline(intent, source_token_hash="h", registry=registry)

    fake_telegram.send.assert_awaited_once()
    fake_telegram.edit.assert_not_called()
    assert result.status == "delivered"
