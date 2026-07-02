"""
test_strategies_batching.py — verifies the N+1 fix for list_strategies.

Five quality dimensions:

  1. SSOT  — per-strategy fields (open_count, closed_count, realised_pnl,
             unrealised_pnl) from the batched path match the expected
             values computed from the same input data.
  2. Perf  — list_strategies fires at most 4 DB queries for N∈{0,1,10}:
             Q1 (Strategy + User JOIN), Q2a (AlgoOrder counts),
             Q2b (StrategyLot aggregates), Q3 (open StrategyLot rows for
             LTP mark). Previously fired up to 7×N.
  3. Stale — strategies.py no longer calls _enrich_with_pnl per row
             inside the list handler.
  4. Reuse — _enrich_many_with_pnl is used by list_strategies; the
             single-row _enrich_with_pnl still exists for get/create/update.
  5. UX    — zero-strategy list returns empty StrategiesResponse; all
             expected StrategyInfo fields present on every row.
"""

from __future__ import annotations

import pathlib
from datetime import datetime, timezone
from decimal import Decimal
from typing import Optional
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

# ── Stale-code guards (no DB needed) ────────────────────────────────────────


_STRATEGIES_SRC = (
    pathlib.Path(__file__).parent.parent / "api" / "routes" / "strategies.py"
).read_text()


def test_list_handler_uses_enrich_many():
    """Stale: _enrich_many_with_pnl must be defined in strategies.py."""
    assert "_enrich_many_with_pnl" in _STRATEGIES_SRC, (
        "strategies.py must define _enrich_many_with_pnl"
    )


def test_single_row_enrich_still_present():
    """Reuse: _enrich_with_pnl must still exist for get/create/update."""
    assert "async def _enrich_with_pnl(" in _STRATEGIES_SRC, (
        "_enrich_with_pnl must remain for single-row handlers"
    )


def test_list_strategies_body_calls_enrich_many():
    """Stale: the list_strategies method body must call _enrich_many_with_pnl."""
    marker = "async def list_strategies("
    idx = _STRATEGIES_SRC.index(marker)
    snippet = _STRATEGIES_SRC[idx: idx + 600]
    assert "_enrich_many_with_pnl" in snippet, (
        "list_strategies body must call _enrich_many_with_pnl"
    )
    assert (
        "for row, owner_username in rows:\n            out.append(await _enrich_with_pnl"
        not in snippet
    ), "list_strategies must not call _enrich_with_pnl per row"


# ── Mock-session helpers ─────────────────────────────────────────────────────


def _make_strategy(sid: int, slug: str) -> MagicMock:
    s = MagicMock()
    s.id = sid
    s.slug = slug
    s.name = f"Strategy {sid}"
    s.description = None
    s.owner_user_id = None
    s.capacity_cap_inr = None
    s.target_volatility = None
    s.is_active = True
    s.created_at = datetime.now(timezone.utc)
    s.updated_at = datetime.now(timezone.utc)
    return s


def _make_order_agg_row(strategy_id: int,
                        open_count: int, closed_count: int) -> MagicMock:
    r = MagicMock()
    r.strategy_id = strategy_id
    r.open_count = open_count
    r.closed_count = closed_count
    return r


def _make_lot_agg_row(strategy_id: int, realised: float,
                      open_lots_count: int) -> MagicMock:
    r = MagicMock()
    r.strategy_id = strategy_id
    r.realised = realised
    r.open_lots_count = open_lots_count
    return r


def _make_mock_session(
    strategies: list[tuple[MagicMock, Optional[str]]],
    order_aggs: list,
    lot_aggs: list,
    open_lots: list,
) -> tuple[AsyncMock, list]:
    """Build a mock async_session context manager.

    Execute call order:
      1 → Q1: Strategy + User JOIN (list_strategies body)
      2 → Q2a: AlgoOrder counts (_enrich_many_with_pnl)
      3 → Q2b: StrategyLot aggregates (_enrich_many_with_pnl)
      4 → Q3: open StrategyLot rows (_enrich_many_with_pnl)

    Returns (mock_session, execute_log).
    """
    execute_log: list = []
    state = {"n": 0}

    async def _execute(stmt):
        execute_log.append(stmt)
        state["n"] += 1
        result = MagicMock()
        n = state["n"]
        if n == 1:
            result.all = MagicMock(return_value=strategies)
        elif n == 2:
            result.all = MagicMock(return_value=order_aggs)
        elif n == 3:
            result.all = MagicMock(return_value=lot_aggs)
        elif n == 4:
            result.all = MagicMock(return_value=open_lots)
        else:
            result.all = MagicMock(return_value=[])
        return result

    mock_session = AsyncMock()
    mock_session.execute = _execute
    mock_session.__aenter__ = AsyncMock(return_value=mock_session)
    mock_session.__aexit__ = AsyncMock(return_value=False)
    return mock_session, execute_log


