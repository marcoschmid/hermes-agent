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
