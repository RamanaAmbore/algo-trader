"""Unit tests for extracted batch_quote + batch_sparkline helper functions.

Quality dimensions (feedback_test_dimensions.md):
  1. SSOT        — helpers are the single dispatch point for their concern;
                   no duplicate logic exists in the master functions.
  2. Performance — helpers are pure / thin; async helpers are sync-safe
                   under asyncio.run().
  3. Stale code  — batch_quote and batch_sparkline bodies must reference each
                   helper (not inline the extracted logic).
  4. Reuse       — helpers importable from backend.api.routes.quote at module level.
  5. UX          — correct key remap (original sym exposed, not resolved contract);
                   stale=True on closed-hours rows; LKG fallback on missing keys.
"""

from __future__ import annotations

import asyncio
import inspect
from typing import Optional
from unittest.mock import AsyncMock, MagicMock, patch

import pytest


# ---------------------------------------------------------------------------
# Helpers accessible at module level
# ---------------------------------------------------------------------------

def _helpers_module():
    import backend.api.routes.quote as q
    return q


# ---------------------------------------------------------------------------
# 1. _normalize_batch_keys
# ---------------------------------------------------------------------------

class TestNormalizeBatchKeys:

    def test_deduplicates(self):
        from backend.api.routes.quote import _normalize_batch_keys
        keys = ["NSE:RELIANCE", "NSE:RELIANCE", "MCX:GOLD"]
        result = _normalize_batch_keys(keys)
        assert result.count("NSE:RELIANCE") == 1
        assert "MCX:GOLD" in result

    def test_strips_whitespace(self):
        from backend.api.routes.quote import _normalize_batch_keys
        result = _normalize_batch_keys(["  NSE:RELIANCE  ", "MCX:GOLD "])
        assert "NSE:RELIANCE" in result
        assert "MCX:GOLD" in result

    def test_filters_keys_without_colon(self):
        from backend.api.routes.quote import _normalize_batch_keys
        result = _normalize_batch_keys(["RELIANCE", "NSE:INFY", ""])
        assert "RELIANCE" not in result
        assert "" not in result
        assert "NSE:INFY" in result

    def test_caps_at_300(self):
        from backend.api.routes.quote import _normalize_batch_keys
        keys = [f"NSE:SYM{i:04d}" for i in range(500)]
        result = _normalize_batch_keys(keys)
        assert len(result) == 300

    def test_empty_input(self):
        from backend.api.routes.quote import _normalize_batch_keys
        assert _normalize_batch_keys([]) == []


# ---------------------------------------------------------------------------
# 2. _build_live_batch_row
# ---------------------------------------------------------------------------

class TestBuildLiveBatchRow:

    def _make_key_map(self, input_key: str, broker_key: str):
        km = MagicMock()
        km.input_to_broker = {input_key: broker_key}
        return km

    def test_standard_row_values(self):
        from backend.api.routes.quote import _build_live_batch_row
        key_map = self._make_key_map("NSE:RELIANCE", "NSE:RELIANCE")
        quote_data = {
            "NSE:RELIANCE": {
                "last_price": 2900.0,
                "volume": 123456,
                "oi": 0,
                "ohlc": {"open": 2880.0, "close": 2850.0, "high": 2920.0, "low": 2870.0},
                "depth": {"buy": [{"price": 2899.0, "quantity": 100, "orders": 5}],
                          "sell": [{"price": 2901.0, "quantity": 200, "orders": 3}]},
            }
        }
        row = _build_live_batch_row("NSE:RELIANCE", quote_data, key_map)
        assert row.tradingsymbol == "RELIANCE"
        assert row.exchange == "NSE"
        assert row.ltp == pytest.approx(2900.0, abs=0.001)
        assert row.bid == pytest.approx(2899.0, abs=0.001)
        assert row.ask == pytest.approx(2901.0, abs=0.001)
        assert row.volume == 123456
        assert row.close == pytest.approx(2850.0, abs=0.001)
        assert row.open == pytest.approx(2880.0, abs=0.001)
        assert not row.stale

    def test_virtual_root_key_remap(self):
        """Response row tradingsymbol uses original input sym, not resolved contract."""
        from backend.api.routes.quote import _build_live_batch_row
        # CRUDEOIL → CRUDEOILM26JULFUT resolution
        key_map = self._make_key_map("MCX:CRUDEOIL", "MCX:CRUDEOILM26JULFUT")
        quote_data = {
            "MCX:CRUDEOILM26JULFUT": {
                "last_price": 6100.0,
                "volume": 1000,
                "oi": 500,
                "ohlc": {"open": 6050.0, "close": 6000.0},
                "depth": {"buy": [], "sell": []},
            }
        }
        row = _build_live_batch_row("MCX:CRUDEOIL", quote_data, key_map)
        assert row.tradingsymbol == "CRUDEOIL", (
            f"Expected 'CRUDEOIL' (original), got '{row.tradingsymbol}'"
        )
        assert row.exchange == "MCX"
        assert row.ltp == pytest.approx(6100.0, abs=0.001)
        assert not row.stale

    def test_stale_row_on_missing_broker_data(self):
        from backend.api.routes.quote import _build_live_batch_row
        key_map = self._make_key_map("NSE:INFY", "NSE:INFY")
        row = _build_live_batch_row("NSE:INFY", {}, key_map)
        assert row.stale is True
        assert row.ltp == pytest.approx(0.0, abs=0.001)

    def test_change_and_change_pct_computed(self):
        from backend.api.routes.quote import _build_live_batch_row
        key_map = self._make_key_map("NSE:TCS", "NSE:TCS")
        quote_data = {
            "NSE:TCS": {
                "last_price": 3900.0,
                "volume": 0,
                "oi": 0,
                "ohlc": {"open": 3850.0, "close": 3800.0},
                "depth": {"buy": [], "sell": []},
            }
        }
        row = _build_live_batch_row("NSE:TCS", quote_data, key_map)
        assert row.change == pytest.approx(100.0, abs=0.001)
        assert row.change_pct == pytest.approx(100.0 / 3800.0 * 100.0, abs=0.001)


