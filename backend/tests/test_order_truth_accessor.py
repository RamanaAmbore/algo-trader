"""
Tests for the canonical single-order-view accessor (2026-10-23
coordinator redirect): `orders_helpers.py:get_broker_order_truth` /
`apply_order_truth`.

Covers:
  - SAFE semantics: a missing/empty broker response (or a falsy id) is
    `available=False`, never a guessed terminal status.
  - A genuine non-terminal broker status (still OPEN/TRIGGER_PENDING)
    is `available=True, algo_status=None` — distinct from "unavailable".
  - `apply_order_truth` locks the row, refuses to write when
    unavailable/non-terminal/unchanged/already-final, and writes the
    FIFO ledger on a FILLED resolution.
"""
from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

import backend.api.routes.orders_helpers as oh
from backend.api.routes.orders_helpers import OrderTruth


def _mock_session(row):
    _result = MagicMock()
    _result.scalar_one_or_none.return_value = row

    mock_session = AsyncMock()
    mock_session.__aenter__ = AsyncMock(return_value=mock_session)
    mock_session.__aexit__ = AsyncMock(return_value=False)
    mock_session.execute = AsyncMock(return_value=_result)
    mock_session.commit = AsyncMock()
    return mock_session


# ── get_broker_order_truth — SAFE semantics ─────────────────────────────

@pytest.mark.asyncio
async def test_falsy_broker_order_id_is_unavailable():
    truth = await oh.get_broker_order_truth(None)
    assert truth.available is False
    assert truth.algo_status is None

    truth2 = await oh.get_broker_order_truth("")
    assert truth2.available is False


@pytest.mark.asyncio
async def test_missing_from_snapshot_is_unavailable_not_guessed():
    """A real id that simply isn't in the broker's own snapshot
    (transient API glitch, order not yet visible) must be
    'unavailable, don't guess' — NEVER a guessed UNFILLED."""
    truth = await oh.get_broker_order_truth("OID-X", broker_status_by_id={})
    assert truth.available is False
    assert truth.algo_status is None


@pytest.mark.asyncio
async def test_non_terminal_broker_status_is_available_but_no_algo_status():
    truth = await oh.get_broker_order_truth(
        "OID-X", broker_status_by_id={"OID-X": {"status": "OPEN", "average_price": 0}},
    )
    assert truth.available is True
    assert truth.algo_status is None
    assert truth.raw_broker_status == "OPEN"


@pytest.mark.asyncio
async def test_complete_maps_to_filled_with_average_price():
    truth = await oh.get_broker_order_truth(
        "OID-X",
        broker_status_by_id={"OID-X": {"status": "COMPLETE", "average_price": 101.5}},
    )
    assert truth.available is True
    assert truth.algo_status == "FILLED"
    assert truth.average_price == 101.5


@pytest.mark.asyncio
async def test_rejected_and_expired_map_correctly():
    r = await oh.get_broker_order_truth(
        "A", broker_status_by_id={"A": {"status": "REJECTED", "average_price": 0}},
    )
    assert r.algo_status == "REJECTED"
    e = await oh.get_broker_order_truth(
        "B", broker_status_by_id={"B": {"status": "EXPIRED", "average_price": 0}},
    )
    assert e.algo_status == "UNFILLED"


# ── apply_order_truth — locking, safety, ledger write ───────────────────

@pytest.mark.asyncio
async def test_apply_order_truth_noop_when_unavailable():
    mutated = await oh.apply_order_truth(1, OrderTruth(available=False))
    assert mutated is False


@pytest.mark.asyncio
async def test_apply_order_truth_noop_when_not_yet_terminal():
    mutated = await oh.apply_order_truth(1, OrderTruth(available=True, algo_status=None))
    assert mutated is False


@pytest.mark.asyncio
async def test_apply_order_truth_noop_when_algo_order_id_none():
    mutated = await oh.apply_order_truth(
        None, OrderTruth(available=True, algo_status="FILLED"),
    )
    assert mutated is False


@pytest.mark.asyncio
async def test_apply_order_truth_refuses_to_overwrite_final_status():
    mock_row = MagicMock()
    mock_row.status = "REJECTED"
    mock_session = _mock_session(mock_row)

    with patch("backend.api.database.async_session", return_value=mock_session):
        mutated = await oh.apply_order_truth(
            1, OrderTruth(available=True, algo_status="FILLED", average_price=100.0),
        )

    assert mutated is False
    assert mock_row.status == "REJECTED"
    mock_session.commit.assert_not_awaited()


@pytest.mark.asyncio
async def test_apply_order_truth_noop_when_status_already_matches():
    mock_row = MagicMock()
    mock_row.status = "CANCELLED"
    mock_session = _mock_session(mock_row)

    with patch("backend.api.database.async_session", return_value=mock_session):
        mutated = await oh.apply_order_truth(
            1, OrderTruth(available=True, algo_status="CANCELLED"),
        )

    assert mutated is False
    mock_session.commit.assert_not_awaited()


@pytest.mark.asyncio
async def test_apply_order_truth_writes_filled_and_records_ledger():
    mock_row = MagicMock()
    mock_row.id = 99
    mock_row.status = "OPEN"
    mock_row.quantity = 50
    mock_session = _mock_session(mock_row)
    mock_ledger = AsyncMock()

    with patch("backend.api.database.async_session", return_value=mock_session), \
         patch("backend.api.routes.orders_postback._pb_write_ledger_fills", mock_ledger):
        mutated = await oh.apply_order_truth(
            99, OrderTruth(available=True, algo_status="FILLED", average_price=102.5),
        )

    assert mutated is True
    assert mock_row.status == "FILLED"
    assert mock_row.fill_price == 102.5
    assert mock_row.filled_quantity == 50
    mock_ledger.assert_awaited_once()
    assert mock_session.commit.await_count == 2  # status write + post-ledger commit
