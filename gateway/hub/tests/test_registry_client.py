"""Tests for gateway.hub.registry_client."""
import json
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
