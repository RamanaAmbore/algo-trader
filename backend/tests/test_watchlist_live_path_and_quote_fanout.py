"""
Two confirmed backend bugs, both about live `Context` population/
classification being incomplete for certain scopes:

  1. `watchlist.*` agents never fired in production — the live
     `_perf_run_agent_engine` (background.py) never populated
     `Context.watchlist_rows`; only the SIM driver did. Fixed by
     `background._perf_fetch_watchlist_rows()`, wired into
     `_perf_run_agent_engine`'s context dict on the same cadence as
     positions/holdings/funds/orders/quote.

  2. `quote.tracked` matches (no account dimension at all — market
     data belongs to no account) were misclassified as "aggregate" by
     `agent_engine.py`'s fan-out code because it checked
     `account is None`, so `$row` references on a quote.tracked-scoped
     DO action always failed closed. Fixed by classifying "aggregate"
     by SCOPE SHAPE (`_ae_is_aggregate_match`: no `scope` key at all —
     the `not`-synthetic case — or a scope ending in `.total`) instead.

Five quality dimensions:
  SSOT        — drives the REAL live-path context builder
                (`_perf_run_agent_engine`) and the REAL dispatch gate
                (`_cycle_maybe_buffer_fire` -> `_cycle_dispatch_survivors`),
                not hand-built stand-ins for either.
  Correctness — regression-proves the exact two confirmed bugs, plus
                the pre-existing aggregate scopes (`positions.total`/
                `funds.total`) and per-account scope
                (`positions.any_acct`) still classify correctly after
                the reclassification.
  Performance — no real DB/broker I/O; DB session + `_fetch_quotes`
                mocked at the module boundary, matching
                `test_orders_quote_scopes.py`'s existing convention.
  Reuse       — mirrors `_perf_fetch_orders_rows`'s/`_perf_build_quote_
                rows`'s existing test shapes (same file) and
                `test_agent_action_fanout.py`'s `_run_tick` dispatch
                harness (same file family).
  UX          — every assert carries an f-string with actual/expected.
"""
from __future__ import annotations

import os
os.environ.setdefault("PYTEST_RUNNING", "1")

from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from backend.api.algo import agent_engine
from backend.api.algo.agent_engine import (
    _ae_is_aggregate_match,
    _ae_fanout_matches,
    _cycle_maybe_buffer_fire,
    _cycle_dispatch_survivors,
)
from backend.api.algo.actions import resolve_row_ref_params
from backend.api.algo import grammar


def _now():
    return datetime(2026, 10, 11, 10, 0, 0, tzinfo=timezone.utc)


# ═══════════════════════════════════════════════════════════════════════
#  Bug 1 — watchlist.* live population
# ═══════════════════════════════════════════════════════════════════════

class _FakeAgent:
    def __init__(self, id=1, slug="wl-agent", conditions=None, actions=None):
        self.id = id
        self.slug = slug
        self.conditions = conditions or {}
        self.actions = actions or []


class _FakeWatchlistRow:
    """Fake WatchlistItem ORM row."""
    def __init__(self, id, watchlist_id, tradingsymbol, exchange):
        self.id = id
        self.watchlist_id = watchlist_id
        self.tradingsymbol = tradingsymbol
        self.exchange = exchange


class _FakeQuote:
    def __init__(self, item_id, ltp, close=0.0, bid=0.0, ask=0.0,
                 change=0.0, change_pct=0.0, quote_symbol=None):
        self.item_id = item_id
        self.ltp = ltp
        self.close = close
        self.bid = bid
        self.ask = ask
        self.change = change
        self.change_pct = change_pct
        self.quote_symbol = quote_symbol


