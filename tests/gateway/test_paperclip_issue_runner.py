"""Tests for gateway.paperclip_issue_runner (Jarvis-OS Phase-4 4a-4 wave 4b)."""
from __future__ import annotations

import time
from datetime import datetime, timezone
from unittest.mock import MagicMock

import pytest

from gateway.paperclip_issue_client import (
    IssueAlreadyRunning,
    IssueRunRecord,
    LockLost,
)
from gateway.paperclip_issue_runner import IssueRunHandle, PaperclipIssueRunner


def _make_record() -> IssueRunRecord:
    now = datetime.now(timezone.utc)
    return IssueRunRecord(
        run_id="run-1",
        company_id="co-1",
        issue_id="iss-1",
        executor="hermes",
        locked_by="w1",
        leased_at=now,
        lease_expires_at=now,
        heartbeat_at=now,
        status="running",
    )


def _make_client() -> MagicMock:
    client = MagicMock()
    client.acquire.return_value = _make_record()
    client.heartbeat.return_value = {"ok": True}
    client.release.return_value = _make_record()
    return client


def test_run_happy_path_release_completed():
    client = _make_client()
    runner = PaperclipIssueRunner(client=client, locked_by="w1", heartbeat_interval_seconds=10.0)

    with runner.run(company_id="co-1", issue_id="iss-1", executor="hermes") as run:
        assert isinstance(run, IssueRunHandle)
        assert run.run_id == "run-1"
        run.complete(exit_code=0, result_summary="done")

    client.acquire.assert_called_once()
    client.release.assert_called_once_with(
        run_id="run-1",
        locked_by="w1",
        status="completed",
        exit_code=0,
        result_summary="done",
    )


def test_run_body_exception_releases_failed():
    client = _make_client()
    runner = PaperclipIssueRunner(client=client, locked_by="w1", heartbeat_interval_seconds=10.0)

    with pytest.raises(RuntimeError, match="boom"):
        with runner.run(company_id="co-1", issue_id="iss-1", executor="hermes"):
            raise RuntimeError("boom")

    args = client.release.call_args.kwargs
    assert args["status"] == "failed"
    assert "RuntimeError: boom" in args["result_summary"]


def test_run_issue_already_running_propagates():
    client = _make_client()
    client.acquire.side_effect = IssueAlreadyRunning(existing=None)
    runner = PaperclipIssueRunner(client=client, locked_by="w1", heartbeat_interval_seconds=10.0)

    with pytest.raises(IssueAlreadyRunning):
        with runner.run(company_id="co-1", issue_id="iss-1", executor="hermes"):
            pass

    client.release.assert_not_called()


def test_heartbeat_loop_sets_lock_inactive_on_lock_lost():
    client = _make_client()
    client.heartbeat.side_effect = LockLost("dead")
    runner = PaperclipIssueRunner(client=client, locked_by="w1", heartbeat_interval_seconds=0.05)

    with runner.run(company_id="co-1", issue_id="iss-1", executor="hermes") as run:
        time.sleep(0.2)
        assert run.lock_active is False

    client.release.assert_not_called()


def test_explicit_fail_sets_failed_status():
    client = _make_client()
    runner = PaperclipIssueRunner(client=client, locked_by="w1", heartbeat_interval_seconds=10.0)

    with runner.run(company_id="co-1", issue_id="iss-1", executor="hermes") as run:
        run.fail(exit_code=2, result_summary="planned-fail")

    args = client.release.call_args.kwargs
    assert args["status"] == "failed"
    assert args["exit_code"] == 2
    assert args["result_summary"] == "planned-fail"


def test_release_lock_lost_is_swallowed():
    client = _make_client()
    client.release.side_effect = LockLost("recovery happened")
    runner = PaperclipIssueRunner(client=client, locked_by="w1", heartbeat_interval_seconds=10.0)

    with runner.run(company_id="co-1", issue_id="iss-1", executor="hermes"):
        pass

    client.release.assert_called_once()
