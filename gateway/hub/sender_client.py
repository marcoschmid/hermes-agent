"""Producer-side client for posting signed notifications to the hub."""
from __future__ import annotations

import json
import uuid
from datetime import datetime, timezone
from typing import Any

import httpx

from gateway.hub.hmac_sign import sign


NOTIFICATIONS_PATH = "/v1/notifications"


class SenderError(Exception):
    """Hub sender request failed with a non-success status."""

    def __init__(self, status: int, detail: Any) -> None:
        super().__init__(f"Hub sender request failed with status {status}: {detail}")
        self.status = status
        self.detail = detail


class HubSenderClient:
    def __init__(
        self,
        hub_url: str,
        source_slug: str,
        hub_secret: bytes,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self.hub_url = _notifications_url(hub_url)
        self.source_slug = source_slug
        self.hub_secret = hub_secret
        self._owned_client = client is None
        self._client = client or httpx.AsyncClient(timeout=10.0)
        self._closed = False

    async def send(
        self,
        *,
        topic: str,
        audience: str,
        title: str,
        body: str,
        severity: str = "info",
        urgency: str = "none",
        actionability: str = "info",
        dedupe_key: str | None = None,
        correlation_id: str | None = None,
        payload: dict | None = None,
    ) -> dict:
        """Compute HMAC + POST. Returns hub response data on 200/201. Raises on 4xx/5xx."""
        request_body = {
            "source_slug": self.source_slug,
            "topic": topic,
            "severity": severity,
            "urgency": urgency,
            "actionability": actionability,
            "audience": audience,
            "title": title,
            "body": body,
        }
        if dedupe_key is not None:
            request_body["dedupe_key"] = dedupe_key
        if correlation_id is not None:
            request_body["correlation_id"] = correlation_id
        if payload is not None:
            request_body["payload"] = payload

        body_bytes = json.dumps(
            request_body,
            ensure_ascii=False,
            separators=(",", ":"),
        ).encode("utf-8")
        timestamp = _utc_timestamp()
        nonce = str(uuid.uuid4())
        signature = sign(self.hub_secret, timestamp, nonce, body_bytes)

        response = await self._client.post(
            self.hub_url,
            content=body_bytes,
            headers={
                "Content-Type": "application/json",
                "X-Hub-Timestamp": timestamp,
                "X-Hub-Nonce": nonce,
                "X-Hub-Signature": signature,
            },
        )

        if response.status_code not in (200, 201):
            raise SenderError(response.status_code, _response_detail(response))

        data = response.json()
        if isinstance(data, dict) and "data" in data:
            return data["data"] or {}
        if isinstance(data, dict):
            return data
        return {"data": data}

    async def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        if self._owned_client:
            await self._client.aclose()


def _utc_timestamp() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).strftime("%Y-%m-%dT%H:%M:%SZ")


def _notifications_url(hub_url: str) -> str:
    url = hub_url.rstrip("/")
    if url.endswith(NOTIFICATIONS_PATH):
        return url
    return f"{url}{NOTIFICATIONS_PATH}"


def _response_detail(response: httpx.Response) -> Any:
    try:
        data = response.json()
    except ValueError:
        return response.text
    if isinstance(data, dict) and "detail" in data:
        return data["detail"]
    return data
