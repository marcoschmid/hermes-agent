"""Builders for FallbackNotificationRouter sender-callables.

Three production transports:

- **make_hub_sender** — POSTs to Hermes-Hub ``/v1/notifications`` with either
  HUB_PILOT_TOKEN Bearer (legacy/Pilot) or v4a HMAC-SHA256 signature.
- **make_mc_sender** — POSTs to Mission-Control ``/api/board/notifications/events``
  with Bearer ``MC_HUB_TOKEN``. Audit-only sink (kein direct User-Push).
- **make_direct_sender** — Subprocess-invocation of ``safe_telegram_send.sh``;
  user-push via openclaw-CLI primary + api.telegram.org direct-fallback.

**make_default_router** is the convenience entrypoint: reads env-vars for tokens,
builds all 3 senders, and returns a wired ``FallbackNotificationRouter``.
Missing tokens are graceful — sender returns ``ok=False`` so cascade advances.

See ``projects/jarvis-os-redesign/plans/2026-05-24-p4-track-a-pilot-wiring.md``
for architectural context.
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
import shlex
import subprocess
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Literal

import requests

from .fallback_channels import FallbackNotificationRouter
from .hub.hmac_sign import sign as hmac_sign

log = logging.getLogger(__name__)

DEFAULT_HUB_URL = "http://127.0.0.1:8766"
DEFAULT_MC_URL = "http://127.0.0.1:3334"
DEFAULT_SAFE_TELEGRAM_SCRIPT = "~/.openclaw/workspace/scripts/safe_telegram_send.sh"
DEFAULT_RUN_LOG_PATH = "~/.openclaw/run/fallback-notification-router.jsonl"


def _utcnow_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _dedupe_key_from(message: str, issue: dict[str, Any]) -> str:
    """Use issue.id when available, else hash of message body."""
    raw_id = issue.get("id")
    if raw_id is not None and str(raw_id):
        return str(raw_id)
    return hashlib.sha256(message.encode("utf-8")).hexdigest()[:32]


def make_hub_sender(
    *,
    hub_url: str = DEFAULT_HUB_URL,
    auth_mode: Literal["bearer", "hmac"] = "bearer",
    bearer_token: str | None = None,
    hmac_secret: str | None = None,
    source_slug: str,
    topic: str = "ops.notification",
    severity: str = "info",
    urgency: str = "none",
    audience: str = "marco",
    actionability: str = "info",
    timeout_seconds: float = 10.0,
) -> Callable[..., dict[str, Any]]:
    """Build a sender that POSTs NotificationIntent payloads to the Hermes-Hub.

    Returns a callable ``sender(message, issue=None, **_) -> dict``.
    Response shape: ``{"ok": bool, "event_id": str?, "status": str?, "error": str?}``.

    Missing auth-credentials -> sender returns ok=False (graceful, cascade advances).
    """
    endpoint = hub_url.rstrip("/") + "/v1/notifications"

    if auth_mode == "bearer" and not bearer_token:
        return _missing_credentials_sender("hub", "bearer_token missing")
    if auth_mode == "hmac" and not hmac_secret:
        return _missing_credentials_sender("hub", "hmac_secret missing")

    def sender(message: str, issue: dict[str, Any] | None = None, **_: Any) -> dict[str, Any]:
        issue = issue or {}
        title = str(issue.get("title") or "Notification")
        payload = {
            "source_slug": source_slug,
            "topic": topic,
            "severity": severity,
            "urgency": urgency,
            "audience": audience,
            "actionability": actionability,
            "title": title,
            "body": message,
            "dedupe_key": _dedupe_key_from(message, issue),
        }
        body_bytes = json.dumps(payload).encode("utf-8")

        if auth_mode == "bearer":
            headers = {"Authorization": f"Bearer {bearer_token}", "Content-Type": "application/json"}
        else:
            timestamp = _utcnow_iso()
            nonce = uuid.uuid4().hex
            signature = hmac_sign(hmac_secret.encode("utf-8"), timestamp, nonce, body_bytes)
            headers = {
                "Content-Type": "application/json",
                "X-Hub-Timestamp": timestamp,
                "X-Hub-Nonce": nonce,
                "X-Hub-Signature": signature,
            }

        try:
            resp = requests.post(endpoint, data=body_bytes, headers=headers, timeout=timeout_seconds)
        except requests.RequestException as exc:
            log.warning("hub_sender request failed: %s", exc)
            return {"ok": False, "error": f"hub request failed: {exc}"}

        if resp.status_code in (200, 201):
            try:
                data = resp.json().get("data") or {}
            except ValueError:
                data = {}
            return {
                "ok": True,
                "event_id": data.get("event_id"),
                "status": data.get("status"),
            }
        return {
            "ok": False,
            "status_code": resp.status_code,
            "error": (resp.text or "")[:400],
        }

    return sender


def make_mc_sender(
    *,
    mc_url: str = DEFAULT_MC_URL,
    bearer_token: str | None = None,
    source_slug: str,
    topic: str = "ops.notification",
    severity: str = "info",
    urgency: str = "none",
    audience: str = "marco",
    actionability: str = "info",
    timeout_seconds: float = 10.0,
) -> Callable[..., dict[str, Any]]:
    """Build a sender that POSTs NotificationIntent payloads directly to MC.

    Audit-only sink — MC stores the event but does NOT push to Telegram.
    Useful as fallback when Hub-Pipeline is down but MC remains reachable.

    Missing bearer_token -> sender returns ok=False (graceful).
    """
    endpoint = mc_url.rstrip("/") + "/api/board/notifications/events"

    if not bearer_token:
        return _missing_credentials_sender("mc", "bearer_token missing")

    def sender(message: str, issue: dict[str, Any] | None = None, **_: Any) -> dict[str, Any]:
        issue = issue or {}
        title = str(issue.get("title") or "Notification")
        payload = {
            "source_slug": source_slug,
            "topic": topic,
            "severity": severity,
            "urgency": urgency,
            "audience": audience,
            "actionability": actionability,
            "title": title,
            "body": message,
            "dedupe_key": _dedupe_key_from(message, issue),
        }
        headers = {"Authorization": f"Bearer {bearer_token}", "Content-Type": "application/json"}

        try:
            resp = requests.post(endpoint, json=payload, headers=headers, timeout=timeout_seconds)
        except requests.RequestException as exc:
            log.warning("mc_sender request failed: %s", exc)
            return {"ok": False, "error": f"mc request failed: {exc}"}

        if resp.status_code in (200, 201):
            try:
                data = resp.json().get("data") or {}
            except ValueError:
                data = {}
            return {"ok": True, "event_id": data.get("event_id")}
        return {
            "ok": False,
            "status_code": resp.status_code,
            "error": (resp.text or "")[:400],
        }

    return sender


def make_direct_sender(
    *,
    script_path: str = DEFAULT_SAFE_TELEGRAM_SCRIPT,
    target_chat_id: str,
    context: str,
    timeout_seconds: float = 20.0,
) -> Callable[..., dict[str, Any]]:
    """Build a sender that invokes safe_telegram_send.sh as subprocess.

    Returns ``{"ok": bool, "returncode": int, "error": str?}``.
    Missing script_path -> sender returns ok=False.
    """
    resolved_path = os.path.expanduser(script_path)

    def sender(message: str, issue: dict[str, Any] | None = None, **_: Any) -> dict[str, Any]:
        if not Path(resolved_path).is_file():
            return {"ok": False, "error": f"safe_telegram_send.sh missing at {resolved_path}"}
        issue = issue or {}
        cmd = [
            "bash",
            resolved_path,
            "--target", str(target_chat_id),
            "--context", context,
            "--message", message,
        ]
        dedupe = issue.get("id")
        if dedupe is not None and str(dedupe):
            cmd.extend(["--dedupe-key", str(dedupe)])

        try:
            result = subprocess.run(
                cmd, capture_output=True, text=True,
                timeout=timeout_seconds, check=False,
            )
        except subprocess.TimeoutExpired:
            log.warning("direct_sender timed out: %s", shlex.join(cmd))
            return {"ok": False, "error": "direct send timed out"}

        if result.returncode == 0:
            return {"ok": True, "returncode": 0}
        return {
            "ok": False,
            "returncode": result.returncode,
            "error": (result.stderr or "").strip()[:400],
        }

    return sender


def make_default_router(
    *,
    source_slug: str,
    target_chat_id: str,
    context: str,
    hub_url_env: str = "HERMES_HUB_URL",
    hub_token_env: str = "HERMES_HUB_BEARER_TOKEN",
    mc_url_env: str = "MC_HUB_URL",
    mc_token_env: str = "MC_HUB_TOKEN",
    safe_telegram_script_env: str = "SAFE_TELEGRAM_SEND_SCRIPT",
    run_log_path: str = DEFAULT_RUN_LOG_PATH,
    eligibility_gate: Callable[[dict[str, Any]], bool] | None = None,
    topic: str = "ops.notification",
) -> FallbackNotificationRouter:
    """Convenience builder: reads env-vars + constructs all 3 senders + Router.

    Missing env-var tokens result in graceful ok=False senders (cascade advances).
    eligibility_gate default = always-allow (Pilot policy).
    """
    hub_url = os.environ.get(hub_url_env) or DEFAULT_HUB_URL
    hub_token = os.environ.get(hub_token_env, "")
    mc_url = os.environ.get(mc_url_env) or DEFAULT_MC_URL
    mc_token = os.environ.get(mc_token_env, "")
    script_path = os.environ.get(safe_telegram_script_env) or DEFAULT_SAFE_TELEGRAM_SCRIPT

    hub_sender = make_hub_sender(
        hub_url=hub_url,
        auth_mode="bearer",
        bearer_token=hub_token or None,
        source_slug=source_slug,
        topic=topic,
    )
    mc_sender = make_mc_sender(
        mc_url=mc_url,
        bearer_token=mc_token or None,
        source_slug=source_slug,
        topic=topic,
    )
    direct_sender = make_direct_sender(
        script_path=script_path,
        target_chat_id=target_chat_id,
        context=context,
    )

    return FallbackNotificationRouter(
        hermes_send=hub_sender,
        mission_control_send=mc_sender,
        direct_send=direct_sender,
        run_log_path=os.path.expanduser(run_log_path),
        eligibility_gate=eligibility_gate or (lambda _issue: True),
    )


def _missing_credentials_sender(stage: str, reason: str) -> Callable[..., dict[str, Any]]:
    """Return a sender that immediately yields ok=False (graceful cascade-advance)."""
    def sender(_message: str, _issue: dict[str, Any] | None = None, **_: Any) -> dict[str, Any]:
        return {"ok": False, "error": f"{stage}: {reason}"}
    return sender