# ---------------------------------------------------------------------------
# 3. _serve_closed_hours_batch (LKG fallback)
# ---------------------------------------------------------------------------

class TestServeClosedHoursBatch:

    def setup_method(self):
        from backend.brokers import broker_apis
        with broker_apis._LAST_GOOD_LTP_LOCK:
            broker_apis._LAST_GOOD_LTP.clear()
        with broker_apis._LAST_GOOD_QUOTE_LOCK:
            broker_apis._LAST_GOOD_QUOTE.clear()

    @pytest.mark.asyncio
    async def test_lkg_fields_in_closed_hours_row(self):
        """After a live-path write, closed-hours response carries open/close/vol/oi."""
        from backend.brokers.broker_apis import record_good_ltp, record_good_quote
        from backend.api.routes.quote import _serve_closed_hours_batch

        record_good_ltp("RELIANCE", 2900.0)
        record_good_quote("RELIANCE", {
            "open": 2870.0, "close": 2850.0, "volume": 500_000, "oi": 0,
            "change": 50.0, "change_pct": 1.75, "bid": 2899.0, "ask": 2901.0,
        })

        km = MagicMock()
        km.input_to_broker = {"NSE:RELIANCE": "NSE:RELIANCE"}

        with patch("backend.api.routes.quote._maybe_warm_closed_hours_quotes", new=AsyncMock()):
            resp = await _serve_closed_hours_batch(["NSE:RELIANCE"], km)

        assert len(resp.items) == 1
        row = resp.items[0]
        assert row.tradingsymbol == "RELIANCE"
        assert row.ltp == pytest.approx(2900.0, abs=0.001)
        assert row.open == pytest.approx(2870.0, abs=0.001)
        assert row.close == pytest.approx(2850.0, abs=0.001)
        assert row.volume == 500_000
        assert row.stale is True
        assert resp.as_of is not None

    @pytest.mark.asyncio
    async def test_missing_lkg_returns_zero_ltp(self):
        """No prior LKG write → ltp=0, stale=True, all fields None."""
        from backend.api.routes.quote import _serve_closed_hours_batch

        km = MagicMock()
        km.input_to_broker = {"NSE:UNKNOWN": "NSE:UNKNOWN"}

        with patch("backend.api.routes.quote._maybe_warm_closed_hours_quotes", new=AsyncMock()):
            resp = await _serve_closed_hours_batch(["NSE:UNKNOWN"], km)

        assert len(resp.items) == 1
        row = resp.items[0]
        assert row.ltp == pytest.approx(0.0, abs=0.001)
        assert row.stale is True

    @pytest.mark.asyncio
    async def test_virtual_root_lkg_resolved_sym_preferred(self):
        """LKG lookup prefers the resolved contract symbol over the bare root."""
        from backend.brokers.broker_apis import record_good_ltp
        from backend.api.routes.quote import _serve_closed_hours_batch

        # Record LKG under the resolved contract name (not the bare root).
        record_good_ltp("CRUDEOILM26JULFUT", 6100.0)

        km = MagicMock()
        km.input_to_broker = {"MCX:CRUDEOIL": "MCX:CRUDEOILM26JULFUT"}

        with patch("backend.api.routes.quote._maybe_warm_closed_hours_quotes", new=AsyncMock()):
            resp = await _serve_closed_hours_batch(["MCX:CRUDEOIL"], km)

        assert len(resp.items) == 1
        row = resp.items[0]
        assert row.ltp == pytest.approx(6100.0, abs=0.001), (
            "closed-hours LKG must fall through to resolved contract symbol"
        )
        assert row.tradingsymbol == "CRUDEOIL"  # response still uses original key


