"""Tests for gateway.paperclip_issue_client (Jarvis-OS Phase-4 4a-4 wave 4b)."""
from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any
from unittest.mock import patch

import httpx
import pytest

from gateway.paperclip_issue_client import (
    IssueAlreadyRunning,
    IssueRunRecord,
    IssueRunsClientError,
    LockLost,
    PaperclipIssueRunsClient,
)


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _make_run_payload(run_id: str = "run-1", owner: str = "w1") -> dict[str, Any]:
    now = _now_iso()
    return {
        "runId": run_id,
        "companyId": "co-1",
        "issueId": "iss-1",
        "executor": "hermes",
        "leaseOwner": owner,
        "leasedAt": now,
        "leaseExpiresAt": now,
        "heartbeatAt": now,
        "status": "running",
    }


@pytest.fixture
def client_factory():
    """Build a client with a MockTransport-backed httpx.Client."""

    def _factory(handler):
        transport = httpx.MockTransport(handler)
        client = PaperclipIssueRunsClient.__new__(PaperclipIssueRunsClient)
        client._base = "http://test"
        client._token = "tok"
        client._client = httpx.Client(
            base_url="http://test",
            transport=transport,
            headers={"Authorization": "Bearer tok"},
        )
        return client

    return _factory


def test_constructor_requires_token(monkeypatch):
    monkeypatch.delenv("PAPERCLIP_API_TOKEN", raising=False)
    with pytest.raises(IssueRunsClientError, match="PAPERCLIP_API_TOKEN"):
        PaperclipIssueRunsClient(base_url="http://test", token=None)


def test_acquire_success(client_factory):
    def handler(req: httpx.Request) -> httpx.Response:
        assert req.url.path == "/api/internal/issue-runs/acquire"
        body = json.loads(req.content)
        assert body["companyId"] == "co-1"
        return httpx.Response(201, json={"acquired": True, "run": _make_run_payload()})

    client = client_factory(handler)
    run = client.acquire(
        company_id="co-1",
        issue_id="iss-1",
        executor="hermes",
        locked_by="w1",
    )
    assert isinstance(run, IssueRunRecord)
    assert run.run_id == "run-1"
    assert run.locked_by == "w1"


def test_acquire_conflict_raises(client_factory):
    def handler(req: httpx.Request) -> httpx.Response:
        return httpx.Response(
            409,
            json={"acquired": False, "reason": "issue_already_running", "existing": {"runId": "x"}},
        )

    client = client_factory(handler)
    with pytest.raises(IssueAlreadyRunning) as exc_info:
        client.acquire(company_id="co-1", issue_id="iss-1", executor="hermes", locked_by="w1")
    assert exc_info.value.existing == {"runId": "x"}


def test_heartbeat_success(client_factory):
    def handler(req: httpx.Request) -> httpx.Response:
        assert req.url.path == "/api/internal/issue-runs/run-1/heartbeat"
        return httpx.Response(200, json={"ok": True, "leaseExpiresAt": _now_iso(), "heartbeatAt": _now_iso()})

    client = client_factory(handler)
    result = client.heartbeat(run_id="run-1", locked_by="w1")
    assert result["ok"] is True


def test_heartbeat_lock_lost(client_factory):
    def handler(req: httpx.Request) -> httpx.Response:
        return httpx.Response(409, json={"ok": False, "reason": "lock_lost"})

    client = client_factory(handler)
    with pytest.raises(LockLost):
        client.heartbeat(run_id="run-1", locked_by="w1")


def test_release_success(client_factory):
    def handler(req: httpx.Request) -> httpx.Response:
        assert req.url.path == "/api/internal/issue-runs/run-1/release"
        body = json.loads(req.content)
        assert body["status"] == "completed"
        return httpx.Response(200, json={"ok": True, "run": _make_run_payload()})

    client = client_factory(handler)
    record = client.release(run_id="run-1", locked_by="w1", status="completed", exit_code=0)
    assert isinstance(record, IssueRunRecord)


