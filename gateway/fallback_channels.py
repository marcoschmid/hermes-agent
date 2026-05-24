"""3-stage notification fallback router for Hermes.

Tries hermes -> mission-control -> safe_telegram_send.sh direct.
Each hop logged to JSONL run-log for audit. Eligibility-gate blocks
cascade beyond primary hop for low-priority issues.

Out-of-scope: per-hop timeout, run-log rotation, async dispatch.
"""
from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

log = logging.getLogger(__name__)


@dataclass(slots=True)
class HopResult:
    hop: str
    ok: bool
    error: str = ""
    blocked_by: str = ""


@dataclass(slots=True)
class SendResult:
    ok: bool
    hop: str
    error: str = ""
    blocked_by: str = ""
    hops_attempted: list[HopResult] = field(default_factory=list)


class FallbackNotificationRouter:
    def __init__(self, *,
                 hermes_send: Callable[..., Any],
                 mission_control_send: Callable[..., Any],
                 direct_send: Callable[..., Any],
                 run_log_path: str,
                 eligibility_gate: Callable[[dict[str, Any]], bool] | None = None) -> None:
        self._hermes = hermes_send
        self._mc = mission_control_send
        self._direct = direct_send
        self._run_log = Path(run_log_path)
        self._eligible = eligibility_gate or (lambda _issue: True)

    def send(self, *,
             message: str,
             issue: dict[str, Any] | None = None) -> SendResult:
        issue = issue or {}
        issue_id = str(issue.get("id") or "")
        result = SendResult(ok=False, hop="")

        hermes_hop = self._try_hop("hermes", self._hermes, message, issue)
        result.hops_attempted.append(hermes_hop)
        self._log_hop(issue_id, hermes_hop)
        if hermes_hop.ok:
            result.ok = True
            result.hop = "hermes"
            return result

        if not self._safe_gate(issue):
            result.ok = False
            result.hop = "hermes"
            result.blocked_by = "eligibility-gate"
            self._log_hop(
                issue_id,
                HopResult(hop="eligibility-gate", ok=False, blocked_by="eligibility-gate"),
            )
            return result

        mc_hop = self._try_hop("mission-control", self._mc, message, issue)
        result.hops_attempted.append(mc_hop)
        self._log_hop(issue_id, mc_hop)
        if mc_hop.ok:
            result.ok = True
            result.hop = "mission-control"
            return result

        direct_hop = self._try_hop("direct-fallback", self._direct, message, issue)
        result.hops_attempted.append(direct_hop)
        self._log_hop(issue_id, direct_hop)
        if direct_hop.ok:
            result.ok = True
            result.hop = "direct-fallback"
            return result

        result.ok = False
        result.hop = "direct-fallback"
        result.error = direct_hop.error or "all hops failed"
        return result

    def _try_hop(self, hop: str, sender: Callable[..., Any],
                 message: str, issue: dict[str, Any]) -> HopResult:
        try:
            resp = sender(message=message, issue=issue)
            ok_value = _value(resp, "ok") if resp is not None else True
            ok = True if ok_value is None else bool(ok_value)
            return HopResult(hop=hop, ok=ok)
        except Exception as exc:
            log.warning("hop %s failed: %s", hop, exc)
            return HopResult(hop=hop, ok=False, error=str(exc))

    def _safe_gate(self, issue: dict[str, Any]) -> bool:
        """Invoke eligibility_gate, default-allow on exception."""
        try:
            return bool(self._eligible(issue))
        except Exception as exc:
            log.warning("eligibility_gate raised, default-allow: %s", exc)
            return True

    def _log_hop(self, issue_id: str, hop: HopResult) -> None:
        entry: dict[str, Any] = {
            "timestamp": _utcnow().isoformat(),
            "issue_id": issue_id,
            "hop": hop.hop,
            "ok": hop.ok,
        }
        if hop.error:
            entry["error"] = hop.error
        if hop.blocked_by:
            entry["blocked_by"] = hop.blocked_by
        self._run_log.parent.mkdir(parents=True, exist_ok=True)
        with self._run_log.open("a", encoding="utf-8") as f:
            f.write(json.dumps(entry) + "\n")


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _value(obj: Any, key: str, default: Any = None) -> Any:
    if isinstance(obj, dict):
        return obj.get(key, default)
    if hasattr(obj, key):
        return getattr(obj, key)
    return default
