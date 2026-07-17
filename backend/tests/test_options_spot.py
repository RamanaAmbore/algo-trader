"""
Tests for calendar-aware spot resolution in _resolve_spot (options.py).

Covers:
  - MCX front-month used when no expiry_hint (Task 1 fallback path)
  - MCX per-expiry future used when expiry_hint set (Task 2 calendar-aware path)
  - MCX front-month fallback when no matching far future is listed
  - Index underlying unaffected by expiry_hint (non-commodity path unchanged)

These tests mock the instruments cache so no broker or DB is needed.
`_resolve_spot` is imported directly; broker.quote is mocked to return
a fixed LTP so the full async chain completes without network calls.
"""

import pytest
from datetime import date
from unittest.mock import MagicMock, patch, AsyncMock


# ---------------------------------------------------------------------------
# Date helpers — keep tests resilient as today's date marches forward.
# The resolver filters past-expired contracts using `date.today()`, so
# hardcoded years bit-rot. These helpers always produce future-relative
# fixtures.
# ---------------------------------------------------------------------------

def _future_month(months_out: int, day: int = 18):
    """Return (year_code_2digit, month_token_3char_upper, expiry_iso) for
    a date `months_out` calendar months past today. Used to construct
    instrument symbols + expiry dates that stay in the future regardless
    of when the suite runs."""
    today = date.today()
    total = today.month + months_out - 1
    yr  = today.year + total // 12
    mo  = total % 12 + 1
    d   = date(yr, mo, day)
    return f"{yr % 100:02d}", d.strftime("%b").upper(), d.isoformat()


# ---------------------------------------------------------------------------
# Fake instrument item shape (matches what the instruments cache returns)
# ---------------------------------------------------------------------------

def _inst(tradingsymbol: str, underlying: str, expiry: str) -> MagicMock:
    """Build a minimal fake instrument matching attrs used by derivatives helpers."""
    inst = MagicMock()
    inst.s = tradingsymbol
    inst.u = underlying
    inst.e = "MCX"
    inst.t = "FUT"
    inst.x = expiry
    return inst


def _make_instruments_resp(items: list) -> MagicMock:
    resp = MagicMock()
    resp.items = items
    return resp


def _make_quote_resp(key: str, ltp: float) -> dict:
    """Broker quote response for a single key."""
    return {key: {"last_price": ltp, "ohlc": {"close": ltp * 0.99}}}


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _patch_instruments(items: list):
    """Patch the instruments cache to return `items`."""
    resp = _make_instruments_resp(items)
    return patch(
        "backend.api.cache.get_or_fetch",
        new=AsyncMock(return_value=resp),
    )


def _patch_broker_quote(ltp_map: dict[str, float]):
    """Patch get_price_broker().quote to return ltp for each key in ltp_map."""
    def _quote(keys):
        return {k: {"last_price": ltp_map.get(k, 0.0),
                    "ohlc": {"close": ltp_map.get(k, 0.0) * 0.99}}
                for k in keys}

    broker = MagicMock()
    broker.quote.side_effect = _quote
    return patch(
        "backend.brokers.registry.get_price_broker",
        return_value=broker,
    )


def _patch_no_sim():
    """Patch sim driver so it's not active (clean state)."""
    drv = MagicMock()
    drv.active = False
    return patch(
        "backend.api.algo.sim.driver.get_driver",
        return_value=drv,
    )


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_mcx_uses_front_month_when_no_expiry_hint():
    """Without expiry_hint, MCX commodity always uses the front-month future."""
    yy_f, mon_f, exp_f = _future_month(1)   # ~1 month out (front)
    yy_b, mon_b, exp_b = _future_month(4)   # ~4 months out (back)
    front_sym = f"CRUDEOIL{yy_f}{mon_f}FUT"
    back_sym  = f"CRUDEOIL{yy_b}{mon_b}FUT"
    front_inst = _inst(front_sym, "CRUDEOIL", exp_f)
    back_inst  = _inst(back_sym,  "CRUDEOIL", exp_b)
    items = [front_inst, back_inst]

    # Front-month = earliest non-expired; only the front responds with LTP
    ltp_map = {f"MCX:{front_sym}": 6800.0}

    from backend.api.routes.options import _resolve_spot

    with _patch_no_sim(), \
         _patch_instruments(items), \
         _patch_broker_quote(ltp_map):
        spot, src, prev_close, anchor = await _resolve_spot(
            "CRUDEOIL", None, expiry_hint=None
        )

    assert src == "futures"
    assert abs(spot - 6800.0) < 0.01
    assert anchor == front_sym


