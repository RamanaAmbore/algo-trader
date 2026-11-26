"""
test_order_gate_mcx_dst_aware.py — the ORDER-PLACEMENT side of "MCX DST-
aware non-agri close time" (see CLAUDE.md). The analytics/Greeks side
(options.py) was fixed first; this covers the separate, YAML-driven
mechanism that actually gates live order placement.

Confirmed by tracing the real code (not assumed): `_segment_is_open()`
(`backend/shared/helpers/date_time_utils.py`, used by `/api/market/status`,
the quote gate, and the watchlist movers gate) and `_ae_segment_flags()`
(`backend/api/algo/agent_engine.py`, the REAL gate behind
`_symbol_exchange_open()` — in turn the gate behind ticket/basket/chase/
template-attach order placement, per this file's own "Order-path gate
status" CLAUDE.md note) both read `seg_cfg.get("sessions")` DIRECTLY from
the static YAML `market_segments.commodity` config
(`backend_config.yaml`), which hardcodes `end: "23:30"` year-round — a
COMPLETELY SEPARATE mechanism from the `exchange_schedule` DB table
already fixed for the analytics side. An MCX order placed at, say,
23:40 IST during the winter DST window (real close 23:55) would have
been refused as "market closed" even though the exchange was genuinely
open — the operator's own originally-reported scenario, on the
order-placement side.

Fix: `resolve_dst_aware_sessions()` (new, `date_time_utils.py`) is the
one SSOT both `_segment_is_open()` and `_ae_segment_flags()` now route
through instead of reading `seg_cfg.get("sessions")` raw. It replaces
the commodity segment's configured evening-close entry with the real
`mcx_non_agri_close_time()`-resolved value for the given date, and
leaves every other segment's sessions list (e.g. equity/NSE) completely
untouched.
"""

from __future__ import annotations

from datetime import date, datetime
from unittest.mock import patch
from zoneinfo import ZoneInfo

import pytest

from backend.shared.helpers.date_time_utils import (
    _segment_is_open,
    close_time_for_exchange,
    resolve_dst_aware_sessions,
)

_IST = ZoneInfo("Asia/Kolkata")

# Confirmed winter-window date (real MCX non-agri close 23:55 IST) and
# DST-window date (real close 23:30 IST) — same two reference dates
# used throughout the sibling options.py test files.
_WINTER_DATE = date(2026, 11, 17)   # Tuesday — matches the operator's own
                                    # originally-reported incident date.
_DST_DATE    = date(2026, 6, 1)     # Monday — deep inside US DST.


def _commodity_seg() -> dict:
    from backend.shared.helpers.utils import config as _cfg
    return (_cfg.get("market_segments", {}) or {}).get("commodity", {}) or {}


def _equity_seg() -> dict:
    from backend.shared.helpers.utils import config as _cfg
    return (_cfg.get("market_segments", {}) or {}).get("equity", {}) or {}


@pytest.fixture(autouse=True)
def _patch_holiday_sources():
    """Isolate from the real holiday/special-session calendar — neither
    reference date is a real NSE/MCX holiday, but this keeps the test
    deterministic regardless of the live calendar's own state."""
    with patch("backend.brokers.broker_apis.fetch_holidays", return_value=set()), \
         patch("backend.brokers.broker_apis.fetch_special_sessions", return_value=[]):
        yield


# ── 1. resolve_dst_aware_sessions() — the shared resolver itself ───────


def test_resolves_winter_close_for_commodity_segment():
    seg = {"holiday_exchange": "MCX", "sessions": [{"start": "09:00", "end": "23:30"}]}
    resolved = resolve_dst_aware_sessions(seg, _WINTER_DATE)
    assert resolved == [{"start": "09:00", "end": "23:55"}]


def test_resolves_dst_close_for_commodity_segment():
    seg = {"holiday_exchange": "MCX", "sessions": [{"start": "09:00", "end": "23:30"}]}
    resolved = resolve_dst_aware_sessions(seg, _DST_DATE)
    assert resolved == [{"start": "09:00", "end": "23:30"}]


