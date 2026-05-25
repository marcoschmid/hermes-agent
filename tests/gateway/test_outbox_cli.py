"""Contract tests for gateway.outbox_cli."""
from __future__ import annotations

import json
import sqlite3
import sys
from io import StringIO
from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest

from gateway.outbox_cli import main


def _capture(argv: list[str], *, stdin: str | None = None) -> tuple[int, str, str]:
    """Invoke main(argv) capturing stdout/stderr/exit."""
    stdout, stderr = StringIO(), StringIO()
    old_stdin = sys.stdin
    if stdin is not None:
        sys.stdin = StringIO(stdin)
    try:
        with patch("sys.stdout", stdout), patch("sys.stderr", stderr):
            rc = main(argv)
    finally:
        sys.stdin = old_stdin
    return rc, stdout.getvalue(), stderr.getvalue()


def _db_row(db_path: Path, dedup_key: str) -> sqlite3.Row | None:
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    try:
        return conn.execute("SELECT * FROM outbox WHERE dedup_key=?", (dedup_key,)).fetchone()
    finally:
        conn.close()


# ---- Happy path ------------------------------------------------------------


def test_cli_enqueues_message_with_target_context(tmp_path: Path):
    db = tmp_path / "outbox.db"
    rc, out, err = _capture([
        "--channel", "telegram",
        "--target", "128314698",
        "--context", "cert_expiry",
        "--dedup-key", "cert-expiry:2026-05-25",
        "--message", "ACME cert expires in 14 days",
        "--db-path", str(db),
    ])

    assert rc == 0, err
    assert err == ""
    result = json.loads(out)
    assert result["ok"] is True
    assert result["status"] == "pending"

    row = _db_row(db, "cert-expiry:2026-05-25")
    assert row is not None
    assert row["channel"] == "telegram"
    payload = json.loads(row["payload_json"])
    assert payload["message"] == "ACME cert expires in 14 days"
    assert payload["target"] == "128314698"
    assert payload["context"] == "cert_expiry"
    assert payload["audience"] == "marco"


# ---- Missing required ------------------------------------------------------


def test_cli_missing_message_returns_exit_2(tmp_path: Path):
    db = tmp_path / "outbox.db"
    rc, _out, err = _capture([
        "--channel", "telegram",
        "--dedup-key", "no-msg",
        "--db-path", str(db),
    ])

    assert rc == 2
    assert "required" in err.lower() or "message" in err.lower()
    # No row written
    assert not db.exists() or _db_row(db, "no-msg") is None


# ---- Dedupe idempotency ----------------------------------------------------


def test_cli_dedupe_key_is_idempotent(tmp_path: Path):
    db = tmp_path / "outbox.db"
    args = [
        "--channel", "telegram",
        "--target", "128314698",
        "--dedup-key", "idempotent-test",
        "--message", "first",
        "--db-path", str(db),
    ]
    rc1, out1, _ = _capture(args)
    rc2, out2, _ = _capture(args[:-2] + ["--message", "second", "--db-path", str(db)])

    assert rc1 == 0 and rc2 == 0
    id1 = json.loads(out1)["row_id"]
    id2 = json.loads(out2)["row_id"]
    assert id1 == id2  # same row returned

    # Original message preserved (first call wins per UNIQUE dedup_key)
    row = _db_row(db, "idempotent-test")
    payload = json.loads(row["payload_json"])
    assert payload["message"] == "first"


# ---- Message via stdin -----------------------------------------------------


def test_cli_message_stdin_reads_from_stdin(tmp_path: Path):
    db = tmp_path / "outbox.db"
    large_body = "x" * 100_000  # 100KB beyond argv-comfort
    rc, out, _ = _capture(
        [
            "--channel", "telegram",
            "--dedup-key", "stdin-test",
            "--message-stdin",
            "--db-path", str(db),
        ],
        stdin=large_body,
    )

    assert rc == 0
    row = _db_row(db, "stdin-test")
    payload = json.loads(row["payload_json"])
    assert payload["message"] == large_body


# ---- Payload-JSON override -------------------------------------------------


def test_cli_payload_json_overrides_individual_fields(tmp_path: Path):
    db = tmp_path / "outbox.db"
    rc, _out, _ = _capture([
        "--channel", "pushover",
        "--dedup-key", "payload-json-test",
        "--payload-json", json.dumps({"message": "custom", "priority": "high", "user_key": "abc"}),
        "--db-path", str(db),
    ])

    assert rc == 0
    row = _db_row(db, "payload-json-test")
    payload = json.loads(row["payload_json"])
    assert payload == {"message": "custom", "priority": "high", "user_key": "abc"}


def test_cli_payload_json_must_be_object(tmp_path: Path):
    db = tmp_path / "outbox.db"
    rc, _out, err = _capture([
        "--channel", "telegram",
        "--dedup-key", "bad-json",
        "--payload-json", "[1, 2, 3]",
        "--db-path", str(db),
    ])

    assert rc == 2
    assert "object" in err.lower() or "decode" in err.lower()


