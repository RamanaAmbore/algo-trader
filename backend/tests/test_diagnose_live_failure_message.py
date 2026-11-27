"""
Regression test for the 2026-10-31 live-incident fix to
`diagnose_live_failure` (backend/api/algo/actions_preflight.py), and
for the 2026-11 real-money audit follow-up fix (this file's later
classes).

Root cause (2026-10-31): when a real place_order call failed with a
genuine, tiny margin shortfall (live-observed: Rs 32,131 short on a Rs
10.37M requirement, during the account's heaviest concurrent trading
minute of the day), the post-failure re-check via basket_order_margins
came back clean moments later -- because margin had simply recovered as
other chase/basket orders on the same account settled, not because the
original failure was a segment-permission issue. The old message
asserted "likely segment permission" as a confident root cause instead
of acknowledging this re-check is time-confounded during concurrent
order flow, and it discarded Kite's own original error (the one
authoritative source for the real required/available figures) after
only a debug-only log line.

Fix (2026-10-31): the success-branch message now states what was
actually observed (margin reports sufficient NOW) without asserting a
specific cause, and preserves the original kite_error text in the
returned string.

Root cause (2026-11, this file's later classes): that same fix still
treated "the re-check call didn't raise" as "sufficient" without ever
comparing the re-checked number to the account's real available
margin, and `basket_order_margins` is a pure calculator -- it never
verifies sufficiency itself. Dhan's and Groww's own adapter methods
never raise on failure either -- they catch internally and return
`{"total": None, "error": ...}` -- so the "didn't raise" branch was
ALWAYS taken for Dhan/Groww, reporting "sufficient" on effectively
every failed Dhan/Groww order regardless of what actually happened.

Fix (2026-11): the returned total is now compared against the real
available margin before ever saying "sufficient"; a `{"total": None,
"error": ...}`-shaped response is now "re-check inconclusive", never
"sufficient"; and a server-verified close now gets explicit
close-aware wording in the message.
"""
from __future__ import annotations

import asyncio
from unittest.mock import MagicMock

import pytest

from backend.api.algo.actions_preflight import diagnose_live_failure


def _make_broker(basket_margin_side_effect=None, basket_margin_return=None,
                  margins_return=None, broker_id=""):
    broker = MagicMock()
    if basket_margin_side_effect is not None:
        broker.basket_order_margins.side_effect = basket_margin_side_effect
    else:
        broker.basket_order_margins.return_value = basket_margin_return or [{}]
    broker.normalise_qty.side_effect = lambda _exch, qty, _ls: qty
    broker.broker_id = broker_id
    if margins_return is not None:
        broker.margins.return_value = margins_return
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


_MCX_ORDER = {
    "exchange": "MCX", "symbol": "GOLD26OCT146000PE", "side": "BUY",
    "qty": 100, "order_type": "LIMIT", "product": "NRML", "price": 5116.0,
}


