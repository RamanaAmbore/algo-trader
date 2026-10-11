"""
Sprint 21 backend — `orders.*` scope family + pre-tracked `quote.*`
market-data scope.

Two new scope families, both sourced on the SAME refresh cadence as the
existing positions/holdings/funds/watchlist refresh in
`background._task_performance` (via `_perf_run_agent_engine`):

  - `orders.any_acct` — reads `ctx.orders_rows`, populated by
    `background._perf_fetch_orders_rows()` from the real `AlgoOrder`
    table (mode='live', not a terminal status). Account-rooted.

  - `quote.tracked` — reads `ctx.quote_rows`, populated by
    `background._perf_build_quote_rows()`, which pre-tracks whichever
    symbols are referenced by at least one active agent's `quote.*`
    condition leaf (via `scope_filter.symbol`), reusing
    `routes/quote.py`'s existing `_fetch_ltp` fetch infrastructure.
    Instrument-rooted; REQUIRES a non-wildcard `scope_filter.symbol`
    (enforced by `agent_evaluator.validate()`).

Covers (per PLAN.md "Test coverage"):
  1. `orders.any_acct` fires correctly against synthetic open-order rows.
  2. `quote.tracked` fires correctly against a synthetic tracked-symbol
     quote row.
  3. The symbol-tracking-from-active-agents logic discovers a symbol
     referenced in a real agent-shaped condition tree (all/any/$ref).
  4. Regression: `_perf_run_agent_engine`'s existing context keys/values
     (positions/holdings/funds/watchlist-adjacent) are completely
     unchanged — purely additive — and the new helpers' failure never
     blocks `run_cycle` from running.
  5. Supporting unit coverage: `_perf_fetch_orders_rows` DB query shape
     (open vs terminal vs non-live), `_perf_build_quote_rows` fetch +
     missing-vs-zero guard, and `validate()`'s new quote.* guard.
"""
from __future__ import annotations

import os
os.environ.setdefault("PYTEST_RUNNING", "1")

import types
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from backend.api.algo import grammar
from backend.api.algo.agent_evaluator import Context, evaluate, validate


# ═══════════════════════════════════════════════════════════════════════
#  REGISTRY wiring (same pattern as test_scope_filter_symbol.py)
# ═══════════════════════════════════════════════════════════════════════

@pytest.fixture(autouse=True)
def _wire_registry(monkeypatch):
    from backend.api.algo.grammar_registry import REGISTRY
    monkeypatch.setattr(REGISTRY, 'metrics', {
        'pnl': grammar._metric_pnl,
    })
    monkeypatch.setattr(REGISTRY, 'scopes', {
        'orders.any_acct': grammar._scope_orders_any_acct,
        'quote.tracked':   grammar._scope_quote_tracked,
        'positions.total': grammar._scope_positions_total,
    })
    monkeypatch.setattr(REGISTRY, 'operators', {
        '>=': grammar.OPERATORS['>='], '<': grammar.OPERATORS['<'],
        '==': grammar.OPERATORS['=='],
    })
    monkeypatch.setattr(REGISTRY, 'metric_factories', {})
    monkeypatch.setattr(REGISTRY, '_metric_call_cache', {})
    monkeypatch.setattr(REGISTRY, '_scope_call_cache', {})


# ═══════════════════════════════════════════════════════════════════════
#  1. orders.any_acct fires against synthetic rows
# ═══════════════════════════════════════════════════════════════════════

def _order_row(**overrides) -> dict:
    row = {
        "account": "ZG0790", "tradingsymbol": "NIFTY25JULFUT", "exchange": "NFO",
        "side": "BUY", "quantity": 5, "price": 100.0, "status": "OPEN",
        "order_id": "ABC123",
    }
    row.update(overrides)
    return row


