"""Pushover delivery adapter for Notification Hub.

API: https://pushover.net/api
Endpoint: POST https://api.pushover.net/1/messages.json

Channel config shape::

    channel.config = {
        "app_token": "<APP_TOKEN_FROM_PUSHOVER>",        # Pushover application token
        "user_key":  "<USER_OR_GROUP_KEY>",              # recipient user/group key
        "device":    "iphone",                           # optional, comma-separated
        "sound":     "siren",                            # optional override
    }

Severity → Pushover priority mapping::

    debug, info → -2 (silent)
    notice      →  0 (default)
    warn        →  1 (high)
    error       →  2 (emergency, retry/expire)
    crit        →  2 (emergency, retry/expire, sound=siren)
"""
from __future__ import annotations

import os
import time
from typing import Any

import httpx

from gateway.hub.adapters.errors import AdapterDeliveryError

PUSHOVER_API_URL = "https://api.pushover.net/1/messages.json"

# Emergency priority retry/expire (Pushover requires both for priority=2)
EMERGENCY_RETRY_SECONDS = 60
EMERGENCY_EXPIRE_SECONDS = 3600

SEVERITY_TO_PRIORITY: dict[str, int] = {
    "debug": -2,
    "info": -2,
    "notice": 0,
    "warn": 1,
    "error": 2,
    "crit": 2,
}


class PushoverAdapter:
    name = "pushover"

    def __init__(
        self,
        client: httpx.AsyncClient | None = None,
        app_token: str | None = None,
        timeout: float = 5.0,
    ) -> None:
        self._app_token = app_token
        self._timeout = timeout
        self._owned_client = client is None
        self._client = client or httpx.AsyncClient(timeout=timeout)

    async def close(self) -> None:
        if self._owned_client:
            await self._client.aclose()

    async def send(self, event: dict, channel: dict):
        from gateway.hub.adapter_registry import AdapterResult

        config = channel.get("config") or {}
        app_token = self._resolve_app_token(config)
        user_key = self._resolve_user_key(config)
        title, message = self._build_message(event)
        severity = (event.get("severity") or "info").lower()
        priority = SEVERITY_TO_PRIORITY.get(severity, 0)

        payload: dict[str, Any] = {
            "token": app_token,
            "user": user_key,
            "title": title,
            "message": message,
            "priority": priority,
        }

        device = config.get("device")
        if device:
            payload["device"] = device

        sound = config.get("sound")
        if sound:
            payload["sound"] = sound
        elif severity == "crit":
            payload["sound"] = "siren"

        if priority == 2:
            payload["retry"] = EMERGENCY_RETRY_SECONDS
            payload["expire"] = EMERGENCY_EXPIRE_SECONDS

        started = time.perf_counter()
        try:
            response = await self._client.post(
                PUSHOVER_API_URL,
                data=payload,
                timeout=self._timeout,
            )
        except httpx.TimeoutException as exc:
            raise AdapterDeliveryError("timeout", str(exc)) from exc
        except httpx.HTTPError as exc:
            raise AdapterDeliveryError("network", str(exc)) from exc

        latency_ms = int((time.perf_counter() - started) * 1000)
        if response.status_code != 200:
            raise AdapterDeliveryError(response.status_code, response.text)

        data = response.json() if response.content else {}
        receipt = data.get("receipt") if isinstance(data, dict) else None
        request_id = data.get("request") if isinstance(data, dict) else None
        provider_id = receipt or request_id

        return AdapterResult(
            status="delivered",
            provider_message_id=str(provider_id) if provider_id else None,
            latency_ms=latency_ms,
        )

    def _resolve_app_token(self, config: dict) -> str:
        token = (
            config.get("app_token")
            or self._app_token
            or os.environ.get("PUSHOVER_APP_TOKEN")
        )
        if not token:
            raise AdapterDeliveryError(
                "configuration",
                "PUSHOVER_APP_TOKEN missing (channel.config.app_token or env)",
            )
        return token

    @staticmethod
    def _resolve_user_key(config: dict) -> str:
        user_key = config.get("user_key") or config.get("user")
        if not user_key:
            raise AdapterDeliveryError(
                "configuration",
                "pushover channel missing config.user_key",
            )
        return str(user_key)

    @staticmethod
    def _build_message(event: dict) -> tuple[str, str]:
        title = str(event.get("title") or "Notification").strip()
        body_raw = event.get("body")
        if body_raw is None:
            body = ""
        elif isinstance(body_raw, str):
            body = body_raw
        else:
            body = str(body_raw)
        return title, body
