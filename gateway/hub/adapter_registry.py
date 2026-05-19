"""Adapter registry — Step 8 of pipeline.

Maps channel_type -> adapter class. Phase 1 supports:
  - inbox_mc: NoOp (event already in MC notification_events)
  - telegram: stub fuer integration mit gateway/platforms/telegram.py (Phase 1 mock)

Phase 2: pushover, ntfy native, weitere
Phase 3: email, discord, ha-notify, apprise sidecar
"""
from dataclasses import dataclass
from typing import Optional


@dataclass
class AdapterResult:
    status: str  # 'delivered' | 'failed'
    provider_message_id: Optional[str] = None
    latency_ms: int = 0
    error: Optional[str] = None


# InboxMcAdapter wandert nach gateway/hub/adapters/inbox_mc.py
from gateway.hub.adapters.inbox_mc import InboxMcAdapter  # noqa: E402  (registry-import)


# TelegramAdapter wandert nach gateway/hub/adapters/telegram.py
from gateway.hub.adapters.telegram import TelegramAdapter  # noqa: E402

# PushoverAdapter (Phase 2)
from gateway.hub.adapters.pushover import PushoverAdapter  # noqa: E402


ADAPTER_MAP: dict[str, type] = {
    "inbox_mc": InboxMcAdapter,
    "telegram": TelegramAdapter,
    "pushover": PushoverAdapter,
}


# Module-level adapter cache: each adapter owns its httpx.AsyncClient with a
# connection pool. Without caching, every notification dispatch creates a new
# client and leaks the connection pool (no .close() is ever called). The lazy
# init keeps tests that monkeypatch ADAPTER_MAP / environment-variables
# independent (cache populates on first request).
_adapter_singletons: dict[str, object] = {}


def get_adapter(channel_type: str):
    """Returns adapter singleton for channel-type or None if unsupported.

    Cache key is (channel_type, class). If ADAPTER_MAP swaps the class
    (e.g. test monkeypatch), the cached instance is replaced. This keeps
    monkeypatch test-fixtures working while still avoiding httpx-client
    leaks in production.
    """
    cls = ADAPTER_MAP.get(channel_type)
    if cls is None:
        return None
    cached = _adapter_singletons.get(channel_type)
    if cached is None or type(cached) is not cls:
        cached = cls()
        _adapter_singletons[channel_type] = cached
    return cached


async def close_adapters() -> None:
    """Close all cached adapter httpx clients. Call on app shutdown."""
    for adapter in list(_adapter_singletons.values()):
        close = getattr(adapter, "close", None)
        if callable(close):
            try:
                await close()
            except Exception:
                pass
    _adapter_singletons.clear()


async def dispatch_to_channel_set(channel_set: dict, event: dict) -> list[AdapterResult]:
    """Calls adapter.send for each member of the channel-set in position order.
    Returns list of AdapterResult, one per member."""
    results: list[AdapterResult] = []
    for member in channel_set.get("members", []):
        channel = member.get("channel", {})
        ch_type = channel.get("type")
        adapter = get_adapter(ch_type)
        if adapter is None:
            results.append(AdapterResult(
                status="failed",
                error=f"No adapter for channel_type={ch_type}",
            ))
            continue
        try:
            result = await adapter.send(event, channel)
            results.append(result)
        except Exception as e:
            results.append(AdapterResult(status="failed", error=str(e)))
    return results
