"""Tests for gateway.hub.adapters.telegram."""
import json

import httpx
import pytest

from gateway.hub.adapters.errors import AdapterDeliveryError
from gateway.hub.adapters.telegram import TelegramAdapter


def make_adapter(handler) -> TelegramAdapter:
    transport = httpx.MockTransport(handler)
    client = httpx.AsyncClient(transport=transport)
    return TelegramAdapter(client=client)


@pytest.mark.asyncio
async def test_success_200(monkeypatch) -> None:
    monkeypatch.setenv("TELEGRAM_HERMES_BOT_TOKEN", "test-token")
    captured = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["path"] = request.url.path
        captured["body"] = json.loads(request.content)
        return httpx.Response(200, json={"ok": True, "result": {"message_id": 42}})

    adapter = make_adapter(handler)
    result = await adapter.send(
        event={"body": "Deploy complete"},
        channel={"type": "telegram", "target": "12345"},
    )

    assert captured["path"] == "/bottest-token/sendMessage"
    assert captured["body"]["chat_id"] == "12345"
    assert captured["body"]["parse_mode"] == "MarkdownV2"
    assert result.status == "delivered"
    assert result.provider_message_id == "42"
    await adapter.close()


@pytest.mark.asyncio
async def test_429_rate_limit(monkeypatch) -> None:
    monkeypatch.setenv("TELEGRAM_HERMES_BOT_TOKEN", "test-token")

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(429, json={"ok": False, "description": "Too Many Requests"})

    adapter = make_adapter(handler)
    with pytest.raises(AdapterDeliveryError) as exc_info:
        await adapter.send(event={"body": "Slow down"}, channel={"chat_id": "12345"})

    assert exc_info.value.status == 429
    assert "Too Many Requests" in exc_info.value.body
    await adapter.close()


@pytest.mark.asyncio
async def test_403_forbidden(monkeypatch) -> None:
    monkeypatch.setenv("TELEGRAM_HERMES_BOT_TOKEN", "test-token")

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(403, json={"ok": False, "description": "Forbidden"})

    adapter = make_adapter(handler)
    with pytest.raises(AdapterDeliveryError) as exc_info:
        await adapter.send(event={"body": "No access"}, channel={"target": "12345"})

    assert exc_info.value.status == 403
    assert "Forbidden" in exc_info.value.body
    await adapter.close()


@pytest.mark.asyncio
async def test_timeout(monkeypatch) -> None:
    monkeypatch.setenv("TELEGRAM_HERMES_BOT_TOKEN", "test-token")

    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("timed out", request=request)

    adapter = make_adapter(handler)
    with pytest.raises(AdapterDeliveryError) as exc_info:
        await adapter.send(event={"body": "Timeout"}, channel={"target": "12345"})

    assert exc_info.value.status == "timeout"
    await adapter.close()


@pytest.mark.asyncio
async def test_markdownv2_escape(monkeypatch) -> None:
    monkeypatch.setenv("TELEGRAM_HERMES_BOT_TOKEN", "test-token")
    captured = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["body"] = json.loads(request.content)
        return httpx.Response(200, json={"ok": True, "result": {"message_id": 7}})

    adapter = make_adapter(handler)
    await adapter.send(
        event={"body": "Hello _team_ *now*. Go!"},
        channel={"target": "12345"},
    )

    assert captured["body"]["text"] == r"Hello \_team\_ \*now\*\. Go\!"
    await adapter.close()


