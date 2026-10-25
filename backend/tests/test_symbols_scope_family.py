"""
Sprint G backend — `symbols.*` scope family (watchlist/pinned/gainers/
losers unified behind one scope token + a required `kind` qualifier).

  - `symbols.all` — reads `ctx.symbols_rows`, populated once per
    `_task_performance` cycle by `background._perf_build_symbols_rows`,
    which reuses:
      * `background._fetch_watchlist_rows_raw` (extracted from the
        existing `_perf_fetch_watchlist_rows`) for the 'watchlist'/
        'pinned' kinds — 'pinned' is the rows belonging to the real
        global Pinned watchlist (`Watchlist.is_global == True`, always
        named "Pinned"), every other named list is 'watchlist'.
      * `routes/watchlist._get_movers_rows` (extracted from the real
        `GET /watchlist/movers` controller body) for the 'gainers'/
        'losers' kinds — split by the sign of `MoverRow.change_pct`,
        the SAME classification `_combine_movers` already uses
        internally (no second winners/losers implementation).
  - REQUIRES a non-wildcard `scope_filter.kind` (enforced by
    `agent_evaluator.validate()`), mirroring the existing `quote.*`
    required-`symbol` guard — the four kinds carry materially different
    row populations, so an unfiltered leaf would be meaningless.

Covers:
  1. `symbols.all` fires correctly against synthetic rows, scope_filter
     isolating one kind at a time.
  2. `validate()` rejects a `symbols.*` leaf with no/wildcard
     scope_filter.kind, mirrors quote.*'s own guard, unaffected for
     every other scope.
  3. `_collect_symbols_kinds_from_conditions` pure-walk unit coverage
     (all/any/$ref recursion, wildcard/absent kind ignored).
  4. `_perf_build_symbols_rows` gating: empty when no active agent
     references `symbols.*`; builds ONLY the kind(s) actually
     referenced; agent-load failure degrades to [] rather than raising.
  5. Row-shaping unit coverage: watchlist fraction -> percent
     normalisation, pinned-vs-watchlist kind split, movers day_change
     derivation.
"""
from __future__ import annotations

import os
os.environ.setdefault("PYTEST_RUNNING", "1")

from unittest.mock import AsyncMock, patch

import pytest

from backend.api.algo import grammar
from backend.api.algo.agent_evaluator import Context, evaluate, validate
from backend.api.algo.grammar_tokens.metric_pnl import _metric_pnl
from backend.api.algo.grammar_tokens.scope_positions_total import (
    _scope_positions_total,
)


# ═══════════════════════════════════════════════════════════════════════
#  REGISTRY wiring (same pattern as test_orders_quote_scopes.py)
# ═══════════════════════════════════════════════════════════════════════

@pytest.fixture(autouse=True)
def _wire_registry(monkeypatch):
    from backend.api.algo.grammar_registry import REGISTRY
    monkeypatch.setattr(REGISTRY, 'metrics', {
        'pnl': _metric_pnl,
    })
    monkeypatch.setattr(REGISTRY, 'scopes', {
        'symbols.all':     grammar._scope_symbols_all,
        'positions.total': _scope_positions_total,
        'quote.tracked':   grammar._scope_quote_tracked,
    })
    monkeypatch.setattr(REGISTRY, 'operators', {
        '>=': grammar.OPERATORS['>='], '<': grammar.OPERATORS['<'],
        '==': grammar.OPERATORS['=='],
    })
    monkeypatch.setattr(REGISTRY, 'metric_factories', {})
    monkeypatch.setattr(REGISTRY, '_metric_call_cache', {})
    monkeypatch.setattr(REGISTRY, '_scope_call_cache', {})


def _symbol_row(**overrides) -> dict:
    row = {
        "kind": "watchlist", "tradingsymbol": "NIFTY", "exchange": "NSE",
        "last_price": 100.0, "day_change": 1.0, "day_change_percentage": 1.0,
    }
    row.update(overrides)
    return row


