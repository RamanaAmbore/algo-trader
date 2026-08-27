"""
Tests for holdings.py and positions.py snapshot-path fixes.

Covers:
  Fix 5 — _holdings_snapshot snapshot path calls _override_stale_close_for_holdings
  Fix 6 — _override_stale_close_for_holdings always sets close_price=ref_close
           (epsilon guard removed)
  Fix 7 — _overlay_snapshot_for_closed_exchanges patches day_change_val,
           day_change_percentage, and close_price for closed-exchange rows

Five quality dimensions per fix:
  1. SSOT     — canonical function is called; no inline re-implementation
  2. Perf     — no redundant DB queries for open-exchange rows
  3. Stale    — epsilon guard removed (Fix 6); snapshot path patched (Fix 5)
  4. Reuse    — shared helper called from both broker and snapshot paths
  5. UX       — patched values are mathematically correct
"""

from __future__ import annotations

import asyncio
import pandas as pd
import pytest
from unittest.mock import AsyncMock, MagicMock, patch, call
from datetime import datetime, timezone


# ---------------------------------------------------------------------------
# Fix 5 — snapshot path calls _override_stale_close_for_holdings
# ---------------------------------------------------------------------------

def _make_raw_row(
    account="ZG0001",
    symbol="RELIANCE",
    exchange="NSE",
    qty=10,
    avg_cost=2000.0,
    ltp=2100.0,
    previous_close=2050.0,
    day_pnl=500.0,
    total_pnl=1000.0,
    prev_ltp=2050.0,
):
    """Build a tuple matching the _HOLDINGS_SNAPSHOT_SQL column order."""
    captured_at = datetime(2026, 8, 27, 15, 30, tzinfo=timezone.utc)
    return (account, symbol, exchange, qty, avg_cost, ltp,
            previous_close, day_pnl, total_pnl, captured_at, prev_ltp)


@pytest.mark.asyncio
async def test_holdings_snapshot_calls_override_stale_close():
    """_holdings_snapshot must call _override_stale_close_for_holdings on the
    snapshot path so previous_close and close_price reflect the real settlement LTP."""
    from backend.api.routes import holdings as _hol_mod

    raw_rows = [_make_raw_row()]

    override_called_with: list = []

    async def _fake_override(raw_df: pd.DataFrame) -> None:
        override_called_with.append(raw_df.copy())
        # No-op: we just verify it was called with a non-empty DataFrame.

    with patch.object(_hol_mod, "_query_holdings_snapshot_rows",
                      new=AsyncMock(return_value=raw_rows)), \
         patch.object(_hol_mod, "_override_stale_close_for_holdings",
                      new=_fake_override):
        result = await _hol_mod._holdings_snapshot()

    assert result is not None, "_holdings_snapshot must return a response when rows exist"
    assert len(override_called_with) == 1, (
        "_override_stale_close_for_holdings was not called on the snapshot path"
    )
    df = override_called_with[0]
    assert not df.empty, (
        "_override_stale_close_for_holdings must receive a non-empty DataFrame"
    )
    assert "account" in df.columns
    assert "tradingsymbol" in df.columns


@pytest.mark.asyncio
async def test_holdings_snapshot_empty_rows_skips_override():
    """When no rows are returned, _override_stale_close_for_holdings is not called
    and _holdings_snapshot returns None."""
    from backend.api.routes import holdings as _hol_mod

    override_called = False

    async def _fake_override(raw_df: pd.DataFrame) -> None:
        nonlocal override_called
        override_called = True

    with patch.object(_hol_mod, "_query_holdings_snapshot_rows",
                      new=AsyncMock(return_value=[])), \
         patch.object(_hol_mod, "_override_stale_close_for_holdings",
                      new=_fake_override):
        result = await _hol_mod._holdings_snapshot()

    assert result is None
    assert not override_called, (
        "_override_stale_close_for_holdings must not be called when there are no rows"
    )


