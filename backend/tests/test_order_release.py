"""Release checks: position must match, and the held order must be HELD."""
import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pandas as pd
import pytest

from backend.api.algo.order_release import (
    _find_net_qty,
    _positions_data_unreliable,
    _positions_net_rows_from_dfs,
    position_matches,
)


def test_sell_close_needs_matching_long_position():
    assert position_matches("SELL", 200, 200) == (True, "ok")


def test_sell_close_refused_when_position_changed():
    ok, why = position_matches("SELL", 200, 100)
    assert ok is False and "position changed" in why


def test_buy_close_needs_matching_short_position():
    assert position_matches("BUY", 200, -200) == (True, "ok")
    assert position_matches("BUY", 200, 200)[0] is False


def test_find_net_qty_matches_symbol_and_exchange():
    positions = {"net": [
        {"tradingsymbol": "CRUDEOIL26OCT8600CE", "exchange": "MCX", "quantity": 200},
        {"tradingsymbol": "CRUDEOIL26OCT8600CE", "exchange": "NFO", "quantity": 5},
    ]}
    assert _find_net_qty(positions, "CRUDEOIL26OCT8600CE", "MCX") == 200
    assert _find_net_qty(positions, "CRUDEOIL26OCT8700CE", "MCX") == 0
    assert _find_net_qty({}, "X", "MCX") == 0


# ── Fix 3 — MCX expiry-close release unit mismatch ──────────────────────
#
# Kite ships MCX/NCO intraday `quantity` in LOTS; `row.quantity` on a held
# expiry-close AlgoOrder is already in CONTRACTS (it came from
# ExpiryEngine._fetch_option_positions -> broker_apis.fetch_positions(),
# which normalises via _annotate_lot_size). Comparing a raw
# `broker.positions()` call (lots) against `row.quantity` (contracts)
# always refused a real, unchanged 1-lot MCX position. See CLAUDE.md's
# "Option qty vs lot_size" guard.

def _mcx_positions_frame(quantity_lots: int, multiplier: int = 100,
                         symbol: str = "CRUDEOIL26OCT8600CE",
                         exchange: str = "MCX") -> "pd.DataFrame":
    """Build a RAW Kite-shape positions row (quantity in LOTS, as Kite
    actually ships it for MCX/NCO) and run it through the REAL
    `_annotate_lot_size` — exactly what `broker_apis._fetch_positions_local`
    does — so the test frame is produced by the real normalisation
    pipeline, not hand-fed already-correct contracts."""
    from backend.brokers.broker_apis import _annotate_lot_size
    df = pd.DataFrame([{
        "tradingsymbol": symbol, "exchange": exchange,
        "quantity": quantity_lots, "multiplier": multiplier,
    }])
    _annotate_lot_size(df)
    return df


def test_positions_net_rows_from_dfs_reads_normalized_contracts():
    frame = _mcx_positions_frame(quantity_lots=1, multiplier=100)
    assert frame.iloc[0]["quantity"] == 100  # 1 lot * 100 multiplier = 100 contracts
    rows = _positions_net_rows_from_dfs([frame])
    assert rows == [{"tradingsymbol": "CRUDEOIL26OCT8600CE", "exchange": "MCX", "quantity": 100}]


def test_positions_net_rows_from_dfs_skips_empty_and_none_frames():
    assert _positions_net_rows_from_dfs(None) == []
    assert _positions_net_rows_from_dfs([]) == []
    assert _positions_net_rows_from_dfs([None, pd.DataFrame()]) == []


@pytest.mark.parametrize("attrs", [
    {"stale": True}, {"fetch_failed": True}, {"circuit_open": True}, {"interval_skipped": True},
])
def test_positions_data_unreliable_detects_each_degraded_attr(attrs):
    df = pd.DataFrame([{"tradingsymbol": "X", "exchange": "MCX", "quantity": 1}])
    df.attrs.update(attrs)
    assert _positions_data_unreliable([df]) is True


def test_positions_data_unreliable_false_for_clean_frame():
    df = pd.DataFrame([{"tradingsymbol": "X", "exchange": "MCX", "quantity": 1}])
    assert _positions_data_unreliable([df]) is False
    assert _positions_data_unreliable([]) is False


def _mock_session(row):
    _result = MagicMock()
    _result.scalar_one_or_none.return_value = row
    mock_session = AsyncMock()
    mock_session.__aenter__ = AsyncMock(return_value=mock_session)
    mock_session.__aexit__ = AsyncMock(return_value=False)
    mock_session.execute = AsyncMock(return_value=_result)
    mock_session.commit = AsyncMock()
    return mock_session


def _held_expiry_row(**extra) -> SimpleNamespace:
    base = dict(
        id=501, status="HELD", hold_json="{}", detail="",
        account="ZG0790", symbol="CRUDEOIL26OCT8600CE", exchange="MCX",
        transaction_type="SELL", quantity=100, product="NRML",
    )
    base.update(extra)
    return SimpleNamespace(**base)


