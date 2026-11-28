"""Tests for broker qty normalization fixes.

Fix 1: Dhan MCX trades — _normalise_trades must expand tradedQuantity by lot_size
        for MCX_COMM exchangeSegment using _DHAN_LOT_BY_SECURITY cache.

Fix 2: GrowwBroker.translate_qty — must return raw_qty unchanged for all exchanges
        (Groww uses CONTRACTS for all exchanges including MCX).
"""

import importlib
import os
import sys

import pytest

# ---------------------------------------------------------------------------
# Ensure broker module can be imported in test context
# ---------------------------------------------------------------------------

os.environ.setdefault("PYTEST_RUNNING", "1")


def _dhan_mod():
    """Return the dhan adapter module, re-importing cleanly each time if needed."""
    import backend.brokers.adapters.dhan as m
    return m


def _groww_mod():
    import backend.brokers.adapters.groww as m
    return m


# ---------------------------------------------------------------------------
# Fix 1 — _normalise_trades lot expansion
# ---------------------------------------------------------------------------


class TestDhanNormaliseTrades:
    """_normalise_trades must expand MCX tradedQuantity by lot_size from cache."""

    def setup_method(self):
        """Clear and pre-populate the lot cache before each test."""
        m = _dhan_mod()
        m._DHAN_LOT_BY_SECURITY.clear()
        # 2026-11-27 fix — the commodity-root fallback is a SEPARATE
        # module-level dict from `_DHAN_LOT_BY_SECURITY`; must be cleared
        # too so a later test's root entry (e.g. "CRUDEOIL") can never
        # leak into these cache-miss tests and silently resolve a qty
        # these tests expect to stay raw/unresolved.
        m._DHAN_LOT_BY_MCX_ROOT.clear()

    def teardown_method(self):
        """Leave cache clean after each test."""
        _dhan_mod()._DHAN_LOT_BY_SECURITY.clear()
        _dhan_mod()._DHAN_LOT_BY_MCX_ROOT.clear()

    def _make_trade(self, *, security_id: str, traded_qty: int, segment: str,
                    exchange: str = "MCX") -> dict:
        return {
            "tradeId": "T001",
            "orderId": "O001",
            "tradingSymbol": "CRUDEOIL24DECFUT",
            "exchange": exchange,
            "exchangeSegment": segment,
            "securityId": security_id,
            "transactionType": "BUY",
            "tradedQuantity": traded_qty,
            "tradedPrice": 5800.0,
            "exchangeTime": "2024-12-10 10:00:00",
        }

    def _wrap(self, *trades: dict) -> dict:
        """Wrap trade dicts in the Dhan {status, data} envelope expected by _unwrap."""
        return {"status": "success", "data": list(trades)}

    def test_mcx_lot_cache_hit_expands_qty(self):
        """MCX trade with cache hit: tradedQuantity=1 × lot_size=100 → quantity=100."""
        m = _dhan_mod()
        m._DHAN_LOT_BY_SECURITY["SID123"] = 100

        trade = self._make_trade(security_id="SID123", traded_qty=1, segment="MCX_COMM")
        result = m._normalise_trades(self._wrap(trade))

        assert len(result) == 1
        assert result[0]["quantity"] == 100

    def test_mcx_lot_cache_hit_multiple_lots(self):
        """MCX trade with 3 lots and lot_size=100 → quantity=300."""
        m = _dhan_mod()
        m._DHAN_LOT_BY_SECURITY["SID999"] = 100

        trade = self._make_trade(security_id="SID999", traded_qty=3, segment="MCX_COMM")
        result = m._normalise_trades(self._wrap(trade))

        assert result[0]["quantity"] == 300

    def test_mcx_lot_cache_miss_returns_raw_qty(self):
        """MCX trade with no cache entry: returns raw qty=1, no crash."""
        m = _dhan_mod()
        # Cache is empty — no entry for this security

        trade = self._make_trade(security_id="MISSING_SID", traded_qty=1, segment="MCX_COMM")
        result = m._normalise_trades(self._wrap(trade))

        assert len(result) == 1
        assert result[0]["quantity"] == 1  # raw qty preserved

    def test_mcx_cache_miss_no_exception(self):
        """Cache miss must not raise — warning logged but function returns normally."""
        m = _dhan_mod()
        trade = self._make_trade(security_id="SID_UNKNOWN", traded_qty=5, segment="MCX_COMM")

        # Should not raise
        result = m._normalise_trades(self._wrap(trade))
        assert result[0]["quantity"] == 5

    def test_nfo_trade_qty_unchanged(self):
        """NFO (non-MCX_COMM) trade: tradedQuantity=150 → quantity=150 (no expansion)."""
        m = _dhan_mod()
        m._DHAN_LOT_BY_SECURITY["SID_NFO"] = 50  # cache populated but should not apply

        trade = {
            "tradeId": "T002",
            "orderId": "O002",
            "tradingSymbol": "NIFTY24DECFUT",
            "exchange": "NSE",
            "exchangeSegment": "NSE_FNO",
            "securityId": "SID_NFO",
            "transactionType": "SELL",
            "tradedQuantity": 150,
            "tradedPrice": 23500.0,
            "exchangeTime": "2024-12-10 10:05:00",
        }
        result = m._normalise_trades(self._wrap(trade))

        assert result[0]["quantity"] == 150

    def test_non_mcx_segment_qty_unchanged(self):
        """BSE_EQ segment trade: qty passes through without expansion."""
        m = _dhan_mod()
        trade = {
            "tradeId": "T003",
            "orderId": "O003",
            "tradingSymbol": "RELIANCE",
            "exchange": "BSE",
            "exchangeSegment": "BSE_EQ",
            "securityId": "SID_REL",
            "transactionType": "BUY",
            "tradedQuantity": 10,
            "tradedPrice": 2900.0,
            "exchangeTime": "2024-12-10 10:10:00",
        }
        result = m._normalise_trades(self._wrap(trade))
        assert result[0]["quantity"] == 10

    def test_zero_qty_mcx_not_expanded(self):
        """Zero tradedQuantity on MCX: stays 0, no expansion guard (0 > 0 is False)."""
        m = _dhan_mod()
        m._DHAN_LOT_BY_SECURITY["SID_ZERO"] = 100

        trade = self._make_trade(security_id="SID_ZERO", traded_qty=0, segment="MCX_COMM")
        result = m._normalise_trades(self._wrap(trade))

        assert result[0]["quantity"] == 0

    def test_other_fields_preserved(self):
        """Other normalised fields (trade_id, order_id, price, etc.) must not be altered."""
        m = _dhan_mod()
        m._DHAN_LOT_BY_SECURITY["SID_FIELDS"] = 100

        trade = self._make_trade(security_id="SID_FIELDS", traded_qty=2, segment="MCX_COMM")
        trade["tradeId"] = "TRADE_XYZ"
        trade["orderId"] = "ORDER_ABC"
        trade["tradedPrice"] = 6100.5

        result = m._normalise_trades(self._wrap(trade))
        row = result[0]

        assert row["trade_id"] == "TRADE_XYZ"
        assert row["order_id"] == "ORDER_ABC"
        assert row["average_price"] == pytest.approx(6100.5)
        assert row["transaction_type"] == "BUY"
        assert row["quantity"] == 200  # 2 lots × 100


