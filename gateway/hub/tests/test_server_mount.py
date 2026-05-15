"""Tests for the standalone Notification Hub FastAPI host."""

from fastapi.testclient import TestClient

from gateway.hub.schemas import NotificationResult
from gateway.hub.server import create_app


def _notification_payload() -> dict:
    return {
        "source_slug": "test",
        "topic": "test",
        "title": "x",
        "body": "y",
    }


def test_health_endpoint_unauthenticated(monkeypatch) -> None:
    monkeypatch.delenv("HUB_PILOT_TOKEN", raising=False)
    client = TestClient(create_app())

    response = client.get("/v1/health")

    assert response.status_code == 200


def test_notifications_requires_bearer(monkeypatch) -> None:
    monkeypatch.setenv("HUB_PILOT_TOKEN", "pilot-secret")
    client = TestClient(create_app())

    response = client.post("/v1/notifications", json=_notification_payload())

    assert response.status_code == 401


def test_notifications_wrong_token(monkeypatch) -> None:
    monkeypatch.setenv("HUB_PILOT_TOKEN", "pilot-secret")
    client = TestClient(create_app())

    response = client.post(
        "/v1/notifications",
        headers={"Authorization": "Bearer wrong-secret"},
        json=_notification_payload(),
    )

    assert response.status_code == 401


def test_notifications_correct_token(monkeypatch) -> None:
    monkeypatch.setenv("HUB_PILOT_TOKEN", "pilot-secret")
    captured: dict[str, str] = {}

    async def fake_run_pipeline(*args, **kwargs) -> NotificationResult:
        captured["source_token_hash"] = kwargs["source_token_hash"]
        return NotificationResult(status="queued")

    monkeypatch.setattr("gateway.hub.api.get_registry", lambda: object())
    monkeypatch.setattr("gateway.hub.api.run_pipeline", fake_run_pipeline)
    client = TestClient(create_app())

    response = client.post(
        "/v1/notifications",
        headers={"Authorization": "Bearer pilot-secret"},
        json=_notification_payload(),
    )

    assert response.status_code == 200
    assert captured["source_token_hash"] == ""
