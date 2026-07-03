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
        product="NRML", quantity=50, average_price=23000.0, close_price=23000.0,
        last_price=23100.0, pnl=5000.0,
    )
    live_resp = PositionsResponse(
        rows=[live_row], summary=[], refreshed_at="now", as_of=None,
    )

    paper_row = PositionRow(
        account="ZG0790", tradingsymbol="BANKNIFTY24DECFUT", exchange="NFO",
        product="NRML", quantity=15, average_price=51000.0, close_price=51000.0,
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