@pytest.mark.asyncio
async def test_holdings_snapshot_override_patches_rows():
    """_holdings_snapshot must propagate the patched previous_close + day_change_val
    from _override_stale_close_for_holdings into the returned HoldingRow structs."""
    from backend.api.routes import holdings as _hol_mod

    raw_rows = [_make_raw_row(ltp=2100.0, previous_close=2050.0)]
    patched_prev_close = 2040.0  # DB says 2040, not 2050 (BHAV was stale)

    async def _fake_override(raw_df: pd.DataFrame) -> None:
        # Simulate the override patching previous_close and day_change_val.
        raw_df['previous_close'] = patched_prev_close
        # day_change_val = (ltp - prev_close) * qty = (2100 - 2040) * 10 = 600
        raw_df['day_change_val'] = (raw_df['last_price'] - patched_prev_close) * raw_df['quantity']
        raw_df['close_price'] = patched_prev_close

    with patch.object(_hol_mod, "_query_holdings_snapshot_rows",
                      new=AsyncMock(return_value=raw_rows)), \
         patch.object(_hol_mod, "_override_stale_close_for_holdings",
                      new=_fake_override):
        result = await _hol_mod._holdings_snapshot()

    assert result is not None
    assert result.rows, "Response must contain at least one row"
    row = result.rows[0]
    assert row.previous_close == patched_prev_close, (
        f"previous_close should be {patched_prev_close} after override, got {row.previous_close}"
    )
    assert row.close_price == patched_prev_close, (
        f"close_price should be {patched_prev_close} after override, got {row.close_price}"
    )
    expected_dcv = (2100.0 - patched_prev_close) * 10  # 600.0
    assert abs(row.day_change_val - expected_dcv) < 0.01, (
        f"day_change_val should be {expected_dcv} after override, got {row.day_change_val}"
    )


# ---------------------------------------------------------------------------
# Fix 6 — epsilon guard removed: close_price always set to ref_close
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_override_stale_close_no_epsilon_guard_small_diff():
    """_override_stale_close_for_holdings must set close_price=ref_close even when
    the difference from the current close_price is less than 0.005 (epsilon removed)."""
    from backend.api.routes import holdings as _hol_mod

    # Difference < 0.005 — the old epsilon guard would have skipped this row.
    current_close = 2050.00
    ref_close_in_db = 2050.003  # diff = 0.003, less than the old 0.005 threshold

    raw = pd.DataFrame([{
        "account": "ZG0001",
        "tradingsymbol": "RELIANCE",
        "close_price": current_close,
        "last_price": 2100.0,
        "quantity": 10,
        "day_change_val": 500.0,
        "day_change": 50.0,
        "previous_close": 0.0,
    }])

    fake_snapshot_map = {("ZG0001", "RELIANCE"): ref_close_in_db}

    async def _fake_db_query(*_args, **_kwargs):
        return MagicMock(all=lambda: [
            ("ZG0001", "RELIANCE", ref_close_in_db)
        ])

    with patch("backend.api.database.async_session") as mock_async_session:
        mock_session = AsyncMock()
        mock_async_session.return_value.__aenter__ = AsyncMock(return_value=mock_session)
        mock_async_session.return_value.__aexit__ = AsyncMock(return_value=False)
        mock_session.execute = AsyncMock(return_value=MagicMock(
            all=lambda: [("ZG0001", "RELIANCE", ref_close_in_db)]
        ))

        await _hol_mod._override_stale_close_for_holdings(raw)

    # After the fix, close_price MUST equal ref_close_in_db (no epsilon gate).
    assert raw.at[0, "close_price"] == ref_close_in_db, (
        f"close_price should be {ref_close_in_db} (no epsilon guard), "
        f"but got {raw.at[0, 'close_price']}"
    )
    assert raw.at[0, "previous_close"] == ref_close_in_db, (
        f"previous_close should be {ref_close_in_db}, got {raw.at[0, 'previous_close']}"
    )


@pytest.mark.asyncio
async def test_override_stale_close_always_sets_close_price_zero_diff():
    """Even when close_price == ref_close (diff = 0), close_price is still set.
    This ensures close_price is always explicitly populated from the snapshot map."""
    from backend.api.routes import holdings as _hol_mod

    ref_close = 2050.0
    raw = pd.DataFrame([{
        "account": "ZG0001",
        "tradingsymbol": "INFY",
        "close_price": ref_close,  # already equal
        "last_price": 2060.0,
        "quantity": 5,
        "day_change_val": 50.0,
        "day_change": 10.0,
        "previous_close": 0.0,
    }])

    with patch("backend.api.database.async_session") as mock_async_session:
        mock_session = AsyncMock()
        mock_async_session.return_value.__aenter__ = AsyncMock(return_value=mock_session)
        mock_async_session.return_value.__aexit__ = AsyncMock(return_value=False)
        mock_session.execute = AsyncMock(return_value=MagicMock(
            all=lambda: [("ZG0001", "INFY", ref_close)]
        ))
        await _hol_mod._override_stale_close_for_holdings(raw)

    # close_price must equal ref_close (set unconditionally, even when equal).
    assert raw.at[0, "close_price"] == ref_close
    assert raw.at[0, "previous_close"] == ref_close


