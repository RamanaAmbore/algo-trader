"""Sprint 3 (auto-close-short-option plan), piece 8 — `price_source`
wiring fix.

Proves the REAL gap closed: before this sprint, `ctx.position_rows`
built by `background.py:_perf_run_agent_engine` never carried a
`price_source` column at all, so `metric_gain_from_entry_pct`'s and
`metric_short_option_gain_from_entry_pct`'s own degraded-price_source
guard could never fire in production. These tests call the REAL
`_perf_run_agent_engine` (mocking only `run_cycle` to capture the
context it was handed, and the handful of other heavy helpers the
existing `TestPerfRunAgentEngineRegression` suite in
test_orders_quote_scopes.py already mocks the same way) rather than
unit-testing `tag_position_rows_price_source` in isolation, which would
prove the function works but miss whether it is actually WIRED IN.
"""
from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pandas as pd
import pytest


def _positions_df(rows: list[dict]) -> pd.DataFrame:
    return pd.DataFrame(rows)


@pytest.mark.asyncio
async def test_pending_open_row_tagged_through_the_real_pipeline():
    """A row on a currently pending-open exchange (resolved via the
    real `_resolve_pending_open_exchanges`, with only the underlying
    calendar probe patched) gets `price_source='pending_open'` stamped
    onto `ctx.position_rows` by the time `run_cycle` sees it."""
    from backend.api.background import _perf_run_agent_engine

    df_positions = _positions_df([{
        "tradingsymbol": "GOLD26OCT142000PE", "exchange": "MCX",
        "quantity": -1, "average_price": 100.0, "last_price": 100.0,
        "prev_close": 95.0,
    }])
    sum_holdings = pd.DataFrame({"account": ["TOTAL"], "pnl": [0.0]})
    sum_positions = pd.DataFrame({"account": ["TOTAL"], "pnl": [0.0]})

    captured = {}

    async def _fake_run_cycle(context, broadcast_fn=None):
        captured.update(context)

    with patch("backend.shared.helpers.utils.is_prod_branch", return_value=True), \
         patch("backend.api.algo.agent_engine.run_cycle", new=_fake_run_cycle), \
         patch("backend.api.background._resolve_spot_prices", return_value={}), \
         patch("backend.api.background._perf_fetch_orders_rows", new=AsyncMock(return_value=[])), \
         patch("backend.api.background._perf_build_quote_rows", new=AsyncMock(return_value=[])), \
         patch("backend.api.helpers.snapshot_gate.prewarm_pending_open_segments",
               new=AsyncMock(return_value=None)), \
         patch("backend.api.helpers.snapshot_gate.is_segment_pending_open",
               side_effect=lambda exch, now: str(exch).upper() == "MCX"):
        await _perf_run_agent_engine(
            sim_active=False, df_holdings=pd.DataFrame(), df_positions=df_positions,
            df_margins=pd.DataFrame(), sum_holdings=sum_holdings, sum_positions=sum_positions,
            ist_display="08:47:00 IST", now=object(), seg_state={}, alert_state={},
            segments=[],
        )

    rows = captured["position_rows"]
    assert len(rows) == 1
    assert rows[0]["price_source"] == "pending_open"

    # And the real degraded-price_source guard now actually fires for
    # this row — the whole point of the fix.
    from backend.api.algo.grammar_tokens.metric_gain_from_entry_pct import (
        _metric_gain_from_entry_pct,
    )
    from backend.api.algo.grammar_tokens.metric_short_option_gain_from_entry_pct import (
        _metric_short_option_gain_from_entry_pct,
    )
    assert _metric_gain_from_entry_pct(None, rows[0]) is None
    assert _metric_short_option_gain_from_entry_pct(None, rows[0]) is None


@pytest.mark.asyncio
async def test_flat_row_tagged_settled_through_the_real_pipeline():
    """A flat (qty==0) row gets `price_source='snapshot_settled'`,
    matching `_row_is_settled_flat`'s own precedence (checked FIRST,
    regardless of exchange state)."""
    from backend.api.background import _perf_run_agent_engine

    df_positions = _positions_df([{
        "tradingsymbol": "NIFTY24800CE", "exchange": "NFO",
        "quantity": 0, "average_price": 100.0, "last_price": 100.0,
        "prev_close": 95.0,
    }])
    sum_holdings = pd.DataFrame({"account": ["TOTAL"], "pnl": [0.0]})
    sum_positions = pd.DataFrame({"account": ["TOTAL"], "pnl": [0.0]})

    captured = {}

    async def _fake_run_cycle(context, broadcast_fn=None):
        captured.update(context)

    with patch("backend.shared.helpers.utils.is_prod_branch", return_value=True), \
         patch("backend.api.algo.agent_engine.run_cycle", new=_fake_run_cycle), \
         patch("backend.api.background._resolve_spot_prices", return_value={}), \
         patch("backend.api.background._perf_fetch_orders_rows", new=AsyncMock(return_value=[])), \
         patch("backend.api.background._perf_build_quote_rows", new=AsyncMock(return_value=[])), \
         patch("backend.api.helpers.snapshot_gate.prewarm_pending_open_segments",
               new=AsyncMock(return_value=None)), \
         patch("backend.api.helpers.snapshot_gate.is_segment_pending_open",
               return_value=False):
        await _perf_run_agent_engine(
            sim_active=False, df_holdings=pd.DataFrame(), df_positions=df_positions,
            df_margins=pd.DataFrame(), sum_holdings=sum_holdings, sum_positions=sum_positions,
            ist_display="16:00:00 IST", now=object(), seg_state={}, alert_state={},
            segments=[],
        )

    rows = captured["position_rows"]
    assert rows[0]["price_source"] == "snapshot_settled"


