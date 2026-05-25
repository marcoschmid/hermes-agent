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

Hardening (Round-2):
- URL allowlist (loopback-only by default); prevents bearer-token exfil to
  attacker-controlled hosts via poisoned HERMES_HUB_URL / MC_HUB_URL env.
- Body-budget truncation to 4000-char NotificationIntent schema cap.
- Token-env resolution at SEND time (not build time) — tolerates token
  rotation and late-binding in LaunchAgent processes.
- Direct-sender dedupe_key normalization (strip + hash-fallback for blank IDs).
- 2xx responses without parseable JSON treated as failure (catches degraded
  proxies / wrong endpoints returning HTML 200).

See ``projects/jarvis-os-redesign/plans/2026-05-24-p4-track-a-pilot-wiring.md``.
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
from urllib.parse import urlparse

import requests

from .fallback_channels import FallbackNotificationRouter
from .hub.hmac_sign import sign as hmac_sign

log = logging.getLogger(__name__)

DEFAULT_HUB_URL = "http://127.0.0.1:8766"
DEFAULT_MC_URL = "http://127.0.0.1:3334"
DEFAULT_SAFE_TELEGRAM_SCRIPT = "~/.openclaw/workspace/scripts/safe_telegram_send.sh"
DEFAULT_RUN_LOG_PATH = "~/.openclaw/run/fallback-notification-router.jsonl"

# URL allowlist: loopback-only by default. Override via *_ALLOWED_HOSTS env-var
# (comma-separated hostnames). Bearer/HMAC credentials never leave allowlisted hosts.
DEFAULT_ALLOWED_HOSTS = frozenset({"127.0.0.1", "localhost", "::1"})

# NotificationIntent schema body cap — bodies above this are truncated server-side
# with 422. We truncate client-side and add an explicit marker so caller can detect.
NOTIFICATION_BODY_MAX_CHARS = 4000
BODY_TRUNCATION_MARKER = "\n…[truncated]"


def _utcnow_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _validate_url(url: str, *, allowed_hosts: frozenset[str], context: str) -> None:
    """Raise ValueError if scheme/host outside allowlist."""
    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https"):
        raise ValueError(f"{context}: unsupported URL scheme {parsed.scheme!r}")
    host = (parsed.hostname or "").lower()
    if host not in allowed_hosts:
        raise ValueError(
            f"{context}: host {host!r} not in allowlist {sorted(allowed_hosts)}. "
            f"Set {context.upper().replace('-', '_')}_ALLOWED_HOSTS env to override."
        )


def _resolve_allowed_hosts(env_var: str) -> frozenset[str]:
    """Read comma-separated hosts from env-var, else default loopback set."""
    raw = os.environ.get(env_var, "").strip()
    if not raw:
        return DEFAULT_ALLOWED_HOSTS
    hosts = {h.strip().lower() for h in raw.split(",") if h.strip()}
    return frozenset(hosts) if hosts else DEFAULT_ALLOWED_HOSTS


def _truncate_body(body: str) -> str:
    """Truncate to NotificationIntent schema cap with explicit marker."""
    if len(body) <= NOTIFICATION_BODY_MAX_CHARS:
        return body
    keep = NOTIFICATION_BODY_MAX_CHARS - len(BODY_TRUNCATION_MARKER)
    return body[:keep] + BODY_TRUNCATION_MARKER


def _dedupe_key_from(message: str, issue: dict[str, Any]) -> str:
    """Use issue.id when meaningful (non-blank), else hash of message body."""
    raw_id = issue.get("id")
    if raw_id is not None:
        stripped = str(raw_id).strip()
        if stripped:
            return stripped
    return hashlib.sha256(message.encode("utf-8")).hexdigest()[:32]


def _resolve_token(
    *,
    direct_value: str | None,
    env_var: str | None,
) -> str | None:
    """Prefer direct constructor-arg; else read env at call-time."""
    if direct_value:
        return direct_value
    if env_var:
        val = os.environ.get(env_var, "").strip()
        return val or None
    return None


