from __future__ import annotations

import importlib
import json


media_pipe_plugin = importlib.import_module("plugins.media_pipe")


class FakeContext:
    def __init__(self):
        self.tools = {}

    def register_tool(self, **kwargs):
        self.tools[kwargs["name"]] = kwargs


def test_register_exposes_media_pipe_tools():
    ctx = FakeContext()

    media_pipe_plugin.register(ctx)

    assert sorted(ctx.tools) == [
        "media_pipe_ledger",
        "media_pipe_quote",
        "media_pipe_run",
        "media_pipe_status",
    ]
    assert {tool["toolset"] for tool in ctx.tools.values()} == {"media_pipe"}


def test_quote_rejects_provider_specific_request_fields():
    result = json.loads(
        media_pipe_plugin._handle_quote(
            {
                "request": {
                    "intent": "image",
                    "workflow": "generate",
                    "brief": "test",
                    "provider": "higgsfield",
                }
            }
        )
    )

    assert result["success"] is False
    assert result["error_type"] == "invalid_request"
    assert "provider-neutral" in result["error"]


def test_quote_calls_media_pipe_with_paperclip_context(monkeypatch):
    captured = {}

    def fake_run_cli(argv):
        captured["argv"] = argv
        request_path = argv[argv.index("--request") + 1]
        with open(request_path, encoding="utf-8") as fh:
            captured["request"] = json.load(fh)
        return json.dumps({"success": True, "quote_id": "q_test"})

    monkeypatch.setattr(media_pipe_plugin, "_run_cli", fake_run_cli)
    monkeypatch.setenv("PAPERCLIP_ISSUE_ID", "44444444-4444-4444-4444-444444444444")
    monkeypatch.setenv("PAPERCLIP_AGENT_ID", "55555555-5555-5555-5555-555555555555")

    result = json.loads(
        media_pipe_plugin._handle_quote(
            {
                "request": {
                    "intent": "image",
                    "workflow": "generate",
                    "brief": "quiet product shot",
                    "strategy": "single",
                }
            }
        )
    )

    assert result["success"] is True
    assert captured["request"]["brief"] == "quiet product shot"
    assert "--paperclip-sync" in captured["argv"]
    assert captured["argv"][captured["argv"].index("--paperclip-issue-id") + 1] == (
        "44444444-4444-4444-4444-444444444444"
    )
    assert captured["argv"][captured["argv"].index("--paperclip-agent-id") + 1] == (
        "55555555-5555-5555-5555-555555555555"
    )
