"""Hub dispatch pipeline.

Implements the 9-step pipeline for inbound notifications:
  1. Schema validation (via Pydantic, before pipeline call)
  2. Registry lookup (source, topic, audience, severity_max)
  3. Source-scope check (allowed_topics, allowed_audiences, max_severity)
  4. Dedup-window check (in-memory, Phase 1)
  5. Cooldown check (per-channel, deferred to dispatch)
  6. Flapping detection (state-change counter)
  7. Quiet-Hours (Phase 1: skipped — no policies seeded)
  8. Rule-Match (registry-driven, priority asc wins)
  9. Dispatch via channel-set adapters

Each step pushes an audit-event to the MC registry. A pipeline rejection in
steps 1-3 surfaces as PipelineError; everything past Step 3 returns a
NotificationResult with a suppressed_* / failed status.
"""
import json
import logging
from dataclasses import dataclass

from gateway.hub.adapter_registry import AdapterResult, get_adapter
from gateway.hub.adapters.errors import AdapterDeliveryError
from gateway.hub.cooldown import is_in_cooldown, record_cooldown
from gateway.hub.dedupe import check_dedup
from gateway.hub.dev_guard import is_dev_source
from gateway.hub.flapping import is_flapping, record_state_change
from gateway.hub.registry_client import RegistryClient
from gateway.hub.rule_matcher import find_matching_rule
from gateway.hub.schemas import (
    DeliveryResult,
    NotificationIntent,
    NotificationResult,
)

logger = logging.getLogger(__name__)


SEVERITY_ORDER = {"debug": 0, "info": 1, "notice": 2, "warn": 3, "error": 4, "crit": 5}


async def _safe_audit(registry: RegistryClient, **kwargs) -> None:
    """Push an audit event without ever aborting the pipeline.

    Audit-push must be best-effort: a transient MC outage should not turn a
    successful dispatch into an HTTP 500 to the caller. Failures are logged
    and the pipeline continues.
    """
    try:
        await registry.post_audit(**kwargs)
    except Exception as exc:  # broad on purpose — never re-raise out of audit
        logger.warning(
            "audit-push failed (action=%s entity=%s): %s",
            kwargs.get("action"), kwargs.get("entity_type"), exc,
        )


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


def _coerce_scope_list(value) -> list[str] | None:
    """Return scope values as a list, or None for legacy wildcard."""
    if value is None:
        return None
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except json.JSONDecodeError:
            return []
    if not isinstance(value, list):
        return []
    return [str(item) for item in value]


async def _raise_scope_violation(
    registry: RegistryClient,
    source: dict,
    error_code: str,
    message: str,
) -> None:
    await _safe_audit(
        registry,
        event_id=None,
        actor="hermes-dispatcher",
        action="scope_violation",
        entity_type="source",
        entity_id=source.get("id"),
        reason=error_code,
    )
    raise PipelineError(403, error_code, message)


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


async def validate_scope(
    intent: NotificationIntent,
    ctx: ResolvedContext,
    registry: RegistryClient,
) -> None:
    """Step 3: enforce per-source topic/audience/severity scope."""
    allowed_topics = _coerce_scope_list(ctx.source.get("allowed_topics"))
    if allowed_topics is not None and intent.topic not in allowed_topics:
        await _raise_scope_violation(
            registry,
            ctx.source,
            "topic_not_allowed",
            f"Source {intent.source_slug} is not allowed to send topic {intent.topic}",
        )

    allowed_audiences = _coerce_scope_list(ctx.source.get("allowed_audiences"))
    if allowed_audiences is not None and ctx.audience_slug not in allowed_audiences:
        await _raise_scope_violation(
            registry,
            ctx.source,
            "audience_not_allowed",
            f"Source {intent.source_slug} is not allowed to send audience {ctx.audience_slug}",
        )

    max_severity = ctx.source.get("max_severity")
    if max_severity is not None:
        max_severity = str(max_severity)
        if max_severity not in SEVERITY_ORDER:
            await _raise_scope_violation(
                registry,
                ctx.source,
                "scope_config_invalid",
                f"Source {intent.source_slug} has invalid max scoped severity {max_severity}",
            )
        if SEVERITY_ORDER[intent.severity] > SEVERITY_ORDER[max_severity]:
            await _raise_scope_violation(
                registry,
                ctx.source,
                "severity_exceeded",
                f"Source {intent.source_slug} max scoped severity is {max_severity}, got {intent.severity}",
            )

    await _safe_audit(
        registry,
        event_id=None,
        actor="hermes-dispatcher",
        action="scope_check_passed",
        entity_type="source",
        entity_id=ctx.source.get("id"),
    )