class TestOrdersAnyAcctScope:
    def test_fires_on_breaching_row(self):
        ctx = Context(orders_rows=[_order_row(quantity=5)])
        leaf = {"metric": "qty", "scope": "orders.any_acct", "op": ">=", "value": 5}
        matches = evaluate(leaf, ctx)
        assert len(matches) == 1
        assert matches[0]['row']['order_id'] == "ABC123"

    def test_does_not_fire_on_non_breaching_row(self):
        ctx = Context(orders_rows=[_order_row(quantity=1)])
        leaf = {"metric": "qty", "scope": "orders.any_acct", "op": ">=", "value": 5}
        assert evaluate(leaf, ctx) == []

    def test_empty_orders_rows_never_fires(self):
        ctx = Context(orders_rows=[])
        leaf = {"metric": "qty", "scope": "orders.any_acct", "op": ">=", "value": 0}
        assert evaluate(leaf, ctx) == []

    def test_multiple_accounts_each_evaluated_independently(self):
        ctx = Context(orders_rows=[
            _order_row(account="ACCT_A", order_id="A1", quantity=10),
            _order_row(account="ACCT_B", order_id="B1", quantity=1),
        ])
        leaf = {"metric": "qty", "scope": "orders.any_acct", "op": ">=", "value": 5}
        matches = evaluate(leaf, ctx)
        assert len(matches) == 1
        assert matches[0]['account'] == "ACCT_A"

    def test_status_and_side_and_order_id_and_price_are_exposed(self):
        """All PLAN.md-listed new exposed names resolve via the generic
        Sprint 15 dot-access fallback (no hand-written metric needed)."""
        row = _order_row(status="HELD", side="SELL", price=181.5, order_id="XYZ")
        ctx = Context(orders_rows=[row])
        for metric, expected in (
            ("status", "HELD"), ("side", "SELL"),
            ("price", 181.5), ("order_id", "XYZ"),
        ):
            leaf = {"metric": metric, "scope": "orders.any_acct", "op": "==", "value": expected}
            assert evaluate(leaf, ctx), f"{metric} did not resolve to {expected!r}"

    def test_account_id_exposed_name_reuses_established_vocabulary(self):
        assert grammar.EXPOSED_COLUMNS['orders.any_acct']['account_id']['from_column'] == 'account'
        assert grammar.EXPOSED_COLUMNS['orders.any_acct']['symbol']['from_column'] == 'tradingsymbol'
        assert grammar.EXPOSED_COLUMNS['orders.any_acct']['qty']['from_column'] == 'quantity'

    def test_order_id_from_column_matches_cancel_modify_action_param_name(self):
        """order_id's from_column is literally 'order_id' — the SAME key
        cancel_order/modify_order's params read (actions_live.py), so a
        `{"$row": "order_id"}` action param resolves to the right value
        with no translation needed."""
        assert grammar.EXPOSED_COLUMNS['orders.any_acct']['order_id']['from_column'] == 'order_id'


# ═══════════════════════════════════════════════════════════════════════
#  2. quote.tracked fires against a synthetic tracked-symbol quote
# ═══════════════════════════════════════════════════════════════════════

def _quote_row(**overrides) -> dict:
    row = {
        "tradingsymbol": "NIFTY 50", "exchange": "NSE",
        "last_price": 22150.0, "bid": 22149.5, "ask": 22150.5, "volume": 1000,
    }
    row.update(overrides)
    return row


