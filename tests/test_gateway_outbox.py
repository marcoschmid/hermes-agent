"""Phase-3 G3 contract tests for the Hermes notification outbox.

These tests intentionally target the planned Phase-3 production surface from
``projects/jarvis-os-redesign/plans/2026-05-02-phase-3.md``:

* ``gateway.outbox.OutboxStore``
* SQLite table ``outbox``
* public methods ``enqueue``, ``claim_due``, ``mark_sent`` and
  ``record_failure``

Until that production module exists, failures are expected and should be
routed to Phase-4 code apply.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
import importlib
import inspect
import json
import sqlite3
from pathlib import Path
from typing import Any

import pytest


def _load_outbox_module():
    try:
        return importlib.import_module("gateway.outbox")
    except ModuleNotFoundError as exc:
        if exc.name == "gateway.outbox":
            pytest.fail(
                "ROUTE_TO_PHASE_4_CODE_APPLY: gateway.outbox fehlt. "
                "Phase-3 G3 Outbox-Tests koennen erst gruen werden, wenn "
                "OutboxStore + SQLite-Schema implementiert sind.",
                pytrace=False,
            )
        raise


def _make_store(tmp_path: Path) -> tuple[Any, Path]:
    module = _load_outbox_module()
    store_cls = getattr(module, "OutboxStore", None)
    if store_cls is None:
        pytest.fail(
            "ROUTE_TO_PHASE_4_CODE_APPLY: gateway.outbox.OutboxStore fehlt.",
            pytrace=False,
        )
    db_path = tmp_path / "outbox.sqlite"
    store = store_cls(str(db_path))
    for initializer in ("init_schema", "initialize", "setup"):
        method = getattr(store, initializer, None)
        if callable(method):
            method()
            break
    return store, db_path


def _call_method(obj: Any, name: str, **kwargs: Any) -> Any:
    method = getattr(obj, name, None)
    if not callable(method):
        pytest.fail(
            f"ROUTE_TO_PHASE_4_CODE_APPLY: OutboxStore.{name} fehlt.",
            pytrace=False,
        )

    signature = inspect.signature(method)
    accepted = {
        key: value
        for key, value in kwargs.items()
        if key in signature.parameters
        or any(p.kind == p.VAR_KEYWORD for p in signature.parameters.values())
    }
    return method(**accepted)


def _enqueue(
    store: Any,
    *,
    channel: str = "telegram",
    payload: dict[str, Any] | None = None,
    dedup_key: str,
    now: datetime | None = None,
) -> Any:
    payload = payload or {"text": "hello"}
    return _call_method(
        store,
        "enqueue",
        channel=channel,
        payload=payload,
        payload_json=json.dumps(payload),
        dedup_key=dedup_key,
        now=now,
        created_at=now,
    )


def _claim_due(store: Any, *, now: datetime, limit: int = 10) -> list[Any]:
    result = _call_method(store, "claim_due", now=now, limit=limit)
    return list(result or [])


def _mark_sent(store: Any, row_id: str, *, now: datetime | None = None) -> None:
    _call_method(store, "mark_sent", row_id=row_id, message_id=row_id, id=row_id, now=now)


def _record_failure(
    store: Any,
    row_id: str,
    *,
    error: str = "telegram 500",
    now: datetime | None = None,
) -> None:
    _call_method(
        store,
        "record_failure",
        row_id=row_id,
        message_id=row_id,
        id=row_id,
        error=error,
        last_error=error,
        now=now,
    )


def _db_rows(db_path: Path) -> list[sqlite3.Row]:
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    try:
        return list(conn.execute("SELECT * FROM outbox ORDER BY created_at, id"))
    finally:
        conn.close()


def _row_by_dedup(db_path: Path, dedup_key: str) -> sqlite3.Row:
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    try:
        row = conn.execute(
            "SELECT * FROM outbox WHERE dedup_key=?", (dedup_key,)
        ).fetchone()
        assert row is not None
        return row
    finally:
        conn.close()


def _row_by_id(db_path: Path, row_id: str) -> sqlite3.Row:
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    try:
        row = conn.execute("SELECT * FROM outbox WHERE id=?", (row_id,)).fetchone()
        assert row is not None
        return row
    finally:
        conn.close()


def _result_value(result: Any, key: str) -> Any:
    if isinstance(result, dict):
        return result.get(key)
    if hasattr(result, key):
        return getattr(result, key)
    try:
        return result[key]
    except (TypeError, KeyError, IndexError):
        return None


def _result_id(result: Any, db_path: Path, dedup_key: str) -> str:
    if isinstance(result, str):
        return result
    value = _result_value(result, "id") or _result_value(result, "message_id")
    if value:
        return str(value)
    return str(_row_by_dedup(db_path, dedup_key)["id"])


def test_outbox_insert_is_idempotent(tmp_path: Path):
    store, db_path = _make_store(tmp_path)

    first = _enqueue(store, dedup_key="issue-1:notify", payload={"text": "A"})
    second = _enqueue(store, dedup_key="issue-1:notify", payload={"text": "A"})

    rows = _db_rows(db_path)
    assert len(rows) == 1
    assert rows[0]["dedup_key"] == "issue-1:notify"
    assert rows[0]["channel"] == "telegram"
    assert json.loads(rows[0]["payload_json"]) == {"text": "A"}
    assert _result_id(first, db_path, "issue-1:notify") == _result_id(
        second, db_path, "issue-1:notify"
    )


def test_outbox_read_claims_due_rows_fifo(tmp_path: Path):
    store, db_path = _make_store(tmp_path)
    now = datetime(2026, 5, 12, 12, 0, tzinfo=timezone.utc)
    _enqueue(store, dedup_key="old", payload={"text": "old"}, now=now)
    _enqueue(
        store,
        dedup_key="new",
        payload={"text": "new"},
        now=now + timedelta(seconds=5),
    )

    claimed = _claim_due(store, now=now + timedelta(minutes=1), limit=10)

    claimed_ids = [str(_result_value(row, "id")) for row in claimed]
    expected_ids = [str(row["id"]) for row in _db_rows(db_path)]
    assert claimed_ids[:2] == expected_ids[:2]


def test_outbox_mark_sent_updates_status(tmp_path: Path):
    store, db_path = _make_store(tmp_path)
    row_id = _result_id(_enqueue(store, dedup_key="sent-me"), db_path, "sent-me")

    _mark_sent(store, row_id, now=datetime(2026, 5, 12, 12, 5, tzinfo=timezone.utc))

    row = _row_by_id(db_path, row_id)
    assert row["status"] == "sent"
    assert row["updated_at"]


def test_outbox_retry_counter_records_failure_and_backoff(tmp_path: Path):
    store, db_path = _make_store(tmp_path)
    row_id = _result_id(_enqueue(store, dedup_key="retry-me"), db_path, "retry-me")
    now = datetime(2026, 5, 12, 12, 10, tzinfo=timezone.utc)

    _record_failure(store, row_id, error="telegram 500", now=now)

    row = _row_by_id(db_path, row_id)
    assert row["attempts"] == 1
    assert row["last_error"] == "telegram 500"
    assert row["status"] in {"pending", "failed"}
    assert row["next_retry_at"]
    assert row["next_retry_at"] > now.isoformat()


def test_outbox_dead_letters_after_fifth_failure(tmp_path: Path):
    store, db_path = _make_store(tmp_path)
    row_id = _result_id(_enqueue(store, dedup_key="dead-me"), db_path, "dead-me")
    now = datetime(2026, 5, 12, 12, 20, tzinfo=timezone.utc)

    for attempt in range(5):
        _record_failure(
            store,
            row_id,
            error=f"telegram failure {attempt}",
            now=now + timedelta(minutes=attempt),
        )

    row = _row_by_id(db_path, row_id)
    assert row["attempts"] >= 5
    assert row["status"] == "dead-lettered"


def test_outbox_db_schema_verify_columns_and_indexes(tmp_path: Path):
    _store, db_path = _make_store(tmp_path)
    conn = sqlite3.connect(db_path)
    try:
        columns = {
            row[1]: row[2]
            for row in conn.execute("PRAGMA table_info(outbox)").fetchall()
        }
        assert {
            "id",
            "channel",
            "payload_json",
            "dedup_key",
            "attempts",
            "last_error",
            "status",
            "next_retry_at",
            "created_at",
            "updated_at",
        }.issubset(columns)

        indexes = conn.execute("PRAGMA index_list(outbox)").fetchall()
        unique_indexes = [idx for idx in indexes if idx[2]]
        indexed_columns = set()
        for idx in unique_indexes:
            indexed_columns.update(
                row[2] for row in conn.execute(f"PRAGMA index_info({idx[1]})")
            )
        assert "dedup_key" in indexed_columns
    finally:
        conn.close()
