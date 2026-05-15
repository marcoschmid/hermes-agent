"""Paperclip issue-runs HTTP client — Jarvis-OS Phase-4 (Marco-Decision 4D-8).

Hermes calls Paperclip's /api/internal/issue-runs/* endpoints instead of touching
the database directly. This client is a thin wrapper around httpx that handles
auth, timeouts, and explicit lock_lost / issue_already_running outcomes.

Env vars:
  PAPERCLIP_API_URL    Defaults to http://127.0.0.1:3347 (per environment.json)
  PAPERCLIP_API_TOKEN  Board-level API token with issue_runs:write authority

Note: per feedback_paperclip_localhost_ipv6, always use 127.0.0.1 not 'localhost'.
"""
from __future__ import annotations

import os
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Literal, Optional

import httpx

DEFAULT_BASE_URL = "http://127.0.0.1:3347"
DEFAULT_TIMEOUT_SECONDS = 10.0


class IssueRunsClientError(RuntimeError):
    """Raised when the Paperclip API returns an unexpected error."""


class LockLost(RuntimeError):
    """Raised when the caller's lock has been lost (heartbeat/release failure)."""


class IssueAlreadyRunning(RuntimeError):
    """Raised when acquire fails because another run holds the lock."""

    def __init__(self, existing: Optional[dict[str, Any]]) -> None:
        super().__init__("issue is already running")
        self.existing = existing


@dataclass(frozen=True)
class IssueRunRecord:
    run_id: str
    company_id: str
    issue_id: str
    executor: str
    locked_by: str
    leased_at: datetime
    lease_expires_at: datetime
    heartbeat_at: datetime
    status: str

    @classmethod
    def from_api(cls, payload: dict[str, Any]) -> "IssueRunRecord":
        return cls(
            run_id=payload["runId"],
            company_id=payload["companyId"],
            issue_id=payload["issueId"],
            executor=payload["executor"],
            locked_by=payload["leaseOwner"],
            leased_at=_parse_dt(payload["leasedAt"]),
            lease_expires_at=_parse_dt(payload["leaseExpiresAt"]),
            heartbeat_at=_parse_dt(payload["heartbeatAt"]),
            status=payload["status"],
        )


class PaperclipIssueRunsClient:
    """Low-level HTTP client. One instance per Hermes process."""

    def __init__(
        self,
        base_url: Optional[str] = None,
        token: Optional[str] = None,
        timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS,
    ) -> None:
        self._base = (base_url or os.environ.get("PAPERCLIP_API_URL") or DEFAULT_BASE_URL).rstrip("/")
        self._token = token or os.environ.get("PAPERCLIP_API_TOKEN")
        if not self._token:
            raise IssueRunsClientError("PAPERCLIP_API_TOKEN env var is required")
        self._client = httpx.Client(
            base_url=self._base,
            timeout=timeout_seconds,
            headers={"Authorization": f"Bearer {self._token}"},
        )

    def close(self) -> None:
        self._client.close()

    def acquire(
        self,
        *,
        company_id: str,
        issue_id: str,
        executor: Literal["hermes", "mc-dispatch"],
        locked_by: str,
        ttl_seconds: Optional[int] = None,
        prompt_snapshot_path: Optional[str] = None,
    ) -> IssueRunRecord:
        body: dict[str, Any] = {
            "companyId": company_id,
            "issueId": issue_id,
            "executor": executor,
            "lockedBy": locked_by,
        }
        if ttl_seconds is not None:
            body["ttlSeconds"] = ttl_seconds
        if prompt_snapshot_path is not None:
            body["promptSnapshotPath"] = prompt_snapshot_path
        r = self._client.post("/api/internal/issue-runs/acquire", json=body)
        data = _ok_json(r)
        if r.status_code == 409 and not data.get("acquired", True):
            raise IssueAlreadyRunning(existing=data.get("existing"))
        if not data.get("acquired"):
            raise IssueRunsClientError(f"acquire returned unexpected payload: {data!r}")
        return IssueRunRecord.from_api(data["run"])

    def heartbeat(
        self,
        *,
        run_id: str,
        locked_by: str,
        extend_by_seconds: Optional[int] = None,
    ) -> dict[str, Any]:
        body: dict[str, Any] = {"lockedBy": locked_by}
        if extend_by_seconds is not None:
            body["extendBySeconds"] = extend_by_seconds
        r = self._client.post(f"/api/internal/issue-runs/{run_id}/heartbeat", json=body)
        data = _ok_json(r)
        if r.status_code == 409 or data.get("ok") is False:
            raise LockLost(f"heartbeat lost lock for run {run_id}")
        return data

    def release(
        self,
        *,
        run_id: str,
        locked_by: str,
        status: Literal["completed", "failed"],
        exit_code: Optional[int] = None,
        result_summary: Optional[str] = None,
    ) -> IssueRunRecord:
        body: dict[str, Any] = {"lockedBy": locked_by, "status": status}
        if exit_code is not None:
            body["exitCode"] = exit_code
        if result_summary is not None:
            body["resultSummary"] = result_summary
        r = self._client.post(f"/api/internal/issue-runs/{run_id}/release", json=body)
        data = _ok_json(r)
        if r.status_code == 409 or data.get("ok") is False:
            raise LockLost(f"release lost lock for run {run_id}")
        return IssueRunRecord.from_api(data["run"])

    def recover_stale(
        self,
        *,
        trigger: Literal["boot", "watchdog", "manual"] = "manual",
        limit: int = 100,
        dry_run: bool = False,
    ) -> dict[str, Any]:
        body = {"trigger": trigger, "limit": limit, "dryRun": dry_run}
        r = self._client.post("/api/internal/issue-runs/recover-stale", json=body)
        return _ok_json(r)

    def list_eligible_fallback_issues(self, *, company_id: str, limit: int = 25) -> list[dict[str, Any]]:
        r = self._client.get(
            "/api/internal/fallback/mc-dispatch/eligible-issues",
            params={"companyId": company_id, "limit": limit},
        )
        data = _ok_json(r)
        issues = data.get("issues") if isinstance(data, dict) else None
        return issues if isinstance(issues, list) else []

    def fire_fallback(
        self,
        *,
        company_id: str,
        issue_id: str,
        issue_run_id: Optional[str] = None,
        reason: str,
        hermes_health_snapshot: Optional[dict[str, Any]] = None,
        dry_run: bool = True,
    ) -> dict[str, Any]:
        body: dict[str, Any] = {
            "companyId": company_id,
            "issueId": issue_id,
            "fallbackFrom": "hermes",
            "reason": reason,
            "dryRun": dry_run,
        }
        if issue_run_id is not None:
            body["issueRunId"] = issue_run_id
        if hermes_health_snapshot is not None:
            body["hermesHealthSnapshot"] = hermes_health_snapshot
        r = self._client.post("/api/internal/fallback/mc-dispatch", json=body)
        return _ok_json(r)


def _ok_json(r: httpx.Response) -> dict[str, Any]:
    if r.status_code >= 500:
        raise IssueRunsClientError(f"server error {r.status_code}: {r.text[:500]}")
    if r.status_code in (401, 403):
        raise IssueRunsClientError(f"auth error {r.status_code}: {r.text[:200]}")
    try:
        return r.json()
    except ValueError as exc:
        raise IssueRunsClientError(f"invalid json from server: {r.text[:200]}") from exc


def _parse_dt(value: str) -> datetime:
    if value.endswith("Z"):
        value = value[:-1] + "+00:00"
    return datetime.fromisoformat(value)
