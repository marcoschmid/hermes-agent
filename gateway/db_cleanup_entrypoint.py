"""LaunchAgent entrypoint for db_cleanup (P4 followup).

Single-shot run (NOT a long-running poll-loop). LaunchAgent triggers daily
via StartCalendarInterval; this entrypoint runs cleanup once + exits.

Invoked by ``scripts/db-cleanup-runner.sh`` from LaunchAgent
``de.marcoschmid.hermes-db-cleanup``.
"""
from __future__ import annotations

import logging
import os
import sys

from .db_cleanup import run_all

log = logging.getLogger(__name__)


def main() -> int:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        stream=sys.stdout,
    )

    outbox_db = os.environ.get("HERMES_OUTBOX_DB", "").strip() or None
    callbacks_db = os.environ.get("TELEGRAM_CALLBACKS_DB", "").strip() or None
    dry_run = os.environ.get("HERMES_DB_CLEANUP_DRY_RUN", "").strip() == "1"

    log.info("db_cleanup_entrypoint starting (dry_run=%s outbox=%s callbacks=%s)",
             dry_run, outbox_db or "default", callbacks_db or "default")

    results = run_all(
        outbox_db=outbox_db,
        telegram_callbacks_db=callbacks_db,
        dry_run=dry_run,
    )

    for r in results:
        log.info("cleanup %s: deleted=%d protected=%d",
                 r.table, r.deleted, r.skipped_protected)

    return 0


if __name__ == "__main__":
    sys.exit(main())
