"""
Tests for backend/api/algo/symbol_resolver.py — virtual first-class symbols.

Verifies the five quality dimensions:
  SSOT  — all resolver logic goes through symbol_resolver.py; no duplicate
           filtering in watchlist.py / derivatives.py.
  Perf  — zero I/O (instruments cache monkey-patched in-process).
  Stale — grep-guards: derivatives.py + watchlist.py must not contain the
           inline ``inst.x > today_iso`` filter pattern after the refactor.
  Reuse — callers import from the canonical module.
  UX    — virtual labels round-trip correctly (forward + reverse).

Core invariants under test:
  - ``resolve_symbol("CRUDEOIL", "MCX")``   → front-month tradingsymbol
  - ``resolve_symbol("CRUDEOIL_NEXT", "MCX")`` → back-month tradingsymbol
  - ``resolve_symbol("CRUDEOIL26JUNFUT", "MCX")`` → pass-through (already real)
  - ``root_of("CRUDEOIL26JUNFUT", "MCX")``  → "CRUDEOIL"
  - ``root_of("CRUDEOIL26JULFUT", "MCX")``  → "CRUDEOIL_NEXT"
  - ``root_of("CRUDEOIL26AUGFUT", "MCX")``  → "CRUDEOIL26AUGFUT" (far-month)
  - CDS pair (USDINR) mirrors MCX path exactly.
  - On expiry day: front-month rolls to next contract (same as rollover tests).
  - All-expired cache-lag: fallback to first active or last listed contract.
  - Non-MCX/CDS exchanges pass through unchanged.
  - ``list_active_futures`` returns empty list for unknown root.
"""

from __future__ import annotations

import asyncio
import datetime as _real_datetime
from contextlib import ExitStack
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch


# ---------------------------------------------------------------------------
# Helpers — identical to test_near_month_rollover fixture pattern
# ---------------------------------------------------------------------------

def _make_fut(sym: str, underlying: str, exchange: str, expiry: str) -> SimpleNamespace:
    return SimpleNamespace(s=sym, u=underlying, e=exchange, t="FUT", x=expiry)


# Three CRUDEOIL contracts: JUN (front), JUL (back), AUG (far)
_CRUDEOIL = [
    _make_fut("CRUDEOIL26JUNFUT", "CRUDEOIL", "MCX", "2026-06-30"),
    _make_fut("CRUDEOIL26JULFUT", "CRUDEOIL", "MCX", "2026-07-31"),
    _make_fut("CRUDEOIL26AUGFUT", "CRUDEOIL", "MCX", "2026-08-31"),
]

# Two USDINR contracts: JUN (front), JUL (back)
_USDINR = [
    _make_fut("USDINR26JUNFUT", "USDINR", "CDS", "2026-06-25"),
    _make_fut("USDINR26JULFUT", "USDINR", "CDS", "2026-07-29"),
]


def _fake_resp(items):
    return SimpleNamespace(items=items)


def _make_fake_datetime_class(today_iso: str):
    d = _real_datetime.date.fromisoformat(today_iso)

    class _FakeDatetime(_real_datetime.datetime):
        @classmethod
        def now(cls, tz=None):
            return _real_datetime.datetime(d.year, d.month, d.day, 12, 0, 0, tzinfo=tz)

    return _FakeDatetime


def _patch_resolver(items, today_iso: str):
    """Patch get_or_fetch + datetime for symbol_resolver.py tests."""
    fake_dt = _make_fake_datetime_class(today_iso)

    async def _fake_gor(*_a, **_kw):
        return _fake_resp(items)

    return [
        patch("backend.api.cache.get_or_fetch",
              new=AsyncMock(side_effect=_fake_gor)),
        patch("datetime.datetime", new=fake_dt),
    ]


# ---------------------------------------------------------------------------
# list_active_futures
# ---------------------------------------------------------------------------

def test_list_active_futures_normal():
    """Returns front and back month in ascending-expiry order."""
    from backend.api.algo.symbol_resolver import list_active_futures

    with ExitStack() as stack:
        for cm in _patch_resolver(_CRUDEOIL, "2026-06-15"):
            stack.enter_context(cm)
        result = asyncio.run(list_active_futures("CRUDEOIL", "MCX", limit=2))

    assert result == ["CRUDEOIL26JUNFUT", "CRUDEOIL26JULFUT"], (
        f"Expected [JUN, JUL], got {result}")


