"""Tests for gateway.hub.adapters.telegram."""
import json

import httpx
import pytest

from gateway.hub.adapters.errors import AdapterDeliveryError
from gateway.hub.adapters.telegram import TelegramAdapter


def make_adapter(handler) -> TelegramAdapter:
    transport = httpx.MockTransport(handler)
    client = httpx.AsyncClient(transport=transport)
    return TelegramAdapter(client=client)


@pytest.mark.asyncio
async def test_success_200(monkeypatch) -> None:
    monkeypatch.setenv("TELEGRAM_HERMES_BOT_TOKEN", "test-token")
    captured = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["path"] = request.url.path
        captured["body"] = json.loads(request.content)
        return httpx.Response(200, json={"ok": True, "result": {"message_id": 42}})

    adapter = make_adapter(handler)
    result = await adapter.send(
        event={"body": "Deploy complete"},
        channel={"type": "telegram", "target": "12345"},
    )

    assert captured["path"] == "/bottest-token/sendMessage"
    assert captured["body"]["chat_id"] == "12345"
    assert captured["body"]["parse_mode"] == "MarkdownV2"
    assert result.status == "delivered"
    assert result.provider_message_id == "42"
    await adapter.close()


@pytest.mark.asyncio
async def test_429_rate_limit(monkeypatch) -> None:
    monkeypatch.setenv("TELEGRAM_HERMES_BOT_TOKEN", "test-token")

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(429, json={"ok": False, "description": "Too Many Requests"})

    adapter = make_adapter(handler)
    with pytest.raises(AdapterDeliveryError) as exc_info:
        await adapter.send(event={"body": "Slow down"}, channel={"chat_id": "12345"})

    assert exc_info.value.status == 429
    assert "Too Many Requests" in exc_info.value.body
    await adapter.close()


@pytest.mark.asyncio
async def test_403_forbidden(monkeypatch) -> None:
    monkeypatch.setenv("TELEGRAM_HERMES_BOT_TOKEN", "test-token")

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(403, json={"ok": False, "description": "Forbidden"})

    adapter = make_adapter(handler)
    with pytest.raises(AdapterDeliveryError) as exc_info:
        await adapter.send(event={"body": "No access"}, channel={"target": "12345"})

    assert exc_info.value.status == 403
    assert "Forbidden" in exc_info.value.body
    await adapter.close()


@pytest.mark.asyncio
async def test_timeout(monkeypatch) -> None:
    monkeypatch.setenv("TELEGRAM_HERMES_BOT_TOKEN", "test-token")

    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("timed out", request=request)

    adapter = make_adapter(handler)
    with pytest.raises(AdapterDeliveryError) as exc_info:
        await adapter.send(event={"body": "Timeout"}, channel={"target": "12345"})

    assert exc_info.value.status == "timeout"
    await adapter.close()


@pytest.mark.asyncio
async def test_markdownv2_escape(monkeypatch) -> None:
    monkeypatch.setenv("TELEGRAM_HERMES_BOT_TOKEN", "test-token")
    captured = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["body"] = json.loads(request.content)
        return httpx.Response(200, json={"ok": True, "result": {"message_id": 7}})

    adapter = make_adapter(handler)
    await adapter.send(
        event={"body": "Hello _team_ *now*. Go!"},
        channel={"target": "12345"},
    )

    assert captured["body"]["text"] == r"Hello \_team\_ \*now\*\. Go\!"
    await adapter.close()
