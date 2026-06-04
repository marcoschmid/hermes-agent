"""Regression: the interactive gateway turn must never go fully silent.

Before this fix, ``gateway/run.py`` built the empty-response fallback as::

    error_msg = f"⚠️ {result['error']}" if result.get("error") else ""

so a turn that produced no ``final_response`` AND carried no ``error`` (e.g. a
Codex/Responses backend HTTP 4xx that exhausted retries/fallback without a
populated ``error`` field) returned an empty string — the bot stayed stumm.
The background-task path already had a "(No response generated)" safety net;
the interactive path did not. ``_finalize_empty_agent_reply`` closes that gap.
"""
from gateway.run import _finalize_empty_agent_reply


def test_error_present_is_surfaced_to_user():
    # Arrange
    result = {"final_response": None, "error": "API call failed after 3 retries: HTTP 400"}
    # Act
    reply = _finalize_empty_agent_reply(result)
    # Assert
    assert "HTTP 400" in reply
    assert reply.startswith("⚠️")


def test_empty_error_still_returns_non_empty_reply():
    # The exact silent-bot case: no response, no error field.
    result = {"final_response": None, "completed": False}
    reply = _finalize_empty_agent_reply(result)
    assert reply, "must never return an empty reply (would be a silent bot)"
    assert reply.strip() != ""
    assert reply.startswith("⚠️")


def test_empty_string_error_treated_as_no_error():
    result = {"final_response": "", "error": ""}
    reply = _finalize_empty_agent_reply(result)
    assert reply, "falsy error must still yield a non-empty generic reply"


def test_non_dict_result_is_handled_safely():
    assert _finalize_empty_agent_reply(None)
    assert _finalize_empty_agent_reply({})


def test_does_not_invent_secrets():
    # The helper must only pass the (already-redacted) error through verbatim,
    # never inject anything token-like of its own on the no-error path.
    reply = _finalize_empty_agent_reply({"final_response": None})
    lowered = reply.lower()
    for needle in ("sk-", "bearer ", "token=", "api_key"):
        assert needle not in lowered