class TestDiagnoseLiveFailureComparesAgainstRealAvailableMargin:
    """
    2026-11 real-money audit fix, item 1: `basket_order_margins` is a
    pure calculator -- it returns a required-margin number, it never
    itself verifies sufficiency. The re-check's returned total must now
    be compared against the account's real available margin before
    the message ever says "sufficient".
    """

    def test_required_within_available_reports_sufficient_with_real_figures(self):
        broker = _make_broker(
            basket_margin_return=[{"total": 400000.0}],
            margins_return={"commodity": {"enabled": True, "net": 500000.0}},
        )
        diag = asyncio.run(diagnose_live_failure(broker, _MCX_ORDER, ""))
        low = diag.lower()
        assert "sufficient" in low
        assert "400,000" in diag or "400000" in diag
        assert "500,000" in diag or "500000" in diag

    def test_required_exceeds_available_does_not_claim_sufficient_and_states_the_gap(self):
        broker = _make_broker(
            basket_margin_return=[{"total": 700000.0}],
            margins_return={"commodity": {"enabled": True, "net": 500000.0}},
        )
        diag = asyncio.run(diagnose_live_failure(broker, _MCX_ORDER, ""))
        low = diag.lower()
        assert "sufficient" not in low, (
            "required (700000) genuinely exceeds the real available margin "
            "(500000) -- the re-check must NOT be reported as sufficient"
        )
        assert "still exceeds" in low or "not recovered" in low
        # the real gap (200000) must be stated, not just a bare "shortfall"
        assert "200,000" in diag or "200000" in diag

    def test_unknown_available_margin_never_claims_sufficient(self):
        """
        A bare MagicMock `broker.margins()` (no explicit margins_return)
        resolves to m_enabled=False / available=None via the real
        `_preflight_fetch_account_margins` parser -- the re-check must
        report "could not be confirmed", never a false "sufficient",
        whenever `required` is a genuine positive number.
        """
        broker = _make_broker(basket_margin_return=[{"total": 400000.0}])
        diag = asyncio.run(diagnose_live_failure(broker, _MCX_ORDER, ""))
        low = diag.lower()
        assert "sufficient" not in low
        assert "could not be confirmed" in low

    def test_segment_disabled_never_claims_sufficient_even_with_a_numeric_net(self):
        """Mirrors `_preflight_handle_positive_margin`'s own m_enabled gate:
        a disabled segment is never compared, however numeric `net` is."""
        broker = _make_broker(
            basket_margin_return=[{"total": 400000.0}],
            margins_return={"commodity": {"enabled": False, "net": 999999.0}},
        )
        diag = asyncio.run(diagnose_live_failure(broker, _MCX_ORDER, ""))
        low = diag.lower()
        assert "sufficient" not in low
        assert "could not be confirmed" in low

    def test_required_zero_or_negative_is_sufficient_without_needing_available(self):
        """required<=0 (a genuine credit/zero-margin-needed outcome) is its
        own sufficient case and needs no available-margin lookup at all --
        a bare unmocked broker.margins() must not change this."""
        broker = _make_broker(basket_margin_return=[{"total": 0.0}])
        diag = asyncio.run(diagnose_live_failure(broker, _MCX_ORDER, ""))
        assert "margin now reports sufficient" in diag.lower()


class TestDiagnoseLiveFailureDhanGrowwNeverRaise:
    """
    2026-11 real-money audit fix, item 2: Dhan's `_margin_for_order` and
    Groww's `basket_order_margins` catch their own SDK failures
    internally and return `{"total": None, "error": "..."}` -- they
    never raise. The old "didn't raise => sufficient" logic therefore
    reported "sufficient" on effectively every failed Dhan/Groww order.
    """

    def test_total_none_with_error_is_inconclusive_never_sufficient(self):
        real_groww_error = (
            "get_order_margin_details() got an unexpected keyword "
            "argument 'trading_symbol'"
        )
        broker = _make_broker(
            basket_margin_return=[{"total": None, "error": real_groww_error, "raw": None}],
            broker_id="groww",
        )
        diag = asyncio.run(diagnose_live_failure(broker, _MCX_ORDER, "Insufficient funds"))
        low = diag.lower()
        # The message explicitly says "does NOT mean margin is sufficient"
        # -- it must never make the bare AFFIRMATIVE claim the real
        # sufficient-branch wording uses ("now reports sufficient" /
        # "now appears sufficient").
        assert "now reports sufficient" not in low, (
            "a Dhan/Groww-shaped {total: None, error: ...} response must "
            "never be reported as margin being sufficient"
        )
        assert "now appears sufficient" not in low
        assert "does not mean margin is sufficient" in low
        assert "inconclusive" in low
        assert real_groww_error in diag, (
            "the broker's own real error text must surface verbatim, the "
            "same convention already used for the original kite_error"
        )

    def test_total_none_error_is_not_run_through_the_margin_keyword_classifier(self):
        """A transport-layer failure whose text happens to contain the
        word 'margin' (purely because of a method name) must not be
        misclassified as a confident margin-shortfall diagnosis -- the
        inconclusive branch must never claim a 'shortfall'."""
        real_groww_error = (
            "get_order_margin_details() got an unexpected keyword "
            "argument 'trading_symbol'"
        )
        broker = _make_broker(
            basket_margin_return=[{"total": None, "error": real_groww_error, "raw": None}],
            broker_id="groww",
        )
        diag = asyncio.run(diagnose_live_failure(broker, _MCX_ORDER, ""))
        assert "shortfall" not in diag.lower()
        assert diag.startswith("margin re-check inconclusive")

    def test_multi_leg_total_none_without_any_leg_error_still_inconclusive(self):
        broker = _make_broker(
            basket_margin_return=[
                {"total": None}, {"total": None},
            ],
            broker_id="dhan",
        )
        diag = asyncio.run(diagnose_live_failure(broker, _MCX_ORDER, ""))
        low = diag.lower()
        assert "now reports sufficient" not in low
        assert "now appears sufficient" not in low
        assert "inconclusive" in low
        assert "no error detail returned" in low

    def test_total_zero_with_a_leg_error_is_inconclusive_not_sufficient(self):
        """
        Live-confirmed payload (CLAUDE.md, pre-fix Groww signature bug):
        `[{"total": 0.0, "error": "...unexpected keyword argument
        'trading_symbol'", "raw": None}]` -- a leg can carry a genuine
        `error` string ALONGSIDE a parsed total of 0.0.
        `_preflight_parse_basket_margin` returns 0.0 (not None) for this
        shape, so the `required is None` check alone does NOT catch it
        -- without also checking for a leg error, this would fall
        straight into the `required <= 0` branch and be reported
        "sufficient", reproducing the exact false-sufficient bug this
        fix exists to remove.
        """
        real_groww_error = (
            "get_order_margin_details() got an unexpected keyword "
            "argument 'trading_symbol'"
        )
        broker = _make_broker(
            basket_margin_return=[
                {"total": 0.0, "error": real_groww_error, "raw": None},
            ],
            broker_id="groww",
        )
        diag = asyncio.run(diagnose_live_failure(broker, _MCX_ORDER, ""))
        low = diag.lower()
        assert "now reports sufficient" not in low, (
            "a leg carrying its own error string must never be reported "
            "as margin being sufficient, however its total parsed"
        )
        assert "now appears sufficient" not in low
        assert "inconclusive" in low
        assert real_groww_error in diag


