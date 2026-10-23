"""
Regression test for audit finding O8 (Wave 1, cluster 1G).

`_start_live_chase` (`orders_helpers.py`) runs `_ch_seed_pre_fill_net_qty`
(invalidates the raw positions cache + fetches positions across ALL
accounts) INSIDE the 15s `asyncio.wait_for` ticket-confirm window, before
the first depth/placement call. A slow broker response (3+ accounts) can
exceed the 15s budget — the ticket then raises a plain `RuntimeError` on
`asyncio.TimeoutError`, which `_ticket_handle_live_place_error` treated
identically to a genuine broker rejection, unconditionally calling
`_record_rejection` and risking a false circuit-breaker trip for an order
that was never actually rejected by the broker (it may have landed and
been killed via the abandonment path).

Fix: `_start_live_chase` raises a distinguishable `ChaseConfirmTimeout`
(a `RuntimeError` subclass) on the confirm-timeout path specifically.
`_ticket_handle_live_place_error` skips `_record_rejection` /
`_maybe_send_breaker_alert` for that exception type, while a genuine
broker rejection (any other exception) still records normally.
"""
from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest


def _data_stub():
    return MagicMock(
        exchange="NFO", product="NRML", price=100.0, variety="regular",
        source="ticket",
    )


@pytest.mark.asyncio
async def test_confirm_timeout_does_not_trip_the_breaker():
    from backend.api.routes import orders_place as m
    from backend.api.routes.orders_helpers import ChaseConfirmTimeout

    exc = ChaseConfirmTimeout(
        "chase confirm timed out after 15.0s waiting for the first "
        "order_placed event — chase task left running; any late "
        "placement will be killed"
    )

    with patch("backend.api.algo.actions.diagnose_live_failure",
               new=AsyncMock(return_value="diagnosis unavailable")), \
         patch("backend.api.routes.orders_helpers._broker_for"), \
         patch("backend.shared.helpers.alert_utils.send_order_failure_alert"), \
         patch("backend.api.algo.agent_engine.record_manual_event", new=AsyncMock()), \
         patch("backend.api.routes.orders_helpers._record_rejection") as mock_record, \
         patch("backend.api.routes.orders_helpers._maybe_send_breaker_alert") as mock_breaker:
        with pytest.raises(Exception):
            await m._ticket_handle_live_place_error(
                _data_stub(), "ZG0790", "NIFTY25JULFUT", "BUY", 50,
                "LIMIT", exc, "ZG0790:NFO:NIFTY25JULFUT",
            )

    mock_record.assert_not_called(), (
        "a client-side chase confirm-timeout must NOT be recorded as a "
        "broker rejection — the order may have actually landed and been "
        "killed via the abandonment path, not genuinely rejected."
    )
    mock_breaker.assert_not_called()


@pytest.mark.asyncio
async def test_genuine_broker_rejection_still_trips_the_breaker():
    """A real broker-raised exception (not the confirm-timeout) must
    still record normally — this fix must not weaken the breaker for
    actual rejections."""
    from backend.api.routes import orders_place as m

    exc = RuntimeError("Insufficient funds")

    with patch("backend.api.algo.actions.diagnose_live_failure",
               new=AsyncMock(return_value="margin shortfall")), \
         patch("backend.api.routes.orders_helpers._broker_for"), \
         patch("backend.shared.helpers.alert_utils.send_order_failure_alert"), \
         patch("backend.api.algo.agent_engine.record_manual_event", new=AsyncMock()), \
         patch("backend.api.routes.orders_helpers._record_rejection",
               return_value=1) as mock_record, \
         patch("backend.api.routes.orders_helpers._maybe_send_breaker_alert") as mock_breaker:
        with pytest.raises(Exception):
            await m._ticket_handle_live_place_error(
                _data_stub(), "ZG0790", "NIFTY25JULFUT", "BUY", 50,
                "LIMIT", exc, "ZG0790:NFO:NIFTY25JULFUT",
            )

    mock_record.assert_called_once_with("ZG0790:NFO:NIFTY25JULFUT"), (
        "a genuine broker rejection must still be recorded against the "
        "circuit breaker — this fix is specific to the client-side "
        "confirm-timeout exception type, not a blanket loosening."
    )
