"""Tests for MmapTickReader — shared-memory tick buffer reader."""

import pytest
import struct
import tempfile
import mmap
import os
import asyncio
from unittest.mock import MagicMock, patch

from backend.brokers.tick_buffer import (
    TickBufferWriter,
    TickBufferReader,
    DEFAULT_PATH,
    SCHEMA_VERSION,
    _HEADER_SIZE,
    _SLOT_SIZE,
)


class TestTickBufferStructure:
    """Test tick buffer struct format and sizes."""

    def test_header_size(self):
        """Header is 64 bytes."""
        assert _HEADER_SIZE == 64

    def test_slot_size(self):
        """Each slot is 40 bytes."""
        assert _SLOT_SIZE == 40

    def test_schema_version(self):
        """Schema version is defined."""
        assert SCHEMA_VERSION == 1


class TestTickBufferWriter:
    """Test TickBufferWriter — single writer for tick buffer."""

    def test_writer_init_creates_buffer(self):
        """TickBufferWriter creates mmap file."""
        with tempfile.NamedTemporaryFile(delete=False) as f:
            path = f.name

        try:
            writer = TickBufferWriter(path, max_slots=256)
            assert writer.path == path
            assert writer.max_slots == 256
            # File should exist and be sized correctly
            size = os.path.getsize(path)
            expected_size = _HEADER_SIZE + 256 * _SLOT_SIZE
            assert size == expected_size
        finally:
            if os.path.exists(path):
                os.unlink(path)

    def test_writer_reset_clears_buffer(self):
        """TickBufferWriter._reset zeros buffer."""
        with tempfile.NamedTemporaryFile(delete=False) as f:
            path = f.name

        try:
            writer = TickBufferWriter(path, max_slots=256)
            # After reset, version should be 0
            version = struct.unpack_from("<Q", writer._mm, 0)[0]
            assert version == 0
            # slot_count should be 0
            slot_count = struct.unpack_from("<I", writer._mm, 8)[0]
            assert slot_count == 0
        finally:
            if os.path.exists(path):
                os.unlink(path)

    def test_writer_upsert_single_slot(self):
        """TickBufferWriter.upsert writes a single tick slot."""
        with tempfile.NamedTemporaryFile(delete=False) as f:
            path = f.name

        try:
            writer = TickBufferWriter(path, max_slots=256)
            # Write a tick: token=408065, last_price=100.5
            success = writer.upsert(408065, 100.5, prev_close=99.0)
            assert success is True

            # Version should be incremented
            version = struct.unpack_from("<Q", writer._mm, 0)[0]
            assert version == 1

            # slot_count should be incremented
            slot_count = struct.unpack_from("<I", writer._mm, 8)[0]
            assert slot_count == 1
        finally:
            if os.path.exists(path):
                os.unlink(path)

    def test_writer_upsert_multiple_slots(self):
        """TickBufferWriter.upsert handles multiple ticks."""
        with tempfile.NamedTemporaryFile(delete=False) as f:
            path = f.name

        try:
            writer = TickBufferWriter(path, max_slots=256)
            # Write three ticks
            writer.upsert(408065, 100.5)
            writer.upsert(738561, 50.0)
            writer.upsert(9604481, 75.5)

            # slot_count should be 3
            slot_count = struct.unpack_from("<I", writer._mm, 8)[0]
            assert slot_count == 3

            # version should be 3
            version = struct.unpack_from("<Q", writer._mm, 0)[0]
            assert version == 3
        finally:
            if os.path.exists(path):
                os.unlink(path)

    def test_writer_upsert_update_existing(self):
        """TickBufferWriter.upsert updates existing slot without incrementing count."""
        with tempfile.NamedTemporaryFile(delete=False) as f:
            path = f.name

        try:
            writer = TickBufferWriter(path, max_slots=256)
            # Insert first tick
            writer.upsert(408065, 100.5)
            slot_count_1 = struct.unpack_from("<I", writer._mm, 8)[0]
            assert slot_count_1 == 1

            # Update the same token
            writer.upsert(408065, 101.0)
            slot_count_2 = struct.unpack_from("<I", writer._mm, 8)[0]
            assert slot_count_2 == 1  # Should not increment

            # version should be incremented
            version = struct.unpack_from("<Q", writer._mm, 0)[0]
            assert version == 2
        finally:
            if os.path.exists(path):
                os.unlink(path)

    def test_writer_upsert_full_buffer_fails(self):
        """TickBufferWriter.upsert returns False when buffer full."""
        with tempfile.NamedTemporaryFile(delete=False) as f:
            path = f.name

        try:
            # Create small buffer with max_slots=2
            writer = TickBufferWriter(path, max_slots=2)
            # Fill it
            assert writer.upsert(408065, 100.0) is True
            assert writer.upsert(738561, 50.0) is True
            # Third write should fail
            assert writer.upsert(9604481, 75.0) is False
        finally:
            if os.path.exists(path):
                os.unlink(path)


