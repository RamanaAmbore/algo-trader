"""Chase: a cancel that is not final on the first status read is re-read before aborting."""
import asyncio
from unittest.mock import AsyncMock, MagicMock

import pytest

import backend.api.algo.chase as chase


def _run(coro):
    return asyncio.run(coro)


def test_confirms_on_retry_and_does_not_abort(monkeypatch):
    reads = [
        (0, 0, 3, 0.0, False),   # first read: not final yet
        (0, 0, 3, 0.0, True),    # second read: CANCELLED
    ]
    monkeypatch.setattr(chase, "_ch_capture_late_fill", AsyncMock(side_effect=reads))
    monkeypatch.setattr(chase, "_ch_cancel_previous", AsyncMock(return_value=None))
    monkeypatch.setattr(chase, "_ch_write_cancel_confirmed_event", AsyncMock(return_value=None))
    monkeypatch.setattr(chase.asyncio, "sleep", AsyncMock(return_value=None))
    abort = MagicMock(return_value="ABORT")
    monkeypatch.setattr(chase, "_ch_build_cancel_unconfirmed_abort", abort)

    out = _run(chase._ch_cancel_and_capture(
        "ZJ6294", "2107429612501262336", MagicMock(), "CRUDEOIL26OCT8750CE", 1,
        MagicMock(), 3, 3, 0, 0, 1, MagicMock(), "BUY",
    ))
    abort.assert_not_called()
    assert out[3] is None


def test_still_unconfirmed_after_all_reads_aborts(monkeypatch):
    capture = AsyncMock(return_value=(0, 0, 3, 0.0, False))
    sleep = AsyncMock(return_value=None)
    monkeypatch.setattr(chase, "_ch_capture_late_fill", capture)
    monkeypatch.setattr(chase, "_ch_cancel_previous", AsyncMock(return_value=None))
    monkeypatch.setattr(chase.asyncio, "sleep", sleep)
    abort = MagicMock(return_value="ABORT")
    monkeypatch.setattr(chase, "_ch_build_cancel_unconfirmed_abort", abort)

    out = _run(chase._ch_cancel_and_capture(
        "ZJ6294", "2107429612501262336", MagicMock(), "CRUDEOIL26OCT8750CE", 1,
        MagicMock(), 3, 3, 0, 0, 1, MagicMock(), "BUY",
    ))
    assert abort.call_count == 1
    assert out[3] == "ABORT"
    # Bounded retry — fixed number of reads (_CH_CANCEL_CONFIRM_READS),
    # never an unbounded loop, with one fewer sleep than reads.
    assert capture.call_count == chase._CH_CANCEL_CONFIRM_READS
    assert sleep.call_count == chase._CH_CANCEL_CONFIRM_READS - 1


def test_capture_late_fill_read_failure_is_fail_safe(monkeypatch):
    """A broker status-read exception must degrade cancel_confirmed to
    False (fail-safe), never raise and never silently default to True."""
    run_mock = AsyncMock(side_effect=RuntimeError("broker timeout"))
    monkeypatch.setattr(chase, "_run", run_mock)

    cumulative_filled, current_order_filled, remaining_qty, avg_price, cancel_confirmed = _run(
        chase._ch_capture_late_fill(
            "ZJ6294", "2107429612501262336", MagicMock(), "CRUDEOIL26OCT8750CE",
            3, 0, 0, 1,
        )
    )
    assert cancel_confirmed is False
    assert remaining_qty == 3


def test_cancel_and_capture_aborts_on_read_failure(monkeypatch):
    """The real _ch_capture_late_fill (status read raising on every
    attempt) must still drive _ch_cancel_and_capture to the abort path —
    the read-failure case is unchanged by the retry fix."""
    run_mock = AsyncMock(side_effect=RuntimeError("broker timeout"))
    monkeypatch.setattr(chase, "_run", run_mock)
    monkeypatch.setattr(chase, "_ch_cancel_previous", AsyncMock(return_value=None))
    monkeypatch.setattr(chase.asyncio, "sleep", AsyncMock(return_value=None))
    abort = MagicMock(return_value="ABORT")
    monkeypatch.setattr(chase, "_ch_build_cancel_unconfirmed_abort", abort)

    out = _run(chase._ch_cancel_and_capture(
        "ZJ6294", "2107429612501262336", MagicMock(), "CRUDEOIL26OCT8750CE", 1,
        MagicMock(), 3, 3, 0, 0, 1, MagicMock(), "BUY",
    ))
    assert abort.call_count == 1
    assert out[3] == "ABORT"
