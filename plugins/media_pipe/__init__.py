"""Hermes plugin exposing OpenClaw Media Pipe as provider-neutral tools."""

from __future__ import annotations

import json
import os
import subprocess
import tempfile
from pathlib import Path
from typing import Any


MEDIA_PIPE_CLI = Path(
    os.environ.get(
        "MEDIA_PIPE_CLI",
        "/Users/marco/.openclaw/workspace/scripts/media-pipe",
    )
)

PROVIDER_SPECIFIC_FIELDS = {
    "provider",
    "provider_id",
    "model",
    "tool",
    "adapter",
    "candidate_calls",
}


def _available() -> bool:
    return MEDIA_PIPE_CLI.exists() and os.access(MEDIA_PIPE_CLI, os.X_OK)


def _json_response(payload: dict[str, Any]) -> str:
    return json.dumps(payload, ensure_ascii=False, indent=2)


def _run_cli(argv: list[str]) -> str:
    proc = subprocess.run(
        [str(MEDIA_PIPE_CLI), *argv],
        text=True,
        capture_output=True,
        timeout=1800,
    )
    if proc.returncode != 0:
        return _json_response(
            {
                "success": False,
                "error": proc.stderr.strip() or proc.stdout.strip() or "media-pipe failed",
                "returncode": proc.returncode,
            }
        )
    try:
        parsed = json.loads(proc.stdout)
    except json.JSONDecodeError:
        return _json_response({"success": True, "stdout": proc.stdout.strip()})
    if isinstance(parsed, dict):
        parsed.setdefault("success", True)
    return _json_response(parsed if isinstance(parsed, dict) else {"success": True, "result": parsed})


def _request_from_args(args: dict[str, Any]) -> dict[str, Any]:
    request = args.get("request") or args.get("request_json")
    if isinstance(request, str):
        request = json.loads(request)
    if not isinstance(request, dict):
        prompt = str(args.get("prompt") or "").strip()
        if not prompt:
            raise ValueError("request object or prompt is required")
        request = {
            "intent": args.get("intent") or "image",
            "workflow": args.get("workflow") or "generate",
            "strategy": args.get("strategy") or "single",
            "budget_policy": "approval_first",
            "brief": prompt,
            "inputs": args.get("inputs") or [],
            "constraints": args.get("constraints") or {},
        }
    bad = sorted(PROVIDER_SPECIFIC_FIELDS.intersection(request.keys()))
    if bad:
        raise ValueError(
            "Media Pipe requests must stay provider-neutral; remove: " + ", ".join(bad)
        )
    return request


def _paperclip_sync_enabled(args: dict[str, Any]) -> bool:
    if "paperclip_sync" in args:
        return bool(args.get("paperclip_sync"))
    return bool(os.environ.get("PAPERCLIP_ISSUE_ID") or os.environ.get("PAPERCLIP_COMPANY_ID"))


def _append_optional(argv: list[str], flag: str, value: Any) -> None:
    if isinstance(value, str) and value.strip():
        argv.extend([flag, value.strip()])


def _handle_quote(args: dict[str, Any], **_kw: Any) -> str:
    try:
        request = _request_from_args(args)
    except Exception as exc:
        return _json_response({"success": False, "error": str(exc), "error_type": "invalid_request"})

    with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False) as tmp:
        json.dump(request, tmp, ensure_ascii=False)
        tmp_path = tmp.name
    try:
        argv = ["quote", "--request", tmp_path]
        if _paperclip_sync_enabled(args):
            argv.append("--paperclip-sync")
            _append_optional(argv, "--paperclip-issue-id", args.get("paperclip_issue_id") or os.environ.get("PAPERCLIP_ISSUE_ID"))
            _append_optional(argv, "--paperclip-company-id", args.get("paperclip_company_id") or os.environ.get("PAPERCLIP_COMPANY_ID"))
            _append_optional(argv, "--paperclip-agent-id", args.get("paperclip_agent_id") or os.environ.get("PAPERCLIP_AGENT_ID"))
        return _run_cli(argv)
    finally:
        Path(tmp_path).unlink(missing_ok=True)


def _handle_run(args: dict[str, Any], **_kw: Any) -> str:
    quote_id = str(args.get("quote_id") or "").strip()
    if not quote_id:
        return _json_response({"success": False, "error": "quote_id is required"})
    return _run_cli(["run", quote_id])