def test_non_mcx_segment_sessions_are_untouched():
    """Equity/NSE (or any other non-MCX holiday_exchange) must get its
    sessions list back byte-identical, regardless of date — this
    seasonal rule applies to MCX alone."""
    seg = {"holiday_exchange": "NSE", "sessions": [{"start": "09:15", "end": "15:30"}]}
    assert resolve_dst_aware_sessions(seg, _WINTER_DATE) == seg["sessions"]
    assert resolve_dst_aware_sessions(seg, _DST_DATE) == seg["sessions"]


def test_absent_sessions_returns_none_unchanged():
    assert resolve_dst_aware_sessions({"holiday_exchange": "MCX"}, _WINTER_DATE) is None
    assert resolve_dst_aware_sessions({}, _WINTER_DATE) is None


def test_resolver_failure_falls_back_to_the_raw_static_list():
    """A resolver exception (e.g. a future zoneinfo/import failure)
    must never raise — it falls back to the original, static YAML
    sessions list rather than crashing the whole market-open check."""
    seg = {"holiday_exchange": "MCX", "sessions": [{"start": "09:00", "end": "23:30"}]}
    with patch(
        "backend.api.algo.derivatives.mcx_non_agri_close_time",
        side_effect=RuntimeError("boom"),
    ):
        resolved = resolve_dst_aware_sessions(seg, _WINTER_DATE)
    assert resolved == seg["sessions"]


def test_only_the_last_session_entry_is_adjusted():
    """A hypothetical multi-window commodity config (not the current
    production shape, but a real input this function must not
    mishandle) must only have its LAST entry's close adjusted."""
    seg = {"holiday_exchange": "MCX",
          "sessions": [{"start": "09:00", "end": "17:00"},
                       {"start": "17:00", "end": "23:30"}]}
    resolved = resolve_dst_aware_sessions(seg, _WINTER_DATE)
    assert resolved[0] == {"start": "09:00", "end": "17:00"}
    assert resolved[1] == {"start": "17:00", "end": "23:55"}


# ── 2. _segment_is_open() — the /market/status, quote-gate, movers-gate path ──


def test_segment_is_open_mcx_at_2340_winter_is_open():
    now = datetime(_WINTER_DATE.year, _WINTER_DATE.month, _WINTER_DATE.day, 23, 40, tzinfo=_IST)
    assert _segment_is_open(_commodity_seg(), now) is True, (
        "the operator's own reported scenario: MCX at 23:40 IST during "
        "the winter window must read OPEN, not closed"
    )


def test_segment_is_open_mcx_at_2340_dst_is_closed():
    """Contrast case — proves this is genuinely season-aware, not just
    'always open past the old 23:30 boundary'. At 23:40 during the DST
    window, MCX's real close (23:30) has already passed."""
    now = datetime(_DST_DATE.year, _DST_DATE.month, _DST_DATE.day, 23, 40, tzinfo=_IST)
    assert _segment_is_open(_commodity_seg(), now) is False


def test_segment_is_open_mcx_at_2356_winter_is_closed():
    """Just after the real 23:55 winter close — correctly closed."""
    now = datetime(_WINTER_DATE.year, _WINTER_DATE.month, _WINTER_DATE.day, 23, 56, tzinfo=_IST)
    assert _segment_is_open(_commodity_seg(), now) is False


def test_segment_is_open_equity_unaffected_by_mcx_dst_rule():
    """NSE/equity must still close at its own 15:30 regardless of the
    MCX seasonal rule — confirms no cross-segment leakage."""
    closed_at = datetime(_WINTER_DATE.year, _WINTER_DATE.month, _WINTER_DATE.day, 15, 35, tzinfo=_IST)
    assert _segment_is_open(_equity_seg(), closed_at) is False


# ── 3. The REAL order-placement gate: _build_context -> _symbol_exchange_open ──
#
# This is the exact mechanism ticket/basket/chase/template-attach order
# placement call — _symbol_exchange_open("MCX", ctx) over a context
# built by _build_context(now). No mocking of the gate itself; only the
# holiday/special-session I/O is isolated (via the autouse fixture
# above), matching the sibling test_market_cycle_tokens.py convention.


