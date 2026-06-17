"""TDD: PushoverRouter rich rendering (image attachment, deep-link url, sound, ttl)."""
from __future__ import annotations

import base64
from pathlib import Path
from typing import Any

from gateway.pushover_router import PushoverRouter


class _Capture:
    def __init__(self, status: int = 200, body: str = '{"status":1}') -> None:
        self.status = status
        self.body = body
        self.calls: list[tuple[str, dict[str, Any], float]] = []

    def __call__(self, url: str, data: dict[str, Any], timeout: float):
        self.calls.append((url, dict(data), timeout))
        return self.status, self.body


def _router(cap: _Capture) -> PushoverRouter:
    return PushoverRouter(api_token="tok", user_key="usr", transport=cap)


def test_url_and_url_title_forwarded() -> None:
    cap = _Capture()
    _router(cap).send(message="m", issue={"severity": "error", "url": "http://x/log",
                                          "url_title": "Log öffnen"})
    sent = cap.calls[0][1]
    assert sent["url"] == "http://x/log"
    assert sent["url_title"] == "Log öffnen"


def test_sound_override_wins() -> None:
    cap = _Capture()
    _router(cap).send(message="m", issue={"severity": "warn", "sound": "cosmic"})
    assert cap.calls[0][1]["sound"] == "cosmic"


def test_crit_defaults_to_siren_without_explicit_sound() -> None:
    cap = _Capture()
    _router(cap).send(message="m", issue={"severity": "crit"})
    assert cap.calls[0][1]["sound"] == "siren"


def test_ttl_set_for_non_emergency_only() -> None:
    cap = _Capture()
    _router(cap).send(message="m", issue={"severity": "warn", "ttl": 86400})
    assert cap.calls[0][1]["ttl"] == 86400

    cap2 = _Capture()
    _router(cap2).send(message="m", issue={"severity": "crit", "ttl": 86400})
    # ttl is ignored by Pushover for priority=2 — must not be sent
    assert "ttl" not in cap2.calls[0][1]


def test_image_attached_as_base64_when_file_present(tmp_path: Path) -> None:
    img = tmp_path / "preview.png"
    raw = b"\x89PNG\r\n\x1a\n" + b"fakepngbytes"
    img.write_bytes(raw)
    cap = _Capture()
    _router(cap).send(message="m", issue={"severity": "info", "image": str(img)})
    sent = cap.calls[0][1]
    assert sent["attachment_type"] == "image/png"
    assert sent["attachment_base64"] == base64.b64encode(raw).decode()


def test_missing_image_sends_text_only(tmp_path: Path) -> None:
    cap = _Capture()
    result = _router(cap).send(message="m", issue={"severity": "warn",
                                                   "image": str(tmp_path / "nope.png")})
    sent = cap.calls[0][1]
    assert "attachment_base64" not in sent
    assert result.ok is True


def test_oversized_image_skipped(tmp_path: Path) -> None:
    big = tmp_path / "big.jpg"
    big.write_bytes(b"x" * (5 * 1024 * 1024 + 1))  # > 5 MB
    cap = _Capture()
    _router(cap).send(message="m", issue={"severity": "warn", "image": str(big)})
    assert "attachment_base64" not in cap.calls[0][1]
