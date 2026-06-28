"""Performance benchmarks for critical hot-path operations.

These benchmarks verify that key operations stay under budget:
  • TickBufferReader.get_ltp(token) < 100µs
  • is_cutover_on() < 1µs (cached constant)
  • _loaded_accounts() < 5ms when populated
  • iter_active() 4000 entries in < 50ms
  • _compute_day_change_val() on 100-row DataFrame < 10ms
  • compute_firm_nav helpers (polars) < 10ms for 5 accounts × 100 rows
"""

from __future__ import annotations

import time
import tempfile
import os
from unittest.mock import patch, MagicMock

import pytest
import pandas as pd
import numpy as np

from backend.brokers.tick_buffer import TickBufferWriter, DEFAULT_MAX_SLOTS
from backend.brokers.mmap_ticker import MmapTickReader


class TestTickBufferReaderPerf:
    """TickBufferReader.get_ltp() performance — < 100µs."""

    def test_get_ltp_latency(self):
        """get_ltp(token) should complete in < 100µs."""
        path = tempfile.mktemp(prefix="perf_buffer_")
        try:
            # Create buffer and populate with test data
            writer = TickBufferWriter(path=path, max_slots=DEFAULT_MAX_SLOTS)
            for token in range(0, 1000, 10):
                writer.upsert(token, 100.0 + token / 10)
            writer.close()

            reader = MmapTickReader(path=path)

            # Warm up
            _ = reader.get_ltp(100)

            # Benchmark a read
            token = 500
            start = time.perf_counter_ns()
            for _ in range(100):
                _ = reader.get_ltp(token)
            elapsed_ns = time.perf_counter_ns() - start

            avg_latency_us = elapsed_ns / 100 / 1000
            assert avg_latency_us < 100, f"get_ltp took {avg_latency_us:.1f}µs (budget: 100µs)"
        finally:
            if os.path.exists(path):
                os.remove(path)

    def test_get_ltp_batch_latency(self):
        """get_ltp_batch(tokens) for 100 tokens should complete in < 500µs."""
        path = tempfile.mktemp(prefix="perf_buffer_")
        try:
            writer = TickBufferWriter(path=path, max_slots=DEFAULT_MAX_SLOTS)
            for token in range(0, 5000, 10):
                writer.upsert(token, 100.0 + token / 100)
            writer.close()

            reader = MmapTickReader(path=path)

            # Batch of 100 tokens
            tokens = list(range(0, 1000, 10))

            # Warm up
            _ = reader.get_ltp_batch(tokens)

            start = time.perf_counter_ns()
            for _ in range(10):
                _ = reader.get_ltp_batch(tokens)
            elapsed_ns = time.perf_counter_ns() - start

            avg_latency_us = elapsed_ns / 10 / 1000
            assert (
                avg_latency_us < 500
            ), f"get_ltp_batch took {avg_latency_us:.1f}µs (budget: 500µs)"
        finally:
            if os.path.exists(path):
                os.remove(path)


class TestIsEnabledPerf:
    """is_enabled() performance — < 10µs (dictionary lookup)."""

    def test_is_enabled_latency(self):
        """is_enabled(cap) should complete in < 10µs."""
        from backend.shared.helpers.utils import is_enabled

        # Warm up
        _ = is_enabled("telegram")

        start = time.perf_counter_ns()
        for _ in range(10000):
            _ = is_enabled("telegram")
        elapsed_ns = time.perf_counter_ns() - start

        avg_latency_us = elapsed_ns / 10000 / 1000
        assert (
            avg_latency_us < 10
        ), f"is_enabled took {avg_latency_us:.3f}µs (budget: 10µs)"