@pytest.mark.asyncio
async def test_release_mcx_expiry_close_succeeds_with_normalized_contracts():
    """1 lot held (100 contracts, row.quantity already in contracts) must
    match a broker book that still reports exactly 1 lot (quantity=1,
    multiplier=100) once normalised — proving the comparison runs on the
    SAME unit on both sides, and release no longer calls raw
    `broker.positions()` at all."""
    from backend.api.algo import order_release as m

    row = _held_expiry_row()
    mock_session = _mock_session(row)
    frame = _mcx_positions_frame(quantity_lots=1, multiplier=100)
    mock_broker = MagicMock()
    mock_broker.quote.return_value = {
        "MCX:CRUDEOIL26OCT8600CE": {
            "depth": {"buy": [{"price": 100.0}], "sell": [{"price": 100.2}]},
            "last_price": 100.1,
            "lower_circuit_limit": 50.0,
            "upper_circuit_limit": 200.0,
        }
    }

    with patch("backend.api.database.async_session", return_value=mock_session), \
         patch("backend.brokers.get_broker", return_value=mock_broker), \
         patch("backend.brokers.broker_apis.fetch_positions", return_value=[frame]), \
         patch("backend.api.routes.orders_helpers._ensure_tick_index", new_callable=AsyncMock), \
         patch.dict("backend.api.routes.orders_helpers._TICK_INDEX",
                    {("MCX", "CRUDEOIL26OCT8600CE"): 0.1}, clear=True), \
         patch("backend.api.algo.order_events.write_event", new_callable=AsyncMock), \
         patch("backend.api.algo.chase.chase_order", new_callable=AsyncMock):
        result = await m.release_held_order(501, actor="operator")
        await asyncio.sleep(0)

    assert result["ok"] is True
    assert row.status == "OPEN"
    mock_broker.positions.assert_not_called()


@pytest.mark.asyncio
async def test_release_mcx_expiry_close_refused_when_quantity_really_changed():
    """Held row expects 1 lot (100 contracts) closed, but the broker book
    now shows 2 lots (200 contracts, multiplier=100) open — a genuine
    position change, correctly refused."""
    from backend.api.algo import order_release as m

    row = _held_expiry_row()
    mock_session = _mock_session(row)
    frame = _mcx_positions_frame(quantity_lots=2, multiplier=100)
    mock_broker = MagicMock()

    with patch("backend.api.database.async_session", return_value=mock_session), \
         patch("backend.brokers.get_broker", return_value=mock_broker), \
         patch("backend.brokers.broker_apis.fetch_positions", return_value=[frame]):
        result = await m.release_held_order(501, actor="operator")

    assert result["ok"] is False
    assert "position changed" in result["reason"]
    assert row.status == "HELD"
    mock_broker.positions.assert_not_called()


@pytest.mark.asyncio
async def test_release_refuses_when_positions_data_is_stale():
    """A degraded/substituted positions read must fail closed instead of
    comparing against a possibly-outdated last-known-good frame."""
    from backend.api.algo import order_release as m

    row = _held_expiry_row()
    mock_session = _mock_session(row)
    frame = _mcx_positions_frame(quantity_lots=1, multiplier=100)
    frame.attrs["stale"] = True
    mock_broker = MagicMock()

    with patch("backend.api.database.async_session", return_value=mock_session), \
         patch("backend.brokers.get_broker", return_value=mock_broker), \
         patch("backend.brokers.broker_apis.fetch_positions", return_value=[frame]):
        result = await m.release_held_order(501, actor="operator")

    assert result["ok"] is False
    assert "stale" in result["reason"] or "unavailable" in result["reason"]
    assert row.status == "HELD"


# ── Fix 2 — release_held_order must clear hold_json ─────────────────────
#
# orders_held.py's list_held query matches `status == "HELD" OR
# hold_json IS NOT NULL`. release_held_order set status="OPEN" but never
# cleared hold_json, so a released expiry-close row stayed stuck in
# HeldOrdersCard forever (both Release and Cancel on it then 409'd,
# "not HELD").

def _list_held_predicate_matches(row) -> bool:
    """Mirrors orders_held.py:list_held's SQLAlchemy filter
    (`or_(AlgoOrder.status == "HELD", AlgoOrder.hold_json.isnot(None))`)
    as a plain Python predicate over an already-fetched row."""
    return row.status == "HELD" or row.hold_json is not None


@pytest.mark.asyncio
async def test_release_clears_hold_json_so_row_no_longer_matches_list_held():
    from backend.api.algo import order_release as m
    from datetime import datetime, timezone
    from backend.api.algo.order_hold import HoldCategory, hold_record

    row = _held_expiry_row(hold_json=hold_record(
        HoldCategory.EXPIRY_CLOSE, "expiry close", "CHASE_MED", None,
        datetime.now(timezone.utc),
    ))
    assert _list_held_predicate_matches(row) is True  # sanity: starts HELD

    mock_session = _mock_session(row)
    frame = _mcx_positions_frame(quantity_lots=1, multiplier=100)
    mock_broker = MagicMock()
    mock_broker.quote.return_value = {
        "MCX:CRUDEOIL26OCT8600CE": {
            "depth": {"buy": [{"price": 100.0}], "sell": [{"price": 100.2}]},
            "last_price": 100.1,
            "lower_circuit_limit": 50.0,
            "upper_circuit_limit": 200.0,
        }
    }

    with patch("backend.api.database.async_session", return_value=mock_session), \
         patch("backend.brokers.get_broker", return_value=mock_broker), \
         patch("backend.brokers.broker_apis.fetch_positions", return_value=[frame]), \
         patch("backend.api.routes.orders_helpers._ensure_tick_index", new_callable=AsyncMock), \
         patch.dict("backend.api.routes.orders_helpers._TICK_INDEX",
                    {("MCX", "CRUDEOIL26OCT8600CE"): 0.1}, clear=True), \
         patch("backend.api.algo.order_events.write_event", new_callable=AsyncMock), \
         patch("backend.api.algo.chase.chase_order", new_callable=AsyncMock):
        result = await m.release_held_order(501, actor="operator")
        await asyncio.sleep(0)

    assert result["ok"] is True
    assert row.status == "OPEN"
    assert row.hold_json is None
    assert _list_held_predicate_matches(row) is False
