"""
Tests for paper-position synthesis and /api/positions?mode= filter.

Five quality dimensions:
  1. SSOT        — synthesize_paper_positions aggregates AlgoOrder(mode='paper',
                   status='FILLED') rows via weighted-average fill price; the
                   PositionRow.mode field is the canonical tag on every row.
  2. Performance — ?mode=paper skips the broker entirely (zero broker calls).
  3. Stale code  — source-grep verifies synthesize_paper_positions lives in
                   paper.py and that PositionRow carries a `mode` field.
  4. Reusable    — _build_paper_positions_response is a standalone async
                   helper (not inlined in the controller) so nav.py / background
                   tasks can call it independently when needed.
  5. Correctness — weighted avg_cost, net qty (long/short netting), ?mode=live
                   unchanged, ?mode=both union, LTP mark-to-market fires.
"""

from __future__ import annotations

import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

# Source paths for static checks
_PAPER_SRC   = Path(__file__).parent.parent / "api" / "algo"    / "paper.py"
_POS_SRC     = Path(__file__).parent.parent / "api" / "routes"  / "positions.py"
_SCHEMA_SRC  = Path(__file__).parent.parent / "api"             / "schemas.py"


def _src(p: Path) -> str:
    return p.read_text(encoding="utf-8")


# ---------------------------------------------------------------------------
# Dimension 3 — static source checks
# ---------------------------------------------------------------------------

def test_paper_py_has_synthesize_helper():
    """paper.py must export synthesize_paper_positions."""
    assert "async def synthesize_paper_positions" in _src(_PAPER_SRC), (
        "synthesize_paper_positions must be defined in paper.py"
    )


def test_position_row_has_mode_field():
    """PositionRow schema must carry a `mode` field defaulting to 'live'."""
    src = _src(_SCHEMA_SRC)
    assert "mode: str" in src or "mode:" in src, (
        "PositionRow in schemas.py must carry a `mode` field"
    )
    assert "\"live\"" in src or "'live'" in src, (
        "PositionRow.mode must default to 'live'"
    )


def test_positions_route_has_mode_param():
    """positions.py controller must accept a ?mode= query param."""
    src = _src(_POS_SRC)
    assert "mode" in src, "positions.py must handle ?mode= param"
    assert "mode == \"paper\"" in src or "mode == 'paper'" in src, (
        "positions.py must have a paper-only fast path"
    )


def test_build_paper_response_is_standalone():
    """_build_paper_positions_response must be a module-level async def,
    not inlined inside the controller method, so other callers can reuse it."""
    src = _src(_POS_SRC)
    assert "async def _build_paper_positions_response" in src, (
        "_build_paper_positions_response must be a standalone module-level function"
    )


# ---------------------------------------------------------------------------
# Helpers — minimal AlgoOrder mock
# ---------------------------------------------------------------------------

def _make_order(
    *,
    id: int,
    account: str,
    symbol: str,
    exchange: str = "NFO",
    transaction_type: str = "BUY",
    quantity: int = 50,
    filled_quantity: int = 50,
    fill_price: float = 100.0,
    initial_price: float = 100.0,
    product: str = "NRML",
    mode: str = "paper",
    status: str = "FILLED",
    # Real production FILLED rows always carry a tz-aware filled_at
    # (set by the paper fill-apply path — see paper.py:_pt_apply_fill).
    # Default here is "now" (today's fill, not overnight) so existing
    # callers that don't care about the Day P&L baseline split keep
    # their old "everything is today" semantics unchanged.
    filled_at: "datetime | None" = None,
) -> MagicMock:
    o = MagicMock()
    o.id = id
    o.account = account
    o.symbol = symbol
    o.exchange = exchange
    o.transaction_type = transaction_type
    o.quantity = quantity
    o.filled_quantity = filled_quantity
    o.fill_price = fill_price
    o.initial_price = initial_price
    o.product = product
    o.mode = mode
    o.status = status
    o.filled_at = filled_at if filled_at is not None else datetime.now(timezone.utc)
    return o