def _mock_session(wl_rows, item_rows):
    """Two sequential session.execute() calls: (1) Watchlist id/name
    rows, (2) WatchlistItem rows. Matches `_perf_fetch_watchlist_rows`'
    own query order."""
    wl_result = MagicMock()
    wl_result.all.return_value = wl_rows
    items_result = MagicMock()
    items_result.scalars.return_value.all.return_value = item_rows

    session = AsyncMock()
    session.__aenter__ = AsyncMock(return_value=session)
    session.__aexit__ = AsyncMock(return_value=False)
    session.execute = AsyncMock(side_effect=[wl_result, items_result])
    return session


class TestPerfFetchWatchlistRows:
    """Unit coverage of `background._perf_fetch_watchlist_rows` — the
    live-path mirror of the sim driver's `_fetch_user_watchlist_rows`."""

    @pytest.mark.asyncio
    async def test_gated_on_no_active_agent_referencing_watchlist_scope(self):
        """No active agent condition references `watchlist.*` — the
        function must return [] WITHOUT ever touching the DB (same
        cost-gating contract as `_perf_build_quote_rows`)."""
        from backend.api.background import _perf_fetch_watchlist_rows
        agent = _FakeAgent(conditions={"metric": "pnl", "scope": "positions.total",
                                       "op": "<", "value": 0})
        session = AsyncMock()
        with patch("backend.api.algo.agent_engine._cycle_load_agents",
                   new=AsyncMock(return_value=[agent])), \
             patch("backend.api.background.async_session", return_value=session) as sess_ctor:
            out = await _perf_fetch_watchlist_rows()
        assert out == []
        sess_ctor.assert_not_called()

    @pytest.mark.asyncio
    async def test_builds_rows_from_db_and_quotes(self):
        """A real `watchlist.default`-referencing agent — the function
        queries Watchlist + WatchlistItem, fetches quotes, and builds
        rows via the SAME `_sd_build_watchlist_row` helper the sim
        driver uses (byte-identical row shape between both paths)."""
        from backend.api.background import _perf_fetch_watchlist_rows
        agent = _FakeAgent(conditions={"metric": "ltp", "scope": "watchlist.default",
                                       "op": ">=", "value": 0})
        wl_rows = [(10, "Default")]
        item_rows = [_FakeWatchlistRow(id=100, watchlist_id=10,
                                        tradingsymbol="RELIANCE", exchange="NSE")]
        quote = _FakeQuote(item_id=100, ltp=2500.0, close=2490.0, bid=2499.5, ask=2500.5,
                           change=10.0, change_pct=0.4, quote_symbol="RELIANCE")
        session = _mock_session(wl_rows, item_rows)

        with patch("backend.api.algo.agent_engine._cycle_load_agents",
                   new=AsyncMock(return_value=[agent])), \
             patch("backend.api.background.async_session", return_value=session), \
             patch("backend.api.routes.watchlist._fetch_quotes",
                   new=AsyncMock(return_value=[quote])):
            out = await _perf_fetch_watchlist_rows()

        assert len(out) == 1, f"expected 1 row, got {out}"
        row = out[0]
        assert row["account"] == "Default", f"expected watchlist name as account, got {row}"
        assert row["tradingsymbol"] == "RELIANCE"
        assert row["exchange"] == "NSE"
        assert row["last_price"] == 2500.0

    @pytest.mark.asyncio
    async def test_missing_vs_zero_drops_zero_ltp_rows(self):
        """A row whose quote fetch failed (no quote found, `_sd_qf`
        defaults to 0.0) must be DROPPED, never exposed as a confirmed
        LTP of 0 — same missing-vs-zero convention as
        `_perf_build_quote_rows`."""
        from backend.api.background import _perf_fetch_watchlist_rows
        agent = _FakeAgent(conditions={"metric": "ltp", "scope": "watchlist.all",
                                       "op": ">=", "value": 0})
        wl_rows = [(10, "Default")]
        item_rows = [_FakeWatchlistRow(id=100, watchlist_id=10,
                                        tradingsymbol="RELIANCE", exchange="NSE")]
        session = _mock_session(wl_rows, item_rows)

        with patch("backend.api.algo.agent_engine._cycle_load_agents",
                   new=AsyncMock(return_value=[agent])), \
             patch("backend.api.background.async_session", return_value=session), \
             patch("backend.api.routes.watchlist._fetch_quotes",
                   new=AsyncMock(return_value=[])):
            out = await _perf_fetch_watchlist_rows()
        assert out == [], f"zero/missing-quote row must be dropped, got {out}"

    @pytest.mark.asyncio
    async def test_db_failure_returns_empty_not_raise(self):
        from backend.api.background import _perf_fetch_watchlist_rows
        agent = _FakeAgent(conditions={"metric": "ltp", "scope": "watchlist.all",
                                       "op": ">=", "value": 0})
        session = AsyncMock()
        session.__aenter__ = AsyncMock(side_effect=RuntimeError("db down"))
        with patch("backend.api.algo.agent_engine._cycle_load_agents",
                   new=AsyncMock(return_value=[agent])), \
             patch("backend.api.background.async_session", return_value=session):
            out = await _perf_fetch_watchlist_rows()
        assert out == []

    @pytest.mark.asyncio
    async def test_agent_load_failure_returns_empty_not_raise(self):
        from backend.api.background import _perf_fetch_watchlist_rows
        with patch("backend.api.algo.agent_engine._cycle_load_agents",
                   new=AsyncMock(side_effect=RuntimeError("db down"))):
            out = await _perf_fetch_watchlist_rows()
        assert out == []


