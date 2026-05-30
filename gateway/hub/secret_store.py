"""Phase-6b Step 6 — file-backed per-source hub_secret store.

In mirror mode (Step 5) the registry source-dict carries no hub_secret (the
Step-4a list endpoint omits all secrets). The HMAC auth path in
`gateway/hub/api.py` falls back to THIS store to obtain the per-source secret,
so the hub can verify request signatures without reading them from Mission
Control on every request — the last write-dependency that blocked revoking the
hermes-hub-service MC token.

File: ~/.hermes/registry_secrets.json  (override: HUB_REGISTRY_SECRETS_PATH)
Shape: {"<source_slug>": "<hub_secret>", ...}

Security posture:
  * The secret VALUE is never logged (only slugs / counts / paths).
  * File perms looser than 0o600 produce a WARNING (never a failure) so a
    mis-permissioned file degrades observably instead of silently breaking
    auth.
  * The parsed mapping is cached and invalidated by file mtime, so an operator
    can rotate a secret in place without a hub restart.
"""
from __future__ import annotations

import json
import logging
import os
import stat
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)

DEFAULT_SECRETS_PATH = "~/.hermes/registry_secrets.json"
_ENV_PATH = "HUB_REGISTRY_SECRETS_PATH"
_SECRET_FILE_MODE = 0o600


def _resolve_path(path: Optional[str]) -> Path:
    """Resolve the secrets-file path: explicit arg > env override > default."""
    raw = path or os.environ.get(_ENV_PATH) or DEFAULT_SECRETS_PATH
    return Path(raw).expanduser()


class SecretStore:
    """Reads per-source hub_secrets from a local JSON file with an mtime cache.

    Never logs secret values. A missing file, malformed JSON, or unknown slug
    all resolve to "no secret" (None) rather than an exception, so the caller
    can fall through to its existing no-secret 401 path unchanged.
    """

    def __init__(self, path: Optional[str] = None) -> None:
        self._path = _resolve_path(path)
        self._cache: dict[str, str] = {}
        self._cached_mtime: Optional[float] = None
        self._loaded = False

    def get_hub_secret(self, slug: str) -> Optional[str]:
        """Return the hub_secret for `slug`, or None if absent/unreadable."""
        self._load_if_stale()
        return self._cache.get(slug)

    def _load_if_stale(self) -> None:
        """(Re)load the file iff it changed since last read (mtime-based)."""
        try:
            mtime = self._path.stat().st_mtime
        except FileNotFoundError:
            if self._loaded and self._cache:
                logger.warning(
                    "registry secrets file disappeared: %s — keeping last cache",
                    self._path,
                )
                return
            if not self._loaded:
                logger.info(
                    "registry secrets file not present yet: %s", self._path
                )
            self._cache = {}
            self._loaded = True
            self._cached_mtime = None
            return
        except OSError as exc:
            logger.warning("cannot stat registry secrets file %s: %s", self._path, exc)
            return

        if self._loaded and self._cached_mtime == mtime:
            return

        self._cache = self._load_file()
        self._cached_mtime = mtime
        self._loaded = True

    def _load_file(self) -> dict[str, str]:
        """Parse + validate the JSON mapping. Returns {} on any read error.

        Never logs the secret values — only the path, the slug count, and the
        nature of any error.
        """
        self._warn_if_perms_loose()
        try:
            raw = self._path.read_text(encoding="utf-8")
        except OSError as exc:
            logger.warning("cannot read registry secrets file %s: %s", self._path, exc)
            return {}

        try:
            data = json.loads(raw)
        except json.JSONDecodeError as exc:
            logger.warning(
                "registry secrets file %s is not valid JSON: %s", self._path, exc
            )
            return {}

        if not isinstance(data, dict):
            logger.warning(
                "registry secrets file %s must be a JSON object {slug: secret}",
                self._path,
            )
            return {}

        mapping: dict[str, str] = {}
        for key, value in data.items():
            if isinstance(key, str) and isinstance(value, str):
                mapping[key] = value
            else:
                logger.warning(
                    "registry secrets file %s: ignoring non-string entry for a slug",
                    self._path,
                )
        logger.debug(
            "loaded %d hub_secret(s) from %s", len(mapping), self._path
        )
        return mapping

    def _warn_if_perms_loose(self) -> None:
        """Warn (never fail) if the file is group/world-accessible (> 0o600)."""
        try:
            mode = stat.S_IMODE(self._path.stat().st_mode)
        except OSError:
            return
        if mode & ~_SECRET_FILE_MODE:
            logger.warning(
                "registry secrets file %s has loose perms %o (expected %o); "
                "tighten with: chmod 600 %s",
                self._path,
                mode,
                _SECRET_FILE_MODE,
                self._path,
            )


# Module-level singleton — created lazily, bound to the env/default path at
# first use. Tests reset gateway.hub.secret_store._secret_store to rebind.
_secret_store: Optional[SecretStore] = None


def get_secret_store() -> SecretStore:
    """Lazily-instantiated shared secret store (env/default path)."""
    global _secret_store
    if _secret_store is None:
        _secret_store = SecretStore()
    return _secret_store
