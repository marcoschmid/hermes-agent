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


class InboxMcAdapter:
    """No-op adapter — event lives in MC notification_events table.
    Dispatch returns success immediately (delivery handled by MC-side createEvent)."""
    name = "inbox_mc"

    async def send(self, event: dict, channel: dict) -> AdapterResult:
        return AdapterResult(status="delivered", provider_message_id=None, latency_ms=0)


class TelegramAdapterStub:
    """Phase 1 stub. Phase 2 wires up gateway.platforms.telegram for real delivery."""
    name = "telegram"

    async def send(self, event: dict, channel: dict) -> AdapterResult:
        # Phase 1: log-only stub
        return AdapterResult(status="delivered", provider_message_id="stub-tg-msg", latency_ms=10)


ADAPTER_MAP: dict[str, type] = {
    "inbox_mc": InboxMcAdapter,
    "telegram": TelegramAdapterStub,
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
