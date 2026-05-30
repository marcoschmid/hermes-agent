"""Registry client — Hermes-side wrapper around MC notification registry endpoints.

Reads tokens from env (MC_HUB_TOKEN, MC_BASE_URL).
Uses httpx.AsyncClient with shared lifespan for connection pooling.
5-min TTL cache for source/topic/rules/channel-set lookups.
"""
import json
import logging
import os
import re
import time
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any
import httpx

if TYPE_CHECKING:  # pragma: no cover - typing only, no runtime import cycle
    from gateway.hub.hub_state import HubState

logger = logging.getLogger(__name__)

DEFAULT_BASE = "http://127.0.0.1:3334"
CACHE_TTL_SECONDS = 300

# Phase-6b Step 5 read-backend swap. Default "mc" keeps the unchanged HTTP path
# (dark-launch). "mirror" reads the registry out of the local SQLite mirror
# (migration 003) instead of Mission Control. See _mirror_* helpers below.
ENV_REGISTRY_SOURCE = "HUB_REGISTRY_SOURCE"

# severity/urgency/actionability ORDER maps — replicated 1:1 from MC's
# notification-rules.ts so the mirror get_rules path applies the SAME
# `query_level >= rule_min_level` thresholds matchRules() uses.
_SEVERITY_ORDER = {"debug": 0, "info": 1, "notice": 2, "warn": 3, "error": 4, "crit": 5}
_URGENCY_ORDER = {"none": 0, "today": 1, "soon": 2, "now": 3}
_ACTIONABILITY_ORDER = {"info": 0, "ack": 1, "decide": 2, "task": 3}

# MC topicMatches() escapes exactly these regex metachars before turning '*'
# into '.*' (it leaves '*' alone, then converts it). Mirror replicates that set
# byte-for-byte so glob matching is identical on both backends.
_TOPIC_GLOB_ESCAPE = re.compile(r"[.+?^${}()|[\]\\]")


def _topic_matches(glob: str | None, topic: str) -> bool:
    """Port of MC topicMatches(): NULL glob → match all; no '*' → exact match;
    with '*' → multi-segment regex (each '*' → '.*'), anchored ^...$."""
    if glob is None:
        return True
    if "*" not in glob:
        return glob == topic
    escaped = _TOPIC_GLOB_ESCAPE.sub(lambda m: "\\" + m.group(0), glob)
    pattern = "^" + escaped.replace("*", ".*") + "$"
    return re.fullmatch(pattern, topic) is not None


class RegistryUnavailable(Exception):
    """A registry read failed transiently rather than answering 'not found'.

    Raised for an auth/permission failure (401/403), a server error (5xx), or
    a transport/timeout failure — i.e. the hub could not read its config from
    Mission Control, as opposed to MC cleanly reporting an unknown
    source/topic/channel-set (404, or get_source 403), which the read methods
    still return as ``None``.

    Callers convert this to an HTTP 503 so one transient MC outage degrades a
    single notification cleanly instead of escaping as an uncaught HTTP 500
    that collapses the whole hub hop into direct-fallback (the 2026-05-27
    cascade). It is deliberately NOT a subclass of ``PipelineError``.
    """

    def __init__(self, status_code: int | None, detail: str) -> None:
        self.status_code = status_code
        self.detail = detail
        super().__init__(f"registry_unavailable({status_code}): {detail}")


@dataclass
class CacheEntry:
    value: Any
    expires_at: float


