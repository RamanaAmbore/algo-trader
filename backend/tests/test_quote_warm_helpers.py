"""Unit tests for closed-hours quote-warm helpers extracted from
_maybe_warm_closed_hours_quotes in backend/api/routes/quote.py.
"""
import pytest

from backend.api.routes.quote import (
    _build_lkg_payload_from_quote,
    _extract_top_price,
)


# ── _extract_top_price ────────────────────────────────────────────────────
class TestExtractTopPrice:
    def test_none_side_returns_none(self):
        assert _extract_top_price(None) is None

    def test_empty_side_returns_none(self):
        assert _extract_top_price([]) is None

    def test_zero_price_returns_none(self):
        assert _extract_top_price([{"price": 0}]) is None

    def test_missing_price_returns_none(self):
        assert _extract_top_price([{}]) is None

    def test_valid_price_returned(self):
        assert _extract_top_price([{"price": 100.5}]) == 100.5

    def test_int_price_cast_to_float(self):
        v = _extract_top_price([{"price": 100}])
        assert v == 100.0
        assert isinstance(v, float)

    def test_bad_price_swallowed(self):
        assert _extract_top_price([{"price": "not a number"}]) is None

    def test_first_element_used_not_last(self):
        assert _extract_top_price([{"price": 1.0}, {"price": 2.0}]) == 1.0


# ── _build_lkg_payload_from_quote ────────────────────────────────────────
class TestBuildLkgPayload:
    def test_full_payload_arithmetic(self):
        q = {
            "last_price": 100.0,
            "ohlc": {"open": 95.0, "close": 98.0},
            "volume": 1000,
            "oi": 50,
            "depth": {
                "buy":  [{"price": 99.5}],
                "sell": [{"price": 100.5}],
            },
        }
        p = _build_lkg_payload_from_quote(q)
        assert p["last_price"] == 100.0
        assert p["open"] == 95.0
        assert p["close"] == 98.0
        assert p["volume"] == 1000
        assert p["oi"] == 50
        assert p["bid"] == 99.5
        assert p["ask"] == 100.5
        assert p["change"] == pytest.approx(2.0)
        assert p["change_pct"] == pytest.approx(2.0 / 98.0 * 100.0)

    def test_no_close_zeroes_change(self):
        q = {"last_price": 100.0, "ohlc": {}, "depth": {}}
        p = _build_lkg_payload_from_quote(q)
        assert p["close"] is None
        assert p["change"] == 0.0
        assert p["change_pct"] == 0.0

    def test_no_ltp_zero_change(self):
        q = {"last_price": 0.0, "ohlc": {"close": 100.0}, "depth": {}}
        p = _build_lkg_payload_from_quote(q)
        assert p["last_price"] == 0.0
        assert p["change"] == 0.0

    def test_missing_depth_none_bid_ask(self):
        q = {"last_price": 100.0, "ohlc": {"close": 100.0}}
        p = _build_lkg_payload_from_quote(q)
        assert p["bid"] is None
        assert p["ask"] is None

    def test_empty_payload(self):
        p = _build_lkg_payload_from_quote({})
        assert p["last_price"] == 0.0
        assert p["open"] is None
        assert p["close"] is None
        assert p["volume"] == 0
        assert p["oi"] == 0
        assert p["change"] == 0.0

    def test_change_positive_when_ltp_above_close(self):
        q = {
            "last_price": 105.0,
            "ohlc": {"close": 100.0},
            "depth": {},
        }
        p = _build_lkg_payload_from_quote(q)
        assert p["change"] == pytest.approx(5.0)
        assert p["change_pct"] == pytest.approx(5.0)

    def test_change_negative_when_ltp_below_close(self):
        q = {
            "last_price": 95.0,
            "ohlc": {"close": 100.0},
            "depth": {},
        }
        p = _build_lkg_payload_from_quote(q)
        assert p["change"] == pytest.approx(-5.0)
        assert p["change_pct"] == pytest.approx(-5.0)

    def test_only_bid_side_populated(self):
        q = {
            "last_price": 100.0,
            "ohlc": {"close": 100.0},
            "depth": {"buy": [{"price": 99.0}]},
        }
        p = _build_lkg_payload_from_quote(q)
        assert p["bid"] == 99.0
        assert p["ask"] is None
