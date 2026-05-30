"""Tests for gateway.hub.registry_sync (Phase 6b Step 4b — full-table puller).

RegistrySync pulls the notification registry out of Mission Control via the
Step-4a list endpoints (`/api/board/notifications/{sources,topics,channels,
channel-sets,audiences}/` + `/rules/all`, Bearer-authed, envelope
``{data:[...], meta:{count}}``) plus the existing per-set
`RegistryClient.get_channel_set_expanded(id)` for channel-set members, and
UPSERTs every table of the migration-003 mirror inside ONE transaction.

Scope: Step 4b only. This does NOT change dispatch (the mirror is first READ in
Step 5) and carries NO secret store (Step 6). All MC auth flows through the
single ``RegistryClient`` base_url + token — no second token.

Test patterns mirror the existing suite:
  - MC HTTP via ``httpx.MockTransport`` + a ``make_client(handler)`` helper
    (see test_registry_client.py).
  - Mirror via ``tmp_path`` + ``HUB_STATE_DB_PATH`` (see test_hub_state.py).
"""

from __future__ import annotations

import json
from unittest.mock import AsyncMock

import httpx
import pytest

from gateway.hub.hub_state import connect
from gateway.hub.registry_client import RegistryClient, RegistryUnavailable
from gateway.hub.registry_sync import RegistrySync, registry_sync_loop


# ---------------------------------------------------------------------------
# Fixtures: a small but representative MC registry payload.
# ---------------------------------------------------------------------------

SOURCES = [
    {
        "id": "src_1",
        "slug": "drobo-backup",
        "name": "Drobo Backup",
        "service_type": "backup",
        "owner": "marco",
        "default_topic_id": "tp_1",
        "default_severity": "info",
        "severity_max": "crit",
        "max_severity": "crit",
        "rate_limit_per_min": 10,
        "enabled": True,
        "allowed_topics": ["home.backup"],
        "allowed_audiences": None,
    },
    {
        "id": "src_2",
        "slug": "paperclip",
        "name": "Paperclip",
        "service_type": "ci",
        "owner": "marco",
        "default_topic_id": None,
        "default_severity": "info",
        "severity_max": "warn",
        "max_severity": "warn",
        "rate_limit_per_min": None,
        "enabled": False,
        "allowed_topics": None,
        "allowed_audiences": None,
    },
]

TOPICS = [
    {
        "id": "tp_1",
        "slug": "home.backup",
        "name": "Backup",
        "description": "Backup events",
        "default_severity": "info",
        "default_urgency": "none",
        "default_actionability": "info",
        "default_audience_id": "aud_1",
        "default_audience_slug": "marco",
        "retention_days": 30,
        "enabled": True,
    },
]

AUDIENCES = [
    {"id": "aud_1", "slug": "marco", "name": "Marco", "enabled": True},
    {"id": "aud_2", "slug": "family", "name": "Family", "enabled": True},
]

CHANNELS = [
    {
        "id": "ch_tg",
        "slug": "tg-marco",
        "type": "telegram",
        "audience_id": "aud_1",
        "target_ref": "12345",
        "target_thread_id": None,
        "target_subtype": None,
        "display_name": "Telegram Marco",
        "bot_token_env_ref": "env:TG_TOKEN",
        "rate_limit_per_min": 20,
        "supports_ack": True,
        "supports_buttons": True,
        "enabled": True,
    },
    {
        "id": "ch_inbox",
        "slug": "inbox-mc",
        "type": "inbox_mc",
        "audience_id": "aud_1",
        "target_ref": "marco",
        "target_thread_id": None,
        "target_subtype": None,
        "display_name": "Inbox MC",
        "bot_token_env_ref": None,
        "rate_limit_per_min": None,
        "supports_ack": False,
        "supports_buttons": False,
        "enabled": True,
    },
]

# channel-sets LIST (members NOT included — pulled per-set via expanded).
CHANNEL_SETS = [
    {
        "id": "cs_1",
        "slug": "marco-default",
        "name": "Marco Default",
        "audience_id": "aud_1",
        "description": "default set",
        "enabled": True,
    },
    {
        "id": "cs_2",
        "slug": "marco-quiet",
        "name": "Marco Quiet",
        "audience_id": "aud_1",
        "description": None,
        "enabled": True,
    },
]

# Per-set expanded payloads, keyed by id (members carried here).
EXPANDED = {
    "cs_1": {
        "id": "cs_1",
        "slug": "marco-default",
        "name": "Marco Default",
        "audience_id": "aud_1",
        "enabled": True,
        "members": [
            {
                "position": 1,
                "fallback_after_seconds": None,
                "required": True,
                "channel": CHANNELS[0],
            },
            {
                "position": 2,
                "fallback_after_seconds": 60,
                "required": False,
                "channel": CHANNELS[1],
            },
        ],
    },
    "cs_2": {
        "id": "cs_2",
        "slug": "marco-quiet",
        "name": "Marco Quiet",
        "audience_id": "aud_1",
        "enabled": True,
        "members": [
            {
                "position": 1,
                "fallback_after_seconds": None,
                "required": False,
                "channel": CHANNELS[1],
            },
        ],
    },
}

