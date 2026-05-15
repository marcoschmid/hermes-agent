"""Phase-3 G3 contract tests for notification fallback resilience.

Target production surface:

* ``gateway.fallback_channels.FallbackNotificationRouter``
* Hermes -> Mission Control -> safe_telegram_send.sh direct fallback chain
* run-log entries for every attempted hop
* eligibility gate to prevent broad automatic fallback

Until ``gateway.fallback_channels`` exists, these tests are expected to fail
and should be routed to Phase-4 code apply.
"""

from __future__ import annotations

import importlib
from pathlib import Path
from typing import Any, Callable

import pytest


class _Recorder:
    def __init__(self, *, fail: bool = False):
        self.fail = fail
        self.calls: list[str] = []

    def __call__(self, message: str, **_kwargs: Any) -> dict[str, Any]:
        self.calls.append(message)
        if self.fail:
            raise RuntimeError("hop unavailable")
        return {"ok": True}


def _load_fallback_module():
    try:
        return importlib.import_module("gateway.fallback_channels")
    except ModuleNotFoundError as exc:
        if exc.name == "gateway.fallback_channels":
            pytest.fail(
                "ROUTE_TO_PHASE_4_CODE_APPLY: gateway.fallback_channels fehlt. "
                "Phase-3 G3 Fallback-Tests koennen erst gruen werden, wenn "
                "FallbackNotificationRouter + Eligibility-Gate implementiert sind.",
                pytrace=False,
            )
        raise


def _make_router(
    tmp_path: Path,
    *,
    hermes_fail: bool = False,
    mc_fail: bool = False,
    eligible: Callable[[dict[str, Any]], bool] | None = None,
):
    module = _load_fallback_module()
    router_cls = getattr(module, "FallbackNotificationRouter", None)
    if router_cls is None:
        pytest.fail(
            "ROUTE_TO_PHASE_4_CODE_APPLY: "
            "gateway.fallback_channels.FallbackNotificationRouter fehlt.",
            pytrace=False,
        )

    hermes = _Recorder(fail=hermes_fail)
    mission_control = _Recorder(fail=mc_fail)
    direct = _Recorder(fail=False)
    run_log = tmp_path / "notification-run-log.jsonl"
    router = router_cls(
        hermes_send=hermes,
        mission_control_send=mission_control,
        direct_send=direct,
        run_log_path=str(run_log),
        eligibility_gate=eligible or (lambda issue: True),
    )
    return router, hermes, mission_control, direct, run_log


def _send(router: Any, *, issue: dict[str, Any] | None = None, message: str = "G3 test"):
    method = getattr(router, "send", None)
    if not callable(method):
        pytest.fail(
            "ROUTE_TO_PHASE_4_CODE_APPLY: FallbackNotificationRouter.send fehlt.",
            pytrace=False,
        )
    return method(message=message, issue=issue or {"id": "TEC-1", "labels": ["g3"]})


def _value(result: Any, key: str) -> Any:
    if isinstance(result, dict):
        return result.get(key)
    if hasattr(result, key):
        return getattr(result, key)
    try:
        return result[key]
    except (TypeError, KeyError, IndexError):
        return None


def test_hermes_send_success_path(tmp_path: Path):
    router, hermes, mission_control, direct, _run_log = _make_router(tmp_path)

    result = _send(router)

    assert _value(result, "ok") is True
    assert _value(result, "hop") == "hermes"
    assert len(hermes.calls) == 1
    assert mission_control.calls == []
    assert direct.calls == []


def test_hermes_down_triggers_mc_fallback(tmp_path: Path):
    router, hermes, mission_control, direct, _run_log = _make_router(
        tmp_path, hermes_fail=True
    )

    result = _send(router)

    assert _value(result, "ok") is True
    assert _value(result, "hop") == "mission-control"
    assert len(hermes.calls) == 1
    assert len(mission_control.calls) == 1
    assert direct.calls == []


def test_mc_down_triggers_direct_fallback(tmp_path: Path):
    router, _hermes, mission_control, direct, _run_log = _make_router(
        tmp_path, hermes_fail=True, mc_fail=True
    )

    result = _send(router)

    assert _value(result, "ok") is True
    assert _value(result, "hop") == "direct-fallback"
    assert len(mission_control.calls) == 1
    assert len(direct.calls) == 1


def test_three_stage_fallback_and_run_log_contains_all_hops(tmp_path: Path):
    router, _hermes, _mission_control, _direct, run_log = _make_router(
        tmp_path, hermes_fail=True, mc_fail=True
    )

    result = _send(router, message="three-stage")

    assert _value(result, "ok") is True
    assert run_log.exists()
    logged = run_log.read_text()
    assert "hermes" in logged
    assert "mission-control" in logged
    assert "direct-fallback" in logged


def test_eligibility_gate_blocks_auto_fallback(tmp_path: Path):
    router, hermes, mission_control, direct, _run_log = _make_router(
        tmp_path,
        hermes_fail=True,
        eligible=lambda issue: "allow-auto-fallback" in issue.get("labels", []),
    )

    result = _send(router, issue={"id": "LOW-1", "labels": []})

    assert _value(result, "ok") is False
    assert _value(result, "blocked_by") == "eligibility-gate"
    assert len(hermes.calls) == 1
    assert mission_control.calls == []
    assert direct.calls == []
