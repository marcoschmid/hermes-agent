"""Phase-6b ZERO-MC-READ dark-verify integration test — the read-drain payoff.

This is the proof that the Phase-6b read-drain is *complete*: in
``HUB_REGISTRY_SOURCE=mirror`` a single valid, HMAC-signed POST /v1/notifications
resolves the ENTIRE registry-read surface of one dispatch

    _authenticate      -> get_source
    validate_and_lookup -> get_source + get_topic
    find_matching_rule  -> get_rules + get_channel_set_expanded
    HMAC verify         -> secret_store (file)

out of the local SQLite mirror (migration 003) plus the file-backed secret store
— with NOT ONE Mission Control HTTP registry GET.

Mechanics of the zero-read assertion
-------------------------------------
We drive the *real* ``RegistryClient`` (not a mock), so the real
``HUB_REGISTRY_SOURCE=mirror`` branch executes. The MC HTTP layer is a real
``httpx.AsyncClient`` wired to an ``httpx.MockTransport`` whose handler RECORDS
every outbound request and **fails the test on any registry GET**. So if any
read silently fell through to MC (a leak), the request would hit the transport
and the assertion fires.

Write-sinks (``post_audit`` POST /audit, ``persist_deliveries`` POST
/events/.../deliveries) are Step-7 scope, not the prover here, so the handler
returns a benign 200 for those POSTs instead of failing — the READ path is the
only prover. The test additionally asserts ``GET_COUNT == 0`` directly.

A contrast case runs the same signed dispatch with ``HUB_REGISTRY_SOURCE=mc``
against a dead MC port and asserts it does NOT resolve from the mirror (it
fails/503s on the very first read) — proving the mirror backend is what makes
the difference.
"""
from __future__ import annotations

import json
from datetime import datetime, timezone

import httpx
import pytest
import pytest_asyncio
from fastapi import FastAPI
from fastapi.testclient import TestClient

from gateway.hub import api as hub_api
from gateway.hub import secret_store as secret_store_mod
from gateway.hub.adapter_registry import AdapterResult
from gateway.hub.api import router
from gateway.hub.hmac_sign import sign
from gateway.hub.hub_state import connect
from gateway.hub.registry_client import RegistryClient


# ---------------------------------------------------------------------------
# Test topology: source -> topic -> rule -> channel_set -> 1 inbox_mc channel,
# all keyed on audience "marco". Written DIRECTLY into the migration-003 mirror.
# ---------------------------------------------------------------------------

TEST_SECRET = "zero-mc-read-hub-secret-abc123"
SOURCE_SLUG = "weekly-preview"
TOPIC_SLUG = "home.kalender.weekly"
AUDIENCE_ID = "aud_marco"
AUDIENCE_SLUG = "marco"

BASIC_INTENT = {
    "source_slug": SOURCE_SLUG,
    "topic": TOPIC_SLUG,
    "severity": "info",
    "audience": AUDIENCE_SLUG,
    "title": "Zero-MC-Read proof",
    "body": "Resolved entirely from the local mirror.",
    # No fingerprint on purpose: get_live_event_by_fingerprint has no mirror
    # branch and would issue an MC GET. A FIRING intent without a fingerprint
    # never enters that branch, so the read path stays mirror-only. (See the
    # honest leak note in the agent report.)
}


