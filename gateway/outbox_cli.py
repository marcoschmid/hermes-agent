"""CLI entrypoint for outbox-enqueue (P4 Track A2 caller-bridge).

Bash callers (cron-jobs, ops-scripts) invoke this via the
``scripts/outbox-enqueue`` shim instead of ``safe_telegram_send.sh``:

    outbox-enqueue \\
      --channel telegram \\
      --target 128314698 \\
      --context cert_expiry \\
      --dedupe-key "cert-expiry:$(date +%Y-%m-%d)" \\
      --message "ACME cert expires in 14 days"

Writes a row to ``OutboxStore`` and returns immediately. The
``notification_worker`` LaunchAgent picks the row up and dispatches via
channel-specific Router cascade (hermes -> mc -> direct).

Semantic change vs ``safe_telegram_send.sh``:
- exit 0 means **enqueued**, NOT delivered. Worker handles delivery async.
- Use ``--dry-run`` to validate args without writing to DB.

See ``projects/jarvis-os-redesign/plans/2026-05-25-p4-track-a2-bulk-migration.md``.
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import sys
from pathlib import Path
from typing import Any

from .outbox import OutboxStore

log = logging.getLogger(__name__)


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="outbox-enqueue",
        description="Enqueue notification row for async dispatch by notification_worker.",
    )
    p.add_argument("--channel", required=True,
                   help="Channel slug (e.g. 'telegram', 'pushover').")
    # Round-2 CRITICAL-1: accept BOTH --dedup-key (new) AND --dedupe-key (legacy
    # safe_telegram_send.sh syntax) so callers migrate mechanically.
    p.add_argument("--dedup-key", "--dedupe-key", dest="dedup_key", required=True,
                   help="Dedup-key (idempotency). --dedupe-key is a legacy alias.")
    p.add_argument("--message",
                   help="Message body (UTF-8). OR use --message-stdin / --payload-json. "
                        "For untrusted text starting with '-' use --message='...' "
                        "(equals-form) or --message-stdin.")
    p.add_argument("--message-stdin", action="store_true",
                   help="Read message from stdin (recommended for large or untrusted text).")
    p.add_argument("--target",
                   help="Channel-specific target (e.g. Telegram chat-id). "
                        "Overrides per-channel default at dispatch time.")
    p.add_argument("--context",
                   help="Logging label / dedup-scope. Overrides per-channel default.")
    p.add_argument("--title",
                   help="Optional title for rich-rendered notifications (Hub-Adapter).")
    p.add_argument("--audience", default="marco",
                   help="Audience field for Hub-Pipeline rule-matcher (default: marco).")
    p.add_argument("--payload-json",
                   help="Override entire payload as JSON dict. Mutually exclusive with --message.")
    p.add_argument("--db-path",
                   help="Outbox DB path. Default: $HERMES_OUTBOX_DB or ~/.hermes/outbox.db.")
    p.add_argument("--dry-run", action="store_true",
                   help="Validate args + construct payload, do NOT write to DB. Prints JSON to stdout.")
    p.add_argument("--quiet", action="store_true",
                   help="Suppress success-JSON on stdout (errors still go to stderr).")
    # Round-2 CRITICAL-1: accept-and-ignore legacy safe_telegram_send.sh flags so
    # mechanical migration of bash callers succeeds without rewriting their args.
    # These options no longer apply in async-enqueue world (Worker + Outbox handle
    # dedupe-window via dedup_key + Hub flapping-suppression; rate-limit via Hub
    # per-source config; buttons via channel-specific renderer).
    p.add_argument("--dedupe-window", help=argparse.SUPPRESS)
    p.add_argument("--rate-limit-window", help=argparse.SUPPRESS)
    p.add_argument("--buttons", help=argparse.SUPPRESS)
    p.add_argument("--media", help=argparse.SUPPRESS)
    return p


def _resolve_message(args: argparse.Namespace) -> str | None:
    if args.payload_json:
        return None
    if args.message_stdin:
        return sys.stdin.read()
    return args.message


def _build_payload(args: argparse.Namespace) -> dict[str, Any]:
    if args.payload_json:
        parsed = json.loads(args.payload_json)
        if not isinstance(parsed, dict):
            raise ValueError("--payload-json must decode to a JSON object")
        return parsed

    message = _resolve_message(args)
    if message is None or message == "":
        raise ValueError("--message, --message-stdin, or --payload-json required")

    payload: dict[str, Any] = {"message": message, "audience": args.audience}
    if args.title:
        payload["title"] = args.title
    if args.target:
        payload["target"] = args.target
    if args.context:
        payload["context"] = args.context
    return payload


def _resolve_db_path(args: argparse.Namespace) -> str:
    if args.db_path:
        return os.path.expanduser(args.db_path)
    env_path = os.environ.get("HERMES_OUTBOX_DB")
    if env_path:
        return os.path.expanduser(env_path)
    return str(Path.home() / ".hermes" / "outbox.db")


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)

    if args.message and args.message_stdin:
        print("ERROR: --message and --message-stdin are mutually exclusive", file=sys.stderr)
        return 2
    if args.payload_json and (args.message or args.message_stdin):
        print("ERROR: --payload-json cannot be combined with --message/--message-stdin",
              file=sys.stderr)
        return 2

    try:
        payload = _build_payload(args)
    except json.JSONDecodeError as exc:
        print(f"ERROR: --payload-json invalid JSON: {exc}", file=sys.stderr)
        return 2
    except ValueError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2

    if args.dry_run:
        out = {"channel": args.channel, "dedup_key": args.dedup_key, "payload": payload}
        print(json.dumps(out))
        return 0

    db_path = _resolve_db_path(args)
    db_dir = Path(db_path).parent
    try:
        db_dir.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        print(f"ERROR: cannot create db dir {db_dir}: {exc}", file=sys.stderr)
        return 1

    try:
        store = OutboxStore(db_path=db_path)
        store.init_schema()
        # Round-2 MEDIUM-4: detect dedupe-collision so callers see it.
        existing_before = store.get_by_id_via_dedup_key(args.dedup_key) \
            if hasattr(store, "get_by_id_via_dedup_key") else None
        row = store.enqueue(
            channel=args.channel,
            dedup_key=args.dedup_key,
            payload=payload,
        )
    except Exception as exc:
        print(f"ERROR: outbox enqueue failed: {exc}", file=sys.stderr)
        return 1

    # Round-2 MEDIUM-4: deduped=true means dedup_key existed; this enqueue was a no-op
    # (caller alert dropped — convention: prefix dedup_key with caller-name).
    deduped = existing_before is not None
    if deduped and not args.quiet:
        print(f"WARN: dedup_key={args.dedup_key!r} already enqueued at "
              f"{existing_before.created_at} (channel={existing_before.channel}); "
              f"this call is a no-op", file=sys.stderr)
    if not args.quiet:
        print(json.dumps({
            "ok": True,
            "row_id": row.id,
            "status": row.status,
            "deduped": deduped,
        }))
    return 0


if __name__ == "__main__":
    sys.exit(main())
