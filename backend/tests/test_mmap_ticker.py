"""Tests for MmapTickReader — the thin reader that lives in main API.

Coverage:
  • MmapTickReader.get_ltp() returns None when buffer missing
  • Local sym→token cache behavior
  • BroadcastBus passthrough and lifecycle
"""

import tempfile
import os
import asyncio
from unittest.mock import patch, MagicMock, AsyncMock

import pytest

from backend.brokers.tick_buffer import TickBufferWriter
from backend.brokers.mmap_ticker import MmapTickReader


@pytest.fixture
def tmp_buffer_path():
    """Temporary buffer file path for tests (file won't exist initially)."""
    path = tempfile.mktemp(prefix="mmap_test_")
    yield path
    if os.path.exists(path):
        os.remove(path)


class TestMmapTickReaderBasics:
    """Basic read operations."""

    def test_get_ltp_returns_none_when_file_missing(self, tmp_buffer_path):
        """get_ltp() returns None when buffer file doesn't exist."""
        reader = MmapTickReader(path=tmp_buffer_path)

        result = reader.get_ltp(100)
        assert result is None, "get_ltp should return None when file missing"

    def test_get_ltp_returns_value_when_file_exists(self, tmp_buffer_path):
        """get_ltp() returns LTP after buffer file appears."""
        # Create the buffer with DEFAULT_MAX_SLOTS (4096) so MmapTickReader
        # can open it without specifying max_slots parameter
        from backend.brokers.tick_buffer import DEFAULT_MAX_SLOTS
        writer = TickBufferWriter(path=tmp_buffer_path, max_slots=DEFAULT_MAX_SLOTS)
        writer.upsert(100, 150.5)
        writer.close()

        # Create reader after buffer is ready
        reader = MmapTickReader(path=tmp_buffer_path)
        result = reader.get_ltp(100)
        assert result == 150.5

    def test_get_ltp_batch(self, tmp_buffer_path):
        """get_ltp_batch() returns dict of found tokens."""
        # Create the buffer with DEFAULT_MAX_SLOTS
        from backend.brokers.tick_buffer import DEFAULT_MAX_SLOTS
        writer = TickBufferWriter(path=tmp_buffer_path, max_slots=DEFAULT_MAX_SLOTS)
        writer.upsert(100, 150.5)
        writer.upsert(200, 250.5)
        writer.close()

        # Now open the reader to an already-existing buffer
        reader = MmapTickReader(path=tmp_buffer_path)
        result = reader.get_ltp_batch([100, 200, 999])

        assert result == {100: 150.5, 200: 250.5}

    def test_get_ltp_by_sym_uses_local_cache(self, tmp_buffer_path):
        """get_ltp_by_sym() uses local sym→token cache."""
        # Create buffer with DEFAULT_MAX_SLOTS
        from backend.brokers.tick_buffer import DEFAULT_MAX_SLOTS
        writer = TickBufferWriter(path=tmp_buffer_path, max_slots=DEFAULT_MAX_SLOTS)
        writer.upsert(100, 150.5)
        writer.upsert(200, 250.5)
        writer.close()

        # Now create reader and populate local cache
        reader = MmapTickReader(path=tmp_buffer_path)
        reader._sym_to_token["NIFTY50"] = 100
        reader._sym_to_token["BANKNIFTY"] = 200

        assert reader.get_ltp_by_sym("NIFTY50") == 150.5
        assert reader.get_ltp_by_sym("BANKNIFTY") == 250.5
        assert reader.get_ltp_by_sym("UNKNOWN") is None


class TestMmapTickReaderSubscription:
    """Subscription and local cache management."""

    def test_local_sym_to_token_cache(self, tmp_buffer_path):
        """Local sym→token cache is accessible and updatable."""
        reader = MmapTickReader(path=tmp_buffer_path)

        # Manually populate cache
        reader._sym_to_token["NIFTY50"] = 100
        reader._token_to_sym[100] = "NIFTY50"

        assert reader._sym_to_token["NIFTY50"] == 100
        assert reader._token_to_sym[100] == "NIFTY50"


class TestMmapTickReaderBusPassthrough:
    """BroadcastBus passthrough."""

    def test_bus_returns_broadcast_bus(self, tmp_buffer_path):
        """bus() returns the local BroadcastBus instance."""
        reader = MmapTickReader(path=tmp_buffer_path)
        bus = reader.bus()
        assert bus is not None

    def test_set_loop_sets_asyncio_loop(self, tmp_buffer_path):
        """set_loop() sets the event loop."""
        reader = MmapTickReader(path=tmp_buffer_path)
        loop = asyncio.new_event_loop()
        try:
            reader.set_loop(loop)
            assert reader._loop is loop
        finally:
            loop.close()


class TestMmapTickReaderLifecycle:
    """Lifecycle management."""

    def test_start_creates_poller_task(self, tmp_buffer_path):
        """start() creates the polling task."""
        reader = MmapTickReader(path=tmp_buffer_path)
        loop = asyncio.new_event_loop()
        try:
            reader.set_loop(loop)
            reader.start()

            assert reader._poll_task is not None

            reader.stop()
        finally:
            loop.close()

    def test_stop_cancels_poller_task(self, tmp_buffer_path):
        """stop() cancels the polling task."""
        reader = MmapTickReader(path=tmp_buffer_path)
        loop = asyncio.new_event_loop()
        try:
            reader.set_loop(loop)
            reader.start()
            poll_task = reader._poll_task

            reader.stop()

            assert reader._poll_task is None
        finally:
            loop.close()

    def test_ensure_started_idempotent(self, tmp_buffer_path):
        """ensure_started() is idempotent — second call doesn't recreate."""
        reader = MmapTickReader(path=tmp_buffer_path)
        loop = asyncio.new_event_loop()
        try:
            reader.set_loop(loop)
            reader.ensure_started()
            task1 = reader._poll_task

            reader.ensure_started()
            task2 = reader._poll_task

            assert task1 is task2

            reader.stop()
        finally:
            loop.close()
