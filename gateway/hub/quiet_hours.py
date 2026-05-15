"""Quiet-hours evaluation — Step 6 of pipeline.

Reads quiet_policies.windows JSON from MC.
Returns either an action ('defer'|'drop'|'escalate') if event falls in
quiet window AND severity does not meet break-glass threshold; else None.

Window format:
  [{"from": "23:00", "to": "07:00", "days": [0..6], "action": "defer"|"drop"|"escalate"}]
days: 0=Mo, 6=So (Python weekday()).
Cross-midnight: if from > to, treat as "from→24:00 + 00:00→to".
"""
from datetime import datetime, time, timezone
from typing import Optional, Literal
from zoneinfo import ZoneInfo


SEVERITY_ORDER = {"debug": 0, "info": 1, "notice": 2, "warn": 3, "error": 4, "crit": 5}

QuietAction = Literal["defer", "drop", "escalate"]


def _parse_hhmm(s: str) -> time:
    h, m = s.split(":")
    return time(int(h), int(m))


def _in_window(now_local: datetime, from_str: str, to_str: str, days: list[int]) -> bool:
    """Return True if *now_local* falls inside [from_str, to_str) on a matching day.

    Days are evaluated by the weekday that "owns" the time slice:
      - same-day window (f <= t): match if today's weekday in days AND f <= nt < t.
      - cross-midnight window (f > t): the late half (nt >= f) belongs to today;
        the early-morning half (nt < t) belongs to yesterday's weekday.
        e.g. window 23:00→07:00 + days=[0..4] (Mon-Fri):
        Friday 23:30 matches (today=Fri ∈ days);
        Saturday 02:00 matches (yesterday=Fri ∈ days);
        Sunday 02:00 does NOT match (yesterday=Sat ∉ days).
    """
    f = _parse_hhmm(from_str)
    t = _parse_hhmm(to_str)
    nt = now_local.time()
    today = now_local.weekday()
    if f <= t:
        return today in days and f <= nt < t
    # Cross-midnight — overnight half belongs to yesterday's weekday slot.
    if nt >= f:
        return today in days
    if nt < t:
        return ((today - 1) % 7) in days
    return False


def is_in_quiet_hours(
    policy: dict,
    severity: str,
    now: Optional[datetime] = None,
) -> Optional[QuietAction]:
    """Returns action if quiet AND severity below break-glass; else None."""
    break_glass = policy.get("break_glass_min_severity", "crit")
    if SEVERITY_ORDER[severity] >= SEVERITY_ORDER[break_glass]:
        return None  # Severity bricht Quiet-Hours

    tz_name = policy.get("timezone", "UTC")
    tz = ZoneInfo(tz_name)
    current = (now or datetime.now(timezone.utc)).astimezone(tz)

    windows = policy.get("windows", [])
    for w in windows:
        if _in_window(current, w["from"], w["to"], w.get("days", list(range(7)))):
            return w.get("action", "defer")
    return None
