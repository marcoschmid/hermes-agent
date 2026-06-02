"""Phase 6b Step 5 — read-backend swap: RegistryClient mirror-mode parity.

The RegistryClient gains a ``HUB_REGISTRY_SOURCE`` flag. Default ``mc`` is the
unchanged HTTP path (dark-launch — no behavior change). ``mirror`` reads the
four registry reads (source / topic / rules / channel-set-expanded) out of the
local SQLite mirror (migration 003) instead of Mission Control.

These tests pin that the mirror path returns the EXACT same shapes/semantics as
the MC path — most critically ``get_rules``, whose MC implementation
(``matchRules`` in mission-control/src/lib/notification-rules.ts) does an
in-memory filter + audience slug→id resolution. A divergence here routes
notifications to the wrong channels, so the mirror replicates ``matchRules``
1:1 against the same fixture the MC route-test uses as its oracle.
"""
from __future__ import annotations

import pytest
import pytest_asyncio

from gateway.hub.hub_state import HubState, connect
from gateway.hub.registry_client import RegistryClient, RegistryUnavailable


# ---------------------------------------------------------------------------
# Fixtures: a connected mirror DB + a RegistryClient pinned to mirror mode.
# ---------------------------------------------------------------------------


@pytest_asyncio.fixture
async def mirror_state(tmp_path, monkeypatch):
    """A migrated, empty mirror DB on a tmp path."""
    db_path = tmp_path / "hub_state.db"
    monkeypatch.setenv("HUB_STATE_DB_PATH", str(db_path))
    db = await connect()
    await db.migrate()
    try:
        yield db
    finally:
        await db.close()


def mirror_client(state: HubState, monkeypatch) -> RegistryClient:
    monkeypatch.setenv("HUB_REGISTRY_SOURCE", "mirror")
    # No httpx client needed for mirror mode; pass the state in explicitly so
    # the read does not depend on env-default DB resolution.
    return RegistryClient(state=state)


# ---------------------------------------------------------------------------
# Seed helpers — write rows in the exact mirror schema (migration 003).
# ---------------------------------------------------------------------------


async def _seed_source(state: HubState, **over) -> None:
    row = {
        "id": "src_1",
        "slug": "weekly-preview",
        "name": "Weekly Preview",
        "service_type": "routine",
        "owner": "marco",
        "default_topic_id": "tp_1",
        "default_severity": "info",
        "severity_max": "crit",
        "max_severity": None,
        "rate_limit_per_min": 60,
        "enabled": 1,
        "allowed_topics": None,
        "allowed_audiences": None,
    }
    row.update(over)
    await state.execute(
        "INSERT INTO registry_sources (id, slug, name, service_type, owner, "
        "default_topic_id, default_severity, severity_max, max_severity, "
        "rate_limit_per_min, enabled, allowed_topics, allowed_audiences, synced_at) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (
            row["id"], row["slug"], row["name"], row["service_type"], row["owner"],
            row["default_topic_id"], row["default_severity"], row["severity_max"],
            row["max_severity"], row["rate_limit_per_min"], row["enabled"],
            row["allowed_topics"], row["allowed_audiences"], 100,
        ),
    )


async def _seed_audience(state: HubState, id_: str, slug: str) -> None:
    await state.execute(
        "INSERT INTO registry_audiences (id, slug, name, enabled) VALUES (?,?,?,1)",
        (id_, slug, slug.title()),
    )


async def _seed_rule(state: HubState, **over) -> None:
    row = {
        "id": "rule_1",
        "slug": "rule-one",
        "priority": 5,
        "topic_glob": None,
        "source_id": None,
        "audience_id": "aud_marco",
        "severity_min": "info",
        "urgency_min": "none",
        "actionability_min": "info",
        "quiet_class": "respect",
        "channel_set_id": "cset_1",
        "escalation_policy_id": None,
        "enabled": 1,
    }
    row.update(over)
    await state.execute(
        "INSERT INTO registry_rules (id, slug, priority, topic_glob, source_id, "
        "audience_id, severity_min, urgency_min, actionability_min, quiet_class, "
        "channel_set_id, escalation_policy_id, enabled, synced_at) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (
            row["id"], row["slug"], row["priority"], row["topic_glob"],
            row["source_id"], row["audience_id"], row["severity_min"],
            row["urgency_min"], row["actionability_min"], row["quiet_class"],
            row["channel_set_id"], row["escalation_policy_id"], row["enabled"], 100,
        ),
    )


