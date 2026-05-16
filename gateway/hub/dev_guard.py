"""Dev-source guard: prevents test/dev sources from polluting prod channels.

Notification-Hub smoke-tests, integration probes, and one-off dev pings must
not reach Telegram/Pushover/ntfy etc. They should be visible in the MC inbox
(audit trail) but never push to a production user channel.

Convention: a source is considered "dev" when its slug matches one of these
patterns:
  - ends with `-dev`, `-test`, or `-smoketest`
  - starts with `test-`, `smoketest-`, or `tmp-`

The pipeline filters non-inbox channels for dev sources; inbox_mc remains
the always-allowed audit sink.
"""
from __future__ import annotations

_DEV_SUFFIXES = ("-dev", "-test", "-smoketest")
_DEV_PREFIXES = ("test-", "smoketest-", "tmp-")


def is_dev_source(slug: str | None) -> bool:
    if not slug:
        return False
    s = slug.lower()
    return s.endswith(_DEV_SUFFIXES) or s.startswith(_DEV_PREFIXES)