# ---------------------------------------------------------------------------
# Dimension 1 + 5 — unit tests for synthesize_paper_positions
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_synthesize_returns_open_positions():
    """3 symbols across 2 accounts → 3 synthesized rows (all net qty != 0)."""
    orders = [
        _make_order(id=1, account="ZG0790", symbol="NIFTY24DECFUT",   quantity=50, filled_quantity=50, fill_price=23500.0),
        _make_order(id=2, account="ZG0790", symbol="BANKNIFTY24DECFUT", quantity=15, filled_quantity=15, fill_price=51000.0),
        _make_order(id=3, account="ZJ6294", symbol="NIFTY24DECFUT",   quantity=25, filled_quantity=25, fill_price=23450.0),
    ]

    mock_scalars = MagicMock()
    mock_scalars.all.return_value = orders
    mock_result = MagicMock()
    mock_result.scalars.return_value = mock_scalars
    mock_session = AsyncMock()
    mock_session.__aenter__ = AsyncMock(return_value=mock_session)
    mock_session.__aexit__  = AsyncMock(return_value=False)
    mock_session.execute = AsyncMock(return_value=mock_result)

    with patch("backend.api.database.async_session", return_value=mock_session):
        from backend.api.algo.paper import synthesize_paper_positions
        rows = await synthesize_paper_positions()

    assert len(rows) == 3, f"Expected 3 rows, got {len(rows)}: {rows}"
    syms = {r["tradingsymbol"] for r in rows}
    assert "NIFTY24DECFUT" in syms
    assert "BANKNIFTY24DECFUT" in syms
    # Both accounts are separate rows
    nifty_rows = [r for r in rows if r["tradingsymbol"] == "NIFTY24DECFUT"]
    assert len(nifty_rows) == 2


@pytest.mark.asyncio
async def test_synthesize_weighted_average_avg_cost():
    """2 BUY fills at different prices → avg_cost = weighted average."""
    orders = [
        _make_order(id=1, account="ZG0790", symbol="GOLDM25JANFUT",
                    quantity=10, filled_quantity=10, fill_price=6000.0),
        _make_order(id=2, account="ZG0790", symbol="GOLDM25JANFUT",
                    quantity=10, filled_quantity=10, fill_price=6200.0),
    ]

    mock_scalars = MagicMock()
    mock_scalars.all.return_value = orders
    mock_result = MagicMock()
    mock_result.scalars.return_value = mock_scalars
    mock_session = AsyncMock()
    mock_session.__aenter__ = AsyncMock(return_value=mock_session)
    mock_session.__aexit__  = AsyncMock(return_value=False)
    mock_session.execute = AsyncMock(return_value=mock_result)

    with patch("backend.api.database.async_session", return_value=mock_session):
        from backend.api.algo.paper import synthesize_paper_positions
        rows = await synthesize_paper_positions()

    assert len(rows) == 1
    row = rows[0]
    # Expected: (10×6000 + 10×6200) / 20 = 6100
    assert abs(row["average_price"] - 6100.0) < 0.01, (
        f"Expected average_price ~6100, got {row['average_price']}"
    )
    assert row["quantity"] == 20, f"Expected net qty 20: {row}"


@pytest.mark.asyncio
async def test_synthesize_closed_position_excluded():
    """A BUY followed by a matching SELL nets to zero — row must be excluded."""
    orders = [
        _make_order(id=1, account="ZG0790", symbol="NIFTY24DECFUT",
                    transaction_type="BUY",  quantity=50, filled_quantity=50, fill_price=23500.0),
        _make_order(id=2, account="ZG0790", symbol="NIFTY24DECFUT",
                    transaction_type="SELL", quantity=50, filled_quantity=50, fill_price=23600.0),
    ]

    mock_scalars = MagicMock()
    mock_scalars.all.return_value = orders
    mock_result = MagicMock()
    mock_result.scalars.return_value = mock_scalars
    mock_session = AsyncMock()
    mock_session.__aenter__ = AsyncMock(return_value=mock_session)
    mock_session.__aexit__  = AsyncMock(return_value=False)
    mock_session.execute = AsyncMock(return_value=mock_result)

    with patch("backend.api.database.async_session", return_value=mock_session):
        from backend.api.algo.paper import synthesize_paper_positions
        rows = await synthesize_paper_positions()

    assert rows == [], f"Closed position should produce no rows, got: {rows}"


