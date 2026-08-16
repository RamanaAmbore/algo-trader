"""
Tests for order-pair feature:
  - _root_symbol helper
  - _auto_pair_positions lot-waterfall algorithm
  - POST /api/orders/pair handler (pair_orders)
  - _annotate_gtt GTT annotation helper

Coverage:
1. _root_symbol: NIFTY24800CE→NIFTY, BANKNIFTY24AUGFUT→BANKNIFTY, INFY→INFY
2. Basic pair: 1 long 2 lots + 1 short 2 lots → P1, paired_qty=2, orphan_qty=0
3. Lot split: long 3 + short 2 → long P1 paired_qty=2 orphan_qty=1, short P1 paired_qty=2 orphan_qty=0
4. Two pairs same underlying: long 2 + long 1 + short 2 + short 1 → P1(2) + P2(1)
5. Fully orphaned: single long, no short → is_orphan=True
6. Different underlyings: NIFTY long + BANKNIFTY short → no cross-pair, both orphans
7. pair_orders happy path — child.parent_order_id set to parent.id
8. pair_orders raises 404 when either order not found
9. pair_orders raises 400 when child already has a parent
10. pair_orders raises 400 when parent_id == child_id
11. _annotate_gtt: matching (account, symbol) → has_gtt=True
12. _annotate_gtt: no match → has_gtt=False
13. _annotate_gtt: empty gtt_set → all rows has_gtt=False
14. PositionRow default has_gtt=False (schema backward compat)
"""

from __future__ import annotations

import pytest
from unittest.mock import AsyncMock, MagicMock, patch


# ---------------------------------------------------------------------------
# Fixture helpers
# ---------------------------------------------------------------------------

def _make_row(account: str, tradingsymbol: str, exchange: str, quantity: int):
    """Build a minimal PositionRow for pairing tests."""
    from backend.api.schemas import PositionRow
    return PositionRow(
        account=account,
        tradingsymbol=tradingsymbol,
        exchange=exchange,
        product="NRML",
        quantity=quantity,
        average_price=100.0,
        close_price=100.0,
        pnl=0.0,
    )


# ---------------------------------------------------------------------------
# 1. _root_symbol
# ---------------------------------------------------------------------------

def test_root_symbol_nifty_option():
    from backend.api.routes.positions import _root_symbol
    assert _root_symbol("NIFTY24800CE") == "NIFTY"


def test_root_symbol_banknifty_future():
    from backend.api.routes.positions import _root_symbol
    assert _root_symbol("BANKNIFTY24AUGFUT") == "BANKNIFTY"


def test_root_symbol_equity():
    from backend.api.routes.positions import _root_symbol
    assert _root_symbol("INFY") == "INFY"


def test_root_symbol_crudeoil_future():
    from backend.api.routes.positions import _root_symbol
    assert _root_symbol("CRUDEOIL24AUGFUT") == "CRUDEOIL"


def test_root_symbol_goldm_future():
    from backend.api.routes.positions import _root_symbol
    assert _root_symbol("GOLDM24AUGFUT") == "GOLDM"


def test_root_symbol_nifty_future():
    from backend.api.routes.positions import _root_symbol
    assert _root_symbol("NIFTY24AUGFUT") == "NIFTY"


# ---------------------------------------------------------------------------
# 2. Basic pair: 1 long 2 lots + 1 short 2 lots → P1 pair
# ---------------------------------------------------------------------------

def test_auto_pair_basic():
    from backend.api.routes.positions import _auto_pair_positions

    rows = [
        _make_row("ZG0790", "NIFTY24AUGFUT", "NFO", 2),
        _make_row("ZG0790", "NIFTY24800CE", "NFO", -2),
    ]
    result = _auto_pair_positions(rows)
    assert len(result) == 2

    long_r = result[0]
    short_r = result[1]

    assert long_r.pair_group_key == "P1"
    assert long_r.is_orphan is False
    assert long_r.paired_qty == 2
    assert long_r.orphan_qty == 0

    assert short_r.pair_group_key == "P1"
    assert short_r.is_orphan is False
    assert short_r.paired_qty == 2
    assert short_r.orphan_qty == 0


# ---------------------------------------------------------------------------
# 3. Lot split: long 3 + short 2 → partial pair
# ---------------------------------------------------------------------------

def test_auto_pair_lot_split():
    from backend.api.routes.positions import _auto_pair_positions

    rows = [
        _make_row("ZG0790", "NIFTY24AUGFUT", "NFO", 3),
        _make_row("ZG0790", "NIFTY24800CE", "NFO", -2),
    ]
    result = _auto_pair_positions(rows)

    long_r = result[0]
    short_r = result[1]

    # Both get P1 with match_qty = min(3, 2) = 2
    assert long_r.pair_group_key == "P1"
    assert long_r.paired_qty == 2
    assert long_r.orphan_qty == 1   # 3 - 2
    assert long_r.is_orphan is False

    assert short_r.pair_group_key == "P1"
    assert short_r.paired_qty == 2
    assert short_r.orphan_qty == 0   # 2 - 2
    assert short_r.is_orphan is False


