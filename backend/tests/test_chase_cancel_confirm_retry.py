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
    monkeypatch.setattr(chase, "_ch_capture_late_fill",
                        AsyncMock(return_value=(0, 0, 3, 0.0, False)))
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