@pytest.mark.asyncio
async def test_mcx_uses_matching_tenor_when_expiry_hint_set():
    """With expiry_hint=Sep, the Sep future is selected, not the Jun front-month."""
    jun_inst = _inst("CRUDEOIL26JUNFUT", "CRUDEOIL", "2026-06-18")
    jul_inst = _inst("CRUDEOIL26JULFUT", "CRUDEOIL", "2026-07-18")
    sep_inst = _inst("CRUDEOIL26SEPFUT", "CRUDEOIL", "2026-09-18")
    items = [jun_inst, jul_inst, sep_inst]

    # Sep future trades at a different level than Jun
    ltp_map = {
        "MCX:CRUDEOIL26JUNFUT": 6800.0,
        "MCX:CRUDEOIL26JULFUT": 6850.0,
        "MCX:CRUDEOIL26SEPFUT": 6950.0,
    }

    from backend.api.routes.options import _resolve_spot

    with _patch_no_sim(), \
         _patch_instruments(items), \
         _patch_broker_quote(ltp_map):
        spot, src, prev_close, anchor = await _resolve_spot(
            "CRUDEOIL", None,
            expiry_hint=date(2026, 9, 25),  # Sep option expiry
        )

    assert src == "futures"
    assert abs(spot - 6950.0) < 0.01, f"Expected Sep LTP 6950, got {spot}"
    assert anchor == "CRUDEOIL26SEPFUT", f"Expected CRUDEOIL26SEPFUT, got {anchor}"


@pytest.mark.asyncio
async def test_mcx_falls_back_to_front_month_when_no_matching_far_future():
    """When the option's expiry is after all listed futures, use the last listed."""
    # Use future-relative dates so the test stays green as wall-clock advances.
    yy_n, mon_n, exp_n = _future_month(1)   # front (nearest)
    yy_f, mon_f, exp_f = _future_month(2)   # second-nearest (the "last listed")
    near_sym = f"CRUDEOIL{yy_n}{mon_n}FUT"
    far_sym  = f"CRUDEOIL{yy_f}{mon_f}FUT"
    near_inst = _inst(near_sym, "CRUDEOIL", exp_n)
    far_inst  = _inst(far_sym,  "CRUDEOIL", exp_f)
    items = [near_inst, far_inst]

    # Far-out expiry_hint (8 months) — no listed future covers it yet;
    # lookup_mcx_future_for_expiry returns the last available (far_sym).
    _, _, exp_hint = _future_month(8)
    hint_date = date.fromisoformat(exp_hint)

    ltp_map = {
        f"MCX:{near_sym}": 6800.0,
        f"MCX:{far_sym}":  6860.0,
    }

    from backend.api.routes.options import _resolve_spot

    with _patch_no_sim(), \
         _patch_instruments(items), \
         _patch_broker_quote(ltp_map):
        spot, src, prev_close, anchor = await _resolve_spot(
            "CRUDEOIL", None,
            expiry_hint=hint_date,  # Far-out option, no listed future
        )

    assert src == "futures"
    # lookup_mcx_future_for_expiry returns candidates[-1] = far_sym (last listed)
    assert anchor == far_sym, f"Expected {far_sym}, got {anchor}"
    assert abs(spot - 6860.0) < 0.01


@pytest.mark.asyncio
async def test_index_unaffected_by_expiry_hint():
    """Passing expiry_hint for NIFTY (non-commodity) still uses NSE:NIFTY 50 spot."""
    items = []  # instruments cache irrelevant for index path

    nifty_ltp = 24500.0
    ltp_map = {"NSE:NIFTY 50": nifty_ltp}

    from backend.api.routes.options import _resolve_spot

    with _patch_no_sim(), \
         _patch_instruments(items), \
         _patch_broker_quote(ltp_map):
        spot, src, prev_close, anchor = await _resolve_spot(
            "NIFTY", None,
            expiry_hint=date(2026, 9, 25),  # Sep expiry hint — must NOT change path
        )

    assert src in ("live", "close", "depth"), \
        f"Expected NSE spot source, got '{src}'"
    assert abs(spot - nifty_ltp) < 0.01
    # Index path never returns a resolved contract
    assert anchor is None, f"Expected anchor=None for index, got {anchor!r}"


# ---------------------------------------------------------------------------
# Phase 4b tests — month-token based resolution via lookup_future_for_option
# ---------------------------------------------------------------------------

def _inst_nfo(tradingsymbol: str, underlying: str, expiry: str) -> MagicMock:
    """Fake NFO future instrument."""
    inst = MagicMock()
    inst.s = tradingsymbol
    inst.u = underlying
    inst.e = "NFO"
    inst.t = "FUT"
    inst.x = expiry
    return inst


