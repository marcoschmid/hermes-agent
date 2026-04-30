"""Tests for the standalone paperclip-notify uvicorn server wrapper."""
from typing import List

from fastapi.testclient import TestClient

from gateway import paperclip_notify_server


def test_health_endpoint(monkeypatch, tmp_path):
    monkeypatch.setenv("PAPERCLIP_NOTIFY_TOKEN", "tkn")
    monkeypatch.setenv("PAPERCLIP_NOTIFY_DB", str(tmp_path / "d.db"))
    app = paperclip_notify_server.build_app(target=None)
    c = TestClient(app)
    r = c.get("/health")
    assert r.status_code == 200
    assert r.json() == {"ok": True}


def test_build_app_without_target_uses_log_sink(monkeypatch, tmp_path, caplog):
    monkeypatch.setenv("PAPERCLIP_NOTIFY_TOKEN", "tkn")
    monkeypatch.setenv("PAPERCLIP_NOTIFY_DB", str(tmp_path / "d.db"))
    app = paperclip_notify_server.build_app(target=None)
    c = TestClient(app)
    p = {
        "check": "x", "status": "warn", "previous_status": "ok", "findings": 1,
        "summary": "s", "content_hash": "h", "scheduled_for": "2026-04-30T00:00:00+00:00",
        "details_hint": "hint",
    }
    r = c.post("/paperclip/notify", json=p, headers={"Authorization": "Bearer tkn"})
    assert r.status_code == 200
    assert r.json() == {"sent": True}


def test_resolve_target_explicit_wins(monkeypatch):
    monkeypatch.setenv("PAPERCLIP_NOTIFY_TARGET", "env-target")
    assert paperclip_notify_server._resolve_target("explicit") == "explicit"


def test_resolve_target_env(monkeypatch):
    monkeypatch.setenv("PAPERCLIP_NOTIFY_TARGET", "env-target")
    assert paperclip_notify_server._resolve_target(None) == "env-target"


def test_resolve_target_falls_back_to_cron_config(monkeypatch):
    monkeypatch.delenv("PAPERCLIP_NOTIFY_TARGET", raising=False)
    fake_cfg = {"cron": {"auto_delivery": {"target": "cfg-target"}}}
    monkeypatch.setattr("hermes_cli.config.load_config", lambda: fake_cfg)
    assert paperclip_notify_server._resolve_target(None) == "cfg-target"


def test_resolve_target_returns_none_when_nothing_set(monkeypatch):
    monkeypatch.delenv("PAPERCLIP_NOTIFY_TARGET", raising=False)
    monkeypatch.setattr("hermes_cli.config.load_config", lambda: {})
    assert paperclip_notify_server._resolve_target(None) is None


def test_telegram_sender_invokes_send_message_tool(monkeypatch):
    captured: List[dict] = []

    def fake_tool(args, **kw):
        captured.append(args)
        return '{"ok": true}'

    monkeypatch.setattr("tools.send_message_tool.send_message_tool", fake_tool)
    sender = paperclip_notify_server._make_telegram_sender("telegram:-1:7")
    sender("hello world")
    assert captured == [{"action": "send", "target": "telegram:-1:7", "message": "hello world"}]


def test_telegram_sender_logs_error_payload(monkeypatch, caplog):
    def fake_tool(args, **kw):
        return '{"error": "something failed"}'

    monkeypatch.setattr("tools.send_message_tool.send_message_tool", fake_tool)
    sender = paperclip_notify_server._make_telegram_sender("telegram:1")
    with caplog.at_level("ERROR"):
        sender("msg")
    assert any("something failed" in r.message for r in caplog.records)
