"""3-stage notification fallback router for Hermes.

Cascade: hermes -> mission-control -> safe_telegram_send.sh direct.
Each hop logged to JSONL run-log for audit. Eligibility-gate blocks
cascade beyond primary hop for low-priority issues.

Hardening (Round-2):
- Eligibility-gate exception is fail-CLOSED (default-deny on raise)
- Run-log I/O is best-effort (OSError caught, cascade unaffected)
- Sender-callable signature inspected — `issue` kwarg passed only when accepted
- Recursion guard via ContextVar: nested send() during cascade is blocked
- Response semantics: dict must carry `ok` key; missing `ok` = failure

Out-of-scope: per-hop timeout, run-log rotation, async dispatch.
Production wiring must enforce per-sender timeout (caller responsibility).
"""
from __future__ import annotations

import contextvars
import inspect
import json
import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

log = logging.getLogger(__name__)

_fallback_depth: contextvars.ContextVar[int] = contextvars.ContextVar(
    "fallback_router_depth", default=0,
)


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
        depth = _fallback_depth.get()
        if depth >= 1:
            log.warning("FallbackNotificationRouter.send blocked: recursion depth=%d", depth)
            return SendResult(ok=False, hop="", error="fallback recursion blocked")

        token = _fallback_depth.set(depth + 1)
        try:
            return self._do_send(message=message, issue=issue or {})
        finally:
            _fallback_depth.reset(token)

    def _do_send(self, *, message: str, issue: dict[str, Any]) -> SendResult:
        raw_id = issue.get("id")
        issue_id = "" if raw_id is None else str(raw_id)
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
            resp = self._call_sender(sender, message, issue)
        except Exception as exc:
            log.warning("hop %s failed: %s", hop, exc)
            return HopResult(hop=hop, ok=False, error=str(exc))

        if resp is None:
            return HopResult(hop=hop, ok=True)
        if isinstance(resp, dict):
            if "ok" not in resp:
                return HopResult(
                    hop=hop, ok=False, error="response missing 'ok' field",
                )
            return HopResult(hop=hop, ok=bool(resp.get("ok")))
        if hasattr(resp, "ok"):
            return HopResult(hop=hop, ok=bool(getattr(resp, "ok")))
        return HopResult(hop=hop, ok=True)

    @staticmethod
    def _call_sender(sender: Callable[..., Any],
                     message: str,
                     issue: dict[str, Any]) -> Any:
        """Inspect sender signature; pass `issue` only if accepted."""
        if _supports_issue_kwarg(sender):
            return sender(message=message, issue=issue)
        return sender(message=message)

    def _safe_gate(self, issue: dict[str, Any]) -> bool:
        """Invoke eligibility_gate; default-DENY (fail-closed) on exception."""
        try:
            return bool(self._eligible(issue))
        except Exception as exc:
            log.warning("eligibility_gate raised, default-DENY: %s", exc)
            return False

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
        try:
            self._run_log.parent.mkdir(parents=True, exist_ok=True)
            with self._run_log.open("a", encoding="utf-8") as f:
                f.write(json.dumps(entry) + "\n")
        except OSError as exc:
            log.warning("run-log write failed (path=%s): %s", self._run_log, exc)


def _supports_issue_kwarg(sender: Callable[..., Any]) -> bool:
    """True if sender callable accepts `issue` kwarg (named or via **kwargs)."""
    try:
        sig = inspect.signature(sender)
    except (ValueError, TypeError):
        return False
    params = sig.parameters
    if "issue" in params:
        return True
    return any(p.kind == p.VAR_KEYWORD for p in params.values())


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)