async def _seed_channel(state: HubState, **over) -> None:
    row = {
        "id": "chan_1",
        "slug": "tg-marco",
        "type": "telegram",
        "audience_id": "aud_marco",
        "target_ref": "12345",
        "target_thread_id": None,
        "target_subtype": None,
        "display_name": "TG Marco",
        "bot_token_env_ref": "env:TG_BOT_TOKEN",
        "rate_limit_per_min": 30,
        "supports_ack": 1,
        "supports_buttons": 0,
        "enabled": 1,
    }
    row.update(over)
    await state.execute(
        "INSERT INTO registry_channels (id, slug, type, audience_id, target_ref, "
        "target_thread_id, target_subtype, display_name, bot_token_env_ref, "
        "rate_limit_per_min, supports_ack, supports_buttons, enabled, synced_at) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (
            row["id"], row["slug"], row["type"], row["audience_id"], row["target_ref"],
            row["target_thread_id"], row["target_subtype"], row["display_name"],
            row["bot_token_env_ref"], row["rate_limit_per_min"], row["supports_ack"],
            row["supports_buttons"], row["enabled"], 100,
        ),
    )


async def _seed_channel_set(state: HubState, **over) -> None:
    row = {
        "id": "cset_1",
        "slug": "cset-marco",
        "name": "Marco Set",
        "audience_id": "aud_marco",
        "enabled": 1,
    }
    row.update(over)
    await state.execute(
        "INSERT INTO registry_channel_sets (id, slug, name, audience_id, enabled, synced_at) "
        "VALUES (?,?,?,?,?,?)",
        (row["id"], row["slug"], row["name"], row["audience_id"], row["enabled"], 100),
    )


async def _seed_member(state: HubState, **over) -> None:
    row = {
        "channel_set_id": "cset_1",
        "channel_id": "chan_1",
        "position": 0,
        "fallback_after_seconds": None,
        "required": 1,
    }
    row.update(over)
    await state.execute(
        "INSERT INTO registry_channel_set_members (channel_set_id, channel_id, "
        "position, fallback_after_seconds, required) VALUES (?,?,?,?,?)",
        (
            row["channel_set_id"], row["channel_id"], row["position"],
            row["fallback_after_seconds"], row["required"],
        ),
    )


# ---------------------------------------------------------------------------
# get_source — shape parity (mirror OHNE hub_secret), miss → None, db-down → 503.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_get_source_mc_vs_mirror_same_shape(mirror_state, monkeypatch):
    """Mirror get_source returns the same dict MC would — minus hub_secret."""
    await _seed_source(
        mirror_state,
        allowed_topics='["home.kalender.weekly"]',
        allowed_audiences=None,
    )
    rc = mirror_client(mirror_state, monkeypatch)
    result = await rc.get_source("weekly-preview")

    assert result == {
        "id": "src_1",
        "slug": "weekly-preview",
        "name": "Weekly Preview",
        "service_type": "routine",
        "owner": "marco",
        "default_topic_id": "tp_1",
        "default_severity": "info",
        "severity_max": "crit",
        "rate_limit_per_min": 60,
        "enabled": True,  # INTEGER 1 -> bool, like MC
        "allowed_topics": ["home.kalender.weekly"],  # JSON-TEXT decoded by _normalize_source_scope
        "allowed_audiences": None,
        "max_severity": None,
    }
    # Mirror has no hub_secret column / value — must NOT leak one.
    assert "hub_secret" not in result


@pytest.mark.asyncio
async def test_mirror_get_source_miss_returns_none(mirror_state, monkeypatch):
    rc = mirror_client(mirror_state, monkeypatch)
    assert await rc.get_source("does-not-exist") is None


@pytest.mark.asyncio
async def test_mirror_db_down_raises_registry_unavailable(mirror_state, monkeypatch):
    """A closed/broken mirror DB must raise RegistryUnavailable (503-contract),
    NOT a raw sqlite error → uncaught 500."""
    await mirror_state.close()  # force every read to fail
    rc = mirror_client(mirror_state, monkeypatch)
    with pytest.raises(RegistryUnavailable):
        await rc.get_source("weekly-preview")


# ---------------------------------------------------------------------------
# get_rules — the correctness-critical parity test (replicates matchRules).
# ---------------------------------------------------------------------------


