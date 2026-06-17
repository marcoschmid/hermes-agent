"""TDD: notification_worker entrypoint registers a standalone 'pushover' channel.

factory('pushover') must return a PushoverRouter (bypassing the telegram
Hub->MC->direct cascade); unknown channels still raise ValueError so the
worker dead-letters them.
"""
from __future__ import annotations

import pytest

from gateway.notification_worker_entrypoint import build_channel_router_factory
from gateway.pushover_router import PushoverRouter


def test_factory_pushover_returns_pushover_router(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PUSHOVER_API_TOKEN", "tok")
    monkeypatch.setenv("PUSHOVER_USER_KEY", "usr")
    factory = build_channel_router_factory()
    router = factory("pushover")
    assert isinstance(router, PushoverRouter)


def test_factory_pushover_missing_creds_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("PUSHOVER_API_TOKEN", raising=False)
    monkeypatch.delenv("PUSHOVER_USER_KEY", raising=False)
    factory = build_channel_router_factory()
    with pytest.raises(ValueError):
        factory("pushover")


def test_factory_unknown_channel_still_raises() -> None:
    factory = build_channel_router_factory()
    with pytest.raises(ValueError):
        factory("does-not-exist")