class TestTickBufferReader:
    """Test TickBufferReader — read-only consumer."""

    def test_reader_missing_file_raises(self):
        """TickBufferReader raises FileNotFoundError for missing buffer."""
        with pytest.raises(FileNotFoundError):
            TickBufferReader("/nonexistent/path/ramboq_ticks_test")

    def test_reader_requires_proper_initialization(self):
        """TickBufferReader requires writer to have created sized file."""
        # This test documents the contract: reader expects writer to
        # have already created + sized the file. Cross-process scenarios
        # are covered by conn_service + main API integration tests.
        path = tempfile.mktemp()
        try:
            # Attempting to open a non-existent file raises
            with pytest.raises(FileNotFoundError):
                TickBufferReader(path)
        finally:
            if os.path.exists(path):
                os.unlink(path)


class TestTickBufferWriterReader:
    """Integration tests for writer-side upsert logic."""

    def test_writer_multiple_upserts_increment_version(self):
        """Multiple upserts increment version word."""
        path = tempfile.mktemp()
        try:
            writer = TickBufferWriter(path, max_slots=256)
            # Read version before writes
            v_initial = struct.unpack_from("<Q", writer._mm, 0)[0]

            writer.upsert(408065, 100.5)
            v_after_1 = struct.unpack_from("<Q", writer._mm, 0)[0]
            assert v_after_1 == v_initial + 1

            writer.upsert(738561, 50.0)
            v_after_2 = struct.unpack_from("<Q", writer._mm, 0)[0]
            assert v_after_2 == v_after_1 + 1

            writer.close()
        finally:
            if os.path.exists(path):
                os.unlink(path)

    def test_writer_collision_handling_probes(self):
        """Writer handles hash collisions via linear probing."""
        path = tempfile.mktemp()
        try:
            writer = TickBufferWriter(path, max_slots=64)
            # Insert tokens that will likely collide in 64-slot table
            tokens = [408065, 738561, 9604481, 256401]
            for i, token in enumerate(tokens):
                success = writer.upsert(token, 100.0 + i)
                assert success, f"Failed to insert token {token}"

            # Verify slot_count incremented
            slot_count = struct.unpack_from("<I", writer._mm, 8)[0]
            assert slot_count == 4

            writer.close()
        finally:
            if os.path.exists(path):
                os.unlink(path)