def test_list_active_futures_limit_1():
    """limit=1 returns only front-month."""
    from backend.api.algo.symbol_resolver import list_active_futures

    with ExitStack() as stack:
        for cm in _patch_resolver(_CRUDEOIL, "2026-06-15"):
            stack.enter_context(cm)
        result = asyncio.run(list_active_futures("CRUDEOIL", "MCX", limit=1))

    assert result == ["CRUDEOIL26JUNFUT"]


def test_list_active_futures_skips_expiry_day():
    """On expiry day JUN is excluded; front-month becomes JUL."""
    from backend.api.algo.symbol_resolver import list_active_futures

    with ExitStack() as stack:
        for cm in _patch_resolver(_CRUDEOIL, "2026-06-30"):  # JUN expiry
            stack.enter_context(cm)
        result = asyncio.run(list_active_futures("CRUDEOIL", "MCX", limit=2))

    assert result == ["CRUDEOIL26JULFUT", "CRUDEOIL26AUGFUT"], (
        f"On expiry day JUN must be skipped; expected [JUL, AUG], got {result}")


def test_list_active_futures_unknown_root_empty():
    """Unknown commodity → empty list."""
    from backend.api.algo.symbol_resolver import list_active_futures

    with ExitStack() as stack:
        for cm in _patch_resolver(_CRUDEOIL, "2026-06-15"):
            stack.enter_context(cm)
        result = asyncio.run(list_active_futures("NONEXISTENT", "MCX", limit=2))

    assert result == []


def test_list_active_futures_cds():
    """CDS path works identically to MCX."""
    from backend.api.algo.symbol_resolver import list_active_futures

    with ExitStack() as stack:
        for cm in _patch_resolver(_USDINR, "2026-06-10"):
            stack.enter_context(cm)
        result = asyncio.run(list_active_futures("USDINR", "CDS", limit=2))

    assert result == ["USDINR26JUNFUT", "USDINR26JULFUT"]


# ---------------------------------------------------------------------------
# resolve_symbol — forward resolution
# ---------------------------------------------------------------------------

def test_resolve_symbol_front_month():
    """Bare root → front-month contract."""
    from backend.api.algo.symbol_resolver import resolve_symbol

    with ExitStack() as stack:
        for cm in _patch_resolver(_CRUDEOIL, "2026-06-15"):
            stack.enter_context(cm)
        result = asyncio.run(resolve_symbol("CRUDEOIL", "MCX"))

    assert result == "CRUDEOIL26JUNFUT"


def test_resolve_symbol_back_month():
    """Root_NEXT → back-month contract."""
    from backend.api.algo.symbol_resolver import resolve_symbol

    with ExitStack() as stack:
        for cm in _patch_resolver(_CRUDEOIL, "2026-06-15"):
            stack.enter_context(cm)
        result = asyncio.run(resolve_symbol("CRUDEOIL_NEXT", "MCX"))

    assert result == "CRUDEOIL26JULFUT"


def test_resolve_symbol_passthrough_real_contract():
    """Already-real contract passes through unchanged."""
    from backend.api.algo.symbol_resolver import resolve_symbol

    with ExitStack() as stack:
        for cm in _patch_resolver(_CRUDEOIL, "2026-06-15"):
            stack.enter_context(cm)
        result = asyncio.run(resolve_symbol("CRUDEOIL26JUNFUT", "MCX"))

    # Real contract has digits — not virtual, must pass through
    assert result == "CRUDEOIL26JUNFUT"


def test_resolve_symbol_non_mcx_passthrough():
    """Non-MCX/CDS exchange passes through unchanged."""
    from backend.api.algo.symbol_resolver import resolve_symbol

    with ExitStack() as stack:
        for cm in _patch_resolver([], "2026-06-15"):
            stack.enter_context(cm)
        result = asyncio.run(resolve_symbol("RELIANCE", "NSE"))

    assert result == "RELIANCE"


