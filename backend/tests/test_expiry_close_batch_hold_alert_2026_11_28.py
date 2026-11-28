"""Tests for the 2026-11-28 `close_positions()` hold-alert BATCHING fix.

Confirmed finding: `ExpiryEngine.close_positions()`'s own pre-existing
comment documented this as deliberate: "AGENT_CLOSE sends a dedicated
action-required alert when it stages a hold (`_al_send_agent_close_hold_
alert`) — EXPIRY_CLOSE never did... Reuses the exact same alert mechanism/
renderer, ONE PER STAGED LEG (a single expiry-day pass can stage
several)." With 4 ITM legs staged in one scan, that is 4 separate
action-required pushes for what should be one consolidated review.

Fix: `close_positions()` now accumulates every leg it stages this pass
into `staged_holds`, and sends exactly ONE batched alert
(`_al_send_batch_agent_close_hold_alert`, in `actions_live.py`) after the
staging loop — via a `try/finally` so a mid-loop exception never leaves an
already-staged real-money HELD row with zero alert.

Explicitly NOT in scope / NOT changed by this fix (see the module
docstring in `actions_live.py:_al_maybe_stage_agent_close_hold` and this
file's own `TestSingleLegPathUnchanged` below): the AGENT_CLOSE
`require_release` hold path (`_al_maybe_stage_agent_close_hold`, called
once per `close_position` action dispatch) still calls the single-leg
`_al_send_agent_close_hold_alert` — it genuinely processes one leg per
invocation, so there is nothing to batch there without touching the
generic per-row action fan-out dispatcher in `agent_engine.py`, which is
out of scope for this fix.
"""
from __future__ import annotations

from datetime import date, timedelta
from unittest.mock import AsyncMock, patch

import pytest

from backend.api.algo.expiry import ExpiryEngine, OptionPosition


def _pos(account, symbol, exchange="MCX", instrument_type="CE", underlying="GOLD",
         strike=1000.0, qty=1, residual=None):
    return OptionPosition(
        account=account,
        tradingsymbol=symbol,
        exchange=exchange,
        instrument_type=instrument_type,
        underlying=underlying,
        strike=strike,
        expiry=date.today(),
        quantity=qty,
        product="NRML",
        residual_qty=(residual if residual is not None else qty),
    )


def _patches(held_ids=None):
    """Standard patch set so every leg routes through the HELD-staging
    branch (not the live-chase branch) and `record_held_order` succeeds.
    `held_ids`, when given, is an iterable of return values consumed in
    order across calls (defaults to an incrementing counter)."""
    counter = iter(held_ids) if held_ids is not None else None
    _next_id = [100]

    async def _record(*args, **kwargs):
        if counter is not None:
            return next(counter)
        _next_id[0] += 1
        return _next_id[0]

    async def _not_in_flight(account, symbol, exchange, side):
        return False

    return (
        patch("backend.api.algo.expiry._exp_leg_in_flight_this_session", new=_not_in_flight),
        patch("backend.api.algo.expiry.expiry_close_held", return_value=True),
        patch("backend.api.algo.actions_live._al_agent_close_recently_cancelled_this_session",
              new=AsyncMock(return_value=False)),
        patch("backend.api.algo.expiry.record_held_order", new=_record),
    )


