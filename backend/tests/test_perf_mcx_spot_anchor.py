"""
Test 4 — Spot-anchor subscription for all option underlyings (Change 5 + extension).

Verifies that _perf_subscribe_book_symbols() subscribes:
  - The front-month MCX future when MCX options positions (CE/PE) are present
    (e.g. CRUDEOIL25AUGCE → CRUDEOIL25AUGFUT on MCX).
  - The underlying equity/index on NSE when NFO/BFO options are present
    (e.g. NIFTY24500CE → NIFTY on NSE, RELIANCE24AUG3000CE → RELIANCE on NSE).

In both cases the goal is: liveSpot for the derivatives payoff chart runs from
the real-time tick stream instead of the 30s batchQuote cadence.

Five quality dimensions:
  SSOT       — Tests _perf_subscribe_book_symbols at the integration level;
               list_active_futures is the resolver SSOT for MCX; direct NSE
               symbol append is the SSOT for NFO/BFO.
  Correctness— MCX CE/PE → front-month future; NFO/BFO CE/PE → NSE root equity.
  Performance— fully mocked; no network calls, no DB.
  Reuse      — _perf_collect_book_pairs helper reused unchanged.
  UX         — Operator sees real-time tick on payoff chart for ALL option types.
"""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
import pandas as pd


async def _run_subscribe(book_pairs_override, active_futures_return):
    """Call _perf_subscribe_book_symbols with fully mocked dependencies.

    book_pairs_override: list of (sym, exch) returned by _perf_collect_book_pairs
    active_futures_return: list[str] returned by list_active_futures
    """
    from backend.api.background import _perf_subscribe_book_symbols

    # Mock ticker — has_sym always False so everything goes to resolve
    mock_ticker = MagicMock()
    mock_ticker.has_sym.return_value = False
    mock_ticker.subscribe_with_sym = MagicMock()

    # _perf_collect_book_pairs returns (book_pairs, book_seen)
    book_seen = set(book_pairs_override)
    mock_collect = MagicMock(return_value=(list(book_pairs_override), book_seen.copy()))

    # _snapshot_book_symbols returns empty set — no backstop interference
    mock_snap = AsyncMock(return_value=set())

    # _resolve_token_for_sym returns a fake token
    async def _mock_rts(sym, exch):
        return hash((sym, exch)) % 100000

    # list_active_futures returns the caller-supplied list
    async def _mock_laf(root, exchange, limit=1):
        return active_futures_return

    # _bg_resolve_tokens_chunked is called internally — let it run normally
    # but with the mocked _rts; we patch only the imports used inside the fn

    with patch("backend.brokers.kite_ticker.get_ticker", return_value=mock_ticker), \
         patch("backend.api.background._perf_collect_book_pairs", mock_collect), \
         patch("backend.api.background._snapshot_book_symbols", mock_snap), \
         patch("backend.api.routes.quote._resolve_token_for_sym", side_effect=_mock_rts), \
         patch("backend.api.algo.symbol_resolver.list_active_futures", side_effect=_mock_laf):
        await _perf_subscribe_book_symbols(
            pd.DataFrame(),  # df_holdings (unused — collect is mocked)
            pd.DataFrame(),  # df_positions (unused — collect is mocked)
        )

    return mock_ticker


@pytest.mark.asyncio
async def test_mcx_option_triggers_future_subscription():
    """When book_pairs contains a MCX CE option, the front-month future
    must be added to the subscription batch."""
    ticker = await _run_subscribe(
        book_pairs_override=[("CRUDEOIL25AUGCE", "MCX")],
        active_futures_return=["CRUDEOIL25AUGFUT"],
    )

    assert ticker.subscribe_with_sym.called, (
        "subscribe_with_sym must be called at least once"
    )
    # Collect all (token, sym) pairs that were subscribed
    all_subscribed_syms = {
        sym
        for call in ticker.subscribe_with_sym.call_args_list
        for _tok, sym in call.args[0]
    }
    assert "CRUDEOIL25AUGFUT" in all_subscribed_syms, (
        f"CRUDEOIL25AUGFUT (front-month future) must be subscribed as spot anchor; "
        f"subscribed: {all_subscribed_syms}"
    )


@pytest.mark.asyncio
async def test_nse_option_does_not_trigger_mcx_future():
    """NFO options must NOT trigger an MCX future subscription."""
    ticker = await _run_subscribe(
        book_pairs_override=[("NIFTY26AUGCE", "NFO")],
        active_futures_return=["CRUDEOIL25AUGFUT"],  # should never be called by MCX path
    )

    # list_active_futures is patched globally but must not be invoked for NFO.
    # Even if ticker.subscribe_with_sym is called (for NIFTY NSE root), the MCX
    # future must never appear in the subscription batch.
    all_subscribed_syms = {
        sym
        for call in ticker.subscribe_with_sym.call_args_list
        for _tok, sym in call.args[0]
    } if ticker.subscribe_with_sym.called else set()

    assert "CRUDEOIL25AUGFUT" not in all_subscribed_syms, (
        "CRUDEOIL25AUGFUT must NOT be subscribed for NFO option positions"
    )


