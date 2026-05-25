"""Telegram callback-webhook route mounted on the Hub FastAPI app.

POST /telegram/webhook receives callback_query updates from Telegram Bot API.
Wraps gateway.telegram_gateway.TelegramCallbackReceiver (PR #7 merged) which:
- verifies X-Telegram-Bot-Api-Secret-Token header (constant-time)
- persists callback to telegram_callbacks SQLite table (dedup via PK)
- enforces per-user rate-limit
- returns CallbackResult dataclass

The route runs sync receiver.handle_webhook via asyncio.to_thread so the
event loop is never blocked by SQLite I/O (Round-2 HIGH-3 fix). Action-dispatch
(Paperclip-Issue updates) happens async via gateway.telegram_action_dispatcher
consumer-loop.

CRITICAL DEPLOYMENT-GATE (Round-2 HIGH-4):
==========================================
Telegram delivers updates to ONE webhook URL per bot. Mission-Control's
live /api/telegram/webhook handles media:*/dec:*/voice/text. If Marco runs
`setWebhook` pointing at Hub, MC stops receiving those updates — voice
commands, image-gen approve buttons, decision-board dec:* all break.

DO NOT switch the bot webhook URL until either:
  (a) MC remains the front-door and relays approve/reject/skip to Hub, OR
  (b) Hub implements multiplexer for media:*/dec:*/voice/text.

See plan §"Architecture-Decision" + §"Risiken / MC-Webhook-Konflikt".

Config via env (read at receiver-build time):
- TELEGRAM_CALLBACKS_DB: SQLite path (default ~/.hermes/telegram_callbacks.db)
- TELEGRAM_WEBHOOK_SECRET: required (production-fatal if missing — see below)
- TELEGRAM_ALLOWED_USER_IDS: comma-separated (default 128314698)
- TELEGRAM_RATE_LIMIT_PER_MINUTE: default 60
- HERMES_TELEGRAM_WEBHOOK_REQUIRED: if set to "1", startup FAILS when
  TELEGRAM_WEBHOOK_SECRET is missing (production mode). Default unset =
  dev/test mode where missing secret silently skips mount.

See ``projects/jarvis-os-redesign/plans/2026-05-24-p4-track-b-telegram-receiver-wiring.md``.
"""
from __future__ import annotations

import asyncio
import logging
import os
from pathlib import Path
from typing import Any

from fastapi import APIRouter, FastAPI, HTTPException, Request

from gateway.telegram_gateway import CallbackResult, TelegramCallbackReceiver

log = logging.getLogger(__name__)

router = APIRouter()


def build_receiver_from_env() -> TelegramCallbackReceiver:
    """Construct receiver from env-vars. Raises if required secrets missing."""
    secret = os.environ.get("TELEGRAM_WEBHOOK_SECRET", "").strip()
    if not secret:
        raise RuntimeError(
            "TELEGRAM_WEBHOOK_SECRET env-var required for Hub webhook mount. "
            "Generate via `openssl rand -hex 32` and store in "
            "~/.openclaw/secrets/env/notification-hub-tokens.env."
        )

    db_path = os.environ.get("TELEGRAM_CALLBACKS_DB", "").strip() or str(
        Path.home() / ".hermes" / "telegram_callbacks.db"
    )
    db_path = os.path.expanduser(db_path)
    Path(db_path).parent.mkdir(parents=True, exist_ok=True)

    allowed_users_raw = os.environ.get("TELEGRAM_ALLOWED_USER_IDS", "").strip() or "128314698"
    allowed = {u.strip() for u in allowed_users_raw.split(",") if u.strip()}

    rate_limit = int(os.environ.get("TELEGRAM_RATE_LIMIT_PER_MINUTE", "60"))

    return TelegramCallbackReceiver(
        db_path=db_path,
        webhook_secret=secret,
        allowed_user_ids=allowed,
        rate_limit_per_minute=rate_limit,
    )


@router.post("/telegram/webhook", status_code=200)
async def telegram_webhook(request: Request) -> dict[str, Any]:
    """Inbound Telegram callback-query webhook.

    Telegram requires <500ms response, so we synchronously persist + return.
    Action-dispatch (approve→Paperclip-Issue-update) runs async in
    telegram_action_dispatcher consumer-loop.
    """
    receiver: TelegramCallbackReceiver | None = getattr(
        request.app.state, "telegram_receiver", None,
    )
    if receiver is None:
        log.error("telegram_webhook called but app.state.telegram_receiver missing")
        raise HTTPException(status_code=500, detail="receiver not initialized")

    try:
        payload = await request.json()
    except Exception as exc:
        log.warning("telegram_webhook invalid JSON: %s", exc)
        raise HTTPException(status_code=400, detail="invalid_json")

    headers = dict(request.headers)
    # Round-2 HIGH-3: run sync sqlite-write off event-loop so DB lock-contention
    # doesn't block other Hub requests + stay within Telegram's <500ms budget.
    result: CallbackResult = await asyncio.to_thread(
        receiver.handle_webhook, headers=headers, payload=payload,
    )

    if result.status_code >= 400:
        raise HTTPException(status_code=result.status_code, detail=result.error or "rejected")

    return {
        "accepted": result.accepted,
        "duplicate": result.duplicate,
        "ignored": result.ignored,
        "rate_limited": result.rate_limited,
        "callback_type": result.callback_type,
        "issue_id": result.issue_id,
    }


def register_telegram_webhook(app: FastAPI) -> None:
    """Build receiver + attach to app.state + mount router.

    Round-2 MEDIUM-6: production-mode (HERMES_TELEGRAM_WEBHOOK_REQUIRED=1)
    raises if TELEGRAM_WEBHOOK_SECRET missing. Dev/test mode silently skips.
    """
    try:
        receiver = build_receiver_from_env()
    except RuntimeError as exc:
        if os.environ.get("HERMES_TELEGRAM_WEBHOOK_REQUIRED", "").strip() == "1":
            log.error("telegram_webhook REQUIRED but config invalid: %s", exc)
            raise
        log.warning("telegram_webhook NOT mounted (dev-mode): %s", exc)
        return
    receiver.init_schema()
    app.state.telegram_receiver = receiver
    app.include_router(router)
    log.info("telegram_webhook mounted at POST /telegram/webhook")
