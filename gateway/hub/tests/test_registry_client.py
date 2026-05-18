"""Tests for gateway.hub.registry_client."""
import json
from unittest.mock import MagicMock, patch
import pytest
import httpx
from gateway.hub.registry_client import RegistryClient


def make_client(handler) -> RegistryClient:
    transport = httpx.MockTransport(handler)
    mock_client = httpx.AsyncClient(
        base_url="http://test",
        transport=transport,
        headers={"Authorization": "Bearer test-token"},
    )
    return RegistryClient(base_url="http://test", token="test-token", client=mock_client)


@pytest.mark.asyncio
async def test_get_source_returns_data() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/api/board/notifications/sources/test-src"
        return httpx.Response(200, json={"data": {"id": "src_1", "slug": "test-src"}})
    rc = make_client(handler)
    result = await rc.get_source("test-src")
    assert result == {"id": "src_1", "slug": "test-src"}
    await rc.close()


@pytest.mark.asyncio
async def test_get_source_404_returns_none() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(404, json={"error": "not found"})
    rc = make_client(handler)
    result = await rc.get_source("missing")
    assert result is None
    await rc.close()


@pytest.mark.asyncio
async def test_get_source_caches_within_ttl() -> None:
    call_count = {"n": 0}
    def handler(request: httpx.Request) -> httpx.Response:
        call_count["n"] += 1
        return httpx.Response(200, json={"data": {"id": "src_1"}})
    rc = make_client(handler)
    await rc.get_source("test")
    await rc.get_source("test")
    assert call_count["n"] == 1  # second call from cache
    await rc.close()


@pytest.mark.asyncio
async def test_get_topic_returns_data() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"data": {"slug": "home.test"}})
    rc = make_client(handler)
    result = await rc.get_topic("home.test")
    assert result["slug"] == "home.test"
    await rc.close()


@pytest.mark.asyncio
async def test_get_rules_passes_query_params() -> None:
    captured = {}
    def handler(request: httpx.Request) -> httpx.Response:
        captured["params"] = dict(request.url.params)
        return httpx.Response(200, json={"data": []})
    rc = make_client(handler)
    await rc.get_rules(topic="home.test", audience="marco", severity="warn")
    assert captured["params"]["topic"] == "home.test"
    assert captured["params"]["severity"] == "warn"
    await rc.close()


@pytest.mark.asyncio
async def test_get_channel_set_expanded() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"data": {"id": "cs_1", "members": []}})
    rc = make_client(handler)
    result = await rc.get_channel_set_expanded("cs_1")
    assert result["id"] == "cs_1"
    await rc.close()


@pytest.mark.asyncio
async def test_post_audit() -> None:
    captured = {}
    def handler(request: httpx.Request) -> httpx.Response:
        captured["body"] = json.loads(request.content)
        return httpx.Response(201, json={"data": {"stored": True}})
    rc = make_client(handler)
    await rc.post_audit(event_id="evt_1", actor="hermes-dispatcher", action="rule_matched")
    assert captured["body"]["actor"] == "hermes-dispatcher"
    await rc.close()


@pytest.mark.asyncio
async def test_persist_deliveries_posts_to_mc() -> None:
    """persist_deliveries POSTs deliveries array to MC route for given event_id."""
    captured = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["path"] = request.url.path
        captured["body"] = json.loads(request.content)
        captured["auth"] = request.headers.get("authorization", "")
        return httpx.Response(200, json={"ok": True, "count": 1})

    rc = make_client(handler)
    deliveries = [
        {"channel_id": "ch_x", "status": "delivered", "provider_message_id": "42", "latency_ms": 100},
    ]
    await rc.persist_deliveries(event_id="evt_1", deliveries=deliveries)

    assert "/events/evt_1/deliveries" in captured["path"]
    assert captured["body"] == {"deliveries": deliveries}
    assert "Bearer test-token" in captured["auth"]
    await rc.close()


@pytest.mark.asyncio
async def test_persist_deliveries_swallows_errors_non_blocking() -> None:
    """Persistence is best-effort; failures must NOT raise."""
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.HTTPError("MC down")

    rc = make_client(handler)
    # Should NOT raise — must return cleanly even on transport error
    await rc.persist_deliveries(event_id="evt_1", deliveries=[])
    await rc.close()


@pytest.mark.asyncio
async def test_persist_deliveries_swallows_http_500() -> None:
    """Non-2xx response (raise_for_status) must also be swallowed."""
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, json={"error": "boom"})

    rc = make_client(handler)
    await rc.persist_deliveries(event_id="evt_1", deliveries=[{"channel_id": "ch_x", "status": "delivered"}])
    await rc.close()


@pytest.mark.asyncio
async def test_get_live_event_by_fingerprint_returns_event() -> None:
    """Happy path: 200 + event dict → returns event dict."""
    captured = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["path"] = request.url.path
        captured["params"] = dict(request.url.params)
        captured["auth"] = request.headers.get("authorization", "")
        return httpx.Response(
            200,
            json={
                "event": {
                    "id": "evt_42",
                    "provider_message_id": "777",
                    "channel_id": "ch_x",
                }
            },
        )

    rc = make_client(handler)
    result = await rc.get_live_event_by_fingerprint("drobo-backup", "fp123")

    assert "/events/by-fingerprint" in captured["path"]
    assert captured["params"]["source"] == "drobo-backup"
    assert captured["params"]["fingerprint"] == "fp123"
    assert "Bearer test-token" in captured["auth"]
    assert result == {
        "id": "evt_42",
        "provider_message_id": "777",
        "channel_id": "ch_x",
    }
    await rc.close()


@pytest.mark.asyncio
async def test_get_live_event_by_fingerprint_null_event_returns_none() -> None:
    """200 + {event: null} → returns None."""
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"event": None})

    rc = make_client(handler)
    result = await rc.get_live_event_by_fingerprint("src", "fp")
    assert result is None
    await rc.close()


@pytest.mark.asyncio
async def test_get_live_event_by_fingerprint_network_error_returns_none() -> None:
    """Best-effort: network error → returns None, does NOT raise."""
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.HTTPError("MC down")

    rc = make_client(handler)
    result = await rc.get_live_event_by_fingerprint("src", "fp")
    assert result is None
    await rc.close()