# ---------------------------------------------------------------------------
# Fix 1 (2026-11-27 revision) — expired-MCX historical backfill:
# security_id cache miss → commodity-root fallback → strict hard-skip.
# ---------------------------------------------------------------------------


class TestDhanNormaliseTradesRootFallbackAndStrictSkip:
    """An expired MCX contract's security_id is absent from
    `_DHAN_LOT_BY_SECURITY` (built from TODAY's live master only) — the
    overwhelming common case for a historical backfill. These tests
    cover the fallback chain `_normalise_trades()` now applies:
    security_id → commodity-root → (strict) hard skip."""

    def setup_method(self):
        m = _dhan_mod()
        m._DHAN_LOT_BY_SECURITY.clear()
        m._DHAN_LOT_BY_MCX_ROOT.clear()

    def teardown_method(self):
        m = _dhan_mod()
        m._DHAN_LOT_BY_SECURITY.clear()
        m._DHAN_LOT_BY_MCX_ROOT.clear()

    def _make_trade(self, *, security_id: str, traded_qty: int, symbol: str,
                     exchange: str = "MCX", segment: str = "MCX_COMM") -> dict:
        return {
            "tradeId": "T100", "orderId": "O100",
            "tradingSymbol": symbol, "exchange": exchange,
            "exchangeSegment": segment, "securityId": security_id,
            "transactionType": "BUY", "tradedQuantity": traded_qty,
            "tradedPrice": 5800.0, "exchangeTime": "2026-05-10 10:00:00",
        }

    def _wrap(self, *trades: dict) -> dict:
        return {"status": "success", "data": list(trades)}

    def test_expired_security_id_resolves_via_live_root_fallback(self):
        """An EXPIRED contract's security_id ('EXPIRED_SID') is absent
        from _DHAN_LOT_BY_SECURITY, but a currently-live sibling
        contract under the same commodity root ('CRUDEOIL') already
        populated _DHAN_LOT_BY_MCX_ROOT — the expired trade must resolve
        to the CORRECT contract quantity via the root, not the raw
        (wrong-unit) quantity."""
        m = _dhan_mod()
        m._DHAN_LOT_BY_MCX_ROOT["CRUDEOIL"] = 100

        # Dhan-format raw symbol for an expired month ("ROOT-DDMonYYYY-FUT").
        trade = self._make_trade(
            security_id="EXPIRED_SID_NOT_IN_LIVE_MASTER",
            traded_qty=2, symbol="CRUDEOIL-17DEC2025-FUT",
        )
        result = m._normalise_trades(self._wrap(trade))

        assert len(result) == 1
        assert result[0]["quantity"] == 200  # 2 lots × 100 (root fallback)
        assert result[0]["tradingsymbol"] == "CRUDEOIL25DECFUT"

    def test_security_id_hit_takes_priority_over_root_fallback(self):
        """A still-live security_id must win over the root fallback, even
        when both are populated and disagree — exact match is always
        preferred over the coarser root-level fallback."""
        m = _dhan_mod()
        m._DHAN_LOT_BY_SECURITY["LIVE_SID"] = 10   # exact — e.g. GOLDM
        m._DHAN_LOT_BY_MCX_ROOT["GOLDM"] = 999      # deliberately disagrees

        trade = self._make_trade(
            security_id="LIVE_SID", traded_qty=1, symbol="GOLDM-05DEC2026-72000-CE",
        )
        result = m._normalise_trades(self._wrap(trade))
        assert result[0]["quantity"] == 10

    def test_goldm_root_never_resolves_via_gold_root(self):
        """GOLDM (mini) and GOLD are DIFFERENT roots with different lot
        sizes — an expired GOLDM trade must never silently resolve via
        GOLD's root entry (root keys are exact-string matches, not
        prefix/substring matches)."""
        m = _dhan_mod()
        m._DHAN_LOT_BY_MCX_ROOT["GOLD"] = 100
        # No "GOLDM" entry at all — deliberately unresolved for this test.

        trade = self._make_trade(
            security_id="EXPIRED_GOLDM_SID", traded_qty=1,
            symbol="GOLDM-05DEC2025-72000-CE",
        )
        # Non-strict (live-path default): falls through to raw qty, never
        # the wrong GOLD lot size.
        result = m._normalise_trades(self._wrap(trade))
        assert result[0]["quantity"] == 1

    def test_crudeoilm_root_never_resolves_via_crudeoil_root(self):
        """CRUDEOILM (mini, lot_size 10) must never resolve via
        CRUDEOIL's (lot_size 100) root entry — distinct root strings."""
        m = _dhan_mod()
        m._DHAN_LOT_BY_MCX_ROOT["CRUDEOIL"] = 100
        m._DHAN_LOT_BY_MCX_ROOT["CRUDEOILM"] = 10

        trade = self._make_trade(
            security_id="EXPIRED_CRUDEOILM_SID", traded_qty=3,
            symbol="CRUDEOILM-17DEC2025-FUT",
        )
        result = m._normalise_trades(self._wrap(trade))
        assert result[0]["quantity"] == 30  # 3 × 10, never 3 × 100

    def test_non_strict_default_unresolved_stores_raw_qty_with_warning(self):
        """Live `trades()` path (strict_mcx_lot=False, the default) must
        be BYTE-IDENTICAL to the pre-2026-11-27 behaviour on a total
        cache miss: store the raw quantity, never skip a same-day fill."""
        m = _dhan_mod()
        trade = self._make_trade(
            security_id="TOTALLY_UNKNOWN_SID", traded_qty=4,
            symbol="SOMENEWCOMMODITY-17DEC2026-FUT",
        )
        result = m._normalise_trades(self._wrap(trade))
        assert len(result) == 1
        assert result[0]["quantity"] == 4

    def test_strict_mode_skips_unresolved_row_never_stores_wrong_qty(self):
        """Backfill path (strict_mcx_lot=True): a totally unresolved MCX
        row is SKIPPED outright — never stored with a known-possibly-
        wrong quantity."""
        m = _dhan_mod()
        trade = self._make_trade(
            security_id="TOTALLY_UNKNOWN_SID", traded_qty=4,
            symbol="SOMENEWCOMMODITY-17DEC2026-FUT",
        )
        result = m._normalise_trades(self._wrap(trade), strict_mcx_lot=True)
        assert result == []

    def test_strict_mode_records_unresolved_entry_for_caller_to_report(self):
        """The `unresolved` out-list captures (security_id, Kite-form
        symbol, exchange_timestamp) for the skipped row so the caller
        can emit one aggregated CRITICAL naming exactly what couldn't be
        resolved."""
        m = _dhan_mod()
        trade = self._make_trade(
            security_id="SID_GHOST", traded_qty=1,
            symbol="SOMENEWCOMMODITY-17DEC2026-FUT",
        )
        unresolved: list = []
        result = m._normalise_trades(
            self._wrap(trade), strict_mcx_lot=True, unresolved=unresolved,
        )
        assert result == []
        assert unresolved == [
            ("SID_GHOST", "SOMENEWCOMMODITY26DECFUT", "2026-05-10 10:00:00")
        ]

    def test_strict_mode_resolved_rows_never_skipped(self):
        """Strict mode must only skip the UNRESOLVED rows — a row that
        resolves via security_id or root fallback is kept, not dropped,
        just because strict_mcx_lot=True is set."""
        m = _dhan_mod()
        m._DHAN_LOT_BY_SECURITY["RESOLVED_SID"] = 100
        trade = self._make_trade(
            security_id="RESOLVED_SID", traded_qty=1, symbol="CRUDEOIL-17DEC2026-FUT",
        )
        result = m._normalise_trades(self._wrap(trade), strict_mcx_lot=True)
        assert len(result) == 1
        assert result[0]["quantity"] == 100

    def test_strict_mode_does_not_affect_non_mcx_rows(self):
        """A non-MCX row is never touched by strict_mcx_lot at all —
        only the MCX_COMM lot-resolution branch reads the flag."""
        m = _dhan_mod()
        trade = {
            "tradeId": "T1", "orderId": "O1", "tradingSymbol": "INFY",
            "exchange": "NSE", "exchangeSegment": "NSE_EQ",
            "securityId": "SID_INFY", "transactionType": "BUY",
            "tradedQuantity": 10, "tradedPrice": 1500.0,
            "exchangeTime": "2026-05-10 10:00:00",
        }
        result = m._normalise_trades(self._wrap(trade), strict_mcx_lot=True)
        assert len(result) == 1
        assert result[0]["quantity"] == 10


