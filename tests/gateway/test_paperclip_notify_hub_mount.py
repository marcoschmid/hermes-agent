"""Verify that the Hub /v1/notifications route is mounted in paperclip_notify_server.

The live LaunchAgent `de.marcoschmid.hermes-paperclip-notify` runs
`python -m gateway.paperclip_notify_server` and must expose the Hub endpoints
so that downstream services (e.g. drobo-backup dual-write) can post events.
"""
from starlette.testclient import TestClient

from gateway.paperclip_notify_server import build_app


def test_hub_route_mounted():
    app = build_app(target="123")
    client = TestClient(app)
    response = client.post("/v1/notifications", json={})
    # Without auth headers we expect 401 (auth-fail), NOT 404 (route-missing)
    assert response.status_code == 401, (
        f"Expected 401, got {response.status_code}. Hub not mounted."
    )
