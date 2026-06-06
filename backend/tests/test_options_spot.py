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
        "backend.shared.brokers.registry.get_price_broker",
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
    jun_inst = _inst("CRUDEOIL26JUNFUT", "CRUDEOIL", "2026-06-18")
    sep_inst = _inst("CRUDEOIL26SEPFUT", "CRUDEOIL", "2026-09-18")
    items = [jun_inst, sep_inst]

    # Front-month = JUN (earliest non-expired); only JUN responds with LTP
    ltp_map = {"MCX:CRUDEOIL26JUNFUT": 6800.0}

    from backend.api.routes.options import _resolve_spot

    with _patch_no_sim(), \
         _patch_instruments(items), \
         _patch_broker_quote(ltp_map):
        spot, src, prev_close, anchor = await _resolve_spot(
            "CRUDEOIL", None, expiry_hint=None
        )

    assert src == "futures"
    assert abs(spot - 6800.0) < 0.01
    assert anchor == "CRUDEOIL26JUNFUT"


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
    jun_inst = _inst("CRUDEOIL26JUNFUT", "CRUDEOIL", "2026-06-18")
    jul_inst = _inst("CRUDEOIL26JULFUT", "CRUDEOIL", "2026-07-18")
    items = [jun_inst, jul_inst]

    # Dec 2026 option — no listed future yet; lookup_mcx_future_for_expiry
    # returns the last available (JUL). Front-month fallback would also give
    # JUN. The calendar-aware helper wins (JUL is closer to Dec than JUN).
    ltp_map = {
        "MCX:CRUDEOIL26JUNFUT": 6800.0,
        "MCX:CRUDEOIL26JULFUT": 6860.0,
    }

    from backend.api.routes.options import _resolve_spot

    with _patch_no_sim(), \
         _patch_instruments(items), \
         _patch_broker_quote(ltp_map):
        spot, src, prev_close, anchor = await _resolve_spot(
            "CRUDEOIL", None,
            expiry_hint=date(2026, 12, 18),  # Far-out option, no listed future
        )

    assert src == "futures"
    # lookup_mcx_future_for_expiry returns candidates[-1] = JULFUT (last listed)
    assert anchor == "CRUDEOIL26JULFUT", f"Expected CRUDEOIL26JULFUT, got {anchor}"
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
