"""Tests for gateway.hub.adapters.pushover."""
from urllib.parse import parse_qs

import httpx
import pytest

from gateway.hub.adapters.errors import AdapterDeliveryError
from gateway.hub.adapters.pushover import (
    EMERGENCY_EXPIRE_SECONDS,
    EMERGENCY_RETRY_SECONDS,
    PUSHOVER_API_URL,
    PushoverAdapter,
    SEVERITY_TO_PRIORITY,
)


def make_adapter(handler) -> PushoverAdapter:
    transport = httpx.MockTransport(handler)
    client = httpx.AsyncClient(transport=transport)
    return PushoverAdapter(client=client)


CH = {
    "type": "pushover",
    "target_ref": "po_marco",
    "config": {"app_token": "appT", "user_key": "userK"},
}


@pytest.mark.asyncio
async def test_send_posts_form_payload_with_priority() -> None:
    captured: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["url"] = str(request.url)
        captured["content_type"] = request.headers.get("content-type", "")
        captured["body"] = parse_qs(request.content.decode())
        return httpx.Response(200, json={"status": 1, "request": "req-abc"})

    adapter = make_adapter(handler)
    event = {"severity": "warn", "title": "T1", "body": "B1"}
    result = await adapter.send(event, CH)

    assert result.status == "delivered"
    assert result.provider_message_id == "req-abc"
    assert captured["url"] == PUSHOVER_API_URL
    assert "application/x-www-form-urlencoded" in captured["content_type"]
    body = captured["body"]
    assert body["token"] == ["appT"]
    assert body["user"] == ["userK"]
    assert body["title"] == ["T1"]
    assert body["message"] == ["B1"]
    assert body["priority"] == ["1"]
    assert "retry" not in body
    assert "expire" not in body


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "severity,expected",
    [
        ("debug", -2),
        ("info", -2),
        ("notice", 0),
        ("warn", 1),
        ("error", 2),
        ("crit", 2),
    ],
)
async def test_severity_priority_mapping(severity: str, expected: int) -> None:
    captured: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["body"] = parse_qs(request.content.decode())
        return httpx.Response(200, json={"status": 1, "request": "x"})

    adapter = make_adapter(handler)
    await adapter.send({"severity": severity, "title": "t", "body": "b"}, CH)
    assert int(captured["body"]["priority"][0]) == expected
    assert SEVERITY_TO_PRIORITY[severity] == expected


@pytest.mark.asyncio
async def test_emergency_priority_adds_retry_and_expire() -> None:
    captured: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["body"] = parse_qs(request.content.decode())
        return httpx.Response(200, json={"status": 1, "receipt": "rcpt-xyz"})

    adapter = make_adapter(handler)
    result = await adapter.send({"severity": "error", "title": "t", "body": "b"}, CH)
    body = captured["body"]
    assert body["priority"] == ["2"]
    assert int(body["retry"][0]) == EMERGENCY_RETRY_SECONDS
    assert int(body["expire"][0]) == EMERGENCY_EXPIRE_SECONDS
    assert result.provider_message_id == "rcpt-xyz"


@pytest.mark.asyncio
async def test_crit_severity_uses_siren_sound_by_default() -> None:
    captured: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["body"] = parse_qs(request.content.decode())
        return httpx.Response(200, json={"status": 1, "request": "x"})

    adapter = make_adapter(handler)
    await adapter.send({"severity": "crit", "title": "t", "body": "b"}, CH)
    assert captured["body"]["sound"] == ["siren"]


@pytest.mark.asyncio
async def test_config_sound_overrides_default() -> None:
    captured: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["body"] = parse_qs(request.content.decode())
        return httpx.Response(200, json={"status": 1, "request": "x"})

    adapter = make_adapter(handler)
    channel = {
        "type": "pushover",
        "target_ref": "po_marco",
        "config": {"app_token": "appT", "user_key": "userK", "sound": "magic"},
    }
    await adapter.send({"severity": "crit", "title": "t", "body": "b"}, channel)
    assert captured["body"]["sound"] == ["magic"]