async def _populate_mirror(state) -> None:
    """Write one consistent topology into the migration-003 mirror tables."""
    # audience
    await state.execute(
        "INSERT INTO registry_audiences (id, slug, name, enabled) VALUES (?,?,?,1)",
        (AUDIENCE_ID, AUDIENCE_SLUG, "Marco"),
    )
    # source (secret-free, exactly like _mirror_get_source returns)
    await state.execute(
        "INSERT INTO registry_sources (id, slug, name, service_type, owner, "
        "default_topic_id, default_severity, severity_max, max_severity, "
        "rate_limit_per_min, enabled, allowed_topics, allowed_audiences, synced_at) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (
            "src_1", SOURCE_SLUG, "Weekly Preview", "routine", "marco",
            "tp_1", "info", "crit", None, 60, 1, None, None, 100,
        ),
    )
    # topic (default_audience_slug present so audience resolution needs no
    # second round-trip)
    await state.execute(
        "INSERT INTO registry_topics (id, slug, name, description, default_severity, "
        "default_urgency, default_actionability, default_audience_id, "
        "default_audience_slug, retention_days, enabled, synced_at) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
        (
            "tp_1", TOPIC_SLUG, "Weekly", None, "info", "none", "info",
            AUDIENCE_ID, AUDIENCE_SLUG, 30, 1, 100,
        ),
    )
    # rule: global default for audience marco, NULL glob, priority 5
    await state.execute(
        "INSERT INTO registry_rules (id, slug, priority, topic_glob, source_id, "
        "audience_id, severity_min, urgency_min, actionability_min, quiet_class, "
        "channel_set_id, escalation_policy_id, enabled, synced_at) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (
            "rule_1", "global-default-marco", 5, None, None, AUDIENCE_ID,
            "info", "none", "info", "respect", "cset_1", None, 1, 100,
        ),
    )
    # channel: a single inbox_mc channel (mockable adapter, no real transport)
    await state.execute(
        "INSERT INTO registry_channels (id, slug, type, audience_id, target_ref, "
        "target_thread_id, target_subtype, display_name, bot_token_env_ref, "
        "rate_limit_per_min, supports_ack, supports_buttons, enabled, synced_at) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (
            "ch_inbox", "inbox-mc", "inbox_mc", AUDIENCE_ID, None, None, None,
            "Inbox MC", None, None, 0, 0, 1, 100,
        ),
    )
    # channel set + member
    await state.execute(
        "INSERT INTO registry_channel_sets (id, slug, name, audience_id, enabled, synced_at) "
        "VALUES (?,?,?,?,?,?)",
        ("cset_1", "cset-marco", "Marco Set", AUDIENCE_ID, 1, 100),
    )
    await state.execute(
        "INSERT INTO registry_channel_set_members (channel_set_id, channel_id, "
        "position, fallback_after_seconds, required) VALUES (?,?,?,?,?)",
        ("cset_1", "ch_inbox", 0, None, 1),
    )


# ---------------------------------------------------------------------------
# Spy transport: records every outbound request. A registry GET fails the test;
# the known Step-7 write POSTs (audit / deliveries) are tolerated with a 200.
# ---------------------------------------------------------------------------

# Path fragments of the MC registry READ endpoints. Any GET to one of these
# means a read leaked past the mirror backend.
_REGISTRY_READ_FRAGMENTS = (
    "/api/board/notifications/sources/",
    "/api/board/notifications/topics/",
    "/api/board/notifications/rules",
    "/api/board/notifications/channel-sets/",
    "/api/board/notifications/events/by-fingerprint",
)


