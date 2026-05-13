"""Registry client — Hermes-side wrapper around MC notification registry endpoints.

Reads tokens from env (MC_HUB_TOKEN, MC_BASE_URL).
Uses httpx.AsyncClient with shared lifespan for connection pooling.
5-min TTL cache for source/topic/rules/channel-set lookups.
"""
import os
import time
from dataclasses import dataclass
from typing import Any
import httpx

DEFAULT_BASE = "http://127.0.0.1:3334"
CACHE_TTL_SECONDS = 300


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

    async def get_source(self, slug: str, token_hash: str | None = None) -> dict | None:
        cache_key = f"source:{slug}:{token_hash or ''}"
        if cached := self._cache_get(cache_key):
            return cached
        params = {"token_hash": token_hash} if token_hash else {}
        r = await self._client.get(f"/api/board/notifications/sources/{slug}", params=params)
        if r.status_code == 404 or r.status_code == 403:
            return None
        r.raise_for_status()
        data = r.json().get("data")
        self._cache_set(cache_key, data)
        return data

    async def get_topic(self, slug: str) -> dict | None:
        cache_key = f"topic:{slug}"
        if cached := self._cache_get(cache_key):
            return cached
        r = await self._client.get(f"/api/board/notifications/topics/{slug}")
        if r.status_code == 404:
            return None
        r.raise_for_status()
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
        r = await self._client.get("/api/board/notifications/rules", params=params)
        r.raise_for_status()
        data = r.json().get("data", [])
        self._cache_set(cache_key, data)
        return data

    async def get_channel_set_expanded(self, channel_set_id: str) -> dict | None:
        cache_key = f"channel_set:{channel_set_id}"
        if cached := self._cache_get(cache_key):
            return cached
        r = await self._client.get(
            f"/api/board/notifications/channel-sets/{channel_set_id}/expanded"
        )
        if r.status_code == 404:
            return None
        r.raise_for_status()
        data = r.json().get("data")
        self._cache_set(cache_key, data)
        return data

    async def post_audit(self, **kwargs) -> None:
        r = await self._client.post("/api/board/notifications/audit", json=kwargs)
        r.raise_for_status()
