"""
Regression test for audit finding O2 (Wave 1, cluster 1B).

`_ch_exhaust_max_attempts` and `_ch_abort_stuck` both run a final
cancel-and-confirm read (`_ch_final_cancel_and_confirm`) before deciding
the chase's terminal outcome. Pre-fix, neither checked whether that final
read revealed the order had actually reached full fill
(`cumulative_filled >= quantity`) before unconditionally recording the
chase as FAILED/`chase_unfilled` (exhaustion) or FAILED/`chase_unfilled`-
or-`chase_cancel_failed` (stuck-abort). The in-loop path
(`_ch_cancel_and_capture` -> `_ch_zero_remaining_fill`) already has this
exact check — these two abort paths must apply the identical check so a
late full fill is recorded as FILLED (firing template-attach/auto-TP via
`_emit_chase_terminal(..., "chase_fill", ...)`), not UNFILLED. Critical
for Dhan/Groww, which lack reliable postback self-healing.
"""
from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

import backend.api.algo.chase as chase
from backend.api.algo.chase import ChaseConfig, ChaseResult, ChaseStatus


def _cfg(max_attempts=5, interval_seconds=1):
    return ChaseConfig(interval_seconds=interval_seconds, max_attempts=max_attempts,
                       exchange="NFO")


@pytest.mark.asyncio
async def test_exhaust_max_attempts_records_fill_on_late_full_fill():
    """A final cancel-confirm read revealing cumulative_filled == quantity
    must finalize the chase as FILLED (chase_fill), not FAILED/chase_unfilled."""
    result = ChaseResult()
    emit = MagicMock()
    mock_terminal = AsyncMock()
    mock_alert = MagicMock()
    with patch.object(chase, "_ch_final_cancel_and_confirm",
                      AsyncMock(return_value=(True, 50, 50, 105.0))), \
         patch.object(chase, "_ch_write_order_event", AsyncMock()), \
         patch.object(chase, "_emit_chase_terminal", mock_terminal), \
         patch.object(chase, "_ch_mark_ambiguous_abort") as mock_mark, \
         patch("backend.shared.helpers.alert_utils.send_order_failure_alert", mock_alert):
        out = await chase._ch_exhaust_max_attempts(
            result, "OID1", _cfg(max_attempts=5),
            "ZG0790", "NIFTY25JULFUT", "BUY", 50, algo_order_id=11, emit=emit,
        )
        # _ch_zero_remaining_fill schedules the terminal write via
        # asyncio.create_task (fire-and-forget) — yield once so it runs.
        await asyncio.sleep(0)

    assert out.status == ChaseStatus.FILLED, (
        "a late full fill caught by the final cancel-confirm read at "
        "max-attempts exhaustion must be recorded as FILLED, not FAILED."
    )
    mock_terminal.assert_awaited_once()
    args = mock_terminal.call_args.args
    assert args[1] == "chase_fill", (
        "terminal outcome must be chase_fill (-> FILLED, fires "
        "template-attach/auto-TP), not chase_unfilled."
    )
    mock_alert.assert_not_called(), "a genuine full fill must not fire a failure alert"
    mock_mark.assert_not_called(), "a confirmed full fill is not an ambiguous abort"


@pytest.mark.asyncio
async def test_abort_stuck_records_fill_on_late_full_fill():
    """Same invariant for the stuck-abort path."""
    result = ChaseResult()
    emit = MagicMock()
    mock_terminal = AsyncMock()
    mock_alert = MagicMock()
    with patch.object(chase, "_ch_final_cancel_and_confirm",
                      AsyncMock(return_value=(True, 75, 75, 210.5))), \
         patch.object(chase, "_emit_chase_terminal", mock_terminal), \
         patch.object(chase, "_ch_mark_ambiguous_abort") as mock_mark, \
         patch("backend.shared.helpers.alert_utils.send_order_failure_alert", mock_alert):
        out = await chase._ch_abort_stuck(
            result, "ZG0790", "OID2", _cfg(max_attempts=5),
            "NIFTY25JULFUT", "SELL", 75,
            cumulative_filled=0, current_order_filled=0,
            algo_order_id=22, emit=emit, attempt=7,
            elapsed=200.0, ceiling=60.0,
        )
        await asyncio.sleep(0)

    assert out.status == ChaseStatus.FILLED, (
        "a late full fill caught by the final cancel-confirm read at "
        "stuck-abort must be recorded as FILLED, not FAILED."
    )
    mock_terminal.assert_awaited_once()
    args = mock_terminal.call_args.args
    assert args[1] == "chase_fill"
    mock_alert.assert_not_called()
    mock_mark.assert_not_called()
