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

import asyncio
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
    from zoneinfo import ZoneInfo
    import datetime as _dt_mod
    today = _dt_mod.datetime.now(ZoneInfo("Asia/Kolkata")).date()
    if months_out == 0:
        # Current month: use today's own IST date so the expiry is
        # guaranteed >= today on any day of the month. A hardcoded
        # day-of-month (e.g. 18) lands in the past from the 19th onward.
        d = today
    else:
        total = today.month + months_out - 1
        yr  = today.year + total // 12
        mo  = total % 12 + 1
        d   = date(yr, mo, day)
    return f"{d.year % 100:02d}", d.strftime("%b").upper(), d.isoformat()


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
async def test_option_on_its_own_expiry_day_does_not_roll_past_a_still_live_future():
    """2026-10-15 fix (operator-reported live bug): MCX options expire
    several business days BEFORE their same-month future — e.g. a CRUDEOIL
    Oct option's own last trading day while the Oct future still has days
    left. `lookup_future_for_option` must return the CURRENT month's
    future whenever IT is still live, never rolling to next month just
    because the paired option reached its own earlier scheduled expiry.
    Both the current-month and next-month futures are present here so a
    regression (rolling early) would be caught, not masked by a
    single-candidate fallback."""
    cur_yy, cur_mon, cur_x = _future_month(0)
    next_yy, next_mon, next_x = _future_month(1)
    cur_inst = _inst(f"CRUDEOIL{cur_yy}{cur_mon}FUT", "CRUDEOIL", cur_x)
    next_inst = _inst(f"CRUDEOIL{next_yy}{next_mon}FUT", "CRUDEOIL", next_x)
    opt_sym = f"CRUDEOIL{cur_yy}{cur_mon}5800CE"
    # The option's OWN instrument row, already past ITS expiry (yesterday)
    # — this is what used to force a premature roll via the now-removed
    # `_check_option_expired` check, even while `cur_inst` (the future)
    # is still clearly live.
    from datetime import timedelta
    opt_inst = _inst(opt_sym, "CRUDEOIL", (date.today() - timedelta(days=1)).isoformat())
    items = [cur_inst, next_inst, opt_inst]

    from backend.api.algo.derivatives import lookup_future_for_option

    with _patch_instruments(items):
        result = await lookup_future_for_option(opt_sym)

    assert result == f"CRUDEOIL{cur_yy}{cur_mon}FUT", \
        f"Expected current-month future (still live), got {result!r} — rolled past month early"


@pytest.mark.asyncio
async def test_future_still_resolves_on_its_own_expiry_day():
    """2026-10-16 fix: when the FUTURE ITSELF (not just the paired
    option, the 2026-10-15 fix above) reaches its own expiry day,
    lookup_future_for_option must still return it — only the next
    trading day's 08:00 IST open rolls to next month. Operator: "on
    expiry day, it should continue to use the current day underlying...
    [o]nly on the next market open day at 8:00 AM it should be reset to
    next month future." Both the expiring-today and next-month futures
    are present so a regression (rolling early) would be caught, not
    masked by a single-candidate fallback."""
    from zoneinfo import ZoneInfo
    import datetime as _dt_mod
    today_ist_iso = _dt_mod.datetime.now(ZoneInfo("Asia/Kolkata")).date().isoformat()
    cur_yy, cur_mon, _ = _future_month(0)
    next_yy, next_mon, next_x = _future_month(1)
    cur_inst = _inst(f"CRUDEOIL{cur_yy}{cur_mon}FUT", "CRUDEOIL", today_ist_iso)
    next_inst = _inst(f"CRUDEOIL{next_yy}{next_mon}FUT", "CRUDEOIL", next_x)
    opt_sym = f"CRUDEOIL{cur_yy}{cur_mon}5800CE"
    # Option itself also expiring today — the common real-world case.
    opt_inst = _inst(opt_sym, "CRUDEOIL", today_ist_iso)
    items = [cur_inst, next_inst, opt_inst]

    from backend.api.algo.derivatives import lookup_future_for_option

    with _patch_instruments(items):
        result = await lookup_future_for_option(opt_sym)

    assert result == f"CRUDEOIL{cur_yy}{cur_mon}FUT", (
        f"Future still live through its own expiry day; expected "
        f"CRUDEOIL{cur_yy}{cur_mon}FUT, got {result!r}")