def test_override_stale_close_source_no_epsilon_guard():
    """Static check: holdings.py must NOT execute 'continue' after the epsilon
    guard 'if abs(ref_close - current_close) <= 0.005: continue' in
    _override_stale_close_for_holdings — the guard must be removed so close_price
    is always set unconditionally."""
    import re
    from pathlib import Path
    src = (
        Path(__file__).parent.parent / "api" / "routes" / "holdings.py"
    ).read_text(encoding="utf-8")
    # The old pattern: a live if-guard followed by continue.
    # Comment mentions of the old guard (for doc purposes) are OK;
    # the actual executable guard is what we must not find.
    # Pattern: "if abs(ref_close - current_close)" (the actual branch line).
    assert "if abs(ref_close - current_close)" not in src, (
        "The live epsilon guard 'if abs(ref_close - current_close)...' must be removed "
        "from _override_stale_close_for_holdings (Fix 6). "
        "close_price should always be set to ref_close unconditionally."
    )


# ---------------------------------------------------------------------------
# Fix 7 — _overlay_snapshot_for_closed_exchanges patches day P&L for closed rows
# ---------------------------------------------------------------------------

def _make_position_row(**kwargs):
    """Build a minimal PositionRow-like struct for overlay tests."""
    from backend.api.schemas import PositionRow
    defaults = dict(
        account="ZG0001",
        tradingsymbol="RELIANCE",
        exchange="NFO",
        product="MIS",
        quantity=10,
        average_price=2000.0,
        close_price=2050.0,
        pnl=500.0,
        last_price=2100.0,
        day_change_val=500.0,
        day_change_percentage=2.44,
        previous_close=2050.0,
    )
    defaults.update(kwargs)
    return PositionRow(**defaults)


@pytest.mark.asyncio
async def test_overlay_snapshot_patches_day_change_for_closed_nfo_row():
    """NFO row (exchange closed) must get day_change_val, day_change_percentage,
    and close_price patched from the ref_close_map.  MCX row (open) must be untouched."""
    from backend.api.routes import positions as _pos_mod

    nfo_row = _make_position_row(
        tradingsymbol="NIFTY26AUG24500CE",
        exchange="NFO",
        quantity=50,
        last_price=150.0,
        day_change_val=0.0,       # stale broker value
        day_change_percentage=0.0,
        close_price=145.0,        # broker's drifted close
        previous_close=140.0,
    )
    mcx_row = _make_position_row(
        tradingsymbol="CRUDEOIL26AUGFUT",
        exchange="MCX",
        quantity=100,
        last_price=8500.0,
        day_change_val=1000.0,    # live, must not be touched
        day_change_percentage=1.2,
        close_price=8400.0,
        previous_close=8400.0,
    )

    ref_close_for_nfo = 142.0  # true prior-session settlement LTP

    def _exchange_closed_mock(exch: str) -> bool:
        return exch.upper() == "NFO"

    # snap_map: NFO row has a snapshot entry, MCX doesn't.
    snap_map = {("ZG0001", "NIFTY26AUG24500CE"): 150.0}

    # ref_close_map returned by _fetch_ref_close_map for closed pairs.
    ref_close_map = {("ZG0001", "NIFTY26AUG24500CE"): ref_close_for_nfo}

    with patch.object(_pos_mod, "is_exchange_closed_now",
                      side_effect=_exchange_closed_mock), \
         patch.object(_pos_mod, "latest_snapshot_ltp_map",
                      new=AsyncMock(return_value=snap_map)), \
         patch.object(_pos_mod, "_fetch_ref_close_map",
                      new=AsyncMock(return_value=ref_close_map)):
        result = await _pos_mod._overlay_snapshot_for_closed_exchanges(
            [nfo_row, mcx_row], kind="positions"
        )

    assert len(result) == 2
    nfo_out, mcx_out = result[0], result[1]

    # NFO row assertions — day P&L patched using ref_close.
    snap_ltp = 150.0
    expected_dcv = (snap_ltp - ref_close_for_nfo) * 50   # (150 - 142) * 50 = 400
    expected_dcp = expected_dcv / abs(ref_close_for_nfo * 50) * 100
    assert abs(nfo_out.day_change_val - expected_dcv) < 0.01, (
        f"NFO day_change_val should be {expected_dcv}, got {nfo_out.day_change_val}"
    )
    assert abs(nfo_out.day_change_percentage - expected_dcp) < 0.01, (
        f"NFO day_change_percentage should be {expected_dcp:.2f}, "
        f"got {nfo_out.day_change_percentage:.2f}"
    )
    assert nfo_out.close_price == ref_close_for_nfo, (
        f"NFO close_price should be {ref_close_for_nfo}, got {nfo_out.close_price}"
    )

    # MCX row assertions — nothing patched (exchange open).
    assert mcx_out.day_change_val == 1000.0, (
        "MCX day_change_val must not be changed (exchange is open)"
    )
    assert mcx_out.day_change_percentage == 1.2, (
        "MCX day_change_percentage must not be changed (exchange is open)"
    )
    assert mcx_out.close_price == 8400.0, (
        "MCX close_price must not be changed (exchange is open)"
    )


