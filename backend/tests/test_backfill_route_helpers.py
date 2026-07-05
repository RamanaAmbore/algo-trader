"""Unit tests for the backfill helpers extracted from
PersistenceAdminController.backfill._run_backfill in
backend/api/routes/health.py.
"""
from unittest import mock

import pytest

from backend.api.routes.health import (
    _backfill_apply_mover_cap,
    _backfill_collect_book_symbols,
)


# ── _backfill_apply_mover_cap ────────────────────────────────────────────
class TestApplyMoverCap:
    def test_merges_movers_when_within_cap(self):
        with mock.patch(
            "backend.shared.helpers.mover_universe.mover_warm_pairs"
        ) as mwp:
            mwp.return_value = [("MOVER1", "NSE"), ("MOVER2", "NSE")]
            symbols = [("BOOK1", "NSE"), ("BOOK2", "NFO"), ("MOVER1", "NSE")]
            seen = set(symbols)
            out = _backfill_apply_mover_cap(symbols, seen, cap=10)
            assert ("BOOK1", "NSE") in out
            assert ("BOOK2", "NFO") in out
            assert ("MOVER1", "NSE") in out
            assert ("MOVER2", "NSE") in out

    def test_book_gets_priority_when_over_cap(self):
        """Universe must not evict operator-book pairs to fit movers."""
        with mock.patch(
            "backend.shared.helpers.mover_universe.mover_warm_pairs"
        ) as mwp:
            mwp.return_value = [("M1", "NSE"), ("M2", "NSE"), ("M3", "NSE")]
            book = [(f"B{i}", "NSE") for i in range(5)]
            out = _backfill_apply_mover_cap(book, set(book), cap=6)
            # cap=6, 5 book pairs → 1 slot for movers, then bulk-append via
            # `for key in _mwp()` then capped again to 6
            assert len(out) == 6
            # Every book row survives
            for b in book:
                assert b in out

    def test_book_exceeds_cap_is_truncated(self):
        with mock.patch(
            "backend.shared.helpers.mover_universe.mover_warm_pairs"
        ) as mwp:
            mwp.return_value = [("M1", "NSE")]
            book = [(f"B{i}", "NSE") for i in range(400)]
            out = _backfill_apply_mover_cap(book, set(book), cap=300)
            assert len(out) == 300

    def test_mover_universe_failure_returns_capped_symbols(self):
        with mock.patch(
            "backend.shared.helpers.mover_universe.mover_warm_pairs",
            side_effect=Exception("mover source down"),
        ):
            symbols = [(f"S{i}", "NSE") for i in range(400)]
            out = _backfill_apply_mover_cap(symbols, set(symbols), cap=300)
            assert len(out) == 300


# ── _backfill_collect_book_symbols ───────────────────────────────────────
class TestCollectBookSymbols:
    def test_empty_dataframes_no_error(self):
        with mock.patch(
            "backend.brokers.broker_apis.fetch_holdings", return_value=[]
        ), mock.patch(
            "backend.brokers.broker_apis.fetch_positions", return_value=[]
        ):
            symbols: list = []
            seen: set = set()
            _backfill_collect_book_symbols(symbols, seen)
            assert symbols == []
            assert seen == set()

    def test_populates_from_holdings_and_positions(self):
        import pandas as pd
        holdings_df = pd.DataFrame(
            {"tradingsymbol": ["INFY", "RELIANCE"],
             "exchange": ["NSE", "BSE"]}
        )
        positions_df = pd.DataFrame(
            {"tradingsymbol": ["NIFTY26JUN20000CE"],
             "exchange": ["NFO"]}
        )
        with mock.patch(
            "backend.brokers.broker_apis.fetch_holdings",
            return_value=[holdings_df],
        ), mock.patch(
            "backend.brokers.broker_apis.fetch_positions",
            return_value=[positions_df],
        ):
            symbols: list = []
            seen: set = set()
            _backfill_collect_book_symbols(symbols, seen)
            assert ("INFY", "NSE") in symbols
            assert ("RELIANCE", "BSE") in symbols
            assert ("NIFTY26JUN20000CE", "NFO") in symbols

    def test_dedupes_across_holdings_and_positions(self):
        import pandas as pd
        h = pd.DataFrame({"tradingsymbol": ["INFY"], "exchange": ["NSE"]})
        p = pd.DataFrame({"tradingsymbol": ["INFY"], "exchange": ["NSE"]})
        with mock.patch(
            "backend.brokers.broker_apis.fetch_holdings", return_value=[h],
        ), mock.patch(
            "backend.brokers.broker_apis.fetch_positions", return_value=[p],
        ):
            symbols: list = []
            seen: set = set()
            _backfill_collect_book_symbols(symbols, seen)
            assert symbols == [("INFY", "NSE")]

    def test_missing_exchange_column_defaults(self):
        import pandas as pd
        h = pd.DataFrame({"tradingsymbol": ["INFY"]})  # no exchange
        with mock.patch(
            "backend.brokers.broker_apis.fetch_holdings", return_value=[h],
        ), mock.patch(
            "backend.brokers.broker_apis.fetch_positions", return_value=[],
        ):
            symbols: list = []
            seen: set = set()
            _backfill_collect_book_symbols(symbols, seen)
            assert ("INFY", "NSE") in symbols

    def test_broker_failure_swallowed(self):
        with mock.patch(
            "backend.brokers.broker_apis.fetch_holdings",
            side_effect=Exception("broker unreachable"),
        ):
            symbols: list = []
            seen: set = set()
            _backfill_collect_book_symbols(symbols, seen)  # should not raise
            assert symbols == []
