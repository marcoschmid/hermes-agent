"""TDD: rich notification keys (image/url/url_title/sound/ttl) reach the router issue."""
from __future__ import annotations

import json

from gateway.notification_worker import _payload_to_router_args
from gateway.outbox import OutboxRow


def _row(payload: dict) -> OutboxRow:
    return OutboxRow(
        id="r1", channel="pushover", payload_json=json.dumps(payload),
        dedup_key="d1", attempts=0, last_error=None, status="claimed",
        next_retry_at=None, created_at="t", updated_at="t",
    )


def test_rich_keys_forwarded_to_issue() -> None:
    msg, issue = _payload_to_router_args(_row({
        "message": "m", "title": "T", "severity": "warn",
        "image": "/tmp/x.png", "url": "http://x/log", "url_title": "open",
        "sound": "cosmic", "ttl": 86400,
    }))
    assert msg == "m"
    assert issue["image"] == "/tmp/x.png"
    assert issue["url"] == "http://x/log"
    assert issue["url_title"] == "open"
    assert issue["sound"] == "cosmic"
    assert issue["ttl"] == 86400


def test_existing_keys_still_forwarded() -> None:
    _msg, issue = _payload_to_router_args(_row({
        "message": "m", "title": "T", "severity": "error", "target": "123",
    }))
    assert issue["title"] == "T"
    assert issue["severity"] == "error"
    assert issue["target"] == "123"
