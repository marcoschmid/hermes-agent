"""Phase-3 G3 contract tests for Hermes Telegram callback handling.

Target production surface:

* ``gateway.telegram_gateway.TelegramCallbackReceiver``
* durable callback persistence table
* Telegram webhook secret-token verification
* duplicate callback idempotency
* unknown callback type logging
* rate-limit handling

Until ``gateway.telegram_gateway`` exists, these tests are expected to fail and
should be routed to Phase-4 code apply.
"""

from __future__ import annotations

from datetime import datetime, timezone
import importlib
import inspect
import logging
from pathlib import Path
import sqlite3
from typing import Any

import pytest


SECRET_HEADER = "X-Telegram-Bot-Api-Secret-Token"


def _load_gateway_module():
    try:
        return importlib.import_module("gateway.telegram_gateway")
    except ModuleNotFoundError as exc:
        if exc.name == "gateway.telegram_gateway":
            pytest.fail(
                "ROUTE_TO_PHASE_4_CODE_APPLY: gateway.telegram_gateway fehlt. "
                "Phase-3 G3 Callback-Tests koennen erst gruen werden, wenn "
                "TelegramCallbackReceiver + Persistenz/Replay-Schutz implementiert sind.",
                pytrace=False,
            )
        raise


def _make_receiver(tmp_path: Path, *, rate_limit_per_minute: int = 60) -> tuple[Any, Path]:
    module = _load_gateway_module()
    receiver_cls = getattr(module, "TelegramCallbackReceiver", None)
    if receiver_cls is None:
        pytest.fail(
            "ROUTE_TO_PHASE_4_CODE_APPLY: "
            "gateway.telegram_gateway.TelegramCallbackReceiver fehlt.",
            pytrace=False,
        )

    db_path = tmp_path / "telegram-callbacks.sqlite"
    receiver = receiver_cls(
        db_path=str(db_path),
        webhook_secret="secret-token",
        allowed_user_ids={"128314698"},
        rate_limit_per_minute=rate_limit_per_minute,
    )
    for initializer in ("init_schema", "initialize", "setup"):
        method = getattr(receiver, initializer, None)
        if callable(method):
            method()
            break
    return receiver, db_path


def _callback_payload(
    callback_id: str,
    data: str = "approve:ISS-1",
    *,
    user_id: int = 128314698,
    update_id: int = 1001,
) -> dict[str, Any]:
    return {
        "update_id": update_id,
        "callback_query": {
            "id": callback_id,
            "from": {"id": user_id, "first_name": "Marco"},
            "message": {"chat": {"id": 128314698}, "message_id": 42},
            "data": data,
        },
    }


def _handle(receiver: Any, payload: dict[str, Any], *, secret: str = "secret-token"):
    method = getattr(receiver, "handle_webhook", None)
    if not callable(method):
        pytest.fail(
            "ROUTE_TO_PHASE_4_CODE_APPLY: "
            "TelegramCallbackReceiver.handle_webhook fehlt.",
            pytrace=False,
        )
    kwargs = {
        "headers": {SECRET_HEADER: secret},
        "payload": payload,
        "now": datetime(2026, 5, 12, 12, 0, tzinfo=timezone.utc),
    }
    signature = inspect.signature(method)
    accepted = {
        key: value
        for key, value in kwargs.items()
        if key in signature.parameters
        or any(p.kind == p.VAR_KEYWORD for p in signature.parameters.values())
    }
    return method(**accepted)


def _value(result: Any, key: str) -> Any:
    if isinstance(result, dict):
        return result.get(key)
    if hasattr(result, key):
        return getattr(result, key)
    try:
        return result[key]
    except (TypeError, KeyError, IndexError):
        return None


def _callback_rows(db_path: Path) -> list[sqlite3.Row]:
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    try:
        tables = {
            row[0]
            for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            ).fetchall()
        }
        assert "telegram_callbacks" in tables
        return list(conn.execute("SELECT * FROM telegram_callbacks"))
    finally:
        conn.close()


def test_webhook_signature_verify_rejects_wrong_secret(tmp_path: Path):
    receiver, _db_path = _make_receiver(tmp_path)

    result = _handle(receiver, _callback_payload("cb-wrong-secret"), secret="wrong")

    assert _value(result, "status_code") in {401, "401"}
    assert _value(result, "accepted") is False


def test_callback_persisted_in_db(tmp_path: Path):
    receiver, db_path = _make_receiver(tmp_path)

    result = _handle(receiver, _callback_payload("cb-persist", data="approve:ISS-42"))

    assert _value(result, "accepted") is True
    rows = _callback_rows(db_path)
    assert len(rows) == 1
    assert rows[0]["callback_id"] == "cb-persist"
    assert rows[0]["callback_type"] == "approve"


def test_duplicate_webhook_is_idempotent(tmp_path: Path):
    receiver, db_path = _make_receiver(tmp_path)
    payload = _callback_payload("cb-duplicate", data="reject:ISS-42")

    first = _handle(receiver, payload)
    duplicate = _handle(receiver, payload)

    assert _value(first, "accepted") is True
    assert _value(duplicate, "duplicate") is True
    assert len(_callback_rows(db_path)) == 1


def test_unknown_callback_type_is_logged_and_ignored(tmp_path: Path, caplog):
    receiver, db_path = _make_receiver(tmp_path)
    caplog.set_level(logging.WARNING)

    result = _handle(receiver, _callback_payload("cb-unknown", data="weird:ISS-42"))

    assert _value(result, "accepted") is False
    assert _value(result, "ignored") is True
    assert len(_callback_rows(db_path)) == 1
    assert "unknown" in caplog.text.lower()


def test_rate_limit_handling(tmp_path: Path):
    receiver, _db_path = _make_receiver(tmp_path, rate_limit_per_minute=1)

    first = _handle(receiver, _callback_payload("cb-rate-1", update_id=1))
    limited = _handle(receiver, _callback_payload("cb-rate-2", update_id=2))

    assert _value(first, "accepted") is True
    assert _value(limited, "status_code") in {429, "429"}
    assert _value(limited, "rate_limited") is True