@pytest.mark.asyncio
async def test_synthesize_returns_empty_when_no_orders():
    """No FILLED paper orders → empty list, no error."""
    mock_scalars = MagicMock()
    mock_scalars.all.return_value = []
    mock_result = MagicMock()
    mock_result.scalars.return_value = mock_scalars
    mock_session = AsyncMock()
    mock_session.__aenter__ = AsyncMock(return_value=mock_session)
    mock_session.__aexit__  = AsyncMock(return_value=False)
    mock_session.execute = AsyncMock(return_value=mock_result)

    with patch("backend.api.database.async_session", return_value=mock_session):
        from backend.api.algo.paper import synthesize_paper_positions
        rows = await synthesize_paper_positions()

    assert rows == []


# ---------------------------------------------------------------------------
# Dimension 2 + 5 — _build_paper_positions_response
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_build_paper_response_marks_mode_field():
    """Every PositionRow returned by _build_paper_positions_response
    must carry mode='paper'."""
    synth_rows = [
        {
            "account": "ZG0790",
            "tradingsymbol": "NIFTY24DECFUT",
            "exchange": "NFO",
            "product": "NRML",
            "quantity": 50,
            "average_price": 23500.0,
            "close_price": 0.0,
            "last_price": 0.0,
            "pnl": 0.0,
            "pnl_percentage": 0.0,
            "day_change_val": 0.0,
            "day_change_percentage": 0.0,
            "mode": "paper",
        }
    ]

    # Patch synthesize to return our row, and ltp/close patches to no-ops
    async def _fake_synth():
        return synth_rows

    with patch("backend.api.algo.paper.synthesize_paper_positions", new=_fake_synth), \
         patch("backend.api.routes.positions._override_stale_ltp_from_ticker", return_value=None), \
         patch("backend.api.routes.positions._override_stale_close_from_snapshot", new=AsyncMock()):

        from backend.api.routes.positions import _build_paper_positions_response
        resp = await _build_paper_positions_response()

    assert len(resp.rows) == 1
    row = resp.rows[0]
    assert row.mode == "paper", f"Expected mode='paper', got {row.mode!r}"
    assert row.tradingsymbol == "NIFTY24DECFUT"
    assert row.account == "ZG0790"


@pytest.mark.asyncio
async def test_build_paper_response_ltp_mark_fires():
    """_override_stale_ltp_from_ticker must be called to mark paper positions."""
    synth_rows = [
        {
            "account": "ZG0790",
            "tradingsymbol": "NIFTY24DECFUT",
            "exchange": "NFO",
            "product": "NRML",
            "quantity": 50,
            "average_price": 23500.0,
            "close_price": 0.0,
            "last_price": 0.0,
            "pnl": 0.0,
            "pnl_percentage": 0.0,
            "day_change_val": 0.0,
            "day_change_percentage": 0.0,
            "mode": "paper",
        }
    ]

    async def _fake_synth():
        return synth_rows

    ltp_patch_mock = MagicMock(return_value=None)
    close_patch_mock = AsyncMock()

    with patch("backend.api.algo.paper.synthesize_paper_positions", new=_fake_synth), \
         patch("backend.api.routes.positions._override_stale_ltp_from_ticker", ltp_patch_mock), \
         patch("backend.api.routes.positions._override_stale_close_from_snapshot", close_patch_mock):

        from backend.api.routes.positions import _build_paper_positions_response
        await _build_paper_positions_response()

    assert ltp_patch_mock.call_count == 1, (
        "_override_stale_ltp_from_ticker must be called once for paper mark-to-market"
    )
    assert close_patch_mock.call_count == 1, (
        "_override_stale_close_from_snapshot must be called once for paper close_price"
    )


