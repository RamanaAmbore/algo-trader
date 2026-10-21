"""
Regression tests for the 2026-10 margin-gate activation fix (items 4/5).

Dhan's `_margin_for_order` / Groww's `basket_order_margins` both return a
FLAT margin shape (`{"total": ..., "var": ..., "exposure": ...,
"available": {...}}`) with no `final`/`initial` nesting at all — unlike
Kite's response. `actions_preflight.py:_preflight_leg_required` used to
only check Kite's nested keys, so `_preflight_leg_required({"total":
150000.0})` returned 0.0 — confirmed directly by running the function —
meaning `required` was always 0 for every Dhan/Groww order on every
exchange, making the margin-shortfall gate completely inert for both
brokers.

These tests build the mocked SDK response from the REAL flat shape (not
a hand-built Kite-shaped dict) — the audit found that hand-built
Kite-shaped stubs are exactly how the original bug went undetected
(`backend/tests/test_funds_order_margin.py` only ever stubbed Kite's
nested shape).
"""

from __future__ import annotations

from unittest.mock import MagicMock, create_autospec

import pytest

from backend.brokers.adapters.dhan import DhanBroker
from backend.brokers.adapters.groww import GrowwBroker
from backend.api.algo.actions_preflight import (
    _preflight_leg_required,
    _preflight_parse_basket_margin,
)

try:
    from growwapi import GrowwAPI
except ImportError:
    GrowwAPI = None


# ---------------------------------------------------------------------------
# Dhan — real flat shape
# ---------------------------------------------------------------------------

@pytest.fixture
def dhan_broker():
    conn = MagicMock()
    conn.account = "DHN123"
    conn._conn_created_at = None
    return DhanBroker(conn=conn)


class TestDhanMarginRealShape:
    def test_margin_for_order_returns_flat_shape_with_real_fields(self, dhan_broker):
        """Dhan's margin_calculator response (status/data/totalMargin/...)
        normalises to the flat {"total", "var", "exposure", "available"}
        shape this adapter has always claimed to return."""
        sdk_mock = MagicMock()
        sdk_mock.margin_calculator.return_value = {
            "status": "success",
            "data": {
                "totalMargin": 48250.5,
                "spanMargin": 30000.0,
                "exposureMargin": 18250.5,
                "availableBalance": 500000.0,
            },
        }
        dhan_broker._conn.get_dhan_conn.return_value = sdk_mock

        result = dhan_broker._margin_for_order({
            "exchange": "NFO", "tradingsymbol": "NIFTY25NOVFUT",
            "transaction_type": "SELL", "quantity": 75,
            "product": "NRML", "price": 24000.0,
        })

        assert result["total"] == pytest.approx(48250.5)
        # Item 4's actual bug, reproduced directly against the real shape:
        # the OLD parser only read entry["final"]["total"] /
        # entry["initial"]["total"] / entry["required"] — none of which
        # exist on this flat dict — so _preflight_leg_required returned
        # 0.0 regardless of the real totalMargin value above.
        assert _preflight_leg_required(result) == pytest.approx(48250.5)

    def test_margin_for_order_missing_field_is_none_not_zero(self, dhan_broker):
        """Missing-vs-zero convention (item 5): a genuinely absent
        totalMargin key must parse to None, never a coerced 0.0 that
        would read as "confirmed zero margin needed"."""
        sdk_mock = MagicMock()
        sdk_mock.margin_calculator.return_value = {
            "status": "success",
            "data": {
                # totalMargin genuinely absent (field-name drift / schema
                # change) — only unrelated fields present.
                "spanMargin": 100.0,
            },
        }
        dhan_broker._conn.get_dhan_conn.return_value = sdk_mock

        result = dhan_broker._margin_for_order({
            "exchange": "NFO", "tradingsymbol": "NIFTY25NOVFUT",
            "transaction_type": "BUY", "quantity": 75,
        })

        assert result["total"] is None
        assert _preflight_leg_required(result) is None

    def test_basket_order_margins_exception_leg_is_none_not_zero(self, dhan_broker):
        """A per-leg failure inside basket_order_margins's own try/except
        must carry total=None (unknown), not 0.0 (confirmed-zero) — a
        genuine failure must never silently read as "no margin needed"."""
        sdk_mock = MagicMock()
        sdk_mock.margin_calculator.side_effect = RuntimeError("Dhan API down")
        dhan_broker._conn.get_dhan_conn.return_value = sdk_mock

        out = dhan_broker.basket_order_margins([{
            "exchange": "NFO", "tradingsymbol": "NIFTY25NOVFUT",
            "transaction_type": "BUY", "quantity": 75,
        }])

        assert len(out) == 1
        assert out[0]["total"] is None
        assert "error" in out[0]


# ---------------------------------------------------------------------------
# Groww — real flat shape
# ---------------------------------------------------------------------------

@pytest.fixture
def groww_broker():
    conn = MagicMock()
    conn.account = "GRW123"
    conn.get_groww_conn = MagicMock()
    return GrowwBroker(conn)


