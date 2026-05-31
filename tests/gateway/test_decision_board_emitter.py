import json
import os
from unittest.mock import patch, MagicMock

import pytest


@pytest.fixture
def _emitter(monkeypatch):
    """Reload emitter with a deterministic secret env."""
    monkeypatch.setenv("DECISION_BOARD_HERMES_SECRET", "test-secret")
    import importlib
    from gateway.hub import decision_board_emitter as ae
    importlib.reload(ae)
    return ae


def test_emit_approval_signs_and_posts(_emitter):
    ae = _emitter
    with patch.object(ae.httpx, "post") as post_mock:
        post_mock.return_value = MagicMock(status_code=201)
        ok = ae.emit_approval_to_decision_board(
            session_key="sess-1",
            command="rm -rf /",
            pattern_key="rm-root",
            description="rm -rf detected",
        )
        assert ok is True
        call = post_mock.call_args
        headers = call.kwargs["headers"]
        assert "X-Decision-Signature" in headers
        assert "X-Decision-Timestamp" in headers
        body = json.loads(call.kwargs["content"])
        assert body["source"]["system"] == "hermes"
        assert body["question"] == "rm -rf detected"
        assert body["type"] == "gate"
        assert body["risk_class"] == "high"
        assert body["blocking"] is True


def test_emit_skipped_when_secret_missing(monkeypatch):
    monkeypatch.delenv("DECISION_BOARD_HERMES_SECRET", raising=False)
    import importlib
    from gateway.hub import decision_board_emitter as ae
    importlib.reload(ae)
    ok = ae.emit_approval_to_decision_board(
        session_key="x", command="ls", pattern_key="p", description="d",
    )
    assert ok is False


def test_emit_severed_skips_post(_emitter, monkeypatch):
    """Phase-6b Step-7: severed → no MC ingest POST, returns False (drop).

    Secret is set (via _emitter), so only the severance guard stops the post.
    """
    monkeypatch.setenv("HUB_MC_WRITE_SEVERED", "1")
    ae = _emitter
    with patch.object(ae.httpx, "post") as post_mock:
        ok = ae.emit_approval_to_decision_board(
            session_key="x", command="c", pattern_key="p", description="d",
        )
    assert ok is False
    assert post_mock.call_count == 0


def test_emit_returns_false_on_5xx(_emitter):
    ae = _emitter
    with patch.object(ae.httpx, "post") as post_mock:
        post_mock.return_value = MagicMock(status_code=500, text="boom")
        ok = ae.emit_approval_to_decision_board(
            session_key="x", command="c", pattern_key="p", description="d",
        )
        assert ok is False


def test_emit_returns_false_on_network_exception(_emitter):
    ae = _emitter
    with patch.object(ae.httpx, "post") as post_mock:
        post_mock.side_effect = Exception("connection refused")
        ok = ae.emit_approval_to_decision_board(
            session_key="x", command="c", pattern_key="p", description="d",
        )
        assert ok is False


def test_evidence_hash_is_stable(_emitter):
    ae = _emitter
    # Two emits with same inputs should produce same evidence.hash
    with patch.object(ae.httpx, "post") as post_mock:
        post_mock.return_value = MagicMock(status_code=201)
        for _ in range(2):
            ae.emit_approval_to_decision_board(
                session_key="s", command="c", pattern_key="p", description="d",
            )
        h1 = json.loads(post_mock.call_args_list[0].kwargs["content"])["evidence"]["hash"]
        h2 = json.loads(post_mock.call_args_list[1].kwargs["content"])["evidence"]["hash"]
        assert h1 == h2
