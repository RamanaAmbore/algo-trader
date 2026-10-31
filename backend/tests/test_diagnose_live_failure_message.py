"""
Regression test for the 2026-10-31 live-incident fix to
`diagnose_live_failure` (backend/api/algo/actions_preflight.py).

Root cause: when a real place_order call failed with a genuine, tiny
margin shortfall (live-observed: Rs 32,131 short on a Rs 10.37M
requirement, during the account's heaviest concurrent trading minute
of the day), the post-failure re-check via basket_order_margins came
back clean moments later -- because margin had simply recovered as
other chase/basket orders on the same account settled, not because the
original failure was a segment-permission issue. The old message
asserted "likely segment permission" as a confident root cause instead
of acknowledging this re-check is time-confounded during concurrent
order flow, and it discarded Kite's own original error (the one
authoritative source for the real required/available figures) after
only a debug-only log line.

Fix: the success-branch message now states what was actually observed
(margin reports sufficient NOW) without asserting a specific cause, and
preserves the original kite_error text in the returned string.
"""
from __future__ import annotations

import asyncio
from unittest.mock import MagicMock

import pytest

from backend.api.algo.actions_preflight import diagnose_live_failure


def _make_broker(basket_margin_side_effect=None, basket_margin_return=None):
    broker = MagicMock()
    if basket_margin_side_effect is not None:
        broker.basket_order_margins.side_effect = basket_margin_side_effect
    else:
        broker.basket_order_margins.return_value = basket_margin_return or [{}]
    broker.normalise_qty.side_effect = lambda _exch, qty, _ls: qty
    return broker


@pytest.fixture(autouse=True)
def _patch_lot_size(monkeypatch):
    async def _fake_lot_size(_exch, _sym):
        return 1
    monkeypatch.setattr(
        "backend.brokers.adapters.kite.get_lot_size", _fake_lot_size,
    )


class TestDiagnoseLiveFailureSuccessBranch:
    def test_does_not_assert_segment_permission_as_the_cause(self):
        broker = _make_broker(basket_margin_return=[{"total": 0.0}])
        order = {
            "exchange": "MCX", "symbol": "GOLD26OCT146000PE", "side": "BUY",
            "qty": 100, "order_type": "LIMIT", "product": "NRML", "price": 5116.0,
        }
        kite_error = (
            "Insufficient funds. Margin required: 10469428.00. "
            "Margin available: 10333759.74. Add 135668.26 to place this order."
        )
        diag = asyncio.run(diagnose_live_failure(broker, order, kite_error))
        assert "likely segment permission" not in diag.lower(), (
            "the re-check alone cannot distinguish a transient shortfall "
            "from a real segment/permission issue -- must not assert it"
        )
        assert "margin now reports sufficient" in diag.lower()

    def test_preserves_the_original_kite_error_with_real_figures(self):
        broker = _make_broker(basket_margin_return=[{"total": 0.0}])
        order = {
            "exchange": "MCX", "symbol": "GOLD26OCT146000PE", "side": "BUY",
            "qty": 100, "order_type": "LIMIT", "product": "NRML", "price": 5116.0,
        }
        kite_error = (
            "Insufficient funds. Margin required: 10469428.00. "
            "Margin available: 10333759.74. Add 135668.26 to place this order."
        )
        diag = asyncio.run(diagnose_live_failure(broker, order, kite_error))
        assert "10469428.00" in diag, (
            "the real required/available figures from Kite's own original "
            "error must survive into the returned diagnosis, not be "
            "discarded after only a debug-only log line"
        )

    def test_no_original_error_omits_the_original_segment_gracefully(self):
        broker = _make_broker(basket_margin_return=[{"total": 0.0}])
        order = {
            "exchange": "MCX", "symbol": "GOLD26OCT146000PE", "side": "BUY",
            "qty": 100, "order_type": "LIMIT", "product": "NRML", "price": 5116.0,
        }
        diag = asyncio.run(diagnose_live_failure(broker, order, ""))
        assert "Original:" not in diag


class TestDiagnoseLiveFailureMarginStillShort:
    def test_margin_keyword_failure_still_reports_margin_shortfall(self):
        broker = _make_broker(
            basket_margin_side_effect=Exception("Insufficient funds for this order"),
        )
        order = {
            "exchange": "MCX", "symbol": "GOLD26OCT146000PE", "side": "BUY",
            "qty": 100, "order_type": "LIMIT", "product": "NRML", "price": 5116.0,
        }
        diag = asyncio.run(diagnose_live_failure(broker, order, "Insufficient funds"))
        assert diag.startswith("margin shortfall")
