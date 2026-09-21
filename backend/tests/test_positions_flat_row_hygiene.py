"""
Test flat-row hygiene in positions.py

Verifies that _apply_flat_row_hygiene correctly zeros day_change_val
and day_change_percentage for rows with quantity=0 (closed intraday positions).
Also tests _is_broker_outage SSOT migration to positions_helpers.py.
"""

import pytest
import pandas as pd


class TestApplyFlatRowHygiene:
    """Test _apply_flat_row_hygiene zeros day_change for closed intraday positions."""

    def test_apply_flat_row_hygiene_zeros_day_change_for_qty_zero_break_even(self):
        """Break-even intraday round-trip (pnl=0): day_change/dcv/pct must all be zeroed.

        When pnl is zero there is no realised gain/loss to preserve.
        Phantom dcv left by stale broker data must be cleared.
        """
        from backend.api.routes.positions import _apply_flat_row_hygiene

        raw = pd.DataFrame([
            {
                "account": "ZG0790",
                "tradingsymbol": "NIFTY26AUGFUT",
                "exchange": "NFO",
                "product": "NRML",
                "quantity": 0,                # CLOSED INTRADAY
                "average_price": 23000.0,
                "close_price": 23000.0,
                "last_price": 23200.0,
                "pnl": 0.0,                   # break-even — no realised P&L
                "pnl_percentage": 0.0,
                "day_change": 200.0,          # stale broker value — should be zeroed
                "day_change_val": 10000.0,    # phantom dcv — should be zeroed (pnl=0)
                "day_change_percentage": 2.2, # undefined — should be zeroed
                "overnight_quantity": 0,
                "last_price_stale": False,
            }
        ])

        _apply_flat_row_hygiene(raw)

        assert raw.at[0, "day_change"] == pytest.approx(0.0), (
            f"day_change={raw.at[0, 'day_change']} must be 0.0 for break-even qty=0 row"
        )
        assert raw.at[0, "day_change_val"] == pytest.approx(0.0), (
            f"day_change_val={raw.at[0, 'day_change_val']} must be 0.0 for break-even row "
            "(phantom NavStrip P&L prevention)"
        )
        assert raw.at[0, "day_change_percentage"] == pytest.approx(0.0), (
            f"day_change_percentage={raw.at[0, 'day_change_percentage']} must be 0.0 for break-even row"
        )
        # Other columns should remain unchanged
        assert raw.at[0, "pnl"] == pytest.approx(0.0), "pnl should not be modified"
        assert raw.at[0, "last_price"] == pytest.approx(23200.0), "last_price should not be zeroed"

    def test_apply_flat_row_hygiene_leaves_open_rows_unchanged(self):
        """qty != 0 rows should not be modified by _apply_flat_row_hygiene."""
        from backend.api.routes.positions import _apply_flat_row_hygiene

        raw = pd.DataFrame([
            {
                "account": "ZG0790",
                "tradingsymbol": "NIFTY26AUGFUT",
                "exchange": "NFO",
                "quantity": 50,                # OPEN
                "average_price": 23000.0,
                "close_price": 23000.0,
                "last_price": 23200.0,
                "day_change": 200.0,
                "day_change_percentage": 2.2,
                "overnight_quantity": 50,
            }
        ])

        _apply_flat_row_hygiene(raw)

        # Open rows should not be modified
        assert raw.at[0, "day_change"] == pytest.approx(200.0), (
            "day_change should remain unchanged for qty != 0"
        )
        assert raw.at[0, "day_change_percentage"] == pytest.approx(2.2), (
            "day_change_percentage should remain unchanged for qty != 0"
        )

    def test_apply_flat_row_hygiene_mixed_open_and_closed(self):
        """Mixed open and closed rows should only zero the closed ones."""
        from backend.api.routes.positions import _apply_flat_row_hygiene

        raw = pd.DataFrame([
            {
                "account": "ZG0790",
                "tradingsymbol": "NIFTY26AUGFUT",
                "quantity": 50,                # OPEN
                "day_change": 200.0,
                "day_change_percentage": 2.2,
            },
            {
                "account": "ZG0790",
                "tradingsymbol": "CRUDEOIL26AUGFUT",
                "quantity": 0,                 # CLOSED
                "day_change": 1000.0,
                "day_change_percentage": 5.0,
            },
        ])

        _apply_flat_row_hygiene(raw)

        # First row (open) unchanged
        assert raw.at[0, "day_change"] == pytest.approx(200.0), "Open row should not be modified"
        # Second row (closed) zeroed
        assert raw.at[1, "day_change"] == pytest.approx(0.0), "Closed row should be zeroed"
        assert raw.at[1, "day_change_percentage"] == pytest.approx(0.0), (
            "Closed row percentage should be zeroed"
        )

    def test_apply_flat_row_hygiene_handles_float_string_quantity(self):
        """Quantity as string should be coerced to numeric before comparison."""
        from backend.api.routes.positions import _apply_flat_row_hygiene

        raw = pd.DataFrame([
            {
                "quantity": "0",       # STRING ZERO
                "day_change": 500.0,
                "day_change_percentage": 1.5,
            }
        ])

        _apply_flat_row_hygiene(raw)

        assert raw.at[0, "day_change"] == pytest.approx(0.0), (
            "String '0' quantity should be coerced and zeroed"
        )

    def test_apply_flat_row_hygiene_zeros_day_change_val_for_qty_zero_break_even(self):
        """Break-even round-trip (pnl=0): day_change_val must be zeroed.

        When pnl is zero there is no realised gain/loss to preserve — zero
        day_change_val so the NavStrip does not show phantom P&L.
        """
        from backend.api.routes.positions import _apply_flat_row_hygiene

        raw = pd.DataFrame([
            {
                "quantity": 0,
                "overnight_quantity": 0,
                "day_change": 200.0,
                "day_change_val": 9_800.0,   # phantom P&L, no real gain
                "day_change_percentage": 1.5,
                "pnl": 0.0,                  # break-even — no realised P&L
            }
        ])
        _apply_flat_row_hygiene(raw)
        assert raw.at[0, "day_change_val"] == pytest.approx(0.0), (
            "day_change_val must be 0.0 for break-even intraday round-trips "
            "(pnl=0) to prevent phantom NavStrip P&L"
        )

    def test_apply_flat_row_hygiene_preserves_day_change_val_when_pnl_nonzero(self):
        """Case 3 (closed intraday with realised P&L): day_change_val must NOT be zeroed.

        apply_day_change_backstop sets dcv=pnl for this case so the NavStrip
        P slot shows the realised gain/loss.  _apply_flat_row_hygiene must not
        overwrite it when abs(pnl) >= 0.005.
        """
        from backend.api.routes.positions import _apply_flat_row_hygiene

        raw = pd.DataFrame([
            {
                "quantity": 0,
                "overnight_quantity": 0,
                "day_change": 0.0,
                "day_change_val": 500.0,   # backstop already set dcv = pnl
                "day_change_percentage": 0.0,
                "pnl": 500.0,
            }
        ])
        _apply_flat_row_hygiene(raw)
        assert raw.at[0, "day_change_val"] == pytest.approx(500.0), (
            "day_change_val must be preserved (500.0) for closed intraday row "
            "with pnl=500 — hygiene must not zero the backstop-restored dcv "
            "(Fix 4: Case 3 intraday exit shows 0 instead of realised P&L)"
        )

    def test_apply_flat_row_hygiene_day_change_val_open_row_preserved(self):
        """Open rows (qty != 0) must NOT have day_change_val zeroed."""
        from backend.api.routes.positions import _apply_flat_row_hygiene

        raw = pd.DataFrame([
            {
                "quantity": 50,
                "day_change": 200.0,
                "day_change_val": 9_800.0,
                "day_change_percentage": 1.5,
            }
        ])
        _apply_flat_row_hygiene(raw)
        assert raw.at[0, "day_change_val"] == pytest.approx(9_800.0), (
            "day_change_val must be preserved for open (qty != 0) rows"
        )

    def test_apply_flat_row_hygiene_no_op_empty_dataframe(self):
        """Empty DataFrame should not raise an error."""
        from backend.api.routes.positions import _apply_flat_row_hygiene

        raw = pd.DataFrame()
        # Should not raise
        _apply_flat_row_hygiene(raw)
        assert raw.empty

    def test_apply_flat_row_hygiene_no_op_missing_quantity_column(self):
        """Missing quantity column should not raise an error."""
        from backend.api.routes.positions import _apply_flat_row_hygiene

        raw = pd.DataFrame([
            {
                "day_change": 200.0,
                "day_change_percentage": 2.2,
            }
        ])

        # Should not raise
        _apply_flat_row_hygiene(raw)
        assert raw.at[0, "day_change"] == pytest.approx(200.0), (
            "Row should remain unchanged when quantity column missing"
        )

    def test_apply_flat_row_hygiene_no_op_missing_day_change_columns(self):
        """Missing day_change / day_change_percentage columns should not raise."""
        from backend.api.routes.positions import _apply_flat_row_hygiene

        raw = pd.DataFrame([
            {
                "quantity": 0,
                "pnl": 1000.0,
            }
        ])

        # Should not raise
        _apply_flat_row_hygiene(raw)
        assert raw.at[0, "pnl"] == pytest.approx(1000.0)

    def test_case3_negative_pnl_closed_intraday_preserved(self):
        """Negative pnl on closed intraday: qty=0, oq=0, pnl=-750.25 → day_change_val preserved."""
        from backend.api.routes.positions import _apply_flat_row_hygiene

        raw = pd.DataFrame([{
            'quantity': 0,
            'overnight_quantity': 0,
            'pnl': -750.25,
            'day_change_val': -750.25,
            'day_change': -3.5,
            'day_change_percentage': -1.8,
        }])
        _apply_flat_row_hygiene(raw)
        assert raw.at[0, 'day_change_val'] == pytest.approx(-750.25), (
            "Negative pnl on closed intraday must preserve day_change_val"
        )

    def test_case3_multiple_sameday_entries_exits_aggregate_pnl(self):
        """Multiple same-day entries/exits: qty=0, oq=0, pnl=1250.75 → day_change_val preserved."""
        from backend.api.routes.positions import _apply_flat_row_hygiene

        raw = pd.DataFrame([{
            'quantity': 0,
            'overnight_quantity': 0,
            'pnl': 1250.75,
            'day_change_val': 1250.75,
            'day_change': 5.2,
            'day_change_percentage': 2.1,
        }])
        _apply_flat_row_hygiene(raw)
        assert raw.at[0, 'day_change_val'] == pytest.approx(1250.75), (
            "Multiple same-day entries/exits with aggregate pnl must preserve day_change_val"
        )

    def test_near_zero_pnl_half_paisa_threshold_below(self):
        """pnl=0.004 (below 0.005 threshold) → day_change_val should be zeroed."""
        from backend.api.routes.positions import _apply_flat_row_hygiene

        raw = pd.DataFrame([{
            'quantity': 0,
            'overnight_quantity': 0,
            'pnl': 0.004,
            'day_change_val': 0.004,
            'day_change': 0.0001,
            'day_change_percentage': 0.0,
        }])
        _apply_flat_row_hygiene(raw)
        assert raw.at[0, 'day_change_val'] == pytest.approx(0.0), (
            "pnl=0.004 (below 0.005 threshold) should have day_change_val zeroed"
        )

    def test_near_zero_pnl_half_paisa_threshold_above(self):
        """pnl=0.006 (above 0.005 threshold) → day_change_val should be preserved."""
        from backend.api.routes.positions import _apply_flat_row_hygiene

        raw = pd.DataFrame([{
            'quantity': 0,
            'overnight_quantity': 0,
            'pnl': 0.006,
            'day_change_val': 0.006,
            'day_change': 0.0002,
            'day_change_percentage': 0.0,
        }])
        _apply_flat_row_hygiene(raw)
        assert raw.at[0, 'day_change_val'] == pytest.approx(0.006), (
            "pnl=0.006 (above 0.005 threshold) should have day_change_val preserved"
        )

    def test_case3_mixed_scenarios(self):
        """Mixed scenarios: Case 3 (pnl), overnight closed (oq>0), and open (qty>0) rows.

        Only the Case 3 row (qty=0, oq=0) is in the flat_mask and gets day_change zeroed.
        Overnight closed (oq>0) rows do NOT match flat_mask, so day_change is NOT modified.
        """
        from backend.api.routes.positions import _apply_flat_row_hygiene

        raw = pd.DataFrame([
            # Row 0: Case 3 — closed intraday with pnl — should preserve day_change_val, zero day_change
            {
                'quantity': 0,
                'overnight_quantity': 0,
                'pnl': 650.0,
                'day_change_val': 650.0,
                'day_change': 2.0,
                'day_change_percentage': 1.0,
            },
            # Row 1: overnight closed (oq>0) — NOT in flat_mask, should NOT be modified
            {
                'quantity': 0,
                'overnight_quantity': 2,
                'pnl': 1200.0,
                'day_change_val': 800.0,
                'day_change': 3.0,
                'day_change_percentage': 1.5,
            },
            # Row 2: open position (qty>0) — NOT in flat_mask, should be untouched
            {
                'quantity': 5,
                'overnight_quantity': 5,
                'pnl': 2500.0,
                'day_change_val': 2000.0,
                'day_change': 5.0,
                'day_change_percentage': 2.0,
            },
        ])
        _apply_flat_row_hygiene(raw)

        # Row 0: Case 3 — day_change_val preserved, day_change zeroed
        assert raw.at[0, 'day_change_val'] == pytest.approx(650.0)
        assert raw.at[0, 'day_change'] == pytest.approx(0.0)
        assert raw.at[0, 'day_change_percentage'] == pytest.approx(0.0)

        # Row 1: overnight closed — NOT in flat_mask, day_change NOT zeroed
        assert raw.at[1, 'day_change_val'] == pytest.approx(800.0)
        assert raw.at[1, 'day_change'] == pytest.approx(3.0), (
            "overnight closed (oq>0) should NOT have day_change zeroed"
        )
        assert raw.at[1, 'day_change_percentage'] == pytest.approx(1.5)

        # Row 2: open — NOT in flat_mask, untouched
        assert raw.at[2, 'day_change_val'] == pytest.approx(2000.0)
        assert raw.at[2, 'day_change'] == pytest.approx(5.0)
        assert raw.at[2, 'day_change_percentage'] == pytest.approx(2.0)