@pytest.mark.asyncio
async def test_open_segment_live_row_not_tagged_through_the_real_pipeline():
    """A genuinely open-segment, non-flat row gets NO price_source key
    at all — the "live" case, unchanged from before this sprint — and
    the real metric still returns a real number for it."""
    from backend.api.background import _perf_run_agent_engine

    df_positions = _positions_df([{
        "tradingsymbol": "NIFTY24800CE", "exchange": "NFO",
        "quantity": -5, "average_price": 100.0, "last_price": 10.0,
        "prev_close": 95.0,
    }])
    sum_holdings = pd.DataFrame({"account": ["TOTAL"], "pnl": [0.0]})
    sum_positions = pd.DataFrame({"account": ["TOTAL"], "pnl": [0.0]})

    captured = {}

    async def _fake_run_cycle(context, broadcast_fn=None):
        captured.update(context)

    with patch("backend.shared.helpers.utils.is_prod_branch", return_value=True), \
         patch("backend.api.algo.agent_engine.run_cycle", new=_fake_run_cycle), \
         patch("backend.api.background._resolve_spot_prices", return_value={}), \
         patch("backend.api.background._perf_fetch_orders_rows", new=AsyncMock(return_value=[])), \
         patch("backend.api.background._perf_build_quote_rows", new=AsyncMock(return_value=[])), \
         patch("backend.api.helpers.snapshot_gate.prewarm_pending_open_segments",
               new=AsyncMock(return_value=None)), \
         patch("backend.api.helpers.snapshot_gate.is_segment_pending_open",
               return_value=False):
        await _perf_run_agent_engine(
            sim_active=False, df_holdings=pd.DataFrame(), df_positions=df_positions,
            df_margins=pd.DataFrame(), sum_holdings=sum_holdings, sum_positions=sum_positions,
            ist_display="11:00:00 IST", now=object(), seg_state={}, alert_state={},
            segments=[],
        )

    rows = captured["position_rows"]
    assert "price_source" not in rows[0]

    from backend.api.algo.grammar_tokens.metric_short_option_gain_from_entry_pct import (
        _metric_short_option_gain_from_entry_pct,
    )
    assert _metric_short_option_gain_from_entry_pct(None, rows[0]) == pytest.approx(90.0)


# ═══════════════════════════════════════════════════════════════════════
#  Unit-level coverage of tag_position_rows_price_source / _pending_open_mask
# ═══════════════════════════════════════════════════════════════════════

class TestTagPositionRowsPriceSourceUnit:
    def test_pin_pending_open_rows_still_works_after_mask_refactor(self):
        """Regression guard for the _pending_open_mask extraction —
        _pin_pending_open_rows must still numerically pin exactly as
        before."""
        from backend.api.routes.positions import _pin_pending_open_rows
        raw = _positions_df([{
            "exchange": "MCX", "prev_close": 95.0, "last_price": 100.0,
            "average_price": 90.0, "quantity": 10, "unrealised": 100.0,
            "realised": 0.0,
        }])
        _pin_pending_open_rows(raw, {"MCX"})
        assert raw.loc[0, "last_price"] == 95.0
        assert raw.loc[0, "day_change_val"] == 0.0

    def test_tag_skips_on_empty_rows_or_frame(self):
        from backend.api.routes.positions import tag_position_rows_price_source
        tag_position_rows_price_source([], _positions_df([{"exchange": "MCX"}]), {"MCX"})
        tag_position_rows_price_source([{"quantity": 1}], pd.DataFrame(), {"MCX"})
        # No exception == pass.

    def test_tag_prioritises_flat_over_pending(self):
        from backend.api.routes.positions import tag_position_rows_price_source
        raw = _positions_df([{"exchange": "MCX", "prev_close": 95.0, "quantity": 0}])
        rows = raw.to_dict("records")
        tag_position_rows_price_source(rows, raw, {"MCX"})
        assert rows[0]["price_source"] == "snapshot_settled"
