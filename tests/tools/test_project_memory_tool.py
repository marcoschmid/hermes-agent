"""Tests for tools/project_memory_tool.py (Jarvis-OS Phase-6 Slice C-2)."""
from __future__ import annotations

import json

import httpx
import pytest

from gateway.paperclip_project_memory_client import PaperclipProjectMemoryClient
import tools.project_memory_tool as pm


def patch_client(monkeypatch, handler):
    def factory():
        return PaperclipProjectMemoryClient(
            base_url="http://test", token="tok", transport=httpx.MockTransport(handler)
        )

    monkeypatch.setattr(pm, "_client", factory)


@pytest.fixture(autouse=True)
def _env(monkeypatch):
    monkeypatch.setenv("PAPERCLIP_API_TOKEN", "tok")
    monkeypatch.delenv("PAPERCLIP_COMPANY_ID", raising=False)


def test_decisions_list_uses_env_company(monkeypatch):
    monkeypatch.setenv("PAPERCLIP_COMPANY_ID", "c1")
    seen: dict = {}

    def handler(req: httpx.Request) -> httpx.Response:
        seen["method"] = req.method
        seen["path"] = req.url.path
        return httpx.Response(200, json=[{"id": "d1"}])

    patch_client(monkeypatch, handler)
    out = pm.pm_decisions_tool({"action": "list", "project_id": "p1"})
    assert json.loads(out) == [{"id": "d1"}]
    assert seen["method"] == "GET"
    assert seen["path"] == "/api/companies/c1/projects/p1/decisions"


def test_decisions_requires_project_id(monkeypatch):
    monkeypatch.setenv("PAPERCLIP_COMPANY_ID", "c1")
    out = pm.pm_decisions_tool({"action": "list"})
    assert "error" in json.loads(out)


def test_decisions_requires_company(monkeypatch):
    out = pm.pm_decisions_tool({"action": "list", "project_id": "p1"})
    assert "error" in json.loads(out)


def test_decisions_upsert_posts_camelcase(monkeypatch):
    captured: dict = {}

    def handler(req: httpx.Request) -> httpx.Response:
        captured["method"] = req.method
        captured["body"] = json.loads(req.content)
        return httpx.Response(201, json={"id": "d1", "status": "accepted"})

    patch_client(monkeypatch, handler)
    out = pm.pm_decisions_tool(
        {
            "action": "upsert",
            "company_id": "c1",
            "project_id": "p1",
            "source_project_slug": "jarvis-os-redesign",
            "source_key": "adr-1",
            "source_hash": "h1",
            "title": "T",
            "decision": "D",
        }
    )
    assert json.loads(out)["id"] == "d1"
    assert captured["method"] == "POST"
    assert captured["body"]["sourceKey"] == "adr-1"
    assert captured["body"]["sourceProjectSlug"] == "jarvis-os-redesign"


def test_decisions_upsert_missing_fields(monkeypatch):
    out = pm.pm_decisions_tool(
        {"action": "upsert", "company_id": "c1", "project_id": "p1", "source_key": "adr-1"}
    )
    assert "error" in json.loads(out)


def test_documents_upsert_puts(monkeypatch):
    captured: dict = {}

    def handler(req: httpx.Request) -> httpx.Response:
        captured["method"] = req.method
        captured["path"] = req.url.path
        captured["body"] = json.loads(req.content)
        return httpx.Response(200, json={"key": "project-memory", "latestRevisionNumber": 2})

    patch_client(monkeypatch, handler)
    out = pm.pm_documents_tool(
        {
            "action": "upsert",
            "company_id": "c1",
            "project_id": "p1",
            "key": "project-memory",
            "body": "# v2",
        }
    )
    assert json.loads(out)["latestRevisionNumber"] == 2
    assert captured["method"] == "PUT"
    assert captured["path"] == "/api/companies/c1/projects/p1/documents/project-memory"
    assert captured["body"] == {"body": "# v2"}


def test_documents_get_404_returns_null(monkeypatch):
    patch_client(monkeypatch, lambda req: httpx.Response(404, json={"error": "x"}))
    out = pm.pm_documents_tool(
        {"action": "get", "company_id": "c1", "project_id": "p1", "key": "missing"}
    )
    assert json.loads(out) is None


def test_unknown_action_returns_error(monkeypatch):
    out = pm.pm_decisions_tool({"action": "frobnicate", "company_id": "c1", "project_id": "p1"})
    assert "error" in json.loads(out)