class McReadSpy:
    """httpx MockTransport handler that proves no registry GET happens."""

    def __init__(self) -> None:
        self.get_requests: list[str] = []
        self.post_requests: list[str] = []

    def handler(self, request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        if request.method == "GET":
            self.get_requests.append(url)
            # Hard-fail signal: a registry GET reached the MC transport. We
            # cannot `assert` inside the transport (httpx swallows it into a
            # transport error), so return a sentinel 599 the test asserts never
            # occurred via get_requests, AND make the body identifiable.
            return httpx.Response(599, json={"leaked_read": url})
        self.post_requests.append(url)
        # Tolerate write-sinks (Step-7 scope): audit + deliveries persistence.
        return httpx.Response(200, json={"data": {}})


class FakeInboxMcAdapter:
    """Channel-send mock (same posture as test_pipeline_full): no MC transport."""

    name = "inbox_mc"

    async def send(self, event: dict, channel: dict) -> AdapterResult:
        return AdapterResult(
            status="delivered",
            provider_message_id="mc-event-id",
            latency_ms=0,
        )


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _reset_pipeline_state():
    """Reset in-memory dedupe/cooldown/flapping state per test for isolation.

    These are module-level singletons; without a reset the proof test's
    cooldown record bleeds into the fingerprint test and suppresses its
    delivery. Mirrors test_pipeline_full's autouse reset.
    """
    from gateway.hub.cooldown import reset_cooldown_state
    from gateway.hub.dedupe import reset_dedupe_state
    from gateway.hub.flapping import reset_flapping_state

    reset_dedupe_state()
    reset_cooldown_state()
    reset_flapping_state()
    yield
    reset_dedupe_state()
    reset_cooldown_state()
    reset_flapping_state()


@pytest_asyncio.fixture
async def mirror_db(tmp_path, monkeypatch):
    """A migrated mirror DB on a tmp path, populated with the test topology."""
    db_path = tmp_path / "hub_state.db"
    monkeypatch.setenv("HUB_STATE_DB_PATH", str(db_path))
    state = await connect()
    await state.migrate()
    await _populate_mirror(state)
    try:
        yield state
    finally:
        await state.close()


@pytest.fixture
def secret_file(tmp_path, monkeypatch):
    """A 0600 secret-store file carrying the test source's hub_secret."""
    path = tmp_path / "registry_secrets.json"
    path.write_text(json.dumps({SOURCE_SLUG: TEST_SECRET}), encoding="utf-8")
    path.chmod(0o600)
    monkeypatch.setenv("HUB_REGISTRY_SECRETS_PATH", str(path))
    # Reset the module-level secret-store singleton so it binds to THIS file.
    secret_store_mod._secret_store = None
    yield str(path)
    secret_store_mod._secret_store = None


@pytest.fixture
def reset_singletons():
    """Reset api-module registry + hub_state singletons before and after."""
    hub_api.set_registry(None)
    hub_api.set_hub_state(None)
    yield
    hub_api.set_registry(None)
    hub_api.set_hub_state(None)


def _iso_now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _hmac_headers(secret: str, body: bytes, *, nonce: str) -> dict:
    ts = _iso_now()
    sig = sign(secret.encode(), ts, nonce, body)
    return {"x-hub-timestamp": ts, "x-hub-nonce": nonce, "x-hub-signature": sig}


# ===========================================================================
# THE PROOF: zero MC registry GET in mirror-mode dispatch.
# ===========================================================================


@pytest.mark.asyncio
async def test_zero_mc_read_dispatch_resolves_entirely_from_mirror(
    mirror_db, secret_file, reset_singletons, monkeypatch
):
    """A signed POST /v1/notifications in mirror-mode resolves source/topic/rule/
    channel-set + verifies HMAC with NULL MC registry GET, and dispatches."""
    monkeypatch.setenv("HUB_REGISTRY_SOURCE", "mirror")
    monkeypatch.delenv("HUB_PILOT_TOKEN", raising=False)

    # Real RegistryClient in mirror mode, sharing the populated mirror state.
    # Its MC HTTP client is the spy transport: any registry GET fails the proof.
    spy = McReadSpy()
    mc_client = httpx.AsyncClient(
        base_url="http://mc-must-not-be-read.invalid",
        transport=httpx.MockTransport(spy.handler),
    )
    registry = RegistryClient(client=mc_client, state=mirror_db)
    assert registry._source == "mirror"  # env honoured
    hub_api.set_registry(registry)
    # Share the SAME mirror DB connection as the hub_state (nonce-replay store).
    hub_api.set_hub_state(mirror_db)

    # Channel-send mock: keep the inbox_mc adapter off any real transport.
    from gateway.hub import pipeline as pipeline_mod

    original_get_adapter = pipeline_mod.get_adapter

    def fake_get_adapter(channel_type: str):
        if channel_type == "inbox_mc":
            return FakeInboxMcAdapter()
        return original_get_adapter(channel_type)

    monkeypatch.setattr("gateway.hub.pipeline.get_adapter", fake_get_adapter)

    body = json.dumps(BASIC_INTENT).encode()
    headers = _hmac_headers(TEST_SECRET, body, nonce="zero-read-nonce-1")

    app = FastAPI()
    app.include_router(router)
    client = TestClient(app, raise_server_exceptions=False)

    resp = client.post("/v1/notifications", content=body, headers=headers)

    # (a) Auth succeeded — NOT 401 (and not 503/500 either).
    assert resp.status_code != 401, resp.text
    assert resp.status_code == 200, resp.text

    # (b) Registry resolved fully from the mirror → dispatch reached channel
    # routing and delivered via the inbox_mc adapter.
    data = resp.json()["data"]
    assert data["status"] == "delivered", data
    assert data["rule_matched"] == "global-default-marco", data
    assert [d["channel_id"] for d in data["deliveries"]] == ["ch_inbox"], data

    # (c) THE ZERO-MC-READ ASSERTION: not a single registry GET reached MC.
    assert spy.get_requests == [], (
        "READ LEAK: a registry GET escaped the mirror backend to Mission "
        f"Control: {spy.get_requests}"
    )
    # No registry-read endpoint was touched by ANY method either.
    for url in spy.get_requests + spy.post_requests:
        for frag in _REGISTRY_READ_FRAGMENTS:
            assert frag not in url, f"READ LEAK on {url} (fragment {frag})"

    # (d) Durable proof-of-dispatch: a hub_events_log row was written.
    row = await mirror_db.fetchone(
        "SELECT event_id, source_slug, topic_slug, status FROM hub_events_log "
        "WHERE source_slug = ? AND topic_slug = ?",
        (SOURCE_SLUG, TOPIC_SLUG),
    )
    assert row is not None, "expected a hub_events_log audit row"
    assert row["status"] == "delivered_inbox"


# ===========================================================================
# CONTRAST: mc-mode against a dead MC does NOT resolve from the mirror.
# Proves the mirror backend is what makes the zero-read difference.
# ===========================================================================


@pytest.mark.asyncio
async def test_mc_mode_against_dead_mc_does_not_resolve_from_mirror(
    mirror_db, secret_file, reset_singletons, monkeypatch
):
    """Same signed dispatch, but HUB_REGISTRY_SOURCE=mc + a dead MC transport.

    In mc-mode the very first registry read (get_source in _authenticate) goes
    to MC, the dead transport raises a transport error -> RegistryUnavailable,
    and the request degrades to a 503. It does NOT silently fall back to the
    populated mirror. This is the contrast that proves the mirror path (not the
    seeded DB alone) is what delivers the zero-read dispatch above.
    """
    monkeypatch.setenv("HUB_REGISTRY_SOURCE", "mc")
    monkeypatch.delenv("HUB_PILOT_TOKEN", raising=False)

    def dead_handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("MC is dead", request=request)

    mc_client = httpx.AsyncClient(
        base_url="http://mc-dead.invalid",
        transport=httpx.MockTransport(dead_handler),
    )
    registry = RegistryClient(client=mc_client, state=mirror_db)
    assert registry._source == "mc"
    hub_api.set_registry(registry)
    hub_api.set_hub_state(mirror_db)

    body = json.dumps(BASIC_INTENT).encode()
    headers = _hmac_headers(TEST_SECRET, body, nonce="contrast-nonce-1")

    app = FastAPI()
    app.include_router(router)
    client = TestClient(app, raise_server_exceptions=False)

    resp = client.post("/v1/notifications", content=body, headers=headers)

    # mc-mode + dead MC → transient registry outage surfaces as a typed 503,
    # NOT a mirror-resolved 200. (The populated mirror is right there, unused.)
    assert resp.status_code == 503, resp.text
    assert resp.json()["detail"]["error_code"] == "registry_unavailable"


# ===========================================================================
# HONEST LEAK PIN: the ONE remaining MC read in the dispatch path.
#
# get_live_event_by_fingerprint (registry_client.py) has NO mirror branch — it
# always issues an MC GET .../events/by-fingerprint. It is only reached when the
# intent carries a `fingerprint` (the v4d-A edit-in-place lookup). It is
# best-effort (swallows all errors → None), so the dispatch still delivers, but
# it IS a registry read that escapes the mirror. The zero-read proof above
# deliberately uses a fingerprint-free FIRING intent (the primary dispatch path)
# where this branch never fires.
#
# This test PINS that leak so it is tracked and visible: a fingerprint dispatch
# emits exactly one MC GET today, to the by-fingerprint endpoint. When a future
# step drains it (event-mirror or dropping the edit-lookup MC dependency), this
# test flips and must be updated — making the residual read impossible to forget.
# ===========================================================================


@pytest.mark.asyncio
async def test_fingerprint_dispatch_still_leaks_one_mc_read_today(
    mirror_db, secret_file, reset_singletons, monkeypatch
):
    """KNOWN GAP (tracked): a fingerprint-bearing dispatch in mirror-mode still
    emits exactly one MC GET — the get_live_event_by_fingerprint edit-lookup,
    which has no mirror backend. source/topic/rule/channel-set still resolve from
    the mirror (zero reads); only the edit-in-place lookup leaks."""
    monkeypatch.setenv("HUB_REGISTRY_SOURCE", "mirror")
    monkeypatch.delenv("HUB_PILOT_TOKEN", raising=False)

    spy = McReadSpy()
    mc_client = httpx.AsyncClient(
        base_url="http://mc-must-not-be-read.invalid",
        transport=httpx.MockTransport(spy.handler),
    )
    registry = RegistryClient(client=mc_client, state=mirror_db)
    hub_api.set_registry(registry)
    hub_api.set_hub_state(mirror_db)

    from gateway.hub import pipeline as pipeline_mod

    original_get_adapter = pipeline_mod.get_adapter

    def fake_get_adapter(channel_type: str):
        if channel_type == "inbox_mc":
            return FakeInboxMcAdapter()
        return original_get_adapter(channel_type)

    monkeypatch.setattr("gateway.hub.pipeline.get_adapter", fake_get_adapter)

    intent = dict(BASIC_INTENT, fingerprint="fp-edit-in-place-1")
    body = json.dumps(intent).encode()
    headers = _hmac_headers(TEST_SECRET, body, nonce="fp-leak-nonce-1")

    app = FastAPI()
    app.include_router(router)
    client = TestClient(app, raise_server_exceptions=False)

    resp = client.post("/v1/notifications", content=body, headers=headers)

    # Best-effort lookup swallows the spy's 599 → dispatch still delivers.
    assert resp.status_code == 200, resp.text
    assert resp.json()["data"]["status"] == "delivered"

    # The leak: exactly one MC GET, to the by-fingerprint edit-lookup endpoint.
    # (source/topic/rules/channel-set all resolved from the mirror — no GET.)
    assert len(spy.get_requests) == 1, spy.get_requests
    assert "/api/board/notifications/events/by-fingerprint" in spy.get_requests[0]


# ===========================================================================
# STEP-7 ZERO-MC-WRITE PROOF: with HUB_MC_WRITE_SEVERED=1 (+ mirror reads), a
# full dispatch makes ZERO MC HTTP calls — neither GET nor POST. This is the
# teeth-verified gate for revoking api_keys id=60: the hub is provably
# MC-independent for both reads AND writes on the dispatch path.
# ===========================================================================


@pytest.mark.asyncio
async def test_severed_dispatch_makes_zero_mc_calls(
    mirror_db, secret_file, reset_singletons, monkeypatch
):
    """mirror + severed → a signed FIRING dispatch delivers with NOT ONE MC
    call. The write sinks (post_audit, persist_deliveries) no-op under
    severance; reads come from the mirror. spy records zero GET and zero POST."""
    monkeypatch.setenv("HUB_REGISTRY_SOURCE", "mirror")
    monkeypatch.setenv("HUB_MC_WRITE_SEVERED", "1")
    monkeypatch.delenv("HUB_PILOT_TOKEN", raising=False)

    spy = McReadSpy()
    mc_client = httpx.AsyncClient(
        base_url="http://mc-must-not-be-touched.invalid",
        transport=httpx.MockTransport(spy.handler),
    )
    registry = RegistryClient(client=mc_client, state=mirror_db)
    hub_api.set_registry(registry)
    hub_api.set_hub_state(mirror_db)

    from gateway.hub import pipeline as pipeline_mod

    original_get_adapter = pipeline_mod.get_adapter

    def fake_get_adapter(channel_type: str):
        if channel_type == "inbox_mc":
            return FakeInboxMcAdapter()
        return original_get_adapter(channel_type)

    monkeypatch.setattr("gateway.hub.pipeline.get_adapter", fake_get_adapter)

    body = json.dumps(BASIC_INTENT).encode()
    headers = _hmac_headers(TEST_SECRET, body, nonce="zero-write-nonce-1")

    app = FastAPI()
    app.include_router(router)
    client = TestClient(app, raise_server_exceptions=False)

    resp = client.post("/v1/notifications", content=body, headers=headers)

    assert resp.status_code == 200, resp.text
    assert resp.json()["data"]["status"] == "delivered"

    # THE ZERO-MC-WRITE PROOF: not a single MC call escaped — read OR write.
    assert spy.get_requests == [], f"READ LEAK under severance: {spy.get_requests}"
    assert spy.post_requests == [], f"WRITE LEAK under severance: {spy.post_requests}"

    # Durable proof-of-dispatch: local hub_events_log still written.
    row = await mirror_db.fetchone(
        "SELECT status FROM hub_events_log WHERE source_slug = ? AND topic_slug = ?",
        (SOURCE_SLUG, TOPIC_SLUG),
    )
    assert row is not None and row["status"] == "delivered_inbox"


@pytest.mark.asyncio
async def test_severance_closes_the_fingerprint_read_leak(
    mirror_db, secret_file, reset_singletons, monkeypatch
):
    """The one pinned read leak (get_live_event_by_fingerprint) is CLOSED by
    severance: a fingerprint-bearing dispatch under HUB_MC_WRITE_SEVERED=1 emits
    ZERO MC GET (vs exactly 1 without severance — see the pin test above)."""
    monkeypatch.setenv("HUB_REGISTRY_SOURCE", "mirror")
    monkeypatch.setenv("HUB_MC_WRITE_SEVERED", "1")
    monkeypatch.delenv("HUB_PILOT_TOKEN", raising=False)

    spy = McReadSpy()
    mc_client = httpx.AsyncClient(
        base_url="http://mc-must-not-be-touched.invalid",
        transport=httpx.MockTransport(spy.handler),
    )
    registry = RegistryClient(client=mc_client, state=mirror_db)
    hub_api.set_registry(registry)
    hub_api.set_hub_state(mirror_db)

    from gateway.hub import pipeline as pipeline_mod

    original_get_adapter = pipeline_mod.get_adapter

    def fake_get_adapter(channel_type: str):
        if channel_type == "inbox_mc":
            return FakeInboxMcAdapter()
        return original_get_adapter(channel_type)

    monkeypatch.setattr("gateway.hub.pipeline.get_adapter", fake_get_adapter)

    intent = dict(BASIC_INTENT, fingerprint="fp-edit-in-place-1")
    body = json.dumps(intent).encode()
    headers = _hmac_headers(TEST_SECRET, body, nonce="severed-fp-nonce-1")

    app = FastAPI()
    app.include_router(router)
    client = TestClient(app, raise_server_exceptions=False)

    resp = client.post("/v1/notifications", content=body, headers=headers)

    assert resp.status_code == 200, resp.text
    assert resp.json()["data"]["status"] == "delivered"

    # Leak closed: the by-fingerprint GET is skipped under severance.
    assert spy.get_requests == [], f"fingerprint read leaked under severance: {spy.get_requests}"
    assert spy.post_requests == [], f"write leaked under severance: {spy.post_requests}"
