#!/usr/bin/env python3
"""Hermes health observer — Jarvis-OS Phase-4 4c-2.

Periodically polls Hermes-Health (gateway/hermes_health_probe.py) and decides
whether to trigger Paperclip mc-dispatch-fallback. Designed for LaunchAgent
operation at 30s StartInterval.

Trigger-Logic per plan § 4c-1:
  - state-history-buffer in $HERMES_HOME/state/hermes-health-observer.json
  - down-trigger:    4 consecutive non-healthy (down|degraded) snapshots
  - recovery-clear:  2 consecutive healthy snapshots

The observer does NOT iterate eligible issues itself — it queries Paperclip
for issues with executor=hermes assignment that have no active lock, then
POSTs fallback for each up to a per-tick limit.

Env vars:
  PAPERCLIP_API_URL          default http://127.0.0.1:3347
  PAPERCLIP_API_TOKEN        required (instance-admin scope)
  HERMES_HOME                default ~/.hermes
  OBSERVER_FAIL_THRESHOLD    default 4
  OBSERVER_RECOVERY_THRESHOLD default 2
  OBSERVER_FALLBACK_LIMIT    default 5
  OBSERVER_DRY_RUN           default true (false flips to real spawn)
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from gateway.hermes_health_probe import HermesHealthSnapshot, probe_hermes_health
from gateway.paperclip_issue_client import (
    IssueRunsClientError,
    PaperclipIssueRunsClient,
)

logger = logging.getLogger("hermes-health-observer")
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

DEFAULT_HERMES_HOME = Path.home() / ".hermes"
STATE_FILENAME = "hermes-health-observer.json"
MAX_HISTORY = 10
DEFAULT_FAIL_THRESHOLD = 4
DEFAULT_RECOVERY_THRESHOLD = 2


def _hermes_home() -> Path:
    raw = os.environ.get("HERMES_HOME")
    if raw:
        return Path(raw).expanduser()
    return DEFAULT_HERMES_HOME


def _state_path() -> Path:
    state_dir = _hermes_home() / "state"
    state_dir.mkdir(parents=True, exist_ok=True)
    return state_dir / STATE_FILENAME


def _load_history() -> list[str]:
    path = _state_path()
    if not path.exists():
        return []
    try:
        data = json.loads(path.read_text())
        history = data.get("history") if isinstance(data, dict) else None
        return [str(s) for s in history][-MAX_HISTORY:] if isinstance(history, list) else []
    except (ValueError, OSError) as exc:
        logger.warning("state file unreadable, resetting: %s", exc)
        return []


def _save_history(history: list[str], snapshot: HermesHealthSnapshot, decision: str) -> None:
    payload = {
        "history": history[-MAX_HISTORY:],
        "last_snapshot": snapshot.to_dict(),
        "last_decision": decision,
        "updated_at": datetime.now(timezone.utc).isoformat(),
    }
    _state_path().write_text(json.dumps(payload, indent=2))


def decide(history: list[str], fail_threshold: int, recovery_threshold: int) -> str:
    """Decide based on the last N states whether to fire down/recover/idle."""
    if len(history) >= fail_threshold:
        tail = history[-fail_threshold:]
        if all(s != "healthy" for s in tail):
            return "fire-fallback"
    if len(history) >= recovery_threshold:
        tail = history[-recovery_threshold:]
        if all(s == "healthy" for s in tail):
            return "recover"
    return "idle"


def main() -> int:
    parser = argparse.ArgumentParser(description="Hermes health observer")
    parser.add_argument(
        "--fail-threshold",
        type=int,
        default=int(os.environ.get("OBSERVER_FAIL_THRESHOLD", str(DEFAULT_FAIL_THRESHOLD))),
    )
    parser.add_argument(
        "--recovery-threshold",
        type=int,
        default=int(os.environ.get("OBSERVER_RECOVERY_THRESHOLD", str(DEFAULT_RECOVERY_THRESHOLD))),
    )
    parser.add_argument(
        "--fallback-limit",
        type=int,
        default=int(os.environ.get("OBSERVER_FALLBACK_LIMIT", "5")),
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        default=os.environ.get("OBSERVER_DRY_RUN", "true").lower() in {"1", "true", "yes"},
    )
    parser.add_argument("--probe-only", action="store_true", help="probe + history but no fallback POSTs")
    args = parser.parse_args()

    snapshot = probe_hermes_health()
    history = _load_history()
    history.append(snapshot.state)
    history = history[-MAX_HISTORY:]
    decision = decide(history, args.fail_threshold, args.recovery_threshold)

    logger.info(
        "tick state=%s decision=%s reason=%s history=%s",
        snapshot.state,
        decision,
        snapshot.reason,
        history,
    )

    _save_history(history, snapshot, decision)

    if args.probe_only or decision != "fire-fallback":
        return 0

    fallback_summary = _fire_fallback(
        snapshot=snapshot,
        limit=args.fallback_limit,
        dry_run=args.dry_run,
    )
    logger.info("fallback summary: %s", fallback_summary)
    return 0


def _fire_fallback(
    *,
    snapshot: HermesHealthSnapshot,
    limit: int,
    dry_run: bool,
) -> dict[str, Any]:
    """Enumerate eligible issues via Paperclip and POST fallback for each."""
    if not os.environ.get("PAPERCLIP_API_TOKEN"):
        return {"fired": 0, "skipped": "no PAPERCLIP_API_TOKEN env var"}
    company_id = os.environ.get("OBSERVER_COMPANY_ID")
    if not company_id:
        return {"fired": 0, "skipped": "no OBSERVER_COMPANY_ID env var"}

    try:
        client = PaperclipIssueRunsClient()
    except IssueRunsClientError as exc:
        return {"fired": 0, "error": str(exc)}

    try:
        eligible = client.list_eligible_fallback_issues(company_id=company_id, limit=limit)
    except IssueRunsClientError as exc:
        client.close()
        return {"fired": 0, "error": f"list eligible failed: {exc}"}

    reason = f"hermes_{snapshot.state}_{int(snapshot.heartbeat_age_seconds or 0)}s"
    outcomes: list[dict[str, Any]] = []
    fired = 0
    for issue in eligible[:limit]:
        issue_id = issue.get("issueId")
        if not issue_id:
            continue
        try:
            result = client.fire_fallback(
                company_id=company_id,
                issue_id=issue_id,
                reason=reason,
                hermes_health_snapshot=snapshot.to_dict(),
                dry_run=dry_run,
            )
            outcomes.append({"issueId": issue_id, "outcome": result.get("outcome")})
            if result.get("accepted"):
                fired += 1
        except IssueRunsClientError as exc:
            outcomes.append({"issueId": issue_id, "error": str(exc)})

    client.close()
    return {
        "fired": fired,
        "evaluated": len(eligible),
        "limit": limit,
        "dry_run": dry_run,
        "snapshot": snapshot.to_dict(),
        "outcomes": outcomes,
    }


if __name__ == "__main__":
    sys.exit(main())
