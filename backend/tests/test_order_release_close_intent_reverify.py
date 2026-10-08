"""P1 fix (2026-10): `release_repeated_rejection_hold` must re-verify a
claimed `intent="close"` against the broker's CURRENT position before
resuming the chase — a repeated-rejection hold can sit for hours
awaiting operator review, and the operator may have flattened the
position some other way in that window. Resuming unconditionally would
place a FRESH opening order (wrong direction) with no size cap, since
`intent="close"` also bypasses the 50-lot adapter ceiling.

See `backend.api.algo.order_release._verify_close_still_valid` for the
re-verification helper and `release_repeated_rejection_hold`'s own
docstring for the wiring.
"""
from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pandas as pd
import pytest

from backend.api.algo.order_release import _verify_close_still_valid


# ── Unit tests for the helper itself ────────────────────────────────────

def _frame(symbol: str, exchange: str, quantity: int) -> "pd.DataFrame":
    return pd.DataFrame([{
        "tradingsymbol": symbol, "exchange": exchange, "quantity": quantity,
    }])


@pytest.mark.asyncio
async def test_verify_close_still_valid_true_when_nothing_remaining():
    ok, why = await _verify_close_still_valid("ZG0790", "X", "NFO", "SELL", 0)
    assert ok is True
    assert "nothing remaining" in why


@pytest.mark.asyncio
async def test_verify_close_still_valid_sell_ok_when_still_long_enough():
    frame = _frame("NIFTY24APR25000CE", "NFO", 40)
    with patch("backend.brokers.broker_apis.fetch_positions", return_value=[frame]):
        ok, why = await _verify_close_still_valid(
            "ZG0790", "NIFTY24APR25000CE", "NFO", "SELL", 30,
        )
    assert ok is True


@pytest.mark.asyncio
async def test_verify_close_still_valid_buy_ok_when_still_short_enough():
    frame = _frame("NIFTY24APR25000CE", "NFO", -40)
    with patch("backend.brokers.broker_apis.fetch_positions", return_value=[frame]):
        ok, why = await _verify_close_still_valid(
            "ZG0790", "NIFTY24APR25000CE", "NFO", "BUY", 30,
        )
    assert ok is True


@pytest.mark.asyncio
async def test_verify_close_still_valid_refuses_when_position_fully_closed():
    """The exact scenario Fix 1 targets: operator flattened the position
    manually some other way while the row sat HELD — net is now zero."""
    frame = _frame("NIFTY24APR25000CE", "NFO", 0)
    with patch("backend.brokers.broker_apis.fetch_positions", return_value=[frame]):
        ok, why = await _verify_close_still_valid(
            "ZG0790", "NIFTY24APR25000CE", "NFO", "SELL", 30,
        )
    assert ok is False
    assert "position no longer supports close" in why


@pytest.mark.asyncio
async def test_verify_close_still_valid_refuses_when_position_flipped():
    """Position flipped sign (was long, now short) — a SELL no longer closes it."""
    frame = _frame("NIFTY24APR25000CE", "NFO", -10)
    with patch("backend.brokers.broker_apis.fetch_positions", return_value=[frame]):
        ok, why = await _verify_close_still_valid(
            "ZG0790", "NIFTY24APR25000CE", "NFO", "SELL", 30,
        )
    assert ok is False


@pytest.mark.asyncio
async def test_verify_close_still_valid_refuses_when_remaining_exceeds_net():
    """Position reduced below the remaining qty to close — partial match
    is not enough; magnitude must cover the FULL remaining amount."""
    frame = _frame("NIFTY24APR25000CE", "NFO", 10)
    with patch("backend.brokers.broker_apis.fetch_positions", return_value=[frame]):
        ok, why = await _verify_close_still_valid(
            "ZG0790", "NIFTY24APR25000CE", "NFO", "SELL", 30,
        )
    assert ok is False


@pytest.mark.asyncio
async def test_verify_close_still_valid_fails_closed_on_stale_data():
    frame = _frame("NIFTY24APR25000CE", "NFO", 40)
    frame.attrs["stale"] = True
    with patch("backend.brokers.broker_apis.fetch_positions", return_value=[frame]):
        ok, why = await _verify_close_still_valid(
            "ZG0790", "NIFTY24APR25000CE", "NFO", "SELL", 30,
        )
    assert ok is False
    assert "stale" in why or "unavailable" in why


@pytest.mark.asyncio
async def test_verify_close_still_valid_fails_closed_on_fetch_exception():
    with patch("backend.brokers.broker_apis.fetch_positions",
               side_effect=RuntimeError("broker down")):
        ok, why = await _verify_close_still_valid(
            "ZG0790", "NIFTY24APR25000CE", "NFO", "SELL", 30,
        )
    assert ok is False
    assert "position check failed" in why


# ── Integration tests through release_repeated_rejection_hold ──────────

def _mock_session(row):
    _result = MagicMock()
    _result.scalar_one_or_none.return_value = row

    mock_session = AsyncMock()
    mock_session.__aenter__ = AsyncMock(return_value=mock_session)
    mock_session.__aexit__ = AsyncMock(return_value=False)
    mock_session.execute = AsyncMock(return_value=_result)
    mock_session.commit = AsyncMock()
    return mock_session


def _held_close_row(**extra) -> SimpleNamespace:
    from datetime import datetime, timezone
    from backend.api.algo.order_hold import HoldCategory, hold_record

    base = dict(
        id=901, status="HELD",
        hold_json=hold_record(
            HoldCategory.AGENT_ORDER, "repeated price rejection", "n/a", None,
            datetime.now(timezone.utc),
        ),
        detail="", account="ZG0790", symbol="NIFTY24APR25000CE", exchange="NFO",
        transaction_type="SELL", quantity=50, product="NRML",
        intent="close", filled_quantity=20,
    )
    base.update(extra)
    return SimpleNamespace(**base)


