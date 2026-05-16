"""Telegram delivery adapter for Notification Hub."""
from __future__ import annotations

import os
import time
from typing import Any

import httpx

from gateway.hub.adapters.errors import AdapterDeliveryError

_MARKDOWN_V2_SPECIALS = set("_*[]()~`>#+-=|{}.!" + "\\")


def escape_markdown_v2(text: str) -> str:
    return "".join(f"\\{char}" if char in _MARKDOWN_V2_SPECIALS else char for char in text)


class TelegramAdapter:
    name = "telegram"

    def __init__(
        self,
        client: httpx.AsyncClient | None = None,
        token: str | None = None,
        timeout: float = 5.0,
    ) -> None:
        self._token = token
        self._timeout = timeout
        self._owned_client = client is None
        self._client = client or httpx.AsyncClient(timeout=timeout)

    async def close(self) -> None:
        if self._owned_client:
            await self._client.aclose()

    async def send(self, event: dict, channel: dict):
        from gateway.hub.adapter_registry import AdapterResult

        token = self._resolve_token()
        chat_id = self._resolve_chat_id(channel)
        body = self._message_body(event)
        payload = {
            "chat_id": chat_id,
            "text": escape_markdown_v2(body),
            "parse_mode": "MarkdownV2",
        }
        started = time.perf_counter()

        try:
            response = await self._client.post(
                f"https://api.telegram.org/bot{token}/sendMessage",
                json=payload,
                timeout=self._timeout,
            )
        except httpx.TimeoutException as exc:
            raise AdapterDeliveryError("timeout", str(exc)) from exc
        except httpx.HTTPError as exc:
            raise AdapterDeliveryError("network", str(exc)) from exc

        latency_ms = int((time.perf_counter() - started) * 1000)
        if response.status_code != 200:
            raise AdapterDeliveryError(response.status_code, response.text)

        data = response.json()
        result = data.get("result") if isinstance(data, dict) else {}
        message_id = result.get("message_id") if isinstance(result, dict) else None
        return AdapterResult(
            status="delivered",
            provider_message_id=str(message_id) if message_id is not None else None,
            latency_ms=latency_ms,
        )

    def _resolve_token(self) -> str:
        token = (
            self._token
            if self._token is not None
            else os.environ.get("TELEGRAM_HERMES_BOT_TOKEN")
        )
        if not token:
            raise AdapterDeliveryError("configuration", "TELEGRAM_HERMES_BOT_TOKEN is not set")
        return token

    @staticmethod
    def _resolve_chat_id(channel: dict) -> str:
        chat_id = channel.get("target_ref") or channel.get("target") or channel.get("chat_id")
        if not chat_id:
            raise AdapterDeliveryError("configuration", "telegram channel missing target_ref/target/chat_id")
        return str(chat_id)

    @staticmethod
    def _message_body(event: dict[str, Any]) -> str:
        body = event.get("body", "")
        if body is None:
            return ""
        if isinstance(body, str):
            return body
        return str(body)