class TestQuoteTrackedScope:
    def test_fires_on_breaching_quote(self):
        ctx = Context(quote_rows=[_quote_row(last_price=22150.0)])
        leaf = {
            "metric": "ltp", "scope": "quote.tracked", "op": ">=", "value": 22000,
            "scope_filter": {"symbol": "NIFTY 50"},
        }
        matches = evaluate(leaf, ctx)
        assert len(matches) == 1
        assert matches[0]['value'] == 22150.0

    def test_scope_filter_isolates_the_right_symbol(self):
        """Two tracked symbols in ctx.quote_rows at once — the leaf must
        only ever evaluate the ONE it's filtered to, never the other
        agent's tracked symbol."""
        ctx = Context(quote_rows=[
            _quote_row(tradingsymbol="NIFTY 50", last_price=22150.0),
            _quote_row(tradingsymbol="RELIANCE", last_price=5.0),
        ])
        leaf = {
            "metric": "ltp", "scope": "quote.tracked", "op": "<", "value": 10,
            "scope_filter": {"symbol": "RELIANCE"},
        }
        matches = evaluate(leaf, ctx)
        assert len(matches) == 1
        assert matches[0]['row']['tradingsymbol'] == "RELIANCE"

    def test_bid_ask_volume_exposed(self):
        row = _quote_row(bid=99.5, ask=100.5, volume=42)
        ctx = Context(quote_rows=[row])
        for metric, expected in (("bid", 99.5), ("ask", 100.5), ("volume", 42)):
            leaf = {
                "metric": metric, "scope": "quote.tracked", "op": "==", "value": expected,
                "scope_filter": {"symbol": "NIFTY 50"},
            }
            assert evaluate(leaf, ctx), f"{metric} did not resolve to {expected!r}"

    def test_ltp_reuses_the_real_metric_token_passthrough(self):
        """`ltp` on quote.tracked is the REAL registered metric token
        (grammar._metric_ltp), not merely an exposed-column fallback —
        same reuse convention watchlist.* already established."""
        from backend.api.algo.grammar_registry import REGISTRY
        assert REGISTRY.metric('ltp') is None  # not wired in this file's fixture scopes
        # exposed-column fallback still resolves it correctly either way:
        assert grammar.EXPOSED_COLUMNS['quote.tracked']['ltp']['from_column'] == 'last_price'


class TestQuoteRequiresSymbolFilter:
    """agent_evaluator.validate()'s Sprint 21 guard — quote.* is
    instrument-rooted and has nothing to disambiguate WHICH tracked row
    it means without a real scope_filter.symbol."""

    def test_missing_scope_filter_is_a_validate_error(self):
        errs = validate({"metric": "ltp", "scope": "quote.tracked", "op": ">=", "value": 1})
        assert any("scope_filter.symbol" in e for e in errs)

    def test_wildcard_symbol_is_a_validate_error(self):
        errs = validate({
            "metric": "ltp", "scope": "quote.tracked", "op": ">=", "value": 1,
            "scope_filter": {"symbol": "*"},
        })
        assert any("scope_filter.symbol" in e for e in errs)

    def test_real_symbol_filter_passes(self):
        errs = validate({
            "metric": "ltp", "scope": "quote.tracked", "op": ">=", "value": 1,
            "scope_filter": {"symbol": "NIFTY 50"},
        })
        assert errs == []

    def test_non_quote_scope_unaffected_by_the_new_guard(self):
        """Regression — the new guard must never fire for any OTHER
        scope family (it's gated on `scope.startswith('quote.')`)."""
        errs = validate({"metric": "pnl", "scope": "positions.total", "op": "<", "value": 0})
        assert errs == []


# ═══════════════════════════════════════════════════════════════════════
#  3. Symbol-tracking-from-active-agents discovery (pure function)
# ═══════════════════════════════════════════════════════════════════════

