"""Tests for gateway.hub.sender_client."""
from __future__ import annotations

import json
import uuid
from datetime import datetime, timezone

import httpx
import pytest

from gateway.hub.hmac_sign import sign, verify
from gateway.hub.sender_client import HubSenderClient, SenderError


_SECRET = b"test-secret-bytes-32-chars-long-x"


@pytest.mark.asyncio
async def test_send_signs_correctly_via_mock_transport() -> None:
    captured: dict[str, object] = {}

    async def handler(request: httpx.Request) -> httpx.Response:
        captured["method"] = request.method
        captured["url"] = str(request.url)
        captured["headers"] = request.headers
        captured["body"] = request.content
        return httpx.Response(201, json={"data": {"event_id": "evt_1", "status": "queued"}})

    transport = httpx.MockTransport(handler)
    async with httpx.AsyncClient(transport=transport) as http_client:
        sender = HubSenderClient(
            "http://hub.test",
            source_slug="paperclip",
            hub_secret=_SECRET,
            client=http_client,
        )

        result = await sender.send(
            topic="ops.deploy",
            audience="marco",
            title="Deploy complete",
            body="Production deploy finished.",
            dedupe_key="deploy-1",
            correlation_id="corr-1",
            payload={"service": "api"},
        )

    assert result == {"event_id": "evt_1", "status": "queued"}
    assert captured["method"] == "POST"
    assert captured["url"] == "http://hub.test/v1/notifications"

    headers = captured["headers"]
    assert isinstance(headers, httpx.Headers)
    timestamp = headers["x-hub-timestamp"]
    nonce = headers["x-hub-nonce"]
    signature = headers["x-hub-signature"]

    assert timestamp.endswith("Z")
    parsed_timestamp = datetime.fromisoformat(timestamp.replace("Z", "+00:00"))
    assert parsed_timestamp.tzinfo is not None
    assert parsed_timestamp.microsecond == 0
    assert uuid.UUID(nonce).version == 4
    assert signature == sign(_SECRET, timestamp, nonce, captured["body"])
    assert signature == signature.lower()

    verify(
        _SECRET,
        timestamp,
        nonce,
        captured["body"],
        signature,
        now=datetime.now(timezone.utc),
    )

    request_body = json.loads(captured["body"])
    assert request_body == {
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


@pytest.mark.asyncio
async def test_send_raises_on_4xx() -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            401,
            json={"detail": {"error_code": "bad_signature", "message": "HMAC signature mismatch"}},
        )

    transport = httpx.MockTransport(handler)
    async with httpx.AsyncClient(transport=transport) as http_client:
        sender = HubSenderClient("http://hub.test", "paperclip", _SECRET, client=http_client)
        with pytest.raises(SenderError) as excinfo:
            await sender.send(
                topic="ops.deploy",
                audience="marco",
                title="Deploy complete",
                body="Production deploy finished.",
            )

    assert excinfo.value.status == 401
    assert excinfo.value.detail == {
        "error_code": "bad_signature",
        "message": "HMAC signature mismatch",
    }
    assert "bad_signature" in str(excinfo.value)


@pytest.mark.asyncio
async def test_send_raises_on_5xx() -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(503, text="hub unavailable")

    transport = httpx.MockTransport(handler)
    async with httpx.AsyncClient(transport=transport) as http_client:
        sender = HubSenderClient("http://hub.test", "paperclip", _SECRET, client=http_client)
        with pytest.raises(SenderError) as excinfo:
            await sender.send(
                topic="ops.deploy",
                audience="marco",
                title="Deploy complete",
                body="Production deploy finished.",
            )

    assert excinfo.value.status == 503
    assert excinfo.value.detail == "hub unavailable"
    assert "hub unavailable" in str(excinfo.value)


@pytest.mark.asyncio
async def test_close_idempotent() -> None:
    sender = HubSenderClient("http://hub.test", "paperclip", _SECRET)

    await sender.close()
    await sender.close()

    assert sender._client.is_closed
