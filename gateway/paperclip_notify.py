"""FastAPI router exposing /paperclip/notify.

Receives JSON alerts from the paperclip routine-check runner, authenticates via
bearer token, suppresses duplicates through `Dedupe`, then forwards a formatted
message to Telegram.

Mount via:
    from gateway.paperclip_notify import build_router
    from gateway.platforms.telegram import send_message  # or equivalent
    app.include_router(build_router(telegram_send=send_message))
"""
import asyncio
import os
from typing import Callable, Optional

from fastapi import APIRouter, Header, HTTPException
from pydantic import BaseModel

from hermes_constants import get_hermes_home

from .paperclip_notify_dedupe import Dedupe


class NotifyPayload(BaseModel):
    check: str
    status: str
    previous_status: Optional[str] = None
    findings: int
    summary: str
    content_hash: str
    scheduled_for: str
    details_hint: str


def _read_token() -> Optional[str]:
    env = os.environ.get("PAPERCLIP_NOTIFY_TOKEN")
    if env:
        return env
    try:
        return (get_hermes_home() / "secrets" / "notify-token").read_text().strip()
    except FileNotFoundError:
        return None


def _default_db_path() -> str:
    return os.environ.get(
        "PAPERCLIP_NOTIFY_DB",
        str(get_hermes_home() / "cron" / "paperclip_notify_dedupe.db"),
    )


def build_router(telegram_send: Callable[[str], None]) -> APIRouter:
    router = APIRouter()
    dedupe = Dedupe(_default_db_path())
    # Serialize dedupe-claim → send → record so two near-simultaneous identical
    # alerts can never both pass should_send() and double-fire Telegram.
    send_lock = asyncio.Lock()

    @router.post("/paperclip/notify", status_code=200)
    async def notify(
        payload: NotifyPayload, authorization: Optional[str] = Header(None)
    ):
        expected = _read_token()
        provided = (
            authorization.split(" ", 1)[1]
            if authorization and authorization.startswith("Bearer ")
            else None
        )
        if not expected or provided != expected:
            raise HTTPException(status_code=401, detail="unauthorized")

        async with send_lock:
            if not dedupe.should_send(
                payload.check,
                payload.content_hash,
                payload.previous_status,
                payload.status,
            ):
                return {"sent": False, "deduped": True}
            dedupe.record(payload.check, payload.content_hash)

        telegram_send(
            f"[paperclip] {payload.check} ({payload.status}): {payload.summary}\n"
            f"→ {payload.details_hint}"
        )
        return {"sent": True}

    return router
