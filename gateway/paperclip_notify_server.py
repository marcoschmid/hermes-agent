"""Standalone uvicorn server for /paperclip/notify."""
import argparse
import logging
import os
import shlex
import subprocess
from pathlib import Path
from typing import Optional

from fastapi import FastAPI

from gateway.paperclip_notify import NotifyDeliveryError, build_router

logger = logging.getLogger(__name__)

DEFAULT_SENDER_SCRIPT = str(
    Path.home() / ".openclaw/workspace/scripts/safe_telegram_send.sh"
)
DEFAULT_TELEGRAM_CHAT_ID = "128314698"


def _resolve_target(explicit: Optional[str]) -> Optional[str]:
    if explicit:
        return explicit
    target = os.environ.get("PAPERCLIP_NOTIFY_TARGET") or os.environ.get(
        "EVM_TELEGRAM_CHAT_ID"
    )
    if target:
        return target
    return DEFAULT_TELEGRAM_CHAT_ID


def _resolve_sender_script() -> str:
    return os.environ.get("PAPERCLIP_NOTIFY_SENDER", DEFAULT_SENDER_SCRIPT)


def _make_telegram_sender(target: Optional[str]):
    sender_script = _resolve_sender_script()

    if not target or not Path(sender_script).is_file():
        if not target:
            logger.warning("PAPERCLIP_NOTIFY_TARGET unset - alerts will only be logged")
        else:
            logger.warning(
                "telegram sender script missing at %s - alerts will only be logged",
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
            raise NotifyDeliveryError("telegram send timed out")
        if result.returncode != 0:
            logger.error(
                "paperclip notify telegram send rc=%d stderr=%s",
                result.returncode,
                (result.stderr or "").strip()[:400],
            )
            raise NotifyDeliveryError(f"telegram send failed rc={result.returncode}")

    return _send


def build_app(target: Optional[str] = None) -> FastAPI:
    app = FastAPI(title="paperclip-notify", version="1")
    app.include_router(build_router(telegram_send=_make_telegram_sender(target)))

    @app.get("/health")
    async def _health():
        return {"ok": True}

    return app


def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    parser = argparse.ArgumentParser(description="paperclip-notify webhook server")
    parser.add_argument(
        "--host", default=os.environ.get("PAPERCLIP_NOTIFY_HOST", "127.0.0.1")
    )
    parser.add_argument(
        "--port",
        type=int,
        default=int(os.environ.get("PAPERCLIP_NOTIFY_PORT", "8765")),
    )
    parser.add_argument("--target", default=None, help="Telegram chat id")
    args = parser.parse_args()

    target = _resolve_target(args.target)
    app = build_app(target=target)

    import uvicorn

    logger.info(
        "paperclip-notify on http://%s:%d target=%s",
        args.host,
        args.port,
        target or "<none>",
    )
    uvicorn.run(app, host=args.host, port=args.port, log_level="info")


if __name__ == "__main__":
    main()
