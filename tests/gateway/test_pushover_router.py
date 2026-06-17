"""TDD: standalone PushoverRouter for the notification_worker.

The router plugs into the worker's per-channel factory as a duck-typed
``.send(message, issue) -> SendResult``. It must:
- map severity -> Pushover priority per ops policy (warn=0, error=1, crit=2),
- set emergency retry/expire (120/3600) + siren only for crit (priority 2),
- return SendResult.ok=False (no raise, no retry storm) on HTTP errors,
- be fully independent of the MC severance path.
"""
from __future__ import annotations

from typing import Any

import pytest

from gateway.pushover_router import PushoverRouter


class _Capture:
    """Fake transport: records calls, returns a canned (status, body)."""

    def __init__(self, status: int = 200, body: str = '{"status":1,"request":"abc"}') -> None:
        self.status = status
        self.body = body
        self.calls: list[tuple[str, dict[str, Any], float]] = []
        self.raise_exc: Exception | None = None

    def __call__(self, url: str, data: dict[str, Any], timeout: float):
        self.calls.append((url, dict(data), timeout))
        if self.raise_exc is not None:
            raise self.raise_exc
        return self.status, self.body


def _router(transport: _Capture) -> PushoverRouter:
    return PushoverRouter(api_token="tok", user_key="usr", transport=transport)


@pytest.mark.parametrize("severity,expected", [("warn", 0), ("error", 1), ("crit", 2)])
def test_severity_maps_to_priority(severity: str, expected: int) -> None:
    cap = _Capture()
    _router(cap).send(message="m", issue={"severity": severity})
    assert cap.calls[0][1]["priority"] == expected


def test_crit_sets_emergency_retry_expire_and_siren() -> None:
    cap = _Capture()
    _router(cap).send(message="boom", issue={"severity": "crit", "title": "T"})
    sent = cap.calls[0][1]
    assert sent["priority"] == 2
    assert sent["retry"] == 120
    assert sent["expire"] == 3600
    assert sent["sound"] == "siren"


def test_warn_has_no_emergency_fields() -> None:
    cap = _Capture()
    _router(cap).send(message="x", issue={"severity": "warn"})
    sent = cap.calls[0][1]
    assert "retry" not in sent and "expire" not in sent and "sound" not in sent


def test_success_returns_ok_with_pushover_hop() -> None:
    cap = _Capture(status=200)
    result = _router(cap).send(message="ok", issue={"severity": "error"})
    assert result.ok is True
    assert result.hop == "pushover"


def test_http_4xx_returns_not_ok_without_raising() -> None:
    cap = _Capture(status=400, body='{"errors":["application token is invalid"]}')
    result = _router(cap).send(message="x", issue={"severity": "error"})
    assert result.ok is False
    assert "400" in result.error


def test_transport_exception_returns_not_ok() -> None:
    cap = _Capture()
    cap.raise_exc = TimeoutError("timed out")
    result = _router(cap).send(message="x", issue={"severity": "warn"})
    assert result.ok is False
    assert "timed out" in result.error


def test_title_message_and_credentials_in_payload() -> None:
    cap = _Capture()
    _router(cap).send(message="the body", issue={"severity": "warn", "title": "Disk full"})
    sent = cap.calls[0][1]
    assert sent["token"] == "tok"
    assert sent["user"] == "usr"
    assert sent["title"] == "Disk full"
    assert sent["message"] == "the body"


def test_from_env_requires_both_credentials(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("PUSHOVER_API_TOKEN", raising=False)
    monkeypatch.delenv("PUSHOVER_USER_KEY", raising=False)
    with pytest.raises(ValueError):
        PushoverRouter.from_env()


def test_independent_of_mc_severance(monkeypatch: pytest.MonkeyPatch) -> None:
    """Pin: delivery is identical with HUB_MC_WRITE_SEVERED=1 (router never
    touches the MC-loopback path that severance gates)."""
    monkeypatch.setenv("HUB_MC_WRITE_SEVERED", "1")
    cap = _Capture(status=200)
    result = _router(cap).send(message="m", issue={"severity": "crit"})
    assert result.ok is True
    assert len(cap.calls) == 1