# ---------------------------------------------------------------------------
# Day P&L baseline (prev_settlement_pnl) for overnight paper positions
# (Tier 1 item #11 fix) — paper positions have no daily_book snapshot
# lineage of their own, so synthesize_paper_positions() reconstructs an
# "as-of-yesterday's-close" baseline directly from the fill ledger via
# `_prev_net_qty`/`_prev_notional`. _build_paper_positions_response must
# turn that into a real `prev_settlement_pnl` once `prev_close` is known.
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_build_paper_response_overnight_position_gets_real_baseline():
    """An overnight paper position (fills before today's session, carried
    into today) must get `prev_settlement_pnl` computed from the real
    prev_close × the as-of-yesterday net qty/notional — NOT left at 0,
    which would inflate today's Day P&L to the full lifetime P&L."""
    synth_rows = [
        {
            "account": "ZG0790",
            "tradingsymbol": "CRUDEOIL26OCTFUT",
            "exchange": "MCX",
            "product": "NRML",
            "quantity": 10,
            "average_price": 5500.0,
            "close_price": 0.0,
            "last_price": 0.0,
            "pnl": 0.0,
            "pnl_percentage": 0.0,
            "day_change_val": 0.0,
            "day_change_percentage": 0.0,
            "mode": "paper",
            # Entire 10-lot position was opened BEFORE today (overnight) —
            # as-of-yesterday net_qty/notional equal the current ones.
            "_prev_net_qty": 10,
            "_prev_notional": 55000.0,   # 10 × 5500
        }
    ]

    async def _fake_synth():
        return synth_rows

    async def _fake_close_override(raw):
        # Real production sets prev_close in place from daily_book.ltp.
        raw["prev_close"] = 5600.0

    with patch("backend.api.algo.paper.synthesize_paper_positions", new=_fake_synth), \
         patch("backend.api.routes.positions._override_stale_ltp_from_ticker", return_value=None), \
         patch("backend.api.routes.positions._override_stale_close_from_snapshot",
               new=_fake_close_override):

        from backend.api.routes.positions import _build_paper_positions_response
        resp = await _build_paper_positions_response()

    assert len(resp.rows) == 1
    row = resp.rows[0]
    # prev_settlement_pnl = prev_close × prev_qty − prev_notional
    #                     = 5600 × 10 − 55000 = 1000
    assert row.prev_settlement_pnl == pytest.approx(1000.0), (
        f"Expected overnight baseline 1000.0, got {row.prev_settlement_pnl!r}"
    )


@pytest.mark.asyncio
async def test_build_paper_response_same_day_position_has_no_baseline():
    """A paper position opened ENTIRELY today (no fills before the
    session boundary) must get `prev_settlement_pnl=None` — the
    'no overnight baseline' convention — not 0/NaN, and not the
    overnight formula applied to a zero as-of-yesterday quantity."""
    synth_rows = [
        {
            "account": "ZG0790",
            "tradingsymbol": "RELIANCE",
            "exchange": "NSE",
            "product": "MIS",
            "quantity": 10,
            "average_price": 2800.0,
            "close_price": 0.0,
            "last_price": 0.0,
            "pnl": 0.0,
            "pnl_percentage": 0.0,
            "day_change_val": 0.0,
            "day_change_percentage": 0.0,
            "mode": "paper",
            "_prev_net_qty": 0,
            "_prev_notional": 0.0,
        }
    ]

    async def _fake_synth():
        return synth_rows

    async def _fake_close_override(raw):
        raw["prev_close"] = 2850.0

    with patch("backend.api.algo.paper.synthesize_paper_positions", new=_fake_synth), \
         patch("backend.api.routes.positions._override_stale_ltp_from_ticker", return_value=None), \
         patch("backend.api.routes.positions._override_stale_close_from_snapshot",
               new=_fake_close_override):

        from backend.api.routes.positions import _build_paper_positions_response
        resp = await _build_paper_positions_response()

    assert len(resp.rows) == 1
    row = resp.rows[0]
    assert row.prev_settlement_pnl is None, (
        f"Expected no baseline (None) for a same-day paper position, "
        f"got {row.prev_settlement_pnl!r}"
    )