# ---------------------------------------------------------------------------
# 4. Two pairs on same underlying: long 2 + long 1 + short 2 + short 1
#    → P1(2 lots) + P2(1 lot) — sorted largest-first so P1 gets the 2-lot pair
# ---------------------------------------------------------------------------

def test_auto_pair_two_pairs():
    from backend.api.routes.positions import _auto_pair_positions

    rows = [
        _make_row("ZG0790", "NIFTY24800CE", "NFO", 2),   # long 2
        _make_row("ZG0790", "NIFTY24900CE", "NFO", 1),   # long 1
        _make_row("ZG0790", "NIFTY24AUGFUT", "NFO", -2), # short 2
        _make_row("ZG0790", "NIFTY24700PE", "NFO", -1),  # short 1
    ]
    result = _auto_pair_positions(rows)

    # Collect by pair_group_key
    by_key: dict = {}
    for r in result:
        k = r.pair_group_key
        by_key.setdefault(k, []).append(r)

    assert "P1" in by_key, f"Expected P1, got keys: {list(by_key)}"
    assert "P2" in by_key, f"Expected P2, got keys: {list(by_key)}"

    p1 = by_key["P1"]
    p2 = by_key["P2"]

    # Each pair has exactly 2 rows (one long, one short)
    assert len(p1) == 2
    assert len(p2) == 2

    p1_qtys = {r.paired_qty for r in p1}
    p2_qtys = {r.paired_qty for r in p2}
    assert p1_qtys == {2}, f"P1 paired_qty expected 2, got {p1_qtys}"
    assert p2_qtys == {1}, f"P2 paired_qty expected 1, got {p2_qtys}"

    for r in result:
        assert r.is_orphan is False
        assert r.orphan_qty == 0


# ---------------------------------------------------------------------------
# 5. Fully orphaned: single long with no matching short
# ---------------------------------------------------------------------------

def test_auto_pair_orphan():
    from backend.api.routes.positions import _auto_pair_positions

    rows = [
        _make_row("ZG0790", "NIFTY24AUGFUT", "NFO", 2),
    ]
    result = _auto_pair_positions(rows)
    r = result[0]

    assert r.is_orphan is True
    assert r.pair_group_key is None
    assert r.paired_qty == 0
    assert r.orphan_qty == 2


# ---------------------------------------------------------------------------
# 6. Different underlyings: NIFTY long + BANKNIFTY short → no cross-pair
# ---------------------------------------------------------------------------

def test_auto_pair_no_cross_underlying():
    from backend.api.routes.positions import _auto_pair_positions

    rows = [
        _make_row("ZG0790", "NIFTY24AUGFUT", "NFO", 1),
        _make_row("ZG0790", "BANKNIFTY24AUGFUT", "NFO", -1),
    ]
    result = _auto_pair_positions(rows)

    nifty_r = next(r for r in result if "BANKNIFTY" not in r.tradingsymbol)
    bnf_r = next(r for r in result if "BANKNIFTY" in r.tradingsymbol)

    assert nifty_r.is_orphan is True, "NIFTY long must not pair with BANKNIFTY short"
    assert bnf_r.is_orphan is True, "BANKNIFTY short must not pair with NIFTY long"
    assert nifty_r.pair_group_key is None
    assert bnf_r.pair_group_key is None


# ---------------------------------------------------------------------------
# 7. Zero-quantity rows are not orphaned and get no pair
# ---------------------------------------------------------------------------

def test_auto_pair_zero_quantity():
    from backend.api.routes.positions import _auto_pair_positions

    rows = [
        _make_row("ZG0790", "NIFTY24AUGFUT", "NFO", 0),
    ]
    result = _auto_pair_positions(rows)
    r = result[0]

    assert r.is_orphan is False
    assert r.pair_group_key is None
    assert r.paired_qty == 0
    assert r.orphan_qty == 0


# ---------------------------------------------------------------------------
# Helper: resolve the raw underlying function from a Litestar handler.
# OrdersController.pair_orders is a litestar.handlers.http_handlers.decorators.post
# instance; the real coroutine function lives at handler.fn.
# ---------------------------------------------------------------------------

def _handler_fn(handler):
    """Return the raw coroutine function from a Litestar route handler."""
    return handler.fn