async def _seed_match_rules_oracle(state: HubState) -> None:
    """Seed the MC rules-route oracle fixture into the mirror.

    Mirrors mission-control/src/.../rules/route.test.ts: audience ``aud_marco``
    (slug ``marco``), one enabled priority-5 rule with NULL glob (global
    default), severity_min=info, urgency_min=none, actionability_min=info.
    """
    await _seed_audience(state, "aud_marco", "marco")
    await _seed_rule(
        state,
        id="rule_route_b3",
        slug="route-b3-rule",
        priority=5,
        topic_glob=None,
        source_id=None,
        audience_id="aud_marco",
        severity_min="info",
        urgency_min="none",
        actionability_min="info",
        quiet_class="respect",
        channel_set_id="cset_route_b3",
        enabled=1,
    )


@pytest.mark.asyncio
async def test_mirror_get_rules_filters_like_mc(mirror_state, monkeypatch):
    """The MC oracle: GET rules?topic=home.ev_charge&audience=marco&severity=info
    must return the seeded priority-5 NULL-glob rule (resolved via slug→id).
    Mirror must match that exact result."""
    await _seed_match_rules_oracle(mirror_state)
    rc = mirror_client(mirror_state, monkeypatch)

    # MC oracle case 1: slug-based audience resolves + global rule matches.
    rules = await rc.get_rules(
        topic="home.ev_charge", audience="marco", severity="info",
    )
    assert len(rules) == 1
    rule = rules[0]
    assert rule["id"] == "rule_route_b3"
    assert rule["priority"] == 5
    assert rule["audience_id"] == "aud_marco"
    assert rule["enabled"] is True
    assert rule["channel_set_id"] == "cset_route_b3"

    # MC oracle case 2: aud_-prefixed id resolves to the SAME rule.
    rules_by_id = await rc.get_rules(
        topic="home.ev_charge", audience="aud_marco", severity="info",
    )
    assert [r["id"] for r in rules_by_id] == ["rule_route_b3"]

    # MC oracle case 3: unknown audience slug → no match (empty list).
    none_aud = await rc.get_rules(
        topic="home.ev_charge", audience="does-not-exist", severity="info",
    )
    assert none_aud == []


@pytest.mark.asyncio
async def test_mirror_get_rules_replicates_all_match_filters(mirror_state, monkeypatch):
    """Exercise each matchRules filter dimension against the mirror path:
    topic_glob (NULL / exact / star), severity/urgency/actionability order
    thresholds, source_id filter, enabled-only, priority ASC sort."""
    await _seed_audience(mirror_state, "aud_marco", "marco")
    # r_global: NULL glob, lowest priority. matches everything >= info.
    await _seed_rule(
        mirror_state, id="r_global", slug="r-global", priority=5, topic_glob=None,
        severity_min="info", channel_set_id="cs_g",
    )
    # r_topic: exact glob match for home.ev_charge, higher priority.
    await _seed_rule(
        mirror_state, id="r_topic", slug="r-topic", priority=3,
        topic_glob="home.ev_charge", severity_min="info", channel_set_id="cs_t",
    )
    # r_star: star glob home.* matches multi-segment; severity threshold warn.
    await _seed_rule(
        mirror_state, id="r_star", slug="r-star", priority=4, topic_glob="home.*",
        severity_min="warn", channel_set_id="cs_s",
    )
    # r_source: source-scoped; only matches when source_id == src_x.
    await _seed_rule(
        mirror_state, id="r_source", slug="r-source", priority=2, topic_glob=None,
        source_id="src_x", severity_min="info", channel_set_id="cs_src",
    )
    # r_disabled: enabled=0 → never returned.
    await _seed_rule(
        mirror_state, id="r_disabled", slug="r-disabled", priority=1, topic_glob=None,
        severity_min="info", enabled=0, channel_set_id="cs_d",
    )
    rc = mirror_client(mirror_state, monkeypatch)

    # topic=home.ev_charge, severity=info, no source → r_topic(3) + r_global(5),
    # NOT r_star (needs warn), NOT r_source (needs src_x), NOT r_disabled.
    rules = await rc.get_rules(topic="home.ev_charge", audience="marco", severity="info")
    assert [r["id"] for r in rules] == ["r_topic", "r_global"]  # priority ASC

    # severity=warn now clears r_star's threshold → r_topic(3), r_star(4), r_global(5).
    rules_warn = await rc.get_rules(topic="home.ev_charge", audience="marco", severity="warn")
    assert [r["id"] for r in rules_warn] == ["r_topic", "r_star", "r_global"]

    # source_id=src_x adds the source-scoped r_source(2) at the front.
    rules_src = await rc.get_rules(
        topic="home.ev_charge", audience="marco", severity="info", source_id="src_x",
    )
    assert [r["id"] for r in rules_src] == ["r_source", "r_topic", "r_global"]

    # star glob spans multiple segments: home.ev_charge.deep matches home.* and NULL.
    rules_deep = await rc.get_rules(
        topic="home.ev_charge.deep", audience="marco", severity="warn",
    )
    assert [r["id"] for r in rules_deep] == ["r_star", "r_global"]

    # urgency below threshold filters a rule out.
    await _seed_rule(
        mirror_state, id="r_urg", slug="r-urg", priority=1, topic_glob=None,
        severity_min="info", urgency_min="now", channel_set_id="cs_u",
    )
    rc2 = mirror_client(mirror_state, monkeypatch)
    rules_urg_low = await rc2.get_rules(
        topic="home.ev_charge", audience="marco", severity="info", urgency="today",
    )
    assert "r_urg" not in [r["id"] for r in rules_urg_low]
    rules_urg_now = await rc2.get_rules(
        topic="home.ev_charge", audience="marco", severity="info", urgency="now",
    )
    assert "r_urg" in [r["id"] for r in rules_urg_now]


