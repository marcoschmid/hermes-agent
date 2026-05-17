"""Decision-Board emitter -- push Hermes approval requests as decision envelopes to MC.

Additive: silent no-op when DECISION_BOARD_HERMES_SECRET is unset. Never blocks the
existing approval flow.
"""
from __future__ import annotations

import hashlib
import hmac as _hmac
import json
import logging
import os
import time
from typing import Any

import httpx

logger = logging.getLogger(__name__)

INGEST_URL = os.environ.get(
    "MC_DECISION_INGEST_URL",
    "http://127.0.0.1:3334/api/board/decision-board/ingest",
)
HUB_SECRET = os.environ.get("DECISION_BOARD_HERMES_SECRET", "")
CALLBACK_BASE = os.environ.get("HERMES_CALLBACK_BASE", "http://127.0.0.1:8787")


def _hash(*parts: str) -> str:
    h = hashlib.sha256()
    for p in parts:
        h.update(p.encode("utf-8"))
    return h.hexdigest()


def _sign(body: bytes) -> tuple[str, int]:
    ts_ms = int(time.time() * 1000)
    # HMAC binds (timestamp.body) -- replays with a stale (body, sig) pair but
    # a fresh in-window timestamp are rejected by MC. Must match the verify
    # contract in src/lib/decision-board-hmac.ts (computeSignature).
    payload = f"{ts_ms}.".encode("utf-8") + body
    sig = _hmac.new(HUB_SECRET.encode("utf-8"), payload, hashlib.sha256).hexdigest()
    return sig, ts_ms


def emit_approval_to_decision_board(
    *,
    session_key: str,
    command: str,
    pattern_key: str,
    description: str,
) -> bool:
    """Push a dangerous-command approval as a Decision-Board envelope.

    Returns True on success. Silently returns False on any failure
    (network, missing secret, etc) -- never raises.
    """
    if not HUB_SECRET:
        logger.debug("decision_board_emitter: HUB_SECRET not configured; skipping")
        return False

    # session_key + command_hash gives stable ref + idempotency
    ref = f"{session_key}:{_hash(command, pattern_key)[:16]}"
    envelope: dict[str, Any] = {
        "source": {
            "system": "hermes",
            "ref": ref,
            "callback_url": f"{CALLBACK_BASE}/approvals/{session_key}/resolve",
        },
        "question": (description or f"Dangerous command: {pattern_key}")[:140],
        "context_md": f"**Command:**\n```\n{command}\n```\n\n**Pattern:** {pattern_key}",
        "type": "gate",
        "options": [
            {"id": "approve", "label": "Approve"},
            {"id": "reject", "label": "Reject"},
            {"id": "approve_session", "label": "Approve (this session only)"},
        ],
        "evidence": {"hash": _hash(session_key, command, pattern_key)},
        "risk_class": "high",
        "severity": "warn",
        "blocking": True,
    }
    body = json.dumps(envelope).encode("utf-8")
    sig, ts_ms = _sign(body)

    try:
        r = httpx.post(
            INGEST_URL,
            content=body,
            headers={
                "Content-Type": "application/json",
                "X-Decision-Signature": sig,
                "X-Decision-Timestamp": str(ts_ms),
            },
            timeout=5.0,
        )
        if r.status_code in (200, 201):
            return True
        logger.warning(
            "decision_board_emitter: ingest returned %s -- %s",
            r.status_code,
            r.text[:200],
        )
        return False
    except Exception as e:  # noqa: BLE001
        logger.warning("decision_board_emitter: post failed -- %s", e)
        return False
