#!/usr/bin/env python3
"""project_memory_tool — Paperclip project memory (decisions + memory documents).

Jarvis-OS Phase-6 Slice C-2. Lets the agent read and write per-project
decisions and memory documents in Paperclip (the project-memory source of
truth) instead of Mission Control. Backed by PaperclipProjectMemoryClient.

company_id falls back to the PAPERCLIP_COMPANY_ID env var when omitted;
project_id is always required (the agent supplies the current project from the
issue context). Requires PAPERCLIP_API_TOKEN (filtered out otherwise).
"""
import json
import os

from tools.registry import registry, tool_error


def _client():
    from gateway.paperclip_project_memory_client import PaperclipProjectMemoryClient

    return PaperclipProjectMemoryClient()


def _resolve_company_id(args: dict) -> str:
    return (args.get("company_id") or os.environ.get("PAPERCLIP_COMPANY_ID") or "").strip()


def _resolve_scope(args: dict):
    """Return (company_id, project_id, error_str_or_None)."""
    company_id = _resolve_company_id(args)
    project_id = (args.get("project_id") or "").strip()
    if not company_id:
        return None, None, tool_error("company_id required (pass it or set PAPERCLIP_COMPANY_ID).")
    if not project_id:
        return None, None, tool_error("project_id is required.")
    return company_id, project_id, None


def pm_decisions_tool(args: dict, **_kw) -> str:
    action = (args.get("action") or "list").strip()
    company_id, project_id, err = _resolve_scope(args)
    if err:
        return err
    try:
        with _client() as client:
            if action == "list":
                return json.dumps(
                    client.list_decisions(company_id=company_id, project_id=project_id),
                    ensure_ascii=False,
                )
            if action == "get":
                source_key = (args.get("source_key") or "").strip()
                if not source_key:
                    return tool_error("source_key is required for action=get.")
                return json.dumps(
                    client.get_decision(
                        company_id=company_id, project_id=project_id, source_key=source_key
                    ),
                    ensure_ascii=False,
                )
            if action == "upsert":
                required = ("source_project_slug", "source_key", "source_hash", "title", "decision")
                missing = [k for k in required if not str(args.get(k) or "").strip()]
                if missing:
                    return tool_error(f"action=upsert requires: {', '.join(missing)}")
                return json.dumps(
                    client.upsert_decision(
                        company_id=company_id,
                        project_id=project_id,
                        source_project_slug=args["source_project_slug"],
                        source_key=args["source_key"],
                        source_hash=args["source_hash"],
                        title=args["title"],
                        decision=args["decision"],
                        context=args.get("context"),
                        consequences=args.get("consequences"),
                        status=args.get("status"),
                        metadata=args.get("metadata"),
                    ),
                    ensure_ascii=False,
                )
            return tool_error(f"unknown action '{action}' (use list/get/upsert).")
    except Exception as exc:  # noqa: BLE001 — tools return JSON errors, never raise
        return tool_error(f"project-memory decisions error: {exc}")


def pm_documents_tool(args: dict, **_kw) -> str:
    action = (args.get("action") or "list").strip()
    company_id, project_id, err = _resolve_scope(args)
    if err:
        return err
    try:
        with _client() as client:
            if action == "list":
                return json.dumps(
                    client.list_project_documents(company_id=company_id, project_id=project_id),
                    ensure_ascii=False,
                )
            if action == "get":
                key = (args.get("key") or "").strip()
                if not key:
                    return tool_error("key is required for action=get.")
                return json.dumps(
                    client.get_project_document(
                        company_id=company_id, project_id=project_id, key=key
                    ),
                    ensure_ascii=False,
                )
            if action == "upsert":
                key = (args.get("key") or "").strip()
                body = args.get("body")
                if not key:
                    return tool_error("key is required for action=upsert.")
                if body is None:
                    return tool_error("body is required for action=upsert.")
                return json.dumps(
                    client.upsert_project_document(
                        company_id=company_id,
                        project_id=project_id,
                        key=key,
                        body=body,
                        title=args.get("title"),
                        change_summary=args.get("change_summary"),
                        tags=args.get("tags"),
                        metadata=args.get("metadata"),
                    ),
                    ensure_ascii=False,
                )
            return tool_error(f"unknown action '{action}' (use list/get/upsert).")
    except Exception as exc:  # noqa: BLE001
        return tool_error(f"project-memory documents error: {exc}")


# =============================================================================
# Function-calling schemas (flat style, no outer "type":"function" wrapper)
# =============================================================================

_SCOPE_PROPS = {
    "project_id": {
        "type": "string",
        "description": "Paperclip project id (UUID) — the current project. Required.",
    },
    "company_id": {
        "type": "string",
        "description": "Company id (UUID). Optional — falls back to PAPERCLIP_COMPANY_ID env.",
    },
}

PM_DECISIONS_SCHEMA = {
    "name": "pm_decisions",
    "description": (
        "Read or write per-project DECISIONS (ADR-style records) in Paperclip "
        "project-memory. action=list returns all decisions for the project; "
        "action=get fetches one by source_key; action=upsert creates or updates "
        "a decision idempotently (keyed by source_key)."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "action": {"type": "string", "enum": ["list", "get", "upsert"]},
            **_SCOPE_PROPS,
            "source_key": {"type": "string", "description": "Stable key (e.g. 'adr-0001'). Required for get/upsert."},
            "source_project_slug": {"type": "string", "description": "Workspace project slug. Required for upsert."},
            "source_hash": {"type": "string", "description": "Content hash for change detection. Required for upsert."},
            "title": {"type": "string", "description": "Decision title. Required for upsert."},
            "decision": {"type": "string", "description": "The decision itself. Required for upsert."},
            "context": {"type": "string", "description": "Optional background/context."},
            "consequences": {"type": "string", "description": "Optional consequences."},
            "status": {"type": "string", "enum": ["proposed", "accepted", "deprecated", "superseded"]},
        },
        "required": ["action", "project_id"],
    },
}

PM_DOCUMENTS_SCHEMA = {
    "name": "pm_documents",
    "description": (
        "Read or write per-project MEMORY DOCUMENTS in Paperclip project-memory "
        "(each keyed by a string like 'project-memory'). action=list returns the "
        "project's documents; action=get fetches the current body by key; "
        "action=upsert writes a new revision (or creates the document) — history "
        "is preserved."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "action": {"type": "string", "enum": ["list", "get", "upsert"]},
            **_SCOPE_PROPS,
            "key": {"type": "string", "description": "Document key (e.g. 'project-memory'). Required for get/upsert."},
            "body": {"type": "string", "description": "Document body (markdown). Required for upsert."},
            "title": {"type": "string", "description": "Optional document title."},
            "change_summary": {"type": "string", "description": "Optional summary of this revision."},
        },
        "required": ["action", "project_id"],
    },
}


registry.register(
    name="pm_decisions",
    toolset="project_memory",
    schema=PM_DECISIONS_SCHEMA,
    handler=lambda args, **kw: pm_decisions_tool(args, **kw),
    requires_env=["PAPERCLIP_API_TOKEN"],
    emoji="🧭",
)

registry.register(
    name="pm_documents",
    toolset="project_memory",
    schema=PM_DOCUMENTS_SCHEMA,
    handler=lambda args, **kw: pm_documents_tool(args, **kw),
    requires_env=["PAPERCLIP_API_TOKEN"],
    emoji="🗂️",
)