@pytest.mark.asyncio
async def test_mirror_get_rules_empty_returns_empty_list_not_none(mirror_state, monkeypatch):
    """No matching rule → [] (NOT None) so find_matching_rule's `if not rules`
    behaves identically to the MC path."""
    await _seed_audience(mirror_state, "aud_marco", "marco")
    rc = mirror_client(mirror_state, monkeypatch)
    result = await rc.get_rules(topic="home.ev_charge", audience="marco", severity="info")
    assert result == []
    assert result is not None


@pytest.mark.asyncio
async def test_mirror_get_rules_unknown_row_enum_skipped_not_500(mirror_state, monkeypatch):
    """A mirror rule carrying an unknown severity_min (corrupt/future-MC value)
    must be SKIPPED defensively, not crash the lookup with an uncaught KeyError
    → 500. A good rule alongside it still matches."""
    await _seed_audience(mirror_state, "aud_marco", "marco")
    # Bad row: severity_min is not in _SEVERITY_ORDER → would KeyError on lookup.
    await _seed_rule(
        mirror_state, id="r_bad", slug="r-bad", priority=2, topic_glob=None,
        severity_min="ZZZ_UNKNOWN", channel_set_id="cs_bad",
    )
    # Good row alongside it: must still be returned.
    await _seed_rule(
        mirror_state, id="r_good", slug="r-good", priority=5, topic_glob=None,
        severity_min="info", channel_set_id="cs_good",
    )
    rc = mirror_client(mirror_state, monkeypatch)

    rules = await rc.get_rules(topic="home.ev_charge", audience="marco", severity="info")
    # Bad row skipped (continue), good row survives — no 500/KeyError.
    assert [r["id"] for r in rules] == ["r_good"]


@pytest.mark.asyncio
async def test_mirror_get_rules_unknown_caller_enum_raises_registry_unavailable(
    mirror_state, monkeypatch
):
    """An unknown caller-supplied severity/urgency/actionability must raise the
    typed RegistryUnavailable (→ 503-contract), NOT an uncaught KeyError → 500."""
    await _seed_audience(mirror_state, "aud_marco", "marco")
    await _seed_rule(
        mirror_state, id="r_global", slug="r-global", priority=5, topic_glob=None,
        severity_min="info", channel_set_id="cs_g",
    )
    rc = mirror_client(mirror_state, monkeypatch)

    with pytest.raises(RegistryUnavailable):
        await rc.get_rules(
            topic="home.ev_charge", audience="marco", severity="ZZZ_UNKNOWN",
        )
    with pytest.raises(RegistryUnavailable):
        await rc.get_rules(
            topic="home.ev_charge", audience="marco", severity="info",
            urgency="ZZZ_UNKNOWN",
        )
    with pytest.raises(RegistryUnavailable):
        await rc.get_rules(
            topic="home.ev_charge", audience="marco", severity="info",
            actionability="ZZZ_UNKNOWN",
        )


