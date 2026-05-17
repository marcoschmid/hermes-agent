"""Tests for NotificationIntent v4c-A extensions (additive, backward-compat)."""
import pytest
from pydantic import ValidationError

from gateway.hub.schemas import NotificationIntent


def test_v4a_payload_still_accepted() -> None:
    """Backward-compat: legacy v4a payloads (no v4c-fields) parse cleanly."""
    intent = NotificationIntent(
        source_slug="paperclip",
        topic="ops.deploy",
        severity="info",
        urgency="none",
        actionability="info",
        audience="marco",
        title="legacy",
        body="no v4c-fields",
    )
    assert intent.lifecycle == "FIRING"        # default
    assert intent.render_version == "v4a"      # default
    assert intent.service is None
    assert intent.fingerprint is None


def test_v4c_payload_fully_populated() -> None:
    """v4c-A payload: all new fields accepted and round-trip via model_dump."""
    intent = NotificationIntent(
        source_slug="ha-pilot",
        topic="device_offline",
        severity="warn",
        urgency="soon",
        actionability="ack",
        audience="marco",
        title="Govée 11 offline",
        body="full v4c",
        service="home-assistant",
        impact="wohnzimmer dunkel",
        action_required="aws-ips checken",
        context={"offline": "11/11", "loss": "33%"},
        links={"dashboard": "https://ha/x"},
        fingerprint="ha-pilot/home-assistant/govee_aws_loss",
        started_at="2026-05-17T08:00:00+02:00",
        lifecycle="FIRING",
        render_version="v4c",
    )
    assert intent.service == "home-assistant"
    assert intent.impact == "wohnzimmer dunkel"
    assert intent.context == {"offline": "11/11", "loss": "33%"}
    assert intent.links == {"dashboard": "https://ha/x"}
    assert intent.fingerprint == "ha-pilot/home-assistant/govee_aws_loss"
    assert intent.lifecycle == "FIRING"
    assert intent.render_version == "v4c"

    # model_dump round-trip preserves all fields
    dumped = intent.model_dump()
    assert dumped["service"] == "home-assistant"
    assert dumped["fingerprint"] == "ha-pilot/home-assistant/govee_aws_loss"


def test_invalid_lifecycle_rejected() -> None:
    """Pydantic Literal enforces lifecycle values; only FIRING/RECOVERED allowed as sender input."""
    with pytest.raises(ValidationError):
        NotificationIntent(
            source_slug="x", topic="y", severity="info", urgency="none",
            actionability="info", audience="marco", title="t", body="b",
            lifecycle="ACKED",  # hub-managed, sender may not set
        )


def test_invalid_render_version_rejected() -> None:
    with pytest.raises(ValidationError):
        NotificationIntent(
            source_slug="x", topic="y", severity="info", urgency="none",
            actionability="info", audience="marco", title="t", body="b",
            render_version="v5",
        )


def test_partial_v4c_fields_accepted() -> None:
    """v4c-A is additive; partial v4c-payload (e.g. only service+fingerprint) is valid."""
    intent = NotificationIntent(
        source_slug="restic", topic="ops.backup", severity="warn",
        urgency="today", actionability="ack", audience="marco",
        title="backup partial", body="b",
        service="qnap-backup", fingerprint="restic/qnap/repo-unreachable",
        render_version="v4c",
    )
    assert intent.service == "qnap-backup"
    assert intent.impact is None
    assert intent.action_required is None
