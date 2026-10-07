"""
Tests for the 2026-09 council audit fix (risk lens) — chase cancel
confirmation, closing a silent duplicate-live-order exposure.

Background: `_ch_cancel_previous` swallows any `broker.cancel_order`
exception with only a warning log — no signal of whether the cancel
actually landed. The caller (`chase_order`'s main loop) then
unconditionally placed a FRESH replacement order sized at the full
`remaining_qty`, abandoning the old `current_order_id` entirely. If the
cancel had silently failed (the old order still resting live at the
broker), this produced TWO live orders for the same leg — the old one
never polled or reconciled again once `current_order_id` moved to the
new order — capable of independently filling for up to 2x the intended
position, with no alert.

Fix: `_ch_capture_late_fill` (already doing a post-cancel status read
for late-fill capture) now ALSO checks whether that status is genuinely
terminal (`_CH_CONFIRMED_GONE_STATUSES`) and returns a `cancel_confirmed`
bool. `_ch_cancel_and_capture` uses it: if NOT confirmed and there's
still a nonzero remaining_qty, the chase is safely ABORTED (no
replacement order placed) with a CRITICAL log + urgent ntfy alert,
instead of blindly proceeding.

Separately: `_ch_exhaust_max_attempts` (a chase giving up after
max_attempts) previously fired no operator alert at all, unlike its
sibling `_chase_abort_on_consecutive_errors` — fixed to alert
consistently, and to note explicitly when its own final cancel attempt
may have also failed.

Five quality dimensions:
  1. SSOT   — tests the real functions, not reimplementations.
  2. Perf   — pure unit / mocked broker calls, no real network.
  3. Stale  — directly reproduces the reported duplicate-order shape
              (cancel silently fails -> would-be replacement order) and
              proves the fix prevents it.
  4. Reuse  — same _run/patch mocking shape as test_chase_fill_accounting.py.
  5. UX     — the alert must actually fire (not just log) since this is
              exactly the scenario needing a human to check the broker's
              order book.
"""
from __future__ import annotations

import pytest
from unittest.mock import AsyncMock, MagicMock, patch


# ── _ch_capture_late_fill: cancel_confirmed determination ─────────────────

class TestCancelConfirmedDetection:
    @pytest.mark.asyncio
    async def test_cancelled_status_is_confirmed(self):
        from backend.api.algo.chase import _ch_capture_late_fill, ChaseConfig
        cfg = ChaseConfig(exchange="NFO")

        async def _fake_run(fn, *args):
            return {"status": "CANCELLED", "filled_quantity": 40, "average_price": 100.0}

        with patch("backend.api.algo.chase._run", side_effect=_fake_run), \
             patch("backend.api.algo.chase._record_partial_fill", new_callable=AsyncMock):
            *_, cancel_confirmed = await _ch_capture_late_fill(
                account="ACC1", order_id="O1", cfg=cfg, symbol="NIFTY24DECFUT",
                quantity=100, cumulative_filled=40, current_order_filled=40,
                algo_order_id=None,
            )
        assert cancel_confirmed is True

    @pytest.mark.asyncio
    async def test_complete_status_is_confirmed(self):
        """COMPLETE means the order is gone (filled before cancel could
        land) — equally safe to proceed as CANCELLED."""
        from backend.api.algo.chase import _ch_capture_late_fill, ChaseConfig
        cfg = ChaseConfig(exchange="NFO")

        async def _fake_run(fn, *args):
            return {"status": "COMPLETE", "filled_quantity": 100, "average_price": 100.0}

        with patch("backend.api.algo.chase._run", side_effect=_fake_run), \
             patch("backend.api.algo.chase._record_partial_fill", new_callable=AsyncMock):
            *_, cancel_confirmed = await _ch_capture_late_fill(
                account="ACC1", order_id="O1", cfg=cfg, symbol="NIFTY24DECFUT",
                quantity=100, cumulative_filled=40, current_order_filled=40,
                algo_order_id=None,
            )
        assert cancel_confirmed is True

    @pytest.mark.asyncio
    async def test_still_open_status_is_not_confirmed(self):
        """THE bug this fixes: cancel_order() raised no exception (or was
        never actually called), but the broker still reports the order
        as OPEN -- it never actually cancelled. Must be False."""
        from backend.api.algo.chase import _ch_capture_late_fill, ChaseConfig
        cfg = ChaseConfig(exchange="NFO")

        async def _fake_run(fn, *args):
            return {"status": "OPEN", "filled_quantity": 40, "average_price": 100.0}

        with patch("backend.api.algo.chase._run", side_effect=_fake_run), \
             patch("backend.api.algo.chase._record_partial_fill", new_callable=AsyncMock):
            *_, cancel_confirmed = await _ch_capture_late_fill(
                account="ACC1", order_id="O1", cfg=cfg, symbol="NIFTY24DECFUT",
                quantity=100, cumulative_filled=40, current_order_filled=40,
                algo_order_id=None,
            )
        assert cancel_confirmed is False

    @pytest.mark.asyncio
    async def test_status_read_failure_fails_safe_not_confirmed(self):
        """A broker error on the status read itself must NOT be treated
        as confirmed -- fail-safe (assume it might still be resting)."""
        from backend.api.algo.chase import _ch_capture_late_fill, ChaseConfig
        cfg = ChaseConfig(exchange="NFO")

        async def _fake_run(fn, *args):
            raise RuntimeError("broker unavailable")

        with patch("backend.api.algo.chase._run", side_effect=_fake_run):
            *_, cancel_confirmed = await _ch_capture_late_fill(
                account="ACC1", order_id="O1", cfg=cfg, symbol="NIFTY24DECFUT",
                quantity=100, cumulative_filled=40, current_order_filled=40,
                algo_order_id=None,
            )
        assert cancel_confirmed is False

    @pytest.mark.asyncio
    async def test_empty_status_dict_is_not_confirmed(self):
        from backend.api.algo.chase import _ch_capture_late_fill, ChaseConfig
        cfg = ChaseConfig(exchange="NFO")

        async def _fake_run(fn, *args):
            return {}

        with patch("backend.api.algo.chase._run", side_effect=_fake_run), \
             patch("backend.api.algo.chase._record_partial_fill", new_callable=AsyncMock):
            *_, cancel_confirmed = await _ch_capture_late_fill(
                account="ACC1", order_id="O1", cfg=cfg, symbol="NIFTY24DECFUT",
                quantity=100, cumulative_filled=40, current_order_filled=40,
                algo_order_id=None,
            )
        assert cancel_confirmed is False