@pytest.mark.asyncio
async def test_send_renders_v4c_payload(monkeypatch) -> None:
    """v4c render_version → rich layout with severity prefix, service badge, impact, action."""
    monkeypatch.setenv("TELEGRAM_HERMES_BOT_TOKEN", "test-token")
    captured = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["body"] = json.loads(request.content)
        return httpx.Response(200, json={"ok": True, "result": {"message_id": 42}})

    adapter = make_adapter(handler)
    event = {
        "render_version": "v4c",
        "severity": "warn",
        "title": "T",
        "body": "B",
        "service": "drobo-backup",
        "impact": "Big",
        "action_required": "Fix it",
    }
    result = await adapter.send(event=event, channel={"target_ref": "123"})

    sent_text = captured["body"]["text"]
    assert "*T*" in sent_text
    assert "drobo\\-backup" in sent_text
    assert "*Impact:*" in sent_text
    assert "*Action:*" in sent_text
    assert captured["body"]["parse_mode"] == "MarkdownV2"
    assert result.provider_message_id == "42"
    await adapter.close()


@pytest.mark.asyncio
async def test_edit_calls_editMessageText_with_renderer(monkeypatch) -> None:
    """edit() POSTs to editMessageText with rendered v4c text and returns status='edited'."""
    monkeypatch.setenv("TELEGRAM_HERMES_BOT_TOKEN", "testtok")
    captured = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["path"] = request.url.path
        captured["body"] = json.loads(request.content)
        return httpx.Response(200, json={"ok": True, "result": {"message_id": 42, "edit_date": 1}})

    adapter = make_adapter(handler)
    event = {
        "render_version": "v4c",
        "severity": "warn",
        "title": "T2",
        "body": "B2",
        "service": "drobo-backup",
    }
    result = await adapter.edit(
        event=event,
        channel={"type": "telegram", "target_ref": "12345"},
        message_id="42",
    )

    assert captured["path"] == "/bottesttok/editMessageText"
    assert captured["body"]["chat_id"] == "12345"
    assert captured["body"]["message_id"] == 42
    assert captured["body"]["parse_mode"] == "MarkdownV2"
    assert "*T2*" in captured["body"]["text"]
    assert result.status == "edited"
    assert result.provider_message_id == "42"
    await adapter.close()


@pytest.mark.asyncio
async def test_edit_treats_message_not_modified_as_success(monkeypatch) -> None:
    """Telegram 400 'message is not modified' is idempotent success — must NOT raise."""
    monkeypatch.setenv("TELEGRAM_HERMES_BOT_TOKEN", "testtok")

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            400,
            json={
                "ok": False,
                "error_code": 400,
                "description": "Bad Request: message is not modified",
            },
        )

    adapter = make_adapter(handler)
    result = await adapter.edit(
        event={"render_version": "v4c", "severity": "info", "title": "X", "body": "Y"},
        channel={"target_ref": "12345"},
        message_id="99",
    )
    assert result.status == "edited"
    assert result.provider_message_id == "99"
    await adapter.close()


@pytest.mark.asyncio
async def test_edit_raises_on_unrecoverable_error(monkeypatch) -> None:
    """Non-recoverable errors (e.g. 403) MUST raise so pipeline can fall back to send."""
    monkeypatch.setenv("TELEGRAM_HERMES_BOT_TOKEN", "testtok")

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(403, json={"ok": False, "description": "Forbidden"})

    adapter = make_adapter(handler)
    with pytest.raises(AdapterDeliveryError) as exc_info:
        await adapter.edit(
            event={"render_version": "v4c", "severity": "info", "title": "X", "body": "Y"},
            channel={"target_ref": "12345"},
            message_id="99",
        )
    assert exc_info.value.status == 403
    await adapter.close()


@pytest.mark.asyncio
async def test_send_v4a_still_returns_plain_body(monkeypatch) -> None:
    """v4a render_version → legacy plain body (no v4c sections)."""
    monkeypatch.setenv("TELEGRAM_HERMES_BOT_TOKEN", "test-token")
    captured = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["body"] = json.loads(request.content)
        return httpx.Response(200, json={"ok": True, "result": {"message_id": 7}})

    adapter = make_adapter(handler)
    event = {
        "render_version": "v4a",
        "title": "X",
        "body": "Plain body content",
    }
    await adapter.send(event=event, channel={"target_ref": "123"})

    sent_text = captured["body"]["text"]
    assert "Plain body content" in sent_text
    # No v4c sections
    assert "Impact" not in sent_text
    assert "Action" not in sent_text
    await adapter.close()


