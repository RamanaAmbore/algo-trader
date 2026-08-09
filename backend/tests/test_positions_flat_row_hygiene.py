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

    def test_apply_flat_row_hygiene_zeros_day_change_for_qty_zero(self):
        """qty=0 rows must have day_change zeroed by _apply_flat_row_hygiene."""
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
                "pnl": 5000.0,
                "pnl_percentage": 2.0,
                "day_change": 200.0,         # non-zero — should be zeroed
                "day_change_val": 10000.0,   # non-zero — should be zeroed
                "day_change_percentage": 2.2,  # non-zero — should be zeroed
                "overnight_quantity": 0,
                "last_price_stale": False,
            }
        ])

        _apply_flat_row_hygiene(raw)

        assert raw.at[0, "day_change"] == pytest.approx(0.0), (
            f"day_change={raw.at[0, 'day_change']} must be 0.0 for qty=0 row"
        )
        assert raw.at[0, "day_change_val"] == pytest.approx(0.0), (
            f"day_change_val={raw.at[0, 'day_change_val']} must be 0.0 for qty=0 row "
            "(Change B1: phantom NavStrip P&L from closed intraday position)"
        )
        assert raw.at[0, "day_change_percentage"] == pytest.approx(0.0), (
            f"day_change_percentage={raw.at[0, 'day_change_percentage']} must be 0.0 for qty=0 row"
        )
        # Other columns should remain unchanged
        assert raw.at[0, "pnl"] == pytest.approx(5000.0), "pnl should not be zeroed"
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

    def test_apply_flat_row_hygiene_zeros_day_change_val_for_qty_zero(self):
        """B1: day_change_val must be zeroed for qty=0 rows to prevent phantom
        NavStrip P&L. This is the primary regression guard for Change B1."""
        from backend.api.routes.positions import _apply_flat_row_hygiene

        raw = pd.DataFrame([
            {
                "quantity": 0,
                "day_change": 200.0,
                "day_change_val": 9_800.0,   # phantom ₹ P&L from flat intraday
                "day_change_percentage": 1.5,
            }
        ])
        _apply_flat_row_hygiene(raw)
        assert raw.at[0, "day_change_val"] == pytest.approx(0.0), (
            "day_change_val must be 0.0 for qty=0 (flat intraday) rows "
            "to prevent phantom NavStrip P&L"
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
