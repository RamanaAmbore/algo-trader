"""
Test for closed-hours snapshot enrichment fix in positions.py.

Context: After the fix (line 1392), when market is closed and the positions route
returns a daily_book snapshot, it calls _enrich_position_greeks(resp.rows) to stamp
underlying_ltp on option rows. Previously, option rows were returned with
underlying_ltp=None, causing NavStrip Exp P&L calculations to fail.

Five quality dimensions:
  1. SSOT       — Test exercises the real closed-hours path and enrichment logic
  2. Correctness— Snapshot option row receives underlying_ltp from broker spot price
  3. Performance— Single batched broker.quote() call via _batch_fetch_spots
  4. Reusable   — Follows existing test patterns from test_closed_hours_snapshot_routes.py
  5. UX         — NavStrip Exp P&L will be correct after this fix
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import pytest


@pytest.mark.asyncio
async def test_positions_snapshot_closed_hours_enriches_greeks():
    """Market closed + snapshot path MUST call _enrich_position_greeks to stamp
    underlying_ltp on option rows before returning.

    Scenario:
      - Market is closed (snapshot_gate returns 'snapshot' source)
      - DB snapshot contains an option row with underlying_ltp=None
      - _enrich_position_greeks must be called to fetch spot and fill underlying_ltp
      - Returned snapshot should have underlying_ltp = spot price from broker

    This test verifies the P0 fix: NavStrip Exp P&L was using None or zero for
    option Greeks before the enrichment path was added.
    """
    from backend.api.schemas import PositionsResponse, PositionRow, PositionsSummaryRow

    # ─────────────────────────────────────────────────────────────────────
    # Step 1: Construct a fake snapshot with an option row (underlying_ltp=None)
    # ─────────────────────────────────────────────────────────────────────
    fake_snapshot = PositionsResponse(
        rows=[
            # Equity position (not an option) — should pass through unchanged
            PositionRow(
                account="ZG0790",
                tradingsymbol="RELIANCE",
                exchange="NSE",
                product="MIS",
                quantity=10,
                average_price=2800.0,
                prev_close=2850.0,
                pnl=500.0,
                last_price=2850.0,
                underlying_ltp=None,  # not an option; no underlying
            ),
            # Option row (MCX CRUDEOIL call) — underlying_ltp MUST be enriched
            PositionRow(
                account="ZG0790",
                tradingsymbol="CRUDEOIL25OCT9400CE",
                exchange="MCX",
                product="NRML",
                quantity=10,
                average_price=150.0,
                prev_close=150.0,
                pnl=0.0,
                last_price=150.0,  # option premium (non-zero so enrichment runs)
                underlying_ltp=None,  # BUG: this must be filled by enrichment
                # Other fields for completeness
                day_change_val=0.0,
                pnl_percentage=0.0,
                day_change_percentage=0.0,
            ),
        ],
        summary=[
            PositionsSummaryRow(account="ZG0790", pnl=500.0),
            PositionsSummaryRow(account="TOTAL", pnl=500.0),
        ],
        refreshed_at="Sun 29 Sep 23:30 IST",
        as_of="2026-09-28T18:00:00+00:00",  # snapshot timestamp (market is closed)
    )

    # ─────────────────────────────────────────────────────────────────────
    # Step 2: Mock the broker spot price fetch (_batch_fetch_spots)
    # ─────────────────────────────────────────────────────────────────────
    # When _enrich_position_greeks calls _batch_fetch_spots for the MCX:CRUDEOIL
    # underlying, it should return the spot price.
    # Note: CRUDEOIL25OCT9400CE parses to expiry=2025-10-31, so the futures key is MCX:CRUDEOIL25OCTFUT
    def mock_batch_fetch_spots(underlying_keys: set[str]) -> dict[str, float]:
        """Simulate broker spot quote for CRUDEOIL underlying."""
        result = {}
        if "MCX:CRUDEOIL25OCTFUT" in underlying_keys:
            result["MCX:CRUDEOIL25OCTFUT"] = 5150.0
        return result

    # ─────────────────────────────────────────────────────────────────────
    # Step 3: Mock market_closed + snapshot path
    # ─────────────────────────────────────────────────────────────────────
    with patch(
        "backend.api.helpers.snapshot_gate._any_segment_open",
        return_value=False,  # market is closed
    ), patch(
        "backend.api.routes.positions._positions_snapshot",
        new=AsyncMock(return_value=fake_snapshot),
    ), patch(
        "backend.api.routes.positions._batch_fetch_spots",
        side_effect=mock_batch_fetch_spots,
    ) as mock_batch_fetch:
        # Also patch auth to allow admin bypass
        with patch(
            "backend.api.routes.positions_helpers.is_admin_request",
            return_value=True,
        ), patch(
            "backend.api.routes.positions_helpers.resolve_role_from_connection",
            return_value="admin",
        ), patch(
            "backend.api.routes.positions_helpers.normalise_role",
            return_value="admin",
        ):
            # ─────────────────────────────────────────────────────────────
            # Step 4: Call the route handler
            # ─────────────────────────────────────────────────────────────
            from backend.api.routes.positions import PositionsController

            handler_fn = PositionsController.get_positions.fn

            mock_request = MagicMock()
            mock_request.headers = {}
            mock_request.user = None

            resp = await handler_fn(None, mock_request, fresh=False)

    # ─────────────────────────────────────────────────────────────────────
    # Step 5: Assertions
    # ─────────────────────────────────────────────────────────────────────

    # Snapshot was returned (as_of is set)
    assert resp.as_of is not None, (
        "Snapshot path must return response with as_of set (market closed)"
    )
    assert resp.as_of == "2026-09-28T18:00:00+00:00"

    # Both rows present
    assert len(resp.rows) == 2, f"Expected 2 rows in snapshot, got {len(resp.rows)}"

    # Equity row unaffected
    eq_row = resp.rows[0]
    assert eq_row.tradingsymbol == "RELIANCE"
    assert eq_row.underlying_ltp is None, (
        "Equity rows have no underlying; underlying_ltp should remain None"
    )

    # Option row enriched with underlying_ltp
    opt_row = resp.rows[1]
    assert opt_row.tradingsymbol == "CRUDEOIL25OCT9400CE"
    assert opt_row.underlying_ltp == 5150.0, (
        f"Option row must have underlying_ltp stamped from broker spot. "
        f"Expected 5150.0, got {opt_row.underlying_ltp}"
    )

    # _batch_fetch_spots must have been called (proof enrichment ran)
    assert mock_batch_fetch.call_count == 1, (
        f"_batch_fetch_spots should be called once to fetch underlying spots; "
        f"got {mock_batch_fetch.call_count} calls"
    )


@pytest.mark.asyncio
async def test_positions_snapshot_enrichment_no_call_on_live_path():
    """When market is OPEN (live path), enrichment is already done earlier
    in the fetch flow (line 762), so the snapshot return path should never execute.

    This verifies that the enrichment call at line 1392 is only on the closed-hours
    snapshot path, not on the live path (to avoid double-enrichment or unnecessary
    broker calls during live market).
    """
    from backend.api.schemas import PositionsResponse

    live_resp = PositionsResponse(
        rows=[],
        summary=[],
        refreshed_at="live",
    )

    mock_enrich = MagicMock()

    with patch(
        "backend.api.helpers.snapshot_gate._any_segment_open",
        return_value=True,  # market is OPEN
    ), patch(
        "backend.api.routes.positions.get_or_fetch",
        new=AsyncMock(return_value=live_resp),
    ), patch(
        "backend.api.routes.positions._enrich_position_greeks",
        mock_enrich,
    ):
        with patch(
            "backend.api.routes.positions_helpers.is_admin_request",
            return_value=True,
        ), patch(
            "backend.api.routes.positions_helpers.resolve_role_from_connection",
            return_value="admin",
        ), patch(
            "backend.api.routes.positions_helpers.normalise_role",
            return_value="admin",
        ):
            from backend.api.routes.positions import PositionsController

            handler_fn = PositionsController.get_positions.fn

            mock_request = MagicMock()
            mock_request.headers = {}
            mock_request.user = None

            resp = await handler_fn(None, mock_request, fresh=False)

    # Live path returns immediately without triggering enrichment on the snapshot path
    assert resp.refreshed_at == "live"
    # Enrichment should NOT be called (already done earlier or not needed on live)
    assert mock_enrich.call_count == 0, (
        "Enrichment must not be called on live path; "
        "snapshot enrichment (line 1392) only runs on closed-hours snapshot return"
    )


@pytest.mark.asyncio
async def test_positions_snapshot_enrichment_zero_spot_price_skip():
    """When broker.quote() fails for an underlying, _batch_fetch_spots returns 0.0.
    _enrich_position_greeks must gracefully skip Greeks computation (leave at 0)
    without crashing.

    This is a defensive test: the enrichment must be robust to broker failures
    during closed hours (the snapshot is still valid, just without the underlying spot).
    """
    from backend.api.schemas import PositionsResponse, PositionRow, PositionsSummaryRow

    fake_snapshot = PositionsResponse(
        rows=[
            PositionRow(
                account="ZG0790",
                tradingsymbol="NIFTY25OCTCE",  # NIFTY call
                exchange="NFO",
                product="NRML",
                quantity=1,
                average_price=500.0,
                prev_close=500.0,
                pnl=0.0,
                last_price=500.0,  # non-zero option premium
                underlying_ltp=None,
            ),
        ],
        summary=[PositionsSummaryRow(account="ZG0790", pnl=0.0)],
        refreshed_at="Mon 29 Sep 08:00 IST",
        as_of="2026-09-28T02:30:00+00:00",
    )

    # Broker failure: return empty dict (no spot prices)
    def mock_batch_fetch_spots_fail(underlying_keys: set[str]) -> dict[str, float]:
        return {}

    with patch(
        "backend.api.helpers.snapshot_gate._any_segment_open",
        return_value=False,  # market closed
    ), patch(
        "backend.api.routes.positions._positions_snapshot",
        new=AsyncMock(return_value=fake_snapshot),
    ), patch(
        "backend.api.routes.positions._batch_fetch_spots",
        side_effect=mock_batch_fetch_spots_fail,
    ):
        with patch(
            "backend.api.routes.positions_helpers.is_admin_request",
            return_value=True,
        ), patch(
            "backend.api.routes.positions_helpers.resolve_role_from_connection",
            return_value="admin",
        ), patch(
            "backend.api.routes.positions_helpers.normalise_role",
            return_value="admin",
        ):
            from backend.api.routes.positions import PositionsController

            handler_fn = PositionsController.get_positions.fn

            mock_request = MagicMock()
            mock_request.headers = {}
            mock_request.user = None

            # Must NOT crash despite broker failure
            resp = await handler_fn(None, mock_request, fresh=False)

    assert resp.as_of is not None
    assert len(resp.rows) == 1
    # underlying_ltp remains None/0 when broker fails (graceful fallback)
    # The handler does not crash and returns the snapshot anyway.
    row = resp.rows[0]
    assert row.tradingsymbol == "NIFTY25OCTCE"
    # underlying_ltp may remain None or 0 depending on enrichment logic
    # (the key is that we don't crash)


@pytest.mark.asyncio
async def test_enrich_position_greeks_direct_call():
    """Direct unit test of _enrich_position_greeks: verify in-place modification
    of rows and underlying_ltp population.

    This isolates the enrichment logic from the route handler.
    """
    from backend.api.routes.positions import _enrich_position_greeks, _batch_fetch_spots
    from backend.api.schemas import PositionRow

    # Rows before enrichment
    rows = [
        PositionRow(
            account="ZG0790",
            tradingsymbol="CRUDEOIL25OCT9400CE",
            exchange="MCX",
            product="NRML",
            quantity=10,
            average_price=150.0,
            prev_close=150.0,
            pnl=0.0,
            last_price=150.0,
            underlying_ltp=None,  # will be filled by enrichment
        ),
    ]

    # Verify underlying_ltp is None before enrichment
    assert rows[0].underlying_ltp is None, "Pre-condition: underlying_ltp should be None"

    # Mock _batch_fetch_spots to return spot price
    def mock_batch_fetch(underlying_keys: set[str]) -> dict[str, float]:
        if "MCX:CRUDEOIL25OCTFUT" in underlying_keys:
            return {"MCX:CRUDEOIL25OCTFUT": 5150.0}
        return {}

    with patch(
        "backend.api.routes.positions._batch_fetch_spots",
        side_effect=mock_batch_fetch,
    ):
        # Call enrichment (synchronous, runs in a thread in the route)
        _enrich_position_greeks(rows)

    # After enrichment, underlying_ltp should be populated
    assert rows[0].underlying_ltp == 5150.0, (
        f"Enrichment must populate underlying_ltp from spot price. "
        f"Expected 5150.0, got {rows[0].underlying_ltp}"
    )


@pytest.mark.asyncio
async def test_enrich_position_greeks_empty_rows():
    """_enrich_position_greeks must handle empty rows gracefully."""
    from backend.api.routes.positions import _enrich_position_greeks

    rows = []

    # Should not crash on empty rows
    _enrich_position_greeks(rows)

    assert rows == [], "Empty rows should remain empty"


@pytest.mark.asyncio
async def test_enrich_position_greeks_non_option_rows_unchanged():
    """_enrich_position_greeks must skip non-option rows (equities, futures)
    and leave their underlying_ltp untouched."""
    from backend.api.routes.positions import _enrich_position_greeks
    from backend.api.schemas import PositionRow

    rows = [
        PositionRow(
            account="ZG0790",
            tradingsymbol="RELIANCE",
            exchange="NSE",
            product="MIS",
            quantity=10,
            average_price=2800.0,
            prev_close=2850.0,
            pnl=500.0,
            last_price=2850.0,
            underlying_ltp=None,
        ),
        PositionRow(
            account="ZG0790",
            tradingsymbol="NIFTY25JUNFUT",
            exchange="NFO",
            product="NRML",
            quantity=50,
            average_price=23000.0,
            prev_close=23100.0,
            pnl=5000.0,
            last_price=23100.0,
            underlying_ltp=None,
        ),
    ]

    with patch(
        "backend.api.routes.positions._batch_fetch_spots",
        return_value={},  # no spots fetched
    ) as mock_fetch:
        _enrich_position_greeks(rows)

    # Both rows should have underlying_ltp untouched (None)
    assert rows[0].underlying_ltp is None, "Equity should not get underlying_ltp"
    assert rows[1].underlying_ltp is None, "Futures should not get underlying_ltp"

    # Broker should not have been called (no options to enrich)
    assert mock_fetch.call_count == 0, (
        "_batch_fetch_spots should not be called for non-option rows"
    )