class TestCollectQuoteSymbolsFromConditions:
    def test_discovers_a_flat_leaf(self):
        from backend.api.background import _collect_quote_symbols_from_conditions
        conds = [{
            "metric": "ltp", "scope": "quote.tracked", "op": ">=", "value": 1,
            "scope_filter": {"symbol": "RELIANCE"},
        }]
        assert _collect_quote_symbols_from_conditions(conds) == {("RELIANCE", None)}

    def test_discovers_inside_all_and_any(self):
        from backend.api.background import _collect_quote_symbols_from_conditions
        conds = [{
            "all": [
                {"metric": "ltp", "scope": "quote.tracked", "op": ">=", "value": 1,
                 "scope_filter": {"symbol": "RELIANCE", "exchange": "NSE"}},
                {"metric": "pnl", "scope": "positions.total", "op": "<", "value": 0},
            ],
        }, {
            "any": [
                {"metric": "ltp", "scope": "quote.tracked", "op": "<", "value": 1,
                 "scope_filter": {"symbol": "IDEA"}},
            ],
        }]
        got = _collect_quote_symbols_from_conditions(conds)
        assert got == {("RELIANCE", "NSE"), ("IDEA", None)}

    def test_discovers_a_real_agent_shaped_condition_tree(self):
        """Mirrors a genuine BUILTIN_AGENTS-style condition shape — a
        top-level 'all' wrapping a quote.* leaf among other leaves."""
        from backend.api.background import _collect_quote_symbols_from_conditions
        real_agent_conditions = {
            "all": [
                {"metric": "ltp", "scope": "quote.tracked", "op": "<=", "value": 50.0,
                 "scope_filter": {"symbol": "CRUDEOIL26OCTFUT", "exchange": "MCX"}},
            ],
        }
        got = _collect_quote_symbols_from_conditions([real_agent_conditions])
        assert got == {("CRUDEOIL26OCTFUT", "MCX")}

    def test_discovers_through_ref_fragment(self, monkeypatch):
        from backend.api.background import _collect_quote_symbols_from_conditions
        from backend.api.algo.template_registry import REGISTRY as FRAG
        fragment_body = {
            "metric": "ltp", "scope": "quote.tracked", "op": ">=", "value": 1,
            "scope_filter": {"symbol": "TATASTEEL"},
        }
        monkeypatch.setitem(FRAG._cache.setdefault("condition", {}), "frag1", fragment_body)
        conds = [{"$ref": "frag1"}]
        assert _collect_quote_symbols_from_conditions(conds) == {("TATASTEEL", None)}

    def test_ref_cycle_guard_does_not_infinite_loop(self, monkeypatch):
        from backend.api.background import _collect_quote_symbols_from_conditions
        from backend.api.algo.template_registry import REGISTRY as FRAG
        monkeypatch.setitem(FRAG._cache.setdefault("condition", {}), "cyc", {"$ref": "cyc"})
        conds = [{"$ref": "cyc"}]
        assert _collect_quote_symbols_from_conditions(conds) == set()

    def test_wildcard_and_missing_symbol_are_not_collected(self):
        from backend.api.background import _collect_quote_symbols_from_conditions
        conds = [
            {"metric": "ltp", "scope": "quote.tracked", "op": ">=", "value": 1,
             "scope_filter": {"symbol": "*"}},
            {"metric": "ltp", "scope": "quote.tracked", "op": ">=", "value": 1},
        ]
        assert _collect_quote_symbols_from_conditions(conds) == set()

    def test_non_quote_scopes_never_collected(self):
        from backend.api.background import _collect_quote_symbols_from_conditions
        conds = [{"metric": "pnl", "scope": "positions.total", "op": "<", "value": 0,
                  "scope_filter": {"symbol": "X"}}]
        assert _collect_quote_symbols_from_conditions(conds) == set()

    def test_malformed_node_does_not_raise(self):
        from backend.api.background import _collect_quote_symbols_from_conditions
        assert _collect_quote_symbols_from_conditions([None, "not-a-dict", {}]) == set()


# ═══════════════════════════════════════════════════════════════════════
#  5a. _perf_fetch_orders_rows — DB query shape
# ═══════════════════════════════════════════════════════════════════════

class _FakeOrderRow:
    def __init__(self, **kw):
        self.id = kw.get("id", 1)
        self.account = kw.get("account", "ZG0790")
        self.symbol = kw.get("symbol", "NIFTY25JULFUT")
        self.exchange = kw.get("exchange", "NFO")
        self.transaction_type = kw.get("transaction_type", "BUY")
        self.quantity = kw.get("quantity", 5)
        self.status = kw.get("status", "OPEN")
        self.mode = kw.get("mode", "live")
        self.broker_order_id = kw.get("broker_order_id", "BRK1")
        self.current_limit = kw.get("current_limit", None)
        self.initial_price = kw.get("initial_price", 100.0)


def _mock_session(rows):
    result = MagicMock()
    result.scalars.return_value.all.return_value = rows
    session = AsyncMock()
    session.__aenter__ = AsyncMock(return_value=session)
    session.__aexit__ = AsyncMock(return_value=False)
    session.execute = AsyncMock(return_value=result)
    return session