def test_cli_payload_json_invalid_json_returns_exit_2(tmp_path: Path):
    db = tmp_path / "outbox.db"
    rc, _out, err = _capture([
        "--channel", "telegram",
        "--dedup-key", "bad-json-2",
        "--payload-json", "{not valid json",
        "--db-path", str(db),
    ])

    assert rc == 2
    assert "json" in err.lower()


# ---- Mutual exclusion ------------------------------------------------------


def test_cli_message_and_payload_json_mutually_exclusive(tmp_path: Path):
    db = tmp_path / "outbox.db"
    rc, _out, err = _capture([
        "--channel", "telegram",
        "--dedup-key", "mx-test",
        "--message", "hello",
        "--payload-json", '{"foo": "bar"}',
        "--db-path", str(db),
    ])

    assert rc == 2
    assert "mutually exclusive" in err.lower() or "cannot be combined" in err.lower()


# ---- Dry-run ---------------------------------------------------------------


def test_cli_dry_run_does_not_write_db(tmp_path: Path):
    db = tmp_path / "outbox.db"
    rc, out, _ = _capture([
        "--channel", "telegram",
        "--target", "128314698",
        "--dedup-key", "dry-1",
        "--message", "dry-run test",
        "--db-path", str(db),
        "--dry-run",
    ])

    assert rc == 0
    result = json.loads(out)
    assert result["channel"] == "telegram"
    assert result["dedup_key"] == "dry-1"
    assert result["payload"]["message"] == "dry-run test"

    # DB never created (no init_schema in dry-run path)
    assert not db.exists()


# ---- Broken DB -------------------------------------------------------------


def test_cli_broken_db_path_returns_exit_1(tmp_path: Path):
    # Use a path where parent is a FILE not a dir → mkdir fails
    blocker = tmp_path / "blocker"
    blocker.write_text("not a dir")
    db = blocker / "nested" / "outbox.db"

    rc, _out, err = _capture([
        "--channel", "telegram",
        "--dedup-key", "broken-db",
        "--message", "x",
        "--db-path", str(db),
    ])

    assert rc == 1
    assert "ERROR" in err


# ---- Quiet mode ------------------------------------------------------------


def test_cli_quiet_suppresses_success_stdout(tmp_path: Path):
    db = tmp_path / "outbox.db"
    rc, out, err = _capture([
        "--channel", "telegram",
        "--dedup-key", "quiet-1",
        "--message", "hello",
        "--db-path", str(db),
        "--quiet",
    ])

    assert rc == 0
    assert out == ""
    assert err == ""
    row = _db_row(db, "quiet-1")
    assert row is not None


# ---- Round-2 Fixes ---------------------------------------------------------


def test_cli_accepts_legacy_dedupe_key_alias(tmp_path: Path):
    """Round-2 CRITICAL-1: --dedupe-key (legacy safe_telegram_send syntax)
    must work alongside --dedup-key."""
    db = tmp_path / "outbox.db"
    rc, out, err = _capture([
        "--channel", "telegram",
        "--dedupe-key", "legacy-syntax-test",  # NOT --dedup-key
        "--message", "alias smoke",
        "--db-path", str(db),
    ])

    assert rc == 0, err
    row = _db_row(db, "legacy-syntax-test")
    assert row is not None


def test_cli_accepts_legacy_dedupe_window_silently(tmp_path: Path):
    """Round-2 CRITICAL-1: --dedupe-window/--rate-limit-window/--media
    are silently accepted for migration compat (no longer applied — dedup_key
    handles idempotency, Hub flapping-suppression handles rate-limit).

    Note: --buttons is no longer in this silent-ignore list. It is now
    actively persisted to payload (see test_cli_buttons_persists_in_payload).
    """
    db = tmp_path / "outbox.db"
    rc, _out, err = _capture([
        "--channel", "telegram",
        "--dedup-key", "legacy-flags",
        "--message", "test",
        "--dedupe-window", "86400",
        "--rate-limit-window", "3600",
        "--media", "/tmp/foo.png",
        "--db-path", str(db),
    ])

    assert rc == 0, err
    row = _db_row(db, "legacy-flags")
    assert row is not None


def test_cli_buttons_persists_in_payload(tmp_path: Path):
    """P4 Track A2 Wave-1 Buttons-Extension: --buttons JSON-array must persist
    into payload so notification_worker can forward to direct-sender for
    inline-keyboard rendering by safe_telegram_send.sh.

    Required for task_monitor_watchdog.sh migration (uses --buttons for
    approve/retry/ack callbacks).
    """
    db = tmp_path / "outbox.db"
    buttons_json = (
        '[[{"text":"Show","callback_data":"task_monitor_stalled"},'
        '{"text":"Retry","callback_data":"task_monitor_retry"}],'
        '[{"text":"Ack","callback_data":"task_monitor_ack"}]]'
    )
    rc, _out, err = _capture([
        "--channel", "telegram",
        "--target", "128314698",
        "--dedup-key", "buttons-persist-test",
        "--message", "stalled tasks",
        "--buttons", buttons_json,
        "--db-path", str(db),
    ])

    assert rc == 0, err
    row = _db_row(db, "buttons-persist-test")
    payload = json.loads(row["payload_json"])
    assert "buttons" in payload, "buttons must be persisted in payload"
    assert payload["buttons"] == json.loads(buttons_json)


