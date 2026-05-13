"""Helper to mount hub routes on the existing Hermes FastAPI app.

Called by gateway/paperclip_notify_server.py at startup.
"""
from fastapi import FastAPI

from gateway.hub.api import router


def register_hub_routes(app: FastAPI) -> None:
    """Add hub /v1/* endpoints to a host FastAPI app."""
    app.include_router(router)
