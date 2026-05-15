"""Tests for gateway.hub.pipeline.run_pipeline (full 8-step pipeline + audit-push)."""
from unittest.mock import AsyncMock

import pytest

from gateway.hub.cooldown import reset_cooldown_state
from gateway.hub.dedupe import reset_dedupe_state
from gateway.hub.flapping import reset_flapping_state
from gateway.hub.pipeline import run_pipeline
from gateway.hub.registry_client import RegistryClient
from gateway.hub.schemas import NotificationIntent


class FakeRegistry(RegistryClient):
    """Test double — overrides every network method touched by the pipeline.

    post_audit is an AsyncMock so tests can spy on the call count.
    """

    def __init__(
        self,
        source: dict | None = None,
        topic: dict | None = None,
        rules: list[dict] | None = None,
        channel_set: dict | None = None,
    ) -> None:
        # Skip super().__init__ — no httpx client needed.
        self._source = source
        self._topic = topic
        self._rules = rules if rules is not None else []
        self._channel_set = channel_set
        self.post_audit = AsyncMock(return_value=None)

    async def get_source(self, slug: str, token_hash: str | None = None) -> dict | None:
        return self._source

    async def get_topic(self, slug: str) -> dict | None:
        return self._topic

    async def get_rules(
        self,
        topic: str,
        audience: str,
        severity: str,
        urgency: str = "none",
        actionability: str = "info",
        source_id: str | None = None,
    ) -> list[dict]:
        return self._rules

    async def get_channel_set_expanded(self, channel_set_id: str) -> dict | None:
        return self._channel_set


@pytest.fixture(autouse=True)
def _reset_pipeline_state():
    """Reset in-memory pipeline state before AND after each test for isolation."""
    reset_dedupe_state()
    reset_cooldown_state()
    reset_flapping_state()
    yield
    reset_dedupe_state()
    reset_cooldown_state()
    reset_flapping_state()


def make_intent(**overrides) -> NotificationIntent:
    defaults = dict(
        source_slug="paperclip",
        topic="ops.deploy",
        severity="info",
        urgency="none",
        actionability="info",
        audience="marco",
        title="Hello",
        body="World",
    )
    defaults.update(overrides)
    return NotificationIntent(**defaults)


def make_source(**overrides) -> dict:
    defaults = {
        "id": "src_1",
        "slug": "paperclip",
        "enabled": True,
        "severity_max": "warn",
    }
    defaults.update(overrides)
    return defaults


def make_topic(**overrides) -> dict:
    defaults = {
        "id": "top_1",
        "slug": "ops.deploy",
        "default_audience_id": "aud_marco",
        "default_audience_slug": "marco",
    }
    defaults.update(overrides)
    return defaults


@pytest.mark.asyncio
async def test_happy_path_returns_delivered_and_pushes_audits() -> None:
    """Valid intent → all 8 steps run → status=delivered, 7+ audit calls."""
    rule = {"id": "rule_1", "slug": "ops-info-default", "priority": 3, "channel_set_id": "cs_1"}
    channel_set = {
        "id": "cs_1",
        "members": [
            {"position": 1, "channel": {"id": "ch_inbox", "type": "inbox_mc"}},
        ],
    }
    registry = FakeRegistry(
        source=make_source(),
        topic=make_topic(),
        rules=[rule],
        channel_set=channel_set,
    )
    result = await run_pipeline(make_intent(), source_token_hash="h", registry=registry)

    assert result.status == "delivered"
    assert result.rule_matched == "ops-info-default"
    assert len(result.deliveries) == 1
    assert result.deliveries[0].status == "delivered"
    assert result.deliveries[0].channel_id == "ch_inbox"
    # Happy-path audits: received, validated, dedupe_passed, cooldown_passed,
    # flapping_passed, quiet_passed, rule_matched, sent → 8 calls.
    assert registry.post_audit.await_count >= 7
    # Spot-check: action="received" was issued.
    actions = [call.kwargs.get("action") for call in registry.post_audit.await_args_list]
    assert "received" in actions
    assert "validated" in actions
    assert "rule_matched" in actions
    assert "sent" in actions


@pytest.mark.asyncio
async def test_source_auth_fail_returns_failed_with_error_code() -> None:
    """Unknown source / bad token → status=failed, error contains code."""
    registry = FakeRegistry(source=None, topic=None)
    result = await run_pipeline(make_intent(), source_token_hash="bad", registry=registry)

    assert result.status == "failed"
    assert result.error is not None
    assert "auth_failed" in result.error
    # No audit-push when validate_and_lookup raises.
    assert registry.post_audit.await_count == 0