# ═══════════════════════════════════════════════════════════════════════
#  1. symbols.all + scope_filter.kind isolation
# ═══════════════════════════════════════════════════════════════════════

class TestSymbolsAllScope:
    def test_fires_on_breaching_row_of_requested_kind(self):
        ctx = Context(symbols_rows=[
            _symbol_row(kind="gainers", tradingsymbol="RELIANCE", day_change_percentage=3.0),
            _symbol_row(kind="losers", tradingsymbol="TATASTEEL", day_change_percentage=-4.0),
        ])
        leaf = {
            "metric": "day_change_pct", "scope": "symbols.all", "op": "<", "value": 0,
            "scope_filter": {"kind": "losers"},
        }
        matches = evaluate(leaf, ctx)
        assert len(matches) == 1
        assert matches[0]["row"]["tradingsymbol"] == "TATASTEEL"

    def test_kind_filter_excludes_other_kinds_even_if_they_breach(self):
        """A gainers row with a (contrived) negative day_change_pct must
        never leak through a `kind: losers` filter."""
        ctx = Context(symbols_rows=[
            _symbol_row(kind="gainers", tradingsymbol="INFY", day_change_percentage=-1.0),
        ])
        leaf = {
            "metric": "day_change_pct", "scope": "symbols.all", "op": "<", "value": 0,
            "scope_filter": {"kind": "losers"},
        }
        assert evaluate(leaf, ctx) == []

    def test_pinned_and_watchlist_kinds_are_distinct(self):
        ctx = Context(symbols_rows=[
            _symbol_row(kind="pinned", tradingsymbol="NIFTY"),
            _symbol_row(kind="watchlist", tradingsymbol="BANKNIFTY"),
        ])
        leaf = {
            "metric": "ltp", "scope": "symbols.all", "op": ">=", "value": 0,
            "scope_filter": {"kind": "pinned"},
        }
        matches = evaluate(leaf, ctx)
        assert len(matches) == 1
        assert matches[0]["row"]["tradingsymbol"] == "NIFTY"

    def test_empty_symbols_rows_never_fires(self):
        ctx = Context(symbols_rows=[])
        leaf = {
            "metric": "ltp", "scope": "symbols.all", "op": ">=", "value": 0,
            "scope_filter": {"kind": "gainers"},
        }
        assert evaluate(leaf, ctx) == []

    def test_exposed_columns_include_kind_symbol_exchange_ltp_day_change(self):
        schema = grammar.exposed_columns_for_scope("symbols.all")
        for name in ("kind", "symbol", "exchange", "ltp", "day_change", "day_change_pct"):
            assert name in schema


# ═══════════════════════════════════════════════════════════════════════
#  2. validate() requires a non-wildcard scope_filter.kind
# ═══════════════════════════════════════════════════════════════════════

class TestValidateRequiresKind:
    def test_missing_scope_filter_rejected(self):
        cond = {"metric": "ltp", "scope": "symbols.all", "op": ">=", "value": 0}
        errors = validate(cond)
        assert len(errors) == 1
        assert "scope_filter.kind" in errors[0]

    def test_wildcard_kind_rejected(self):
        cond = {
            "metric": "ltp", "scope": "symbols.all", "op": ">=", "value": 0,
            "scope_filter": {"kind": "*"},
        }
        errors = validate(cond)
        assert len(errors) == 1
        assert "scope_filter.kind" in errors[0]

    def test_real_kind_accepted(self):
        cond = {
            "metric": "ltp", "scope": "symbols.all", "op": ">=", "value": 0,
            "scope_filter": {"kind": "gainers"},
        }
        assert validate(cond) == []

    def test_unrelated_scope_unaffected(self):
        cond = {"metric": "pnl", "scope": "positions.total", "op": "<", "value": 0}
        assert validate(cond) == []

    def test_quote_guard_still_independent_of_symbols_guard(self):
        """Regression — adding the symbols.* guard must not disturb the
        pre-existing quote.* required-symbol guard."""
        cond = {"metric": "ltp", "scope": "quote.tracked", "op": ">=", "value": 0}
        errors = validate(cond)
        assert len(errors) == 1
        assert "scope_filter.symbol" in errors[0]


