"""Paperclip project-memory HTTP client — Jarvis-OS Phase-6 (Slice C).

Reads/writes per-project decisions and memory documents via Paperclip's
project-scoped routes, instead of touching Mission Control. Mirrors
``paperclip_issue_client`` (auth, base URL, timeout, _ok_json error handling).

Env vars:
  PAPERCLIP_API_URL    Defaults to http://127.0.0.1:3347 (per environment.json)
  PAPERCLIP_API_TOKEN  Board-level API token with company access

Note: per feedback_paperclip_localhost_ipv6, always use 127.0.0.1 not 'localhost'.
"""
from __future__ import annotations

import os
from typing import Any, Optional

import httpx

DEFAULT_BASE_URL = "http://127.0.0.1:3347"
DEFAULT_TIMEOUT_SECONDS = 10.0


class ProjectMemoryClientError(RuntimeError):
    """Raised when the Paperclip project-memory API returns an unexpected error."""


def _ok_json(r: httpx.Response) -> Any:
    if r.status_code >= 500:
        raise ProjectMemoryClientError(f"server error {r.status_code}: {r.text[:500]}")
    if r.status_code in (401, 403):
        raise ProjectMemoryClientError(f"auth error {r.status_code}: {r.text[:200]}")
    if r.status_code >= 400:
        raise ProjectMemoryClientError(
            f"{r.request.method} {r.request.url.path}: {r.status_code} {r.text[:200]}"
        )
    try:
        return r.json()
    except ValueError as exc:
        raise ProjectMemoryClientError(f"invalid json from server: {r.text[:200]}") from exc


def _drop_none(payload: dict[str, Any]) -> dict[str, Any]:
    return {k: v for k, v in payload.items() if v is not None}


class PaperclipProjectMemoryClient:
    """Low-level HTTP client for project-scoped decisions + memory documents."""

    def __init__(
        self,
        base_url: Optional[str] = None,
        token: Optional[str] = None,
        timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS,
        transport: Optional[httpx.BaseTransport] = None,
    ) -> None:
        self._base = (base_url or os.environ.get("PAPERCLIP_API_URL") or DEFAULT_BASE_URL).rstrip("/")
        self._token = token or os.environ.get("PAPERCLIP_API_TOKEN")
        if not self._token:
            raise ProjectMemoryClientError("PAPERCLIP_API_TOKEN env var is required")
        self._client = httpx.Client(
            base_url=self._base,
            timeout=timeout_seconds,
            headers={"Authorization": f"Bearer {self._token}"},
            transport=transport,
        )

    def close(self) -> None:
        self._client.close()

    def __enter__(self) -> "PaperclipProjectMemoryClient":
        return self

    def __exit__(self, *_exc: object) -> None:
        self.close()

    # ── decisions ────────────────────────────────────────────────────────
    def list_decisions(self, *, company_id: str, project_id: str) -> list[dict[str, Any]]:
        r = self._client.get(f"/api/companies/{company_id}/projects/{project_id}/decisions")
        return _ok_json(r)

    def get_decision(
        self, *, company_id: str, project_id: str, source_key: str
    ) -> Optional[dict[str, Any]]:
        r = self._client.get(
            f"/api/companies/{company_id}/projects/{project_id}/decisions/{source_key}"
        )
        if r.status_code == 404:
            return None
        return _ok_json(r)

    def upsert_decision(
        self,
        *,
        company_id: str,
        project_id: str,
        source_project_slug: str,
        source_key: str,
        source_hash: str,
        title: str,
        decision: str,
        context: Optional[str] = None,
        consequences: Optional[str] = None,
        status: Optional[str] = None,
        metadata: Optional[dict[str, Any]] = None,
    ) -> dict[str, Any]:
        body = _drop_none(
            {
                "sourceProjectSlug": source_project_slug,
                "sourceKey": source_key,
                "sourceHash": source_hash,
                "title": title,
                "decision": decision,
                "context": context,
                "consequences": consequences,
                "status": status,
                "metadata": metadata,
            }
        )
        r = self._client.post(
            f"/api/companies/{company_id}/projects/{project_id}/decisions", json=body
        )
        return _ok_json(r)

    # ── project memory documents ─────────────────────────────────────────
    def list_project_documents(
        self, *, company_id: str, project_id: str
    ) -> list[dict[str, Any]]:
        r = self._client.get(f"/api/companies/{company_id}/projects/{project_id}/documents")
        return _ok_json(r)

    def get_project_document(
        self, *, company_id: str, project_id: str, key: str
    ) -> Optional[dict[str, Any]]:
        r = self._client.get(
            f"/api/companies/{company_id}/projects/{project_id}/documents/{key}"
        )
        if r.status_code == 404:
            return None
        return _ok_json(r)

    def upsert_project_document(
        self,
        *,
        company_id: str,
        project_id: str,
        key: str,
        body: str,
        title: Optional[str] = None,
        change_summary: Optional[str] = None,
        tags: Optional[list[Any]] = None,
        metadata: Optional[dict[str, Any]] = None,
    ) -> dict[str, Any]:
        payload = _drop_none(
            {
                "body": body,
                "title": title,
                "changeSummary": change_summary,
                "tags": tags,
                "metadata": metadata,
            }
        )
        r = self._client.put(
            f"/api/companies/{company_id}/projects/{project_id}/documents/{key}", json=payload
        )
        return _ok_json(r)