def make_hub_sender(
    *,
    hub_url: str = DEFAULT_HUB_URL,
    auth_mode: Literal["bearer", "hmac"] = "bearer",
    bearer_token: str | None = None,
    bearer_token_env: str | None = None,
    hmac_secret: str | None = None,
    hmac_secret_env: str | None = None,
    source_slug: str,
    topic: str = "ops.notification",
    severity: str = "info",
    urgency: str = "none",
    audience: str = "marco",
    actionability: str = "info",
    timeout_seconds: float = 10.0,
    allowed_hosts: frozenset[str] = DEFAULT_ALLOWED_HOSTS,
) -> Callable[..., dict[str, Any]]:
    """Build a sender that POSTs NotificationIntent payloads to the Hermes-Hub.

    Tokens are resolved at SEND time (not build time) — pass ``bearer_token`` for
    immediate values or ``bearer_token_env`` for late-binding env-var lookup.

    URL is validated against ``allowed_hosts`` at build time (loopback default).
    Bodies are truncated to NOTIFICATION_BODY_MAX_CHARS with explicit marker.
    """
    _validate_url(hub_url, allowed_hosts=allowed_hosts, context="hub-url")
    endpoint = hub_url.rstrip("/") + "/v1/notifications"

    def sender(message: str, issue: dict[str, Any] | None = None, **_: Any) -> dict[str, Any]:
        if auth_mode == "bearer":
            token = _resolve_token(direct_value=bearer_token, env_var=bearer_token_env)
            if not token:
                return {"ok": False, "error": "hub: bearer token missing"}
            secret = None
        else:
            token = None
            secret = _resolve_token(direct_value=hmac_secret, env_var=hmac_secret_env)
            if not secret:
                return {"ok": False, "error": "hub: hmac secret missing"}

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
            "body": _truncate_body(message),
            "dedupe_key": _dedupe_key_from(message, issue),
        }
        body_bytes = json.dumps(payload).encode("utf-8")

        if auth_mode == "bearer":
            headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}
        else:
            timestamp = _utcnow_iso()
            nonce = uuid.uuid4().hex
            signature = hmac_sign(secret.encode("utf-8"), timestamp, nonce, body_bytes)
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

        return _parse_intent_response(resp, stage="hub")

    return sender


def make_mc_sender(
    *,
    mc_url: str = DEFAULT_MC_URL,
    bearer_token: str | None = None,
    bearer_token_env: str | None = None,
    source_slug: str,
    topic: str = "ops.notification",
    severity: str = "info",
    urgency: str = "none",
    audience: str = "marco",
    actionability: str = "info",
    timeout_seconds: float = 10.0,
    allowed_hosts: frozenset[str] = DEFAULT_ALLOWED_HOSTS,
) -> Callable[..., dict[str, Any]]:
    """Build a sender that POSTs NotificationIntent payloads directly to MC.

    Audit-only sink — MC stores the event but does NOT push to Telegram.
    Token resolved at SEND time; URL allowlist enforced at build time.
    """
    _validate_url(mc_url, allowed_hosts=allowed_hosts, context="mc-url")
    endpoint = mc_url.rstrip("/") + "/api/board/notifications/events"

    def sender(message: str, issue: dict[str, Any] | None = None, **_: Any) -> dict[str, Any]:
        token = _resolve_token(direct_value=bearer_token, env_var=bearer_token_env)
        if not token:
            return {"ok": False, "error": "mc: bearer_token missing"}

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
            "body": _truncate_body(message),
            "dedupe_key": _dedupe_key_from(message, issue),
        }
        headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}

        try:
            resp = requests.post(endpoint, json=payload, headers=headers, timeout=timeout_seconds)
        except requests.RequestException as exc:
            log.warning("mc_sender request failed: %s", exc)
            return {"ok": False, "error": f"mc request failed: {exc}"}

        return _parse_intent_response(resp, stage="mc")

    return sender