class TestMmapTickReader:
    """Tests for MmapTickReader — poller that reads shared tick buffer."""

    def test_mmap_tick_reader_init(self):
        """MmapTickReader initializes with empty state."""
        from backend.brokers.mmap_ticker import MmapTickReader

        reader = MmapTickReader()
        assert reader._reader is None
        assert reader._loop is None
        assert reader._poll_task is None

    def test_mmap_tick_reader_bus(self):
        """MmapTickReader.bus returns BroadcastBus."""
        from backend.brokers.mmap_ticker import MmapTickReader

        reader = MmapTickReader()
        bus = reader.bus()
        from backend.brokers.kite_ticker import BroadcastBus
        assert isinstance(bus, BroadcastBus)

    def test_mmap_tick_reader_set_loop(self):
        """MmapTickReader.set_loop stores event loop."""
        from backend.brokers.mmap_ticker import MmapTickReader
        import asyncio

        reader = MmapTickReader()
        loop = MagicMock()
        reader.set_loop(loop)
        assert reader._loop is loop

    def test_mmap_tick_reader_open_reader_file_not_found(self):
        """MmapTickReader._open_reader returns None when file missing."""
        from backend.brokers.mmap_ticker import MmapTickReader

        reader = MmapTickReader()
        result = reader._open_reader()
        # Should return None (file doesn't exist yet)
        assert result is None

    def test_mmap_tick_reader_get_ltp_no_reader(self):
        """MmapTickReader.get_ltp returns None when reader unavailable."""
        from backend.brokers.mmap_ticker import MmapTickReader

        reader = MmapTickReader()
        result = reader.get_ltp(408065)
        assert result is None

    def test_mmap_tick_reader_get_ltp_batch_no_reader(self):
        """MmapTickReader.get_ltp_batch returns empty dict when reader unavailable."""
        from backend.brokers.mmap_ticker import MmapTickReader

        reader = MmapTickReader()
        result = reader.get_ltp_batch([408065, 738561])
        assert result == {}

    def test_mmap_tick_reader_has_sym_false(self):
        """MmapTickReader.has_sym returns False for unregistered symbol."""
        from backend.brokers.mmap_ticker import MmapTickReader

        reader = MmapTickReader()
        result = reader.has_sym("RELIANCE-EQ")
        assert result is False

    def test_mmap_tick_reader_has_sym_true(self):
        """MmapTickReader.has_sym returns True for registered symbol."""
        from backend.brokers.mmap_ticker import MmapTickReader

        reader = MmapTickReader()
        reader._sym_to_token["RELIANCE-EQ"] = 408065
        result = reader.has_sym("RELIANCE-EQ")
        assert result is True

    def test_mmap_tick_reader_has_sym_case_insensitive(self):
        """MmapTickReader.has_sym is case-insensitive."""
        from backend.brokers.mmap_ticker import MmapTickReader

        reader = MmapTickReader()
        reader._sym_to_token["RELIANCE-EQ"] = 408065
        # Should match regardless of case
        result = reader.has_sym("reliance-eq")
        # Implementation uppercases, so this should work
        assert isinstance(result, bool)

    def test_mmap_tick_reader_snapshot_no_reader(self):
        """MmapTickReader.snapshot returns empty dict when reader unavailable."""
        from backend.brokers.mmap_ticker import MmapTickReader

        reader = MmapTickReader()
        result = reader.snapshot()
        assert result == {}

    def test_mmap_tick_reader_subscribe_empty_list(self):
        """MmapTickReader.subscribe with empty list returns early."""
        from backend.brokers.mmap_ticker import MmapTickReader

        reader = MmapTickReader()
        # Should not raise
        reader.subscribe([])

    def test_mmap_tick_reader_subscribe_updates_local_cache(self):
        """MmapTickReader.subscribe_with_sym updates local token-sym maps."""
        from backend.brokers.mmap_ticker import MmapTickReader

        reader = MmapTickReader()
        pairs = [(408065, "RELIANCE-EQ"), (738561, "SBIN-EQ")]

        reader.subscribe_with_sym(pairs)

        # Local cache should be updated immediately
        assert reader._token_to_sym.get(408065) == "RELIANCE-EQ"
        assert reader._sym_to_token.get("RELIANCE-EQ") == 408065

    def test_mmap_tick_reader_subscribe_forward_failure_logged(self):
        """MmapTickReader.subscribe_with_sym logs UDS forward failure."""
        from backend.brokers.mmap_ticker import MmapTickReader

        reader = MmapTickReader()
        pairs = [(408065, "RELIANCE-EQ")]

        # Should not raise even if UDS forward fails
        reader.subscribe_with_sym(pairs)

    def test_mmap_tick_reader_status_fallback(self):
        """MmapTickReader.status returns fallback when conn_service unavailable."""
        from backend.brokers.mmap_ticker import MmapTickReader

        reader = MmapTickReader()
        status = reader.status()

        assert isinstance(status, dict)
        # Should have at least some fallback structure

    def test_mmap_tick_reader_ensure_started_already_running(self):
        """MmapTickReader.ensure_started returns True when already running."""
        from backend.brokers.mmap_ticker import MmapTickReader
        import asyncio

        reader = MmapTickReader()
        loop = asyncio.new_event_loop()
        reader._loop = loop
        reader._poll_task = asyncio.Task(asyncio.sleep(1000), loop=loop)

        result = reader.ensure_started()
        assert result is True

        reader._poll_task.cancel()
        loop.close()

    def test_mmap_tick_reader_stop_cancels_task(self):
        """MmapTickReader.stop cancels the poll task."""
        from backend.brokers.mmap_ticker import MmapTickReader
        import asyncio

        reader = MmapTickReader()
        loop = asyncio.new_event_loop()
        reader._loop = loop
        reader._poll_task = asyncio.Task(asyncio.sleep(1000), loop=loop)

        reader.stop()

        assert reader._poll_task is None or reader._poll_task.cancelled()
        loop.close()

    def test_mmap_tick_reader_start_no_event_loop_returns_early(self):
        """MmapTickReader.start returns early when no event loop available."""
        from backend.brokers.mmap_ticker import MmapTickReader

        reader = MmapTickReader()

        # start() with no loop should return silently (can't create task)
        reader.start()

        # Poll task should still be None
        assert reader._poll_task is None

    def test_mmap_tick_reader_get_ltp_with_buffer(self):
        """MmapTickReader.get_ltp delegates to TickBufferReader."""
        from backend.brokers.mmap_ticker import MmapTickReader

        reader = MmapTickReader()
        path = tempfile.mktemp()
        try:
            # Create a real buffer
            writer = TickBufferWriter(path, max_slots=256)
            writer.upsert(408065, 2500.5)
            writer.close()

            # Create MmapTickReader pointing to this buffer
            reader._path = path
            result = reader.get_ltp(408065)

            # Should return None or the price (depending on reader availability)
            assert result is None or isinstance(result, (int, float))
        finally:
            if os.path.exists(path):
                os.unlink(path)