def test_resolve_symbol_back_month_only_one_contract():
    """_NEXT with only one active contract falls back to front-month."""
    from backend.api.algo.symbol_resolver import resolve_symbol

    # Only JUN listed (AUG is far future, not in instruments)
    only_jun = [_make_fut("CRUDEOIL26JUNFUT", "CRUDEOIL", "MCX", "2026-06-30")]
    with ExitStack() as stack:
        for cm in _patch_resolver(only_jun, "2026-06-15"):
            stack.enter_context(cm)
        result = asyncio.run(resolve_symbol("CRUDEOIL_NEXT", "MCX"))

    # Only one contract available — _NEXT falls back to it
    assert result == "CRUDEOIL26JUNFUT"


def test_resolve_symbol_rolls_on_expiry_day():
    """On expiry day, resolve_symbol returns the new front-month."""
    from backend.api.algo.symbol_resolver import resolve_symbol

    with ExitStack() as stack:
        for cm in _patch_resolver(_CRUDEOIL, "2026-06-30"):  # JUN expiry
            stack.enter_context(cm)
        result = asyncio.run(resolve_symbol("CRUDEOIL", "MCX"))

    assert result == "CRUDEOIL26JULFUT", (
        f"On expiry day CRUDEOIL should resolve to JUL, got {result!r}")


def test_resolve_symbol_cds_pair():
    """CDS currency pair resolution mirrors MCX."""
    from backend.api.algo.symbol_resolver import resolve_symbol

    with ExitStack() as stack:
        for cm in _patch_resolver(_USDINR, "2026-06-10"):
            stack.enter_context(cm)
        result = asyncio.run(resolve_symbol("USDINR", "CDS"))

    assert result == "USDINR26JUNFUT"


def test_resolve_symbol_cds_next():
    """CDS _NEXT resolution."""
    from backend.api.algo.symbol_resolver import resolve_symbol

    with ExitStack() as stack:
        for cm in _patch_resolver(_USDINR, "2026-06-10"):
            stack.enter_context(cm)
        result = asyncio.run(resolve_symbol("USDINR_NEXT", "CDS"))

    assert result == "USDINR26JULFUT"


# ---------------------------------------------------------------------------
# root_of — reverse resolution
# ---------------------------------------------------------------------------

def test_root_of_front_month():
    """Front-month contract → bare root."""
    from backend.api.algo.symbol_resolver import root_of

    with ExitStack() as stack:
        for cm in _patch_resolver(_CRUDEOIL, "2026-06-15"):
            stack.enter_context(cm)
        result = asyncio.run(root_of("CRUDEOIL26JUNFUT", "MCX"))

    assert result == "CRUDEOIL", f"Front-month should map to 'CRUDEOIL', got {result!r}"


def test_root_of_back_month():
    """Back-month contract → ROOT_NEXT."""
    from backend.api.algo.symbol_resolver import root_of

    with ExitStack() as stack:
        for cm in _patch_resolver(_CRUDEOIL, "2026-06-15"):
            stack.enter_context(cm)
        result = asyncio.run(root_of("CRUDEOIL26JULFUT", "MCX"))

    assert result == "CRUDEOIL_NEXT", (
        f"Back-month should map to 'CRUDEOIL_NEXT', got {result!r}")


def test_root_of_far_month_passthrough():
    """Far-month contract (slot > 1) → pass-through (raw contract)."""
    from backend.api.algo.symbol_resolver import root_of

    with ExitStack() as stack:
        for cm in _patch_resolver(_CRUDEOIL, "2026-06-15"):
            stack.enter_context(cm)
        result = asyncio.run(root_of("CRUDEOIL26AUGFUT", "MCX"))

    assert result == "CRUDEOIL26AUGFUT", (
        f"Far-month should pass through, got {result!r}")


def test_root_of_non_future_passthrough():
    """Options / equities pass through unchanged."""
    from backend.api.algo.symbol_resolver import root_of

    with ExitStack() as stack:
        for cm in _patch_resolver(_CRUDEOIL, "2026-06-15"):
            stack.enter_context(cm)
        result_eq   = asyncio.run(root_of("RELIANCE",           "NSE"))
        result_opt  = asyncio.run(root_of("CRUDEOIL26JUN8500PE", "MCX"))

    assert result_eq == "RELIANCE"
    assert result_opt == "CRUDEOIL26JUN8500PE"