# ═══════════════════════════════════════════════════════════════════════
#  3. _collect_symbols_kinds_from_conditions — pure walk
# ═══════════════════════════════════════════════════════════════════════

class TestCollectSymbolsKindsFromConditions:
    def test_simple_leaf(self):
        from backend.api.background import _collect_symbols_kinds_from_conditions
        conds = [{
            "metric": "ltp", "scope": "symbols.all", "op": ">=", "value": 0,
            "scope_filter": {"kind": "gainers"},
        }]
        assert _collect_symbols_kinds_from_conditions(conds) == {"gainers"}

    def test_all_any_not_recursion(self):
        from backend.api.background import _collect_symbols_kinds_from_conditions
        conds = [{
            "all": [
                {"any": [
                    {"metric": "ltp", "scope": "symbols.all", "op": ">=", "value": 0,
                     "scope_filter": {"kind": "watchlist"}},
                    {"not": {
                        "metric": "day_change_pct", "scope": "symbols.all", "op": "<", "value": 0,
                        "scope_filter": {"kind": "pinned"},
                    }},
                ]},
                {"metric": "pnl", "scope": "positions.total", "op": "<", "value": -1000},
            ]
        }]
        assert _collect_symbols_kinds_from_conditions(conds) == {"watchlist", "pinned"}

    def test_wildcard_kind_ignored(self):
        from backend.api.background import _collect_symbols_kinds_from_conditions
        conds = [{
            "metric": "ltp", "scope": "symbols.all", "op": ">=", "value": 0,
            "scope_filter": {"kind": "*"},
        }]
        assert _collect_symbols_kinds_from_conditions(conds) == set()

    def test_no_symbols_scope_returns_empty(self):
        from backend.api.background import _collect_symbols_kinds_from_conditions
        conds = [{"metric": "pnl", "scope": "positions.total", "op": "<", "value": 0}]
        assert _collect_symbols_kinds_from_conditions(conds) == set()

    def test_malformed_input_does_not_raise(self):
        from backend.api.background import _collect_symbols_kinds_from_conditions
        assert _collect_symbols_kinds_from_conditions([None, "not-a-dict", {}]) == set()

    def test_case_normalised_lowercase(self):
        from backend.api.background import _collect_symbols_kinds_from_conditions
        conds = [{
            "metric": "ltp", "scope": "symbols.all", "op": ">=", "value": 0,
            "scope_filter": {"kind": "GAINERS"},
        }]
        assert _collect_symbols_kinds_from_conditions(conds) == {"gainers"}


# ═══════════════════════════════════════════════════════════════════════
#  4. _perf_build_symbols_rows — gating + kind-scoped building
# ═══════════════════════════════════════════════════════════════════════

class _FakeAgent:
    def __init__(self, conditions):
        self.conditions = conditions


