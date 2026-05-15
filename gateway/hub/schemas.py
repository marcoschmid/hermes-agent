"""Pydantic models for the notification hub pipeline."""
from typing import Literal, Optional
from pydantic import BaseModel, Field

Severity = Literal["debug", "info", "notice", "warn", "error", "crit"]
Urgency = Literal["none", "today", "soon", "now"]
Actionability = Literal["info", "ack", "decide", "task"]


class NotificationIntent(BaseModel):
    """Inbound payload from a sender to POST /v1/notifications."""
    source_slug: str = Field(min_length=1, max_length=128)
    topic: str = Field(min_length=1, max_length=128)
    severity: Severity = "info"
    urgency: Urgency = "none"
    actionability: Actionability = "info"
    audience: Optional[str] = None  # falls None: topic.default_audience_id
    title: str = Field(min_length=1, max_length=200)
    body: str = Field(min_length=1, max_length=4000)
    dedupe_key: Optional[str] = Field(default=None, max_length=256)
    correlation_id: Optional[str] = Field(default=None, max_length=128)
    payload: Optional[dict] = None


class DeliveryResult(BaseModel):
    channel_id: str
    status: Literal["pending", "delivered", "failed", "fallback_used"]
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
