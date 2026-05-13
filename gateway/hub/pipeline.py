"""Hub dispatch pipeline.

Implements the 8-step pipeline. Phase 1 C3 covers steps 1-2:
  1. Schema validation (via Pydantic, before pipeline call)
  2. Registry lookup (source, topic, audience, severity_max)

Steps 3-8 (dedup, cooldown, flapping, quiet, rule-match, dispatch) added in subsequent tasks.
"""
from dataclasses import dataclass

from gateway.hub.registry_client import RegistryClient
from gateway.hub.schemas import NotificationIntent, NotificationResult


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
    audience_slug: str  # final audience-slug after override-merge


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

    # Step 2d: Audience-Resolution + Match
    intent_audience = intent.audience  # may be None → fall back to topic default
    topic_default_audience_id = topic.get("default_audience_id")

    if intent_audience is None:
        if not topic_default_audience_id:
            raise PipelineError(400, "audience_missing", "No audience specified and topic has no default")
        # We cannot resolve id→slug without another lookup — for now use the id as the resolved value
        # For now: assume topic.default_audience_id matches an aud_<slug> id pattern
        # Actual slug-resolution: caller can use audience.id directly downstream
        audience_slug = topic_default_audience_id  # caller resolves
    else:
        audience_slug = intent_audience
        # Hard validation: if topic has a default_audience_id, the override must match (Phase 2 may relax this)
        # Phase 1: Allow override when audience explicitly set; document constraint for Phase 2.

    return ResolvedContext(source=source, topic=topic, audience_slug=audience_slug)
