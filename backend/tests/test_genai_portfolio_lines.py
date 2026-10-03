"""
2026-10 audit fix: `fetch_holdings()` / `fetch_positions()`
(backend/brokers/broker_apis.py) rename the raw broker column
'close_price' -> 'prev_close' as part of the prev_close/ltp invariant.
`_holding_row_line` (backend/shared/helpers/genai_api.py) still read
`row.get('close_price', 0)` as the current LTP for the Gemini market
report prompt -- after the rename that key is simply absent, so every
holding line was sent with ltp=₹0.00 regardless of its real price.

`_position_row_line` already had the correct fallback chain
(`row.get('close_price', 0) or row.get('last_price', 0) or 0`), which
is why it was never broken by the same rename — this fix brings
`_holding_row_line` in line with that existing, working pattern.
"""
from __future__ import annotations

from backend.shared.helpers.genai_api import _holding_row_line, _position_row_line


class TestHoldingRowLineLtpFallback:
    def test_falls_back_to_last_price_when_close_price_absent(self):
        """Post-rename shape: no 'close_price' key at all (as produced by
        fetch_holdings()'s rename to 'prev_close'); 'last_price' is the
        real current LTP."""
        row = {
            "tradingsymbol": "RELIANCE",
            "quantity": 10,
            "average_price": 2400.0,
            "last_price": 2500.0,
            "prev_close": 2450.0,
            "pnl": 1000.0,
            "day_change_val": 500.0,
            "day_change_percentage": 2.0,
        }
        result = _holding_row_line(row)
        assert result is not None
        line, sym = result
        assert sym == "RELIANCE"
        assert "ltp=₹2500.00" in line, f"expected real LTP in line, got: {line!r}"
        assert "ltp=₹0.00" not in line

    def test_prefers_close_price_when_present_legacy_shape(self):
        """Back-compat: if a caller ever passes a row that DOES still
        carry 'close_price' (e.g. a raw un-renamed broker dict), it must
        still be used — this fix only ADDS a fallback, not replace the
        primary key."""
        row = {
            "tradingsymbol": "INFY",
            "quantity": 5,
            "average_price": 1400.0,
            "close_price": 1500.0,
            "last_price": 9999.0,  # must NOT be used when close_price present
            "pnl": 500.0,
            "day_change_val": 100.0,
            "day_change_percentage": 1.0,
        }
        result = _holding_row_line(row)
        line, sym = result
        assert "ltp=₹1500.00" in line

    def test_zero_qty_row_skipped(self):
        row = {"tradingsymbol": "TCS", "quantity": 0, "last_price": 3000.0}
        assert _holding_row_line(row) is None

    def test_missing_symbol_skipped(self):
        row = {"tradingsymbol": "", "quantity": 10, "last_price": 100.0}
        assert _holding_row_line(row) is None

    def test_matches_position_row_line_fallback_semantics(self):
        """Regression lock: both row-line formatters must resolve LTP the
        same way given the identical post-rename shape, since they read
        from the same fetch_holdings()/fetch_positions() pipeline."""
        shared_fields = {
            "tradingsymbol": "WIPRO",
            "average_price": 400.0,
            "last_price": 420.0,
            "pnl": 200.0,
        }
        holding_row = {**shared_fields, "quantity": 10,
                       "day_change_val": 50.0, "day_change_percentage": 1.0}
        position_row = {**shared_fields, "quantity": 10}

        h_line, _ = _holding_row_line(holding_row)
        p_line, _, _ = _position_row_line(position_row)
        assert "ltp=₹420.00" in h_line
        assert "ltp=₹420.00" in p_line