@pytest.mark.asyncio
async def test_device_field_optional_passed_when_present() -> None:
    captured: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["body"] = parse_qs(request.content.decode())
        return httpx.Response(200, json={"status": 1, "request": "x"})

    adapter = make_adapter(handler)
    channel = {
        "type": "pushover",
        "target_ref": "po_marco",
        "config": {"app_token": "appT", "user_key": "userK", "device": "iphone"},
    }
    await adapter.send({"severity": "info", "title": "t", "body": "b"}, channel)
    assert captured["body"]["device"] == ["iphone"]


@pytest.mark.asyncio
async def test_missing_app_token_raises() -> None:
    def handler(request):
        return httpx.Response(200, json={"status": 1})

    adapter = make_adapter(handler)
    channel = {"type": "pushover", "config": {"user_key": "userK"}}
    with pytest.raises(AdapterDeliveryError) as excinfo:
        await adapter.send({"severity": "info", "title": "t", "body": "b"}, channel)
    assert "PUSHOVER_APP_TOKEN" in str(excinfo.value)


@pytest.mark.asyncio
async def test_app_token_from_env_when_config_missing(monkeypatch) -> None:
    monkeypatch.setenv("PUSHOVER_APP_TOKEN", "env-token-xyz")
    captured: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["body"] = parse_qs(request.content.decode())
        return httpx.Response(200, json={"status": 1, "request": "x"})

    adapter = make_adapter(handler)
    channel = {"type": "pushover", "config": {"user_key": "userK"}}
    await adapter.send({"severity": "info", "title": "t", "body": "b"}, channel)
    assert captured["body"]["token"] == ["env-token-xyz"]


@pytest.mark.asyncio
async def test_missing_user_key_raises() -> None:
    def handler(request):
        return httpx.Response(200, json={"status": 1})

    adapter = make_adapter(handler)
    channel = {"type": "pushover", "config": {"app_token": "appT"}}
    with pytest.raises(AdapterDeliveryError) as excinfo:
        await adapter.send({"severity": "info", "title": "t", "body": "b"}, channel)
    assert "user_key" in str(excinfo.value)


@pytest.mark.asyncio
async def test_400_raises_adapter_error() -> None:
    def handler(request):
        return httpx.Response(400, text='{"errors":["invalid token"]}')

    adapter = make_adapter(handler)
    with pytest.raises(AdapterDeliveryError) as excinfo:
        await adapter.send({"severity": "info", "title": "t", "body": "b"}, CH)
    assert excinfo.value.status == 400


@pytest.mark.asyncio
async def test_429_rate_limit_raises() -> None:
    def handler(request):
        return httpx.Response(429, text="rate limited")

    adapter = make_adapter(handler)
    with pytest.raises(AdapterDeliveryError) as excinfo:
        await adapter.send({"severity": "info", "title": "t", "body": "b"}, CH)
    assert excinfo.value.status == 429


@pytest.mark.asyncio
async def test_5xx_raises() -> None:
    def handler(request):
        return httpx.Response(503, text="upstream down")

    adapter = make_adapter(handler)
    with pytest.raises(AdapterDeliveryError) as excinfo:
        await adapter.send({"severity": "warn", "title": "t", "body": "b"}, CH)
    assert excinfo.value.status == 503


@pytest.mark.asyncio
async def test_timeout_raises() -> None:
    def handler(request):
        raise httpx.TimeoutException("slow")

    adapter = make_adapter(handler)
    with pytest.raises(AdapterDeliveryError) as excinfo:
        await adapter.send({"severity": "info", "title": "t", "body": "b"}, CH)
    assert excinfo.value.status == "timeout"


@pytest.mark.asyncio
async def test_network_error_raises() -> None:
    def handler(request):
        raise httpx.ConnectError("refused")

    adapter = make_adapter(handler)
    with pytest.raises(AdapterDeliveryError) as excinfo:
        await adapter.send({"severity": "info", "title": "t", "body": "b"}, CH)
    assert excinfo.value.status == "network"


@pytest.mark.asyncio
async def test_registry_picks_pushover_for_channel_type() -> None:
    from gateway.hub.adapter_registry import ADAPTER_MAP, get_adapter

    assert ADAPTER_MAP["pushover"] is PushoverAdapter
    adapter = get_adapter("pushover")
    assert isinstance(adapter, PushoverAdapter)