@pytest.mark.asyncio
async def test_release_refuses_close_intent_resume_when_position_already_flat():
    """Core P1 regression: a close-intent hold whose position has since
    gone to zero (operator flattened it manually while held) must refuse
    the release outright — chase_order is NEVER called, the chase-active
    mark is NEVER taken, and the row stays HELD with its hold_json intact."""
    from backend.api.algo import order_release as m

    row = _held_close_row()
    mock_session = _mock_session(row)
    flat_frame = _frame("NIFTY24APR25000CE", "NFO", 0)
    mock_chase_order = AsyncMock()
    mock_mark_active = MagicMock()
    mock_alert = MagicMock()

    with patch("backend.api.database.async_session", return_value=mock_session), \
         patch("backend.brokers.broker_apis.fetch_positions", return_value=[flat_frame]), \
         patch("backend.api.algo.order_events.write_event", new_callable=AsyncMock), \
         patch("backend.api.algo.chase.chase_order", mock_chase_order), \
         patch("backend.api.algo.chase._ch_mark_chase_active", mock_mark_active), \
         patch("backend.api.algo.chase._ch_mark_chase_inactive"), \
         patch("backend.shared.helpers.alert_utils.send_order_failure_alert", mock_alert):
        result = await m.release_repeated_rejection_hold(901, actor="operator")
        await asyncio.sleep(0)

    assert result["ok"] is False
    assert "position check failed" in result["reason"]
    assert result["status"] == "HELD"
    assert row.status == "HELD"
    assert row.hold_json is not None
    mock_chase_order.assert_not_called()
    mock_mark_active.assert_not_called()
    mock_alert.assert_called_once()


@pytest.mark.asyncio
async def test_release_refuses_close_intent_resume_when_position_flipped():
    """Same refusal, but the position flipped sign entirely instead of
    going flat — still must refuse, never resume."""
    from backend.api.algo import order_release as m

    row = _held_close_row()
    mock_session = _mock_session(row)
    flipped_frame = _frame("NIFTY24APR25000CE", "NFO", -15)
    mock_chase_order = AsyncMock()

    with patch("backend.api.database.async_session", return_value=mock_session), \
         patch("backend.brokers.broker_apis.fetch_positions", return_value=[flipped_frame]), \
         patch("backend.api.algo.order_events.write_event", new_callable=AsyncMock), \
         patch("backend.api.algo.chase.chase_order", mock_chase_order), \
         patch("backend.api.algo.chase._ch_mark_chase_active"), \
         patch("backend.api.algo.chase._ch_mark_chase_inactive"), \
         patch("backend.shared.helpers.alert_utils.send_order_failure_alert", MagicMock()):
        result = await m.release_repeated_rejection_hold(901, actor="operator")
        await asyncio.sleep(0)

    assert result["ok"] is False
    assert row.status == "HELD"
    mock_chase_order.assert_not_called()


@pytest.mark.asyncio
async def test_release_resumes_legitimate_close_intent_still_valid():
    """Regression guard: a close-intent hold whose position STILL
    supports the close (still long enough to cover the remaining qty)
    must resume normally, exactly as before this fix."""
    from backend.api.algo import order_release as m

    row = _held_close_row()
    mock_session = _mock_session(row)
    valid_frame = _frame("NIFTY24APR25000CE", "NFO", 40)  # remaining=30, net=40
    mock_chase_order = AsyncMock()

    with patch("backend.api.database.async_session", return_value=mock_session), \
         patch("backend.brokers.broker_apis.fetch_positions", return_value=[valid_frame]), \
         patch("backend.api.algo.order_events.write_event", new_callable=AsyncMock), \
         patch("backend.api.algo.chase.chase_order", mock_chase_order), \
         patch("backend.api.algo.chase._ch_mark_chase_active"), \
         patch("backend.api.algo.chase._ch_mark_chase_inactive"):
        result = await m.release_repeated_rejection_hold(901, actor="operator")
        await asyncio.sleep(0)

    assert result["ok"] is True
    assert row.status == "OPEN"
    assert row.hold_json is None
    mock_chase_order.assert_called_once()
    kw = mock_chase_order.call_args.kwargs
    assert kw["cfg"].intent == "close"
    assert kw["already_filled"] == 20


@pytest.mark.asyncio
async def test_release_plain_open_order_skips_close_reverify_entirely():
    """A plain OPEN order (intent=None) must NOT trigger the close
    re-verification at all — no broker position fetch happens."""
    from backend.api.algo import order_release as m

    row = _held_close_row(intent=None, transaction_type="BUY")
    mock_session = _mock_session(row)
    mock_fetch = MagicMock()
    mock_chase_order = AsyncMock()

    with patch("backend.api.database.async_session", return_value=mock_session), \
         patch("backend.brokers.broker_apis.fetch_positions", mock_fetch), \
         patch("backend.api.algo.order_events.write_event", new_callable=AsyncMock), \
         patch("backend.api.algo.chase.chase_order", mock_chase_order), \
         patch("backend.api.algo.chase._ch_mark_chase_active"), \
         patch("backend.api.algo.chase._ch_mark_chase_inactive"):
        result = await m.release_repeated_rejection_hold(901, actor="operator")
        await asyncio.sleep(0)

    assert result["ok"] is True
    mock_fetch.assert_not_called()
    mock_chase_order.assert_called_once()
