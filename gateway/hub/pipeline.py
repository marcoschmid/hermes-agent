"""Hub dispatch pipeline.

Implements the 8-step pipeline for inbound notifications:
  1. Schema validation (via Pydantic, before pipeline call)
  2. Registry lookup (source, topic, audience, severity_max)
  3. Dedup-window check (in-memory, Phase 1)
  4. Cooldown check (per-channel, deferred to dispatch)
  5. Flapping detection (state-change counter)
  6. Quiet-Hours (Phase 1: skipped — no policies seeded)
  7. Rule-Match (registry-driven, priority asc wins)
  8. Dispatch via channel-set adapters

Each step pushes an audit-event to the MC registry. A pipeline rejection in
steps 1-2 surfaces as PipelineError; everything past Step 2 returns a
NotificationResult with a suppressed_* / failed status.
"""
from dataclasses import dataclass

from gateway.hub.adapter_registry import dispatch_to_channel_set
from gateway.hub.cooldown import record_cooldown
from gateway.hub.dedupe import check_dedup
from gateway.hub.flapping import is_flapping, record_state_change
from gateway.hub.registry_client import RegistryClient
from gateway.hub.rule_matcher import find_matching_rule
from gateway.hub.schemas import (
    DeliveryResult,
    NotificationIntent,
    NotificationResult,
)


SEVERITY_ORDER = {"debug": 0, "info": 1, "notice": 2, "warn": 3, "error": 4, "crit": 5}


@dataclass
class PipelineError(Exception):
    """Pipeline rejection — caller should surface as HTTP error."""
    status_code: int
    error_code: str
    message: str

    def __str__(self) -> str:
        return f"{self.error_code}: {self.message}"


@dataclass
class ResolvedContext:
    """Resolved registry data after Step 2."""
    source: dict
    topic: dict
    audience_slug: str  # ALWAYS a bare slug (e.g. "marco"), never an aud_-id.
    # MC's matchRules accepts both, but we keep the slug semantics here so the
    # cache-key in registry_client stays slug-stable across senders.


async def validate_and_lookup(
    intent: NotificationIntent,
    source_token_hash: str,
    registry: RegistryClient,
) -> ResolvedContext:
    """Steps 1-2: Schema is already validated (Pydantic). Now lookup + checks."""
    # Step 2a: Source-Lookup mit Token-Validation
    source = await registry.get_source(intent.source_slug, token_hash=source_token_hash)
    if source is None:
        raise PipelineError(401, "auth_failed", f"Unknown source or invalid token: {intent.source_slug}")
    if not source.get("enabled", True):
        raise PipelineError(403, "source_disabled", f"Source {intent.source_slug} is disabled")

    # Step 2b: Severity-Max-Check
    severity_max = source.get("severity_max", "warn")
    if SEVERITY_ORDER[intent.severity] > SEVERITY_ORDER[severity_max]:
        raise PipelineError(
            403,
            "severity_too_high",
            f"Source {intent.source_slug} max severity is {severity_max}, got {intent.severity}",
        )

    # Step 2c: Topic-Lookup
    topic = await registry.get_topic(intent.topic)
    if topic is None:
        raise PipelineError(404, "topic_not_found", f"Unknown topic: {intent.topic}")

    # Step 2d: Audience-Resolution — ALWAYS produces a bare slug.
    #
    # `intent.audience` is a slug per schema (e.g. "family"). The topic
    # endpoint now joins audiences and surfaces `default_audience_slug`
    # alongside `default_audience_id`, so we can fall back without a
    # second registry round-trip. Falling back to `default_audience_id`
    # would push an aud_-prefixed string through the rest of the
    # pipeline, which used to surface as `suppressed_no_rule` because
    # MC's rule-store is keyed on slug-resolved audience-ids.
    intent_audience = intent.audience  # may be None → fall back to topic default
    if intent_audience is not None:
        audience_slug = intent_audience
    else:
        topic_default_audience_slug = topic.get("default_audience_slug")
        if not topic_default_audience_slug:
            raise PipelineError(
                400,
                "audience_missing",
                "No audience specified and topic has no default",
            )
        audience_slug = topic_default_audience_slug

    return ResolvedContext(source=source, topic=topic, audience_slug=audience_slug)


