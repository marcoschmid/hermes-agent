"""Standalone FastAPI host for the Notification Hub (Phase 2a).

Mounts gateway.hub.api routes via gateway.hub.mount.register_hub_routes().
Auth-Layer: simple Bearer against HUB_PILOT_TOKEN env. No HMAC; v4 scope.
"""

from __future__ import annotations

from contextlib import asynccontextmanager

from fastapi import FastAPI

from gateway.hub.adapter_registry import close_adapters
from gateway.hub.hub_state import connect as hub_state_connect
from gateway.hub.mount import register_hub_routes


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Apply hub_state schema migrations at startup (T0b table is currently
    # unused by pipeline but DAO + schema must exist so v4 wiring is durable).
    db = await hub_state_connect()
    await db.migrate()
    try:
        yield
    finally:
        await close_adapters()
        await db.close()


def create_app() -> FastAPI:
    app = FastAPI(title="Hermes Notification Hub", version="2a", lifespan=lifespan)
    register_hub_routes(app)
    return app


app = create_app()
