import json
import os

import httpx
import pytest

from gateway.hub.adapter_registry import AdapterResult
from gateway.hub.adapters.errors import AdapterDeliveryError
from gateway.hub.adapters.inbox_mc import InboxMcAdapter


def make_event(**overrides) -> dict:
    event = {
        "source_slug": "paperclip",
        "topic": "ops.deploy",
        "severity": "info",
        "urgency": "none",
        "actionability": "info",
        "audience": "marco",
        "title": "Deploy complete",
        "body": "Production deploy finished.",
        "dedupe_key": "deploy-1",
        "correlation_id": "corr-1",
        "payload": {"service": "api"},
    }
    event.update(overrides)
    return event


async def send_with_transport(handler, monkeypatch, event: dict | None = None) -> AdapterResult:
    monkeypatch.setenv("MC_HUB_BASE_URL", "http://mc.test")
    monkeypatch.setenv("MC_HUB_TOKEN", "test-token")
    transport = httpx.MockTransport(handler)
    async with httpx.AsyncClient(
        transport=transport,
        base_url=os.environ["MC_HUB_BASE_URL"],
    ) as client:
        adapter = InboxMcAdapter(client=client)
        return await adapter.send(event if event is not None else make_event(), {"type": "inbox_mc"})


@pytest.mark.asyncio
async def test_success(monkeypatch) -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        assert request.method == "POST"
        assert str(request.url) == "http://mc.test/api/board/notifications/events"
        assert request.headers["authorization"] == "Bearer test-token"
        body = json.loads(request.content)
        assert body["source_slug"] == "paperclip"
        assert body["topic"] == "ops.deploy"
        return httpx.Response(201, json={"event_id": "mc-event-id"})

    result = await send_with_transport(handler, monkeypatch)

    assert result.status == "delivered"
    assert result.provider_message_id == "mc-event-id"


@pytest.mark.asyncio
async def test_401_raises(monkeypatch) -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(401, text="unauthorized")

    with pytest.raises(AdapterDeliveryError) as excinfo:
        await send_with_transport(handler, monkeypatch)

    assert excinfo.value.status == 401
    assert excinfo.value.body == "unauthorized"


@pytest.mark.asyncio
async def test_422_raises(monkeypatch) -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(422, json={"error": "schema violation"})

    with pytest.raises(AdapterDeliveryError) as excinfo:
        await send_with_transport(handler, monkeypatch)

    assert excinfo.value.status == 422
    assert "schema violation" in excinfo.value.body
    assert "schema violation" in str(excinfo.value)


@pytest.mark.asyncio
async def test_5xx_raises(monkeypatch) -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, text="internal error")

    with pytest.raises(AdapterDeliveryError) as excinfo:
        await send_with_transport(handler, monkeypatch)

    assert excinfo.value.status == 500
    assert excinfo.value.body == "internal error"


@pytest.mark.asyncio
async def test_timeout(monkeypatch) -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.TimeoutException("timed out", request=request)

    with pytest.raises(AdapterDeliveryError) as excinfo:
        await send_with_transport(handler, monkeypatch)

    assert excinfo.value.status == "timeout"