# ---------------------------------------------------------------------------
# 8. pair_orders happy path
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_pair_orders_happy_path():
    """pair_orders sets child.parent_order_id = parent.id and commits."""
    from backend.api.routes.orders import OrdersController, PairOrdersInput

    parent = MagicMock()
    parent.id = 10
    parent.parent_order_id = None

    child = MagicMock()
    child.id = 20
    child.parent_order_id = None

    mock_session = AsyncMock()
    mock_session.get = AsyncMock(side_effect=lambda model, pk: {10: parent, 20: child}[pk])
    mock_session.commit = AsyncMock()

    mock_ctx = AsyncMock()
    mock_ctx.__aenter__ = AsyncMock(return_value=mock_session)
    mock_ctx.__aexit__ = AsyncMock(return_value=False)

    fn = _handler_fn(OrdersController.pair_orders)
    data = PairOrdersInput(parent_id=10, child_id=20)

    # The handler imports async_session from backend.api.database inside the body.
    with patch("backend.api.database.async_session", return_value=mock_ctx):
        result = await fn(None, data)  # self=None for unbound call

    assert result["ok"] is True
    assert result["parent_id"] == 10
    assert result["child_id"] == 20
    assert child.parent_order_id == 10, "child.parent_order_id must be set to parent.id"
    mock_session.commit.assert_awaited_once()


# ---------------------------------------------------------------------------
# 9. pair_orders raises 404 when order not found
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_pair_orders_not_found():
    """pair_orders raises HTTPException(404) when either order is not found."""
    from litestar.exceptions import HTTPException
    from backend.api.routes.orders import OrdersController, PairOrdersInput

    mock_session = AsyncMock()
    mock_session.get = AsyncMock(return_value=None)  # simulate not found
    mock_session.commit = AsyncMock()

    mock_ctx = AsyncMock()
    mock_ctx.__aenter__ = AsyncMock(return_value=mock_session)
    mock_ctx.__aexit__ = AsyncMock(return_value=False)

    fn = _handler_fn(OrdersController.pair_orders)
    data = PairOrdersInput(parent_id=1, child_id=2)

    with patch("backend.api.database.async_session", return_value=mock_ctx):
        with pytest.raises(HTTPException) as exc_info:
            await fn(None, data)

    assert exc_info.value.status_code == 404
    assert "not found" in exc_info.value.detail


# ---------------------------------------------------------------------------
# 10. pair_orders raises 400 when child already has a parent
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_pair_orders_child_already_has_parent():
    """pair_orders raises HTTPException(400) when child.parent_order_id is already set."""
    from litestar.exceptions import HTTPException
    from backend.api.routes.orders import OrdersController, PairOrdersInput

    parent = MagicMock()
    parent.id = 10

    child = MagicMock()
    child.id = 20
    child.parent_order_id = 5  # already paired

    mock_session = AsyncMock()
    mock_session.get = AsyncMock(side_effect=lambda model, pk: {10: parent, 20: child}[pk])
    mock_session.commit = AsyncMock()

    mock_ctx = AsyncMock()
    mock_ctx.__aenter__ = AsyncMock(return_value=mock_session)
    mock_ctx.__aexit__ = AsyncMock(return_value=False)

    fn = _handler_fn(OrdersController.pair_orders)
    data = PairOrdersInput(parent_id=10, child_id=20)

    with patch("backend.api.database.async_session", return_value=mock_ctx):
        with pytest.raises(HTTPException) as exc_info:
            await fn(None, data)

    assert exc_info.value.status_code == 400
    assert "unpair" in exc_info.value.detail


# ---------------------------------------------------------------------------
# 11. pair_orders raises 400 when parent_id == child_id
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_pair_orders_same_id():
    """pair_orders raises HTTPException(400) when parent_id == child_id."""
    from litestar.exceptions import HTTPException
    from backend.api.routes.orders import OrdersController, PairOrdersInput

    fn = _handler_fn(OrdersController.pair_orders)
    data = PairOrdersInput(parent_id=42, child_id=42)

    # No session needed — same-id check fires before DB access.
    with pytest.raises(HTTPException) as exc_info:
        await fn(None, data)

    assert exc_info.value.status_code == 400
    assert "different" in exc_info.value.detail


# ---------------------------------------------------------------------------
# 11-14. _annotate_gtt and has_gtt field
# ---------------------------------------------------------------------------

def test_annotate_gtt_match():
    """Row whose (account, tradingsymbol) is in gtt_set gets has_gtt=True."""
    from backend.api.routes.positions import _annotate_gtt

    rows = [_make_row("ZG0790", "NIFTY24AUGFUT", "NFO", 1)]
    gtt_set = {("ZG0790", "NIFTY24AUGFUT")}
    result = _annotate_gtt(rows, gtt_set)

    assert result[0].has_gtt is True