# ---------------------------------------------------------------------------
# Fix 1 — _apply_dhan_instruments populates _DHAN_LOT_BY_SECURITY
# ---------------------------------------------------------------------------


class TestApplyDhanInstrumentsLotCache:
    """_apply_dhan_instruments must populate _DHAN_LOT_BY_SECURITY for MCX/NCO rows."""

    def setup_method(self):
        _dhan_mod()._DHAN_LOT_BY_SECURITY.clear()
        _dhan_mod()._DHAN_LOT_BY_MCX_ROOT.clear()

    def teardown_method(self):
        _dhan_mod()._DHAN_LOT_BY_SECURITY.clear()
        _dhan_mod()._DHAN_LOT_BY_MCX_ROOT.clear()

    def _build_data(self, by_exchange: dict) -> dict:
        return {"by_exchange": by_exchange, "by_symbol": {}, "date": "2024-12-10"}

    def test_mcx_rows_populate_lot_cache(self):
        """MCX rows with lot_size > 1 are added to _DHAN_LOT_BY_SECURITY."""
        m = _dhan_mod()
        data = self._build_data({
            "MCX": [
                {"security_id": "SID_A", "lot_size": 100},
                {"security_id": "SID_B", "lot_size": 50},
            ]
        })
        m._apply_dhan_instruments(data)

        assert m._DHAN_LOT_BY_SECURITY["SID_A"] == 100
        assert m._DHAN_LOT_BY_SECURITY["SID_B"] == 50

    def test_nco_rows_populate_lot_cache(self):
        """NCO rows with lot_size > 1 are added to _DHAN_LOT_BY_SECURITY."""
        m = _dhan_mod()
        data = self._build_data({
            "NCO": [
                {"security_id": "SID_NCO", "lot_size": 25},
            ]
        })
        m._apply_dhan_instruments(data)

        assert m._DHAN_LOT_BY_SECURITY["SID_NCO"] == 25

    def test_nse_rows_not_in_lot_cache(self):
        """NSE rows are not added to the lot cache."""
        m = _dhan_mod()
        data = self._build_data({
            "NSE": [
                {"security_id": "SID_NSE", "lot_size": 50},
            ]
        })
        m._apply_dhan_instruments(data)

        assert "SID_NSE" not in m._DHAN_LOT_BY_SECURITY

    def test_lot_size_lte_1_excluded(self):
        """Rows with lot_size=1 or 0 are not added to the cache."""
        m = _dhan_mod()
        data = self._build_data({
            "MCX": [
                {"security_id": "SID_ONE", "lot_size": 1},
                {"security_id": "SID_ZERO", "lot_size": 0},
            ]
        })
        m._apply_dhan_instruments(data)

        assert "SID_ONE" not in m._DHAN_LOT_BY_SECURITY
        assert "SID_ZERO" not in m._DHAN_LOT_BY_SECURITY

    def test_apply_clears_stale_cache(self):
        """A second call to _apply_dhan_instruments clears the previous lot cache."""
        m = _dhan_mod()
        m._DHAN_LOT_BY_SECURITY["STALE_SID"] = 999

        data = self._build_data({
            "MCX": [{"security_id": "FRESH_SID", "lot_size": 100}]
        })
        m._apply_dhan_instruments(data)

        assert "STALE_SID" not in m._DHAN_LOT_BY_SECURITY
        assert m._DHAN_LOT_BY_SECURITY["FRESH_SID"] == 100

    def test_mcx_rows_populate_root_fallback_cache(self):
        """Every live row under one commodity root agreeing on lot_size
        populates _DHAN_LOT_BY_MCX_ROOT, keyed by the Kite-form root
        (e.g. 'CRUDEOIL'), regardless of expiry month — two different
        expiry-month rows for the same root agreeing on lot_size=100."""
        m = _dhan_mod()
        data = self._build_data({
            "MCX": [
                {"security_id": "SID_DEC", "lot_size": 100,
                 "tradingsymbol": "CRUDEOIL26DECFUT"},
                {"security_id": "SID_JAN", "lot_size": 100,
                 "tradingsymbol": "CRUDEOIL27JANFUT"},
            ]
        })
        m._apply_dhan_instruments(data)
        assert m._DHAN_LOT_BY_MCX_ROOT["CRUDEOIL"] == 100

    def test_conflicting_root_lot_sizes_left_unresolved(self):
        """Two rows sharing the SAME root string but DISAGREEING on
        lot_size must leave that root OUT of the fallback dict entirely
        — never guessed from an ambiguous live master."""
        m = _dhan_mod()
        data = self._build_data({
            "MCX": [
                {"security_id": "SID_X", "lot_size": 100,
                 "tradingsymbol": "WEIRDCOMMODITY26DECFUT"},
                {"security_id": "SID_Y", "lot_size": 50,
                 "tradingsymbol": "WEIRDCOMMODITY27JANFUT"},
            ]
        })
        m._apply_dhan_instruments(data)
        assert "WEIRDCOMMODITY" not in m._DHAN_LOT_BY_MCX_ROOT

    def test_distinct_roots_never_collide(self):
        """CRUDEOIL and CRUDEOILM are DISTINCT root strings and must
        resolve to their own independent lot sizes."""
        m = _dhan_mod()
        data = self._build_data({
            "MCX": [
                {"security_id": "SID_FULL", "lot_size": 100,
                 "tradingsymbol": "CRUDEOIL26DECFUT"},
                {"security_id": "SID_MINI", "lot_size": 10,
                 "tradingsymbol": "CRUDEOILM26DECFUT"},
            ]
        })
        m._apply_dhan_instruments(data)
        assert m._DHAN_LOT_BY_MCX_ROOT["CRUDEOIL"] == 100
        assert m._DHAN_LOT_BY_MCX_ROOT["CRUDEOILM"] == 10

    def test_apply_clears_stale_root_cache(self):
        """A second call to _apply_dhan_instruments clears the previous
        root-fallback cache, same as the security_id cache."""
        m = _dhan_mod()
        m._DHAN_LOT_BY_MCX_ROOT["STALE_ROOT"] = 999

        data = self._build_data({
            "MCX": [{"security_id": "FRESH_SID", "lot_size": 100,
                      "tradingsymbol": "FRESHROOT26DECFUT"}]
        })
        m._apply_dhan_instruments(data)

        assert "STALE_ROOT" not in m._DHAN_LOT_BY_MCX_ROOT
        assert m._DHAN_LOT_BY_MCX_ROOT["FRESHROOT"] == 100


