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


@pytest.mark.asyncio
async def test_get_live_event_by_fingerprint_plumbs_channel_param() -> None:
    """Task 3.5b: channel_id arg → ?channel=… in request query string.

    Verifies the hub passes the dispatch channel through to MC so MC's
    delivery JOIN is filtered to the same channel — otherwise a multi-
    delivery event surfaces the wrong row and edit-in-place misses.
    """
    captured: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["params"] = dict(request.url.params)
        return httpx.Response(
            200,
            json={
                "event": {
                    "id": "evt_42",
                    "provider_message_id": "tg-mid",
                    "channel_id": "ch_tg_marco",
                }
            },
        )

    rc = make_client(handler)
    await rc.get_live_event_by_fingerprint(
        "drobo-backup", "fp123", channel_id="ch_tg_marco"
    )

    assert captured["params"]["source"] == "drobo-backup"
    assert captured["params"]["fingerprint"] == "fp123"
    assert captured["params"]["channel"] == "ch_tg_marco"
    await rc.close()


@pytest.mark.asyncio
async def test_get_live_event_by_fingerprint_omits_channel_when_not_passed() -> None:
    """Backwards-compat: no channel_id → no ?channel= in request."""
    captured: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["params"] = dict(request.url.params)
        return httpx.Response(200, json={"event": None})

    rc = make_client(handler)
    await rc.get_live_event_by_fingerprint("src", "fp")
    assert "channel" not in captured["params"]
    await rc.close()


# ---------------------------------------------------------------------------
# Channel-isolation (5d): a revoked token (401) / server error (5xx) / network
# failure on a registry read must raise the typed RegistryUnavailable instead
# of letting a raw httpx.HTTPStatusError escape (which surfaced upstream as an
# uncaught HTTP 500 → whole-hub-hop cascade on 2026-05-27). 404 / get_source-403
# keep their "not found" → None semantics so genuine unknown-source/topic
# answers are NOT masked as transient outages.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_get_source_401_raises_registry_unavailable() -> None:
    from gateway.hub.registry_client import RegistryUnavailable

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(401, json={"error": "token revoked"})

    rc = make_client(handler)
    with pytest.raises(RegistryUnavailable):
        await rc.get_source("test-src")
    await rc.close()


@pytest.mark.asyncio
async def test_get_source_500_raises_registry_unavailable() -> None:
    from gateway.hub.registry_client import RegistryUnavailable

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, json={"error": "boom"})

    rc = make_client(handler)
    with pytest.raises(RegistryUnavailable):
        await rc.get_source("test-src")
    await rc.close()


@pytest.mark.asyncio
async def test_get_source_403_still_returns_none() -> None:
    """Negative control: 403 is the MC 'unknown source / no match' answer and
    must stay None, NOT become a transient RegistryUnavailable."""
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(403, json={"error": "forbidden"})

    rc = make_client(handler)
    result = await rc.get_source("test-src")
    assert result is None
    await rc.close()


@pytest.mark.asyncio
async def test_get_topic_401_raises_registry_unavailable() -> None:
    from gateway.hub.registry_client import RegistryUnavailable

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(401, json={"error": "token revoked"})

    rc = make_client(handler)
    with pytest.raises(RegistryUnavailable):
        await rc.get_topic("home.test")
    await rc.close()


@pytest.mark.asyncio
async def test_get_topic_404_still_returns_none() -> None:
    """Negative control: unknown topic stays None."""
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(404, json={"error": "not found"})

    rc = make_client(handler)
    result = await rc.get_topic("nope")
    assert result is None
    await rc.close()


@pytest.mark.asyncio
async def test_get_rules_503_raises_registry_unavailable() -> None:
    from gateway.hub.registry_client import RegistryUnavailable

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(503, json={"error": "unavailable"})

    rc = make_client(handler)
    with pytest.raises(RegistryUnavailable):
        await rc.get_rules(topic="home.test", audience="marco", severity="warn")
    await rc.close()


@pytest.mark.asyncio
async def test_get_channel_set_502_raises_registry_unavailable() -> None:
    from gateway.hub.registry_client import RegistryUnavailable

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(502, json={"error": "bad gateway"})

    rc = make_client(handler)
    with pytest.raises(RegistryUnavailable):
        await rc.get_channel_set_expanded("cs_1")
    await rc.close()


@pytest.mark.asyncio
async def test_get_channel_set_404_still_returns_none() -> None:
    """Negative control: unknown channel-set stays None."""
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(404, json={"error": "not found"})

    rc = make_client(handler)
    result = await rc.get_channel_set_expanded("missing")
    assert result is None
    await rc.close()


@pytest.mark.asyncio
async def test_get_source_connect_error_raises_registry_unavailable() -> None:
    """A network/transport failure (MC down) must also raise RegistryUnavailable."""
    from gateway.hub.registry_client import RegistryUnavailable

    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused")

    rc = make_client(handler)
    with pytest.raises(RegistryUnavailable):
        await rc.get_source("test-src")
    await rc.close()


@pytest.mark.asyncio
async def test_get_topic_403_raises_registry_unavailable() -> None:
    """403 on a non-source endpoint is a token/permission failure (not the
    source-scoped 'not found' that get_source uses), so it must isolate as
    RegistryUnavailable rather than escape as a raw httpx error -> 500."""
    from gateway.hub.registry_client import RegistryUnavailable

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(403, json={"error": "forbidden"})

    rc = make_client(handler)
    with pytest.raises(RegistryUnavailable):
        await rc.get_topic("home.test")
    await rc.close()


@pytest.mark.asyncio
async def test_get_channel_set_403_raises_registry_unavailable() -> None:
    from gateway.hub.registry_client import RegistryUnavailable

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(403, json={"error": "forbidden"})

    rc = make_client(handler)
    with pytest.raises(RegistryUnavailable):
        await rc.get_channel_set_expanded("cs_1")
    await rc.close()


@pytest.mark.asyncio
async def test_get_rules_403_raises_registry_unavailable() -> None:
    from gateway.hub.registry_client import RegistryUnavailable

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(403, json={"error": "forbidden"})

    rc = make_client(handler)
    with pytest.raises(RegistryUnavailable):
        await rc.get_rules(topic="home.test", audience="marco", severity="warn")
    await rc.close()


@pytest.mark.asyncio
async def test_registry_unavailable_carries_status_code() -> None:
    from gateway.hub.registry_client import RegistryUnavailable

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(401, json={"error": "revoked"})

    rc = make_client(handler)
    with pytest.raises(RegistryUnavailable) as exc_info:
        await rc.get_source("test-src")
    assert exc_info.value.status_code == 401
    await rc.close()


@pytest.mark.asyncio
async def test_registry_unavailable_is_not_cached() -> None:
    """A transient failure must not poison the cache: the next call re-hits MC
    (and would succeed once MC recovers), rather than being served a cached
    None or a stuck error."""
    from gateway.hub.registry_client import RegistryUnavailable

    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        return httpx.Response(401, json={"error": "revoked"})

    rc = make_client(handler)
    with pytest.raises(RegistryUnavailable):
        await rc.get_source("test-src")
    with pytest.raises(RegistryUnavailable):
        await rc.get_source("test-src")
    assert calls["n"] == 2  # second call re-hit MC, not cached
    await rc.close()
