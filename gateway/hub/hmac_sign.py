"""HMAC sign + verify helper for Notification Hub Phase v4a.

Contract (sender ↔ receiver):
  signature = HMAC-SHA256(secret, "{timestamp}\\n{nonce}\\n{body_sha256_hex}")

Headers (case-insensitive):
  X-Hub-Timestamp: ISO-8601 UTC (e.g. 2026-05-16T15:30:00Z)
  X-Hub-Nonce:     UUID-v4 or random 128-bit hex
  X-Hub-Signature: hex-encoded HMAC-SHA256

Server-side guarantees (verify):
  1. timestamp within ±max_age_seconds (default 60)
  2. signature matches (constant-time compare)
  3. nonce not in replay-cache (caller checks separately)

Producer-spec is mirrored in docs/HUB_SENDER_SPEC.md (v4b).
"""
from __future__ import annotations

import hashlib
import hmac
from datetime import datetime, timezone


class HmacError(Exception):
    """HMAC-signature verification failure. error_code is set for HTTP mapping."""

    def __init__(self, error_code: str, message: str, status_code: int = 401) -> None:
        super().__init__(message)
        self.error_code = error_code
        self.message = message
        self.status_code = status_code


def sign(secret: bytes, timestamp: str, nonce: str, body: bytes) -> str:
    """Compute the canonical signature for a hub-bound request."""
    body_hash = hashlib.sha256(body).hexdigest()
    msg = f"{timestamp}\n{nonce}\n{body_hash}".encode()
    return hmac.new(secret, msg, hashlib.sha256).hexdigest()


def verify(
    secret: bytes,
    timestamp: str,
    nonce: str,
    body: bytes,
    signature: str,
    *,
    max_age_seconds: int = 60,
    now: datetime | None = None,
) -> None:
    """Verify signature + timestamp window. Caller checks nonce-replay separately."""
    if not timestamp or not nonce or not signature:
        raise HmacError("missing_header", "X-Hub-Timestamp/Nonce/Signature required", 422)

    try:
        ts = datetime.fromisoformat(timestamp.replace("Z", "+00:00"))
    except ValueError as exc:
        raise HmacError("bad_timestamp", f"Cannot parse timestamp: {timestamp}", 422) from exc

    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=timezone.utc)

    current = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    delta = abs((current - ts).total_seconds())
    if delta > max_age_seconds:
        raise HmacError("stale_timestamp", f"Timestamp drift {delta:.0f}s > {max_age_seconds}s", 401)

    expected = sign(secret, timestamp, nonce, body)
    if not hmac.compare_digest(expected.encode(), signature.encode()):
        raise HmacError("bad_signature", "HMAC signature mismatch", 401)