async def run_pipeline(
    intent: NotificationIntent,
    source_token_hash: str,
    registry: RegistryClient,
) -> NotificationResult:
    """Run all 8 steps. Push audit to MC after each step.

    event_id is None in Phase 1 — MC owns event-id assignment via createEvent.
    The audit-stream is keyed off (source, topic, channel) instead.
    """
    # Step 1+2: Schema valid (Pydantic) + Registry-Lookup
    try:
        ctx = await validate_and_lookup(intent, source_token_hash, registry)
        await registry.post_audit(
            event_id=None, actor="hermes-dispatcher", action="received",
            entity_type="source", entity_id=ctx.source["id"],
        )
        await registry.post_audit(
            event_id=None, actor="hermes-dispatcher", action="validated",
            entity_type="topic", entity_id=ctx.topic["id"],
        )
    except PipelineError as e:
        return NotificationResult(status="failed", error=f"{e.error_code}: {e.message}")

    # Step 3: Dedup
    dedup = check_dedup(ctx.source["id"], ctx.topic["id"], intent.dedupe_key)
    if dedup.is_duplicate:
        await registry.post_audit(
            event_id=None, actor="hermes-dispatcher", action="dedupe_hit",
            reason=f"count={dedup.count}",
        )
        return NotificationResult(
            status="suppressed_dedup",
            suppression={
                "reason": "dedupe_window",
                "count": dedup.count,
                "first_seen_at": dedup.first_seen_at.isoformat() if dedup.first_seen_at else None,
                "suppressed_until": dedup.suppressed_until.isoformat() if dedup.suppressed_until else None,
            },
        )
    await registry.post_audit(
        event_id=None, actor="hermes-dispatcher", action="dedupe_check_passed",
    )

    # Step 4: Cooldown — checked per-channel, deferred to dispatch step (channels not known yet)
    await registry.post_audit(
        event_id=None, actor="hermes-dispatcher", action="cooldown_check_passed",
    )

    # Step 5: Flapping
    record_state_change(ctx.source["id"], ctx.topic["id"])
    if is_flapping(ctx.source["id"], ctx.topic["id"]):
        await registry.post_audit(
            event_id=None, actor="hermes-dispatcher", action="flapping_block",
        )
        return NotificationResult(status="suppressed_flapping")
    await registry.post_audit(
        event_id=None, actor="hermes-dispatcher", action="flapping_check_passed",
    )

    # Step 6: Quiet-Hours — Phase 1: skipped (no policies seeded)
    # Phase 2 will fetch quiet_policies via registry + apply
    await registry.post_audit(
        event_id=None, actor="hermes-dispatcher", action="quiet_check_passed",
        reason="no_policies_phase1",
    )

    # Step 7: Rule-Match
    rule, channel_set = await find_matching_rule(
        registry,
        topic=intent.topic,
        audience_slug=ctx.audience_slug,
        severity=intent.severity,
        urgency=intent.urgency,
        actionability=intent.actionability,
        source_id=ctx.source["id"],
    )
    if rule is None or channel_set is None:
        await registry.post_audit(
            event_id=None, actor="hermes-dispatcher", action="no_rule_match",
        )
        return NotificationResult(status="suppressed_no_rule")
    await registry.post_audit(
        event_id=None, actor="hermes-dispatcher", action="rule_matched",
        entity_type="rule", entity_id=rule["id"],
    )

    # Step 8: Dispatch
    event_dict = intent.model_dump()
    results = await dispatch_to_channel_set(channel_set, event_dict)
    members = channel_set.get("members", [])
    deliveries = [
        DeliveryResult(
            channel_id=member["channel"]["id"],
            status=r.status,
            provider_message_id=r.provider_message_id,
            latency_ms=r.latency_ms,
        )
        for member, r in zip(members, results)
    ]

    # Audit per delivery + record cooldown for successful sends
    for member, r in zip(members, results):
        await registry.post_audit(
            event_id=None, actor="hermes-dispatcher",
            action="sent" if r.status == "delivered" else "failed",
            entity_type="channel", entity_id=member["channel"]["id"],
            reason=r.error,
        )
        if r.status == "delivered":
            record_cooldown(ctx.source["id"], member["channel"]["id"], ctx.topic["id"])

    # Aggregate status
    delivered_count = sum(1 for r in results if r.status == "delivered")
    if delivered_count == len(results):
        agg_status = "delivered"
    elif delivered_count == 0:
        agg_status = "failed"
    else:
        agg_status = "partial"

    return NotificationResult(
        status=agg_status,
        rule_matched=rule.get("slug"),
        deliveries=deliveries,
    )