class TestGrowwMarginRealShape:
    """2026-10-20: the per-order-kwargs call to `get_order_margin_details`
    (`trading_symbol=..., exchange=..., segment=...` etc.) never matched the
    real installed SDK's signature — `(segment: str, orders: list[dict],
    timeout=None)` — and raised TypeError on every real call, confirmed live
    against dev.ramboq.com. These tests use `create_autospec(GrowwAPI)` (not
    a bare `MagicMock`) so a future call-shape regression fails loudly — a
    bare MagicMock accepts any kwargs and is exactly how the original
    signature bug went undetected. The response shape below
    (`total_requirement`/`span_margin_required`/`exposure_margin_required`,
    no `data`/`payload` wrapper) is the real `OrderMarginResponse` schema;
    `GrowwAPI._parse_response` already unwraps `payload` before returning."""

    @pytest.fixture
    def autospec_groww(self):
        if GrowwAPI is None:
            pytest.skip("growwapi SDK not installed in this environment")
        return create_autospec(GrowwAPI, instance=True)

    def test_basket_order_margins_calls_real_batch_signature(self, groww_broker, autospec_groww):
        """Fails against the pre-fix per-order-kwargs call: autospec
        enforces the real `(segment, orders, timeout=None)` signature, so
        a call shaped like `get_order_margin_details(trading_symbol=...,
        exchange=..., segment=..., ...)` raises TypeError here exactly as
        it did live, instead of silently succeeding against a bare mock."""
        autospec_groww.get_order_margin_details.return_value = {
            "total_requirement": 62500.0,
            "span_margin_required": 40000.0,
            "exposure_margin_required": 22500.0,
        }
        # `groww` is a read-only property backed by `_conn.get_groww_conn()`.
        groww_broker._conn.get_groww_conn.return_value = autospec_groww

        out = groww_broker.basket_order_margins([{
            "exchange": "NFO", "tradingsymbol": "NIFTY25NOVFUT",
            "transaction_type": "SELL", "quantity": 75,
            "product": "MIS", "order_type": "LIMIT", "price": 24000.0,
        }])

        assert len(out) == 1
        assert "error" not in out[0]
        assert out[0]["total"] == pytest.approx(62500.0)
        assert _preflight_leg_required(out[0]) == pytest.approx(62500.0)

        call = autospec_groww.get_order_margin_details.call_args
        assert call.kwargs["segment"] == "FNO"
        order0 = call.kwargs["orders"][0]
        assert order0["trading_symbol"] == "NIFTY25NOVFUT"
        assert order0["transaction_type"] == "SELL"
        assert order0["quantity"] == 75
        # NFO maps to Groww exchange "NSE" (Groww distinguishes F&O at the
        # segment level, not the exchange level) — see _EXCHANGE_TO_GROWW.
        assert order0["exchange"] == "NSE"

    def test_basket_order_margins_returns_flat_shape_with_real_fields(self, groww_broker):
        """Groww's real `get_order_margin_details` response
        (`total_requirement`/...) normalises to the flat {"total", "var",
        "exposure", "available"} shape this adapter has always claimed to
        return."""
        groww_broker.groww.get_order_margin_details = MagicMock(return_value={
            "total_requirement": 62500.0,
            "span_margin_required": 40000.0,
            "exposure_margin_required": 22500.0,
        })

        out = groww_broker.basket_order_margins([{
            "exchange": "NFO", "tradingsymbol": "NIFTY25NOVFUT",
            "transaction_type": "SELL", "quantity": 75,
            "product": "MIS", "order_type": "LIMIT", "price": 24000.0,
        }])

        assert len(out) == 1
        assert out[0]["total"] == pytest.approx(62500.0)
        # Item 4's actual bug, reproduced directly against the real shape.
        assert _preflight_leg_required(out[0]) == pytest.approx(62500.0)

    def test_basket_order_margins_missing_field_is_none_not_zero(self, groww_broker):
        """Missing-vs-zero convention (item 5): a genuinely absent
        total_requirement key must parse to None, never a coerced 0.0."""
        groww_broker.groww.get_order_margin_details = MagicMock(return_value={
            # total_requirement genuinely absent.
            "span_margin_required": 100.0,
        })

        out = groww_broker.basket_order_margins([{
            "exchange": "NFO", "tradingsymbol": "NIFTY25NOVFUT",
            "transaction_type": "BUY", "quantity": 75,
        }])

        assert out[0]["total"] is None
        assert _preflight_leg_required(out[0]) is None

    def test_basket_order_margins_exception_leg_is_none_not_zero(self, groww_broker):
        """A per-leg failure must carry total=None (unknown), not 0.0."""
        groww_broker.groww.get_order_margin_details = MagicMock(
            side_effect=RuntimeError("Groww API down")
        )

        out = groww_broker.basket_order_margins([{
            "exchange": "NFO", "tradingsymbol": "NIFTY25NOVFUT",
            "transaction_type": "BUY", "quantity": 75,
        }])

        assert out[0]["total"] is None
        assert "error" in out[0]


# ---------------------------------------------------------------------------
# Multi-leg sum — fail-open on the WHOLE basket when ANY leg is unknown
# ---------------------------------------------------------------------------

class TestMultiLegFailOpen:
    def test_any_none_leg_makes_whole_basket_unknown(self):
        """A 2-leg basket where leg 1 parses fine (flat Dhan/Groww shape)
        but leg 2 is unparseable must sum to None overall, never a
        silent partial sum that understates the real requirement."""
        bm_result = [
            {"total": 50000.0},
            {"error": "timeout", "total": None},
        ]
        assert _preflight_parse_basket_margin(bm_result) is None

    def test_all_legs_present_sums_flat_shape(self):
        """Multi-leg sum still works correctly for the flat Dhan/Groww
        shape when every leg parses."""
        bm_result = [{"total": 30000.0}, {"total": 15000.0}]
        assert _preflight_parse_basket_margin(bm_result) == pytest.approx(45000.0)

    def test_kite_nested_shape_unaffected_by_flat_fallback(self):
        """Kite's real nested shape (final/initial) must still be read
        exactly as before — the new flat-`total` fallback only engages
        when final/initial are BOTH absent, so a genuine Kite response
        is never affected."""
        bm_result = [{"initial": {"total": 5000.0}, "final": {"total": 4000.0}}]
        # final.total is checked first — unchanged from pre-fix behavior.
        assert _preflight_parse_basket_margin(bm_result) == pytest.approx(4000.0)
