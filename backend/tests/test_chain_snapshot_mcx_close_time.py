"""
test_chain_snapshot_mcx_close_time.py — `GET /api/options/chain-snapshot`
never passed `close_time=` to `days_to_expiry()` at all, so every call
silently defaulted to the NFO/equity 15:30 IST close even for an MCX
underlying (confirmed: the SAME handler already branches Greeks to
`greeks_76`/Black-76 via `is_mcx_underlying(und)` for that exact case).
This was wrong by 8+ hours every single day of the year, independent
of the separate MCX DST-window issue fixed in `/analytics` and
`/strategy-analytics` (see CLAUDE.md "MCX DST-aware non-agri close
time").

Fix (2026-11-21): both `days_to_expiry()` call sites inside
`chain_snapshot()` (the early sym_by_strike-empty return, and the full
response) now pass `close_time=_close_time`, resolved via
`mcx_non_agri_close_time(expiry_d)` when `is_mcx_underlying(und)`,
mirroring the pattern already shipped in `/analytics` and
`/strategy-analytics`.
"""

from __future__ import annotations

import inspect
from datetime import date, datetime
from unittest.mock import AsyncMock, MagicMock, patch
from zoneinfo import ZoneInfo

import pytest

import backend.api.routes.options as _options_mod
from backend.api.routes.options import OptionsController, mcx_non_agri_close_time

_IST = ZoneInfo("Asia/Kolkata")


def _frozen_datetime(fixed_now: datetime):
    """Standard subclass-based "freeze time" shim — see
    test_options_mcx_dst_close_time_wiring.py for why a bare MagicMock
    replacement of the module-level `datetime` name breaks
    `isinstance(expiry, datetime)` inside `days_to_expiry`."""
    class _Frozen(datetime):
        @classmethod
        def now(cls, tz=None):
            return fixed_now if tz else fixed_now.replace(tzinfo=None)
    return _Frozen


# ── 1. Source-grep — no bare days_to_expiry(expiry_d) call survives ────


def test_chain_snapshot_never_calls_days_to_expiry_without_close_time():
    handler = OptionsController.__dict__["chain_snapshot"]
    src = inspect.getsource(handler.fn)
    assert "days_to_expiry(expiry_d)" not in src, (
        "chain_snapshot must never call days_to_expiry(expiry_d) with no "
        "close_time= — that silently defaults to the NFO 15:30 close even "
        "for an MCX underlying"
    )
    assert "mcx_non_agri_close_time" in src, (
        "chain_snapshot must resolve its MCX close_time via "
        "mcx_non_agri_close_time(), matching /analytics and "
        "/strategy-analytics"
    )
    assert "days_to_expiry(expiry_d, close_time=_close_time)" in src


# ── 2. Behavioral — the early-return (empty instruments) path ──────────


async def _call_chain_snapshot_with_empty_instruments(underlying: str, expiry: str):
    """Drives chain_snapshot() down its `if not sym_by_strike:` early
    return by making the instruments cache report no matching contracts
    — the simplest real path that reaches a days_to_expiry() call
    without needing broker-quote mocking."""
    # Controller.__init__ (Litestar) requires an `owner` (Router) arg in
    # the installed Litestar version — a bare MagicMock satisfies it
    # structurally since chain_snapshot() never touches `self`.
    controller = OptionsController(owner=MagicMock())
    # `controller.chain_snapshot` resolves to the `@get(...)` descriptor
    # object itself (not a normally-bound method) — call its raw `.fn`
    # directly, passing `controller` as the explicit `self` arg, the
    # same unbinding `inspect.getsource(handler.fn)` already relies on
    # elsewhere in this file.
    handler = OptionsController.__dict__["chain_snapshot"]
    empty_inst_resp = MagicMock()
    empty_inst_resp.items = []  # no instrument matches und/exp → sym_by_strike={}

    with patch.object(_options_mod, "_resolve_spot",
                      new=AsyncMock(return_value=(100.0, "live", 95.0, None))), \
         patch("backend.api.cache.peek", return_value=empty_inst_resp):
        return await handler.fn(
            controller, underlying=underlying, expiry=expiry, atm_window=10,
        )