# ── _ch_cancel_and_capture: abort-on-unconfirmed wiring ────────────────────

class TestCancelAndCaptureAbortsOnUnconfirmed:
    @pytest.mark.asyncio
    async def test_unconfirmed_cancel_with_remaining_qty_aborts_no_replacement(self):
        """THE core fix, end-to-end through the wrapper: cancel appears
        to succeed (no exception) but the post-cancel status shows OPEN
        (still resting) -- must return an early FAILED result instead of
        None, so the caller NEVER reaches the replacement-order placement
        code."""
        from backend.api.algo.chase import _ch_cancel_and_capture, ChaseResult, ChaseStatus, ChaseConfig

        cfg = ChaseConfig(exchange="NFO")
        result = ChaseResult()

        async def _fake_run(fn, *args):
            # _cancel_order call: succeeds silently (no exception).
            # _order_status call: still OPEN -- cancel didn't actually land.
            if fn.__name__ == "_order_status" or "status" in getattr(fn, "__name__", ""):
                return {"status": "OPEN", "filled_quantity": 40, "average_price": 100.0}
            return None

        with patch("backend.api.algo.chase._run", side_effect=_fake_run), \
             patch("backend.api.algo.chase._record_partial_fill", new_callable=AsyncMock), \
             patch("backend.api.algo.chase.logger") as mock_log:
            cumulative, current_filled, remaining, early = await _ch_cancel_and_capture(
                account="ACC1", current_order_id="O1", cfg=cfg, symbol="NIFTY24DECFUT",
                attempt=2, emit=lambda *a, **kw: None,
                quantity=100, remaining_qty=60,
                cumulative_filled=40, current_order_filled=40,
                algo_order_id=None, result=result, transaction_type="BUY",
            )

        assert early is not None, (
            "an unconfirmed cancel with remaining_qty > 0 MUST return a "
            "non-None early_result so the caller never places a "
            "replacement order alongside a possibly-still-live old one"
        )
        assert early.status == ChaseStatus.FAILED
        assert "not confirmed" in early.detail.lower() or "unconfirmed" in early.detail.lower()
        mock_log.critical.assert_called_once()
        extra = mock_log.critical.call_args.kwargs["extra"]
        assert extra["tags"] == ["chase"]
        assert extra["alert_event"] == "cancel_unconfirmed"

    @pytest.mark.asyncio
    async def test_confirmed_cancel_with_remaining_qty_proceeds_normally(self):
        """Sanity check: a GENUINELY confirmed cancel must still allow
        the normal flow (early_result=None, caller proceeds to place the
        replacement order) -- the fix must not be overly conservative."""
        from backend.api.algo.chase import _ch_cancel_and_capture, ChaseResult, ChaseConfig

        cfg = ChaseConfig(exchange="NFO")
        result = ChaseResult()

        async def _fake_run(fn, *args):
            return {"status": "CANCELLED", "filled_quantity": 40, "average_price": 100.0}

        with patch("backend.api.algo.chase._run", side_effect=_fake_run), \
             patch("backend.api.algo.chase._record_partial_fill", new_callable=AsyncMock):
            cumulative, current_filled, remaining, early = await _ch_cancel_and_capture(
                account="ACC1", current_order_id="O1", cfg=cfg, symbol="NIFTY24DECFUT",
                attempt=2, emit=lambda *a, **kw: None,
                quantity=100, remaining_qty=60,
                cumulative_filled=40, current_order_filled=40,
                algo_order_id=None, result=result, transaction_type="BUY",
            )
        assert early is None, "a confirmed cancel must NOT abort the chase"

    @pytest.mark.asyncio
    async def test_no_previous_order_never_evaluates_confirmation(self):
        """The very first attempt (current_order_id=None, nothing to
        cancel yet) must proceed unaffected — no status read, no
        confirmation gate applies."""
        from backend.api.algo.chase import _ch_cancel_and_capture, ChaseResult, ChaseConfig

        cfg = ChaseConfig(exchange="NFO")
        result = ChaseResult()

        with patch("backend.api.algo.chase._run") as mock_run:
            cumulative, current_filled, remaining, early = await _ch_cancel_and_capture(
                account="ACC1", current_order_id=None, cfg=cfg, symbol="NIFTY24DECFUT",
                attempt=1, emit=lambda *a, **kw: None,
                quantity=100, remaining_qty=100,
                cumulative_filled=0, current_order_filled=0,
                algo_order_id=None, result=result, transaction_type="BUY",
            )
        assert early is None
        mock_run.assert_not_called()