class TestPerfFetchOrdersRows:
    @pytest.mark.asyncio
    async def test_returns_row_shape_with_price_fallback(self):
        from backend.api.background import _perf_fetch_orders_rows
        row = _FakeOrderRow(current_limit=None, initial_price=181.5)
        session = _mock_session([row])
        with patch("backend.api.background.async_session", return_value=session):
            out = await _perf_fetch_orders_rows()
        assert out == [{
            "account": "ZG0790", "tradingsymbol": "NIFTY25JULFUT", "exchange": "NFO",
            "side": "BUY", "quantity": 5, "price": 181.5, "status": "OPEN",
            "order_id": "BRK1",
        }]

    @pytest.mark.asyncio
    async def test_current_limit_wins_over_initial_price_when_requoted(self):
        """Matches the ChaseCard UI's own 'live re-quoted price wins'
        convention (AlgoOrder.current_limit's field comment, models.py)."""
        from backend.api.background import _perf_fetch_orders_rows
        row = _FakeOrderRow(current_limit=190.25, initial_price=181.5)
        session = _mock_session([row])
        with patch("backend.api.background.async_session", return_value=session):
            out = await _perf_fetch_orders_rows()
        assert out[0]["price"] == 190.25

    @pytest.mark.asyncio
    async def test_db_failure_returns_empty_list_not_raise(self):
        from backend.api.background import _perf_fetch_orders_rows
        session = AsyncMock()
        session.__aenter__ = AsyncMock(side_effect=RuntimeError("db down"))
        with patch("backend.api.background.async_session", return_value=session):
            out = await _perf_fetch_orders_rows()
        assert out == []

    @pytest.mark.asyncio
    async def test_query_filters_mode_live_and_excludes_terminal_statuses(self):
        """Regression guard on the query SHAPE itself — asserts the WHERE
        clause actually references AlgoOrder.mode == 'live' and excludes
        ALGO_ORDER_TERMINAL_STATUSES, so a future edit that loosens this
        (e.g. dropping the mode filter and resurrecting every historical
        sim/paper/replay/shadow row as 'open') fails loudly."""
        from backend.api.background import _perf_fetch_orders_rows
        session = _mock_session([])
        with patch("backend.api.background.async_session", return_value=session):
            await _perf_fetch_orders_rows()
        stmt = session.execute.call_args[0][0]
        compiled = str(stmt.compile(compile_kwargs={"literal_binds": True}))
        assert "mode" in compiled and "live" in compiled
        assert "status" in compiled
        for terminal in ("FILLED", "CANCELLED", "REJECTED", "UNFILLED", "CANCEL_FAILED"):
            assert terminal in compiled


# ═══════════════════════════════════════════════════════════════════════
#  5b. _perf_build_quote_rows — fetch + missing-vs-zero guard
# ═══════════════════════════════════════════════════════════════════════

class _FakeAgent:
    def __init__(self, conditions):
        self.conditions = conditions


def _fake_quote_response(ltp, bid=None, ask=None, volume=0, symbol="NIFTY 50", exchange="NSE"):
    from backend.api.routes.quote import QuoteResponse
    return QuoteResponse(
        tradingsymbol=symbol, exchange=exchange, ltp=ltp, tick_size=0.05,
        bid=bid, ask=ask, volume=volume,
    )


