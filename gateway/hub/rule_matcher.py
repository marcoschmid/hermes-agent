"""Rule matcher — Step 7 of pipeline.

Calls registry.get_rules with the event params, picks the highest-priority
rule (priority=1 wins). Returns the rule + its expanded channel-set.
"""
from typing import Optional

from gateway.hub.registry_client import RegistryClient


async def find_matching_rule(
    registry: RegistryClient,
    topic: str,
    audience_slug: str,
    severity: str,
    urgency: str = "none",
    actionability: str = "info",
    source_id: Optional[str] = None,
) -> tuple[Optional[dict], Optional[dict]]:
    """Returns (rule, expanded_channel_set) or (None, None) if no rule matches."""
    rules = await registry.get_rules(
        topic=topic,
        audience=audience_slug,
        severity=severity,
        urgency=urgency,
        actionability=actionability,
        source_id=source_id,
    )
    if not rules:
        return None, None

    # Sort by priority asc; first wins (priority=1=Safety, 5=Global)
    sorted_rules = sorted(rules, key=lambda r: r["priority"])
    rule = sorted_rules[0]
    channel_set = await registry.get_channel_set_expanded(rule["channel_set_id"])
    return rule, channel_set
