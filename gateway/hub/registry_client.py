"""Registry client — Hermes-side wrapper around MC notification registry endpoints.

Reads tokens from env (MC_HUB_TOKEN, MC_BASE_URL).
Uses httpx.AsyncClient with shared lifespan for connection pooling.
5-min TTL cache for source/topic/rules/channel-set lookups.
"""
import json
import logging
import os
import time
from dataclasses import dataclass
from typing import Any
import httpx

logger = logging.getLogger(__name__)

DEFAULT_BASE = "http://127.0.0.1:3334"
CACHE_TTL_SECONDS = 300


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
