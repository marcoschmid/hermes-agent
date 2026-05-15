"""Tests for gateway.hub.adapter_registry (Step 8)."""
import pytest

from gateway.hub.adapter_registry import (
    ADAPTER_MAP,
    AdapterResult,
    InboxMcAdapter,
    dispatch_to_channel_set,
    get_adapter,
)
from gateway.hub.adapters.errors import AdapterDeliveryError


class FakeDeliveredInboxAdapter:
    name = "inbox_mc"

    async def send(self, event: dict, channel: dict) -> AdapterResult:
        return AdapterResult(status="delivered", provider_message_id=None, latency_ms=0)


class FakeDeliveredTelegramAdapter:
    name = "telegram"

    async def send(self, event: dict, channel: dict) -> AdapterResult:
        return AdapterResult(
            status="delivered",
            provider_message_id="tg-msg-id",
            latency_ms=10,
        )


def make_event() -> dict:
    return {
        "source_slug": "paperclip",
        "topic": "ops.deploy",
        "severity": "info",
        "urgency": "none",
        "actionability": "info",
        "audience": "marco",
        "title": "Hello",
        "body": "World",
    }


def test_get_adapter_inbox_mc_returns_instance() -> None:
    adapter = get_adapter("inbox_mc")
    assert adapter is not None
    assert isinstance(adapter, InboxMcAdapter)
    assert adapter.name == "inbox_mc"


def test_get_adapter_unknown_returns_none() -> None:
    assert get_adapter("does-not-exist") is None
    assert get_adapter("") is None


@pytest.mark.asyncio
async def test_inbox_mc_adapter_send_requires_token(monkeypatch) -> None:
    monkeypatch.delenv("MC_HUB_TOKEN", raising=False)
    adapter = InboxMcAdapter()
    with pytest.raises(AdapterDeliveryError) as excinfo:
        await adapter.send(event=make_event(), channel={"type": "inbox_mc"})
    assert excinfo.value.status == "missing_token"


@pytest.mark.asyncio
async def test_dispatch_to_channel_set_two_members_returns_two_results(monkeypatch) -> None:
    monkeypatch.setitem(ADAPTER_MAP, "inbox_mc", FakeDeliveredInboxAdapter)
    monkeypatch.setitem(ADAPTER_MAP, "telegram", FakeDeliveredTelegramAdapter)
    channel_set = {
        "members": [
            {"position": 1, "channel": {"type": "inbox_mc", "id": "ch_1"}},
            {"position": 2, "channel": {"type": "telegram", "id": "ch_2"}},
        ]
    }
    results = await dispatch_to_channel_set(channel_set, event=make_event())
    assert len(results) == 2
    assert all(r.status == "delivered" for r in results)
    # Position-order respected: inbox_mc first, telegram second.
    assert results[0].provider_message_id is None
    assert results[1].provider_message_id == "tg-msg-id"


@pytest.mark.asyncio
async def test_dispatch_to_channel_set_unknown_adapter_returns_failed_result(monkeypatch) -> None:
    monkeypatch.setitem(ADAPTER_MAP, "inbox_mc", FakeDeliveredInboxAdapter)
    channel_set = {
        "members": [
            {"position": 1, "channel": {"type": "inbox_mc"}},
            {"position": 2, "channel": {"type": "carrier-pigeon"}},
        ]
    }
    results = await dispatch_to_channel_set(channel_set, event={"id": "evt_1"})
    assert len(results) == 2
    assert results[0].status == "delivered"
    assert results[1].status == "failed"
    assert results[1].error is not None
    assert "carrier-pigeon" in results[1].error