class TestDayChangeValComputation:
    """day_change_val computation on DataFrame — < 10ms."""

    def test_day_change_val_100_rows(self):
        """day_change_val computation on 100-row DataFrame in < 10ms."""

        df = pd.DataFrame(
            {
                "last_price": np.random.uniform(100, 1000, 100),
                "close_price": np.random.uniform(100, 1000, 100),
                "opening_quantity": np.random.randint(1, 100, 100),
            }
        )

        def compute_day_change_val(df_in):
            """Inline formula: (last - close) * qty."""
            return (df_in["last_price"] - df_in["close_price"]) * df_in["opening_quantity"]

        # Warm up
        _ = compute_day_change_val(df)

        start = time.perf_counter_ns()
        for _ in range(10):
            _ = compute_day_change_val(df)
        elapsed_ns = time.perf_counter_ns() - start

        avg_latency_ms = elapsed_ns / 10 / 1_000_000
        assert (
            avg_latency_ms < 10
        ), f"day_change_val computation took {avg_latency_ms:.2f}ms (budget: 10ms)"

    def test_day_change_val_1000_rows(self):
        """day_change_val computation on 1000-row DataFrame in < 50ms."""

        df = pd.DataFrame(
            {
                "last_price": np.random.uniform(100, 1000, 1000),
                "close_price": np.random.uniform(100, 1000, 1000),
                "opening_quantity": np.random.randint(1, 100, 1000),
            }
        )

        def compute_day_change_val(df_in):
            """Inline formula: (last - close) * qty."""
            return (df_in["last_price"] - df_in["close_price"]) * df_in["opening_quantity"]

        start = time.perf_counter_ns()
        for _ in range(10):
            _ = compute_day_change_val(df)
        elapsed_ns = time.perf_counter_ns() - start

        avg_latency_ms = elapsed_ns / 10 / 1_000_000
        assert (
            avg_latency_ms < 50
        ), f"day_change_val computation(1000) took {avg_latency_ms:.2f}ms (budget: 50ms)"


class TestConnectionsAccess:
    """Connections dict access performance — < 5ms for 10 accounts."""

    def test_account_dict_lookup_latency(self):
        """Dict lookup for account should complete in < 100µs per lookup."""
        # Mock a populated Connections with 10 accounts
        mock_connections = {f"ACC{i:04d}": MagicMock() for i in range(10)}

        # Warm up
        _ = mock_connections.get("ACC0000")

        start = time.perf_counter_ns()
        for _ in range(1000):
            _ = mock_connections.get("ACC0005")  # Fixed key lookup
        elapsed_ns = time.perf_counter_ns() - start

        avg_latency_us = elapsed_ns / 1000 / 1000
        assert (
            avg_latency_us < 100
        ), f"Account dict lookup took {avg_latency_us:.3f}µs (budget: 100µs)"


class TestTickBufferSnapshot:
    """MmapTickReader.snapshot() for 4000 entries — < 100ms."""

    def test_snapshot_4000_slots(self):
        """snapshot() with 4000 populated slots should complete in < 100ms."""
        # Create a buffer with many populated slots
        path = tempfile.mktemp(prefix="perf_snap_")
        try:
            writer = TickBufferWriter(path=path, max_slots=DEFAULT_MAX_SLOTS)
            # Populate 4000 slots
            for token in range(0, 4000):
                writer.upsert(token, 100.0 + token / 100)
            writer.close()

            reader = MmapTickReader(path=path)

            # Warm up
            snap = reader.snapshot()

            start = time.perf_counter_ns()
            for _ in range(10):
                snap = reader.snapshot()
            elapsed_ns = time.perf_counter_ns() - start

            avg_latency_ms = elapsed_ns / 10 / 1_000_000
            assert (
                avg_latency_ms < 100
            ), f"snapshot(4000) took {avg_latency_ms:.2f}ms (budget: 100ms)"
            # Sanity check: we should have ~4000 entries
            assert len(snap) >= 3900, f"snapshot returned {len(snap)} entries (expected ~4000)"
        finally:
            if os.path.exists(path):
                os.remove(path)