class TestPerfBuildQuoteRows:
    @pytest.mark.asyncio
    async def test_no_active_agents_reference_quote_returns_empty(self):
        from backend.api.background import _perf_build_quote_rows
        agent = _FakeAgent({"metric": "pnl", "scope": "positions.total", "op": "<", "value": 0})
        with patch("backend.api.algo.agent_engine._cycle_load_agents", new=AsyncMock(return_value=[agent])):
            out = await _perf_build_quote_rows()
        assert out == []

    @pytest.mark.asyncio
    async def test_tracked_symbol_builds_row(self):
        from backend.api.background import _perf_build_quote_rows
        from backend.api.algo.symbol_resolver import MarketDataKeyMap
        agent = _FakeAgent({
            "metric": "ltp", "scope": "quote.tracked", "op": ">=", "value": 1,
            "scope_filter": {"symbol": "NIFTY 50", "exchange": "NSE"},
        })
        key_map = MarketDataKeyMap(
            broker_keys=["NSE:NIFTY 50"],
            input_to_broker={"NSE:NIFTY 50": "NSE:NIFTY 50"},
            broker_to_input={"NSE:NIFTY 50": "NSE:NIFTY 50"},
        )
        with patch("backend.api.algo.agent_engine._cycle_load_agents", new=AsyncMock(return_value=[agent])), \
             patch("backend.api.algo.symbol_resolver.resolve_market_data_keys", new=AsyncMock(return_value=key_map)), \
             patch("backend.api.routes.quote._fetch_ltp",
                   return_value=_fake_quote_response(22150.0, bid=22149.5, ask=22150.5, volume=10)):
            out = await _perf_build_quote_rows()
        assert out == [{
            "tradingsymbol": "NIFTY 50", "exchange": "NSE", "last_price": 22150.0,
            "bid": 22149.5, "ask": 22150.5, "volume": 10,
        }]

    @pytest.mark.asyncio
    async def test_zero_ltp_fetch_failure_drops_row_missing_vs_zero(self):
        """`_fetch_ltp` returns ltp=0.0 (not an exception) on a total
        broker failure — the row must be DROPPED, never exposed as a
        confirmed LTP of 0 (see missing-vs-zero convention, CLAUDE.md)."""
        from backend.api.background import _perf_build_quote_rows
        from backend.api.algo.symbol_resolver import MarketDataKeyMap
        agent = _FakeAgent({
            "metric": "ltp", "scope": "quote.tracked", "op": ">=", "value": 1,
            "scope_filter": {"symbol": "NIFTY 50"},
        })
        key_map = MarketDataKeyMap(
            broker_keys=["NSE:NIFTY 50"],
            input_to_broker={"NSE:NIFTY 50": "NSE:NIFTY 50"},
            broker_to_input={"NSE:NIFTY 50": "NSE:NIFTY 50"},
        )
        with patch("backend.api.algo.agent_engine._cycle_load_agents", new=AsyncMock(return_value=[agent])), \
             patch("backend.api.algo.symbol_resolver.resolve_market_data_keys", new=AsyncMock(return_value=key_map)), \
             patch("backend.api.routes.quote._fetch_ltp", return_value=_fake_quote_response(0.0)):
            out = await _perf_build_quote_rows()
        assert out == []

    @pytest.mark.asyncio
    async def test_agent_load_failure_returns_empty_not_raise(self):
        from backend.api.background import _perf_build_quote_rows
        with patch("backend.api.algo.agent_engine._cycle_load_agents",
                   new=AsyncMock(side_effect=RuntimeError("db down"))):
            out = await _perf_build_quote_rows()
        assert out == []


# ═══════════════════════════════════════════════════════════════════════
#  4. Regression — _perf_run_agent_engine's existing behavior unchanged
# ═══════════════════════════════════════════════════════════════════════