def test_root_of_non_mcx_passthrough():
    """Non-MCX/CDS contracts pass through regardless of shape."""
    from backend.api.algo.symbol_resolver import root_of

    with ExitStack() as stack:
        for cm in _patch_resolver([], "2026-06-15"):
            stack.enter_context(cm)
        result = asyncio.run(root_of("NIFTY26JUNFUT", "NFO"))

    assert result == "NIFTY26JUNFUT"


def test_root_of_cds():
    """CDS currency pair reverse resolution."""
    from backend.api.algo.symbol_resolver import root_of

    with ExitStack() as stack:
        for cm in _patch_resolver(_USDINR, "2026-06-10"):
            stack.enter_context(cm)
        front = asyncio.run(root_of("USDINR26JUNFUT", "CDS"))
        back  = asyncio.run(root_of("USDINR26JULFUT", "CDS"))

    assert front == "USDINR"
    assert back  == "USDINR_NEXT"


def test_root_of_round_trips_with_resolve():
    """root_of(resolve_symbol(virtual)) == virtual for front and back-month."""
    from backend.api.algo.symbol_resolver import resolve_symbol, root_of

    for virtual in ("CRUDEOIL", "CRUDEOIL_NEXT"):
        with ExitStack() as stack:
            for cm in _patch_resolver(_CRUDEOIL, "2026-06-15"):
                stack.enter_context(cm)
            real = asyncio.run(resolve_symbol(virtual, "MCX"))
            back = asyncio.run(root_of(real, "MCX"))
        assert back == virtual, (
            f"Round-trip failed: {virtual!r} → {real!r} → {back!r}")


# ---------------------------------------------------------------------------
# SSOT guard — no inline inst.x filter in delegating modules
# ---------------------------------------------------------------------------

def test_ssot_derivatives_delegated_functions_no_inline_filter():
    """The three refactored functions in derivatives.py must NOT contain
    their own inline inst.x filter — they now delegate to symbol_resolver.

    Note: other functions (e.g. option_underlying_quote_key) may still
    contain inst.x comparisons for DIFFERENT cutoff semantics (e.g.
    ``inst.x > cutoff`` where cutoff is a matched-future expiry, not today).
    This guard checks only that the three delegating functions don't have
    a full inline list-comp fetching from the instruments cache.
    """
    import pathlib

    src = pathlib.Path(
        "backend/api/algo/derivatives.py"
    ).read_text()

    # The three refactored functions must not contain `get_or_fetch` calls
    # (they used to inline the entire cache fetch + filter; now they call
    # symbol_resolver which owns the fetch).
    import re
    # Extract just the bodies of the three wrapper functions to check them
    # in isolation — other functions legitimately use get_or_fetch.
    for fn_name in ("lookup_mcx_futures_list", "lookup_cds_futures_list",
                    "lookup_mcx_front_month_future"):
        # Find function start
        start_pat = re.compile(rf"async def {fn_name}\b")
        m = start_pat.search(src)
        assert m, f"Could not find {fn_name} in derivatives.py"
        fn_start = m.start()
        # Find the next function definition after this one
        next_fn = re.compile(r"\nasync def |\ndef ")
        nm = next_fn.search(src, fn_start + 5)
        fn_body = src[fn_start:nm.start() if nm else len(src)]
        assert "get_or_fetch" not in fn_body, (
            f"{fn_name} still calls get_or_fetch inline — should delegate "
            f"to symbol_resolver.list_active_futures")


def test_ssot_watchlist_no_inline_filter():
    """watchlist.py must NOT contain the inline inst.x > _today_iso filter.
    _resolve_mcx_commodity / _resolve_cds_currency delegate to symbol_resolver."""
    import pathlib

    src = pathlib.Path(
        "backend/api/routes/watchlist.py"
    ).read_text()

    assert "inst.x > _today_iso" not in src, (
        "watchlist.py still contains the inline inst.x filter — "
        "_resolve_mcx_commodity / _resolve_cds_currency should delegate to "
        "symbol_resolver.list_active_futures")


