"""Tests for gateway.hub.schemas."""
import pytest
from pydantic import ValidationError

from gateway.hub.schemas import NotificationIntent


def test_notification_intent_valid() -> None:
    intent = NotificationIntent(
        source_slug="paperclip",
        topic="ops.deploy",
        severity="warn",
        urgency="soon",
        actionability="ack",
        audience="marco",
        title="Deploy failed",
        body="The deploy pipeline failed at step 3.",
        dedupe_key="deploy:abc123",
        correlation_id="req_xyz",
        payload={"step": 3},
    )
    assert intent.source_slug == "paperclip"
    assert intent.severity == "warn"
    assert intent.audience == "marco"


def test_notification_intent_missing_required_field() -> None:
    with pytest.raises(ValidationError) as excinfo:
        NotificationIntent(
            source_slug="paperclip",
            topic="ops.deploy",
            # title missing
            body="something",
        )
    errors = excinfo.value.errors()
    assert any(e["loc"] == ("title",) for e in errors)


def test_notification_intent_severity_outside_enum() -> None:
    with pytest.raises(ValidationError) as excinfo:
        NotificationIntent(
            source_slug="paperclip",
            topic="ops.deploy",
            severity="catastrophic",  # not in Severity literal
            title="t",
            body="b",
        )
    errors = excinfo.value.errors()
    assert any(e["loc"] == ("severity",) for e in errors)


def test_notification_intent_title_too_long() -> None:
    with pytest.raises(ValidationError) as excinfo:
        NotificationIntent(
            source_slug="paperclip",
            topic="ops.deploy",
            title="x" * 201,
            body="b",
        )
    errors = excinfo.value.errors()
    assert any(e["loc"] == ("title",) for e in errors)