@pytest.mark.asyncio
async def test_option_symbol_matches_same_month_future():
    """CRUDEOIL25JUN5800CE resolves to CRUDEOIL25JUNFUT even though the
    option expires before the future (MCX options expire ~2 days before
    the future within the same contract month)."""
    jun_inst = _inst("CRUDEOIL25JUNFUT", "CRUDEOIL", "2025-06-19")
    items = [jun_inst]

    ltp_map = {"MCX:CRUDEOIL25JUNFUT": 5750.0}

    from backend.api.routes.options import _resolve_spot

    with _patch_no_sim(), \
         _patch_instruments(items), \
         _patch_broker_quote(ltp_map):
        spot, src, prev_close, anchor = await _resolve_spot(
            "CRUDEOIL", None,
            expiry_hint=date(2025, 6, 17),       # option expires 17-Jun
            option_symbol="CRUDEOIL25JUN5800CE",  # same month token → JUN future
        )

    assert src == "futures"
    assert abs(spot - 5750.0) < 0.01
    assert anchor == "CRUDEOIL25JUNFUT", \
        f"Expected CRUDEOIL25JUNFUT (month-token match), got {anchor!r}"


@pytest.mark.asyncio
async def test_weekly_option_falls_back_to_front_month():
    """NIFTY2762422000CE is a weekly option (no 3-letter MON token).
    lookup_future_for_option detects it as weekly → falls back to the
    front-month NFO future (NIFTY27JUNFUT)."""
    # Use a future date so the "expiry > today" filter passes
    jun_inst = _inst_nfo("NIFTY27JUNFUT", "NIFTY", "2027-06-24")
    items = [jun_inst]

    # NSE:NIFTY 50 spot also available — but since we supply option_symbol,
    # the commodity branch is NOT triggered for NIFTY (is_mcx_underlying=False).
    # The weekly test exercises lookup_future_for_option directly via unit test.
    from backend.api.algo.derivatives import lookup_future_for_option

    with _patch_instruments(items):
        result = await lookup_future_for_option("NIFTY2762422000CE")

    assert result == "NIFTY27JUNFUT", \
        f"Expected NIFTY27JUNFUT for weekly NIFTY option, got {result!r}"


@pytest.mark.asyncio
async def test_option_symbol_with_unlisted_future_falls_through():
    """When the month-token future isn't in the cache yet, lookup_future_for_option
    returns None, and _resolve_spot falls through to expiry_hint, then
    front-month if needed."""
    # Only JUL listed, no JUN future.
    jul_inst = _inst("CRUDEOIL25JULFUT", "CRUDEOIL", "2025-07-18")
    items = [jul_inst]

    ltp_map = {"MCX:CRUDEOIL25JULFUT": 5800.0}

    from backend.api.routes.options import _resolve_spot

    with _patch_no_sim(), \
         _patch_instruments(items), \
         _patch_broker_quote(ltp_map):
        spot, src, prev_close, anchor = await _resolve_spot(
            "CRUDEOIL", None,
            expiry_hint=date(2025, 7, 18),        # JUL expiry hint
            option_symbol="CRUDEOIL25JUN5800CE",   # JUN not in cache → falls through
        )

    # lookup_future_for_option misses (no JUN future), expiry_hint=JUL resolves JULFUT
    assert src == "futures"
    assert anchor == "CRUDEOIL25JULFUT", \
        f"Expected CRUDEOIL25JULFUT via expiry_hint fallback, got {anchor!r}"


@pytest.mark.asyncio
async def test_option_symbol_takes_priority_over_expiry_hint():
    """When both option_symbol and expiry_hint are provided, the month-token
    match from option_symbol wins over the date-based expiry_hint lookup."""
    yy_a, mon_a, exp_a = _future_month(2)   # ~2 months out — A (priority)
    yy_b, mon_b, exp_b = _future_month(5)   # ~5 months out — B (hint)
    sym_a  = f"CRUDEOIL{yy_a}{mon_a}FUT"
    sym_b  = f"CRUDEOIL{yy_b}{mon_b}FUT"
    inst_a = _inst(sym_a, "CRUDEOIL", exp_a)
    inst_b = _inst(sym_b, "CRUDEOIL", exp_b)
    items = [inst_a, inst_b]

    ltp_map = {
        f"MCX:{sym_a}": 5750.0,
        f"MCX:{sym_b}": 5900.0,
    }

    from backend.api.routes.options import _resolve_spot
    opt_symbol = f"CRUDEOIL{yy_a}{mon_a}5800CE"

    with _patch_no_sim(), \
         _patch_instruments(items), \
         _patch_broker_quote(ltp_map):
        spot, src, prev_close, anchor = await _resolve_spot(
            "CRUDEOIL", None,
            expiry_hint=date.fromisoformat(exp_b),   # B hint
            option_symbol=opt_symbol,                # A token — must win
        )

    # option_symbol month-token (A) must take priority over expiry_hint (B)
    assert src == "futures"
    assert anchor == sym_a, \
        f"Expected {sym_a} (option_symbol priority), got {anchor!r}"
    assert abs(spot - 5750.0) < 0.01, \
        f"Expected A LTP 5750, got {spot}"