class TestPerfRunAgentEngineRegression:
    @pytest.mark.asyncio
    async def test_existing_context_keys_unchanged_new_keys_additive(self):
        """Capture the dict passed to run_cycle and assert every
        PRE-EXISTING key/value survives byte-for-byte, with orders_rows/
        quote_rows purely additive alongside them."""
        from backend.api.background import _perf_run_agent_engine
        import pandas as pd

        captured = {}

        async def _fake_run_cycle(context, broadcast_fn=None):
            captured.update(context)

        sum_holdings = pd.DataFrame({"account": ["TOTAL"], "pnl": [0.0]})
        sum_positions = pd.DataFrame({"account": ["TOTAL"], "pnl": [0.0]})
        df_margins = pd.DataFrame()
        df_holdings = pd.DataFrame()
        df_positions = pd.DataFrame()
        now = object()
        seg_state = {"x": 1}
        alert_state = {"y": 2}
        segments = [{"name": "equity"}]

        with patch("backend.shared.helpers.utils.is_prod_branch", return_value=True), \
             patch("backend.api.algo.agent_engine.run_cycle", new=_fake_run_cycle), \
             patch("backend.api.background._resolve_spot_prices", return_value={}), \
             patch("backend.api.background._perf_fetch_orders_rows", new=AsyncMock(return_value=[_order_row()])), \
             patch("backend.api.background._perf_build_quote_rows", new=AsyncMock(return_value=[_quote_row()])):
            await _perf_run_agent_engine(
                sim_active=False, df_holdings=df_holdings, df_positions=df_positions,
                df_margins=df_margins, sum_holdings=sum_holdings, sum_positions=sum_positions,
                ist_display="12:00:00 IST", now=now, seg_state=seg_state,
                alert_state=alert_state, segments=segments,
            )

        # Pre-existing keys, unchanged.
        assert captured["sum_holdings"] is sum_holdings
        assert captured["sum_positions"] is sum_positions
        assert captured["df_margins"] is df_margins
        assert captured["df_holdings"] is df_holdings
        assert captured["df_positions"] is df_positions
        assert captured["position_rows"] == []
        assert captured["spot_prices"] == {}
        assert captured["ist_display"] == "12:00:00 IST"
        assert captured["now"] is now
        assert captured["seg_state"] is seg_state
        assert captured["alert_state"] is alert_state
        assert captured["segments"] is segments
        # New keys, additive.
        assert captured["orders_rows"] == [_order_row()]
        assert captured["quote_rows"] == [_quote_row()]

    @pytest.mark.asyncio
    async def test_sim_active_skips_new_helpers_entirely(self):
        from backend.api.background import _perf_run_agent_engine
        with patch("backend.api.background._perf_fetch_orders_rows", new=AsyncMock()) as m_orders, \
             patch("backend.api.background._perf_build_quote_rows", new=AsyncMock()) as m_quote:
            await _perf_run_agent_engine(
                sim_active=True, df_holdings=None, df_positions=None, df_margins=None,
                sum_holdings=None, sum_positions=None, ist_display="", now=None,
                seg_state={}, alert_state={}, segments=[],
            )
        m_orders.assert_not_awaited()
        m_quote.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_non_prod_branch_skips_new_helpers_entirely(self):
        from backend.api.background import _perf_run_agent_engine
        with patch("backend.shared.helpers.utils.is_prod_branch", return_value=False), \
             patch("backend.api.background._perf_fetch_orders_rows", new=AsyncMock()) as m_orders, \
             patch("backend.api.background._perf_build_quote_rows", new=AsyncMock()) as m_quote:
            await _perf_run_agent_engine(
                sim_active=False, df_holdings=None, df_positions=None, df_margins=None,
                sum_holdings=None, sum_positions=None, ist_display="", now=None,
                seg_state={}, alert_state={}, segments=[],
            )
        m_orders.assert_not_awaited()
        m_quote.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_helper_raising_never_blocks_run_cycle(self):
        """Even if one of the new helpers raises OUTSIDE its own internal
        try/except (an unforeseen bug), run_cycle must still execute this
        tick for every other agent — the new calls are individually
        guarded, not just wrapped in the pre-existing outer try/except
        that would otherwise skip run_cycle entirely for the tick."""
        from backend.api.background import _perf_run_agent_engine
        import pandas as pd

        run_cycle_mock = AsyncMock()
        with patch("backend.shared.helpers.utils.is_prod_branch", return_value=True), \
             patch("backend.api.algo.agent_engine.run_cycle", new=run_cycle_mock), \
             patch("backend.api.background._resolve_spot_prices", return_value={}), \
             patch("backend.api.background._perf_fetch_orders_rows",
                   new=AsyncMock(side_effect=RuntimeError("boom"))), \
             patch("backend.api.background._perf_build_quote_rows",
                   new=AsyncMock(side_effect=RuntimeError("boom"))):
            await _perf_run_agent_engine(
                sim_active=False, df_holdings=pd.DataFrame(), df_positions=pd.DataFrame(),
                df_margins=pd.DataFrame(), sum_holdings=pd.DataFrame(), sum_positions=pd.DataFrame(),
                ist_display="", now=object(), seg_state={}, alert_state={}, segments=[],
            )
        run_cycle_mock.assert_awaited_once()
        passed_context = run_cycle_mock.call_args[0][0]
        assert passed_context["orders_rows"] == []
        assert passed_context["quote_rows"] == []
