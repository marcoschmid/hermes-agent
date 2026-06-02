"""Mission Control inbox adapter for hub notification events."""
import os
import time
from typing import TYPE_CHECKING

import httpx
from pydantic import BaseModel, Field, ValidationError

from gateway.hub.adapters.errors import AdapterDeliveryError
from gateway.hub.schemas import NotificationIntent
from gateway.hub.severance import mc_writes_severed

if TYPE_CHECKING:
    from gateway.hub.adapter_registry import AdapterResult


DEFAULT_MC_HUB_BASE_URL = "http://127.0.0.1:3334"
MC_EVENTS_PATH = "/api/board/notifications/events"

# Allowlist for MC base URL hostnames. MC_HUB_BASE_URL is env-controlled,
# so a poisoned env could redirect MC_HUB_TOKEN to an attacker host (SSRF).
# Restrict to loopback by default; broader allowlist needs explicit opt-in
# via MC_HUB_ALLOW_HOSTS env (comma-separated).
_LOOPBACK_HOSTS = {"127.0.0.1", "localhost", "::1", "0.0.0.0"}


def _validate_base_url(url: str) -> None:
    from urllib.parse import urlparse
    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https"):
        raise AdapterDeliveryError("configuration", f"MC_HUB_BASE_URL scheme rejected: {parsed.scheme}")
    host = (parsed.hostname or "").lower()
    if host in _LOOPBACK_HOSTS:
        return
    allow_extra = os.environ.get("MC_HUB_ALLOW_HOSTS", "")
    allowed = {h.strip().lower() for h in allow_extra.split(",") if h.strip()}
    if host in allowed:
        return
    raise AdapterDeliveryError(
        "configuration",
        f"MC_HUB_BASE_URL host '{host}' not in loopback allowlist; set MC_HUB_ALLOW_HOSTS to override",
    )


class McEventData(BaseModel):
    event_id: str = Field(min_length=1)


class McEventResponse(BaseModel):
    data: McEventData


class InboxMcAdapter:
    name = "inbox_mc"

    def __init__(
        self,
        base_url: str | None = None,
        token: str | None = None,
        client: httpx.AsyncClient | None = None,
        timeout: float = 10.0,
    ) -> None:
        self.base_url = base_url or os.environ.get("MC_HUB_BASE_URL", DEFAULT_MC_HUB_BASE_URL)
        _validate_base_url(self.base_url)
        self.token = token
        self.timeout = timeout
        self._owned_client = client is None
        self._client = client or httpx.AsyncClient(base_url=self.base_url, timeout=timeout)

    async def close(self) -> None:
        if self._owned_client:
            await self._client.aclose()

    def _resolve_token(self) -> str:
        token = self.token if self.token is not None else os.environ.get("MC_HUB_TOKEN")
        if not token or not token.strip():
            raise AdapterDeliveryError("missing_token", "MC_HUB_TOKEN is required")
        return token.strip()

    def _validate_event(self, event: dict) -> dict:
        try:
            return NotificationIntent.model_validate(event).model_dump(mode="json")
        except ValidationError as exc:
            raise AdapterDeliveryError(422, exc.json()) from exc

    async def send(self, event: dict, channel: dict) -> "AdapterResult":
        # Phase-6b Step-7: severed → never POST to MC. inbox_mc is retired at
        # apply-time (channel-set members removed); this guard guarantees a
        # stray dispatch can't write to MC after the api_key is revoked. Fail
        # loudly rather than fake a delivery.
        if mc_writes_severed():
            raise AdapterDeliveryError(
                "severed", "inbox_mc retired (Phase-6b write-severance) — no MC write"
            )
        token = self._resolve_token()
        payload = self._validate_event(event)
        started = time.monotonic()

        try:
            response = await self._client.post(
                MC_EVENTS_PATH,
                json=payload,
                headers={"Authorization": f"Bearer {token}"},
                timeout=self.timeout,
            )
        except httpx.TimeoutException as exc:
            raise AdapterDeliveryError("timeout") from exc
        except httpx.HTTPError as exc:
            # Catches ConnectError, RemoteProtocolError, etc — match TelegramAdapter
            # contract: all transport errors surface as AdapterDeliveryError.
            raise AdapterDeliveryError("network", str(exc)) from exc

        body = response.text
        if response.status_code != 201:
            raise AdapterDeliveryError(response.status_code, body)

        try:
            mc_response = McEventResponse.model_validate(response.json())
        except (ValueError, ValidationError) as exc:
            raise AdapterDeliveryError(response.status_code, body) from exc

        latency_ms = int((time.monotonic() - started) * 1000)
        from gateway.hub.adapter_registry import AdapterResult

        return AdapterResult(
            status="delivered",
            provider_message_id=mc_response.data.event_id,
            latency_ms=latency_ms,
        )