def test_order_placement_gate_accepts_mcx_at_2340_winter():
    """THE CONFIRMATION the operator asked for: a simulated order-
    placement check at 23:40 IST on a winter-window MCX date now
    resolves as market-open, not refused."""
    from backend.api.algo.agent_engine import _build_context, _symbol_exchange_open

    now = datetime(_WINTER_DATE.year, _WINTER_DATE.month, _WINTER_DATE.day, 23, 40, tzinfo=_IST)
    ctx = _build_context(now)
    assert ctx["mcx_open"] is True, ctx
    assert _symbol_exchange_open("MCX", ctx) is True, (
        "an MCX order at 23:40 IST during the winter DST window must be "
        "accepted as market-open by the real order-placement gate"
    )
    # NCO shares the same 'commodity' segment mapping (see
    # _EXCHANGE_TO_SEGMENT in agent_engine.py) and must agree.
    assert _symbol_exchange_open("NCO", ctx) is True


def test_order_placement_gate_rejects_mcx_at_2340_during_dst():
    """Contrast case on the real gate, not just the lower-level
    resolver — proves the full order-placement path is genuinely
    season-aware end to end."""
    from backend.api.algo.agent_engine import _build_context, _symbol_exchange_open

    now = datetime(_DST_DATE.year, _DST_DATE.month, _DST_DATE.day, 23, 40, tzinfo=_IST)
    ctx = _build_context(now)
    assert ctx["mcx_open"] is False, ctx
    assert _symbol_exchange_open("MCX", ctx) is False


def test_order_placement_gate_rejects_mcx_after_the_real_winter_close():
    """At 23:56 — one minute after the real 23:55 winter close — the
    gate must correctly refuse; the fix must not make MCX orders
    acceptable at all hours."""
    from backend.api.algo.agent_engine import _build_context, _symbol_exchange_open

    now = datetime(_WINTER_DATE.year, _WINTER_DATE.month, _WINTER_DATE.day, 23, 56, tzinfo=_IST)
    ctx = _build_context(now)
    assert _symbol_exchange_open("MCX", ctx) is False


def test_order_placement_gate_nse_unaffected():
    """NSE/equity order placement must still close at its own 15:30,
    regardless of the MCX seasonal rule."""
    from backend.api.algo.agent_engine import _build_context, _symbol_exchange_open

    now = datetime(_WINTER_DATE.year, _WINTER_DATE.month, _WINTER_DATE.day, 15, 35, tzinfo=_IST)
    ctx = _build_context(now)
    assert _symbol_exchange_open("NSE", ctx) is False


def test_pre_fix_static_yaml_would_have_rejected_this_order():
    """Sanity check that this is a REAL, reproducible regression and
    not a strawman: reading the commodity segment's RAW (unresolved)
    YAML sessions directly — i.e. the pre-fix behaviour — would have
    reported 23:40 winter as closed."""
    from backend.shared.helpers.date_time_utils import is_market_open
    from datetime import time as dtime

    seg = _commodity_seg()
    raw_sessions = seg.get("sessions")  # the static YAML list, unresolved
    now = datetime(_WINTER_DATE.year, _WINTER_DATE.month, _WINTER_DATE.day, 23, 40, tzinfo=_IST)
    pre_fix_result = is_market_open(
        now, set(), dtime(9, 0), dtime(23, 30), exchange="MCX",
        sessions=raw_sessions, evening_open_on_holidays=True,
        special_sessions=[],
    )
    assert pre_fix_result is False, (
        "confirms the bug was real: the static YAML sessions list alone "
        "reports 23:40 IST winter as closed — this is exactly what "
        "_segment_is_open()/_ae_segment_flags() did before routing "
        "through resolve_dst_aware_sessions()"
    )