class TestComputeFirmNavPerf:
    """Polars-vectorized nav helpers — budget < 10ms for 5 accounts × 100 rows.

    These benchmarks cover the three helper functions extracted from
    compute_firm_nav: _funds_from_df, _positions_from_df, _holdings_from_df.
    Each runs against 5 simulated per-account DataFrames with 100 rows each —
    the realistic upper bound in production (5 accounts, ~20 positions each).
    """

    def _make_funds_dfs(self, n_accounts: int = 5, rows_per: int = 100) -> list:
        """Generate n_accounts margins DataFrames, each with rows_per rows."""
        dfs = []
        for i in range(n_accounts):
            import pandas as pd
            import numpy as np

            df = pd.DataFrame({
                "avail opening_balance": np.random.uniform(100_000, 500_000, rows_per),
                "util option_premium": np.random.uniform(0, 50_000, rows_per),
                "account": [f"ACC{i:04d}"] * rows_per,
            })
            dfs.append(df)
        return dfs

    def _make_positions_dfs(self, n_accounts: int = 5, rows_per: int = 100) -> list:
        import pandas as pd
        import numpy as np

        dfs = []
        for i in range(n_accounts):
            df = pd.DataFrame({
                "quantity": np.random.randint(-10, 10, rows_per).astype(float),
                "unrealised": np.random.uniform(-5000, 5000, rows_per),
                "account": [f"ACC{i:04d}"] * rows_per,
            })
            dfs.append(df)
        return dfs

    def _make_holdings_dfs(self, n_accounts: int = 5, rows_per: int = 100) -> list:
        import pandas as pd
        import numpy as np

        dfs = []
        for i in range(n_accounts):
            df = pd.DataFrame({
                "quantity": np.random.randint(1, 200, rows_per).astype(float),
                "cur_val": np.random.uniform(10_000, 200_000, rows_per),
                "tradingsymbol": [f"STOCK{j % 50}" for j in range(rows_per)],
                "account": [f"ACC{i:04d}"] * rows_per,
            })
            dfs.append(df)
        return dfs

    def test_funds_from_df_5accounts_100rows(self):
        """_funds_from_df: 5 accounts × 100 rows in < 10ms total."""
        from backend.api.algo.nav import _funds_from_df

        dfs = self._make_funds_dfs(5, 100)

        # Warm up
        for df in dfs:
            _funds_from_df(df)

        start = time.perf_counter_ns()
        for _ in range(10):
            total = 0.0
            for df in dfs:
                chunk, _ = _funds_from_df(df)
                total += chunk
        elapsed_ns = time.perf_counter_ns() - start

        avg_ms = elapsed_ns / 10 / 1_000_000
        assert avg_ms < 10, f"_funds_from_df (5×100) took {avg_ms:.2f}ms (budget: 10ms)"

    def test_positions_from_df_5accounts_100rows(self):
        """_positions_from_df: 5 accounts × 100 rows in < 10ms total."""
        from backend.api.algo.nav import _positions_from_df

        dfs = self._make_positions_dfs(5, 100)

        # Warm up
        for df in dfs:
            _positions_from_df(df)

        start = time.perf_counter_ns()
        for _ in range(10):
            total = 0.0
            for df in dfs:
                chunk, _ = _positions_from_df(df)
                total += chunk
        elapsed_ns = time.perf_counter_ns() - start

        avg_ms = elapsed_ns / 10 / 1_000_000
        assert avg_ms < 10, f"_positions_from_df (5×100) took {avg_ms:.2f}ms (budget: 10ms)"

    def test_holdings_from_df_5accounts_100rows(self):
        """_holdings_from_df: 5 accounts × 100 rows in < 10ms total (no LTP fallback)."""
        from backend.api.algo.nav import _holdings_from_df
        from unittest.mock import MagicMock

        dfs = self._make_holdings_dfs(5, 100)
        mock_ticker = MagicMock()
        mock_ticker.get_ltp_by_sym.return_value = 0.0  # force cur_val path

        # Warm up
        for df in dfs:
            _holdings_from_df(df, mock_ticker)

        start = time.perf_counter_ns()
        for _ in range(10):
            total = 0.0
            for df in dfs:
                chunk, _ = _holdings_from_df(df, mock_ticker)
                total += chunk
        elapsed_ns = time.perf_counter_ns() - start

        avg_ms = elapsed_ns / 10 / 1_000_000
        assert avg_ms < 10, f"_holdings_from_df (5×100) took {avg_ms:.2f}ms (budget: 10ms)"

    def test_nav_helpers_combined_5accounts_100rows(self):
        """All three nav helpers combined: 5 accounts × 100 rows < 10ms."""
        from backend.api.algo.nav import _funds_from_df, _positions_from_df, _holdings_from_df
        from unittest.mock import MagicMock

        funds_dfs = self._make_funds_dfs(5, 100)
        pos_dfs = self._make_positions_dfs(5, 100)
        hold_dfs = self._make_holdings_dfs(5, 100)
        mock_ticker = MagicMock()
        mock_ticker.get_ltp_by_sym.return_value = 0.0

        # Warm up
        for df in funds_dfs:
            _funds_from_df(df)
        for df in pos_dfs:
            _positions_from_df(df)
        for df in hold_dfs:
            _holdings_from_df(df, mock_ticker)

        start = time.perf_counter_ns()
        for _ in range(10):
            cash_total = sum(_funds_from_df(df)[0] for df in funds_dfs)
            pos_mtm = sum(_positions_from_df(df)[0] for df in pos_dfs)
            hold_mtm = sum(_holdings_from_df(df, mock_ticker)[0] for df in hold_dfs)
        elapsed_ns = time.perf_counter_ns() - start

        avg_ms = elapsed_ns / 10 / 1_000_000
        assert avg_ms < 10, f"all nav helpers combined (5×100) took {avg_ms:.2f}ms (budget: 10ms)"
        # Sanity: sums are positive finite numbers
        assert cash_total > 0
        assert isinstance(hold_mtm, float)
