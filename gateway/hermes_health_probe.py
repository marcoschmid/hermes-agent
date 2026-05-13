"""Hermes health probe — Jarvis-OS Phase-4 4c-1.

Reads gateway runtime status (gateway/status.py write_runtime_status) and
classifies Hermes into one of three states for fallback decisions:

- `healthy`  — gateway alive, last heartbeat fresh (< 90s)
- `degraded` — alive but heartbeat stale (>= 90s, < 300s) or last_exit nonzero
- `down`     — no status file, file older than 300s, or pid not running

An external observer script polls this probe at a fixed interval and decides
whether to trigger the Paperclip mc-dispatch-fallback endpoint (4c-2).
"""
from __future__ import annotations

import json
import logging
import os
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal, Optional

logger = logging.getLogger(__name__)

HermesHealthState = Literal["healthy", "degraded", "down"]

DEFAULT_FRESH_HEARTBEAT_SECONDS = 90
DEFAULT_STALE_HEARTBEAT_SECONDS = 300


@dataclass(frozen=True)
class HermesHealthSnapshot:
    state: HermesHealthState
    pid: Optional[int]
    heartbeat_age_seconds: Optional[float]
    last_exit_code: Optional[int]
    status_file_age_seconds: Optional[float]
    reason: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "state": self.state,
            "pid": self.pid,
            "heartbeat_age_seconds": self.heartbeat_age_seconds,
            "last_exit_code": self.last_exit_code,
            "status_file_age_seconds": self.status_file_age_seconds,
            "reason": self.reason,
        }


def _default_status_path() -> Path:
    hermes_home = os.environ.get("HERMES_HOME")
    if hermes_home:
        return Path(hermes_home).expanduser() / "runtime" / "gateway-status.json"
    return Path.home() / ".hermes" / "runtime" / "gateway-status.json"


def _pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
        return True
    except (ProcessLookupError, PermissionError):
        return False
    except OSError:
        return False


def _read_status(path: Path) -> Optional[dict[str, Any]]:
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text())
    except (ValueError, OSError) as exc:
        logger.warning("status file unreadable at %s: %s", path, exc)
        return None


def _parse_heartbeat_seconds(value: Any) -> Optional[float]:
    if value is None:
        return None
    try:
        from datetime import datetime, timezone

        if isinstance(value, (int, float)):
            return time.time() - float(value)
        if isinstance(value, str):
            cleaned = value.rstrip("Z")
            try:
                dt = datetime.fromisoformat(cleaned)
            except ValueError:
                return None
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            return (datetime.now(timezone.utc) - dt).total_seconds()
        return None
    except Exception:
        return None


def probe_hermes_health(
    *,
    status_path: Optional[Path] = None,
    now: Optional[float] = None,
    fresh_heartbeat_seconds: int = DEFAULT_FRESH_HEARTBEAT_SECONDS,
    stale_heartbeat_seconds: int = DEFAULT_STALE_HEARTBEAT_SECONDS,
) -> HermesHealthSnapshot:
    """Probe and classify Hermes health state."""
    path = status_path or _default_status_path()
    now_ts = now if now is not None else time.time()

    status = _read_status(path)
    if status is None:
        return HermesHealthSnapshot(
            state="down",
            pid=None,
            heartbeat_age_seconds=None,
            last_exit_code=None,
            status_file_age_seconds=None,
            reason="status file missing or unreadable",
        )

    file_age = now_ts - path.stat().st_mtime
    if file_age > stale_heartbeat_seconds:
        return HermesHealthSnapshot(
            state="down",
            pid=status.get("pid") if isinstance(status.get("pid"), int) else None,
            heartbeat_age_seconds=None,
            last_exit_code=status.get("last_exit_code"),
            status_file_age_seconds=file_age,
            reason=f"status file older than {stale_heartbeat_seconds}s",
        )

    pid = status.get("pid") if isinstance(status.get("pid"), int) else None
    if pid is not None and not _pid_alive(pid):
        return HermesHealthSnapshot(
            state="down",
            pid=pid,
            heartbeat_age_seconds=None,
            last_exit_code=status.get("last_exit_code"),
            status_file_age_seconds=file_age,
            reason=f"pid {pid} not running",
        )

    heartbeat_age = _parse_heartbeat_seconds(status.get("heartbeat_at") or status.get("last_heartbeat_at"))
    last_exit = status.get("last_exit_code")

    if heartbeat_age is None:
        return HermesHealthSnapshot(
            state="degraded",
            pid=pid,
            heartbeat_age_seconds=None,
            last_exit_code=last_exit,
            status_file_age_seconds=file_age,
            reason="heartbeat_at missing from status",
        )

    if heartbeat_age >= stale_heartbeat_seconds:
        return HermesHealthSnapshot(
            state="down",
            pid=pid,
            heartbeat_age_seconds=heartbeat_age,
            last_exit_code=last_exit,
            status_file_age_seconds=file_age,
            reason=f"heartbeat stale {heartbeat_age:.0f}s",
        )

    if heartbeat_age >= fresh_heartbeat_seconds:
        return HermesHealthSnapshot(
            state="degraded",
            pid=pid,
            heartbeat_age_seconds=heartbeat_age,
            last_exit_code=last_exit,
            status_file_age_seconds=file_age,
            reason=f"heartbeat aged {heartbeat_age:.0f}s",
        )

    if isinstance(last_exit, int) and last_exit != 0:
        return HermesHealthSnapshot(
            state="degraded",
            pid=pid,
            heartbeat_age_seconds=heartbeat_age,
            last_exit_code=last_exit,
            status_file_age_seconds=file_age,
            reason=f"last_exit_code={last_exit}",
        )

    return HermesHealthSnapshot(
        state="healthy",
        pid=pid,
        heartbeat_age_seconds=heartbeat_age,
        last_exit_code=last_exit,
        status_file_age_seconds=file_age,
        reason="ok",
    )