# ── 4. Confirms closed_hours_or_broker()'s own DB-driven mechanism ─────
#
# closed_hours_or_broker() (backend/api/helpers/snapshot_gate.py, the
# canonical closed-hours gate every data route uses) delegates to
# exchange_clock.is_any_segment_open() — a COMPLETELY SEPARATE,
# DB-driven mechanism from _ae_segment_flags() above. It reads
# exchange_schedule's close_time DIRECTLY (a plain bounded-range check,
# `open_time <= now_t < close_time` — no "+15min" derivation involved,
# so there is no midnight-wrap hazard here at all). This mechanism
# self-corrects for free once the exchange_schedule DST sync
# (_sync_mcx_dst_close_time_coro) has actually written the real winter
# close_time into the DB row — confirmed here against a row shaped the
# way it looks POST-sync.


def test_exchange_clock_is_any_segment_open_correct_once_db_row_synced():
    """Simulates the exchange_schedule MCX row AFTER the DST sync has
    run (close_time='23:55') — confirms is_any_segment_open() correctly
    reports MCX open at 23:40 IST on the winter date. This mechanism
    needed no code change: _is_within_session()'s bounded-range check
    was never subject to the midnight-wrap hazard that affected the
    close-SNAPSHOT trigger derivation (close_time + 15 min) fixed
    earlier in this same pass."""
    import types
    from datetime import time as dtime
    from backend.api.helpers import exchange_clock

    mcx_row = types.SimpleNamespace(
        gate="MCX", date=None, weekdays=[0, 1, 2, 3, 4],
        open_time=dtime(8, 0), close_time=dtime(23, 55),  # post-sync value
        snapshot_time=None, exchanges=["MCX", "NCO"],
        is_open=True, session_name="regular", source="system",
    )
    now = datetime(_WINTER_DATE.year, _WINTER_DATE.month, _WINTER_DATE.day, 23, 40, tzinfo=_IST)

    original_cache = exchange_clock._CACHE
    try:
        exchange_clock._CACHE = [mcx_row]
        with patch.object(exchange_clock, "_now_ist", return_value=now):
            assert exchange_clock.is_any_segment_open(["MCX"]) is True
            assert exchange_clock.is_exchange_open("MCX") is True
    finally:
        exchange_clock._CACHE = original_cache


def test_exchange_clock_is_any_segment_open_still_stale_pre_sync():
    """Contrast: the SAME check against the PRE-sync row shape
    (close_time='23:30', what the live DB still holds until the sync
    has actually run) correctly reports MCX closed at 23:40 — proving
    this mechanism's correctness depends entirely on the DB row's
    stored value, not on any code path that still needs a fix."""
    import types
    from datetime import time as dtime
    from backend.api.helpers import exchange_clock

    mcx_row = types.SimpleNamespace(
        gate="MCX", date=None, weekdays=[0, 1, 2, 3, 4],
        open_time=dtime(8, 0), close_time=dtime(23, 30),  # pre-sync (stale) value
        snapshot_time=None, exchanges=["MCX", "NCO"],
        is_open=True, session_name="regular", source="system",
    )
    now = datetime(_WINTER_DATE.year, _WINTER_DATE.month, _WINTER_DATE.day, 23, 40, tzinfo=_IST)

    original_cache = exchange_clock._CACHE
    try:
        exchange_clock._CACHE = [mcx_row]
        with patch.object(exchange_clock, "_now_ist", return_value=now):
            assert exchange_clock.is_any_segment_open(["MCX"]) is False
    finally:
        exchange_clock._CACHE = original_cache


# ── 5. End-to-end through the REAL ticket-route gate function ──────────
#
# _ticket_gate_market_hours_and_align_price() (orders_place.py) is what
# POST /api/orders/ticket actually calls — it wraps _symbol_exchange_open
# via _build_now_ctx(), the same mechanism tested above, but driving THIS
# function directly is the most honest version of "simulated order
# placement": no HTTPException(409) means the order was accepted as
# market-open.


