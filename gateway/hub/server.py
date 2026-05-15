"""Standalone FastAPI host for the Notification Hub (Phase 2a).

Mounts gateway.hub.api routes via gateway.hub.mount.register_hub_routes().
Auth-Layer: simple Bearer against HUB_PILOT_TOKEN env. No HMAC; v4 scope.
"""

from __future__ import annotations

from fastapi import FastAPI

from gateway.hub.mount import register_hub_routes


def create_app() -> FastAPI:
    app = FastAPI(title="Hermes Notification Hub", version="2a")
    register_hub_routes(app)
    return app


app = create_app()
