"""Phase-6b Step-7 write-severance flag.

Single source of truth for "is the hub forbidden from writing to Mission
Control?". When severed, every hub->MC write sink (inbox_mc delivery,
post_audit, persist_deliveries, fingerprint-read, router_factory event-POST,
decision_board_emitter) becomes a no-op so the shared MC api_key (id=60) can be
revoked.

Default OFF (writes enabled) preserves current behaviour — dark switch,
mirrors the HUB_REGISTRY_SOURCE pattern. Read at call-time so the cutover flip
takes effect on the next dispatch without rebuilding any client.
"""
import os

ENV_WRITE_SEVERED = "HUB_MC_WRITE_SEVERED"

_TRUTHY = {"1", "true", "yes", "on"}


def mc_writes_severed() -> bool:
    """True when hub->MC writes are severed (env flag set to a truthy value)."""
    return os.environ.get(ENV_WRITE_SEVERED, "").strip().lower() in _TRUTHY