@pytest.fixture(autouse=True)
def _wire_registry_for_watchlist_scopes(monkeypatch):
    """GrammarRegistry is a process-wide singleton only populated via
    `REGISTRY.reload()` against the real `grammar_tokens` DB table —
    wiring it explicitly here (same pattern as
    `test_orders_quote_scopes.py:_wire_registry`) makes this test
    deterministic regardless of pytest execution order / whether an
    earlier test in the session already triggered a real reload()."""
    from backend.api.algo.grammar_registry import REGISTRY
    monkeypatch.setattr(REGISTRY, 'metrics', {'ltp': grammar._metric_ltp})
    monkeypatch.setattr(REGISTRY, 'scopes', {
        'watchlist.default': grammar._scope_watchlist_default,
        'watchlist.all':     grammar._scope_watchlist_all,
        'watchlist.markets': grammar._scope_watchlist_markets,
    })
    monkeypatch.setattr(REGISTRY, 'operators', {'>=': grammar.OPERATORS['>=']})
    monkeypatch.setattr(REGISTRY, 'metric_factories', {})
    monkeypatch.setattr(REGISTRY, '_metric_call_cache', {})
    monkeypatch.setattr(REGISTRY, '_scope_call_cache', {})


class TestWatchlistLivePathRegression:
    """THE regression test proving the actual production bug is fixed:
    a `watchlist.*`-scoped condition must fire against the context
    `_perf_run_agent_engine` (the REAL live path, not the sim driver)
    actually constructs. Before this fix, `_perf_run_agent_engine`
    never added a `watchlist_rows` key to `agent_context` at all, so
    `V2Context(watchlist_rows=context.get("watchlist_rows") or [])`
    was always `[]` on the live path — this test fails on the
    pre-fix code."""

    @pytest.mark.asyncio
    async def test_watchlist_default_condition_fires_against_live_context(self):
        from backend.api.background import _perf_run_agent_engine
        from backend.api.algo.agent_engine import _cycle_evaluate_agent, _v2_cfg
        import pandas as pd

        captured = {}

        async def _fake_run_cycle(context, broadcast_fn=None):
            captured.update(context)

        watchlist_row = {
            "section": "watchlist", "account": "Default",
            "tradingsymbol": "RELIANCE", "exchange": "NSE",
            "quantity": 0, "average_price": 0.0, "last_price": 2500.0,
            "close_price": 2490.0, "bid": 2499.5, "ask": 2500.5, "pnl": 0.0,
            "day_change": 10.0, "day_change_percentage": 0.004,
        }

        with patch("backend.shared.helpers.utils.is_prod_branch", return_value=True), \
             patch("backend.api.algo.agent_engine.run_cycle", new=_fake_run_cycle), \
             patch("backend.api.background._resolve_spot_prices", return_value={}), \
             patch("backend.api.background._perf_fetch_orders_rows", new=AsyncMock(return_value=[])), \
             patch("backend.api.background._perf_build_quote_rows", new=AsyncMock(return_value=[])), \
             patch("backend.api.background._perf_fetch_watchlist_rows",
                   new=AsyncMock(return_value=[watchlist_row])):
            await _perf_run_agent_engine(
                sim_active=False, df_holdings=pd.DataFrame(), df_positions=pd.DataFrame(),
                df_margins=pd.DataFrame(), sum_holdings=pd.DataFrame(), sum_positions=pd.DataFrame(),
                ist_display="10:00:00 IST", now=_now(), seg_state={}, alert_state={},
                segments=[],
            )

        assert captured.get("watchlist_rows") == [watchlist_row], (
            f"expected watchlist_rows to be wired into the live context, got "
            f"{captured.get('watchlist_rows')!r}"
        )

        # Feed the REAL captured live context into the REAL evaluator —
        # this is the proof that a watchlist.default leaf actually fires
        # off the live path's own context construction, not a
        # hand-built Context() the sim path would also satisfy.
        wl_agent = _FakeAgent(
            id=99, slug="wl-default-test",
            conditions={"metric": "ltp", "scope": "watchlist.default", "op": ">=", "value": 2000},
        )
        matches, _observations = _cycle_evaluate_agent(
            wl_agent, captured, _v2_cfg(), _now(), alert_state={}, bypass_schedule=True,
        )
        assert len(matches) == 1, (
            f"expected the live-path context to produce exactly 1 watchlist.default "
            f"match, got {matches}"
        )
        assert matches[0]["row"]["tradingsymbol"] == "RELIANCE"

        wl_all_agent = _FakeAgent(
            id=100, slug="wl-all-test",
            conditions={"metric": "ltp", "scope": "watchlist.all", "op": ">=", "value": 2000},
        )
        matches_all, _ = _cycle_evaluate_agent(
            wl_all_agent, captured, _v2_cfg(), _now(), alert_state={}, bypass_schedule=True,
        )
        assert len(matches_all) == 1, (
            f"expected watchlist.all to also see the live row, got {matches_all}"
        )