# ---------------------------------------------------------------------------
# Weekly-vs-monthly cadence — CDS lists both; resolver must pick monthlies
# ---------------------------------------------------------------------------

# USDINR mixed universe — CDS lists weeklies (numeric MMDD) alongside monthlies.
# Kite tradingsymbols observed on live dev cache (2026-07-02):
#   Weekly:  USDINR26703FUT, USDINR26710FUT (YY + MMDD)
#   Monthly: USDINR26JULFUT, USDINR26AUGFUT (YY + MON)
# The root+NEXT convention is monthly-cadence — weeklies MUST be filtered out
# so USDINR → USDINR26JULFUT (not USDINR26703FUT).
_USDINR_MIXED = [
    _make_fut("USDINR26703FUT", "USDINR", "CDS", "2026-07-03"),  # weekly (skip)
    _make_fut("USDINR26710FUT", "USDINR", "CDS", "2026-07-10"),  # weekly (skip)
    _make_fut("USDINR26717FUT", "USDINR", "CDS", "2026-07-17"),  # weekly (skip)
    _make_fut("USDINR26JULFUT", "USDINR", "CDS", "2026-07-29"),  # monthly front
    _make_fut("USDINR26AUGFUT", "USDINR", "CDS", "2026-08-27"),  # monthly back
    _make_fut("USDINR26SEPFUT", "USDINR", "CDS", "2026-09-28"),  # monthly far
]


def test_list_active_futures_cds_skips_weeklies():
    """CDS mixed universe: weeklies (YYMMDD-cadence) are filtered out; only
    monthly (YYMONFUT) contracts are returned by list_active_futures."""
    from backend.api.algo.symbol_resolver import list_active_futures

    with ExitStack() as stack:
        for cm in _patch_resolver(_USDINR_MIXED, "2026-07-01"):
            stack.enter_context(cm)
        result = asyncio.run(list_active_futures("USDINR", "CDS", limit=3))

    assert result == ["USDINR26JULFUT", "USDINR26AUGFUT", "USDINR26SEPFUT"], (
        f"Weeklies must be filtered; expected monthly-only, got {result}")


def test_resolve_symbol_cds_picks_monthly_front():
    """USDINR resolves to monthly front-month, NOT the nearest weekly."""
    from backend.api.algo.symbol_resolver import resolve_symbol

    with ExitStack() as stack:
        for cm in _patch_resolver(_USDINR_MIXED, "2026-07-01"):
            stack.enter_context(cm)
        result = asyncio.run(resolve_symbol("USDINR", "CDS"))

    assert result == "USDINR26JULFUT", (
        f"USDINR must skip weeklies and pick monthly front, got {result!r}")


def test_resolve_symbol_cds_next_picks_monthly_back():
    """USDINR_NEXT resolves to monthly back-month, NOT the second weekly."""
    from backend.api.algo.symbol_resolver import resolve_symbol

    with ExitStack() as stack:
        for cm in _patch_resolver(_USDINR_MIXED, "2026-07-01"):
            stack.enter_context(cm)
        result = asyncio.run(resolve_symbol("USDINR_NEXT", "CDS"))

    assert result == "USDINR26AUGFUT", (
        f"USDINR_NEXT must be monthly back, got {result!r}")


def test_root_of_cds_monthly_round_trip_with_weeklies_present():
    """Reverse resolver still identifies front/back correctly when weeklies
    are also listed in the instruments cache. Weeklies pass through as raw
    contracts (they're not part of the root+NEXT convention)."""
    from backend.api.algo.symbol_resolver import root_of

    with ExitStack() as stack:
        for cm in _patch_resolver(_USDINR_MIXED, "2026-07-01"):
            stack.enter_context(cm)
        front  = asyncio.run(root_of("USDINR26JULFUT", "CDS"))
        back   = asyncio.run(root_of("USDINR26AUGFUT", "CDS"))
        weekly = asyncio.run(root_of("USDINR26710FUT", "CDS"))

    assert front == "USDINR"
    assert back  == "USDINR_NEXT"
    # Weekly tradingsymbol doesn't match _FUT_RE (needs 3-letter month) so
    # falls through to raw pass-through — correct behaviour.
    assert weekly == "USDINR26710FUT"