# ---------------------------------------------------------------------------
# Fix 2 — GrowwBroker.translate_qty returns raw qty for all exchanges
# ---------------------------------------------------------------------------


class TestGrowwTranslateQty:
    """GrowwBroker.translate_qty must return raw_qty unchanged for all exchanges."""

    def _make_groww_broker(self):
        """Instantiate GrowwBroker without a real GrowwConnection using __new__."""
        from backend.brokers.adapters.groww import GrowwBroker
        obj = GrowwBroker.__new__(GrowwBroker)
        return obj

    def test_mcx_returns_raw_qty(self):
        """translate_qty('MCX', 100, 100) → 100 (not 1)."""
        broker = self._make_groww_broker()
        assert broker.translate_qty("MCX", 100, 100) == 100

    def test_nfo_returns_raw_qty(self):
        """translate_qty('NFO', 75, 75) → 75."""
        broker = self._make_groww_broker()
        assert broker.translate_qty("NFO", 75, 75) == 75

    def test_nse_returns_raw_qty(self):
        """NSE equity: translate_qty('NSE', 10, 1) → 10."""
        broker = self._make_groww_broker()
        assert broker.translate_qty("NSE", 10, 1) == 10

    def test_nco_sub_lot_raises(self):
        """NCO: translate_qty('NCO', 5, 100) — sub-lot qty (2026-09 orders-page
        council audit finding #6 fix). Previously returned 5 unchanged; Groww's
        no-conversion behavior is still correct (contracts stay contracts) but
        a sub-lot quantity is a malformed order regardless of unit convention
        (no exchange allows fractional lots) and must now raise, matching the
        same guard Kite/Dhan already get via @exchange_qty_convention."""
        broker = self._make_groww_broker()
        with pytest.raises(ValueError, match="QTY-GUARD"):
            broker.translate_qty("NCO", 5, 100)

    def test_zero_qty_returns_zero(self):
        """Zero qty stays zero regardless of exchange."""
        broker = self._make_groww_broker()
        assert broker.translate_qty("MCX", 0, 100) == 0

    def test_large_mcx_qty_not_divided(self):
        """Large MCX contract qty is returned as-is — no division by lot_size."""
        broker = self._make_groww_broker()
        # If base class wrongly divided 500 by 100 → 5; this asserts no division.
        assert broker.translate_qty("MCX", 500, 100) == 500