async def _dispatch_one_channel(
    adapter,
    channel: dict,
    event: dict,
    registry: RegistryClient,
) -> AdapterResult:
    """Dispatch one channel: edit-in-place if a live prior event exists for
    the same fingerprint AND its delivery was on THIS channel; otherwise send.

    v4d-A core. Edit-failure falls back to send() so a transient Telegram
    400 (message deleted / >48h / not-modified-but-not-mapped) never drops
    the notification. Channel-id match is enforced so a prior telegram-only
    event does not accidentally edit when the new firing also fans out to
    inbox_mc (multi-channel safety).
    """
    fingerprint = event.get("fingerprint")
    if fingerprint:
        try:
            prior = await registry.get_live_event_by_fingerprint(
                event["source_slug"], fingerprint
            )
        except Exception as exc:  # registry-client is best-effort but be defensive
            logger.warning("fingerprint lookup raised: %s", exc)
            prior = None
        if (
            prior
            and prior.get("provider_message_id")
            and prior.get("channel_id") == channel.get("id")
            and hasattr(adapter, "edit")
        ):
            try:
                return await adapter.edit(
                    event=event,
                    channel=channel,
                    message_id=prior["provider_message_id"],
                )
            except AdapterDeliveryError as exc:
                logger.warning(
                    "edit_failed_falling_back_to_send: source=%s channel=%s error=%s",
                    event.get("source_slug"), channel.get("id"), exc,
                )
                # fall through to send()
            except Exception as exc:  # noqa: BLE001 — never drop a notification
                logger.warning(
                    "edit_unexpected_error_falling_back_to_send: source=%s channel=%s error=%s",
                    event.get("source_slug"), channel.get("id"), exc,
                )
                # fall through to send()

    try:
        return await adapter.send(event, channel)
    except Exception as exc:  # noqa: BLE001 — preserve prior behaviour
        return AdapterResult(status="failed", error=str(exc))


