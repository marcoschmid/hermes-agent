"""Test that register_hub_routes mounts the hub router."""
from fastapi import FastAPI
from fastapi.testclient import TestClient

from gateway.hub.mount import register_hub_routes


def test_register_hub_routes_adds_health_endpoint() -> None:
    app = FastAPI()
    register_hub_routes(app)
    client = TestClient(app)
    r = client.get("/v1/health")
    assert r.status_code == 200
    assert r.json()["data"]["status"] == "healthy"


def test_register_hub_routes_adds_notifications_endpoint() -> None:
    app = FastAPI()
    register_hub_routes(app)
    client = TestClient(app)
    r = client.post("/v1/notifications", json={
        "source_slug": "test",
        "topic": "test",
        "title": "x",
        "body": "y",
    })
    # Without bearer -> 401
    assert r.status_code == 401