# ---------------------------------------------------------------------------
# Parametrized 7-root convention check — covers every MARKETS_DEFAULT
# MCX/CDS bare root and asserts front+back both resolve non-null.
# ---------------------------------------------------------------------------

def _mixed_universe_for(root: str, exchange: str) -> list:
    """Build a synthetic 3-contract monthly-cadence fixture for *root* on
    *exchange*.  Every root gets the same shape (JUL/AUG/SEP 2026) so the
    parametrized test can assert deterministic front/back values."""
    return [
        _make_fut(f"{root}26JULFUT", root, exchange, "2026-07-29"),
        _make_fut(f"{root}26AUGFUT", root, exchange, "2026-08-27"),
        _make_fut(f"{root}26SEPFUT", root, exchange, "2026-09-28"),
    ]


_PINNED_ROOTS: list[tuple[str, str]] = [
    ("SILVER",   "MCX"),
    ("SILVERM",  "MCX"),
    ("GOLD",     "MCX"),
    ("GOLDM",    "MCX"),
    ("CRUDEOIL", "MCX"),
    ("COPPER",   "MCX"),
    ("USDINR",   "CDS"),
]


def test_pinned_roots_all_resolve_front_and_back():
    """Every pinned MCX/CDS root must resolve front (root) + back (_NEXT)
    to non-null monthly-cadence contracts.  If any root fails this check
    it must be removed from MARKETS_DEFAULT."""
    from backend.api.algo.symbol_resolver import resolve_symbol

    for root, exchange in _PINNED_ROOTS:
        universe = _mixed_universe_for(root, exchange)
        with ExitStack() as stack:
            for cm in _patch_resolver(universe, "2026-07-01"):
                stack.enter_context(cm)
            front = asyncio.run(resolve_symbol(root, exchange))
            back  = asyncio.run(resolve_symbol(f"{root}_NEXT", exchange))

        assert front == f"{root}26JULFUT", (
            f"{root}/{exchange} front-month convention broken; got {front!r}")
        assert back == f"{root}26AUGFUT", (
            f"{root}/{exchange} _NEXT back-month convention broken; got {back!r}")


def test_pinned_roots_all_round_trip():
    """Every pinned root: resolve_symbol → root_of round-trips cleanly for
    both front and back-month. This is the invariant that makes the display-
    label ↔ ticker-subscription contract work end-to-end."""
    from backend.api.algo.symbol_resolver import resolve_symbol, root_of

    for root, exchange in _PINNED_ROOTS:
        universe = _mixed_universe_for(root, exchange)
        for virtual in (root, f"{root}_NEXT"):
            with ExitStack() as stack:
                for cm in _patch_resolver(universe, "2026-07-01"):
                    stack.enter_context(cm)
                real = asyncio.run(resolve_symbol(virtual, exchange))
                back = asyncio.run(root_of(real, exchange))
            assert back == virtual, (
                f"Round-trip failed for {root}/{exchange}: "
                f"{virtual!r} → {real!r} → {back!r}")


def test_pinned_roots_next_falls_back_when_only_one_contract():
    """During transition weeks a root may have only ONE active monthly
    contract in the instruments cache.  _NEXT must fall back to the front
    month (not None) so the row stays visible until the new back-month lists."""
    from backend.api.algo.symbol_resolver import resolve_symbol

    for root, exchange in _PINNED_ROOTS:
        universe = [_make_fut(f"{root}26JULFUT", root, exchange, "2026-07-29")]
        with ExitStack() as stack:
            for cm in _patch_resolver(universe, "2026-07-01"):
                stack.enter_context(cm)
            back = asyncio.run(resolve_symbol(f"{root}_NEXT", exchange))
        assert back == f"{root}26JULFUT", (
            f"{root}/{exchange} _NEXT must fall back to front-month during "
            f"transition weeks; got {back!r}")