# ---- P4 Track A2: Inline-Keyboard (Buttons) Hub-Side Support ---------------


@pytest.mark.asyncio
async def test_send_with_buttons_sets_reply_markup(monkeypatch) -> None:
    """P4 Track A2 Hub-Side: event.payload.buttons must be sent as Telegram
    reply_markup={inline_keyboard: ...}, enabling Hub-path delivery of buttons
    (removes hub-sender refuse-early hack from PR #20)."""
    monkeypatch.setenv("TELEGRAM_HERMES_BOT_TOKEN", "test-token")
    captured = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["body"] = json.loads(request.content)
        return httpx.Response(200, json={"ok": True, "result": {"message_id": 99}})

    adapter = make_adapter(handler)
    buttons = [
        [{"text": "Show", "callback_data": "task_monitor_stalled"},
         {"text": "Retry", "callback_data": "task_monitor_retry"}],
        [{"text": "Ack", "callback_data": "task_monitor_ack"}],
    ]
    event = {
        "title": "Stalled",
        "body": "2 stalled tasks",
        "payload": {"buttons": buttons},
    }
    await adapter.send(event=event, channel={"target_ref": "128314698"})

    sent = captured["body"]
    assert "reply_markup" in sent, "buttons must produce reply_markup field"
    assert sent["reply_markup"] == {"inline_keyboard": buttons}
    await adapter.close()


@pytest.mark.asyncio
async def test_send_without_buttons_omits_reply_markup(monkeypatch) -> None:
    """Backward-compat: events without payload.buttons must not include
    reply_markup (Telegram API rejects empty reply_markup)."""
    monkeypatch.setenv("TELEGRAM_HERMES_BOT_TOKEN", "test-token")
    captured = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["body"] = json.loads(request.content)
        return httpx.Response(200, json={"ok": True, "result": {"message_id": 10}})

    adapter = make_adapter(handler)
    event = {"title": "X", "body": "no buttons here"}
    await adapter.send(event=event, channel={"target_ref": "12345"})

    assert "reply_markup" not in captured["body"]
    await adapter.close()


@pytest.mark.asyncio
async def test_send_with_empty_buttons_list_omits_reply_markup(monkeypatch) -> None:
    """Edge case: payload.buttons=[] is empty, must not produce reply_markup."""
    monkeypatch.setenv("TELEGRAM_HERMES_BOT_TOKEN", "test-token")
    captured = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["body"] = json.loads(request.content)
        return httpx.Response(200, json={"ok": True, "result": {"message_id": 11}})

    adapter = make_adapter(handler)
    event = {"title": "X", "body": "empty buttons", "payload": {"buttons": []}}
    await adapter.send(event=event, channel={"target_ref": "12345"})

    assert "reply_markup" not in captured["body"]
    await adapter.close()


@pytest.mark.asyncio
async def test_edit_with_buttons_sets_reply_markup(monkeypatch) -> None:
    """editMessageText must also forward buttons as reply_markup."""
    monkeypatch.setenv("TELEGRAM_HERMES_BOT_TOKEN", "test-token")
    captured = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["path"] = request.url.path
        captured["body"] = json.loads(request.content)
        return httpx.Response(200, json={"ok": True, "result": {"message_id": 42}})

    adapter = make_adapter(handler)
    buttons = [[{"text": "Ack", "callback_data": "ack"}]]
    event = {"title": "X", "body": "edit with buttons", "payload": {"buttons": buttons}}
    await adapter.edit(event=event, channel={"target_ref": "12345"}, message_id="42")

    assert "/editMessageText" in captured["path"]
    assert captured["body"]["reply_markup"] == {"inline_keyboard": buttons}
    await adapter.close()
