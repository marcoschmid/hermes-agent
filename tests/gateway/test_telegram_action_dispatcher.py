"""Contract tests for gateway.telegram_action_dispatcher (P4 Track B Round-2)."""
from __future__ import annotations

import sqlite3
import threading
import time
from datetime import datetime, timedelta, timezone
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

    assert stats.claimed == 1
    assert stats.dispatched == 1
    assert stats.failed == 0
    approve_handler.assert_called_once()
    issue_id_arg = approve_handler.call_args.args[0]
    assert issue_id_arg == "ISS-42"

    row = _row(db, "cb-approve-1")
    assert row["dispatch_status"] == "processed"
    assert row["processed_at"] is not None
    assert row["claim_token"] is None  # fence released after processed


def test_dispatcher_marks_unhandled_callback_type_as_permanent_failure(tmp_path: Path):
    """Unknown callback_type → dispatch_status='failed' permanently (no retry)."""
    db = _make_receiver_with_rows(tmp_path, [])
    # Direct insert bypassing receiver to simulate accepted=1 + unknown type
    conn = sqlite3.connect(str(db))
    # init_schema is via TelegramCallbackReceiver, but db already has schema from _make_receiver
    conn.execute(
        "INSERT INTO telegram_callbacks "
        "(callback_id, update_id, user_id, callback_type, callback_data, "
        " received_at, accepted, dispatch_status, attempts) "
        "VALUES ('unhandled-1', 2001, '128', 'rare_type', 'rare:X-1', "
        "'2026-05-25T12:00:00+00:00', 1, 'pending', 0)"
    )
    conn.commit()
    conn.close()

    dispatcher = TelegramActionDispatcher(
        db_path=str(db),
        handlers={"approve": MagicMock(), "reject": MagicMock(), "skip": MagicMock()},
    )

    stats = dispatcher.run_once()

    assert stats.claimed == 1
    assert stats.failed == 1
    row = _row(db, "unhandled-1")
    assert row["dispatch_status"] == "failed"
    assert row["processed_at"] is not None
    assert "no handler" in (row["last_error"] or "")


def test_dispatcher_skips_unaccepted_rows(tmp_path: Path):
    db = _make_receiver_with_rows(tmp_path, [
        {"id": "cb-known", "data": "approve:ISS-1"},
        {"id": "cb-unknown", "data": "weird:ISS-2"},  # accepted=0, dispatch_status='ignored'
    ])
    handlers = {"approve": MagicMock(), "reject": MagicMock(), "skip": MagicMock()}
    dispatcher = TelegramActionDispatcher(db_path=str(db), handlers=handlers)

    stats = dispatcher.run_once()

    # Only the known/accepted/pending one claimed
    assert stats.claimed == 1
    assert stats.dispatched == 1
    weird_row = _row(db, "cb-unknown")
    assert weird_row["dispatch_status"] == "ignored"
    assert weird_row["processed_at"] is None


def test_dispatcher_transient_handler_failure_schedules_retry(tmp_path: Path):
    """Round-2 HIGH-2: handler exception increments attempts + schedules backoff."""
    db = _make_receiver_with_rows(tmp_path, [
        {"id": "cb-transient", "data": "reject:ISS-bug"},
    ])
    def raising_handler(issue_id: str, _row: dict) -> None:
        raise RuntimeError("paperclip API down")
    dispatcher = TelegramActionDispatcher(
        db_path=str(db),
        handlers={"approve": MagicMock(), "reject": raising_handler, "skip": MagicMock()},
    )

    now = datetime(2026, 5, 25, 12, 0, tzinfo=timezone.utc)
    stats = dispatcher.run_once(now=now)

    assert stats.failed == 1
    assert stats.dead_lettered == 0
    row = _row(db, "cb-transient")
    assert row["dispatch_status"] == "pending"  # back to pending for retry
    assert row["attempts"] == 1
    assert "paperclip API down" in (row["last_error"] or "")
    # Backoff scheduled in future
    assert row["next_retry_at"] > now.isoformat()
    # Claim released
    assert row["claim_token"] is None


def test_dispatcher_dead_letters_after_threshold_failures(tmp_path: Path):
    """Round-2 HIGH-2: after DEAD_LETTER_THRESHOLD failures → dispatch_status='dead-lettered'."""
    db = _make_receiver_with_rows(tmp_path, [
        {"id": "cb-dead", "data": "approve:ISS-dead"},
    ])
    def raising_handler(issue_id: str, _row: dict) -> None:
        raise RuntimeError("permanent test failure")
    dispatcher = TelegramActionDispatcher(
        db_path=str(db),
        handlers={"approve": raising_handler, "reject": MagicMock(), "skip": MagicMock()},
    )

    base_now = datetime(2026, 5, 25, 12, 0, tzinfo=timezone.utc)
    final_stats = None
    for i in range(5):
        cycle_time = base_now + timedelta(hours=i)
        # Reset next_retry_at + status to make row claimable each cycle
        with sqlite3.connect(str(db)) as conn:
            conn.execute(
                "UPDATE telegram_callbacks SET dispatch_status='pending', "
                "next_retry_at=?, claim_token=NULL WHERE callback_id='cb-dead'",
                (cycle_time.isoformat(),),
            )
        final_stats = dispatcher.run_once(now=cycle_time)

    row = _row(db, "cb-dead")
    assert row["dispatch_status"] == "dead-lettered"
    assert row["attempts"] >= 5
    assert final_stats.dead_lettered == 1


def test_dispatcher_claim_token_fences_stale_workers(tmp_path: Path):
    """Round-2 HIGH-5: two dispatcher instances cannot double-dispatch."""
    db = _make_receiver_with_rows(tmp_path, [
        {"id": "cb-fence", "data": "approve:ISS-fence"},
    ])
    handler_a = MagicMock()
    handler_b = MagicMock()

    dispatcher_a = TelegramActionDispatcher(
        db_path=str(db),
        handlers={"approve": handler_a, "reject": MagicMock(), "skip": MagicMock()},
    )
    dispatcher_b = TelegramActionDispatcher(
        db_path=str(db),
        handlers={"approve": handler_b, "reject": MagicMock(), "skip": MagicMock()},
    )

    # A claims first (UPDATE...RETURNING is atomic)
    stats_a = dispatcher_a.run_once()
    stats_b = dispatcher_b.run_once()

    # Only one of them claims; the other sees nothing
    assert stats_a.claimed + stats_b.claimed == 1
    total_handler_calls = handler_a.call_count + handler_b.call_count
    assert total_handler_calls == 1


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

    s1 = dispatcher.run_once()
    s2 = dispatcher.run_once()
    s3 = dispatcher.run_once()
    s4 = dispatcher.run_once()
    s5 = dispatcher.run_once()

    assert s1.claimed == 3
    assert s2.claimed == 3
    assert s3.claimed == 3
    assert s4.claimed == 1
    assert s5.claimed == 0
    assert handler.call_count == 10