def test_annotate_gtt_no_match():
    """Row whose (account, tradingsymbol) is NOT in gtt_set keeps has_gtt=False."""
    from backend.api.routes.positions import _annotate_gtt

    rows = [_make_row("ZG0790", "INFY", "NSE", 10)]
    gtt_set = {("ZG0790", "NIFTY24AUGFUT")}
    result = _annotate_gtt(rows, gtt_set)

    assert result[0].has_gtt is False


def test_annotate_gtt_empty_set():
    """Empty gtt_set → all rows have has_gtt=False."""
    from backend.api.routes.positions import _annotate_gtt

    rows = [
        _make_row("ZG0790", "NIFTY24AUGFUT", "NFO", 1),
        _make_row("ZG0790", "INFY", "NSE", 10),
    ]
    result = _annotate_gtt(rows, set())

    assert all(r.has_gtt is False for r in result)


def test_annotate_gtt_partial_match():
    """Only the matching row gets has_gtt=True; the other stays False."""
    from backend.api.routes.positions import _annotate_gtt

    rows = [
        _make_row("ZG0790", "NIFTY24AUGFUT", "NFO", 1),
        _make_row("ZG0790", "INFY", "NSE", 10),
    ]
    gtt_set = {("ZG0790", "NIFTY24AUGFUT")}
    result = _annotate_gtt(rows, gtt_set)

    nifty_r = next(r for r in result if r.tradingsymbol == "NIFTY24AUGFUT")
    infy_r = next(r for r in result if r.tradingsymbol == "INFY")

    assert nifty_r.has_gtt is True
    assert infy_r.has_gtt is False


# ---------------------------------------------------------------------------
# 15. longs_q waterfall-advance regression
#     Two longs, two shorts of equal sizes → the first long is *fully* consumed
#     by the first short (longs_q[0][1] reaches 0 and must be popped from
#     longs_q, not the read-only longs list).  If the wrong list were popped
#     the zero-qty entry would remain at the head of longs_q and the while-loop
#     would spin forever (infinite loop).  This test verifies that the second
#     long is correctly advanced to and paired with the second short.
# ---------------------------------------------------------------------------

def test_auto_pair_longs_q_waterfall_advance():
    """longs_q.pop(0) is called (not longs.pop(0)) so the waterfall advances."""
    from backend.api.routes.positions import _auto_pair_positions

    # long 1 (2 lots), long 2 (1 lot), short 1 (2 lots), short 2 (1 lot)
    # After sorting largest-first:
    #   longs_q:  [(idx0, 2), (idx1, 1)]
    #   shorts_q: [(idx2, 2), (idx3, 1)]
    # Iteration 1: match min(2,2)=2 → longs_q[0][1]=0 → pop → longs_q becomes [(idx1,1)]
    # Iteration 2: match min(1,1)=1 → both pop → loop ends
    rows = [
        _make_row("ZG0790", "NIFTY24800CE", "NFO", 2),   # long 2
        _make_row("ZG0790", "NIFTY24900CE", "NFO", 1),   # long 1
        _make_row("ZG0790", "NIFTY24AUGFUT", "NFO", -2), # short 2
        _make_row("ZG0790", "NIFTY24700PE", "NFO", -1),  # short 1
    ]
    # This call must terminate (not infinite-loop).
    result = _auto_pair_positions(rows)

    # Every row should be paired — no orphans.
    assert all(r.pair_group_key is not None for r in result), (
        "All rows must be paired when longs and shorts fully balance"
    )
    assert all(r.is_orphan is False for r in result)
    assert all(r.orphan_qty == 0 for r in result)

    # There must be exactly two distinct pair groups.
    keys = {r.pair_group_key for r in result}
    assert keys == {"P1", "P2"}, f"Expected P1+P2, got {keys}"

    # Verify paired_qty per group.
    by_key: dict = {}
    for r in result:
        by_key.setdefault(r.pair_group_key, []).append(r)

    p1_qtys = {r.paired_qty for r in by_key["P1"]}
    p2_qtys = {r.paired_qty for r in by_key["P2"]}
    assert p1_qtys == {2}, f"P1 paired_qty should be 2, got {p1_qtys}"
    assert p2_qtys == {1}, f"P2 paired_qty should be 1, got {p2_qtys}"


def test_position_row_default_has_gtt():
    """PositionRow default value for has_gtt is False (backward compat)."""
    from backend.api.schemas import PositionRow

    row = PositionRow(
        account="ZG0790",
        tradingsymbol="INFY",
        exchange="NSE",
        product="CNC",
        quantity=10,
        average_price=1500.0,
        close_price=1505.0,
        pnl=50.0,
    )
    assert row.has_gtt is False
