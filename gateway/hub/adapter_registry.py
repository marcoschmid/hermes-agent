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


ADAPTER_MAP: dict[str, type] = {
    "inbox_mc": InboxMcAdapter,
    "telegram": TelegramAdapter,
}


def get_adapter(channel_type: str):
    """Returns adapter instance for channel-type or None if unsupported."""
    cls = ADAPTER_MAP.get(channel_type)
    if cls is None:
        return None
    return cls()


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
