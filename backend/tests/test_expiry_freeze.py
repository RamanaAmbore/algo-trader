"""
Tests for backend/api/algo/expiry_freeze.py — the expiry-day-final
snapshot freeze fix (2026-09 investigation, accounts ZG0790/GOLD and
ZJ6294/GOLDM).

Five quality dimensions:
  SSOT        — expiry_status() is the single classifier used by both the
                daily_snapshot.py prune and positions.py serving fixes.
  Correctness — expired-and-closed-on-own-expiry-day vs. ordinary-closed-
                with-time-left is verified as a hard boundary; the 08:00
                IST next-market-open (not bare midnight / not calendar-day)
                boundary is verified against a real Friday-expiry-then-
                weekend scenario and a holiday-Monday scenario.
  Performance — pure in-memory mocks; no network or real DB calls.
  Reuse       — reuses holidays_store.get_or_fetch_holidays (not a
                hand-rolled parallel calendar) — verified via mock call.
  UX          — a row must never flip from "frozen" to "refresh_eligible"
                before the operator's own stated boundary (8AM next
                market-open day), and never stay "frozen" past it either.
"""

from __future__ import annotations

import asyncio
from datetime import date, datetime, timezone
from unittest.mock import AsyncMock, MagicMock, patch
from zoneinfo import ZoneInfo

import pytest

_IST = ZoneInfo("Asia/Kolkata")


def _utc(y, m, d, h, mi, s=0):
    return datetime(y, m, d, h, mi, s, tzinfo=timezone.utc)


# ---------------------------------------------------------------------------
# session_date_of
# ---------------------------------------------------------------------------

def test_session_date_of_post_midnight_write_attributed_to_prior_day():
    """A settlement write at 2026-09-25 18:30 UTC (= 2026-09-26 00:00 IST,
    just after midnight) must be attributed to the 2026-09-25 trading
    session — the same session as its own earlier same-day 23:4x write —
    matching positions.py's _SESSION_ANCHOR_CUTOFF_TS_SQL precedent."""
    from backend.api.algo.expiry_freeze import session_date_of

    captured_at = _utc(2026, 9, 25, 18, 30, 19)
    assert session_date_of(captured_at) == date(2026, 9, 25)


def test_session_date_of_mid_session_write():
    """A normal EOD write at 18:16 UTC (23:46 IST same day) is attributed
    to that same calendar day's session."""
    from backend.api.algo.expiry_freeze import session_date_of

    captured_at = _utc(2026, 9, 25, 18, 16, 16)
    assert session_date_of(captured_at) == date(2026, 9, 25)


# ---------------------------------------------------------------------------
# expiry_if_closed_on_own_expiry_day — expired vs. closed distinction
# ---------------------------------------------------------------------------

def test_expiry_final_row_returns_expiry_date():
    """GOLDM26SEP155000PE, last captured during the session that equals
    its OWN real expiry (2026-09-25, last Friday of Sept 2026) — this is
    genuinely the operator's 'last minute of expiry day' snapshot."""
    from backend.api.algo.expiry_freeze import expiry_if_closed_on_own_expiry_day

    captured_at = _utc(2026, 9, 25, 18, 30, 19)  # session date 2026-09-25
    result = expiry_if_closed_on_own_expiry_day("GOLDM26SEP155000PE", captured_at)
    assert result == date(2026, 9, 25)


def test_ordinary_closed_position_not_treated_as_expired():
    """GOLD26SEP153000CE closed via normal trading on 2026-09-18 — a full
    week before its real 2026-09-25 expiry (confirmed real prod data,
    account ZG0790). Must return None: this is an ORDINARY closed
    position, NOT an expiry artifact, and must keep using the existing
    unmodified 7-day sweep."""
    from backend.api.algo.expiry_freeze import expiry_if_closed_on_own_expiry_day

    captured_at = _utc(2026, 9, 18, 18, 30, 19)  # session date 2026-09-18
    result = expiry_if_closed_on_own_expiry_day("GOLD26SEP153000CE", captured_at)
    assert result is None


def test_non_fo_symbol_returns_none():
    """A plain equity symbol has no expiry concept."""
    from backend.api.algo.expiry_freeze import expiry_if_closed_on_own_expiry_day

    result = expiry_if_closed_on_own_expiry_day("RELIANCE", _utc(2026, 9, 25, 10, 0))
    assert result is None


def test_empty_marker_symbol_never_matches():
    """The confirmed-empty sentinel symbol must never be classified as an
    expiry-frozen candidate under any circumstance."""
    from backend.api.algo.expiry_freeze import (
        EMPTY_MARKER_SYMBOL, expiry_if_closed_on_own_expiry_day,
    )

    result = expiry_if_closed_on_own_expiry_day(EMPTY_MARKER_SYMBOL, _utc(2026, 9, 25, 10, 0))
    assert result is None