@pytest.mark.asyncio
async def test_overlay_snapshot_no_ref_close_leaves_day_change_unchanged():
    """When ref_close_map has no entry for a closed-exchange row, day_change_val
    must NOT be zeroed or corrupted — the broker value passes through."""
    from backend.api.routes import positions as _pos_mod

    row = _make_position_row(
        exchange="NFO",
        quantity=10,
        last_price=200.0,
        day_change_val=300.0,    # broker value to preserve
        close_price=170.0,
    )

    def _exchange_closed_mock(exch: str) -> bool:
        return True  # All closed

    with patch.object(_pos_mod, "is_exchange_closed_now",
                      side_effect=_exchange_closed_mock), \
         patch.object(_pos_mod, "latest_snapshot_ltp_map",
                      new=AsyncMock(return_value={})), \
         patch.object(_pos_mod, "_fetch_ref_close_map",
                      new=AsyncMock(return_value={})):  # no ref close found
        result = await _pos_mod._overlay_snapshot_for_closed_exchanges(
            [row], kind="positions"
        )

    assert len(result) == 1
    out = result[0]
    # No ref_close — day_change_val must not be modified.
    assert out.day_change_val == 300.0, (
        f"day_change_val should remain 300.0 when no ref_close found, got {out.day_change_val}"
    )


@pytest.mark.asyncio
async def test_overlay_snapshot_holdings_path_skips_ref_close_query():
    """For kind='holdings', _fetch_ref_close_map must not be called
    (holdings overlay does not patch day P&L via this path)."""
    from backend.api.routes import positions as _pos_mod
    from backend.api.schemas import HoldingRow

    # Build a minimal HoldingRow.
    row = HoldingRow(
        account="ZG0001",
        tradingsymbol="RELIANCE",
        exchange="NSE",
        quantity=10,
        opening_quantity=10,
        average_price=2000.0,
        close_price=2050.0,
        last_price=2100.0,
        inv_val=20000.0,
        cur_val=21000.0,
        pnl=1000.0,
        pnl_percentage=5.0,
        day_change_val=500.0,
        day_change_percentage=2.44,
    )

    fetch_ref_called = False

    async def _fail_if_called(*_a, **_kw):
        nonlocal fetch_ref_called
        fetch_ref_called = True
        return {}

    def _exchange_closed_mock(exch: str) -> bool:
        return True  # NSE closed

    with patch.object(_pos_mod, "is_exchange_closed_now",
                      side_effect=_exchange_closed_mock), \
         patch.object(_pos_mod, "latest_snapshot_ltp_map",
                      new=AsyncMock(return_value={})), \
         patch.object(_pos_mod, "_fetch_ref_close_map",
                      new=_fail_if_called):
        await _pos_mod._overlay_snapshot_for_closed_exchanges([row], kind="holdings")

    assert not fetch_ref_called, (
        "_fetch_ref_close_map must not be called for kind='holdings' — "
        "holdings day P&L is already patched by _override_stale_close_for_holdings"
    )