class TestPerfBuildSymbolsRows:
    @pytest.mark.asyncio
    async def test_no_active_agent_references_symbols_returns_empty(self):
        from backend.api.background import _perf_build_symbols_rows
        agent = _FakeAgent({"metric": "pnl", "scope": "positions.total", "op": "<", "value": 0})
        with patch("backend.api.algo.agent_engine._cycle_load_agents", new=AsyncMock(return_value=[agent])):
            out = await _perf_build_symbols_rows()
        assert out == []

    @pytest.mark.asyncio
    async def test_agent_load_failure_returns_empty_not_raise(self):
        from backend.api.background import _perf_build_symbols_rows
        with patch("backend.api.algo.agent_engine._cycle_load_agents",
                   new=AsyncMock(side_effect=RuntimeError("db down"))):
            out = await _perf_build_symbols_rows()
        assert out == []

    @pytest.mark.asyncio
    async def test_only_watchlist_kind_referenced_skips_movers_fetch(self):
        """An install referencing only `kind:watchlist` must never pay the
        movers fetch cost — the gainers/losers helper is never called."""
        from backend.api.background import _perf_build_symbols_rows
        agent = _FakeAgent({
            "metric": "ltp", "scope": "symbols.all", "op": ">=", "value": 0,
            "scope_filter": {"kind": "watchlist"},
        })
        raw = [{"account": "Default", "tradingsymbol": "NIFTY", "exchange": "NSE",
                "last_price": 100.0, "day_change": 1.0, "day_change_percentage": 0.01}]
        with patch("backend.api.algo.agent_engine._cycle_load_agents", new=AsyncMock(return_value=[agent])), \
             patch("backend.api.background._fetch_watchlist_rows_raw", new=AsyncMock(return_value=raw)) as m_wl, \
             patch("backend.api.background._symbols_rows_from_movers", new=AsyncMock(return_value=[])) as m_mv:
            out = await _perf_build_symbols_rows()
        m_wl.assert_awaited_once()
        m_mv.assert_not_called()
        assert out == [{
            "tradingsymbol": "NIFTY", "exchange": "NSE", "last_price": 100.0,
            "day_change": 1.0, "day_change_percentage": 1.0, "kind": "watchlist",
        }]

    @pytest.mark.asyncio
    async def test_only_gainers_kind_referenced_skips_watchlist_fetch(self):
        from backend.api.background import _perf_build_symbols_rows
        agent = _FakeAgent({
            "metric": "ltp", "scope": "symbols.all", "op": ">=", "value": 0,
            "scope_filter": {"kind": "gainers"},
        })
        with patch("backend.api.algo.agent_engine._cycle_load_agents", new=AsyncMock(return_value=[agent])), \
             patch("backend.api.background._fetch_watchlist_rows_raw", new=AsyncMock(return_value=[])) as m_wl, \
             patch("backend.api.background._symbols_rows_from_movers",
                   new=AsyncMock(return_value=[_symbol_row(kind="gainers")])) as m_mv:
            out = await _perf_build_symbols_rows()
        m_wl.assert_not_called()
        m_mv.assert_awaited_once_with({"gainers"})
        assert out == [_symbol_row(kind="gainers")]

    @pytest.mark.asyncio
    async def test_watchlist_fetch_failure_degrades_to_empty_not_raise(self):
        from backend.api.background import _perf_build_symbols_rows
        agent = _FakeAgent({
            "metric": "ltp", "scope": "symbols.all", "op": ">=", "value": 0,
            "scope_filter": {"kind": "pinned"},
        })
        with patch("backend.api.algo.agent_engine._cycle_load_agents", new=AsyncMock(return_value=[agent])), \
             patch("backend.api.background._fetch_watchlist_rows_raw",
                   new=AsyncMock(side_effect=RuntimeError("db down"))):
            out = await _perf_build_symbols_rows()
        assert out == []


# ═══════════════════════════════════════════════════════════════════════
#  5. Row-shaping unit coverage
# ═══════════════════════════════════════════════════════════════════════

