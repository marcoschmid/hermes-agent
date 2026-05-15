"""Tests for gateway.hub.adapter_registry (Step 8)."""
import pytest

from gateway.hub.adapter_registry import (
    AdapterResult,
    InboxMcAdapter,
    dispatch_to_channel_set,
    get_adapter,
)


def test_get_adapter_inbox_mc_returns_instance() -> None:
    adapter = get_adapter("inbox_mc")
    assert adapter is not None
    assert isinstance(adapter, InboxMcAdapter)
    assert adapter.name == "inbox_mc"


def test_get_adapter_unknown_returns_none() -> None:
    assert get_adapter("does-not-exist") is None
    assert get_adapter("") is None


@pytest.mark.asyncio
async def test_inbox_mc_adapter_send_always_delivered() -> None:
    adapter = InboxMcAdapter()
    result = await adapter.send(event={"id": "evt_1"}, channel={"type": "inbox_mc"})
    assert isinstance(result, AdapterResult)
    assert result.status == "delivered"
    assert result.error is None


@pytest.mark.asyncio
async def test_dispatch_to_channel_set_two_members_returns_two_results() -> None:
    channel_set = {
        "members": [
            {"position": 1, "channel": {"type": "inbox_mc", "id": "ch_1"}},
            {"position": 2, "channel": {"type": "telegram", "id": "ch_2"}},
        ]
    }
    results = await dispatch_to_channel_set(channel_set, event={"id": "evt_1"})
    assert len(results) == 2
    assert all(r.status == "delivered" for r in results)
    # Position-order respected: inbox_mc first (no provider id), telegram second (stub id).
    assert results[0].provider_message_id is None
    assert results[1].provider_message_id == "stub-tg-msg"


@pytest.mark.asyncio
async def test_dispatch_to_channel_set_unknown_adapter_returns_failed_result() -> None:
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