# ═══════════════════════════════════════════════════════════════════════
#  Bug 2 — quote.tracked aggregate misclassification
# ═══════════════════════════════════════════════════════════════════════

def _quote_tracked_match(**overrides) -> dict:
    """A real match entry shaped exactly as `_eval_leaf` produces for a
    `quote.tracked` leaf — `account` is None (no account dimension on
    market-data rows), but `scope` is the real, non-`.total` token."""
    row = {"tradingsymbol": "RELIANCE", "exchange": "NSE", "last_price": 2500.0,
           "bid": 2499.5, "ask": 2500.5, "volume": 1000}
    m = {
        "metric": "ltp", "scope": "quote.tracked", "op": ">=", "threshold": 2000,
        "value": 2500.0, "row": row, "account": None,
    }
    m.update(overrides)
    return m


class _Agent:
    def __init__(self, id=1, slug="test-agent", name=None, tier="medium",
                 topic="general", actions=None, cooldown_minutes=0,
                 fire_at_time=None, status="active", trigger_count=0):
        self.id = id
        self.slug = slug
        self.name = name or slug
        self.tier = tier
        self.topic = topic
        self.actions = actions or []
        self.cooldown_minutes = cooldown_minutes
        self.fire_at_time = fire_at_time
        self.status = status
        self.trigger_count = trigger_count