# ── 1. SSOT — field values match input data ──────────────────────────────────


@pytest.mark.asyncio
async def test_ssot_fields_from_ledger(async_client):
    """SSOT: when has_ledger=True, realised comes from lot_agg.realised.
    Unrealised falls back to 0.0 when open_lots_count=0."""
    strat = _make_strategy(1, "ledger-strat")
    order_agg = _make_order_agg_row(1, open_count=3, closed_count=7)
    lot_agg = _make_lot_agg_row(1, realised=1100.0, open_lots_count=0)

    mock_session, _ = _make_mock_session(
        strategies=[(strat, "alice")],
        order_aggs=[order_agg],
        lot_aggs=[lot_agg],
        open_lots=[],
    )

    with patch("backend.api.routes.strategies.async_session",
               return_value=mock_session), \
         patch("backend.api.routes.strategies.cap_guard",
               return_value=lambda h: h):
        resp = await async_client.get("/api/strategies/")

    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert len(data["rows"]) == 1
    row = data["rows"][0]

    assert row["open_order_count"] == 3
    assert row["closed_order_count"] == 7
    # has_ledger=True (realised != 0) → realised from lot ledger
    assert row["realised_pnl"] == pytest.approx(1100.0)
    # open_lots_count=0 → unrealised=0
    assert row["unrealised_pnl"] == pytest.approx(0.0)
    assert row["owner_username"] == "alice"


@pytest.mark.asyncio
async def test_ssot_legacy_no_ledger_returns_zero_realised(async_client):
    """SSOT: when has_ledger=False (no open lots AND realised==0),
    realised and unrealised both return 0.0 (legacy path, no pnl column)."""
    strat = _make_strategy(1, "legacy-strat")
    order_agg = _make_order_agg_row(1, open_count=0, closed_count=5)
    lot_agg = _make_lot_agg_row(1, realised=0.0, open_lots_count=0)

    mock_session, _ = _make_mock_session(
        strategies=[(strat, None)],
        order_aggs=[order_agg],
        lot_aggs=[lot_agg],
        open_lots=[],
    )

    with patch("backend.api.routes.strategies.async_session",
               return_value=mock_session), \
         patch("backend.api.routes.strategies.cap_guard",
               return_value=lambda h: h):
        resp = await async_client.get("/api/strategies/")

    assert resp.status_code == 200, resp.text
    data = resp.json()
    row = data["rows"][0]
    # Legacy path: has_ledger=False → both default to 0.0
    assert row["realised_pnl"] == pytest.approx(0.0)
    assert row["unrealised_pnl"] == pytest.approx(0.0)


# ── 2. Perf — at most 4 DB executes regardless of N ─────────────────────────


@pytest.mark.asyncio
async def test_perf_zero_strategies_fires_one_query(async_client):
    """Perf: 0 strategies → only Q1 fires (_enrich_many_with_pnl early-returns)."""
    mock_session, execute_log = _make_mock_session([], [], [], [])

    with patch("backend.api.routes.strategies.async_session",
               return_value=mock_session), \
         patch("backend.api.routes.strategies.cap_guard",
               return_value=lambda h: h):
        resp = await async_client.get("/api/strategies/")

    assert resp.status_code == 200, resp.text
    assert resp.json()["rows"] == []
    assert len(execute_log) == 1, (
        f"0 strategies: expected 1 DB query (Q1), got {len(execute_log)}"
    )


@pytest.mark.asyncio
async def test_perf_one_strategy_fires_at_most_four_queries(async_client):
    """Perf: 1 strategy → at most 4 DB queries (Q1 + Q2a + Q2b + Q3)."""
    strat = _make_strategy(1, "single")
    mock_session, execute_log = _make_mock_session(
        strategies=[(strat, None)],
        order_aggs=[_make_order_agg_row(1, 1, 2)],
        lot_aggs=[_make_lot_agg_row(1, 150.0, 0)],
        open_lots=[],
    )

    with patch("backend.api.routes.strategies.async_session",
               return_value=mock_session), \
         patch("backend.api.routes.strategies.cap_guard",
               return_value=lambda h: h):
        resp = await async_client.get("/api/strategies/")

    assert resp.status_code == 200, resp.text
    assert len(resp.json()["rows"]) == 1
    assert len(execute_log) <= 4, (
        f"1 strategy: expected ≤4 DB queries, got {len(execute_log)} "
        "(N+1 regression — old code fired up to 7)"
    )


