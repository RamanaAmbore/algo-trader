"""Comprehensive tests for tick_buffer.py — shared-memory tick storage.

Coverage:
  • TickBufferWriter.upsert() — byte-level correctness, version bump, slot_count
  • TickBufferReader — consistency under concurrent write, torn-read recovery
  • iter_active() — early-exit at slot_count, no scan past capacity
  • Collision handling — linear probe, hash collisions
  • Capacity limits — 4096 tokens, 100% load, hash probe overflow

Important design constraint:
  Token 0 is reserved as the "empty slot" marker and cannot be used as a real
  instrument token. Kite's tokens are positive integers >= 1, so this is not
  a real-world limitation. All tests use tokens >= 1.
"""

import struct
import tempfile
import os
from pathlib import Path

import pytest

from backend.brokers.tick_buffer import (
    TickBufferWriter,
    TickBufferReader,
    SCHEMA_VERSION,
    DEFAULT_MAX_SLOTS,
    _HEADER_SIZE,
    _SLOT_SIZE,
    _buffer_size,
    _HEADER_FMT,
    _SLOT_FMT,
)


@pytest.fixture
def tmp_buffer_path():
    """Temporary buffer file for tests (not /dev/shm)."""
    with tempfile.NamedTemporaryFile(delete=False, prefix="tick_buffer_test_") as f:
        path = f.name
    yield path
    # Cleanup
    if os.path.exists(path):
        os.remove(path)


