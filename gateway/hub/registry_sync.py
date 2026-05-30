"""Phase 6b Step 4b — full-table puller for the local registry mirror.

``RegistrySync.sync_once()`` reads the entire notification registry out of
Mission Control via the Step-4a list endpoints and UPSERTs it into the
migration-003 mirror (``registry_*`` tables) inside ONE transaction, so a
transient MC outage rolls back cleanly and leaves the prior (now stale)
snapshot intact rather than half-written.

Auth/transport flows through the single :class:`RegistryClient` (its
``base_url`` + ``token`` are the ONLY MC credential — no second token). A
revoked token / 5xx / network error on any pull raises
:class:`RegistryUnavailable`, the same 503-contract carrier the read path uses.

``registry_sync_loop`` is the periodic driver, modelled 1:1 on
``server._nonce_prune_loop``: sleep → sync → swallow-and-continue on a
non-cancel error, re-raise on ``CancelledError``.

Scope: Step 4b ONLY. Dispatch is unchanged — the mirror is first READ in
Step 5; no secret store here (Step 6).
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import time
from typing import Any

import httpx

from gateway.hub.hub_state import HubState
from gateway.hub.registry_client import RegistryClient, RegistryUnavailable

logger = logging.getLogger(__name__)

DEFAULT_SYNC_INTERVAL_SECONDS = 300
ENV_SYNC_INTERVAL = "HUB_REGISTRY_SYNC_INTERVAL_S"

# Step-4a MC list endpoints (Bearer-authed, envelope {data:[...], meta:{count}}).
_LIST_PATHS = {
    "sources": "/api/board/notifications/sources/",
    "topics": "/api/board/notifications/topics/",
    "channels": "/api/board/notifications/channels/",
    "channel_sets": "/api/board/notifications/channel-sets/",
    "audiences": "/api/board/notifications/audiences/",
    "rules": "/api/board/notifications/rules/all",
}


def sync_interval_seconds() -> int:
    """Resolve the loop interval from env, falling back to the default."""
    raw = os.environ.get(ENV_SYNC_INTERVAL)
    if raw is None:
        return DEFAULT_SYNC_INTERVAL_SECONDS
    try:
        return int(raw)
    except ValueError:
        logger.warning(
            "invalid %s=%r — using default %ds",
            ENV_SYNC_INTERVAL,
            raw,
            DEFAULT_SYNC_INTERVAL_SECONDS,
        )
        return DEFAULT_SYNC_INTERVAL_SECONDS


def _bool_int(value: Any, default: int = 0) -> int:
    """Coerce an MC JSON boolean (or 0/1) to a SQLite INTEGER 0/1."""
    if value is None:
        return default
    return 1 if value else 0


def _json_or_none(value: Any) -> str | None:
    """Store a list scope field (allowed_topics/audiences) as JSON text.

    MC returns these as JSON arrays (already decoded to lists) or null. Mirror
    columns are nullable TEXT; round-tripping through JSON keeps the on-disk
    shape consistent with MC's own TEXT-of-JSON storage.
    """
    if value is None:
        return None
    if isinstance(value, str):
        return value
    return json.dumps(value)


class RegistrySync:
    """Pulls the MC notification registry into the local SQLite mirror."""

    def __init__(self, registry: RegistryClient, state: HubState) -> None:
        self._registry = registry
        self._state = state

    async def _fetch_list(self, path: str) -> list[dict]:
        """GET a Step-4a list endpoint, isolating transient failures.

        Reuses the RegistryClient base_url + token (single auth source). A
        revoked token / 5xx / network error → RegistryUnavailable (the
        503-contract carrier); a clean 200 returns the envelope ``data`` array.
        """
        client = self._registry._client
        try:
            resp = await client.get(path)
        except httpx.TransportError as exc:  # TimeoutException is a subclass
            raise RegistryUnavailable(None, f"GET {path}: {type(exc).__name__}") from exc
        if resp.status_code in (401, 403) or resp.status_code >= 500:
            raise RegistryUnavailable(
                resp.status_code, f"GET {path} -> {resp.status_code}"
            )
        resp.raise_for_status()
        return resp.json().get("data", []) or []

    async def sync_once(self) -> dict[str, int]:
        """Pull every registry table from MC and UPSERT the local mirror.

        All pulls happen BEFORE the transaction opens, so a transient MC
        failure raises without ever touching the mirror (the prior snapshot
        stays intact). The whole write set then lands in ONE transaction, so a
        write error rolls back atomically too.

        Returns per-table row counts (the count synced for each table).
        """
        # --- Fetch phase (no DB writes yet) ------------------------------
        sources = await self._fetch_list(_LIST_PATHS["sources"])
        topics = await self._fetch_list(_LIST_PATHS["topics"])
        channels = await self._fetch_list(_LIST_PATHS["channels"])
        channel_sets = await self._fetch_list(_LIST_PATHS["channel_sets"])
        audiences = await self._fetch_list(_LIST_PATHS["audiences"])
        rules = await self._fetch_list(_LIST_PATHS["rules"])

        # channel-set MEMBERS are not in the list payload → pull per set via
        # the existing expanded route (also RegistryUnavailable-isolating).
        expanded: dict[str, dict] = {}
        for cs in channel_sets:
            cs_id = cs.get("id")
            if not cs_id:
                continue
            exp = await self._registry.get_channel_set_expanded(cs_id)
            if exp is not None:
                expanded[cs_id] = exp

        now = int(time.time())

        # --- Write phase (single transaction → atomic / rollback-safe) ---
        async with self._state.transaction() as tx:
            for s in sources:
                await self._upsert_source(tx, s, now)
            for t in topics:
                await self._upsert_topic(tx, t, now)
            for a in audiences:
                await self._upsert_audience(tx, a)
            for c in channels:
                await self._upsert_channel(tx, c, now)
            for cs in channel_sets:
                await self._upsert_channel_set(tx, cs, now)
            member_count = await self._replace_members(tx, expanded)
            for r in rules:
                await self._upsert_rule(tx, r, now)
            await tx.execute(
                "INSERT INTO registry_sync_meta (key, value, updated_at) "
                "VALUES ('last_success', ?, ?) "
                "ON CONFLICT(key) DO UPDATE SET "
                "value = excluded.value, updated_at = excluded.updated_at",
                (str(now), now),
            )

        return {
            "sources": len(sources),
            "topics": len(topics),
            "audiences": len(audiences),
            "channels": len(channels),
            "channel_sets": len(channel_sets),
            "channel_set_members": member_count,
            "rules": len(rules),
        }

    # --- UPSERT helpers (one per table) ----------------------------------

    async def _upsert_source(self, tx: HubState, s: dict, now: int) -> None:
        await tx.execute(
            "INSERT INTO registry_sources ("
            "  id, slug, name, service_type, owner, default_topic_id,"
            "  default_severity, severity_max, max_severity, rate_limit_per_min,"
            "  enabled, allowed_topics, allowed_audiences, synced_at"
            ") VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?) "
            "ON CONFLICT(id) DO UPDATE SET "
            "  slug = excluded.slug, name = excluded.name,"
            "  service_type = excluded.service_type, owner = excluded.owner,"
            "  default_topic_id = excluded.default_topic_id,"
            "  default_severity = excluded.default_severity,"
            "  severity_max = excluded.severity_max,"
            "  max_severity = excluded.max_severity,"
            "  rate_limit_per_min = excluded.rate_limit_per_min,"
            "  enabled = excluded.enabled,"
            "  allowed_topics = excluded.allowed_topics,"
            "  allowed_audiences = excluded.allowed_audiences,"
            "  synced_at = excluded.synced_at",
            (
                s["id"],
                s["slug"],
                s.get("name"),
                s.get("service_type"),
                s.get("owner"),
                s.get("default_topic_id"),
                s.get("default_severity"),
                s.get("severity_max"),
                s.get("max_severity"),
                s.get("rate_limit_per_min"),
                _bool_int(s.get("enabled"), default=1),
                _json_or_none(s.get("allowed_topics")),
                _json_or_none(s.get("allowed_audiences")),
                now,
            ),
        )

    async def _upsert_topic(self, tx: HubState, t: dict, now: int) -> None:
        await tx.execute(
            "INSERT INTO registry_topics ("
            "  id, slug, name, description, default_severity, default_urgency,"
            "  default_actionability, default_audience_id, default_audience_slug,"
            "  retention_days, enabled, synced_at"
            ") VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?) "
            "ON CONFLICT(id) DO UPDATE SET "
            "  slug = excluded.slug, name = excluded.name,"
            "  description = excluded.description,"
            "  default_severity = excluded.default_severity,"
            "  default_urgency = excluded.default_urgency,"
            "  default_actionability = excluded.default_actionability,"
            "  default_audience_id = excluded.default_audience_id,"
            "  default_audience_slug = excluded.default_audience_slug,"
            "  retention_days = excluded.retention_days,"
            "  enabled = excluded.enabled, synced_at = excluded.synced_at",
            (
                t["id"],
                t["slug"],
                t.get("name"),
                t.get("description"),
                t.get("default_severity"),
                t.get("default_urgency"),
                t.get("default_actionability"),
                t.get("default_audience_id"),
                t.get("default_audience_slug"),
                t.get("retention_days"),
                _bool_int(t.get("enabled"), default=1),
                now,
            ),
        )

    async def _upsert_audience(self, tx: HubState, a: dict) -> None:
        await tx.execute(
            "INSERT INTO registry_audiences (id, slug, name, enabled) "
            "VALUES (?, ?, ?, ?) "
            "ON CONFLICT(id) DO UPDATE SET "
            "  slug = excluded.slug, name = excluded.name, enabled = excluded.enabled",
            (
                a["id"],
                a["slug"],
                a.get("name"),
                _bool_int(a.get("enabled"), default=1),
            ),
        )

    async def _upsert_channel(self, tx: HubState, c: dict, now: int) -> None:
        await tx.execute(
            "INSERT INTO registry_channels ("
            "  id, slug, type, audience_id, target_ref, target_thread_id,"
            "  target_subtype, display_name, bot_token_env_ref, rate_limit_per_min,"
            "  supports_ack, supports_buttons, enabled, synced_at"
            ") VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?) "
            "ON CONFLICT(id) DO UPDATE SET "
            "  slug = excluded.slug, type = excluded.type,"
            "  audience_id = excluded.audience_id, target_ref = excluded.target_ref,"
            "  target_thread_id = excluded.target_thread_id,"
            "  target_subtype = excluded.target_subtype,"
            "  display_name = excluded.display_name,"
            "  bot_token_env_ref = excluded.bot_token_env_ref,"
            "  rate_limit_per_min = excluded.rate_limit_per_min,"
            "  supports_ack = excluded.supports_ack,"
            "  supports_buttons = excluded.supports_buttons,"
            "  enabled = excluded.enabled, synced_at = excluded.synced_at",
            (
                c["id"],
                c["slug"],
                c["type"],
                c.get("audience_id"),
                c.get("target_ref"),
                c.get("target_thread_id"),
                c.get("target_subtype"),
                c.get("display_name"),
                c.get("bot_token_env_ref"),
                c.get("rate_limit_per_min"),
                _bool_int(c.get("supports_ack")),
                _bool_int(c.get("supports_buttons")),
                _bool_int(c.get("enabled"), default=1),
                now,
            ),
        )

    async def _upsert_channel_set(self, tx: HubState, cs: dict, now: int) -> None:
        # Mirror table has no `description` column → omit it.
        await tx.execute(
            "INSERT INTO registry_channel_sets ("
            "  id, slug, name, audience_id, enabled, synced_at"
            ") VALUES (?, ?, ?, ?, ?, ?) "
            "ON CONFLICT(id) DO UPDATE SET "
            "  slug = excluded.slug, name = excluded.name,"
            "  audience_id = excluded.audience_id,"
            "  enabled = excluded.enabled, synced_at = excluded.synced_at",
            (
                cs["id"],
                cs["slug"],
                cs.get("name"),
                cs.get("audience_id"),
                _bool_int(cs.get("enabled"), default=1),
                now,
            ),
        )

    async def _replace_members(
        self, tx: HubState, expanded: dict[str, dict]
    ) -> int:
        """DELETE-by-set then re-insert members from each expanded channel-set.

        Per-set replace (not a global wipe) keeps a set whose expanded fetch
        was skipped — e.g. it 404'd between the list and the per-set read —
        from silently losing its members.
        """
        total = 0
        for cs_id, exp in expanded.items():
            await tx.execute(
                "DELETE FROM registry_channel_set_members WHERE channel_set_id = ?",
                (cs_id,),
            )
            for m in exp.get("members", []) or []:
                channel = m.get("channel", {}) or {}
                channel_id = channel.get("id")
                if not channel_id:
                    continue
                await tx.execute(
                    "INSERT INTO registry_channel_set_members ("
                    "  channel_set_id, channel_id, position,"
                    "  fallback_after_seconds, required"
                    ") VALUES (?, ?, ?, ?, ?) "
                    "ON CONFLICT(channel_set_id, channel_id) DO UPDATE SET "
                    "  position = excluded.position,"
                    "  fallback_after_seconds = excluded.fallback_after_seconds,"
                    "  required = excluded.required",
                    (
                        cs_id,
                        channel_id,
                        m.get("position", 0),
                        m.get("fallback_after_seconds"),
                        _bool_int(m.get("required")),
                    ),
                )
                total += 1
        return total

    async def _upsert_rule(self, tx: HubState, r: dict, now: int) -> None:
        await tx.execute(
            "INSERT INTO registry_rules ("
            "  id, slug, priority, topic_glob, source_id, audience_id,"
            "  severity_min, urgency_min, actionability_min, quiet_class,"
            "  channel_set_id, escalation_policy_id, enabled, synced_at"
            ") VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?) "
            "ON CONFLICT(id) DO UPDATE SET "
            "  slug = excluded.slug, priority = excluded.priority,"
            "  topic_glob = excluded.topic_glob, source_id = excluded.source_id,"
            "  audience_id = excluded.audience_id,"
            "  severity_min = excluded.severity_min,"
            "  urgency_min = excluded.urgency_min,"
            "  actionability_min = excluded.actionability_min,"
            "  quiet_class = excluded.quiet_class,"
            "  channel_set_id = excluded.channel_set_id,"
            "  escalation_policy_id = excluded.escalation_policy_id,"
            "  enabled = excluded.enabled, synced_at = excluded.synced_at",
            (
                r["id"],
                r["slug"],
                r["priority"],
                r.get("topic_glob"),
                r.get("source_id"),
                r["audience_id"],
                r["severity_min"],
                r["urgency_min"],
                r["actionability_min"],
                r.get("quiet_class"),
                r["channel_set_id"],
                r.get("escalation_policy_id"),
                _bool_int(r.get("enabled"), default=1),
                now,
            ),
        )


async def registry_sync_loop(sync: RegistrySync, interval: int) -> None:
    """Periodic mirror-sync driver (modelled on server._nonce_prune_loop).

    Sleeps ``interval`` seconds, runs ``sync_once()``, and — critically — a
    single failed iteration (RegistryUnavailable or any other Exception) is
    logged and swallowed so the loop survives a transient MC outage. Only
    ``CancelledError`` (lifespan shutdown) stops it.
    """
    while True:
        try:
            await asyncio.sleep(interval)
            counts = await sync.sync_once()
            logger.debug("registry mirror sync ok: %s", counts)
        except asyncio.CancelledError:
            raise
        except RegistryUnavailable as exc:
            logger.warning("registry mirror sync skipped (MC unavailable): %s", exc)
        except Exception as exc:
            logger.warning("registry mirror sync iteration failed: %s", exc)