def test_release_lock_lost(client_factory):
    def handler(req: httpx.Request) -> httpx.Response:
        return httpx.Response(409, json={"ok": False, "reason": "lock_lost"})

    client = client_factory(handler)
    with pytest.raises(LockLost):
        client.release(run_id="run-1", locked_by="w1", status="failed")


def test_recover_stale(client_factory):
    def handler(req: httpx.Request) -> httpx.Response:
        body = json.loads(req.content)
        assert body == {"trigger": "watchdog", "limit": 50, "dryRun": True}
        return httpx.Response(
            200,
            json={"trigger": "watchdog", "dryRun": True, "candidates": [], "recovered": []},
        )

    client = client_factory(handler)
    result = client.recover_stale(trigger="watchdog", limit=50, dry_run=True)
    assert result["dryRun"] is True


def test_server_error_raises_client_error(client_factory):
    def handler(req: httpx.Request) -> httpx.Response:
        return httpx.Response(500, text="boom")

    client = client_factory(handler)
    with pytest.raises(IssueRunsClientError, match="server error"):
        client.heartbeat(run_id="run-1", locked_by="w1")


def test_auth_error_raises_client_error(client_factory):
    def handler(req: httpx.Request) -> httpx.Response:
        return httpx.Response(401, text="not auth")

    client = client_factory(handler)
    with pytest.raises(IssueRunsClientError, match="auth error"):
        client.heartbeat(run_id="run-1", locked_by="w1")


def test_list_eligible_fallback_issues(client_factory):
    def handler(req: httpx.Request) -> httpx.Response:
        assert req.url.path == "/api/internal/fallback/mc-dispatch/eligible-issues"
        assert req.url.params.get("companyId") == "co-1"
        assert req.url.params.get("limit") == "10"
        return httpx.Response(
            200,
            json={
                "companyId": "co-1",
                "issues": [
                    {"issueId": "iss-1", "assigneeAgentId": "a-1", "issueStatus": "todo"},
                    {"issueId": "iss-2", "assigneeAgentId": "a-2", "issueStatus": "in_progress"},
                ],
            },
        )

    client = client_factory(handler)
    issues = client.list_eligible_fallback_issues(company_id="co-1", limit=10)
    assert len(issues) == 2
    assert issues[0]["issueId"] == "iss-1"


def test_list_eligible_fallback_empty(client_factory):
    def handler(req: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"companyId": "co-1", "issues": []})

    client = client_factory(handler)
    issues = client.list_eligible_fallback_issues(company_id="co-1")
    assert issues == []


def test_fire_fallback_accepted_dry_run(client_factory):
    def handler(req: httpx.Request) -> httpx.Response:
        assert req.url.path == "/api/internal/fallback/mc-dispatch"
        body = json.loads(req.content)
        assert body["dryRun"] is True
        assert body["fallbackFrom"] == "hermes"
        return httpx.Response(
            200,
            json={
                "accepted": True,
                "mode": "mc-dispatch",
                "outcome": "accepted-dry-run",
                "legacyTaskId": None,
                "issueRunId": None,
                "warnings": [],
            },
        )

    client = client_factory(handler)
    result = client.fire_fallback(
        company_id="co-1",
        issue_id="iss-1",
        reason="hermes_down_60s",
        dry_run=True,
    )
    assert result["accepted"] is True
    assert result["outcome"] == "accepted-dry-run"


def test_fire_fallback_with_snapshot(client_factory):
    def handler(req: httpx.Request) -> httpx.Response:
        body = json.loads(req.content)
        assert body["hermesHealthSnapshot"]["state"] == "down"
        return httpx.Response(
            200,
            json={
                "accepted": True,
                "mode": "mc-dispatch",
                "outcome": "accepted-spawned",
                "legacyTaskId": None,
                "issueRunId": "run-1",
                "warnings": [],
            },
        )

    client = client_factory(handler)
    result = client.fire_fallback(
        company_id="co-1",
        issue_id="iss-1",
        reason="hermes_down_180s",
        hermes_health_snapshot={"state": "down", "pid": None},
        dry_run=False,
    )
    assert result["outcome"] == "accepted-spawned"
