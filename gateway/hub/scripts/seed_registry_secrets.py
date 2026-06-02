#!/usr/bin/env python3
"""Phase-6b Step 6 — one-shot seeder for the local hub_secret store.

MANUAL, NO AUTO-RUN. This script is run once (and re-run only on rotation) by
an operator to materialise ~/.hermes/registry_secrets.json from Mission
Control, so the hub's HMAC auth path can read per-source hub_secrets from disk
in mirror mode instead of from MC on every request.

It reads each source's hub_secret via the Bearer-authenticated MC endpoint
(`get_source(slug)` — hub_secret is only returned to Bearer callers) and writes
the {slug: secret} JSON file with 0o600 perms. It is:
  * idempotent  — re-running merges/overwrites the listed slugs, preserving any
                  other slugs already in the file;
  * silent      — NEVER logs a secret value (only slugs / counts / the path).

Requires MC_HUB_TOKEN (+ optional MC_BASE_URL) in the environment, exactly like
the live RegistryClient.

Usage:
    .venv/bin/python -m gateway.hub.scripts.seed_registry_secrets
    .venv/bin/python -m gateway.hub.scripts.seed_registry_secrets weekly-preview telegram
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import sys
from pathlib import Path

from gateway.hub.registry_client import RegistryClient
from gateway.hub.secret_store import DEFAULT_SECRETS_PATH, _ENV_PATH

logger = logging.getLogger("seed_registry_secrets")

# The 6 sources that carry a hub_secret (Phase-6b secret inventory).
DEFAULT_SLUGS = [
    "weekly-preview",
    "decision-board-hermes",
    "decision-board-paperclip",
    "decision-board-codex",
    "drobo-backup",
    "telegram",
]

_FILE_MODE = 0o600


def _target_path() -> Path:
    raw = os.environ.get(_ENV_PATH) or DEFAULT_SECRETS_PATH
    return Path(raw).expanduser()


def _load_existing(path: Path) -> dict[str, str]:
    """Read the current file so re-seeds merge instead of clobbering."""
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        logger.warning("could not read existing %s (%s); starting fresh", path, exc)
        return {}
    return {k: v for k, v in data.items() if isinstance(k, str) and isinstance(v, str)}


def _write_secure(path: Path, mapping: dict[str, str]) -> None:
    """Write the JSON file atomically-ish with 0o600 perms (never logs values).

    The temp file is created via ``os.open(..., O_CREAT|O_EXCL, 0o600)`` so it
    exists at 0o600 from the first byte — closing the umask window where a
    ``write_text``-then-``chmod`` sequence would briefly expose secrets at the
    default (umask-derived) mode.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    # O_EXCL refuses a pre-existing tmp; clear a stale one from a crashed run.
    try:
        tmp.unlink()
    except FileNotFoundError:
        pass
    payload = json.dumps(mapping, indent=2, sort_keys=True)
    fd = os.open(str(tmp), os.O_WRONLY | os.O_CREAT | os.O_EXCL, _FILE_MODE)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(payload)
    except BaseException:
        # Don't leave a half-written secret tmp behind on failure.
        tmp.unlink(missing_ok=True)
        raise
    # Belt-and-suspenders: a non-zero umask cannot widen O_CREAT mode, but an
    # inherited stricter mode is fine; force the canonical 0o600 either way.
    os.chmod(tmp, _FILE_MODE)
    os.replace(tmp, path)
    os.chmod(path, _FILE_MODE)


async def seed(slugs: list[str]) -> int:
    """Fetch each slug's hub_secret from MC and merge into the local store.

    Returns the number of secrets successfully written. Never logs a value.
    """
    if not os.environ.get("MC_HUB_TOKEN"):
        logger.error("MC_HUB_TOKEN not set — cannot read hub_secrets from MC")
        return 0

    path = _target_path()
    mapping = _load_existing(path)

    registry = RegistryClient()
    written = 0
    missing: list[str] = []
    try:
        for slug in slugs:
            source = await registry.get_source(slug)
            secret = (source or {}).get("hub_secret")
            if secret:
                mapping[slug] = secret
                written += 1
            else:
                missing.append(slug)
    finally:
        await registry.close()

    _write_secure(path, mapping)

    logger.info("wrote %d hub_secret(s) to %s (%d total in file)",
                written, path, len(mapping))
    if missing:
        logger.warning("no hub_secret returned for: %s", ", ".join(missing))
    return written


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    args = list(argv if argv is not None else sys.argv[1:])
    slugs = args or DEFAULT_SLUGS
    written = asyncio.run(seed(slugs))
    return 0 if written else 1


if __name__ == "__main__":
    raise SystemExit(main())
