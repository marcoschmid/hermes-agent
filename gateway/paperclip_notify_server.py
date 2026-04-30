"""Standalone uvicorn server for /paperclip/notify.

Runs as a tiny FastAPI app (default 127.0.0.1:8765) that paperclip's routine
checks POST to. Forwards alerts to Telegram via the existing send_message_tool.
Launched by LaunchAgent (`de.marcoschmid.hermes-paperclip-notify.plist`) so it
stays alive independent of the hermes gateway and web UI.

Run:
    python -m gateway.paperclip_notify_server
        [--host 127.0.0.1] [--port 8765] [--target telegram:CHAT_ID]

Configuration (env, all optional):
    PAPERCLIP_NOTIFY_HOST    bind host        (default 127.0.0.1)
    PAPERCLIP_NOTIFY_PORT    bind port        (default 8765)
    PAPERCLIP_NOTIFY_TARGET  Telegram target  (default telegram:CHAT_ID via cron config)
    PAPERCLIP_NOTIFY_TOKEN   bearer token     (else ~/.hermes/secrets/notify-token)
    PAPERCLIP_NOTIFY_DB      dedupe SQLite    (default ~/.hermes/cron/paperclip_notify_dedupe.db)
"""
import argparse
import json
import logging
import os
from typing import Optional

from fastapi import FastAPI

from gateway.paperclip_notify import build_router

logger = logging.getLogger(__name__)


def _resolve_target(explicit: Optional[str]) -> Optional[str]:
    if explicit:
        return explicit
    env = os.environ.get("PAPERCLIP_NOTIFY_TARGET")
    if env:
        return env
    try:
        from hermes_cli.config import load_config

        cfg = load_config() or {}
        target = cfg.get("cron", {}).get("auto_delivery", {}).get("target")
        if target:
            return target
    except Exception as e:
        logger.warning("could not load cron auto_delivery target from config: %s", e)
    return None


def _make_telegram_sender(target: Optional[str]):
    """Return callable(str)->None that delivers the message to the configured target.

    Falls back to a logger-only sink when no target is configured so the webhook
    still acks 200 instead of 500ing on every alert.
    """
    if not target:
        logger.warning(
            "no PAPERCLIP_NOTIFY_TARGET / cron.auto_delivery.target — alerts will only be logged"
        )

        def _sink(message: str) -> None:
            logger.info("[paperclip-notify (no target)] %s", message)

        return _sink

    from tools.send_message_tool import send_message_tool

    def _send(message: str) -> None:
        result = send_message_tool({"action": "send", "target": target, "message": message})
        try:
            parsed = json.loads(result) if isinstance(result, str) else result
        except (TypeError, ValueError):
            parsed = {"raw": result}
        if isinstance(parsed, dict) and parsed.get("error"):
            logger.error("paperclip notify Telegram send failed: %s", parsed["error"])

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