# ---------------------------------------------------------------------------
# NFO/BFO spot-anchor tests (NSE underlying equity/index subscription)
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_nfo_option_triggers_nse_underlying_subscription():
    """NIFTY24500CE (NFO) must cause ('NIFTY', 'NSE') to be subscribed."""
    ticker = await _run_subscribe(
        book_pairs_override=[("NIFTY24500CE", "NFO")],
        active_futures_return=[],  # MCX path returns nothing (irrelevant here)
    )

    assert ticker.subscribe_with_sym.called, (
        "subscribe_with_sym must be called at least once for NFO option"
    )
    all_subscribed_syms = {
        sym
        for call in ticker.subscribe_with_sym.call_args_list
        for _tok, sym in call.args[0]
    }
    assert "NIFTY" in all_subscribed_syms, (
        f"NIFTY (NSE underlying) must be subscribed when NIFTY24500CE (NFO) is held; "
        f"subscribed: {all_subscribed_syms}"
    )


@pytest.mark.asyncio
async def test_stock_nfo_option_triggers_nse_underlying_subscription():
    """RELIANCE24AUG3000CE (NFO stock option) must cause ('RELIANCE', 'NSE') to be
    subscribed so the payoff chart spot price updates at tick cadence."""
    ticker = await _run_subscribe(
        book_pairs_override=[("RELIANCE24AUG3000CE", "NFO")],
        active_futures_return=[],
    )

    all_subscribed_syms = {
        sym
        for call in ticker.subscribe_with_sym.call_args_list
        for _tok, sym in call.args[0]
    } if ticker.subscribe_with_sym.called else set()

    assert "RELIANCE" in all_subscribed_syms, (
        f"RELIANCE (NSE underlying) must be subscribed when RELIANCE24AUG3000CE (NFO) is held; "
        f"subscribed: {all_subscribed_syms}"
    )


@pytest.mark.asyncio
async def test_nfo_underlying_not_duplicated_if_already_in_book():
    """When NIFTY is already in book_pairs as ('NIFTY', 'NSE'), it must NOT be
    appended a second time (dedup via already_subscribed set)."""
    ticker = await _run_subscribe(
        book_pairs_override=[
            ("NIFTY24500CE", "NFO"),
            ("NIFTY", "NSE"),  # already held directly
        ],
        active_futures_return=[],
    )

    all_subscribed_syms_list = [
        sym
        for call in ticker.subscribe_with_sym.call_args_list
        for _tok, sym in call.args[0]
    ]
    count = all_subscribed_syms_list.count("NIFTY")
    assert count <= 1, (
        f"NIFTY must not be subscribed more than once; found {count} times"
    )


@pytest.mark.asyncio
async def test_mcx_future_not_duplicated_if_already_in_book():
    """If the MCX future is already in book_pairs (held directly), it must
    not be appended a second time (dedup via book_seen set)."""
    ticker = await _run_subscribe(
        book_pairs_override=[
            ("CRUDEOIL25AUGCE", "MCX"),
            ("CRUDEOIL25AUGFUT", "MCX"),  # already present
        ],
        active_futures_return=["CRUDEOIL25AUGFUT"],
    )

    all_subscribed_syms_list = [
        sym
        for call in ticker.subscribe_with_sym.call_args_list
        for _tok, sym in call.args[0]
    ]
    count = all_subscribed_syms_list.count("CRUDEOIL25AUGFUT")
    assert count <= 1, (
        f"CRUDEOIL25AUGFUT must not be subscribed more than once; found {count} times"
    )


@pytest.mark.asyncio
async def test_empty_list_active_futures_no_crash():
    """If list_active_futures returns [] (cold instruments cache), the function
    must not crash — just skip that root."""
    try:
        await _run_subscribe(
            book_pairs_override=[("CRUDEOIL25AUGCE", "MCX")],
            active_futures_return=[],  # empty — instruments cache cold
        )
    except Exception as exc:
        pytest.fail(f"_perf_subscribe_book_symbols must not raise on empty futures list: {exc}")


@pytest.mark.asyncio
async def test_spot_anchor_code_present_in_source():
    """Both MCX and NFO/BFO spot-anchor blocks must be present in
    _perf_subscribe_book_symbols source."""
    import inspect
    from backend.api.background import _perf_subscribe_book_symbols
    src = inspect.getsource(_perf_subscribe_book_symbols)
    # MCX path: must call list_active_futures
    assert "list_active_futures" in src or "_laf" in src, (
        "_perf_subscribe_book_symbols must call list_active_futures for MCX spot anchors"
    )
    assert "MCX" in src and "(CE|PE)" in src, (
        "MCX option pattern (CE|PE) must be present in the subscription logic"
    )
    # NFO/BFO path: must handle NFO and BFO exchanges and append NSE root
    assert "NFO" in src and "BFO" in src, (
        "NFO and BFO exchange checks must be present for equity-option spot anchors"
    )
    assert "'NSE'" in src or '"NSE"' in src, (
        "NSE exchange must be appended as the spot anchor for NFO/BFO options"
    )