# ── _ch_exhaust_max_attempts: alert now fires ──────────────────────────────

class TestExhaustMaxAttemptsAlerts:
    @pytest.mark.asyncio
    async def test_alert_fires_on_max_attempts_exhaustion(self):
        from backend.api.algo.chase import _ch_exhaust_max_attempts, ChaseResult, ChaseConfig

        cfg = ChaseConfig(exchange="NFO", max_attempts=5)
        result = ChaseResult()

        with patch("backend.api.algo.chase._run", new_callable=AsyncMock), \
             patch("backend.shared.helpers.alert_utils.send_order_failure_alert") as mock_alert:
            await _ch_exhaust_max_attempts(
                result=result, current_order_id="O1", cfg=cfg,
                account="ACC1", symbol="NIFTY24DECFUT", transaction_type="BUY",
                quantity=100, algo_order_id=None, emit=lambda *a, **kw: None,
            )
        mock_alert.assert_called_once()

    @pytest.mark.asyncio
    async def test_detail_notes_when_final_cancel_also_failed(self):
        from backend.api.algo.chase import _ch_exhaust_max_attempts, ChaseResult, ChaseConfig

        cfg = ChaseConfig(exchange="NFO", max_attempts=5)
        result = ChaseResult()

        async def _fake_run(fn, *args):
            raise RuntimeError("cancel rejected")

        with patch("backend.api.algo.chase._run", side_effect=_fake_run), \
             patch("backend.shared.helpers.alert_utils.send_order_failure_alert") as mock_alert:
            await _ch_exhaust_max_attempts(
                result=result, current_order_id="O1", cfg=cfg,
                account="ACC1", symbol="NIFTY24DECFUT", transaction_type="BUY",
                quantity=100, algo_order_id=None, emit=lambda *a, **kw: None,
            )
        assert "may have failed" in result.detail.lower() or "may still be resting" in result.detail.lower()
        # The alert message itself should also carry this detail.
        alert_kwargs = mock_alert.call_args.kwargs
        assert "cancel" in alert_kwargs.get("error", "").lower()