# ---------------------------------------------------------------------------
# 4. SSOT: master functions reference helpers in source
# ---------------------------------------------------------------------------

class TestHelperSsot:

    def test_batch_quote_references_normalize(self):
        """batch_quote body must call _normalize_batch_keys."""
        import backend.api.routes.quote as q
        handler = q.QuoteController.batch_quote
        fn = getattr(handler, "fn", handler)
        src = inspect.getsource(fn)
        assert "_normalize_batch_keys" in src

    def test_batch_quote_references_serve_closed(self):
        """batch_quote body must delegate closed-hours path to _serve_closed_hours_batch."""
        import backend.api.routes.quote as q
        handler = q.QuoteController.batch_quote
        fn = getattr(handler, "fn", handler)
        src = inspect.getsource(fn)
        assert "_serve_closed_hours_batch" in src

    def test_batch_quote_references_build_live_row(self):
        """batch_quote body must call _build_live_batch_row for live path."""
        import backend.api.routes.quote as q
        handler = q.QuoteController.batch_quote
        fn = getattr(handler, "fn", handler)
        src = inspect.getsource(fn)
        assert "_build_live_batch_row" in src

    def test_batch_quote_references_subscribe(self):
        """batch_quote body must delegate ticker subscribe to _subscribe_batch_universe_to_ticker."""
        import backend.api.routes.quote as q
        handler = q.QuoteController.batch_quote
        fn = getattr(handler, "fn", handler)
        src = inspect.getsource(fn)
        assert "_subscribe_batch_universe_to_ticker" in src

    def test_batch_sparkline_references_normalize(self):
        """batch_sparkline body must call _normalize_sparkline_symbols."""
        import backend.api.routes.quote as q
        handler = q.SparklineController.batch_sparkline
        fn = getattr(handler, "fn", handler)
        src = inspect.getsource(fn)
        assert "_normalize_sparkline_symbols" in src

    def test_batch_sparkline_references_fetch_bars(self):
        """batch_sparkline body must delegate bar fetching to _fetch_bars_parallel."""
        import backend.api.routes.quote as q
        handler = q.SparklineController.batch_sparkline
        fn = getattr(handler, "fn", handler)
        src = inspect.getsource(fn)
        assert "_fetch_bars_parallel" in src

    def test_batch_sparkline_references_self_heal(self):
        """batch_sparkline body must delegate self-heal to _self_heal_empty_bars."""
        import backend.api.routes.quote as q
        handler = q.SparklineController.batch_sparkline
        fn = getattr(handler, "fn", handler)
        src = inspect.getsource(fn)
        assert "_self_heal_empty_bars" in src

    def test_batch_sparkline_references_resolve_ltps(self):
        """batch_sparkline body must delegate LTP resolution to _resolve_spark_ltps."""
        import backend.api.routes.quote as q
        handler = q.SparklineController.batch_sparkline
        fn = getattr(handler, "fn", handler)
        src = inspect.getsource(fn)
        assert "_resolve_spark_ltps" in src

    def test_batch_sparkline_references_compose(self):
        """batch_sparkline body must delegate composition to _compose_and_dual_write."""
        import backend.api.routes.quote as q
        handler = q.SparklineController.batch_sparkline
        fn = getattr(handler, "fn", handler)
        src = inspect.getsource(fn)
        assert "_compose_and_dual_write" in src

    def test_all_helpers_importable(self):
        """All new helpers must be importable directly from quote module."""
        from backend.api.routes.quote import (  # noqa: F401
            _normalize_batch_keys,
            _serve_closed_hours_batch,
            _build_live_batch_row,
            _record_live_batch_lkg,
            _subscribe_batch_universe_to_ticker,
            _normalize_sparkline_symbols,
            _fetch_bars_parallel,
            _self_heal_empty_bars,
            _resolve_spark_ltps,
            _compose_and_dual_write,
        )
