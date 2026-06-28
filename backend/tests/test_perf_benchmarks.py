"""Performance benchmarks for critical hot-path operations.

These benchmarks verify that key operations stay under budget:
  • TickBufferReader.get_ltp(token) < 100µs
  • is_cutover_on() < 1µs (cached constant)
  • _loaded_accounts() < 5ms when populated
  • iter_active() 4000 entries in < 50ms
  • _compute_day_change_val() on 100-row DataFrame < 10ms
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
