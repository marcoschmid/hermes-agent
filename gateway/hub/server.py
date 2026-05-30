"""Standalone FastAPI host for the Notification Hub.

Mounts gateway.hub.api routes via gateway.hub.mount.register_hub_routes().
Auth-Layer: HMAC (v4a) + HUB_PILOT_TOKEN Bearer transition-fallback.
"""

from __future__ import annotations

import asyncio
import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI

from gateway.hub.adapter_registry import close_adapters
from gateway.hub.hub_state import connect as hub_state_connect
from gateway.hub.mount import register_hub_routes
from gateway.hub.nonce_store import prune_old
from gateway.hub.registry_client import RegistryClient
from gateway.hub.registry_sync import RegistrySync, registry_sync_loop, sync_interval_seconds
from gateway.hub.telegram_webhook_route import register_telegram_webhook

logger = logging.getLogger(__name__)

# Prune nonces older than 2x the HMAC max_age_seconds (60s) → 120s default.
# Runs hourly. Keeps hub_nonces table from growing unbounded.
_NONCE_PRUNE_INTERVAL_SECONDS = 3600
_NONCE_PRUNE_MAX_AGE_SECONDS = 120


async def _nonce_prune_loop(db) -> None:
    """Background task: prune stale hub_nonces every hour."""
    while True:
        try:
            await asyncio.sleep(_NONCE_PRUNE_INTERVAL_SECONDS)
            deleted = await prune_old(db, max_age_seconds=_NONCE_PRUNE_MAX_AGE_SECONDS)
            if deleted:
                logger.info("hub_nonces prune deleted %d stale rows", deleted)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.warning("hub_nonces prune iteration failed: %s", exc)


@asynccontextmanager
async def lifespan(app: FastAPI):
    db = await hub_state_connect()
    await db.migrate()
    # Initial prune on startup (catches any stale rows from previous run).
    await prune_old(db, max_age_seconds=_NONCE_PRUNE_MAX_AGE_SECONDS)
    prune_task = asyncio.create_task(_nonce_prune_loop(db))

    # Phase 6b Step 4b: keep a local mirror of the MC notification registry.
    # The mirror is not yet read at dispatch (Step 5) — this only populates it.
    registry = RegistryClient()
    registry_sync = RegistrySync(registry, db)
    # Best-effort initial sync. This MUST NOT crash the lifespan: if MC is down
    # at boot the hub still has to come up (a startup crash here would recreate
    # the very bootstrap-cascade Phase 6b is closing). The periodic loop retries.
    try:
        await registry_sync.sync_once()
    except Exception as exc:
        logger.warning("initial registry mirror sync skipped: %s", exc)
    sync_task = asyncio.create_task(
        registry_sync_loop(registry_sync, interval=sync_interval_seconds())
    )

    try:
        yield
    finally:
        prune_task.cancel()
        sync_task.cancel()
        for task in (prune_task, sync_task):
            try:
                await task
            except asyncio.CancelledError:
                pass
        await registry.close()
        await close_adapters()
        await db.close()


def create_app() -> FastAPI:
    app = FastAPI(title="Hermes Notification Hub", version="v4a", lifespan=lifespan)
    register_hub_routes(app)
    # P4 Track B: Telegram callback webhook (silently-skipped if
    # TELEGRAM_WEBHOOK_SECRET unset — dev/test environments still boot).
    register_telegram_webhook(app)
    return app


app = create_app()
