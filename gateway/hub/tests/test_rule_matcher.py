"""Tests for gateway.hub.rule_matcher (Step 7)."""
import pytest

from gateway.hub.registry_client import RegistryClient
from gateway.hub.rule_matcher import find_matching_rule


class FakeRegistry(RegistryClient):
    """Test double — overrides get_rules + get_channel_set_expanded."""

    def __init__(
        self,
        rules: list[dict] | None = None,
        channel_set: dict | None = None,
    ) -> None:
        # Skip super().__init__ to avoid creating an httpx client we don't need.
        self._rules = rules if rules is not None else []
        self._channel_set = channel_set
        self.get_rules_calls: list[dict] = []
        self.get_channel_set_calls: list[str] = []

    async def get_rules(
        self,
        topic: str,
        audience: str,
        severity: str,
        urgency: str = "none",
        actionability: str = "info",
        source_id: str | None = None,
    ) -> list[dict]:
        self.get_rules_calls.append({
            "topic": topic,
            "audience": audience,
            "severity": severity,
            "urgency": urgency,
            "actionability": actionability,
            "source_id": source_id,
        })
        return self._rules

    async def get_channel_set_expanded(self, channel_set_id: str) -> dict | None:
        self.get_channel_set_calls.append(channel_set_id)
        return self._channel_set


@pytest.mark.asyncio
async def test_find_matching_rule_no_rules_returns_none_pair() -> None:
    registry = FakeRegistry(rules=[])
    rule, channel_set = await find_matching_rule(
        registry,
        topic="ops.deploy",
        audience_slug="marco",
        severity="info",
    )
    assert rule is None
    assert channel_set is None
    # Channel-set lookup must not happen when no rule matches.
    assert registry.get_channel_set_calls == []


@pytest.mark.asyncio
async def test_find_matching_rule_single_rule_returns_with_channel_set() -> None:
    rule = {"id": "rule_1", "priority": 3, "channel_set_id": "cs_1"}
    expanded = {"id": "cs_1", "members": [{"channel": {"type": "inbox_mc"}}]}
    registry = FakeRegistry(rules=[rule], channel_set=expanded)
    matched, channel_set = await find_matching_rule(
        registry,
        topic="ops.deploy",
        audience_slug="marco",
        severity="info",
    )
    assert matched == rule
    assert channel_set == expanded
    assert registry.get_channel_set_calls == ["cs_1"]


@pytest.mark.asyncio
async def test_find_matching_rule_multiple_rules_picks_lowest_priority_number() -> None:
    rules = [
        {"id": "rule_global", "priority": 5, "channel_set_id": "cs_global"},
        {"id": "rule_safety", "priority": 1, "channel_set_id": "cs_safety"},
        {"id": "rule_topic", "priority": 3, "channel_set_id": "cs_topic"},
    ]
    expanded = {"id": "cs_safety", "members": []}
    registry = FakeRegistry(rules=rules, channel_set=expanded)
    matched, channel_set = await find_matching_rule(
        registry,
        topic="home.smoke",
        audience_slug="haushalt",
        severity="crit",
    )
    assert matched["id"] == "rule_safety"
    assert matched["priority"] == 1
    assert channel_set == expanded
    # Only the winning rule's channel-set is fetched.
    assert registry.get_channel_set_calls == ["cs_safety"]


@pytest.mark.asyncio
async def test_find_matching_rule_passes_through_all_params() -> None:
    rule = {"id": "rule_1", "priority": 2, "channel_set_id": "cs_1"}
    registry = FakeRegistry(rules=[rule], channel_set={"id": "cs_1", "members": []})
    await find_matching_rule(
        registry,
        topic="ops.deploy",
        audience_slug="marco",
        severity="warn",
        urgency="ack",
        actionability="actionable",
        source_id="src_42",
    )
    assert len(registry.get_rules_calls) == 1
    call = registry.get_rules_calls[0]
    assert call["topic"] == "ops.deploy"
    assert call["audience"] == "marco"
    assert call["severity"] == "warn"
    assert call["urgency"] == "ack"
    assert call["actionability"] == "actionable"
    assert call["source_id"] == "src_42"
