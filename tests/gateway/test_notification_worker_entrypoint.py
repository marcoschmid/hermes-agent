"""Unit tests for gateway.notification_worker_entrypoint helpers."""
from __future__ import annotations

import pytest

from gateway.notification_worker_entrypoint import _resolve_hmac_secret_env


def test_resolve_hmac_secret_env_prefers_canonical_when_set(monkeypatch):
    """Preferred name `{CHANNEL}_HUB_HMAC_SECRET` wins when set."""
    monkeypatch.setenv("TELEGRAM_HUB_HMAC_SECRET", "preferred-value")
    monkeypatch.delenv("TELEGRAM_HUB_SECRET", raising=False)
    assert _resolve_hmac_secret_env("telegram") == "TELEGRAM_HUB_HMAC_SECRET"


def test_resolve_hmac_secret_env_falls_back_to_legacy(monkeypatch):
    """Legacy `{CHANNEL}_HUB_SECRET` returned when only it is set
    (drobo-backup pre-existing convention)."""
    monkeypatch.delenv("DROBO_BACKUP_HUB_HMAC_SECRET", raising=False)
    monkeypatch.setenv("DROBO_BACKUP_HUB_SECRET", "legacy-value")
    assert _resolve_hmac_secret_env("drobo-backup") == "DROBO_BACKUP_HUB_SECRET"


def test_resolve_hmac_secret_env_prefers_canonical_when_both_set(monkeypatch):
    """Both vars set → canonical wins (encourages migration to one name)."""
    monkeypatch.setenv("WEEKLY_PREVIEW_HUB_HMAC_SECRET", "new")
    monkeypatch.setenv("WEEKLY_PREVIEW_HUB_SECRET", "old")
    assert _resolve_hmac_secret_env("weekly-preview") == "WEEKLY_PREVIEW_HUB_HMAC_SECRET"


def test_resolve_hmac_secret_env_returns_canonical_when_neither_set(monkeypatch):
    """Neither set → return canonical name. router_factory will fail-fast with
    error mentioning the convention we want callers to follow."""
    monkeypatch.delenv("NEWCHAN_HUB_HMAC_SECRET", raising=False)
    monkeypatch.delenv("NEWCHAN_HUB_SECRET", raising=False)
    assert _resolve_hmac_secret_env("newchan") == "NEWCHAN_HUB_HMAC_SECRET"


def test_resolve_hmac_secret_env_normalises_dashes_to_underscores(monkeypatch):
    """Channel names with dashes ('drobo-backup', 'cert-expiry') map to
    underscore env-var names per POSIX convention. Fixes pre-existing bug
    where 'channel.upper()' alone left dashes intact, making env lookup miss
    the real var."""
    monkeypatch.setenv("CERT_EXPIRY_HUB_HMAC_SECRET", "x")
    assert _resolve_hmac_secret_env("cert-expiry") == "CERT_EXPIRY_HUB_HMAC_SECRET"