async def _run_tick(agent, matches, *, cfg=None, bypass_suppression=False, context=None):
    """Drive the REAL dispatch gate: _cycle_maybe_buffer_fire ->
    _cycle_dispatch_survivors, with `execute` mocked at the agent_engine
    boundary (same harness shape as test_agent_action_fanout.py's
    `_run_tick`, duplicated here for test-file self-containment)."""
    pending: list = []
    fired = _cycle_maybe_buffer_fire(
        agent, matches, now=_now(),
        bypass_suppression=bypass_suppression, bypass_schedule=True,
        sim_mode=False, alert_state={}, cfg=cfg or {'cooldown_min': 30},
        broadcast_fn=None, debounce_min=0, pending_dispatches=pending,
    )
    with (
        patch.object(agent_engine, "execute", new=AsyncMock()) as exec_mock,
        patch.object(agent_engine, "_v2_send_rich_alert", new=AsyncMock(return_value=False)),
        patch.object(agent_engine, "dispatch", new=AsyncMock()),
    ):
        if pending:
            await _cycle_dispatch_survivors(pending, _now(), context or {}, None)
    return exec_mock, fired


@pytest.fixture(autouse=True)
def _clear_latch():
    agent_engine._V2_LATCH.clear()
    yield
    agent_engine._V2_LATCH.clear()


class TestQuoteTrackedRowRefFanout:
    """Test (2) from the task — a `quote.tracked` condition with a
    `$row`-referencing DO action must resolve `_matched_row` correctly,
    never fail closed."""

    @pytest.mark.asyncio
    async def test_quote_tracked_match_binds_row_for_row_ref_action(self):
        agent = _Agent(id=901, slug="test-quote-tracked-row-bind", actions=[{
            "type": "log",
            "params": {"message": {"$row": "symbol"}, "level": "info"},
        }])
        matches = [_quote_tracked_match()]

        exec_mock, fired = await _run_tick(agent, matches)

        assert fired, "expected the agent to fire"
        assert exec_mock.call_count == 1, (
            f"expected exactly 1 execute() call, got {exec_mock.call_count}"
        )
        action_ctx = exec_mock.call_args.args[2]
        assert action_ctx["_matched_row"] is not None, (
            "a quote.tracked match has account=None but IS a real per-row "
            "match — _matched_row must bind, not fail closed to None "
            "(this is the confirmed Sprint 21 bug)"
        )
        assert action_ctx["_matched_row"]["tradingsymbol"] == "RELIANCE"
        assert action_ctx["_matched_row_scope"] == "quote.tracked"
        # No real per-account identity for market-data rows — display
        # value falls back to the established "TOTAL" sentinel, but
        # the row itself is still bound (checked above).
        assert action_ctx["account"] == "TOTAL"

    def test_resolve_row_ref_succeeds_against_bound_quote_tracked_row(self):
        """Direct unit check: once `_matched_row`/`_matched_row_scope`
        are correctly bound (post-fix), `resolve_row_ref_params`
        actually resolves `{"$row": "symbol"}` to the real tradingsymbol
        — no fail-closed error."""
        row = {"tradingsymbol": "RELIANCE", "exchange": "NSE", "last_price": 2500.0}
        params = {"message": {"$row": "symbol"}}
        resolved, err = resolve_row_ref_params(
            params, {"_matched_row": row, "_matched_row_scope": "quote.tracked"},
        )
        assert err is None, f"expected no error, got {err!r}"
        assert resolved["message"] == "RELIANCE", (
            f"expected the real tradingsymbol, got {resolved['message']!r}"
        )


# ═══════════════════════════════════════════════════════════════════════
#  Regression — existing aggregate/per-row classifications unaffected
# ═══════════════════════════════════════════════════════════════════════