class TestIsBrokerOutageSSOT:
    """Test _is_broker_outage importable from positions_helpers (SSOT)."""

    def test_is_broker_outage_importable_from_positions_helpers(self):
        """_is_broker_outage must be importable from positions_helpers (single SSOT)."""
        from backend.api.routes.positions_helpers import _is_broker_outage

        assert _is_broker_outage(Exception("502 bad gateway"))
        assert _is_broker_outage(Exception("503 service unavailable"))
        assert _is_broker_outage(Exception("504 gateway timeout"))
        assert not _is_broker_outage(Exception("some other error"))

    def test_is_broker_outage_case_insensitive(self):
        """_is_broker_outage should be case-insensitive."""
        from backend.api.routes.positions_helpers import _is_broker_outage

        assert _is_broker_outage(Exception("BAD GATEWAY"))
        assert _is_broker_outage(Exception("SERVICE UNAVAILABLE"))

    def test_is_broker_outage_detects_all_codes(self):
        """_is_broker_outage detects all HTTP error codes and phrases."""
        from backend.api.routes.positions_helpers import _is_broker_outage

        assert _is_broker_outage(Exception("502"))
        assert _is_broker_outage(Exception("503"))
        assert _is_broker_outage(Exception("504"))

    def test_is_broker_outage_not_defined_in_positions(self):
        """positions.py must NOT define _is_broker_outage — import from positions_helpers."""
        import inspect
        import backend.api.routes.positions as _pos

        src = inspect.getsource(_pos)
        assert "def _is_broker_outage" not in src, (
            "positions.py must not define _is_broker_outage — import from positions_helpers"
        )

    def test_is_broker_outage_not_defined_in_holdings(self):
        """holdings.py must NOT define _is_broker_outage — import from positions_helpers."""
        import inspect
        import backend.api.routes.holdings as _h

        src = inspect.getsource(_h)
        assert "def _is_broker_outage" not in src, (
            "holdings.py must not define _is_broker_outage — import from positions_helpers"
        )

    def test_is_broker_outage_not_defined_in_funds(self):
        """funds.py must NOT define _is_broker_outage — import from positions_helpers."""
        import inspect
        import backend.api.routes.funds as _f

        src = inspect.getsource(_f)
        assert "def _is_broker_outage" not in src, (
            "funds.py must not define _is_broker_outage — import from positions_helpers"
        )

    def test_is_broker_outage_signature(self):
        """_is_broker_outage should accept an Exception and return bool."""
        from backend.api.routes.positions_helpers import _is_broker_outage
        import inspect

        sig = inspect.signature(_is_broker_outage)
        # Should have one parameter (err)
        params = list(sig.parameters.keys())
        assert len(params) == 1, f"Expected 1 parameter, got {len(params)}: {params}"
        # Return type should be bool
        result = _is_broker_outage(Exception("503"))
        assert isinstance(result, bool), f"Expected bool return, got {type(result)}"