@pytest.mark.asyncio
async def test_dedup_hit_returns_suppressed_dedup() -> None:
    """Second event with same dedupe_key inside window → suppressed_dedup."""
    rule = {"id": "rule_1", "slug": "ops-info", "priority": 3, "channel_set_id": "cs_1"}
    channel_set = {
        "id": "cs_1",
        "members": [{"position": 1, "channel": {"id": "ch_inbox", "type": "inbox_mc"}}],
    }
    registry = FakeRegistry(
        source=make_source(),
        topic=make_topic(),
        rules=[rule],
        channel_set=channel_set,
    )

    # First call — primes the dedup-window.
    first = await run_pipeline(
        make_intent(dedupe_key="dedup-A"),
        source_token_hash="h",
        registry=registry,
    )
    assert first.status == "delivered"

    # Second call with the same dedupe_key — should suppress.
    second = await run_pipeline(
        make_intent(dedupe_key="dedup-A"),
        source_token_hash="h",
        registry=registry,
    )
    assert second.status == "suppressed_dedup"
    assert second.suppression is not None
    assert second.suppression["reason"] == "dedupe_window"
    assert second.suppression["count"] == 2

    # dedupe_hit audit was emitted on the second call.
    actions = [call.kwargs.get("action") for call in registry.post_audit.await_args_list]
    assert "dedupe_hit" in actions


@pytest.mark.asyncio
async def test_no_rule_match_returns_suppressed_no_rule() -> None:
    """Empty rules list → status=suppressed_no_rule, no_rule_match audit."""
    registry = FakeRegistry(
        source=make_source(),
        topic=make_topic(),
        rules=[],
        channel_set=None,
    )
    result = await run_pipeline(make_intent(), source_token_hash="h", registry=registry)

    assert result.status == "suppressed_no_rule"
    assert result.deliveries == []
    actions = [call.kwargs.get("action") for call in registry.post_audit.await_args_list]
    assert "no_rule_match" in actions
    # No "sent" / "failed" delivery audits.
    assert "sent" not in actions


@pytest.mark.asyncio
async def test_multi_channel_partial_failure_returns_partial() -> None:
    """One channel succeeds, one fails (unknown adapter) → status=partial."""
    rule = {"id": "rule_1", "slug": "ops-info", "priority": 3, "channel_set_id": "cs_1"}
    channel_set = {
        "id": "cs_1",
        "members": [
            {"position": 1, "channel": {"id": "ch_inbox", "type": "inbox_mc"}},
            {"position": 2, "channel": {"id": "ch_pigeon", "type": "carrier-pigeon"}},
        ],
    }
    registry = FakeRegistry(
        source=make_source(),
        topic=make_topic(),
        rules=[rule],
        channel_set=channel_set,
    )
    result = await run_pipeline(make_intent(), source_token_hash="h", registry=registry)

    assert result.status == "partial"
    assert len(result.deliveries) == 2
    statuses = {d.status for d in result.deliveries}
    assert "delivered" in statuses
    assert "failed" in statuses

    # Audits include both "sent" (delivered) and "failed".
    actions = [call.kwargs.get("action") for call in registry.post_audit.await_args_list]
    assert "sent" in actions
    assert "failed" in actions


@pytest.mark.asyncio
async def test_severity_too_high_returns_failed() -> None:
    """Severity above source.severity_max → status=failed with severity_too_high code."""
    registry = FakeRegistry(
        source=make_source(severity_max="info"),
        topic=make_topic(),
    )
    result = await run_pipeline(
        make_intent(severity="crit"),
        source_token_hash="h",
        registry=registry,
    )
    assert result.status == "failed"
    assert "severity_too_high" in result.error
    assert registry.post_audit.await_count == 0


@pytest.mark.asyncio
async def test_all_channels_fail_returns_failed_status() -> None:
    """Every channel fails (unknown adapter) → status=failed (not partial)."""
    rule = {"id": "rule_1", "slug": "ops-info", "priority": 3, "channel_set_id": "cs_1"}
    channel_set = {
        "id": "cs_1",
        "members": [
            {"position": 1, "channel": {"id": "ch_pigeon1", "type": "carrier-pigeon"}},
            {"position": 2, "channel": {"id": "ch_pigeon2", "type": "fax"}},
        ],
    }
    registry = FakeRegistry(
        source=make_source(),
        topic=make_topic(),
        rules=[rule],
        channel_set=channel_set,
    )
    result = await run_pipeline(make_intent(), source_token_hash="h", registry=registry)

    assert result.status == "failed"
    assert all(d.status == "failed" for d in result.deliveries)


# ===== H1: audit-push failure must NOT abort the pipeline =====

import httpx


