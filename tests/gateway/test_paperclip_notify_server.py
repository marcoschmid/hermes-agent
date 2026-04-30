"""Tests for the standalone paperclip-notify uvicorn server wrapper."""
import os
import subprocess

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


def test_build_app_without_target_uses_log_sink(monkeypatch, tmp_path):
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


def test_resolve_target_falls_back_to_evm_chat_id(monkeypatch):
    monkeypatch.delenv("PAPERCLIP_NOTIFY_TARGET", raising=False)
    monkeypatch.setenv("EVM_TELEGRAM_CHAT_ID", "evm-chat-99")
    assert paperclip_notify_server._resolve_target(None) == "evm-chat-99"


def test_resolve_target_falls_back_to_default_chat_id(monkeypatch):
    monkeypatch.delenv("PAPERCLIP_NOTIFY_TARGET", raising=False)
    monkeypatch.delenv("EVM_TELEGRAM_CHAT_ID", raising=False)
    assert (
        paperclip_notify_server._resolve_target(None)
        == paperclip_notify_server.DEFAULT_TELEGRAM_CHAT_ID
    )


def test_telegram_sender_invokes_safe_telegram_send(monkeypatch, tmp_path):
    captured = {}

    def fake_run(cmd, **kwargs):
        captured["cmd"] = cmd
        captured["kwargs"] = kwargs
        return subprocess.CompletedProcess(cmd, 0, "ok", "")

    fake_script = tmp_path / "safe_telegram_send.sh"
    fake_script.write_text("#!/bin/sh\necho ok\n")
    fake_script.chmod(0o755)

    monkeypatch.setenv("PAPERCLIP_NOTIFY_SENDER", str(fake_script))
    monkeypatch.setattr(subprocess, "run", fake_run)

    sender = paperclip_notify_server._make_telegram_sender("128314698")
    sender("hello")

    cmd = captured["cmd"]
    assert cmd[0] == "bash"
    assert cmd[1] == str(fake_script)
    assert "--target" in cmd and "128314698" in cmd
    assert "--message" in cmd and "hello" in cmd
    assert "--context" in cmd and "paperclip-notify" in cmd


def test_telegram_sender_logs_nonzero_exit(monkeypatch, tmp_path, caplog):
    fake_script = tmp_path / "safe_telegram_send.sh"
    fake_script.write_text("#!/bin/sh\nexit 1\n")
    fake_script.chmod(0o755)
    monkeypatch.setenv("PAPERCLIP_NOTIFY_SENDER", str(fake_script))

    def fake_run(cmd, **kwargs):
        return subprocess.CompletedProcess(cmd, 7, "", "boom")

    monkeypatch.setattr(subprocess, "run", fake_run)
    sender = paperclip_notify_server._make_telegram_sender("128314698")
    with caplog.at_level("ERROR"):
        sender("msg")
    assert any("rc=7" in r.message and "boom" in r.message for r in caplog.records)


def test_telegram_sender_falls_back_when_script_missing(monkeypatch, tmp_path, caplog):
    monkeypatch.setenv("PAPERCLIP_NOTIFY_SENDER", str(tmp_path / "does-not-exist.sh"))
    with caplog.at_level("WARNING"):
        sender = paperclip_notify_server._make_telegram_sender("128314698")
    sender("msg")
    assert any("sender script missing" in r.message for r in caplog.records)


def test_telegram_sender_handles_timeout(monkeypatch, tmp_path, caplog):
    fake_script = tmp_path / "safe_telegram_send.sh"
    fake_script.write_text("#!/bin/sh\nsleep 60\n")
    fake_script.chmod(0o755)
    monkeypatch.setenv("PAPERCLIP_NOTIFY_SENDER", str(fake_script))

    def boom(cmd, **kwargs):
        raise subprocess.TimeoutExpired(cmd, 20)

    monkeypatch.setattr(subprocess, "run", boom)
    sender = paperclip_notify_server._make_telegram_sender("128314698")
    with caplog.at_level("ERROR"):
        sender("msg")
    assert any("timed out" in r.message for r in caplog.records)