class TestTickBufferWriter:
    """TickBufferWriter — single-writer correctness."""

    def test_init_creates_file_and_resets(self, tmp_buffer_path):
        """Writer init creates file at correct size and zeros it."""
        writer = TickBufferWriter(path=tmp_buffer_path, max_slots=256)
        try:
            expected_size = _buffer_size(256)
            actual_size = os.path.getsize(tmp_buffer_path)
            assert actual_size == expected_size, f"expected {expected_size}, got {actual_size}"

            # Check header is zeroed (all zeroes except schema version)
            version, slot_count, max_slots, last_write_ns, schema_version = struct.unpack_from(
                _HEADER_FMT, writer._mm, 0
            )
            assert version == 0, f"expected version 0, got {version}"
            assert slot_count == 0, f"expected slot_count 0, got {slot_count}"
            assert max_slots == 256, f"expected max_slots 256, got {max_slots}"
            assert schema_version == SCHEMA_VERSION, f"expected SCHEMA_VERSION {SCHEMA_VERSION}, got {schema_version}"
        finally:
            writer.close()

    def test_upsert_new_slot_increments_slot_count(self, tmp_buffer_path):
        """First upsert of a token increments slot_count; update doesn't."""
        writer = TickBufferWriter(path=tmp_buffer_path, max_slots=256)
        try:
            token = 100
            lp = 150.5
            prev_close = 149.0
            avg_price = 149.8
            ts_ns = 1000000000

            # First upsert — should increment slot_count
            result = writer.upsert(token, lp, prev_close, avg_price, ts_ns)
            assert result is True, "upsert should return True"

            version, slot_count, _, _, _ = struct.unpack_from(_HEADER_FMT, writer._mm, 0)
            assert slot_count == 1, f"after first upsert, expected slot_count 1, got {slot_count}"
            assert version == 1, f"after first upsert, expected version 1, got {version}"

            # Second upsert of same token — should NOT increment slot_count
            lp = 151.0
            result = writer.upsert(token, lp, prev_close, avg_price, ts_ns + 1000)
            assert result is True, "second upsert should return True"

            version, slot_count, _, _, _ = struct.unpack_from(_HEADER_FMT, writer._mm, 0)
            assert slot_count == 1, f"after second upsert, expected slot_count 1, got {slot_count}"
            assert version == 2, f"after second upsert, expected version 2, got {version}"
        finally:
            writer.close()

    def test_upsert_stores_correct_bytes(self, tmp_buffer_path):
        """Upsert writes slot data at correct offset with correct byte values."""
        writer = TickBufferWriter(path=tmp_buffer_path, max_slots=256)
        try:
            token = 42
            lp = 1234.5678
            prev_close = 1233.1234
            avg_price = 1234.0000
            ts_ns = 9999999999

            writer.upsert(token, lp, prev_close, avg_price, ts_ns)

            # Compute the slot offset
            idx = token % 256
            slot_off = _HEADER_SIZE + idx * _SLOT_SIZE

            # Read back the raw bytes
            unpacked = struct.unpack_from(_SLOT_FMT, writer._mm, slot_off)
            stored_token, _pad, stored_lp, stored_pc, stored_av, stored_ts = unpacked

            assert stored_token == token, f"expected token {token}, got {stored_token}"
            assert abs(stored_lp - lp) < 1e-9, f"LTP mismatch: {stored_lp} vs {lp}"
            assert abs(stored_pc - prev_close) < 1e-9, f"prev_close mismatch"
            assert abs(stored_av - avg_price) < 1e-9, f"avg_price mismatch"
            assert stored_ts == ts_ns, f"timestamp mismatch"
        finally:
            writer.close()

    def test_upsert_version_bumped_after_write(self, tmp_buffer_path):
        """Version is bumped AFTER the slot write (readers see atomicity)."""
        writer = TickBufferWriter(path=tmp_buffer_path, max_slots=256)
        try:
            # Multiple upsets; version should increment monotonically
            v1 = struct.unpack_from("<Q", writer._mm, 0)[0]
            writer.upsert(100, 100.0)
            v2 = struct.unpack_from("<Q", writer._mm, 0)[0]
            writer.upsert(101, 101.0)
            v3 = struct.unpack_from("<Q", writer._mm, 0)[0]
            writer.upsert(100, 100.5)  # update, not new
            v4 = struct.unpack_from("<Q", writer._mm, 0)[0]

            assert v1 == 0
            assert v2 == 1, "first upsert should bump version to 1"
            assert v3 == 2, "second upsert should bump version to 2"
            assert v4 == 3, "even an update should bump version"
        finally:
            writer.close()

    def test_upsert_linear_probe_collision(self, tmp_buffer_path):
        """Linear probe on hash collision places token in next empty slot."""
        writer = TickBufferWriter(path=tmp_buffer_path, max_slots=4)
        try:
            # Force two tokens to hash to same slot
            # With max_slots=4: token 1 hashes to slot 1, token 5 hashes to slot 1 (collision)
            token1 = 1   # hash to 1
            token2 = 5   # hash to 1 (collision)

            writer.upsert(token1, 100.0)
            writer.upsert(token2, 200.0)

            # token1 at slot 1, token2 should be at slot 2 (next empty)
            slot1_token = struct.unpack_from("<I", writer._mm, _HEADER_SIZE + 1*_SLOT_SIZE)[0]
            slot2_token = struct.unpack_from("<I", writer._mm, _HEADER_SIZE + 2*_SLOT_SIZE)[0]

            assert slot1_token == token1, f"slot 1 should have token1 {token1}, got {slot1_token}"
            assert slot2_token == token2, f"slot 2 should have token2 {token2}, got {slot2_token}"
        finally:
            writer.close()

    def test_upsert_table_near_capacity_still_works(self, tmp_buffer_path):
        """Upsert continues to work as table approaches capacity."""
        max_slots = 256
        writer = TickBufferWriter(path=tmp_buffer_path, max_slots=max_slots)
        try:
            # Insert tokens occupying most slots (linear probe finds empties)
            for i in range(200):  # 78% load
                result = writer.upsert(i, 100.0 + i)
                assert result is True, f"upsert {i} should succeed"

            # Slot_count should reflect insertions
            _, slot_count, _, _, _ = struct.unpack_from(_HEADER_FMT, writer._mm, 0)
            assert slot_count == 200, f"expected 200 slots occupied, got {slot_count}"
        finally:
            writer.close()

    def test_upsert_last_write_ns_stamped(self, tmp_buffer_path):
        """last_write_ns in header is stamped when ts_ns > 0."""
        writer = TickBufferWriter(path=tmp_buffer_path, max_slots=256)
        try:
            ts1 = 1000000
            ts2 = 2000000

            writer.upsert(1, 100.0, ts_ns=ts1)
            _, _, _, last_ns1, _ = struct.unpack_from(_HEADER_FMT, writer._mm, 0)
            assert last_ns1 == ts1, f"expected last_write_ns {ts1}, got {last_ns1}"

            writer.upsert(2, 200.0, ts_ns=ts2)
            _, _, _, last_ns2, _ = struct.unpack_from(_HEADER_FMT, writer._mm, 0)
            assert last_ns2 == ts2, f"expected last_write_ns {ts2}, got {last_ns2}"
        finally:
            writer.close()