class TestSymbolsRowsFromWatchlistRaw:
    def test_pinned_name_maps_to_pinned_kind(self):
        from backend.api.background import (
            _symbols_rows_from_watchlist_raw, _SYMBOLS_PINNED_WATCHLIST_NAME,
        )
        raw = [{"account": _SYMBOLS_PINNED_WATCHLIST_NAME, "tradingsymbol": "NIFTY",
                "exchange": "NSE", "last_price": 100.0, "day_change": 1.0,
                "day_change_percentage": 0.01}]
        out = _symbols_rows_from_watchlist_raw(raw)
        assert out[0]["kind"] == "pinned"

    def test_other_name_maps_to_watchlist_kind(self):
        from backend.api.background import _symbols_rows_from_watchlist_raw
        raw = [{"account": "Markets", "tradingsymbol": "NIFTY", "exchange": "NSE",
                "last_price": 100.0, "day_change": 1.0, "day_change_percentage": 0.01}]
        out = _symbols_rows_from_watchlist_raw(raw)
        assert out[0]["kind"] == "watchlist"

    def test_fraction_normalised_to_percent(self):
        """The raw watchlist row's own day_change_percentage is a FRACTION
        (-0.02 = -2%, see grammar.py's _WATCHLIST_COLUMNS collision-guard
        note) — must be normalised to a true percent in symbols_rows."""
        from backend.api.background import _symbols_rows_from_watchlist_raw
        raw = [{"account": "Default", "tradingsymbol": "NIFTY", "exchange": "NSE",
                "last_price": 100.0, "day_change": -2.0, "day_change_percentage": -0.02}]
        out = _symbols_rows_from_watchlist_raw(raw)
        assert out[0]["day_change_percentage"] == pytest.approx(-2.0)

    def test_missing_fraction_stays_none(self):
        from backend.api.background import _symbols_rows_from_watchlist_raw
        raw = [{"account": "Default", "tradingsymbol": "NIFTY", "exchange": "NSE",
                "last_price": 100.0, "day_change": None, "day_change_percentage": None}]
        out = _symbols_rows_from_watchlist_raw(raw)
        assert out[0]["day_change_percentage"] is None


class TestSymbolsRowsFromMovers:
    @pytest.mark.asyncio
    async def test_splits_by_sign_of_change_pct(self):
        from backend.api.background import _symbols_rows_from_movers
        from backend.api.routes.watchlist import MoverRow, MoversResponse
        resp = MoversResponse(
            movers=[
                MoverRow(tradingsymbol="RELIANCE", exchange="NSE", last_price=2805.0,
                          previous_close=2800.0, change_pct=2.5, peak_pct=2.5, sticky=False),
                MoverRow(tradingsymbol="TATASTEEL", exchange="NSE", last_price=95.0,
                          previous_close=100.0, change_pct=-5.0, peak_pct=-5.0, sticky=False),
            ],
            threshold_pct=1.5, session_date="2026-10-11",
        )
        with patch("backend.api.routes.watchlist._get_movers_rows", new=AsyncMock(return_value=resp)):
            out = await _symbols_rows_from_movers({"gainers", "losers"})
        kinds = {r["tradingsymbol"]: r["kind"] for r in out}
        assert kinds == {"RELIANCE": "gainers", "TATASTEEL": "losers"}

    @pytest.mark.asyncio
    async def test_only_requested_kind_returned(self):
        from backend.api.background import _symbols_rows_from_movers
        from backend.api.routes.watchlist import MoverRow, MoversResponse
        resp = MoversResponse(
            movers=[
                MoverRow(tradingsymbol="RELIANCE", exchange="NSE", last_price=2805.0,
                          previous_close=2800.0, change_pct=2.5, peak_pct=2.5, sticky=False),
                MoverRow(tradingsymbol="TATASTEEL", exchange="NSE", last_price=95.0,
                          previous_close=100.0, change_pct=-5.0, peak_pct=-5.0, sticky=False),
            ],
            threshold_pct=1.5, session_date="2026-10-11",
        )
        with patch("backend.api.routes.watchlist._get_movers_rows", new=AsyncMock(return_value=resp)):
            out = await _symbols_rows_from_movers({"gainers"})
        assert len(out) == 1
        assert out[0]["tradingsymbol"] == "RELIANCE"

    @pytest.mark.asyncio
    async def test_fetch_failure_degrades_to_empty(self):
        from backend.api.background import _symbols_rows_from_movers
        with patch("backend.api.routes.watchlist._get_movers_rows",
                   new=AsyncMock(side_effect=RuntimeError("broker down"))):
            out = await _symbols_rows_from_movers({"gainers", "losers"})
        assert out == []
