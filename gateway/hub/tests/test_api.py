"""Tests for gateway.hub.api skeleton."""
from fastapi import FastAPI
from fastapi.testclient import TestClient
import pytest

from gateway.hub.api import router


@pytest.fixture
def client() -> TestClient:
    app = FastAPI()
    app.include_router(router)
    return TestClient(app)


def test_health_endpoint_returns_200(client: TestClient) -> None:
    r = client.get("/v1/health")
    assert r.status_code == 200


def test_health_endpoint_returns_healthy_status(client: TestClient) -> None:
    r = client.get("/v1/health")
    body = r.json()
    assert body["data"]["status"] == "healthy"


def test_health_endpoint_returns_version(client: TestClient) -> None:
    r = client.get("/v1/health")
    body = r.json()
    assert body["data"]["version"] == "0.1.0"