def _handle_status(args: dict[str, Any], **_kw: Any) -> str:
    identifier = str(args.get("id") or args.get("run_id") or args.get("quote_id") or "").strip()
    if not identifier:
        return _json_response({"success": False, "error": "id is required"})
    return _run_cli(["status", identifier])


def _handle_ledger(args: dict[str, Any], **_kw: Any) -> str:
    if args.get("today", True):
        return _run_cli(["ledger", "--today"])
    return _json_response({"success": False, "error": "Only today=true is supported"})


MEDIA_PIPE_QUOTE_SCHEMA = {
    "name": "media_pipe_quote",
    "description": (
        "Create a provider-neutral Media Pipe quote. The agent supplies intent, "
        "workflow, brief, constraints, and strategy; providers/models are chosen by Media Pipe."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "request": {
                "type": "object",
                "description": "Provider-neutral MediaRequest JSON object.",
            },
            "request_json": {"type": "string", "description": "Provider-neutral MediaRequest JSON string."},
            "prompt": {"type": "string", "description": "Convenience brief when no request object is supplied."},
            "intent": {"type": "string", "enum": ["image", "video", "audio"], "default": "image"},
            "workflow": {
                "type": "string",
                "enum": ["generate", "edit", "image_to_video", "product", "marketplace", "soul_id", "tts", "music"],
                "default": "generate",
            },
            "strategy": {
                "type": "string",
                "enum": ["single", "fallback", "fanout", "race", "shadow"],
                "default": "single",
            },
            "constraints": {"type": "object"},
            "inputs": {"type": "array", "items": {"type": "object"}},
            "paperclip_sync": {"type": "boolean"},
            "paperclip_issue_id": {"type": "string"},
            "paperclip_company_id": {"type": "string"},
            "paperclip_agent_id": {"type": "string"},
        },
    },
}

MEDIA_PIPE_RUN_SCHEMA = {
    "name": "media_pipe_run",
    "description": "Run an approved Media Pipe quote. Approval can be local or linked Paperclip approval.",
    "parameters": {
        "type": "object",
        "properties": {"quote_id": {"type": "string"}},
        "required": ["quote_id"],
    },
}

MEDIA_PIPE_STATUS_SCHEMA = {
    "name": "media_pipe_status",
    "description": "Fetch a Media Pipe quote or run by id.",
    "parameters": {
        "type": "object",
        "properties": {
            "id": {"type": "string"},
            "quote_id": {"type": "string"},
            "run_id": {"type": "string"},
        },
    },
}

MEDIA_PIPE_LEDGER_SCHEMA = {
    "name": "media_pipe_ledger",
    "description": "Show today's Media Pipe ledger summary.",
    "parameters": {
        "type": "object",
        "properties": {"today": {"type": "boolean", "default": True}},
    },
}


def register(ctx: Any) -> None:
    ctx.register_tool(
        name="media_pipe_quote",
        toolset="media_pipe",
        schema=MEDIA_PIPE_QUOTE_SCHEMA,
        handler=_handle_quote,
        check_fn=_available,
        requires_env=[],
        description=MEDIA_PIPE_QUOTE_SCHEMA["description"],
        emoji="MP",
    )
    ctx.register_tool(
        name="media_pipe_run",
        toolset="media_pipe",
        schema=MEDIA_PIPE_RUN_SCHEMA,
        handler=_handle_run,
        check_fn=_available,
        requires_env=[],
        description=MEDIA_PIPE_RUN_SCHEMA["description"],
        emoji="MP",
    )
    ctx.register_tool(
        name="media_pipe_status",
        toolset="media_pipe",
        schema=MEDIA_PIPE_STATUS_SCHEMA,
        handler=_handle_status,
        check_fn=_available,
        requires_env=[],
        description=MEDIA_PIPE_STATUS_SCHEMA["description"],
        emoji="MP",
    )
    ctx.register_tool(
        name="media_pipe_ledger",
        toolset="media_pipe",
        schema=MEDIA_PIPE_LEDGER_SCHEMA,
        handler=_handle_ledger,
        check_fn=_available,
        requires_env=[],
        description=MEDIA_PIPE_LEDGER_SCHEMA["description"],
        emoji="MP",
    )