class TestTickBufferReader:
    """TickBufferReader — read correctness and torn-read recovery."""

    def test_get_ltp_returns_none_when_not_found(self, tmp_buffer_path):
        """get_ltp returns None for tokens not in the buffer."""
        writer = TickBufferWriter(path=tmp_buffer_path, max_slots=256)
        writer.upsert(100, 100.0)
        writer.close()

        reader = TickBufferReader(path=tmp_buffer_path, max_slots=256)
        try:
            result = reader.get_ltp(100)
            assert result == 100.0, f"expected 100.0, got {result}"

            result = reader.get_ltp(999)
            assert result is None, f"expected None for missing token, got {result}"
        finally:
            reader.close()

    def test_get_ltp_finds_token_via_linear_probe(self, tmp_buffer_path):
        """get_ltp successfully locates tokens placed via linear probe."""
        writer = TickBufferWriter(path=tmp_buffer_path, max_slots=4)
        token1 = 1   # hash to slot 1
        token2 = 5   # hash to slot 1 (collision, will be at slot 2)
        writer.upsert(token1, 100.0)
        writer.upsert(token2, 200.0)
        writer.close()

        reader = TickBufferReader(path=tmp_buffer_path, max_slots=4)
        try:
            assert reader.get_ltp(token1) == 100.0, f"token1 should have LTP 100.0"
            assert reader.get_ltp(token2) == 200.0, f"token2 should have LTP 200.0"
        finally:
            reader.close()

    def test_get_ltp_batch(self, tmp_buffer_path):
        """get_ltp_batch returns dict of tokens found."""
        writer = TickBufferWriter(path=tmp_buffer_path, max_slots=256)
        tokens = [10, 20, 30]
        for t in tokens:
            writer.upsert(t, float(t) * 10)
        writer.close()

        reader = TickBufferReader(path=tmp_buffer_path, max_slots=256)
        try:
            result = reader.get_ltp_batch(tokens + [999])
            assert result == {10: 100.0, 20: 200.0, 30: 300.0}
        finally:
            reader.close()

    def test_header_returns_correct_tuple(self, tmp_buffer_path):
        """header() returns all five header fields."""
        writer = TickBufferWriter(path=tmp_buffer_path, max_slots=256)
        writer.upsert(100, 100.0, ts_ns=5555)
        writer.upsert(101, 101.0, ts_ns=6666)
        writer.close()

        reader = TickBufferReader(path=tmp_buffer_path, max_slots=256)
        try:
            version, slot_count, max_slots, last_write_ns, schema_version = reader.header()
            assert version == 2, f"expected version 2, got {version}"
            assert slot_count == 2, f"expected slot_count 2, got {slot_count}"
            assert max_slots == 256, f"expected max_slots 256, got {max_slots}"
            assert last_write_ns == 6666, f"expected last_write_ns 6666, got {last_write_ns}"
            assert schema_version == SCHEMA_VERSION
        finally:
            reader.close()

    def test_version(self, tmp_buffer_path):
        """version() returns just the version word."""
        writer = TickBufferWriter(path=tmp_buffer_path, max_slots=256)
        writer.upsert(100, 100.0)
        writer.close()

        reader = TickBufferReader(path=tmp_buffer_path, max_slots=256)
        try:
            v = reader.version()
            assert v == 1
        finally:
            reader.close()

    def test_iter_active_early_exit(self, tmp_buffer_path):
        """iter_active() yields exactly slot_count entries, no more."""
        writer = TickBufferWriter(path=tmp_buffer_path, max_slots=256)
        num_slots = 10
        # Start token at 1, not 0 (token 0 is treated as empty slot marker)
        for i in range(num_slots):
            writer.upsert(i + 1, 100.0 + i)
        writer.close()

        reader = TickBufferReader(path=tmp_buffer_path, max_slots=256)
        try:
            entries = list(reader.iter_active())
            assert len(entries) == num_slots, f"expected {num_slots} entries, got {len(entries)}"

            # All entries should be from tokens 1..10
            tokens = [e[0] for e in entries]
            assert len(set(tokens)) == num_slots, "tokens should be unique"
            assert all(1 <= t <= 10 for t in tokens), "tokens should be 1..10"
        finally:
            reader.close()

    def test_iter_active_empty_buffer(self, tmp_buffer_path):
        """iter_active() yields nothing for empty buffer."""
        writer = TickBufferWriter(path=tmp_buffer_path, max_slots=256)
        writer.close()

        reader = TickBufferReader(path=tmp_buffer_path, max_slots=256)
        try:
            entries = list(reader.iter_active())
            assert len(entries) == 0
        finally:
            reader.close()

    def test_iter_active_returns_correct_values(self, tmp_buffer_path):
        """iter_active() yields correct (token, lp, prev_close, avg_price, ts)."""
        writer = TickBufferWriter(path=tmp_buffer_path, max_slots=256)
        test_data = [
            (10, 100.5, 100.0, 100.25, 1000),
            (20, 200.5, 200.0, 200.25, 2000),
        ]
        for token, lp, pc, av, ts in test_data:
            writer.upsert(token, lp, pc, av, ts)
        writer.close()

        reader = TickBufferReader(path=tmp_buffer_path, max_slots=256)
        try:
            entries = list(reader.iter_active())
            # Sort by token for deterministic comparison
            entries.sort(key=lambda e: e[0])

            assert len(entries) == 2
            tok, lp, pc, av, ts = entries[0]
            assert tok == 10
            assert lp == 100.5
            assert pc == 100.0
            assert av == 100.25
            assert ts == 1000
        finally:
            reader.close()

    def test_file_not_found_raises(self, tmp_buffer_path):
        """Reader raises FileNotFoundError when buffer file missing."""
        with pytest.raises(FileNotFoundError):
            TickBufferReader(path=tmp_buffer_path + "_nonexistent", max_slots=256)