@pytest.mark.asyncio
async def test_perf_ten_strategies_fires_at_most_four_queries(async_client):
    """Perf (key invariant): 10 strategies → SAME ≤4 DB queries as 1.

    The old code fired up to 7×10=70 queries for this case.
    """
    strategies = [(_make_strategy(i, f"strat-{i}"), None) for i in range(1, 11)]
    order_aggs = [_make_order_agg_row(i, i, i * 2) for i in range(1, 11)]
    lot_aggs = [_make_lot_agg_row(i, float(i * 15), 0) for i in range(1, 11)]

    mock_session, execute_log = _make_mock_session(
        strategies=strategies,
        order_aggs=order_aggs,
        lot_aggs=lot_aggs,
        open_lots=[],
    )

    with patch("backend.api.routes.strategies.async_session",
               return_value=mock_session), \
         patch("backend.api.routes.strategies.cap_guard",
               return_value=lambda h: h):
        resp = await async_client.get("/api/strategies/")

    assert resp.status_code == 200, resp.text
    assert len(resp.json()["rows"]) == 10
    assert len(execute_log) <= 4, (
        f"10 strategies: expected ≤4 DB queries (batch), got {len(execute_log)}. "
        "N+1 regression — query count must be constant regardless of N."
    )


@pytest.mark.asyncio
async def test_perf_query_count_invariant_n1_eq_n10(async_client):
    """Perf: query count for N=1 must equal query count for N=10.

    This is the core invariant of the batching fix.
    """

    async def _count_queries_for_n(n: int, client) -> int:
        strats = [(_make_strategy(i, f"s-{i}"), None) for i in range(1, n + 1)]
        oagg = [_make_order_agg_row(i, 0, 0) for i in range(1, n + 1)]
        lagg = [_make_lot_agg_row(i, 0.0, 0) for i in range(1, n + 1)]
        mock_session, elog = _make_mock_session(strats, oagg, lagg, [])
        with patch("backend.api.routes.strategies.async_session",
                   return_value=mock_session), \
             patch("backend.api.routes.strategies.cap_guard",
                   return_value=lambda h: h):
            r = await client.get("/api/strategies/")
        assert r.status_code == 200, r.text
        return len(elog)

    count_1 = await _count_queries_for_n(1, async_client)
    count_10 = await _count_queries_for_n(10, async_client)

    assert count_1 == count_10, (
        f"Query count for N=1 ({count_1}) != N=10 ({count_10}). "
        "Batching must produce a constant number of queries."
    )


# ── 5. UX — correct schema fields, no missing keys ───────────────────────────


@pytest.mark.asyncio
async def test_ux_response_schema_complete(async_client):
    """UX: each row in StrategiesResponse must contain all expected fields."""
    strat = _make_strategy(42, "full-schema-strat")
    order_agg = _make_order_agg_row(42, 2, 4)
    lot_agg = _make_lot_agg_row(42, 700.0, 1)

    open_lot = MagicMock()
    open_lot.strategy_id = 42
    open_lot.symbol = "NIFTY24500CE"
    open_lot.exchange = "NFO"
    open_lot.side = "B"
    open_lot.open_price = Decimal("120.0")
    open_lot.remaining_qty = 50

    mock_session, _ = _make_mock_session(
        strategies=[(strat, "trader1")],
        order_aggs=[order_agg],
        lot_aggs=[lot_agg],
        open_lots=[open_lot],
    )

    with patch("backend.api.routes.strategies.async_session",
               return_value=mock_session), \
         patch("backend.api.routes.strategies.cap_guard",
               return_value=lambda h: h), \
         patch("backend.brokers.kite_ticker.get_ticker",
               side_effect=Exception("no ticker in test")), \
         patch("backend.brokers.registry.get_price_broker",
               side_effect=Exception("no broker in test")):
        resp = await async_client.get("/api/strategies/")

    assert resp.status_code == 200, resp.text
    row = resp.json()["rows"][0]
    required_fields = {
        "id", "slug", "name", "description",
        "owner_user_id", "owner_username",
        "capacity_cap_inr", "target_volatility",
        "is_active",
        "open_order_count", "closed_order_count",
        "realised_pnl", "unrealised_pnl",
        "created_at", "updated_at",
    }
    missing = required_fields - set(row.keys())
    assert not missing, f"Missing fields in StrategyInfo response: {missing}"
    assert row["id"] == 42
    assert row["slug"] == "full-schema-strat"
    assert row["owner_username"] == "trader1"
    # realised from lot ledger (has_ledger=True, open_lots_count=1)
    assert row["realised_pnl"] == pytest.approx(700.0)
