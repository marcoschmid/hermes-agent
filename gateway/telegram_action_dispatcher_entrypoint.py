"""LaunchAgent entrypoint for telegram_action_dispatcher (P4 Track B).

Reads env-config + builds handler-map + runs dispatcher forever.

Invoked by ``scripts/telegram-action-dispatcher-runner.sh`` from LaunchAgent
``de.marcoschmid.hermes-telegram-action-dispatcher``.

Handler-wiring (production):
- approve → paperclip_issue_action(issue_id, "approved")
- reject  → paperclip_issue_action(issue_id, "rejected")
- skip    → log-only (no Paperclip-side action)

paperclip_issue_action is a stub that logs the intent. Marco wires actual
Paperclip-API-call when client-library is ready (out-of-scope for Track B
Round-2 — see plan §"Out-of-Scope / Action-Dispatcher").
"""
from __future__ import annotations

import logging
import os
import sys
from pathlib import Path
from typing import Any

from .telegram_action_dispatcher import TelegramActionDispatcher

log = logging.getLogger(__name__)


def paperclip_issue_action(issue_id: str, status: str, row: dict[str, Any]) -> None:
    """Stub Paperclip-Issue action handler.

    Marco-wires actual implementation when paperclip_issue_client supports
    Issue-status-updates (out-of-scope for Track B Round-2 PR). Until then,
    logs intent so dispatcher cycles complete + Marco can observe rows
    transitioning via processed_at column.

    To replace with real implementation: import paperclip_issue_client +
    update this function. Dispatcher picks up changes on next process-restart.
    """
    log.info("paperclip_issue_action stub: issue_id=%s status=%s callback_id=%s",
             issue_id, status, row.get("callback_id"))


def approve_handler(issue_id: str, row: dict[str, Any]) -> None:
    if not issue_id:
        raise ValueError("approve received empty issue_id")
    paperclip_issue_action(issue_id, "approved", row)


def reject_handler(issue_id: str, row: dict[str, Any]) -> None:
    if not issue_id:
        raise ValueError("reject received empty issue_id")
    paperclip_issue_action(issue_id, "rejected", row)


def skip_handler(issue_id: str, row: dict[str, Any]) -> None:
    log.info("skip callback for issue_id=%s — no-op", issue_id)


def build_handlers() -> dict:
    return {
        "approve": approve_handler,
        "reject": reject_handler,
        "skip": skip_handler,
    }


def main() -> int:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        stream=sys.stdout,
    )

    db_path = os.environ.get("TELEGRAM_CALLBACKS_DB", "").strip() or str(
        Path.home() / ".hermes" / "telegram_callbacks.db"
    )
    db_path = os.path.expanduser(db_path)
    poll_interval = float(os.environ.get("TELEGRAM_DISPATCHER_POLL_INTERVAL", "5"))
    batch_size = int(os.environ.get("TELEGRAM_DISPATCHER_BATCH_SIZE", "25"))

    Path(db_path).parent.mkdir(parents=True, exist_ok=True)

    dispatcher = TelegramActionDispatcher(
        db_path=db_path,
        handlers=build_handlers(),
        poll_interval=poll_interval,
        batch_size=batch_size,
    )

    log.info("telegram_action_dispatcher_entrypoint: db=%s poll=%.1fs",
             db_path, poll_interval)
    dispatcher.run_forever()
    return 0


if __name__ == "__main__":
    sys.exit(main())
