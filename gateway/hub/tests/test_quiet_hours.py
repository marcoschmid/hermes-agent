"""Tests for gateway.hub.quiet_hours."""
from datetime import datetime, timezone

from gateway.hub.quiet_hours import is_in_quiet_hours


def _policy_berlin_night() -> dict:
    return {
        "timezone": "Europe/Berlin",
        "break_glass_min_severity": "crit",
        "windows": [
            {
                "from": "23:00",
                "to": "07:00",
                "days": [0, 1, 2, 3, 4, 5, 6],
                "action": "defer",
            }
        ],
    }


def test_within_window_below_break_glass_returns_action() -> None:
    # 2026-05-14 00:00 UTC = 02:00 Berlin (Sommerzeit) — inside 23:00→07:00
    now = datetime(2026, 5, 14, 0, 0, tzinfo=timezone.utc)
    result = is_in_quiet_hours(_policy_berlin_night(), severity="warn", now=now)
    assert result == "defer"


def test_within_window_at_break_glass_returns_none() -> None:
    now = datetime(2026, 5, 14, 0, 0, tzinfo=timezone.utc)
    result = is_in_quiet_hours(_policy_berlin_night(), severity="crit", now=now)
    assert result is None


def test_outside_window_returns_none() -> None:
    # 2026-05-14 10:00 UTC = 12:00 Berlin — outside 23:00→07:00
    now = datetime(2026, 5, 14, 10, 0, tzinfo=timezone.utc)
    result = is_in_quiet_hours(_policy_berlin_night(), severity="warn", now=now)
    assert result is None


def test_cross_midnight_window_at_2am() -> None:
    # 2026-05-14 00:00 UTC = 02:00 Berlin (Sommerzeit), inside cross-midnight window
    now = datetime(2026, 5, 14, 0, 0, tzinfo=timezone.utc)
    result = is_in_quiet_hours(_policy_berlin_night(), severity="info", now=now)
    assert result == "defer"


def test_tz_aware_berlin_vs_utc() -> None:
    # 2026-05-14 00:00 UTC = 02:00 Europe/Berlin (DST: UTC+2)
    # Window 01:00→03:00 Berlin local — only matches when interpreted in Berlin TZ
    policy = {
        "timezone": "Europe/Berlin",
        "break_glass_min_severity": "crit",
        "windows": [
            {
                "from": "01:00",
                "to": "03:00",
                "days": [0, 1, 2, 3, 4, 5, 6],
                "action": "drop",
            }
        ],
    }
    now = datetime(2026, 5, 14, 0, 0, tzinfo=timezone.utc)
    # In UTC, time is 00:00 — outside 01:00→03:00.
    # But TZ-aware conversion → 02:00 Berlin → inside window.
    result = is_in_quiet_hours(policy, severity="warn", now=now)
    assert result == "drop"


# ===== H3: weekday-scoped cross-midnight =====

def _weekday_night_policy() -> dict:
    """Mon-Fri nights 23:00 → 07:00 next-morning, Europe/Berlin."""
    return {
        "timezone": "Europe/Berlin",
        "break_glass_min_severity": "crit",
        "windows": [
            {
                "from": "23:00",
                "to": "07:00",
                "days": [0, 1, 2, 3, 4],  # Mo-Fr
                "action": "defer",
            }
        ],
    }


def test_h3_friday_late_night_matches_friday_window() -> None:
    """Fri 23:30 Berlin — today is Fri (4) ∈ days, late half of cross-midnight."""
    # Friday 2026-05-15 21:30 UTC = 23:30 Berlin (DST)
    now = datetime(2026, 5, 15, 21, 30, tzinfo=timezone.utc)
    assert is_in_quiet_hours(_weekday_night_policy(), severity="warn", now=now) == "defer"


def test_h3_saturday_early_morning_matches_friday_night_window() -> None:
    """Sat 02:00 Berlin — overnight half should match because yesterday=Fri ∈ days.
    Before the fix: today=Sat (5) ∉ days → returned None, weekday-night window
    silently never triggered for the early-morning half.
    """
    # Saturday 2026-05-16 00:00 UTC = 02:00 Berlin
    now = datetime(2026, 5, 16, 0, 0, tzinfo=timezone.utc)
    assert is_in_quiet_hours(_weekday_night_policy(), severity="warn", now=now) == "defer"


def test_h3_sunday_early_morning_does_not_match_friday_night_window() -> None:
    """Sun 02:00 Berlin — yesterday=Sat (5) ∉ days, must NOT match."""
    # Sunday 2026-05-17 00:00 UTC = 02:00 Berlin
    now = datetime(2026, 5, 17, 0, 0, tzinfo=timezone.utc)
    assert is_in_quiet_hours(_weekday_night_policy(), severity="warn", now=now) is None


def test_h3_saturday_late_does_not_match_weekday_night_window() -> None:
    """Sat 23:30 Berlin — today=Sat (5) ∉ days, must NOT match."""
    # Saturday 2026-05-16 21:30 UTC = 23:30 Berlin
    now = datetime(2026, 5, 16, 21, 30, tzinfo=timezone.utc)
    assert is_in_quiet_hours(_weekday_night_policy(), severity="warn", now=now) is None
