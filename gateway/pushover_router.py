"""Standalone Pushover channel router for the notification_worker.

Reuses the Hub PushoverAdapter's POST shape but as a SYNC, hub-free router
that plugs directly into the worker's per-channel factory (duck-typed
``.send(message, issue) -> SendResult``). It bypasses the (dead) Hub->MC
cascade entirely, so it is severance-free by construction: HUB_MC_WRITE_SEVERED
gates only MC-loopback sinks, which this code never touches.

Severity -> Pushover priority (ops notification policy, 2026-06-17):

    debug, info -> -2 (silent; normally never pushed — logged-only)
    notice      ->  0
    warn        ->  0 (normal)
    error       ->  1 (high)
    crit        ->  2 (emergency: retry=120s, expire=3600s, sound=siren)

Credentials come from PUSHOVER_API_TOKEN / PUSHOVER_USER_KEY (the names used by
~/.openclaw/secrets/env/pushover.env), NOT the Hub adapter's PUSHOVER_APP_TOKEN.
"""
from __future__ import annotations

import base64
import os
import urllib.error
import urllib.parse
import urllib.request
from typing import Any, Callable

from .fallback_channels import SendResult

PUSHOVER_API_URL = "https://api.pushover.net/1/messages.json"

# Pushover requires both retry+expire for priority=2 (emergency).
EMERGENCY_RETRY_SECONDS = 120
EMERGENCY_EXPIRE_SECONDS = 3600

# Image attachment limit (Pushover: 5 MB, one per message).
ATTACHMENT_MAX_BYTES = 5 * 1024 * 1024
_IMAGE_MIME = {
    ".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg",
    ".gif": "image/gif", ".webp": "image/webp",
}


def _read_attachment(path: str | None) -> "tuple[str, str] | None":
    """Return (base64, mime) for a sendable image, or None (text-only fallback)."""
    if not path:
        return None
    try:
        if not os.path.isfile(path):
            return None
        size = os.path.getsize(path)
        if size == 0 or size > ATTACHMENT_MAX_BYTES:
            return None
        mime = _IMAGE_MIME.get(os.path.splitext(path)[1].lower(), "image/png")
        with open(path, "rb") as fh:
            return base64.b64encode(fh.read()).decode("ascii"), mime
    except OSError:
        return None

SEVERITY_TO_PRIORITY: dict[str, int] = {
    "debug": -2,
    "info": -2,
    "notice": 0,
    "warn": 0,
    "error": 1,
    "crit": 2,
}

# transport(url, data, timeout) -> (status_code, body_text)
Transport = Callable[[str, dict[str, Any], float], "tuple[int, str]"]


def _urllib_post(url: str, data: dict[str, Any], timeout: float) -> "tuple[int, str]":
    encoded = urllib.parse.urlencode(data).encode()
    req = urllib.request.Request(url, data=encoded, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:  # noqa: S310 (fixed https URL)
            return int(resp.status), resp.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as exc:
        return int(exc.code), exc.read().decode("utf-8", "replace")


class PushoverRouter:
    """Duck-typed channel router: ``.send(message, issue) -> SendResult``.

    The worker only requires ``.send(message=..., issue=...)`` returning an
    object with ``.ok`` (and ``.error``/``.hop`` for logging).
    """

    hop = "pushover"

    def __init__(
        self,
        *,
        api_token: str,
        user_key: str,
        transport: Transport | None = None,
        timeout: float = 10.0,
    ) -> None:
        self._api_token = api_token
        self._user_key = user_key
        self._transport: Transport = transport or _urllib_post
        self._timeout = timeout

    @classmethod
    def from_env(cls, **kwargs: Any) -> "PushoverRouter":
        token = os.environ.get("PUSHOVER_API_TOKEN")
        user = os.environ.get("PUSHOVER_USER_KEY")
        if not token or not user:
            raise ValueError(
                "PushoverRouter requires PUSHOVER_API_TOKEN and PUSHOVER_USER_KEY "
                "in the environment (source ~/.openclaw/secrets/env/pushover.env)"
            )
        return cls(api_token=token, user_key=user, **kwargs)

    def send(self, message: str, issue: dict[str, Any] | None = None, **_: Any) -> SendResult:
        issue = issue or {}
        severity = str(issue.get("severity") or "info").lower()
        priority = SEVERITY_TO_PRIORITY.get(severity, 0)
        title = str(issue.get("title") or "Notification").strip()

        data: dict[str, Any] = {
            "token": self._api_token,
            "user": self._user_key,
            "title": title,
            "message": message or "",
            "priority": priority,
        }
        device = issue.get("device")
        if device:
            data["device"] = str(device)

        # Sound: explicit override wins; crit defaults to the siren.
        sound = issue.get("sound") or ("siren" if severity == "crit" else None)
        if sound:
            data["sound"] = str(sound)

        # Deep-link (tap-to-open). Pushover has no inline actions; this is the substitute.
        url = issue.get("url")
        if url:
            data["url"] = str(url)[:512]
            url_title = issue.get("url_title")
            if url_title:
                data["url_title"] = str(url_title)[:100]

        if priority == 2:
            data["retry"] = EMERGENCY_RETRY_SECONDS
            data["expire"] = EMERGENCY_EXPIRE_SECONDS
        else:
            # Auto-expire low-importance pushes (Pushover ignores ttl for priority=2).
            ttl = issue.get("ttl")
            try:
                ttl_int = int(ttl) if ttl is not None else 0
            except (TypeError, ValueError):
                ttl_int = 0
            if ttl_int > 0:
                data["ttl"] = ttl_int

        # Image attachment (screenshot/preview/chart). Missing/oversized -> text-only.
        attachment = _read_attachment(issue.get("image"))
        if attachment:
            data["attachment_base64"], data["attachment_type"] = attachment

        try:
            status_code, body = self._transport(PUSHOVER_API_URL, data, self._timeout)
        except Exception as exc:  # noqa: BLE001 — network/timeout: report, never raise
            return SendResult(ok=False, hop=self.hop, error=f"pushover transport: {exc}")

        if status_code == 200:
            return SendResult(ok=True, hop=self.hop)
        # 4xx (config/payload) and 5xx (transient) both -> ok=False. The worker's
        # record_failure applies exponential backoff + dead-letter after 5 tries;
        # we do NOT retry here (no per-row retry storm).
        return SendResult(
            ok=False,
            hop=self.hop,
            error=f"pushover HTTP {status_code}: {body[:200]}",
        )