@pytest.mark.asyncio
async def test_audit_push_5xx_during_dispatch_does_not_abort_pipeline() -> None:
    """H1: if MC returns 5xx on a post_audit call AFTER dispatch, the pipeline
    must still complete and return the delivered notification result.

    Before the fix, an httpx.HTTPStatusError raised from post_audit (after step 2)
    propagated out of run_pipeline → FastAPI → 500 to caller, while the
    notification had actually been delivered (orphaned audit log).
    """
    rule = {"id": "rule_1", "slug": "ops-info", "priority": 3, "channel_set_id": "cs_1"}
    channel_set = {
        "id": "cs_1",
        "members": [{"position": 1, "channel": {"id": "ch_inbox_1", "type": "inbox_mc"}}],
    }
    registry = FakeRegistry(
        source=make_source(),
        topic=make_topic(),
        rules=[rule],
        channel_set=channel_set,
    )
    # Replace post_audit with a mock that raises like httpx.raise_for_status would
    boom_request = httpx.Request("POST", "http://mc/api/audit")
    boom_response = httpx.Response(503, request=boom_request)
    registry.post_audit = AsyncMock(
        side_effect=httpx.HTTPStatusError("MC down", request=boom_request, response=boom_response)
    )

    result = await run_pipeline(make_intent(), source_token_hash="h", registry=registry)

    # Pipeline completed; notification was delivered to the inbox adapter (NoOp)
    assert result.status == "delivered"
    # post_audit was called many times — all raised, all were swallowed
    assert registry.post_audit.await_count >= 4


@pytest.mark.asyncio
async def test_audit_push_5xx_during_step2_does_not_abort_pipeline() -> None:
    """Same protection applies to the audit-pushes inside the validate_and_lookup
    try/except. PipelineError catches PipelineError, NOT httpx errors — so
    the audits in steps 1-2 must also be wrapped.
    """
    rule = {"id": "rule_1", "slug": "ops-info", "priority": 3, "channel_set_id": "cs_1"}
    channel_set = {
        "id": "cs_1",
        "members": [{"position": 1, "channel": {"id": "ch_inbox_1", "type": "inbox_mc"}}],
    }
    registry = FakeRegistry(
        source=make_source(),
        topic=make_topic(),
        rules=[rule],
        channel_set=channel_set,
    )
    boom_request = httpx.Request("POST", "http://mc/api/audit")
    boom_response = httpx.Response(503, request=boom_request)
    registry.post_audit = AsyncMock(
        side_effect=httpx.HTTPStatusError("MC down", request=boom_request, response=boom_response)
    )

    # No exception should escape from run_pipeline
    result = await run_pipeline(make_intent(), source_token_hash="h", registry=registry)
    assert result.status == "delivered"


# ===== H2: Cooldown gate must actually block per-channel resend =====

@pytest.mark.asyncio
async def test_cooldown_blocks_repeat_send_to_same_channel() -> None:
    """H2: dispatch records cooldown for channel. Subsequent send to same
    source+topic+channel within cooldown window must be suppressed at dispatch.
    Before the fix, Step 4 declared 'cooldown_check_passed' but never invoked
    is_in_cooldown, so spamming the same channel was unbounded.
    """
    from gateway.hub.cooldown import reset_cooldown_state as _reset_cd
    _reset_cd()
    rule = {"id": "rule_1", "slug": "ops-info", "priority": 3, "channel_set_id": "cs_1"}
    channel_set = {
        "id": "cs_1",
        "members": [{"position": 1, "channel": {"id": "ch_inbox_repeat", "type": "inbox_mc"}}],
    }
    # First send delivers + records cooldown
    registry1 = FakeRegistry(
        source=make_source(),
        topic=make_topic(),
        rules=[rule],
        channel_set=channel_set,
    )
    intent1 = make_intent(dedupe_key="key-1")
    result1 = await run_pipeline(intent1, source_token_hash="h", registry=registry1)
    assert result1.status == "delivered"

    # Second send within cooldown window — dispatch must be suppressed for the
    # channel (status=failed with cooldown_active error from AdapterResult).
    # Reset dedupe/flapping so they don't short-circuit before dispatch.
    from gateway.hub.dedupe import reset_dedupe_state as _reset_dd
    from gateway.hub.flapping import reset_flapping_state as _reset_fl
    _reset_dd()
    _reset_fl()
    registry2 = FakeRegistry(
        source=make_source(),
        topic=make_topic(),
        rules=[rule],
        channel_set=channel_set,
    )
    intent2 = make_intent(dedupe_key="key-2")
    result2 = await run_pipeline(intent2, source_token_hash="h", registry=registry2)
    # All channels suppressed → aggregate "failed"
    assert result2.status == "failed"
    assert result2.deliveries[0].status == "failed"
    # Confirm the cooldown_block audit was pushed
    audit_actions = [c.kwargs.get("action") for c in registry2.post_audit.await_args_list]
    assert "cooldown_block" in audit_actions
