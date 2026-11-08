"""Tests for grammar_tokens/metric_gain_from_entry_pct.py —
`gain_from_entry_pct` metric.

Sprint 1 foundation (auto-close-short-option-at-90%-profit plan).

  Long (qty > 0):  (ltp - avg_price) / avg_price * 100
  Short (qty < 0): (avg_price - ltp) / avg_price * 100

None (never a coerced 0) when avg_price/ltp/qty is missing, non-positive,
non-finite, or the row's own `price_source` is a confirmed degraded tag.
"""

from __future__ import annotations

import math

import pytest

from backend.api.algo.grammar_tokens.metric_gain_from_entry_pct import (
    _metric_gain_from_entry_pct,
    _coerce_positive_finite_float,
    _DEGRADED_PRICE_SOURCES,
)


def _row(**kw):
    defaults = {'average_price': 100.0, 'last_price': 110.0, 'quantity': 10}
    defaults.update(kw)
    return defaults


class TestLongPosition:
    def test_long_gain(self):
        row = _row(average_price=100.0, last_price=110.0, quantity=10)
        assert _metric_gain_from_entry_pct(None, row) == pytest.approx(10.0)

    def test_long_loss_is_negative(self):
        row = _row(average_price=100.0, last_price=90.0, quantity=5)
        assert _metric_gain_from_entry_pct(None, row) == pytest.approx(-10.0)

    def test_long_90_pct_profit_trigger_shape(self):
        """The exact shape the next sprint's auto-close feature cares
        about: a long option bought at 100, now worth 10 (90% captured
        if this were a SHORT — for a LONG this is a 90% LOSS, confirming
        direction-awareness matters)."""
        row = _row(average_price=100.0, last_price=10.0, quantity=1)
        assert _metric_gain_from_entry_pct(None, row) == pytest.approx(-90.0)


class TestShortPosition:
    def test_short_gain_when_price_drops(self):
        """Short option sold at 100 premium, now worth 10 — 90% of the
        premium captured (the exact real-world trigger shape for the
        planned auto-close-short-at-90%-profit agent)."""
        row = _row(average_price=100.0, last_price=10.0, quantity=-1)
        assert _metric_gain_from_entry_pct(None, row) == pytest.approx(90.0)

    def test_short_loss_when_price_rises(self):
        row = _row(average_price=100.0, last_price=150.0, quantity=-2)
        assert _metric_gain_from_entry_pct(None, row) == pytest.approx(-50.0)


class TestMissingVsZeroConvention:
    def test_zero_quantity_returns_none_not_zero(self):
        """No open position — 'gain from entry' is undefined, not 0."""
        row = _row(quantity=0)
        assert _metric_gain_from_entry_pct(None, row) is None

    def test_missing_quantity_returns_none(self):
        row = _row()
        del row['quantity']
        assert _metric_gain_from_entry_pct(None, row) is None

    def test_missing_avg_price_returns_none(self):
        row = _row()
        del row['average_price']
        assert _metric_gain_from_entry_pct(None, row) is None

    def test_zero_avg_price_returns_none(self):
        row = _row(average_price=0.0)
        assert _metric_gain_from_entry_pct(None, row) is None

    def test_negative_avg_price_returns_none(self):
        row = _row(average_price=-5.0)
        assert _metric_gain_from_entry_pct(None, row) is None

    def test_nan_avg_price_returns_none(self):
        row = _row(average_price=float('nan'))
        assert _metric_gain_from_entry_pct(None, row) is None

    def test_missing_ltp_returns_none(self):
        row = _row()
        del row['last_price']
        assert _metric_gain_from_entry_pct(None, row) is None

    def test_zero_ltp_returns_none(self):
        row = _row(last_price=0.0)
        assert _metric_gain_from_entry_pct(None, row) is None

    def test_negative_ltp_returns_none(self):
        row = _row(last_price=-1.0)
        assert _metric_gain_from_entry_pct(None, row) is None

    def test_nan_ltp_returns_none(self):
        row = _row(last_price=float('nan'))
        assert _metric_gain_from_entry_pct(None, row) is None

    def test_nan_quantity_returns_none(self):
        row = _row(quantity=float('nan'))
        assert _metric_gain_from_entry_pct(None, row) is None

    def test_infinite_avg_price_returns_none(self):
        row = _row(average_price=float('inf'))
        assert _metric_gain_from_entry_pct(None, row) is None

    def test_infinite_ltp_returns_none(self):
        row = _row(last_price=float('inf'))
        assert _metric_gain_from_entry_pct(None, row) is None


class TestDegradedPriceSourceGuard:
    @pytest.mark.parametrize('tag', sorted(_DEGRADED_PRICE_SOURCES))
    def test_each_confirmed_degraded_tag_returns_none(self, tag):
        row = _row(price_source=tag)
        assert _metric_gain_from_entry_pct(None, row) is None, (
            f"price_source={tag!r} must be treated as degraded"
        )

    def test_live_price_source_is_not_degraded(self):
        row = _row(price_source='live')
        assert _metric_gain_from_entry_pct(None, row) == pytest.approx(10.0)

    def test_absent_price_source_is_not_treated_as_degraded(self):
        """A row carrying NO price_source key at all (the normal shape
        of ctx.position_rows on the real live engine path today — see
        the module docstring) must NOT be treated as degraded, or this
        metric would return None on every live tick in production."""
        row = _row()
        assert 'price_source' not in row
        assert _metric_gain_from_entry_pct(None, row) == pytest.approx(10.0)

    def test_degraded_tag_wins_even_with_otherwise_valid_numbers(self):
        row = _row(average_price=100.0, last_price=200.0, quantity=5,
                   price_source='snapshot_settled')
        assert _metric_gain_from_entry_pct(None, row) is None


class TestCoercePositiveFiniteFloat:
    def test_none_returns_none(self):
        assert _coerce_positive_finite_float(None) is None

    def test_non_numeric_string_returns_none(self):
        assert _coerce_positive_finite_float("abc") is None

    def test_zero_returns_none(self):
        assert _coerce_positive_finite_float(0) is None

    def test_negative_returns_none(self):
        assert _coerce_positive_finite_float(-5) is None

    def test_nan_returns_none(self):
        assert _coerce_positive_finite_float(float('nan')) is None

    def test_positive_infinity_returns_none(self):
        """inf is neither NaN nor <= 0, so a naive guard would let it
        through — must be explicitly rejected as non-finite."""
        assert _coerce_positive_finite_float(float('inf')) is None

    def test_real_positive_value_passes_through(self):
        assert _coerce_positive_finite_float("123.5") == pytest.approx(123.5)
