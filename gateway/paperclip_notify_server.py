"""Standalone uvicorn server for /paperclip/notify.

Runs as a tiny FastAPI app (default 127.0.0.1:8765) that paperclip's routine
checks POST to. Forwards alerts to Telegram by shelling out to
safe_telegram_send.sh — the same script openclaw cron scripts already use.
That keeps the notify server independent of the hermes gateway daemon and its
config-loading machinery (the gateway-resident `send_message_tool` only works
when the full platform stack has booted).

Launched by LaunchAgent (`de.marcoschmid.hermes-paperclip-notify.plist`) so it
stays alive independent of the hermes gateway and web UI.

Run:
    python -m gateway.paperclip_notify_server
        [--host 127.0.0.1] [--port 8765] [--target CHAT_ID]

Configuration (env, all optional):
    PAPERCLIP_NOTIFY_HOST     bind host         (default 127.0.0.1)
    PAPERCLIP_NOTIFY_PORT     bind port         (default 8765)
    PAPERCLIP_NOTIFY_TARGET   Telegram chat_id  (default EVM_TELEGRAM_CHAT_ID env or 128314698)
    PAPERCLIP_NOTIFY_TOKEN    bearer token      (else ~/.hermes/secrets/notify-token)
    PAPERCLIP_NOTIFY_DB       dedupe SQLite     (default ~/.hermes/cron/paperclip_notify_dedupe.db)
    PAPERCLIP_NOTIFY_SENDER   absolute path to  safe_telegram_send.sh
                              (default ~/.openclaw/workspace/scripts/safe_telegram_send.sh)
"""
import argparse
import logging
import os
import shlex
import subprocess
from pathlib import Path
from typing import Optional

from fastapi import FastAPI

from gateway.paperclip_notify import build_router

logger = logging.getLogger(__name__)

DEFAULT_SENDER_SCRIPT = str(
    Path.home() / ".openclaw/workspace/scripts/safe_telegram_send.sh"
)


def _resolve_target(explicit: Optional[str]) -> Optional[str]:
    if explicit:
        return explicit
    return os.environ.get("PAPERCLIP_NOTIFY_TARGET")


def _resolve_sender_script() -> str:
    return os.environ.get("PAPERCLIP_NOTIFY_SENDER", DEFAULT_SENDER_SCRIPT)


def _make_telegram_sender(target: Optional[str]):
    """Return callable(str)->None that delivers the message to a Telegram chat.

    Routes through `safe_telegram_send.sh` so we inherit Marco's existing
    bot-token/auth setup without booting the hermes gateway. If the script is
    missing or the chat target is unset we degrade to a log-only sink: the
    webhook still acks 200 so paperclip's routine-checks don't see false
    failures.
    """
    sender_script = _resolve_sender_script()

    if not target or not Path(sender_script).is_file():
        if not target:
            logger.warning(
                "PAPERCLIP_NOTIFY_TARGET unset — alerts will only be logged"
            )
        else:
            logger.warning(
                "telegram sender script missing at %s — alerts will only be logged",
                sender_script,
            )

        def _sink(message: str) -> None:
            logger.info("[paperclip-notify (no sender)] %s", message)

        return _sink

    def _send(message: str) -> None:
        cmd = [
            "bash",
            sender_script,
            "--target",
            str(target),
            "--context",
            "paperclip-notify",
            "--message",
            message,
        ]
        try:
            result = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                timeout=20,
                check=False,
            )
        except subprocess.TimeoutExpired:
            logger.error("paperclip notify telegram send timed out: %s", shlex.join(cmd))
            return
        if result.returncode != 0:
            logger.error(
                "paperclip notify telegram send rc=%d stderr=%s",
                result.returncode,
                (result.stderr or "").strip()[:400],
            )

    return _send


def build_app(target: Optional[str] = None) -> FastAPI:
    app = FastAPI(title="paperclip-notify", version="1")
    app.include_router(build_router(telegram_send=_make_telegram_sender(target)))

    @app.get("/health")
    async def _health():
        return {"ok": True}

    return app


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    parser = argparse.ArgumentParser(description="paperclip-notify webhook server")
    parser.add_argument("--host", default=os.environ.get("PAPERCLIP_NOTIFY_HOST", "127.0.0.1"))
    parser.add_argument("--port", type=int, default=int(os.environ.get("PAPERCLIP_NOTIFY_PORT", "8765")))
    parser.add_argument("--target", default=None, help="Telegram target like 'telegram:-1234:567'")
    args = parser.parse_args()

    target = _resolve_target(args.target)
    app = build_app(target=target)

    import uvicorn

    logger.info("paperclip-notify on http://%s:%d  target=%s", args.host, args.port, target or "<none>")
    uvicorn.run(app, host=args.host, port=args.port, log_level="info")


if __name__ == "__main__":
    main()