# ---------------------------------------------------------------------------
# next_market_open_ist — 08:00 IST next-MARKET-OPEN boundary (not bare
# next-calendar-day)
# ---------------------------------------------------------------------------

def _patch_holidays(holidays: set[date]):
    async def _fake_get_or_fetch_holidays(exchange, year=None):
        return holidays
    return patch(
        "backend.api.persistence.holidays_store.get_or_fetch_holidays",
        side_effect=_fake_get_or_fetch_holidays,
    )


def _patch_no_special_sessions():
    mock_result = MagicMock()
    mock_result.first.return_value = None
    mock_session = AsyncMock()
    mock_session.execute = AsyncMock(return_value=mock_result)
    mock_session.__aenter__ = AsyncMock(return_value=mock_session)
    mock_session.__aexit__ = AsyncMock(return_value=False)
    return patch("backend.api.database.async_session", return_value=mock_session)


def test_next_market_open_skips_weekend_to_monday():
    """Friday 2026-09-25 expiry (real GOLDM incident date) — next market
    open must be Monday 2026-09-28 08:00 IST, NOT Saturday 08:00 (which
    would be a bare 'next calendar day' boundary — explicitly wrong per
    the operator's own 'next market open day' wording)."""
    from backend.api.algo.expiry_freeze import next_market_open_ist

    with _patch_holidays(set()), _patch_no_special_sessions():
        boundary = asyncio.run(next_market_open_ist(date(2026, 9, 25), exchange="MCX"))

    assert boundary == datetime(2026, 9, 28, 8, 0, tzinfo=_IST), (
        f"Expected Monday 2026-09-28 08:00 IST, got {boundary}"
    )


def test_next_market_open_skips_holiday_monday():
    """When the following Monday is ALSO a configured holiday, the
    boundary must roll to Tuesday — proving the holiday calendar
    (holidays_store, not hand-rolled logic) is actually consulted."""
    from backend.api.algo.expiry_freeze import next_market_open_ist

    with _patch_holidays({date(2026, 9, 28)}), _patch_no_special_sessions():
        boundary = asyncio.run(next_market_open_ist(date(2026, 9, 25), exchange="MCX"))

    assert boundary == datetime(2026, 9, 29, 8, 0, tzinfo=_IST)


def test_next_market_open_reuses_holidays_store_not_hand_rolled():
    """Verify next_market_open_ist actually calls the canonical
    holidays_store helper (reuse requirement) rather than re-deriving its
    own holiday set."""
    from backend.api.algo.expiry_freeze import next_market_open_ist

    calls = []

    async def _fake(exchange, year=None):
        calls.append((exchange, year))
        return set()

    with patch("backend.api.persistence.holidays_store.get_or_fetch_holidays",
               side_effect=_fake), _patch_no_special_sessions():
        asyncio.run(next_market_open_ist(date(2026, 9, 25), exchange="MCX"))

    assert calls, "next_market_open_ist must call get_or_fetch_holidays"
    assert all(c[0] == "MCX" for c in calls)


# ---------------------------------------------------------------------------
# expiry_status — the combined classifier
# ---------------------------------------------------------------------------

def test_expiry_status_frozen_before_boundary():
    """Real ZJ6294/GOLDM scenario: expiry 2026-09-25 (Friday), 'now' is
    Monday 2026-09-28 02:13 IST (before that day's 08:00 open) — the row
    must still be 'frozen'."""
    from backend.api.algo.expiry_freeze import expiry_status

    captured_at = _utc(2026, 9, 25, 18, 30, 19)  # session 2026-09-25 == expiry
    now_ist = datetime(2026, 9, 28, 2, 13, tzinfo=_IST)

    with _patch_holidays(set()), _patch_no_special_sessions():
        status = asyncio.run(
            expiry_status("GOLDM26SEP155000PE", captured_at, "MCX", now_ist)
        )
    assert status == "frozen"


def test_expiry_status_refresh_eligible_after_boundary():
    """Same row, but 'now' has crossed Monday 2026-09-28 08:00 IST — must
    become 'refresh_eligible'."""
    from backend.api.algo.expiry_freeze import expiry_status

    captured_at = _utc(2026, 9, 25, 18, 30, 19)
    now_ist = datetime(2026, 9, 28, 8, 0, 1, tzinfo=_IST)

    with _patch_holidays(set()), _patch_no_special_sessions():
        status = asyncio.run(
            expiry_status("GOLDM26SEP155000PE", captured_at, "MCX", now_ist)
        )
    assert status == "refresh_eligible"