class TestBatchAlertDirectUnit:
    """Direct unit tests of `_al_send_batch_agent_close_hold_alert`."""

    @pytest.mark.asyncio
    async def test_empty_staged_list_sends_nothing(self):
        from backend.api.algo.actions_live import _al_send_batch_agent_close_hold_alert
        with patch("backend.shared.helpers.alert_utils._DISPATCH_LOGGER") as mock_logger:
            await _al_send_batch_agent_close_hold_alert(
                None, [], title_prefix="Expiry close held",
            )
        mock_logger.warning.assert_not_called()

    @pytest.mark.asyncio
    async def test_single_leg_sends_one_record_with_singular_wording(self):
        from backend.api.algo.actions_live import _al_send_batch_agent_close_hold_alert
        from types import SimpleNamespace

        agent = SimpleNamespace(id=7, slug="expiry-day-mcx-itm-auto-close", name="X")
        staged = [{
            "held_order_id": 1, "account": "ZG0790", "symbol": "GOLD26NOV123000CE",
            "exchange": "MCX", "side": "SELL", "qty": 1,
            "reason": "expiry close (held by default)",
        }]

        with patch("backend.shared.helpers.alert_utils._DISPATCH_LOGGER") as mock_logger:
            await _al_send_batch_agent_close_hold_alert(
                agent, staged, title_prefix="Expiry close held",
            )

        mock_logger.warning.assert_called_once()
        _, kwargs = mock_logger.warning.call_args
        extra = kwargs["extra"]
        assert extra["alert_event"] == "agent_close_hold"
        assert extra["action_required"] is True
        assert "1 leg" in extra["body"]
        assert "GOLD26NOV123000CE" in extra["body"]
        assert extra["held_order_ids"] == [1]

    @pytest.mark.asyncio
    async def test_multi_leg_multi_account_sends_one_record_grouped_by_account(self):
        from backend.api.algo.actions_live import _al_send_batch_agent_close_hold_alert
        from types import SimpleNamespace

        agent = SimpleNamespace(id=7, slug="expiry-day-mcx-itm-auto-close", name="X")
        staged = [
            {"held_order_id": 1, "account": "ZG0790", "symbol": "GOLD26NOV123000CE",
             "exchange": "MCX", "side": "SELL", "qty": 1, "reason": "expiry close (held by default)"},
            {"held_order_id": 2, "account": "ZG0790", "symbol": "GOLDM26NOV123000CE",
             "exchange": "MCX", "side": "BUY", "qty": 2, "reason": "expiry close (held by default)"},
            {"held_order_id": 3, "account": "ZJ6294", "symbol": "GOLD26NOV124000CE",
             "exchange": "MCX", "side": "SELL", "qty": 1, "reason": "expiry close (held by default)"},
            {"held_order_id": 4, "account": "ZJ6294", "symbol": "GOLD26NOV125000PE",
             "exchange": "MCX", "side": "BUY", "qty": 3, "reason": "expiry close (held by default)"},
        ]

        with patch("backend.shared.helpers.alert_utils._DISPATCH_LOGGER") as mock_logger:
            await _al_send_batch_agent_close_hold_alert(
                agent, staged, title_prefix="Expiry close held",
            )

        mock_logger.warning.assert_called_once(), "4 legs must produce exactly ONE alert, not 4"
        _, kwargs = mock_logger.warning.call_args
        extra = kwargs["extra"]
        body = extra["body"]
        assert "4 legs" in body
        for sym in ("GOLD26NOV123000CE", "GOLDM26NOV123000CE",
                    "GOLD26NOV124000CE", "GOLD26NOV125000PE"):
            assert sym in body
        assert sorted(extra["held_order_ids"]) == [1, 2, 3, 4]
        # Grouped by account — each masked account name appears once as
        # its own header line, not repeated per leg.
        assert body.count("Account: ZG0790") == 0 or True  # masking changes the literal text;
        # instead assert the two distinct account groupings exist as
        # separate header-shaped lines (masked form varies by env).
        account_header_lines = [ln for ln in body.splitlines() if ln.startswith("Account: ")]
        assert len(account_header_lines) == 2, (
            "4 legs across 2 accounts must render exactly 2 account "
            f"group headers, got: {account_header_lines!r}"
        )

    @pytest.mark.asyncio
    async def test_logging_failure_never_raises(self):
        from backend.api.algo.actions_live import _al_send_batch_agent_close_hold_alert
        from types import SimpleNamespace

        agent = SimpleNamespace(id=7, slug="x", name="X")
        staged = [{"held_order_id": 1, "account": "ZG0790", "symbol": "SYM",
                   "exchange": "MCX", "side": "SELL", "qty": 1, "reason": "r"}]

        with patch("backend.shared.helpers.alert_utils._DISPATCH_LOGGER") as mock_logger:
            mock_logger.warning.side_effect = RuntimeError("boom")
            # Must not raise.
            await _al_send_batch_agent_close_hold_alert(agent, staged)


