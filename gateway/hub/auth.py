"""Pilot Bearer auth for the Notification Hub."""

from __future__ import annotations

import hmac
import os
from typing import Optional

from fastapi import Header, HTTPException, status


def _constant_time_equal(a: str, b: str) -> bool:
    return hmac.compare_digest(a.encode("utf-8"), b.encode("utf-8"))


def require_pilot_token(authorization: Optional[str] = Header(default=None)) -> None:
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Missing or invalid Authorization header",
        )
    expected = os.environ.get("HUB_PILOT_TOKEN")
    if not expected:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Pilot token not configured",
        )
    # strip(): tolerate trailing newline/whitespace that env-files commonly carry
    # (e.g. `HUB_PILOT_TOKEN=$(cat file)` keeps the file's trailing \n).
    token = authorization[len("Bearer "):].strip()
    if not _constant_time_equal(token, expected):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid token",
        )