def test_expiry_status_not_expiry_for_ordinary_closed_position():
    """ZG0790/GOLD real scenario — closed a week before real expiry. Must
    be 'not_expiry' regardless of 'now', and must NOT touch the holiday
    calendar at all (no DB/holiday lookup needed for this classification)."""
    from backend.api.algo.expiry_freeze import expiry_status

    captured_at = _utc(2026, 9, 18, 18, 30, 19)  # session 2026-09-18 != expiry 2026-09-25
    now_ist = datetime(2026, 9, 28, 2, 13, tzinfo=_IST)

    with patch("backend.api.persistence.holidays_store.get_or_fetch_holidays") as mock_hol:
        status = asyncio.run(
            expiry_status("GOLD26SEP153000CE", captured_at, "MCX", now_ist)
        )
    assert status == "not_expiry"
    mock_hol.assert_not_called()


# ---------------------------------------------------------------------------
# is_live_row_past_freeze_window — the LIVE broker-fetch-path fix
# (2026-09-29 GOLDM incident, account ZJ6294:
# `positions.py:_filter_expired_live_rows` / `_fetch()`)
# ---------------------------------------------------------------------------

def test_live_row_before_boundary_not_past_freeze():
    """Real ZJ6294/GOLDM scenario: expiry Friday 2026-09-25, 'now' is
    Monday 2026-09-28 02:13 IST — BEFORE that Monday's 08:00 open. Must
    be False (still inside its freeze window, must keep showing live)."""
    from backend.api.algo.expiry_freeze import is_live_row_past_freeze_window

    now_ist = datetime(2026, 9, 28, 2, 13, tzinfo=_IST)
    with _patch_holidays(set()), _patch_no_special_sessions():
        result = asyncio.run(
            is_live_row_past_freeze_window("GOLDM26SEP148000PE", "MCX", now_ist)
        )
    assert result is False


def test_live_row_at_boundary_is_past_freeze():
    """Same contract, 'now' has crossed Monday 2026-09-28 08:00 IST — the
    freeze window has ended; this row must be filtered from the live
    broker fetch (the actual 2026-09-29 production bug: Kite kept
    returning it past this exact boundary)."""
    from backend.api.algo.expiry_freeze import is_live_row_past_freeze_window

    now_ist = datetime(2026, 9, 28, 8, 0, 1, tzinfo=_IST)
    with _patch_holidays(set()), _patch_no_special_sessions():
        result = asyncio.run(
            is_live_row_past_freeze_window("GOLDM26SEP148000PE", "MCX", now_ist)
        )
    assert result is True


def test_live_row_same_day_as_expiry_not_past_freeze():
    """A contract expiring TODAY (session still legitimately trading) must
    never be filtered — this is the fast, no-I/O path: expiry == today."""
    from backend.api.algo.expiry_freeze import is_live_row_past_freeze_window

    now_ist = datetime(2026, 9, 25, 14, 0, tzinfo=_IST)  # expiry day itself
    with patch("backend.api.persistence.holidays_store.get_or_fetch_holidays") as mock_hol:
        result = asyncio.run(
            is_live_row_past_freeze_window("GOLDM26SEP148000PE", "MCX", now_ist)
        )
    assert result is False
    mock_hol.assert_not_called()


def test_live_row_future_expiry_not_past_freeze_no_io():
    """A currently-valid contract (expiry well in the future) must resolve
    False on the fast, no-I/O path — this is the overwhelming majority
    case exercised on every positions poll."""
    from backend.api.algo.expiry_freeze import is_live_row_past_freeze_window

    now_ist = datetime(2026, 9, 28, 10, 0, tzinfo=_IST)
    with patch("backend.api.persistence.holidays_store.get_or_fetch_holidays") as mock_hol:
        result = asyncio.run(
            is_live_row_past_freeze_window("GOLDM26OCT150000PE", "MCX", now_ist)
        )
    assert result is False
    mock_hol.assert_not_called()


def test_live_row_non_fo_symbol_always_false():
    """Equity/cash symbols have no expiry concept — always False, never a
    DB call."""
    from backend.api.algo.expiry_freeze import is_live_row_past_freeze_window

    now_ist = datetime(2026, 9, 28, 10, 0, tzinfo=_IST)
    result = asyncio.run(is_live_row_past_freeze_window("RELIANCE", "NSE", now_ist))
    assert result is False


def test_live_row_empty_marker_symbol_always_false():
    from backend.api.algo.expiry_freeze import (
        EMPTY_MARKER_SYMBOL, is_live_row_past_freeze_window,
    )

    now_ist = datetime(2026, 9, 28, 10, 0, tzinfo=_IST)
    result = asyncio.run(
        is_live_row_past_freeze_window(EMPTY_MARKER_SYMBOL, "MCX", now_ist)
    )
    assert result is False
