"""Notification Hub API — Phase 1 dispatcher endpoint for MC-orchestrated notifications.

Routes:
  GET /v1/health — liveness + adapter status
  POST /v1/notifications — single dispatch endpoint, runs the 8-step pipeline
"""
from typing import Optional

from fastapi import APIRouter, Depends

from gateway.hub.auth import require_pilot_token
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


@router.post("/v1/notifications", dependencies=[Depends(require_pilot_token)])
async def notifications(
    intent: NotificationIntent,
) -> dict:
    """Run the 8-step dispatch pipeline for an inbound notification intent.

    Phase 2a auth model: HUB_PILOT_TOKEN at edge is the *only* auth. The
    per-source token-hash check from Phase 1 is intentionally bypassed below
    (source_token_hash=""). This widens the trust boundary — any caller with
    the pilot token can dispatch as ANY registered source slug without that
    source's own token. Acceptable for localhost-only standalone deployment.

    Phase v4 will replace this with HMAC(timestamp+nonce+body) per-source
    secret, restoring per-source identity + adding replay protection.
    See docs/plans/2026-05-16-phase-v4-foundation.md (AD-1, AD-2).
    """
    result = await run_pipeline(
        intent,
        source_token_hash="",  # Phase 2a: see docstring above; v4 will fix.
        registry=get_registry(),
    )
    return {"data": result.model_dump(exclude_none=True)}
