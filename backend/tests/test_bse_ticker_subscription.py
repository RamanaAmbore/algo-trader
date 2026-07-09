"""
test_bse_ticker_subscription.py

Covers the BSE-only ticker subscription fix (background.py chunked uncap +
quote.py Tier 3 per-exchange warning log).

Root cause: _perf_subscribe_book_symbols used a hard [:50] slice on
_need_resolve before calling asyncio.gather.  A portfolio with >50
unresolved NSE/NFO symbols meant BSE-only holdings that appeared after
position 50 in _book_pairs were silently dropped every cycle.

Fix: replaced the slice with a chunked loop (CHUNK=50) that processes
every unresolved symbol.  All chunks are processed sequentially in the
hot-path task; the total is unbounded.

Five quality dimensions (feedback_test_dimensions.md):

  1. SSOT       — _perf_subscribe_book_symbols is the single subscription
                  driver in the perf task; _register_universe_with_ticker
                  is the warm path.  Both must cover the full universe.
  2. Performance — chunked gather keeps concurrency bounded (50 per
                  gather) while processing the full list in O(N/50) rounds.
  3. Stale code  — grep verifies the old [:50] slice is gone.
  4. Reusable    — _resolve_token_for_sym is the single token-resolution
                  callsite; no inline broker.instruments() in the perf task.
  5. Correctness — three scenarios: BSE past 50-cap, BSE token resolution,
                  BSE empty-exchange warning.
"""

from __future__ import annotations

import asyncio
import inspect
from unittest.mock import AsyncMock, MagicMock, patch

import pytest


# ---------------------------------------------------------------------------
# 1. Stale-code guard — hard cap slice must be gone
# ---------------------------------------------------------------------------

def test_no_hard_cap_slice_in_perf_subscribe():
    """The old `_need_resolve[:50]` slice must not appear in
    _perf_subscribe_book_symbols after the fix."""
    import backend.api.background as bg

    src = inspect.getsource(bg._perf_subscribe_book_symbols)
    assert "_need_resolve[:50]" not in src, (
        "Hard cap '_need_resolve[:50]' must be removed from "
        "_perf_subscribe_book_symbols — use chunked loop instead"
    )
    assert "capped = _need_resolve[:50]" not in src, (
        "'capped = _need_resolve[:50]' must be removed — "
        "BSE-only holdings after position 50 would never be subscribed"
    )


# ---------------------------------------------------------------------------
# 2. SSOT — chunked loop present
# ---------------------------------------------------------------------------

def test_chunked_loop_present_in_perf_subscribe():
    """_perf_subscribe_book_symbols must use a CHUNK variable and a for loop
    to iterate over all unresolved symbols, not a single gather over a slice."""
    import backend.api.background as bg

    src = inspect.getsource(bg._perf_subscribe_book_symbols)
    assert "CHUNK" in src, (
        "CHUNK constant must be present in _perf_subscribe_book_symbols"
    )
    assert "range(0, len(_need_resolve), CHUNK)" in src, (
        "Chunked range loop over _need_resolve must be present"
    )


# ---------------------------------------------------------------------------
# 3. Correctness — BSE symbol subscribed even when >50 NSE symbols precede it
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_bse_symbol_subscribed_past_50_nse():
    """Create 51 NSE-unresolved holdings + 1 BSE-only holding.
    After _perf_subscribe_book_symbols, the BSE token must be included in the
    batch passed to subscribe_with_sym (not dropped by any cap).
    """
    import pandas as pd
    import backend.api.background as bg

    # 51 NSE holdings that are "not yet subscribed"
    nse_syms = [f"NSE{i:03d}" for i in range(51)]
    bse_sym  = "BSEONLYSYM"

    # Holdings DataFrame: 51 NSE + 1 BSE
    all_rows = [{"tradingsymbol": s, "exchange": "NSE"} for s in nse_syms]
    all_rows.append({"tradingsymbol": bse_sym, "exchange": "BSE"})
    df_holdings = pd.DataFrame(all_rows)
    df_positions = pd.DataFrame()   # empty

    # Token assignments: NSE tokens 1..51, BSE token 9_000_001
    def _fake_resolve(sym, exch):
        if exch == "BSE" and sym == bse_sym:
            return 9_000_001
        # NSE symbols get incremental tokens
        try:
            idx = int(sym.replace("NSE", ""))
            return idx + 1
        except Exception:
            return None

    # Fake ticker: nothing subscribed yet → has_sym always False
    mock_ticker = MagicMock()
    mock_ticker.has_sym.return_value = False

    # Capture what is passed to subscribe_with_sym
    subscribed_batches: list[list[tuple[int, str]]] = []

    def _fake_subscribe(pairs):
        subscribed_batches.append(list(pairs))

    mock_ticker.subscribe_with_sym.side_effect = _fake_subscribe

    # Patch _get_ticker, _resolve_token_for_sym, and _snapshot_book_symbols
    with (
        patch("backend.brokers.kite_ticker.get_ticker", return_value=mock_ticker),
        patch(
            "backend.api.routes.quote._resolve_token_for_sym",
            new=AsyncMock(side_effect=_fake_resolve),
        ),
        patch(
            "backend.api.background._snapshot_book_symbols",
            new=AsyncMock(return_value=[]),
        ),
    ):
        await bg._perf_subscribe_book_symbols(df_holdings, df_positions)

    # Flatten all (token, sym) pairs from all subscribe_with_sym calls
    all_pairs = [pair for batch in subscribed_batches for pair in batch]
    subscribed_syms = {sym for _tok, sym in all_pairs}
    subscribed_tokens = {tok for tok, _sym in all_pairs}

    assert bse_sym in subscribed_syms, (
        f"BSE-only symbol '{bse_sym}' must be subscribed even when >50 NSE symbols "
        "precede it in _book_pairs.  Old code capped at 50 and dropped it."
    )
    assert 9_000_001 in subscribed_tokens, (
        "BSE token 9_000_001 must appear in the subscribe_with_sym batch"
    )
    # All 51 NSE symbols must also be subscribed
    for s in nse_syms:
        assert s in subscribed_syms, f"NSE symbol {s} missing from subscribed set"