def make_direct_sender(
    *,
    script_path: str = DEFAULT_SAFE_TELEGRAM_SCRIPT,
    target_chat_id: str,
    context: str,
    timeout_seconds: float = 20.0,
) -> Callable[..., dict[str, Any]]:
    """Build a sender that invokes safe_telegram_send.sh as subprocess.

    Always passes ``--dedupe-key`` (uses _dedupe_key_from to compute hash
    fallback if issue.id is missing/blank).
    """
    resolved_path = os.path.expanduser(script_path)

    def sender(message: str, issue: dict[str, Any] | None = None, **_: Any) -> dict[str, Any]:
        if not Path(resolved_path).is_file():
            return {"ok": False, "error": f"safe_telegram_send.sh missing at {resolved_path}"}
        issue = issue or {}
        dedupe_key = _dedupe_key_from(message, issue)
        # Round-2 HIGH-2: caller-supplied target/context override factory-default.
        # Critical for outbox-cli callers that pass --target X --context Y.
        effective_target = str(issue.get("target") or target_chat_id)
        effective_context = str(issue.get("context") or context)
        cmd = [
            "bash",
            resolved_path,
            "--target", effective_target,
            "--context", effective_context,
            "--message", message,
            "--dedupe-key", dedupe_key,
        ]

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
    hub_auth_mode: Literal["bearer", "hmac"] = "bearer",
    hub_url_env: str = "HERMES_HUB_URL",
    hub_token_env: str = "HERMES_HUB_BEARER_TOKEN",
    hub_hmac_secret_env: str = "HERMES_HUB_HMAC_SECRET",
    hub_allowed_hosts_env: str = "HERMES_HUB_ALLOWED_HOSTS",
    mc_url_env: str = "MC_HUB_URL",
    mc_token_env: str = "MC_HUB_TOKEN",
    mc_allowed_hosts_env: str = "MC_HUB_ALLOWED_HOSTS",
    safe_telegram_script_env: str = "SAFE_TELEGRAM_SEND_SCRIPT",
    run_log_path: str = DEFAULT_RUN_LOG_PATH,
    eligibility_gate: Callable[[dict[str, Any]], bool] | None = None,
    topic: str = "ops.notification",
) -> FallbackNotificationRouter:
    """Convenience builder: reads env-vars + constructs all 3 senders + Router.

    Tokens resolved at send-time so rotation/late-binding works.
    URLs validated against allowlists (loopback default; override via *_ALLOWED_HOSTS env).

    Hub auth modes:
    - ``bearer`` (default, Pilot/transitional): Hub accepts only HUB_PILOT_TOKEN.
    - ``hmac`` (v4b production): per-source HMAC-SHA256 — secret read from
      ``hub_hmac_secret_env`` at send-time, must match MC source-registry hub_secret.
    """
    hub_url = os.environ.get(hub_url_env) or DEFAULT_HUB_URL
    mc_url = os.environ.get(mc_url_env) or DEFAULT_MC_URL
    script_path = os.environ.get(safe_telegram_script_env) or DEFAULT_SAFE_TELEGRAM_SCRIPT
    hub_allowed = _resolve_allowed_hosts(hub_allowed_hosts_env)
    mc_allowed = _resolve_allowed_hosts(mc_allowed_hosts_env)

    hub_sender_kwargs: dict[str, Any] = {
        "hub_url": hub_url,
        "auth_mode": hub_auth_mode,
        "source_slug": source_slug,
        "topic": topic,
        "allowed_hosts": hub_allowed,
    }
    if hub_auth_mode == "bearer":
        hub_sender_kwargs["bearer_token_env"] = hub_token_env
    else:
        hub_sender_kwargs["hmac_secret_env"] = hub_hmac_secret_env
    hub_sender = make_hub_sender(**hub_sender_kwargs)
    mc_sender = make_mc_sender(
        mc_url=mc_url,
        bearer_token_env=mc_token_env,
        source_slug=source_slug,
        topic=topic,
        allowed_hosts=mc_allowed,
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


def _parse_intent_response(resp: Any, *, stage: str) -> dict[str, Any]:
    """Parse Hub/MC NotificationIntent response.

    Round-2: 2xx without parseable JSON containing data.event_id is treated
    as failure (catches degraded proxies returning 200 HTML/empty body).
    """
    status_code = resp.status_code
    if status_code not in (200, 201):
        return {
            "ok": False,
            "status_code": status_code,
            "error": (resp.text or "")[:400],
        }
    try:
        body = resp.json()
    except (ValueError, json.JSONDecodeError):
        return {
            "ok": False,
            "status_code": status_code,
            "error": f"{stage}: 2xx response not JSON",
        }
    data = (body or {}).get("data") if isinstance(body, dict) else None
    if not isinstance(data, dict) or not data.get("event_id"):
        return {
            "ok": False,
            "status_code": status_code,
            "error": f"{stage}: 2xx response missing data.event_id",
        }
    result = {"ok": True, "event_id": data.get("event_id")}
    if data.get("status"):
        result["status"] = data["status"]
    return result