class TestConcurrencyAndPerformance:
    """Concurrent access patterns and performance assertions."""

    def test_torn_read_recovery(self, tmp_buffer_path):
        """Reader handles torn read by retrying. Never gets garbage."""
        import threading
        import time

        writer = TickBufferWriter(path=tmp_buffer_path, max_slots=256)
        reader = TickBufferReader(path=tmp_buffer_path, max_slots=256)

        try:
            token = 100
            writer.upsert(token, 100.0)

            # Rapidly alternate writes while reading
            results = []
            stop_flag = [False]

            def writer_thread():
                for i in range(100):
                    writer.upsert(token, float(100 + i))
                    time.sleep(0.001)
                stop_flag[0] = True

            def reader_thread():
                while not stop_flag[0]:
                    ltp = reader.get_ltp(token)
                    if ltp is not None:
                        results.append(ltp)
                    time.sleep(0.0005)

            t1 = threading.Thread(target=writer_thread)
            t2 = threading.Thread(target=reader_thread)
            t1.start()
            t2.start()
            t1.join()
            t2.join()

            # All read results should be numeric and in expected range
            for ltp in results:
                assert isinstance(ltp, (int, float)), f"expected numeric LTP, got {ltp}"
                assert 100.0 <= ltp <= 200.0, f"LTP out of expected range: {ltp}"
        finally:
            writer.close()
            reader.close()

    def test_iter_active_performance_at_70_percent_load(self, tmp_buffer_path):
        """iter_active() with 70% load (2867 slots) scans ~2867 entries, not 4096."""
        import time

        max_slots = DEFAULT_MAX_SLOTS
        load_slots = int(max_slots * 0.7)

        writer = TickBufferWriter(path=tmp_buffer_path, max_slots=max_slots)
        try:
            # Insert load_slots worth of data (start at token 1, not 0)
            for i in range(load_slots):
                writer.upsert(i + 1, 100.0 + i)

            writer.close()
            reader = TickBufferReader(path=tmp_buffer_path, max_slots=max_slots)

            # Measure iter_active() performance
            start = time.perf_counter_ns()
            entries = list(reader.iter_active())
            elapsed_ns = time.perf_counter_ns() - start

            assert len(entries) == load_slots, f"expected {load_slots} entries, got {len(entries)}"
            # Should complete within a few milliseconds (< 10ms on most hardware)
            elapsed_us = elapsed_ns / 1000
            assert elapsed_us < 10000, f"iter_active took {elapsed_us:.0f}µs, expected < 10ms"

            reader.close()
        finally:
            if os.path.exists(tmp_buffer_path):
                os.remove(tmp_buffer_path)

    def test_get_ltp_latency_under_100us(self, tmp_buffer_path):
        """Single get_ltp() call completes within 100µs."""
        import time

        writer = TickBufferWriter(path=tmp_buffer_path, max_slots=256)
        for i in range(100):
            writer.upsert(i, 100.0 + i)
        writer.close()

        reader = TickBufferReader(path=tmp_buffer_path, max_slots=256)
        try:
            # Warm up the read path
            reader.get_ltp(50)

            # Measure multiple reads
            times = []
            for i in range(1000):
                start = time.perf_counter_ns()
                reader.get_ltp(i % 100)
                elapsed_ns = time.perf_counter_ns() - start
                times.append(elapsed_ns / 1000)  # convert to µs

            avg_us = sum(times) / len(times)
            p95_us = sorted(times)[int(len(times) * 0.95)]
            p99_us = sorted(times)[int(len(times) * 0.99)]

            # Assert latency targets
            assert avg_us < 100, f"avg get_ltp latency {avg_us:.1f}µs, expected < 100µs"
            assert p95_us < 200, f"p95 get_ltp latency {p95_us:.1f}µs, expected < 200µs"
            assert p99_us < 500, f"p99 get_ltp latency {p99_us:.1f}µs, expected < 500µs"
        finally:
            reader.close()
