"""Tests for workspace safe_telegram_send.sh direct Telegram fallback."""

from __future__ import annotations

from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import os
from pathlib import Path
import subprocess
import threading
from typing import Any
from urllib.parse import parse_qs


SCRIPT = Path.home() / ".openclaw" / "workspace" / "scripts" / "safe_telegram_send.sh"


class _TelegramMockHandler(BaseHTTPRequestHandler):
    def do_POST(self):  # noqa: N802 - stdlib hook name
        length = int(self.headers.get("Content-Length", "0"))
        body = self.rfile.read(length).decode()
        self.server.requests.append(  # type: ignore[attr-defined]
            {"path": self.path, "body": body, "form": parse_qs(body)}
        )
        status = self.server.status_code  # type: ignore[attr-defined]
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        if status == 200:
            self.wfile.write(b'{"ok":true}')
        else:
            self.wfile.write(b'{"ok":false}')

    def log_message(self, *_args: Any) -> None:
        return


class _TelegramMockServer:
    def __init__(self, *, status_code: int = 200):
        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), _TelegramMockHandler)
        self.httpd.status_code = status_code  # type: ignore[attr-defined]
        self.httpd.requests = []  # type: ignore[attr-defined]
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)

    def __enter__(self):
        self.thread.start()
        return self

    def __exit__(self, *_exc):
        self.httpd.shutdown()
        self.thread.join(timeout=2)

    @property
    def url(self) -> str:
        host, port = self.httpd.server_address
        return f"http://{host}:{port}"

    @property
    def requests(self) -> list[dict[str, Any]]:
        return self.httpd.requests  # type: ignore[attr-defined]


def _env(tmp_path: Path, api_base: str) -> dict[str, str]:
    env = os.environ.copy()
    env.update(
        {
            "WORKSPACE": str(tmp_path / "workspace"),
            "OPENCLAW_BIN": str(tmp_path / "missing-openclaw"),
            "SECURITY_CMD": str(tmp_path / "missing-security"),
            "TELEGRAM_BOT_TOKEN": "123:abc",
            "TELEGRAM_CHAT_ID": "4242",
            "TELEGRAM_API_BASE": api_base,
        }
    )
    return env


def test_safe_telegram_send_shell_syntax_is_valid():
    result = subprocess.run(["bash", "-n", str(SCRIPT)], capture_output=True, text=True)

    assert result.returncode == 0, result.stderr


def test_missing_openclaw_uses_direct_fallback(tmp_path: Path):
    with _TelegramMockServer() as server:
        result = subprocess.run(
            ["bash", str(SCRIPT), "--message", "direct fallback test"],
            capture_output=True,
            text=True,
            env=_env(tmp_path, server.url),
        )

    assert result.returncode == 0, result.stderr
    assert len(server.requests) == 1
    request = server.requests[0]
    assert request["path"] == "/bot123:abc/sendMessage"
    assert request["form"]["chat_id"] == ["4242"]
    assert request["form"]["text"] == ["direct fallback test"]
    log = tmp_path / "workspace" / "memory" / "telegram-send-log.tsv"
    assert "direct-fallback" in log.read_text()


def test_openclaw_failure_falls_back_to_direct_send(tmp_path: Path):
    fake_openclaw = tmp_path / "fake-openclaw"
    fake_openclaw.write_text("#!/usr/bin/env bash\nexit 42\n")
    fake_openclaw.chmod(0o755)

    with _TelegramMockServer() as server:
        env = _env(tmp_path, server.url)
        env["OPENCLAW_BIN"] = str(fake_openclaw)
        result = subprocess.run(
            ["bash", str(SCRIPT), "--target", "777", "--message", "after failure"],
            capture_output=True,
            text=True,
            env=env,
        )

    assert result.returncode == 0, result.stderr
    assert len(server.requests) == 1
    assert server.requests[0]["form"]["chat_id"] == ["777"]
    assert server.requests[0]["form"]["text"] == ["after failure"]


def test_explicit_dedupe_key_prevents_double_direct_send(tmp_path: Path):
    with _TelegramMockServer() as server:
        command = [
            "bash",
            str(SCRIPT),
            "--dedupe-key",
            "same-message",
            "--dedupe-window",
            "3600",
            "--message",
            "idempotent direct fallback",
        ]
        first = subprocess.run(
            command,
            capture_output=True,
            text=True,
            env=_env(tmp_path, server.url),
        )
        second = subprocess.run(
            command,
            capture_output=True,
            text=True,
            env=_env(tmp_path, server.url),
        )

    assert first.returncode == 0, first.stderr
    assert second.returncode == 0, second.stderr
    assert len(server.requests) == 1


def test_direct_fallback_surfaces_telegram_http_failure(tmp_path: Path):
    with _TelegramMockServer(status_code=500) as server:
        result = subprocess.run(
            ["bash", str(SCRIPT), "--message", "should fail"],
            capture_output=True,
            text=True,
            env=_env(tmp_path, server.url),
        )

    assert len(server.requests) == 1
    assert result.returncode != 0
    assert "Telegram direct fallback failed with HTTP 500" in result.stderr