@pytest.mark.asyncio
async def test_ticket_route_gate_accepts_mcx_order_at_2340_winter():
    """The explicit confirmation through the REAL route-level gate
    function: a simulated MCX order at 23:40 IST on the winter-window
    date must NOT be rejected with HTTPException(409) 'market closed'."""
    import types
    from datetime import datetime as _dt
    from unittest.mock import patch as _patch
    from backend.api.routes.orders_place import _ticket_gate_market_hours_and_align_price
    from backend.shared.helpers import date_time_utils

    data = types.SimpleNamespace(exchange="MCX", price=None, trigger_price=None)
    frozen_now = _dt(_WINTER_DATE.year, _WINTER_DATE.month, _WINTER_DATE.day, 23, 40, tzinfo=_IST)

    with _patch.object(date_time_utils, "timestamp_indian", return_value=frozen_now):
        # No exception == accepted as market-open.
        await _ticket_gate_market_hours_and_align_price(data, "CRUDEOIL26NOV9200PE")


@pytest.mark.asyncio
async def test_ticket_route_gate_rejects_mcx_order_during_dst_at_2340():
    """Contrast through the same real route-level function: during the
    DST window (real close 23:30, already passed), the SAME 23:40
    instant must still be rejected as closed."""
    import types
    from datetime import datetime as _dt
    from unittest.mock import patch as _patch
    from litestar.exceptions import HTTPException
    from backend.api.routes.orders_place import _ticket_gate_market_hours_and_align_price
    from backend.shared.helpers import date_time_utils

    data = types.SimpleNamespace(exchange="MCX", price=None, trigger_price=None)
    frozen_now = _dt(_DST_DATE.year, _DST_DATE.month, _DST_DATE.day, 23, 40, tzinfo=_IST)

    with _patch.object(date_time_utils, "timestamp_indian", return_value=frozen_now):
        with pytest.raises(HTTPException) as exc_info:
            await _ticket_gate_market_hours_and_align_price(data, "CRUDEOIL26JUN9200PE")
    assert exc_info.value.status_code == 409


# ── close_time_for_exchange() — shared SSOT for any sync caller that
#    needs a raw (hour, minute) close time, e.g. a days_to_expiry()
#    computation, rather than an open/closed boolean. Added 2026-11-26
#    per operator instruction ("everything should be from market
#    calendar ssot") after a sibling bug was found in
#    scope_positions_expiring_today.py hardcoding (23, 30) for MCX and
#    a bare (15, 30) for everything else, instead of routing through
#    resolve_dst_aware_sessions() like the order-placement gate does. ──


def test_close_time_for_exchange_mcx_winter():
    assert close_time_for_exchange("MCX", _WINTER_DATE) == (23, 55)


def test_close_time_for_exchange_mcx_dst():
    assert close_time_for_exchange("MCX", _DST_DATE) == (23, 30)


def test_close_time_for_exchange_nco_mirrors_mcx():
    # NCO (cross-currency on MCX) shares the commodity segment.
    assert close_time_for_exchange("NCO", _WINTER_DATE) == (23, 55)


def test_close_time_for_exchange_nfo_is_1530_regardless_of_season():
    assert close_time_for_exchange("NFO", _WINTER_DATE) == (15, 30)
    assert close_time_for_exchange("NFO", _DST_DATE) == (15, 30)


def test_close_time_for_exchange_nse_bse_cds_bcd_all_equity_segment():
    for exch in ("NSE", "BSE", "CDS", "BCD"):
        assert close_time_for_exchange(exch, _WINTER_DATE) == (15, 30)


def test_close_time_for_exchange_unknown_exchange_returns_none():
    assert close_time_for_exchange("XYZ", _WINTER_DATE) is None
    assert close_time_for_exchange("", _WINTER_DATE) is None
    assert close_time_for_exchange(None, _WINTER_DATE) is None


def test_close_time_for_exchange_resolver_failure_returns_none_not_a_guess():
    with patch(
        "backend.shared.helpers.utils.config",
        {"market_segments": {}},
    ):
        assert close_time_for_exchange("MCX", _WINTER_DATE) is None