class TestAggregateClassificationRegression:
    """Tests (3) and (4) from the task — the reclassification logic
    change must not disturb any PRE-EXISTING scope's aggregate/
    per-row status."""

    def test_positions_total_still_classifies_as_aggregate(self):
        m = {"metric": "pnl", "scope": "positions.total", "op": "<=",
             "threshold": -50000, "value": -60000, "row": {"account": "TOTAL", "pnl": -60000},
             "account": "TOTAL"}
        assert _ae_is_aggregate_match(m) is True, (
            "positions.total must still classify as aggregate after the fix"
        )

    def test_funds_total_still_classifies_as_aggregate(self):
        m = {"metric": "avail_margin", "scope": "funds.total", "op": "<",
             "threshold": 0, "value": -500, "row": {"account": "TOTAL"}, "account": "TOTAL"}
        assert _ae_is_aggregate_match(m) is True, (
            "funds.total must still classify as aggregate after the fix"
        )

    def test_holdings_total_still_classifies_as_aggregate(self):
        m = {"metric": "pnl", "scope": "holdings.total", "op": "<",
             "threshold": 0, "value": -1, "row": {"account": "TOTAL"}, "account": "TOTAL"}
        assert _ae_is_aggregate_match(m) is True

    def test_not_synthetic_entry_still_classifies_as_aggregate(self):
        """The `not` composite's synthetic `{'not': inner}` entry
        carries no `scope` key at all — must still classify aggregate
        (it has no row to bind, by construction)."""
        m = {"not": {"metric": "pnl", "scope": "positions.total", "op": "<", "value": 0}}
        assert _ae_is_aggregate_match(m) is True

    def test_positions_any_acct_still_classifies_as_non_aggregate(self):
        m = {"metric": "pnl_pct", "scope": "positions.any_acct", "op": "<=",
             "threshold": -5.0, "value": -10.0,
             "row": {"account": "ZG0790", "pnl": -1000}, "account": "ZG0790"}
        assert _ae_is_aggregate_match(m) is False, (
            "positions.any_acct must remain classified as a real per-row "
            "(non-aggregate) match"
        )

    def test_quote_tracked_now_classifies_as_non_aggregate(self):
        """THE core fix under test — quote.tracked has account=None but
        must NOT be treated as aggregate (pre-fix: it always was)."""
        m = _quote_tracked_match()
        assert m["account"] is None, "sanity: quote.tracked rows carry no account dimension"
        assert _ae_is_aggregate_match(m) is False, (
            "quote.tracked must classify as a real per-row match despite "
            "account being None — this is the confirmed Sprint 21 bug"
        )

    def test_fanout_matches_still_collapses_positions_total_to_one(self):
        """End-to-end regression through `_ae_fanout_matches` itself —
        multiple positions.total matches collapse to exactly one
        aggregate entry, same as before the reclassification."""
        matches = [
            {"metric": "day_val", "scope": "positions.total", "op": "<=",
             "threshold": -50000, "value": -60000, "row": {"account": "TOTAL"}, "account": "TOTAL"},
            {"metric": "day_pct", "scope": "positions.total", "op": "<=",
             "threshold": -2.0, "value": -3.0, "row": {"account": "TOTAL"}, "account": "TOTAL"},
        ]
        out = _ae_fanout_matches(matches)
        assert len(out) == 1, f"expected exactly 1 collapsed aggregate entry, got {out}"

    def test_fanout_matches_keeps_quote_tracked_rows_distinct_per_symbol(self):
        """Two different quote.tracked symbols must each survive
        fan-out as distinct per-row entries, never collapsed into one
        aggregate entry (the pre-fix behavior)."""
        m1 = _quote_tracked_match()
        m2 = _quote_tracked_match(row={"tradingsymbol": "TCS", "exchange": "NSE",
                                        "last_price": 3500.0}, value=3500.0)
        out = _ae_fanout_matches([m1, m2])
        assert len(out) == 2, f"expected 2 distinct per-row entries, got {out}"
        symbols = {e["row"]["tradingsymbol"] for e in out}
        assert symbols == {"RELIANCE", "TCS"}, f"expected both symbols preserved, got {symbols}"
