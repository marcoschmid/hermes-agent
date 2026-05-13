#!/usr/bin/env python3
"""Hermes issue-lock watchdog — Jarvis-OS Phase-4 (4a-3 wave 3).

Periodically calls Paperclip's POST /api/internal/issue-runs/recover-stale to
clean up locks abandoned by crashed Hermes workers. Designed to run as a
LaunchAgent every WATCHDOG_INTERVAL_SECONDS (default 300, prod).

Per Marco-Decision 4D-6=C: Telegram alert only on repeated recoveries within
an hour, not on every single one. This script writes a small ring-buffer
state file at $HERMES_HOME/state/issue-lock-watchdog.json to track recovery
counts. If ≥ 2 recoveries land within 1h, an alert is emitted via the
safe_telegram_send.sh wrapper.

Env vars:
  PAPERCLIP_API_URL        Default http://127.0.0.1:3347
  PAPERCLIP_API_TOKEN      Instance-admin token (required, since recover-stale needs admin scope)
  WATCHDOG_LIMIT           Default 100, max candidates per run
  WATCHDOG_DRY_RUN         If "1" or "true", report only without mutating
  HERMES_HOME              Defaults to ~/.hermes; state file lives in $HERMES_HOME/state/
  TELEGRAM_CHAT_ID         Optional, target for repeat-recovery alerts
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import subprocess
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from gateway.paperclip_issue_client import (
    IssueRunsClientError,
    PaperclipIssueRunsClient,
)

logger = logging.getLogger("hermes-issue-lock-watchdog")
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

DEFAULT_HERMES_HOME = Path.home() / ".hermes"
STATE_FILENAME = "issue-lock-watchdog.json"
ALERT_WINDOW = timedelta(hours=1)
ALERT_THRESHOLD = 2
SAFE_TELEGRAM_SEND = Path.home() / ".openclaw" / "workspace" / "scripts" / "safe_telegram_send.sh"


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
        return list(data.get("recoveries", []))
    except (ValueError, OSError) as exc:
        logger.warning("state file unreadable, resetting: %s", exc)
        return []


def _save_history(history: list[str]) -> None:
    payload = {"recoveries": history, "updated_at": datetime.now(timezone.utc).isoformat()}
    _state_path().write_text(json.dumps(payload, indent=2))


def _prune_history(history: list[str], cutoff: datetime) -> list[str]:
    kept = []
    for iso in history:
        try:
            ts = datetime.fromisoformat(iso)
        except ValueError:
            continue
        if ts >= cutoff:
            kept.append(iso)
    return kept


def _send_alert(message: str, chat_id: str) -> None:
    if not SAFE_TELEGRAM_SEND.exists():
        logger.warning("safe_telegram_send.sh not at %s; skipping alert", SAFE_TELEGRAM_SEND)
        return
    try:
        subprocess.run(
            [
                str(SAFE_TELEGRAM_SEND),
                "--target",
                chat_id,
                "--context",
                "hermes_issue_lock_watchdog",
                "--dedupe-key",
                f"issue-lock-watchdog:{datetime.now(timezone.utc).strftime('%Y-%m-%d-%H')}",
                "--dedupe-window",
                "3600",
                "--message",
                message,
            ],
            check=False,
            timeout=10,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        logger.warning("telegram alert failed: %s", exc)


def main() -> int:
    parser = argparse.ArgumentParser(description="Hermes issue-lock recovery watchdog")
    parser.add_argument(
        "--limit",
        type=int,
        default=int(os.environ.get("WATCHDOG_LIMIT", "100")),
        help="max candidates per run",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        default=os.environ.get("WATCHDOG_DRY_RUN", "").lower() in {"1", "true", "yes"},
        help="report only, no mutation",
    )
    args = parser.parse_args()

    started_at = time.time()
    try:
        client = PaperclipIssueRunsClient()
    except IssueRunsClientError as exc:
        logger.error("client init failed: %s", exc)
        return 2

    try:
        result = client.recover_stale(
            trigger="watchdog",
            limit=args.limit,
            dry_run=args.dry_run,
        )
    except IssueRunsClientError as exc:
        logger.error("recover_stale call failed: %s", exc)
        return 3
    finally:
        client.close()

    recovered = result.get("recovered", [])
    candidates = result.get("candidates", [])
    elapsed = time.time() - started_at

    logger.info(
        "watchdog tick: candidates=%d recovered=%d dry_run=%s elapsed=%.2fs",
        len(candidates),
        len(recovered),
        result.get("dryRun", args.dry_run),
        elapsed,
    )

    if args.dry_run or not recovered:
        return 0

    history = _load_history()
    history.extend([entry.get("recoveredAt", datetime.now(timezone.utc).isoformat()) for entry in recovered])
    cutoff = datetime.now(timezone.utc) - ALERT_WINDOW
    history = _prune_history(history, cutoff)
    _save_history(history)

    if len(history) >= ALERT_THRESHOLD:
        chat_id = os.environ.get("TELEGRAM_CHAT_ID")
        if chat_id:
            _send_alert(
                f"Hermes lock-watchdog: {len(recovered)} stale lock(s) recovered now, "
                f"{len(history)} in last hour. Review issue_runs.",
                chat_id,
            )
        else:
            logger.warning("repeat-recovery threshold reached but TELEGRAM_CHAT_ID unset")

    return 0


if __name__ == "__main__":
    sys.exit(main())