class TestDiagnoseLiveFailureCloseAwareness:
    """
    2026-11 real-money audit fix, item 3: a margin rejection on a
    server-verified close is a different, more concerning situation
    than one on a fresh open, and the message must say so.
    """

    def test_is_close_false_default_adds_no_close_text(self):
        broker = _make_broker(basket_margin_return=[{"total": 0.0}])
        diag = asyncio.run(diagnose_live_failure(broker, _MCX_ORDER, ""))
        assert "close" not in diag.lower()

    def test_verified_close_on_a_shortfall_states_the_close_fact_not_a_cause(self):
        broker = _make_broker(
            basket_margin_return=[{"total": 700000.0}],
            margins_return={"commodity": {"enabled": True, "net": 500000.0}},
            broker_id="zerodha_kite",
        )
        diag = asyncio.run(
            diagnose_live_failure(broker, _MCX_ORDER, "", is_close=True)
        )
        low = diag.lower()
        assert "verified close order" in low
        assert "unexpected and should be reviewed" in low
        # must state the fact, never assert a specific root cause (the
        # exact over-certainty mistake the 2026-10-31 fix already
        # corrected for the generic case)
        assert "product mismatch" not in low
        assert "likely" not in low

    def test_verified_close_on_dhan_groww_shortfall_gets_the_naked_pricing_note(self):
        broker = _make_broker(
            basket_margin_return=[{"total": 700000.0}],
            margins_return={"commodity": {"enabled": True, "net": 500000.0}},
            broker_id="dhan",
        )
        diag = asyncio.run(
            diagnose_live_failure(broker, _MCX_ORDER, "", is_close=True)
        )
        low = diag.lower()
        # Dhan/Groww reuse `_DHAN_GROWW_CLOSE_NOTE` verbatim (it already
        # opens with "Verified close —") rather than a second,
        # redundant "verified close order" lead-in sentence.
        assert "verified close" in low
        assert "do not net against existing positions" in low, (
            "Dhan/Groww price a close naked -- must reuse the SAME "
            "_DHAN_GROWW_CLOSE_NOTE text the real preflight gate already "
            "shows the operator, not a second, possibly-divergent note"
        )

    def test_verified_close_inconclusive_branch_also_gets_close_text(self):
        broker = _make_broker(
            basket_margin_return=[{"total": None, "error": "boom"}],
            broker_id="groww",
        )
        diag = asyncio.run(
            diagnose_live_failure(broker, _MCX_ORDER, "", is_close=True)
        )
        low = diag.lower()
        assert "inconclusive" in low
        assert "verified close" in low

    def test_verified_close_on_raised_exception_branch_also_gets_close_text(self):
        broker = _make_broker(
            basket_margin_side_effect=Exception("Insufficient funds for this order"),
        )
        diag = asyncio.run(
            diagnose_live_failure(broker, _MCX_ORDER, "", is_close=True)
        )
        low = diag.lower()
        assert diag.startswith("margin shortfall")
        assert "verified close order" in low
