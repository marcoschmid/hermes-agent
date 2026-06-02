"""Tests for gateway/paperclip_project_memory_client.py (Jarvis-OS Phase-6 Slice C)."""
from __future__ import annotations

import json

import httpx
import pytest

from gateway.paperclip_project_memory_client import (
    PaperclipProjectMemoryClient,
    ProjectMemoryClientError,
)

CID = "c1"
PID = "p1"


def make_client(handler, token="tok"):
    transport = httpx.MockTransport(handler)
    return PaperclipProjectMemoryClient(base_url="http://test", token=token, transport=transport)


def test_requires_token(monkeypatch):
    monkeypatch.delenv("PAPERCLIP_API_TOKEN", raising=False)
    with pytest.raises(ProjectMemoryClientError):
        PaperclipProjectMemoryClient(base_url="http://test", token=None)


def test_list_decisions_builds_request_and_parses():
    seen: dict = {}

    def handler(req: httpx.Request) -> httpx.Response:
        seen["method"] = req.method
        seen["path"] = req.url.path
        seen["auth"] = req.headers.get("authorization")
        return httpx.Response(200, json=[{"id": "d1", "sourceKey": "adr-1"}])

    out = make_client(handler).list_decisions(company_id=CID, project_id=PID)
    assert out == [{"id": "d1", "sourceKey": "adr-1"}]
    assert seen["method"] == "GET"
    assert seen["path"] == f"/api/companies/{CID}/projects/{PID}/decisions"
    assert seen["auth"] == "Bearer tok"


def test_get_decision_404_returns_none():
    client = make_client(lambda req: httpx.Response(404, json={"error": "x"}))
    assert client.get_decision(company_id=CID, project_id=PID, source_key="missing") is None


def test_upsert_decision_posts_camelcase_and_drops_none():
    captured: dict = {}

    def handler(req: httpx.Request) -> httpx.Response:
        captured["method"] = req.method
        captured["path"] = req.url.path
        captured["body"] = json.loads(req.content)
        return httpx.Response(201, json={"id": "d1", "status": "accepted"})

    out = make_client(handler).upsert_decision(
        company_id=CID,
        project_id=PID,
        source_project_slug="jarvis-os-redesign",
        source_key="adr-1",
        source_hash="h1",
        title="T",
        decision="D",
    )
    assert out["id"] == "d1"
    assert captured["method"] == "POST"
    assert captured["path"] == f"/api/companies/{CID}/projects/{PID}/decisions"
    assert captured["body"] == {
        "sourceProjectSlug": "jarvis-os-redesign",
        "sourceKey": "adr-1",
        "sourceHash": "h1",
        "title": "T",
        "decision": "D",
    }
    assert "context" not in captured["body"]


def test_get_project_document_404_returns_none():
    client = make_client(lambda req: httpx.Response(404, json={"error": "x"}))
    assert client.get_project_document(company_id=CID, project_id=PID, key="project-memory") is None


def test_upsert_project_document_puts_body():
    captured: dict = {}

    def handler(req: httpx.Request) -> httpx.Response:
        captured["method"] = req.method
        captured["path"] = req.url.path
        captured["body"] = json.loads(req.content)
        return httpx.Response(200, json={"key": "project-memory", "latestRevisionNumber": 2})

    out = make_client(handler).upsert_project_document(
        company_id=CID,
        project_id=PID,
        key="project-memory",
        body="# v2",
        change_summary="2nd",
    )
    assert out["latestRevisionNumber"] == 2
    assert captured["method"] == "PUT"
    assert captured["path"] == f"/api/companies/{CID}/projects/{PID}/documents/project-memory"
    assert captured["body"] == {"body": "# v2", "changeSummary": "2nd"}


def test_auth_error_raises():
    client = make_client(lambda req: httpx.Response(403, text="forbidden"))
    with pytest.raises(ProjectMemoryClientError):
        client.list_decisions(company_id=CID, project_id=PID)