RULES = [
    {
        "id": "rule_1",
        "slug": "backup-info",
        "priority": 3,
        "topic_glob": "home.backup",
        "source_id": "src_1",
        "audience_id": "aud_1",
        "severity_min": "info",
        "urgency_min": "none",
        "actionability_min": "info",
        "quiet_class": None,
        "channel_set_id": "cs_1",
        "escalation_policy_id": None,
        "enabled": True,
    },
    {
        "id": "rule_2",
        "slug": "backup-crit",
        "priority": 1,
        "topic_glob": "home.*",
        "source_id": None,
        "audience_id": "aud_1",
        "severity_min": "crit",
        "urgency_min": "none",
        "actionability_min": "info",
        "quiet_class": "always",
        "channel_set_id": "cs_2",
        "escalation_policy_id": None,
        "enabled": False,
    },
]


# ---------------------------------------------------------------------------
# MC mock transport: serves the Step-4a list endpoints + per-set expanded.
# ---------------------------------------------------------------------------

_LIST_PAYLOADS = {
    "/api/board/notifications/sources/": SOURCES,
    "/api/board/notifications/topics/": TOPICS,
    "/api/board/notifications/channels/": CHANNELS,
    "/api/board/notifications/channel-sets/": CHANNEL_SETS,
    "/api/board/notifications/audiences/": AUDIENCES,
    "/api/board/notifications/rules/all": RULES,
}


def _ok_handler(captured: dict | None = None):
    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if captured is not None:
            captured.setdefault("paths", []).append(path)
            captured.setdefault("auth", request.headers.get("authorization", ""))
        if path in _LIST_PAYLOADS:
            rows = _LIST_PAYLOADS[path]
            return httpx.Response(200, json={"data": rows, "meta": {"count": len(rows)}})
        # Per-set expanded: /api/board/notifications/channel-sets/<id>/expanded
        if path.endswith("/expanded"):
            cs_id = path.split("/")[-2]
            exp = EXPANDED.get(cs_id)
            if exp is None:
                return httpx.Response(404, json={"error": "not found"})
            return httpx.Response(200, json={"data": exp})
        return httpx.Response(404, json={"error": f"unexpected path {path}"})

    return handler


def make_registry(handler) -> RegistryClient:
    transport = httpx.MockTransport(handler)
    mock_client = httpx.AsyncClient(
        base_url="http://test",
        transport=transport,
        headers={"Authorization": "Bearer test-token"},
    )
    return RegistryClient(base_url="http://test", token="test-token", client=mock_client)


async def _make_state(tmp_path, monkeypatch):
    db_path = tmp_path / "hub_state.db"
    monkeypatch.setenv("HUB_STATE_DB_PATH", str(db_path))
    state = await connect()
    await state.migrate()
    return state


async def _count(state, table: str) -> int:
    row = await state.fetchone(f"SELECT COUNT(*) AS n FROM {table}")
    return row["n"]


# ---------------------------------------------------------------------------
# sync_once: pulls every table into the mirror.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_sync_once_pulls_all_tables_into_mirror(tmp_path, monkeypatch) -> None:
    registry = make_registry(_ok_handler())
    state = await _make_state(tmp_path, monkeypatch)
    try:
        sync = RegistrySync(registry, state)
        result = await sync.sync_once()

        assert await _count(state, "registry_sources") == 2
        assert await _count(state, "registry_topics") == 1
        assert await _count(state, "registry_audiences") == 2
        assert await _count(state, "registry_channels") == 2
        assert await _count(state, "registry_channel_sets") == 2
        # cs_1 has 2 members, cs_2 has 1 → 3 total.
        assert await _count(state, "registry_channel_set_members") == 3
        assert await _count(state, "registry_rules") == 2

        # Booleans coerced to int; allowed_topics serialised as JSON text.
        src = await state.fetchone(
            "SELECT enabled, allowed_topics FROM registry_sources WHERE id = ?",
            ("src_1",),
        )
        assert src["enabled"] == 1
        assert json.loads(src["allowed_topics"]) == ["home.backup"]
        disabled = await state.fetchone(
            "SELECT enabled, allowed_topics FROM registry_sources WHERE id = ?",
            ("src_2",),
        )
        assert disabled["enabled"] == 0
        assert disabled["allowed_topics"] is None

        # Channel booleans coerced.
        ch = await state.fetchone(
            "SELECT supports_ack, supports_buttons, enabled "
            "FROM registry_channels WHERE id = ?",
            ("ch_tg",),
        )
        assert (ch["supports_ack"], ch["supports_buttons"], ch["enabled"]) == (1, 1, 1)

        # Disabled rule still synced.
        rule2 = await state.fetchone(
            "SELECT enabled, channel_set_id FROM registry_rules WHERE id = ?",
            ("rule_2",),
        )
        assert rule2["enabled"] == 0
        assert rule2["channel_set_id"] == "cs_2"

        # Member detail preserved.
        mem = await state.fetchone(
            "SELECT position, fallback_after_seconds, required "
            "FROM registry_channel_set_members "
            "WHERE channel_set_id = ? AND channel_id = ?",
            ("cs_1", "ch_inbox"),
        )
        assert mem["position"] == 2
        assert mem["fallback_after_seconds"] == 60
        assert mem["required"] == 0

        # sync_once reports per-table counts.
        assert result["sources"] == 2
        assert result["channel_set_members"] == 3
        assert result["rules"] == 2

        # last_success watermark recorded in sync_meta.
        meta = await state.fetchone(
            "SELECT value FROM registry_sync_meta WHERE key = ?", ("last_success",)
        )
        assert meta is not None and int(meta["value"]) > 0
    finally:
        await state.close()
        await registry.close()