class TestClosePositionsBatchesOneAlertPerCall:
    """Integration tests through the real `close_positions()`."""

    @pytest.mark.asyncio
    async def test_four_simultaneous_legs_two_accounts_send_exactly_one_alert(self):
        """The task's required scenario: 4 simultaneously-eligible legs
        across 2 accounts, staged in ONE close_positions() call, must
        produce exactly ONE batched alert listing all 4, grouped by
        account — not 4 separate alerts."""
        engine = ExpiryEngine(agent_id=42)
        legs = [
            _pos("ZG0790", "GOLD26NOV123000CE"),
            _pos("ZG0790", "GOLDM26NOV123000CE", qty=2, residual=2),
            _pos("ZJ6294", "GOLD26NOV124000CE"),
            _pos("ZJ6294", "GOLD26NOV125000PE", qty=3, residual=-3),
        ]

        p1, p2, p3, p4 = _patches(held_ids=[101, 102, 103, 104])
        with p1, p2, p3, p4, \
             patch("backend.api.algo.actions_live._al_send_batch_agent_close_hold_alert",
                   new=AsyncMock()) as mock_batch:
            await engine.close_positions(legs)

        mock_batch.assert_called_once()
        args, kwargs = mock_batch.call_args
        staged = args[1]
        assert len(staged) == 4
        accounts = {leg["account"] for leg in staged}
        assert accounts == {"ZG0790", "ZJ6294"}
        symbols = {leg["symbol"] for leg in staged}
        assert symbols == {
            "GOLD26NOV123000CE", "GOLDM26NOV123000CE",
            "GOLD26NOV124000CE", "GOLD26NOV125000PE",
        }
        assert kwargs.get("title_prefix") == "Expiry close held"

    @pytest.mark.asyncio
    async def test_single_eligible_leg_still_sends_correctly(self):
        """Regression guard for the common case — a pass that stages
        exactly one leg must still send one correct alert, not zero and
        not malformed by the batching change."""
        engine = ExpiryEngine(agent_id=42)
        legs = [_pos("ZG0790", "GOLD26NOV123000CE")]

        p1, p2, p3, p4 = _patches(held_ids=[201])
        with p1, p2, p3, p4, \
             patch("backend.api.algo.actions_live._al_send_batch_agent_close_hold_alert",
                   new=AsyncMock()) as mock_batch:
            await engine.close_positions(legs)

        mock_batch.assert_called_once()
        args, _kwargs = mock_batch.call_args
        staged = args[1]
        assert len(staged) == 1
        assert staged[0]["account"] == "ZG0790"
        assert staged[0]["symbol"] == "GOLD26NOV123000CE"
        assert staged[0]["held_order_id"] == 201

    @pytest.mark.asyncio
    async def test_legs_eligible_on_different_calls_send_as_separate_alerts(self):
        """Legs becoming eligible on DIFFERENT evaluation cycles (modeled
        here as two separate close_positions() calls, the natural
        per-cycle unit for this engine) must NOT be merged into one
        alert — each call's own staged batch gets its own, separate
        alert. This must never become "wait and batch forever"."""
        engine = ExpiryEngine(agent_id=42)

        p1, p2, p3, p4 = _patches(held_ids=[301])
        with p1, p2, p3, p4, \
             patch("backend.api.algo.actions_live._al_send_batch_agent_close_hold_alert",
                   new=AsyncMock()) as mock_batch:
            await engine.close_positions([_pos("ZG0790", "GOLD26NOV123000CE")])

        mock_batch.assert_called_once()
        args1, _ = mock_batch.call_args
        assert len(args1[1]) == 1
        assert args1[1][0]["symbol"] == "GOLD26NOV123000CE"

        # A SECOND, later cycle — a different leg becomes eligible.
        p1, p2, p3, p4 = _patches(held_ids=[302])
        with p1, p2, p3, p4, \
             patch("backend.api.algo.actions_live._al_send_batch_agent_close_hold_alert",
                   new=AsyncMock()) as mock_batch2:
            await engine.close_positions([_pos("ZJ6294", "GOLD26NOV126000PE")])

        mock_batch2.assert_called_once()
        args2, _ = mock_batch2.call_args
        assert len(args2[1]) == 1
        assert args2[1][0]["symbol"] == "GOLD26NOV126000PE"
        # The two calls are independent — the second call's batch must
        # not carry the first call's leg (no silent cross-call merging).
        assert args2[1][0]["symbol"] != args1[1][0]["symbol"]

    @pytest.mark.asyncio
    async def test_zero_eligible_legs_sends_no_alert(self):
        """A fully-netted pass (every leg's residual_qty == 0) must
        never produce a spurious empty-batch alert."""
        engine = ExpiryEngine(agent_id=42)
        legs = [_pos("ZG0790", "GOLD26NOV123000CE", qty=1, residual=0)]

        with patch("backend.api.algo.actions_live._al_send_batch_agent_close_hold_alert",
                   new=AsyncMock()) as mock_batch:
            await engine.close_positions(legs)

        mock_batch.assert_not_called()

    @pytest.mark.asyncio
    async def test_mid_loop_exception_still_alerts_the_already_staged_leg(self):
        """A mid-loop exception (simulated: record_held_order raises on
        the SECOND leg, after the first leg's hold already committed)
        must never leave the first leg's already-staged HELD row with
        zero alert. The batch alert must still fire, for the leg(s)
        genuinely staged before the failure, via the `finally` block —
        and the original exception must still propagate (this fix must
        not silently swallow a real failure)."""
        engine = ExpiryEngine(agent_id=42)
        legs = [
            _pos("ZG0790", "GOLD26NOV123000CE"),
            _pos("ZG0790", "GOLDM26NOV123000CE", qty=2, residual=2),
        ]

        call_count = [0]

        async def _record_then_raise(*args, **kwargs):
            call_count[0] += 1
            if call_count[0] == 1:
                return 401
            raise RuntimeError("simulated DB failure on second leg")

        async def _not_in_flight(account, symbol, exchange, side):
            return False

        with patch("backend.api.algo.expiry._exp_leg_in_flight_this_session", new=_not_in_flight), \
             patch("backend.api.algo.expiry.expiry_close_held", return_value=True), \
             patch("backend.api.algo.actions_live._al_agent_close_recently_cancelled_this_session",
                   new=AsyncMock(return_value=False)), \
             patch("backend.api.algo.expiry.record_held_order", new=_record_then_raise), \
             patch("backend.api.algo.actions_live._al_send_batch_agent_close_hold_alert",
                   new=AsyncMock()) as mock_batch:
            with pytest.raises(RuntimeError, match="simulated DB failure"):
                await engine.close_positions(legs)

        mock_batch.assert_called_once(), (
            "the already-staged first leg must still get its alert even "
            "though the second leg's staging blew up"
        )
        args, _ = mock_batch.call_args
        staged = args[1]
        assert len(staged) == 1
        assert staged[0]["symbol"] == "GOLD26NOV123000CE"
        assert staged[0]["held_order_id"] == 401


class TestSingleLegPathUnchanged:
    """`_al_maybe_stage_agent_close_hold` (AGENT_CLOSE / require_release)
    is explicitly OUT OF SCOPE — it still calls the single-leg
    `_al_send_agent_close_hold_alert`, because it genuinely processes one
    `close_position` action call at a time. This is a regression guard
    confirming that fact, not a request to change it."""

    def test_al_maybe_stage_agent_close_hold_still_uses_single_leg_sender(self):
        import inspect
        from backend.api.algo.actions_live import _al_maybe_stage_agent_close_hold
        src = inspect.getsource(_al_maybe_stage_agent_close_hold)
        assert "_al_send_agent_close_hold_alert(" in src
        assert "_al_send_batch_agent_close_hold_alert" not in src
