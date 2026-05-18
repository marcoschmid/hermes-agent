"""Pydantic models for the notification hub pipeline."""
from typing import Literal, Optional
from pydantic import BaseModel, Field

Severity = Literal["debug", "info", "notice", "warn", "error", "crit"]
Urgency = Literal["none", "today", "soon", "now"]
Actionability = Literal["info", "ack", "decide", "task"]
# v4c-A: sender contract for lifecycle; hub may transition to ACKED/SNOOZED.
SenderLifecycle = Literal["FIRING", "RECOVERED"]
RenderVersion = Literal["v4a", "v4c"]


class NotificationIntent(BaseModel):
    """Inbound payload from a sender to POST /v1/notifications."""
    source_slug: str = Field(min_length=1, max_length=128)
    topic: str = Field(min_length=1, max_length=128)
    severity: Severity = "info"
    urgency: Urgency = "none"
    actionability: Actionability = "info"
    audience: Optional[str] = None
    title: str = Field(min_length=1, max_length=200)
    body: str = Field(min_length=1, max_length=4000)
    dedupe_key: Optional[str] = Field(default=None, max_length=256)
    correlation_id: Optional[str] = Field(default=None, max_length=128)
    payload: Optional[dict] = None
    # v4c-A additive fields. All optional; no cross-field validation in v4c-A.
    service: Optional[str] = Field(default=None, max_length=64)
    impact: Optional[str] = Field(default=None, max_length=200)
    action_required: Optional[str] = Field(default=None, max_length=200)
    context: Optional[dict] = None
    links: Optional[dict] = None
    fingerprint: Optional[str] = Field(default=None, max_length=120)
    started_at: Optional[str] = None
    lifecycle: SenderLifecycle = "FIRING"
    render_version: RenderVersion = "v4a"


class DeliveryResult(BaseModel):
    channel_id: str
    # "edited" surfaces from TelegramAdapter.edit (v4d-A fingerprint-edit).
    # MC normalises it to "delivered" server-side via DeliveryStatusInput;
    # aggregate logic in run_pipeline treats edited and delivered as equivalent.
    status: Literal["pending", "delivered", "edited", "failed", "fallback_used"]
    provider_message_id: Optional[str] = None
    latency_ms: Optional[int] = None


class NotificationResult(BaseModel):
    """Response payload from POST /v1/notifications."""
    event_id: Optional[str] = None
    status: Literal[
        "queued", "delivered", "partial", "failed",
        "suppressed_dedup", "suppressed_cooldown", "suppressed_flapping",
        "suppressed_quiet", "suppressed_no_rule"
    ]
    rule_matched: Optional[str] = None
    deliveries: list[DeliveryResult] = []
    suppression: Optional[dict] = None
    error: Optional[str] = None
