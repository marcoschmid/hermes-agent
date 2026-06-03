"""Tests for the codex tool-call-leak silent-bot fix (2026-06-03).

Covers: aux model off rejected *-codex slug, serial tool calls + gpt-5.5
payload sanitizer in the request builder, and Harmony tool-call-leak recovery.
"""
import json


def test_codex_aux_model_is_chatgpt_account_supported():
    from agent.auxiliary_client import _CODEX_AUX_MODEL
    assert "-codex" not in _CODEX_AUX_MODEL  # ChatGPT-account codex rejects *-codex
    assert _CODEX_AUX_MODEL in {"gpt-5.5", "gpt-5.4", "gpt-5.4-mini"}


def _build(model, **extra):
    from agent.transports.codex import ResponsesApiTransport
    params = dict(
        is_codex_backend=True,
        reasoning_config={"effort": "xhigh", "enabled": True},
    )
    params.update(extra)
    return ResponsesApiTransport().build_kwargs(
        model=model,
        messages=[{"role": "user", "content": "hi"}],
        tools=[],
        **params,
    )


def test_serial_tool_calls_on_codex():
    kw = _build("gpt-5.5")
    assert kw["parallel_tool_calls"] is False


def test_gpt55_payload_sanitized():
    kw = _build("gpt-5.5")
    # encrypted-reasoning replay removed, store=true, effort de-escalated
    assert "reasoning.encrypted_content" not in kw.get("include", [])
    assert kw.get("store") is True
    assert kw.get("reasoning", {}).get("effort") not in {"high", "xhigh"}


def test_gpt54_payload_not_sanitized():
    # Only gpt-5.5 is the stricter model; gpt-5.4 keeps the normal codex payload.
    kw = _build("gpt-5.4")
    assert "reasoning.encrypted_content" in kw.get("include", [])
    assert kw.get("store") is False


def test_recover_leaked_harmony_tool_call():
    from agent.codex_responses_adapter import _recover_leaked_tool_calls
    text = 'I will read it. assistant to=functions.read_file {"path": "/tmp/x"} now.'
    calls = _recover_leaked_tool_calls(text)
    assert len(calls) == 1
    assert calls[0].function.name == "read_file"
    assert json.loads(calls[0].function.arguments) == {"path": "/tmp/x"}
    assert calls[0].type == "function"
    assert calls[0].call_id


def test_recover_skips_unparseable_leak():
    from agent.codex_responses_adapter import _recover_leaked_tool_calls
    # Degenerate/hallucinated leak with no valid JSON object → no recovery.
    text = "to=functions.shell ; then parse JSON robustly we'll call terminal"
    assert _recover_leaked_tool_calls(text) == []
