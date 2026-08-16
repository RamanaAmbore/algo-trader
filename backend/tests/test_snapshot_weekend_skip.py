"""Tests for the weekend startup-snapshot guard in _task_daily_snapshot.

Root cause fixed: service restart on Saturday/Sunday fired snapshot_daily_book()
with target_date=today, creating stale rows in daily_book that displaced Friday's
EOD data in the latest_batch CTE, making Day P&L = 0 all weekend.

Fix: _task_daily_snapshot skips the startup snapshot when today.weekday() >= 5.

Strategy: test the decision logic (weekday() >= 5 → skip) directly rather than
running the full coroutine, since _probe_nse_mcx is a nested closure.
"""

from __future__ import annotations

from datetime import date


# August 2026 calendar:
#   2026-08-14 = Friday  (weekday=4)
#   2026-08-15 = Saturday (weekday=5)
#   2026-08-16 = Sunday  (weekday=6)
#   2026-08-17 = Monday  (weekday=0)

SAT = date(2026, 8, 15)
SUN = date(2026, 8, 16)
MON = date(2026, 8, 17)
FRI = date(2026, 8, 14)


# ──────────────────────────────────────────────────────────────────────────────
# 1. Weekday sanity (confirms the dates used in tests are correct)
# ──────────────────────────────────────────────────────────────────────────────

def test_weekday_constants():
    assert FRI.weekday() == 4
    assert SAT.weekday() == 5
    assert SUN.weekday() == 6
    assert MON.weekday() == 0


# ──────────────────────────────────────────────────────────────────────────────
# 2. Guard logic: weekend → skip; weekday + closed → fire
# ──────────────────────────────────────────────────────────────────────────────

def _should_fire_startup(today: date, nse_open: bool, mcx_open: bool) -> bool:
    """Mirror the guard added to _task_daily_snapshot (lines 1848–1867)."""
    if today.weekday() >= 5:
        return False          # weekend — skip
    if nse_open or mcx_open:
        return False          # market open — skip
    return True               # weekday, markets closed → fire


def test_saturday_skips():
    assert not _should_fire_startup(SAT, nse_open=False, mcx_open=False)


def test_sunday_skips():
    assert not _should_fire_startup(SUN, nse_open=False, mcx_open=False)


def test_friday_after_close_fires():
    assert _should_fire_startup(FRI, nse_open=False, mcx_open=False)


def test_monday_after_close_fires():
    assert _should_fire_startup(MON, nse_open=False, mcx_open=False)


def test_weekday_nse_open_skips():
    assert not _should_fire_startup(MON, nse_open=True, mcx_open=False)


def test_weekday_mcx_open_skips():
    assert not _should_fire_startup(MON, nse_open=False, mcx_open=True)


def test_weekday_both_open_skips():
    assert not _should_fire_startup(MON, nse_open=True, mcx_open=True)


# Saturday with markets open (MCX Saturday session) — still skip startup snapshot:
# the 23:31 MCX-close settlement pass handles MCX EOD on Saturdays.
def test_saturday_mcx_open_still_skips():
    assert not _should_fire_startup(SAT, nse_open=False, mcx_open=True)


# ──────────────────────────────────────────────────────────────────────────────
# 3. The actual background.py code contains the guard (code-reading test)
# ──────────────────────────────────────────────────────────────────────────────

def test_background_py_contains_weekend_guard():
    """Ensure the weekend guard is present in the source file (regression guard)."""
    import pathlib
    src = pathlib.Path(__file__).parent.parent / "api" / "background.py"
    text = src.read_text()
    assert "_today_d.weekday() >= 5" in text, (
        "Weekend guard missing from background.py _task_daily_snapshot. "
        "The guard `if _today_d.weekday() >= 5` must be present."
    )
    assert "skipping startup snapshot — weekend" in text, (
        "Weekend skip log message not found — guard may have been removed or renamed."
    )
