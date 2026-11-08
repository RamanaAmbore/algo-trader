"""Tests for grammar_tokens/metric_short_option_gain_from_entry_pct.py —
`short_option_gain_from_entry_pct` metric.

Sprint 3 of the auto-close-short-option plan. Folds the qty<0 direction
check and the CE/PE instrument-type check INSIDE the metric itself, so
the planned agent's condition is a single leaf — avoiding Sprint 1's
confirmed `all[]`/fan-out cross-join finding (see the module's own
docstring and `test_sprint1_autoclose_foundation.py`'s Spike A).
"""

from __future__ import annotations

import pytest

from backend.api.algo.grammar_tokens.metric_short_option_gain_from_entry_pct import (
    _metric_short_option_gain_from_entry_pct,
)


def _row(**kw):
    defaults = {
        'tradingsymbol': 'NIFTY24800CE',
        'average_price': 100.0, 'last_price': 10.0, 'quantity': -1,
    }
    defaults.update(kw)
    return defaults


class TestShortOptionCapturesPremium:
    def test_short_call_90_pct_captured(self):
        row = _row(tradingsymbol='NIFTY24800CE', average_price=100.0,
                   last_price=10.0, quantity=-1)
        assert _metric_short_option_gain_from_entry_pct(None, row) == pytest.approx(90.0)

    def test_short_put_loss_when_premium_rises(self):
        row = _row(tradingsymbol='NIFTY24800PE', average_price=100.0,
                   last_price=150.0, quantity=-2)
        assert _metric_short_option_gain_from_entry_pct(None, row) == pytest.approx(-50.0)


class TestWrongDirectionReturnsNone:
    def test_long_option_returns_none(self):
        """A LONG option row must never be evaluated by this metric —
        the exact cross-join hazard this metric exists to avoid."""
        row = _row(quantity=5)
        assert _metric_short_option_gain_from_entry_pct(None, row) is None

    def test_flat_row_returns_none(self):
        row = _row(quantity=0)
        assert _metric_short_option_gain_from_entry_pct(None, row) is None

    def test_nan_quantity_returns_none(self):
        row = _row(quantity=float('nan'))
        assert _metric_short_option_gain_from_entry_pct(None, row) is None

    def test_negative_infinite_quantity_returns_none(self):
        row = _row(quantity=float('-inf'))
        assert _metric_short_option_gain_from_entry_pct(None, row) is None


class TestWrongInstrumentTypeReturnsNone:
    def test_future_symbol_returns_none(self):
        row = _row(tradingsymbol='BANKNIFTY24AUGFUT', quantity=-1)
        assert _metric_short_option_gain_from_entry_pct(None, row) is None

    def test_equity_symbol_ending_in_ce_not_misread_as_option(self):
        """RELIANCE/BAJFINANCE-shaped tickers end in a CE/PE-like
        suffix with no strike digit before it — must not be
        misclassified as an option (same digit-anchored guard
        `_option_type()` already provides for auto-pairing)."""
        row = _row(tradingsymbol='RELIANCE', quantity=-1)
        assert _metric_short_option_gain_from_entry_pct(None, row) is None

    def test_missing_symbol_returns_none(self):
        row = _row(tradingsymbol=None, quantity=-1)
        assert _metric_short_option_gain_from_entry_pct(None, row) is None


class TestDegradedPriceSourceReturnsNone:
    @pytest.mark.parametrize("tag", ["snapshot_settled", "snapshot_unsettled", "pending_open"])
    def test_each_confirmed_degraded_tag(self, tag):
        row = _row(price_source=tag)
        assert _metric_short_option_gain_from_entry_pct(None, row) is None

    def test_live_tag_is_not_degraded(self):
        row = _row(price_source='live')
        assert _metric_short_option_gain_from_entry_pct(None, row) == pytest.approx(90.0)

    def test_absent_price_source_is_not_treated_as_degraded(self):
        row = _row()
        assert 'price_source' not in row
        assert _metric_short_option_gain_from_entry_pct(None, row) == pytest.approx(90.0)


class TestMissingNumericDataReturnsNone:
    def test_missing_average_price_returns_none(self):
        row = _row(average_price=None)
        assert _metric_short_option_gain_from_entry_pct(None, row) is None

    def test_zero_average_price_returns_none(self):
        row = _row(average_price=0)
        assert _metric_short_option_gain_from_entry_pct(None, row) is None

    def test_negative_average_price_returns_none(self):
        row = _row(average_price=-5)
        assert _metric_short_option_gain_from_entry_pct(None, row) is None

    def test_missing_ltp_returns_none(self):
        row = _row(last_price=None)
        assert _metric_short_option_gain_from_entry_pct(None, row) is None

    def test_zero_ltp_returns_none(self):
        row = _row(last_price=0)
        assert _metric_short_option_gain_from_entry_pct(None, row) is None

    def test_nan_average_price_returns_none(self):
        row = _row(average_price=float('nan'))
        assert _metric_short_option_gain_from_entry_pct(None, row) is None

    def test_inf_ltp_returns_none(self):
        row = _row(last_price=float('inf'))
        assert _metric_short_option_gain_from_entry_pct(None, row) is None