# ---------------------------------------------------------------------------
# get_channel_set_expanded — nested-members shape parity.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_mirror_channel_set_expanded_nested_members_shape(mirror_state, monkeypatch):
    """Mirror join must produce the EXACT nested shape of
    notifications-channels.ts getChannelSetExpanded (position-sorted members,
    each with fallback_after_seconds, required:bool, channel{...} bools)."""
    await _seed_channel(mirror_state, id="chan_1", slug="tg-marco", position=None)
    await _seed_channel(
        mirror_state, id="chan_2", slug="pushover-marco", type="pushover",
        target_ref="userkey", display_name="Pushover", bot_token_env_ref="env:PO",
        supports_ack=0, supports_buttons=1, enabled=1, rate_limit_per_min=10,
    )
    await _seed_channel_set(mirror_state)
    # Insert out of position order to prove ORDER BY position ASC.
    await _seed_member(
        mirror_state, channel_id="chan_2", position=1,
        fallback_after_seconds=30, required=0,
    )
    await _seed_member(
        mirror_state, channel_id="chan_1", position=0,
        fallback_after_seconds=None, required=1,
    )
    rc = mirror_client(mirror_state, monkeypatch)

    result = await rc.get_channel_set_expanded("cset_1")

    assert result == {
        "id": "cset_1",
        "slug": "cset-marco",
        "name": "Marco Set",
        "audience_id": "aud_marco",
        "enabled": True,
        "members": [
            {
                "position": 0,
                "fallback_after_seconds": None,
                "required": True,
                "channel": {
                    "id": "chan_1",
                    "slug": "tg-marco",
                    "type": "telegram",
                    "audience_id": "aud_marco",
                    "target_ref": "12345",
                    "target_thread_id": None,
                    "target_subtype": None,
                    "display_name": "TG Marco",
                    "bot_token_env_ref": "env:TG_BOT_TOKEN",
                    "rate_limit_per_min": 30,
                    "supports_ack": True,
                    "supports_buttons": False,
                    "enabled": True,
                },
            },
            {
                "position": 1,
                "fallback_after_seconds": 30,
                "required": False,
                "channel": {
                    "id": "chan_2",
                    "slug": "pushover-marco",
                    "type": "pushover",
                    "audience_id": "aud_marco",
                    "target_ref": "userkey",
                    "target_thread_id": None,
                    "target_subtype": None,
                    "display_name": "Pushover",
                    "bot_token_env_ref": "env:PO",
                    "rate_limit_per_min": 10,
                    "supports_ack": False,
                    "supports_buttons": True,
                    "enabled": True,
                },
            },
        ],
    }


@pytest.mark.asyncio
async def test_mirror_channel_set_expanded_miss_returns_none(mirror_state, monkeypatch):
    rc = mirror_client(mirror_state, monkeypatch)
    assert await rc.get_channel_set_expanded("cset_missing") is None


@pytest.mark.asyncio
async def test_mirror_get_topic_same_shape_and_miss(mirror_state, monkeypatch):
    """get_topic mirror parity: row → dict, miss → None."""
    await mirror_state.execute(
        "INSERT INTO registry_topics (id, slug, name, description, default_severity, "
        "default_urgency, default_actionability, default_audience_id, "
        "default_audience_slug, retention_days, enabled, synced_at) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
        (
            "tp_1", "home.kalender.weekly", "Weekly", None, "info", "none", "info",
            "aud_marco", "marco", 30, 1, 100,
        ),
    )
    rc = mirror_client(mirror_state, monkeypatch)
    topic = await rc.get_topic("home.kalender.weekly")
    assert topic["id"] == "tp_1"
    assert topic["slug"] == "home.kalender.weekly"
    assert topic["default_audience_slug"] == "marco"
    assert topic["enabled"] is True
    assert await rc.get_topic("nope") is None


# ---------------------------------------------------------------------------
# Default (mc) mode is unchanged: mirror helpers are NOT consulted.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_default_source_is_mc(monkeypatch):
    """No env / source=mc → the client stays in MC HTTP mode (dark-launch)."""
    monkeypatch.delenv("HUB_REGISTRY_SOURCE", raising=False)
    rc = RegistryClient(base_url="http://test", token="t")
    assert rc._source == "mc"
    await rc.close()