async def run_pipeline(
    intent: NotificationIntent,
    source_token_hash: str,
    registry: RegistryClient,
    state=None,  # Optional HubState — if provided, writes audit-row to hub_events_log
) -> NotificationResult:
    """Run all 9 steps. Push audit to MC after each step.

    event_id is None in Phase 1 — MC owns event-id assignment via createEvent.
    The audit-stream is keyed off (source, topic, channel) instead.
    """
    # Step 1+2: Schema valid (Pydantic) + Registry-Lookup
    try:
        ctx = await validate_and_lookup(intent, source_token_hash, registry)
        await _safe_audit(registry, 
            event_id=None, actor="hermes-dispatcher", action="received",
            entity_type="source", entity_id=ctx.source["id"],
        )
        await _safe_audit(registry, 
            event_id=None, actor="hermes-dispatcher", action="validated",
            entity_type="topic", entity_id=ctx.topic["id"],
        )
    except PipelineError as e:
        return NotificationResult(status="failed", error=f"{e.error_code}: {e.message}")

    # Step 3: Source scope. Violations intentionally propagate so the API
    # returns HTTP 403 instead of a 200 with status=failed.
    await validate_scope(intent, ctx, registry)

    # Step 4: Dedup
    dedup = check_dedup(ctx.source["id"], ctx.topic["id"], intent.dedupe_key)
    if dedup.is_duplicate:
        await _safe_audit(registry, 
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
    await _safe_audit(registry, 
        event_id=None, actor="hermes-dispatcher", action="dedupe_check_passed",
    )

    # Step 5: Cooldown — pre-check defers to per-channel evaluation in Step 9
    # (channel set not known yet). Audit pre-check so the dispatcher contract
    # has its expected event order; per-channel cooldown decisions audit below.
    await _safe_audit(registry,
        event_id=None, actor="hermes-dispatcher", action="cooldown_pre_check",
    )

    # Step 6: Flapping
    record_state_change(ctx.source["id"], ctx.topic["id"])
    if is_flapping(ctx.source["id"], ctx.topic["id"]):
        await _safe_audit(registry, 
            event_id=None, actor="hermes-dispatcher", action="flapping_block",
        )
        return NotificationResult(status="suppressed_flapping")
    await _safe_audit(registry, 
        event_id=None, actor="hermes-dispatcher", action="flapping_check_passed",
    )

    # Step 7: Quiet-Hours — Phase 1: skipped (no policies seeded)
    # Phase 2 will fetch quiet_policies via registry + apply
    await _safe_audit(registry, 
        event_id=None, actor="hermes-dispatcher", action="quiet_check_passed",
        reason="no_policies_phase1",
    )

    # Step 8: Rule-Match
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
        await _safe_audit(registry, 
            event_id=None, actor="hermes-dispatcher", action="no_rule_match",
        )
        return NotificationResult(status="suppressed_no_rule")
    await _safe_audit(registry, 
        event_id=None, actor="hermes-dispatcher", action="rule_matched",
        entity_type="rule", entity_id=rule["id"],
    )

    # Step 9: Dispatch (per channel, with per-channel cooldown gate from Step 5)
    event_dict = intent.model_dump()
    # Inject resolved audience_slug so InboxMcAdapter sends a non-None audience
    # to MC. Pipeline already resolved topic.default_audience when intent.audience
    # was None; without this overwrite, model_dump() preserves the original None
    # and MC /events returns 422.
    if event_dict.get("audience") is None and ctx.audience_slug:
        event_dict["audience"] = ctx.audience_slug
    if not channel_set.get("enabled", True):
        # Channel-set disabled at registry level — refuse to dispatch.
        return NotificationResult(
            status="failed",
            rule_matched=rule.get("slug"),
            deliveries=[],
            error="channel_set_disabled",
        )
    members = channel_set.get("members", [])
    results: list[AdapterResult] = []
    deliveries: list[DeliveryResult] = []
    source_is_dev = is_dev_source(ctx.source.get("slug"))
    for member in members:
        channel = member.get("channel", {})
        channel_id = channel.get("id")
        ch_type = channel.get("type")
        if not channel.get("enabled", True):
            # Channel disabled at registry — skip without dispatch.
            await _safe_audit(registry,
                event_id=None, actor="hermes-dispatcher", action="rejected",
                entity_type="channel", entity_id=channel_id, reason="channel_disabled",
            )
            results.append(AdapterResult(status="failed", error="channel_disabled"))
            deliveries.append(DeliveryResult(
                channel_id=channel_id, status="failed",
                provider_message_id=None, latency_ms=None,
            ))
            continue

        # Dev-source guard: keep smoke-tests / dev pings out of prod user
        # channels. Inbox_mc remains the audit sink for all sources.
        if source_is_dev and ch_type != "inbox_mc":
            await _safe_audit(registry,
                event_id=None, actor="hermes-dispatcher", action="dev_guard_skip",
                entity_type="channel", entity_id=channel_id,
                reason=f"dev_source slug={ctx.source.get('slug')}",
            )
            results.append(AdapterResult(status="failed", error="dev_guard_skip"))
            deliveries.append(DeliveryResult(
                channel_id=channel_id, status="failed",
                provider_message_id=None, latency_ms=None,
            ))
            continue

        # Per-channel cooldown gate — H2 fix: previously declared in Step 4 but
        # never enforced. Skip dispatch + audit suppressed_cooldown when active.
        if channel_id and is_in_cooldown(ctx.source["id"], channel_id, ctx.topic["id"]):
            await _safe_audit(registry,
                event_id=None, actor="hermes-dispatcher", action="cooldown_block",
                entity_type="channel", entity_id=channel_id,
            )
            suppressed_result = AdapterResult(status="failed", error="cooldown_active")
            results.append(suppressed_result)
            deliveries.append(DeliveryResult(
                channel_id=channel_id,
                status="failed",
                provider_message_id=None,
                latency_ms=None,
            ))
            continue

        adapter = get_adapter(ch_type)
        if adapter is None:
            r = AdapterResult(status="failed", error=f"No adapter for channel_type={ch_type}")
        else:
            r = await _dispatch_one_channel(
                adapter=adapter,
                channel=channel,
                event=event_dict,
                registry=registry,
            )
        results.append(r)
        deliveries.append(DeliveryResult(
            channel_id=channel_id,
            status=r.status,
            provider_message_id=r.provider_message_id,
            latency_ms=r.latency_ms,
        ))

        # "edited" (v4d-A fingerprint-edit-in-place) is a successful delivery
        # for audit + cooldown purposes; surfaced as "sent" in the audit-stream
        # so downstream dashboards keep their existing event-name contract.
        await _safe_audit(registry,
            event_id=None, actor="hermes-dispatcher",
            action="sent" if r.status in ("delivered", "edited") else "failed",
            entity_type="channel", entity_id=channel_id,
            reason=r.error,
        )
        if r.status in ("delivered", "edited") and channel_id:
            record_cooldown(ctx.source["id"], channel_id, ctx.topic["id"])

    # Aggregate status — both "delivered" and "edited" (v4d-A) count as success.
    delivered_count = sum(1 for r in results if r.status in ("delivered", "edited"))
    if delivered_count == len(results) and len(results) > 0:
        agg_status = "delivered"
    elif delivered_count == 0:
        agg_status = "failed"
    else:
        agg_status = "partial"

    # v4d-A Phase 0.5: extract MC event_id from the inbox_mc delivery so the
    # API layer can persist per-channel dispatch results back to MC. The
    # InboxMcAdapter returns the freshly-created MC event_id as its
    # provider_message_id; all other channels return adapter-native ids.
    mc_event_id: str | None = None
    for member, r in zip(members, results):
        if (
            member.get("channel", {}).get("type") == "inbox_mc"
            and r.status in ("delivered", "edited")
        ):
            mc_event_id = r.provider_message_id
            break

    # T4: Write durable audit-row to hub_events_log if state was provided.
    # status maps: delivered → delivered_inbox; failed → failed; partial → failed
    # (Phase v4a status enum is coarser than NotificationResult — refined v4b)
    if state is not None:
        import json as _json
        import time as _time
        import uuid as _uuid
        event_id_for_log = f"evt_{_uuid.uuid4().hex[:16]}"
        log_status = "delivered_inbox" if agg_status == "delivered" else "failed"
        try:
            await state.execute(
                "INSERT INTO hub_events_log (event_id, source_slug, topic_slug, status, payload, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (event_id_for_log, intent.source_slug, intent.topic, log_status,
                 _json.dumps(intent.model_dump()), int(_time.time())),
            )
        except Exception as exc:
            logger.warning("hub_events_log insert failed: %s", exc)

    return NotificationResult(
        event_id=mc_event_id,
        status=agg_status,
        rule_matched=rule.get("slug"),
        deliveries=deliveries,
    )
