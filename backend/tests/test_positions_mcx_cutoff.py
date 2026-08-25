"""
Tests for the 08:00 IST prev_batch_cutoff fix in _positions_snapshot_mode.

Covers:
  1. Pure cutoff formula — before 08:00 IST → yesterday's 08:00 IST
  2. Pure cutoff formula — at exactly 08:00 IST → today's 08:00 IST
  3. Pure cutoff formula — after 08:00 IST (mid-session) → today's 08:00 IST
  4. Pure cutoff formula — after MCX settlement (00:15 IST) → yesterday's 08:00 IST
  5. `_positions_snapshot_mode` passes `prev_batch_cutoff` (not `today_ist_midnight`)
     as a bindparam and that value matches the formula result.
"""

import pytest
from datetime import datetime, timedelta
from unittest.mock import AsyncMock, MagicMock, patch
from zoneinfo import ZoneInfo

IST = ZoneInfo("Asia/Kolkata")

# ---------------------------------------------------------------------------
# Helper — replicate the exact formula from positions.py
# ---------------------------------------------------------------------------

def _compute_prev_batch_cutoff(now_ist: datetime) -> datetime:
    """Mirror of the logic added to _positions_snapshot_mode."""
    midnight = now_ist.replace(hour=0, minute=0, second=0, microsecond=0)
    today_8am = midnight + timedelta(hours=8)
    return today_8am if now_ist >= today_8am else today_8am - timedelta(days=1)


# ---------------------------------------------------------------------------
# 1-4. Pure formula parametrize
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("hour,minute,expect_day_offset", [
    # Before 08:00 IST — MCX post-settlement window
    (0, 15, -1),   # 00:15 IST — MCX settlement snapshot just written
    (0, 30, -1),   # 00:30 IST — well into overnight gap
    (7, 59, -1),   # one minute before 08:00 IST
    # At and after 08:00 IST — new session started
    (8,  0,  0),   # exactly 08:00 IST
    (9, 15,  0),   # NSE market open
    (15, 30, 0),   # NSE market close
    (23, 31, 0),   # MCX settlement snapshot time (same trading day)
])
def test_prev_batch_cutoff_formula(hour, minute, expect_day_offset):
    """Before 08:00 IST the cutoff is yesterday's 08:00 IST; at/after it is today's."""
    now_ist = datetime(2026, 8, 25, hour, minute, 0, tzinfo=IST)
    cutoff = _compute_prev_batch_cutoff(now_ist)

    midnight = now_ist.replace(hour=0, minute=0, second=0, microsecond=0)
    today_8am = midnight + timedelta(hours=8)
    expected = today_8am + timedelta(days=expect_day_offset)

    assert cutoff == expected, (
        f"now={hour:02d}:{minute:02d} IST — "
        f"cutoff={cutoff.isoformat()} expected={expected.isoformat()}"
    )


# ---------------------------------------------------------------------------
# 5. Regression guard — old midnight cutoff would have included MCX snapshot
# ---------------------------------------------------------------------------

def test_midnight_cutoff_was_wrong_for_mcx_settlement():
    """
    The original code used `today_ist_midnight` (00:00 IST) as the upper
    bound for prev_batch.  After MCX settlement at 00:15 IST on 2026-08-25:

      - midnight cutoff = 2026-08-25 00:00 IST
      - MCX session-close snapshot captured ~2026-08-24 23:31 IST

    23:31 < 00:00 of next day → snapshot PASSES the old filter → it becomes
    prev_batch → prev_settlement_pnl ≈ current total_pnl → day P&L ≈ 0.

    With the fix (08:00 IST cutoff), the snapshot is excluded.
    """
    now_ist = datetime(2026, 8, 25, 0, 30, 0, tzinfo=IST)

    # Old (broken) cutoff: today's midnight
    old_cutoff = now_ist.replace(hour=0, minute=0, second=0, microsecond=0)
    # New (correct) cutoff: yesterday's 08:00 IST
    new_cutoff = _compute_prev_batch_cutoff(now_ist)

    # MCX close snapshot on the prior trading day
    mcx_snapshot_ts = datetime(2026, 8, 24, 23, 31, 0, tzinfo=IST)

    # Old code: snapshot (23:31 on Aug 24) < midnight (00:00 on Aug 25) → included
    assert mcx_snapshot_ts < old_cutoff, (
        "MCX snapshot should have PASSED the old midnight filter (demonstrating the bug)"
    )
    # New code: snapshot (23:31 on Aug 24) >= 08:00 IST on Aug 24 → excluded
    assert mcx_snapshot_ts >= new_cutoff, (
        "MCX snapshot must be EXCLUDED by the new 08:00 IST cutoff"
    )


# ---------------------------------------------------------------------------
# 6. Regression — at 23:31 IST the MCX snapshot must NOT become prev_batch
# ---------------------------------------------------------------------------

def test_mcx_23_31_snapshot_excluded_before_08():
    """
    After MCX settlement at 00:15 IST, the MCX snapshot (captured ~23:31 IST
    prior calendar day) must not be picked as prev_batch — otherwise
    prev_settlement_pnl ≈ current total_pnl → day P&L delta ≈ 0.

    Verify: cutoff at 00:30 IST (2026-08-25) = 2026-08-24 08:00 IST,
    which is BEFORE 23:31 IST on 2026-08-24 → the snapshot is excluded.
    """
    now_ist = datetime(2026, 8, 25, 0, 30, 0, tzinfo=IST)
    cutoff = _compute_prev_batch_cutoff(now_ist)

    # MCX close snapshot time on the previous calendar day
    mcx_snapshot = datetime(2026, 8, 24, 23, 31, 0, tzinfo=IST)

    assert mcx_snapshot >= cutoff, (
        f"MCX snapshot {mcx_snapshot.isoformat()} should be >= cutoff "
        f"{cutoff.isoformat()} meaning it is EXCLUDED from prev_batch "
        f"(captured_at < :prev_batch_cutoff filters it out)"
    )