@pytest.mark.asyncio
async def test_lookup_mcx_future_for_expiry_includes_contract_expiring_today():
    """2026-10-16 fix: lookup_mcx_future_for_expiry must not exclude a
    future whose own expiry is today — it's still correctly tradeable
    through its entire expiry day and the closed overnight window."""
    from backend.api.algo.derivatives import lookup_mcx_future_for_expiry
    from zoneinfo import ZoneInfo
    import datetime as _dt_mod
    today = _dt_mod.datetime.now(ZoneInfo("Asia/Kolkata")).date()
    cur_inst = _inst("CRUDEOIL26OCTFUT", "CRUDEOIL", today.isoformat())
    items = [cur_inst]

    with _patch_instruments(items):
        result = await lookup_mcx_future_for_expiry("CRUDEOIL", today)

    assert result == "CRUDEOIL26OCTFUT", (
        f"Expected the today-expiring future to still be selectable, got {result!r}")


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


@pytest.mark.asyncio
async def test_option_symbol_priority_not_overridden_by_transient_4a_timeout():
    """A transient failure fetching the PRICE of the option_symbol-priority
    contract must never silently substitute a DIFFERENT contract chosen via
    expiry_hint instead.

    Root-caused 2026-10-25 from an intermittent failure of
    `test_option_symbol_takes_priority_over_expiry_hint` that only reproduced
    under real system load (never in an isolated tight loop) — confirmed via
    a forced 4a timeout, which flipped the resolved anchor from the
    option_symbol month to the expiry_hint month. `_commodity_spot_4b`
    (the walk-forward fallback) starts from `expiry_hint` and constructs a
    DIFFERENT symbol outright, ignoring the already-resolved `resolved_sym` —
    correct when no contract could be resolved at all, wrong when
    option_symbol HAD already resolved one and only its price quote failed.

    This test forces exactly that: `lookup_future_for_option` resolves A
    correctly, but the 4a `asyncio.wait_for` for A's quote is forced to raise
    `asyncio.TimeoutError` (not a mocked broker failure — a real timeout
    exception, via the exact mechanism that caused the live flake). B's own
    quote (ltp_map has it) must never be reached."""
    yy_a, mon_a, exp_a = _future_month(2)
    yy_b, mon_b, exp_b = _future_month(5)
    sym_a  = f"CRUDEOIL{yy_a}{mon_a}FUT"
    sym_b  = f"CRUDEOIL{yy_b}{mon_b}FUT"
    inst_a = _inst(sym_a, "CRUDEOIL", exp_a)
    inst_b = _inst(sym_b, "CRUDEOIL", exp_b)
    items = [inst_a, inst_b]

    ltp_map = {
        f"MCX:{sym_a}": 5750.0,
        f"MCX:{sym_b}": 5900.0,
    }

    from backend.api.routes import options_helpers as oh
    opt_symbol = f"CRUDEOIL{yy_a}{mon_a}5800CE"

    real_wait_for = asyncio.wait_for
    call_count = {"n": 0}

    async def _fake_wait_for(coro, timeout):
        call_count["n"] += 1
        if call_count["n"] == 1:
            coro.close()
            raise asyncio.TimeoutError()
        return await real_wait_for(coro, timeout)

    with _patch_no_sim(), \
         _patch_instruments(items), \
         _patch_broker_quote(ltp_map), \
         patch("backend.api.routes.options_helpers.asyncio.wait_for",
               side_effect=_fake_wait_for):
        price, src, prev_close, resolved_sym, anchor = await oh._resolve_commodity_spot(
            "CRUDEOIL", date.fromisoformat(exp_b), opt_symbol,
            broker=MagicMock(quote=MagicMock(side_effect=lambda keys: {
                k: {"last_price": ltp_map.get(k, 0.0),
                    "ohlc": {"close": ltp_map.get(k, 0.0) * 0.99}}
                for k in keys
            })),
            spot_cache_put=MagicMock(),
            ltp_from_quote=lambda q: (
                (q.get("last_price"), "futures") if q.get("last_price") else (None, "none")
            ),
            prev_close_from_quote=lambda q: (q.get("ohlc") or {}).get("close"),
        )

    # The 4a timeout must be real and must have actually fired.
    assert call_count["n"] >= 1
    # Price fetch failed for A — price is None, NOT B's 5900.0.
    assert price is None, f"Expected no price (4a failed), got {price}"
    # resolved_sym must stay A (option_symbol priority), never silently
    # replaced by B (expiry_hint) due to the price-fetch failure.
    assert resolved_sym == sym_a, \
        f"Expected resolved_sym to stay {sym_a!r} (option_symbol priority) " \
        f"even though its price quote failed, got {resolved_sym!r}"
    assert anchor == sym_a, f"Expected anchor {sym_a!r}, got {anchor!r}"