# ---------------------------------------------------------------------------
# 4. Correctness — BSE token resolution via _get_today_token_map
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_bse_token_resolution_via_token_map():
    """_resolve_token_for_sym("BSEONLYSYM", "BSE") must return the token from
    the day-cached map when BSE is present in _get_today_token_map."""
    from backend.api.routes.quote import _resolve_token_for_sym

    fake_map = {
        ("BSEONLYSYM", "BSE"): 9_000_001,
    }

    mock_broker = MagicMock()

    with (
        patch("backend.brokers.registry.get_sparkline_broker", return_value=mock_broker),
        patch(
            "backend.api.routes.quote._get_today_token_map",
            return_value=fake_map,
        ),
    ):
        # asyncio.to_thread is called internally; patch the sync fn instead
        with patch("asyncio.to_thread", new=AsyncMock(return_value=fake_map)):
            tok = await _resolve_token_for_sym("BSEONLYSYM", "BSE")

    assert tok == 9_000_001, (
        f"Expected token 9_000_001 for BSE-only symbol, got {tok}"
    )


# ---------------------------------------------------------------------------
# 5. Observability — empty BSE instruments triggers a warning log
# ---------------------------------------------------------------------------

def test_bse_empty_instruments_logs_warning():
    """When broker.instruments('BSE') returns [], _get_today_token_map must
    call logger.warning with a message mentioning 'BSE' and '0 instruments'
    so operators can identify BSE feed-permission issues.

    Note: ramboq_logger sets propagate=False so pytest caplog does not capture
    its output.  We patch the module-level logger directly instead.
    """
    # We need to exercise the Tier 3 (live broker fetch) path, which means
    # Tier 1 (_instr_mem) and Tier 2 (_TOKEN_MAP_CACHE) must both be cold.
    from backend.api.routes import quote as q_mod

    # Clear module-level caches to force Tier 3
    q_mod._TOKEN_MAP_CACHE.clear()

    mock_broker = MagicMock()

    def _instruments_side_effect(exch):
        if exch == "BSE":
            return []          # empty → should trigger warning
        return [{"tradingsymbol": "RELIANCE", "instrument_token": 12345}]

    mock_broker.instruments.side_effect = _instruments_side_effect

    warning_calls: list[str] = []

    def _capture_warning(msg, *args, **kwargs):
        warning_calls.append(str(msg))

    # Freeze Tier 1 path — patch _instr_mem so warm_keys check fails
    empty_mem: dict = {}
    with (
        patch("backend.api.persistence.instruments_store._MEM_CACHE", empty_mem),
        patch("backend.api.persistence.instruments_store._purge_stale"),
        patch("backend.api.routes.quote._trigger_instruments_store_populate"),
        patch.object(q_mod.logger, "warning", side_effect=_capture_warning),
    ):
        q_mod._get_today_token_map(mock_broker)

    bse_warning_found = any(
        "BSE" in msg and "0 instruments" in msg
        for msg in warning_calls
    )
    assert bse_warning_found, (
        "A WARNING containing 'BSE' and '0 instruments' must be emitted when "
        "broker.instruments('BSE') returns an empty list.  Captured warnings: "
        + str(warning_calls)
    )
