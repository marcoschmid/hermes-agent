"""Tests for gateway.hub.pipeline.validate_and_lookup (Steps 1-2)."""
import pytest

from gateway.hub.pipeline import (
    PipelineError,
    ResolvedContext,
    validate_and_lookup,
)
from gateway.hub.registry_client import RegistryClient
from gateway.hub.schemas import NotificationIntent


class FakeRegistry(RegistryClient):
    """Test double — overrides the network methods used in steps 1-2."""

    def __init__(
        self,
        source: dict | None = None,
        topic: dict | None = None,
    ) -> None:
        # Skip super().__init__ to avoid creating an httpx client we don't need.
        self._source = source
        self._topic = topic
        self.calls: list[tuple[str, tuple, dict]] = []

    async def get_source(self, slug: str, token_hash: str | None = None) -> dict | None:
        self.calls.append(("get_source", (slug,), {"token_hash": token_hash}))
        return self._source

    async def get_topic(self, slug: str) -> dict | None:
        self.calls.append(("get_topic", (slug,), {}))
        return self._topic


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


@pytest.mark.asyncio
async def test_source_not_found_raises_auth_failed() -> None:
    registry = FakeRegistry(source=None)
    with pytest.raises(PipelineError) as excinfo:
        await validate_and_lookup(make_intent(), "tokenhash", registry)
    assert excinfo.value.status_code == 401
    assert excinfo.value.error_code == "auth_failed"


@pytest.mark.asyncio
async def test_source_disabled_raises_403() -> None:
    registry = FakeRegistry(
        source={"id": "src_1", "slug": "paperclip", "enabled": False, "severity_max": "warn"},
        topic={"slug": "ops.deploy", "default_audience_id": "aud_marco", "default_audience_slug": "marco"},
    )
    with pytest.raises(PipelineError) as excinfo:
        await validate_and_lookup(make_intent(), "tokenhash", registry)
    assert excinfo.value.status_code == 403
    assert excinfo.value.error_code == "source_disabled"


@pytest.mark.asyncio
async def test_severity_too_high_raises_403() -> None:
    registry = FakeRegistry(
        source={"id": "src_1", "slug": "paperclip", "enabled": True, "severity_max": "warn"},
        topic={"slug": "ops.deploy", "default_audience_id": "aud_marco", "default_audience_slug": "marco"},
    )
    with pytest.raises(PipelineError) as excinfo:
        await validate_and_lookup(make_intent(severity="crit"), "tokenhash", registry)
    assert excinfo.value.status_code == 403
    assert excinfo.value.error_code == "severity_too_high"


@pytest.mark.asyncio
async def test_topic_not_found_raises_404() -> None:
    registry = FakeRegistry(
        source={"id": "src_1", "slug": "paperclip", "enabled": True, "severity_max": "warn"},
        topic=None,
    )
    with pytest.raises(PipelineError) as excinfo:
        await validate_and_lookup(make_intent(), "tokenhash", registry)
    assert excinfo.value.status_code == 404
    assert excinfo.value.error_code == "topic_not_found"


@pytest.mark.asyncio
async def test_audience_missing_no_default_raises_400() -> None:
    registry = FakeRegistry(
        source={"id": "src_1", "slug": "paperclip", "enabled": True, "severity_max": "warn"},
        topic={"slug": "ops.deploy", "default_audience_id": None, "default_audience_slug": None},
    )
    with pytest.raises(PipelineError) as excinfo:
        await validate_and_lookup(make_intent(audience=None), "tokenhash", registry)
    assert excinfo.value.status_code == 400
    assert excinfo.value.error_code == "audience_missing"


@pytest.mark.asyncio
async def test_happy_path_returns_resolved_context() -> None:
    source = {"id": "src_1", "slug": "paperclip", "enabled": True, "severity_max": "warn"}
    topic = {"slug": "ops.deploy", "default_audience_id": "aud_marco", "default_audience_slug": "marco"}
    registry = FakeRegistry(source=source, topic=topic)
    ctx = await validate_and_lookup(make_intent(audience="marco"), "tokenhash", registry)
    assert isinstance(ctx, ResolvedContext)
    assert ctx.source == source
    assert ctx.topic == topic
    assert ctx.audience_slug == "marco"


@pytest.mark.asyncio
async def test_audience_falls_back_to_topic_default_slug() -> None:
    """When intent.audience is None, ResolvedContext.audience_slug must be the
    topic's default_audience_slug (a bare slug), NOT the aud_-prefixed id —
    otherwise downstream rule-matching falls through to suppressed_no_rule."""
    source = {"id": "src_1", "slug": "paperclip", "enabled": True, "severity_max": "warn"}
    topic = {
        "slug": "ops.deploy",
        "default_audience_id": "aud_family",
        "default_audience_slug": "family",
    }
    registry = FakeRegistry(source=source, topic=topic)
    ctx = await validate_and_lookup(make_intent(audience=None), "tokenhash", registry)
    assert ctx.audience_slug == "family"
    assert not ctx.audience_slug.startswith("aud_")