class RegistryClient:
    def __init__(
        self,
        base_url: str | None = None,
        token: str | None = None,
        client: httpx.AsyncClient | None = None,
        state: "HubState | None" = None,
    ) -> None:
        self.base_url = base_url or os.environ.get("MC_BASE_URL", DEFAULT_BASE)
        self.token = token or os.environ.get("MC_HUB_TOKEN", "")
        self._owned_client = client is None
        self._client = client or httpx.AsyncClient(
            base_url=self.base_url,
            timeout=10.0,
            headers={"Authorization": f"Bearer {self.token}"} if self.token else {},
        )
        self._cache: dict[str, CacheEntry] = {}
        # Read-backend flag (Step 5). "mc" = unchanged HTTP path; "mirror" reads
        # the local SQLite mirror. `state` is the HubState used in mirror mode;
        # constructed lazily on first mirror read when not injected.
        self._source = os.environ.get(ENV_REGISTRY_SOURCE, "mc")
        self._state = state

    async def close(self) -> None:
        if self._owned_client:
            await self._client.aclose()

    def _cache_get(self, key: str) -> Any | None:
        entry = self._cache.get(key)
        if not entry or entry.expires_at < time.time():
            return None
        return entry.value

    def _cache_set(self, key: str, value: Any) -> None:
        self._cache[key] = CacheEntry(value=value, expires_at=time.time() + CACHE_TTL_SECONDS)

    async def _get(
        self,
        path: str,
        params: dict | None = None,
        *,
        none_statuses: tuple[int, ...] = (),
    ) -> httpx.Response | None:
        """GET a registry endpoint with transient-failure isolation.

        Returns the response, or ``None`` when the status is in
        ``none_statuses`` (a clean 'not found' answer — checked first, so an
        endpoint that maps 403/404 to None still does). Otherwise raises
        :class:`RegistryUnavailable` for an auth/permission failure (401/403),
        a server error (5xx), or a transport/timeout failure, so the caller
        surfaces a 503 instead of letting a raw ``httpx`` error escape as an
        uncaught 500. Other 4xx (e.g. a 400 from a malformed hub request) keep
        escaping via ``raise_for_status`` — those are hub bugs, not outages.
        """
        try:
            r = await self._client.get(path, params=params or {})
        except httpx.TransportError as exc:  # TimeoutException is a subclass
            raise RegistryUnavailable(None, f"GET {path}: {type(exc).__name__}") from exc
        if r.status_code in none_statuses:
            return None
        if r.status_code in (401, 403) or r.status_code >= 500:
            raise RegistryUnavailable(r.status_code, f"GET {path} -> {r.status_code}")
        r.raise_for_status()
        return r

    async def get_source(self, slug: str, token_hash: str | None = None) -> dict | None:
        if self._source == "mirror":
            return await self._mirror_get_source(slug, token_hash)
        cache_key = f"source:{slug}:{token_hash or ''}"
        if cached := self._cache_get(cache_key):
            return cached
        params = {"token_hash": token_hash} if token_hash else {}
        r = await self._get(
            f"/api/board/notifications/sources/{slug}",
            params=params,
            none_statuses=(404, 403),
        )
        if r is None:
            return None
        data = r.json().get("data")
        if isinstance(data, dict):
            data = self._normalize_source_scope(data)
        self._cache_set(cache_key, data)
        return data

    def _normalize_source_scope(self, source: dict) -> dict:
        """Decode MC JSON-array scope fields when present.

        Mission Control stores source scopes as nullable TEXT columns containing
        JSON arrays. Normalize them to Python lists at the boundary while keeping
        legacy/missing/null fields as None.
        """
        normalized = dict(source)
        for key in ("allowed_topics", "allowed_audiences"):
            value = normalized.get(key)
            if value is None or isinstance(value, list):
                continue
            if isinstance(value, str):
                try:
                    parsed = json.loads(value)
                except json.JSONDecodeError:
                    continue
                if isinstance(parsed, list):
                    normalized[key] = parsed
        return normalized

    async def get_topic(self, slug: str) -> dict | None:
        if self._source == "mirror":
            return await self._mirror_get_topic(slug)
        cache_key = f"topic:{slug}"
        if cached := self._cache_get(cache_key):
            return cached
        r = await self._get(
            f"/api/board/notifications/topics/{slug}", none_statuses=(404,)
        )
        if r is None:
            return None
        data = r.json().get("data")
        self._cache_set(cache_key, data)
        return data

    async def get_rules(
        self,
        topic: str,
        audience: str,
        severity: str,
        urgency: str = "none",
        actionability: str = "info",
        source_id: str | None = None,
    ) -> list[dict]:
        if self._source == "mirror":
            return await self._mirror_get_rules(
                topic, audience, severity, urgency, actionability, source_id
            )
        cache_key = f"rules:{topic}:{audience}:{severity}:{urgency}:{actionability}:{source_id or ''}"
        if cached := self._cache_get(cache_key):
            return cached
        params: dict[str, str] = {
            "topic": topic,
            "audience": audience,
            "severity": severity,
            "urgency": urgency,
            "actionability": actionability,
        }
        if source_id:
            params["source_id"] = source_id
        r = await self._get("/api/board/notifications/rules", params=params)
        data = r.json().get("data", [])
        self._cache_set(cache_key, data)
        return data

    async def get_channel_set_expanded(self, channel_set_id: str) -> dict | None:
        if self._source == "mirror":
            return await self._mirror_get_channel_set_expanded(channel_set_id)
        cache_key = f"channel_set:{channel_set_id}"
        if cached := self._cache_get(cache_key):
            return cached
        r = await self._get(
            f"/api/board/notifications/channel-sets/{channel_set_id}/expanded",
            none_statuses=(404,),
        )
        if r is None:
            return None
        data = r.json().get("data")
        self._cache_set(cache_key, data)
        return data

    # =====================================================================
    # Phase-6b Step 5 — mirror read backend (HUB_REGISTRY_SOURCE=mirror).
    #
    # Each helper reads the migration-003 mirror via HubState and returns the
    # EXACT same shape its MC counterpart returns, so dispatch is byte-for-byte
    # identical regardless of backend. Contract:
    #   * miss → None (get_rules → empty list, never None)
    #   * any sqlite/connect failure → RegistryUnavailable (503-contract), so a
    #     broken mirror degrades one notification cleanly instead of escaping
    #     as an uncaught 500 (same posture as the MC transient-failure path).
    #   * INTEGER 0/1 → bool where MC returns a bool (enabled / supports_* /
    #     required); scope TEXT-of-JSON → list via _normalize_source_scope.
    # =====================================================================

    async def _mirror_state(self) -> "HubState":
        """Return the connected HubState for mirror reads, connecting lazily.

        A failure to open the mirror DB is itself a transient registry outage,
        so it surfaces as RegistryUnavailable (not a raw sqlite error).
        """
        from gateway.hub.hub_state import HubState

        if self._state is None:
            try:
                self._state = await HubState().connect()
            except Exception as exc:  # pragma: no cover - exercised via db-down test
                raise RegistryUnavailable(
                    None, f"mirror read failed: connect: {type(exc).__name__}"
                ) from exc
        return self._state

    async def _mirror_get_source(
        self, slug: str, token_hash: str | None = None
    ) -> dict | None:
        # token_hash is IGNORED in mirror mode: the mirror carries no
        # api_token_hash column (Step 4a list endpoint omits all secrets), and
        # the hub_secret HMAC check is the real auth barrier — not this hash.
        state = await self._mirror_state()
        try:
            row = await state.fetchone(
                "SELECT id, slug, name, service_type, owner, default_topic_id, "
                "default_severity, severity_max, rate_limit_per_min, enabled, "
                "allowed_topics, allowed_audiences, max_severity "
                "FROM registry_sources WHERE slug = ?",
                (slug,),
            )
        except Exception as exc:
            raise RegistryUnavailable(
                None, f"mirror read failed: sources: {type(exc).__name__}"
            ) from exc
        if row is None:
            return None
        data = dict(row)
        data["enabled"] = bool(data["enabled"])
        # Mirror has no hub_secret column → never present (matches the Step-4a
        # secret-free posture; hub_secret is read from the Step-6 secret store).
        return self._normalize_source_scope(data)

    async def _mirror_get_topic(self, slug: str) -> dict | None:
        state = await self._mirror_state()
        try:
            row = await state.fetchone(
                "SELECT id, slug, name, description, default_severity, "
                "default_urgency, default_actionability, default_audience_id, "
                "default_audience_slug, retention_days, enabled "
                "FROM registry_topics WHERE slug = ?",
                (slug,),
            )
        except Exception as exc:
            raise RegistryUnavailable(
                None, f"mirror read failed: topics: {type(exc).__name__}"
            ) from exc
        if row is None:
            return None
        data = dict(row)
        data["enabled"] = bool(data["enabled"])
        return data

    async def _mirror_resolve_audience_id(
        self, state: "HubState", slug_or_id: str
    ) -> str | None:
        """Replicates MC resolveAudienceId: aud_-prefixed passes through; a bare
        slug resolves via registry_audiences; an unknown slug → None (no match)."""
        if slug_or_id.startswith("aud_"):
            return slug_or_id
        row = await state.fetchone(
            "SELECT id FROM registry_audiences WHERE slug = ?", (slug_or_id,)
        )
        return row["id"] if row is not None else None

    async def _mirror_get_rules(
        self,
        topic: str,
        audience: str,
        severity: str,
        urgency: str,
        actionability: str,
        source_id: str | None,
    ) -> list[dict]:
        """Replicates matchRules() against the mirror: resolve audience slug→id,
        fetch enabled rows for that audience ORDER BY priority ASC, then filter
        in-memory on topic_glob / severity / urgency / actionability / source_id.
        Returns [] (never None) on no-match, matching the MC empty-data shape."""
        state = await self._mirror_state()
        try:
            resolved = await self._mirror_resolve_audience_id(state, audience)
            if resolved is None:
                return []
            rows = await state.fetchall(
                "SELECT id, slug, priority, topic_glob, source_id, audience_id, "
                "severity_min, urgency_min, actionability_min, quiet_class, "
                "channel_set_id, escalation_policy_id, enabled "
                "FROM registry_rules WHERE enabled = 1 AND audience_id = ? "
                "ORDER BY priority ASC",
                (resolved,),
            )
        except Exception as exc:
            raise RegistryUnavailable(
                None, f"mirror read failed: rules: {type(exc).__name__}"
            ) from exc

        sev = _SEVERITY_ORDER[severity]
        urg = _URGENCY_ORDER[urgency]
        act = _ACTIONABILITY_ORDER[actionability]

        result: list[dict] = []
        for raw in rows:
            r = dict(raw)
            if not _topic_matches(r["topic_glob"], topic):
                continue
            if sev < _SEVERITY_ORDER[r["severity_min"]]:
                continue
            if urg < _URGENCY_ORDER[r["urgency_min"]]:
                continue
            if act < _ACTIONABILITY_ORDER[r["actionability_min"]]:
                continue
            if r["source_id"] is not None and r["source_id"] != source_id:
                continue
            r["enabled"] = bool(r["enabled"])
            result.append(r)
        return result

    async def _mirror_get_channel_set_expanded(
        self, channel_set_id: str
    ) -> dict | None:
        """Replicates getChannelSetExpanded(): the set row plus position-sorted
        members, each as {position, fallback_after_seconds, required, channel{}}.
        Miss (unknown set) → None."""
        state = await self._mirror_state()
        try:
            cs = await state.fetchone(
                "SELECT id, slug, name, audience_id, enabled "
                "FROM registry_channel_sets WHERE id = ?",
                (channel_set_id,),
            )
            if cs is None:
                return None
            members = await state.fetchall(
                "SELECT m.position, m.fallback_after_seconds, m.required, "
                "c.id AS channel_id, c.slug AS channel_slug, c.type AS type, "
                "c.audience_id AS channel_audience_id, c.target_ref AS target_ref, "
                "c.target_thread_id AS target_thread_id, "
                "c.target_subtype AS target_subtype, "
                "c.display_name AS display_name, "
                "c.bot_token_env_ref AS bot_token_env_ref, "
                "c.rate_limit_per_min AS rate_limit_per_min, "
                "c.supports_ack AS supports_ack, "
                "c.supports_buttons AS supports_buttons, "
                "c.enabled AS channel_enabled "
                "FROM registry_channel_set_members m "
                "JOIN registry_channels c ON c.id = m.channel_id "
                "WHERE m.channel_set_id = ? ORDER BY m.position ASC",
                (channel_set_id,),
            )
        except Exception as exc:
            raise RegistryUnavailable(
                None, f"mirror read failed: channel_set: {type(exc).__name__}"
            ) from exc

        return {
            "id": cs["id"],
            "slug": cs["slug"],
            "name": cs["name"],
            "audience_id": cs["audience_id"],
            "enabled": bool(cs["enabled"]),
            "members": [
                {
                    "position": m["position"],
                    "fallback_after_seconds": m["fallback_after_seconds"],
                    "required": bool(m["required"]),
                    "channel": {
                        "id": m["channel_id"],
                        "slug": m["channel_slug"],
                        "type": m["type"],
                        "audience_id": m["channel_audience_id"],
                        "target_ref": m["target_ref"],
                        "target_thread_id": m["target_thread_id"],
                        "target_subtype": m["target_subtype"],
                        "display_name": m["display_name"],
                        "bot_token_env_ref": m["bot_token_env_ref"],
                        "rate_limit_per_min": m["rate_limit_per_min"],
                        "supports_ack": bool(m["supports_ack"]),
                        "supports_buttons": bool(m["supports_buttons"]),
                        "enabled": bool(m["channel_enabled"]),
                    },
                }
                for m in members
            ],
        }

    async def post_audit(self, **kwargs) -> None:
        r = await self._client.post("/api/board/notifications/audit", json=kwargs)
        r.raise_for_status()

    async def get_live_event_by_fingerprint(
        self,
        source_slug: str,
        fingerprint: str,
        channel_id: str | None = None,
    ) -> dict | None:
        """Query MC for latest live event matching (source_slug, fingerprint).

        Returns event dict (with provider_message_id, channel_id) or None.
        Best-effort: returns None on any non-200 / network error (never raises).

        Task 3.5b: passing `channel_id` filters MC's delivery JOIN to that
        channel. Without it, an event with both inbox_mc + telegram deliveries
        can surface the wrong delivery row → pipeline channel-equality check
        fails → fallback to send instead of edit. Pipeline always passes the
        dispatch channel's id so the returned row matches.
        """
        params: dict[str, str] = {"source": source_slug, "fingerprint": fingerprint}
        if channel_id:
            params["channel"] = channel_id
        try:
            resp = await self._client.get(
                f"{self.base_url}/api/board/notifications/events/by-fingerprint",
                params=params,
                headers={"Authorization": f"Bearer {self.token}"},
                timeout=5.0,
            )
            resp.raise_for_status()
            return resp.json().get("event")
        except Exception as exc:  # broad on purpose — never propagate
            logger.warning(
                "get_live_event_by_fingerprint failed (%s/%s/%s): %s",
                source_slug,
                fingerprint,
                channel_id,
                exc,
            )
            return None

    async def persist_deliveries(self, event_id: str, deliveries: list[dict]) -> None:
        """Persist dispatch results to MC (v4d-A Phase 0.5).

        POSTs to /api/board/notifications/events/:id/deliveries so the
        fingerprint -> provider_message_id round-trip exists in MC-DB for
        subsequent edit-in-place firings.

        Best-effort: errors are logged but never raised — MC persistence
        failure must not break the Hub-response to senders. MC normalises
        "edited" -> "delivered" server-side for the CHECK constraint.
        """
        try:
            resp = await self._client.post(
                f"{self.base_url}/api/board/notifications/events/{event_id}/deliveries",
                json={"deliveries": deliveries},
                headers={"Authorization": f"Bearer {self.token}"},
                timeout=5.0,
            )
            resp.raise_for_status()
        except Exception as exc:  # broad on purpose — never propagate
            logger.warning(
                "persist_deliveries failed for event %s: %s", event_id, exc,
            )