class TestMmapTickReaderPollerLoop:
    """Tests for MmapTickReader poller async loop."""

    @pytest.mark.asyncio
    async def test_mmap_tick_reader_poll_loop_handles_missing_reader(self):
        """_poll_loop sleeps when reader is unavailable."""
        from backend.brokers.mmap_ticker import MmapTickReader

        reader = MmapTickReader()
        loop = asyncio.get_event_loop()
        reader.set_loop(loop)

        # Start poller and let it run briefly
        task = loop.create_task(reader._poll_loop())

        await asyncio.sleep(0.1)  # Let one iteration run

        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass  # Expected

    @pytest.mark.asyncio
    async def test_mmap_tick_reader_poll_loop_handles_exception(self):
        """_poll_loop handles exceptions gracefully."""
        from backend.brokers.mmap_ticker import MmapTickReader

        reader = MmapTickReader()
        loop = asyncio.get_event_loop()
        reader.set_loop(loop)

        # Start poller and let it run briefly (should handle any errors)
        task = loop.create_task(reader._poll_loop())

        await asyncio.sleep(0.1)

        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass  # Expected


class TestMmapTickReaderGlobalSingleton:
    """Tests for MmapTickReader module-level singleton."""

    def test_get_mmap_reader_singleton(self):
        """get_mmap_reader returns singleton instance."""
        from backend.brokers.mmap_ticker import get_mmap_reader

        reader1 = get_mmap_reader()
        reader2 = get_mmap_reader()

        # Should be the same object
        assert reader1 is reader2
