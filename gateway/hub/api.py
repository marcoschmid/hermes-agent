"""Notification Hub API — Phase 1 dispatcher endpoint for MC-orchestrated notifications.

Routes:
  GET /v1/health — liveness + adapter status
  POST /v1/notifications — single dispatch endpoint, runs the 8-step pipeline
"""
import hashlib
from typing import Optional

from fastapi import APIRouter, Header, HTTPException

from gateway.hub.pipeline import run_pipeline
from gateway.hub.registry_client import RegistryClient
from gateway.hub.schemas import NotificationIntent

router = APIRouter()

# Module-level shared client; created lazily on first request.
_registry: Optional[RegistryClient] = None


def get_registry() -> RegistryClient:
    """Lazily-instantiated shared registry client (httpx connection pool)."""
    global _registry
    if _registry is None:
        _registry = RegistryClient()
    return _registry


def set_registry(registry: Optional[RegistryClient]) -> None:
    """Test-helper. Set a custom registry (or None to reset)."""
    global _registry
    _registry = registry


@router.get("/v1/health")
async def health() -> dict:
    """Liveness endpoint for MC-side health-check."""
    return {
        "data": {
            "status": "healthy",
            "version": "0.1.0",
        }
    }


@router.post("/v1/notifications")
async def notifications(
    intent: NotificationIntent,
    authorization: Optional[str] = Header(default=None),
) -> dict:
    """Run the 8-step dispatch pipeline for an inbound notification intent.

    Auth: Bearer token, hashed (SHA-256) and matched server-side against
    sources.token_hash via RegistryClient.get_source.
    """
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="Missing Bearer token")
    token = authorization.removeprefix("Bearer ").strip()
    if not token:
        raise HTTPException(status_code=401, detail="Empty Bearer token")
    token_hash = hashlib.sha256(token.encode("utf-8")).hexdigest()

    result = await run_pipeline(
        intent,
        source_token_hash=token_hash,
        registry=get_registry(),
    )
    return {"data": result.model_dump(exclude_none=True)}
