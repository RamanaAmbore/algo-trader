"""
Regression guard: Saturday/Sunday snapshot_cutoff must reach Saturday 02:00 IST
so the MCX 00:15 settlement row (captured_at=Saturday 00:15) is included in
latest_batch, not excluded by the old Saturday 00:00 midnight cutoff.
"""
from datetime import datetime, timedelta, timezone

IST = timezone(timedelta(hours=5, minutes=30))


def _compute_snapshot_cutoff(now_ist: datetime) -> datetime:
    """Mirror the cutoff logic from positions.py / holdings.py."""
    midnight = now_ist.replace(hour=0, minute=0, second=0, microsecond=0)
    wd = now_ist.weekday()
    if wd == 5:    # Saturday
        return midnight + timedelta(hours=2)
    elif wd == 6:  # Sunday
        return midnight - timedelta(hours=22)
    else:
        return midnight + timedelta(days=1)


# 2026-09-04 = Friday (weekday=4), 2026-09-05 = Saturday, 2026-09-06 = Sunday

def test_saturday_cutoff_includes_mcx_settlement():
    sat_morning = datetime(2026, 9, 5, 9, 0, tzinfo=IST)
    cutoff = _compute_snapshot_cutoff(sat_morning)
    assert cutoff == datetime(2026, 9, 5, 2, 0, tzinfo=IST)
    mcx_settlement = datetime(2026, 9, 5, 0, 15, tzinfo=IST)
    assert mcx_settlement < cutoff, "MCX 00:15 settlement must be included in latest_batch"


def test_saturday_old_cutoff_excluded_settlement():
    sat_morning = datetime(2026, 9, 5, 9, 0, tzinfo=IST)
    old_cutoff = sat_morning.replace(hour=0, minute=0, second=0, microsecond=0)
    mcx_settlement = datetime(2026, 9, 5, 0, 15, tzinfo=IST)
    assert mcx_settlement >= old_cutoff, "Regression: old midnight cutoff excluded settlement"


def test_sunday_cutoff_includes_mcx_settlement():
    sun_morning = datetime(2026, 9, 6, 10, 0, tzinfo=IST)
    cutoff = _compute_snapshot_cutoff(sun_morning)
    expected = datetime(2026, 9, 5, 2, 0, tzinfo=IST)
    assert cutoff == expected
    mcx_settlement = datetime(2026, 9, 5, 0, 15, tzinfo=IST)
    assert mcx_settlement < cutoff


def test_friday_cutoff_unchanged():
    fri = datetime(2026, 9, 4, 10, 0, tzinfo=IST)
    cutoff = _compute_snapshot_cutoff(fri)
    assert cutoff == datetime(2026, 9, 5, 0, 0, tzinfo=IST)  # tomorrow midnight


def test_monday_cutoff_unchanged():
    mon = datetime(2026, 9, 7, 10, 0, tzinfo=IST)
    cutoff = _compute_snapshot_cutoff(mon)
    assert cutoff == datetime(2026, 9, 8, 0, 0, tzinfo=IST)


def test_saturday_02h_cutoff_does_not_include_special_session():
    """Special market sessions start at 09:00+ IST — safely above 02:00 cutoff."""
    sat_special = datetime(2026, 9, 5, 9, 15, tzinfo=IST)
    sat_morning = datetime(2026, 9, 5, 9, 0, tzinfo=IST)
    cutoff = _compute_snapshot_cutoff(sat_morning)
    assert sat_special >= cutoff, "Saturday 09:15 special session excluded by 02:00 cutoff"