@pytest.mark.asyncio
async def test_sync_once_idempotent_row_counts(tmp_path, monkeypatch) -> None:
    registry = make_registry(_ok_handler())
    state = await _make_state(tmp_path, monkeypatch)
    try:
        sync = RegistrySync(registry, state)
        await sync.sync_once()
        # Cache would otherwise short-circuit get_channel_set_expanded; reset so
        # the second sync genuinely re-pulls (mirrors a real periodic loop where
        # cache TTL has expired).
        registry._cache.clear()
        await sync.sync_once()

        assert await _count(state, "registry_sources") == 2
        assert await _count(state, "registry_topics") == 1
        assert await _count(state, "registry_audiences") == 2
        assert await _count(state, "registry_channels") == 2
        assert await _count(state, "registry_channel_sets") == 2
        assert await _count(state, "registry_channel_set_members") == 3
        assert await _count(state, "registry_rules") == 2
    finally:
        await state.close()
        await registry.close()


@pytest.mark.asyncio
async def test_sync_once_transient_mc_failure_raises_registry_unavailable(
    tmp_path, monkeypatch
) -> None:
    """A 5xx on any list pull → RegistryUnavailable, and the mirror stays
    untouched/stale (the whole sync is one transaction → rolled back)."""

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/board/notifications/rules/all":
            return httpx.Response(503, json={"error": "unavailable"})
        return _ok_handler()(request)

    registry = make_registry(handler)
    state = await _make_state(tmp_path, monkeypatch)
    try:
        sync = RegistrySync(registry, state)
        with pytest.raises(RegistryUnavailable):
            await sync.sync_once()

        # Nothing committed — the mirror is still empty (stale), not partial.
        assert await _count(state, "registry_sources") == 0
        assert await _count(state, "registry_channels") == 0
        assert await _count(state, "registry_rules") == 0
        meta = await state.fetchone(
            "SELECT value FROM registry_sync_meta WHERE key = ?", ("last_success",)
        )
        assert meta is None
    finally:
        await state.close()
        await registry.close()


@pytest.mark.asyncio
async def test_sync_once_transient_failure_leaves_prior_mirror_intact(
    tmp_path, monkeypatch
) -> None:
    """After a good sync, a later transient failure must not wipe the mirror:
    the prior (now stale) snapshot survives for the read-backend (Step 5)."""
    state = await _make_state(tmp_path, monkeypatch)
    good = make_registry(_ok_handler())
    try:
        await RegistrySync(good, state).sync_once()
        assert await _count(state, "registry_sources") == 2

        def failing(request: httpx.Request) -> httpx.Response:
            return httpx.Response(500, json={"error": "boom"})

        bad = make_registry(failing)
        try:
            with pytest.raises(RegistryUnavailable):
                await RegistrySync(bad, state).sync_once()
        finally:
            await bad.close()

        # Prior snapshot intact.
        assert await _count(state, "registry_sources") == 2
        assert await _count(state, "registry_channel_set_members") == 3
    finally:
        await state.close()
        await good.close()


# ---------------------------------------------------------------------------
# registry_sync_loop: one failed iteration must NOT kill the loop.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_sync_loop_continues_after_failed_iteration() -> None:
    """A RegistryUnavailable in one iteration is logged and swallowed; the loop
    keeps going. CancelledError is the only thing that stops it."""
    import asyncio

    calls = {"n": 0}

    async def fake_sync_once() -> dict:
        calls["n"] += 1
        if calls["n"] == 1:
            raise RegistryUnavailable(503, "transient")
        if calls["n"] >= 3:
            # Stop the loop deterministically once we've proved it survived the
            # first failure and ran again.
            raise asyncio.CancelledError()
        return {"sources": 2}

    sync = AsyncMock()
    sync.sync_once = AsyncMock(side_effect=fake_sync_once)

    with pytest.raises(asyncio.CancelledError):
        # interval=0 → no real sleep delay between iterations.
        await registry_sync_loop(sync, interval=0)

    # Iter 1 raised (survived), iter 2 succeeded, iter 3 cancelled → 3 calls.
    assert calls["n"] == 3