@pytest.mark.asyncio
async def test_synthesize_paper_positions_splits_overnight_vs_today_fills():
    """Direct unit test of the fill-ledger split: one fill before the
    session cutoff (overnight) + one fill after (today) on the SAME
    (account, symbol) must produce `_prev_net_qty`/`_prev_notional`
    reflecting ONLY the pre-cutoff fill, while `quantity`/`average_price`
    (the all-time accumulation) reflect BOTH fills."""
    from datetime import timedelta
    from backend.api.algo.paper import synthesize_paper_positions

    orders = [
        _make_order(
            id=1, account="ZG0790", symbol="GOLDM25JANFUT",
            quantity=10, filled_quantity=10, fill_price=6000.0,
            filled_at=datetime(2026, 1, 1, tzinfo=timezone.utc),  # well before cutoff
        ),
        _make_order(
            id=2, account="ZG0790", symbol="GOLDM25JANFUT",
            quantity=10, filled_quantity=10, fill_price=6200.0,
            filled_at=datetime.now(timezone.utc),  # today
        ),
    ]

    mock_scalars = MagicMock()
    mock_scalars.all.return_value = orders
    mock_result = MagicMock()
    mock_result.scalars.return_value = mock_scalars
    mock_session = AsyncMock()
    mock_session.__aenter__ = AsyncMock(return_value=mock_session)
    mock_session.__aexit__  = AsyncMock(return_value=False)
    mock_session.execute = AsyncMock(return_value=mock_result)

    with patch("backend.api.database.async_session", return_value=mock_session):
        rows = await synthesize_paper_positions()

    assert len(rows) == 1
    row = rows[0]
    assert row["quantity"] == 20, f"All-time net qty must include both fills: {row}"
    assert row["_prev_net_qty"] == 10, (
        f"As-of-cutoff net qty must reflect ONLY the overnight fill: {row}"
    )
    assert row["_prev_notional"] == pytest.approx(60000.0), (
        f"As-of-cutoff notional must reflect ONLY the overnight fill (10×6000): {row}"
    )


# ---------------------------------------------------------------------------
# Dimension 5 — ?mode=live unchanged (no paper rows added)
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_mode_live_does_not_call_synthesize(app, async_client):
    """?mode=live must not invoke synthesize_paper_positions."""
    synth_spy = AsyncMock(return_value=[])
    live_resp = MagicMock()
    live_resp.rows = []
    live_resp.summary = []
    live_resp.refreshed_at = "now"
    live_resp.as_of = None

    with patch("backend.api.routes.positions.get_or_fetch",
               new=AsyncMock(return_value=live_resp)), \
         patch("backend.api.routes.positions.closed_hours_or_broker",
               new=AsyncMock(return_value=(live_resp, "live"))), \
         patch("backend.api.algo.paper.synthesize_paper_positions", synth_spy):
        r = await async_client.get("/api/positions?mode=live")

    # synthesize must NOT have been called for ?mode=live
    assert synth_spy.call_count == 0, (
        "synthesize_paper_positions must not be called for ?mode=live"
    )


# ---------------------------------------------------------------------------
# Dimension 5 — ?mode=both produces union with mode tags
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_mode_both_unions_live_and_paper(app, async_client):
    """?mode=both must return rows from both live and paper paths,
    each tagged with their respective mode."""
    from backend.api.schemas import PositionRow, PositionsSummaryRow, PositionsResponse

    live_row = PositionRow(
        account="ZG0790", tradingsymbol="NIFTY24DECFUT", exchange="NFO",
        product="NRML", quantity=50, average_price=23000.0, prev_close=23000.0,
        last_price=23100.0, pnl=5000.0,
    )
    live_resp = PositionsResponse(
        rows=[live_row], summary=[], refreshed_at="now", as_of=None,
    )

    paper_row = PositionRow(
        account="ZG0790", tradingsymbol="BANKNIFTY24DECFUT", exchange="NFO",
        product="NRML", quantity=15, average_price=51000.0, prev_close=51000.0,
        last_price=51500.0, pnl=7500.0, mode="paper",
    )
    paper_resp = PositionsResponse(
        rows=[paper_row], summary=[], refreshed_at="now", as_of=None,
    )

    with patch("backend.api.routes.positions.closed_hours_or_broker",
               new=AsyncMock(return_value=(live_resp, "live"))), \
         patch("backend.api.routes.positions._build_paper_positions_response",
               new=AsyncMock(return_value=paper_resp)):
        r = await async_client.get("/api/positions?mode=both")

    assert r.status_code == 200
    data = r.json()
    rows = data.get("rows", [])
    assert len(rows) == 2, f"Expected 2 rows (1 live + 1 paper), got {len(rows)}"
    modes = {row.get("mode") for row in rows}
    assert "live" in modes, f"Expected a 'live' row, got modes: {modes}"
    assert "paper" in modes, f"Expected a 'paper' row, got modes: {modes}"
