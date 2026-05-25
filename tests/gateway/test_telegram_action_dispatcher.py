"""Contract tests for gateway.telegram_action_dispatcher (P4 Track B)."""
from __future__ import annotations

import sqlite3
import threading
import time
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import pytest

from gateway.telegram_action_dispatcher import TelegramActionDispatcher
from gateway.telegram_gateway import TelegramCallbackReceiver


SECRET = "test"


def _make_receiver_with_rows(tmp_path: Path, callbacks: list[dict[str, Any]]) -> Path:
    """Build receiver + persist callbacks. Returns db_path."""
    db_path = tmp_path / "cb.sqlite"
    receiver = TelegramCallbackReceiver(
        db_path=str(db_path),
        webhook_secret=SECRET,
        allowed_user_ids={"128314698"},
        rate_limit_per_minute=1000,
    )
    receiver.init_schema()

    for i, cb in enumerate(callbacks):
        payload = {
            "update_id": 1000 + i,
            "callback_query": {
                "id": cb["id"],
                "from": {"id": 128314698, "first_name": "Marco"},
                "message": {"chat": {"id": 128314698}, "message_id": 42 + i},
                "data": cb["data"],
            },
        }
        receiver.handle_webhook(
            headers={"X-Telegram-Bot-Api-Secret-Token": SECRET},
            payload=payload,
        )
    return db_path


def _row(db_path: Path, callback_id: str) -> sqlite3.Row:
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    try:
        return conn.execute("SELECT * FROM telegram_callbacks WHERE callback_id=?",
                            (callback_id,)).fetchone()
    finally:
        conn.close()


# ---- Happy path ------------------------------------------------------------


def test_dispatcher_invokes_handler_for_approve_callback(tmp_path: Path):
    db = _make_receiver_with_rows(tmp_path, [
        {"id": "cb-approve-1", "data": "approve:ISS-42"},
    ])
    approve_handler = MagicMock()
    dispatcher = TelegramActionDispatcher(
        db_path=str(db),
        handlers={"approve": approve_handler, "reject": MagicMock(), "skip": MagicMock()},
    )

    stats = dispatcher.run_once()

    assert stats.fetched == 1
    assert stats.dispatched == 1
    assert stats.failed == 0
    approve_handler.assert_called_once()
    issue_id_arg = approve_handler.call_args.args[0]
    assert issue_id_arg == "ISS-42"

    row = _row(db, "cb-approve-1")
    assert row["processed_at"] is not None


def test_dispatcher_marks_unhandled_callback_type_as_failed(tmp_path: Path):
    # Direct DB insert with unknown type (bypassing receiver which would mark accepted=0)
    db = _make_receiver_with_rows(tmp_path, [])
    conn = sqlite3.connect(str(db))
    conn.execute(
        "INSERT INTO telegram_callbacks "
        "(callback_id, update_id, user_id, callback_type, callback_data, "
        " received_at, accepted) "
        "VALUES ('unhandled-1', 2001, '128', 'rare_type', 'rare:X-1', "
        "'2026-05-25T12:00:00+00:00', 1)"
    )
    conn.commit()
    conn.close()

    dispatcher = TelegramActionDispatcher(
        db_path=str(db),
        handlers={"approve": MagicMock(), "reject": MagicMock(), "skip": MagicMock()},
    )

    stats = dispatcher.run_once()

    assert stats.fetched == 1
    assert stats.failed == 1
    row = _row(db, "unhandled-1")
    assert row["processed_at"] is not None
    assert "no handler" in (row["error"] or "")


def test_dispatcher_skips_unaccepted_rows(tmp_path: Path):
    db = _make_receiver_with_rows(tmp_path, [
        {"id": "cb-known", "data": "approve:ISS-1"},
        {"id": "cb-unknown", "data": "weird:ISS-2"},  # accepted=0
    ])
    handlers = {"approve": MagicMock(), "reject": MagicMock(), "skip": MagicMock()}
    dispatcher = TelegramActionDispatcher(db_path=str(db), handlers=handlers)

    stats = dispatcher.run_once()

    # Only the known/accepted one fetched
    assert stats.fetched == 1
    assert stats.dispatched == 1
    # weird:ISS-2 was persisted with accepted=0, skipped by dispatcher
    weird_row = _row(db, "cb-unknown")
    assert weird_row["processed_at"] is None  # not picked up


def test_dispatcher_handler_exception_marks_failed_with_error(tmp_path: Path):
    db = _make_receiver_with_rows(tmp_path, [
        {"id": "cb-fail", "data": "reject:ISS-bug"},
    ])
    def raising_handler(issue_id: str, _row: dict) -> None:
        raise RuntimeError("paperclip API down")
    dispatcher = TelegramActionDispatcher(
        db_path=str(db),
        handlers={"approve": MagicMock(), "reject": raising_handler, "skip": MagicMock()},
    )

    stats = dispatcher.run_once()

    assert stats.failed == 1
    row = _row(db, "cb-fail")
    assert row["processed_at"] is not None
    assert "paperclip API down" in (row["error"] or "")


def test_dispatcher_processes_only_unprocessed_rows(tmp_path: Path):
    db = _make_receiver_with_rows(tmp_path, [
        {"id": "cb-first", "data": "approve:ISS-1"},
        {"id": "cb-second", "data": "approve:ISS-2"},
    ])
    handler = MagicMock()
    dispatcher = TelegramActionDispatcher(
        db_path=str(db),
        handlers={"approve": handler, "reject": MagicMock(), "skip": MagicMock()},
    )

    stats1 = dispatcher.run_once()
    assert stats1.dispatched == 2

    # Second run: no unprocessed rows left
    stats2 = dispatcher.run_once()
    assert stats2.fetched == 0
    assert stats2.dispatched == 0
    assert handler.call_count == 2  # not re-invoked


def test_dispatcher_run_forever_stops_on_signal(tmp_path: Path):
    db = _make_receiver_with_rows(tmp_path, [])
    dispatcher = TelegramActionDispatcher(
        db_path=str(db),
        handlers={"approve": MagicMock(), "reject": MagicMock(), "skip": MagicMock()},
        poll_interval=0.05,
    )

    thread = threading.Thread(target=dispatcher.run_forever, daemon=True)
    thread.start()
    time.sleep(0.15)
    dispatcher.stop()
    thread.join(timeout=2.0)

    assert not thread.is_alive()


def test_dispatcher_batch_size_limits_rows_per_cycle(tmp_path: Path):
    callbacks = [{"id": f"cb-batch-{i}", "data": f"approve:ISS-{i}"} for i in range(10)]
    db = _make_receiver_with_rows(tmp_path, callbacks)
    handler = MagicMock()
    dispatcher = TelegramActionDispatcher(
        db_path=str(db),
        handlers={"approve": handler, "reject": MagicMock(), "skip": MagicMock()},
        batch_size=3,
    )

    stats1 = dispatcher.run_once()
    assert stats1.fetched == 3
    stats2 = dispatcher.run_once()
    assert stats2.fetched == 3
    stats3 = dispatcher.run_once()
    assert stats3.fetched == 3
    stats4 = dispatcher.run_once()
    assert stats4.fetched == 1
    stats5 = dispatcher.run_once()
    assert stats5.fetched == 0
