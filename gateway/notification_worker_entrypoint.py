"""LaunchAgent entrypoint for notification_worker (P4 Track C).

Reads env-vars for config + builds OutboxStore + per-channel
router_factory + NotificationWorker + run_forever loop.

Invoked by ``scripts/notification-worker-runner.sh`` from LaunchAgent.
"""
from __future__ import annotations

import logging
import os
import sys
from pathlib import Path
from typing import Callable

from .fallback_channels import FallbackNotificationRouter
from .notification_worker import NotificationWorker
from .outbox import OutboxStore
from .router_factory import make_default_router

log = logging.getLogger(__name__)


# Per-channel target-chat-id resolution. Add entries as new channels onboard.
CHANNEL_DEFAULTS: dict[str, dict[str, str]] = {
    "telegram": {
        "target_chat_id_env": "TELEGRAM_FAMILY_CHAT_ID",
        "default_chat_id": "128314698",
        "context": "notification-worker",
    },
}


def build_channel_router_factory() -> Callable[[str], FallbackNotificationRouter]:
    """Return a callable mapping channel-name -> configured Router.

    Raises ValueError for unknown channels (caller — NotificationWorker —
    catches and routes to record_failure → eventual dead-letter).
    """
    def factory(channel: str) -> FallbackNotificationRouter:
        config = CHANNEL_DEFAULTS.get(channel)
        if config is None:
            raise ValueError(f"unknown channel: {channel}")
        chat_id = (
            os.environ.get(config["target_chat_id_env"], "").strip()
            or config["default_chat_id"]
        )
        return make_default_router(
            source_slug=channel,
            target_chat_id=chat_id,
            context=config["context"],
            hub_auth_mode="hmac",
            hub_hmac_secret_env=f"{channel.upper()}_HUB_HMAC_SECRET",
            topic=f"ops.notification.{channel}",
        )

    return factory


def main() -> int:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        stream=sys.stdout,
    )

    db_path = os.environ.get("HERMES_OUTBOX_DB") or str(Path.home() / ".hermes" / "outbox.db")
    poll_interval = float(os.environ.get("HERMES_NOTIFICATION_WORKER_POLL_INTERVAL", "10"))
    batch_size = int(os.environ.get("HERMES_NOTIFICATION_WORKER_BATCH_SIZE", "25"))
    zombie_timeout = int(os.environ.get("HERMES_NOTIFICATION_WORKER_ZOMBIE_TIMEOUT", "300"))

    Path(db_path).parent.mkdir(parents=True, exist_ok=True)
    outbox = OutboxStore(db_path=db_path)
    outbox.init_schema()

    worker = NotificationWorker(
        outbox=outbox,
        router_factory=build_channel_router_factory(),
        poll_interval=poll_interval,
        claim_batch_size=batch_size,
        zombie_timeout_seconds=zombie_timeout,
    )

    log.info("notification_worker_entrypoint: db=%s poll=%.1fs", db_path, poll_interval)
    worker.run_forever()
    return 0


if __name__ == "__main__":
    sys.exit(main())
