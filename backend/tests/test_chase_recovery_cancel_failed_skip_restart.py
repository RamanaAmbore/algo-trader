"""
Regression test for audit finding O3 (Wave 1, cluster 1C).

`_recover_query_and_cancel_resting` swallows a `cancel_order` exception
at WARNING and then re-reads the order's status once. Pre-fix, whatever
that re-read returned was handed straight back to
`_recover_chase_already_filled` with no check that the order was
actually confirmed gone — if the cancel genuinely failed (order still
OPEN/resting live at the broker), `already_filled` was still derived
from that read and `recover_live_chases()` would restart `chase_order()`
fresh (`current_order_id=None`), placing a BRAND-NEW order for the full
remaining quantity on top of the still-resting original: up to 2x the
intended position.

Fix: when the post-cancel re-read shows the order is still NOT
confirmed gone (not in `_CH_CONFIRMED_GONE_STATUSES`), treat this
exactly like an unverifiable read — return `None` (skip the restart
this cycle) instead of a status dict that lets the derivation proceed.
"""
from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from backend.api.algo.chase import ChaseConfig


@pytest.mark.asyncio
async def test_cancel_exception_with_order_still_resting_skips_restart():
    """Cancel raises, and the post-cancel re-read shows the order is
    STILL OPEN (genuinely still resting live at the broker) — recovery
    must skip restarting this row, not derive an already_filled value
    that `recover_live_chases()` would use to place a duplicate order."""
    from backend.api.background import _recover_chase_already_filled

    row = SimpleNamespace(
        id=1, account="ACC1", symbol="NIFTY24DECFUT", exchange="NFO",
        transaction_type="BUY", quantity=225, filled_quantity=0,
        broker_order_id="B1", agent_id=None,
    )
    cfg = ChaseConfig(exchange="NFO", variety="regular")

    # Both the pre-cancel AND the post-cancel re-read report the SAME
    # still-resting OPEN order — the cancel never actually landed.
    def _fake_order_status(account, order_id):
        return {"status": "OPEN", "quantity": 225, "filled_quantity": 0,
                "average_price": 0.0}

    def _raise_cancel(account, order_id, variety, exchange):
        raise RuntimeError("broker cancel failed")

    with (
        patch("backend.api.algo.chase._order_status", side_effect=_fake_order_status),
        patch("backend.api.algo.chase._cancel_order", side_effect=_raise_cancel),
        patch("backend.shared.helpers.alert_utils.send_order_failure_alert") as mock_alert,
    ):
        result = await _recover_chase_already_filled(row, cfg)

    assert result is None, (
        "a cancel failure with the order confirmed STILL resting live "
        "must skip restarting this row (return None), never derive an "
        "already_filled value that would let recover_live_chases() "
        "place a duplicate order on top of the still-live original."
    )
    mock_alert.assert_called_once(), (
        "an unconfirmed-cancel-during-recovery must alert for manual "
        "review, mirroring chase.py's own ambiguous-cancel pattern."
    )
    assert mock_alert.call_args.kwargs.get("bypass_gates") is True


@pytest.mark.asyncio
async def test_cancel_exception_but_order_confirmed_gone_on_reread_proceeds():
    """Cancel raises, but the broker had ALREADY cancelled/completed it
    by the time of the post-cancel re-read (e.g. a race, or the broker
    cancel landed despite the client-side exception) — recovery must
    still proceed normally, deriving already_filled from the confirmed
    final state."""
    from backend.api.background import _recover_chase_already_filled

    row = SimpleNamespace(
        id=2, account="ACC1", symbol="NIFTY24DECFUT", exchange="NFO",
        transaction_type="BUY", quantity=225, filled_quantity=75,
        broker_order_id="B2", agent_id=None,
    )
    cfg = ChaseConfig(exchange="NFO", variety="regular")

    calls = {"n": 0}

    def _fake_order_status(account, order_id):
        calls["n"] += 1
        if calls["n"] == 1:
            return {"status": "OPEN", "quantity": 150, "filled_quantity": 0,
                    "average_price": 0.0}
        return {"status": "CANCELLED", "quantity": 150, "filled_quantity": 0,
                "average_price": 0.0}

    def _raise_cancel(account, order_id, variety, exchange):
        raise RuntimeError("transient network blip")

    with (
        patch("backend.api.algo.chase._order_status", side_effect=_fake_order_status),
        patch("backend.api.algo.chase._cancel_order", side_effect=_raise_cancel),
    ):
        result = await _recover_chase_already_filled(row, cfg)

    assert result is not None, (
        "once the order is CONFIRMED gone (even after a cancel-call "
        "exception), recovery must proceed normally — not treat every "
        "cancel exception as an automatic skip."
    )
    already_filled, _ = result
    assert already_filled == 75  # 225 - (150 - 0) unfilled remainder
