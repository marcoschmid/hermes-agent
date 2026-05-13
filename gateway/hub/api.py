"""Notification Hub API — Phase 1 dispatcher endpoint for MC-orchestrated notifications.

Routes:
  GET /v1/health — liveness + adapter status
  POST /v1/notifications — single dispatch endpoint (Phase 2+ implementation)
"""
from fastapi import APIRouter

router = APIRouter()


@router.get("/v1/health")
async def health() -> dict:
    """Liveness endpoint for MC-side health-check."""
    return {
        "data": {
            "status": "healthy",
            "version": "0.1.0",
        }
    }