@pytest.mark.asyncio
async def test_mcx_underlying_at_23_37_ist_on_winter_expiry_shows_positive_dte():
    """The exact reported-class scenario: an MCX underlying (CRUDEOIL)
    expiring 2026-11-17 (the real winter-window date — real close
    23:55 IST), evaluated at 23:37 IST that same day. Pre-fix, this
    endpoint always used the 15:30 close, so DTE was already deeply
    negative-floored-to-zero for HOURS before this moment. Post-fix, 18
    minutes of real time remain before the actual 23:55 close."""
    fixed_now = datetime(2026, 11, 17, 23, 37, tzinfo=_IST)
    with patch("backend.api.algo.derivatives.datetime", _frozen_datetime(fixed_now)):
        resp = await _call_chain_snapshot_with_empty_instruments("CRUDEOIL", "2026-11-17")

    assert resp.days_to_expiry > 0.0, (
        "at 23:37 IST on the real 2026-11-17 winter-window expiry date, "
        "days_to_expiry must be > 0 (18 minutes remain before the real "
        "23:55 close) — chain-snapshot must not treat it as already past"
    )
    # Sanity cross-check against the real resolver directly.
    close_t = mcx_non_agri_close_time(date(2026, 11, 17))
    assert close_t.hour == 23 and close_t.minute == 55


@pytest.mark.asyncio
async def test_mcx_underlying_at_15_45_ist_is_not_falsely_past_close():
    """The literal reported defect, at its simplest: 15:45 IST is PAST
    the (wrong) NFO 15:30 close the old code always used, but hours
    BEFORE any real MCX close (23:30 or 23:55). Pre-fix this returned
    `days_to_expiry=0.0` (floored, already "expired") for an MCX
    underlying on its own expiry day, 8+ hours before the real close —
    wrong every single day of the year, not just in the DST window."""
    fixed_now = datetime(2026, 11, 17, 15, 45, tzinfo=_IST)
    with patch("backend.api.algo.derivatives.datetime", _frozen_datetime(fixed_now)):
        resp = await _call_chain_snapshot_with_empty_instruments("CRUDEOIL", "2026-11-17")

    assert resp.days_to_expiry > 0.0, (
        "at 15:45 IST — past the WRONG 15:30 NFO close but hours before "
        "the REAL MCX close — days_to_expiry must still be positive"
    )


@pytest.mark.asyncio
async def test_non_mcx_underlying_unaffected_still_uses_15_30_close():
    """A genuine NFO/equity underlying must keep using the 15:30 close —
    this fix must not change behavior for the non-MCX case."""
    fixed_now = datetime(2026, 11, 17, 15, 45, tzinfo=_IST)
    with patch("backend.api.algo.derivatives.datetime", _frozen_datetime(fixed_now)):
        resp = await _call_chain_snapshot_with_empty_instruments("NIFTY", "2026-11-17")

    # 15:45 is past NIFTY's real 15:30 close on its own expiry day →
    # days_to_expiry floors to 0.0, unchanged pre-existing behavior.
    assert resp.days_to_expiry == 0.0


@pytest.mark.asyncio
async def test_mcx_underlying_after_the_real_winter_close_is_genuinely_expired():
    """At 23:56 IST (one minute after the REAL 23:55 winter close),
    days_to_expiry must correctly floor to 0.0 — the fix must not make
    an MCX underlying NEVER expire."""
    fixed_now = datetime(2026, 11, 17, 23, 56, tzinfo=_IST)
    with patch("backend.api.algo.derivatives.datetime", _frozen_datetime(fixed_now)):
        resp = await _call_chain_snapshot_with_empty_instruments("CRUDEOIL", "2026-11-17")

    assert resp.days_to_expiry == 0.0