def test_cli_buttons_invalid_json_returns_exit_2(tmp_path: Path):
    """--buttons malformed-JSON must exit 2 with clear error (caller bug,
    not silent-drop)."""
    db = tmp_path / "outbox.db"
    rc, _out, err = _capture([
        "--channel", "telegram",
        "--dedup-key", "buttons-bad-json",
        "--message", "test",
        "--buttons", "not-a-json-array",
        "--db-path", str(db),
    ])

    assert rc == 2
    assert "buttons" in err.lower()


def test_cli_buttons_non_array_returns_exit_2(tmp_path: Path):
    """--buttons must be a JSON array (Telegram inline-keyboard convention).
    Non-array (e.g. object, string) must exit 2."""
    db = tmp_path / "outbox.db"
    rc, _out, err = _capture([
        "--channel", "telegram",
        "--dedup-key", "buttons-not-array",
        "--message", "test",
        "--buttons", '{"text":"OK"}',  # object, not array
        "--db-path", str(db),
    ])

    assert rc == 2
    assert "array" in err.lower() or "buttons" in err.lower()


def test_cli_dedupe_collision_reports_deduped_true(tmp_path: Path):
    """Round-2 MEDIUM-4: second enqueue with same dedup_key must report deduped=true."""
    db = tmp_path / "outbox.db"
    args1 = [
        "--channel", "telegram",
        "--dedup-key", "collision-test",
        "--message", "first",
        "--db-path", str(db),
    ]
    rc1, out1, _ = _capture(args1)
    rc2, out2, err2 = _capture(args1[:-2] + ["--message", "second", "--db-path", str(db)])

    assert rc1 == 0 and rc2 == 0
    r1 = json.loads(out1)
    r2 = json.loads(out2)
    assert r1["deduped"] is False
    assert r2["deduped"] is True
    assert "already enqueued" in err2.lower()


def test_cli_message_equals_form_for_leading_dash(tmp_path: Path):
    """Round-2 MEDIUM-3: --message='-foo' (equals form) accepts leading-dash text."""
    db = tmp_path / "outbox.db"
    rc, _out, err = _capture([
        "--channel", "telegram",
        "--dedup-key", "leading-dash",
        "--message=-Critical: 95% disk full",  # equals-form bypasses argparse next-flag scan
        "--db-path", str(db),
    ])

    assert rc == 0, err
    row = _db_row(db, "leading-dash")
    payload = json.loads(row["payload_json"])
    assert payload["message"] == "-Critical: 95% disk full"


def test_cli_target_and_context_persist_in_payload(tmp_path: Path):
    """Round-2 HIGH-2: --target/--context must reach payload so worker forwards
    to direct-sender (override factory-default chat-id/context)."""
    db = tmp_path / "outbox.db"
    rc, _out, _err = _capture([
        "--channel", "telegram",
        "--target", "999888777",
        "--context", "cert_expiry",
        "--dedup-key", "target-ctx-test",
        "--message", "test",
        "--db-path", str(db),
    ])

    assert rc == 0
    row = _db_row(db, "target-ctx-test")
    payload = json.loads(row["payload_json"])
    assert payload["target"] == "999888777"
    assert payload["context"] == "cert_expiry"


def test_cli_concurrent_init_schema_safe(tmp_path: Path):
    """Round-2 MEDIUM-5: 2 callers race-init same DB (no-claim-token column) —
    one ALTER wins, other tolerates duplicate-column error."""
    # Pre-create pre-claim-token schema (legacy)
    db = tmp_path / "outbox.db"
    conn = sqlite3.connect(str(db))
    conn.executescript("""
    CREATE TABLE IF NOT EXISTS outbox (
        id TEXT PRIMARY KEY, channel TEXT NOT NULL, payload_json TEXT NOT NULL,
        dedup_key TEXT NOT NULL, attempts INTEGER DEFAULT 0, last_error TEXT,
        status TEXT NOT NULL, next_retry_at TEXT, created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL
    );
    CREATE UNIQUE INDEX outbox_dedup_key_idx ON outbox(dedup_key);
    """)
    conn.close()

    # Sequential init_schema calls (simulates 2 callers, race not deterministic
    # but second call exercises duplicate-column path explicitly)
    from gateway.outbox import OutboxStore
    store1 = OutboxStore(str(db))
    store1.init_schema()  # adds claim_token
    store2 = OutboxStore(str(db))
    store2.init_schema()  # claim_token already present — must not raise

    cols = {row[1] for row in sqlite3.connect(str(db)).execute("PRAGMA table_info(outbox)").fetchall()}
    assert "claim_token" in cols
